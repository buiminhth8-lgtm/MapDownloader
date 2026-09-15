"""批量瓦片下载调度（Slice 3）。

结构（与 Spec 的推荐拓扑一致）::

    Tile Producer (协程，惰性生成)
          ↓
      asyncio.Queue  ← 有界，容量 = concurrency * 4
          ↓
    Worker 1 ─┐
    Worker 2 ─┤
    Worker 3 ─┼→ on_tile(result)     # 成功后立刻交出去，永不囤积
    Worker 4 ─┤
    Worker 5 ─┤
    Worker 6 ─┘
          ↓
      on_progress(progress)          # 节流到 ~6.7 次/秒

关键约束（都是有意为之）
------------------------

- **固定数量的 worker**，绝不为每张瓦片创建一个 Task：十万张瓦片就是十万个
  Task，光调度开销就能拖垮事件循环。
- **有界队列 + 惰性生产者**：生产者会被队列容量反压，因此内存占用与总瓦片数
  无关。这也是"不保存全部瓦片到 list 后再返回"的实现方式。
- **边下载边消费**：成功瓦片立刻通过 ``on_tile`` 交出去，本模块不持有瓦片字节。
- 本模块**不写磁盘、不建库**：落盘是 Slice 4 的 MBTilesWriter 的事。

暂停 / 取消的语义
-----------------

- ``pause()``：worker 在**发起下一个请求之前**等闸门。已经发出的请求允许跑完，
  不会被中断（中断只会浪费已消耗的配额）。
- ``resume()``：放行闸门。
- ``cancel()``：停止生产新瓦片，并**同时抬起闸门**让 worker 能继续消费队列里
  的残留项直至哨兵退出——否则 worker 会卡在暂停状态上，生产者也会因队列满
  而永久阻塞在 ``put`` 上。

三个方法都可以从**其它线程**调用（Qt 主线程会这么做），内部通过
``loop.call_soon_threadsafe`` 投递到任务所在的事件循环。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import Counter, deque
from collections.abc import AsyncIterator, Awaitable, Iterator
from dataclasses import dataclass, field

from app.config.settings import (
    DEFAULT_DOWNLOAD_CONCURRENCY,
    DEFAULT_REQUEST_TIMEOUT,
    MAX_DOWNLOAD_CONCURRENCY,
    MAX_LOGGED_TILE_FAILURES,
    MIN_DOWNLOAD_CONCURRENCY,
    PROGRESS_EMIT_INTERVAL_SECONDS,
    PROGRESS_SPEED_WINDOW_SECONDS,
    QUEUE_CAPACITY_FACTOR,
)
from app.downloader.models import (
    DownloadProgress,
    DownloadSummary,
    ProgressCallback,
    TileCallback,
    TileCoordinate,
    TileDownloadResult,
    TileFailureKind,
)
from app.downloader.tile_downloader import TileDownloader
from app.geo import GeoBounds, TileRange, calculate_tile_ranges
from app.models.map_layer import MapLayerType

__all__ = ["DownloadManager", "iter_tile_coordinates"]


_logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 瓦片枚举
# --------------------------------------------------------------------------- #
def iter_tile_coordinates(
    bounds: GeoBounds,
    min_zoom: int,
    max_zoom: int,
) -> Iterator[TileCoordinate]:
    """按 ``z`` 从低到高枚举覆盖范围内的每一张瓦片（**生成器**）。

    生成器而非列表：总瓦片数可以是千万级，先materialize 成 list 就违背了
    "边下载边消费"的初衷。

    顺序为 ``z`` 优先、``y`` 次之、``x`` 最后。低缩放先行有个实际好处：
    用户能很快看到若干个完整层级的成果，而不是等了半小时才拿到第一张。

    Args:
        bounds: 地理范围。
        min_zoom: 起始缩放级别。
        max_zoom: 结束缩放级别（含）。

    Yields:
        :class:`TileCoordinate`，顺序确定（同样的输入永远得到同样的序列）。

    Raises:
        ValueError: zoom 非法或 ``min_zoom > max_zoom``（由 Slice 1 的
            :func:`app.geo.calculate_tile_ranges` 抛出）。
    """
    for tile_range in calculate_tile_ranges(bounds, min_zoom, max_zoom):
        for y in range(tile_range.min_y, tile_range.max_y + 1):
            for x in range(tile_range.min_x, tile_range.max_x + 1):
                yield TileCoordinate(z=tile_range.zoom, x=x, y=y)


# --------------------------------------------------------------------------- #
# 回调工具
# --------------------------------------------------------------------------- #
async def _maybe_await(value: object) -> None:
    """允许回调是普通函数或协程函数。

    返回值只在是 awaitable 时才被 await —— 这样 ``on_tile=lambda r: None``
    与 ``async def on_tile(r): ...`` 都能直接用，调用方不必先判断自己是不是
    协程函数。
    """
    if inspect.isawaitable(value):
        await value  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
@dataclass
class _Counters:
    """下载过程中的可变统计（只在事件循环内被访问，无需加锁）。"""

    total: int
    succeeded: int = 0
    failed: int = 0
    bytes_downloaded: int = 0
    skipped: int = 0
    current_zoom: int | None = None
    #: 失败原因 → 张数。只存人话原因（不含 Token / 完整 URL）。
    failure_reasons: Counter[str] = field(default_factory=Counter)

    @property
    def completed(self) -> int:
        return self.succeeded + self.failed


# --------------------------------------------------------------------------- #
# 管理器
# --------------------------------------------------------------------------- #
class DownloadManager:
    """批量下载调度器：瓦片枚举 → 队列 → 固定 worker → 回调。

    一个实例同一时刻只跑一个任务；``pause`` / ``resume`` / ``cancel``
    作用于当前任务。
    """

    def __init__(
        self,
        downloader: TileDownloader,
        *,
        concurrency: int = DEFAULT_DOWNLOAD_CONCURRENCY,
        progress_interval_seconds: float = PROGRESS_EMIT_INTERVAL_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            downloader: 单瓦片下载器（持有复用的 ``AsyncClient``）。
            concurrency: worker 数量，必须落在
                ``[MIN_DOWNLOAD_CONCURRENCY, MAX_DOWNLOAD_CONCURRENCY]``。
            progress_interval_seconds: 进度回调的最小间隔（秒）。
            logger: 日志器，默认用本模块的 logger。

        Raises:
            ValueError: 并发数越界或进度间隔为负。
        """
        if not MIN_DOWNLOAD_CONCURRENCY <= concurrency <= MAX_DOWNLOAD_CONCURRENCY:
            raise ValueError(
                f"concurrency 必须在 [{MIN_DOWNLOAD_CONCURRENCY}, "
                f"{MAX_DOWNLOAD_CONCURRENCY}] 之间，实际为 {concurrency}"
            )
        if progress_interval_seconds < 0:
            raise ValueError(
                f"progress_interval_seconds 不能为负，实际为 {progress_interval_seconds}"
            )

        self._downloader = downloader
        self._concurrency = concurrency
        self._progress_interval = progress_interval_seconds
        self._logger = logger or _logger

        # --- 当前任务的运行时状态（每次 download() 重置） ---
        self._loop: asyncio.AbstractEventLoop | None = None
        self._gate: asyncio.Event | None = None
        self._cancel_event: asyncio.Event | None = None
        self._counters = _Counters(total=0)
        self._speed_samples: deque[tuple[float, int]] = deque()
        self._last_emit = 0.0
        self._last_progress = DownloadProgress()
        self._logged_failures = 0
        self._paused = False
        self._cancelled = False
        self._running = False
        #: 本次 download() 传入的进度回调，供 worker 在 _record 里复用
        self._progress_callback: ProgressCallback | None = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # 只读属性
    # ------------------------------------------------------------------ #
    @property
    def concurrency(self) -> int:
        """worker 数量。"""
        return self._concurrency

    @property
    def downloader(self) -> TileDownloader:
        """底层单瓦片下载器。"""
        return self._downloader

    @property
    def is_running(self) -> bool:
        """是否有任务正在运行。

        刻意不用"生产者任务是否 done"来判断：生产者结束只说明瓦片都发完了，
        此时 worker 还在跑，任务并没有结束。
        """
        return self._running

    @property
    def is_paused(self) -> bool:
        """当前是否处于暂停状态。"""
        return self._paused

    @property
    def is_cancelled(self) -> bool:
        """当前任务是否已被要求取消。"""
        return self._cancelled

    @property
    def progress(self) -> DownloadProgress:
        """最近一次进度快照。"""
        return self._last_progress

    # ------------------------------------------------------------------ #
    # 控制（可从其它线程调用）
    # ------------------------------------------------------------------ #
    def pause(self) -> None:
        """暂停：不再发起新的瓦片请求，已发出的请求允许完成。"""
        self._request(lambda: self._set_paused(True))

    def resume(self) -> None:
        """恢复：放行闸门。"""
        self._request(lambda: self._set_paused(False))

    def cancel(self) -> None:
        """取消：停止生产新瓦片，并让 worker 消费完残留项后正常退出。"""
        self._request(self._do_cancel)

    def _request(self, action: Callable[[], None]) -> None:
        """把状态变更投递到任务所在的事件循环（必要时跨线程）。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            # 没有正在运行的任务：什么都不用做，避免把"暂停"状态遗留到下一次任务。
            self._logger.debug("下载任务未运行，忽略控制指令")
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            action()
        else:
            loop.call_soon_threadsafe(action)

    def _set_paused(self, paused: bool) -> None:
        """在事件循环内真正改变暂停状态。"""
        gate = self._gate
        if gate is None:
            return
        if paused == self._paused:
            return
        self._paused = paused
        if paused:
            gate.clear()
            self._logger.info("Download paused")
        else:
            gate.set()
            self._logger.info("Download resumed")

    def _do_cancel(self) -> None:
        """在事件循环内真正执行取消。"""
        event = self._cancel_event
        gate = self._gate
        if event is None or gate is None or self._cancelled:
            return
        self._cancelled = True
        event.set()
        # 关键：抬起闸门。否则处于暂停态的 worker 永远等不到放行，
        # 既不会消费队列，生产者也会被满队列卡死。
        self._paused = False
        gate.set()
        self._logger.info("Download cancelled")

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    async def download(
        self,
        bounds: GeoBounds,
        min_zoom: int,
        max_zoom: int,
        layer: MapLayerType,
        on_tile: TileCallback | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> DownloadSummary:
        """下载覆盖范围内的全部瓦片。

        Args:
            bounds: 地理范围，由 Slice 1 的 :class:`~app.geo.GeoBounds` 保证合法。
            min_zoom: 起始缩放级别。
            max_zoom: 结束缩放级别（含）。
            layer: 图层类型。
            on_tile: 单瓦片回调，**仅在下载成功时**调用，用于把瓦片字节交给
                消费方（Slice 4 的 MBTilesWriter）。返回 ``None`` 或 awaitable 均可。
            on_progress: 进度回调，按 ``progress_interval_seconds`` 节流。

        Returns:
            :class:`DownloadSummary`。被取消时 ``cancelled=True``，已下载的
            统计仍然有效。

        Raises:
            ValueError: 参数非法，或已有任务在运行。
        """
        if self.is_running:
            raise ValueError("已有下载任务正在运行，请先取消或等待其结束")
        if not isinstance(bounds, GeoBounds):
            raise ValueError(f"bounds 必须是 GeoBounds，实际为 {type(bounds).__name__}")
        if not isinstance(layer, MapLayerType):
            raise ValueError(
                f"layer 必须是 MapLayerType 成员，实际为 {layer!r}；"
                f"支持 {[item.value for item in MapLayerType]}"
            )

        # 交给 Slice 1 做 zoom 校验与瓦片统计（这里不复制任何公式）
        ranges = calculate_tile_ranges(bounds, min_zoom, max_zoom)
        total = sum(item.tile_count for item in ranges)

        self._reset_state(total)
        self._loop = asyncio.get_running_loop()
        self._running = True
        try:
            return await self._run(
                bounds, min_zoom, max_zoom, layer, ranges, total, on_tile, on_progress
            )
        finally:
            # 无论正常结束、取消还是异常，都必须把运行时状态清干净，
            # 否则下一次 download() 会被 is_running 误判为"还在跑"。
            self._running = False
            self._loop = None
            self._task = None

    async def _run(
        self,
        bounds: GeoBounds,
        min_zoom: int,
        max_zoom: int,
        layer: MapLayerType,
        ranges: list,
        total: int,
        on_tile: TileCallback | None,
        on_progress: ProgressCallback | None,
    ) -> DownloadSummary:
        """真正干活的部分（状态已就绪，异常由 :meth:`download` 统一收尾）。"""
        started = time.perf_counter()
        self._logger.info(
            "Download started layer=%s zoom=%d-%d tiles=%d concurrency=%d",
            layer.value,
            min_zoom,
            max_zoom,
            total,
            self._concurrency,
        )
        self._logger.debug(
            "各级瓦片数：%s", "，".join(f"z{r.zoom}={r.tile_count}" for r in ranges)
        )

        # 先把初始进度抛出去：UI / 调用方需要立刻知道分母
        self._take_snapshot()
        await self._emit_progress(on_progress, force=True)

        if total <= 0:  # pragma: no cover - 每个 zoom 至少一张瓦片，防御性分支
            self._logger.warning("Download finished with zero tiles")
            return self._build_summary(started, cancelled=False)

        queue: asyncio.Queue[TileCoordinate | None] = asyncio.Queue(
            maxsize=max(1, self._concurrency * QUEUE_CAPACITY_FACTOR)
        )
        gate = self._gate
        assert gate is not None and self._cancel_event is not None

        producer = asyncio.create_task(
            self._produce(queue, iter_tile_coordinates(bounds, min_zoom, max_zoom)),
            name="tile-producer",
        )
        workers = [
            asyncio.create_task(
                self._worker(queue, layer, on_tile), name=f"tile-worker-{index}"
            )
            for index in range(self._concurrency)
        ]
        self._task = producer

        try:
            await producer
        finally:
            # 无论正常结束还是取消，都必须放足哨兵；否则 worker 会永久阻塞在
            # queue.get() 上，download() 永远不会返回。
            # （取消时 worker 仍在排空队列，所以这些 put 一定会成功。）
            for _ in range(self._concurrency):
                await queue.put(None)
            results = await asyncio.gather(*workers, return_exceptions=True)
            for item in results:
                if isinstance(item, BaseException):
                    self._logger.error("下载 worker 异常退出：%r", item)

        self._finalize_skipped()
        self._take_snapshot()
        await self._emit_progress(on_progress, force=True)

        summary = self._build_summary(started, cancelled=self._cancelled)
        self._log_summary(summary)
        return summary

    # ------------------------------------------------------------------ #
    # 生产 / 消费
    # ------------------------------------------------------------------ #
    async def _produce(
        self,
        queue: asyncio.Queue[TileCoordinate | None],
        tiles: Iterator[TileCoordinate],
    ) -> None:
        """生产者：把瓦片喂进有界队列，受闸门与取消控制。"""
        assert self._gate is not None and self._cancel_event is not None
        gate = self._gate
        cancel_event = self._cancel_event

        for tile in tiles:
            if cancel_event.is_set():
                return
            # 暂停时停在这里：不生产新任务，队列里的残留项仍会被 worker 消费完。
            await gate.wait()
            if cancel_event.is_set():
                return
            await queue.put(tile)

    async def _worker(
        self,
        queue: asyncio.Queue[TileCoordinate | None],
        layer: MapLayerType,
        on_tile: TileCallback | None,
    ) -> None:
        """消费者：单张瓦片下载 + 统计 + 交出去，然后处理下一张。"""
        assert self._gate is not None and self._cancel_event is not None
        gate = self._gate
        cancel_event = self._cancel_event

        while True:
            tile = await queue.get()
            if tile is None:
                return
            if cancel_event.is_set():
                # 取消后仍继续把队列排空（否则生产者会被卡住），但不再发请求。
                continue
            await gate.wait()
            if cancel_event.is_set():
                continue

            try:
                result = await self._downloader.download_tile(tile, layer)
            except Exception as exc:  # noqa: BLE001 - 单张瓦片的问题不能掀翻整个任务
                result = TileDownloadResult(
                    tile=tile,
                    success=False,
                    content=None,
                    status_code=None,
                    elapsed_ms=0.0,
                    attempts=0,
                    error=f"未知错误：{exc}",
                    failure_kind=TileFailureKind.UNEXPECTED,
                )
            await self._record(result, on_tile)

    async def _record(
        self,
        result: TileDownloadResult,
        on_tile: TileCallback | None,
    ) -> None:
        """更新统计并调用回调。"""
        counters = self._counters
        if result.success:
            delivered = True
            if on_tile is not None:
                try:
                    await _maybe_await(on_tile(result))
                except Exception as exc:  # noqa: BLE001
                    # 消费方（Slice 4 的 writer）失败等同于这张瓦片没落定，
                    # 因此计入 failed 而不是 succeeded。
                    delivered = False
                    self._logger.error(
                        "瓦片回调失败 %s：%r", result.tile, exc
                    )
            if delivered:
                counters.succeeded += 1
                counters.bytes_downloaded += result.content_length
            else:
                counters.failed += 1
                counters.failure_reasons["瓦片回调处理失败"] += 1
        else:
            counters.failed += 1
            # 按"人话原因"聚合，便于收尾时告诉用户到底为什么失败。
            counters.failure_reasons[result.error or "未知错误"] += 1
            self._log_tile_failure(result)

        counters.current_zoom = result.tile.z
        self._take_snapshot()
        await self._emit_progress(self._progress_callback, force=False)

    def _log_tile_failure(self, result: TileDownloadResult) -> None:
        """逐条打印失败瓦片，但只打印前若干条——一次网络故障可能让几百张
        瓦片同时失败，全打出来会把日志淹掉。"""
        if self._logged_failures >= MAX_LOGGED_TILE_FAILURES:
            return
        self._logged_failures += 1
        suffix = ""
        if self._logged_failures == MAX_LOGGED_TILE_FAILURES:
            suffix = "（后续同类失败只计数不再逐条打印）"
        self._logger.warning("瓦片下载失败：%s%s", result.summary(), suffix)

    # ------------------------------------------------------------------ #
    # 进度
    # ------------------------------------------------------------------ #
    async def _emit_progress(
        self, on_progress: ProgressCallback | None, *, force: bool
    ) -> None:
        """按节流间隔抛出进度快照。

        节流在这里而不在 UI：把限流责任放在数据源头，任何调用方（Qt 信号、
        终端进度条、测试记录器）都自动受益。

        注意：**没有回调时不做任何节流判断**——快照由
        :meth:`_take_snapshot` 独立维护，因此 ``manager.progress`` 无论有没有
        回调都是最新的。
        """
        if on_progress is None:
            return
        self._progress_callback = on_progress
        now = asyncio.get_running_loop().time()
        if not force and now - self._last_emit < self._progress_interval:
            return
        self._last_emit = now
        await _maybe_await(on_progress(self._last_progress))

    def _take_snapshot(self) -> DownloadProgress:
        """刷新并返回当前进度快照。

        每完成一张瓦片、以及任务首尾各调用一次；顺手往速度窗口里补一个采样点。
        速度用的是"最近 ``PROGRESS_SPEED_WINDOW_SECONDS`` 秒"而不是"总量 / 总
        时长"——后者在任务后期几乎不再变化，用户看不出现在到底快不快。
        """
        now = asyncio.get_running_loop().time()
        self._speed_samples.append((now, self._counters.completed))
        self._last_progress = DownloadProgress(
            total=self._counters.total,
            completed=self._counters.completed,
            succeeded=self._counters.succeeded,
            failed=self._counters.failed,
            skipped=self._counters.skipped,
            bytes_downloaded=self._counters.bytes_downloaded,
            tiles_per_second=self._speed(now),
            current_zoom=self._counters.current_zoom,
        )
        return self._last_progress

    def _speed(self, now: float) -> float:
        """最近一个滑动窗口内的平均速度（瓦片/秒）。"""
        window = self._speed_samples
        while len(window) > 1 and now - window[0][0] > PROGRESS_SPEED_WINDOW_SECONDS:
            window.popleft()
        if len(window) < 2:
            return 0.0
        first_time, first_completed = window[0]
        elapsed = now - first_time
        if elapsed <= 0:
            return 0.0
        return max(0.0, (self._counters.completed - first_completed) / elapsed)

    # ------------------------------------------------------------------ #
    # 收尾
    # ------------------------------------------------------------------ #
    def _reset_state(self, total: int) -> None:
        """为一次新任务重置运行时状态。"""
        self._gate = asyncio.Event()
        self._gate.set()  # 默认放行
        self._cancel_event = asyncio.Event()
        self._counters = _Counters(total=total)
        self._speed_samples.clear()
        self._last_emit = 0.0
        self._last_progress = DownloadProgress(total=total)
        self._logged_failures = 0
        self._paused = False
        self._cancelled = False
        self._progress_callback = None

    def _finalize_skipped(self) -> None:
        """被取消时把"没轮到的瓦片"记成 skipped。

        本 Slice 没有断点续传，跳过的瓦片不会被补下——``skipped`` 只是让用户
        知道"这次少下了多少张"，不做任何后续动作。
        """
        if not self._cancelled:
            self._counters.skipped = 0
            return
        self._counters.skipped = max(0, self._counters.total - self._counters.completed)

    def _build_summary(self, started: float, *, cancelled: bool) -> DownloadSummary:
        """汇总本次任务。"""
        counters = self._counters
        duration = max(0.0, time.perf_counter() - started)
        completed = counters.completed
        return DownloadSummary(
            total=counters.total,
            succeeded=counters.succeeded,
            failed=counters.failed,
            skipped=counters.skipped,
            bytes_downloaded=counters.bytes_downloaded,
            duration_seconds=duration,
            average_tiles_per_second=(completed / duration) if duration > 0 else 0.0,
            cancelled=cancelled,
            failures_by_reason=dict(counters.failure_reasons),
        )

    def _log_summary(self, summary: DownloadSummary) -> None:
        """按 Spec 要求的格式输出收尾日志，并附上失败原因（若失败）。"""
        if summary.cancelled:
            self._logger.info(
                "Download cancelled success=%d failed=%d skipped=%d duration=%.2fs",
                summary.succeeded,
                summary.failed,
                summary.skipped,
                summary.duration_seconds,
            )
        else:
            self._logger.info(
                "Download completed success=%d failed=%d duration=%.2fs speed=%.1f/s",
                summary.succeeded,
                summary.failed,
                summary.duration_seconds,
                summary.average_tiles_per_second,
            )
        if summary.failed:
            # 只打去重后的 Top N：失败原因已聚合，不会随失败瓦片数膨胀。
            detail = "；".join(
                f"{reason}（{count} 张）" for reason, count in summary.top_failures()
            )
            self._logger.warning("Download failure reasons: %s", detail)


# --------------------------------------------------------------------------- #
# 类型别名（供外部声明回调时使用，避免到处 import Awaitable）
# --------------------------------------------------------------------------- #
_MANAGER_CALLBACK_ALIAS: type = AsyncIterator  # noqa: B018 - 保持 Awaitable 导入语义
