"""任务区模型层（Slice 22，Spec §4）。

三个概念
--------

==========================================  ====================================
:class:`MissionWaypoint`                    一个航点（WGS84 经纬度）
:class:`MissionRegion`                      一个**已生成**的任务区（航线 + Buffer → 多边形）
:class:`MissionRegionMetadata`              落盘的元数据（版本 / 半径 / 几何版本 / 范围）
==========================================  ====================================

只有 :class:`MissionRegion` 会被业务层到处传；几何（Shapely 对象）刻意**不放在
本模型里** —— 它由 :class:`~app.mission.region_builder.MissionRegionBuilder` 算出、
由 :class:`~app.mission.mission_region_store.MissionRegionStore` 落成 GeoJSON，
本模型只保留"几何在哪、范围多大、是哪个版本"这三件可序列化的事。
理由与 Slice 18 的目录条目一样：一个值对象带着几 MB 的坐标到处走，
迟早会在某个 `==` 或日志里炸掉。

为什么纬度上限是 Web 墨卡托极限
--------------------------------

纬度落在 ±85.05112878 之外的航点，本工具**算不出瓦片**（Tile Math 在极点附近
没有定义）。与其在下单阶段给出一个荒唐的张数，不如在"导入航线"这一步就说清楚：
这是好输入与坏输入的分界，不是"我们偷懒少支持一点"。

单位：km 与 m 各在哪一侧
------------------------

Buffer 半径对**用户**是 km（界面输入框写 "5"），对**几何库**是米（`buffer(5000)`）。
换算只允许出现在 :func:`km_to_meters` / :func:`meters_to_km` 里 ——
业务层与持久化一律按米。多一处换算，就多一处"5 米 vs 5 千米"的事故。

任务区 id 的形态
----------------

``mission-001`` 这样的 id 会被当作**目录名**使用（``data/mission_regions/<id>/``），
因此它同时受两条规则约束：

- 前缀必须是 :data:`~app.config.settings.MISSION_ID_PREFIX`（跨层契约：
  下载链路与 Inspector 靠这个前缀认出"这是任务区，不是行政区目录里的 id"）；
- 必须是一个安全的**单段目录名** —— 复用
  :func:`~app.geo.region.is_safe_region_relative_path`（全项目唯一一份路径安全规则），
  再额外要求"不含目录分隔符"。``..`` 与绝对路径在这里同样被拒。
"""

from __future__ import annotations

import math
import numbers
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Mapping

from app.config.settings import (
    MAX_LONGITUDE,
    MISSION_GEOMETRY_VERSION,
    MISSION_ID_PREFIX,
    MISSION_METADATA_VERSION,
    MISSION_MIN_WAYPOINTS,
    MIN_LONGITUDE,
    WEB_MERCATOR_MAX_LAT,
)
from app.geo.bounds import GeoBounds
from app.geo.region import is_safe_region_relative_path
# 唯一一处「任务区层 → 规划层」的依赖，与 app.geo.region 对 RegionLevel 的依赖
# 同形（那边也是只 import 这个零依赖模块）。等级枚举本身不含任何规则，
# 因此这条边不构成循环。
from app.planning.region_level import RegionLevel

__all__ = [
    "MissionWaypoint",
    "MissionRegion",
    "MissionRegionMetadata",
    "MissionError",
    "MissionRouteError",
    "MissionRegionError",
    "MissionRegionNotFound",
    "MissionRegionReferenced",
    "km_to_meters",
    "meters_to_km",
    "is_mission_id",
    "now_iso",
    "MISSION_REGION_LEVEL",
    "MISSION_METADATA_FIELD_ORDER",
]

