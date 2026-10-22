"""风险报告数据模型（Slice 23，Spec §4 / §15）。

两个不可变值对象：

- :class:`DownloadRiskReport` —— **一块地**的风险结论（单任务 / 计划条目都用它）；
- :class:`PlanRiskReport` —— **一份计划**的汇总结论（逐项 + 合计）。

关于字段与 Spec 的两处偏差（都是有意的）
----------------------------------------

1. ``reasons`` / ``recommendations`` 用 **tuple** 而不是 ``list``。这是一个
   ``frozen=True`` 的对象：里面塞一个可变的 ``list``，拿到它的人随手
   ``append`` 就绕过了不可变性（"这个报告明明没变，怎么多了一条原因"）。
   与 :attr:`~app.geo.RegionTileEstimate.per_zoom` 同一处理。
2. 比 Spec §4 多四个字段：:attr:`~DownloadRiskReport.region_level` /
   :attr:`~DownloadRiskReport.region_name`（界面上要说清"这是哪块地、什么等级"）
   与 :attr:`~DownloadRiskReport.per_zoom` /
   :attr:`~DownloadRiskReport.average_tile_size`（Spec §14 要求明细表
   **复用 estimator 的 per_zoom**，而它必须有个载体 —— 不放进报告，
   对话框就只能自己再遍历一次，那正是 §21 禁止的）。

格式化：所有展示文本都从这里出去
----------------------------------

``tiles_text`` / ``size_text`` 一律走 :func:`~app.geo.tile_math.format_tile_count`
与 :func:`~app.geo.tile_math.format_size`（Spec §20：禁止 UI 自己实现多套格式化）。
界面、日志、验收脚本读的是同一份字符串，因此"界面写 2.74 B、日志写 2735435470"
这种两个来源各说各话的情况不会出现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.config.settings import DEFAULT_AVERAGE_TILE_SIZE
from app.geo.tile_math import format_size, format_tile_count
from app.planning.region_level import RegionLevel
from app.planning.risk_level import RISK_LABEL, RiskLevel

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注
    # 导入期**不能**拉 ``app.geo.region_estimator``：它会回头拉 ``app.geo.region``，
    # 而后者在导入 ``app.planning.region_level`` 时会让本包（``app.planning``）
    # 处于"正在初始化"的状态 —— 那条链走到这里就会 ImportError。与
    # ``app/planning/download_plan.py`` 同一处理（见本包 ``__init__`` 的说明）。
    from app.geo.region_estimator import ZoomRegionStats

__all__ = [
    "RISK_ACTUAL_ZOOM_LABEL",
    "RISK_LEVEL_LABEL",
    "RISK_OVER_ZOOM_LABEL",
    "RISK_PLAN_TOTAL_LABEL",
    "RISK_RECOMMENDED_ZOOM_LABEL",
    "RISK_SIZE_LABEL",
    "RISK_TILES_LABEL",
    "DownloadRiskReport",
    "PlanItemRisk",
    "PlanRiskReport",
]

#: 报告里的固定行前缀。放模型层而不是 UI 里：界面、日志、验收脚本要逐字一致。
RISK_LEVEL_LABEL: str = RISK_LABEL                      # "风险："
RISK_TILES_LABEL: str = "预计瓦片："
RISK_SIZE_LABEL: str = "预计容量："
RISK_ACTUAL_ZOOM_LABEL: str = "实际 Zoom："
RISK_RECOMMENDED_ZOOM_LABEL: str = "推荐 Zoom："
RISK_OVER_ZOOM_LABEL: str = "高于推荐 Zoom 的瓦片："
RISK_PLAN_TOTAL_LABEL: str = "计划合计："


@dataclass(frozen=True)
class DownloadRiskReport:
    """一块地的风险结论（Spec §4）。

    Attributes:
        level: 最终风险等级（各维度取最高）。
        region_level: 规划等级（只参与策略判断，不直接决定风险，Spec §7）。
        region_name: 区域展示名（界面上"这是哪块地"）。
        total_tiles: **选中**瓦片数（Spec §5：绝不使用外包矩形候选数）。
        estimated_bytes: 预估容量（同一份 ``selected`` 换算而来）。
        recommended_min_zoom: 该等级的推荐下界；自定义等级为 ``None``
            （自定义区域不套用等级规则，硬给一个区间反而是误导）。
        recommended_max_zoom: 推荐上界；自定义等级为 ``None``。
        actual_min_zoom / actual_max_zoom: 实际要下载的区间。
        over_recommended_tiles: 位于推荐上界**以上**的选中张数。
        over_recommended_percent: 上面那些占全部选中的比例（0~100）。
        reasons: 人话原因（Spec §8：不许只显示一个 ``DANGEROUS``）。
        recommendations: 建议方案（Spec §9；推荐区间来自策略层）。
        requires_confirmation: 是否要弹确认框。
        requires_strong_confirmation: 是否要**强确认**（手输确认词）。
        per_zoom: estimator 的逐级明细（**直接引用，不重新遍历**，Spec §14 / §21）。
        average_tile_size: 逐级容量换算用的平均瓦片体积 —— 与总量同一个来源，
            因此"逐级相加"与"合计"不会对不上。

    Raises:
        ValueError: 张数 / 容量为负，或比例越界。
    """

    level: RiskLevel
    region_level: RegionLevel = RegionLevel.CUSTOM
    region_name: str = ""
    total_tiles: int = 0
    estimated_bytes: int = 0
    recommended_min_zoom: int | None = None
    recommended_max_zoom: int | None = None
    actual_min_zoom: int = 0
    actual_max_zoom: int = 0
    over_recommended_tiles: int = 0
    over_recommended_percent: float = 0.0
    reasons: tuple[str, ...] = ()
    recommendations: tuple[str, ...] = ()
    requires_confirmation: bool = False
    requires_strong_confirmation: bool = False
    per_zoom: tuple[ZoomRegionStats, ...] = field(default_factory=tuple)
    average_tile_size: int = DEFAULT_AVERAGE_TILE_SIZE

    def __post_init__(self) -> None:
        if not isinstance(self.level, RiskLevel):
            raise ValueError(
                f"level 必须是 RiskLevel，实际为 {type(self.level).__name__}"
            )
        for name in ("total_tiles", "estimated_bytes", "over_recommended_tiles"):
            value = getattr(self, name)
            if int(value) < 0:
                raise ValueError(f"{name} 不能为负，实际为 {value}")
        if not 0.0 <= float(self.over_recommended_percent) <= 100.0:
            raise ValueError(
                f"over_recommended_percent 必须在 0~100，实际为 {self.over_recommended_percent}"
            )
        if self.actual_min_zoom > self.actual_max_zoom:
            raise ValueError(
                "actual_min_zoom 不能大于 actual_max_zoom，"
                f"实际为 {self.actual_min_zoom} > {self.actual_max_zoom}"
            )
        if (
            self.recommended_min_zoom is not None
            and self.recommended_max_zoom is not None
            and self.recommended_min_zoom > self.recommended_max_zoom
        ):  # pragma: no cover - 策略层已保证区间成立
            raise ValueError("推荐区间不成立")

    # ------------------------------------------------------------------ #
    # 展示
    # ------------------------------------------------------------------ #
    @property
    def level_text(self) -> str:
        """等级的中文名（``危险``）。"""
        return self.level.display_name

    @property
    def level_line(self) -> str:
        """整行等级文本（``风险：危险（DANGEROUS）``）。"""
        return f"{RISK_LEVEL_LABEL}{self.level.label(with_value=True)}"

    @property
    def tiles_text(self) -> str:
        """紧凑张数（``2.74 B``）。"""
        return format_tile_count(self.total_tiles)

    @property
    def size_text(self) -> str:
        """容量文本（``76.00 TB``）。"""
        return format_size(self.estimated_bytes)

    @property
    def over_tiles_text(self) -> str:
        """超出推荐范围的张数文本。"""
        return format_tile_count(self.over_recommended_tiles)

    @property
    def over_percent_text(self) -> str:
        """超出推荐范围的占比文本（``98.4%``）。"""
        return f"{self.over_recommended_percent:.1f}%"

    @property
    def actual_range_text(self) -> str:
        """实际区间文本（``Z8~18``）。"""
        return f"Z{self.actual_min_zoom}~{self.actual_max_zoom}"

    @property
    def recommended_range_text(self) -> str | None:
        """推荐区间文本（``Z4~10``）；自定义等级为 ``None``。"""
        if self.recommended_min_zoom is None or self.recommended_max_zoom is None:
            return None
        return f"Z{self.recommended_min_zoom}~{self.recommended_max_zoom}"

    @property
    def has_recommendation(self) -> bool:
        """是否有可用的推荐区间（自定义等级没有）。"""
        return self.recommended_range_text is not None

    @property
    def is_normal(self) -> bool:
        """是否无需确认。"""
        return self.level.is_normal

    @property
    def is_dangerous(self) -> bool:
        """是否达到危险等级。"""
        return self.level.is_dangerous

    def is_above_recommendation(self, zoom: int) -> bool:
        """某个缩放级别是否高于推荐上界（明细表那一列用）。"""
        return (
            self.recommended_max_zoom is not None and int(zoom) > self.recommended_max_zoom
        )

    def zoom_tiles(self, zoom: int) -> int:
        """某个缩放级别的选中张数（从 :attr:`per_zoom` 里查，查不到为 0）。"""
        for item in self.per_zoom:
            if int(item.zoom) == int(zoom):
                return int(item.selected_tiles)
        return 0

    # ------------------------------------------------------------------ #
    # 文案
    # ------------------------------------------------------------------ #
    def headline_lines(self) -> list[str]:
        """面板上那几行（Spec §10）：风险 → 张数 → 容量 → 超推荐占比。

        "高于推荐 Zoom 的瓦片"只在真的有超出时出现 —— 一切正常时它恒为 0.0%，
        摆在界面上只会让人去找"是不是出事了"。
        """
        lines = [self.level_line]
        lines.append(f"{RISK_TILES_LABEL}{self.tiles_text}")
        lines.append(f"{RISK_SIZE_LABEL}{self.size_text}")
        if self.over_recommended_tiles > 0:
            lines.append(f"{RISK_OVER_ZOOM_LABEL}{self.over_percent_text}")
        return lines

    def as_lines(self) -> list[str]:
        """完整文案：概览 + 实际 / 推荐区间 + 原因 + 建议（日志与详情对话框）。

        原因与建议各自带一个小标题（而不是逐行加 ``建议：`` 前缀）：
        "建议：推荐 Zoom：Z4~10" 这种双重冒号读起来像程序拼错了。
        """
        lines = list(self.headline_lines())
        lines.append(f"{RISK_ACTUAL_ZOOM_LABEL}{self.actual_range_text}")
        recommended = self.recommended_range_text
        if recommended is not None:
            lines.append(f"{RISK_RECOMMENDED_ZOOM_LABEL}{recommended}")
        if self.reasons:
            lines.append("原因：")
            lines.extend(f"- {item}" for item in self.reasons)
        if self.recommendations:
            lines.append("建议：")
            lines.extend(f"- {item}" for item in self.recommendations)
        return lines

    def describe(self) -> str:
        """一行日志文案（如 ``示例国 Z8~18 危险 2.74 B 张 / 76.00 TB``）。"""
        return (
            f"{self.region_name or '当前区域'} {self.actual_range_text} "
            f"{self.level_text} {self.tiles_text} 张 / {self.size_text}"
        )


@dataclass(frozen=True)
class PlanItemRisk:
    """计划里某一项的风险（Spec §15）。

    Attributes:
        region_id: 区域 id（与 :class:`~app.planning.DownloadPlanItem` 同一个契约）。
        region_name: 展示名。
        zoom_text: 该项的区间文本（``Z4~18``）。
        report: 该项的风险报告。
    """

    region_id: str
    region_name: str
    zoom_text: str
    report: DownloadRiskReport

    @property
    def level(self) -> RiskLevel:
        """这一项的风险等级。"""
        return self.report.level

    def describe(self) -> str:
        """一行文案（如 ``示例国 Z4~18 危险``）。"""
        return (
            f"{self.region_name or self.region_id} {self.zoom_text} "
            f"{self.level.display_name}"
        )


@dataclass(frozen=True)
class PlanRiskReport:
    """整份计划的风险（Spec §15 / §16）。

    总等级 = **两个来源里更高的那个**：

    1. 合计张数 / 合计容量各自的档位；
    2. 所有参与条目里最高的一档。

    只做第 1 条会漏掉"总量不大但有一项是 TB 级"的计划（它照样会卡住磁盘）；
    只做第 2 条会漏掉"每一项都不大、加起来装不下"的计划。

    Attributes:
        level: 计划最终风险等级。
        total_tiles: 参与合计的条目张数之和。
        estimated_bytes: 参与合计的条目容量之和。
        items: 参与合计的条目（**启用且可用**，Spec §12 的同一口径）。
            禁用 / 不可用的条目不在这里，因此不参与计划风险。
        dangerous_items: 其中达到危险等级的条目（界面要逐条点出来）。
        reasons: 计划级原因（人话）。
        recommendations: 计划级建议。
        requires_confirmation: 是否要弹确认框。
        requires_strong_confirmation: 是否要强确认。
    """

    level: RiskLevel
    total_tiles: int = 0
    estimated_bytes: int = 0
    items: tuple[PlanItemRisk, ...] = field(default_factory=tuple)
    dangerous_items: tuple[PlanItemRisk, ...] = field(default_factory=tuple)
    reasons: tuple[str, ...] = ()
    recommendations: tuple[str, ...] = ()
    requires_confirmation: bool = False
    requires_strong_confirmation: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.level, RiskLevel):
            raise ValueError(
                f"level 必须是 RiskLevel，实际为 {type(self.level).__name__}"
            )

    # ------------------------------------------------------------------ #
    # 派生
    # ------------------------------------------------------------------ #
    @property
    def level_text(self) -> str:
        """等级中文名。"""
        return self.level.display_name

    @property
    def level_line(self) -> str:
        """整行等级文本（``风险：危险（DANGEROUS）``）。"""
        return f"{RISK_LEVEL_LABEL}{self.level.label(with_value=True)}"

    @property
    def tiles_text(self) -> str:
        """合计张数文本。"""
        return format_tile_count(self.total_tiles)

    @property
    def size_text(self) -> str:
        """合计容量文本。"""
        return format_size(self.estimated_bytes)

    @property
    def has_dangerous_item(self) -> bool:
        """是否有危险条目。"""
        return bool(self.dangerous_items)

    def level_of(self, region_id: str) -> RiskLevel | None:
        """某一项的风险等级；这一项不参与合计（禁用 / 不可用）时返回 ``None``。"""
        for item in self.items:
            if item.region_id == region_id:
                return item.level
        return None

    def dangerous_names(self) -> list[str]:
        """危险条目的展示名（``示例国 Z4~18``）。"""
        return [item.describe() for item in self.dangerous_items]

    # ------------------------------------------------------------------ #
    # 文案
    # ------------------------------------------------------------------ #
    def headline_lines(self) -> list[str]:
        """合计行文案（计划对话框的合计区）。"""
        lines = [
            f"{RISK_PLAN_TOTAL_LABEL}{self.tiles_text} 张 / {self.size_text}",
            self.level_line,
        ]
        names = self.dangerous_names()
        if names:
            lines.append(f"危险任务：{'、'.join(names)}")
        return lines

    def as_lines(self) -> list[str]:
        """完整文案：合计 + 原因 + 建议（计划确认框与日志）。"""
        lines = list(self.headline_lines())
        if self.reasons:
            lines.append("原因：")
            lines.extend(f"- {item}" for item in self.reasons)
        if self.recommendations:
            lines.append("建议：")
            lines.extend(f"- {item}" for item in self.recommendations)
        return lines

    def describe(self) -> str:
        """一行日志文案。"""
        return (
            f"计划风险 {self.level_text}（{self.tiles_text} 张 / {self.size_text}，"
            f"{len(self.items)} 项参与合计）"
        )
