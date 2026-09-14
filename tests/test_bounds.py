"""``app.geo.bounds`` 单元测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.config.settings import WEB_MERCATOR_MAX_LAT
from app.geo import GeoBounds


# --------------------------------------------------------------------------- #
# 正常构造与派生属性
# --------------------------------------------------------------------------- #
def test_defaults_and_tuple() -> None:
    bounds = GeoBounds(116.0, 39.0, 117.0, 40.0)
    assert bounds.as_tuple() == (116.0, 39.0, 117.0, 40.0)
    assert bounds.width == pytest.approx(1.0)
    assert bounds.height == pytest.approx(1.0)


def test_center_returns_lat_lon_order() -> None:
    """center 的返回顺序是 (lat, lon)，别写反。"""
    lat, lon = GeoBounds(116.0, 39.0, 117.0, 40.0).center
    assert lat == pytest.approx(39.5)
    assert lon == pytest.approx(116.5)


def test_contains_includes_boundary() -> None:
    bounds = GeoBounds(116.0, 39.0, 117.0, 40.0)
    assert bounds.contains(116.0, 39.0) is True
    assert bounds.contains(117.0, 40.0) is True
    assert bounds.contains(116.5, 39.5) is True
    assert bounds.contains(115.9, 39.5) is False
    assert bounds.contains(116.5, 40.1) is False


def test_is_frozen_and_hashable() -> None:
    bounds = GeoBounds(116.0, 39.0, 117.0, 40.0)
    with pytest.raises(FrozenInstanceError):
        bounds.west = 0.0  # type: ignore[misc]
    assert len({bounds, GeoBounds(116.0, 39.0, 117.0, 40.0)}) == 1


def test_ints_are_normalised_to_float() -> None:
    bounds = GeoBounds(116, 39, 117, 40)
    assert isinstance(bounds.west, float)
    assert isinstance(bounds.north, float)


# --------------------------------------------------------------------------- #
# 合法边界
# --------------------------------------------------------------------------- #
def test_full_longitude_range_is_allowed() -> None:
    bounds = GeoBounds(-180.0, -85.0, 180.0, 85.0)
    assert bounds.width == pytest.approx(360.0)


def test_web_mercator_latitude_limits_are_allowed() -> None:
    bounds = GeoBounds(0.0, -WEB_MERCATOR_MAX_LAT, 1.0, WEB_MERCATOR_MAX_LAT)
    assert bounds.north == pytest.approx(WEB_MERCATOR_MAX_LAT)


def test_tiny_but_ordered_extent_is_allowed() -> None:
    bounds = GeoBounds(116.0, 39.0, 116.000001, 39.000001)
    assert bounds.width > 0.0


# --------------------------------------------------------------------------- #
# 非法参数
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("west", "south", "east", "north"),
    [
        (117.0, 39.0, 116.0, 40.0),  # west > east
        (116.0, 39.0, 116.0, 40.0),  # west == east
        (116.0, 40.0, 117.0, 39.0),  # south > north
        (116.0, 39.0, 117.0, 39.0),  # south == north
    ],
)
def test_inverted_or_degenerate_extent_raises(
    west: float, south: float, east: float, north: float
) -> None:
    with pytest.raises(ValueError):
        GeoBounds(west, south, east, north)


@pytest.mark.parametrize(
    ("west", "east"),
    [
        (-180.1, 10.0),  # west 越界
        (10.0, 180.1),  # east 越界
        (-200.0, 200.0),  # 两端都越界
    ],
)
def test_longitude_out_of_range_raises(west: float, east: float) -> None:
    with pytest.raises(ValueError):
        GeoBounds(west, 0.0, east, 10.0)


@pytest.mark.parametrize(
    ("south", "north"),
    [
        (-85.1, 10.0),
        (-10.0, 85.1),
        (-WEB_MERCATOR_MAX_LAT - 0.001, WEB_MERCATOR_MAX_LAT + 0.001),
    ],
)
def test_latitude_beyond_web_mercator_raises(south: float, north: float) -> None:
    with pytest.raises(ValueError):
        GeoBounds(0.0, south, 10.0, north)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_values_raise(bad: float) -> None:
    with pytest.raises(ValueError):
        GeoBounds(0.0, 0.0, bad, 10.0)
    with pytest.raises(ValueError):
        GeoBounds(bad, 0.0, 10.0, 10.0)


@pytest.mark.parametrize("bad", ["abc", None, object()])
def test_non_numeric_values_raise(bad: object) -> None:
    with pytest.raises(ValueError):
        GeoBounds(bad, 0.0, 10.0, 10.0)  # type: ignore[arg-type]


def test_error_message_names_the_offending_field() -> None:
    """报错信息要能直接指出哪个字段不合法。"""
    with pytest.raises(ValueError, match="west 必须小于 east"):
        GeoBounds(117.0, 39.0, 116.0, 40.0)
    with pytest.raises(ValueError, match="south 必须小于 north"):
        GeoBounds(116.0, 40.0, 117.0, 39.0)