#: 任务区在规划等级里的位置（Slice 22，Spec §5）。
#:
#: 放在模型层、且只有**这一个地方**写这个等级：任务区层里凡是需要"我是哪一级"
#: 的地方（下载区域、Zoom 建议、计划条目）都必须引用它。
#: 直接在各处写 ``RegionLevel.MISSION`` 会让"任务区属于哪一级"变成一个
#: 散落在五个文件里的事实 —— 那正是 ``tests/test_zoom_policy.py``
#: 那条静态守卫在防的事（它有一份"允许写等级"的名单，本文件在名单上）。
MISSION_REGION_LEVEL: RegionLevel = RegionLevel.MISSION


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class MissionError(ValueError):
    """任务区相关错误的总类。

    刻意继承 :class:`ValueError`：任务区出问题几乎总是"输入不成立"
    （航线点不够、半径越界、id 不合法）。让它们落在 ``ValueError`` 上，
    规划层的 ``_ITEM_ERRORS`` 那份"一个坏条目不该让整个计划算不出来"的兜底
    才自动对它生效 —— 否则一个坏任务区会让整个计划的估算崩掉。
    """


class MissionRouteError(MissionError):
    """航线（GeoJSON）不合法 / 读不出来 / 点不够。"""


class MissionRegionError(MissionError):
    """任务区构建或持久化失败（几何无效、路径越界、元数据对不上…）。"""


class MissionRegionNotFound(MissionRegionError):
    """任务区不存在，或它的几何文件已经不在了。"""


class MissionRegionReferenced(MissionRegionError):
    """任务区仍被下载任务 / 计划 / 执行记录引用，拒绝删除（Spec §22）。"""


# --------------------------------------------------------------------------- #
# 单位换算（唯一两处）
# --------------------------------------------------------------------------- #
def km_to_meters(value: float) -> float:
    """千米 → 米。

    >>> km_to_meters(5)
    5000.0
    """
    return float(value) * 1000.0


def meters_to_km(value: float) -> float:
    """米 → 千米。

    >>> meters_to_km(5000)
    5.0
    """
    return float(value) / 1000.0


def now_iso() -> str:
    """当前本地时间的 ISO 8601 字符串（带时区），如 ``2026-09-21T10:40:10+08:00``。

    与 ``app.package.models.now_iso`` 是同一个格式、同一个理由（缺时区的时间戳
    在跨时区场景下是个谜）。这里单独写一份而不是 import 打包层：
    ``app.mission`` 不该为了一个时间戳依赖 ``app.package``。
    """
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- #
# 航点
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MissionWaypoint:
    """一个航点（WGS84 经纬度，十进制度）。

    Raises:
        MissionRouteError: 经纬度不是有限数，或超出可用的合法区间
            （经度 ±180；纬度受 Web 墨卡托限制，±85.05112878）。
    """

    lon: float
    lat: float

    def __post_init__(self) -> None:
        lon = _as_finite_float(self.lon, "航点经度")
        lat = _as_finite_float(self.lat, "航点纬度")
        if not (MIN_LONGITUDE <= lon <= MAX_LONGITUDE):
            raise MissionRouteError(
                f"航点经度超出合法区间 [{MIN_LONGITUDE}, {MAX_LONGITUDE}]：{lon}"
            )
        if not (-WEB_MERCATOR_MAX_LAT <= lat <= WEB_MERCATOR_MAX_LAT):
            raise MissionRouteError(
                "航点纬度超出可下载区间 "
                f"[-{WEB_MERCATOR_MAX_LAT}, {WEB_MERCATOR_MAX_LAT}]：{lat}"
                "（再往极点走，瓦片编号本身没有定义）"
            )
        object.__setattr__(self, "lon", lon)
        object.__setattr__(self, "lat", lat)

    def as_xy(self) -> tuple[float, float]:
        """``(lon, lat)`` —— 与 GeoJSON 的坐标顺序一致（**不是** lat/lon）。"""
        return (self.lon, self.lat)

    def text(self) -> str:
        """一行展示文案（6 位小数，约 0.1 m，够看清"点在不在路上"）。"""
        return f"{self.lon:.6f}, {self.lat:.6f}"


