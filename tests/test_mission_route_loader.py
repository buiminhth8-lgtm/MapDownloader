"""航线导入（Slice 22，Spec §6）。

这一层只有一件事：**把一份 GeoJSON 变成一串可信的航点**。后面所有环节
（米制缓冲、瓦片预估、下载）都以这串点为唯一输入，因此这里多放进去一个
非法坐标，就会在下单阶段变成一张荒唐的预估表。

用例按"输入长什么样"组织：三种合法写法各一条，然后是一整排"不许通过"的
输入 —— 每一条都对应一种真实会遇到的错文件（拿 GPX 当 GeoJSON、
把行政区边界当航线、航线只有一个点、点在极点外……）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.mission import (
    MissionRouteError,
    MissionWaypoint,
    load_route_waypoints,
    parse_route_geojson,
)

LINE = {"type": "LineString", "coordinates": [[120.0, 30.0], [120.1, 30.1]]}


def write(work_dir: Path, payload, name: str = "route.geojson") -> Path:
    """把对象写成文件（用例要真读文件，不能只测内存路径）。"""
    path = work_dir / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 三种合法写法
# --------------------------------------------------------------------------- #
def test_bare_linestring_is_accepted() -> None:
    """裸 LineString → 航点序列（顺序原样保留）。"""
    points = parse_route_geojson(LINE)
    assert points == (MissionWaypoint(120.0, 30.0), MissionWaypoint(120.1, 30.1))
    # 经纬度顺序不能反：GeoJSON 是 [lon, lat]
    assert points[0].lon == 120.0
    assert points[0].lat == 30.0


def test_feature_with_linestring_geometry_is_accepted() -> None:
    """``Feature`` 包一层也一样（这是最常见的导出格式）。"""
    points = parse_route_geojson(
        {
            "type": "Feature",
            "properties": {"name": "西湖巡检"},
            "geometry": LINE,
        }
    )
    assert len(points) == 2


def test_feature_collection_takes_the_single_linestring() -> None:
    """``FeatureCollection`` 里混着点也要能选出那条线。"""
    points = parse_route_geojson(
        {
            "type": "FeatureCollection",
            "features": [
                # 起降点常常被导出成一个 Point，它不参与走廊计算
                {"type": "Feature", "geometry": {"type": "Point", "coordinates": [120.0, 30.0]}},
                {"type": "Feature", "geometry": LINE},
            ],
        }
    )
    assert len(points) == 2


def test_route_is_read_from_a_real_file(work_dir: Path) -> None:
    """真读文件（错误的编码 / 不存在的路径都要在这里暴露）。"""
    path = write(work_dir, {"type": "Feature", "geometry": LINE})
    assert len(load_route_waypoints(path)) == 2


def test_third_dimension_is_ignored(work_dir: Path) -> None:
    """带高程的坐标要能用：高程与本工具的瓦片计算无关，丢掉即可。"""
    points = parse_route_geojson(
        {"type": "LineString", "coordinates": [[120.0, 30.0, 12.5], [120.1, 30.1, 30.0]]}
    )
    assert points[0].as_xy() == (120.0, 30.0)


def test_consecutive_duplicate_points_are_dropped() -> None:
    """相邻重复点直接去掉（手工描航线时很常见）。"""
    points = parse_route_geojson(
        {
            "type": "LineString",
            "coordinates": [[120.0, 30.0], [120.0, 30.0], [120.1, 30.1], [120.1, 30.1]],
        }
    )
    assert len(points) == 2


# --------------------------------------------------------------------------- #
# 不接受的形式
# --------------------------------------------------------------------------- #
def test_kml_is_rejected_with_an_actionable_message(work_dir: Path) -> None:
    """KML / GPX 一律拒绝，并告诉用户该转成什么。"""
    path = work_dir / "route.kml"
    path.write_text("<kml><Placemark/></kml>", encoding="utf-8")
    with pytest.raises(MissionRouteError) as excinfo:
        load_route_waypoints(path)
    message = str(excinfo.value)
    assert "KML" in message
    assert "GeoJSON" in message


def test_gpx_is_rejected(work_dir: Path) -> None:
    path = work_dir / "track.gpx"
    path.write_text("<gpx><trk/></gpx>", encoding="utf-8")
    with pytest.raises(MissionRouteError):
        load_route_waypoints(path)


def test_missing_file_is_rejected(work_dir: Path) -> None:
    with pytest.raises(MissionRouteError) as excinfo:
        load_route_waypoints(work_dir / "nope.geojson")
    assert "不存在" in str(excinfo.value)


def test_non_json_file_is_rejected(work_dir: Path) -> None:
    path = work_dir / "route.geojson"
    path.write_text("this is not json", encoding="utf-8")
    with pytest.raises(MissionRouteError) as excinfo:
        load_route_waypoints(path)
    assert "JSON" in str(excinfo.value)


def test_polygon_is_rejected(work_dir: Path) -> None:
    """把行政区边界当航线导入 → 拒（它是面，不是线）。"""
    path = write(
        work_dir,
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[120.0, 30.0], [120.1, 30.0], [120.1, 30.1], [120.0, 30.0]]],
            },
        },
    )
    with pytest.raises(MissionRouteError) as excinfo:
        load_route_waypoints(path)
    assert "LineString" in str(excinfo.value)


def test_multilinestring_is_rejected() -> None:
    """多段航线要拆开：并成一条会缓冲出一大片，不是"沿航线"。"""
    with pytest.raises(MissionRouteError) as excinfo:
        parse_route_geojson(
            {
                "type": "MultiLineString",
                "coordinates": [[[120.0, 30.0], [120.1, 30.1]], [[121.0, 31.0], [121.1, 31.1]]],
            }
        )
    assert "MultiLineString" in str(excinfo.value)


def test_feature_collection_with_two_lines_is_rejected() -> None:
    """两条线 → 拒（不猜用户要哪一条）。"""
    with pytest.raises(MissionRouteError) as excinfo:
        parse_route_geojson(
            {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "geometry": LINE},
                    {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[121.0, 31.0], [121.1, 31.1]]}},
                ],
            }
        )
    assert "不止一条" in str(excinfo.value)


def test_feature_collection_without_a_line_is_rejected() -> None:
    with pytest.raises(MissionRouteError) as excinfo:
        parse_route_geojson(
            {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "geometry": {"type": "Point", "coordinates": [120.0, 30.0]}},
                ],
            }
        )
    assert "没有找到 LineString" in str(excinfo.value)


def test_unknown_top_level_type_is_rejected() -> None:
    with pytest.raises(MissionRouteError) as excinfo:
        parse_route_geojson({"type": "Topology", "objects": {}})
    assert "不认识" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 点数
# --------------------------------------------------------------------------- #
def test_single_point_route_is_rejected() -> None:
    """一个点只能缓冲出一个圆 —— 那是"自定义区域"，不是航线任务。"""
    with pytest.raises(MissionRouteError) as excinfo:
        parse_route_geojson({"type": "LineString", "coordinates": [[120.0, 30.0]]})
    assert "2 个航点" in str(excinfo.value)


def test_all_points_identical_is_rejected() -> None:
    """三个点重合 → 去重后只剩一个，同样不构成一条线。"""
    with pytest.raises(MissionRouteError) as excinfo:
        parse_route_geojson(
            {
                "type": "LineString",
                "coordinates": [[120.0, 30.0], [120.0, 30.0], [120.0, 30.0]],
            }
        )
    assert "不同" in str(excinfo.value)


def test_empty_coordinates_is_rejected() -> None:
    with pytest.raises(MissionRouteError):
        parse_route_geojson({"type": "LineString", "coordinates": []})


# --------------------------------------------------------------------------- #
# 坐标合法性
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "position",
    [
        [200.0, 30.0],  # 经度越界
        [-181.0, 30.0],
        [120.0, 89.0],  # 纬度超出 Web 墨卡托（瓦片编号在极点附近没有定义）
        [120.0, -90.0],
    ],
)
def test_illegal_coordinates_are_rejected(position) -> None:
    with pytest.raises(MissionRouteError):
        parse_route_geojson(
            {"type": "LineString", "coordinates": [position, [120.1, 30.1]]}
        )


def test_polar_latitude_message_explains_why() -> None:
    """拒绝的理由要说清"为什么不行"，而不是只说"不合法"。"""
    with pytest.raises(MissionRouteError) as excinfo:
        parse_route_geojson({"type": "LineString", "coordinates": [[120.0, 88.0], [120.1, 88.1]]})
    assert "瓦片" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["120", None, True, float("nan"), float("inf")])
def test_non_numeric_or_non_finite_coordinates_are_rejected(bad) -> None:
    with pytest.raises(MissionRouteError):
        parse_route_geojson(
            {"type": "LineString", "coordinates": [[bad, 30.0], [120.1, 30.1]]}
        )


def test_short_position_is_rejected() -> None:
    """``[120]`` 只有经度 —— 必须拒，绝不能"补一个 0 当纬度"。"""
    with pytest.raises(MissionRouteError) as excinfo:
        parse_route_geojson(
            {"type": "LineString", "coordinates": [[120.0], [120.1, 30.1]]}
        )
    assert "第 1 个坐标" in str(excinfo.value)


def test_error_points_at_the_offending_index() -> None:
    """坏点要指出是**第几个** —— 一份几千个点的航线里，"有个点不合法"帮不上忙。"""
    with pytest.raises(MissionRouteError) as excinfo:
        parse_route_geojson(
            {
                "type": "LineString",
                "coordinates": [[120.0, 30.0], [120.1, 30.1], [999.0, 30.2]],
            }
        )
    assert "第 3 个" in str(excinfo.value)


def test_mapping_input_skips_the_file_layer(work_dir: Path) -> None:
    """直接给已经解析好的对象也要能用（界面预览航线时省一次读盘）。"""
    assert load_route_waypoints(dict(LINE)) == parse_route_geojson(LINE)
