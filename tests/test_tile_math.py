"""``app.geo.tile_math`` 单元测试。

参考值说明：北京天安门 (116.397, 39.908) 在 z10 的瓦片是 ``(843, 388)``、
z12 是 ``(3372, 1552)``，与 Slippy Map 通用实现一致，作为公式回归锚点。
"""

from __future__ import annotations

import pytest

from app.config.settings import (
    DEFAULT_AVERAGE_TILE_SIZE,
    TILE_MATH_MAX_ZOOM,
    TILE_MATH_MIN_ZOOM,
    WEB_MERCATOR_MAX_LAT,
)
from app.geo import (
    GeoBounds,
    TileRange,
    calculate_tile_range,
    calculate_tile_ranges,
    estimate_size_bytes,
    estimate_tile_count,
    format_size,
    lat_to_tile_y,
    lon_to_tile_x,
    tms_y_to_xyz_y,
    xyz_y_to_tms_y,
)

BEIJING = GeoBounds(116.0, 39.0, 117.0, 40.0)


# --------------------------------------------------------------------------- #
# 基础坐标
# --------------------------------------------------------------------------- #
def test_zoom_zero_world_collapses_to_single_tile() -> None:
    """zoom=0 全世界就是一张瓦片，(0,0) 必须落进去。"""
    assert lon_to_tile_x(0.0, 0) == 0
    assert lat_to_tile_y(0.0, 0) == 0
    assert lon_to_tile_x(-180.0, 0) == 0
    assert lon_to_tile_x(180.0, 0) == 0
    assert lat_to_tile_y(WEB_MERCATOR_MAX_LAT, 0) == 0
    assert lat_to_tile_y(-WEB_MERCATOR_MAX_LAT, 0) == 0


def test_beijing_reference_values() -> None:
    assert lon_to_tile_x(116.397, 10) == 843
    assert lat_to_tile_y(39.908, 10) == 388
    assert lon_to_tile_x(116.397, 12) == 3372
    assert lat_to_tile_y(39.908, 12) == 1552


@pytest.mark.parametrize("zoom", [0, 1, 5, 10, 17, 22])
@pytest.mark.parametrize(
    ("lon", "lat"),
    [(116.397, 39.908), (0.0, 0.0), (-122.4194, 37.7749), (151.2093, -33.8688)],
)
def test_results_stay_inside_tile_matrix(zoom: int, lon: float, lat: float) -> None:
    limit = 1 << zoom
    x = lon_to_tile_x(lon, zoom)
    y = lat_to_tile_y(lat, zoom)
    assert 0 <= x < limit
    assert 0 <= y < limit
    assert isinstance(x, int) and isinstance(y, int)


# --------------------------------------------------------------------------- #
# 边界
# --------------------------------------------------------------------------- #
def test_longitude_bounds() -> None:
    assert lon_to_tile_x(-180.0, 3) == 0
    assert lon_to_tile_x(180.0, 3) == 7  # 钳到 2**3 - 1
    assert lon_to_tile_x(179.999, 3) == 7
    assert lon_to_tile_x(-179.999, 3) == 0


def test_latitude_limits_map_to_first_and_last_row() -> None:
    for zoom in (0, 3, 10):
        assert lat_to_tile_y(WEB_MERCATOR_MAX_LAT, zoom) == 0
        assert lat_to_tile_y(-WEB_MERCATOR_MAX_LAT, zoom) == (1 << zoom) - 1


def test_latitude_is_clamped_beyond_mercator_limit() -> None:
    """±90 会被抹平到极限纬度，不抛异常、也不越界。"""
    assert lat_to_tile_y(90.0, 5) == lat_to_tile_y(WEB_MERCATOR_MAX_LAT, 5)
    assert lat_to_tile_y(-90.0, 5) == lat_to_tile_y(-WEB_MERCATOR_MAX_LAT, 5)
    assert lat_to_tile_y(1000.0, 5) == 0


def test_y_grows_from_north_to_south() -> None:
    """XYZ 的行号自北向南增长。"""
    zoom = 6
    rows = [lat_to_tile_y(lat, zoom) for lat in (80.0, 40.0, 0.0, -40.0, -80.0)]
    assert rows == sorted(rows)
    assert rows[0] < rows[-1]


