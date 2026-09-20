"""区域加载器：路径安全、GeoJSON 加载、构造 DownloadRegion（Slice 18，Spec §4 / §11 / §12）。

这一层是"目录条目"与"能下载的区域"之间唯一的通道，因此它同时扛着三件事：

1. **路径安全**：只允许资源目录内的相对路径（Spec §12）。危害很具体——
   一个 ``../../`` 就能让"读行政区边界"变成"读任意文件"。
2. **边界缺失要说人话**：告诉用户文件叫什么、放哪、要什么授权，
   而**绝不**退化成按外包矩形下载（那会静默下一大片区域外的瓦片）。
3. **范围从几何算**：目录里那个可选的 ``bounds`` 只是声明，真实范围一律来自
   GeoJSON（Spec §11 明令禁止拿手填的值顶替）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.geo import CHINA_REGION_PRESET, GeoBounds, RegionBoundaryMissing, RegionType
from app.geo.geojson_loader import GeoJSONError
from app.planning import RegionLevel
from app.regions import RegionCatalog, RegionLoader, RegionPreset

OTHER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "regions"


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _catalog_with(root: Path, *entries: RegionPreset) -> RegionCatalog:
    return RegionCatalog(entries, root=root)


def _entry(region_id: str, relative: str, *, level=RegionLevel.CITY, name="示例区") -> RegionPreset:
    return RegionPreset(id=region_id, name=name, level=level, geojson_path=relative)


def _install(root: Path, relative: str, source_name: str) -> RegionPreset:
    """把 ``tests/fixtures/regions/<source_name>`` 装到 ``root/<relative>`` 下。"""
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(OTHER_FIXTURES / source_name, target)
    return _entry(Path(relative).stem, relative)


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def test_peek_path_points_into_the_resource_root(region_loader: RegionLoader, region_root: Path) -> None:
    """算路径不该要求文件存在 —— UI 在"文件还没放"时就要把位置说清楚。"""
    path = region_loader.peek_path("rivertown")

    assert path == (region_root / "city" / "rivertown.geojson").resolve()
    assert path.is_file()


def test_peek_path_works_even_when_the_file_is_missing(
    region_loader: RegionLoader, region_root: Path
) -> None:
    (region_root / "city" / "rivertown.geojson").unlink()

    assert region_loader.peek_path("rivertown").name == "rivertown.geojson"
    assert region_loader.available("rivertown") is False


def test_resolve_path_raises_a_speakable_error_when_missing(
    region_loader: RegionLoader, region_root: Path
) -> None:
    (region_root / "city" / "rivertown.geojson").unlink()

    with pytest.raises(RegionBoundaryMissing) as excinfo:
        region_loader.resolve_path("rivertown")

    message = str(excinfo.value)
    assert "rivertown.geojson" in message, "要告诉用户文件叫什么"
    assert str(region_root) in message, "要说清放哪"
    assert "审图" in message or "授权" in message, "要提授权要求"
    assert excinfo.value.path.endswith("rivertown.geojson")


def test_unknown_region_id_is_a_catalog_error(region_loader: RegionLoader) -> None:
    from app.regions import RegionCatalogError

    with pytest.raises(RegionCatalogError, match="hangzhou"):
        region_loader.peek_path("hangzhou")


def test_a_path_escaping_the_root_is_refused_before_any_io() -> None:
    """越界路径在**构造条目**时就被拒 —— 连目录都进不来，更不会走到读盘。"""
    with pytest.raises(ValueError, match="不安全"):
        RegionPreset(id="evil", name="坏", geojson_path="../../secret.geojson")


def test_peek_path_rejects_a_tampered_path(region_root: Path) -> None:
    """就算有人绕过模型校验塞了个坏路径，加载器也要拦住（第二道闸）。"""
    good = _entry("ok", "city/rivertown.geojson")
    tampered = RegionPreset(
        id="ok", name="示例区", level=RegionLevel.CITY, geojson_path="city/rivertown.geojson"
    )
    # 用 object.__setattr__ 模拟"有人直接把字段改了"（frozen 才有这道门槛）
    object.__setattr__(tampered, "geojson_path", "../escape.geojson")
    catalog = _catalog_with(region_root, good, tampered)

    with pytest.raises(ValueError, match="不安全"):
        RegionLoader(catalog).peek_path("ok")


def test_service_keeps_the_file_inside_the_root(region_loader: RegionLoader, region_root: Path) -> None:
    """正常路径解析出来的文件必然在资源目录内（Spec §12 的兜底断言）。"""
    path = region_loader.resolve_path("demoland")

    assert region_root.resolve() in path.parents


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #
def test_load_document_reads_geometry_and_properties(region_loader: RegionLoader) -> None:
    document = region_loader.load_document("rivertown")

    assert document.geometry.geom_type == "Polygon"
    assert document.name == "示例河市"
    assert document.boundary_version == "2026-09-fixture"
    assert document.source.endswith("rivertown.geojson")


def test_load_bounds_is_the_geometry_bounds(region_loader: RegionLoader) -> None:
    bounds = region_loader.load_bounds("rivertown")

    assert (bounds.west, bounds.south, bounds.east, bounds.north) == (110.0, 33.0, 111.0, 34.0)


def test_bounds_are_recomputed_from_disk_not_cached(region_loader: RegionLoader, region_root: Path) -> None:
    """换掉边界文件之后，范围必须跟着变（Spec §11 末句：缓存要有失效办法）。

    本层选择**根本不缓存**：估算前本来就要加载几何（prepared + intersects），
    缓存只在"只想知道范围"这条路上省一次读盘，却引入一份可能过期的数据。
    """
    first = region_loader.load_bounds("rivertown")
    assert first.east == 111.0

    payload = json.loads((region_root / "city" / "rivertown.geojson").read_text(encoding="utf-8"))
    payload["geometry"]["coordinates"] = [
        [[110.0, 33.0], [110.5, 33.0], [110.5, 33.5], [110.0, 33.5], [110.0, 33.0]]
    ]
    (region_root / "city" / "rivertown.geojson").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    second = region_loader.load_bounds("rivertown")
    assert second.east == 110.5, "读到的是新文件，不是上一次的缓存"


@pytest.mark.parametrize(
    "source, expected",
    [
        ("multipolygon.geojson", "Polygon|MultiPolygon"),
        ("feature_collection.geojson", "Polygon|MultiPolygon"),
        ("bare_polygon.geojson", "Polygon"),
    ],
)
def test_various_geojson_shapes_are_supported(
    region_root: Path, source: str, expected: str
) -> None:
    """目录层不挑 GeoJSON 的形态：加载器认的它都认（复用 Slice 16 的实现）。"""
    import re

    entry = _install(region_root, "province/thing.geojson", source)
    loader = RegionLoader(_catalog_with(region_root, entry))

    geometry = loader.load_geometry("thing")
    assert re.fullmatch(expected, geometry.geom_type)


@pytest.mark.parametrize("source", ["broken_json.geojson", "point_only.geojson", "no_type.geojson"])
def test_broken_boundary_files_raise_geojson_errors(region_root: Path, source: str) -> None:
    entry = _install(region_root, "province/thing.geojson", source)
    loader = RegionLoader(_catalog_with(region_root, entry))

    with pytest.raises(GeoJSONError):
        loader.load_document("thing")


def test_bowtie_can_be_loaded_only_when_asked_to_repair(region_root: Path) -> None:
    """自相交几何默认拒收；显式要求修复才放行（Slice 16 的既定姿态）。"""
    entry = _install(region_root, "province/thing.geojson", "bowtie.geojson")
    loader = RegionLoader(_catalog_with(region_root, entry))

    with pytest.raises(GeoJSONError):
        loader.load_document("thing")
    assert loader.load_document("thing", attempt_repair=True) is not None


# --------------------------------------------------------------------------- #
# DownloadRegion
# --------------------------------------------------------------------------- #
def test_to_download_region_from_the_file(region_loader: RegionLoader) -> None:
    region = region_loader.to_download_region("rivertown")

    assert region.region_type is RegionType.POLYGON
    assert region.is_polygon is True
    assert region.name == "示例河市"
    assert region.region_id == "rivertown"
    assert region.region_level is RegionLevel.CITY
    assert region.boundary_version == "2026-09-fixture", "目录没写版本时用文件自带的"
    assert region.geojson_path is not None and region.geojson_path.endswith("rivertown.geojson")
    assert (region.bounds.west, region.bounds.south) == (110.0, 33.0)


def test_to_download_region_prefers_the_catalog_version(region_root: Path) -> None:
    """目录里写了版本就以目录为准（部署方认定"这批数据是哪版"）。"""
    entry = RegionPreset(
        id="rivertown",
        name="示例河市",
        level=RegionLevel.CITY,
        geojson_path="city/rivertown.geojson",
        boundary_version="2026-09-catalog",
    )
    loader = RegionLoader(_catalog_with(region_root, entry))

    assert loader.to_download_region("rivertown").boundary_version == "2026-09-catalog"


def test_to_download_region_does_not_read_the_file_when_bounds_are_given(
    region_loader: RegionLoader, region_root: Path, monkeypatch
) -> None:
    """已经算好范围时不再读盘 —— 面板为了显示范围已经读过一次了。"""
    from app.geo import geojson_loader
    from app.regions import loader as loader_module

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("给了 bounds 就不该再读文件")

    monkeypatch.setattr(loader_module, "load_geojson_document", boom)
    monkeypatch.setattr(geojson_loader, "load_geojson_document", boom)

    region = region_loader.to_download_region(
        "rivertown", bounds=GeoBounds(110.0, 33.0, 111.0, 34.0)
    )

    assert region.bounds.east == 111.0


def test_to_download_region_reuses_a_loaded_document(region_loader: RegionLoader, monkeypatch) -> None:
    from app.geo import geojson_loader
    from app.regions import loader as loader_module

    document = region_loader.load_document("rivertown")

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("复用已有文档时不该再读文件")

    monkeypatch.setattr(loader_module, "load_geojson_document", boom)
    monkeypatch.setattr(geojson_loader, "load_geojson_document", boom)

    region = region_loader.to_download_region("rivertown", document=document)

    assert region.boundary_version == "2026-09-fixture"
    assert region.bounds.west == 110.0


def test_to_download_region_reuses_a_loaded_geometry(region_loader: RegionLoader) -> None:
    geometry = region_loader.load_geometry("rivertown")

    region = region_loader.to_download_region("rivertown", geometry=geometry)

    assert (region.bounds.west, region.bounds.north) == (110.0, 34.0)


def test_to_download_region_can_override_the_name(region_loader: RegionLoader) -> None:
    region = region_loader.to_download_region("rivertown", name="临时作业区")

    assert region.name == "临时作业区"
    assert region.region_id == "rivertown", "名字只是展示，id 仍然来自目录"


def test_to_download_region_raises_before_touching_the_disk_when_missing(
    region_loader: RegionLoader, region_root: Path
) -> None:
    (region_root / "city" / "rivertown.geojson").unlink()

    with pytest.raises(RegionBoundaryMissing):
        region_loader.to_download_region("rivertown")


def test_download_region_feeds_the_estimator(region_loader: RegionLoader) -> None:
    """Loader 与 Slice 16 的估算链路接得上（Spec §16 的 Integration）。"""
    from app.geo import RegionTileEstimator

    region = region_loader.to_download_region("rivertown")
    estimate = RegionTileEstimator(region).estimate(12, 12)

    assert estimate.selected_tiles > 0
    assert estimate.selected_tiles <= estimate.candidate_tiles


# --------------------------------------------------------------------------- #
# 目录条目 → 扁平预设（两个同名模型的唯一转换方向）
# --------------------------------------------------------------------------- #
def test_to_geo_preset_carries_root_and_relative_path(region_loader: RegionLoader, region_root: Path) -> None:
    preset = region_loader.geo_preset("rivertown")

    assert preset.region_id == "rivertown"
    assert preset.name == "示例河市"
    assert preset.region_level is RegionLevel.CITY
    assert preset.geojson_filename == "city/rivertown.geojson"
    assert preset.geojson_path == (region_root / "city" / "rivertown.geojson").resolve()
    assert preset.available is True


def test_geo_preset_missing_message_matches_the_loader(
    region_loader: RegionLoader, region_root: Path
) -> None:
    """同一个区域在两条路径上被问到（面板走目录、续传走扁平预设）要说同一句话。"""
    (region_root / "city" / "rivertown.geojson").unlink()

    assert region_loader.missing_message("rivertown") == region_loader.geo_preset(
        "rivertown"
    ).missing_message()


def test_geo_preset_of_china_keeps_the_slice16_shape() -> None:
    """Slice 16 的扁平预设仍然可用（放行子目录路径，其余一字未改）。"""
    assert CHINA_REGION_PRESET.region_id == "china"
    assert CHINA_REGION_PRESET.region_level is RegionLevel.COUNTRY
    assert CHINA_REGION_PRESET.geojson_filename == "country/china.geojson"
    assert CHINA_REGION_PRESET.geojson_path.name == "china.geojson"
    assert CHINA_REGION_PRESET.geojson_path.parent.name == "country"
