"""多级下载计划的数据模型（Slice 20，Spec §2 / §8 / §13）。

一个 :class:`DownloadPlan` 回答的是"我要一次规划好几块地"：

::

    DownloadPlan
    ├── DownloadPlanItem  中国    全国级  Z4~10
    ├── DownloadPlanItem  浙江省  省级    Z9~13
    └── DownloadPlanItem  杭州市  城市级  Z12~16

为什么要单独一个模型，而不是"多建几个 :class:`~app.models.DownloadTask`"
----------------------------------------------------------------------

因为这两件事的**生命周期完全不同**：

============================  ==========================================
:class:`~app.models.DownloadTask`  已经决定要下、且会被下载器反复改写进度
:class:`DownloadPlanItem`          还在规划中，只回答"要下哪块、用多少 Zoom"
============================  ==========================================

计划里的一个条目**没有**下载器认可的状态机、没有进度、也不该有 —— 它一旦
变成"要下"的东西，就由 :meth:`~app.planning.plan_service.PlanService.create_download_tasks`
转成一个真正的 ``DownloadTask``（Spec §14 要求复用现有那条路）。

刻意不存的东西（Spec §2 / §8 / §16）
------------------------------------

- **几何**：条目只记 ``region_id``。一块省级边界的 GeoJSON 有好几 MB，
  存进计划文件等于把"几十行的计划"变成一个几十 MB 的文件；而几时读几何
  由 :class:`~app.regions.RegionService` 按 id 决定（Spec §5 的 lazy load）。
- **瓦片坐标列表**：哪怕只存"全国 Z4~10 的那些瓦片"，也是几万条记录。
  计划只保留**计数**（候选 / 选中 / 过滤 / 容量）与逐级明细，
  真正要下哪些瓦片由下载阶段按同一套 Tile Math 重新算。

哪些字段**不**持久化
--------------------

:attr:`DownloadPlanItem.status` / :attr:`~DownloadPlanItem.invalid_reason`
是**每次打开时重新判定的结论**，不写进 ``plans.json``：它回答的是
"这个区域现在还不在目录里"，而目录是会变的（Spec §18）。把一次判定写进
磁盘，就等于把"当时对"当成"永远对"——用户删掉一个区域后，计划里那条
记录会一直显示"可用"，直到有人手工改文件。

同一个道理也适用于 ``selected_tiles`` / ``estimated_bytes`` / ``per_zoom``：
它们是**估算的产物**，重新打开时重算一遍即可（本地计算，不需要网络）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from app.config.settings import (
    DEFAULT_MAX_ZOOM,
    DEFAULT_MIN_ZOOM,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PLAN_NAME,
    MBTILES_FILE_SUFFIX,
)
from app.geo.tile_math import format_size
from app.models.naming import sanitize_map_name
from app.planning.region_level import RegionLevel

if TYPE_CHECKING:  # pragma: no cover - 只给类型检查器看
    # ``app.geo.region_estimator`` 在导入期会拉进 ``app.geo.region``，
    # 而 ``app.geo.region`` 又会回头拉 ``app.planning`` ——这条边只能用在
    # 注解里（``from __future__ import annotations`` 让它永不求值）。
    from app.geo.region_estimator import ZoomRegionStats
    from collections.abc import Iterable

__all__ = [
    "DownloadPlan",
    "DownloadPlanItem",
    "PlanItemStatus",
    "PLAN_LEVEL_ORDER",
    "new_item_id",
    "new_plan_id",
    "level_sort_key",
    "plan_folder_name",
    "plan_directory",
    "item_file_stem",
    "item_output_path",
    "planned_output_paths",
]


def new_plan_id() -> str:
    """生成计划 id（短 uuid，与任务 id 同一风格：够用且不暴露自增信息）。"""
    return uuid.uuid4().hex[:12]


def new_item_id() -> str:
    """生成计划条目 id。

    条目 id 与 ``region_id`` **不是一回事**：前者是"这一行"的身份，后者是
    "哪块地"。同一块地在一个计划里只允许出现一次（Spec §5），但条目 id 让
    "删掉再加回来"成为一个新条目 —— 界面上的勾选 / 选中行都按条目 id 走，
    不会因为区域 id 不变而把用户的选择带到新条目上。
    """
    return uuid.uuid4().hex[:12]


class PlanItemStatus(Enum):
    """一个计划条目当前**能不能用来生成任务**（Slice 20，Spec §18）。

    ``value`` 会出现在日志里，因此与其它枚举一样是稳定契约。

    判定由 :class:`~app.planning.plan_validator.PlanValidator` 与
    :class:`~app.planning.plan_estimator.DownloadPlanEstimator` 负责
    ——模型自己不去问目录（那是 ``app.regions`` 的事，模型要保持零依赖）。
    """

    #: 区域在目录里、边界在、可估算。
    VALID = "valid"

    #: 区域已从目录删除，或边界文件缺失 / 读不出。**不参与总量、不生成任务**，
    #: 但保留在计划里：用户补上数据文件（或换回区域目录）之后它就能重新可用
    #: ——直接丢掉会让"我明明加过杭州"变成一次静默的数据丢失（Spec §18）。
    INVALID = "invalid"

    @classmethod
    def parse(cls, value: object) -> "PlanItemStatus":
        """宽容解析：认不出来一律按 :attr:`VALID` 处理。

        与 :meth:`RegionLevel.parse` 同一套路（读历史数据的那一侧要宽容）：
        这里的调用方是"从文件读回来"的路径，而本字段**本来就不写盘**，
        认不出来只可能是有人手工改了文件 —— 那种情况下按"还需要重新判定"
        处理，而 :meth:`VALID` 正是"未判定"的初始态。

        >>> PlanItemStatus.parse(None) is PlanItemStatus.VALID
        True
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            for item in cls:
                if item.value == normalized:
                    return item
        return cls.VALID

    @property
    def display_name(self) -> str:
        """界面上的中文（``可用`` / ``不可用``）。"""
        return "可用" if self is PlanItemStatus.VALID else "不可用"

    @property
    def is_valid(self) -> bool:
        """这个状态是否允许生成下载任务。"""
        return self is PlanItemStatus.VALID


