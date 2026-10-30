"""批量瓦片下载调度（Slice 3；Slice 5 增加跳过已有瓦片）。

结构（与 Spec 的推荐拓扑一致）::

    Tile Producer (协程，惰性生成)
          ↓
      region_filter.intersects_tile(tile)   # Slice 16：区域外的瓦片在这里丢掉
          ↓ True（区域外直接 continue，不计数）
      should_skip_tile(tile)         # Slice 5：已有瓦片在这里就被拦下
          ↓ False
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

跳过判定（Slice 5）
-------------------

``should_skip_tile`` 在**生产者**里被调用，也就是在瓦片进入队列、进入任何
HTTP 请求之前。命中的瓦片只增加 ``skipped`` 计数，完全不消耗天地图配额。
判定失败（抛异常）时保守地按"需要下载"处理：宁可多下一张，也不能因为一次
查询异常就把瓦片永久漏掉。

``skipped``（库里有）与 ``unprocessed``（取消时没轮到）是两个不同的字段：
前者计入 ``completed``，后者不计。

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
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from typing import Protocol

from app.config.settings import (
    DEFAULT_DOWNLOAD_CONCURRENCY,
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
    SkipPredicate,
    TileCallback,
    TileCoordinate,
    TileDownloadResult,
    TileFailureKind,
)
from app.downloader.rate_limiter import RateLimitExceeded
from app.downloader.tile_downloader import TileDownloader
from app.geo import GeoBounds, calculate_tile_ranges
from app.models.map_layer import MapLayerType, MapType

__all__ = ["DownloadManager", "RegionFilterLike", "iter_tile_coordinates"]

#: ``layer`` 参数允许的取值。
#:
#: Slice 3~5 传的是单个图层（:class:`MapLayerType`）；Slice 6 的合成模式要传
#: :class:`MapType`（"矢量 / 影像"这个业务语义），由
#: :class:`~app.downloader.CompositeTileDownloader` 自己展开成两层。
#:
#: 本模块**不关心**它到底是哪一个：它只把 ``layer`` 原样交给下载器
#: （``downloader.download_tile(tile, layer)``）。因此这里不需要一套
#: "合成模式下换成别的参数"的分支——下载调度与"一层还是两层"无关。
LayerArgument = MapLayerType | MapType


class RegionFilterLike(Protocol):
    """区域过滤器的**结构化类型**（Slice 16）。

    本模块只需要它回答一个问题："这张瓦片要不要下？"。刻意只声明这一个方法，
    而不是 import :class:`~app.geo.PolygonTileRegionFilter` 这个具体类：下载调度
    与"用多边形还是别的什么形状裁剪"是两件独立的事，测试里传一个
    ``lambda tile: ...`` 的假过滤器也应当能跑。

    实现方**必须**保证判定是纯函数式的（同一张瓦片每次问都得到同样的答案）：
    生产者的顺序遍历依赖这一点。
    """

    def intersects_tile(self, tile: TileCoordinate) -> bool:
        """``True`` 表示这张瓦片落在区域内，应当下载。"""
        ...


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


async def _maybe_await_bool(value: object) -> bool:
    """求值一个"可能是协程"的布尔判定（Slice 5 的跳过谓词用）。

    ``should_skip_tile`` 既可能是 ``lambda tile: True``（同步查表），
    也可能是 ``async def``（异步存储/LRU）。这里统一求值，调用方只写一行。
    """
    if inspect.isawaitable(value):
        return bool(await value)  # type: ignore[misc]
    return bool(value)


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
    #: 已有瓦片、本次跳过（Slice 5）
    skipped: int = 0
    #: 因取消而完全没被处理的瓦片数
    unprocessed: int = 0
    current_zoom: int | None = None
    #: 失败原因 → 张数。只存人话原因（不含 Token / 完整 URL）。
    failure_reasons: Counter[str] = field(default_factory=Counter)

    @property
    def completed(self) -> int:
        """已处理 = 成功 + 失败 + 已有跳过。

        跳过的必须算进来：续传任务的分母是"任务目标张数"，如果跳过的 6 万张
        不计入分子，进度条就会永远停在 40% 然后突然跳到 100%。
        """
        return self.succeeded + self.failed + self.skipped


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
        self._stop_reason = ""
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
        layer: LayerArgument,
        on_tile: TileCallback | None = None,
        on_progress: ProgressCallback | None = None,
        should_skip_tile: SkipPredicate | None = None,
        region_filter: RegionFilterLike | None = None,
        total_tiles: int | None = None,
    ) -> DownloadSummary:
        """下载覆盖范围内的全部瓦片。

        Args:
            bounds: 地理范围，由 Slice 1 的 :class:`~app.geo.GeoBounds` 保证合法。
            min_zoom: 起始缩放级别。
            max_zoom: 结束缩放级别（含）。
            layer: 图层（:class:`~app.models.MapLayerType`）**或**地图类型
                （:class:`~app.models.MapType`，Slice 6 的合成模式）。
                本模块只把它原样转交给下载器，不做任何解释。
            on_tile: 单瓦片回调，**仅在下载成功时**调用，用于把瓦片字节交给
                消费方（Slice 4 的 MBTilesWriter）。返回 ``None`` 或 awaitable 均可。
            on_progress: 进度回调，按 ``progress_interval_seconds`` 节流。
            should_skip_tile: 跳过判定（Slice 5 的断点续传）。返回 ``True`` 的瓦片
                **不会进入下载队列**，直接计入 ``skipped``。

                **判定发生在生产者里，也就是任何 HTTP 请求之前**——
                这是 Spec §5 的硬性要求："禁止先下载，再发现数据库里已经有"。
                判定本身可以是同步函数，也可以是协程函数。
            region_filter: 区域过滤器（Slice 16 的多边形裁剪）。给了它以后，
                **只有** :meth:`RegionFilterLike.intersects_tile` 返回 ``True``
                的候选瓦片才会进入队列；区域外的瓦片**连计数都不参与** ——
                既不算 ``failed``，也不算 ``skipped_existing``。这一点是
                Spec §10 的硬性要求：把区域外的瓦片算进失败或跳过，会让
                用户看到"失败 1.4 万张"这种纯粹由裁剪造成的假故障。

                传 ``None``（默认）时行为与 Slice 0~15 完全一致。
            total_tiles: 本次任务的**目标张数**（进度分母）。省略时按矩形候选数
                计算；给了 ``region_filter`` 而没给它，本方法会自己流式数一遍
                （正确但会多遍历一轮候选瓦片，千万级任务请由调用方传预算好的值 ——
                :class:`~app.geo.RegionTileEstimator` 已经算过一次了）。

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
        if not isinstance(layer, (MapLayerType, MapType)):
            raise ValueError(
                f"layer 必须是 MapLayerType（或 Slice 6 的 MapType）成员，"
                f"实际为 {layer!r}；支持 {[item.value for item in MapLayerType]}"
            )
        if should_skip_tile is not None and not callable(should_skip_tile):
            raise ValueError(
                f"should_skip_tile 必须是可调用对象，实际为 {type(should_skip_tile).__name__}"
            )
        if region_filter is not None and not callable(
            getattr(region_filter, "intersects_tile", None)
        ):
            raise ValueError(
                "region_filter 必须提供 intersects_tile(tile) 方法，"
                f"实际为 {type(region_filter).__name__}"
            )

        # 交给 Slice 1 做 zoom 校验与瓦片统计（这里不复制任何公式）
        ranges = calculate_tile_ranges(bounds, min_zoom, max_zoom)
        if total_tiles is not None:
            total = int(total_tiles)
            if total < 0:
                raise ValueError(f"total_tiles 不能为负，实际为 {total}")
        elif region_filter is None:
            total = sum(item.tile_count for item in ranges)
        else:
            # 没给预算值：自己流式数一遍选中瓦片。**必须用 selected 而不是
            # candidate**，否则进度条永远到不了 100%（分母里混着区域外瓦片）。
            total = self._count_region_tiles(bounds, min_zoom, max_zoom, region_filter)

        self._reset_state(total)
        self._loop = asyncio.get_running_loop()
        self._running = True
        control = getattr(self._downloader, "set_request_control", None)
        if control is not None:
            control(self._gate, self._cancel_event)
        try:
            return await self._run(
                bounds,
                min_zoom,
                max_zoom,
                layer,
                ranges,
                total,
                on_tile,
                on_progress,
                should_skip_tile,
                region_filter,
            )
        finally:
            # 无论正常结束、取消还是异常，都必须把运行时状态清干净，
            # 否则下一次 download() 会被 is_running 误判为"还在跑"。
            if control is not None:
                control(None, None)
            self._running = False
            self._loop = None
            self._task = None

    async def _run(
        self,
        bounds: GeoBounds,
        min_zoom: int,
        max_zoom: int,
        layer: LayerArgument,
        ranges: list,
        total: int,
        on_tile: TileCallback | None,
        on_progress: ProgressCallback | None,
        should_skip_tile: SkipPredicate | None = None,
        region_filter: RegionFilterLike | None = None,
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
            self._produce(
                queue,
                (TileCoordinate(z, x, y)
                 for z in range(min_zoom, max_zoom + 1)
                 for x, y in region_filter.iter_candidate_tiles(bounds, z))
                if region_filter is not None and hasattr(region_filter, "iter_candidate_tiles")
                else iter_tile_coordinates(bounds, min_zoom, max_zoom),
                should_skip_tile,
                region_filter,
            ),
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

        self._finalize_unprocessed()
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
        should_skip_tile: SkipPredicate | None = None,
        region_filter: RegionFilterLike | None = None,
    ) -> None:
        """生产者：把瓦片喂进有界队列，受闸门、取消、"区域判定"与"跳过判定"控制。

        两道判定都放在这里（而不是 worker 里）是**有意为之**：

        - 命中的瓦片**根本不会进队列**，因此绝不会消耗 HTTP 配额，
          也绝不会出现"下完了才发现库里已经有"的浪费；
        - 判定与下载的先后顺序在代码结构上就是确定的，不依赖 worker 的自觉。

        两道的顺序也有讲究：**先区域、后跳过**。区域外的瓦片连"要不要跳过"
        都不必问 —— 它压根不属于这次任务，去查一次数据库纯属浪费。
        """
        assert self._gate is not None and self._cancel_event is not None
        gate = self._gate
        cancel_event = self._cancel_event

        for tile_index, tile in enumerate(tiles):
            # 连续跳过已有瓦片时没有 HTTP await；让线程安全的取消指令得到执行。
            if tile_index % 256 == 0:
                await asyncio.sleep(0)
            if cancel_event.is_set():
                return
            # 暂停时停在这里：不生产新任务，队列里的残留项仍会被 worker 消费完。
            await gate.wait()
            if cancel_event.is_set():
                return

            if region_filter is not None and not region_filter.intersects_tile(tile):
                # 区域外的候选瓦片：**不计数、不入队**，直接翻篇。
                #
                # 这里刻意连 ``self._counters`` 都不碰：一旦给它记个
                # failed / skipped，用户就会在收尾报告里看到"失败 1.4 万张"
                # 或者"跳过 1.4 万张"，而真相是这些瓦片本来就不该被下
                # （Spec §10/§32）。把它们排除在计数之外，total（=选中瓦片数）
                # 与各计数项才自洽。
                continue

            if should_skip_tile is not None:
                try:
                    skip = await _maybe_await_bool(should_skip_tile(tile))
                except Exception as exc:  # noqa: BLE001 - 判定失败不应该掀翻任务
                    # 判定本身出错时**保守选择"下载"**：宁可多下一张，
                    # 也不要因为一个查询异常就把瓦片永久漏掉。
                    self._logger.warning(
                        "跳过判定失败，按需要下载处理 %s：%r", tile, exc
                    )
                    skip = False
                if skip:
                    self._counters.skipped += 1
                    self._counters.current_zoom = tile.z
                    self._take_snapshot()
                    await self._emit_progress(self._progress_callback, force=False)
                    continue

            await queue.put(tile)

    def _count_region_tiles(
        self,
        bounds: GeoBounds,
        min_zoom: int,
        max_zoom: int,
        region_filter: RegionFilterLike,
    ) -> int:
        """流式数一遍"选中瓦片"数（仅在调用方没给 ``total_tiles`` 时用到）。

        刻意用生成器逐张过，不落地任何列表：全国 z14 的候选瓦片是千万级，
        先 materialize 再过滤会把内存吃光（Spec §9）。
        """
        count = 0
        if hasattr(region_filter, "iter_selected_tiles") and hasattr(region_filter, "iter_candidate_tiles"):
            return sum(1 for z in range(min_zoom, max_zoom + 1)
                       for _ in region_filter.iter_selected_tiles(bounds, z))
        for tile in iter_tile_coordinates(bounds, min_zoom, max_zoom):
            if region_filter.intersects_tile(tile):
                count += 1
        return count

    async def _worker(
        self,
        queue: asyncio.Queue[TileCoordinate | None],
        layer: LayerArgument,
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
            except RateLimitExceeded as exc:
                self._stop_reason = str(exc)
                cancel_event.set()
                gate.set()
                self._logger.warning(self._stop_reason)
                continue
            except asyncio.CancelledError:
                if cancel_event.is_set():
                    continue
                raise
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
            unprocessed=self._counters.unprocessed,
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
        self._stop_reason = ""
        self._progress_callback = None

    def _finalize_unprocessed(self) -> None:
        """被取消时把"根本没轮到的瓦片"记成 ``unprocessed``。

        Slice 5 起 ``skipped`` 的含义是"已有瓦片、本次未重下"，
        与"因取消而没处理"是两件完全不同的事，因此拆成两个字段：

        - ``skipped``：库里有 → 不需要下。**这是成果**，会计入 ``completed``。
        - ``unprocessed``：还没来得及处理 → 下次续传再下。**这是欠账**。

        混在一起会让"续传了 6 万张"和"取消了剩 6 万张"在界面上长得一模一样。
        """
        if not self._cancelled and not self._stop_reason:
            self._counters.unprocessed = 0
            return
        self._counters.unprocessed = max(
            0, self._counters.total - self._counters.completed
        )

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
            unprocessed=counters.unprocessed,
            bytes_downloaded=counters.bytes_downloaded,
            duration_seconds=duration,
            average_tiles_per_second=(completed / duration) if duration > 0 else 0.0,
            cancelled=cancelled,
            stop_reason=self._stop_reason,
            failures_by_reason=dict(counters.failure_reasons),
        )

    def _log_summary(self, summary: DownloadSummary) -> None:
        """按 Spec 要求的格式输出收尾日志，并附上失败原因（若失败）。"""
        if summary.stop_reason:
            self._logger.warning("Download stopped: %s; unprocessed=%d", summary.stop_reason, summary.unprocessed)
        elif summary.cancelled:
            self._logger.info(
                "Download cancelled success=%d failed=%d skipped=%d unprocessed=%d "
                "duration=%.2fs",
                summary.succeeded,
                summary.failed,
                summary.skipped,
                summary.unprocessed,
                summary.duration_seconds,
            )
        else:
            self._logger.info(
                "Download completed success=%d failed=%d skipped=%d duration=%.2fs "
                "speed=%.1f/s",
                summary.succeeded,
                summary.failed,
                summary.skipped,
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
