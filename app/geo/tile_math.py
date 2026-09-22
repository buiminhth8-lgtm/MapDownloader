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
    "tile_x_to_lon",
    "tile_y_to_lat",
    "tile_bounds",
    "calculate_tile_range",
    "calculate_tile_ranges",
    "estimate_tile_count",
    "validate_zoom",
    "validate_tile_axis",
    "xyz_y_to_tms_y",
    "tms_y_to_xyz_y",
    "estimate_size_bytes",
    "format_size",
    "format_tile_count",
    "TILE_COUNT_COMPACT_THRESHOLD",
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
def validate_zoom(zoom: int) -> int:
    """校验缩放级别在 ``[0, 22]`` 且为真整数，返回规范化后的 int。

    公开它是为了让"合法缩放级别"这条规则只有一处定义：Slice 4 写 MBTiles 时
    需要先把 ``z`` 规范化成 ``int`` 再入列（``zoom_level``），若各层自己写一遍
    边界判断，早晚会出现"能下载但写不进库"的错位。

    Raises:
        ValueError: 非整数，或超出 ``[TILE_MATH_MIN_ZOOM, TILE_MATH_MAX_ZOOM]``。
    """
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
    return 1 << validate_zoom(zoom)


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


#: 正变换的"整数吸附"窗口，单位是**格**（不是度）。
#:
#: 见 :func:`_snap_to_integer` 的说明。取 1e-6 格，依据是实测：
#:
#: - 最坏的正反变换复合误差出现在 **z22 的极区行**（``y=11``，纬度 85.05°）——
#:   那里 ``d(行号)/d(纬度)`` 高达约 1.3e5 行/度，纬度上 1.9e-14 度的表示误差
#:   就会被放大成 **2.8e-9 行**（实测 2.79e-9）。1e-9 挡不住它，会漏掉整整一行。
#:   1e-6 相对这个上界还有 350 倍余量。
#: - 同时它远小于任何有意义的坐标分辨率：z22 下 1e-6 行 ≈ 7.5e-9 度，
#:   比"数据保留 6 位小数"（≈ 1e-6 度）小两个数量级以上。
#:
#: 一句话：这个窗口只用来吸收浮点噪声，不可能吃掉真实坐标差。
_TILE_INDEX_EPSILON = 1e-6


def _snap_to_integer(value: float) -> float:
    """把因浮点误差而"差一点点"的整数值吸附回去。

    正变换（经纬度 → 瓦片号）与反变换（:func:`tile_y_to_lat` /
    :func:`tile_x_to_lon`）是互逆的一对公式，但分两次计算会累积约 1e-16 的
    相对误差。当输入**正好**落在瓦片边界上时（行政区边界数据里很常见——数据源
    常把坐标四舍五入到 6 位小数），正向算出的值会是 ``3.9999999999999996``
    这种形态，``int()`` 截断后整张瓦片偏移一格。

    这不是吹毛求疵：外包围盒多算/少算一整行，"预期瓦片数"就跟着偏，而它是
    覆盖度、进度分母、大任务阈值共同依赖的基准 —— 一处偏了处处对不上，
    且看起来像"产品算错了"。
    """
    rounded = round(value)
    if abs(value - rounded) < _TILE_INDEX_EPSILON:
        return float(rounded)
    return value


def validate_tile_axis(name: str, value: int, zoom: int) -> int:
    """校验瓦片的某个轴编号落在 ``[0, 2**zoom - 1]``，返回规范化后的 int。

    公开出来是因为"合法瓦片编号"这条规则需要被多处共用：

    - :func:`xyz_y_to_tms_y` / :func:`tms_y_to_xyz_y` 只校验 ``y``；
    - Slice 4 写 MBTiles 前需要**单独**校验 ``x``（``tile_column``），
      而拒绝非法编号这件事必须与 Tile Math 用同一套边界，不能各写一份。

    Args:
        name: 轴名称（``"x"`` / ``"y"``），只用于错误信息。
        value: 待校验的编号，必须支持 ``__index__``（即真整数）。
        zoom: 缩放级别，``0 ~ 22``。

    Returns:
        规范化后的 ``int``。

    Raises:
        ValueError: zoom 非法，或编号超出该缩放级别的合法区间。

    >>> validate_tile_axis("x", 4, 3)
    4
    >>> validate_tile_axis("x", 8, 3)
    Traceback (most recent call last):
    ValueError: x 必须在 [0, 7] 之间（zoom=3），实际为 8
    """
    limit = _tiles_per_axis(zoom)
    try:
        number = operator.index(value)
    except TypeError as exc:
        raise ValueError(f"{name} 必须是整数，实际为 {value!r}") from exc
    if not 0 <= number < limit:
        raise ValueError(
            f"{name} 必须在 [0, {limit - 1}] 之间（zoom={zoom}），实际为 {number}"
        )
    return number


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

    x = int(_snap_to_integer((longitude + 180.0) / 360.0 * tiles))
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
    raw = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * tiles
    y = int(_snap_to_integer(raw))
    return _clamp(y, 0, tiles - 1)