def test_x_grows_from_west_to_east() -> None:
    zoom = 6
    cols = [lon_to_tile_x(lon, zoom) for lon in (-150.0, -60.0, 0.0, 60.0, 150.0)]
    assert cols == sorted(cols)
    assert cols[0] < cols[-1]


# --------------------------------------------------------------------------- #
# 非法参数
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("zoom", [-1, -100, 23, 30])
def test_zoom_out_of_supported_range_raises(zoom: int) -> None:
    with pytest.raises(ValueError, match="zoom 必须在"):
        lon_to_tile_x(0.0, zoom)
    with pytest.raises(ValueError, match="zoom 必须在"):
        lat_to_tile_y(0.0, zoom)
    with pytest.raises(ValueError, match="zoom 必须在"):
        calculate_tile_range(BEIJING, zoom)


@pytest.mark.parametrize("zoom", [TILE_MATH_MIN_ZOOM, TILE_MATH_MAX_ZOOM])
def test_zoom_limits_are_inclusive(zoom: int) -> None:
    assert lon_to_tile_x(0.0, zoom) >= 0


@pytest.mark.parametrize("zoom", [1.5, "3", None])
def test_non_integer_zoom_raises(zoom: object) -> None:
    with pytest.raises(ValueError, match="zoom 必须是整数"):
        lon_to_tile_x(0.0, zoom)  # type: ignore[arg-type]


@pytest.mark.parametrize("lon", [-180.001, 180.001, -360.0])
def test_longitude_out_of_range_raises(lon: float) -> None:
    with pytest.raises(ValueError, match="lon 必须在"):
        lon_to_tile_x(lon, 5)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_input_raises(bad: float) -> None:
    with pytest.raises(ValueError):
        lon_to_tile_x(bad, 5)
    with pytest.raises(ValueError):
        lat_to_tile_y(bad, 5)


# --------------------------------------------------------------------------- #
# BoundingBox -> TileRange
# --------------------------------------------------------------------------- #
def test_tile_range_shape_and_ordering() -> None:
    for zoom in range(0, 19):
        rng = calculate_tile_range(BEIJING, zoom)
        assert rng.zoom == zoom
        assert rng.min_x <= rng.max_x
        assert rng.min_y <= rng.max_y
        assert rng.tile_count > 0
        assert rng.tile_count == rng.x_count * rng.y_count


def test_tile_range_maps_north_to_min_y() -> None:
    """北边界对应更小的行号——写反了会让下载区域整体偏移。"""
    zoom = 10
    rng = calculate_tile_range(BEIJING, zoom)
    assert rng.min_y == lat_to_tile_y(BEIJING.north, zoom)
    assert rng.max_y == lat_to_tile_y(BEIJING.south, zoom)
    assert rng.min_y < rng.max_y
    assert rng.min_x == lon_to_tile_x(BEIJING.west, zoom)
    assert rng.max_x == lon_to_tile_x(BEIJING.east, zoom)


def test_tile_range_reference_values() -> None:
    rng = calculate_tile_range(BEIJING, 8)
    assert (rng.min_x, rng.max_x) == (210, 211)
    assert (rng.min_y, rng.max_y) == (96, 97)
    assert rng.tile_count == 4


def test_tile_count_is_multiplicative() -> None:
    rng = TileRange(zoom=10, min_x=3, max_x=7, min_y=10, max_y=19)
    assert rng.tile_count == 5 * 10


def test_tile_range_of_world_at_zoom_zero() -> None:
    world = GeoBounds(-180.0, -WEB_MERCATOR_MAX_LAT, 180.0, WEB_MERCATOR_MAX_LAT)
    rng = calculate_tile_range(world, 0)
    assert (rng.min_x, rng.max_x) == (0, 0)
    assert (rng.min_y, rng.max_y) == (0, 0)
    assert rng.tile_count == 1


def test_single_point_box_still_yields_one_tile() -> None:
    tiny = GeoBounds(116.397, 39.908, 116.3974, 39.9084)
    rng = calculate_tile_range(tiny, 4)
    assert rng.tile_count >= 1


