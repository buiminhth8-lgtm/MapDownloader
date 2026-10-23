"""地面分辨率换算与无人机档位表（Slice 29）。

这个模块是"给无人机选 Zoom"的判据来源，因此测试的重点不是"函数能跑"，
而是**换算本身对不对** —— 一个错的 mpp 会让整张推荐表都错，而且错得很安静
（用户看不出"Z16 应该是 2 米"还是"3 米"）。

已知值取自公开的路网常识：
- 赤道 Z0 一像素 = 赤道周长 / 256 ≈ 156543 米；
- 北纬 32° 的 Z17 ≈ 1 米/像素（这也是"能看清一小块田"的量级）；
- 每加一级，米/像素减半。
"""

from __future__ import annotations

import itertools
import math

import pytest

from app.config.settings import MAX_SUPPORTED_ZOOM, MIN_SUPPORTED_ZOOM
from app.planning.ground_resolution import (
    EARTH_CIRCUMFERENCE_METERS,
    UAV_BASELINE_ZOOM,
    UAV_ZOOM_PRESETS,
    UavZoomPreset,
    describe_resolution,
    meters_per_pixel,
    resolution_range,
    zoom_covering,
)


# --------------------------------------------------------------------------- #
# meters_per_pixel：换算本身
# --------------------------------------------------------------------------- #
def test_equator_zoom_one_matches_the_web_mercator_constant() -> None:
    """赤道 Z1 一像素 ≈ 78271.52 米（Z0 的定义值 156543.03 的一半）。

    这里从 Z1 起算而不是 Z0：项目的 ``MIN_SUPPORTED_ZOOM`` 是 1，
    Z0 在下载器里不可达。换算函数与下载器的合法范围保持一致，
    免得"算得出 Z0 却下不了"。
    """
    assert meters_per_pixel(1, 0.0) == pytest.approx(78271.52, rel=1e-6)
    assert meters_per_pixel(1, 0.0) == pytest.approx(
        EARTH_CIRCUMFERENCE_METERS / 512, rel=1e-12
    )


def test_each_zoom_level_halves_the_resolution() -> None:
    """加一级 = 米/像素减半。这是整个金字塔的基础约定，坏了全套估算都错。"""
    for zoom in range(1, 20):
        assert meters_per_pixel(zoom + 1) == pytest.approx(
            meters_per_pixel(zoom) / 2, rel=1e-12
        )


def test_latitude_makes_the_resolution_finer_not_coarser() -> None:
    """纬度越高，同一级 Zoom 的米/像素**越小**（图更清楚）。

    这一条容易搞反：墨卡托把高纬"拉伸"了，所以屏幕上同样的像素数对应
    更少的地面距离。写成 ``/ cos`` 会让哈尔滨的 Z16 比广州的"粗 30 度"，
    推荐值在北方就全部偏保守。
    """
    equator = meters_per_pixel(16, 0.0)
    mid = meters_per_pixel(16, 32.0)
    north = meters_per_pixel(16, 45.0)
    assert mid < equator
    assert north < mid
    # 32° 处的 cos 是 0.848 → 比赤道细 15%
    assert mid == pytest.approx(equator * math.cos(math.radians(32.0)), rel=1e-12)


@pytest.mark.parametrize(
    ("zoom", "latitude", "expected"),
    [
        (16, 32.0, 2.03),
        (17, 32.0, 1.01),
        (18, 32.0, 0.51),
        (10, 32.0, 129.64),
    ],
)
def test_known_anchor_values(zoom: int, latitude: float, expected: float) -> None:
    """几个固定锚点：它们就是推荐区间里那些数字的依据。"""
    assert meters_per_pixel(zoom, latitude) == pytest.approx(expected, abs=0.03)


@pytest.mark.parametrize("bad_zoom", ["16", 16.0, True, None])
def test_zoom_must_be_a_real_integer(bad_zoom: object) -> None:
    """``True`` 也是 int 的子类，但它不是 Zoom —— 放过去会得到 Z1 的答案。"""
    with pytest.raises(ValueError, match="zoom"):
        meters_per_pixel(bad_zoom)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_zoom", [0, 23, -1])
def test_zoom_out_of_supported_range_is_rejected(bad_zoom: int) -> None:
    with pytest.raises(ValueError, match=r"zoom|范围"):
        meters_per_pixel(bad_zoom)


@pytest.mark.parametrize("bad_lat", [85.1, -86.0, 100.0])
def test_latitude_outside_mercator_is_rejected(bad_lat: float) -> None:
    """超出 ±85.05° 时墨卡托无定义（cos 接近 0），必须拒绝而不是返回 0。"""
    with pytest.raises(ValueError, match="latitude"):
        meters_per_pixel(12, bad_lat)


# --------------------------------------------------------------------------- #
# resolution_range：区间
# --------------------------------------------------------------------------- #
def test_range_returns_coarse_first_then_fine() -> None:
    coarse, fine = resolution_range(14, 16, 32.0)
    assert coarse > fine
    assert coarse == pytest.approx(meters_per_pixel(14, 32.0))
    assert fine == pytest.approx(meters_per_pixel(16, 32.0))


def test_range_of_a_single_zoom_has_equal_ends() -> None:
    coarse, fine = resolution_range(16, 16, 32.0)
    assert coarse == fine