# --------------------------------------------------------------------------- #
# XYZ 瓦片 -> 经纬度（反向换算）
# --------------------------------------------------------------------------- #
#: 瓦片轴编号允许的上界类型：``0 <= v <= 2**zoom``。
#:
#: 与 :func:`validate_tile_axis` 的 ``[0, 2**zoom)`` **故意不同**：那里校验的是
#: "存不存在这张瓦片"，而这里算的是**边界**。``x == 2**zoom`` 是最后一张瓦片的
#: 东边界（经度 180），``y == 2**zoom`` 是最下一张瓦片的南边界 —— 它们不是合法
#: 瓦片编号，但是合法的边界值，缺了它们 :func:`tile_bounds` 就取不到 east / south。
def _check_tile_edge(name: str, value: int, zoom: int) -> int:
    """校验"边界用"的瓦片轴编号落在 ``[0, 2**zoom]``（含右上界）。"""
    limit = _tiles_per_axis(zoom)
    try:
        number = operator.index(value)
    except TypeError as exc:
        raise ValueError(f"{name} 必须是整数，实际为 {value!r}") from exc
    if not 0 <= number <= limit:
        raise ValueError(
            f"{name} 必须在 [0, {limit}] 之间（zoom={zoom}），实际为 {number}"
        )
    return number


def tile_x_to_lon(x: int, zoom: int) -> float:
    """XYZ 列号 → 该列**西边界**的经度。

    这是 :func:`lon_to_tile_x` 的逆运算（在"取整数格"的意义上）：对任何合法
    列号 ``x``，``lon_to_tile_x(tile_x_to_lon(x, z), z) == x``。

    本函数**只在这里实现一次**墨卡托经度反变换，:func:`tile_bounds` 复用它 ——
    任何地方需要"某张瓦片盖到哪条经线"，都必须调这里，不许再写一遍
    ``x / 2**z * 360 - 180``（两份公式迟早会有一份忘记处理浮点边界）。

    Args:
        x: 列号，``0 <= x <= 2**zoom``。``2**zoom`` 表示最东侧的边界值 180°。
        zoom: 缩放级别，``0 ~ 22``。

    Returns:
        经度（度），范围 ``[-180, 180]``。

    Raises:
        ValueError: zoom 非法，或 ``x`` 超出 ``[0, 2**zoom]``。

    >>> tile_x_to_lon(0, 0)
    -180.0
    >>> tile_x_to_lon(1, 0)
    180.0
    >>> tile_x_to_lon(0, 1)
    -180.0
    >>> tile_x_to_lon(1, 1)
    0.0
    """
    tiles = _tiles_per_axis(zoom)
    column = _check_tile_edge("x", x, zoom)
    return column / tiles * 360.0 - 180.0


def tile_y_to_lat(y: int, zoom: int) -> float:
    """XYZ 行号 → 该行**北边界**的纬度。

    与 :func:`lat_to_tile_y` 互逆：``lat_to_tile_y(tile_y_to_lat(y, z), z) == y``。
    墨卡托纬度反变换为 ``atan(sinh(pi * (1 - 2y/n)))``，与正向的
    ``asinh(tan(phi))`` 是一对（两者都由同一个投影关系导出，见模块 docstring）。

    Args:
        y: 行号（XYZ，自北向南），``0 <= y <= 2**zoom``。
            ``2**zoom`` 表示最南侧的边界值 -85.05112878°。
        zoom: 缩放级别，``0 ~ 22``。

    Returns:
        纬度（度），``y=0`` 时为 +85.05112878°，``y=2**zoom`` 时为 -85.05112878°。

    Raises:
        ValueError: zoom 非法，或 ``y`` 超出 ``[0, 2**zoom]``。

    >>> round(tile_y_to_lat(0, 0), 6)
    85.051129
    >>> round(tile_y_to_lat(1, 0), 6)
    -85.051129
    >>> round(tile_y_to_lat(1, 1), 6)
    0.0
    """
    tiles = _tiles_per_axis(zoom)
    row = _check_tile_edge("y", y, zoom)
    lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * row / tiles)))
    latitude = math.degrees(lat_rad)
    # 浮点误差可能让 y=0 / y=2**z 得到 85.05112877980659 这类值，
    # 钳一下以严格满足 GeoBounds 的 ±WEB_MERCATOR_MAX_LAT 约束。
    return max(-WEB_MERCATOR_MAX_LAT, min(WEB_MERCATOR_MAX_LAT, latitude))


