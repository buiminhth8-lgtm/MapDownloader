"""GeoJSON 边界加载与校验（Slice 16，Spec §4 / §23）。

覆盖三件事：

1. **四种顶层形态都能读**：``Feature`` / ``FeatureCollection`` / 裸
   ``Polygon`` / ``MultiPolygon``（外加混着点的 ``GeometryCollection``）；
2. **每种坏数据都有一句说得清原因的异常**：非法 JSON、缺 ``type``、
   顶层类型不支持、几何缺失、features 为空、不含面、自相交、
   跨国际日期变更线、纬度越界；
3. **绝不静默修复**（Spec §32）：自相交几何默认必须报错，
   只有显式 ``attempt_repair=True`` 才放行。

夹具全部是**自建合成图形**（``tests/fixtures/regions/``），不含任何真实
行政区数据 —— 真实边界必须由用户自行提供合法授权文件（见仓库 README）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shapely.geometry import box

from app.config import settings
from app.geo import (
    GeoBounds,
    GeoJSONDocument,
    GeoJSONGeometryError,
    GeoJSONNotFoundError,
    GeoJSONParseError,
    geometry_to_bounds,
    load_geojson_document,
    load_geojson_geometry,
)

#: 夹具目录。用 ``__file__`` 推导而不是相对 cwd —— 从仓库根跑 pytest 与
#: 从任意目录跑 ``pytest tests/xxx.py`` 必须都找得到。
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "regions"


def fixture(name: str) -> Path:
    """取一个夹具文件路径，顺便断言它真的在（改名后忘记改用例会立刻红）。"""
    path = FIXTURES / name
    assert path.is_file(), f"夹具缺失：{path}"
    return path


# --------------------------------------------------------------------------- #
# 正常路径：四种顶层形态
# --------------------------------------------------------------------------- #
def test_feature_with_polygon_loads_geometry_and_properties() -> None:
    doc = load_geojson_document(fixture("square.geojson"))

    assert isinstance(doc, GeoJSONDocument)
    assert doc.geojson_type == "Feature"
    assert doc.geometry.geom_type == "Polygon"
    assert doc.name == "测试方框"
    assert doc.region_id == "test-square"
    assert doc.boundary_version == "2026.09-fixture"
    # 1 度 × 1 度的方框，面积就是 1（度²）。**算出来而不是写死**：
    # 夹具的角点一变，这里必须跟着变，而不是悄悄放过。
    assert doc.geometry.area == pytest.approx(1.0)
    assert doc.source.endswith("square.geojson")


def test_bare_polygon_has_no_properties_but_still_loads() -> None:
    doc = load_geojson_document(fixture("bare_polygon.geojson"))

    assert doc.geojson_type == "Polygon"
    assert doc.geometry.geom_type == "Polygon"
    # 裸几何没有 properties 可收集：id / 版本由调用方（RegionPreset）补
    assert doc.properties == {}
    assert doc.name == ""
    assert doc.region_id == ""
    assert doc.boundary_version == ""


def test_feature_collection_is_merged_into_one_geometry() -> None:
    doc = load_geojson_document(fixture("feature_collection.geojson"))

    assert doc.geojson_type == "FeatureCollection"
    # 两个不相邻的方框合并后一定是 MultiPolygon；若被合成一个 Polygon，
    # 说明合并逻辑把两块地连起来了 —— 那会让中间那片区域被误判为"区域内"。
    assert doc.geometry.geom_type == "MultiPolygon"
    assert len(doc.geometry.geoms) == 2
    assert doc.geometry.area == pytest.approx(2.0)
    # 两个 feature 的 properties 会合并，后者覆盖前者；未被覆盖的保留
    assert doc.name == "测试方框 B"
    assert doc.boundary_version == "2026.09-fixture"


def test_multipolygon_geometry_loads_as_multi() -> None:
    doc = load_geojson_document(fixture("multipolygon.geojson"))

    assert doc.geojson_type == "MultiPolygon"
    assert doc.geometry.geom_type == "MultiPolygon"
    assert doc.geometry.area == pytest.approx(2.0)


def test_geometry_collection_keeps_only_areal_parts() -> None:
    """``GeometryCollection`` 里的点必须被丢掉。

    放行点会得到一个荒唐的结果："瓦片与点相交"。点没有面积，对"哪些瓦片
    落在区域内"这个问题没有答案。
    """
    doc = load_geojson_document(fixture("geometry_collection.geojson"))

    assert doc.geometry.geom_type == "Polygon"
    assert doc.geometry.area == pytest.approx(1.0)


def test_load_geojson_geometry_is_the_thin_wrapper() -> None:
    doc = load_geojson_document(fixture("square.geojson"))
    geometry = load_geojson_geometry(fixture("square.geojson"))

    assert geometry.equals(doc.geometry)
    assert geometry is not doc.geometry  # 各自读盘一次，不是同一个对象


# --------------------------------------------------------------------------- #
# 坏数据：每种都要有自己的异常类型与人话
# --------------------------------------------------------------------------- #
def test_missing_file_reports_the_expected_path() -> None:
    """文件不存在时，错误信息里必须有**用户要放文件的那个路径**。

    Spec §3：报文之外还要给"往哪放"。只说"文件不存在"，用户只能猜。
    """
    missing = FIXTURES / "definitely-not-here.geojson"

    with pytest.raises(GeoJSONNotFoundError) as excinfo:
        load_geojson_document(missing)

    message = str(excinfo.value)
    assert str(missing) in message
    # 提示语来自 settings，测试读同一份常量而不是抄一份文案
    assert settings.REGION_BOUNDARY_MISSING_HINT.split("{")[0] in message


def test_broken_json_is_a_parse_error() -> None:
    with pytest.raises(GeoJSONParseError) as excinfo:
        load_geojson_document(fixture("broken_json.geojson"))

    assert "不是合法 JSON" in str(excinfo.value)


def test_missing_top_level_type_is_a_parse_error() -> None:
    with pytest.raises(GeoJSONParseError) as excinfo:
        load_geojson_document(fixture("no_type.geojson"))

    assert "type" in str(excinfo.value)


def test_unsupported_top_level_type_is_a_parse_error() -> None:
    """``Topology`` 是 TopoJSON 的东西，不是 GeoJSON。"""
    with pytest.raises(GeoJSONParseError) as excinfo:
        load_geojson_document(fixture("unsupported_type.geojson"))

    message = str(excinfo.value)
    assert "Topology" in message
    # 报错要顺便说清"支持哪些"，否则用户只能靠试
    assert "FeatureCollection" in message


def test_feature_without_geometry_is_rejected() -> None:
    """``geometry: null`` 的 feature 被跳过，于是**没有任何面留下** → 报错。

    走的是"不含任何面几何"这条分支（``null`` 几何在采集阶段就被跳过，
    不会走到"文件里没有几何"那条）。两条分支的文案不同，用例名也要能区分开，
    否则哪天分支被合并了就没人知道。
    """
    with pytest.raises(GeoJSONGeometryError) as excinfo:
        load_geojson_document(fixture("missing_geometry.geojson"))

    assert "不含任何面几何" in str(excinfo.value)


def test_empty_feature_collection_is_rejected() -> None:
    """``features: []`` 走的是"文件里没有几何"这条分支。"""
    with pytest.raises(GeoJSONGeometryError) as excinfo:
        load_geojson_document(fixture("empty_features.geojson"))

    assert "没有任何几何" in str(excinfo.value)


@pytest.mark.parametrize("name", ["point_only.geojson", "line_string.geojson"])
def test_non_areal_geometry_is_rejected(name: str) -> None:
    """点与线必须被拒绝，且理由要写出来（不是一句"格式不对"）。"""
    with pytest.raises(GeoJSONGeometryError) as excinfo:
        load_geojson_document(fixture(name))

    message = str(excinfo.value)
    assert "不含任何面" in message
    assert "Polygon" in message


def test_bowtie_geometry_is_rejected_by_default() -> None:
    """自相交几何默认**不修复**（Spec §32）。

    悄悄 ``buffer(0)`` 抹平的后果是：用户拿到一份"看起来能用"的边界，
    但它的 ``intersects`` 结果是不可信的 —— 少下或者多下一片，没人看得出来。
    """
    with pytest.raises(GeoJSONGeometryError) as excinfo:
        load_geojson_document(fixture("bowtie.geojson"))

    message = str(excinfo.value)
    assert "几何非法" in message
    # 必须告诉用户"是数据源要修"，并给出显式放行的开关
    assert "attempt_repair" in message


def test_bowtie_geometry_can_be_repaired_when_explicitly_asked() -> None:
    """显式要求时才修复；修复后必须是合法非空几何。"""
    doc = load_geojson_document(fixture("bowtie.geojson"), attempt_repair=True)

    assert doc.geometry.is_valid
    assert not doc.geometry.is_empty
    # 修复是"消掉自相交"，不是凭空造地：结果必然**落在原图形的外包框之内**，
    # 而面积严格小于外包框的 1.0（实测 buffer(0) 只留下右半边三角形）。
    # 写死一个具体数字没有意义（它随 shapely 版本与环的方向会变），
    # "没有长出框外、也没有占满整个框"才是这件事的**定义**。
    envelope = box(116.0, 39.0, 117.0, 40.0)
    assert envelope.covers(doc.geometry)
    assert 0.0 < doc.geometry.area < envelope.area


def test_antimeridian_geometry_is_rejected() -> None:
    """跨国际日期变更线的数据必须报错，**不能**静默取模。

    经纬度取模后的瓦片编号会完全错误 —— 一个"下东南亚结果下到了南美"
    的 bug，靠肉眼几乎不可能在日志里发现。
    """
    with pytest.raises(GeoJSONGeometryError) as excinfo:
        geometry_to_bounds(load_geojson_geometry(fixture("across_antimeridian.geojson")))

    assert "边界范围无法用于瓦片计算" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# geometry_to_bounds
# --------------------------------------------------------------------------- #
def test_geometry_to_bounds_round_trips_the_fixture_bbox() -> None:
    bounds = geometry_to_bounds(load_geojson_geometry(fixture("square.geojson")))

    assert bounds == GeoBounds(west=116.0, south=39.0, east=117.0, north=40.0)


def test_polar_geometry_is_clamped_to_mercator_limit() -> None:
    """纬度 ±89 → clamp 到墨卡托极限（与 ``lat_to_tile_y`` 对极点一致）。

    不 clamp 的话，±90 的边界会喂给瓦片公式一个无穷大，下游拿到的是一堆
    看似正常的数字（或干脆崩掉）。
    """
    bounds = geometry_to_bounds(load_geojson_geometry(fixture("polar.geojson")))

    assert bounds.south == pytest.approx(-settings.WEB_MERCATOR_MAX_LAT)
    assert bounds.north == pytest.approx(settings.WEB_MERCATOR_MAX_LAT)
    # 经度不参与 clamp
    assert bounds.west == -10.0
    assert bounds.east == 10.0


def test_geometry_to_bounds_rejects_empty_geometry() -> None:
    from shapely.geometry import Polygon

    with pytest.raises(GeoJSONGeometryError):
        geometry_to_bounds(Polygon())


# --------------------------------------------------------------------------- #
# 编码
# --------------------------------------------------------------------------- #
def test_utf8_is_required_and_cp936_would_fail(work_dir: Path) -> None:
    """中文属性必须按 UTF-8 读。

    中文 Windows 上默认编码是 GBK：用平台默认编码读一份 UTF-8 的边界文件，
    要么直接 ``UnicodeDecodeError``，要么读出乱码区域名 —— 后者更糟，
    因为它在界面上看起来"只是名字怪怪的"。
    """
    path = work_dir / "gbk.geojson"
    payload = {
        "type": "Polygon",
        "coordinates": [[[116.0, 39.0], [117.0, 39.0], [117.0, 40.0], [116.0, 39.0]]],
    }
    # 显式写成 GBK：里面塞一个 GBK 独有、UTF-8 里非法的字节序列
    path.write_bytes(
        json.dumps(payload, ensure_ascii=True).encode("ascii")
        + "\n// 注释：中文".encode("gbk")
    )

    with pytest.raises(GeoJSONParseError):
        # GBK 的"中文"字节序列在 UTF-8 下非法 → 必须先撞上解码错误
        load_geojson_document(path)


def test_utf8_chinese_properties_survive_a_round_trip(work_dir: Path) -> None:
    """反例：真正的 UTF-8 中文属性必须原样读出来（不能变成乱码）。"""
    path = work_dir / "utf8.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {"name": "测试方框", "id": "test-square"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[116.0, 39.0], [117.0, 39.0], [117.0, 40.0], [116.0, 39.0]]
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    doc = load_geojson_document(path)

    assert doc.name == "测试方框"
    assert doc.region_id == "test-square"
