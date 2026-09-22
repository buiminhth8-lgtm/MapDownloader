"""米制 Buffer 构建器（Slice 22，Spec §7 / §10）—— 本 Slice 的技术核心。

这组用例里最重要的是**"按米、不按度"**那一条。它不能靠"看代码里有没有
``buffer(0.05)``"来守（那是字符串匹配，抓到的是写法不是行为），而要靠
**量出来的地面宽度**：

- 同一条航线、同一个 5000 米半径，在**北纬 20°** 与**北纬 60°** 缓冲出来的
  走廊，地面宽度必须一样；
- 而"在度数平面上 buffer 0.05"在同样两个纬度上分别得到约 10.5 km 与 5.6 km
  —— 差近一倍。

所以用例直接测"走廊的地面半宽"，并把"度数做法会得到的那个数"也一并算出来作对照。
只断言"面积变大了"是不够的：两种做法面积都会变大。
"""

from __future__ import annotations

import json
import math

import pytest
from pyproj import Geod
from shapely.geometry import LineString

from app.config.settings import (
    DEFAULT_MISSION_BUFFER_METERS,
    MISSION_BUFFER_METERS_MAX,
    MISSION_BUFFER_METERS_MIN,
    MISSION_GEOMETRY_VERSION,
)
from app.geo.geojson_loader import geometry_to_bounds
from app.mission import (
    MissionRegionBuilder,
    MissionRegionError,
    MissionRouteError,
    MissionWaypoint,
    region_geojson_document,
    route_geojson_document,
)

GEOD = Geod(ellps="WGS84")

HORIZONTAL = [(120.00, 30.00), (120.05, 30.02), (120.10, 30.01)]
VERTICAL_AT_20 = [(120.00, 20.00), (120.00, 20.10)]
VERTICAL_AT_60 = [(120.00, 60.00), (120.00, 60.10)]


def builder() -> MissionRegionBuilder:
    return MissionRegionBuilder()