#: 计划条目的默认排列顺序（Spec §13）。
#:
#: 存在的理由不是为了"好看"：Slice 21 会按这个顺序**串行**跑计划里的任务，
#: 而"先下全国再下省再下市"是有实际意义的顺序 —— 大范围的先跑，用户能最早
#: 看到一张能用的概览图；反过来（先市后国）则是在细节图上耗完配额之后
#: 才开始铺底图，中途停下时手里只有一堆碎片。
#:
#: ``CUSTOM`` 放在最后：它没有行政层级含义，也没有"该先下"的理由。
#: 加新等级时这里必须同步补一行 —— ``tests/test_download_plan.py`` 有一条
#: 用例直接把枚举与这张表对齐，漏掉就红。
PLAN_LEVEL_ORDER: tuple[RegionLevel, ...] = (
    RegionLevel.COUNTRY,
    RegionLevel.PROVINCE,
    RegionLevel.CITY,
    RegionLevel.MISSION,
    RegionLevel.CUSTOM,
)

#: 等级 → 排序名次（从 :data:`PLAN_LEVEL_ORDER` 推导，**不是**第二份手工表）
_LEVEL_RANK: dict[RegionLevel, int] = {
    level: index for index, level in enumerate(PLAN_LEVEL_ORDER)
}


def level_sort_key(level: object) -> int:
    """等级的排序名次。

    :meth:`RegionLevel.parse` 已经把认不出来的值收敛到 ``CUSTOM``（它在
    :data:`PLAN_LEVEL_ORDER` 里排在最后），所以这里**没有兜底分支** ——
    一个永远走不到的 ``else`` 只会让人以为"还有别的情况要处理"。
    """
    return _LEVEL_RANK[RegionLevel.parse(level)]


