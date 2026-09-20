"""行政层级：中国 → 省 → 市 的规则、查询与 Zoom 集成（Slice 18，Spec §6 / §9 / §10）。

层级这条约束的价值在于它**可被机器检查**：一个把市直接挂在国下的目录，
如果没人拦，界面上看起来完全正常（下拉框照样有它），只有等到"按省算覆盖"
或者"按层级推荐 Zoom"时才出错 —— 而那时已经查不出是哪一条记录的问题。

本文件守三件事：

1. 树本身对不对（父级存在、层级匹配、没有环）；
2. :class:`~app.regions.RegionService` 的层级查询（Spec §10）；
3. **等级 → Zoom** 这条链（Spec §9）：区间永远来自
   :data:`~app.planning.DEFAULT_ZOOM_POLICIES`，目录里一个数字都不存。
"""

from __future__ import annotations

import pytest

from app.geo import CHINA_REGION_PRESET, REGION_PRESETS, RegionTileEstimator, get_region_preset
from app.planning import (
    ADMINISTRATIVE_LEVELS,
    PARENT_LEVEL_RULES,
    RegionLevel,
    ZoomStrategyService,
)
from app.regions import (
    RegionCatalog,
    RegionCatalogError,
    RegionService,
    validate_catalog,
)
from app.regions.models import CatalogIssueCode, IssueSeverity, RegionPreset


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _entry(region_id: str, level, parent: str | None = None, **overrides) -> dict:
    base = {
        "id": region_id,
        "name": f"示例-{region_id}",
        "level": level.value if isinstance(level, RegionLevel) else level,
        "parentId": parent,
        "geojson": f"country/{region_id}.geojson",
    }
    base.update(overrides)
    return base


def _catalog(*entries: dict, root=None) -> RegionCatalog:
    return RegionCatalog.from_payload({"version": 1, "regions": list(entries)}, root=root)


def _codes(catalog: RegionCatalog) -> set[CatalogIssueCode]:
    return {item.code for item in validate_catalog(catalog).issues}


def _service(region_root) -> RegionService:
    return RegionService(root=region_root)


# --------------------------------------------------------------------------- #
# 层级规则（Spec §6）
# --------------------------------------------------------------------------- #
def test_the_fixture_tree_is_three_levels_deep(region_catalog: RegionCatalog) -> None:
    assert region_catalog.get("demoland").parent_id is None
    assert region_catalog.get("northland").parent_id == "demoland"
    assert region_catalog.get("rivertown").parent_id == "northland"

    assert [item.id for item in region_catalog.get_ancestors("rivertown")] == [
        "demoland",
        "northland",
    ]
    assert region_catalog.path_text("hilltown") == "示例国 / 示例北省 / 示例山市"


def test_administrative_levels_are_derived_from_the_parent_table() -> None:
    """行政层级集合**从**父子规则表推导 —— 加等级时不会有人漏改。"""
    assert ADMINISTRATIVE_LEVELS == tuple(PARENT_LEVEL_RULES)
    assert set(ADMINISTRATIVE_LEVELS) == {
        RegionLevel.COUNTRY,
        RegionLevel.PROVINCE,
        RegionLevel.CITY,
    }


def test_country_may_not_have_a_parent() -> None:
    catalog = _catalog(_entry("a", RegionLevel.COUNTRY), _entry("b", RegionLevel.COUNTRY, "a"))

    result = validate_catalog(catalog)

    assert result.has(CatalogIssueCode.COUNTRY_HAS_PARENT)
    assert result.ok is False


def test_province_must_hang_under_a_country() -> None:
    catalog = _catalog(
        _entry("c", RegionLevel.COUNTRY),
        _entry("p", RegionLevel.PROVINCE, "c"),
    )
    assert validate_catalog(catalog).ok is True


def test_city_may_not_hang_directly_under_a_country() -> None:
    """``CITY`` 直接挂 ``COUNTRY`` 是**非法**的（Spec §6 点名）。"""
    catalog = _catalog(
        _entry("c", RegionLevel.COUNTRY),
        _entry("t", RegionLevel.CITY, "c"),
    )

    result = validate_catalog(catalog)

    assert result.has(CatalogIssueCode.PARENT_LEVEL_INVALID)
    (issue,) = result.errors
    assert issue.region_id == "t"
    assert "省级" in issue.message


