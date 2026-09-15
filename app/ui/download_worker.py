"""下载并落盘的后台执行器（Slice 3 → Slice 4 演进）。

这是薄薄的**线程 + 事件循环胶水**：把 ``app.services`` 的协程跑到 UI 主线程
之外，并把进度 / 结果通过 Qt 信号送回主线程。

为什么要额外一层线程：``asyncio`` 需要一个事件循环，而 Qt 主线程的事件循环
（``app.exec()``）不是 asyncio 循环。直接在 ``QThread.run()`` 里 ``asyncio.run``
是最简单、也最不容易出错的接法——不用去桥接两套事件循环。

分工说明：

- 瓦片枚举、并发、限速、重试、响应判定全在 ``app.downloader``；
- 落盘（metadata / 事务 / 单 Writer）全在 ``app.mbtiles``；
- 两者的编排顺序（建库 → 起 sink → 下载 → drain → close → validate）全在
  ``app.services.MapDownloadService``；
- 本类只负责"起线程 → 建 service → 转发信号 → 支持暂停/继续/取消"；
- ``MainWindow`` 只负责"读表单 → 建 worker → 展示"。

Slice 4 的改动只有一处：把上一版的 ``DownloadManager`` 换成了
``MapDownloadService``（后者内部仍然是 ``DownloadManager`` + ``MBTilesTileSink``），
因此暂停/继续/取消的语义、信号形状都保持不变。

暂停 / 取消的时序细节
---------------------

``pause()`` / ``cancel()`` 可能在任务真正启动**之前**就被调用（用户点得快，
或测试点得快）。此时 ``service`` 已经建好但内部 ``manager`` 还不存在，
指令会被记在 ``_pending_*`` 上，并在**第一次进度回调**里落地——那一帧恰好发生在
``DownloadManager`` 的闸门与取消事件就绪之后，时机刚好。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from PySide6.QtCore import QObject, QThread, Signal

from app.config.settings import (
    DEFAULT_DOWNLOAD_CONCURRENCY,
    DEFAULT_MBTILES_BATCH_SIZE,
    DEFAULT_MBTILES_QUEUE_CAPACITY,
    DEFAULT_REQUEST_TIMEOUT,
    PROGRESS_EMIT_INTERVAL_SECONDS,
)
from app.downloader import DownloadProgress, RateLimiter, RetryPolicy
from app.geo import GeoBounds
from app.mbtiles import MBTilesWriteError
from app.models.map_layer import MapLayerType
from app.providers import TiandituProvider
from app.services import MapDownloadService

__all__ = ["DownloadWorker"]


class DownloadWorker(QThread):
    """在后台线程里跑一次"下载 → 写入 MBTiles"。

    信号名刻意避开 ``QThread`` 自带的 ``finished``。
    """

    #: 进度更新（节流后），携带 :class:`~app.downloader.DownloadProgress`
    progress_changed = Signal(object)
    #: 任务结束，携带 :class:`~app.services.MapDownloadOutcome`
    completed = Signal(object)
    #: 任务无法进行或异常终止，携带人话原因
    failed = Signal(str)

    def __init__(
        self,
        provider: TiandituProvider,
        bounds: GeoBounds,
        min_zoom: int,
        max_zoom: int,
        layer: MapLayerType,
        output_path: str | Path,
        *,
        map_name: str | None = None,
        description: str = "",
        concurrency: int = DEFAULT_DOWNLOAD_CONCURRENCY,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        retry_policy: RetryPolicy | None = None,
        rate_limiter: RateLimiter | None = None,
        queue_capacity: int = DEFAULT_MBTILES_QUEUE_CAPACITY,
        batch_size: int = DEFAULT_MBTILES_BATCH_SIZE,
        progress_interval_seconds: float = PROGRESS_EMIT_INTERVAL_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        parent: QObject | None = None,
    ) -> None:
        """
        Args:
            provider: 瓦片源（持有 Token，不对外展示）。
            bounds: 下载范围。
            min_zoom: 起始缩放级别。
            max_zoom: 结束缩放级别（含）。
            layer: 图层类型。
            output_path: 输出的 ``.mbtiles`` 完整路径。
            map_name: MBTiles 的 ``name``；``None`` 时取输出文件名主体。
            description: MBTiles 的 ``description``。
            concurrency: 并发 worker 数。
            timeout: 单请求超时（秒）。
            retry_policy: 重试策略，默认 :class:`RetryPolicy`。
            rate_limiter: 限速器，默认按 ``DEFAULT_REQUESTS_PER_SECOND``；
                传 :meth:`RateLimiter.disabled` 可关闭。
            queue_capacity: MBTiles 写队列容量（有界）。
            batch_size: MBTiles 批量事务大小。
            progress_interval_seconds: 进度信号的最小间隔（秒）。
            transport: 传输层，**仅供测试注入 ``httpx.MockTransport``**。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._provider = provider
        self._bounds = bounds
        self._min_zoom = min_zoom
        self._max_zoom = max_zoom
        self._layer = layer
        self._output_path = Path(output_path)
        self._map_name = map_name
        self._description = description
        self._concurrency = concurrency
        self._timeout = timeout
        self._retry_policy = retry_policy
        self._rate_limiter = rate_limiter
        self._queue_capacity = queue_capacity
        self._batch_size = batch_size
        self._progress_interval = progress_interval_seconds
        self._transport = transport

        #: 编排器实例，仅在 worker 线程内被创建 / 使用
        self._service = None
        #: 引擎尚未就绪时暂存的控制指令
        self._pause_requested = False
        self._cancel_requested = False

    # ------------------------------------------------------------------ #
    # 只读状态
    # ------------------------------------------------------------------ #
    @property
    def layer(self) -> MapLayerType:
        """本次下载的图层。"""
        return self._layer

    @property
    def min_zoom(self) -> int:
        """起始缩放级别。"""
        return self._min_zoom

    @property
    def max_zoom(self) -> int:
        """结束缩放级别。"""
        return self._max_zoom

    @property
    def output_path(self) -> Path:
        """本次任务的输出文件路径。"""
        return self._output_path

    @property
    def pause_requested(self) -> bool:
        """是否已请求暂停（含引擎尚未就绪的情况）。"""
        return self._pause_requested

    # ------------------------------------------------------------------ #
    # 控制（由 UI 主线程调用）
    # ------------------------------------------------------------------ #
    def pause(self) -> None:
        """暂停下载。引擎未就绪时先记下，等首帧进度回调落地。"""
        self._pause_requested = True
        service = self._service
        if service is not None:
            service.pause()

    def resume(self) -> None:
        """继续下载。"""
        self._pause_requested = False
        service = self._service
        if service is not None:
            service.resume()

    def cancel(self) -> None:
        """取消下载。

        取消**不会**丢弃已经下载并进入写队列的瓦片：service 会 drain 队列、
        flush 并关闭数据库，因此磁盘上会留下一个可用的（不完整的）``.mbtiles``。
        """
        self._cancel_requested = True
        service = self._service
        if service is not None:
            service.cancel()

    # ------------------------------------------------------------------ #
    # 线程入口
    # ------------------------------------------------------------------ #
    def run(self) -> None:  # noqa: D102 - QThread 约定的入口
        try:
            asyncio.run(self._main())
        except MBTilesWriteError as exc:
            # 写盘失败（磁盘满 / 库损坏 / 无写权限）——必须让用户看见原话
            self.failed.emit(f"写入 MBTiles 失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 绝不让异常冒泡进 Qt 事件循环
            self.failed.emit(f"下载异常终止：{exc}")

    async def _main(self) -> None:
        """在 worker 线程的独立事件循环里跑完整任务。"""
        service = MapDownloadService(
            self._provider,
            concurrency=self._concurrency,
            timeout=self._timeout,
            retry_policy=self._retry_policy,
            rate_limiter=self._rate_limiter,
            queue_capacity=self._queue_capacity,
            batch_size=self._batch_size,
            progress_interval_seconds=self._progress_interval,
            transport=self._transport,
        )
        self._service = service
        try:
            outcome = await service.download_to_mbtiles(
                self._bounds,
                self._min_zoom,
                self._max_zoom,
                self._layer,
                self._output_path,
                map_name=self._map_name,
                description=self._description,
                on_progress=self._on_progress,
            )
        finally:
            self._service = None
        self.completed.emit(outcome)

    # ------------------------------------------------------------------ #
    # 回调（在 worker 线程的事件循环里执行）
    # ------------------------------------------------------------------ #
    def _on_progress(self, progress: DownloadProgress) -> None:
        """转发进度，并顺手落地那些"来得太早"的控制指令。"""
        service = self._service
        if service is not None:
            if self._pause_requested:
                service.pause()
            if self._cancel_requested:
                service.cancel()
        self.progress_changed.emit(progress)