def geodesic_span(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两点之间的椭球面距离（米）。**地面量出来的数才是判据。**"""
    return GEOD.inv(a[0], a[1], b[0], b[1])[2]


def corridor_half_width(bounds, route_lat: float) -> float:
    """水平航线走廊的**北向半宽**（地面米）。

    以航线中点所在的经度向北量到多边形上边界 —— AEQD 是等距投影，
    沿半径方向的这个距离**恰好**等于缓冲半径（这也是本用例的判据）。
    """
    lon_mid = (bounds.west + bounds.east) / 2
    return geodesic_span((lon_mid, route_lat), (lon_mid, bounds.north))


def corridor_width_east_west(bounds) -> float:
    """垂直航线走廊的**东西向宽度**（地面米）。"""
    lat_mid = (bounds.south + bounds.north) / 2
    return geodesic_span((bounds.east, lat_mid), (bounds.west, lat_mid))


def degree_buffer_width(route, degrees: float) -> float:
    """"在 WGS84 上 buffer 一个度数"会得到的宽度（米）—— **反面参照**。

    刻意在用例里把它算出来：只说"我们的实现对"证明不了"另一种做法错"，
    而这条边界正是本 Slice 存在的全部理由。
    """
    polygon = LineString(route).buffer(degrees)
    lat_mid = (polygon.bounds[1] + polygon.bounds[3]) / 2
    return geodesic_span(
        (polygon.bounds[2], lat_mid), (polygon.bounds[0], lat_mid)
    )


# --------------------------------------------------------------------------- #
# 米制，不是度制
# --------------------------------------------------------------------------- #
def test_same_radius_gives_the_same_ground_width_at_different_latitudes() -> None:
    """同一个 5 km 半径，在北纬 20° 与 60° 得到同样的地面半宽（±0.5%）。"""
    b = builder()
    low = b.build_geometry(VERTICAL_AT_20, 5000.0)
    high = b.build_geometry(VERTICAL_AT_60, 5000.0)

    width_low = corridor_width_east_west(low.bounds)
    width_high = corridor_width_east_west(high.bounds)

    assert width_low == pytest.approx(10_000.0, rel=0.01)
    assert width_high == pytest.approx(10_000.0, rel=0.01)
    assert abs(width_low - width_high) < 0.005 * 10_000.0


def test_north_south_half_width_equals_the_radius_in_meters() -> None:
    """水平航线的北向半宽 == 缓冲半径（米），在南北两地都成立。

    这一条同时否掉了"按度缓冲"：0.05° 的纬度差是 5.57 km，不是 5 km。
    """
    b = builder()
    for lat in (20.0, 60.0):
        geometry = b.build_geometry([(120.0, lat), (120.1, lat)], 5000.0)
        half = corridor_half_width(geometry.bounds, lat)
        assert half == pytest.approx(5000.0, rel=0.005), f"纬度 {lat} 的半宽不对：{half}"


def test_the_degree_buffer_answer_is_not_what_we_produce() -> None:
    """把"度数做法会得到的数"摆出来对照：两者必须明显不同。

    没有这条，一个"恰好在本用例的纬度上算得差不多"的错误实现也能混过去。
    """
    b = builder()
    our_width = corridor_width_east_west(b.build_geometry(VERTICAL_AT_60, 5000.0).bounds)
    naive_width = degree_buffer_width(VERTICAL_AT_60, 0.05)

    assert abs(our_width - naive_width) > 0.3 * naive_width


def test_projection_uses_a_metric_local_crs() -> None:
    """投影串里必须有 ``+units=m``（平面单位是米，这是"按米缓冲"的前提）。"""
    from app.mission.region_builder import _aeqd_crs

    crs = _aeqd_crs(120.0, 30.0)
    assert "+units=m" in crs
    assert "+proj=aeqd" in crs
    assert "+lat_0=30" in crs
    assert "+lon_0=120" in crs


# --------------------------------------------------------------------------- #
# 几何本身
# --------------------------------------------------------------------------- #
def test_result_is_a_valid_polygon() -> None:
    geometry = builder().build_geometry(HORIZONTAL, 5000.0)
    assert geometry.geometry.geom_type in {"Polygon", "MultiPolygon"}
    assert geometry.geometry.is_valid is True
    assert geometry.geometry.is_empty is False
    assert geometry.geometry.area > 0


def test_polygon_contains_the_route() -> None:
    """走廊必须**盖住航线本身**（缓冲半径再小也不能把线漏在外面）。"""
    geometry = builder().build_geometry(HORIZONTAL, 500.0)
    line = LineString(HORIZONTAL)
    assert geometry.geometry.covers(line) is True


def test_bigger_radius_makes_a_bigger_corridor() -> None:
    """500 m 与 5 km：面积必须显著变大（且不是"平移了一下"）。"""
    b = builder()
    small = b.build_geometry(HORIZONTAL, 500.0)
    large = b.build_geometry(HORIZONTAL, 5000.0)

    assert large.geometry.area > small.geometry.area
    assert large.geometry.area > small.geometry.area * 5
    # 范围也必须真的更宽（面积相等但形状不同是另一种 bug）
    assert large.bounds.east - large.bounds.west > small.bounds.east - small.bounds.west


def test_bounds_match_the_geometry() -> None:
    """``MissionGeometry.bounds`` 必须**从几何算出来**，不能是手填的。"""
    geometry = builder().build_geometry(HORIZONTAL, 5000.0)
    assert geometry.bounds == geometry_to_bounds(geometry.geometry)


def test_coordinates_are_legal_wgs84() -> None:
    """输出坐标必须落在合法经纬度区间内，且没有 NaN。"""
    geometry = builder().build_geometry(HORIZONTAL, 5000.0)
    for x, y in geometry.geometry.exterior.coords:
        assert -180.0 <= x <= 180.0
        assert -85.05112878 <= y <= 85.05112878
        assert not math.isnan(x) and not math.isnan(y)


def test_route_span_beyond_the_limit_is_rejected() -> None:
    """跨度超过 10° 的"航线"直接拒 —— 单点 AEQD 已经不适合它了。"""
    with pytest.raises(MissionRouteError) as excinfo:
        builder().build_geometry([(100.0, 20.0), (130.0, 40.0)], 5000.0)
    assert "跨度" in str(excinfo.value)


def test_dateline_crossing_route_is_rejected() -> None:
    """跨国际日期变更线的航线：范围不成立，必须拒（不能悄悄取模）。"""
    with pytest.raises(MissionRouteError):
        builder().build_geometry([(179.9, 30.0), (-179.9, 30.0)], 5000.0)


# --------------------------------------------------------------------------- #
# 半径校验
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("radius", [0.0, -1.0, float("nan"), float("inf")])
def test_non_positive_or_non_finite_radius_is_rejected(radius: float) -> None:
    with pytest.raises(MissionRegionError):
        builder().build_geometry(HORIZONTAL, radius)


def test_radius_below_the_minimum_is_rejected() -> None:
    with pytest.raises(MissionRegionError) as excinfo:
        builder().build_geometry(HORIZONTAL, MISSION_BUFFER_METERS_MIN - 1)
    assert "之间" in str(excinfo.value)


def test_radius_above_the_maximum_is_rejected() -> None:
    with pytest.raises(MissionRegionError):
        builder().build_geometry(HORIZONTAL, MISSION_BUFFER_METERS_MAX + 1)


def test_default_radius_is_used_when_not_given() -> None:
    geometry = builder().build_geometry(HORIZONTAL)
    assert geometry.buffer_meters == pytest.approx(DEFAULT_MISSION_BUFFER_METERS)


# --------------------------------------------------------------------------- #
# 版本号
# --------------------------------------------------------------------------- #
def test_version_is_stable_for_the_same_input() -> None:
    b = builder()
    first = b.build_geometry(HORIZONTAL, 5000.0)
    second = b.build_geometry(HORIZONTAL, 5000.0)
    assert first.geometry_version == second.geometry_version
    assert first.geometry_version.startswith(f"{MISSION_GEOMETRY_VERSION}:")


def test_version_changes_with_the_radius() -> None:
    b = builder()
    assert (
        b.build_geometry(HORIZONTAL, 5000.0).geometry_version
        != b.build_geometry(HORIZONTAL, 5001.0).geometry_version
    )


def test_version_changes_when_a_waypoint_moves() -> None:
    """改航点 = 新几何 = 新版本（Spec §21：绝不悄悄改一个在跑的任务的几何）。"""
    b = builder()
    moved = [(120.00, 30.00), (120.05, 30.02), (120.11, 30.01)]
    assert (
        b.build_geometry(HORIZONTAL, 5000.0).geometry_version
        != b.build_geometry(moved, 5000.0).geometry_version
    )


def test_version_ignores_sub_centimetre_float_noise() -> None:
    """浮点最后几位的抖动不该产生"新版本"（7 位小数 ≈ 1 cm 之下都归一）。"""
    b = builder()
    jittered = [(lon + 1e-10, lat - 1e-10) for lon, lat in HORIZONTAL]
    assert (
        b.build_geometry(HORIZONTAL, 5000.0).geometry_version
        == b.build_geometry(jittered, 5000.0).geometry_version
    )


# --------------------------------------------------------------------------- #
# build()：几何只算一次
# --------------------------------------------------------------------------- #
def test_build_reuses_the_supplied_geometry() -> None:
    """传了 ``geometry`` 就必须**完全**跳过投影与缓冲（Spec §23）。

    判据：故意传一份半径 1 km 的几何，同时把 ``buffer_meters`` 参数写成 9 km。
    结果里的半径必须来自**几何**（1 km）—— 说明它没有重算。
    """
    b = builder()
    prepared = b.build_geometry(HORIZONTAL, 1000.0)
    region = b.build("mission-001", "测试", HORIZONTAL, 9000.0, geometry=prepared)

    assert region.buffer_meters == pytest.approx(1000.0)
    assert region.geometry_version == prepared.geometry_version
    assert region.bounds == prepared.bounds


def test_build_returns_a_region_with_bounds_and_version() -> None:
    region = builder().build("mission-001", "西湖巡检航线", HORIZONTAL, 5000.0)
    assert region.id == "mission-001"
    assert region.waypoint_count == 3
    assert region.buffer_km == pytest.approx(5.0)
    assert region.geometry_version
    assert region.bounds_text()


def test_build_accepts_plain_lon_lat_pairs() -> None:
    region = builder().build("mission-001", "x", HORIZONTAL, 500.0)
    assert all(isinstance(point, MissionWaypoint) for point in region.waypoints)


def test_build_rejects_a_bad_mission_id() -> None:
    with pytest.raises(MissionRegionError) as excinfo:
        builder().build("downtown", "x", HORIZONTAL, 500.0)
    assert "id" in str(excinfo.value)


def test_build_rejects_an_empty_name() -> None:
    with pytest.raises(MissionRegionError):
        builder().build("mission-001", "   ", HORIZONTAL, 500.0)


# --------------------------------------------------------------------------- #
# 落盘文档
# --------------------------------------------------------------------------- #
def test_route_document_keeps_the_original_line() -> None:
    """``route.geojson`` 存的是**原始航线**（不是缓冲后的多边形）。"""
    region = builder().build("mission-001", "西湖巡检航线", HORIZONTAL, 5000.0)
    document = route_geojson_document(
        region.waypoints, mission_id=region.id, name=region.name, buffer_meters=5000.0
    )
    assert document["geometry"]["type"] == "LineString"
    assert document["geometry"]["coordinates"] == [[lon, lat] for lon, lat in HORIZONTAL]
    assert document["properties"]["id"] == "mission-001"


def test_region_document_is_polygonal_and_self_describing() -> None:
    """``region.geojson`` 是面几何，且文件里就写着"我是谁、几公里、哪一版"。"""
    b = builder()
    geometry = b.build_geometry(HORIZONTAL, 5000.0)
    document = region_geojson_document(
        geometry.geometry,
        mission_id="mission-001",
        name="西湖巡检航线",
        buffer_meters=5000.0,
        geometry_version=geometry.geometry_version,
    )
    assert document["geometry"]["type"] in {"Polygon", "MultiPolygon"}
    props = document["properties"]
    assert props["regionType"] == "polygon"
    # 等级取自任务区层那唯一一处常量，不是文件里另写一个字符串
    from app.mission.models import MISSION_REGION_LEVEL

    assert props["regionLevel"] == MISSION_REGION_LEVEL.value
    assert props["boundaryVersion"] == geometry.geometry_version
    assert props["bufferMeters"] == 5000.0


def test_documents_are_json_serialisable() -> None:
    """两份文档都要能直接 ``json.dumps``（Shapely 对象混进去就会在这里炸）。"""
    b = builder()
    geometry = b.build_geometry(HORIZONTAL, 5000.0)
    region = b.build("mission-001", "x", HORIZONTAL, 5000.0, geometry=geometry)
    json.dumps(
        route_geojson_document(
            region.waypoints, mission_id=region.id, name=region.name, buffer_meters=5000.0
        ),
        ensure_ascii=False,
    )
    json.dumps(
        region_geojson_document(
            geometry.geometry,
            mission_id=region.id,
            name=region.name,
            buffer_meters=5000.0,
            geometry_version=region.geometry_version,
        ),
        ensure_ascii=False,
    )
