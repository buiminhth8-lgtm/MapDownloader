"""HierarchyBuilder：COUNTRY → PROVINCE → CITY 层级校验（Slice 26，Spec §8 / §9 / §26 Hierarchy 部分）。

守的正是 Spec §8 点名的那张清单：parent 不存在、duplicate id、cycle、
level 错误、orphan city、city 直接挂 country —— 任一严重层级错误必须让
导入 FAIL，而不是"生成半正确 Catalog"。

直辖市兼容（Spec §9）单独一组：等级映射把"直辖市"落到省级、其下属区域
挂成市级；源数据没有市级层时省照常可用（warning，不是错误）。
"""

from __future__ import annotations

from pathlib import Path

from app.planning.region_level import RegionLevel
from app.regions.importer import (
    RegionImportConfig,
    RegionImportIssueCode,
    build_hierarchy,
    normalize_features,
    read_source,
)
from tests.conftest import REGION_IMPORT_FIXTURE_ROOT


def _build(
    source: str = "sample.geojson",
    *,
    country_id: str | None = None,
    level_aliases: dict[str, str] | None = None,
    **field_kwargs,
):
    """读 → 标准化 → 建层级，返回 ``(entries, issues)``。"""
    fields = {
        "id_field": "code",
        "name_field": "name",
        "level_field": "level",
        "parent_field": "parent",
        "boundary_version": "2026-10-test",
        # 建层级不碰发布目录（占位路径，见 test_region_source_reader 的说明）。
        "output_root": Path("unused-in-dry-run"),
    }
    fields.update(field_kwargs)
    config = RegionImportConfig(
        source_file=REGION_IMPORT_FIXTURE_ROOT / source,
        country_id=country_id,
        level_aliases=level_aliases,
        **fields,
    )
    features, issues = read_source(config)
    alive = normalize_features(features, issues=issues)
    entries = build_hierarchy(alive, config, issues=issues)
    return entries, issues


# --------------------------------------------------------------------------- #
# 正常三级层级
# --------------------------------------------------------------------------- #
def test_country_province_city_tree_is_built() -> None:
    entries, issues = _build()

    errors = [item for item in issues if item.is_error]
    assert errors == []
    by_id = {item.id: item for item in entries}
    assert set(by_id) == {
        "test_country",
        "test_province_a",
        "test_province_b",
        "test_city_a1",
        "test_city_a2",
    }
    assert by_id["test_country"].parent_id is None
    assert by_id["test_province_a"].parent_id == "test_country"
    assert by_id["test_province_b"].parent_id == "test_country"
    assert by_id["test_city_a1"].parent_id == "test_province_a"
    assert by_id["test_city_a2"].parent_id == "test_province_a"
    # bounds 来自几何（每个市都真的在自己的省里面）。
    assert by_id["test_city_a1"].bounds.west == 110.0
    assert by_id["test_province_a"].bounds.north == 32.0


def test_city_is_inside_province_inside_country() -> None:
    """Spec §25 的几何要求：City ⊂ Province ⊂ Country（夹具本身就是这么画的）。"""
    entries, _issues = _build()
    by_id = {item.id: item for item in entries}

    def contains(outer, inner) -> bool:
        return outer.feature.geometry.contains(inner.feature.geometry)

    assert contains(by_id["test_country"], by_id["test_province_a"])
    assert contains(by_id["test_country"], by_id["test_province_b"])
    assert contains(by_id["test_province_a"], by_id["test_city_a1"])
    assert contains(by_id["test_province_a"], by_id["test_city_a2"])


# --------------------------------------------------------------------------- #
# Spec §8 点名的每一类错误
# --------------------------------------------------------------------------- #
def test_duplicate_id_is_rejected() -> None:
    _entries, issues = _build("duplicate_id.geojson")
    dup = [item for item in issues if item.code is RegionImportIssueCode.DUPLICATE_ID]
    assert len(dup) == 1


def test_missing_parent_is_rejected() -> None:
    """父级指向不存在的 id + 市级条目没有父级（orphan city）。"""
    _entries, issues = _build("missing_parent.geojson")
    missing = [item for item in issues if item.code is RegionImportIssueCode.PARENT_MISSING]
    assert len(missing) == 2
    assert any("孤儿省" in item.feature for item in missing)
    assert any("孤儿市" in item.feature for item in missing)


def test_parent_cycle_is_rejected() -> None:
    """两个省互指：环本身 + 等级错误都报（FAIL 的两种走法）。"""
    _entries, issues = _build("cycle.geojson")
    cycles = [item for item in issues if item.code is RegionImportIssueCode.PARENT_CYCLE]
    assert cycles, "环必须被检测出来"
    level_errors = [
        item for item in issues if item.code is RegionImportIssueCode.PARENT_LEVEL_INVALID
    ]
    assert level_errors, "省挂省同时也是等级错误"


def test_city_directly_under_country_is_rejected() -> None:
    """city 直接挂 country：Spec §8 单点的一类。"""
    _entries, issues = _build("wrong_level.geojson")
    under_country = [
        item for item in issues if item.code is RegionImportIssueCode.CITY_UNDER_COUNTRY
    ]
    assert len(under_country) == 1
    assert "直辖错市级" in under_country[0].feature


def test_country_with_parent_is_rejected() -> None:
    _entries, issues = _build("wrong_level.geojson")
    has_parent = [
        item for item in issues if item.code is RegionImportIssueCode.COUNTRY_HAS_PARENT
    ]
    assert len(has_parent) == 1


