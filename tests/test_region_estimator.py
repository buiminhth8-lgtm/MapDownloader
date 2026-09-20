"""区域瓦片预估（Slice 16，Spec §8 / §25）。

预估是"用户点计算后看到的那些数字"的来源，因此这里守两件事：

1. **分级正确**：候选 / 选中 / 过滤三个数必须自洽（``候选 - 选中 = 过滤``），
   且逐级明细加起来等于总数；
2. **矩形零变化**：矩形任务的每个数字都要与 Slice 0~15 逐位一致
   （``selected == candidate``、不出现候选/过滤那几行）。

第 2 条不是"顺便兼容一下"，而是 Spec §7 的验收项：矩形是既有用户的主力用法，
它一旦多出"过滤 0 张"这种行、或者数字变了，都算破坏性变更。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import box

from app.geo import (
    DownloadRegion,
    GeoBounds,
    RegionTileEstimate,
    RegionTileEstimator,
    RegionType,
    ZoomRegionStats,
    build_region_filter,
    calculate_tile_range,
    estimate_region_tiles,
    estimate_size_bytes,
    estimate_tile_count,
    format_size,
)

#: 夹具用 ``__file__`` 推导（相对 cwd 的路径从别的目录跑会失败）。
SQUARE = str(Path(__file__).resolve().parent / "fixtures" / "regions" / "square.geojson")
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "regions"

#: 外包矩形：比 ``square.geojson``（116~117）宽出两度，于是必然产生过滤。
ENVELOPE = GeoBounds(116.0, 39.0, 119.0, 40.0)
SQUARE_REGION_BOUNDS = GeoBounds(116.0, 39.0, 117.0, 40.0)


def polygon_region(name: str = "测试方框", path: str = SQUARE) -> DownloadRegion:
    return DownloadRegion(
        region_type=RegionType.POLYGON,
        name=name,
        bounds=ENVELOPE,
        region_id="test-square",
        geojson_path=path,
    )


# --------------------------------------------------------------------------- #
# 矩形：与 Slice ≤15 逐位一致
# --------------------------------------------------------------------------- #
def test_rectangle_selected_equals_candidate() -> None:
    region = DownloadRegion.rectangle("自定义矩形", ENVELOPE)
    estimate = RegionTileEstimator(region).estimate(8, 12)

    assert estimate.region_type is RegionType.RECTANGLE
    assert estimate.is_polygon is False
    # 逐位等于老公式算出来的数 —— 不是"差不多"
    assert estimate.candidate_tiles == estimate_tile_count(ENVELOPE, 8, 12)
    assert estimate.selected_tiles == estimate.candidate_tiles
    assert estimate.filtered_tiles == 0
    assert estimate.filtered_percent == 0.0


def test_rectangle_per_zoom_matches_calculate_tile_range() -> None:
    region = DownloadRegion.rectangle("自定义矩形", ENVELOPE)
    estimate = RegionTileEstimator(region).estimate(6, 12)

    assert [item.zoom for item in estimate.per_zoom] == list(range(6, 13))
    for item in estimate.per_zoom:
        expected = calculate_tile_range(ENVELOPE, item.zoom).tile_count
        assert item.candidate_tiles == expected
        assert item.selected_tiles == expected
        assert item.filtered_tiles == 0


def test_rectangle_does_not_build_a_region_filter() -> None:
    """矩形路径**不构造过滤器**（因此不会去碰 shapely、更不会读边界文件）。

    ``DownloadRegion.rectangle()`` 根本没有 ``geojson_path``；一旦有人给矩形
    也挂上过滤器，Spec §7 "矩形行为逐位一致"的保证就没了 —— 数字也许还对，
    但一条本该与几何库无关的链路被拖进了几何依赖。
    """
    region = DownloadRegion.rectangle("自定义矩形", ENVELOPE)
    estimator = RegionTileEstimator(region)

    assert region.geojson_path is None
    assert estimator.region_filter is None
    assert estimator.estimate(10, 10).selected_tiles > 0


# --------------------------------------------------------------------------- #
# 多边形：选中 < 候选
# --------------------------------------------------------------------------- #
def test_polygon_selection_is_smaller_than_candidate() -> None:
    estimate = RegionTileEstimator(polygon_region()).estimate(8, 10)

    assert estimate.region_type is RegionType.POLYGON
    assert estimate.is_polygon is True
    # 区域只占外包矩形宽度的三分之一 → 必须真的裁掉一批
    assert 0 < estimate.selected_tiles < estimate.candidate_tiles
    assert estimate.filtered_tiles == estimate.candidate_tiles - estimate.selected_tiles
    assert estimate.filtered_percent == pytest.approx(
        estimate.filtered_tiles * 100.0 / estimate.candidate_tiles
    )
    # Spec §8 的验收数字形态：过滤比例应当可观（三分之一宽度 ≈ 六成以上被裁）
    assert estimate.filtered_percent > 30.0


def test_polygon_per_zoom_sums_to_the_totals() -> None:
    estimate = RegionTileEstimator(polygon_region()).estimate(5, 12)

    assert [item.zoom for item in estimate.per_zoom] == list(range(5, 13))
    assert sum(item.candidate_tiles for item in estimate.per_zoom) == estimate.candidate_tiles
    assert sum(item.selected_tiles for item in estimate.per_zoom) == estimate.selected_tiles
    assert sum(item.filtered_tiles for item in estimate.per_zoom) == estimate.filtered_tiles
    # 低 zoom 几乎裁不掉（一张瓦片盖住整个区域），高 zoom 裁得多：
    # 这条单调性正是 Spec §17 要求给出逐 Zoom 明细的理由。
    assert estimate.per_zoom[0].filtered_percent <= estimate.per_zoom[-1].filtered_percent


def test_estimator_and_filter_agree_on_the_selected_count() -> None:
    """预估层与过滤器层是两条独立实现，结果必须一致。

    预估算 selected 用的是 ``iter_selected_tiles`` 的**计数器**，而下载时
    ``DownloadManager`` 用的是同一批坐标逐张判定。两边一旦对不上，进度条
    与最终落盘张数就会差出一个无法解释的缺口。
    """
    region = polygon_region()
    estimator = RegionTileEstimator(region)
    region_filter = estimator.region_filter
    assert region_filter is not None

    estimate = estimator.estimate(10, 12)

    per_zoom_total = 0
    for item in estimate.per_zoom:
        stats = region_filter.count_selected_tiles(ENVELOPE, item.zoom)
        assert stats.selected_tiles == item.selected_tiles
        assert stats.candidate_tiles == item.candidate_tiles
        per_zoom_total += stats.selected_tiles
    assert per_zoom_total == estimate.selected_tiles


def test_polygon_region_fully_covering_the_envelope_filters_nothing() -> None:
    """区域恰好等于外包矩形 → 过滤 0（不该把该留的也滤掉）。

    这张图还有个隐含的结论：瓦片区间是**向外**取的（含边界点的那张瓦片），
    所以边缘的候选瓦片有一部分落在矩形之外 —— 它们靠着"与矩形有重叠"仍然
    必须被选中。区域与矩形一致却出现 ``过滤 > 0``，说明判定把边缘瓦片丢了。
    """
    region = polygon_region()
    envelope_box = box(ENVELOPE.west, ENVELOPE.south, ENVELOPE.east, ENVELOPE.north)

    estimate = RegionTileEstimator(region, geometry=envelope_box).estimate(8, 10)

    assert estimate.selected_tiles == estimate.candidate_tiles
    assert estimate.filtered_tiles == 0


def test_geometry_can_be_injected_to_avoid_a_second_read() -> None:
    """传了 ``geometry`` 就不该再读盘（用不存在的路径做反证）。"""
    region = DownloadRegion(
        region_type=RegionType.POLYGON,
        name="注入几何",
        bounds=ENVELOPE,
        region_id="injected",
        geojson_path=str(FIXTURES / "definitely-not-here.geojson"),
    )
    from app.geo import load_geojson_geometry

    geometry = load_geojson_geometry(FIXTURES / "square.geojson")

    # 不注入 → 会去读那个不存在的文件
    with pytest.raises(Exception):
        RegionTileEstimator(region).estimate(10, 10)

    # 注入 → 正常出数
    estimate = RegionTileEstimator(region, geometry=geometry).estimate(10, 10)
    assert estimate.selected_tiles > 0


def test_explicit_region_filter_is_reused() -> None:
    """已经构造好的过滤器要能复用（prepared 索引不重建）。"""
    region = polygon_region()
    shared = build_region_filter(region)
    assert shared is not None

    estimator = RegionTileEstimator(region, region_filter=shared)

    assert estimator.region_filter is shared
    assert estimator.estimate(10, 10).selected_tiles > 0


# --------------------------------------------------------------------------- #
# 容量 / 请求量：全部以 selected 为准
# --------------------------------------------------------------------------- #
def test_size_and_requests_are_derived_from_selected() -> None:
    """容量与请求量必须按 **selected** 算。

    用候选算的后果：预估容量虚高（用户会以为要下 3 倍的数据），
    请求量虚高（用户会以为配额要花 3 倍）。
    """
    estimate = RegionTileEstimator(polygon_region()).estimate(8, 10)

    assert estimate.estimated_size_bytes == estimate_size_bytes(estimate.selected_tiles)
    assert estimate.size_text == format_size(estimate_size_bytes(estimate.selected_tiles))
    assert estimate.requests() == estimate.selected_tiles
    assert estimate.requests(per_tile=2) == estimate.selected_tiles * 2
    # 反证：按候选算会是另一个（更大的）数
    assert estimate.estimated_size_bytes != estimate_size_bytes(estimate.candidate_tiles)


def test_requests_with_a_nonpositive_multiplier_is_zero() -> None:
    estimate = RegionTileEstimator(polygon_region()).estimate(10, 10)

    assert estimate.requests(per_tile=0) == 0
    assert estimate.requests(per_tile=-3) == 0


def test_filtered_percent_text_is_one_decimal() -> None:
    estimate = RegionTileEstimator(polygon_region()).estimate(8, 10)

    assert estimate.filtered_percent_text.endswith("%")
    assert estimate.filtered_percent_text == f"{estimate.filtered_percent:.1f}%"


# --------------------------------------------------------------------------- #
# 展示文案
# --------------------------------------------------------------------------- #
def test_rectangle_lines_look_like_slice_15() -> None:
    """矩形**不出现**属于多边形的那几行汇总（区域 / 候选 / 区域内 / 过滤 / 比例）。

    对矩形而言它们恒等于"候选 = 选中、过滤 = 0"，是纯噪音；而且会让
    Slice ≤15 的消息式验收（"结果区就那几行"）失效。

    逐 Zoom 明细是**两种模式共有**的行（Slice 1 起日志里就有"各级瓦片数"），
    所以这里只按**行首标记**判断，不对 "过滤" 这个词做全局否定 —— 那样会把
    共有的明细行一起否掉。
    """
    estimate = RegionTileEstimator(DownloadRegion.rectangle("自定义矩形", ENVELOPE)).estimate(
        8, 10
    )

    lines = estimate.as_lines()

    assert lines[0] == "区域：自定义矩形"
    assert "预计瓦片：74" in lines or any(line.startswith("预计瓦片：") for line in lines)
    for marker in ("候选瓦片：", "区域内瓦片：", "过滤瓦片：", "过滤比例："):
        assert not any(line.startswith(marker) for line in lines), marker
    # 逐 Zoom 明细照旧（矩形也有）
    assert any(line.startswith("  z10") for line in lines)


def test_polygon_lines_show_candidate_selected_filtered_and_zoom_detail() -> None:
    """Spec §17：五件事一个都不能少（区域 / 候选 / 区域内 / 过滤 / 比例）。"""
    estimate = RegionTileEstimator(polygon_region()).estimate(10, 12)

    lines = estimate.as_lines()
    text = "\n".join(lines)

    assert lines[0] == "区域：测试方框"
    assert f"候选瓦片：{estimate.candidate_tiles:,}" in lines
    assert f"区域内瓦片：{estimate.selected_tiles:,}" in lines
    assert f"过滤瓦片：{estimate.filtered_tiles:,}" in lines
    assert f"过滤比例：{estimate.filtered_percent_text}" in lines
    assert f"预计容量：{estimate.size_text}" in lines
    assert "Zoom 明细：" in text
    for item in estimate.per_zoom:
        assert f"z{item.zoom}" in text
    # 逐级明细里三个数都要在（只给一个"选中"看不出被裁掉多少）
    assert f"候选 {estimate.per_zoom[0].candidate_tiles:,}" in text


def test_zoom_detail_can_be_switched_off() -> None:
    estimate = RegionTileEstimator(polygon_region()).estimate(10, 12)

    text = "\n".join(estimate.as_lines(zoom_detail=False))

    assert "Zoom 明细" not in text
    assert f"区域内瓦片：{estimate.selected_tiles:,}" in text


def test_lines_report_the_merged_request_count() -> None:
    """开中文注记后每张瓦片两次请求 —— 文案要跟着翻倍。"""
    estimate = RegionTileEstimator(polygon_region()).estimate(10, 10)

    single = "\n".join(estimate.as_lines(per_tile=1))
    merged = "\n".join(estimate.as_lines(per_tile=2))

    assert f"WMTS 请求：{estimate.selected_tiles:,}" in single
    assert f"WMTS 请求：{estimate.selected_tiles * 2:,}" in merged


# --------------------------------------------------------------------------- #
# 参数校验
# --------------------------------------------------------------------------- #
def test_min_zoom_greater_than_max_zoom_is_rejected() -> None:
    with pytest.raises(ValueError, match="min_zoom 不能大于 max_zoom"):
        RegionTileEstimator(DownloadRegion.rectangle("x", ENVELOPE)).estimate(12, 8)


@pytest.mark.parametrize("zoom", [-1, 23])
def test_out_of_range_zoom_is_rejected(zoom: int) -> None:
    estimator = RegionTileEstimator(DownloadRegion.rectangle("x", ENVELOPE))

    with pytest.raises(ValueError):
        estimator.estimate(zoom, 10)
    with pytest.raises(ValueError):
        estimator.estimate(0, zoom)


def test_estimator_rejects_a_non_region_object() -> None:
    with pytest.raises(ValueError, match="必须是 DownloadRegion"):
        RegionTileEstimator(ENVELOPE)  # type: ignore[arg-type]


def test_single_zoom_range_reports_one_row() -> None:
    estimate = RegionTileEstimator(polygon_region()).estimate(12, 12)

    assert len(estimate.per_zoom) == 1
    assert estimate.per_zoom[0].zoom == 12


# --------------------------------------------------------------------------- #
# 便捷入口
# --------------------------------------------------------------------------- #
def test_estimate_region_tiles_matches_the_estimator() -> None:
    region = polygon_region()

    direct = estimate_region_tiles(region, 10, 12)
    manual = RegionTileEstimator(region).estimate(10, 12)

    assert direct == manual


def test_estimate_is_frozen_and_comparable() -> None:
    """``RegionTileEstimate`` / ``ZoomRegionStats`` 都是不可变值对象。

    不可变让"算完之后再被谁改一笔"这种问题在语言层面就不可能发生 ——
    预估结果会被 UI、日志、大任务判断三处同时读，可变对象迟早有一处踩到
    另一处留下的状态。
    """
    estimate = RegionTileEstimator(polygon_region()).estimate(10, 10)

    assert isinstance(estimate, RegionTileEstimate)
    assert isinstance(estimate.per_zoom[0], ZoomRegionStats)
    with pytest.raises(Exception):
        estimate.selected_tiles = 0  # type: ignore[misc]
    with pytest.raises(Exception):
        estimate.per_zoom[0].zoom = 3  # type: ignore[misc]
