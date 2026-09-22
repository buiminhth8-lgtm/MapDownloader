"""计划级风险分析（Slice 23，Spec §15 / §16 / §17 / §22）。

守四件事：

1. **总风险取两个来源里更高的那个**：合计张数 / 容量的档位，与逐项最高档。
   只做前者会漏掉"总量不大但有一项是 TB 级"，只做后者会漏掉"每项都不大、
   加起来装不下"—— 两条各有一个用例。
2. **禁用 / 不可用的条目不参与风险**（与 Slice 20 的合计口径逐字一致）。
3. **计划风险分析不重算瓦片**：真实估算一次之后把估算器打成"一调用就炸"，
   风险分析必须照常出结论（Spec §21 / §26）。
4. **确认等级跟着计划总风险走**（Spec §12 / §16）。

数值夹具直接构造 :class:`DownloadPlanItem`（张数 / 容量都是它的字段），
因为这里要守的是**汇总与判定规则**，不是估算链路本身；估算链路由
``test_plan_estimator.py`` 与最后那条集成用例负责。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.geo.region_estimator import RegionTileEstimator
from app.geo.tile_math import estimate_size_bytes, format_size, format_tile_count
from app.planning.download_plan import DownloadPlan, DownloadPlanItem
from app.planning.download_risk_analyzer import DownloadRiskAnalyzer, _plan_reasons
from app.planning.plan_builder import DownloadPlanBuilder
from app.planning.plan_estimator import DownloadPlanEstimator
from app.planning.region_level import RegionLevel
from app.planning.risk_level import RiskLevel
from app.planning.risk_models import PlanItemRisk
from app.planning.risk_policy import (
    DANGEROUS_TILE_THRESHOLD,
    LARGE_TILE_THRESHOLD,
    VERY_LARGE_TILE_THRESHOLD,
    RiskPolicy,
)
from app.planning.zoom_strategy import ZoomStrategyService
from app.regions.service import RegionService


@pytest.fixture
def service(region_resources: Path) -> RegionService:
    """挂在合成目录上的区域服务（与 ``test_plan_estimator`` 同一套）。"""
    return RegionService()


@pytest.fixture
def analyzer() -> DownloadRiskAnalyzer:
    return DownloadRiskAnalyzer(strategy=ZoomStrategyService())


def _item(
    region_id: str,
    *,
    level: RegionLevel,
    tiles: int,
    min_zoom: int,
    max_zoom: int,
    name: str = "",
    enabled: bool = True,
    bytes_override: int | None = None,
    per_zoom: tuple | None = None,
) -> DownloadPlanItem:
    """造一个"已经估算过"的条目（张数 / 容量 / 逐级明细都在它身上）。"""
    if per_zoom is None:
        # 张数全算在 max_zoom 那一级：足够表达"超推荐"那条规则，
        # 也不需要在夹具里铺开十几个 zoom。
        from app.geo.region_estimator import ZoomRegionStats

        per_zoom = (
            ZoomRegionStats(
                zoom=max_zoom,
                candidate_tiles=tiles,
                selected_tiles=tiles,
                filtered_tiles=0,
            ),
        )
    item = DownloadPlanItem(
        region_id=region_id,
        region_name=name or region_id,
        region_level=level,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        selected_tiles=tiles,
        estimated_bytes=(
            estimate_size_bytes(tiles) if bytes_override is None else bytes_override
        ),
        candidate_tiles=tiles,
        per_zoom=per_zoom,
        enabled=enabled,
    )
    item.mark_valid()
    return item


def _plan(*items: DownloadPlanItem) -> DownloadPlan:
    plan = DownloadPlan(name="offline_plan")
    for item in items:
        plan.add_item(item)
    return plan.recalculate_totals()


# --------------------------------------------------------------------------- #
# 单项 → 计划
# --------------------------------------------------------------------------- #
def test_a_single_item_plan_has_the_items_level(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    plan = _plan(_item("rivertown", level=RegionLevel.CITY, tiles=500, min_zoom=12,
                       max_zoom=16))
    report = analyzer.analyze_plan(plan)
    assert report.level is RiskLevel.NORMAL
    assert report.total_tiles == 500
    assert len(report.items) == 1
    assert report.items[0].region_id == "rivertown"
    assert report.requires_confirmation is False


def test_the_plan_totals_are_the_sum_of_its_items(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """合计 = 逐项相加（不许自己另算一套）。"""
    plan = _plan(
        _item("demoland", level=RegionLevel.COUNTRY, tiles=30_000, min_zoom=4,
              max_zoom=10),
        _item("northland", level=RegionLevel.PROVINCE, tiles=20_000, min_zoom=9,
              max_zoom=13),
        _item("rivertown", level=RegionLevel.CITY, tiles=10_000, min_zoom=12,
              max_zoom=16),
    )
    report = analyzer.analyze_plan(plan)
    assert report.total_tiles == 60_000
    assert report.estimated_bytes == sum(item.report.estimated_bytes for item in report.items)
    assert report.tiles_text == format_tile_count(60_000)
    assert report.size_text == format_size(report.estimated_bytes)


# --------------------------------------------------------------------------- #
# 总容量把风险抬起来（Spec §15）
# --------------------------------------------------------------------------- #
def test_the_total_can_raise_the_level_above_every_item(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """每项都正常，加起来超档 → 计划升级。

    单项取 ``LARGE_TILE_THRESHOLD // 2``（约 5 万张 / 1.4 GiB）：**两个维度都**
    在 LARGE 阈值以下。千万别随手写 9 万 —— 那是"张数没超但容量超了"
    （9 万 × 30 KiB ≈ 2.57 GiB > 2 GiB），条目自己就已经是 LARGE，
    这条用例就变成"测了个寂寞"。
    """
    per_item = LARGE_TILE_THRESHOLD // 2
    items = [
        _item(f"city-{index}", level=RegionLevel.CITY, tiles=per_item, min_zoom=12,
              max_zoom=16)
        for index in range(4)
    ]
    plan = _plan(*items)
    report = analyzer.analyze_plan(plan)

    assert all(item.level is RiskLevel.NORMAL for item in report.items)
    assert report.total_tiles == per_item * 4
    assert report.level is RiskLevel.LARGE
    assert report.requires_confirmation is True
    assert report.requires_strong_confirmation is False
    # 合计口径与计划自己算的那份逐字一致（不是两套真值）
    assert report.total_tiles == plan.total_tiles


def test_one_dangerous_item_makes_the_whole_plan_dangerous(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """有一条 TB 级的项 → 计划危险，并且**点名**是它（Spec §22）。

    注意：默认阈值下"单项抬级"其实证明不了什么 —— 单项有 1000 万张，合计
    必然 ≥ 1000 万。所以这里守的是**结论 + 可解释性**：等级要跟着最坏的那项
    走，原因里要有"谁"。"合计没超但单项超"的文案由下面那条直接调纯函数守。
    """
    plan = _plan(
        _item("rivertown", level=RegionLevel.CITY, tiles=500, min_zoom=12, max_zoom=16),
        _item("demoland", level=RegionLevel.COUNTRY, tiles=DANGEROUS_TILE_THRESHOLD,
              min_zoom=4, max_zoom=18, name="示例国"),
    )
    report = analyzer.analyze_plan(plan)

    assert report.level is RiskLevel.DANGEROUS
    assert report.requires_strong_confirmation is True
    assert report.has_dangerous_item is True
    assert [entry.region_id for entry in report.dangerous_items] == ["demoland"]
    # 一个 500 张的城市被 17.9 B 张的全国拖下水时，用户得看出是谁干的
    assert any("示例国" in line for line in report.reasons)
    assert "示例国" in "\n".join(report.dangerous_names())


def test_the_reasons_name_the_item_that_raised_the_level() -> None:
    """单项抬级时要说清"合计不大、是某一项大"（否则用户以为等级算错）。

    这条直接调纯函数 :func:`_plan_reasons` 并**手工**给出两个来源不同的档位：
    默认阈值是单调的（合计张数 / 容量不小于任何单项），走真实计划永远碰不到
    这个组合 —— 那不代表这段文案可以不存在。将来换上"只看单项"的保守策略时，
    它就会出现在界面上，因此现在就得钉住它的措辞。
    """
    entry = PlanItemRisk(
        region_id="demoland",
        region_name="示例国",
        zoom_text="Z4~18",
        report=DownloadRiskAnalyzer().analyze_item(
            _item("demoland", level=RegionLevel.COUNTRY,
                  tiles=DANGEROUS_TILE_THRESHOLD, min_zoom=4, max_zoom=18, name="示例国")
        ),
    )
    reasons = _plan_reasons(
        [entry],
        total_tiles=500,
        total_bytes=entry.report.estimated_bytes,
        totals_level=RiskLevel.NORMAL,
        max_item_level=entry.level,
        dangerous=(),
    )
    assert any("单项" in line and "危险" in line for line in reasons), reasons


def test_dangerous_items_come_first(analyzer: DownloadRiskAnalyzer) -> None:
    """界面与建议都按"先看最要紧的"排序。"""
    plan = _plan(
        _item("rivertown", level=RegionLevel.CITY, tiles=500, min_zoom=12, max_zoom=16),
        _item("demoland", level=RegionLevel.COUNTRY, tiles=DANGEROUS_TILE_THRESHOLD,
              min_zoom=4, max_zoom=18),
        _item("northland", level=RegionLevel.PROVINCE, tiles=VERY_LARGE_TILE_THRESHOLD,
              min_zoom=9, max_zoom=13),
    )
    report = analyzer.analyze_plan(plan)
    assert [entry.region_id for entry in report.items] == [
        "demoland",
        "northland",
        "rivertown",
    ]


# --------------------------------------------------------------------------- #
# 禁用 / 不可用不参与（Spec §22）
# --------------------------------------------------------------------------- #
def test_a_disabled_item_does_not_count_toward_the_plan_risk(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """禁用项不会被下载 —— 对着它警告等于让用户改一个不会发生的事。"""
    plan = _plan(
        _item("rivertown", level=RegionLevel.CITY, tiles=500, min_zoom=12, max_zoom=16),
        _item("demoland", level=RegionLevel.COUNTRY, tiles=DANGEROUS_TILE_THRESHOLD,
              min_zoom=4, max_zoom=18, enabled=False),
    )
    report = analyzer.analyze_plan(plan)

    assert report.level is RiskLevel.NORMAL
    assert report.total_tiles == 500
    assert [entry.region_id for entry in report.items] == ["rivertown"]
    assert report.level_of("demoland") is None
    # 反向：如果"顺手"把禁用项也算进来，等级会是危险
    assert DEFAULT_LEVEL_OF_DANGEROUS_ITEM() is RiskLevel.DANGEROUS


def DEFAULT_LEVEL_OF_DANGEROUS_ITEM() -> RiskLevel:
    """对照值：那一项**自己**确实是危险（证明上一条不是因为夹具太小）。"""
    return DownloadRiskAnalyzer().analyze_item(
        _item("demoland", level=RegionLevel.COUNTRY,
              tiles=DANGEROUS_TILE_THRESHOLD, min_zoom=4, max_zoom=18)
    ).level


def test_an_invalid_item_does_not_count_either(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    plan = _plan(
        _item("rivertown", level=RegionLevel.CITY, tiles=500, min_zoom=12, max_zoom=16),
    )
    broken = _item("demoland", level=RegionLevel.COUNTRY,
                   tiles=DANGEROUS_TILE_THRESHOLD, min_zoom=4, max_zoom=18)
    broken.mark_invalid("边界文件缺失")
    plan.add_item(broken)
    plan.recalculate_totals()

    report = analyzer.analyze_plan(plan)
    assert report.level is RiskLevel.NORMAL
    assert [entry.region_id for entry in report.items] == ["rivertown"]


def test_an_empty_plan_is_normal_and_says_so(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    report = analyzer.analyze_plan(DownloadPlan(name="offline_plan"))
    assert report.level is RiskLevel.NORMAL
    assert report.items == ()
    assert report.requires_confirmation is False
    assert any("还没有" in line for line in report.reasons)


# --------------------------------------------------------------------------- #
# 建议（Spec §16）
# --------------------------------------------------------------------------- #
def test_recommendations_group_items_of_the_same_level(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """十个省不该写十行一模一样的建议（对话框会变成一堵墙）。"""
    plan = _plan(
        _item("northland", level=RegionLevel.PROVINCE, tiles=900_000, min_zoom=9,
              max_zoom=18, name="示例北省"),
        _item("southland", level=RegionLevel.PROVINCE, tiles=900_000, min_zoom=9,
              max_zoom=18, name="示例南省"),
    )
    report = analyzer.analyze_plan(plan)
    lines = [line for line in report.recommendations if "示例北省" in line]
    assert lines, report.recommendations
    assert any("示例南省" in line for line in report.recommendations)
    # 同一档的两项合并成一行
    assert len([line for line in report.recommendations if "省" in line]) == 1
    # 建议里要带出该等级的推荐区间（来自策略层，不是抄来的）
    assert "Z9~13" in lines[0]


def test_a_normal_plan_has_no_recommendations(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    plan = _plan(_item("rivertown", level=RegionLevel.CITY, tiles=500, min_zoom=12,
                       max_zoom=16))
    assert analyzer.analyze_plan(plan).recommendations == ()


# --------------------------------------------------------------------------- #
# 不重算瓦片（Spec §21 / §26）
# --------------------------------------------------------------------------- #
def test_plan_risk_analysis_never_re_estimates(
    analyzer: DownloadRiskAnalyzer,
    service: RegionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真估算一次 → 把估算器打成"一调用就炸" → 风险分析必须照常出结论。

    这条比"数一数调用了几次"更强：它证明**整条重算路径都不可达**，
    而不是"这次恰好没走到"。
    """
    builder = DownloadPlanBuilder(service)
    builder.add_region("rivertown", use_recommended_zoom=True)
    plan = builder.plan
    DownloadPlanEstimator(service).estimate(plan)

    def boom(*_args, **_kwargs):  # pragma: no cover - 只会在实现退化时执行
        raise AssertionError("风险分析重算了瓦片")

    monkeypatch.setattr(RegionTileEstimator, "estimate", boom)
    report = analyzer.analyze_plan(plan)

    assert report.total_tiles == plan.total_tiles
    assert report.items[0].report.total_tiles == plan.total_tiles
    assert report.level is not None


