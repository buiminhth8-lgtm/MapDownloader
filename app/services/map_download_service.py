"""下载并落盘为一个 MBTiles 文件（Slice 4）。

这是本 Slice 的**编排层**：把 Slice 1/2/3/4 的能力串成"一个业务动作"。

完整链路::

    BoundingBox                 (Slice 1：范围与合法性)
        ↓
    Tile Math                   (Slice 1：算出瓦片编号区间)
        ↓
    TiandituProvider            (Slice 2：拼 WMTS URL、判响应合法性)
        ↓
    Async Downloader            (Slice 3：并发 / 限速 / 重试 / 暂停取消)
        ↓  on_tile 回调
    MBTilesTileSink             (Slice 4：有界队列 + 单消费者)
        ↓
    MBTilesWriter               (Slice 4：批量事务写 SQLite)
        ↓
    map.mbtiles
        ↓
    Validator                   (Slice 4：结构 + 抽样数据校验)

为什么要有这一层（而不是塞进 MainWindow）
------------------------------------------

上面这条链路涉及 6 个模块、3 种资源（网络连接、SQLite 连接、事件循环任务），
任何一步的收尾顺序写错都会留下"连接没关 / 文件没落盘 / 任务没退出"的尾巴。
把这些顺序收敛到一个地方，UI 就只剩"读表单 → 调这里 → 显示结果"。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.config.settings import (
    DEFAULT_DOWNLOAD_CONCURRENCY,
    DEFAULT_MAP_NAME,
    DEFAULT_MBTILES_BATCH_SIZE,
    DEFAULT_MBTILES_QUEUE_CAPACITY,
    DEFAULT_REQUEST_TIMEOUT,
    MBTILES_FILE_SUFFIX,
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
from app.geo import GeoBounds, format_size
from app.mbtiles import (
    MBTilesMetadata,
    MBTilesReader,
    MBTilesTileSink,
    MBTilesValidationResult,
    MBTilesWriteError,
    MBTilesWriter,
    validate_mbtiles,
)
from app.models.map_layer import MapLayerType
from app.providers import TiandituProvider

__all__ = [
    "MapDownloadOutcome",
    "MapDownloadService",
    "build_output_path",
    "sanitize_map_name",
]


_logger = logging.getLogger(__name__)


#: Windows 文件名非法字符（``\ / : * ? " < > |``）与其余控制字符
_ILLEGAL_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

#: 文件名里不允许出现的保留名（不带扩展名比较）
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

#: 地图名称的最大长度（避免超出文件系统上限）
_MAX_NAME_LENGTH = 80


def sanitize_map_name(name: str) -> str:
    """把用户输入的地图名称规整成可安全用作文件名的形式。

    只做"最小必要的清理"，不改变用户看得懂的部分：

    - 去掉首尾空白，替换 Windows 非法字符与控制字符为 ``_``；
    - 去掉结尾的点和空格（Windows 会静默丢弃，导致路径与预期不一致）；
    - 命中 Windows 保留名（``CON`` / ``LPT1`` …）时加后缀；
    - 清完为空 → 回退到 ``DEFAULT_MAP_NAME``。

    Args:
        name: 原始名称。

    Returns:
        可直接当作文件名主体使用的字符串（不含扩展名）。

    >>> sanitize_map_name("  北京 地图  ")
    '北京 地图'
    >>> sanitize_map_name("a/b:c")
    'a_b_c'
    """
    cleaned = _ILLEGAL_NAME_CHARS.sub("_", str(name).strip()).rstrip(". ")
    if not cleaned:
        return DEFAULT_MAP_NAME
    if cleaned.split(".")[0].upper() in _RESERVED_NAMES:
        cleaned = f"{cleaned}_map"
    return cleaned[:_MAX_NAME_LENGTH]


def build_output_path(output_dir: str | Path, map_name: str) -> Path:
    """由"输出目录 + 地图名称"得到 ``<目录>/<名称>.mbtiles``。

    Args:
        output_dir: 输出目录（不存在也没关系，写入时会自动创建）。
        map_name: 地图名称，会被 :func:`sanitize_map_name` 规整。

    Returns:
        输出文件的完整路径。
    """
    return Path(output_dir) / f"{sanitize_map_name(map_name)}{MBTILES_FILE_SUFFIX}"


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #
@dataclass
class MapDownloadOutcome:
    """一次"下载并落盘"的最终结果（GUI 直接照着展示）。"""

    summary: DownloadSummary
    output_path: Path
    metadata: MBTilesMetadata
    validation: MBTilesValidationResult
    #: 数据库里的实际瓦片数（由 Reader 统计，而不是"我们以为写了多少张"）
    tile_count: int = 0
    file_size: int = 0
    #: 写库阶段被丢弃的瓦片数（写库失败时 > 0）
    discarded: int = 0
    extra_lines: list[str] = field(default_factory=list)

    @property
    def cancelled(self) -> bool:
        """是否是用户取消。"""
        return self.summary.cancelled

    @property
    def is_success(self) -> bool:
        """跑完、零失败、且文件通过校验。"""
        return (
            not self.cancelled and self.summary.failed == 0 and self.validation.valid
        )

    @property
    def size_text(self) -> str:
        """格式化后的文件体积。"""
        return format_size(self.file_size)

    def as_lines(self) -> list[str]:
        """多行展示文案（GUI 结果区 / 日志逐行展示）。"""
        lines = [
            f"文件：{self.output_path}",
            f"瓦片：{self.tile_count:,}",
            f"大小：{self.size_text}",
            f"Zoom：{self.validation.zoom_text}",
            f"验证：{self.validation.status_text}",
        ]
        lines.extend(
            f"失败原因：{reason}（{count:,} 张）"
            for reason, count in self.summary.top_failures()
        )
        if self.discarded:
            lines.append(f"写库失败丢弃：{self.discarded:,} 张")
        lines.extend(self.validation.errors)
        lines.extend(self.extra_lines)
        return lines


# --------------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------------- #
class MapDownloadService:
    """一次下载任务的编排者，同时是对外唯一的"控制面板"。

    用法（GUI 侧）::

        service = MapDownloadService(provider, bounds=..., min_zoom=8, ...)
        summary_task = asyncio.create_task(service.download_to_mbtiles(...))
        ...
        service.pause()      # 可从**其它线程**调用
        service.resume()
        service.cancel()

    暂停 / 继续 / 取消可以跨线程调用：内部转发给
    :class:`~app.downloader.DownloadManager`，由它投递到任务所在的事件循环。
    """

    def __init__(
        self,
        provider: TiandituProvider,
        *,
        concurrency: int = DEFAULT_DOWNLOAD_CONCURRENCY,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        retry_policy: RetryPolicy | None = None,
        rate_limiter: RateLimiter | None = None,
        queue_capacity: int = DEFAULT_MBTILES_QUEUE_CAPACITY,
        batch_size: int = DEFAULT_MBTILES_BATCH_SIZE,
        progress_interval_seconds: float = PROGRESS_EMIT_INTERVAL_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Args:
            provider: 瓦片源（持有 Token）。
            concurrency: HTTP 并发数。
            timeout: 单请求超时（秒）。
            retry_policy: 重试策略，``None`` 用引擎默认。
            rate_limiter: 限速器，``None`` 用引擎默认。
            queue_capacity: MBTiles 写队列容量。
            batch_size: MBTiles 批量事务大小。
            progress_interval_seconds: 进度回调节流间隔。
            transport: httpx 传输层，**仅供测试注入 ``httpx.MockTransport``**。

        注意 ``transport`` 必须在这里暴露：``TileDownloader`` 用的是自己的
        transport，``TiandituProvider`` 上挂的那个不影响下载器。少了这个参数，
        集成测试就只能去打真实天地图——那既不允许也不稳定。
        """
        self._provider = provider
        self._concurrency = concurrency
        self._timeout = timeout
        self._retry_policy = retry_policy
        self._rate_limiter = rate_limiter
        self._queue_capacity = queue_capacity
        self._batch_size = batch_size
        self._progress_interval = progress_interval_seconds
        self._transport = transport

        self._manager: DownloadManager | None = None
        self._sink: MBTilesTileSink | None = None
        self._tiles_written = 0

    # ------------------------------------------------------------------ #
    # 只读状态
    # ------------------------------------------------------------------ #
    @property
    def manager(self) -> DownloadManager | None:
        """当前任务的下载管理器（未运行时为 ``None``）。"""
        return self._manager

    @property
    def progress(self) -> DownloadProgress | None:
        """最新进度快照。"""
        manager = self._manager
        return manager.progress if manager is not None else None

    @property
    def tiles_written(self) -> int:
        """已交给 MBTiles 写入器的瓦片数（最后一次任务的）。"""
        return self._tiles_written

    # ------------------------------------------------------------------ #
    # 控制（可从其它线程调用）
    # ------------------------------------------------------------------ #
    def pause(self) -> None:
        """暂停当前任务（未运行时为空操作）。"""
        manager = self._manager
        if manager is not None:
            manager.pause()

    def resume(self) -> None:
        """继续当前任务（未运行时为空操作）。"""
        manager = self._manager
        if manager is not None:
            manager.resume()

    def cancel(self) -> None:
        """取消当前任务；**已下载并进队列的瓦片仍会写入 MBTiles**。"""
        manager = self._manager
        if manager is not None:
            manager.cancel()

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    async def download_to_mbtiles(
        self,
        bounds: GeoBounds,
        min_zoom: int,
        max_zoom: int,
        layer: MapLayerType,
        output_path: str | Path,
        *,
        map_name: str | None = None,
        description: str = "",
        image_format: str = "png",
        on_progress=None,
    ) -> MapDownloadOutcome:
        """下载给定范围并写成 MBTiles，返回最终结果。

        Args:
            bounds: 下载范围。
            min_zoom / max_zoom: 缩放区间（含两端）。
            layer: 图层。
            output_path: 输出的 ``.mbtiles`` 路径（父目录自动创建）。
            map_name: MBTiles 的 ``name``；``None`` 时取输出文件名主体。
            description: MBTiles 的 ``description``。
            image_format: 瓦片图片格式。
            on_progress: 进度回调（``DownloadProgress``），已节流。

        Returns:
            :class:`MapDownloadOutcome`

        Raises:
            MBTilesWriteError: **写库阶段**失败（磁盘满、库损坏等）。
                这种错误无法继续，必须让用户看见，因此不作降级处理。
            ValueError: 参数非法。
        """
        if not isinstance(bounds, GeoBounds):
            raise ValueError(f"bounds 必须是 GeoBounds，实际为 {type(bounds).__name__}")
        if not isinstance(layer, MapLayerType):
            raise ValueError(
                f"layer 必须是 MapLayerType，实际为 {layer!r}；"
                f"支持 {[item.value for item in MapLayerType]}"
            )

        path = Path(output_path)
        name = map_name or path.stem
        metadata = MBTilesMetadata.for_bounds(
            bounds,
            min_zoom,
            max_zoom,
            name=name,
            description=description,
            image_format=image_format,
        )

        path.parent.mkdir(parents=True, exist_ok=True)
        writer = MBTilesWriter(path, batch_size=self._batch_size)
        try:
            writer.open()
            writer.write_metadata(metadata)
        except Exception as exc:  # noqa: BLE001 - 建库失败统一归为写库失败
            # 建库阶段挂了（目录不可写、磁盘满、磁盘上的库已损坏……）：
            # 首先要保证不留下半个连接，其次要给出统一的异常类型，
            # 让 GUI 只需要认 MBTilesWriteError 一种"写盘失败"。
            writer.abort()
            raise MBTilesWriteError(
                f"无法创建 MBTiles（{path}）：{exc}"
            ) from exc

        sink = MBTilesTileSink(writer, queue_capacity=self._queue_capacity)
        sink.start()
        downloader = TileDownloader(
            self._provider,
            transport=self._transport,
            retry_policy=self._retry_policy,
            rate_limiter=self._rate_limiter,
            timeout=self._timeout,
        )

        self._sink = sink
        self._tiles_written = 0
        write_error: MBTilesWriteError | None = None

        try:
            manager = DownloadManager(
                downloader,
                concurrency=self._concurrency,
                progress_interval_seconds=self._progress_interval,
            )
            self._manager = manager
            summary = await manager.download(
                bounds,
                min_zoom,
                max_zoom,
                layer,
                on_tile=sink.consume,
                on_progress=self._make_progress_forwarder(manager, on_progress),
            )
        finally:
            self._manager = None
            await downloader.close()
            # 无论正常结束还是取消，都要把已进队列的瓦片落盘
            try:
                await sink.drain()
            except MBTilesWriteError as exc:
                write_error = exc
            finally:
                try:
                    writer.close()
                except sqlite3.Error as exc:
                    # close() 内部会 flush 剩余瓦片，写盘失败会在这里冒出来
                    if write_error is None:
                        write_error = MBTilesWriteError(
                            f"关闭 MBTiles 连接失败（{exc}）"
                        )
                self._sink = None
                self._tiles_written = sink.written

        if write_error is not None:
            # 写库失败属于"任务不可继续"，必须上抛（§14：不能被吞掉）
            raise MBTilesWriteError(
                f"MBTiles 写入失败（文件 {path}，已入库 {sink.written} 张，"
                f"丢弃 {sink.discarded} 张）：{write_error}"
            ) from write_error

        tile_count, file_size = self._inspect(path)
        validation = validate_mbtiles(path)
        _logger.info(
            "Map saved path=%s tiles=%d size=%s zoom=%s valid=%s",
            path.name,
            tile_count,
            format_size(file_size),
            validation.zoom_text,
            validation.status_text,
        )

        extra_lines: list[str] = []
        if summary.cancelled:
            extra_lines.append("状态：已取消，已完成瓦片已保留（Slice 5 支持断点续传）")

        return MapDownloadOutcome(
            summary=summary,
            output_path=path,
            metadata=metadata,
            validation=validation,
            tile_count=tile_count,
            file_size=file_size,
            discarded=sink.discarded,
            extra_lines=extra_lines,
        )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _make_progress_forwarder(self, manager: DownloadManager, on_progress):
        """包装进度回调：顺手把"写库失败"传导成"取消下载"。

        写库挂了还继续下载纯属白耗配额，所以一旦 sink 报错就取消任务，
        真正的异常仍由 :meth:`download_to_mbtiles` 统一抛出。
        """

        def forward(progress: DownloadProgress) -> None:
            sink = self._sink
            if sink is not None and sink.failure is not None:
                manager.cancel()
            if on_progress is not None:
                on_progress(progress)

        return forward

    @staticmethod
    def _inspect(path: Path) -> tuple[int, int]:
        """用 Reader 统计实际瓦片数，并读取文件体积。

        刻意**不**用 ``writer.tiles_written`` 当答案：那个数字是"我们以为写了
        多少张"（重复写入会重复计数），而验收要的是"文件里到底有多少张"。
        """
        file_size = path.stat().st_size if path.exists() else 0
        with MBTilesReader(path) as reader:
            return (reader.get_tile_count(), file_size)
