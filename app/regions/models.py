"""行政区域目录的模型层（Slice 18，Spec §2 / §3）。

这里的 :class:`RegionPreset` 是**目录里的一条元数据记录**：这个区域叫什么、
属于哪一级、它的上级是谁、边界文件在资源目录下的哪个位置。它**不持有几何**
（Spec §2 明令）：一份省级边界就有几 MB，目录里三百个区县就是几百 MB ——
几何只在用户真的选中、要预估或下载时才从磁盘读（Spec §5 的 lazy load）。

同名不同物：两个 ``RegionPreset``
--------------------------------

===============================  ==============================================
``app.geo.region.RegionPreset``  Slice 16 的**扁平预设**：只有文件（相对）名、
                                 没有层级，用来把 ``region_id`` 还原成一个可下载
                                 的区域 —— 续传与 Inspector 走它。
``app.regions.RegionPreset``     本模块的**目录条目**：有 ``parent_id`` / ``code``
                                 / 相对路径，用来回答"中国下面有哪些省"。
===============================  ==============================================

两者**不是同一个类**，也不该"顺手统一"：目录条目要 ``parent_id``，扁平预设没有
这个概念；扁平预设要"一个已经解析好的绝对路径"，目录条目只存相对路径。
转换只有一个方向：目录条目 →（:meth:`~app.regions.loader.RegionLoader.to_geo_preset`）
→ 扁平预设。

``tests/test_region_catalog.py`` 有一条用例把两者的差别钉住；
``tests/test_region_hierarchy.py`` 另有一条守卫用例盯着**两个 china 是否一致**。

为什么问题用枚举而不是异常
--------------------------

一条坏记录不该让整份目录不可用（用户会因此连"中国"都选不到）。所以：

- **整份目录读不出来**（index.json 不存在 / 不是 JSON / 顶层结构不对）→
  抛 :class:`RegionCatalogError`；
- **某一条记录有问题**（id 重复、parent 指向不存在的区域、路径越界…）→
  记一条 :class:`CatalogIssue` 并**跳过那一条**，其余照常工作。

:class:`CatalogIssueCode` 是给日志与测试用的稳定机器码；
面向人的那句说明在 :attr:`CatalogIssue.message`（按 Slice 14 的教训，
**界面与报告里不出现错误码**，所以两个方法分开：
:meth:`CatalogIssue.as_line` 给日志，:meth:`CatalogIssue.describe` 给报告）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from app.geo.bounds import GeoBounds
from app.geo.region import (
    is_safe_region_relative_path,
    normalize_region_relative_path,
    resolve_region_path,
)
from app.planning.region_level import (
    ADMINISTRATIVE_LEVELS,
    PARENT_LEVEL_RULES,
    RegionLevel,
)

__all__ = [
    "RegionPreset",
    "RegionCatalogError",
    "CatalogIssue",
    "CatalogIssueCode",
    "CatalogValidationResult",
    "IssueSeverity",
    # 层级规则：**从规划层转出**（同一个对象，不是第二份表）。
    # 等级字面量（COUNTRY / PROVINCE / CITY）只允许出现在 app.planning 里，
    # ``tests/test_zoom_policy.py`` 的静态守卫盯着这件事。
    "ADMINISTRATIVE_LEVELS",
    "PARENT_LEVEL_RULES",
    # 路径规则同理：转出 app.geo.region 的那一份实现。
    "normalize_region_relative_path",
    "is_safe_region_relative_path",
    "resolve_region_path",
]


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class RegionCatalogError(RuntimeError):
    """整份目录不可用（index.json 缺失 / 读不出 / 顶层结构不对）。"""


class IssueSeverity(Enum):
    """一条目录问题的严重程度。

    ``ERROR`` 表示"这条记录本身不成立"（重复 id、parent 不存在、路径越界…）；
    ``WARNING`` 表示"记录没问题，但当前状态不完整"——典型的就是**边界文件还没放**
    （Slice 16 的既定姿态：缺数据是可预期的部署状态，不是目录的缺陷）。
    """

    ERROR = "error"
    WARNING = "warning"


class CatalogIssueCode(Enum):
    """问题的稳定机器码（日志 grep、测试断言、报告分栏都用它）。

    ``value`` 是持久化契约：文档、验收脚本、将来的排查手册都会引用它，
    改名等于让历史日志对不上号。
    """

    # --- index.json 本身 ---
    # 只有"版本号不认识"这一条：整份文件读不出来是**硬失败**（抛
    # RegionCatalogError 并带上行号），不是一条可以跳过的问题。
    INDEX_VERSION_UNSUPPORTED = "index_version_unsupported"

    # --- 单条记录的结构 ---
    ENTRY_NOT_OBJECT = "entry_not_object"
    MISSING_ID = "missing_id"
    DUPLICATE_ID = "duplicate_id"
    MISSING_NAME = "missing_name"
    MISSING_LEVEL = "missing_level"
    UNKNOWN_LEVEL = "unknown_level"
    BOUNDS_MALFORMED = "bounds_malformed"

    # --- 路径（Spec §12）---
    UNSAFE_PATH = "unsafe_path"
    MISSING_GEOJSON_PATH = "missing_geojson_path"
    GEOJSON_NOT_FOUND = "geojson_not_found"

    # --- 层级（Spec §6 / §13）---
    PARENT_MISSING = "parent_missing"
    SELF_PARENT = "self_parent"
    COUNTRY_HAS_PARENT = "country_has_parent"
    PARENT_LEVEL_INVALID = "parent_level_invalid"
    PARENT_CYCLE = "parent_cycle"

    # --- deep 模式：真的去读几何 ---
    # 坏文件 / 空几何 / 不含面几何都由加载器抛 GeoJSONError，统一归到这一条：
    # "这条数据文件读不出可用的几何"，具体原因在异常消息里（而消息会进 issue）。
    BOUNDARY_UNREADABLE = "boundary_unreadable"
    BOUNDS_MISMATCH = "bounds_mismatch"


@dataclass(frozen=True)
class CatalogIssue:
    """目录里的一条问题。

    Attributes:
        code: 机器码（见 :class:`CatalogIssueCode`）。
        message: 给人看的一句话（**不含错误码**，Spec 的一贯要求）。
        region_id: 相关区域；与具体记录无关时是空串。
        severity: 严重程度。
    """

    code: CatalogIssueCode
    message: str
    region_id: str = ""
    severity: IssueSeverity = IssueSeverity.ERROR

    def __post_init__(self) -> None:
        if not isinstance(self.code, CatalogIssueCode):
            raise ValueError(f"code 必须是 CatalogIssueCode，实际为 {self.code!r}")
        if not isinstance(self.severity, IssueSeverity):
            raise ValueError(f"severity 必须是 IssueSeverity，实际为 {self.severity!r}")
        object.__setattr__(self, "message", str(self.message).strip())
        object.__setattr__(self, "region_id", str(self.region_id or "").strip())

    @property
    def is_error(self) -> bool:
        """是否是"这条记录不成立"级别的问题。"""
        return self.severity is IssueSeverity.ERROR

    def subject(self) -> str:
        """这条问题说的是谁（没写 region_id 时是"整份目录"）。"""
        return self.region_id or "目录"

    def describe(self) -> str:
        """面向人的一行（**不带机器码**，给报告与界面用）。"""
        return f"{self.subject()}：{self.message}"

    def as_line(self) -> str:
        """面向日志的一行（带机器码，好 grep）。"""
        return f"{self.severity.value} {self.code.value} [{self.subject()}] {self.message}"


@dataclass(frozen=True)
class CatalogValidationResult:
    """一次目录校验的结论（Spec §13）。

    Attributes:
        issues: 全部问题，顺序即发现顺序。
        deep: 这次是否做了深度校验（读过每个 GeoJSON）。
        checked: 参与校验的区域条数。
    """

    issues: tuple[CatalogIssue, ...] = ()
    deep: bool = False
    checked: int = 0

    @property
    def errors(self) -> tuple[CatalogIssue, ...]:
        """error 级别的问题。"""
        return tuple(item for item in self.issues if item.is_error)

    @property
    def warnings(self) -> tuple[CatalogIssue, ...]:
        """warning 级别的问题。"""
        return tuple(item for item in self.issues if not item.is_error)

    @property
    def ok(self) -> bool:
        """有没有 error（warning 不算失败：缺边界文件是预期状态）。"""
        return not self.errors

    def count(self, code: CatalogIssueCode) -> int:
        """按机器码数一数（测试里比"某类问题出现几次"用）。"""
        return sum(1 for item in self.issues if item.code is code)

    def has(self, code: CatalogIssueCode) -> bool:
        """有没有某类问题。"""
        return self.count(code) > 0

    def as_lines(self) -> list[str]:
        """报告的正文行（人话在前，机器码只在末尾的统计行里）。"""
        lines = [
            f"行政区目录校验：{'通过' if self.ok else '不通过'}"
            f"（{self.checked} 条区域，{len(self.errors)} 个错误 / "
            f"{len(self.warnings)} 个提醒"
            f"{'，深度模式' if self.deep else '，快速模式'}）"
        ]
        lines.extend(item.describe() for item in self.errors)
        lines.extend(f"提醒：{item.describe()}" for item in self.warnings)
        if self.issues:
            lines.append(
                "机器码统计："
                + "、".join(
                    f"{code.value}×{self.count(code)}"
                    for code in CatalogIssueCode
                    if self.has(code)
                )
            )
        return lines


# --------------------------------------------------------------------------- #
# 层级规则：**权威来源在 app.planning**（Spec §6）
# --------------------------------------------------------------------------- #
# :data:`ADMINISTRATIVE_LEVELS` 与 :data:`PARENT_LEVEL_RULES` 就定义在
# ``app.planning.region_level`` —— 那里是等级枚举所在之处，"等级怎么嵌套"与
# "有哪些等级"必须待在一起，否则加了等级一定会有人忘了改规则。
# 本模块只是把同一份对象转出来给区域层就近使用（**不是第二份表**：
# ``tests/test_region_hierarchy.py`` 有用例断言两边是同一个对象）。
#
# 同理，下面这些 ``is_country`` 之类的判断也一律委托给等级本身，
# 不在这一层写 ``RegionLevel.COUNTRY`` 这样的字面量 ——
# ``tests/test_zoom_policy.py`` 有一条静态守卫专门盯这件事。


# --------------------------------------------------------------------------- #
# 目录条目
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RegionPreset:
    """目录里的一条行政区域记录（Spec §2）。

    Attributes:
        id: 全局唯一的稳定标识（写进任务记录、MBTiles metadata 与 manifest）。
            归一成小写：``region_id`` 是跨层契约，``China`` / ``china`` 混用
            只会在某个查表的地方炸掉。
        name: 展示名（中文）。
        level: 规划等级（Slice 17 的 :class:`~app.planning.RegionLevel`）。
            **只存等级，不存 Zoom 数值**（Spec §9）：区间由
            :data:`~app.planning.DEFAULT_ZOOM_POLICIES` 决定。
        parent_id: 上级区域的 ``id``；``None`` 表示根（全国）。
        geojson_path: **相对资源目录**的边界文件路径，如
            ``country/china.geojson``。允许子目录（省、市各有自己的目录），
            但绝不接受绝对路径或 ``..``（Spec §12）。
        boundary_version: 边界数据版本。``None`` 表示"以文件里的
            ``properties.boundaryVersion`` 为准" —— 数据自带版本时不在目录里
            再抄一份，那两者迟早不一致。
        bounds: 可选的外包矩形。**默认不填**：真实范围一律从几何算
            （Spec §11 禁止拿手填的 73/3/136/54 当 Polygon 的范围）。
            填了的话，deep 校验会拿它跟几何实际范围对一遍。
        code: 可选的国家标准行政区划代码（如 ``330100``），只为将来与外部
            数据对齐预留，本 Slice 不解析也不校验其取值。

    Raises:
        ValueError: ``id`` / ``name`` 为空，或 ``geojson_path`` 写法不安全。
    """

    id: str
    name: str
    level: RegionLevel = RegionLevel.CUSTOM
    parent_id: str | None = None
    geojson_path: str = ""
    boundary_version: str | None = None
    bounds: GeoBounds | None = None
    code: str | None = None

    def __post_init__(self) -> None:
        region_id = str(self.id or "").strip().lower()
        if not region_id:
            raise ValueError("RegionPreset.id 不能为空")
        object.__setattr__(self, "id", region_id)

        name = str(self.name or "").strip()
        if not name:
            raise ValueError(f"RegionPreset.name 不能为空（id={region_id}）")
        object.__setattr__(self, "name", name)

        # 宽容解析：目录里写成 "country" 字符串是常态。认不出来会落到 CUSTOM，
        # 而"未知等级"这件事由**加载器**在更早的地方发现（它看得见原始字符串），
        # 见 app/regions/catalog.py 的 _read_level。
        object.__setattr__(self, "level", RegionLevel.parse(self.level))

        parent = str(self.parent_id or "").strip().lower()
        object.__setattr__(self, "parent_id", parent or None)

        raw = normalize_region_relative_path(self.geojson_path)
        if raw and not is_safe_region_relative_path(raw):
            raise ValueError(
                f"行政区边界路径不安全（id={region_id}）：{self.geojson_path!r} "
                "必须是资源目录内的相对路径，不允许绝对路径或 .."
            )
        object.__setattr__(self, "geojson_path", raw)

        version = str(self.boundary_version or "").strip()
        object.__setattr__(self, "boundary_version", version or None)

        code = str(self.code or "").strip()
        object.__setattr__(self, "code", code or None)

    # ------------------------------------------------------------------ #
    # 派生（一律委托给 :class:`~app.planning.RegionLevel`，
    # 本层不出现等级字面量 —— 见上面"层级规则"一节的说明）
    # ------------------------------------------------------------------ #
    @property
    def is_country(self) -> bool:
        """是不是全国级（层级的根）。"""
        return self.level.is_country

    @property
    def is_province(self) -> bool:
        """是不是省级。"""
        return self.level.is_province

    @property
    def is_city(self) -> bool:
        """是不是城市级。"""
        return self.level.is_city

    @property
    def is_county(self) -> bool:
        """是不是县级（县 / 区 / 旗 / 县级市）。"""
        return self.level.is_county

    @property
    def is_administrative(self) -> bool:
        """是否纳入行政层级规则（全国 / 省 / 市 / 县）。"""
        return self.level.is_administrative

    @property
    def has_parent(self) -> bool:
        """有没有上级。"""
        return self.parent_id is not None

    def path_under(self, root: Path) -> Path:
        """这个区域的边界文件在 ``root`` 下的**规范路径**（不检查是否存在）。

        Raises:
            ValueError: 路径越界（见 :func:`~app.geo.region.resolve_region_path`）。
        """
        return resolve_region_path(root, self.geojson_path)

    def with_bounds(self, bounds: GeoBounds) -> "RegionPreset":
        """换一个声明范围（返回新实例；本类型不可变）。"""
        return replace(self, bounds=bounds)

    # ------------------------------------------------------------------ #
    # 展示
    # ------------------------------------------------------------------ #
    def describe(self) -> str:
        """给用户看的一行，如 ``中国（全国级）``。"""
        return f"{self.name}（{self.level.display_name}）"

    def as_dict(self) -> dict[str, object]:
        """回写成 index.json 的形状（诊断与测试用，不用于发布）。"""
        data: dict[str, object] = {
            "id": self.id,
            "name": self.name,
            "level": self.level.value,
            "parentId": self.parent_id,
            "geojson": self.geojson_path,
            "boundaryVersion": self.boundary_version,
        }
        if self.bounds is not None:
            data["bounds"] = {
                "west": self.bounds.west,
                "south": self.bounds.south,
                "east": self.bounds.east,
                "north": self.bounds.north,
            }
        if self.code is not None:
            data["code"] = self.code
        return data