def test_plan_risk_analysis_does_not_modify_the_plan(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """分析是只读的：条目里的张数、Zoom、启用状态一个都不许变。"""
    plan = _plan(
        _item("rivertown", level=RegionLevel.CITY, tiles=500, min_zoom=12, max_zoom=16),
        _item("demoland", level=RegionLevel.COUNTRY, tiles=20_000, min_zoom=4,
              max_zoom=10, enabled=False),
    )
    before = [
        (item.region_id, item.min_zoom, item.max_zoom, item.selected_tiles, item.enabled)
        for item in plan.items
    ]
    analyzer.analyze_plan(plan)
    after = [
        (item.region_id, item.min_zoom, item.max_zoom, item.selected_tiles, item.enabled)
        for item in plan.items
    ]
    assert after == before
    # 计划自己的合计口径也是"启用且可用"（与风险分析同一份），所以这里能对等
    assert plan.total_tiles == 500


def test_analyze_plan_rejects_a_non_plan(analyzer: DownloadRiskAnalyzer) -> None:
    with pytest.raises(ValueError):
        analyzer.analyze_plan({"name": "offline_plan"})  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 与真实估算器串起来（集成）
# --------------------------------------------------------------------------- #
def test_the_report_matches_a_real_estimator_run(
    analyzer: DownloadRiskAnalyzer,
    service: RegionService,
    builder: DownloadPlanBuilder | None = None,
) -> None:
    """真实链路：目录 → 估算器 → 计划条目 → 风险报告。

    断言全部对着**生产同一套函数**算出来的数字（不写字面量）：
    报告里的张数就是条目里的张数，容量就是张数 × 平均体积。
    """
    plan_builder = DownloadPlanBuilder(service)
    plan_builder.add_region("rivertown", use_recommended_zoom=True)
    plan = plan_builder.plan
    DownloadPlanEstimator(service).estimate(plan)

    item = plan.item("rivertown")
    assert item is not None and item.selected_tiles > 0
    report = analyzer.analyze_plan(plan)

    assert report.total_tiles == item.selected_tiles
    assert report.estimated_bytes == item.estimated_bytes
    assert report.items[0].zoom_text == item.zoom_text
    # 城市级推荐 Z12~16，条目就是按推荐区间加的 → 不该有"超推荐"
    assert report.items[0].report.recommended_range_text == "Z12~16"
    assert report.items[0].report.over_recommended_tiles == 0
