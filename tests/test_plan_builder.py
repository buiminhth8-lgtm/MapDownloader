"""计划编辑器（Slice 20，Spec §3 / §4 / §5 / §6）。

守三件事：

1. **Zoom 只有一个来源**：推荐值一律来自
   :class:`~app.planning.ZoomStrategyService`。因此这里有一条**行为**守卫 ——
   把策略表换掉，编辑器给出的区间必须跟着变（而不是"恰好也等于 4~10"）。
   这比在源码里搜 ``Z4`` 强：字符串匹配既抓不到算出来的数字，
   又会被注释里的 ``Z4~10`` 误伤（Slice 14 的教训）。
2. **防重复**（Spec §5）：同一个区域再加一次是"更新"，不是新增一行。
3. **不偷偷改用户的值**（Spec §6）：自定义 Zoom 原样保留，超范围由校验器提醒。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.planning.download_plan import DownloadPlan, DownloadPlanItem, PlanItemStatus
from app.planning.plan_builder import DownloadPlanBuilder, PlanAddResult
from app.planning.region_level import RegionLevel
from app.planning.zoom_policy import DEFAULT_ZOOM_POLICIES
from app.planning.zoom_strategy import ZoomStrategyService
from app.regions.models import RegionCatalogError
from app.regions.service import RegionService


@pytest.fixture
def service(region_resources: Path) -> RegionService:
    """挂在合成目录上的区域服务（``region_resources`` 改的是资源根全局）。"""
    return RegionService()


@pytest.fixture
def builder(service: RegionService) -> DownloadPlanBuilder:
    return DownloadPlanBuilder(service)


# --------------------------------------------------------------------------- #
# 加区域（Spec §3 / §4）
# --------------------------------------------------------------------------- #
def test_add_country_uses_the_strategy_recommendation(
    builder: DownloadPlanBuilder, service: RegionService
) -> None:
    """推荐区间必须**等于**策略层算出来的值（而不是恰好相同的常量）。"""
    result = builder.add_region("demoland")
    item = result.item
    expected = service.strategy.recommend(RegionLevel.COUNTRY)

    assert result.created is True
    assert item.region_id == "demoland"
    assert item.region_name == "示例国"
    assert item.region_level is RegionLevel.COUNTRY
    assert (item.min_zoom, item.max_zoom) == (expected.min_zoom, expected.max_zoom)
    assert item.enabled is True
    assert item.status is PlanItemStatus.VALID
    assert item.is_estimated is False  # 加入计划不等于算过了


def test_add_province_and_city(builder: DownloadPlanBuilder) -> None:
    builder.add_region("northland")
    builder.add_region("rivertown")
    plan = builder.plan
    assert [item.region_level for item in plan.ordered_items()] == [
        RegionLevel.PROVINCE,
        RegionLevel.CITY,
    ]
    assert plan.item("northland").region_name == "示例北省"
    assert plan.item("rivertown").region_name == "示例河市"


def test_add_country_province_city_forms_three_levels(
    builder: DownloadPlanBuilder,
) -> None:
    """Spec §4 的完整三级（不强制三级，但三级要能建出来）。"""
    for region_id in ("demoland", "northland", "rivertown"):
        assert builder.add_region(region_id).created is True
    plan = builder.plan
    assert [item.region_id for item in plan.ordered_items()] == [
        "demoland",
        "northland",
        "rivertown",
    ]
    assert plan.coverage_levels() == (
        RegionLevel.COUNTRY,
        RegionLevel.PROVINCE,
        RegionLevel.CITY,
    )


def test_add_only_province_or_only_city_is_allowed(builder: DownloadPlanBuilder) -> None:
    """Spec §4：不要强制必须完整三级。"""
    builder.add_region("rivertown")
    assert builder.plan.count == 1
    assert builder.plan.item("rivertown").region_level is RegionLevel.CITY


def test_add_unknown_region_raises(builder: DownloadPlanBuilder) -> None:
    with pytest.raises(RegionCatalogError):
        builder.add_region("没有这块地")
    assert builder.plan.is_empty


def test_result_message_says_the_same_thing_for_new_and_updated(
    builder: DownloadPlanBuilder,
) -> None:
    first = builder.add_region("demoland")
    second = builder.add_region("demoland")
    assert first.created is True and second.created is False
    assert "已加入下载计划" in first.message
    assert "已更新下载计划中的" in second.message
    assert first.item is second.item  # 同一行，不是新建的一行


# --------------------------------------------------------------------------- #
# 防重复（Spec §5）
# --------------------------------------------------------------------------- #
def test_re_adding_updates_instead_of_duplicating(builder: DownloadPlanBuilder) -> None:
    builder.add_region("northland", use_recommended_zoom=False, min_zoom=8, max_zoom=12)
    builder.add_region("northland", use_recommended_zoom=False, min_zoom=10, max_zoom=14)
    assert builder.plan.count == 1
    item = builder.plan.item("northland")
    assert (item.min_zoom, item.max_zoom) == (10, 14)


def test_re_adding_with_a_changed_zoom_drops_the_stale_estimate(
    builder: DownloadPlanBuilder,
) -> None:
    builder.add_region("northland")
    item = builder.plan.item("northland")
    item.candidate_tiles = 1000
    item.selected_tiles = 900
    item.estimated_bytes = 12345

    builder.add_region("northland", use_recommended_zoom=False, min_zoom=9, max_zoom=11)
    assert item.candidate_tiles == 0 and item.selected_tiles == 0
    assert item.estimated_bytes == 0 and item.tiles_text == "—"


def test_re_adding_with_the_same_zoom_keeps_the_estimate(
    builder: DownloadPlanBuilder,
) -> None:
    """点重复一次"加入"不该让界面上那行数字突然变成 ``—``。"""
    builder.add_region("northland")
    item = builder.plan.item("northland")
    item.candidate_tiles = 1000
    item.selected_tiles = 900
    item.estimated_bytes = 12345

    builder.add_region("northland")
    assert item.selected_tiles == 900
    assert item.estimated_bytes == 12345


def test_re_adding_re_enables_a_disabled_item(builder: DownloadPlanBuilder) -> None:
    """用户点"加入"就是在说"我要它"。"""
    builder.add_region("northland")
    builder.set_enabled("northland", False)
    assert builder.plan.enabled_count == 0
    builder.add_region("northland")
    assert builder.plan.item("northland").enabled is True


def test_re_adding_restores_a_region_that_came_back(builder: DownloadPlanBuilder) -> None:
    """区域被删掉又放回来：再点一次"加入"就该重新可用。"""
    builder.add_region("northland")
    item = builder.plan.item("northland")
    item.mark_invalid("区域没了")
    builder.add_region("northland")
    assert item.status is PlanItemStatus.VALID
    assert item.invalid_reason == ""


# --------------------------------------------------------------------------- #
# Zoom（Spec §3 / §6）
# --------------------------------------------------------------------------- #
def test_recommended_zoom_follows_the_injected_policy_table(
    service: RegionService,
) -> None:
    """**行为**守卫：把策略表换掉，编辑器给出的区间必须跟着变。

    Spec §3 最后一句是"禁止再次硬编码 Zoom 数值"。源码里搜 ``4`` / ``10``
    抓不到"算出来的错值"，而这条用例抓得到：只要有人在
    :meth:`~app.planning.plan_builder.DownloadPlanBuilder.add_region` 里写死了
    一个区间，它就会红。
    """
    from dataclasses import replace

    country = DEFAULT_ZOOM_POLICIES[RegionLevel.COUNTRY]
    table = dict(DEFAULT_ZOOM_POLICIES)
    table[RegionLevel.COUNTRY] = replace(
        country, recommended_min_zoom=5, recommended_max_zoom=9, warning_max_zoom=9
    )
    builder = DownloadPlanBuilder(
        service, strategy=ZoomStrategyService(table)
    )
    item = builder.add_region("demoland").item
    assert (item.min_zoom, item.max_zoom) == (5, 9)


def test_recommended_zoom_for_each_level_comes_from_the_strategy(
    builder: DownloadPlanBuilder, service: RegionService
) -> None:
    for region_id, level in (
        ("demoland", RegionLevel.COUNTRY),
        ("northland", RegionLevel.PROVINCE),
        ("rivertown", RegionLevel.CITY),
    ):
        builder.add_region(region_id)
        expected = service.strategy.recommend(level)
        item = builder.plan.item(region_id)
        assert (item.min_zoom, item.max_zoom) == (expected.min_zoom, expected.max_zoom)
        assert item.zoom_text == expected.range_text


def test_recommended_zoom_helper_matches_add_region(
    builder: DownloadPlanBuilder,
) -> None:
    assert builder.recommended_zoom("rivertown") == (
        builder.add_region("rivertown").item.min_zoom,
        builder.plan.item("rivertown").max_zoom,
    )


def test_custom_zoom_is_kept_verbatim_even_beyond_the_recommendation(
    builder: DownloadPlanBuilder, service: RegionService
) -> None:
    """Spec §6：全国 Z4~18 这样的配置**可以存在**，编辑器不许改它。

    改与不改的分界很清楚：编辑器负责"记下来"，校验器负责"提醒"。
    """
    recommendation = service.strategy.recommend(RegionLevel.COUNTRY)
    builder.add_region(
        "demoland",
        use_recommended_zoom=False,
        min_zoom=recommendation.min_zoom,
        max_zoom=18,
    )
    item = builder.plan.item("demoland")
    assert item.max_zoom == 18
    assert item.max_zoom > recommendation.max_zoom


def test_custom_zoom_requires_both_bounds(builder: DownloadPlanBuilder) -> None:
    with pytest.raises(ValueError):
        builder.add_region("demoland", use_recommended_zoom=False, min_zoom=4)
    with pytest.raises(ValueError):
        builder.add_region("demoland", use_recommended_zoom=False, max_zoom=10)
    assert builder.plan.is_empty


def test_custom_zoom_rejects_an_inverted_range(builder: DownloadPlanBuilder) -> None:
    """顺序反了是非法输入，不顺手帮忙调过来（与 Slice 17 同一条规则）。"""
    with pytest.raises(ValueError):
        builder.add_region("demoland", use_recommended_zoom=False, min_zoom=12, max_zoom=10)


def test_custom_zoom_rejects_an_illegal_zoom(builder: DownloadPlanBuilder) -> None:
    with pytest.raises(ValueError):
        builder.add_region("demoland", use_recommended_zoom=False, min_zoom=4, max_zoom=99)


def test_zoom_and_recommended_flag_together_are_rejected(
    builder: DownloadPlanBuilder,
) -> None:
    """两个都给说明调用方自己没想清楚要哪一个 —— 直接报错，别猜。"""
    with pytest.raises(ValueError):
        builder.add_region("demoland", use_recommended_zoom=True, min_zoom=4, max_zoom=10)


# --------------------------------------------------------------------------- #
# 删 / 启停 / 清空（Spec §11 / §12）
# --------------------------------------------------------------------------- #
def test_remove_region(builder: DownloadPlanBuilder) -> None:
    builder.add_region("demoland")
    builder.add_region("northland")
    assert builder.remove_region("demoland") is True
    assert builder.remove_region("demoland") is False
    assert [item.region_id for item in builder.plan.items] == ["northland"]


def test_remove_recalculates_the_totals(builder: DownloadPlanBuilder) -> None:
    builder.add_region("demoland")
    builder.add_region("northland")
    builder.plan.item("demoland").candidate_tiles = 10
    builder.plan.item("demoland").selected_tiles = 10
    builder.plan.item("northland").candidate_tiles = 20
    builder.plan.item("northland").selected_tiles = 20
    builder.build()
    assert builder.plan.total_tiles == 30

    builder.remove_region("northland")
    assert builder.plan.total_tiles == 10


def test_set_enabled_changes_the_totals(builder: DownloadPlanBuilder) -> None:
    builder.add_region("demoland")
    item = builder.plan.item("demoland")
    item.candidate_tiles = 10
    item.selected_tiles = 10
    builder.build()
    assert builder.plan.total_tiles == 10

    assert builder.set_enabled("demoland", False) is True
    assert builder.plan.total_tiles == 0
    assert item.candidate_tiles == 10  # 禁用不动配置，也不丢估算

    assert builder.set_enabled("demoland", True) is True
    assert builder.plan.total_tiles == 10
    assert builder.set_enabled("northland", False) is False  # 计划里没有它


def test_reset_keeps_the_plans_identity(builder: DownloadPlanBuilder) -> None:
    builder.set_name("我的计划")
    builder.set_output_dir("D:/out")
    builder.add_region("demoland")
    plan_id = builder.plan.id

    builder.reset()
    assert builder.plan.id == plan_id
    assert builder.plan.is_empty
    assert (builder.plan.name, builder.plan.output_dir) == ("我的计划", "D:/out")


# --------------------------------------------------------------------------- #
# 计划级设置
# --------------------------------------------------------------------------- #
def test_build_sets_the_name_and_recalculates(builder: DownloadPlanBuilder) -> None:
    builder.add_region("demoland")
    item = builder.plan.item("demoland")
    item.candidate_tiles = 7
    item.selected_tiles = 7
    item.estimated_bytes = 100
    plan = builder.build("新名字")
    assert plan.name == "新名字"
    assert plan.total_tiles == 7 and plan.estimated_bytes == 100


def test_setters_normalize_values(builder: DownloadPlanBuilder) -> None:
    from app.models.map_layer import MapLayerType

    assert builder.set_name("  计划  ") == "计划"
    assert builder.set_output_dir("  D:/out  ") == "D:/out"
    assert builder.set_map_layer(MapLayerType.VECTOR) == "vec_w"
    assert builder.set_map_layer("img_w") == "img_w"
    assert builder.set_with_labels(True) is True
    assert builder.plan.with_labels is True


def test_load_swaps_the_plan_being_edited(builder: DownloadPlanBuilder) -> None:
    builder.add_region("demoland")
    other = DownloadPlan(name="另一份计划")
    other.add_item(DownloadPlanItem(region_id="china", region_name="中国"))
    assert builder.load(other) is other
    assert builder.plan is other
    assert builder.contains("china") is True
    assert builder.contains("demoland") is False


def test_load_rejects_a_non_plan(builder: DownloadPlanBuilder) -> None:
    with pytest.raises(ValueError):
        builder.load({"id": "不是计划"})  # type: ignore[arg-type]


def test_new_builder_starts_with_the_default_name(service: RegionService) -> None:
    from app.config.settings import DEFAULT_PLAN_NAME

    assert DownloadPlanBuilder(service).plan.name == DEFAULT_PLAN_NAME


def test_builder_reuses_the_services_strategy(service: RegionService) -> None:
    """默认复用 ``service.strategy``：多造一个实例就多一处要同步的注入点。"""
    builder = DownloadPlanBuilder(service)
    assert builder.strategy is service.strategy


def test_contains_and_service_accessors(builder: DownloadPlanBuilder) -> None:
    assert builder.contains("demoland") is False
    builder.add_region("demoland")
    assert builder.contains("demoland") is True
    assert builder.service is not None


def test_plan_add_result_is_a_frozen_value_object() -> None:
    item = DownloadPlanItem(region_id="china", region_name="中国")
    result = PlanAddResult(item=item, created=True)
    assert result.item is item and result.created is True
    with pytest.raises(Exception):
        result.created = False  # type: ignore[misc]
