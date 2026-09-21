"""计划估算（Slice 20，Spec §7 / §8 / §12 / §20 / §21）。

守四件事：

1. **汇总就是逐项相加**（Spec §7）：总量不能自己另算一套，否则界面上的
   "3 项之和"与计划总量迟早对不上。
2. **多边形看"选中"张数**（Spec §7）：用三角形夹具把 ``selected`` 与
   ``candidate`` 拉开 —— 合成目录里其余多边形都是轴对齐矩形，两者相等，
   一个"用候选数"的错实现能在那些数据上全绿。
3. **一个坏条目不该拖垮整个计划**（Spec §18 的同一条原则）：区域被删 /
   边界文件缺失只把那一项标成不可用，其余照算。
4. **取消立刻生效且不动配置**（Spec §21）：检查点在条目之间，取消后
   计划里的 Zoom / 条目一个字都没变。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config.settings import DEFAULT_AVERAGE_TILE_SIZE
from app.geo.region_estimator import RegionTileEstimator
from app.planning.download_plan import (
    DownloadPlan,
    DownloadPlanItem,
    PlanItemStatus,
)
from app.planning.plan_builder import DownloadPlanBuilder
from app.planning.plan_estimator import (
    DownloadPlanEstimator,
    PlanEstimateCancelled,
    PlanEstimateProgress,
)
from app.planning.region_level import RegionLevel
from app.regions.service import RegionService


@pytest.fixture
def service(region_resources: Path) -> RegionService:
    """挂在合成目录（含三角形区域）上的区域服务。"""
    return RegionService()


@pytest.fixture
def estimator(service: RegionService) -> DownloadPlanEstimator:
    return DownloadPlanEstimator(service)


@pytest.fixture
def builder(service: RegionService) -> DownloadPlanBuilder:
    return DownloadPlanBuilder(service)


# --------------------------------------------------------------------------- #
# 逐项与汇总（Spec §7 / §8）
# --------------------------------------------------------------------------- #
def test_estimate_single_item_writes_back(
    estimator: DownloadPlanEstimator, builder: DownloadPlanBuilder
) -> None:
    """单项估算把结果**写回条目**，并让计划总量等于它自己。"""
    builder.add_region("demoland")
    plan = builder.plan
    item = plan.item("demoland")
    assert item.is_estimated is False
    assert plan.needs_estimate() is True

    estimator.estimate(plan)

    assert item.is_estimated is True
    assert item.status is PlanItemStatus.VALID
    assert item.invalid_reason == ""
    assert item.candidate_tiles > 0
    assert item.selected_tiles > 0
    assert item.selected_tiles <= item.candidate_tiles
    assert item.estimated_bytes == item.selected_tiles * DEFAULT_AVERAGE_TILE_SIZE
    assert plan.total_tiles == item.selected_tiles
    assert plan.estimated_bytes == item.estimated_bytes
    assert plan.needs_estimate() is False


def test_multi_item_totals_are_the_sum_of_the_items(
    estimator: DownloadPlanEstimator, builder: DownloadPlanBuilder
) -> None:
    """总量 = 各项之和（不另算一套）。"""
    for region_id in ("demoland", "northland", "rivertown"):
        builder.add_region(region_id)
    plan = builder.plan

    estimator.estimate(plan)

    items = list(plan.ordered_items())
    assert len(items) == 3
    assert all(item.is_estimated for item in items)
    assert plan.total_tiles == sum(item.selected_tiles for item in items)
    assert plan.estimated_bytes == sum(item.estimated_bytes for item in items)
    assert all(item.selected_tiles > 0 for item in items)
    # 合成目录里的多边形都是轴对齐矩形 → 裁不掉任何瓦片。
    # 这条断言同时把夹具的**形状**钉住：哪天真有一条被裁过的数据混进来，
    # 它会先红，而不是让"多边形按选中张数汇总"那条用例悄悄失去区分度。
    assert all(item.filtered_tiles == 0 for item in items)


def test_per_zoom_detail_is_kept_and_covers_the_range(
    estimator: DownloadPlanEstimator, builder: DownloadPlanBuilder
) -> None:
    """逐级明细要保留（Spec §8），且覆盖计划里那段 Zoom。"""
    builder.add_region("rivertown")
    plan = builder.plan
    item = plan.item("rivertown")

    estimator.estimate(plan)

    zooms = [row.zoom for row in item.per_zoom]
    assert zooms == list(range(item.min_zoom, item.max_zoom + 1))
    assert sum(row.selected_tiles for row in item.per_zoom) == item.selected_tiles
    # 明细里没有瓦片坐标这种重东西（Spec §8：不放瓦片列表）
    assert all(not hasattr(row, "coordinates") for row in item.per_zoom)


def test_polygon_item_counts_selected_tiles_not_candidates(
    estimator: DownloadPlanEstimator, service: RegionService, builder: DownloadPlanBuilder
) -> None:
    """Spec §7 的硬要求：多边形按**选中**张数汇总。

    三角形夹具让 ``selected < candidate`` 明显成立 —— 如果实现拿了候选数，
    下面第一条断言就会红。
    """
    builder.add_region("triangleland")
    plan = builder.plan
    item = plan.item("triangleland")

    estimator.estimate(plan)

    assert item.candidate_tiles > item.selected_tiles > 0, (
        "夹具失效：三角形区域本应让候选数与选中数明显分离"
    )
    assert item.filtered_tiles == item.candidate_tiles - item.selected_tiles
    assert plan.total_tiles == item.selected_tiles
    assert plan.total_tiles != item.candidate_tiles

    # 与生产同一个函数算出来的值对齐（不是写死的推导值）
    expected = RegionTileEstimator(
        service.get_download_region("triangleland"),
        average_tile_size=DEFAULT_AVERAGE_TILE_SIZE,
    ).estimate(item.min_zoom, item.max_zoom)
    assert item.selected_tiles == expected.selected_tiles
    assert item.estimated_bytes == expected.estimated_size_bytes


def test_average_tile_size_drives_the_capacity(
    service: RegionService, builder: DownloadPlanBuilder
) -> None:
    """容量按注入的平均瓦片体积算（不写死 30KB 一档）。"""
    builder.add_region("demoland")
    plan = builder.plan

    DownloadPlanEstimator(service, average_tile_size=1000).estimate(plan)
    small = plan.item("demoland").estimated_bytes
    DownloadPlanEstimator(service, average_tile_size=4000).estimate(plan)
    big = plan.item("demoland").estimated_bytes

    assert small > 0
    assert big == small * 4


def test_names_and_levels_follow_the_catalog(
    estimator: DownloadPlanEstimator, builder: DownloadPlanBuilder
) -> None:
    """条目里的名字 / 等级是缓存，估算时以目录为准（否则界面两处各说各话）。"""
    builder.add_region("rivertown")
    plan = builder.plan
    item = plan.item("rivertown")
    item.region_name = "过期的旧名字"
    item.region_level = RegionLevel.COUNTRY

    estimator.estimate(plan)

    assert item.region_name == "示例河市"
    assert item.region_level is RegionLevel.CITY


# --------------------------------------------------------------------------- #
# 禁用项（Spec §12）
# --------------------------------------------------------------------------- #
def test_disabled_item_is_still_estimated_but_excluded_from_totals(
    estimator: DownloadPlanEstimator, builder: DownloadPlanBuilder
) -> None:
    """禁用项**照算**（那一行仍要显示自己的容量），只是不入总量。"""
    builder.add_region("demoland")
    builder.add_region("northland")
    builder.set_enabled("northland", False)
    plan = builder.plan

    estimator.estimate(plan)

    disabled = plan.item("northland")
    enabled = plan.item("demoland")
    assert disabled.is_estimated is True, "禁用项也要算出自己的数字，否则用户无法决定要不要打开"
    assert disabled.selected_tiles > 0
    assert disabled.counts_toward_total is False
    assert plan.total_tiles == enabled.selected_tiles
    assert plan.estimated_bytes == enabled.estimated_bytes


def test_turning_an_item_back_on_only_recalculates_totals(
    estimator: DownloadPlanEstimator, builder: DownloadPlanBuilder
) -> None:
    """重新启用不需要重算几何：数字还在，总量跟着变。"""
    builder.add_region("demoland")
    builder.add_region("northland")
    plan = builder.plan
    estimator.estimate(plan)
    both = plan.total_tiles

    builder.set_enabled("northland", False)
    plan.recalculate_totals()
    assert plan.total_tiles < both

    builder.set_enabled("northland", True)
    plan.recalculate_totals()
    assert plan.total_tiles == both


# --------------------------------------------------------------------------- #
# 坏条目（Spec §18 的同一条原则）
# --------------------------------------------------------------------------- #
def test_unknown_region_marks_only_that_item_invalid(
    estimator: DownloadPlanEstimator, builder: DownloadPlanBuilder
) -> None:
    """区域不在目录里 → 这一项不可用，**不抛异常**，其余照算。"""
    builder.add_region("demoland")
    plan = builder.plan
    plan.add_item(DownloadPlanItem(region_id="nowhere", region_name="不存在的地区"))

    estimator.estimate(plan)

    missing = plan.item("nowhere")
    good = plan.item("demoland")
    assert missing.status is PlanItemStatus.INVALID
    assert missing.invalid_reason
    assert missing.selected_tiles == 0
    assert missing.counts_toward_total is False
    assert good.is_estimated is True
    assert plan.total_tiles == good.selected_tiles
    # 重试一次是它恢复的唯一路径 → 必须仍在"待估算"名单里
    assert plan.needs_estimate() is True


def test_missing_boundary_file_marks_the_item_invalid(
    estimator: DownloadPlanEstimator,
    builder: DownloadPlanBuilder,
    region_resources: Path,
) -> None:
    """边界文件被删 → 这一项不可用；其余条目不受影响。"""
    builder.add_region("demoland")
    builder.add_region("northland")
    (region_resources / "province" / "northland.geojson").unlink()
    plan = builder.plan

    estimator.estimate(plan)

    broken = plan.item("northland")
    assert broken.status is PlanItemStatus.INVALID
    assert broken.invalid_reason
    assert plan.total_tiles == plan.item("demoland").selected_tiles


def test_invalid_item_keeps_no_stale_numbers(
    estimator: DownloadPlanEstimator, builder: DownloadPlanBuilder
) -> None:
    """先算成功、再让边界文件消失 → 旧数字必须被清掉（不能留个陈旧的张数）。"""
    builder.add_region("northland")
    plan = builder.plan
    estimator.estimate(plan)
    assert plan.item("northland").selected_tiles > 0

    plan.item("northland").mark_invalid("模拟：别人改坏了目录")
    plan.recalculate_totals()

    assert plan.item("northland").selected_tiles == 0
    assert plan.item("northland").tiles_text == "—"
    assert plan.total_tiles == 0


# --------------------------------------------------------------------------- #
# 进度与取消（Spec §20 / §21）
# --------------------------------------------------------------------------- #
def test_progress_reports_every_item(
    estimator: DownloadPlanEstimator, builder: DownloadPlanBuilder
) -> None:
    """每算完一项回调一次，最后一条的百分比是 100。"""
    for region_id in ("demoland", "northland", "rivertown"):
        builder.add_region(region_id)
    plan = builder.plan
    seen: list[PlanEstimateProgress] = []

    estimator.estimate(plan, progress=seen.append)

    assert [row.done for row in seen] == [1, 2, 3]
    assert all(row.total == 3 for row in seen)
    assert seen[-1].percent == 100
    assert seen[-1].describe() == "估算完成（3 项）"
    assert seen[0].describe().startswith("正在估算 1/3：")
    assert all(row.failed is False for row in seen)
    assert seen[0].selected_tiles > 0


def test_progress_marks_a_failed_item(
    estimator: DownloadPlanEstimator, builder: DownloadPlanBuilder
) -> None:
    """算不出来的那一项在进度里要标出来（界面据此说"某项不可用"）。"""
    builder.add_region("demoland")
    plan = builder.plan
    plan.add_item(DownloadPlanItem(region_id="nowhere", region_name="不存在的地区"))
    seen: list[PlanEstimateProgress] = []

    estimator.estimate(plan, progress=seen.append)

    assert [row.failed for row in seen] == [False, True]
    assert seen[-1].selected_tiles == 0


def test_cancelling_stops_before_the_next_item(
    estimator: DownloadPlanEstimator, builder: DownloadPlanBuilder
) -> None:
    """取消：已完成的一项留着，后面的保持"未估算"，且**抛的是取消不是失败**。"""
    for region_id in ("demoland", "northland", "rivertown"):
        builder.add_region(region_id)
    plan = builder.plan
    seen: list[PlanEstimateProgress] = []
    calls = {"n": 0}

    def should_cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # 第一项放行，第二项之前喊停

    with pytest.raises(PlanEstimateCancelled):
        estimator.estimate(plan, should_cancel=should_cancel, progress=seen.append)

    assert len(seen) == 1
    assert plan.item("demoland").is_estimated is True
    assert plan.item("northland").is_estimated is False
    assert plan.item("rivertown").is_estimated is False
    assert plan.needs_estimate() is True


def test_cancelling_leaves_the_plan_configuration_untouched(
    estimator: DownloadPlanEstimator, builder: DownloadPlanBuilder
) -> None:
    """Spec §21：取消估算**不是**取消计划 —— 条目、Zoom、启用状态一个字都不动。"""
    builder.add_region("demoland")
    builder.add_region("northland", use_recommended_zoom=False, min_zoom=9, max_zoom=12)
    builder.set_enabled("northland", False)
    plan = builder.plan
    before = [
        (item.id, item.region_id, item.min_zoom, item.max_zoom, item.enabled)
        for item in plan.items
    ]

    with pytest.raises(PlanEstimateCancelled):
        estimator.estimate(plan, should_cancel=lambda: True)

    after = [
        (item.id, item.region_id, item.min_zoom, item.max_zoom, item.enabled)
        for item in plan.items
    ]
    assert after == before
    assert plan.count == 2


def test_cancelling_on_an_empty_plan_completes_without_raising(
    estimator: DownloadPlanEstimator,
) -> None:
    """空计划：没有条目就没有检查点，因此**正常算完**（不报"已取消"）。

    这不是漏了取消 —— Spec §21 的检查点在**条目之间**，而这里一个条目都没有：
    没有"剩下的活"可取消，报"已取消"只会让界面显示一句没有信息量的话。
    """
    plan = DownloadPlan(name="空计划")

    result = estimator.estimate(plan, should_cancel=lambda: True)

    assert result is plan
    assert plan.total_tiles == 0
    assert plan.needs_estimate() is False


def test_cancellation_is_checked_before_every_item(
    estimator: DownloadPlanEstimator, builder: DownloadPlanBuilder
) -> None:
    """``should_cancel`` 每次迭代都要问一次（不是只在开头问）。"""
    for region_id in ("demoland", "northland", "rivertown"):
        builder.add_region(region_id)
    plan = builder.plan
    calls = {"n": 0}

    def never() -> bool:
        calls["n"] += 1
        return False

    estimator.estimate(plan, should_cancel=never)

    assert calls["n"] == 3


def test_progress_percent_handles_the_empty_case() -> None:
    """进度对象的边界：没有条目时是 100%，不会除零。"""
    assert PlanEstimateProgress(done=0, total=0).percent == 100
    assert PlanEstimateProgress(done=1, total=4).percent == 25