@dataclass
class DownloadPlanItem:
    """计划里的一项：一块区域 + 一对 Zoom（Spec §2）。

    字段顺序与 Spec §2 的定义保持一致（前九个），后面几个是估算摘要与运行时
    状态。**没有**几何、没有瓦片列表（见模块文档）。

    Attributes:
        id: 条目 id（:func:`new_item_id`）。
        region_id: 区域权威引用 —— 唯一的"这是哪块地"的依据（Spec §2）。
        region_name: 区域展示名（缓存一份只为读文件时不必立刻查目录；
            真正权威的名字仍然来自目录，重新校验时会被刷新）。
        region_level: 规划等级。**来自目录条目**，不是界面猜的。
        min_zoom / max_zoom: 这一项要下载的缩放区间。
        selected_tiles: 区域内瓦片数（**下游一切数字的分母**，Spec §7）。
            矩形区域等于候选数；多边形是裁剪之后的张数。
        estimated_bytes: 按平均瓦片体积粗估的容量。
        enabled: 是否参与总量、是否生成下载任务（Spec §12）。
        candidate_tiles: 外包矩形里的瓦片数（只用于展示"裁掉了多少"）。
        filtered_tiles: 被多边形裁掉的张数。
        per_zoom: 逐级明细（:class:`~app.geo.region_estimator.ZoomRegionStats`）。
            **只在内存里**存在：它是估算的产物，不进计划文件。
        status: 可用性判定结果（不持久化，见模块文档）。
        invalid_reason: ``status`` 为不可用时**给用户看**的一句话。
            按 Slice 14 的教训，这里不出现错误码。
        error: 估算过程中这个条目自己的失败原因（与 :attr:`invalid_reason`
            的区别：它在估算后仍可能被下一次估算覆盖，用于展示"这次估算跳过了它"）。
    """

    id: str = field(default_factory=new_item_id)
    region_id: str = ""
    region_name: str = ""
    region_level: RegionLevel = RegionLevel.CUSTOM
    min_zoom: int = DEFAULT_MIN_ZOOM
    max_zoom: int = DEFAULT_MAX_ZOOM

    selected_tiles: int = 0
    estimated_bytes: int = 0
    enabled: bool = True

    # --- 估算摘要（Spec §8）：只有计数，没有任何瓦片坐标 ---
    candidate_tiles: int = 0
    filtered_tiles: int = 0
    per_zoom: tuple["ZoomRegionStats", ...] = ()

    # --- 运行时状态：不写盘（见模块文档）---
    status: PlanItemStatus = PlanItemStatus.VALID
    invalid_reason: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        """规整类型。**不校验 Zoom**：非法 Zoom 要能被报出来，而不是炸在这里。"""
        object.__setattr__(self, "id", str(self.id) or new_item_id())
        object.__setattr__(self, "region_id", str(self.region_id).strip())
        object.__setattr__(self, "region_name", str(self.region_name).strip())
        object.__setattr__(self, "region_level", RegionLevel.parse(self.region_level))
        object.__setattr__(self, "status", PlanItemStatus.parse(self.status))
        object.__setattr__(self, "min_zoom", int(self.min_zoom))
        object.__setattr__(self, "max_zoom", int(self.max_zoom))

    # ------------------------------------------------------------------ #
    # 派生
    # ------------------------------------------------------------------ #
    @property
    def zoom_text(self) -> str:
        """缩放区间文本，如 ``Z4~10``（界面与文件名都用它）。"""
        return f"Z{self.min_zoom}~{self.max_zoom}"

    @property
    def tiles_text(self) -> str:
        """千分位张数，如 ``28,341``；还没估算时是 ``—``。"""
        if self.candidate_tiles <= 0 and self.selected_tiles <= 0:
            return "—"
        return f"{self.selected_tiles:,}"

    @property
    def size_text(self) -> str:
        """格式化容量，如 ``820.31 MB``；还没估算时是 ``—``。"""
        if self.candidate_tiles <= 0 and self.estimated_bytes <= 0:
            return "—"
        return format_size(self.estimated_bytes)

    @property
    def is_estimated(self) -> bool:
        """是否已经算过（判据是**候选数**，见 :meth:`DownloadPlan.needs_estimate`）。"""
        return self.candidate_tiles > 0

    @property
    def is_valid(self) -> bool:
        """是否可用（区域在目录里、边界在、估算成功）。"""
        return self.status.is_valid

    @property
    def counts_toward_total(self) -> bool:
        """是否计入计划总量：既要启用，也要可用（Spec §9 / §12）。"""
        return self.enabled and self.is_valid

    @property
    def level_text(self) -> str:
        """等级的中文展示名（``全国级`` / ``省级`` …）。"""
        return self.region_level.display_name

    def clear_estimate(self) -> None:
        """把估算结果清回"尚未估算"（区域或 Zoom 变了时必须调）。"""
        self.selected_tiles = 0
        self.estimated_bytes = 0
        self.candidate_tiles = 0
        self.filtered_tiles = 0
        self.per_zoom = ()

    def mark_invalid(self, reason: str) -> None:
        """标记为不可用并清掉估算（``reason`` 是给用户看的一句话）。"""
        self.status = PlanItemStatus.INVALID
        self.invalid_reason = str(reason)
        self.clear_estimate()

    def mark_valid(self) -> None:
        """标记为可用（区域重新出现在目录里时用）。"""
        self.status = PlanItemStatus.VALID
        self.invalid_reason = ""

    def describe(self) -> str:
        """一行日志文案，如 ``示例国（全国级）Z4~10 1,234 张``。"""
        text = f"{self.region_name or self.region_id}（{self.level_text}）{self.zoom_text}"
        if self.is_estimated:
            text += f" {self.selected_tiles:,} 张"
        if not self.enabled:
            text += "（未启用）"
        if not self.is_valid:
            text += f"（{self.status.display_name}）"
        return text