def test_city_must_hang_under_a_province_once_removed_from_a_country() -> None:
    """反过来：市挂在省下就没事（对照组，防止上一条把正常情况也判成错）。"""
    catalog = _catalog(
        _entry("c", RegionLevel.COUNTRY),
        _entry("p", RegionLevel.PROVINCE, "c"),
        _entry("t", RegionLevel.CITY, "p"),
    )

    assert validate_catalog(catalog).ok is True


def test_dangling_parent_is_an_error() -> None:
    catalog = _catalog(_entry("p", RegionLevel.PROVINCE, "nowhere"))

    result = validate_catalog(catalog)

    assert result.has(CatalogIssueCode.PARENT_MISSING)
    (issue,) = result.errors
    assert "nowhere" in issue.message


def test_self_parent_is_reported_separately() -> None:
    """自环单独报：它既是环，也是个更蠢的错误，消息要一眼看懂。"""
    catalog = _catalog(_entry("a", RegionLevel.COUNTRY, "a"))

    result = validate_catalog(catalog)

    assert result.has(CatalogIssueCode.SELF_PARENT)
    assert not result.has(CatalogIssueCode.PARENT_CYCLE)


def test_a_cycle_is_reported_once() -> None:
    """``a → b → a``：报一次就够，挂在该环里 id 最小的那个上。"""
    catalog = _catalog(
        _entry("a", RegionLevel.PROVINCE, "b"),
        _entry("b", RegionLevel.PROVINCE, "a"),
    )

    result = validate_catalog(catalog)

    assert result.count(CatalogIssueCode.PARENT_CYCLE) == 1
    cycle_issue = next(
        item for item in result.issues if item.code is CatalogIssueCode.PARENT_CYCLE
    )
    assert cycle_issue.region_id == "a"
    assert "a" in cycle_issue.message and "b" in cycle_issue.message


def test_duplicate_ids_are_reported() -> None:
    payload = {
        "version": 1,
        "regions": [
            _entry("same", RegionLevel.COUNTRY),
            _entry("same", RegionLevel.COUNTRY, name="重复的"),
        ],
    }
    catalog = RegionCatalog.from_payload(payload)

    result = validate_catalog(catalog)

    assert result.has(CatalogIssueCode.DUPLICATE_ID)
    assert len(catalog) == 1, "重复的那条被跳过，目录里只剩先出现的"


def test_mission_and_custom_are_exempt_from_the_level_rule() -> None:
    """任务区 / 自定义区域可以挂在任何地方 —— 但父级仍然必须存在（Spec §6）。"""
    catalog = _catalog(
        _entry("c", RegionLevel.COUNTRY),
        _entry("p", RegionLevel.PROVINCE, "c"),
        _entry("m", RegionLevel.MISSION, "p"),
        _entry("x", RegionLevel.CUSTOM, "c"),
    )

    assert validate_catalog(catalog).ok is True


def test_mission_with_a_dangling_parent_is_still_an_error() -> None:
    catalog = _catalog(_entry("m", RegionLevel.MISSION, "nowhere"))

    assert validate_catalog(catalog).has(CatalogIssueCode.PARENT_MISSING)


def test_hierarchy_rules_come_from_the_planning_layer() -> None:
    """规则表**只有一份**：区域层转出的就是规划层那个对象。

    这条用例防的是"两边各存一份表，改了一边" —— 那比完全没有规则更难查，
    因为校验结果会随着"问的是谁"而变化。
    """
    from app.planning import region_level as planning_module
    from app.regions import models as models_module

    assert models_module.PARENT_LEVEL_RULES is planning_module.PARENT_LEVEL_RULES
    assert models_module.ADMINISTRATIVE_LEVELS is planning_module.ADMINISTRATIVE_LEVELS


