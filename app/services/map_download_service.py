"""下载并落盘为一个 MBTiles 文件（Slice 4；Slice 5 增加断点续传）。

这是**编排层**：把 Slice 1/2/3/4/5 的能力串成"一个业务动作"。

完整链路::

    BoundingBox                 (Slice 1：范围与合法性)
        ↓
    Tile Math                   (Slice 1：算出瓦片编号区间)
        ↓
    TiandituProvider            (Slice 2：拼 WMTS URL、判响应合法性)
        ↓
    Async Downloader            (Slice 3：并发 / 限速 / 重试 / 暂停取消)
        ↓  should_skip_tile      (Slice 5：已有瓦片在这里被拦下，不进 HTTP)
    MBTilesTileSink             (Slice 4：有界队列 + 单消费者)
        ↓
    MBTilesWriter               (Slice 4：批量事务写 SQLite)
        ↓
    map.mbtiles
        ↓
    Validator                   (Slice 4：结构 + 抽样数据校验)

续传（Slice 5）额外多出来的一圈::

    DownloadTask                (Slice 5：任务记录，来自 tasks.json)
        ↓
    output_path 已存在？
        ↓ 是
    MBTilesReader 读 metadata
        ↓
    一致性校验（bounds / zoom / layer）  ← 不匹配就**拒绝续传**，绝不往错库里写
        ↓ 通过
    ExistingTileIndex           (Slice 5：已有瓦片 → 跳过)
        ↓
    只下缺失的那些，写回同一个 .mbtiles

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

from app.composer import TileComposer
from app.config.settings import (
    DEFAULT_DOWNLOAD_CONCURRENCY,
    DEFAULT_MBTILES_BATCH_SIZE,
    DEFAULT_MBTILES_QUEUE_CAPACITY,
    DEFAULT_REQUEST_TIMEOUT,
    MBTILES_BOUNDS_TOLERANCE,
    MBTILES_FILE_SUFFIX,
    MBTILES_PROVIDER_TIANDITU,
    MBTILES_VALIDATE_SAMPLE_LIMIT,
    PROGRESS_EMIT_INTERVAL_SECONDS,
    UI_TEXT_COMPOSITION_MERGED,
    UI_TEXT_COMPOSITION_SINGLE,
    UI_TEXT_RESUME_INCOMPATIBLE,
    UI_TEXT_RESUME_TOKEN_REQUIRED,
)
from app.downloader import (
    CompositeTileDownloader,
    DownloadManager,
    DownloadProgress,
    DownloadSummary,
    ExistingTileIndex,
    RateLimiter,
    ResumePolicy,
    RetryPolicy,
    SkipMode,
    TileDownloader,
    describe_existing_tiles,
    make_skip_predicate,
)
from app.geo import (
    DownloadRegion,
    GeoBounds,
    RegionTileEstimator,
    build_region_filter,
    estimate_tile_count,
    format_size,
    region_from_task,
)
from app.mbtiles import (
    MBTilesDiagnosticsResult,
    MBTilesMetadata,
    MBTilesReader,
    MBTilesTileSink,
    MBTilesValidationResult,
    MBTilesWriteError,
    MBTilesWriter,
)
from app.models.download_task import DownloadTask, DownloadTaskStatus
from app.models.map_layer import (
    COMPOSITION_SINGLE,
    MapLayerType,
    MapType,
    composition_for,
)

#: Slice 9 起 :func:`sanitize_map_name` 住在 :mod:`app.models.naming`（见那里的
#: 模块说明：包构建器也要用它，留在服务层会形成循环导入）。这里原样导入并在
#: ``__all__`` 里继续导出，既有调用方与测试一行都不用改。
from app.models.naming import sanitize_map_name
from app.providers import TiandituProvider
from app.services.mbtiles_inspection_service import MBTilesInspectionService

__all__ = [
    "LayerArgument",
    "MapDownloadOutcome",
    "MapDownloadService",
    "MapResumeError",
    "ResumeCompatibility",
    "build_output_path",
    "check_resume_compatibility",
    "count_existing_region_tiles",
    "count_existing_tiles",
    "region_target_tiles",
    "sanitize_map_name",
]


#: 下载目标：单图层（:class:`~app.models.MapLayerType`）或地图类型
#: （:class:`~app.models.MapType`）。服务层两种都收，由它决定走哪条链路。
LayerArgument = MapLayerType | MapType


_logger = logging.getLogger(__name__)


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
# 恢复前的一致性校验（Slice 5，Spec §12）
# --------------------------------------------------------------------------- #
class MapResumeError(RuntimeError):
    """无法安全地继续一个下载任务。

    与 :class:`~app.mbtiles.MBTilesWriteError` 的区别：那个是"写盘出问题了"，
    这个是"**不该往这个文件里写**"——例如已有 MBTiles 属于另一个区域、
    另一个缩放级别、或者根本不是本工具生成的合法文件。

    刻意单独一个类型：GUI 需要给它完全不同的措辞（"参数不一致，无法安全继续"
    而不是"磁盘写失败"），用户据此决定是换个文件名还是重新下载。
    """


@dataclass
class ResumeCompatibility:
    """已有 MBTiles 与任务参数的比对结果。"""

    #: ``True`` 表示可以安全续传
    compatible: bool = True
    #: 阻断续传的问题（用户必须处理）
    problems: list[str] = field(default_factory=list)
    #: 不阻断但值得告知的差异
    warnings: list[str] = field(default_factory=list)
    #: 已有瓦片数（按任务 zoom 区间统计，不读 BLOB）
    existing_tiles: int = 0
    #: 任务目标瓦片数
    target_tiles: int = 0

    @property
    def message(self) -> str:
        """给用户看的一整段说明。"""
        if self.compatible:
            return ""
        lines = [UI_TEXT_RESUME_INCOMPATIBLE]
        lines.extend(f"· {problem}" for problem in self.problems)
        return "\n".join(lines)


def check_resume_compatibility(
    task: DownloadTask,
    metadata: MBTilesMetadata | None,
    *,
    tolerance: float = MBTILES_BOUNDS_TOLERANCE,
) -> ResumeCompatibility:
    """比对"已有 MBTiles 的元数据"与"任务参数"。

    比对四项：范围、最小缩放、最大缩放、图层。前两项是硬性的——往一个
    北京 z12 的库里写杭州 z15 的瓦片，得到的文件既不是北京也不是杭州，
    只是坏掉的数据；图层同理（矢量底图和影像底图混在一起肉眼可见地花）。

    ``metadata is None`` 表示"文件不存在或读不出元数据"：此时**视为兼容**
    （当作全新下载），由调用方在日志里说明。

    Args:
        task: 任务记录。
        metadata: 已有文件的元数据；``None`` 表示没有可比对的对象。
        tolerance: 经纬度允许偏差（度）。

    Returns:
        :class:`ResumeCompatibility`
    """
    result = ResumeCompatibility(
        target_tiles=max(0, int(task.total_tiles)),
    )
    if metadata is None:
        return result

    # --- 范围 ---
    for name, task_value, meta_value in (
        ("西边界", task.west, metadata.west),
        ("南边界", task.south, metadata.south),
        ("东边界", task.east, metadata.east),
        ("北边界", task.north, metadata.north),
    ):
        if abs(float(task_value) - float(meta_value)) > tolerance:
            result.problems.append(
                f"{name}不一致：任务 {float(task_value):.6f}，文件 {float(meta_value):.6f}"
            )

    # --- 缩放 ---
    if int(task.min_zoom) != int(metadata.min_zoom):
        result.problems.append(
            f"最小缩放不一致：任务 z{int(task.min_zoom)}，文件 z{int(metadata.min_zoom)}"
        )
    if int(task.max_zoom) != int(metadata.max_zoom):
        result.problems.append(
            f"最大缩放不一致：任务 z{int(task.max_zoom)}，文件 z{int(metadata.max_zoom)}"
        )

    # --- 图层 ---
    # 图层不是 MBTiles 的标准字段，本工具把它写进 description。
    # 提取得到才比对；提取不到就**不判不兼容**（否则会把别的工具生成的文件、
    # 或用户自己改过 description 的文件一律拒之门外）。
    if task.map_layer:
        detected = _layer_tokens_in_description(metadata.description)
        if detected and task.map_layer not in detected:
            result.problems.append(
                f"图层不一致：任务 {task.map_layer}，文件 {'/'.join(detected)}"
            )
        elif not detected and metadata.description and (
            metadata.description != _default_description(task)
        ):
            result.warnings.append(
                "文件的 description 与本次任务不同（无法从中识别图层，已放行）"
            )

    # --- 地图类型（Slice 6，Spec §12）---
    # 只有两边都说得清才比对：Slice ≤5 生成的文件根本没有 ``map_type`` 这一项，
    # 拿空字符串去比会给每个老文件都判一次"不一致"，那是误报不是保护。
    task_map_type = _task_map_type(task)
    file_map_type = (metadata.source_map_type or "").strip().lower()
    if task_map_type and file_map_type and task_map_type != file_map_type:
        result.problems.append(
            f"地图类型不一致：任务 {task_map_type}，文件 {file_map_type}"
        )

    # --- 合成方式（Slice 6，Spec §12）---
    # 底图瓦片与"底图 + 注记"的合成瓦片**不可混写**：混出来的地图
    # "有的地块有字、有的没有"，在手机上看着完全正常，只是内容不全 ——
    # 属于最难被发现的那类缺陷。因此这里比 description 严格得多：**一律拦截**。
    #
    # 文件没有 ``composition`` 项 = Slice ≤5 的产物，那时只可能下过单层底图，
    # 所以按 ``single`` 对待。于是"老文件 + 勾了中文注记"会被拒（正确：
    # 老文件里没有注记）；"老文件 + 不勾注记"照常继续（正确：语义一致）。
    task_composition = _task_composition(task)
    file_composition = (metadata.composition or "").strip().lower() or COMPOSITION_SINGLE
    if task_composition != file_composition:
        result.problems.append(
            f"合成方式不一致：任务 {task_composition}，文件 {file_composition}"
            "（底图与合成瓦片不能混在同一个地图包里）"
        )

    # --- 区域（Slice 16，Spec §12）---
    _check_region_compatibility(task, metadata, result)

    result.compatible = not result.problems
    return result


def _check_region_compatibility(
    task: DownloadTask,
    metadata: MBTilesMetadata,
    result: ResumeCompatibility,
) -> None:
    """比对区域标识（Slice 16，Spec §12）。

    三条规则，都遵守同一个原则：**只在两边都说得清时才比对**（与 ``map_type``
    一致）。Slice ≤15 生成的文件根本没有 ``region_id`` / ``boundary_version``，
    拿空串去比会给每一个老文件都判一次"不一致"—— 那是误报，不是保护。

    1. 两边都有 ``region_id`` 且不同 → **硬拦**。用户要的是 A 省，文件里是
       B 省的瓦片，继续往里写得到的既不是 A 也不是 B。
    2. 任务是行政区域、而文件**明确**记录着 ``region_type=rectangle`` →
       仅提示。文件里的矩形瓦片仍然有效，续传也不会删它们；但用户该知道
       "这个包里混着区域外的瓦片"。
    3. 两边都有 ``boundary_version`` 且不同 → 仅提示。边界数据换版本意味着
       区域形状可能变了：续传会把新的补齐，但**不会**清掉按旧边界下的那些。
    """
    task_region_id = str(getattr(task, "region_id", "") or "").strip()
    file_region_id = (metadata.region_id or "").strip()
    if task_region_id and file_region_id and task_region_id != file_region_id:
        result.problems.append(
            f"行政区域不一致：任务 {task_region_id}，文件 {file_region_id}"
            "（不同区域的瓦片不能混在同一个地图包里）"
        )

    file_region_type = (metadata.region_type or "").strip().lower()
    if task_region_id and file_region_type == "rectangle":
        result.warnings.append(
            "文件是按矩形范围下载的，里面含有区域外的瓦片；"
            "本次续传不会删除它们"
        )

    task_version = str(getattr(task, "boundary_version", "") or "").strip()
    file_version = (metadata.boundary_version or "").strip()
    if task_version and file_version and task_version != file_version:
        result.warnings.append(
            f"边界版本不同：任务 {task_version}，文件 {file_version}"
            "（按旧边界下载的瓦片不会被删除，必要时请重新下载）"
        )


def _task_map_type(task) -> str:
    """从任务记录里取出地图类型字符串；认不出来返回空串（= 不比对）。

    鸭子类型：只要求对象身上有 :attr:`DownloadTask.source_map_type` /
    ``map_type`` / ``map_layer`` 里的任意一个。测试里的轻量替身也走得通。
    """
    resolved = getattr(task, "source_map_type", None)
    if isinstance(resolved, MapType):
        return resolved.value
    raw = str(getattr(task, "map_type", "") or "").strip().lower()
    if raw:
        return raw
    layer = str(getattr(task, "map_layer", "") or "")
    if not layer:
        return ""
    try:
        return MapType.from_layer(layer).value
    except ValueError:
        return ""


def _task_composition(task) -> str:
    """从任务记录里取出合成方式（``single`` / ``merged``）。"""
    value = getattr(task, "composition", None)
    if isinstance(value, str) and value:
        return value
    return composition_for(bool(getattr(task, "with_labels", False)))


def _layer_tokens_in_description(description: str) -> list[str]:
    """从 ``description`` 里尽力提取图层标识（形如 ``vec_w`` / ``cia_w``）。"""
    found: list[str] = []
    for match in re.finditer(r"([a-z]{2,4}_[a-z])\b", description or ""):
        token = match.group(1)
        if token not in found:
            found.append(token)
    return found


def _default_description(task: DownloadTask) -> str:
    """本工具写进 ``description`` 的标准内容（与 Slice 4 的写法保持一致）。

    Slice 6 起合成模式把**两个图层**都写进去：续传校验会从 ``description``
    里提取图层标识，只写底图的话，"任务是合成、文件里只有底图"这类不一致
    就少了一道本该有的线索。
    """
    layers = " + ".join(item.value for item in _task_layers(task))
    return (
        f"天地图 {layers or task.map_layer} "
        f"z{task.min_zoom}~{task.max_zoom} "
        f"{task.bounds}"
    )


def _task_layers(task) -> tuple[MapLayerType, ...]:
    """任务实际要下载的图层（合成模式是"底图 + 注记"两层，底图在前）。"""
    with_labels = bool(getattr(task, "with_labels", False))
    map_type = getattr(task, "source_map_type", None)
    if with_labels and isinstance(map_type, MapType):
        return map_type.layers(True)
    raw_layer = str(getattr(task, "map_layer", "") or "")
    try:
        return (MapLayerType(raw_layer),)
    except ValueError:
        return map_type.layers(False) if isinstance(map_type, MapType) else ()


def _resolve_task_target(task) -> tuple[LayerArgument, bool]:
    """由任务记录还原"这次要下什么"。

    返回 ``(layer, with_labels)``：

    - 任务记录了中文注记合成 → ``(MapType, True)``，服务层据此走合成链路；
    - 否则优先用记录里那个**具体图层** —— Slice ≤5 的记录就是这个语义，
      行为一字不变（包括"只下注记层"这种少见但合法的记录）；
    - 具体图层认不出来时，退回由地图类型展开出的底图。这时按"记录已经损坏"
      处理，不猜。

    Raises:
        MapResumeError: 图层与地图类型都认不出来。
    """
    with_labels = bool(getattr(task, "with_labels", False))
    map_type = getattr(task, "source_map_type", None)
    if with_labels:
        if not isinstance(map_type, MapType):
            raise MapResumeError(
                f"任务 {getattr(task, 'id', '?')} 记录了中文注记合成，"
                f"但地图类型无法识别：{getattr(task, 'map_type', '')!r}；"
                f"支持 {[item.value for item in MapType]}"
            )
        return (map_type, True)

    raw_layer = str(getattr(task, "map_layer", "") or "")
    if raw_layer:
        try:
            return (MapLayerType(raw_layer), False)
        except ValueError:
            pass
    if isinstance(map_type, MapType):
        return (map_type.base_layer, False)
    raise MapResumeError(
        f"任务 {getattr(task, 'id', '?')} 的图层无法识别：{raw_layer!r}；"
        f"支持 {[item.value for item in MapLayerType]}"
    )


def read_existing_metadata(path: str | Path) -> MBTilesMetadata | None:
    """读取已有 ``.mbtiles`` 的元数据，**宽容地**还原成模型。

    刻意不复用 :class:`MBTilesMetadata` 的严格构造器做"校验式读取"：
    那个构造器对 ``format`` 有白名单，对数值有范围要求。别的工具生成的文件
    完全可能带一个我们没见过的 format，而我们的目标是"读出信息用来比对"，
    不是"拒绝这个文件"。因此不认识的字段退回默认值，完全读不出来才返回 ``None``。

    Args:
        path: MBTiles 路径。

    Returns:
        元数据；文件不存在 / 不是合法 SQLite / 缺关键键时返回 ``None``。
    """
    target = Path(path)
    if not target.is_file():
        return None
    try:
        with MBTilesReader(target) as reader:
            raw = reader.get_metadata()
    except (sqlite3.DatabaseError, OSError, RuntimeError):
        return None

    bounds = _parse_metadata_bounds(raw.get("bounds", ""))
    if bounds is None:
        return None
    west, south, east, north = bounds
    try:
        min_zoom = int(float(raw.get("minzoom", "")))
        max_zoom = int(float(raw.get("maxzoom", "")))
    except (TypeError, ValueError):
        return None

    common = {
        "name": raw.get("name") or target.stem,
        "description": raw.get("description", ""),
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "west": west,
        "south": south,
        "east": east,
        "north": north,
        # Slice 6 的本工具自有字段。**必须读回来**：续传一致性校验正是靠
        # ``composition`` / ``map_type`` 判断"这个包还能不能接着往里写"。
        # 漏掉它们会让每个合成文件的续传都被误判成"合成方式不一致"，
        # 而且报错信息会说文件是 single——因为空串会被当成 Slice ≤5 的老文件。
        "provider": raw.get("provider", ""),
        "source_map_type": raw.get("map_type", ""),
        "composition": raw.get("composition", ""),
    }
    labels = _parse_metadata_bool(raw.get("labels"))
    for candidate in (
        {"format": (raw.get("format") or "png").strip().lower()},
        {"format": "png"},
    ):
        try:
            return MBTilesMetadata(with_labels=labels, **candidate, **common)
        except ValueError:
            # format 不认识：退回 png 再试一次；仍失败就认定"读不出元数据"。
            continue
    return None


def _parse_metadata_bool(value: str | None) -> bool | None:
    """解析 metadata 里的 ``true`` / ``false``。

    ``None`` 表示"这个键不存在"——不能粗暴地当成 ``False``，
    否则一个 Slice ≤5 的老文件会被误报成"明确声明没有注记"。
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def _parse_metadata_bounds(value: str) -> tuple[float, float, float, float] | None:
    """解析 metadata 里的 ``west,south,east,north``。"""
    parts = [piece.strip() for piece in str(value).split(",")]
    if len(parts) != 4:
        return None
    try:
        west, south, east, north = (float(piece) for piece in parts)
    except ValueError:
        return None
    return (west, south, east, north)


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

    # --- Slice 6：本次下的是什么 ---
    #: 地图类型（``MapType.value``）
    map_type: str = ""
    #: 瓦片里是否已经合成了中文注记
    with_labels: bool = False

    # --- Slice 5：续传相关 ---
    #: 本次是否是"继续已有任务"
    resumed: bool = False
    #: 开始前检测到的已有瓦片数
    existing_tiles: int = 0
    #: 任务目标瓦片数
    target_tiles: int = 0
    #: 存在性判断走的哪条路（批量缓存 / 索引查询）
    skip_mode: SkipMode | None = None
    #: 不能标成"已完成"的原因（``None`` 表示没有阻碍）
    stop_reason: str | None = None
    #: Slice 8（Spec §22）：完成后的**快速诊断**结论。``validation`` 是它的
    #: 窄视图（老接口），这里保留完整结论是为了让完成小结能显示
    #: "缺失瓦片 / 抽样损坏"这类 ``MBTilesValidationResult`` 装不下的信息。
    diagnostics: MBTilesDiagnosticsResult | None = None

    @property
    def composition_text(self) -> str:
        """一行合成说明（"这次产物到底是什么形态"）。

        直接复用 metadata 的判定，不在这里重写一遍 ``with_labels`` 的分支：
        结果区、日志、任务记录三处说的必须是同一件事。
        """
        if self.with_labels:
            return UI_TEXT_COMPOSITION_MERGED
        return UI_TEXT_COMPOSITION_SINGLE

    @property
    def cancelled(self) -> bool:
        """是否是用户取消。"""
        return self.summary.cancelled

    @property
    def pending_tiles(self) -> int:
        """还需要下载的张数（目标 - 库里实际有的）。"""
        return max(0, self.target_tiles - self.tile_count)

    @property
    def is_success(self) -> bool:
        """是否"跑完 + 零失败 + 文件通过校验 + 库里张数达标"。

        Spec §19 的完成判定：光"没报错"不算完成，必须
        **数据库有效瓦片数 ≈ 任务目标瓦片数**。少了几张（服务端 404、
        网络抖动重试耗尽）时不能标成已完成，否则用户以为拿到了完整地图。
        """
        if self.cancelled or not self.validation.valid:
            return False
        if self.summary.failed:
            return False
        return not (self.target_tiles and self.tile_count < self.target_tiles)

    @property
    def size_text(self) -> str:
        """格式化后的文件体积。"""
        return format_size(self.file_size)

    @property
    def failure_text(self) -> str:
        """一句话说明"为什么没完成"，用于写进任务记录的 ``error``。"""
        if self.cancelled:
            return "任务已取消"
        if not self.validation.valid:
            errors = "；".join(self.validation.errors) or "校验未通过"
            return f"MBTiles 校验未通过：{errors}"
        if self.summary.failed:
            detail = "；".join(
                f"{reason}（{count} 张）" for reason, count in self.summary.top_failures()
            )
            return f"有 {self.summary.failed:,} 张瓦片下载失败：{detail}"
        if self.target_tiles and self.tile_count < self.target_tiles:
            return (
                f"库中瓦片数不足：{self.tile_count:,} / {self.target_tiles:,}"
                f"（缺 {self.pending_tiles:,} 张）"
            )
        return ""

    @property
    def verdict(self) -> DownloadTaskStatus:
        """这次结果对应的任务状态（``COMPLETED`` / ``FAILED`` / ``CANCELLED``）。"""
        if self.cancelled:
            return DownloadTaskStatus.CANCELLED
        if self.is_success:
            return DownloadTaskStatus.COMPLETED
        return DownloadTaskStatus.FAILED

    def as_lines(self) -> list[str]:
        """多行展示文案（GUI 结果区 / 日志逐行展示）。"""
        lines = [
            f"文件：{self.output_path}",
            f"瓦片：{self.tile_count:,}",
            f"大小：{self.size_text}",
            f"Zoom：{self.validation.zoom_text}",
            f"验证：{self.validation.status_text}",
        ]
        if self.resumed:
            lines.append(
                f"续传：已有 {self.existing_tiles:,} 张，本次跳过 "
                f"{self.summary.skipped:,} 张，新下载 {self.summary.succeeded:,} 张"
            )
        elif self.summary.skipped:
            lines.append(f"已有跳过：{self.summary.skipped:,} 张")
        if self.target_tiles:
            lines.append(
                f"目标：{self.target_tiles:,} 张（偏差 "
                f"{self.tile_count - self.target_tiles:+,}）"
            )
        lines.extend(
            f"失败原因：{reason}（{count:,} 张）"
            for reason, count in self.summary.top_failures()
        )
        if self.discarded:
            lines.append(f"写库失败丢弃：{self.discarded:,} 张")
        if self.summary.unprocessed:
            lines.append(f"未处理（取消）：{self.summary.unprocessed:,} 张")
        lines.extend(self.validation.errors)
        lines.extend(self.extra_lines)
        return lines