def tile_bounds(zoom: int, x: int, y: int) -> GeoBounds:
    """一张 XYZ 瓦片覆盖的经纬度矩形。

    返回值的取整方向与 :func:`calculate_tile_range` **严格一致**：同一个
    ``bounds`` 算出的 ``TileRange`` 里，每一张瓦片的 :func:`tile_bounds`
    都落在原 ``bounds`` 之内（这正是 Polygon 过滤"不漏块"的前提）。

    Args:
        zoom: 缩放级别，``0 ~ 22``。
        x: 列号，``0 <= x < 2**zoom``。
        y: 行号（XYZ，自北向南），``0 <= y < 2**zoom``。

    Returns:
        ``GeoBounds(west, south, east, north)``：瓦片左/下/右/上四条边的坐标。

    Raises:
        ValueError: zoom 非法，或 ``x`` / ``y`` 不是合法瓦片编号。

    >>> bounds = tile_bounds(1, 1, 0)
    >>> (round(bounds.west, 3), round(bounds.south, 3))
    (0.0, 0.0)
    >>> (round(bounds.east, 3), round(bounds.north, 3))
    (180.0, 85.051)
    """
    validate_zoom(zoom)
    column = validate_tile_axis("x", x, zoom)
    row = validate_tile_axis("y", y, zoom)
    return GeoBounds(
        west=tile_x_to_lon(column, zoom),
        south=tile_y_to_lat(row + 1, zoom),
        east=tile_x_to_lon(column + 1, zoom),
        north=tile_y_to_lat(row, zoom),
    )


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
    validate_zoom(zoom)
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
    validate_zoom(min_zoom)
    validate_zoom(max_zoom)
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


#: 张数超过它才开始用 ``K`` / ``M`` / ``B`` 这类紧凑写法。
#:
#: 阈值取 10 万而不是 1 万：五位数以内带千分位的原值（``28,452``）比
#: ``28.45 K`` 更可读，也让"上次说 28,452、这次说 28.4 K"这种看起来像变了的
#: 展示消失。跨过阈值之后原值已经读不出量级（``2,735,435,470``），紧凑写法才
#: 真正有用。
TILE_COUNT_COMPACT_THRESHOLD: int = 100_000

#: 紧凑张数的单位表（1000 进制：它是"多少个"而不是字节，别混用 1024）
_TILE_UNITS: tuple[str, ...] = ("", "K", "M", "B", "T")


def format_tile_count(tile_count: int) -> str:
    """把瓦片张数格式化为可读字符串（Spec §20）。

    三条规则，与 :func:`format_size` 刻意不同：

    1. **10 万以下保留原值 + 千分位**（``28,452``）—— 这个量级下用户要的是
       精确数字（"还差 3 张"这种事只在几万张的规模上有意义）；
    2. **10 万以上换 1000 进制紧凑写法**（``1.25 M`` / ``2.73 B``）——
       十位数原值在界面上一眼看不出量级，而它恰恰是风险判断要的信息；
    3. **单位不带空格**（``2.73 B`` 里的 B 是 billion，不是字节）。
       与 ``format_size`` 的 ``76.43 TB`` 放在一起时不会混淆（一个是容量、
       一个是张数，各自带在自己的行里）。

    >>> format_tile_count(28452)
    '28,452'
    >>> format_tile_count(1_250_000)
    '1.25 M'
    >>> format_tile_count(2_735_435_470)
    '2.74 B'
    """
    count = operator.index(tile_count)
    if count < 0:
        raise ValueError(f"tile_count 不能为负，实际为 {count}")
    if count < TILE_COUNT_COMPACT_THRESHOLD:
        return f"{count:,}"

    value = float(count)
    for index, unit in enumerate(_TILE_UNITS):
        scaled = value / (1000.0 ** index)
        # 按**四舍五入之后**的值决定要不要升单位：999,999 张如果不这样处理
        # 会变成 "1000 K"，而它显然是 1 M。
        if round(scaled, 2) >= 1000.0 and index < len(_TILE_UNITS) - 1:
            continue
        # 去掉无意义的尾零：``100.00 K`` 读起来像伪精度，``100 K`` 才诚实。
        text = f"{scaled:.2f}".rstrip("0").rstrip(".")
        return f"{text} {unit}" if unit else text
    raise AssertionError("unreachable")  # pragma: no cover