# --------------------------------------------------------------------------- #
# RegionService 的层级查询（Spec §10）
# --------------------------------------------------------------------------- #
def test_service_lists_the_hierarchy(region_root) -> None:
    service = _service(region_root)

    assert [item.id for item in service.list_countries()] == ["demoland"]
    assert [item.id for item in service.list_provinces("demoland")] == [
        "northland",
        "southland",
    ]
    assert [item.id for item in service.list_cities("northland")] == [
        "rivertown",
        "hilltown",
    ]
    assert service.list_cities("southland")[0].id == "lakecity"


def test_service_rejects_unknown_parents_with_a_helpful_message(region_root) -> None:
    service = _service(region_root)

    with pytest.raises(RegionCatalogError) as excinfo:
        service.list_provinces("france")

    assert "france" in str(excinfo.value)
    assert "demoland" in str(excinfo.value), "报错要顺便告诉用户目录里有什么"


def test_service_get_region_and_ancestors(region_root) -> None:
    service = _service(region_root)

    assert service.get_region("nope") is None
    assert service.require_region("rivertown").name == "示例河市"
    assert service.path_text("rivertown") == "示例国 / 示例北省 / 示例河市"
    assert [item.id for item in service.ancestors("rivertown")] == ["demoland", "northland"]
    assert [item.id for item in service.children("northland")] == ["rivertown", "hilltown"]
    assert [item.id for item in service.children(None)] == ["demoland"]


def test_service_resolves_paths_and_availability(region_root) -> None:
    service = _service(region_root)

    assert service.boundary_available("rivertown") is True
    assert service.resolve_path("rivertown").name == "rivertown.geojson"
    assert service.boundary_available("不存在的区域") is False


def test_service_download_region_is_a_polygon(region_root) -> None:
    region = _service(region_root).get_download_region("rivertown")

    assert region.is_polygon is True
    assert region.region_id == "rivertown"
    assert region.region_level is RegionLevel.CITY


# --------------------------------------------------------------------------- #
# 等级 → Zoom（Spec §9）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "region_id, expected",
    [
        ("demoland", "Z4~10"),
        ("northland", "Z9~13"),
        ("rivertown", "Z12~16"),
    ],
)
def test_level_drives_the_zoom_recommendation(region_root, region_id, expected) -> None:
    """目录里只存 ``level``，区间由策略表决定（Spec §9）。"""
    recommendation = _service(region_root).get_zoom_recommendation(region_id)

    assert recommendation.range_text == expected
    assert recommendation.warning is False, "纯推荐还没有「当前 Zoom」，谈不上超出"


def test_recommendation_comes_from_the_strategy_service(region_root) -> None:
    """逐字段对齐策略服务的结果 —— 证明这一层没有自己造一套建议。"""
    service = _service(region_root)
    strategy = ZoomStrategyService()

    for region_id in ("demoland", "northland", "rivertown"):
        entry = service.require_region(region_id)
        expected = strategy.recommend(entry.level)
        assert service.get_zoom_recommendation(region_id) == expected


def test_catalog_does_not_store_any_zoom_number() -> None:
    """目录条目里**没有** Zoom 字段（Spec §9 最后一句）。"""
    entry = RegionPreset(id="a", name="A", level=RegionLevel.PROVINCE)

    fields = set(entry.as_dict()) | set(vars(entry))
    assert not any("zoom" in name.lower() for name in fields)


def test_evaluate_reports_a_warning_without_touching_the_user_range(region_root) -> None:
    """全国 + Z8~18：给出 warning，但记下来的仍然是用户原样的 8 和 18。"""
    recommendation = _service(region_root).get_zoom_recommendation(
        "demoland", min_zoom=8, max_zoom=18
    )

    assert recommendation.range_text == "Z4~10"
    assert (recommendation.requested_min_zoom, recommendation.requested_max_zoom) == (8, 18)
    assert recommendation.warning is True


def test_recommend_with_estimate_reuses_the_estimator(region_root) -> None:
    """推荐 + 容量预估：数字必须与直接调估算器逐字段一致（Spec §9）。"""
    service = _service(region_root)

    plan = service.recommend_with_estimate("rivertown", min_zoom=12, max_zoom=12)
    region = service.get_download_region("rivertown")
    direct = RegionTileEstimator(region).estimate(12, 12)

    assert plan.selected_tiles == direct.selected_tiles
    assert plan.candidate_tiles == direct.candidate_tiles
    assert plan.size_text == direct.size_text
    assert plan.estimated_min_zoom == 12 and plan.estimated_max_zoom == 12