# --------------------------------------------------------------------------- #
# Zoom 范围统计
# --------------------------------------------------------------------------- #
def test_calculate_tile_ranges_covers_every_level() -> None:
    ranges = calculate_tile_ranges(BEIJING, 8, 10)
    assert [r.zoom for r in ranges] == [8, 9, 10]


def test_calculate_tile_ranges_single_level() -> None:
    ranges = calculate_tile_ranges(BEIJING, 12, 12)
    assert len(ranges) == 1
    assert ranges[0].zoom == 12


def test_estimate_tile_count_matches_sum_of_ranges() -> None:
    total = estimate_tile_count(BEIJING, 8, 17)
    expected = sum(r.tile_count for r in calculate_tile_ranges(BEIJING, 8, 17))
    assert total == expected
    assert total == 230376


def test_estimate_tile_count_grows_with_zoom() -> None:
    assert estimate_tile_count(BEIJING, 8, 9) < estimate_tile_count(BEIJING, 8, 12)


def test_min_zoom_greater_than_max_zoom_raises() -> None:
    with pytest.raises(ValueError, match="min_zoom 不能大于 max_zoom"):
        calculate_tile_ranges(BEIJING, 12, 8)
    with pytest.raises(ValueError, match="min_zoom 不能大于 max_zoom"):
        estimate_tile_count(BEIJING, 12, 8)


# --------------------------------------------------------------------------- #
# XYZ <-> TMS
# --------------------------------------------------------------------------- #
def test_xyz_to_tms_reference_value() -> None:
    assert xyz_y_to_tms_y(3, 2) == 5


def test_xyz_tms_round_trip() -> None:
    for zoom in range(0, 13):
        for y in range(1 << zoom):
            assert tms_y_to_xyz_y(zoom, xyz_y_to_tms_y(zoom, y)) == y
            assert xyz_y_to_tms_y(zoom, tms_y_to_xyz_y(zoom, y)) == y


def test_xyz_tms_flips_the_axis() -> None:
    zoom = 4
    assert xyz_y_to_tms_y(zoom, 0) == 15
    assert xyz_y_to_tms_y(zoom, 15) == 0
    # 对称关系
    assert xyz_y_to_tms_y(zoom, y=7) == 8
    assert xyz_y_to_tms_y(zoom, y=8) == 7


def test_tms_y_out_of_range_raises() -> None:
    with pytest.raises(ValueError, match="y 必须在"):
        xyz_y_to_tms_y(3, 8)
    with pytest.raises(ValueError, match="y 必须在"):
        xyz_y_to_tms_y(3, -1)
    with pytest.raises(ValueError, match="y 必须在"):
        tms_y_to_xyz_y(3, 8)


# --------------------------------------------------------------------------- #
# 容量预估
# --------------------------------------------------------------------------- #
def test_estimate_size_bytes_uses_default_tile_size() -> None:
    assert estimate_size_bytes(1) == DEFAULT_AVERAGE_TILE_SIZE
    assert estimate_size_bytes(10) == 10 * DEFAULT_AVERAGE_TILE_SIZE


def test_estimate_size_bytes_accepts_custom_tile_size() -> None:
    assert estimate_size_bytes(4, average_tile_size=1024) == 4096


def test_estimate_size_bytes_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="tile_count 不能为负"):
        estimate_size_bytes(-1)
    with pytest.raises(ValueError, match="average_tile_size 必须为正"):
        estimate_size_bytes(10, average_tile_size=0)


@pytest.mark.parametrize(
    ("size_bytes", "expected"),
    [
        (0, "0.00 B"),
        (512, "512.00 B"),
        (1024, "1.00 KB"),
        (1536, "1.50 KB"),
        (1048576, "1.00 MB"),
        (1073741824, "1.00 GB"),
        (1099511627776, "1.00 TB"),
    ],
)
def test_format_size(size_bytes: int, expected: str) -> None:
    assert format_size(size_bytes) == expected


def test_format_size_rejects_negative() -> None:
    with pytest.raises(ValueError, match="不能为负"):
        format_size(-1)


def test_end_to_end_estimate_reads_like_a_ui_label() -> None:
    """模拟 UI 上要显示的两行内容。"""
    count = estimate_tile_count(BEIJING, 8, 17)
    text = format_size(estimate_size_bytes(count))
    assert f"{count:,}" == "230,376"
    assert text == "6.59 GB"
