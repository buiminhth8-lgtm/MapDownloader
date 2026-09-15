"""批量下载的后台执行器（Slice 3）。

这是薄薄的**线程 + 事件循环胶水**：把 ``app.downloader`` 的协程跑到 UI 主线程
之外，并把进度 / 结果通过 Qt 信号送回主线程。

为什么要额外一层线程：``asyncio`` 需要一个事件循环，而 Qt 主线程的事件循环
（``app.exec()``）不是 asyncio 循环。直接在 ``QThread.run()`` 里 ``asyncio.run``
是最简单、也最不容易出错的接法——不用去桥接两套事件循环。

分工说明：

- 瓦片枚举、并发、限速、重试、响应判定全在 ``app.downloader``；
- 本类只负责"起线程 → 建引擎 → 转发信号 → 支持暂停/继续/取消"；
- ``MainWindow`` 只负责"读表单 → 建 worker → 展示"。

暂停 / 取消的时序细节
---------------------

``pause()`` / ``cancel()`` 可能在引擎真正启动**之前**就被调用（用户点得快，
或测试点得快）。此时 ``manager`` 还不存在，指令会被记在 ``_pending_*`` 上，
并在**第一次进度回调**里落地——那一帧恰好发生在 ``DownloadManager`` 的闸门与
取消事件就绪之后，时机刚好。
"""

from __future__ import annotations

import asyncio

from PySide6.QtCore import QObject, QThread, Signal

from app.config.settings import (
    DEFAULT_DOWNLOAD_CONCURRENCY,
    DEFAULT_REQUEST_TIMEOUT,
    PROGRESS_EMIT_INTERVAL_SECONDS,
)
from app.downloader import (
    DownloadManager,
    DownloadProgress,
    DownloadSummary,
    RateLimiter,
    RetryPolicy,
    TileDownloader,
)
from app.geo import GeoBounds
from app.models.map_layer import MapLayerType
from app.providers import TiandituProvider

__all__ = ["DownloadWorker"]


class DownloadWorker(QThread):
    """在后台线程里跑一次批量下载。

    信号名刻意避开 ``QThread`` 自带的 ``finished``。
    """

    #: 进度更新（节流后），携带 :class:`~app.downloader.DownloadProgress`
    progress_changed = Signal(object)
    #: 下载正常结束，携带 :class:`~app.downloader.DownloadSummary`
    completed = Signal(object)
    #: 下载无法进行或异常终止，携带人话原因
    failed = Signal(str)
    #: 需要写进 UI 日志的消息，(message, level)
    log_message = Signal(str, str)

    def __init__(
        self,
        provider: TiandituProvider,
        bounds: GeoBounds,
        min_zoom: int,
        max_zoom: int,
        layer: MapLayerType,
        *,
        concurrency: int = DEFAULT_DOWNLOAD_CONCURRENCY,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        retry_policy: RetryPolicy | None = None,
        rate_limiter: RateLimiter | None = None,
        progress_interval_seconds: float = PROGRESS_EMIT_INTERVAL_SECONDS,
        parent: QObject | None = None,
    ) -> None:
        """
        Args:
            provider: 瓦片源（持有 Token，不对外展示）。
            bounds: 下载范围。
            min_zoom: 起始缩放级别。
            max_zoom: 结束缩放级别（含）。
            layer: 图层类型。
            concurrency: 并发 worker 数。
            timeout: 单请求超时（秒）。
            retry_policy: 重试策略，默认 :class:`RetryPolicy`。
            rate_limiter: 限速器，默认按 ``DEFAULT_REQUESTS_PER_SECOND``；
                传 :meth:`RateLimiter.disabled` 可关闭。
            progress_interval_seconds: 进度信号的最小间隔（秒）。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._provider = provider
        self._bounds = bounds
        self._min_zoom = min_zoom
        self._max_zoom = max_zoom
        self._layer = layer
        self._concurrency = concurrency
        self._timeout = timeout
        self._retry_policy = retry_policy
        self._rate_limiter = rate_limiter
        self._progress_interval = progress_interval_seconds

        #: 引擎实例，仅在 worker 线程内被创建 / 使用
        self._manager: DownloadManager | None = None
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
    def pause_requested(self) -> bool:
        """是否已请求暂停（含引擎尚未就绪的情况）。"""
        return self._pause_requested

    # ------------------------------------------------------------------ #
    # 控制（由 UI 主线程调用）
    # ------------------------------------------------------------------ #
    def pause(self) -> None:
        """暂停下载。引擎未就绪时先记下，等首帧进度回调落地。"""
        self._pause_requested = True
        manager = self._manager
        if manager is not None:
            manager.pause()

    def resume(self) -> None:
        """继续下载。"""
        self._pause_requested = False
        manager = self._manager
        if manager is not None:
            manager.resume()

    def cancel(self) -> None:
        """取消下载。引擎未就绪时先记下。"""
        self._cancel_requested = True
        manager = self._manager
        if manager is not None:
            manager.cancel()

    # ------------------------------------------------------------------ #
    # 线程入口
    # ------------------------------------------------------------------ #
    def run(self) -> None:  # noqa: D102 - QThread 约定的入口
        try:
            asyncio.run(self._main())
        except Exception as exc:  # noqa: BLE001 - 绝不让异常冒泡进 Qt 事件循环
            self.failed.emit(f"下载异常终止：{exc}")

    async def _main(self) -> None:
        """在 worker 线程的独立事件循环里跑完整任务。"""
        downloader = TileDownloader(
            self._provider,
            retry_policy=self._retry_policy,
            rate_limiter=self._rate_limiter,
            timeout=self._timeout,
        )
        try:
            manager = DownloadManager(
                downloader,
                concurrency=self._concurrency,
                progress_interval_seconds=self._progress_interval,
            )
            self._manager = manager
            summary = await manager.download(
                self._bounds,
                self._min_zoom,
                self._max_zoom,
                self._layer,
                # 本 Slice 不落盘：不传 sink，只统计。Slice 4 的 MBTilesWriter
                # 会作为 on_tile 挂在这里。
                on_tile=None,
                on_progress=self._on_progress,
            )
        finally:
            await downloader.close()
            self._manager = None
        self.completed.emit(summary)

    # ------------------------------------------------------------------ #
    # 回调（在 worker 线程的事件循环里执行）
    # ------------------------------------------------------------------ #
    def _on_progress(self, progress: DownloadProgress) -> None:
        """转发进度，并顺手落地那些"来得太早"的控制指令。"""
        if self._pause_requested and self._manager is not None:
            self._manager.pause()
        if self._cancel_requested and self._manager is not None:
            self._manager.cancel()
        self.progress_changed.emit(progress)