def _apply_task_verdict(task, task_service, outcome: MapDownloadOutcome) -> None:
    """把 :meth:`MapDownloadOutcome.verdict` 的结果写进任务记录（Spec §18 / §19）。

    放在服务层而不是 GUI 层的原因：任务的最终状态是**数据**，不是"界面效果"。
    脚本、测试、将来的定时任务都可能直接调 ``MapDownloadService``；状态落盘
    若只写在某一个 GUI 槽里，那些调用方就会留下一条永远 ``DOWNLOADING`` 的记录。

    写盘失败**只记日志**：任务已经下完了，不能因为 tasks.json 不可写而让整个
    下载被判定为失败。
    """
    if task is None or task_service is None:
        return
    try:
        verdict = outcome.verdict
        if verdict is DownloadTaskStatus.COMPLETED:
            task_service.complete_task(task)
        elif verdict is DownloadTaskStatus.CANCELLED:
            task_service.cancel_task(task)
        else:
            task_service.fail_task(task, outcome.failure_text or "未达到完成条件")
        # Spec §23 要求有一行能一眼看出"这个任务结束了、结果是什么"的日志
        _logger.info(
            "Task %s id=%s tiles=%d/%d failed=%d skipped=%d",
            verdict.value,
            task.id,
            outcome.tile_count,
            outcome.target_tiles,
            outcome.summary.failed,
            outcome.summary.skipped,
        )
    except Exception as exc:  # noqa: BLE001 - 见 docstring：落盘失败不该影响下载结论
        _logger.warning("任务终态落盘失败 id=%s：%s", getattr(task, "id", "?"), exc)