def test_recommend_with_estimate_uses_the_regions_own_level(region_root) -> None:
    """等级取自区域对象本身 —— 「下载的区域」和「算建议的等级」在物理上是同一个字段。"""
    service = _service(region_root)

    plan = service.recommend_with_estimate("northland", min_zoom=9, max_zoom=10)

    assert plan.level is RegionLevel.PROVINCE
    assert plan.recommended_range_text == "Z9~13"


def test_recommend_with_estimate_defaults_to_the_recommended_span(region_root) -> None:
    """不给 Zoom 就按推荐区间预估（全国级 → Z4~10）。"""
    plan = _service(region_root).recommend_with_estimate("demoland")

    assert (plan.estimated_min_zoom, plan.estimated_max_zoom) == (4, 10)
    assert plan.used_recommended_zoom is True
    assert plan.selected_tiles > 0


def test_service_rejects_half_a_zoom_range(region_root) -> None:
    with pytest.raises(ValueError, match="同时给出"):
        _service(region_root).get_zoom_recommendation("demoland", min_zoom=4)


def test_estimator_works_through_the_service_on_the_country(region_root) -> None:
    """全国级区域也能走完整的预估链路（多边形过滤照旧生效）。"""
    service = _service(region_root)

    region = service.get_download_region("demoland")
    estimate = RegionTileEstimator(region).estimate(6, 7)

    assert estimate.selected_tiles > 0
    assert [item.zoom for item in estimate.per_zoom] == [6, 7]


# --------------------------------------------------------------------------- #
# 中国预设：目录与扁平预设必须一致
# --------------------------------------------------------------------------- #
def test_china_is_a_country_in_the_shipped_catalog() -> None:
    """产品资源目录里的 china 就是全国级（Spec §16 的 Integration 第一条）。"""
    service = RegionService()

    entry = service.require_region("china")
    assert entry.name == "中国"
    assert entry.level is RegionLevel.COUNTRY
    assert entry.is_country is True
    assert entry.parent_id is None
    assert [item.id for item in service.list_countries()] == ["china"]


def test_catalog_and_legacy_preset_agree_on_china() -> None:
    """目录里的 china 与 Slice 16 的扁平预设**指向同一个区域、同一个文件**。

    两处都要存在的理由：界面走目录（可扩展），而续传 / Inspector 走扁平预设
    （``get_region_preset`` 在 ``app.geo`` 里，不能反向依赖 ``app.regions``，
    否则就是循环导入）。既然非有两处不可，就得有一条用例盯着它们不许漂移 ——
    一旦分家，用户会看到"界面说边界文件放这、续传说放那"。"""
    entry = RegionService().require_region("china")
    preset = get_region_preset("china")
    assert preset is not None

    assert preset is CHINA_REGION_PRESET
    assert preset.region_id == entry.id
    assert preset.name == entry.name
    assert preset.region_level is entry.level
    assert preset.geojson_filename == entry.geojson_path
    assert preset.geojson_path == entry.path_under(preset.resource_root())


def test_legacy_preset_list_is_still_china_only() -> None:
    """Slice 16 的常量没被动过：本 Slice **不**加省 / 市数据（Spec §19）。"""
    assert [item.region_id for item in REGION_PRESETS] == ["china"]


def test_shipped_catalog_contains_no_province_or_city_yet() -> None:
    """仓库自带的目录里只有中国：省市数据必须来自合法授权来源（Spec §8 / §19）。"""
    service = RegionService()

    assert [item.id for item in service.list_provinces("china")] == []
    assert service.catalog.ids == ("china",)


def test_shipped_catalog_missing_boundary_is_only_a_warning() -> None:
    """仓库里没有国界数据 —— 那是**提醒**不是错误，程序照常可用。"""
    result = RegionService().validate()

    assert result.ok is True
    assert result.has(CatalogIssueCode.GEOJSON_NOT_FOUND)
    assert all(item.severity is IssueSeverity.WARNING for item in result.warnings)