def _as_finite_float(value: object, what: str) -> float:
    """转成 float 并拒绝 NaN / inf（它们会一路污染到 Shapely 与瓦片计算）。

    只接受**真正的数字**：``str`` 与 ``bool`` 一律拒收，不做"能转就转"的宽容。

    - ``bool`` 看着像数（``True == 1``），但它出现在坐标里一定是数据错了 ——
      悄悄当成经度 1.0 会得到一条完全不相干、却"看起来正常"的航线；
    - ``"120"`` 这种带引号的坐标常见于表格导出，说明**上游把数字写成了字符串**。
      这一次能转成功，下一次遇到 ``"120.5N"`` 就转不动了；与其一半能读一半报错，
      不如在读入口就说清"坐标必须是数字"。
    """
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise MissionRouteError(f"{what}不是数字：{value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise MissionRouteError(f"{what}不是有限数：{value!r}")
    return number


# --------------------------------------------------------------------------- #
# 任务区 id
# --------------------------------------------------------------------------- #
#: id 在**前缀之后**允许的字符：小写字母、数字、下划线、点、连字符。
#: 不含 ``/`` 与 ``\`` —— 它要当目录名用，单段是硬要求。
_MISSION_ID_BODY = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

#: id 的长度上限。它出现在目录名、任务名、MBTiles metadata 里，
#: 一个几百字符的 id 只会给下游添麻烦。
_MISSION_ID_MAX_LENGTH = 64


def is_mission_id(value: object) -> bool:
    """这个字符串是不是一个合法的任务区 id。

    >>> is_mission_id("mission-001")
    True
    >>> is_mission_id("china")
    False
    >>> is_mission_id("mission-../etc")
    False
    """
    text = str(value or "").strip()
    if not text or len(text) > _MISSION_ID_MAX_LENGTH:
        return False
    if not text.startswith(MISSION_ID_PREFIX):
        return False
    body = text[len(MISSION_ID_PREFIX) :]
    if not _MISSION_ID_BODY.match(body):
        return False
    # 复用全项目唯一一份路径安全规则：``..`` / 绝对路径 / 盘符 / NUL 一并拦下。
    # 前缀规则已经排除了绝大部分写法，但"安全判断只允许有一份实现"这条更重要。
    return is_safe_region_relative_path(text)


def require_mission_id(value: object) -> str:
    """校验并返回任务区 id。

    Raises:
        MissionRegionError: 形态不合法。
    """
    text = str(value or "").strip()
    if not is_mission_id(text):
        raise MissionRegionError(
            "任务区 id 不合法："
            f"{value!r}（必须以 {MISSION_ID_PREFIX!r} 开头，"
            "且只含小写字母 / 数字 / 下划线 / 点 / 连字符，长度不超过 "
            f"{_MISSION_ID_MAX_LENGTH}）"
        )
    return text


# --------------------------------------------------------------------------- #
# 任务区
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MissionRegion:
    """一个已生成的任务区（Spec §4）。

    Attributes:
        id: 任务区标识（``mission-001``）。同时是落盘目录名与下载链路的 ``region_id``。
        name: 展示名（"西湖巡检航线"）。
        waypoints: 航点序列（**不可变元组**；至少两个点）。
        buffer_meters: 走廊半径（米）。几何按它在地面（米制）上扩张。
        bounds: 多边形的外包范围。它同时是下载链路的"候选矩形"——
            先用它快速缩小搜索面，再由几何逐张判定，两者缺一不可（Spec §9）。
        geojson_path: ``region.geojson`` 的**绝对路径**；尚未落盘时是空串
            （由 :meth:`MissionRegionStore.save` 回填，见那里的说明）。
        geometry_version: 几何版本号。同一份"航点 + 半径"永远得到同一个版本号，
            它会被写进任务记录的 ``boundary_version`` 与 MBTiles metadata，
            用来回答"这批瓦片是按哪一版几何裁的"（Spec §21）。
        created_at: 创建时间（ISO 8601 带时区）。缺省为空串（由服务在落盘时填）。

    Raises:
        MissionRegionError: id 不合法 / 名称为空 / 航点不足两个 /
            Buffer 半径不是正数 / 几何版本号为空。
    """

    id: str
    name: str
    waypoints: tuple[MissionWaypoint, ...]
    buffer_meters: float
    bounds: GeoBounds
    geojson_path: str = ""
    geometry_version: str = MISSION_GEOMETRY_VERSION
    created_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_mission_id(self.id))

        name = str(self.name or "").strip()
        if not name:
            raise MissionRegionError("任务区名称不能为空")
        object.__setattr__(self, "name", name)

        points = tuple(self.waypoints or ())
        if len(points) < MISSION_MIN_WAYPOINTS:
            raise MissionRegionError(
                f"任务区至少需要 {MISSION_MIN_WAYPOINTS} 个航点，实际为 {len(points)} 个"
            )
        for point in points:
            if not isinstance(point, MissionWaypoint):
                raise MissionRegionError(
                    f"航点必须是 MissionWaypoint，实际为 {type(point).__name__}"
                )
        object.__setattr__(self, "waypoints", points)

        buffer_meters = _as_finite_float(self.buffer_meters, "Buffer 半径")
        if buffer_meters <= 0:
            raise MissionRegionError(
                f"Buffer 半径必须大于 0 米（0 会退化成一条线，覆盖不到任何瓦片）："
                f"{buffer_meters}"
            )
        object.__setattr__(self, "buffer_meters", buffer_meters)

        if not isinstance(self.bounds, GeoBounds):
            raise MissionRegionError(
                f"bounds 必须是 GeoBounds，实际为 {type(self.bounds).__name__}"
            )

        version = str(self.geometry_version or "").strip()
        if not version:
            raise MissionRegionError(
                "geometry_version 不能为空：没有它就无法回答"
                "'这批瓦片是按哪一版几何裁的'"
            )
        object.__setattr__(self, "geometry_version", version)

        path = str(self.geojson_path or "").strip()
        object.__setattr__(self, "geojson_path", path)
        object.__setattr__(self, "created_at", str(self.created_at or "").strip())

    # ------------------------------------------------------------------ #
    # 派生
    # ------------------------------------------------------------------ #
    @property
    def waypoint_count(self) -> int:
        """航点数。"""
        return len(self.waypoints)

    @property
    def buffer_km(self) -> float:
        """Buffer 半径（千米）—— 只给展示用。"""
        return meters_to_km(self.buffer_meters)

    def bounds_text(self) -> str:
        """紧凑范围文本（界面"范围："那行用，与 :meth:`DownloadRegion.bounds_text` 同格式）。"""
        return (
            f"{self.bounds.west:.4f}, {self.bounds.south:.4f}, "
            f"{self.bounds.east:.4f}, {self.bounds.north:.4f}"
        )

    def describe(self) -> str:
        """一行展示文案，如 ``西湖巡检航线（12 个航点 / 5.00 km）``。"""
        return (
            f"{self.name}（{self.waypoint_count} 个航点 / "
            f"{self.buffer_km:.2f} km）"
        )

    def with_geojson_path(self, path: str) -> "MissionRegion":
        """回填几何文件路径（返回新实例；本类型不可变）。"""
        return replace(self, geojson_path=str(path))