def test_range_rejects_inverted_zooms() -> None:
    with pytest.raises(ValueError, match="min_zoom"):
        resolution_range(16, 14, 32.0)


# --------------------------------------------------------------------------- #
# zoom_covering：反解
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("zoom", [12, 14, 16, 17, 18])
def test_zoom_covering_round_trips(zoom: int) -> None:
    """拿这一级的米/像素去反解，应该回到它自己（边界上取整不会跳到下一级）。"""
    latitude = 32.0
    mpp = meters_per_pixel(zoom, latitude)
    assert zoom_covering(mpp, latitude) == zoom


def test_zoom_covering_rounds_up_to_the_finer_level() -> None:
    """要"至少 1.5 米"时给 Z17（1.01）而不是 Z16（2.03）—— 宁细勿粗。"""
    assert zoom_covering(1.5, 32.0) == 17
    assert zoom_covering(2.03, 32.0) == 16


def test_zoom_covering_rejects_impossible_requests() -> None:
    """比 Z18 还细的要求做不到：天地图主站没有 Z19 的瓦片。"""
    with pytest.raises(ValueError, match="超出支持范围"):
        zoom_covering(0.1, 32.0)


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
def test_zoom_covering_rejects_non_positive_or_nan(bad: float) -> None:
    with pytest.raises(ValueError, match=r"正数|meters"):
        zoom_covering(bad, 32.0)


# --------------------------------------------------------------------------- #
# describe_resolution：界面文案
# --------------------------------------------------------------------------- #
def test_describe_keeps_one_decimal_above_one_meter() -> None:
    assert describe_resolution(14, 16, 32.0) == "约 8.1 ~ 2.0 米/像素"


def test_describe_keeps_two_decimals_below_one_meter() -> None:
    """粗的一端（1.0）用一位小数，细的一端（0.51）保留两位。

    两位是给 "0.5 还是 0.4" 这种判断留的：Z18 是 0.51、Z19 才是 0.26，
    合成一位会把两级说成一样。
    """
    assert describe_resolution(17, 18, 32.0) == "约 1.0 ~ 0.51 米/像素"


def test_describe_drops_decimals_for_kilometre_scale() -> None:
    text = describe_resolution(5, 6, 32.0)
    assert text == "约 4149 ~ 2074 米/像素"


# --------------------------------------------------------------------------- #
# 无人机档位表
# --------------------------------------------------------------------------- #
def test_every_preset_is_within_the_supported_zoom_range() -> None:
    for preset in UAV_ZOOM_PRESETS:
        assert MIN_SUPPORTED_ZOOM <= preset.min_zoom <= preset.max_zoom
        assert preset.max_zoom <= MAX_SUPPORTED_ZOOM


def test_presets_are_ordered_from_coarse_to_fine() -> None:
    """档位要按"由粗到细"排，界面上才不会跳来跳去。"""
    for lower, upper in itertools.pairwise(UAV_ZOOM_PRESETS):
        assert lower.min_zoom <= upper.min_zoom
        assert lower.max_zoom <= upper.max_zoom


def test_presets_cover_the_baseline_zoom() -> None:
    """默认档必须真的落在某一档里，否则"取推荐值"会给出没人认领的层级。"""
    covering = [p for p in UAV_ZOOM_PRESETS if p.min_zoom <= UAV_BASELINE_ZOOM <= p.max_zoom]
    assert covering, f"Z{UAV_BASELINE_ZOOM} 不在任何一档里"


def test_baseline_is_the_data_saturation_point() -> None:
    """默认档钉在 Z16：约 2 米/像素，对应天地图农村影像（高分一号/六号）的原生分辨率。

    这条断言的作用是**拦住"顺手把默认值调到 18"**：Z17/Z18 在多数非城区只是
    插值放大，体积却是 4 倍 / 16 倍。要在城区要更高精度，用 ``inspect`` 档。
    """
    assert UAV_BASELINE_ZOOM == 16
    mpp = meters_per_pixel(UAV_BASELINE_ZOOM, 32.0)
    assert 1.5 < mpp < 2.5, f"Z{UAV_BASELINE_ZOOM} 在北纬 32° 应约 2 米/像素，实测 {mpp:.2f}"


def test_preset_keys_are_unique_and_named() -> None:
    keys = [p.key for p in UAV_ZOOM_PRESETS]
    assert len(keys) == len(set(keys))
    assert all(p.name.strip() and p.purpose.strip() for p in UAV_ZOOM_PRESETS)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"key": " "}, "key"),
        ({"name": ""}, "name"),
        ({"min_zoom": 16, "max_zoom": 14}, "区间"),
        ({"max_zoom": 19}, "超出支持范围"),
    ],
)
def test_preset_rejects_invalid_values(kwargs: dict, match: str) -> None:
    base = {"key": "k", "name": "n", "min_zoom": 14, "max_zoom": 16, "purpose": "p"}
    base.update(kwargs)
    with pytest.raises(ValueError, match=match):
        UavZoomPreset(**base)  # type: ignore[arg-type]


def test_preset_range_text_is_compact() -> None:
    preset = UAV_ZOOM_PRESETS[0]
    assert preset.range_text == f"Z{preset.min_zoom}~{preset.max_zoom}"