@dataclass
class DownloadPlan:
    """一次多区域下载计划（Spec §2）。

    Attributes:
        id: 计划 id。
        name: 计划名。它同时是**输出子目录名**（``<输出目录>/<计划名>/``，
            Spec §16），因此界面上必须能改。
        items: 条目，**保持用户添加顺序**（去掉/加回来不该跳位置）。
            展示与生成任务的顺序请用 :meth:`ordered_items`（Spec §13）。
        total_tiles: 启用且可用的条目张数之和（Spec §7 / §12）。
        estimated_bytes: 同上，容量之和。
        output_dir: 计划共享的基础输出目录（Spec §16）。字符串而不是 ``Path``：
            它要进 JSON，而"读回来永远是 ``str``"比"有时是 Path 有时是 str"
            少一类隐形 bug。
        map_layer: 图层标识（``MapLayerType.value``，如 ``vec_w``）。与
            :class:`~app.models.DownloadTask` 用同一个字段名与取值风格：
            生成任务时原样传下去，不必再做一次翻译。
        with_labels: 是否把中文注记合成进瓦片（Slice 6 的语义，计划级共享）。

    Raises:
        ValueError: 名称为空 —— 生成任务时目录名无从谈起。
    """

    id: str = field(default_factory=new_plan_id)
    name: str = ""
    items: list[DownloadPlanItem] = field(default_factory=list)

    total_tiles: int = 0
    estimated_bytes: int = 0

    output_dir: str = ""
    map_layer: str = ""
    with_labels: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", str(self.id) or new_plan_id())
        object.__setattr__(self, "name", str(self.name).strip())
        object.__setattr__(self, "items", list(self.items))
        object.__setattr__(self, "output_dir", str(self.output_dir or "").strip())
        object.__setattr__(self, "map_layer", str(self.map_layer or "").strip())

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    @property
    def is_empty(self) -> bool:
        """一个条目都没有。"""
        return not self.items

    @property
    def count(self) -> int:
        """条目总数（含未启用、含不可用）。"""
        return len(self.items)

    @property
    def enabled_items(self) -> tuple[DownloadPlanItem, ...]:
        """启用**且可用**的条目（Spec §12：不可用的项不参与总量）。"""
        return tuple(item for item in self.items if item.counts_toward_total)

    @property
    def enabled_count(self) -> int:
        """参与下载的条目数。"""
        return len(self.enabled_items)

    @property
    def invalid_items(self) -> tuple[DownloadPlanItem, ...]:
        """不可用的条目（Spec §18：保留在计划里，但标出来）。"""
        return tuple(item for item in self.items if not item.is_valid)

    def item(self, region_id: str) -> DownloadPlanItem | None:
        """按 ``region_id`` 取条目；没有返回 ``None``。

        Spec §5：同一个计划里同一个区域**最多一项**，因此这里的"取一个"
        是良定义的。
        """
        wanted = str(region_id or "").strip()
        for item in self.items:
            if item.region_id == wanted:
                return item
        return None

    def has_region(self, region_id: str) -> bool:
        """计划里有没有这个区域。"""
        return self.item(region_id) is not None

    def index_of(self, region_id: str) -> int:
        """条目的下标；没有返回 ``-1``。"""
        wanted = str(region_id or "").strip()
        for index, item in enumerate(self.items):
            if item.region_id == wanted:
                return index
        return -1

    def needs_estimate(self) -> bool:
        """是否还有条目没算过（界面上显示 ``—`` 的那些）。

        判据是**候选瓦片数**而不是"选中张数 > 0"：一块极小的区域在低 Zoom 下
        也可能选中 0 张（那是一个**结论**，该被报出来），而候选数为 0 只可能
        意味着"根本没算过"——候选范围是纯几何计算，任何非空区域都有瓦片。
        用"张数 > 0"当判据会把前者误判成"还没算"，于是界面永远在重算。

        **不可用的条目也算"没算过"**：它们的数字被清空了，而用户重试一次
        估算（补上边界文件、把区域放回目录之后）正是让它们恢复的唯一路径。
        让它们一直待在"不参与重算"的名单里，等于要求用户手动删了再加回来。
        """
        return any(not item.is_estimated for item in self.items)

    # ------------------------------------------------------------------ #
    # 顺序（Spec §13）
    # ------------------------------------------------------------------ #
    def ordered_items(self) -> list[DownloadPlanItem]:
        """按等级排序后的全部条目；同级别**保持添加顺序**。

        ``list.sort`` 是稳定排序，因此"同级别保持用户添加顺序"不需要额外
        写一个序号字段 —— 输入顺序本身就是那个序号。
        """
        return sorted(self.items, key=lambda item: level_sort_key(item.region_level))

    def ordered_enabled_items(self) -> list[DownloadPlanItem]:
        """按等级排序后、参与下载的那些条目（生成任务的顺序）。"""
        ordered = self.ordered_items()
        return [item for item in ordered if item.counts_toward_total]

    def coverage_levels(self) -> tuple[RegionLevel, ...]:
        """启用条目覆盖到的**行政**等级（Spec §19）。

        返回顺序固定为"从大到小"（见 :data:`PLAN_LEVEL_ORDER`），且去重 ——
        界面上那句"已形成三级离线地图覆盖"拼的就是它。``MISSION`` /
        ``CUSTOM`` 不算：那句提示说的是行政层级覆盖，把作业区算进去会让人
        以为"全国 + 一条航线"也算三级。
        """
        covered = {
            item.region_level
            for item in self.enabled_items
            if item.region_level.is_administrative
        }
        return tuple(level for level in PLAN_LEVEL_ORDER if level in covered)

    # ------------------------------------------------------------------ #
    # 编辑
    # ------------------------------------------------------------------ #
    def add_item(self, item: DownloadPlanItem) -> DownloadPlanItem:
        """加入一个条目；同 ``region_id`` 已存在时**替换**它（Spec §5）。"""
        index = self.index_of(item.region_id)
        if index >= 0:
            self.items[index] = item
        else:
            self.items.append(item)
        return item

    def remove_region(self, region_id: str) -> bool:
        """按 ``region_id`` 删掉一个条目。

        Returns:
            ``True`` 表示删掉了；``False`` 表示本来就没有（**不抛异常**：
            界面上的「移除」按钮可能被连点两次）。
        """
        index = self.index_of(region_id)
        if index < 0:
            return False
        del self.items[index]
        return True

    def set_enabled(self, region_id: str, enabled: bool = True) -> bool:
        """启用 / 禁用一个条目（Spec §12）。

        Returns:
            ``True`` 表示改动了；``False`` 表示没有这个区域。
        """
        item = self.item(region_id)
        if item is None:
            return False
        item.enabled = bool(enabled)
        return True

    def clear_items(self) -> None:
        """清空条目（保留计划本身：id / 名字 / 输出设置都不动）。"""
        self.items.clear()
        self.recalculate_totals()

    # ------------------------------------------------------------------ #
    # 汇总
    # ------------------------------------------------------------------ #
    def recalculate_totals(self) -> "DownloadPlan":
        """按"启用且可用"的条目重算总量（Spec §7 / §12）。

        单独一个方法而不是只让估算器顺手写一遍：**禁用 / 删除 / 勾选**都会
        改变总量，而那三件事都不需要重新估算。重算只是几个加法。
        """
        self.total_tiles = sum(item.selected_tiles for item in self.enabled_items)
        self.estimated_bytes = sum(item.estimated_bytes for item in self.enabled_items)
        return self

    @property
    def total_tiles_text(self) -> str:
        """总张数的千分位文本，如 ``28,341``。"""
        return f"{self.total_tiles:,}"

    @property
    def size_text(self) -> str:
        """总容量的格式化文本，如 ``5.10 GB``。"""
        return format_size(self.estimated_bytes)

    def describe(self) -> str:
        """一行日志文案，如 ``离线计划（3 项 / 12,345 张 / 361.93 MB）``。"""
        return (
            f"{self.name or self.id}（{self.enabled_count} 项 / "
            f"{self.total_tiles_text} 张 / {self.size_text}）"
        )


