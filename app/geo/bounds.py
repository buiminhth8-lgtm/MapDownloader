"""地理包围盒（BoundingBox）。

本模块只做"数据描述 + 合法性校验"，不含任何瓦片编号换算（在 ``tile_math``）。

坐标约定：全部为 WGS84 经纬度（EPSG:4326），单位为度。
纬度上界取 Web 墨卡托能表达的极限 ±85.05112878——超出该范围的纬度在
EPSG:3857 中无法表示，直接拒绝而不是静默截断，避免下游算出错误的瓦片。

本 Slice **不支持**跨国际日期变更线（antimeridian）的包围盒，即必须
``west < east``；需要跨线时应拆成两个 GeoBounds。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.config.settings import MAX_LONGITUDE, MIN_LONGITUDE, WEB_MERCATOR_MAX_LAT

__all__ = ["GeoBounds"]


def _check_finite(name: str, value: float) -> float:
    """确认是有限实数（排除 NaN / inf / 非数值）。"""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数值，实际为 {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} 必须是有限数值，实际为 {value!r}")
    return number


@dataclass(frozen=True)
class GeoBounds:
    """矩形地理范围。

    校验规则（任一不满足抛 ``ValueError``）：

    - 四个字段都必须有限数值
    - ``-180 <= west < east <= 180``（不支持跨日期变更线）
    - ``-85.05112878 <= south < north <= 85.05112878``

    >>> GeoBounds(116.0, 39.0, 117.0, 40.0).center
    (39.5, 116.5)
    """

    west: float
    south: float
    east: float
    north: float

    def __post_init__(self) -> None:
        west = _check_finite("west", self.west)
        south = _check_finite("south", self.south)
        east = _check_finite("east", self.east)
        north = _check_finite("north", self.north)

        # 借 frozen dataclass 的合法通道写回归一化后的 float
        object.__setattr__(self, "west", west)
        object.__setattr__(self, "south", south)
        object.__setattr__(self, "east", east)
        object.__setattr__(self, "north", north)

        if not MIN_LONGITUDE <= west <= MAX_LONGITUDE:
            raise ValueError(
                f"west 必须在 [{MIN_LONGITUDE}, {MAX_LONGITUDE}] 度之间，实际为 {west}"
            )
        if not MIN_LONGITUDE <= east <= MAX_LONGITUDE:
            raise ValueError(
                f"east 必须在 [{MIN_LONGITUDE}, {MAX_LONGITUDE}] 度之间，实际为 {east}"
            )
        if west >= east:
            raise ValueError(
                f"west 必须小于 east（本版本不支持跨国际日期变更线），实际 west={west}, east={east}"
            )
        if not -WEB_MERCATOR_MAX_LAT <= south <= WEB_MERCATOR_MAX_LAT:
            raise ValueError(
                f"south 必须在 [-{WEB_MERCATOR_MAX_LAT}, {WEB_MERCATOR_MAX_LAT}] 度之间"
                f"（Web 墨卡托纬度极限），实际为 {south}"
            )
        if not -WEB_MERCATOR_MAX_LAT <= north <= WEB_MERCATOR_MAX_LAT:
            raise ValueError(
                f"north 必须在 [-{WEB_MERCATOR_MAX_LAT}, {WEB_MERCATOR_MAX_LAT}] 度之间"
                f"（Web 墨卡托纬度极限），实际为 {north}"
            )
        if south >= north:
            raise ValueError(
                f"south 必须小于 north，实际 south={south}, north={north}"
            )

    # ------------------------------------------------------------------ #
    # 派生信息
    # ------------------------------------------------------------------ #
    @property
    def center(self) -> tuple[float, float]:
        """矩形中心，返回 ``(lat, lon)``（注意纬度在前）。"""
        return ((self.south + self.north) / 2.0, (self.west + self.east) / 2.0)

    @property
    def width(self) -> float:
        """经度跨度（度）。"""
        return self.east - self.west

    @property
    def height(self) -> float:
        """纬度跨度（度）。"""
        return self.north - self.south

    def as_tuple(self) -> tuple[float, float, float, float]:
        """返回 ``(west, south, east, north)``，便于与既有 dict 结构互转。"""
        return (self.west, self.south, self.east, self.north)

    def contains(self, lon: float, lat: float) -> bool:
        """判断某点是否落在范围内（含边界）。"""
        return self.west <= lon <= self.east and self.south <= lat <= self.north
