"""米制 Buffer 构建器（Slice 22，Spec §7 / §10）—— 本 Slice 的技术核心。

为什么不能直接在 WGS84 上 buffer
---------------------------------

``line.buffer(0.05)`` 里那个 ``0.05`` 的单位是**度**。同样 0.05 度，
在广州（北纬 23°）东西向约 5.1 km，在漠河（北纬 53°）只有约 3.3 km ——
一个随纬度变化的"半径"不是半径。

而且缓冲出来的形状**还会在投回 WGS84 后继续变形**：在度数平面上等距的点，
落到球面上就不再等距。结果是一条"看起来对、量起来不对"的走廊。

正确做法（本模块实现的就是这一段）
----------------------------------

::

    航点（WGS84 度）
      → 取航线外包矩形的中心作为投影原点（离所有点最远距离最小）
      → 局部方位等距投影 AEQD（proj 串 + pyproj.Transformer，always_xy=True）
      → 投影到**米**制平面，连成 LineString
      → buffer(buffer_meters, join 圆角, cap 圆角)   ← 这里的 5000 真的是 5000 m
      → 投回 WGS84，得到 Polygon / MultiPolygon
      → 校验几何合法性 → 算外包范围（复用 app.geo 的那一份实现）

为什么用 AEQD
-------------

方位等距投影以投影原点为中心，**半径方向上的距离是严格等距的**（这正是
"Buffer 半径"唯一在意的量）。在一条几公里到几百公里的作业走廊上，
它的形变可以忽略；换成 Web 墨卡托或 UTM 都会引入一个方向的尺度误差，
而"哪个方向失真"恰好是我们最不想赌的事。

跨度上限
--------

只有在"航线跨度有限"时上面那句话才成立，因此本模块对跨度设了硬上限
（:data:`~app.config.settings.MISSION_MAX_ROUTE_SPAN_DEGREES`，默认 10°）：
超过就直接报错。给一个悄悄失真的走廊，比报错糟糕得多 —— 用户会拿着它去飞。

几何版本号
----------

``1:5000.0:9c1f4a3b2d0e`` —— 结构版本 : Buffer 半径 : 航线摘要。同一份
"航点 + 半径"永远得到同一个版本号（航点先归一化到 7 位小数，约 1 cm），
因此它可以直接当"边界版本"写进任务记录与 MBTiles metadata（Spec §21）。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from shapely import is_valid, make_valid
from shapely.geometry import LineString, mapping
from shapely.ops import transform as shapely_transform, unary_union
from pyproj import Transformer

from app.config.settings import (
    DEFAULT_MISSION_BUFFER_METERS,
    MISSION_BUFFER_METERS_MAX,
    MISSION_BUFFER_METERS_MIN,
    MISSION_GEOMETRY_VERSION,
    MISSION_MAX_ROUTE_SPAN_DEGREES,
)
from app.geo.bounds import GeoBounds
from app.geo.geojson_loader import geometry_to_bounds
from app.mission.models import (
    MISSION_REGION_LEVEL,
    MissionRegion,
    MissionRegionError,
    MissionRouteError,
    MissionWaypoint,
    require_mission_id,
)
from app.mission.route_loader import dedupe_waypoints

__all__ = [
    "MissionGeometry",
    "MissionRegionBuilder",
    "route_geojson_document",
    "region_geojson_document",
]

#: 圆形端帽 / 圆角的弧段数。8 段在几公里尺度上肉眼已经看不出棱角；
#: 16 段让"贴着航线的边界"在放大后仍然是平滑的曲线，代价是多几十个顶点。
_QUAD_SEGS = 16

#: 坐标归一化到几位小数参与摘要计算。7 位 ≈ 1 cm ——
#: 比任何真实航线的定位精度都细，同时把"浮点最后一位抖动"挡在版本号之外。
_VERSION_PRECISION = 7


def _validate_buffer(buffer_meters: object) -> float:
    """校验 Buffer 半径（米）。

    Raises:
        MissionRegionError: 不是有限正数，或超出允许区间。
    """
    try:
        value = float(buffer_meters)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise MissionRegionError(f"Buffer 半径不是数字：{buffer_meters!r}") from exc
    if not math.isfinite(value):
        raise MissionRegionError(f"Buffer 半径不是有限数：{buffer_meters!r}")
    if value <= 0:
        raise MissionRegionError(
            f"Buffer 半径必须大于 0 米（0 会退化成一条线）：{value}"
        )
    if not (MISSION_BUFFER_METERS_MIN <= value <= MISSION_BUFFER_METERS_MAX):
        raise MissionRegionError(
            f"Buffer 半径必须在 {MISSION_BUFFER_METERS_MIN:.1f} ~ "
            f"{MISSION_BUFFER_METERS_MAX:.1f} 米之间：{value}"
        )
    return value


def _coerce_waypoints(waypoints: Iterable[Any]) -> tuple[MissionWaypoint, ...]:
    """把各种写法（``MissionWaypoint`` / ``(lon, lat)``）统一成航点元组。

    Raises:
        MissionRouteError: 元素既不是航点，也不是长度为 2 的序列。
    """
    points: list[MissionWaypoint] = []
    for index, item in enumerate(waypoints or ()):
        if isinstance(item, MissionWaypoint):
            points.append(item)
            continue
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                points.append(MissionWaypoint(lon=item[0], lat=item[1]))
            except MissionRouteError as exc:
                raise MissionRouteError(
                    f"第 {index + 1} 个航点不合法：{exc}"
                ) from exc
            continue
        raise MissionRouteError(
            f"第 {index + 1} 个航点既不是 MissionWaypoint 也不是 [经度, 纬度]：{item!r}"
        )
    return tuple(points)


def _aeqd_crs(lon_0: float, lat_0: float) -> str:
    """以 ``(lon_0, lat_0)`` 为原点的局部方位等距投影串。

    显式写 ``+units=m``：整个"按米 Buffer"的正确性就落在这一行上。
    """
    return (
        f"+proj=aeqd +lat_0={lat_0:.7f} +lon_0={lon_0:.7f} "
        "+datum=WGS84 +units=m +no_defs"
    )


def _polygonal_only(geometry: Any) -> Any:
    """从任意几何里取出**面**的部分（``make_valid`` 可能产出混合集合）。"""
    geom_type = geometry.geom_type
    if geom_type in {"Polygon", "MultiPolygon"}:
        return geometry
    if geom_type == "GeometryCollection":
        parts = [part for part in geometry.geoms if part.geom_type in {"Polygon", "MultiPolygon"}]
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else unary_union(parts)
    return None


@dataclass(frozen=True, eq=False)
class MissionGeometry:
    """一次米制 Buffer 的完整产物（Spec §10）。

    刻意**不**放进 :class:`~app.mission.models.MissionRegion`：值对象带着一个
    几万顶点的 Shapely 几何到处走，迟早会在某个 ``==``、日志或 JSON 序列化里炸掉。
    本对象只在"生成几何 → 落盘"这一小段里传递，落盘之后就只留范围与版本号。

    ``eq=False``：两个 Shapely 几何的默认 ``==`` 是"结构相同"而不是"形状相同"，
    拿它当值语义会骗人。要比较形状请显式用 ``geometry.equals(...)``。

    Attributes:
        geometry: 投回 WGS84 的面几何（Polygon / MultiPolygon）。
        bounds: 几何的外包范围（**从几何算出来**，不是手填的）。
        geometry_version: 几何版本号（同一份航点 + 半径恒定）。
        buffer_meters: 实际使用的 Buffer 半径（米）。
    """

    geometry: Any
    bounds: GeoBounds
    geometry_version: str
    buffer_meters: float


class MissionRegionBuilder:
    """航点 + 半径 → 任务区几何 / 任务区（Spec §10）。

    Attributes:
        default_buffer_meters: 不传半径时的缺省值。
        max_span_degrees: 允许的最大航线跨度（度）。
    """

    def __init__(
        self,
        *,
        default_buffer_meters: float = DEFAULT_MISSION_BUFFER_METERS,
        max_span_degrees: float = MISSION_MAX_ROUTE_SPAN_DEGREES,
        version_prefix: str = MISSION_GEOMETRY_VERSION,
    ) -> None:
        self._default_buffer_meters = float(default_buffer_meters)
        self._max_span_degrees = float(max_span_degrees)
        self._version_prefix = str(version_prefix)

    # ------------------------------------------------------------------ #
    # 只读属性
    # ------------------------------------------------------------------ #
    @property
    def default_buffer_meters(self) -> float:
        """缺省 Buffer 半径（米）。"""
        return self._default_buffer_meters

    @property
    def max_span_degrees(self) -> float:
        """允许的最大航线跨度（度）。"""
        return self._max_span_degrees

    # ------------------------------------------------------------------ #
    # 航点归一化
    # ------------------------------------------------------------------ #
    def normalize_waypoints(self, waypoints: Sequence[Any]) -> tuple[MissionWaypoint, ...]:
        """把各种写法统一成"干净"的航点元组（校验 + 去相邻重复）。

        Raises:
            MissionRouteError: 元素不合法 / 去重后不足两个点。
        """
        return dedupe_waypoints(_coerce_waypoints(waypoints))

    # ------------------------------------------------------------------ #
    # 几何
    # ------------------------------------------------------------------ #
    def build_geometry(
        self,
        waypoints: Sequence[Any],
        buffer_meters: float | None = None,
    ) -> MissionGeometry:
        """航点 + 半径（米） → 投回 WGS84 的多边形。

        Raises:
            MissionRouteError: 航点不足 / 不合法 / 跨度超限。
            MissionRegionError: 半径不合法，或结果不是一个合法的面几何。
        """
        radius = _validate_buffer(
            self._default_buffer_meters if buffer_meters is None else buffer_meters
        )
        points = self.normalize_waypoints(waypoints)
        center_lon, center_lat = self._projection_center(points)
        self._check_span(points)

        crs = _aeqd_crs(center_lon, center_lat)
        forward = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        backward = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)

        xs, ys = forward.transform(
            [point.lon for point in points], [point.lat for point in points]
        )
        line = LineString(list(zip(xs, ys)))

        # 到这一步才做 buffer：平面单位是**米**，5000 就是 5000 米。
        buffered = line.buffer(
            radius, quad_segs=_QUAD_SEGS, cap_style="round", join_style="round"
        )
        wgs84 = shapely_transform(lambda x, y, z=None: backward.transform(x, y), buffered)
        geometry = self._validate_geometry(wgs84, radius)
        bounds = self._bounds_of(geometry)
        return MissionGeometry(
            geometry=geometry,
            bounds=bounds,
            geometry_version=self.geometry_version(points, radius),
            buffer_meters=radius,
        )

    def build(
        self,
        mission_id: str,
        name: str,
        waypoints: Sequence[Any],
        buffer_meters: float | None = None,
        *,
        geometry: MissionGeometry | None = None,
        geojson_path: str = "",
        created_at: str = "",
    ) -> MissionRegion:
        """构造一个 :class:`~app.mission.models.MissionRegion`。

        Args:
            mission_id: 任务区 id（``mission-001``）。
            name: 展示名。
            waypoints: 航点（``MissionWaypoint`` 或 ``(lon, lat)``）。
            buffer_meters: 走廊半径（米）；``None`` 用缺省值。
            geometry: **已经算好的几何**。传进来就完全跳过投影与 Buffer ——
                这是"几何只生成一次"的落点（Spec §23）：服务层算完几何、
                落盘时再构造区域模型，不必重算一遍。
            geojson_path: ``region.geojson`` 的绝对路径；落盘前可以为空。
            created_at: 创建时间（ISO 8601），缺省由服务填。

        Raises:
            MissionRouteError: 航点不合法。
            MissionRegionError: id / 名称 / 半径不合法。
        """
        mission_id = require_mission_id(mission_id)
        points = self.normalize_waypoints(waypoints)
        prepared = (
            geometry
            if geometry is not None
            else self.build_geometry(points, buffer_meters)
        )
        return MissionRegion(
            id=mission_id,
            name=name,
            waypoints=points,
            buffer_meters=prepared.buffer_meters,
            bounds=prepared.bounds,
            geojson_path=geojson_path,
            geometry_version=prepared.geometry_version,
            created_at=created_at,
        )

    # ------------------------------------------------------------------ #
    # 版本号
    # ------------------------------------------------------------------ #
    def geometry_version(
        self, waypoints: Sequence[MissionWaypoint], buffer_meters: float
    ) -> str:
        """由"航点 + 半径"推出几何版本号（同名不同几何 = 不同版本）。

        形态：``<结构版本>:<半径>:<航线摘要>``。半径与结构版本都进字符串，
        是为了让版本号本身就能解释"为什么变了"；只有摘要进 hash，
        是因为它对用户毫无意义、只用来做等值判断。
        """
        canonical = "|".join(
            f"{point.lon:.{_VERSION_PRECISION}f},{point.lat:.{_VERSION_PRECISION}f}"
            for point in waypoints
        )
        digest = hashlib.sha256(
            f"{canonical}|{float(buffer_meters):.3f}".encode("utf-8")
        ).hexdigest()[:12]
        return f"{self._version_prefix}:{float(buffer_meters):.1f}:{digest}"

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _projection_center(
        self, points: Sequence[MissionWaypoint]
    ) -> tuple[float, float]:
        """投影原点：航线外包矩形的中心。

        选它而不是"第一个点"或"质心"，是因为要让**离原点最远**的那个点的距离
        尽量小 —— AEQD 的形变随离原点的距离增长，外界矩形中心把最大距离压到最小。
        """
        lons = [point.lon for point in points]
        lats = [point.lat for point in points]
        return ((min(lons) + max(lons)) / 2.0, (min(lats) + max(lats)) / 2.0)

    def _check_span(self, points: Sequence[MissionWaypoint]) -> None:
        """跨度超限就报错（Spec §10）。

        Raises:
            MissionRouteError: 东西或南北跨度超过 ``max_span_degrees``。
        """
        lons = [point.lon for point in points]
        lats = [point.lat for point in points]
        lon_span = max(lons) - min(lons)
        lat_span = max(lats) - min(lats)
        if max(lon_span, lat_span) <= self._max_span_degrees:
            return
        raise MissionRouteError(
            f"航线跨度过大（经度 {lon_span:.3f}° / 纬度 {lat_span:.3f}°），"
            f"超过 {self._max_span_degrees:.1f}° 上限："
            "单点方位等距投影（AEQD）只适用于局部走廊，跨省 / 跨国航线请在"
            "航线上分段建立多个任务区"
        )

    def _validate_geometry(self, geometry: Any, radius: float) -> Any:
        """确认结果是一个**合法、非空的面**。

        不合法时先试一次 ``make_valid``（自相交等小毛病它能修），修不好就报错：
        拿一个坏几何去下几万张瓦片，比在下单前报错糟糕得多。

        Raises:
            MissionRegionError: 空几何 / 修复后仍不是面 / 结果不合法。
        """
        candidate = geometry
        if candidate is None or candidate.is_empty:
            raise MissionRegionError(
                "Buffer 结果是空几何：航线点可能退化成了一条线"
                f"（半径 {radius:.1f} 米）"
            )
        if not is_valid(candidate):
            repaired = _polygonal_only(make_valid(candidate))
            if repaired is None or repaired.is_empty or not is_valid(repaired):
                raise MissionRegionError(
                    "Buffer 结果不是合法多边形，且自动修复没成功："
                    "请检查航线是否有自相交或重复点"
                )
            candidate = repaired
        polygonal = _polygonal_only(candidate)
        if polygonal is None:
            raise MissionRegionError(
                f"Buffer 结果不是面几何（{candidate.geom_type}）：无法用于瓦片过滤"
            )
        return polygonal

    def _bounds_of(self, geometry: Any) -> GeoBounds:
        """从几何算外包范围。

        Raises:
            MissionRegionError: 范围不成立（跨国际日期变更线等）。
        """
        try:
            return geometry_to_bounds(geometry)
        except Exception as exc:  # noqa: BLE001 - 统一转成任务区错误，保留原文
            raise MissionRegionError(
                f"任务区几何的范围无法用于瓦片计算：{exc}"
            ) from exc


# --------------------------------------------------------------------------- #
# GeoJSON 文档（落盘用）
# --------------------------------------------------------------------------- #
def route_geojson_document(
    waypoints: Sequence[MissionWaypoint],
    *,
    mission_id: str,
    name: str,
    buffer_meters: float,
) -> dict[str, Any]:
    """``route.geojson`` 的结构（Spec §11）。

    存**原始航线**（不是 Buffer 后的多边形）有两个用处：用户能看出"任务区是
    按哪条航线生成的"，以及重建任务区时不必回到原始文件。注意它**不是**
    下载依据 —— 下载用的是 ``region.geojson``（Spec §13：几何只按一份用）。
    """
    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": [[point.lon, point.lat] for point in waypoints],
        },
        "properties": {
            "id": mission_id,
            "name": name,
            "regionType": "mission",
            "bufferMeters": float(buffer_meters),
        },
    }


def region_geojson_document(
    geometry: Any,
    *,
    mission_id: str,
    name: str,
    buffer_meters: float,
    geometry_version: str,
) -> dict[str, Any]:
    """``region.geojson`` 的结构（Spec §11 / §13）。

    ``properties`` 里带上 id / 版本 / 半径：这份文件会被 Inspector、包校验和
    "用户手工打开看看"三个场景读到，把"它是谁"写在文件里，比要求读的人
    先去别处查目录要靠得住。
    """
    return {
        "type": "Feature",
        "geometry": mapping(geometry),
        "properties": {
            "id": mission_id,
            "name": name,
            "regionType": "polygon",
            # 等级从任务区层那唯一一处常量取（= ``mission``）。
            # 写死在 JSON 字面量里，"任务区属于哪一级"就多了一处可能改漏的地方。
            "regionLevel": MISSION_REGION_LEVEL.value,
            "boundaryVersion": geometry_version,
            "bufferMeters": float(buffer_meters),
        },
    }
