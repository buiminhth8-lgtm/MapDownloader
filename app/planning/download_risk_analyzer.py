"""下载风险分析器（Slice 23，Spec §5 / §7 / §15 / §18 / §19 / §21）。

把"这块地的等级 + Zoom 区间 + 已有估算"变成一份 :class:`DownloadRiskReport`。
它是**纯计算**：不枚举瓦片、不请求网络、不写磁盘、不 import Qt。

三条硬约束（Spec §5 / §21 / §26）
---------------------------------

1. **只吃现成的估算结果。** 输入必须是 :class:`RegionTileEstimate` 或计划条目
   已经存下来的那几个数：张数、容量、逐级明细。本模块里**没有任何**
   ``calculate_tile_range`` / ``iter_selected_tiles`` / ``RegionTileEstimator``
   —— 既不用它们，也不 import 它们（``tests/test_download_risk_analyzer.py``
   用 AST 守着这条）。风险计算的复杂度是
   ``O(zoom 级数)``，与区域多大无关。
2. **只用 ``selected`` 张数。** 外包矩形候选数在渤海湾、广州湾这类形状上能虚高
   一个数量级，拿它当风险依据会得到"你其实下不了这么多，但我按这么多警告你"。
   界面上候选数可以显示（它是"不裁剪会有多少"），风险一律看选中数。
3. **等级只参与策略判断，不直接决定风险**（Spec §7）。任务区 Z15~18 的 Zoom
   数字比全国 Z4~10 高得多，但它只覆盖一条走廊 —— 判定一律看实际张数与容量。

为什么单独一个类而不是几个函数
------------------------------

因为策略表（:class:`~app.planning.RiskPolicy`）与 Zoom 策略
（:class:`~app.planning.ZoomStrategyService`）都可以注入：测试要能构造
"阈值收紧一半"的策略来验证边界，将来也可能有"保守模式"。类持有这两份依赖，
调用方（界面 / 计划对话框）就不必各自管理。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from app.config.settings import DEFAULT_AVERAGE_TILE_SIZE
from app.geo.tile_math import format_size, format_tile_count, validate_zoom
from app.planning.download_plan import DownloadPlan, DownloadPlanItem
from app.planning.region_level import RegionLevel
from app.planning.risk_level import RiskLevel
from app.planning.risk_models import (
    DownloadRiskReport,
    PlanItemRisk,
    PlanRiskReport,
)
from app.planning.risk_policy import DEFAULT_RISK_POLICY, RiskPolicy
from app.planning.zoom_strategy import ZoomStrategyService

__all__ = ["DownloadRiskAnalyzer", "RISK_ESTIMATE_ATTRS"]

_logger = logging.getLogger(__name__)

#: 一份"可分析的估算"必须提供的字段。刻意用**鸭子类型**而不是
#: ``isinstance(estimate, RegionTileEstimate)``：后者要在导入期拉
#: ``app.geo.region_estimator``，而那正是"能枚举瓦片的那一层"——
#: 一个不 import 它、也拿不到枚举能力的分析器，才是"不会偷偷重算"的分析器。
RISK_ESTIMATE_ATTRS: tuple[str, ...] = (
    "selected_tiles",
    "estimated_size_bytes",
    "per_zoom",
)


class DownloadRiskAnalyzer:
    """风险分析入口（Spec §5）。

    Attributes:
        policy: 阈值策略（默认 :data:`~app.planning.DEFAULT_RISK_POLICY`）。
        strategy: Zoom 策略服务（推荐区间与建议文案的来源）。
        average_tile_size: 逐级容量换算用的平均瓦片体积。默认值与
            :class:`~app.geo.RegionTileEstimator` 的默认值**同一个常量**，
            因此"逐级相加"与"总量"必然一致。
    """

    def __init__(
        self,
        policy: RiskPolicy | None = None,
        *,
        strategy: ZoomStrategyService | None = None,
        average_tile_size: int = DEFAULT_AVERAGE_TILE_SIZE,
    ) -> None:
        """
        Args:
            policy: 阈值策略；``None`` 用默认那一份。
            strategy: Zoom 策略服务；``None`` 时自建（就是默认策略表）。
                界面请把自己那一个传进来 —— 否则"界面上的推荐区间"与
                "风险报告里的推荐区间"会是两个实例算出来的（值一样，但
                将来注入自定义策略时就会分叉）。
            average_tile_size: 平均瓦片体积（逐级容量展示用）。

        Raises:
            ValueError: ``average_tile_size`` 非正。
        """
        self._policy = policy if policy is not None else DEFAULT_RISK_POLICY
        self._strategy = strategy if strategy is not None else ZoomStrategyService()
        size = int(average_tile_size)
        if size <= 0:
            raise ValueError(f"average_tile_size 必须为正，实际为 {size}")
        self._average_tile_size = size

    # ------------------------------------------------------------------ #
    # 只读
    # ------------------------------------------------------------------ #
    @property
    def policy(self) -> RiskPolicy:
        """阈值策略。"""
        return self._policy

    @property
    def strategy(self) -> ZoomStrategyService:
        """Zoom 策略服务。"""
        return self._strategy

    @property
    def average_tile_size(self) -> int:
        """平均瓦片体积。"""
        return self._average_tile_size

    # ------------------------------------------------------------------ #
    # 单块地
    # ------------------------------------------------------------------ #
    def analyze(
        self,
        region_level: object,
        min_zoom: int,
        max_zoom: int,
        estimate: object,
    ) -> DownloadRiskReport:
        """分析一块地的风险（Spec §5）。

        Args:
            region_level: 规划等级（:class:`~app.planning.RegionLevel`）。
            min_zoom: 实际下界。
            max_zoom: 实际上界。
            estimate: 预估结果（:class:`~app.geo.RegionTileEstimate` 或任何
                提供 ``selected_tiles`` / ``estimated_size_bytes`` /
                ``per_zoom`` 的对象）。

        Returns:
            :class:`DownloadRiskReport`。

        Raises:
            ValueError: 等级非法、Zoom 非法 / 上下界反了，或 ``estimate``
                缺少必要字段。
        """
        low = validate_zoom(min_zoom)
        high = validate_zoom(max_zoom)
        if low > high:
            raise ValueError(
                f"min_zoom 不能大于 max_zoom，实际 min_zoom={low}, max_zoom={high}"
            )
        # 先问策略层要一次策略：等级非法时在这里就报出来（信息比后面的断言清楚）
        self._strategy.get_policy(region_level)
        total_tiles, total_bytes, per_zoom = _read_estimate(estimate)
        return self._build(
            region_level,
            low,
            high,
            total_tiles=total_tiles,
            total_bytes=total_bytes,
            per_zoom=per_zoom,
            region_name=str(getattr(estimate, "region_name", "") or ""),
        )

    def analyze_item(self, item: DownloadPlanItem) -> DownloadRiskReport:
        """分析计划里的一个条目 —— **完全使用它已经存下来的估算**。

        计划条目在估算阶段就把张数、容量与逐级明细写进了自己
        （:meth:`~app.planning.plan_estimator.DownloadPlanEstimator.estimate_item`），
        因此这里不需要（也不允许）再去碰几何。

        Args:
            item: 计划条目。

        Returns:
            风险报告；条目还没估算过（张数为 0）时是一份 NORMAL 报告。

        Raises:
            ValueError: 条目不是 :class:`~app.planning.DownloadPlanItem`，
                Zoom 非法。
        """
        if not isinstance(item, DownloadPlanItem):
            raise ValueError(
                f"item 必须是 DownloadPlanItem，实际为 {type(item).__name__}"
            )
        low = validate_zoom(item.min_zoom)
        high = validate_zoom(item.max_zoom)
        if low > high:
            # 与 ``analyze`` 同一判据：条目里的 Zoom 是用户/计划文件给的，
            # 它是**外部输入**，不能假设一定成立。
            raise ValueError(
                f"条目 {item.region_id} 的 Zoom 区间不成立："
                f"min_zoom={low} > max_zoom={high}"
            )
        return self._build(
            item.region_level,
            low,
            high,
            total_tiles=int(item.selected_tiles),
            total_bytes=int(item.estimated_bytes),
            per_zoom=item.per_zoom,
            region_name=item.region_name or item.region_id,
        )

    # ------------------------------------------------------------------ #
    # 整份计划
    # ------------------------------------------------------------------ #
    def analyze_plan(self, plan: DownloadPlan) -> PlanRiskReport:
        """分析整份计划：逐项 + 合计（Spec §15 / §16）。

        两个口径必须说清楚：

        * **只有"启用且可用"的条目参与**（``counts_toward_total``，与
          Slice 20 的合计口径逐字一致）。禁用项不会被下载，把它算进风险
          等于对着一个不会发生的事警告用户；不可用项连张数都没有。
        * **计划等级取"两个来源里更高的那个"**：合计张数 / 容量本身的档位，
          与所有条目里最高的一档。只做前者会漏掉"总量不大但有一项是 TB 级"，
          只做后者会漏掉"每项都不大、加起来装不下"。

        Args:
            plan: 计划（只读，不会被改写）。

        Returns:
            :class:`PlanRiskReport`。

        Raises:
            ValueError: ``plan`` 不是 :class:`~app.planning.DownloadPlan`。
        """
        if not isinstance(plan, DownloadPlan):
            raise ValueError(
                f"plan 必须是 DownloadPlan，实际为 {type(plan).__name__}"
            )

        # 严重的排前面：界面与建议文案都希望"先看最要紧的那一项"
        risks: list[PlanItemRisk] = []
        for item in plan.ordered_items():
            if not item.counts_toward_total:
                continue
            risks.append(
                PlanItemRisk(
                    region_id=item.region_id,
                    region_name=item.region_name or item.region_id,
                    zoom_text=item.zoom_text,
                    report=self.analyze_item(item),
                )
            )
        risks.sort(
            key=lambda entry: (-entry.level.severity, entry.region_id)
        )

        total_tiles = sum(entry.report.total_tiles for entry in risks)
        total_bytes = sum(entry.report.estimated_bytes for entry in risks)
        totals_level = self._policy.level_for(total_tiles, total_bytes)
        max_item_level = RiskLevel.worst_of(*(entry.level for entry in risks))
        # 两个来源取高者（Spec §15）。默认阈值是**单调**的（合计张数 / 容量都不
        # 小于任何单项），因此 ``totals_level`` 通常已经压住 ``max_item_level``；
        # 这一步是**契约**而不是装饰：策略可注入，将来若有只看单项的保守模式，
        # 计划等级必须跟着单项走，而不是静默按合计降级。
        level = totals_level.worst(max_item_level)
        dangerous = tuple(entry for entry in risks if entry.level.is_dangerous)

        reasons = _plan_reasons(
            risks,
            total_tiles=total_tiles,
            total_bytes=total_bytes,
            totals_level=totals_level,
            max_item_level=max_item_level,
            dangerous=dangerous,
        )

        report = PlanRiskReport(
            level=level,
            total_tiles=total_tiles,
            estimated_bytes=total_bytes,
            items=tuple(risks),
            dangerous_items=dangerous,
            reasons=tuple(reasons),
            recommendations=tuple(_plan_recommendations(risks)),
            requires_confirmation=self._policy.requires_confirmation(level),
            requires_strong_confirmation=self._policy.requires_strong_confirmation(
                level
            ),
        )
        _logger.info("风险分析：%s", report.describe())
        return report

    # ------------------------------------------------------------------ #
    # 内部：构造报告
    # ------------------------------------------------------------------ #
    def _build(
        self,
        region_level: object,
        min_zoom: int,
        max_zoom: int,
        *,
        total_tiles: int,
        total_bytes: int,
        per_zoom: Sequence[object],
        region_name: str,
    ) -> DownloadRiskReport:
        """把数字变成报告。

        分两步：先造一份"只有数字"的报告，再用**它自己**的属性去写原因与建议。
        这样"原因里说的 2.74 B"与"报告里的 tiles_text"必然是同一个字符串 ——
        各算一遍迟早会出现"原因写 2.73 B、面板写 2.74 B"这种没法排查的差异。
        """
        # 两份策略各管一件事，名字必须分开：Zoom 策略给**推荐区间**，
        # 风险策略给**阈值**。合成一个变量名会让"改阈值"变成改错文件。
        zoom_policy = self._strategy.get_policy(region_level)
        level = RegionLevel.parse(region_level)
        recommended_max = (
            None if level.is_custom else zoom_policy.recommended_max_zoom
        )
        recommended_min = (
            None if level.is_custom else zoom_policy.recommended_min_zoom
        )
        detail = tuple(per_zoom)
        over_tiles = (
            0
            if recommended_max is None
            else sum(
                int(getattr(item, "selected_tiles", 0))
                for item in detail
                if int(getattr(item, "zoom", -1)) > recommended_max
            )
        )
        total = max(0, int(total_tiles))
        percent = (over_tiles * 100.0 / total) if total else 0.0

        base = DownloadRiskReport(
            level=self._policy.level_for(total, max(0, int(total_bytes))),
            region_level=level,
            region_name=str(region_name),
            total_tiles=total,
            estimated_bytes=max(0, int(total_bytes)),
            recommended_min_zoom=recommended_min,
            recommended_max_zoom=recommended_max,
            actual_min_zoom=min_zoom,
            actual_max_zoom=max_zoom,
            over_recommended_tiles=min(over_tiles, total) if total else over_tiles,
            over_recommended_percent=percent,
            requires_confirmation=False,  # 先占位，下一步按等级定
            per_zoom=detail,
            average_tile_size=self._average_tile_size,
        )
        # 确认规则直接由**等级**推出来（Spec §12），而不是由张数再判一次：
        # 否则"什么算危险"就有了第二份阈值。
        base = replace(
            base,
            requires_confirmation=self._policy.requires_confirmation(base.level),
            requires_strong_confirmation=self._policy.requires_strong_confirmation(
                base.level
            ),
        )
        return _with_text(base, self._policy)


# --------------------------------------------------------------------------- #
# 报告文案
# --------------------------------------------------------------------------- #
def _with_text(report: DownloadRiskReport, policy: RiskPolicy) -> DownloadRiskReport:
    """给报告补上理由与建议（Spec §8 / §9）。

    用 ``dataclasses.replace`` 而不是"先算文案再构造"：文案里的每一个数字
    都取自报告自己的属性，因此不可能出现两处对不上的值。

    放在模块级而不是 :meth:`DownloadRiskAnalyzer._build` 的后半段：这段逻辑
    只依赖"一份报告 + 一份策略"，没有任何实例状态，因此它可以被单独读、单独测。
    """
    reasons: list[str] = []
    recommendations: list[str] = []

    if report.has_recommendation:
        reasons.append(
            f"区域等级：{report.region_level.display_name}，"
            f"推荐区间 {report.recommended_range_text}。"
        )
    else:
        reasons.append(f"区域等级：{report.region_level.display_name}（不套用等级建议）。")

    reasons.append(f"预计下载 {report.total_tiles:,} 张瓦片。")
    reasons.append(f"预计容量约 {report.size_text}。")

    if report.over_recommended_tiles > 0 and report.recommended_max_zoom is not None:
        reasons.append(
            f"{report.region_level.display_name}推荐最大 Zoom 为 "
            f"Z{report.recommended_max_zoom}，当前为 Z{report.actual_max_zoom}。"
        )
        reasons.append(
            f"{report.over_percent_text} 的瓦片位于推荐 Zoom 以上"
            f"（{report.over_recommended_tiles:,} 张）。"
        )

    if report.has_recommendation:
        # 刻意**不**把"推荐 Zoom：Z4~10"塞进 recommendations：它已经在报告的
        # ``推荐 Zoom：`` 那一行里了，重复一次只会让"建议"列表看起来更像噪音。
        # 下面的建议文案里已经包含推荐区间（来自策略层），信息一点没少。
        recommendations.append(
            policy.advice_for(report.region_level, report.recommended_range_text or "")
        )
        if report.over_recommended_tiles > 0 and report.recommended_max_zoom is not None:
            recommendations.append(
                f"把 Max Zoom 从 Z{report.actual_max_zoom} 降到 "
                f"Z{report.recommended_max_zoom} 可以减少约 "
                f"{report.over_tiles_text} 张瓦片（{report.over_percent_text}）。"
            )
    else:
        recommendations.append(policy.advice_for(report.region_level, ""))

    if report.level.is_dangerous:
        recommendations.append(
            f"当前规模（{report.tiles_text} 张 / {report.size_text}）不建议直接执行。"
        )
    return replace(
        report, reasons=tuple(reasons), recommendations=tuple(recommendations)
    )


def _plan_reasons(
    risks: Sequence[PlanItemRisk],
    *,
    total_tiles: int,
    total_bytes: int,
    totals_level: RiskLevel,
    max_item_level: RiskLevel,
    dangerous: Sequence[PlanItemRisk],
) -> list[str]:
    """计划级的"为什么是这个等级"（Spec §16）。

    单独一个纯函数有两个好处：它只吃"已经算好的几个数"，因此能脱离计划对象
    单测；同时 :meth:`DownloadRiskAnalyzer.analyze_plan` 的主流程只剩判定，
    读起来是一张表。

    两种最容易被当成"算错了"的情形必须解释：

    * **合计不大但有单项把等级抬起来**。默认阈值是单调的（合计张数 / 容量不
      小于任何单项），所以这行在默认策略下不会出现 —— 它守的是**注入策略**
      （如将来的保守模式）：等级变了，界面必须说得出是谁干的。
    * **有危险项** —— 逐条点名（``示例国（全国级）Z4~18 17.9 B 张``），
      而不是笼统地说一句"计划太大"。
    """
    reasons = [
        f"{format_tile_count(total_tiles)} 张瓦片、约 {format_size(total_bytes)}"
        f"（{len(risks)} 项参与合计）。"
    ]
    if max_item_level.severity > totals_level.severity:
        reasons.append(
            f"合计规模本身只有 {totals_level.display_name}，"
            f"但有单项达到{_level_phrase(max_item_level)}。"
        )
    if dangerous:
        reasons.append(
            f"其中 {len(dangerous)} 项达到危险等级："
            f"{'、'.join(entry.describe() for entry in dangerous)}。"
        )
    if not risks:
        reasons.append("计划里还没有启用且可用的区域。")
    return reasons


def _plan_recommendations(risks: Sequence[PlanItemRisk]) -> list[str]:
    """把逐项建议按"等级组合"归并成几条（Spec §15 / §16）。

    十个省各写一遍"建议按 Z9~13 下载省域"会把对话框变成一堵墙，
    因此同等级、同风险档的条目合并成一行（``浙江省、江苏省：……``）。
    严重程度高的排在前面 —— 用户第一眼要看的是最要紧的那条建议。
    """
    groups: dict[tuple[RegionLevel, RiskLevel], list[str]] = {}
    samples: dict[tuple[RegionLevel, RiskLevel], DownloadRiskReport] = {}
    order: list[tuple[RegionLevel, RiskLevel]] = []
    for entry in risks:
        if entry.level.is_normal:
            continue
        key = (entry.report.region_level, entry.level)
        if key not in groups:
            groups[key] = []
            samples[key] = entry.report
            order.append(key)
        groups[key].append(entry.region_name)

    lines: list[str] = []
    for key in order:
        names = "、".join(groups[key])
        advice = _first_advice(samples[key])
        lines.append(f"{names}：{advice}")
    return lines


def _first_advice(report: DownloadRiskReport) -> str:
    """取报告里的第一条建议（没有再退回空串）。

    报告保证"只要不是自定义等级就至少有一条建议"，因此这里的兜底分支
    只在等级策略被换掉时才走得到。
    """
    return report.recommendations[0] if report.recommendations else ""


def _level_phrase(level: RiskLevel) -> str:
    """把等级写成"XX 等级"（拼进句子用）。"""
    return f"{level.display_name}等级"


def _read_estimate(estimate: object) -> tuple[int, int, tuple[object, ...]]:
    """从估算对象里读出三个数（缺字段就报清楚缺哪个）。

    Raises:
        ValueError: ``estimate`` 缺少 :data:`RISK_ESTIMATE_ATTRS` 里的字段。
    """
    missing = [name for name in RISK_ESTIMATE_ATTRS if not hasattr(estimate, name)]
    if missing:
        raise ValueError(
            "estimate 必须提供 "
            + " / ".join(RISK_ESTIMATE_ATTRS)
            + f"，实际缺少：{'、'.join(missing)} —— "
            "风险分析只吃现成的估算结果，不接受'先给我几何我算给你看'"
        )
    per_zoom = getattr(estimate, "per_zoom") or ()
    return (
        int(getattr(estimate, "selected_tiles")),
        int(getattr(estimate, "estimated_size_bytes")),
        tuple(per_zoom),
    )
