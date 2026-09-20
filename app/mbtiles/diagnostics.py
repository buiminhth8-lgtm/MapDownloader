"""MBTiles 诊断模型与检查规则（Slice 8）。

本模块把"这个 .mbtiles 到底能不能用、哪里有问题"变成一份**结构化、可展示、
可断言、可导出**的结论（:class:`MBTilesDiagnosticsResult`），而不是让用户
自己拿 SQLite 工具去看。

分工（这是本 Slice 最重要的一条边界）
------------------------------------

- :mod:`app.mbtiles.reader` 负责"库里有什么"（SQL 只在那里出现）；
- **本模块**负责"这算不算问题"（所有判定规则集中在这里）；
- :class:`app.services.MBTilesInspectionService` 负责"按什么顺序跑、怎么上报进度、
  怎么兜住异常"。

这样切的收益：规则可以脱离 SQLite 单独断言（构造一个假的 reader 就能测），
而查询不会在三个文件里各写一份。

检查项（Spec §3 ~ §14）
-----------------------

============================  ==================================================
结构（§3）                     文件存在 / 是普通文件 / 可被 SQLite 打开 /
                              ``metadata`` 与 ``tiles`` 表存在 /
                              ``tiles`` 的四个必需字段存在
SQLite 自检（§4）              ``PRAGMA quick_check``（重量级的 ``integrity_check``
                              只在 ``deep_check=True`` 时跑）
metadata 诊断（§5）            format 是否本工具读得动、bounds 是否在
                              ±180 / ±85.05112878 之内、zoom 能否解析、
                              声明 zoom 与实际 zoom 是否一致
Zoom 统计（§6）                ``GROUP BY`` 聚合，不读 tile_data
TMS / XYZ 坐标（§7）           ``0 <= x, tms_y < 2**z``，越界即 ERROR
重复瓦片（§8）                 ``GROUP BY ... HAVING COUNT(*) > 1``
空瓦片（§9）                   ``tile_data IS NULL OR length(tile_data) = 0``
图片抽样（§10）                分层抽样 + magic bytes + Pillow ``verify()``
深度图片扫描（§11）            可选全量，``fetchmany()`` 流式
覆盖完整性（§12/§13/§14）       复用 Slice 1 的 ``calculate_tile_range``，
                              缺失明细封顶 :data:`MAX_MISSING_TILE_DETAILS` 条
============================  ==================================================

坐标约定
--------

对外（本模块的全部入参与结论）一律 **XYZ**；库内的 TMS 只在调用
:func:`app.geo.xyz_y_to_tms_y` / :func:`app.geo.tms_y_to_xyz_y` 的那一瞬出现。
缺失瓦片明细也一律按 XYZ 输出 —— 用户在其它工具里看到的是 XYZ。

只读保证
--------

本模块**只发 SELECT / PRAGMA**，一条写语句都没有；连接本身由 Reader 以
``mode=ro`` 打开（Spec §23）。检查工具绝不修库。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from app.config.settings import (
    DIAGNOSTICS_MISSING_WARNING_RATIO,
    DIAGNOSTICS_STATUS_FAIL,
    DIAGNOSTICS_STATUS_PASS,
    DIAGNOSTICS_STATUS_WARNING,
    DIAGNOSTICS_SUPPORTED_FORMATS,
    MAX_LONGITUDE,
    MAX_MISSING_TILE_DETAILS,
    MIN_LONGITUDE,
    REPORT_DEEP_SCAN_LINE,
    REPORT_ERRORS_HEADER,
    REPORT_EXPECTED_LINE,
    REPORT_FILE_LINE,
    REPORT_METADATA_HEADER,
    REPORT_MISSING_DETAIL_HEADER,
    REPORT_MISSING_DETAIL_TRUNCATED,
    REPORT_MISSING_LINE,
    REPORT_NONE,
    REPORT_NOTICES_HEADER,
    REPORT_RESULT_LINE,
    REPORT_SAMPLE_FORMATS_LINE,
    REPORT_SAMPLE_LINE,
    REPORT_SIZE_LINE,
    REPORT_SQLITE_LINE,
    REPORT_TILE_COUNT_LINE,
    REPORT_WARNINGS_HEADER,
    REPORT_ZOOM_HEADER,
    TILE_MATH_MAX_ZOOM,
    TILE_MATH_MIN_ZOOM,
    WEB_MERCATOR_MAX_LAT,
)
from app.geo import (
    DownloadRegion,
    GeoBounds,
    GeoJSONError,
    PolygonTileRegionFilter,
    RegionType,
    TileRange,
    build_region_filter,
    calculate_tile_range,
    format_size,
    region_from_metadata,
    tms_y_to_xyz_y,
    xyz_y_to_tms_y,
)
from app.mbtiles.models import (
    SUPPORTED_TILE_FORMATS,
    MBTilesValidationResult,
)
from app.mbtiles.reader import MBTilesReader
from app.mbtiles.validator import parse_bool, parse_bounds, parse_zoom

# 复用 Slice 2 的 magic bytes 判定：判断"这是不是一张图片"的规则必须全链路
# 一致，否则会出现"下载时判合法、诊断时判非法"这种自相矛盾。
from app.providers import detect_image_format

__all__ = [
    "ZoomTileStats",
    "InspectionProgress",
    "ImageProbe",
    "MBTilesDiagnosticsResult",
    "check_file",
    "check_structure",
    "check_sqlite_integrity",
    "check_metadata",
    "collect_zoom_stats",
    "check_metadata_zoom_consistency",
    "check_coordinates",
    "check_duplicates",
    "check_empty_tiles",
    "check_samples",
    "check_coverage",
    "collect_missing_details",
    "deep_scan_tiles",
    "merge_validation_into",
    "finalize",
    "render_report",
    "probe_image",
    "is_informational",
    "emit",
    "TILES_REQUIRED_COLUMNS",
    "REQUIRED_TABLES",
]


_logger = logging.getLogger(__name__)


#: ``tiles`` 表的四个必需字段（Spec §3）。
TILES_REQUIRED_COLUMNS: tuple[str, ...] = (
    "zoom_level",
    "tile_column",
    "tile_row",
    "tile_data",
)

#: MBTiles 规范要求的两张表。
REQUIRED_TABLES: tuple[str, ...] = ("metadata", "tiles")

#: 明细类信息最多列几条（坐标、重复组……），避免一个坏库把消息刷成几千行。
_MAX_LISTED_DETAILS = 5


# --------------------------------------------------------------------------- #
# 数据模型（Spec §2）
# --------------------------------------------------------------------------- #
@dataclass
class ZoomTileStats:
    """单个缩放级别的规模与覆盖情况。

    Attributes:
        zoom: 缩放级别。
        tile_count: 库里这个级别实际有多少张。
        min_x / max_x: 实际出现的列号范围（**XYZ** 的 ``x``）。
        min_y_tms / max_y_tms: 实际出现的行号范围（**TMS**，即 ``tile_row``）。
            刻意保留 TMS 原值：这是"库里真的存了什么"，直接对着
            ``SELECT`` 的结果看不需要再做一次换算。
        expected_tiles: 按 ``bounds`` + 该 zoom 理论应该有多少张；
            没做覆盖检查时为 ``None``（"没算"与"算出 0"必须能分辨）。
        missing_tiles: 缺失张数；没做覆盖检查时为 ``None``。
    """

    zoom: int
    tile_count: int
    min_x: int | None = None
    max_x: int | None = None
    min_y_tms: int | None = None
    max_y_tms: int | None = None
    expected_tiles: int | None = None
    missing_tiles: int | None = None

    @property
    def x_count(self) -> int:
        """实际出现的列数；没有瓦片时为 0。"""
        if self.min_x is None or self.max_x is None:
            return 0
        return self.max_x - self.min_x + 1

    @property
    def y_count(self) -> int:
        """实际出现的行数；没有瓦片时为 0。"""
        if self.min_y_tms is None or self.max_y_tms is None:
            return 0
        return self.max_y_tms - self.min_y_tms + 1

    @property
    def missing_ratio(self) -> float | None:
        """缺失比例；没有期望值或期望为 0 时返回 ``None``。"""
        if not self.expected_tiles:
            return None
        return (self.missing_tiles or 0) / self.expected_tiles

    @property
    def coverage_ok(self) -> bool | None:
        """这个 zoom 是否张数正好齐；没做覆盖检查时为 ``None``。"""
        if self.missing_tiles is None:
            return None
        return self.missing_tiles == 0


@dataclass(frozen=True)
class InspectionProgress:
    """一次检查的进度快照（Spec §19）。

    Attributes:
        stage: 阶段名（``"数据库结构"`` / ``"图片"`` ……），GUI 拿它做粗粒度提示。
        message: 已经写好的人类可读文案，GUI 直接显示，不再自己拼。
        current / total: 计数；``total <= 0`` 表示"这个阶段没有可量化的总量"。
    """

    stage: str
    message: str
    current: int = 0
    total: int = 0

    @property
    def percent(self) -> int:
        """百分比；不可量化时返回 ``-1``（进度条应切成"不确定"模式）。"""
        if self.total <= 0:
            return -1
        return max(0, min(100, round(self.current * 100 / self.total)))


@dataclass(frozen=True)
class ImageProbe:
    """一张瓦片的内容探测结果。

    Attributes:
        ok: 能否被 Pillow 正常解码。
        format: 探测到的格式（``"PNG"`` / ``"JPEG"``）；认不出时 ``None``。
        width / height: 像素尺寸（Pillow 认不出来时为 0）。
        error: ``ok=False`` 时的原因。
    """

    ok: bool
    format: str | None = None
    width: int = 0
    height: int = 0
    error: str = ""


@dataclass
class MBTilesDiagnosticsResult:
    """一次 MBTiles 诊断的完整结论（Spec §2）。

    ``errors`` 与 ``warnings`` 的分工即 :attr:`status` 的判定依据：

    - 有 ``errors`` → ``FAIL``（:attr:`valid` 为 ``False``）；
    - 只有 ``warnings`` → ``WARNING``（文件基本可用，但有可疑之处）；
    - 都没有 → ``PASS``。

    三个瓦片计数器刻意分开，因为它们指向**三种完全不同的故障**：

    - :attr:`invalid_tiles`：坐标越界（``x`` / ``tms_y`` 不在 ``[0, 2**z)``）
      —— 文件被写坏了，任何客户端都无法正确索引；
    - :attr:`empty_tiles`：``tile_data`` 为空 —— 地图上会出现空白洞；
    - :attr:`corrupt_tiles`：图片无法解码 —— 地图上会出现黑块 / 裂图。
    """

    path: str
    valid: bool = False

    file_size: int = 0

    metadata: dict[str, str] = field(default_factory=dict)

    tile_count: int = 0

    min_zoom: int | None = None
    max_zoom: int | None = None

    zoom_stats: list[ZoomTileStats] = field(default_factory=list)

    invalid_tiles: int = 0
    empty_tiles: int = 0
    corrupt_tiles: int = 0

    missing_tiles: int | None = None

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 「普通提示」：只是说明检查做了什么 / 一切正常，**不影响结论**。
    #: Spec §20 对 PASS 的定义是"没有 ERROR，可以存在普通提示"，
    #: 因此这一档是必需的：没有它，一个完美文件也会因为"已抽样检查 6 张瓦片"
    #: 这种说明性文字被抬成 WARNING，PASS 将永远不可达。
    notices: list[str] = field(default_factory=list)

    # --- 以下为 Slice 8 的扩展字段（GUI / 报告要用，Spec 允许"类似"扩展）---
    #: 库里实际存在的表名
    tables: tuple[str, ...] = ()
    #: ``tiles`` 表的列名（用于判断四个必需字段是否齐全）
    tiles_columns: tuple[str, ...] = ()
    #: ``PRAGMA quick_check`` / ``integrity_check`` 的结果；没跑时为 ``None``
    sqlite_check: str | None = None
    #: 重复坐标的组数（正常库恒为 0）
    duplicate_tiles: int = 0
    #: 覆盖检查的期望总数与实际总数
    expected_tiles: int | None = None
    actual_tiles_in_coverage: int | None = None
    coverage_checked: bool = False
    #: 缺失瓦片明细（``z/x/y_xyz``，XYZ 语义），最多 ``MAX_MISSING_TILE_DETAILS`` 条
    missing_details: list[str] = field(default_factory=list)
    #: 因超出上限而未展开的缺失瓦片数
    missing_details_truncated: int = 0
    #: 图片抽样统计
    sample_size: int = 0
    sample_valid: int = 0
    sample_corrupt: int = 0
    sample_empty: int = 0
    sample_formats: dict[str, int] = field(default_factory=dict)
    #: 是否跑了深度图片扫描
    deep_scan: bool = False
    #: 深度扫描是否**跑完**了。用户中途关掉对话框时为 ``False``——
    #: 这时 :attr:`corrupt_tiles` 只是"已经扫过的那部分"的结论，
    #: 不能当成全量结果展示（这是"数字对不上"最难查的一类 bug）。
    deep_scan_complete: bool = False
    deep_scanned_tiles: int = 0
    #: metadata 里解析出来的关键项
    format: str | None = None
    bounds: tuple[float, float, float, float] | None = None
    bounds_text: str | None = None
    provider: str | None = None
    map_type: str | None = None
    labels: bool | None = None
    composition: str | None = None

    # ------------------------------------------------------------------ #
    # 结论
    # ------------------------------------------------------------------ #
    @property
    def status(self) -> str:
        """``"PASS"`` / ``"WARNING"`` / ``"FAIL"``（Spec §20 的统一规则）。"""
        if not self.valid:
            return DIAGNOSTICS_STATUS_FAIL
        if self.warnings:
            return DIAGNOSTICS_STATUS_WARNING
        return DIAGNOSTICS_STATUS_PASS

    @property
    def status_text(self) -> str:
        """给 GUI 用的别名（与 ``MBTilesValidationResult.status_text`` 同名）。"""
        return self.status

    @property
    def is_merged(self) -> bool:
        """是否是"底图 + 中文注记已合成"的产物。"""
        return self.composition == "merged"

    @property
    def zoom_text(self) -> str:
        """``"8~17"``；没有瓦片时为 ``"—"``。"""
        if self.min_zoom is None or self.max_zoom is None:
            return "—"
        return f"{self.min_zoom}~{self.max_zoom}"

    @property
    def missing_text(self) -> str:
        """缺失瓦片的一行说明；没做覆盖检查时为 ``"—"``。"""
        if self.missing_tiles is None:
            return "—"
        if self.missing_tiles == 0:
            return "0"
        return f"{self.missing_tiles:,}"

    @property
    def sampled_formats_text(self) -> str:
        """抽样里出现过的图片格式，形如 ``"PNG×50"``。"""
        if not self.sample_formats:
            return "—"
        return "、".join(
            f"{name}×{count}" for name, count in sorted(self.sample_formats.items())
        )

    def as_lines(self) -> list[str]:
        """多行展示文案（GUI 结果区 / 日志 / 任务记录共用同一套说法）。"""
        lines = [
            f"文件：{self.path}",
            f"大小：{format_size(self.file_size)}",
            f"状态：{self.status}",
            f"瓦片：{self.tile_count:,}",
            f"Zoom：{self.zoom_text}",
        ]
        if self.missing_tiles is not None:
            lines.append(f"缺失瓦片：{self.missing_text}")
        if self.sample_size:
            lines.append(
                f"图片抽样：{self.sample_size} 张，有效 {self.sample_valid} 张，"
                f"损坏 {self.sample_corrupt} 张"
            )
        lines.extend(f"错误：{message}" for message in self.errors)
        lines.extend(f"告警：{message}" for message in self.warnings)
        lines.extend(f"提示：{message}" for message in self.notices)
        return lines

    def to_validation(self) -> MBTilesValidationResult:
        """转成 :class:`~app.mbtiles.models.MBTilesValidationResult`。

        下载完成链路（Spec §22）用的是那个更老、更窄的结论对象，
        而 GUI / 任务记录 / 既有测试都在读它。诊断结果里多出来的信息
        （缺失瓦片、抽样损坏）以提示行的形式带过去，不改变字段语义。
        """
        warnings = list(self.warnings) + list(self.notices)
        if self.missing_tiles:
            warnings.append(
                f"覆盖完整性：缺失 {self.missing_tiles:,} 张瓦片"
                + (
                    f"（占期望 {self.expected_tiles:,} 张的 "
                    f"{(self.missing_tiles / self.expected_tiles * 100):.1f}%）"
                    if self.expected_tiles
                    else ""
                )
            )
        if self.sample_corrupt:
            warnings.append(f"图片抽样：{self.sample_corrupt} 张无法解码")
        return MBTilesValidationResult(
            valid=self.valid,
            tile_count=self.tile_count,
            min_zoom=self.min_zoom,
            max_zoom=self.max_zoom,
            file_size=self.file_size,
            errors=list(self.errors),
            warnings=warnings,
            provider=self.provider,
            map_type=self.map_type,
            labels=self.labels,
            composition=self.composition,
        )


# --------------------------------------------------------------------------- #
# 各检查项（Spec §3 ~ §14）
# --------------------------------------------------------------------------- #
def check_file(path: str | Path, result: MBTilesDiagnosticsResult) -> bool:
    """第 1 关：文件存在、是普通文件、大小可读（Spec §3）。

    Returns:
        ``True`` 表示可以继续往下打开数据库；``False`` 表示没必要继续了。
    """
    file_path = Path(path)
    if not file_path.exists():
        result.errors.append(f"文件不存在：{file_path}")
        return False
    if file_path.is_dir():
        result.errors.append(f"路径是目录而不是文件：{file_path}")
        return False
    try:
        result.file_size = file_path.stat().st_size
    except OSError as exc:
        result.errors.append(f"无法读取文件信息：{exc}")
        return False
    if result.file_size == 0:
        result.errors.append("文件大小为 0 字节（不是有效的 MBTiles）")
        return False
    return True


def check_structure(reader: MBTilesReader, result: MBTilesDiagnosticsResult) -> None:
    """第 2 关：表与字段（Spec §3）。

    缺表 / 缺字段一律 ERROR：缺了它们就不是 MBTiles，后面的统计也无从谈起。
    ``metadata`` 缺几个可选键只记 WARNING（那是 :func:`check_metadata` 的事）。
    """
    tables = reader.list_tables()
    result.tables = tuple(sorted(tables))
    for name in REQUIRED_TABLES:
        if name not in tables:
            result.errors.append(f"缺少 {name} 表")
    if "tiles" not in tables:
        return

    columns = reader.get_table_columns("tiles")
    result.tiles_columns = columns
    for column in TILES_REQUIRED_COLUMNS:
        if column not in columns:
            result.errors.append(f"tiles 表缺少必需字段：{column}")


def check_sqlite_integrity(
    reader: MBTilesReader,
    result: MBTilesDiagnosticsResult,
    *,
    deep_check: bool = False,
) -> None:
    """SQLite 自检（Spec §4）。

    默认只跑 ``PRAGMA quick_check``；``deep_check=True`` 时才升级成
    ``integrity_check`` —— 后者在几十万行的库上要读遍所有页并逐行比对索引，
    是分钟级操作，不该出现在"日常检查"里。
    """
    lines = reader.integrity_check() if deep_check else reader.quick_check()
    joined = "、".join(lines)
    result.sqlite_check = joined
    if any(line.strip().lower() != "ok" for line in lines):
        kind = "integrity_check" if deep_check else "quick_check"
        result.errors.append(f"SQLite {kind} 未通过：{joined}")


def check_metadata(reader: MBTilesReader, result: MBTilesDiagnosticsResult) -> None:
    """metadata 诊断（Spec §5）。

    只补"Slice 4 校验器没做的那部分"：

    - ``format`` 是不是本工具读得动的格式（``webp`` / ``pbf`` 规范允许、
      但本项目读不了 → WARNING 而不是 ERROR，文件对别的客户端可能仍然有效）；
    - ``bounds`` 是否落在 ±180 / ±85.05112878 之内（越界就是 ERROR，
      这种范围内根本算不出瓦片）；
    - 把 ``provider / map_type / labels / composition`` 解析出来给 GUI 展示。

    必备键是否齐全、``west<east``、``minzoom<=maxzoom``、合成信息是否自洽，
    都由 :func:`app.mbtiles.validator.validate_mbtiles` 负责 —— 本函数刻意
    不重复实现，避免同一件事出现两套措辞相反的结论。
    """
    metadata = reader.get_metadata()
    result.metadata = dict(metadata)
    if not metadata:
        return

    raw_format = (metadata.get("format") or "").strip().lower()
    result.format = raw_format or None
    if raw_format:
        if raw_format not in SUPPORTED_TILE_FORMATS:
            result.warnings.append(
                f"metadata.format 不是 MBTiles 规范支持的取值：{raw_format!r}"
            )
        elif raw_format not in DIAGNOSTICS_SUPPORTED_FORMATS:
            result.warnings.append(
                f"metadata.format={raw_format!r} 本工具无法直接读取"
                f"（只支持 {sorted(DIAGNOSTICS_SUPPORTED_FORMATS)}）"
                f"，本机地图应用可能显示空白"
            )

    raw_bounds = metadata.get("bounds")
    if raw_bounds is not None:
        result.bounds_text = str(raw_bounds)
        parsed = parse_bounds(raw_bounds)
        if parsed is not None:
            west, south, east, north = parsed
            result.bounds = parsed
            if not (
                MIN_LONGITUDE <= west <= MAX_LONGITUDE
                and MIN_LONGITUDE <= east <= MAX_LONGITUDE
            ):
                result.errors.append(
                    f"metadata.bounds 经度超出 ±180：{raw_bounds}"
                )
            if not (
                -WEB_MERCATOR_MAX_LAT <= south <= WEB_MERCATOR_MAX_LAT
                and -WEB_MERCATOR_MAX_LAT <= north <= WEB_MERCATOR_MAX_LAT
            ):
                result.errors.append(
                    f"metadata.bounds 纬度超出 Web 墨卡托极限"
                    f"±{WEB_MERCATOR_MAX_LAT}：{raw_bounds}"
                )

    result.provider = (metadata.get("provider") or "").strip() or None
    result.map_type = (metadata.get("map_type") or "").strip().lower() or None
    result.labels = parse_bool(metadata.get("labels", ""))
    result.composition = (metadata.get("composition") or "").strip().lower() or None


def collect_zoom_stats(
    reader: MBTilesReader,
    result: MBTilesDiagnosticsResult,
    progress_callback: Callable[[InspectionProgress], None] | None = None,
) -> None:
    """逐 zoom 的规模统计（Spec §6）。

    一条 ``GROUP BY`` 拿全部数字，**绝不读 tile_data**：一个 50 万张的库，
    这里返回的仍然是十几行元组。
    """
    rows = reader.get_zoom_stats()
    stats: list[ZoomTileStats] = []
    total = 0
    for zoom, count, min_x, max_x, min_y, max_y in rows:
        stats.append(
            ZoomTileStats(
                zoom=zoom,
                tile_count=count,
                min_x=min_x,
                max_x=max_x,
                min_y_tms=min_y,
                max_y_tms=max_y,
            )
        )
        total += count
    result.zoom_stats = stats
    result.tile_count = total
    if stats:
        result.min_zoom = stats[0].zoom
        result.max_zoom = stats[-1].zoom
    emit(
        progress_callback,
        "Zoom 统计",
        f"统计完成：{len(stats)} 个缩放级别，共 {total:,} 张瓦片",
        len(stats),
        len(stats),
    )


def check_metadata_zoom_consistency(result: MBTilesDiagnosticsResult) -> None:
    """metadata 声明的 zoom 与库里实际 zoom 是否一致（Spec §5）。

    不一致几乎总是"下载没跑完"或"metadata 被改过"。这里只记 WARNING：
    文件本身仍然可读，只是覆盖范围跟它自己声明的不一样。
    """
    if not result.metadata or result.min_zoom is None:
        return
    declared_min = parse_zoom(result.metadata.get("minzoom", ""))
    declared_max = parse_zoom(result.metadata.get("maxzoom", ""))
    if declared_max is not None and declared_max != result.max_zoom:
        result.warnings.append(
            f"metadata 声明 maxzoom={declared_max}，"
            f"但库里实际最大 zoom={result.max_zoom}"
            "（下载可能未完成，或 metadata 被改过）"
        )
    if declared_min is not None and declared_min != result.min_zoom:
        result.warnings.append(
            f"metadata 声明 minzoom={declared_min}，"
            f"但库里实际最小 zoom={result.min_zoom}"
            "（下载可能未完成，或 metadata 被改过）"
        )


def _xyz_hint(zoom: int, tms_y: int) -> str:
    """给一张越界瓦片补一句"XYZ 的 y 是多少"；换算不了时如实说明。

    ``tms_y_to_xyz_y`` 的契约是**验证式**的：行号不在 ``[0, 2**zoom)`` 就抛
    ``ValueError`` —— 这对正常调用是对的（XYZ 里根本不存在这个行号）。

    但"列出越界样本"这件事**恰恰就是在处理越界行**，于是这里必须容错：
    早先直接在这里调 ``tms_y_to_xyz_y``，结果是坐标检查在格式化自己的结论时
    崩掉，``_guard`` 把整个阶段变成一句"坐标检查失败"，用户**永远看不到是哪张
    瓦片坏了** —— 一个专门用来报错的检查，却报不出自己发现的错。

    Returns:
        形如 ``"，XYZ 的 y=123"``；行号本身越界时返回一句说明。
    """
    try:
        return f"，XYZ 的 y={tms_y_to_xyz_y(zoom, tms_y)}"
    except ValueError:
        return "，行号越界，无法换算成 XYZ"


def check_coordinates(reader: MBTilesReader, result: MBTilesDiagnosticsResult) -> None:
    """TMS / XYZ 坐标诊断（Spec §7）。

    规范要求 ``0 <= tile_column < 2**z`` 且 ``0 <= tile_row < 2**z``
    （``tile_row`` 是 TMS 行号，两者共享同一个上界）。越界就是 ERROR：
    这种行没有任何客户端能正确索引到对应位置。

    换算式一律复用 Slice 1 的 :func:`app.geo.tms_y_to_xyz_y`，
    不在这里重写第二套公式。
    """
    out_of_range = reader.count_tiles_with_illegal_zoom(
        TILE_MATH_MIN_ZOOM, TILE_MATH_MAX_ZOOM
    )
    if out_of_range:
        result.invalid_tiles += out_of_range
        samples = reader.find_illegal_zoom_tiles(
            TILE_MATH_MIN_ZOOM, TILE_MATH_MAX_ZOOM, _MAX_LISTED_DETAILS
        )
        listed = "、".join(
            f"zoom={z} column={x} row={row}（TMS）" for z, x, row in samples
        )
        result.errors.append(
            f"{out_of_range:,} 张瓦片的 zoom_level 超出 "
            f"[{TILE_MATH_MIN_ZOOM}, {TILE_MATH_MAX_ZOOM}]：{listed}"
        )

    for stats in result.zoom_stats:
        if not TILE_MATH_MIN_ZOOM <= stats.zoom <= TILE_MATH_MAX_ZOOM:
            continue  # 上面已经报过，别重复刷屏
        bad = reader.count_invalid_coordinates(stats.zoom)
        if not bad:
            continue
        result.invalid_tiles += bad
        samples = reader.find_invalid_coordinates(stats.zoom, _MAX_LISTED_DETAILS)
        listed = "、".join(
            f"zoom={stats.zoom} column={x} row={row}（TMS{_xyz_hint(stats.zoom, row)}）"
            for x, row in samples
        )
        limit = (1 << stats.zoom) - 1
        result.errors.append(
            f"zoom={stats.zoom} 下 {bad:,} 张瓦片坐标越界"
            f"（合法范围 0 ~ {limit}）：{listed}"
        )


def check_duplicates(reader: MBTilesReader, result: MBTilesDiagnosticsResult) -> None:
    """重复瓦片（Spec §8）。

    标准库有主键兜底，这条恒为 0；它存在的意义是守住"别人的库 / 被改过的库"
    —— 一个坐标出现两次时，不同客户端可能读到不同的字节。

    判定完全在 SQL 里做（``GROUP BY ... HAVING COUNT(*) > 1``），
    **不把全表坐标读进 Python 再判重**。
    """
    duplicates = reader.count_duplicate_tiles()
    result.duplicate_tiles = duplicates
    if not duplicates:
        return
    samples = reader.find_duplicate_tiles(_MAX_LISTED_DETAILS)
    listed = "、".join(
        f"{z}/{x}/{row}（TMS，出现 {n} 次）" for z, x, row, n in samples
    )
    result.errors.append(
        f"存在 {duplicates:,} 组重复坐标（同一 (zoom, column, row) 出现多次）：{listed}"
    )


def check_empty_tiles(reader: MBTilesReader, result: MBTilesDiagnosticsResult) -> None:
    """空瓦片（Spec §9）。

    ``tile_data IS NULL`` 或 ``length(tile_data) = 0`` 都算。结果 > 0 即 ERROR：
    空瓦片在地图上就是一个无法渲染的洞，和"这张瓦片没下"是两回事
    （后者由覆盖完整性检查负责）。
    """
    empty = reader.count_empty_tiles()
    result.empty_tiles = empty
    if empty:
        result.errors.append(
            f"{empty:,} 张瓦片的 tile_data 为空（NULL 或零长度），"
            "这些位置在地图上会显示为空白"
        )


def probe_image(data: bytes) -> ImageProbe:
    """探测一张瓦片的内容：magic bytes + Pillow 解码。

    两级判断，缺一不可：

    1. **magic bytes**（复用 :func:`app.providers.detect_image_format`）：
       认出这不是 PNG/JPEG 就直接否定，连解码都不必试 —— 错误页、
       被截断的响应体都会在这里被拦下；
    2. **Pillow ``verify()``**：magic 对不代表内容对。``verify()`` 会真的
       解一遍容器结构（PNG 会校验 CRC），截断的 PNG / 坏掉的 IDAT 在这里现形。

    环境里没有 Pillow 时退回第 1 级（返回 ``ok=True``）—— 诊断工具本身
    不该因为缺少可选依赖而把整个库判成坏的。

    ``error`` 一律是 :func:`_describe_probe_error` 翻译过的**稳定文案**，
    不把 Pillow 的原文（含对象地址）透出去 —— 那个地址每次都不同，
    会让"同一个问题"的报告两份对不上。
    """
    magic = detect_image_format(data)
    if magic is None:
        return ImageProbe(ok=False, error="magic bytes 既不是 PNG 也不是 JPEG")

    try:
        from PIL import Image  # noqa: PLC0415 - 延迟导入：下载链路不需要 Pillow
    except ImportError:  # pragma: no cover - requirements 里已声明
        return ImageProbe(ok=True, format=magic)

    try:
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            detected = image.format or magic
            image.verify()
    except Exception as exc:  # noqa: BLE001 - 诊断工具：任何解码异常都算"损坏"
        return ImageProbe(ok=False, format=magic, error=_describe_probe_error(exc))
    return ImageProbe(ok=True, format=detected, width=width, height=height)


#: Pillow 的异常文本 → 给用户看的一句话。
#:
#: 为什么必须翻译：Pillow 的原话里带着**对象地址**
#: （``cannot identify image file <_io.BytesIO object at 0x000002597B604310>``）——
#: 把它直接写进界面和报告，用户不但看不懂，还会以为程序漏了调试信息；
#: 而且那个地址每次都不同，同一份坏文件生成的报告两份对不上，
#: 想比对"是不是同一个问题"都做不到。
_PROBE_ERROR_TABLE: tuple[tuple[str, str], ...] = (
    # 措辞避免括号套括号：这条消息会被包进 "…（错误原因）" 里显示
    ("cannot identify image file", "无法识别图片内容：文件头对但正文不是有效图片"),
    ("image file is truncated", "图片被截断：下载中断或响应不完整"),
    ("broken data stream", "图片数据流损坏：IDAT / IEND 不完整"),
    ("crc", "图片校验和 CRC 不通过"),
    ("error reading", "图片数据读取失败：块结构损坏"),
)


def _describe_probe_error(exc: BaseException) -> str:
    """把解码异常翻译成一句稳定的、给用户看的话。

    刻意**不**保留对象地址等进程内状态：诊断结论要可复现、可对比。
    认不出的异常退回原文（截断到一行），至少不会把信息丢掉。
    """
    raw = str(exc).strip() or type(exc).__name__
    lowered = raw.lower()
    for needle, friendly in _PROBE_ERROR_TABLE:
        if needle in lowered:
            return friendly
    return " ".join(raw.split())[:120]


def check_samples(
    reader: MBTilesReader,
    result: MBTilesDiagnosticsResult,
    sample_size: int,
) -> None:
    """图片合法性抽样（Spec §10）。

    **分层**抽样（每个 zoom 都抽一点），而不是从头取 N 张 —— 后者会全部落在
    最小 zoom 上，"z17 的瓦片是坏的"这种问题永远抽不到。

    默认不碰全量：几十万张瓦片逐张解码是分钟级操作，那是
    :func:`deep_scan_tiles` 的活儿，必须由用户显式选择。
    """
    counts = reader.count_tiles_by_zoom()
    if not counts:
        return
    per_zoom = max(1, sample_size // len(counts))
    try:
        samples = reader.sample_tiles_stratified(counts, per_zoom, sample_size)
    except ValueError as exc:
        # reader 只产出**合法 XYZ**：遇到坐标越界的行会抛 ValueError。那种行已经
        # 由 check_coordinates 逐条报过 ERROR，这里只如实说明"抽样做不了"，
        # 而不是把一个原始换算异常糊到用户脸上（那看着像工具自己坏了）。
        result.warnings.append(
            f"无法进行图片抽样：库里存在坐标非法的瓦片（{exc}）——"
            "这些瓦片已由坐标检查逐条列出，请按那里的坐标定位"
        )
        return

    result.sample_size = len(samples)
    corrupt_coordinates: list[str] = []
    for zoom, x, y_xyz, data in samples:
        coordinate = f"{zoom}/{x}/{y_xyz}"
        if not data:
            result.sample_empty += 1
            continue
        probe = probe_image(data)
        if not probe.ok:
            result.sample_corrupt += 1
            if len(corrupt_coordinates) < _MAX_LISTED_DETAILS:
                corrupt_coordinates.append(f"{coordinate}（{probe.error}）")
            continue
        result.sample_valid += 1
        name = probe.format or "未知"
        result.sample_formats[name] = result.sample_formats.get(name, 0) + 1

    # 没做深度扫描时，抽样结果就是"损坏张数"的全部证据。
    if not result.deep_scan:
        result.corrupt_tiles = result.sample_corrupt

    if result.sample_empty:
        result.errors.append(
            f"抽样 {result.sample_size} 张中有 {result.sample_empty} 张 tile_data 为空"
        )
    if result.sample_corrupt:
        result.errors.append(
            f"抽样 {result.sample_size} 张中有 {result.sample_corrupt} 张图片无法解码："
            + "；".join(corrupt_coordinates)
        )
    elif result.sample_valid:
        result.notices.append(
            f"图片抽样：{result.sample_valid} 张全部可正常解码"
            f"（{result.sampled_formats_text}）"
        )
    _check_sample_format(result)


def _check_sample_format(result: MBTilesDiagnosticsResult) -> None:
    """抽样格式与 metadata.format 是否对得上（Spec §10）。

    ``format=png`` 却抽出一堆 JPEG，说明有瓦片绕过了正常写入路径。
    只记 WARNING：图片本身能解码，地图能看，只是与声明不符。
    """
    declared = (result.format or "").lower()
    if not declared or not result.sample_formats:
        return
    expected = {"PNG"} if declared == "png" else {"JPEG", "JPG"}
    found = set(result.sample_formats)
    if not found <= expected:
        result.warnings.append(
            f"metadata.format={declared!r}，但抽样里出现了 {sorted(found - expected)}"
            f"（全部抽样格式：{result.sampled_formats_text}）"
        )


def check_coverage(
    reader: MBTilesReader,
    result: MBTilesDiagnosticsResult,
    progress_callback: Callable[[InspectionProgress], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """覆盖完整性（Spec §12 ~ §14）。

    需要的输入是 metadata 里合法的 ``bounds`` + ``minzoom`` + ``maxzoom``：
    没有它们就无从谈起"应该覆盖哪些瓦片"。缺任一项时跳过并记一条提示，
    而不是报错 —— 一个只有 ``tiles`` 表的库仍然可以被读出来。

    期望集合一律用 Slice 1 的 :func:`app.geo.calculate_tile_range` 计算，
    **不另写一套 Bounds→瓦片的推导**：下载时算的目标集合与这里算的必须语义
    一致，否则"下完了却报缺失"这种自相矛盾的结论迟早会出现。
    """
    # 下面这几条"做不了覆盖检查"都是**说明**而不是文件缺陷：一个只有 tiles 表
    # 的库、或一个还没开始下载的空库，都不该因此被判 WARNING。
    if result.tile_count == 0:
        result.notices.append("库中没有瓦片，跳过覆盖完整性检查")
        return
    if result.bounds is None or result.min_zoom is None or result.max_zoom is None:
        result.notices.append(
            "无法进行覆盖完整性检查：metadata 缺少可用的 bounds / minzoom / maxzoom"
        )
        return
    declared_min = parse_zoom(result.metadata.get("minzoom", ""))
    declared_max = parse_zoom(result.metadata.get("maxzoom", ""))
    if declared_min is None or declared_max is None or declared_min > declared_max:
        result.notices.append(
            "无法进行覆盖完整性检查：metadata 的 minzoom / maxzoom 不可用"
        )
        return
    if not (
        TILE_MATH_MIN_ZOOM <= declared_min <= declared_max <= TILE_MATH_MAX_ZOOM
    ):
        # 越界的 zoom 会让 calculate_tile_range 直接抛异常。这里不做"钳到边界"
        # 这种自作聪明的事：钳出来的期望值和用户声明的就不是一回事了。
        result.notices.append(
            f"无法进行覆盖完整性检查：metadata 的 zoom 区间 "
            f"[{declared_min}, {declared_max}] 超出 "
            f"[{TILE_MATH_MIN_ZOOM}, {TILE_MATH_MAX_ZOOM}]"
        )
        return

    try:
        bounds = GeoBounds(*result.bounds)
    except ValueError as exc:
        # bounds 解析得出四个数、但范围本身不成立（west>=east 之类）。
        # 校验器已经为此报过 ERROR，这里只说明"覆盖检查没法做"。
        result.notices.append(f"无法进行覆盖完整性检查：bounds 不是有效范围（{exc}）")
        return

    # --- 区域（Slice 16）：多边形包按行政区几何算 expected，而不是按 bounding box ---
    region, region_filter, region_note = _resolve_coverage_region(result, bounds)
    if region_note:
        # **不退回矩形**：那会把区域外的瓦片全报成缺失（一个完整的多边形包会被
        # 判成缺 60%），属于 Spec §20 明令禁止的误报。宁可承认"不知道"。
        result.warnings.append(
            f"{region_note}；覆盖完整性无法按行政区几何判定，已标记为"
            "未知（不按外包矩形估算，以免把区域外瓦片误报成缺失）"
        )
        return

    by_zoom = {stats.zoom: stats for stats in result.zoom_stats}
    expected_total = 0
    actual_total = 0
    missing_total = 0
    details: list[str] = []
    zooms = list(range(declared_min, declared_max + 1))

    aborted = False
    for index, zoom in enumerate(zooms, start=1):
        if should_stop is not None and should_stop():
            aborted = True
            break
        if region_filter is None:
            tile_range = calculate_tile_range(bounds, zoom)
            expected = tile_range.tile_count
            actual = reader.count_tiles_in_range(
                zoom,
                tile_range.min_x,
                tile_range.max_x,
                xyz_y_to_tms_y(zoom, tile_range.max_y),  # XYZ 的 max_y ↔ TMS 的 min
                xyz_y_to_tms_y(zoom, tile_range.min_y),
            )
            missing = max(0, expected - actual)
            if missing and len(details) < MAX_MISSING_TILE_DETAILS:
                found = _missing_in_range(
                    reader, tile_range, MAX_MISSING_TILE_DETAILS - len(details)
                )
                details.extend(found)
        else:
            # 多边形：expected 只数"与区域相交"的瓦片（Spec §20）。
            # 按外包矩形数的话，区域外那一大片会被算成"应该有但没有"。
            expected, actual, found = _polygon_zoom_coverage(
                reader,
                bounds,
                zoom,
                region_filter,
                MAX_MISSING_TILE_DETAILS - len(details),
            )
            missing = max(0, expected - actual)
            details.extend(found)

        expected_total += expected
        actual_total += actual
        missing_total += missing

        stats = by_zoom.get(zoom)
        if stats is None:
            stats = ZoomTileStats(zoom=zoom, tile_count=0)
            by_zoom[zoom] = stats
            result.zoom_stats.append(stats)
        stats.expected_tiles = expected
        stats.missing_tiles = missing

        emit(
            progress_callback,
            "覆盖范围",
            f"覆盖检查 z{zoom}：期望 {expected:,} 张，实际 {actual:,} 张，缺失 {missing:,} 张",
            index,
            len(zooms),
        )

    result.zoom_stats.sort(key=lambda item: item.zoom)
    if aborted:
        # 半截的合计数字比没有数字更危险：它看起来是完整的。
        # 各 zoom 的明细已经算准了，保留；全局合计一律置空。
        result.notices.append(
            f"覆盖完整性检查被中止（已检查 {index - 1} / {len(zooms)} 个缩放级别）"
        )
        return

    result.expected_tiles = expected_total
    result.actual_tiles_in_coverage = actual_total
    result.missing_tiles = missing_total
    result.coverage_checked = True
    result.missing_details = details
    result.missing_details_truncated = max(0, missing_total - len(details))

    if region_filter is not None and region is not None:
        # 说清"这个期望值是按什么算的"：同一个包按矩形算与按行政区算会得到
        # 天差地别的 expected，报告里不写明依据，数字就没法解释。
        result.notices.append(
            f"覆盖完整性按行政区几何计算（{region.describe()}）："
            f"期望 {expected_total:,} 张只统计与区域相交的瓦片"
        )

    _report_coverage(result)


def _resolve_coverage_region(
    result: MBTilesDiagnosticsResult,
    bounds: GeoBounds,
) -> tuple[DownloadRegion | None, PolygonTileRegionFilter | None, str]:
    """读 metadata 里的区域信息并准备过滤器（Slice 16，Spec §20）。

    Returns:
        ``(region, region_filter, note)``：

        - 矩形包 / 老包（没有 ``region_type``）→ ``(None, None, "")``，
          调用方走原来的矩形路径，行为与 Slice ≤15 逐位一致；
        - 多边形且边界可用 → 过滤器；
        - 多边形但边界不可用 / 认不出 ``region_id`` → ``note`` 非空，
          调用方应把覆盖度标成**未知**。

    "边界不可用就标未知"是 Spec §20 的硬性要求，理由很实际：按 bounding box
    去算一个多边形包的 expected，会得出"缺了区域外那 60% 瓦片"的结论，
    而那个包很可能是**完整**的。误报比不知道更糟 —— 用户会去重下几万张。
    """
    region_type = str(result.metadata.get("region_type") or "").strip().lower()
    if RegionType.parse(region_type) is RegionType.RECTANGLE:
        return None, None, ""

    region, note = region_from_metadata(
        region_type,
        result.metadata.get("region_id"),
        result.metadata.get("boundary_version"),
        bounds,
    )
    if region is None:
        return None, None, note or "行政区边界资源不可用"

    try:
        region_filter = build_region_filter(region)
    except GeoJSONError as exc:
        return None, None, f"行政区边界资源不可用：{exc}"
    if region_filter is None:  # pragma: no cover - is_polygon 已保证非空
        return None, None, "行政区边界资源不可用：无法构造几何过滤器"
    return region, region_filter, ""


def _polygon_zoom_coverage(
    reader: MBTilesReader,
    bounds: GeoBounds,
    zoom: int,
    region_filter: PolygonTileRegionFilter,
    detail_limit: int,
) -> tuple[int, int, list[str]]:
    """按行政区几何统计某个 zoom 的 ``(expected, actual, missing_details)``。

    算法（两次流式遍历，都不落地"期望瓦片全集"）：

    1. 用 ``reader.iter_tile_coordinates(zoom)`` 读库里该级别的坐标
       （**只读三列，不读 BLOB**），把**落在区域内**的存进一个小集合；
    2. 遍历外包矩形的格子，对每张与区域相交的瓦片：``expected += 1``；
       再查它在不在第 1 步的集合里 —— 在则 ``actual += 1``，不在则记一条缺失明细。

    ``expected == actual + missing`` **恒成立**（两者都只在"区域内 ∩ 候选范围内"
    这个集合上统计）。这一点很重要：如果库里混着区域外的瓦片（例如这个文件的
    前身是个矩形任务），按"库内总数 - 区域外"去凑就会得出自相矛盾的三个数字。

    内存占用与**库内该级别的瓦片数**同阶，与"区域面积"无关 —— 全国 z14 的
    期望瓦片是千万级，先 materialize 再比对会直接把内存吃光（Spec §9）。
    """
    present: set[tuple[int, int]] = set()
    for z, x, y in reader.iter_tile_coordinates(zoom):
        if region_filter.intersects_tile_xyz(z, x, y):
            present.add((x, y))

    tile_range = calculate_tile_range(bounds, zoom)
    expected = 0
    actual = 0
    details: list[str] = []
    for y in range(tile_range.min_y, tile_range.max_y + 1):
        for x in range(tile_range.min_x, tile_range.max_x + 1):
            if not region_filter.intersects_tile_xyz(zoom, x, y):
                continue
            expected += 1
            if (x, y) in present:
                actual += 1
                continue
            if len(details) < detail_limit:
                details.append(f"{zoom}/{x}/{y}")
    return expected, actual, details


def _missing_in_range(
    reader: MBTilesReader, tile_range: TileRange, limit: int
) -> list[str]:
    """在矩形范围内找出前 ``limit`` 个缺失坐标（**XYZ**，形如 ``"13/6742/3102"``）。

    算法：**逐列**判断这一列缺不缺（一次计数），确认缺了才逐行展开。
    直接双层循环枚举整个范围是行不通的 —— 世界范围 z14 就是 1.6 亿格，
    而真正缺失的可能只有一张。

    列内按 XYZ 的 ``y`` 升序产出，与用户在别处看到的顺序一致。
    """
    zoom = tile_range.zoom
    details: list[str] = []
    if limit <= 0:
        return details
    min_tms = xyz_y_to_tms_y(zoom, tile_range.max_y)
    max_tms = xyz_y_to_tms_y(zoom, tile_range.min_y)

    for x in range(tile_range.min_x, tile_range.max_x + 1):
        present = set(reader.iter_tile_rows_in_column(zoom, x, min_tms, max_tms))
        if len(present) >= tile_range.y_count:
            continue  # 这一列是满的
        for y_xyz in range(tile_range.min_y, tile_range.max_y + 1):
            if xyz_y_to_tms_y(zoom, y_xyz) in present:
                continue
            details.append(f"{zoom}/{x}/{y_xyz}")
            if len(details) >= limit:
                return details
    return details


def _report_coverage(result: MBTilesDiagnosticsResult) -> None:
    """把覆盖结论翻译成 WARNING（Spec §21）。

    缺失**默认只记 WARNING，绝不判 FAIL**：结构与图片都有效时，文件仍然是
    局部可用的，用户拿它看已经下好的那一块完全没问题。只有缺失比例
    超过 :data:`DIAGNOSTICS_MISSING_WARNING_RATIO` 时，措辞才会更醒目
    —— 但也仍然只是 WARNING。
    """
    total = result.missing_tiles or 0
    if total == 0:
        # "没有缺失"是好消息，不是告警：放进 notices，别把一个完整的地图
        # 判成 WARNING（Spec §20：PASS 允许存在普通提示）。
        result.notices.append(
            f"覆盖完整性：期望 {result.expected_tiles:,} 张，"
            f"实际 {result.actual_tiles_in_coverage:,} 张，无缺失"
        )
        return

    ratio = total / result.expected_tiles if result.expected_tiles else 0.0
    detail = ""
    if result.missing_details:
        detail = "，例如 " + "、".join(result.missing_details[:3])
        if result.missing_details_truncated:
            detail += f"（另有 {result.missing_details_truncated:,} 张未展开）"
    if ratio > DIAGNOSTICS_MISSING_WARNING_RATIO:
        result.warnings.append(
            f"覆盖完整性：缺失 {total:,} 张瓦片，占期望 {result.expected_tiles:,} 张的 "
            f"{ratio * 100:.1f}%（超过 {DIAGNOSTICS_MISSING_WARNING_RATIO * 100:.0f}%）"
            f"，地图上会有成片空白{detail}"
        )
    else:
        result.warnings.append(
            f"覆盖完整性：缺失 {total:,} 张瓦片"
            f"（占期望 {result.expected_tiles:,} 张的 {ratio * 100:.2f}%）{detail}"
        )


def deep_scan_tiles(
    reader: MBTilesReader,
    result: MBTilesDiagnosticsResult,
    *,
    batch_size: int,
    progress_callback: Callable[[InspectionProgress], None] | None = None,
    total: int = 0,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """深度图片扫描（Spec §11）：逐张校验全部 ``tile_data``。

    **必须**流式读取（``reader.iter_tiles`` 内部用 ``fetchmany``）：
    50 万张瓦片每张几十 KB，一次性 ``fetchall()`` 就是几个 GB 的常驻内存，
    这是 Spec §11/§24 明令禁止的写法。

    跑完之后 :attr:`MBTilesDiagnosticsResult.corrupt_tiles` 是**全量**结果，
    不再是抽样推断。

    Args:
        batch_size: 每次从 SQLite 取多少行。
        progress_callback: 进度回调，收 :class:`InspectionProgress`。
        total: 总张数（进度分母）。
        should_stop: 可选中止条件；每读完一批检查一次。用户关掉对话框时，
            一个几十万张的深度扫描不该把进程一起拖住（Spec §11 的扫描是
            分钟级操作，必须能被放弃）。
    """
    corrupt = 0
    empty = 0
    not_image = 0
    scanned = 0
    listed: list[str] = []
    truncated_reason = ""

    tiles = reader.iter_tiles(batch_size)
    try:
        for zoom, x, y_xyz, data in tiles:
            scanned += 1
            if not data:
                empty += 1
                corrupt += 1
            else:
                probe = probe_image(data)
                if not probe.ok:
                    corrupt += 1
                    if detect_image_format(data) is None:
                        not_image += 1
                    if len(listed) < _MAX_LISTED_DETAILS:
                        listed.append(f"{zoom}/{x}/{y_xyz}（{probe.error}）")
            if scanned % batch_size == 0:
                if should_stop is not None and should_stop():
                    # 已经扫过的部分仍然有价值（损坏张数只多不少），但**必须**
                    # 标明"没扫完"——否则界面上那个数字会被当成全量结论。
                    result.deep_scan = True
                    result.deep_scan_complete = False
                    result.deep_scanned_tiles = scanned
                    result.corrupt_tiles = corrupt
                    result.empty_tiles = max(result.empty_tiles, empty)
                    result.warnings.append(
                        f"深度检查被中止（已扫描 {scanned:,} / {total:,} 张），"
                        "损坏张数只是已扫描部分的结论"
                    )
                    return
                emit(
                    progress_callback,
                    "深度扫描",
                    f"深度检查图片 {scanned:,} / {total:,}",
                    scanned,
                    total,
                )
    except ValueError as exc:
        # reader 只产出**合法 XYZ**：坐标越界的行换算不出来，流式遍历会中断。
        # 那种行已由 check_coordinates 逐条报过 ERROR，这里同样是"没扫完"，
        # 不能把半截的损坏张数当成全量结论。
        truncated_reason = f"库里存在坐标非法的瓦片（{exc}）"
    finally:
        tiles.close()

    if truncated_reason:
        result.deep_scan = True
        result.deep_scan_complete = False
        result.deep_scanned_tiles = scanned
        result.corrupt_tiles = corrupt
        result.empty_tiles = max(result.empty_tiles, empty)
        result.warnings.append(
            f"深度检查提前结束（已扫描 {scanned:,} / {total:,} 张）：{truncated_reason}"
            "——损坏张数只是已扫描部分的结论"
        )
        return

    result.deep_scan = True
    result.deep_scan_complete = True
    result.deep_scanned_tiles = scanned
    result.corrupt_tiles = corrupt
    result.empty_tiles = max(result.empty_tiles, empty)
    if not_image:
        result.errors.append(
            f"深度检查发现 {not_image:,} 张瓦片的 magic bytes 既不是 PNG 也不是 JPEG"
        )
    if empty:
        result.errors.append(f"深度检查发现 {empty:,} 张瓦片的 tile_data 为空")
    if corrupt and corrupt - empty - not_image > 0:
        result.errors.append(
            f"深度检查发现 {corrupt - empty - not_image:,} 张瓦片无法解码："
            + "；".join(listed)
        )
    emit(
        progress_callback,
        "深度扫描",
        f"深度检查完成：共 {scanned:,} 张，损坏 {corrupt:,} 张",
        scanned,
        scanned,
    )


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
#: Slice 4 校验器里"只是说明它做了什么"的两条信息，它们被写在 ``warnings`` 里
#: 只是为了在日志里留痕，**不表示文件有任何问题**：
#:
#: 1. ``已抽样检查 N 张瓦片（共 M 张，大小 X）``
#: 2. ``抽样 N 张的图片格式为 [...]，请确认与 metadata.format 一致``
#:    —— 注意它在格式**完全一致**时也会出现（只判断了"有没有认出格式"），
#:    真正的"格式与声明不符"检查由 :func:`_check_sample_format` 负责。
#:
#: 用正则精确匹配而不是"让校验器改口径"：那两句文案已经被 Slice 4 的测试与
#: 用户日志引用，改它属于跨 Slice 的行为变更，而本 Slice 的 Spec 明确
#: 不修改既有下载/校验核心逻辑。
_INFORMATIONAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^已抽样检查 \d+ 张瓦片（共 [\d,]+ 张，大小 .+）$"),
    re.compile(r"^抽样 \d+ 张的图片格式为 \[.*\]，请确认与 metadata\.format 一致$"),
)


def is_informational(message: str) -> bool:
    """这条消息是不是"普通提示"（不构成告警，不影响 PASS/WARNING 判定）。"""
    return any(pattern.match(message) for pattern in _INFORMATIONAL_PATTERNS)


def merge_validation_into(
    result: MBTilesDiagnosticsResult, validation: MBTilesValidationResult
) -> None:
    """把 Slice 4/6 校验器的结论并进诊断结果。

    为什么不重写一遍那套规则：``metadata`` 必备键、``minzoom<=maxzoom``、
    合成信息自洽性这些判定已经被下载完成链路用了整整三个 Slice，
    在诊断工具里再实现一遍，唯一的结果就是"同一份文件两处给不同结论"。

    重复的问题只记一条（按原文去重）：两套检查天然会有交集
    （例如"缺少 tiles 表"），并排显示两遍纯属噪音。
    """
    for message in validation.errors:
        if message not in result.errors:
            result.errors.append(message)
    for message in validation.warnings:
        target = result.notices if is_informational(message) else result.warnings
        if message not in target:
            target.append(message)
    # 校验器能拿到而我们拿不到的兜底数据（例如 tiles 表不存在时它仍然报了张数）
    if not result.tile_count and validation.tile_count:
        result.tile_count = validation.tile_count
    if result.min_zoom is None and validation.min_zoom is not None:
        result.min_zoom = validation.min_zoom
    if result.max_zoom is None and validation.max_zoom is not None:
        result.max_zoom = validation.max_zoom
    if not result.file_size and validation.file_size:
        result.file_size = validation.file_size


def finalize(result: MBTilesDiagnosticsResult) -> MBTilesDiagnosticsResult:
    """定稿：由 ``errors`` 决定 ``valid``（Spec §20）。

    ``valid = 没有 ERROR``。WARNING 不影响可用性 —— 这正是
    "metadata 少个 description" 不该判 FAIL 的落点。
    """
    result.valid = not result.errors
    return result


def emit(
    progress_callback: Callable[[InspectionProgress], None] | None,
    stage: str,
    message: str,
    current: int = 0,
    total: int = 0,
) -> None:
    """向调用方推一条进度（回调为空时什么也不做）。

    回调里的异常**不允许**影响检查本身：进度上报是"锦上添花"，
    一个 GUI 回调炸掉不该让整份诊断结果丢失。
    """
    if progress_callback is None:
        return
    try:
        progress_callback(
            InspectionProgress(
                stage=stage, message=message, current=current, total=total
            )
        )
    except Exception as exc:  # noqa: BLE001 - 见 docstring
        _logger.warning("诊断进度回调失败（忽略）：%s", exc)


# --------------------------------------------------------------------------- #
# 文本报告（Spec §26）
# --------------------------------------------------------------------------- #
def render_report(result: MBTilesDiagnosticsResult) -> str:
    """把诊断结论渲染成一份纯文本报告（UTF-8，便于贴进 issue / 存档）。

    刻意**不做** PDF / HTML / Excel：本 Slice 只要求"简单文本报告"。
    """
    lines: list[str] = [
        REPORT_FILE_LINE.format(path=result.path),
        REPORT_SIZE_LINE.format(
            size=format_size(result.file_size), bytes=result.file_size
        ),
        REPORT_SQLITE_LINE.format(
            result=result.sqlite_check if result.sqlite_check is not None else "—"
        ),
        "",
        REPORT_METADATA_HEADER,
    ]
    if result.metadata:
        for key in sorted(result.metadata):
            lines.append(f"  {key}: {result.metadata[key]}")
    else:
        lines.append(f"  {REPORT_NONE}")

    lines.append("")
    lines.append(REPORT_ZOOM_HEADER)
    if result.zoom_stats:
        for stats in result.zoom_stats:
            lines.append(
                "  {zoom} | {tiles:,} | {expected} | {missing}".format(
                    zoom=stats.zoom,
                    tiles=stats.tile_count,
                    expected=(
                        f"{stats.expected_tiles:,}"
                        if stats.expected_tiles is not None
                        else "—"
                    ),
                    missing=(
                        f"{stats.missing_tiles:,}"
                        if stats.missing_tiles is not None
                        else "—"
                    ),
                )
            )
    else:
        lines.append(f"  {REPORT_NONE}")

    lines.extend(
        [
            "",
            REPORT_TILE_COUNT_LINE.format(count=f"{result.tile_count:,}"),
            REPORT_EXPECTED_LINE.format(
                count=(
                    f"{result.expected_tiles:,}"
                    if result.expected_tiles is not None
                    else "—"
                )
            ),
            REPORT_MISSING_LINE.format(
                count=(
                    f"{result.missing_tiles:,}"
                    if result.missing_tiles is not None
                    else "—"
                )
            ),
            REPORT_SAMPLE_LINE.format(
                samples=result.sample_size,
                valid=result.sample_valid,
                corrupt=result.sample_corrupt,
                empty=result.sample_empty,
            ),
        ]
    )
    if result.deep_scan:
        lines.append(
            REPORT_DEEP_SCAN_LINE.format(
                scanned=result.deep_scanned_tiles, corrupt=result.corrupt_tiles
            )
        )
    if result.sample_formats:
        lines.append(
            REPORT_SAMPLE_FORMATS_LINE.format(formats=result.sampled_formats_text)
        )

    if result.missing_details:
        lines.append("")
        lines.append(
            REPORT_MISSING_DETAIL_HEADER.format(count=len(result.missing_details))
        )
        lines.extend(f"  {detail}" for detail in result.missing_details)
        if result.missing_details_truncated:
            lines.append(
                REPORT_MISSING_DETAIL_TRUNCATED.format(
                    count=result.missing_details_truncated
                )
            )

    lines.append("")
    lines.append(REPORT_ERRORS_HEADER)
    lines.extend(
        (f"  {message}" for message in result.errors) if result.errors else [f"  {REPORT_NONE}"]
    )
    lines.append(REPORT_WARNINGS_HEADER)
    lines.extend(
        (f"  {message}" for message in result.warnings)
        if result.warnings
        else [f"  {REPORT_NONE}"]
    )
    lines.append(REPORT_NOTICES_HEADER)
    lines.extend(
        (f"  {message}" for message in result.notices)
        if result.notices
        else [f"  {REPORT_NONE}"]
    )

    lines.append("")
    lines.append(REPORT_RESULT_LINE.format(result=result.status))
    return "\n".join(lines) + "\n"
