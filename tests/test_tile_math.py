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
    tile_bounds,
    tile_x_to_lon,
    tile_y_to_lat,
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


# --------------------------------------------------------------------------- #
# 反向换算：瓦片编号 -> 经纬度（Slice 16，Spec §5）
#
# 这组用例存在的理由：区域裁剪要回答"这张瓦片盖住哪块地"，靠的就是这三个
# 函数。它们与正变换必须**互为逆运算**——不一致的后果不是"差一点点"，而是
# 外包围盒多算/少算一整行，于是预期瓦片数、覆盖度、进度分母一起偏。
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("zoom", [0, 1, 2, 5, 10, 12, 18, 22])
def test_reverse_lon_round_trips_to_the_same_column(zoom: int) -> None:
    """``lon_to_tile_x(tile_x_to_lon(x, z), z) == x``（穷举该级别所有列）。

    ``x == 2**zoom``（最东侧的边界值 180°）要单独处理：它不是一个合法列号，
    正向换算会把它钳回最后一列，所以只断言 ``<= 2**zoom`` 的那些。
    """
    tiles = 2**zoom
    for x in range(tiles + 1):
        lon = tile_x_to_lon(x, zoom)
        expected = min(x, tiles - 1)
        assert lon_to_tile_x(lon, zoom) == expected, f"z{zoom} x={x}"


@pytest.mark.parametrize("zoom", [0, 1, 2, 5, 10, 12, 18, 22])
def test_reverse_lat_round_trips_to_the_same_row(zoom: int) -> None:
    """``lat_to_tile_y(tile_y_to_lat(y, z), z) == y``。"""
    tiles = 2**zoom
    for y in range(tiles + 1):
        lat = tile_y_to_lat(y, zoom)
        expected = min(y, tiles - 1)
        assert lat_to_tile_y(lat, zoom) == expected, f"z{zoom} y={y}"


def test_lon_to_x_round_trip_has_zero_inconsistencies() -> None:
    """把上两条用例的失败都收集起来报总数。

    逐条 ``assert`` 只能告诉你"第一处不一致是哪个"，而这类浮点问题的价值
    恰恰在**一共有多少处**：只有 1 处可能是个巧合，有 14 处说明公式没对齐。
    """
    mismatches: list[tuple[str, int, int, int]] = []
    for zoom in range(0, 23):
        tiles = 2**zoom
        for x in range(tiles + 1):
            got = lon_to_tile_x(tile_x_to_lon(x, zoom), zoom)
            if got != min(x, tiles - 1):
                mismatches.append(("lon", zoom, x, got))
        for y in range(tiles + 1):
            got = lat_to_tile_y(tile_y_to_lat(y, zoom), zoom)
            if got != min(y, tiles - 1):
                mismatches.append(("lat", zoom, y, got))
    assert mismatches == []


def test_tile_x_to_lon_anchors() -> None:
    """z0 只有一张瓦片：西边界 -180、东边界 180。"""
    assert tile_x_to_lon(0, 0) == pytest.approx(-180.0)
    assert tile_x_to_lon(1, 0) == pytest.approx(180.0)
    # z1 两列：左列覆盖 -180~0，右列覆盖 0~180
    assert tile_x_to_lon(0, 1) == pytest.approx(-180.0)
    assert tile_x_to_lon(1, 1) == pytest.approx(0.0)
    assert tile_x_to_lon(2, 1) == pytest.approx(180.0)


def test_tile_y_to_lat_anchors() -> None:
    """z0 只有一行：北边界是墨卡托上限，南边界是它的相反数。"""
    assert tile_y_to_lat(0, 0) == pytest.approx(WEB_MERCATOR_MAX_LAT)
    assert tile_y_to_lat(1, 0) == pytest.approx(-WEB_MERCATOR_MAX_LAT)
    # z1 上半行北边界仍是上限，分界线是赤道
    assert tile_y_to_lat(0, 1) == pytest.approx(WEB_MERCATOR_MAX_LAT)
    assert tile_y_to_lat(1, 1) == pytest.approx(0.0)


def test_tile_y_to_lat_is_monotonic_decreasing() -> None:
    """行号越大越靠南：纬度必须严格递减（这个方向反了整张地图会上下颠倒）。"""
    lats = [tile_y_to_lat(y, 6) for y in range(2**6 + 1)]
    assert all(a > b for a, b in zip(lats, lats[1:]))


@pytest.mark.parametrize(("zoom", "x", "y"), [(0, 0, 0), (1, 1, 0), (10, 843, 388), (12, 3372, 1552)])
def test_tile_bounds_contains_the_tile_center(zoom: int, x: int, y: int) -> None:
    """瓦片自己的**中心点**必须落在它自己的 bounds 里。

    这是"bounds 取反了"最容易暴露的地方：上下/左右取反时，中心点会跑到框外。
    """
    bounds = tile_bounds(zoom, x, y)
    center_lon = (bounds.west + bounds.east) / 2.0
    center_lat = (bounds.south + bounds.north) / 2.0

    assert bounds.contains(center_lon, center_lat)
    # 而且中心点确实属于这张瓦片（正向换算要能回到同一张）
    assert lon_to_tile_x(center_lon, zoom) == x
    assert lat_to_tile_y(center_lat, zoom) == y