# --------------------------------------------------------------------------- #
# 输出命名（Spec §15 / §16）
# --------------------------------------------------------------------------- #
# 规则放在**模型模块**里而不是界面或校验器里，是为了让"文件名怎么来的"只有
# 一处实现：界面要显示它、校验器要拿它看"目标文件是不是已经存在"、生成任务时
# 要真的用它。三处各写一遍的话，迟早会出现"界面上写着 a_z4_10.mbtiles、
# 实际写进了 a_z4_10-2.mbtiles"。
#
# 复用 :func:`app.models.naming.sanitize_map_name`（Slice 7 起就是项目里唯一的
# 文件名清理实现）：Windows 非法字符、结尾的点与空格、``CON``/``LPT1`` 这类
# 保留名都由它处理，这里不另写一套。


def plan_folder_name(plan: DownloadPlan) -> str:
    """计划的输出子目录名（Spec §16）。

    取计划名，交给 :func:`~app.models.naming.sanitize_map_name` 规整；
    计划名为空时落到 :data:`~app.config.settings.DEFAULT_PLAN_NAME`
    —— 空名字会直接变成"输出目录根"，几个计划的文件就会互相撞在一起。
    """
    return sanitize_map_name(plan.name or DEFAULT_PLAN_NAME)


def plan_directory(plan: DownloadPlan, *, output_dir: object = None) -> Path:
    """计划共享的输出目录：``<输出目录>/<计划名>/``（Spec §16）。

    Args:
        plan: 计划。
        output_dir: 覆盖计划自带的输出目录（界面上的「输出目录」输入框可能
            刚被改过而计划还没保存）。``None`` 时用 ``plan.output_dir``；
            两者都空则落到 :data:`~app.config.settings.DEFAULT_OUTPUT_DIR`。

    **只算路径，不创建目录**：本 Slice 只创建任务配置，不提前落任何文件
    （Spec §16 最后一句）。真正的目录由下载阶段创建。
    """
    base = plan.output_dir if output_dir is None else str(output_dir or "")
    return Path(str(base or DEFAULT_OUTPUT_DIR)) / plan_folder_name(plan)


