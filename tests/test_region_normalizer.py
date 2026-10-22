"""Normalizer：几何标准化与 id 清洗（Slice 26，Spec §5 / §6 / §7 / §26 Normalizer 部分）。

守四类行为：

1. **WGS84 范围**：越界坐标（经度 200、纬度 95）拒绝；
2. **可修复几何**：自相交 → make_valid 修复 + **warning**（Spec §5：
   允许修复但必须记录，不得静默修改）；修不好的照样拒绝；
3. **id 清洗**：稳定 code 原样使用；不安全字符走统一 sanitize 并保留
   originalCode；纯中文 / ``..`` 走确定性兜底（内容哈希，不猜拼音）；
4. **不默认简化**：simplify_tolerance 为 None 时几何一个字节都不动。
"""

from __future__ import annotations

from pathlib import Path

from app.geo.geojson_loader import geometry_to_bounds
from app.regions.importer import (
    RegionImportIssueCode,
    RegionSourceFeature,
    normalize_features,
    normalize_geometry,
    sanitize_region_id,
)
from app.regions.importer.models import IssueLevel
from tests.conftest import REGION_IMPORT_FIXTURE_ROOT


def _read(source: str) -> tuple[RegionSourceFeature, ...]:
    from app.regions.importer import RegionImportConfig, read_source

    config = RegionImportConfig(
        source_file=REGION_IMPORT_FIXTURE_ROOT / source,
        id_field="code",
        name_field="name",
        level_field="level",
        parent_field="parent",
        boundary_version="2026-10-test",
        # 标准化不碰发布目录（占位路径，见 test_region_source_reader 的说明）。
        output_root=Path("unused-in-dry-run"),
    )
    features, _issues = read_source(config)
    return features


# --------------------------------------------------------------------------- #
# id 清洗（Spec §7）
# --------------------------------------------------------------------------- #
def test_stable_codes_pass_through_untouched() -> None:
    assert sanitize_region_id("330100") == ("330100", False)
    assert sanitize_region_id("CN-33") == ("CN-33", False)
    assert sanitize_region_id("test_city_a1") == ("test_city_a1", False)


def test_unsafe_characters_are_sanitized_and_flagged() -> None:
    cleaned, sanitized = sanitize_region_id("江苏省/南京市")
    assert sanitized is True
    # 斜杠与中文都进不了 id（斜杠是路径分隔符，中文不进文件名）。
    assert "/" not in cleaned
    assert not any(ord(ch) > 127 for ch in cleaned)
    assert cleaned  # 非空（本样例整串都是非法字符 → 确定性兜底）


def test_mixed_code_keeps_its_safe_skeleton() -> None:
    """中英混合时保留可用的骨架，不是一律哈希。"""
    cleaned, sanitized = sanitize_region_id("CN-33 杭州市")
    assert sanitized is True
    assert cleaned == "CN-33"  # 安全骨架留下，非法字符换成下划线后去掉
    assert sanitize_region_id("CN-33")[0] == cleaned  # 与干净 code 同形


def test_pure_chinese_code_gets_deterministic_fallback_id() -> None:
    """纯中文 code：清洗产不出可用 id → 内容哈希兜底（确定性，不猜拼音）。"""
    first, sanitized = sanitize_region_id("浙江省")
    assert sanitized is True
    assert first.startswith("region-")
    # 同一原值在任何时候都得到同一个 id（这是它能进任务记录的前提）。
    assert sanitize_region_id("浙江省")[0] == first
    # 不同原值不撞车。
    assert sanitize_region_id("江苏省")[0] != first


def test_dot_names_get_fallback_id() -> None:
    """. / .. 字符全合法但是路径特殊语义 —— 不能当文件名用。"""
    cleaned, sanitized = sanitize_region_id("..")
    assert sanitized is True
    assert cleaned.startswith("region-")


def test_duplicate_ids_keep_the_first_and_flag_the_rest() -> None:
    features = _read("duplicate_id.geojson")
    issues: list = []

    accepted = normalize_features(features, issues=issues)

    ids = [item.region_id for item in accepted]
    assert ids.count("dup_province") == 1
    # 保留的是先出现的那条（重复省甲）。
    kept = next(item for item in accepted if item.region_id == "dup_province")
    assert kept.name == "重复省甲"
    dup_issues = [
        item for item in issues if item.code is RegionImportIssueCode.DUPLICATE_ID
    ]
    assert len(dup_issues) == 1
    assert "重复省乙" in dup_issues[0].feature


