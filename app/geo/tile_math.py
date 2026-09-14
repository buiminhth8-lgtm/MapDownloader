"""Web 墨卡托（EPSG:3857）瓦片坐标计算。

纯函数模块：不依赖 UI、不发网络请求、不写磁盘，可被单元测试完全覆盖。

术语与坐标约定
--------------

- **XYZ / Slippy Map**：瓦片编号 ``(z, x, y)``，其中 ``x`` 自西向东增长，
  ``y`` **自北向南**增长。原点 ``(0, 0)`` 在左上角（西北角）。
- **TMS**（Tile Map Service）：``y`` 自南向北增长，``tms_y = 2**z - 1 - xyz_y``。
  MBTiles 内部使用 TMS 约定，因此这组换算是 Slice 3 的关键前置。

公式（标准 Web 墨卡托，``n = 2**zoom``）::

    x = floor((lon + 180) / 360 * n)

    y = floor((1 - asinh(tan(lat_rad)) / pi) / 2 * n)

其中 ``asinh(tan(phi))`` 等价于 ``ln(tan(phi) + sec(phi))``，是墨卡托投影的
纬度变换。

参数校验策略（有意不对称，理由见各函数 docstring）
------------------------------------------------

- ``lon`` 超出 ±180 → 抛 ``ValueError``（本版本不支持跨日期变更线，静默取模会
  算出完全错误的瓦片）。
- ``lat`` 超出 ±85.05112878 → **clamp**（这是 Web 墨卡托对极点的标准抹平方式，
  也是各家实现的通行做法）。
- ``zoom`` 超出 ``[0, 22]`` → 抛 ``ValueError``。
"""

from __future__ import annotations

import math
import operator
from dataclasses import dataclass

from app.config.settings import (
    DEFAULT_AVERAGE_TILE_SIZE,
    MAX_LONGITUDE,
    MIN_LONGITUDE,
    TILE_MATH_MAX_ZOOM,
    TILE_MATH_MIN_ZOOM,
    WEB_MERCATOR_MAX_LAT,
)
from app.geo.bounds import GeoBounds

__all__ = [
    "TileRange",
    "lon_to_tile_x",
    "lat_to_tile_y",
    "calculate_tile_range",
    "calculate_tile_ranges",
    "estimate_tile_count",
    "xyz_y_to_tms_y",
    "tms_y_to_xyz_y",
    "estimate_size_bytes",
    "format_size",
]


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TileRange:
    """某个缩放级别下，覆盖一段地理范围的瓦片编号区间（闭区间）。"""

    zoom: int
    min_x: int
    max_x: int
    min_y: int
    max_y: int

    @property
    def tile_count(self) -> int:
        """区间内瓦片总数。"""
        return (self.max_x - self.min_x + 1) * (self.max_y - self.min_y + 1)

    @property
    def x_count(self) -> int:
        """横向瓦片列数。"""
        return self.max_x - self.min_x + 1

    @property
    def y_count(self) -> int:
        """纵向瓦片行数。"""
        return self.max_y - self.min_y + 1


# --------------------------------------------------------------------------- #
# 参数校验
# --------------------------------------------------------------------------- #
def _validate_zoom(zoom: int) -> int:
    """校验缩放级别，返回规范化后的 int。"""
    try:
        value = operator.index(zoom)
    except TypeError as exc:
        raise ValueError(f"zoom 必须是整数，实际为 {zoom!r}") from exc

    if not TILE_MATH_MIN_ZOOM <= value <= TILE_MATH_MAX_ZOOM:
        raise ValueError(
            f"zoom 必须在 [{TILE_MATH_MIN_ZOOM}, {TILE_MATH_MAX_ZOOM}] 之间，实际为 {value}"
        )
    return value


def _tiles_per_axis(zoom: int) -> int:
    """该缩放级别下每轴的瓦片数 ``2**zoom``。"""
    return 1 << _validate_zoom(zoom)


def _check_finite(name: str, value: float) -> float:
    """确认是有限实数（排除 NaN / inf / 非数值）。"""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数值，实际为 {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} 必须是有限数值，实际为 {value!r}")
    return number


def _clamp(value: int, low: int, high: int) -> int:
    """把整数限制在 ``[low, high]``。"""
    return min(max(value, low), high)


# --------------------------------------------------------------------------- #
# 经纬度 -> XYZ 瓦片
# --------------------------------------------------------------------------- #
def lon_to_tile_x(lon: float, zoom: int) -> int:
    """经度换算为 XYZ 瓦片列号。

    Args:
        lon: 经度（度）。可接受 ``[-180, 180]``，含边界。
        zoom: 缩放级别，``0 ~ 22``。

    Returns:
        ``0 <= x < 2**zoom`` 的列号。

    Raises:
        ValueError: zoom 非法，或 lon 超出 ±180（本版本不支持跨日期变更线）。
    """
    tiles = _tiles_per_axis(zoom)
    longitude = _check_finite("lon", lon)
    if not MIN_LONGITUDE <= longitude <= MAX_LONGITUDE:
        raise ValueError(
            f"lon 必须在 [{MIN_LONGITUDE}, {MAX_LONGITUDE}] 度之间，实际为 {longitude}"
        )

    x = int((longitude + 180.0) / 360.0 * tiles)
    # lon=180 会算出 x=n，钳到 n-1；浮点误差产生的 -1 同理钳回 0
    return _clamp(x, 0, tiles - 1)


