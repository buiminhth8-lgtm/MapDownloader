"""SourceReader：GeoJSON FeatureCollection 导入（Slice 26，Spec §2 / §4 / §26 Reader 部分）。

守四类行为：

1. **正常读取**：FeatureCollection → 结构化条目，Polygon 与 MultiPolygon
   都认，properties / geometry 原样带着走；
2. **字段名不假设**：换一套键名（GBCODE/NAME/LEVEL/PARENT）的夹具配对应
   配置照样读得动；配置写错时报错里列出源数据里真实存在的键；
3. **拒绝坏数据**：Point / LineString / 空几何 / 缺必要字段逐条拒绝，
   且**每一条都有 issue**（Spec §4"不要静默忽略大量错误数据"）；
4. **顶层结构**：只认 FeatureCollection 与单个 Feature，其余拒绝。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.regions.importer import (
    RegionImportConfig,
    RegionImportError,
    RegionImportIssueCode,
    read_source,
)
from app.planning.region_level import RegionLevel

from tests.conftest import REGION_IMPORT_FIXTURE_ROOT


def _config(source: str = "sample.geojson", **kwargs) -> RegionImportConfig:
    defaults = dict(
        id_field="code",
        name_field="name",
        level_field="level",
        parent_field="parent",
        boundary_version="2026-10-test",
        # 读 / 标准化 / 建层级都不碰发布目录；这里给个显式占位，
        # 免得某个用例顺手把数据写进仓库的 resources/regions/generated。
        output_root=Path("unused-in-dry-run"),
    )
    defaults.update(kwargs)
    return RegionImportConfig(
        source_file=REGION_IMPORT_FIXTURE_ROOT / source, **defaults
    )


# --------------------------------------------------------------------------- #
# 正常读取
# --------------------------------------------------------------------------- #
def test_reads_feature_collection_into_structured_entries() -> None:
    features, issues = read_source(_config())

    assert issues == []
    assert len(features) == 5
    by_id = {item.region_id: item for item in features}
    assert set(by_id) == {
        "test_country",
        "test_province_a",
        "test_province_b",
        "test_city_a1",
        "test_city_a2",
    }
    country = by_id["test_country"]
    assert country.name == "测试国"
    assert country.level is RegionLevel.COUNTRY
    assert country.parent_ref == ""
    assert country.geometry.geom_type == "Polygon"
    # 原始 properties 与 geometry 字典都带着走（写回时原样保留用）。
    assert country.raw_properties["code"] == "test_country"
    assert country.raw_geometry["type"] == "Polygon"

    city = by_id["test_city_a1"]
    assert city.level is RegionLevel.CITY
    assert city.parent_ref == "test_province_a"
    assert city.source_index == 3  # 顺序与源文件一致


def test_accepts_multi_polygon_geometry(tmp_path: Path) -> None:
    source = tmp_path / "multi.geojson"
    source.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "code": "mp_province",
                            "name": "多块省",
                            "level": "province",
                            "parent": "mp_country",
                        },
                        "geometry": {
                            "type": "MultiPolygon",
                            "coordinates": [
                                [
                                    [
                                        [110.0, 30.0],
                                        [111.0, 30.0],
                                        [111.0, 31.0],
                                        [110.0, 31.0],
                                        [110.0, 30.0],
                                    ]
                                ],
                                [
                                    [
                                        [112.0, 32.0],
                                        [113.0, 32.0],
                                        [113.0, 33.0],
                                        [112.0, 33.0],
                                        [112.0, 32.0],
                                    ]
                                ],
                            ],
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    features, issues = read_source(_config(str(source)))

    assert issues == []
    assert len(features) == 1
    assert features[0].geometry.geom_type == "MultiPolygon"


def test_accepts_single_feature_document(tmp_path: Path) -> None:
    source = tmp_path / "single.geojson"
    source.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {"code": "solo", "name": "单个", "level": "country"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    features, issues = read_source(_config(str(source), parent_field=None))

    assert issues == []
    assert [item.region_id for item in features] == ["solo"]


# --------------------------------------------------------------------------- #
# 字段映射（Spec §2：不许假设字段名）
# --------------------------------------------------------------------------- #
def test_alternative_field_names_are_configurable() -> None:
    """alt_fields 夹具用 GBCODE/NAME/LEVEL/PARENT + 数字等级 1/2/3。"""
    features, issues = read_source(
        _config(
            "alt_fields.geojson",
            id_field="GBCODE",
            name_field="NAME",
            level_field="LEVEL",
            parent_field="PARENT",
        )
    )

    assert issues == []
    assert len(features) == 3
    levels = {item.region_id: item.level for item in features}
    assert levels["alt_country"] is RegionLevel.COUNTRY
    assert levels["alt_province"] is RegionLevel.PROVINCE
    assert levels["alt_city"] is RegionLevel.CITY


def test_misconfigured_field_names_report_available_keys() -> None:
    """配置的字段不存在时，报错列出源数据里真实有的键（Spec §2）。"""
    features, issues = read_source(_config("alt_fields.geojson"))

    assert features == ()
    field_issues = [
        item for item in issues if item.code is RegionImportIssueCode.FIELD_NOT_IN_SOURCE
    ]
    # id / name / level 三个必要字段是 error；父级字段只是 warning
    # （源数据没有父级列是合法形态，层级错误会在更准确地地方现形）。
    assert sum(1 for item in field_issues if item.is_error) == 3
    assert sum(1 for item in field_issues if not item.is_error) == 1
    message = field_issues[0].message
    assert "GBCODE" in message and "NAME" in message
    assert "'code'" in message  # 报的是用户配错的键


def test_unknown_level_value_is_rejected_not_guessed(tmp_path: Path) -> None:
    source = tmp_path / "weird_level.geojson"
    source.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"code": "x", "name": "怪等级", "level": "village"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]
                            ],
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    features, issues = read_source(_config(str(source), parent_field=None))

    assert features == ()
    errors = [item for item in issues if item.is_error]
    assert [item.code for item in errors] == [RegionImportIssueCode.UNKNOWN_LEVEL]


# --------------------------------------------------------------------------- #
# 拒绝坏数据（Spec §4）
# --------------------------------------------------------------------------- #
def test_bad_geometry_features_are_rejected_one_by_one() -> None:
    """点 / 线 / 无几何 / 空几何：逐条拒绝，每条都有 issue（Spec §4）。

    注意**坐标越界不在这层管**：那是 normalizer 的职责（Spec §5），
    由 ``test_region_normalizer`` 的
    ``test_out_of_range_coordinates_are_rejected`` 守。reader 只判断
    "是不是面几何、解析不解析得动"。
    """
    features, issues = read_source(_config("bad_geometry.geojson"))

    # 全国条目 + 越界坐标那条（几何类型合法，范围留给 normalizer 判）。
    assert [item.region_id for item in features] == ["test_country", "out_of_range"]
    codes = [item.code for item in issues]
    assert codes.count(RegionImportIssueCode.UNSUPPORTED_GEOMETRY) == 2  # Point + LineString
    assert codes.count(RegionImportIssueCode.EMPTY_GEOMETRY) == 2  # null + 空坐标
    assert len(issues) == 4
    for issue in issues:
        assert issue.is_error
        assert issue.feature  # 每条都说得上是谁


def test_missing_required_fields_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "missing_fields.geojson"
    source.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"code": "no_name", "level": "country"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]
                            ],
                        },
                    },
                    {
                        "type": "Feature",
                        "properties": {"name": "无 code", "level": "country"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]
                            ],
                        },
                    },
                    {
                        "type": "Feature",
                        "properties": {"code": "no_level", "name": "无等级"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]
                            ],
                        },
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    features, issues = read_source(_config(str(source), parent_field=None))

    assert features == ()
    assert len(issues) == 3
    assert all(item.code is RegionImportIssueCode.MISSING_FIELD for item in issues)


def test_country_feature_without_parent_is_accepted() -> None:
    """parent 字段缺失是全国级条目的正常形态（不是错误）。"""
    features, issues = read_source(_config())
    country = next(item for item in features if item.region_id == "test_country")
    assert country.parent_ref == ""
    assert issues == []


# --------------------------------------------------------------------------- #
# 顶层结构与文件级错误
# --------------------------------------------------------------------------- #
def test_unsupported_top_level_type_is_hard_error(tmp_path: Path) -> None:
    source = tmp_path / "geometry.geojson"
    source.write_text(
        json.dumps(
            {
                "type": "Polygon",
                "coordinates": [
                    [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RegionImportError, match="顶层类型"):
        read_source(_config(str(source)))


def test_missing_file_is_hard_error() -> None:
    with pytest.raises(RegionImportError, match="不存在"):
        read_source(_config("no_such_file.geojson"))


def test_broken_json_is_hard_error(tmp_path: Path) -> None:
    source = tmp_path / "broken.geojson"
    source.write_text("{ not json", encoding="utf-8")
    with pytest.raises(RegionImportError, match="不是合法 JSON"):
        read_source(_config(str(source)))


def test_non_utf8_is_hard_error(tmp_path: Path) -> None:
    source = tmp_path / "gbk.geojson"
    # 带真实非 ASCII 内容的 GBK 字节流（纯 ASCII 的 GBK 就是 ASCII，
    # 解码得出来 —— 那不算"别的编码"）。
    source.write_bytes('{"name": "测试"}'.encode("gbk"))
    with pytest.raises(RegionImportError, match="UTF-8"):
        read_source(_config(str(source)))


def test_empty_feature_collection_is_hard_error(tmp_path: Path) -> None:
    source = tmp_path / "empty.geojson"
    source.write_text(
        json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8"
    )
    with pytest.raises(RegionImportError, match="没有任何 Feature"):
        read_source(_config(str(source)))
