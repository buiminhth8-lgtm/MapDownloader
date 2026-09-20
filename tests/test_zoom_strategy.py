"""Zoom 策略服务（Slice 17，Spec §5 / §9 / §10）。

两块内容：

- **推荐 / 评估**：``recommend``、``evaluate``。核心主张是"只提醒、不改写" ——
  用户设了 Z8~18，返回值里就必须还是 8~18，只是多一个 ``warning``；
- **推荐 + 预估**：``recommend_with_estimate``。它必须**复用**
  :class:`~app.geo.RegionTileEstimator`，因此这里的期望值不是写死的数字，
  而是"直接调一次估算器，结果必须逐字段一致"。

瓦片数一律由**生产同一套函数**算出来（``calculate_tile_range`` /
``RegionTileEstimator``），不写"大概是 60 张"这种推导值。
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from app.config.settings import DEFAULT_AVERAGE_TILE_SIZE, MAX_SUPPORTED_ZOOM
from app.geo import (
    DownloadRegion,
    GeoBounds,
    RegionTileEstimator,
    RegionType,
    calculate_tile_range,
    format_size,
    tile_x_to_lon,
    tile_y_to_lat,
)
from app.planning import (
    DEFAULT_ZOOM_POLICIES,
    PLAN_SIZE_LABEL,
    PLAN_TILES_LABEL,
    RegionLevel,
    ZoomPlanEstimate,
    ZoomPolicy,
    ZoomStrategyService,
    get_zoom_policy,
)

ZOOM = 12

#: 与 ``tests/test_polygon_download.py`` 同一块地（北京一带的 10×10 瓦片）。
BASE_X, BASE_Y = 3372, 1552
BLOCK = 10

#: 命中多边形覆盖的列 / 行数（缩进到瓦片内部，见 ``_inset_polygon``）。
HIT_COLS, HIT_ROWS = 6, 10

#: 让矩形停在"最后一张瓦片内部"，避免把东边界那张瓦片也算成候选。
EDGE_NUDGE = 1e-4


# --------------------------------------------------------------------------- #
# 夹具（全部由瓦片数学算出来）
# --------------------------------------------------------------------------- #
def block_bounds() -> GeoBounds:
    """10×10 瓦片块的外包矩形（z12）。"""
    return GeoBounds(
        west=tile_x_to_lon(BASE_X, ZOOM),
        south=tile_y_to_lat(BASE_Y + BLOCK, ZOOM) + EDGE_NUDGE,
        east=tile_x_to_lon(BASE_X + BLOCK, ZOOM) - EDGE_NUDGE,
        north=tile_y_to_lat(BASE_Y, ZOOM),
    )


def _row_height(row: int) -> float:
    return tile_y_to_lat(row, ZOOM) - tile_y_to_lat(row + 1, ZOOM)


def _inset_polygon() -> list[list[list[float]]]:
    """列 ``BASE_X .. +5`` × 行 ``BASE_Y .. +9`` 的多边形，四边缩进 1/4 瓦片。

    缩进去是为了让"命中哪几张"是确定的 6×10：``intersects`` 把"只共一条边"
    也算相交，边贴着瓦片画会白白多出一整列 / 一行。
    """
    column = 360.0 / (2**ZOOM)
    west = tile_x_to_lon(BASE_X, ZOOM) + column * 0.25
    east = tile_x_to_lon(BASE_X + HIT_COLS, ZOOM) - column * 0.25
    north = tile_y_to_lat(BASE_Y, ZOOM) - _row_height(BASE_Y) * 0.25
    south = tile_y_to_lat(BASE_Y + HIT_ROWS, ZOOM) + _row_height(
        BASE_Y + HIT_ROWS - 1
    ) * 0.25
    return [[[west, south], [east, south], [east, north], [west, north], [west, south]]]


@pytest.fixture()
def rectangle() -> DownloadRegion:
    """自定义矩形区域（不读任何文件）。"""
    return DownloadRegion.rectangle("测试矩形", block_bounds())


@pytest.fixture()
def polygon(work_dir: Path) -> DownloadRegion:
    """多边形区域：库里的候选是 10×10=100，真正命中的是 6×10=60。"""
    path = work_dir / "square-in-block.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {"name": "块内方框", "boundaryVersion": "2026.09-test"},
                "geometry": {"type": "Polygon", "coordinates": _inset_polygon()},
            }
        ),
        encoding="utf-8",
    )
    return DownloadRegion(
        region_type=RegionType.POLYGON,
        name="块内方框",
        bounds=block_bounds(),
        region_id="test-square",
        boundary_version="2026.09-test",
        geojson_path=str(path),
        region_level=RegionLevel.CITY,
    )


@pytest.fixture()
def service() -> ZoomStrategyService:
    return ZoomStrategyService()


# --------------------------------------------------------------------------- #
# get_policy / policies
# --------------------------------------------------------------------------- #
def test_default_service_uses_the_registry(service: ZoomStrategyService) -> None:
    assert service.get_policy(RegionLevel.COUNTRY) is get_zoom_policy(RegionLevel.COUNTRY)
    assert service.policies == dict(DEFAULT_ZOOM_POLICIES)


def test_policies_property_is_a_copy(service: ZoomStrategyService) -> None:
    """拿到的是副本：改它不该影响服务（策略表只有一处权威）。"""
    snapshot = service.policies
    assert isinstance(snapshot, dict)
    snapshot.clear()
    assert service.get_policy(RegionLevel.CITY) is get_zoom_policy(RegionLevel.CITY)


def test_service_rejects_an_incomplete_policy_table() -> None:
    with pytest.raises(ValueError, match="缺少等级"):
        ZoomStrategyService(
            {RegionLevel.COUNTRY: DEFAULT_ZOOM_POLICIES[RegionLevel.COUNTRY]}
        )


def test_service_can_be_fed_a_custom_table() -> None:
    """策略表可注入（将来"用户自定喜好"用得上），且注入的那张说了算。"""
    table = dict(DEFAULT_ZOOM_POLICIES)
    table[RegionLevel.COUNTRY] = ZoomPolicy(
        level=RegionLevel.COUNTRY,
        recommended_min_zoom=3,
        recommended_max_zoom=6,
        warning_max_zoom=6,
        description="测试用：全国只到 Z6。",
    )
    custom = ZoomStrategyService(table)
    assert custom.get_policy(RegionLevel.COUNTRY).recommended_range_text == "Z3~6"
    assert custom.recommend(RegionLevel.COUNTRY).range_text == "Z3~6"
    # 默认服务不受影响（互不干扰）
    assert get_zoom_policy(RegionLevel.COUNTRY).recommended_range_text == "Z4~10"


@pytest.mark.parametrize("bad", ["country", None, 4, object()])
def test_get_policy_rejects_bad_levels(
    service: ZoomStrategyService, bad: object
) -> None:
    with pytest.raises(ValueError, match="RegionLevel"):
        service.get_policy(bad)


# --------------------------------------------------------------------------- #
# recommend：纯推荐
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("level", "low", "high"),
    [
        (RegionLevel.COUNTRY, 4, 10),
        (RegionLevel.PROVINCE, 9, 13),
        (RegionLevel.CITY, 12, 16),
        (RegionLevel.MISSION, 15, 18),
    ],
)
def test_recommend_returns_the_policy_range(
    service: ZoomStrategyService, level: RegionLevel, low: int, high: int
) -> None:
    recommendation = service.recommend(level)
    assert (recommendation.min_zoom, recommendation.max_zoom) == (low, high)
    assert recommendation.level is level
    assert recommendation.warning is False, "还没有当前 Zoom，谈不上超出"
    assert recommendation.has_request is False
    assert recommendation.requested_range_text is None
    assert recommendation.exceeds_recommendation is False
    assert recommendation.reason == service.get_policy(level).description
    assert recommendation.as_lines() == [f"推荐 Zoom：Z{low}~{high}"]


def test_recommend_for_custom_still_answers(service: ZoomStrategyService) -> None:
    recommendation = service.recommend(RegionLevel.CUSTOM)
    assert (recommendation.min_zoom, recommendation.max_zoom) == (1, MAX_SUPPORTED_ZOOM)
    assert recommendation.warning is False


# --------------------------------------------------------------------------- #
# evaluate：评估用户设置（只提醒，不改写）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("level", "low", "high", "warning"),
    [
        # Spec §13 点名的四条
        (RegionLevel.COUNTRY, 4, 10, False),
        (RegionLevel.COUNTRY, 8, 18, True),
        (RegionLevel.PROVINCE, 9, 13, False),
        (RegionLevel.CITY, 12, 16, False),
        (RegionLevel.MISSION, 15, 18, False),
        # 阈值上下各一条：警告的判据是 max_zoom > warning_max_zoom
        (RegionLevel.COUNTRY, 4, 11, True),
        (RegionLevel.PROVINCE, 9, 14, False),  # Z14 是过渡带，不警告
        (RegionLevel.PROVINCE, 9, 15, True),
        (RegionLevel.CITY, 12, 17, True),
        (RegionLevel.MISSION, 15, 19, True),
        # 自定义等级：合法 Zoom 一律不因等级策略报警（Spec §5）
        (RegionLevel.CUSTOM, 1, 18, False),
        (RegionLevel.CUSTOM, 12, 20, False),
        (RegionLevel.CUSTOM, 0, 22, False),
    ],
)
def test_evaluate_decides_warning_from_the_upper_bound(
    service: ZoomStrategyService, level: RegionLevel, low: int, high: int, warning: bool
) -> None:
    recommendation = service.evaluate(level, low, high)
    assert recommendation.warning is warning
    assert recommendation.level is level
    assert (recommendation.requested_min_zoom, recommendation.requested_max_zoom) == (
        low,
        high,
    )


def test_evaluate_never_rewrites_the_user_zoom(service: ZoomStrategyService) -> None:
    """Spec §10 的核心：中国 + Z8~18 → 推荐 Z4~10、warning=True，但**不改** 8~18。"""
    recommendation = service.evaluate(RegionLevel.COUNTRY, 8, 18)
    assert (recommendation.min_zoom, recommendation.max_zoom) == (4, 10)
    assert (recommendation.requested_min_zoom, recommendation.requested_max_zoom) == (
        8,
        18,
    )
    assert recommendation.warning is True
    assert recommendation.exceeds_recommendation is True
    assert "Z4~10" in recommendation.warning_text
    assert "Z8~18" in recommendation.warning_text


def test_evaluate_within_recommendation_is_quiet(service: ZoomStrategyService) -> None:
    recommendation = service.evaluate(RegionLevel.COUNTRY, 4, 10)
    assert recommendation.warning is False
    assert recommendation.exceeds_recommendation is False
    assert recommendation.warning_text == ""
    assert recommendation.as_lines() == ["推荐 Zoom：Z4~10", "当前 Zoom：Z4~10"]


def test_evaluate_warning_line_carries_the_reason(
    service: ZoomStrategyService,
) -> None:
    lines = service.evaluate(RegionLevel.COUNTRY, 8, 18).as_lines()
    assert len(lines) == 3
    assert lines[0] == "推荐 Zoom：Z4~10"
    assert lines[1] == "当前 Zoom：Z8~18"
    assert lines[2].startswith("警告：")
    assert get_zoom_policy(RegionLevel.COUNTRY).description in lines[2]


def test_evaluate_marks_a_too_wide_low_end_as_exceeding(
    service: ZoomStrategyService,
) -> None:
    """下界低于建议也算"超出建议"，但不触发警告（代价更小，不需要提醒）。"""
    recommendation = service.evaluate(RegionLevel.CITY, 8, 16)
    assert recommendation.exceeds_recommendation is True
    assert recommendation.warning is False


def test_recommendation_is_immutable(service: ZoomStrategyService) -> None:
    recommendation = service.evaluate(RegionLevel.COUNTRY, 8, 18)
    with pytest.raises(FrozenInstanceError):
        recommendation.requested_max_zoom = 10  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# evaluate：非法输入
# --------------------------------------------------------------------------- #
def test_evaluate_rejects_reversed_range(service: ZoomStrategyService) -> None:
    with pytest.raises(ValueError, match="min_zoom"):
        service.evaluate(RegionLevel.CITY, 16, 12)


@pytest.mark.parametrize("zoom", [-1, 23, 1.5, "12", None])
def test_evaluate_rejects_out_of_range_or_non_integer_zoom(
    service: ZoomStrategyService, zoom: object
) -> None:
    with pytest.raises(ValueError, match="zoom"):
        service.evaluate(RegionLevel.CITY, zoom, 12)  # type: ignore[arg-type]


@pytest.mark.parametrize("level", ["country", "", None, 0])
def test_evaluate_rejects_unknown_level(
    service: ZoomStrategyService, level: object
) -> None:
    with pytest.raises(ValueError, match="RegionLevel"):
        service.evaluate(level, 4, 10)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# recommend_with_estimate：复用 RegionTileEstimator（Spec §9）
# --------------------------------------------------------------------------- #
def test_estimate_reuses_the_region_tile_estimator(
    service: ZoomStrategyService, rectangle: DownloadRegion
) -> None:
    """期望值不是写死的：直接调一次估算器，两边必须逐字段一致。"""
    plan = service.recommend_with_estimate(rectangle, RegionLevel.COUNTRY)
    expected = RegionTileEstimator(rectangle).estimate(4, 10)

    assert plan.estimate is not None
    assert plan.selected_tiles == expected.selected_tiles
    assert plan.candidate_tiles == expected.candidate_tiles
    assert plan.size_text == expected.size_text
    assert plan.used_recommended_zoom is True
    assert (plan.estimated_min_zoom, plan.estimated_max_zoom) == (4, 10)
    assert plan.average_tile_size == DEFAULT_AVERAGE_TILE_SIZE


def test_estimate_reports_candidate_and_selected_truthfully(
    service: ZoomStrategyService, rectangle: DownloadRegion
) -> None:
    plan = service.recommend_with_estimate(rectangle, RegionLevel.COUNTRY)
    expected_candidate = sum(
        calculate_tile_range(rectangle.bounds, zoom).tile_count
        for zoom in range(4, 11)
    )
    assert plan.candidate_tiles == expected_candidate
    assert plan.selected_tiles == expected_candidate, "矩形：选中 == 候选"


def test_estimate_honours_the_user_zoom_range(
    service: ZoomStrategyService, rectangle: DownloadRegion
) -> None:
    """给了用户 Zoom 就按用户区间预估，并把 warning 一并带回来。"""
    plan = service.recommend_with_estimate(
        rectangle, RegionLevel.COUNTRY, min_zoom=12, max_zoom=12
    )
    assert plan.used_recommended_zoom is False
    assert (plan.estimated_min_zoom, plan.estimated_max_zoom) == (12, 12)
    assert plan.recommendation.warning is True
    expected = RegionTileEstimator(rectangle).estimate(12, 12)
    assert plan.selected_tiles == expected.selected_tiles


def test_estimate_rejects_a_reversed_user_range(
    service: ZoomStrategyService, rectangle: DownloadRegion
) -> None:
    """``18 ~ 8`` 是非法输入：**炸**，不要顺手调成 8~18。

    顺手调过来的后果是"界面写着 18~8、实际按 8~18 下了"——用户永远发现不了，
    直到发现下载量和自己算的对不上。
    """
    with pytest.raises(ValueError, match="min_zoom"):
        service.recommend_with_estimate(
            rectangle, RegionLevel.COUNTRY, min_zoom=18, max_zoom=8
        )


def test_estimate_on_a_polygon_uses_the_real_geometry(
    service: ZoomStrategyService, polygon: DownloadRegion
) -> None:
    """多边形：候选 100 / 区域内 60（z12），与直接调估算器一致。"""
    plan = service.recommend_with_estimate(
        polygon, RegionLevel.CITY, min_zoom=ZOOM, max_zoom=ZOOM
    )
    expected = RegionTileEstimator(polygon).estimate(ZOOM, ZOOM)

    assert plan.candidate_tiles == BLOCK * BLOCK == 100
    assert plan.selected_tiles == HIT_COLS * HIT_ROWS == 60
    assert plan.selected_tiles == expected.selected_tiles
    assert plan.selected_tiles < plan.candidate_tiles, "多边形必须真的裁掉一些"


def test_estimate_lines_show_recommendation_tiles_and_size(
    service: ZoomStrategyService, rectangle: DownloadRegion
) -> None:
    plan = service.recommend_with_estimate(rectangle, RegionLevel.COUNTRY)
    lines = plan.as_lines()
    assert lines[0] == "推荐 Zoom：Z4~10"
    assert f"{PLAN_TILES_LABEL}{plan.selected_tiles:,}" in lines
    assert f"{PLAN_SIZE_LABEL}{plan.size_text}" in lines


def test_estimate_uses_the_given_average_tile_size(
    service: ZoomStrategyService, rectangle: DownloadRegion
) -> None:
    plan = service.recommend_with_estimate(
        rectangle, RegionLevel.COUNTRY, average_tile_size=1024
    )
    assert plan.average_tile_size == 1024
    assert plan.size_text == format_size(plan.selected_tiles * 1024)


def test_estimate_requires_both_zoom_bounds(
    service: ZoomStrategyService, rectangle: DownloadRegion
) -> None:
    with pytest.raises(ValueError, match="同时给出"):
        service.recommend_with_estimate(rectangle, RegionLevel.CITY, min_zoom=12)
    with pytest.raises(ValueError, match="同时给出"):
        service.recommend_with_estimate(rectangle, RegionLevel.CITY, max_zoom=16)


def test_estimate_rejects_a_non_region(
    service: ZoomStrategyService, rectangle: DownloadRegion
) -> None:
    with pytest.raises(ValueError, match="DownloadRegion"):
        service.recommend_with_estimate(block_bounds(), RegionLevel.CITY)  # type: ignore[arg-type]


def test_estimate_rejects_an_inconsistent_zoom_detail(
    service: ZoomStrategyService, rectangle: DownloadRegion
) -> None:
    """明细区间与自报区间对不上时必须炸 —— 那种对象会静默算出错的容量。"""
    plan = service.recommend_with_estimate(rectangle, RegionLevel.COUNTRY)
    with pytest.raises(ValueError, match="预估明细"):
        ZoomPlanEstimate(
            recommendation=plan.recommendation,
            estimate=plan.estimate,
            estimated_min_zoom=4,
            estimated_max_zoom=9,  # 明细其实是 4~10
            average_tile_size=plan.average_tile_size,
            used_recommended_zoom=True,
        )


def test_estimate_does_not_mutate_the_region(
    service: ZoomStrategyService, polygon: DownloadRegion
) -> None:
    """预估只读区域：等级、范围、路径都不许被改（区域是不可变值对象）。"""
    before = (
        polygon.region_level,
        polygon.bounds,
        polygon.geojson_path,
        polygon.name,
    )
    service.recommend_with_estimate(polygon, RegionLevel.CITY, min_zoom=ZOOM, max_zoom=ZOOM)
    after = (
        polygon.region_level,
        polygon.bounds,
        polygon.geojson_path,
        polygon.name,
    )
    assert before == after
    assert polygon.region_level is RegionLevel.CITY