def lat_to_tile_y(lat: float, zoom: int) -> int:
    """纬度换算为 XYZ 瓦片行号。

    纬度**超出 Web 墨卡托极限时会被 clamp**（而不是抛异常）——极点附近在
    EPSG:3857 中本就无法表达，抹平到 ±85.05112878 是标准做法。

    Args:
        lat: 纬度（度）。超出 ``±85.05112878`` 会被收敛到该极限。
        zoom: 缩放级别，``0 ~ 22``。

    Returns:
        ``0 <= y < 2**zoom`` 的行号，**自北向南**增长（北半球值更小）。

    Raises:
        ValueError: zoom 非法，或 lat 不是有限数值。
    """
    tiles = _tiles_per_axis(zoom)
    latitude = _check_finite("lat", lat)
    latitude = max(-WEB_MERCATOR_MAX_LAT, min(WEB_MERCATOR_MAX_LAT, latitude))

    lat_rad = math.radians(latitude)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * tiles)
    return _clamp(y, 0, tiles - 1)


# --------------------------------------------------------------------------- #
# BoundingBox -> TileRange
# --------------------------------------------------------------------------- #
def calculate_tile_range(bounds: GeoBounds, zoom: int) -> TileRange:
    """计算覆盖给定地理范围的瓦片区间。

    映射关系（XYZ 的行号自北向南增长）::

        west  -> min_x
        east  -> max_x
        north -> min_y      # 北边界对应的行号更小
        south -> max_y

    注意这里取的是"包含边界点所在的那张瓦片"，因此结果区间是保守的
    （可能略微超出实际覆盖面积），不会漏瓦片。

    Args:
        bounds: 地理范围。
        zoom: 缩放级别，``0 ~ 22``。

    Returns:
        该缩放级别下的 ``TileRange``。

    Raises:
        ValueError: zoom 非法。
    """
    _validate_zoom(zoom)
    return TileRange(
        zoom=zoom,
        min_x=lon_to_tile_x(bounds.west, zoom),
        max_x=lon_to_tile_x(bounds.east, zoom),
        min_y=lat_to_tile_y(bounds.north, zoom),
        max_y=lat_to_tile_y(bounds.south, zoom),
    )


def calculate_tile_ranges(
    bounds: GeoBounds, min_zoom: int, max_zoom: int
) -> list[TileRange]:
    """逐级计算 ``min_zoom ~ max_zoom``（含首尾）的瓦片区间。

    Raises:
        ValueError: zoom 非法，或 ``min_zoom > max_zoom``。
    """
    _validate_zoom(min_zoom)
    _validate_zoom(max_zoom)
    if min_zoom > max_zoom:
        raise ValueError(
            f"min_zoom 不能大于 max_zoom，实际 min_zoom={min_zoom}, max_zoom={max_zoom}"
        )
    return [calculate_tile_range(bounds, z) for z in range(min_zoom, max_zoom + 1)]


def estimate_tile_count(bounds: GeoBounds, min_zoom: int, max_zoom: int) -> int:
    """统计整个缩放区间内的瓦片总数。

    Raises:
        ValueError: zoom 非法，或 ``min_zoom > max_zoom``。
    """
    return sum(r.tile_count for r in calculate_tile_ranges(bounds, min_zoom, max_zoom))


# --------------------------------------------------------------------------- #
# XYZ <-> TMS
# --------------------------------------------------------------------------- #
def xyz_y_to_tms_y(zoom: int, y: int) -> int:
    """XYZ 行号 → TMS 行号（Y 轴翻转）。

    >>> xyz_y_to_tms_y(3, 2)
    5
    """
    tiles = _tiles_per_axis(zoom)
    row = operator.index(y)
    if not 0 <= row < tiles:
        raise ValueError(f"y 必须在 [0, {tiles - 1}] 之间，实际为 {row}")
    return tiles - 1 - row


def tms_y_to_xyz_y(zoom: int, y: int) -> int:
    """TMS 行号 → XYZ 行号（与 :func:`xyz_y_to_tms_y` 互为逆运算）。"""
    tiles = _tiles_per_axis(zoom)
    row = operator.index(y)
    if not 0 <= row < tiles:
        raise ValueError(f"y 必须在 [0, {tiles - 1}] 之间，实际为 {row}")
    return tiles - 1 - row


# --------------------------------------------------------------------------- #
# 容量预估
# --------------------------------------------------------------------------- #
def estimate_size_bytes(
    tile_count: int, average_tile_size: int = DEFAULT_AVERAGE_TILE_SIZE
) -> int:
    """按平均瓦片体积粗估总下载量。

    仅为量级估算，用于下载前给用户一个心理预期，不要求精准。

    Args:
        tile_count: 瓦片数量，非负。
        average_tile_size: 单张瓦片平均字节数，默认 30KB。

    Raises:
        ValueError: 参数为负或平均体积非正。
    """
    count = operator.index(tile_count)
    average = operator.index(average_tile_size)
    if count < 0:
        raise ValueError(f"tile_count 不能为负，实际为 {count}")
    if average <= 0:
        raise ValueError(f"average_tile_size 必须为正，实际为 {average}")
    return count * average


#: 容量展示单位，按 1024 进制递进
_SIZE_UNITS: tuple[str, ...] = ("B", "KB", "MB", "GB", "TB", "PB")


def format_size(size_bytes: int) -> str:
    """把字节数格式化为可读字符串（1024 进制，保留两位小数）。

    >>> format_size(1024)
    '1.00 KB'
    >>> format_size(1048576)
    '1.00 MB'
    """
    size = operator.index(size_bytes)
    if size < 0:
        raise ValueError(f"size_bytes 不能为负，实际为 {size}")

    value = float(size)
    for unit in _SIZE_UNITS:
        if value < 1024.0 or unit == _SIZE_UNITS[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024.0
    raise AssertionError("unreachable")  # pragma: no cover