def item_file_stem(item: DownloadPlanItem, *, taken: "Iterable[str]" = ()) -> str:
    """一个条目的文件名主体（不含扩展名），如 ``china_z4_10``（Spec §15）。

    形如 ``<区域 id>_z<下界>_<上界>``：区域 id 而不是展示名，因为 id 是
    稳定且跨平台的（中文名字在命令行、外部工具、日志 grep 里都更难处理），
    而"这块地下的是哪段 Zoom"必须一眼看得出来 —— 那正是同一个区域在计划里
    可能出现多次唯一的区别。

    Args:
        item: 计划条目。
        taken: 已经用掉的名字集合。命中时追加 ``-2`` / ``-3`` ……

    Spec §5 已经保证同一个计划里不会有两个同名条目，因此正常的计划走不到
    去重分支；留着它是为了"文件被手工编辑过"或"将来允许同一区域两种 Zoom"
    时不会静默覆盖 —— 覆盖的是用户上一次下载的成果，那是不可逆的。
    """
    stem = sanitize_map_name(
        f"{item.region_id}_z{item.min_zoom}_{item.max_zoom}"
    )
    used = {str(name) for name in taken}
    if stem not in used:
        return stem
    index = 2
    while f"{stem}-{index}" in used:
        index += 1
    return f"{stem}-{index}"