# --------------------------------------------------------------------------- #
# 元数据（落 metadata.json）
# --------------------------------------------------------------------------- #
#: ``metadata.json`` 的字段顺序。写文件时按它输出，读回来时**不强求**顺序 ——
#: 顺序只为了 diff 好看与人工核对。
MISSION_METADATA_FIELD_ORDER: tuple[str, ...] = (
    "version",
    "id",
    "name",
    "buffer_meters",
    "waypoint_count",
    "geometry_version",
    "created_at",
    "bounds",
)


@dataclass(frozen=True)
class MissionRegionMetadata:
    """任务区目录里的 ``metadata.json``（Spec §11）。

    它是"这个目录里装着什么"的唯一说明：几何本身在 ``region.geojson`` 里，
    但"半径多少、有几个航点、是哪一版几何"只有这里知道 —— 续传与 Inspector
    都不该靠"反推几何"来回答这些问题。

    Attributes:
        version: 元数据格式版本（见 :data:`~app.config.settings.MISSION_METADATA_VERSION`）。
        id: 任务区 id（与目录名一致，读的时候会校验）。
        name: 展示名。
        buffer_meters: 走廊半径（米）。
        waypoint_count: 航点数。
        geometry_version: 几何版本号。
        created_at: 创建时间（ISO 8601 带时区）。
        bounds: 几何外包范围。**与 ``region.geojson`` 的实际范围必须一致** ——
            读的时候会对一遍（见 :meth:`MissionRegionStore.load`）。
    """

    id: str
    name: str
    buffer_meters: float
    waypoint_count: int
    geometry_version: str
    bounds: GeoBounds
    created_at: str = ""
    version: int = MISSION_METADATA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_mission_id(self.id))
        object.__setattr__(self, "name", str(self.name or "").strip())
        object.__setattr__(self, "buffer_meters", _as_finite_float(self.buffer_meters, "Buffer 半径"))
        object.__setattr__(self, "waypoint_count", int(self.waypoint_count))
        object.__setattr__(
            self, "geometry_version", str(self.geometry_version or "").strip()
        )
        object.__setattr__(self, "created_at", str(self.created_at or "").strip())
        object.__setattr__(self, "version", int(self.version))
        if not isinstance(self.bounds, GeoBounds):
            raise MissionRegionError(
                f"metadata.bounds 必须是 GeoBounds，实际为 {type(self.bounds).__name__}"
            )

    @classmethod
    def from_region(cls, region: MissionRegion) -> "MissionRegionMetadata":
        """从一个任务区生成它的元数据。"""
        return cls(
            id=region.id,
            name=region.name,
            buffer_meters=region.buffer_meters,
            waypoint_count=region.waypoint_count,
            geometry_version=region.geometry_version,
            bounds=region.bounds,
            created_at=region.created_at or now_iso(),
        )

    def to_dict(self) -> dict[str, Any]:
        """转成 JSON 结构（snake_case，与 ``tasks.json`` / ``plans.json`` 一致）。"""
        data: dict[str, Any] = {
            "version": self.version,
            "id": self.id,
            "name": self.name,
            "buffer_meters": self.buffer_meters,
            "waypoint_count": self.waypoint_count,
            "geometry_version": self.geometry_version,
            "created_at": self.created_at,
            "bounds": {
                "west": self.bounds.west,
                "south": self.bounds.south,
                "east": self.bounds.east,
                "north": self.bounds.north,
            },
        }
        return {key: data[key] for key in MISSION_METADATA_FIELD_ORDER}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MissionRegionMetadata":
        """从 JSON 结构还原。

        Raises:
            MissionRegionError: 结构不成立（不是对象 / 缺字段 / bounds 不完整）。
        """
        if not isinstance(data, Mapping):
            raise MissionRegionError(
                f"任务区元数据必须是 JSON 对象，实际为 {type(data).__name__}"
            )
        raw_bounds = data.get("bounds")
        if not isinstance(raw_bounds, Mapping):
            raise MissionRegionError("任务区元数据缺少 bounds（或它不是对象）")
        try:
            bounds = GeoBounds(
                west=raw_bounds.get("west"),
                south=raw_bounds.get("south"),
                east=raw_bounds.get("east"),
                north=raw_bounds.get("north"),
            )
        except (TypeError, ValueError) as exc:
            raise MissionRegionError(f"任务区元数据的 bounds 不合法：{exc}") from exc

        for key in ("id", "name", "buffer_meters", "waypoint_count", "geometry_version"):
            if key not in data:
                raise MissionRegionError(f"任务区元数据缺少字段 {key!r}")

        return cls(
            id=data["id"],
            name=data["name"],
            buffer_meters=data["buffer_meters"],
            waypoint_count=data["waypoint_count"],
            geometry_version=data["geometry_version"],
            bounds=bounds,
            created_at=data.get("created_at") or "",
            version=int(data.get("version") or MISSION_METADATA_VERSION),
        )