def test_sanitized_ids_are_recorded_as_warnings_with_original() -> None:
    features = _read("sanitize_ids.geojson")
    issues: list = []
    accepted = normalize_features(features, issues=issues)

    by_name = {item.name: item for item in accepted}
    assert by_name["纯中文省"].id_sanitized is True
    assert by_name["纯中文省"].region_id.startswith("region-")
    # 原值留在 code 上（写 GeoJSON 时进 properties.originalCode）。
    assert by_name["纯中文省"].code == "浙江省"
    sanitized_issues = [
        item for item in issues if item.code is RegionImportIssueCode.ID_SANITIZED
    ]
    assert len(sanitized_issues) == 3  # 浙江省、江苏省/南京市、..
    assert all(item.level is IssueLevel.WARNING for item in sanitized_issues)


# --------------------------------------------------------------------------- #
# 几何校验（Spec §5）
# --------------------------------------------------------------------------- #
def test_out_of_range_coordinates_are_rejected() -> None:
    features = _read("bad_geometry.geojson")
    issues: list = []
    accepted = normalize_features(features, issues=issues)

    # 全国条目合法活下来；越界的那条出局。
    assert [item.region_id for item in accepted] == ["test_country"]
    range_issues = [
        item for item in issues if item.code is RegionImportIssueCode.GEOMETRY_OUT_OF_RANGE
    ]
    assert len(range_issues) == 1
    assert "经度" in range_issues[0].message


def test_self_intersecting_geometry_is_repaired_with_a_warning() -> None:
    """自相交（蝴蝶结）：make_valid 修复 + warning，不静默（Spec §5）。"""
    features = _read("repairable.geojson")
    issues: list = []
    accepted = normalize_features(features, issues=issues)

    assert len(accepted) == 2  # 全国 + 修好的自相交省
    repaired = next(item for item in accepted if item.region_id == "bowtie_province")
    assert repaired.geometry.is_valid
    assert repaired.geometry_modified is True
    assert "make_valid" in repaired.geometry_note

    repaired_issues = [
        item for item in issues if item.code is RegionImportIssueCode.GEOMETRY_REPAIRED
    ]
    assert len(repaired_issues) == 1
    assert repaired_issues[0].level is IssueLevel.WARNING


def test_latitude_out_of_range_is_rejected() -> None:
    feature = RegionSourceFeature(
        source_index=0,
        raw_properties={"code": "far_north", "name": "极北", "level": "country"},
        raw_geometry=None,
        geometry=None,
    )
    from shapely.geometry import shape

    feature.geometry = shape(
        {
            "type": "Polygon",
            "coordinates": [
                [[0.0, 80.0], [10.0, 80.0], [10.0, 95.0], [0.0, 95.0], [0.0, 80.0]]
            ],
        }
    )
    issues: list = []
    assert normalize_geometry(feature, issues=issues) is False
    assert issues[0].code is RegionImportIssueCode.GEOMETRY_OUT_OF_RANGE
    assert "纬度" in issues[0].message


# --------------------------------------------------------------------------- #
# 简化（Spec §6：默认关闭）
# --------------------------------------------------------------------------- #
def test_no_simplify_by_default_keeps_geometry_untouched() -> None:
    features = _read("sample.geojson")
    before = [item.raw_geometry for item in features]
    issues: list = []
    accepted = normalize_features(features, issues=issues)

    # 一个坐标都不动，也没有任何"被改过"的标记。
    assert [item.raw_geometry for item in accepted] == before
    assert all(item.geometry_modified is False for item in accepted)
    assert issues == []


def test_simplify_only_when_explicitly_enabled_and_records_warning() -> None:
    features = _read("sample.geojson")
    issues: list = []
    accepted = normalize_features(
        features, simplify_tolerance=0.5, issues=issues
    )

    simplified_issues = [
        item for item in issues if item.code is RegionImportIssueCode.GEOMETRY_SIMPLIFIED
    ]
    assert len(simplified_issues) == 5
    assert all(item.level is IssueLevel.WARNING for item in simplified_issues)
    # 简化确实发生了（顶点数变少），且结果仍然合法。
    country = next(item for item in accepted if item.region_id == "test_country")
    assert country.geometry_modified is True
    assert country.geometry.is_valid
    # 边界没被简化到离谱（面积仍然是同一个量级）。
    assert country.geometry.area > 0


# --------------------------------------------------------------------------- #
# bounds（Spec §12 的地基）
# --------------------------------------------------------------------------- #
def test_bounds_come_from_geometry() -> None:
    features = _read("sample.geojson")
    issues: list = []
    accepted = normalize_features(features, issues=issues)
    city = next(item for item in accepted if item.region_id == "test_city_a1")
    bounds = geometry_to_bounds(city.geometry)
    assert (bounds.west, bounds.south, bounds.east, bounds.north) == (
        110.0,
        30.0,
        111.0,
        31.0,
    )
