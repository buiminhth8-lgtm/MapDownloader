"""航线导入（Slice 22，Spec §6）：GeoJSON LineString → 航点序列。

只认 **GeoJSON 的线语义**，三种写法都接受：

===========================================  =====================================
``{"type": "LineString", "coordinates": …}``  裸几何
``{"type": "Feature", "geometry": {…}}``      单要素
``{"type": "FeatureCollection", "features":…}``只取**唯一**那条 LineString
===========================================  =====================================

不认 **KML / GPX**（Spec §6 明令）：它们是另一套 XML 方言，各自的"航迹"语义
（航路点 / 航段 / 时间戳）与"一条折线"并不等价，硬塞进来只会让人以为
"导入的航迹点都算数"。真要用，先用外部工具转成 GeoJSON。

三条输入纪律
------------

1. **至少两个点**：一个点只能缓冲出一个圆，那是"自定义区域"而不是航线任务。
2. **经纬度必须合法**：经度 ±180；纬度受 Web 墨卡托限制（±85.05112878）——
   越界的点在瓦片计算里没有定义。
3. **相邻重复点直接去掉**：手工描的航线里经常出现"同一个点连点两下"，
   它不改变几何，却会让"12 个航点"这种数字与用户的认知对不上。

返回的是**元组**（不是列表）：航点序列随后要放进不可变的
:class:`~app.mission.models.MissionRegion`，一开始就是不可变的，
省得在某个环节被就地改一下。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.mission.models import MissionRouteError, MissionWaypoint

__all__ = [
    "load_route_waypoints",
    "load_route_document",
    "parse_route_geojson",
    "read_route_text",
]

#: 明确拒绝的扩展名 → 给用户的一句人话（Spec §6）。
_REJECTED_EXTENSIONS: dict[str, str] = {
    ".kml": "KML",
    ".kmz": "KMZ",
    ".gpx": "GPX",
}

#: GeoJSON 里"线"的顶层类型
_LINE_TYPES = frozenset({"LineString", "MultiLineString"})


def load_route_waypoints(source: str | Path | Mapping[str, Any]) -> tuple[MissionWaypoint, ...]:
    """读一个航线文件（或已经解析好的 GeoJSON 对象）→ 航点元组。

    Args:
        source: 文件路径，或已经是 ``dict`` 的 GeoJSON。

    Raises:
        MissionRouteError: 文件不存在 / 不是 JSON / 是 KML·GPX / 顶层类型不认识 /
            没有 LineString / 坐标不合法 / 去重后不足两个点。
    """
    if isinstance(source, Mapping):
        return parse_route_geojson(source)
    return parse_route_geojson(load_route_document(source))


def load_route_document(path: str | Path) -> dict[str, Any]:
    """读航线文件并解析成 JSON 对象。

    Raises:
        MissionRouteError: 文件不存在、不是 JSON、或顶层不是对象。
    """
    target = Path(path)
    text = read_route_text(target)
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MissionRouteError(
            f"航线文件不是合法的 JSON（{target}）：第 {exc.lineno} 行第 {exc.colno} 列 {exc.msg}"
        ) from exc
    if not isinstance(document, dict):
        raise MissionRouteError(
            f"航线文件的顶层必须是 JSON 对象（GeoJSON），实际为 {type(document).__name__}：{target}"
        )
    return document


def read_route_text(path: Path) -> str:
    """读航线文件的文本内容，顺带把"KML / GPX 这类格式"讲清楚。

    Raises:
        MissionRouteError: 文件不存在 / 是 KML·KMZ·GPX / 读不出来。
    """
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix in _REJECTED_EXTENSIONS:
        raise MissionRouteError(
            f"不支持 {_REJECTED_EXTENSIONS[suffix]} 格式（{target.name}）："
            "本工具只认 GeoJSON 的 LineString，请先转成 .geojson 再导入。"
            "（KML/GPX 的航迹点与「一条折线」并不等价，直接读会让人以为"
            "所有航路点都参与了运算）"
        )
    if not target.is_file():
        raise MissionRouteError(f"航线文件不存在：{target}")
    try:
        return target.read_text(encoding="utf-8")
    except OSError as exc:
        raise MissionRouteError(f"航线文件读不出来（{target}）：{exc}") from exc
    except UnicodeDecodeError as exc:
        raise MissionRouteError(
            f"航线文件不是 UTF-8 文本（{target}）：{exc}（GeoJSON 规定必须是 UTF-8）"
        ) from exc


def parse_route_geojson(document: Mapping[str, Any]) -> tuple[MissionWaypoint, ...]:
    """从一个已解析的 GeoJSON 对象里取出航点。

    Raises:
        MissionRouteError: 见模块 docstring 的三条输入纪律。
    """
    if not isinstance(document, Mapping):
        raise MissionRouteError(
            f"GeoJSON 必须是对象，实际为 {type(document).__name__}"
        )
    top_type = str(document.get("type") or "").strip()
    raw_coordinates: Any = None

    if top_type == "LineString":
        raw_coordinates = document.get("coordinates")
    elif top_type == "MultiLineString":
        raise MissionRouteError(
            "本工具只接受单条 LineString 航线：MultiLineString 请拆成多条，"
            "每条单独建一个任务区（多段航线各自的 Buffer 会并成一大片，"
            "那不是「沿航线的高精度走廊」）"
        )
    elif top_type == "Feature":
        raw_coordinates = _coordinates_from_geometry(document.get("geometry"))
    elif top_type == "FeatureCollection":
        raw_coordinates = _coordinates_from_features(document.get("features"))
    else:
        raise MissionRouteError(
            f"顶层 type 不认识：{top_type!r}（只支持 LineString / Feature / FeatureCollection）"
        )

    return _waypoints_from_coordinates(raw_coordinates)


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
def _coordinates_from_geometry(geometry: Any) -> Any:
    """``Feature.geometry`` → 坐标数组。"""
    if not isinstance(geometry, Mapping):
        raise MissionRouteError(
            "Feature 里没有合法的 geometry（它必须是对象，且 type=LineString）"
        )
    geometry_type = str(geometry.get("type") or "").strip()
    if geometry_type not in _LINE_TYPES:
        raise MissionRouteError(
            f"Feature 的几何类型是 {geometry_type!r}，本工具只接受 LineString"
        )
    if geometry_type == "MultiLineString":
        raise MissionRouteError(
            "本工具只接受单条 LineString 航线：MultiLineString 请拆成多条分别建任务区"
        )
    return geometry.get("coordinates")


def _coordinates_from_features(features: Any) -> Any:
    """``FeatureCollection.features`` → **唯一**那条 LineString 的坐标数组。"""
    if not isinstance(features, list):
        raise MissionRouteError("FeatureCollection 的 features 必须是数组")
    found: list[Any] = []
    for index, feature in enumerate(features):
        if not isinstance(feature, Mapping):
            continue
        geometry = feature.get("geometry")
        if not isinstance(geometry, Mapping):
            continue
        if str(geometry.get("type") or "").strip() != "LineString":
            continue
        found.append(geometry.get("coordinates"))
        if len(found) > 1:
            raise MissionRouteError(
                "FeatureCollection 里有不止一条 LineString："
                f"第 {index + 1} 个要素是第 {len(found)} 条。"
                "请只保留一条，或拆成多个文件分别建任务区"
            )
    if not found:
        raise MissionRouteError("FeatureCollection 里没有找到 LineString 几何")
    return found[0]


def _waypoints_from_coordinates(raw: Any) -> tuple[MissionWaypoint, ...]:
    """坐标数组 → 航点元组（校验 + 去重）。

    Raises:
        MissionRouteError: 不是数组 / 点数不足 / 某一点不是合法的 ``[lon, lat]``。
    """
    if not isinstance(raw, (list, tuple)):
        raise MissionRouteError(
            f"LineString.coordinates 必须是数组，实际为 {type(raw).__name__}"
        )
    waypoints: list[MissionWaypoint] = []
    for index, item in enumerate(raw):
        waypoints.append(_waypoint_from_position(item, index))
    return dedupe_waypoints(waypoints)


def _waypoint_from_position(item: Any, index: int) -> MissionWaypoint:
    """单个坐标位置 ``[lon, lat]``（允许带第三维，直接忽略）→ 航点。"""
    if not isinstance(item, (list, tuple)) or len(item) < 2:
        raise MissionRouteError(
            f"第 {index + 1} 个坐标必须是 [经度, 纬度] 形式，实际为 {item!r}"
        )
    lon, lat = item[0], item[1]
    try:
        return MissionWaypoint(lon=lon, lat=lat)
    except MissionRouteError as exc:
        raise MissionRouteError(f"第 {index + 1} 个坐标不合法：{exc}") from exc


def dedupe_waypoints(waypoints: Iterable[MissionWaypoint]) -> tuple[MissionWaypoint, ...]:
    """去掉**相邻**重复点，并确认剩下的点数够画一条线。

    先 materialize 再数数：``waypoints`` 可能是生成器，先 ``len()`` 再遍历
    会得到一个"已经被消费掉"的空序列（错误信息里就会写"只有 0 个航点"，
    而真相是 3 个重合点）。

    Raises:
        MissionRouteError: 去重后不足两个点。
    """
    listed = list(waypoints)
    unique: list[MissionWaypoint] = []
    for point in listed:
        if unique and (point.lon, point.lat) == (unique[-1].lon, unique[-1].lat):
            continue
        unique.append(point)
    if len(unique) < 2:
        if len(listed) < 2:
            raise MissionRouteError(
                f"航线至少需要 2 个航点，实际只有 {len(listed)} 个"
            )
        raise MissionRouteError(
            f"航线至少需要 2 个**不同**的航点，"
            f"实际 {len(listed)} 个点全部落在同一个位置（{listed[0].text()}）"
        )
    return tuple(unique)