def _mark_task_failed(task, task_service, message: str) -> None:
    """把任务标成 ``FAILED`` 并记下原因（写库阶段炸掉时用）。"""
    if task is None or task_service is None:
        return
    try:
        task_service.fail_task(task, message)
    except Exception as exc:  # noqa: BLE001 - 同上：落盘失败不该盖住真正的错误
        _logger.warning("任务失败态落盘失败 id=%s：%s", getattr(task, "id", "?"), exc)


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
        provider: TiandituProvider | None = None,
        *,
        concurrency: int = DEFAULT_DOWNLOAD_CONCURRENCY,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        retry_policy: RetryPolicy | None = None,
        rate_limiter: RateLimiter | None = None,
        queue_capacity: int = DEFAULT_MBTILES_QUEUE_CAPACITY,
        batch_size: int = DEFAULT_MBTILES_BATCH_SIZE,
        progress_interval_seconds: float = PROGRESS_EMIT_INTERVAL_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        composer: TileComposer | None = None,
    ) -> None:
        """
        Args:
            provider: 瓦片源（持有 Token）。可以传 ``None``，此时只能通过
                :meth:`resume_task` 的 ``token`` 参数提供（恢复任务时 Token
                来自界面输入，不在任务记录里）。
            concurrency: HTTP 并发数。
            timeout: 单请求超时（秒）。
            retry_policy: 重试策略，``None`` 用引擎默认。
            rate_limiter: 限速器，``None`` 用引擎默认。
            queue_capacity: MBTiles 写队列容量。
            batch_size: MBTiles 批量事务大小。
            progress_interval_seconds: 进度回调节流间隔。
            transport: httpx 传输层，**仅供测试注入 ``httpx.MockTransport``**。
            composer: 图像合成器（Slice 6）。``None`` 表示用
                :class:`~app.composer.TileComposer` 的默认配置；
                单独留一个注入口是为了将来能换图像后端（例如带金字塔抽稀的
                实现）而不必改服务层。

        注意 ``transport`` 必须在这里暴露：``TileDownloader`` 用的是自己的
        transport，``TiandituProvider`` 上挂的那个不影响下载器。少了这个参数，
        集成测试就只能去打真实天地图——那既不允许也不稳定。
        续传时由 ``token`` 现造的 provider 也会带上这个 transport（见
        :meth:`_resolve_provider`），否则"恢复了任务却打了真网"这种事故
        会在测试里悄悄发生。
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
        self._composer = composer
        #: Slice 8（Spec §22）：下载结束后的校验升级为"快速诊断"。
        #: 一个无状态的纯函数式对象，可以安全地常驻复用。
        self._inspection = MBTilesInspectionService()

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
    # 主流程：全新下载
    # ------------------------------------------------------------------ #
    async def download_to_mbtiles(
        self,
        bounds: GeoBounds,
        min_zoom: int,
        max_zoom: int,
        layer: LayerArgument,
        output_path: str | Path,
        *,
        with_labels: bool = False,
        map_name: str | None = None,
        description: str = "",
        image_format: str = "png",
        on_progress=None,
        resume_policy: ResumePolicy | None = None,
        task: DownloadTask | None = None,
        task_service=None,
        region: DownloadRegion | None = None,
    ) -> MapDownloadOutcome:
        """下载给定范围并写成 MBTiles，返回最终结果。

        Args:
            bounds: 下载范围。
            min_zoom / max_zoom: 缩放区间（含两端）。
            layer: 下载目标。传 :class:`~app.models.MapLayerType` 时**只下这一层**；
                传 :class:`~app.models.MapType` 时按 :attr:`with_labels` 决定
                是"只下底图"还是"底图 + 中文注记合成成一张"。
            with_labels: 是否把中文注记合成进瓦片。**只能与 ``MapType`` 一起用**
                ——一块注记瓦片没法自己回答"它属于矢量还是影像"。
            output_path: 输出的 ``.mbtiles`` 路径（父目录自动创建）。
            map_name: MBTiles 的 ``name``；``None`` 时取输出文件名主体。
            description: MBTiles 的 ``description``。
            image_format: 瓦片图片格式。合成模式强制 PNG（注记依赖透明通道）。
            on_progress: 进度回调（``DownloadProgress``），已节流。
            resume_policy: 跳过策略。默认 ``None`` 即"不做跳过"——
                全新下载的目标是"这个文件从无到有"，跳过已有瓦片是**续传**
                才需要的行为（GUI 在用户选"继续下载"时会显式传入）。
            task: 关联的任务记录（可选）。给了就会随进度落盘，
                使"程序被强杀"最多只丢 1 秒的进度。
            task_service: 任务服务（与 ``task`` 成对出现）。
            region: 下载区域（Slice 16）。``None`` 表示"按 ``bounds`` 这个矩形下"
                ——行为与 Slice 0~15 逐位一致；传多边形区域时，只有与行政区域
                **相交**的瓦片会被下载，且进度分母是 selected 而不是候选数。

        Returns:
            :class:`MapDownloadOutcome`

        Raises:
            MBTilesWriteError: **写库阶段**失败（磁盘满、库损坏等）。
                这种错误无法继续，必须让用户看见，因此不作降级处理。
            GeoJSONError: 多边形区域的边界文件不可读 / 不合法。
                **不会**退化成矩形下载：那会静默下一大片区域外瓦片。
            ValueError: 参数非法（含"注记层没有对应的地图类型"）。
        """
        return await self._execute(
            provider=self._provider,
            bounds=bounds,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            layer=layer,
            with_labels=with_labels,
            output_path=Path(output_path),
            map_name=map_name,
            description=description,
            image_format=image_format,
            on_progress=on_progress,
            resume_policy=resume_policy,
            task=task,
            task_service=task_service,
            resumed=False,
            region=region,
        )

    # ------------------------------------------------------------------ #
    # 主流程：断点续传（Slice 5）
    # ------------------------------------------------------------------ #
    async def resume_task(
        self,
        task: DownloadTask,
        token: str = "",
        *,
        on_progress=None,
        task_service=None,
        resume_policy: ResumePolicy | None = None,
    ) -> MapDownloadOutcome:
        """继续一个已有任务：只下载缺失的瓦片，写回同一个 MBTiles。

        流程严格按 Spec §11 / §12：

        ::

            读取 Task
                ↓
            检查 output_path（不存在 → 当全新下载，并有日志说明）
                ↓
            打开已有 MBTiles 读 metadata
                ↓
            一致性校验（bounds / 最小缩放 / 最大缩放 / 图层）
                ↓ 不匹配 → 抛 MapResumeError，**一张都不写**
            计算完整 Tile Range
                ↓
            检测已有瓦片（默认策略：只查坐标三列，不读 BLOB）
                ↓
            已有 → skip；缺失 → download
                ↓
            写入同一个 MBTiles（INSERT OR REPLACE，不会产生重复行）
                ↓
            任务进度持续落盘

        Args:
            task: 任务记录（来自 ``tasks.json``）。
            token: 天地图 Token。**Token 不在任务记录里**（Spec §9），
                恢复时要由界面重新提供；传空串则沿用构造本服务时的 provider。
            on_progress: 进度回调。
            task_service: 任务服务；给了就由本方法负责把进度与最终状态落盘。
            resume_policy: 跳过策略，``None`` 用 :class:`ResumePolicy` 默认值
                （跳过已有、不校验内容）。

        Returns:
            :class:`MapDownloadOutcome`。

        Raises:
            MapResumeError: 参数不兼容 / Token 缺失 / 图层无法识别。
            MBTilesWriteError: 写库失败。
        """
        if not isinstance(task, DownloadTask):
            raise ValueError(
                f"task 必须是 DownloadTask，实际为 {type(task).__name__}"
            )

        # --- 1. 任务参数还原（bounds 的合法性由 GeoBounds 负责） ---
        try:
            bounds = GeoBounds(task.west, task.south, task.east, task.north)
        except ValueError as exc:
            raise MapResumeError(
                f"任务 {task.id} 的范围非法，无法继续：{exc}"
            ) from exc
        # --- 1b. 下载目标：具体图层 / 地图类型 + 是否合成（Slice 6） ---
        target, with_labels = _resolve_task_target(task)

        # --- 1c. 区域（Slice 16）：老任务没有这三个字段 → 矩形，行为与以前一致 ---
        region = region_from_task(task)
        if region is None:
            # 任务记录说是多边形、但本版本找不到对应预设：**不能猜**。
            # 按矩形续传会把区域外的瓦片也下下来，等于悄悄改变了这个任务的定义。
            raise MapResumeError(
                f"任务 {task.id} 记录的区域（region_id={task.region_id!r}）"
                "在本版本找不到对应的行政区边界，无法安全续传"
            )

        path = Path(task.output_path)
        _logger.info("Resume task: %s", task.id)

        # --- 2. provider（Token 只在这一刻从内存里取，绝不落盘） ---
        provider = self._resolve_provider(token)

        # --- 3. 已有文件的一致性校验 ---
        policy = resume_policy or ResumePolicy()
        metadata: MBTilesMetadata | None = None
        if path.is_file():
            metadata = read_existing_metadata(path)
            if metadata is None:
                raise MapResumeError(
                    f"已有文件无法读取元数据，不能安全续传：{path}"
                )
            compatibility = check_resume_compatibility(task, metadata)
            if not compatibility.compatible:
                # 关键：**一张都不写**。往不属于这个任务的地图包里写瓦片，
                # 得到的既不是 A 也不是 B，只是一份坏数据。
                raise MapResumeError(compatibility.message)
            for message in compatibility.warnings:
                _logger.warning("续传提示：%s", message)
        else:
            _logger.warning("目标文件不存在，按全新下载处理：%s", path)

        # --- 4. 完整 Tile Range 与"已有 / 待下载" ---
        # 分母必须是"这次任务真正该有多少张"：多边形区域下它是 selected，
        # 用矩形的候选数会让续传永远显示"还差 N 张"却再也下不动（Spec §12）。
        total = region_target_tiles(region, bounds, task.min_zoom, task.max_zoom)
        zooms = list(range(int(task.min_zoom), int(task.max_zoom) + 1))
        existing = count_existing_region_tiles(path, zooms, region)
        task.total_tiles = total
        task.skipped_tiles = 0
        # 区域信息一并写回任务记录：老任务第一次续传后就补上了这三个字段
        task.region_type = region.region_type.value
        task.region_id = region.region_id or ""
        task.boundary_version = region.boundary_version or ""
        _logger.info("Existing tiles: %d", existing)
        _logger.info("Remaining tiles: %d", max(0, total - existing))
        for line in describe_existing_tiles(total, existing):
            _logger.info("Resume %s", line)
        _logger.info("Resume download started")

        if task_service is not None:
            task_service.resume_task(task)

        # --- 5. 复用同一条落盘流水线（跳过判定由 resume_policy 驱动） ---
        outcome = await self._execute(
            provider=provider,
            bounds=bounds,
            min_zoom=int(task.min_zoom),
            max_zoom=int(task.max_zoom),
            layer=target,
            with_labels=with_labels,
            output_path=path,
            map_name=task.name or None,
            description=metadata.description if metadata else _default_description(task),
            # 续传时**不重写**已有元数据（见 _execute 里 has_metadata 的判断）
            image_format=metadata.format if metadata else "png",
            on_progress=on_progress,
            resume_policy=policy,
            task=task,
            task_service=task_service,
            resumed=True,
            # **必须**把区域传下去：漏了它，续传会按矩形候选区间去下，
            # 于是一边报"目标 60 张"、一边把区域外那 40 张也下下来 ——
            # 任务的定义被悄悄改掉了，而且只有对着账单才看得出（Spec §12）。
            region=region,
        )
        _logger.info(
            "Resume finished id=%s skipped=%d downloaded=%d failed=%d db_tiles=%d/%d",
            task.id,
            outcome.summary.skipped,
            outcome.summary.succeeded,
            outcome.summary.failed,
            outcome.tile_count,
            outcome.target_tiles,
        )
        return outcome

    # ------------------------------------------------------------------ #
    # 内部：落盘流水线（全新下载与续传共用）
    # ------------------------------------------------------------------ #
    async def _execute(
        self,
        *,
        provider: TiandituProvider | None,
        bounds: GeoBounds,
        min_zoom: int,
        max_zoom: int,
        layer: LayerArgument,
        with_labels: bool,
        output_path: Path,
        map_name: str | None,
        description: str,
        image_format: str,
        on_progress,
        resume_policy: ResumePolicy | None,
        task: DownloadTask | None,
        task_service,
        resumed: bool,
        region: DownloadRegion | None = None,
    ) -> MapDownloadOutcome:
        """建库 → 起 sink → 下载 → drain → close → 校验（两条入口共用）。"""
        if not isinstance(bounds, GeoBounds):
            raise ValueError(f"bounds 必须是 GeoBounds，实际为 {type(bounds).__name__}")
        if not isinstance(layer, (MapLayerType, MapType)):
            raise ValueError(
                f"layer 必须是 MapLayerType 或 MapType，实际为 {layer!r}；"
                f"支持图层 {[item.value for item in MapLayerType]} "
                f"或地图类型 {[item.value for item in MapType]}"
            )
        if with_labels and not isinstance(layer, MapType):
            # 一块注记瓦片没法自己回答"它属于矢量还是影像"，所以合成模式
            # 只能从"地图类型"出发。这里直接拒绝而不是猜一个类型：
            # 猜错会把影像的注记贴到矢量底图上，成品看着"有字"但整体是错的。
            raise ValueError(
                "with_labels=True 需要传 MapType（矢量 / 影像地图）；"
                f"实际传的是 {layer!r}（单图层）。"
                "请改传 MapType.VECTOR / MapType.IMAGE，或把 with_labels 设为 False。"
            )

        # --- 下载目标解析（Spec §2：从唯一那张映射表里取图层） ---
        map_type = layer if isinstance(layer, MapType) else MapType.from_layer(layer)
        merged = bool(with_labels)
        path = output_path
        name = map_name or path.stem

        # --- 区域计划（Slice 16）---
        # 放在**建库之前**：边界文件缺失/损坏时应当当场失败，而不是先建出一个
        # 空 .mbtiles 再抛异常 —— 后者会在输出目录里留下一堆无法解释的空文件。
        region_filter = build_region_filter(region)
        total = (
            estimate_tile_count(bounds, min_zoom, max_zoom)
            if region_filter is None
            else RegionTileEstimator(
                region, region_filter=region_filter
            ).estimate(min_zoom, max_zoom).selected_tiles
        )
        if region_filter is not None:
            _logger.info(
                "Polygon region: %s id=%s version=%s selected=%d",
                region.name,
                region.region_id,
                region.boundary_version,
                total,
            )

        if merged and str(image_format).strip().lower() != "png":
            # 合成产物一律 PNG（Spec §4）：注记依赖透明通道，JPEG 存不下 alpha。
            # 与其落一个"metadata 说 jpg、内容其实是 png"的库，不如当场拒绝。
            raise ValueError(
                f"合成模式（中文注记）只能输出 PNG，实际要求 {image_format!r}"
            )

        metadata = MBTilesMetadata.for_bounds(
            bounds,
            min_zoom,
            max_zoom,
            name=name,
            description=description,
            image_format=image_format,
            provider=MBTILES_PROVIDER_TIANDITU,
            source_map_type=map_type.value,
            with_labels=merged,
            composition=composition_for(merged),
            # Spec §19：矩形也要显式写 region_type=rectangle —— Inspector 才不用
            # 靠"没有这个键"去猜（那也是老文件的情形，两者得分得开）。
            region_type=region.region_type.value if region else "rectangle",
            region_id=region.region_id if region else "",
            boundary_version=region.boundary_version if region else "",
            # Slice 25（Spec §20 修正）：等级**所有区域都写** —— 矩形是 custom、
            # 行政区是 country/province/city、任务区是 mission。
            # Slice 22 只给任务区写，理由是"行政区的等级能从 region_id 反查"；
            # 那只对 Windows 自己成立 —— Android 侧没有区域目录，反查不出来，
            # 行政区包在那边全落到 RegionLevel.UNKNOWN，Slice 24 的等级优先级
            # （"更具体的一级才能换下当前包"）整条失效。
            # 没有 region（纯矩形下载）时写 custom 而不是留空：留空在 Android 侧
            # 与"老包没有这条信息"收敛到同一个 UNKNOWN，而这两件事不一样。
            region_level=(region.region_level.value if region else "custom"),
            # 走廊半径只对任务区有意义（Spec §20），别的区域类型不写。
            buffer_meters=(
                region.buffer_meters if region and region.is_mission else ""
            ),
        )

        path.parent.mkdir(parents=True, exist_ok=True)
        writer = MBTilesWriter(path, batch_size=self._batch_size)
        try:
            writer.open()
            if not writer.has_metadata:
                # 新库（或只有空表的文件）才写元数据；续传已有库时保留原有内容，
                # 免得把用户改过的 name / description 覆盖掉。
                writer.write_metadata(metadata)
        except Exception as exc:  # noqa: BLE001 - 建库失败统一归为写库失败
            # 建库阶段挂了（目录不可写、磁盘满、磁盘上的库已损坏……）：
            # 首先要保证不留下半个连接，其次要给出统一的异常类型，
            # 让 GUI 只需要认 MBTilesWriteError 一种"写盘失败"。
            writer.abort()
            message = f"无法创建 MBTiles（{path}）：{exc}"
            _mark_task_failed(task, task_service, message)
            raise MBTilesWriteError(message) from exc

        # --- 跳过判定：用 writer 自己的写连接查"库里已有什么" ---
        zooms = list(range(int(min_zoom), int(max_zoom) + 1))
        policy = resume_policy or ResumePolicy.disabled()
        # total 已在建库前算好（多边形下即"选中瓦片数"），这里**不再重算**。
        # 它有三个身份：进度条分母、ExistingTileIndex 的 expected_total
        # （决定走 CACHED 还是 QUERY 模式）、收尾报告里的"目标张数"。
        # 只查一次（GROUP BY 一次拿回所有 zoom 的计数），别再放进生成器里循环调
        counts_by_zoom = writer.count_tiles_by_zoom()
        if region_filter is None:
            existing_tiles = sum(counts_by_zoom.get(zoom, 0) for zoom in zooms)
        else:
            # 多边形任务下"文件里一共有多少行"是个没有意义的数：区域外的瓦片
            # 不算"已有"。报 100 而目标 60，用户会在收尾小结里看到
            # "已有 100 / 目标 60"这种自相矛盾的一行（Spec §12）。
            # 走**同一个函数**，于是服务、GUI 扫描、续传预算三处口径完全一致。
            existing_tiles = count_existing_region_tiles(path, zooms, region)
        index = ExistingTileIndex(
            writer, policy, expected_total=total, zooms=zooms
        )
        if index.mode is SkipMode.CACHED:
            index.prime()
        skip_predicate = make_skip_predicate(index) if policy.enabled else None
        if policy.enabled:
            _logger.info(
                "已有瓦片索引：mode=%s existing=%d target=%d verify=%s",
                index.mode.display_name,
                existing_tiles,
                total,
                policy.verify_existing_tile,
            )

        sink = MBTilesTileSink(writer, queue_capacity=self._queue_capacity)
        sink.start()
        downloader = TileDownloader(
            provider,
            transport=self._transport,
            retry_policy=self._retry_policy,
            rate_limiter=self._rate_limiter,
            timeout=self._timeout,
        )
        # 合成模式：**同一个** TileDownloader 被用两次（底图 + 注记）。
        # 于是连接复用、限速、重试三件事与单层模式完全一致 ——
        # "1 万张瓦片 ≈ 2 万次请求"里的每一次都仍然受 8 请求/秒约束。
        # 生命周期上 CompositeTileDownloader **不拥有**它（谁创建谁关闭），
        # 所以下面 finally 里关的仍然是 downloader 本身。
        worker = (
            CompositeTileDownloader(downloader, self._composer)
            if merged
            else downloader
        )
        # 单层模式下 MapType 要展开成**底图**图层：不勾中文注记时产物就是
        # "没有字的底图"。这一步由服务层写死，不让下载器去猜。
        target: LayerArgument = (
            map_type
            if merged
            else (layer if isinstance(layer, MapLayerType) else map_type.base_layer)
        )
        if merged:
            _logger.info(
                "Composite mode: %s + %s → PNG",
                map_type.base_layer.value,
                map_type.label_layer.value,
            )

        self._sink = sink
        self._tiles_written = 0
        write_error: MBTilesWriteError | None = None

        try:
            manager = DownloadManager(
                worker,
                concurrency=self._concurrency,
                progress_interval_seconds=self._progress_interval,
            )
            self._manager = manager
            summary = await manager.download(
                bounds,
                min_zoom,
                max_zoom,
                target,
                on_tile=sink.consume,
                on_progress=self._make_progress_forwarder(
                    manager, on_progress, task=task, task_service=task_service
                ),
                should_skip_tile=skip_predicate,
                region_filter=region_filter,
                total_tiles=total,
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
            message = (
                f"MBTiles 写入失败（文件 {path}，已入库 {sink.written} 张，"
                f"丢弃 {sink.discarded} 张）：{write_error}"
            )
            _mark_task_failed(task, task_service, message)
            raise MBTilesWriteError(message) from write_error

        tile_count, file_size = self._inspect(path)
        # Spec §22：完成后的校验从"基础校验"升级为"快速诊断"——
        # 结构与统计、metadata、坐标、覆盖完整性、图片抽样全跑一遍，
        # 但 **deep_scan 保持 False**：每次下载结束都全量解码几十万张图片
        # 是不可接受的（那是 GUI 上"深度检查"按钮的活儿）。
        # 抽样张数沿用 Slice 4 的 20 张，落在 Spec 要求的 20~50 区间内。
        diagnostics = self._inspection.inspect(
            path,
            sample_size=MBTILES_VALIDATE_SAMPLE_LIMIT,
            check_coverage=True,
        )
        validation = diagnostics.to_validation()
        _logger.info(
            "Map saved path=%s tiles=%d size=%s zoom=%s valid=%s",
            path.name,
            tile_count,
            format_size(file_size),
            validation.zoom_text,
            validation.status_text,
        )
        if policy.enabled:
            for line in index.stats.as_lines():
                _logger.info(line)

        extra_lines: list[str] = []
        if summary.cancelled:
            extra_lines.append(
                "状态：已取消，已完成瓦片已保留；可在「下载任务」里点「继续」接着下"
            )
        if policy.verify_existing_tile and index.stats.invalid:
            extra_lines.append(
                f"已有瓦片中校验发现 {index.stats.invalid:,} 张损坏"
                + ("，已重新下载覆盖" if policy.redownload_invalid_tile else "，已沿用")
            )

        outcome = MapDownloadOutcome(
            summary=summary,
            output_path=path,
            metadata=metadata,
            validation=validation,
            tile_count=tile_count,
            file_size=file_size,
            discarded=sink.discarded,
            extra_lines=extra_lines,
            map_type=map_type.value,
            with_labels=merged,
            resumed=resumed,
            existing_tiles=existing_tiles,
            target_tiles=total,
            skip_mode=index.mode if policy.enabled else None,
            diagnostics=diagnostics,
        )
        # 终态在**数据流出这一层**落地：GUI / 脚本 / 测试任何调用方都自动获得
        # 正确的任务状态，不必各自记得"跑完之后别忘了写状态"。
        _apply_task_verdict(task, task_service, outcome)
        return outcome

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _resolve_provider(self, token: str | None) -> TiandituProvider:
        """按"有没有传 Token"决定用哪个 provider。

        刻意把 ``self._transport`` 带上：Token 是运行时拿到的，但**测试注入的
        MockTransport 必须继续生效**——否则一个"恢复任务"的用例会突然去打真实
        天地图，既是违规的（测试禁止触网）也是不稳定的。
        """
        cleaned = (token or "").strip()
        if cleaned:
            return TiandituProvider(cleaned, transport=self._transport)
        if self._provider is not None:
            return self._provider
        raise MapResumeError(UI_TEXT_RESUME_TOKEN_REQUIRED)

    def _make_progress_forwarder(
        self,
        manager: DownloadManager,
        on_progress,
        *,
        task: DownloadTask | None = None,
        task_service=None,
    ):
        """包装进度回调：顺手把"写库失败"传导成"取消下载"，并把进度落盘。

        写库挂了还继续下载纯属白耗配额，所以一旦 sink 报错就取消任务，
        真正的异常仍由 :meth:`_execute` 统一抛出。

        任务进度在这里落盘（而不是在 GUI 层）：GUI 的进度槽只在主线程收到
        信号时才跑，而"程序被强杀"恰恰发生在没人处理信号的时候。放在数据流出
        源头这一层，任何调用方（GUI、脚本、测试）都自动获得持久化。
        """

        def forward(progress: DownloadProgress) -> None:
            sink = self._sink
            if sink is not None and sink.failure is not None:
                manager.cancel()
            if task is not None and task_service is not None:
                # update_progress 内部有节流（1 秒 / 100 张），这里不必再判
                task_service.update_progress(task, progress)
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


def count_existing_tiles(path: str | Path, zooms) -> int:
    """统计一个已有 ``.mbtiles`` 在给定 zoom 区间里的瓦片数。

    用 ``GROUP BY zoom_level`` 的计数查询，**不读任何 BLOB**（Spec §22）。
    文件不存在 / 打不开时返回 ``0``——"数不出来"和"没有瓦片"在这个场景下
    的后续动作是一样的（都要去下），没必要让调用方分辨。

    Args:
        path: MBTiles 路径。
        zooms: 要统计的缩放级别（可迭代）。

    Returns:
        瓦片数。
    """
    target = Path(path)
    if not target.is_file():
        return 0
    wanted = {int(zoom) for zoom in zooms}
    try:
        with MBTilesReader(target) as reader:
            counts = reader.count_tiles_by_zoom()
    except (sqlite3.DatabaseError, OSError, RuntimeError):
        return 0
    return sum(count for zoom, count in counts.items() if zoom in wanted)


# --------------------------------------------------------------------------- #
# 区域计划（Slice 16）
# --------------------------------------------------------------------------- #
def region_target_tiles(
    region: DownloadRegion | None,
    bounds: GeoBounds,
    min_zoom: int,
    max_zoom: int,
) -> int:
    """本次任务的**目标瓦片数**（进度分母、续传的"共多少张"）。

    - 矩形（或 ``region`` 为 ``None``）→ 候选数，与 Slice 0~15 逐位一致；
    - 多边形 → 与区域真正相交的瓦片数（``selected``）。

    用候选数当分母的后果不是"数字大一点"：进度条会永远停在 60% 然后突然跳满，
    续传时"还差 1.4 万张"也会一直说不准（Spec §11/§12）。
    """
    if region is None or not region.is_polygon:
        return estimate_tile_count(bounds, min_zoom, max_zoom)
    return RegionTileEstimator(region).estimate(min_zoom, max_zoom).selected_tiles


def count_existing_region_tiles(
    path: str | Path,
    zooms,
    region: DownloadRegion | None,
) -> int:
    """统计库里**落在区域内**的瓦片数（多边形续传的"已有多少张"）。

    为什么不复用 :func:`count_existing_tiles`：那个数是按 zoom 统计**全部行**的，
    如果文件里混着区域外的瓦片（例如这个文件的前身是个矩形任务），"已有"就会
    偏大、"还要下"偏小，用户面对的是一个永远补不平的缺口（Spec §12 明确要求
    "Polygon 外 Tile 不能参与 Resume 统计"）。

    这里逐张过一遍几何判定，与下载时的判据**同源**（同一个
    :class:`~app.geo.PolygonTileRegionFilter`），不存在"下载算一套、统计算另一套"。
    矩形区域直接转发给 :func:`count_existing_tiles`，不做任何额外 IO。
    """
    if region is None or not region.is_polygon:
        return count_existing_tiles(path, zooms)

    region_filter = build_region_filter(region)
    if region_filter is None:  # pragma: no cover - is_polygon 已保证非空
        return count_existing_tiles(path, zooms)

    target = Path(path)
    if not target.is_file():
        return 0
    total = 0
    try:
        with MBTilesReader(target) as reader:
            for zoom in sorted({int(z) for z in zooms}):
                for z, x, y in reader.iter_tile_coordinates(zoom):
                    if region_filter.intersects_tile_xyz(z, x, y):
                        total += 1
    except (sqlite3.DatabaseError, OSError, RuntimeError):
        # 与 count_existing_tiles 同一态度：数不出来就当作 0，
        # 后续动作都是"去下"，没必要让调用方分辨。
        return 0
    return total
