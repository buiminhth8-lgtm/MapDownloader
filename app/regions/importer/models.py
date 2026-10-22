"""行政区数据导入管线的模型层（Slice 26，Spec §2 / §3 / §14）。

这一层定义三组东西：

1. **导入配置** :class:`RegionImportConfig` —— 源文件在哪、字段叫什么、
   发布到哪、数据集叫什么版本。外部数据的字段名**不可假设**（Spec §2），
   所以四个字段名都是配置项，没有默认值以外的"一定是这个键"。
2. **源数据条目** :class:`RegionSourceFeature` —— SourceReader 的产出：
   一条 Feature 的原始属性 + 解析出来的几何 + 原始几何字典。
   ``raw_geometry`` 与 ``geometry`` **同时保留**：前者是 JSON 里的原始坐标
   （写回文件时逐字节保留，Spec §6"原始 Geometry 应尽量保留"），
   后者是 shapely 对象（校验与算 bounds 用）。
3. **结果模型** —— :class:`RegionImportIssue`（一条问题）、
   :class:`RegionImportResult`（一次验证的结论，含 PASS/WARNING/FAIL）、
   :class:`RegionDatasetManifest`（发布数据集的版本与来源身份证）、
   :class:`ImportReport`（一次真实导入的回执）。

与 Slice 18 目录模型的关系
--------------------------

``app.regions.models`` 那套（:class:`~app.regions.models.CatalogIssue` 等）
回答"**已经发布**的目录有什么问题"；本模块回答"**正要导入**的源数据
能不能变成一个目录"。两者刻意不共用问题类型：导入期的问题带"第几条
Feature、哪个字段"，目录期的问题带"哪条记录、哪个文件"，硬合在一起只会
让两边都多一层无意义的适配。

问题为什么用枚举 + 列表而不是异常
--------------------------------

Spec §4 要求"不要静默忽略大量错误数据"：一条坏 Feature 不该让整个导入
爆炸（用户常常是拿一份 90% 能用的数据来试），但每一条被拒的 Feature 都
**必须**在结果里数得着。所以拒绝走 issue（ERROR 级），整份数据能不能用
由 :attr:`RegionImportResult.verdict` 统一回答 —— 有 ERROR 就是 FAIL，
FAIL 时禁止发布（Spec §8 / §19）。

Zoom 不在这里
-------------

本模块**没有任何 Zoom 字段、区间或常量**（Spec §22 / §29）：区间只存在于
``app.planning.DEFAULT_ZOOM_POLICIES``，导入器只写等级，运行时才查策略。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from app.planning.region_level import RegionLevel

__all__ = [
    "DEFAULT_LEVEL_ALIASES",
    "SAFE_ID_PATTERN",
    "ImportReport",
    "ImportVerdict",
    "IssueLevel",
    "RegionDatasetManifest",
    "RegionImportConfig",
    "RegionImportError",
    "RegionImportIssue",
    "RegionImportIssueCode",
    "RegionImportResult",
    "RegionSourceFeature",
    "map_level",
    "sanitize_region_id",
]


class RegionImportError(RuntimeError):
    """导入管线自身的硬错误（源文件读不出、输出目录不可写等）。

    与"数据问题"（:class:`RegionImportIssue`）分开：前者是**这次导入跑不完**，
    后者是**这批数据不合格**。调用方对两者的反应不同 —— 前者要修环境或路径，
    后者要修数据或字段映射。
    """


class IssueLevel(Enum):
    """一条导入问题的严重程度。

    ``ERROR``：这条数据不成立，导入必须失败（Spec §8"任一严重层级错误：
    Import Validation FAIL"）。
    ``WARNING``：不成立但可继续 —— 典型的是"几何被 make_valid 修过"
    （Spec §5：允许修复，但必须记录）与"id 被清洗过"（Spec §7）。
    """

    ERROR = "error"
    WARNING = "warning"


class ImportVerdict(Enum):
    """一次验证的总体结论（Spec §19 的 PASS / WARNING / FAIL）。"""

    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"


class RegionImportIssueCode(Enum):
    """导入问题的稳定机器码（日志 grep、测试断言、报告分栏都用它）。

    ``value`` 是持久化契约：验收报告与排查文档会引用它，改名等于让历史
    报告对不上号。
    """

    # --- 源文件与配置 ---
    SOURCE_UNREADABLE = "source_unreadable"
    SOOURCE_NOT_FEATURE_COLLECTION = "source_not_feature_collection"
    FIELD_NOT_IN_SOURCE = "field_not_in_source"

    # --- 单条 Feature（Spec §4）---
    MISSING_FIELD = "missing_field"
    UNSUPPORTED_GEOMETRY = "unsupported_geometry"
    EMPTY_GEOMETRY = "empty_geometry"
    UNPARSEABLE_GEOMETRY = "unparseable_geometry"
    UNKNOWN_LEVEL = "unknown_level"

    # --- 几何标准化（Spec §5）---
    GEOMETRY_OUT_OF_RANGE = "geometry_out_of_range"
    GEOMETRY_INVALID = "geometry_invalid"
    GEOMETRY_REPAIRED = "geometry_repaired"
    GEOMETRY_SIMPLIFIED = "geometry_simplified"

    # --- id（Spec §7）---
    DUPLICATE_ID = "duplicate_id"
    ID_SANITIZED = "id_sanitized"
    ID_EMPTY = "id_empty"

    # --- 层级（Spec §8 / §9）---
    COUNTRY_HAS_PARENT = "country_has_parent"
    PARENT_MISSING = "parent_missing"
    SELF_PARENT = "self_parent"
    PARENT_LEVEL_INVALID = "parent_level_invalid"
    CITY_UNDER_COUNTRY = "city_under_country"
    PARENT_CYCLE = "parent_cycle"
    NO_COUNTRY_ENTRY = "no_country_entry"
    MULTIPLE_COUNTRIES = "multiple_countries"
    NO_CITY_LAYER = "no_city_layer"

    # --- 发布（Spec §13）---
    CATALOG_INVALID = "catalog_invalid"


@dataclass(frozen=True)
class RegionImportIssue:
    """导入期的一条问题。

    Attributes:
        code: 机器码（见 :class:`RegionImportIssueCode`）。
        message: 给人看的一句话（**不含机器码**，与 Slice 18 同一规矩）。
        feature: 出问题的源 Feature 标识（"第 12 条"或它的 id）；与具体
            Feature 无关时是空串。
        level: 严重程度。
    """

    code: RegionImportIssueCode
    message: str
    feature: str = ""
    level: IssueLevel = IssueLevel.ERROR

    def __post_init__(self) -> None:
        if not isinstance(self.code, RegionImportIssueCode):
            raise ValueError(
                f"code 必须是 RegionImportIssueCode，实际为 {self.code!r}"
            )
        if not isinstance(self.level, IssueLevel):
            raise ValueError(f"level 必须是 IssueLevel，实际为 {self.level!r}")
        object.__setattr__(self, "message", str(self.message).strip())
        object.__setattr__(self, "feature", str(self.feature or "").strip())

    @property
    def is_error(self) -> bool:
        """是否是"这批数据不能要"级别的问题。"""
        return self.level is IssueLevel.ERROR

    def subject(self) -> str:
        """这条问题说的是谁（没写 feature 时是"源数据"）。"""
        return self.feature or "源数据"

    def describe(self) -> str:
        """面向人的一行（不带机器码，给界面与报告用）。"""
        return f"{self.subject()}：{self.message}"


@dataclass(frozen=True)
class RegionImportResult:
    """一次"验证"（dry-run）或"导入"的结论（Spec §19）。

    Attributes:
        issues: 全部问题，顺序即发现顺序。
        counts: 通过校验的条目按等级计数的结果（拒绝的不计）。
        geometry_invalid: 几何非法（含被修复的）条数。
        missing_parent: 父级缺失 / 指向不存在条数。
        duplicate_id: id 重复条数。
        checked: 源数据里的 Feature 总数（含被拒的）。
    """

    issues: tuple[RegionImportIssue, ...] = ()
    counts: Mapping[RegionLevel, int] = field(default_factory=dict)
    geometry_invalid: int = 0
    missing_parent: int = 0
    duplicate_id: int = 0
    checked: int = 0

    @property
    def errors(self) -> tuple[RegionImportIssue, ...]:
        """error 级别的问题。"""
        return tuple(item for item in self.issues if item.is_error)

    @property
    def warnings(self) -> tuple[RegionImportIssue, ...]:
        """warning 级别的问题。"""
        return tuple(item for item in self.issues if not item.is_error)

    @property
    def verdict(self) -> ImportVerdict:
        """PASS / WARNING / FAIL（Spec §19）。

        有 ERROR 就是 FAIL —— 没有"带错也发"这个选项（Spec §8：
        不要生成半正确 Catalog）。
        """
        if self.errors:
            return ImportVerdict.FAIL
        if self.warnings:
            return ImportVerdict.WARNING
        return ImportVerdict.PASS

    @property
    def ok(self) -> bool:
        """能不能发布（只有 PASS / WARNING 可以）。"""
        return self.verdict is not ImportVerdict.FAIL

    def count_of(self, level: RegionLevel) -> int:
        """某一等级的条目数。"""
        return int(self.counts.get(level, 0))

    def count_code(self, code: RegionImportIssueCode) -> int:
        """按机器码数一数（测试与统计行用）。"""
        return sum(1 for item in self.issues if item.code is code)

    def summary_lines(self) -> list[str]:
        """Spec §19 要的那几行统计（界面结果区与 CLI 共用）。"""
        verdict_text = {
            ImportVerdict.PASS: "PASS",
            ImportVerdict.WARNING: "WARNING",
            ImportVerdict.FAIL: "FAIL",
        }[self.verdict]
        lines = [
            f"总区域：{sum(self.counts.values())}",
            f"COUNTRY：{self.count_of(RegionLevel.COUNTRY)}",
            f"PROVINCE：{self.count_of(RegionLevel.PROVINCE)}",
            f"CITY：{self.count_of(RegionLevel.CITY)}",
            f"COUNTY：{self.count_of(RegionLevel.COUNTY)}",
            f"Geometry Invalid：{self.geometry_invalid}",
            f"Missing Parent：{self.missing_parent}",
            f"Duplicate ID：{self.duplicate_id}",
            f"结果：{verdict_text}",
        ]
        return lines


# --------------------------------------------------------------------------- #
# 等级映射（Spec §3）
# --------------------------------------------------------------------------- #
#: 源数据里的等级字面量 → :class:`~app.planning.RegionLevel`。
#:
#: 为什么是一张表而不是 ``if/elif``：真实数据的等级列千奇百怪 —— 英文
#: （``country``）、数字（``1``）、中文（``全国``）、还有把自治区与直辖市
#: 写进等级列的。Spec §3 点名"country → COUNTRY、1 → COUNTRY 都要支持"，
#: Spec §9 要求直辖市能映射到省级。加一种写法就是加一行，不需要动逻辑。
#:
#: 表里**没有**的值 → :func:`map_level` 返回 ``None``，调用方记一条
#: UNKNOWN_LEVEL 并拒绝该条 Feature（不猜：猜错等级比拒绝更糟 ——
#: 一个省会被挂到市下面，用户在界面上看到"浙江省属于杭州市"）。
DEFAULT_LEVEL_ALIASES: dict[str, RegionLevel] = {
    # 全国级
    "country": RegionLevel.COUNTRY,
    "1": RegionLevel.COUNTRY,
    "全国": RegionLevel.COUNTRY,
    "国家级": RegionLevel.COUNTRY,
    "中国": RegionLevel.COUNTRY,
    # 省级（省 / 自治区 / 直辖市 / 州 / 盟 —— Spec §9 的直辖市兼容）
    "province": RegionLevel.PROVINCE,
    "2": RegionLevel.PROVINCE,
    "省": RegionLevel.PROVINCE,
    "省级": RegionLevel.PROVINCE,
    "自治区": RegionLevel.PROVINCE,
    "直辖市": RegionLevel.PROVINCE,
    "特别行政区": RegionLevel.PROVINCE,
    "州": RegionLevel.PROVINCE,
    "盟": RegionLevel.PROVINCE,
    "地区": RegionLevel.PROVINCE,
    # 市级（市 / 自治州 / 盟）
    "city": RegionLevel.CITY,
    "3": RegionLevel.CITY,
    "市": RegionLevel.CITY,
    "市级": RegionLevel.CITY,
    # 县级（县 / 区 / 旗 / 县级市 —— Slice 27）
    "county": RegionLevel.COUNTY,
    "4": RegionLevel.COUNTY,
    "县": RegionLevel.COUNTY,
    "区": RegionLevel.COUNTY,
    "区县": RegionLevel.COUNTY,
    "县级": RegionLevel.COUNTY,
    "县级市": RegionLevel.COUNTY,
    "自治县": RegionLevel.COUNTY,
    "旗": RegionLevel.COUNTY,
}


def map_level(raw: object, extra: Mapping[str, str] | None = None) -> RegionLevel | None:
    """把源数据里的等级字面量映射成 :class:`~app.planning.RegionLevel`。

    Args:
        raw: 源数据里的原始值（字符串 / 数字都接受）。
        extra: 调用方追加的映射（键同样大小写不敏感；值必须是三个行政等级
            之一的 ``value``）。配置里的扩展优先生效 —— 用户比默认表更清楚
            自己那份数据怎么写等级。

    Returns:
        映射结果；认不出来返回 ``None``（调用方负责记 issue，**不猜**）。
    """
    if isinstance(raw, RegionLevel):
        return raw
    text = str(raw if raw is not None else "").strip().lower()
    if not text:
        return None
    if extra:
        for key, value in extra.items():
            if str(key).strip().lower() != text:
                continue
            wanted = str(value).strip().lower()
            for item in RegionLevel:
                if item.value == wanted:
                    return item
    return DEFAULT_LEVEL_ALIASES.get(text)


# --------------------------------------------------------------------------- #
# Region id（Spec §7）
# --------------------------------------------------------------------------- #
#: 合法 region id 的字符表：字母、数字、下划线、点、连字符。
#:
#: 为什么是这张表：id 会变成**文件名**（``<id>.geojson``）、会进
#: ``tasks.json`` / MBTiles metadata / 地图包 manifest。路径分隔符、盘符、
#: 通配符、点号开头（隐藏文件）在任何一层都是安全隐患或歧义源，因此在入口
#: 统一拦掉。
SAFE_ID_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_.-]+$")

#: 清洗后仍不允许出现的"点文件名"（``.`` / ``..`` 在路径里是特殊语义）。
_FORBIDDEN_IDS = frozenset({".", ".."})

#: id 长度上限。超长 id 会让文件名难读，也会把日志撑爆；截断时保留头部
#: （行政区划代码的区分度在头部）。
_MAX_ID_LENGTH = 64


def _fallback_id(raw: str) -> str:
    """纯中文 / 全符号等清洗后为空的原值 → 确定性兜底 id。

    为什么不转拼音：``北京市`` → ``beijing`` 看着美好，但同一个字在
    "重庆市 / 重庆县" 里同音不同地，拼音表也决定不了 ``重`` 读哪个音 ——
    一个**猜**出来的 id 会比没有 id 更糟（它会被写进任务记录，然后永远
    对不上第二批数据）。

    用内容哈希：同一份原值在任何机器、任何一次导入都得到同一个 id
    （确定性），且与源数据内容绑定（换一批数据不会撞车）。原值照旧进
    ``originalCode``，追溯链路不断。
    """
    import hashlib

    digest = hashlib.sha1(raw.strip().encode("utf-8")).hexdigest()[:8]
    return f"region-{digest}"


def sanitize_region_id(raw: object) -> tuple[str, bool]:
    """把源数据里的行政区 code 清洗成文件名安全的 region id（Spec §7）。

    **优先原样使用**：稳定行政区 code（``330100``、``CN-33``）直接通过，
    不做任何改写 —— id 的稳定性比好看重要（它进任务记录与 manifest）。

    不合法的（含中文、空格、斜杠…）走统一清洗：非法字符换成 ``_``，
    首尾的 ``_.-`` 去掉（避免隐藏文件与难看的名字），仍为空时给一个
    确定性的兜底值。**中文名称绝不直接当 id**（Spec §7）—— 清洗只作为
    万不得已时的兜底，且原始值由调用方记入 ``originalCode``。

    Args:
        raw: 源数据里的 id / code 原值。

    Returns:
        ``(id, sanitized)`` —— 清洗后的 id，以及它是否被改过
        （``True`` 时调用方应记一条 :data:`RegionImportIssueCode.ID_SANITIZED`
        并把原值写进 properties 的 ``originalCode``）。
    """
    text = str(raw if raw is not None else "").strip()
    if not text:
        return "", False
    if text in _FORBIDDEN_IDS:
        # ``.`` / ``..`` 虽然字符全合法，在路径里却是特殊语义（当前目录 /
        # 上级目录）—— 当 id 用会造出 ``province/...geojson`` 这种鬼东西。
        # 与纯中文同样走确定性兜底，原值进 originalCode。
        return _fallback_id(text), True
    if SAFE_ID_PATTERN.match(text):
        return text[:_MAX_ID_LENGTH], False
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    cleaned = cleaned.strip("_.-")
    cleaned = cleaned[:_MAX_ID_LENGTH].strip("_.-")
    if not cleaned:
        # 纯中文 / 全符号的原值：清洗产不出可用 id，走确定性兜底
        # （内容哈希）。**不是**拼音猜测，见 :func:`_fallback_id`。
        return _fallback_id(text), True
    return cleaned, True


# --------------------------------------------------------------------------- #
# 导入配置（Spec §3）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RegionImportConfig:
    """一次行政区数据导入的全部输入（Spec §3）。

    Attributes:
        source_file: 源 GeoJSON（FeatureCollection）。
        id_field: 取 region id 的属性名（通常就是行政区划 code）。
        name_field: 取展示名的属性名。
        level_field: 取等级的属性名（值经 :data:`DEFAULT_LEVEL_ALIASES`
            与 ``level_aliases`` 映射）。
        parent_field: 取父级 id 的属性名；``None`` 表示源数据没有父级列
            （全国级条目本来就不需要它）。
        country_id: 全国级条目的期望 id。两个用途（见下）。
        output_root: 发布目录（默认 ``resources/regions/generated/``）。
        boundary_version: 数据集版本号（写进每个 GeoJSON 的 properties、
            index.json 与 manifest）。
        source_name: 数据来源名称（manifest，Spec §14 / §15）。
        source_license: 授权说明（manifest）。
        source_description: 可选补充说明（manifest）。
        simplify_tolerance: 几何简化容差（度）。``None`` = 不简化
            （**默认**，Spec §6：不默认 simplify 行政边界，避免边界 Tile
            漏下载）。只有用户显式给值才简化。
        level_aliases: 追加的等级映射（键：源数据写法，值：三个行政等级
            之一的 ``value``）。例如 ``{"直辖市": "province"}`` 已经在默认
            表里，但 ``{"level2": "province"}`` 这种就得显式加。

    ``country_id`` 的两个用途：

    1. 源数据**有**全国级条目时，它作为该条目 id 的**覆写**：数据里 code
       是 ``100000`` 而用户想要 ``china`` 时就靠它（省级条目的 parent
       引用会一起改写到新 id，不会留下指向旧 id 的断链）。
    2. 源数据**没有**全国级条目时，它触发**沿用**：从输出目录里已发布的
       数据集继承同 id 的全国条目（文件必须真的存在且合法）。都做不到
       就 FAIL —— 本管线**不凭空造全国边界**（Spec §19 禁止事项：
       不提交来源不明真实边界数据；造一个没有几何的全国条目同样不行）。
    """

    source_file: Path
    id_field: str
    name_field: str
    level_field: str
    parent_field: str | None
    output_root: Path
    boundary_version: str
    country_id: str | None = None
    source_name: str = ""
    source_license: str = ""
    source_description: str = ""
    simplify_tolerance: float | None = None
    level_aliases: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        for name in ("id_field", "name_field", "level_field"):
            value = str(getattr(self, name) or "").strip()
            if not value:
                raise ValueError(f"RegionImportConfig.{name} 不能为空")
            object.__setattr__(self, name, value)
        parent = self.parent_field
        object.__setattr__(
            self, "parent_field", str(parent).strip() if parent else None
        )
        version = str(self.boundary_version or "").strip()
        if not version:
            raise ValueError(
                "RegionImportConfig.boundary_version 不能为空："
                "数据集版本会写进每个边界文件与 manifest，是\"这批瓦片按哪版"
                "边界裁的\"的唯一线索"
            )
        object.__setattr__(self, "boundary_version", version)
        object.__setattr__(self, "source_file", Path(self.source_file))
        object.__setattr__(self, "output_root", Path(self.output_root))
        tolerance = self.simplify_tolerance
        if tolerance is not None:
            tolerance = float(tolerance)
            if tolerance <= 0:
                object.__setattr__(self, "simplify_tolerance", None)
            else:
                object.__setattr__(self, "simplify_tolerance", tolerance)
        country = str(self.country_id or "").strip()
        object.__setattr__(self, "country_id", country or None)

    def describe(self) -> str:
        """一行人类可读的配置摘要（日志用；不含任何路径以外的敏感信息）。"""
        return (
            f"源={self.source_file.name} 字段(id={self.id_field}, "
            f"name={self.name_field}, level={self.level_field}, "
            f"parent={self.parent_field or '无'}) "
            f"版本={self.boundary_version} 发布={self.output_root}"
        )


# --------------------------------------------------------------------------- #
# 源数据条目（Spec §4）
# --------------------------------------------------------------------------- #
@dataclass
class RegionSourceFeature:
    """源数据里的一条 Feature（SourceReader 的产出）。

    Attributes:
        source_index: 在 ``features`` 数组里的序号（从 0 开始；报错时说
            "第 12 条"用的是 ``source_index + 1``）。
        raw_properties: 原始 properties 字典（只读快照）。
        raw_geometry: 原始 geometry 字典。**写回文件时优先用它** ——
            JSON  round-trip 会动浮点数精度（``30.0`` → ``30``、
            长小数被重新格式化），而 Spec §6 要求"原始 Geometry 应尽量
            保留"。几何被修复 / 简化过时才换成形如 ``mapping(geometry)``
            的新字典（``geometry_modified`` 置位）。
        geometry: shapely 几何（校验、算 bounds、相交判定用）。
        geometry_modified: 几何是否被 normalizer 改过（修复 / 简化）。
        geometry_note: 改动的说明（warning issue 的正文用）。
        region_id: 清洗后的 region id（normalizer 填）。
        id_sanitized: id 是否被清洗过（Spec §7 的 ``originalCode`` 线索）。
        level: 映射后的等级（hierarchy_builder 填）。
        parent_ref: 原始父级引用（id 字段的原值，未清洗）。
        name: 展示名（原样）。
        code: 原始 code（``originalCode`` 用；未被清洗时与 region_id 相同）。
    """

    source_index: int
    raw_properties: Mapping[str, Any]
    raw_geometry: Mapping[str, Any] | None
    geometry: Any
    geometry_modified: bool = False
    geometry_note: str = ""
    region_id: str = ""
    id_sanitized: bool = False
    level: RegionLevel | None = None
    parent_ref: str = ""
    name: str = ""
    code: str = ""

    @property
    def label(self) -> str:
        """报错时的称呼（有 id 用 id，否则用序号）。"""
        return self.region_id or f"第 {self.source_index + 1} 条"

    def output_geometry(self) -> Mapping[str, Any] | None:
        """写回 GeoJSON 的 geometry 字典（原样优先，见属性说明）。"""
        if not self.geometry_modified and self.raw_geometry is not None:
            return self.raw_geometry
        if self.geometry is None:  # pragma: no cover - 构造期已保证非空
            return None
        from shapely.geometry import mapping

        return dict(mapping(self.geometry))


# --------------------------------------------------------------------------- #
# 数据集版本（Spec §14）
# --------------------------------------------------------------------------- #
#: 数据集 manifest 的格式版本。
DATASET_MANIFEST_FORMAT_VERSION: int = 1

#: manifest 的文件名（发布目录根下）。
DATASET_MANIFEST_FILENAME: str = "manifest.json"


@dataclass(frozen=True)
class RegionDatasetManifest:
    """已发布行政区数据集的身份证（Spec §14）。

    Attributes:
        format_version: manifest 自身的格式版本。
        boundary_version: 数据集版本（与每个 GeoJSON 里的
            ``properties.boundaryVersion`` 一致）。
        source_name: 数据来源名称。
        created_at: 发布时间（ISO 8601，本地时区）。
        region_count / country_count / province_count / city_count / county_count: 条数。
        source_license: 授权说明。
        source_description: 可选补充说明。

    **不记录 Token 等敏感信息**（Spec §14）：来源是"谁给的、什么版本、
            什么授权"，不是"用什么密钥拿的"。
    """

    format_version: int = DATASET_MANIFEST_FORMAT_VERSION
    boundary_version: str = ""
    source_name: str = ""
    created_at: str = ""
    region_count: int = 0
    country_count: int = 0
    province_count: int = 0
    city_count: int = 0
    county_count: int = 0
    source_license: str = ""
    source_description: str = ""

    def as_dict(self) -> dict[str, Any]:
        """回写成 JSON 结构（键名即持久化契约）。"""
        return {
            "formatVersion": self.format_version,
            "boundaryVersion": self.boundary_version,
            "sourceName": self.source_name,
            "createdAt": self.created_at,
            "regionCount": self.region_count,
            "countryCount": self.country_count,
            "provinceCount": self.province_count,
            "cityCount": self.city_count,
            "countyCount": self.county_count,
            "sourceLicense": self.source_license,
            "sourceDescription": self.source_description,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RegionDatasetManifest:
        """从 JSON 结构读回来（缺字段给默认值 —— 老版本 manifest 向前兼容）。"""

        def _int(value: object) -> int:
            try:
                return int(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return 0

        return cls(
            format_version=_int(payload.get("formatVersion")) or 1,
            boundary_version=str(payload.get("boundaryVersion") or ""),
            source_name=str(payload.get("sourceName") or ""),
            created_at=str(payload.get("createdAt") or ""),
            region_count=_int(payload.get("regionCount")),
            country_count=_int(payload.get("countryCount")),
            province_count=_int(payload.get("provinceCount")),
            city_count=_int(payload.get("cityCount")),
            county_count=_int(payload.get("countyCount")),
            source_license=str(payload.get("sourceLicense") or ""),
            source_description=str(payload.get("sourceDescription") or ""),
        )

    def summary_text(self) -> str:
        """一行摘要（日志与结果框用）。"""
        return (
            f"{self.boundary_version}：{self.region_count} 个区域"
            f"（全国 {self.country_count} / 省 {self.province_count} / "
            f"市 {self.city_count} / 县 {self.county_count}），"
            f"来源 {self.source_name or '未注明'}"
        )


@dataclass(frozen=True)
class ImportReport:
    """一次真实导入的回执（Spec §13 发布成功后返回）。

    Attributes:
        result: 导入前的验证结论（与 dry-run 同一套判据）。
        output_root: 实际发布目录。
        manifest: 写进发布目录的 dataset manifest。
        published_files: 发布的边界文件数（不含 index.json / manifest.json）。
        carried_over_country: 全国条目是否是从旧数据集沿用的（Spec §9
            的直辖市 / 无全国层数据场景）。
    """

    result: RegionImportResult
    output_root: Path
    manifest: RegionDatasetManifest
    published_files: int = 0
    carried_over_country: bool = False

    @property
    def ok(self) -> bool:
        """导入是否成功发布。"""
        return self.result.ok
