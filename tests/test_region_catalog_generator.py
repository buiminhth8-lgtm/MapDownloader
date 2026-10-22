"""CatalogGenerator：单区域 GeoJSON / index.json / manifest（Slice 26，Spec §10 ~ §14 / §26 Generator 部分）。

守四组输出契约：

1. **单 Region GeoJSON**（Spec §11）：顶层 Feature、properties 六件套
   （id/name/level/parentId/code/boundaryVersion）、UTF-8 无转义；
2. **index.json**（Spec §12）：Slice 18 认得的同一套键、bounds 来自几何
   （且与深度校验的 1e-9 容差相容 —— 不能四舍五入）、**一个 Zoom 字段
   都没有**；
3. **dataset manifest**（Spec §14）：formatVersion / boundaryVersion /
   sourceName / createdAt / 四个计数 / 授权信息，且**不含敏感信息**；
4. **原样保留**（Spec §6）：没被修复 / 简化的几何写回源数据的原始坐标。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.regions.catalog import INDEX_FILENAME, RegionCatalog
from app.regions.importer import (
    RegionImportConfig,
    build_hierarchy,
    generate_dataset,
    normalize_features,
    read_source,
)
from app.regions.importer.models import DATASET_MANIFEST_FILENAME
from tests.conftest import REGION_IMPORT_FIXTURE_ROOT


def _generate(tmp_path: Path, source: str = "sample.geojson", **kwargs) -> tuple:
    config = RegionImportConfig(
        source_file=REGION_IMPORT_FIXTURE_ROOT / source,
        id_field="code",
        name_field="name",
        level_field="level",
        parent_field="parent",
        output_root=tmp_path / "generated",
        boundary_version=kwargs.pop("boundary_version", "2026-10-test"),
        **kwargs,
    )
    features, issues = read_source(config)
    alive = normalize_features(features, issues=issues)
    entries = build_hierarchy(alive, config, issues=issues)
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    generated = generate_dataset(entries, config, build_dir)
    return generated, config, entries


# --------------------------------------------------------------------------- #
# 目录布局（Spec §10）
# --------------------------------------------------------------------------- #
def test_layout_matches_the_documented_tree(tmp_path: Path) -> None:
    generated, _config, _entries = _generate(tmp_path)

    assert (generated.root / INDEX_FILENAME).is_file()
    assert (generated.root / DATASET_MANIFEST_FILENAME).is_file()
    assert (generated.root / "country" / "test_country.geojson").is_file()
    assert (generated.root / "province" / "test_province_a.geojson").is_file()
    assert (generated.root / "province" / "test_province_b.geojson").is_file()
    assert (generated.root / "city" / "test_city_a1.geojson").is_file()
    assert (generated.root / "city" / "test_city_a2.geojson").is_file()
    assert generated.geojson_count == 5


# --------------------------------------------------------------------------- #
# 单 Region GeoJSON（Spec §11）
# --------------------------------------------------------------------------- #
def test_single_region_geojson_is_a_feature_with_full_properties(tmp_path: Path) -> None:
    generated, _config, _entries = _generate(tmp_path)

    payload = json.loads(
        (generated.root / "province" / "test_province_a.geojson").read_text(
            encoding="utf-8"
        )
    )
    assert payload["type"] == "Feature"
    assert payload["properties"] == {
        "id": "test_province_a",
        "name": "测试甲省",
        "level": "province",
        "parentId": "test_country",
        "code": "test_province_a",
        "boundaryVersion": "2026-10-test",
    }
    assert payload["geometry"]["type"] == "Polygon"
    # UTF-8、ensure_ascii=False：文件里是中文本身，不是 \uXXXX。
    raw = (generated.root / "province" / "test_province_a.geojson").read_text(
        encoding="utf-8"
    )
    assert "测试甲省" in raw
    assert "\\u" not in raw


def test_original_code_is_kept_when_id_was_sanitized(tmp_path: Path) -> None:
    generated, _config, _entries = _generate(tmp_path, "sanitize_ids.geojson")

    # 纯中文 code → 哈希 id，原值进 originalCode。
    hashed = list((generated.root / "province").glob("*.geojson"))
    originals = set()
    for path in hashed:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["properties"].get("originalCode"):
            originals.add(payload["properties"]["originalCode"])
    assert originals == {"浙江省", "江苏省/南京市", ".."}


def test_geometry_is_written_back_verbatim_when_untouched(tmp_path: Path) -> None:
    """没被修复 / 简化的几何：写回源数据的原始坐标字典（Spec §6）。"""
    generated, _config, _entries = _generate(tmp_path)

    source = json.loads(
        (REGION_IMPORT_FIXTURE_ROOT / "sample.geojson").read_text(encoding="utf-8")
    )
    raw_city = next(
        item
        for item in source["features"]
        if item["properties"]["code"] == "test_city_a1"
    )["geometry"]
    written = json.loads(
        (generated.root / "city" / "test_city_a1.geojson").read_text(encoding="utf-8")
    )
    assert written["geometry"] == raw_city


# --------------------------------------------------------------------------- #
# index.json（Spec §12）
# --------------------------------------------------------------------------- #
def test_index_json_is_loadable_by_the_slice18_catalog(tmp_path: Path) -> None:
    generated, _config, _entries = _generate(tmp_path)

    catalog = RegionCatalog.load(root=generated.root)
    assert len(catalog) == 5
    assert [item.id for item in catalog.list_children(None)] == ["test_country"]
    assert sorted(item.id for item in catalog.list_children("test_country")) == [
        "test_province_a",
        "test_province_b",
    ]
    assert sorted(item.id for item in catalog.list_children("test_province_a")) == [
        "test_city_a1",
        "test_city_a2",
    ]


def test_index_bounds_come_from_geometry_with_full_precision(tmp_path: Path) -> None:
    """bounds 禁止人工填写；且必须是精确值（目录深度校验容差 1e-9）。"""
    generated, _config, _entries = _generate(tmp_path)

    payload = json.loads((generated.root / INDEX_FILENAME).read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in payload["regions"]}
    city = by_id["test_city_a1"]
    assert city["bounds"] == {"west": 110.0, "south": 30.0, "east": 111.0, "north": 31.0}
    # 顶层版本号 + 每条记录的 boundaryVersion 都在。
    assert payload["version"] == 1
    assert payload["boundaryVersion"] == "2026-10-test"
    assert all(
        item["boundaryVersion"] == "2026-10-test" for item in payload["regions"]
    )


def test_index_json_contains_no_zoom_fields(tmp_path: Path) -> None:
    """Spec §22 / §29：目录里一个 Zoom 字段都不许有。"""
    generated, _config, _entries = _generate(tmp_path)

    text = (generated.root / INDEX_FILENAME).read_text(encoding="utf-8")
    for key in ("zoom", "minZoom", "maxZoom", "min_zoom", "max_zoom"):
        assert key not in text, f"index.json 里不该出现 {key}"


def test_deep_validation_of_generated_catalog_passes(tmp_path: Path) -> None:
    from app.regions.service import RegionService

    generated, _config, _entries = _generate(tmp_path)
    service = RegionService(root=generated.root)

    result = service.validate(deep=True)
    assert result.ok, result.as_lines()


# --------------------------------------------------------------------------- #
# dataset manifest（Spec §14）
# --------------------------------------------------------------------------- #
def test_dataset_manifest_records_version_source_and_counts(tmp_path: Path) -> None:
    generated, _config, _entries = _generate(
        tmp_path,
        source_name="测试来源",
        source_license="测试授权",
        source_description="自建几何",
    )

    payload = json.loads(
        (generated.root / DATASET_MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    assert payload["formatVersion"] == 1
    assert payload["boundaryVersion"] == "2026-10-test"
    assert payload["sourceName"] == "测试来源"
    assert payload["sourceLicense"] == "测试授权"
    assert payload["sourceDescription"] == "自建几何"
    assert payload["regionCount"] == 5
    assert payload["countryCount"] == 1
    assert payload["provinceCount"] == 2
    assert payload["cityCount"] == 2
    assert payload["createdAt"]  # ISO 时间戳非空
    # 不记录敏感信息：整个 manifest 里没有 token / key / secret 字样。
    lowered = json.dumps(payload, ensure_ascii=False).lower()
    for word in ("token", "secret", "password", "apikey"):
        assert word not in lowered


def test_manifest_counts_reflect_municipality_fixture(tmp_path: Path) -> None:
    generated, _config, _entries = _generate(tmp_path, "municipality.geojson")
    assert generated.manifest.country_count == 1
    assert generated.manifest.province_count == 2  # 直辖市也算省级
    assert generated.manifest.city_count == 1
    assert generated.manifest.region_count == 4