def test_tile_range_tiles_all_overlap_the_requested_rectangle() -> None:
    """**一张都不白算**：区间里每张瓦片都与原矩形有交集。

    ``calculate_tile_range`` 取的是"包含边界点的那张瓦片"，所以区间是**保守
    向外**取的（Union ⊇ 矩形）。保守本身没问题（宁可多算不可漏算），但要说清
    它保守到什么程度：区间里的每一张都必须真的碰到矩形，否则出现的是"凭空多下
    了一整行"这种没人看得懂的浪费。

    对 Polygon 过滤而言这是前提：候选区间若不覆盖整个外包矩形，落在区间外的
    瓦片连判定的机会都没有 —— 那才是真正的"漏块"。
    """
    lonely: list[tuple[int, int, int]] = []
    for zoom in range(0, 13):
        tile_range = calculate_tile_range(BEIJING, zoom)
        for y in range(tile_range.min_y, tile_range.max_y + 1):
            for x in range(tile_range.min_x, tile_range.max_x + 1):
                tile = tile_bounds(zoom, x, y)
                if not _rectangles_overlap(tile, BEIJING):
                    lonely.append((zoom, x, y))
    assert lonely == []


def _rectangles_overlap(a: GeoBounds, b: GeoBounds) -> bool:
    """两个经纬度矩形是否有交集（闭区间，擦边算相交）。

    直接写四个不等式而不是引 shapely：这条用例要断言的正是**我们自己的**
    取整规则，用另一个几何库来"背书"会把问题从"公式对不对"变成"两个库的
    浮点处理是否一致"。
    """
    return not (
        a.east < b.west or b.east < a.west or a.north < b.south or b.north < a.south
    )


def test_tile_range_union_covers_the_requested_rectangle() -> None:
    """区间里所有瓦片合起来必须**盖住**原矩形（四边都不许短）。

    与上一条一起构成"保守但不浪费"的完整表述：既没漏，也没多到离谱。
    """
    holes: list[tuple[int, str]] = []
    for zoom in range(0, 13):
        tile_range = calculate_tile_range(BEIJING, zoom)
        west = tile_bounds(zoom, tile_range.min_x, tile_range.min_y).west
        east = tile_bounds(zoom, tile_range.max_x, tile_range.max_y).east
        north = tile_bounds(zoom, tile_range.min_x, tile_range.min_y).north
        south = tile_bounds(zoom, tile_range.max_x, tile_range.max_y).south
        if west > BEIJING.west:
            holes.append((zoom, "west"))
        if east < BEIJING.east:
            holes.append((zoom, "east"))
        if north < BEIJING.north:
            holes.append((zoom, "north"))
        if south > BEIJING.south:
            holes.append((zoom, "south"))
    assert holes == []


def test_tile_bounds_at_zoom_zero_is_the_whole_world() -> None:
    bounds = tile_bounds(0, 0, 0)

    assert bounds.west == pytest.approx(-180.0)
    assert bounds.east == pytest.approx(180.0)
    assert bounds.south == pytest.approx(-WEB_MERCATOR_MAX_LAT)
    assert bounds.north == pytest.approx(WEB_MERCATOR_MAX_LAT)


@pytest.mark.parametrize("zoom", [TILE_MATH_MIN_ZOOM, 12, TILE_MATH_MAX_ZOOM])
def test_reverse_functions_reject_out_of_range_axis(zoom: int) -> None:
    with pytest.raises(ValueError, match="必须在"):
        tile_x_to_lon(2**zoom + 1, zoom)
    with pytest.raises(ValueError, match="必须在"):
        tile_y_to_lat(2**zoom + 1, zoom)
    with pytest.raises(ValueError, match="必须在"):
        tile_x_to_lon(-1, zoom)


@pytest.mark.parametrize("zoom", [TILE_MATH_MIN_ZOOM - 1, TILE_MATH_MAX_ZOOM + 1])
def test_reverse_functions_reject_out_of_range_zoom(zoom: int) -> None:
    with pytest.raises(ValueError):
        tile_x_to_lon(0, zoom)
    with pytest.raises(ValueError):
        tile_y_to_lat(0, zoom)
    with pytest.raises(ValueError):
        tile_bounds(zoom, 0, 0)


def test_tile_bounds_accepts_the_edge_index_but_not_beyond() -> None:
    """``x == 2**zoom`` 是**边界值**（合法），不是瓦片编号。

    ``tile_bounds`` 走的是 ``validate_tile_axis``（``[0, 2**z)``），所以
    它必须拒绝 ``2**z``；而 ``tile_x_to_lon`` 单独调用时接受它 —— 两条
    规则**故意不同**，混用会让 east / south 永远取不到。
    """
    with pytest.raises(ValueError):
        tile_bounds(2, 4, 0)  # x == 2**2 不是瓦片
    assert tile_x_to_lon(4, 2) == pytest.approx(180.0)


def test_tile_bounds_rejects_non_integer_axis() -> None:
    with pytest.raises(ValueError, match="必须是整数"):
        tile_x_to_lon(1.5, 2)
    with pytest.raises(ValueError, match="必须是整数"):
        tile_y_to_lat(1.5, 2)