def test_province_under_province_is_a_level_error() -> None:
    _entries, issues = _build("wrong_level.geojson")
    level_errors = [
        item for item in issues if item.code is RegionImportIssueCode.PARENT_LEVEL_INVALID
    ]
    assert any("省挂省" in item.feature for item in level_errors)


def test_dataset_without_country_fails() -> None:
    """没有全国条目：省无处可挂（沿用逻辑在 import_service，builder 负责报错）。"""
    _entries, issues = _build("province_only.geojson")
    no_country = [
        item for item in issues if item.code is RegionImportIssueCode.NO_COUNTRY_ENTRY
    ]
    assert no_country
    assert no_country[0].is_error


def test_province_without_parent_fails() -> None:
    """省级条目 parent 为空 = 层级错误（不是"当根节点用"）。"""
    _entries, issues = _build("missing_parent.geojson")
    # 孤儿市（无父级）已经覆盖了空 parent；这里补一条省级的空 parent 场景。
    import json
    import tempfile
    from pathlib import Path


    directory = Path(tempfile.mkdtemp(prefix="hierarchy-test-"))
    try:
        source = directory / "no_parent.geojson"
        source.write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"code": "c", "name": "国", "level": "country"},
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]
                                ],
                            },
                        },
                        {
                            "type": "Feature",
                            "properties": {"code": "p", "name": "无父级省", "level": "province"},
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
                                ],
                            },
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        entries, issues = _build(str(source))
    finally:
        import shutil

        shutil.rmtree(directory, ignore_errors=True)

    missing = [item for item in issues if item.code is RegionImportIssueCode.PARENT_MISSING]
    assert any("无父级省" in item.feature for item in missing)
    assert all(item.id != "p" for item in entries), "父级不成立的条目不进目录"


# --------------------------------------------------------------------------- #
# 直辖市兼容（Spec §9）
# --------------------------------------------------------------------------- #
def test_municipality_maps_to_province_level() -> None:
    """"直辖市"经等级映射落到省级，下属区域挂成市级。"""
    entries, issues = _build("municipality.geojson")
    by_id = {item.id: item for item in entries}

    assert by_id["test_municipality"].level is RegionLevel.PROVINCE
    assert by_id["test_municipality_district"].level is RegionLevel.CITY
    assert by_id["test_municipality_district"].parent_id == "test_municipality"
    # 另一个没有市级层的省照常可用。
    assert by_id["test_plain_province"].level is RegionLevel.PROVINCE
    errors = [item for item in issues if item.is_error]
    assert errors == []


def test_missing_city_layer_is_a_warning_not_an_error() -> None:
    """源数据没有市级层：省照常可用（Spec §9 原话）。"""
    entries, issues = _build("municipality.geojson")
    # municipality 夹具里有市级条目，这里换一个只有两层的场景验证 warning 本体。
    import json
    import shutil
    import tempfile
    from pathlib import Path

    directory = Path(tempfile.mkdtemp(prefix="hierarchy-test-"))
    try:
        source = directory / "two_levels.geojson"
        source.write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"code": "c", "name": "国", "level": "country"},
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]
                                ],
                            },
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "code": "p",
                                "name": "无市省",
                                "level": "province",
                                "parent": "c",
                            },
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
                                ],
                            },
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        entries, issues = _build(str(source))
    finally:
        shutil.rmtree(directory, ignore_errors=True)

    warnings = [
        item
        for item in issues
        if item.code is RegionImportIssueCode.NO_CITY_LAYER
    ]
    assert len(warnings) == 1
    assert not warnings[0].is_error
    # 省仍然在目录里（不因为没有市就把它拖出去）。
    assert any(item.id == "p" for item in entries)


def test_extra_level_aliases_are_configurable(tmp_path) -> None:
    """配置追加的等级映射优先生效（用户比默认表更清楚自己的数据）。"""
    import json

    source = tmp_path / "custom_level.geojson"
    source.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"code": "c", "name": "国", "level": "country"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
                        },
                    },
                    {
                        "type": "Feature",
                        "properties": {
                            "code": "p",
                            "name": "特殊省",
                            "level": "level2",
                            "parent": "c",
                        },
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    # 不配别名：level2 认不出来 → 拒绝。
    _entries, issues = _build(str(source))
    assert any(
        item.code is RegionImportIssueCode.UNKNOWN_LEVEL for item in issues
    )
    # 配上别名：正常映射成省级。
    entries, issues = _build(
        str(source), level_aliases={"level2": "province"}
    )
    assert any(item.id == "p" and item.level is RegionLevel.PROVINCE for item in entries)
    assert not [item for item in issues if item.code is RegionImportIssueCode.UNKNOWN_LEVEL]


# --------------------------------------------------------------------------- #
# country_id 覆写（Spec §3）
# --------------------------------------------------------------------------- #
def test_country_id_override_rewrites_province_parents() -> None:
    """country_id 覆写全国条目 id 时，省级条目的 parent 引用一起改写。"""
    entries, issues = _build("sample.geojson", country_id="china")

    by_id = {item.id: item for item in entries}
    assert "china" in by_id
    assert "test_country" not in by_id
    assert by_id["test_province_a"].parent_id == "china"
    assert by_id["test_province_b"].parent_id == "china"
    errors = [item for item in issues if item.is_error]
    assert errors == []


def test_country_id_override_matching_source_is_a_noop() -> None:
    entries, issues = _build("sample.geojson", country_id="test_country")
    assert any(item.id == "test_country" for item in entries)
    errors = [item for item in issues if item.is_error]
    assert errors == []