def item_output_path(
    plan: DownloadPlan,
    item: DownloadPlanItem,
    *,
    output_dir: object = None,
) -> Path:
    """一个条目的输出路径（**在它自己这一项参与下载时**才是最终文件）。

    等价于 :func:`planned_output_paths` 里属于它的那一条；单独一个函数是为了
    让"某一项会写到哪"这个问法不必先把整份映射算出来。
    """
    paths = planned_output_paths(plan, output_dir=output_dir)
    return paths.get(
        item.region_id,
        plan_directory(plan, output_dir=output_dir)
        / f"{item_file_stem(item)}{MBTILES_FILE_SUFFIX}",
    )


def planned_output_paths(
    plan: DownloadPlan, *, output_dir: object = None
) -> dict[str, Path]:
    """计划会产出的全部文件：``{region_id: 路径}``（Spec §15 / §16）。

    **只有参与下载的条目**在表里（启用且可用，顺序即
    :meth:`DownloadPlan.ordered_enabled_items`）——这张表就是"生成任务时会发生
    什么"的答案，界面拿它显示"会写到哪"、校验器拿它查"是不是已经有同名文件了"、
    生成任务时按它落盘。

    三处共用同一个函数是**硬要求**：如果界面按一套规则显示、生成任务时按另一套
    命名，用户会在按下「生成任务」之后发现文件名和他刚才看到的不一样 ——
    而那是他自己磁盘上的东西，没有比这更让人不信任的体验。

    去重按表格顺序累积（见 :func:`item_file_stem` 的 ``taken``）。
    """
    directory = plan_directory(plan, output_dir=output_dir)
    paths: dict[str, Path] = {}
    taken: list[str] = []
    for item in plan.ordered_enabled_items():
        stem = item_file_stem(item, taken=taken)
        taken.append(stem)
        paths[item.region_id] = directory / f"{stem}{MBTILES_FILE_SUFFIX}"
    return paths
