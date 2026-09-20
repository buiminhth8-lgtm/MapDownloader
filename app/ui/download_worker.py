"""下载并落盘的后台执行器（Slice 3 → Slice 4 → Slice 5 演进）。

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

Slice 5 的改动：多了一种入口。给 ``resume_task`` 传一个任务记录时，走
``MapDownloadService.resume_task``（跳过已有瓦片）；不传就是原来的全新下载。
两种模式共用同一套信号与暂停/取消路径，因此 UI 侧不需要分支。

暂停 / 取消的时序细节
---------------------

``pause()`` / ``cancel()`` 可能在任务真正启动**之前**就被调用（用户点得快，
或测试点得快）。此时 ``service`` 已经建好但内部 ``manager`` 还不存在，
指令会被记在 ``_pause_requested`` / ``_cancel_requested`` 上，并在
**第一次进度回调**里落地——那一帧恰好发生在 ``DownloadManager`` 的闸门与
取消事件就绪之后，时机刚好。
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
from app.downloader import DownloadProgress, RateLimiter, ResumePolicy, RetryPolicy
from app.geo import DownloadRegion, GeoBounds
from app.mbtiles import MBTilesWriteError
from app.models.download_task import DownloadTask
from app.models.map_layer import MapLayerType, MapType
from app.providers import TiandituProvider
from app.services import MapDownloadService, MapResumeError

__all__ = ["DownloadWorker"]


class DownloadWorker(QThread):
    """在后台线程里跑一次"下载 → 写入 MBTiles"（全新下载或续传）。

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
        layer: "MapLayerType | MapType",
        output_path: str | Path,
        *,
        with_labels: bool = False,
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
        resume_policy: ResumePolicy | None = None,
        resume_task: DownloadTask | None = None,
        region: DownloadRegion | None = None,
        token: str = "",
        task: DownloadTask | None = None,
        task_service=None,
        parent: QObject | None = None,
    ) -> None:
        """
        Args:
            provider: 瓦片源（持有 Token，不对外展示）。
            bounds: 下载范围（续传模式下取自任务记录）。
            min_zoom: 起始缩放级别。
            max_zoom: 结束缩放级别（含）。
            layer: 下载目标。``MapLayerType`` 表示只下这一层；
                ``MapType`` 配合 ``with_labels`` 决定"只下底图"还是"底图 + 注记合成"。
            output_path: 输出的 ``.mbtiles`` 完整路径。
            with_labels: 是否把中文注记合成进瓦片（Slice 6）。为 ``True`` 时
                ``layer`` 必须是 ``MapType`` —— 服务层会据此改用合成下载器。
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
            resume_policy: 跳过策略；``None`` 表示不跳过已有瓦片。
            resume_task: **续传模式**的任务记录。非 ``None`` 时走
                ``service.resume_task``，此时 ``bounds/zoom/layer`` 只是
                冗余信息（服务层会以任务记录为准重新校验）。
            region: 下载区域（Slice 16）。行政区域模式下区域内的瓦片才下，
                区域外的**不进任务总数、不计失败/跳过**。``None`` 或矩形时
                行为与 Slice ≤15 完全一致。续传模式下以任务记录里的区域为准，
                这里传什么都会被忽略。
            token: 续传时用的 Token（任务记录里不存 Token，Spec §9）。
            task: 用于**持续落盘进度**的任务记录（全新下载时由 UI 先创建）。
            task_service: 任务服务，与 ``task`` 成对出现。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._provider = provider
        self._bounds = bounds
        self._min_zoom = min_zoom
        self._max_zoom = max_zoom
        self._layer = layer
        self._output_path = Path(output_path)
        self._with_labels = bool(with_labels)
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
        self._resume_policy = resume_policy
        self._resume_task = resume_task
        self._region = region
        self._token = token
        self._task = task
        self._task_service = task_service

        #: 编排器实例，仅在 worker 线程内被创建 / 使用
        self._service = None
        #: 引擎尚未就绪时暂存的控制指令
        self._pause_requested = False
        self._cancel_requested = False

    # ------------------------------------------------------------------ #
    # 只读状态
    # ------------------------------------------------------------------ #
    @property
    def layer(self) -> "MapLayerType | MapType":
        """本次下载的图层 / 地图类型。"""
        return self._layer

    @property
    def with_labels(self) -> bool:
        """本次是否把中文注记合成进瓦片（Slice 6）。"""
        return self._with_labels

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
    def is_resume(self) -> bool:
        """本次是否是"继续已有任务"。"""
        return self._resume_task is not None

    @property
    def region(self) -> DownloadRegion | None:
        """本次下载的区域（Slice 16）；``None`` 表示纯矩形范围。"""
        return self._region

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
        except MapResumeError as exc:
            # 无法安全续传（参数不匹配 / 缺 Token）：消息本身就是给用户看的
            self.failed.emit(str(exc))
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
            if self._resume_task is not None:
                # 续传：参数以**任务记录**为准，Target 文件里的已有瓦片会被跳过
                outcome = await service.resume_task(
                    self._resume_task,
                    self._token,
                    on_progress=self._on_progress,
                    task_service=self._task_service,
                    resume_policy=self._resume_policy,
                )
            else:
                outcome = await service.download_to_mbtiles(
                    self._bounds,
                    self._min_zoom,
                    self._max_zoom,
                    self._layer,
                    self._output_path,
                    with_labels=self._with_labels,
                    map_name=self._map_name,
                    description=self._description,
                    on_progress=self._on_progress,
                    resume_policy=self._resume_policy,
                    task=self._task,
                    task_service=self._task_service,
                    region=self._region,
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
