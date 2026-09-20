"""多边形瓦片过滤器（Slice 16，Spec §6 / §24）。

这组用例守的是一条**产品红线**：判定必须走 ``intersects``（瓦片矩形与区域
有任何重叠就算命中），**不能**走"瓦片中心点是否在区域内"。

原因不是性能，是正确性：边界正好切过一张瓦片时，中心点落在外侧，于是这张瓦片
被丢弃 —— 而它有一半是用户要的地。地图上出现的是**沿着行政区边界的一整圈缺口**，
位置随边界形状无规律出现，事后极难排查。多下几张外包矩形边缘的瓦片用户看不出
问题，少下的会。

因此这里不只测"命中/不命中"，还要专门构造"中心在外、但确有重叠"的瓦片，
断言它**必须被选中**。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import Point, Polygon, box

from app.geo import (
    DownloadRegion,
    GeoBounds,
    GeoJSONNotFoundError,
    PolygonTileRegionFilter,
    RegionType,
    ZoomTileFilterStats,
    build_region_filter,
    calculate_tile_range,
    tile_bounds,
)
from app.geo.geojson_loader import load_geojson_geometry

#: 夹具用 ``__file__`` 推导：从仓库根跑 pytest 与从任意目录跑单个文件都必须
#: 找得到（相对 cwd 的路径在后者会直接失败）。
SQUARE = str(Path(__file__).resolve().parent / "fixtures" / "regions" / "square.geojson")
SQUARE_BOUNDS = GeoBounds(116.0, 39.0, 117.0, 40.0)


# --------------------------------------------------------------------------- #
# 判定本身
# --------------------------------------------------------------------------- #
def test_tile_straddling_the_boundary_is_selected() -> None:
    """**核心用例**：区域边界切过瓦片 → 这张瓦片必须在。

    构造方式：取一张瓦片，区域只盖住它右半边的 40%。瓦片中心（左半边）落在
    区域外 —— 走中心点判据就会丢块。
    """
    zoom, x, y = 12, 3372, 1552
    tile = tile_bounds(zoom, x, y)
    width = tile.east - tile.west

    # 区域 = 瓦片右半边往东 10% 宽的一条：左边界在中心点以东
    region_polygon = box(
        tile.west + width * 0.6, tile.south, tile.east, tile.north
    )
    region_filter = PolygonTileRegionFilter(region_polygon, name="边界条")

    # 先确认判据本身的前提成立：瓦片的左半边（含中心点）确实在区域外
    left_half_center = Point(tile.west + width * 0.5, (tile.south + tile.north) / 2.0)
    assert not region_polygon.contains(left_half_center)

    assert region_filter.intersects_tile_xyz(zoom, x, y) is True


def test_tile_fully_outside_is_not_selected() -> None:
    zoom, x, y = 12, 3372, 1552
    tile = tile_bounds(zoom, x, y)
    # 区域整体在瓦片以西一整度，连边都碰不到
    region_polygon = box(tile.west - 2.0, tile.south, tile.west - 1.0, tile.north)
    region_filter = PolygonTileRegionFilter(region_polygon)

    assert region_filter.intersects_tile_xyz(zoom, x, y) is False


def test_tile_touching_only_a_corner_is_selected() -> None:
    """只擦到一个角也算命中。

    ``intersects`` 包含边界接触。反过来（用 ``crosses`` / 严格内部判据）
    会沿着区域四角各丢一张，症状同样是"边界上的洞"。
    """
    zoom, x, y = 12, 3372, 1552
    tile = tile_bounds(zoom, x, y)
    # 区域只覆盖瓦片的西北角：一个很小的方块
    corner = box(
        tile.west - 0.1,
        tile.north - (tile.north - tile.south) * 0.1,
        tile.west + (tile.east - tile.west) * 0.1,
        tile.north + 0.1,
    )
    region_filter = PolygonTileRegionFilter(corner)

    assert region_filter.intersects_tile_xyz(zoom, x, y) is True


def test_hole_in_the_region_is_respected() -> None:
    """区域内部的空洞（enclave）必须被认出来 —— 洞里的瓦片不算区域内。

    行政区边界数据里 enclave 很常见（飞地、湖泊）。把它当成实心多边形处理，
    结果是"多下了一整块用户没要的地"。
    """
    zoom, x, y = 12, 3372, 1552
    tile = tile_bounds(zoom, x, y)
    outer = box(tile.west - 0.5, tile.south - 0.5, tile.east + 0.5, tile.north + 0.5)
    # 洞比瓦片大一圈 → 瓦片整个落在洞里
    hole = box(tile.west - 0.1, tile.south - 0.1, tile.east + 0.1, tile.north + 0.1)
    donut = Polygon(outer.exterior.coords, [hole.exterior.coords])
    assert donut.is_valid

    region_filter = PolygonTileRegionFilter(donut)

    assert region_filter.intersects_tile_xyz(zoom, x, y) is False


def test_intersects_tile_accepts_duck_typed_coordinates() -> None:
    """``intersects_tile`` 只读 ``z/x/y``，不强制 ``TileCoordinate`` 类型。

    ``app.geo`` 是纯计算层，不该 import ``app.downloader``。用鸭子类型让两边
    各自演进，代价是这里要有一条用例证明"随便一个有三个属性的对象也能用"。
    """

    class FakeTile:
        z, x, y = 12, 3372, 1552

    region_filter = PolygonTileRegionFilter(
        box(SQUARE_BOUNDS.west, SQUARE_BOUNDS.south, SQUARE_BOUNDS.east, SQUARE_BOUNDS.north)
    )

    assert region_filter.intersects_tile(FakeTile()) is True


def test_intersects_tile_xyz_rejects_invalid_coordinates() -> None:
    region_filter = PolygonTileRegionFilter(box(0.0, 0.0, 1.0, 1.0))

    with pytest.raises(ValueError):
        region_filter.intersects_tile_xyz(12, 4096, 0)  # x 越界
    with pytest.raises(ValueError):
        region_filter.intersects_tile_xyz(25, 0, 0)  # zoom 越界


def test_intersects_bounds_answers_the_cheap_question() -> None:
    """整个矩形碰不碰区域（用于"这个范围完全不相干"的快速判断）。"""
    region_filter = PolygonTileRegionFilter(box(116.0, 39.0, 117.0, 40.0))

    assert region_filter.intersects_bounds(GeoBounds(116.5, 39.5, 116.6, 39.6)) is True
    assert region_filter.intersects_bounds(GeoBounds(0.0, 0.0, 1.0, 1.0)) is False


# --------------------------------------------------------------------------- #
# prepared 几何只建一次（Spec §6 的性能约定）
# --------------------------------------------------------------------------- #
def test_prepared_geometry_is_built_once_and_reused() -> None:
    """``prep()`` 必须在构造函数里做**一次**，循环里只做查询。

    国家尺度的边界有几十万个顶点，每张瓦片现 prepare 一次能慢一到两个数量级。
    断言方式是"同一个对象"：如果哪天有人把它挪进 ``intersects_tile_xyz``，
    ``self._prepared`` 就会每帧换一个新对象 —— 这条用例会立刻红。
    """
    region_filter = PolygonTileRegionFilter(box(116.0, 39.0, 117.0, 40.0))

    first = region_filter.prepared
    region_filter.intersects_tile_xyz(12, 3372, 1552)
    area_before = region_filter.prepared.context

    assert region_filter.prepared is first
    assert region_filter.prepared.context is area_before
    assert region_filter.tile_count == 1


def test_empty_geometry_is_rejected_at_construction() -> None:
    """空几何必须在**入口**拦掉。

    prepare 一个空几何不会报错，但之后每张瓦片都返回 ``False`` ——
    表现为"一片都没下"，这种静默失败比抛异常危险得多。
    """
    with pytest.raises(ValueError, match="不能为空"):
        PolygonTileRegionFilter(Polygon())


# --------------------------------------------------------------------------- #
# 批量：流式产出与统计
# --------------------------------------------------------------------------- #
def test_iter_selected_tiles_is_a_stream_in_xy_order() -> None:
    """生成器按 ``y`` 升序、``x`` 升序产出，且与逐张判定结果一致。"""
    region_filter = PolygonTileRegionFilter(
        box(SQUARE_BOUNDS.west, SQUARE_BOUNDS.south, SQUARE_BOUNDS.east, SQUARE_BOUNDS.north)
    )
    zoom = 8
    produced = list(region_filter.iter_selected_tiles(SQUARE_BOUNDS, zoom))

    assert produced, "北京市范围的方框在 z8 至少该有一张瓦片"
    assert produced == sorted(produced, key=lambda item: (item[1], item[0]))
    # 与"翻遍整个候选区间逐张判"得到的结果必须一致
    tile_range = calculate_tile_range(SQUARE_BOUNDS, zoom)
    expected = [
        (x, y)
        for y in range(tile_range.min_y, tile_range.max_y + 1)
        for x in range(tile_range.min_x, tile_range.max_x + 1)
        if region_filter.intersects_tile_xyz(zoom, x, y)
    ]
    assert produced == expected
    # 生成器而不是 list：全国 z14 的候选是千万级，materialize 会把内存吃光
    assert not isinstance(region_filter.iter_selected_tiles(SQUARE_BOUNDS, zoom), list)


def test_count_selected_tiles_matches_the_stream() -> None:
    region_filter = PolygonTileRegionFilter(
        box(SQUARE_BOUNDS.west, SQUARE_BOUNDS.south, SQUARE_BOUNDS.east, SQUARE_BOUNDS.north)
    )
    stats = region_filter.count_selected_tiles(SQUARE_BOUNDS, 8)

    assert isinstance(stats, ZoomTileFilterStats)
    assert stats.candidate_tiles == calculate_tile_range(SQUARE_BOUNDS, 8).tile_count
    assert stats.selected_tiles == len(list(region_filter.iter_selected_tiles(SQUARE_BOUNDS, 8)))
    assert stats.filtered_tiles == stats.candidate_tiles - stats.selected_tiles
    assert stats.zoom == 8


def test_region_matching_the_envelope_selects_every_candidate() -> None:
    """区域恰好等于外包矩形时，候选 == 选中（过滤 0）。

    这是"过滤器会不会把该留的也滤掉"的底线：区域与外框相同就不该少一张。
    """
    region_filter = PolygonTileRegionFilter(
        box(SQUARE_BOUNDS.west, SQUARE_BOUNDS.south, SQUARE_BOUNDS.east, SQUARE_BOUNDS.north)
    )

    stats = region_filter.count_selected_tiles(SQUARE_BOUNDS, 10)

    assert stats.selected_tiles == stats.candidate_tiles
    assert stats.filtered_tiles == 0
    assert stats.filtered_percent == 0.0


def test_half_region_filters_roughly_half() -> None:
    """区域只盖住外框西半边 → 选中数明显小于候选，但不是 0。

    用范围断言而不是精确数字：精确值取决于取整规则，那是
    ``test_region_estimator`` 的职责；这条只回答"过滤到底有没有生效"。
    """
    middle = (SQUARE_BOUNDS.west + SQUARE_BOUNDS.east) / 2.0
    region_filter = PolygonTileRegionFilter(
        box(SQUARE_BOUNDS.west, SQUARE_BOUNDS.south, middle, SQUARE_BOUNDS.north)
    )

    stats = region_filter.count_selected_tiles(SQUARE_BOUNDS, 12)

    assert 0 < stats.selected_tiles < stats.candidate_tiles
    assert stats.filtered_percent > 30.0


def test_boundary_tiles_are_never_lost_along_a_half_tile_offset() -> None:
    """沿边界横扫一整行：**不允许出现"上一张命中、下一张不命中"的空洞**。

    这是中心点判据最典型的症状 —— 边界切过一串瓦片时，命中与否取决于每张
    瓦片中心落在哪侧，于是出现"隔一张漏一张"的锯齿。真正的 intersects 判据
    从区域一端扫到另一端，命中集合必须是**连续**的。
    """
    zoom = 10
    row = 388  # 北京一带的一行
    tile_range = calculate_tile_range(SQUARE_BOUNDS, zoom)
    row = max(tile_range.min_y, min(tile_range.max_y, row))

    # 区域 = 一条竖直窄带，只占瓦片宽度的 10%，正好切在行中间
    first = tile_bounds(zoom, tile_range.min_x, row)
    band_west = first.west + (first.east - first.west) * 0.45
    band_east = band_west + (first.east - first.west) * 0.1
    region_filter = PolygonTileRegionFilter(box(band_west, -85.0, band_east, 85.0))

    hits = [
        region_filter.intersects_tile_xyz(zoom, x, row)
        for x in range(tile_range.min_x, tile_range.max_x + 1)
    ]

    assert any(hits), "窄带至少该切到一张瓦片"
    # 命中的列必须连续：把 True 的区间取出来，长度要等于 True 的总数
    first_hit = hits.index(True)
    last_hit = len(hits) - 1 - hits[::-1].index(True)
    assert all(hits[first_hit : last_hit + 1]), f"边界上出现空洞：{hits}"


# --------------------------------------------------------------------------- #
# build_region_filter：什么时候**不该**构造过滤器
# --------------------------------------------------------------------------- #
def test_build_region_filter_returns_none_for_none_region() -> None:
    assert build_region_filter(None) is None


def test_build_region_filter_returns_none_for_rectangle() -> None:
    """矩形必须返回 ``None``（表示"不过滤"）。

    这是 Spec §7 的兼容红线：矩形任务走的必须还是 Slice 0~15 那条链路，
    一旦这里返回了过滤器，矩形下载就会多出一层几何判定 —— 数字仍可能对，
    但"矩形行为逐位一致"的保证就没了。
    """
    region = DownloadRegion.rectangle("自定义矩形", SQUARE_BOUNDS)

    assert region.region_type is RegionType.RECTANGLE
    assert build_region_filter(region) is None


def test_build_region_filter_loads_the_boundary_file() -> None:
    region = DownloadRegion(
        region_type=RegionType.POLYGON,
        name="测试方框",
        bounds=SQUARE_BOUNDS,
        region_id="test-square",
        geojson_path=SQUARE,
    )

    region_filter = build_region_filter(region)

    assert isinstance(region_filter, PolygonTileRegionFilter)
    assert region_filter.name == "测试方框"
    assert region_filter.geometry.area == pytest.approx(1.0)
    # 方框中心点必须在区域内 —— 顺带验证几何的经纬度轴没被交换
    # （经度/纬度写反时几何会挪到几内亚湾，面积却一样）
    assert region_filter.geometry.contains(Point(116.5, 39.5))
    assert not region_filter.geometry.contains(Point(39.5, 116.5))


def test_build_region_filter_accepts_a_preloaded_geometry() -> None:
    """已经读过的几何要能直接传进来，避免二次读盘。

    GUI 面板在构造 :class:`~app.geo.RegionTileEstimator` 时会复用自己缓存的
    geometry —— 二次读盘不仅浪费，还可能造成"用户看到的区域"与"实际裁剪的区域"
    来自两次不同的读取。
    """
    region = DownloadRegion(
        region_type=RegionType.POLYGON,
        name="测试方框",
        bounds=SQUARE_BOUNDS,
        region_id="test-square",
        geojson_path=SQUARE,
    )
    geometry = load_geojson_geometry(SQUARE)

    region_filter = build_region_filter(region, geometry=geometry)

    assert region_filter is not None
    assert region_filter.geometry is geometry


def test_build_region_filter_reports_a_missing_boundary_file(work_dir: Path) -> None:
    """边界文件不在时必须抛 ``GeoJSONNotFoundError``（而不是返回 None）。

    返回 ``None`` 会被调用方理解成"不用过滤"，于是静默退化成矩形下载 ——
    正是 Spec §16/§32 明令禁止的那件事。
    """
    missing = work_dir / "nope.geojson"
    region = DownloadRegion(
        region_type=RegionType.POLYGON,
        name="缺失边界",
        bounds=SQUARE_BOUNDS,
        region_id="missing",
        geojson_path=str(missing),
    )

    with pytest.raises(GeoJSONNotFoundError):
        build_region_filter(region)


def test_polygon_region_without_a_path_is_a_programming_error() -> None:
    """多边形区域没带 ``geojson_path`` —— ``DownloadRegion`` 就该拦下它。"""
    with pytest.raises(ValueError, match="geojson_path"):
        DownloadRegion(
            region_type=RegionType.POLYGON,
            name="没有边界文件",
            bounds=SQUARE_BOUNDS,
            region_id="x",
        )
