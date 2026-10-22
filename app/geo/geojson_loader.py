"""GeoJSON 几何加载与校验（Slice 16）。

职责单一：**把一个 GeoJSON 文件读成一个 Shapely 几何对象**，并把"读不出来"
的每种情况翻译成一个说得清原因的异常。本模块**不做**瓦片计算、不碰 UI、
不写磁盘。

支持的顶层形态
--------------

根据 RFC 7946，本模块接受四种顶层 ``type``：

- ``FeatureCollection`` —— 取全部 feature 的几何做 :func:`shapely.ops.unary_union`，
  合并成一个（可能是 MultiPolygon 的）几何。这是行政区边界最常见的形态
  （一个省 = 一个 feature）。
- ``Feature`` —— 直接取它的 ``geometry``。
- ``Polygon`` / ``MultiPolygon`` —— 裸几何。
- ``GeometryCollection`` —— 递归收集其中所有面几何后合并。

``Point`` / ``LineString`` 这类**不含面**的几何会被拒绝：它们对"哪些瓦片落在
区域内"这个问题没有答案，放行只会让下游算出一堆看似正常的数字。

校验策略（有意从严）
-------------------

按 Spec §4/§32：**不静默修复损坏的几何**。自相交（``bowtie``）之类的几何
在 :meth:`shapely.Geometry.is_valid` 上为 ``False``，此处直接抛
:class:`GeoJSONGeometryError`，并把 ``is_valid_reason()`` 带进错误信息 ——
真实边界数据有问题时，用户需要知道"是数据源要修"，而不是看到一个悄悄被
``buffer(0)`` 抹平的形状（那种几何的 ``intersects`` 结果是不可信的）。

确实需要放行时，显式传 ``attempt_repair=True``：这不是"静默修复"，因为
调用方必须自己写下这个参数，而且修复失败仍会抛异常。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from app.config.settings import REGION_BOUNDARY_MISSING_HINT, WEB_MERCATOR_MAX_LAT
from app.geo.bounds import GeoBounds

__all__ = [
    "GeoJSONDocument",
    "GeoJSONError",
    "GeoJSONGeometryError",
    "GeoJSONNotFoundError",
    "GeoJSONParseError",
    "geometry_to_bounds",
    "load_geojson_document",
    "load_geojson_geometry",
]


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class GeoJSONError(ValueError):
    """行政区边界文件相关错误的基类。

    单独一棵异常树（而不是复用 ``ValueError`` / ``OSError``）的理由：调用方需要
    把"文件不在"（去放文件）、"JSON 坏了"（去修文件）、"几何非法"（去找数据源）
    分成三种完全不同的处置，而它们都有明确的用户可见文案。
    """


class GeoJSONNotFoundError(GeoJSONError):
    """文件不存在，或不是一个可读的普通文件。"""


class GeoJSONParseError(GeoJSONError):
    """文件不是合法 JSON，或顶层结构不是本模块支持的 GeoJSON 形态。"""


class GeoJSONGeometryError(GeoJSONError):
    """几何缺失、为空、非法（自相交等），或不含任何面。"""


# --------------------------------------------------------------------------- #
# 加载结果
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GeoJSONDocument:
    """一次成功加载的产物。

    Attributes:
        geometry: 合并后的 Shapely 几何（Polygon / MultiPolygon / GeometryCollection）。
        properties: 从 Feature / FeatureCollection 里收集到的 properties
            （多个 feature 时后者覆盖前者的同名字段）。裸几何时为空 dict。
        geojson_type: 文件顶层的 ``type``，用于错误信息与日志。
        feature_count: 参与合并的 feature 数（裸几何计 1）。数字本身没意义，
            但它能解释"为什么这个文件加载出来这么慢"。
        source: 源文件路径（字符串），只用于展示与日志。
    """

    geometry: BaseGeometry
    properties: dict[str, Any] = field(default_factory=dict)
    geojson_type: str = ""
    feature_count: int = 1
    source: str = ""

    @property
    def name(self) -> str:
        """``properties.name``；没有时返回空串。"""
        return str(self.properties.get("name", "") or "").strip()

    @property
    def region_id(self) -> str:
        """``properties.id``；没有时返回空串。"""
        return str(self.properties.get("id", "") or "").strip()

    @property
    def boundary_version(self) -> str:
        """``properties.boundaryVersion``；没有时返回空串。

        兼容 ``boundary_version`` 这个蛇形写法：不同数据源对同一个概念的
        键名不一样，读的时候两种都认，比在 README 里规定"必须用驼峰"更实在。
        """
        value = self.properties.get("boundaryVersion")
        if value is None:
            value = self.properties.get("boundary_version")
        return str(value or "").strip()


#: 允许的顶层 GeoJSON ``type``（其余一律拒绝，见模块 docstring）。
_SUPPORTED_TOP_TYPES: frozenset[str] = frozenset(
    {"FeatureCollection", "Feature", "Polygon", "MultiPolygon", "GeometryCollection"}
)

#: 含面（有"区域"语义）的几何类型。只有这些能回答"瓦片是否落在区域内"。
_AREA_GEOMETRY_TYPES: frozenset[str] = frozenset(
    {"Polygon", "MultiPolygon", "GeometryCollection"}
)


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _iter_polygonal(geometry: BaseGeometry) -> Iterator[BaseGeometry]:
    """递归产出几何里所有**面**部分（丢掉点/线）。

    ``GeometryCollection`` 里可以混着点和线，直接 ``shape()`` 出来再
    ``intersects`` 会得到"点到瓦片也算相交"的荒唐结果，因此这里主动过滤。
    """
    if geometry.geom_type == "GeometryCollection":
        for item in geometry.geoms:  # type: ignore[attr-defined]
            yield from _iter_polygonal(item)
    elif geometry.geom_type in {"Polygon", "MultiPolygon"}:
        yield geometry
    # Point / LineString / ...：跳过


def _merge(geometries: list[BaseGeometry]) -> BaseGeometry:
    """合并成单个几何；只有一个时原样返回（省掉一次昂贵的 union）。"""
    if len(geometries) == 1:
        return geometries[0]
    return unary_union(geometries)


def _read_json(path: Path) -> Any:
    """读文件并解析 JSON，把两种失败翻译成明确异常。"""
    if not path.is_file():
        raise GeoJSONNotFoundError(
            REGION_BOUNDARY_MISSING_HINT.format(path=path)
        )
    try:
        # 显式 utf-8：GeoJSON 规范要求 UTF-8，用平台默认编码读中文属性
        # 在中文 Windows（GBK）上会直接 UnicodeDecodeError。
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise GeoJSONParseError(
            f"{path} 不是合法的 UTF-8 文本（GeoJSON 规范要求 UTF-8）：{exc}"
        ) from exc
    except OSError as exc:
        raise GeoJSONNotFoundError(f"无法读取行政区边界文件 {path}：{exc}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GeoJSONParseError(f"{path} 不是合法 JSON：{exc}") from exc


def _extract_features(document: Any, path: Path) -> tuple[list[Any], dict[str, Any]]:
    """把顶层文档拆成 ``(geometry 节点列表, properties)``。

    Raises:
        GeoJSONParseError: 顶层不是对象 / 缺 ``type`` / ``type`` 不受支持 /
            ``FeatureCollection`` 的 ``features`` 不是数组。
    """
    if not isinstance(document, Mapping):
        raise GeoJSONParseError(
            f"{path} 的顶层必须是 JSON 对象，实际为 {type(document).__name__}"
        )

    top_type = document.get("type")
    if not isinstance(top_type, str) or not top_type:
        raise GeoJSONParseError(f"{path} 的顶层缺少字符串字段 type")

    if top_type not in _SUPPORTED_TOP_TYPES:
        raise GeoJSONParseError(
            f"{path} 的顶层 type={top_type!r} 不受支持，"
            f"只支持 {sorted(_SUPPORTED_TOP_TYPES)}"
        )

    properties: dict[str, Any] = {}

    if top_type == "FeatureCollection":
        features = document.get("features")
        if not isinstance(features, list):
            raise GeoJSONParseError(
                f"{path} 的 FeatureCollection.features 必须是数组，"
                f"实际为 {type(features).__name__}"
            )
        nodes: list[Any] = []
        for feature in features:
            if not isinstance(feature, Mapping):
                continue
            item_props = feature.get("properties")
            if isinstance(item_props, Mapping):
                properties.update(item_props)
            nodes.append(feature.get("geometry"))
        return nodes, properties

    if top_type == "Feature":
        item_props = document.get("properties")
        if isinstance(item_props, Mapping):
            properties.update(item_props)
        return [document.get("geometry")], properties

    # 裸几何：没有 properties 可收集，id / boundaryVersion 由调用方（Preset）补
    return [document], properties


# --------------------------------------------------------------------------- #
# 公开 API
# --------------------------------------------------------------------------- #
def load_geojson_document(
    path: str | Path,
    *,
    attempt_repair: bool = False,
) -> GeoJSONDocument:
    """加载并校验一个 GeoJSON 文件，返回几何与 properties。

    Args:
        path: ``.geojson`` / ``.json`` 文件路径。
        attempt_repair: 几何非法时是否尝试 ``buffer(0)`` 修复。**默认关闭**
            （Spec §32：不静默修复严重损坏的 Geometry）。开启后仍要求修复结果
            非空且合法，否则照样抛异常。

    Returns:
        :class:`GeoJSONDocument`。

    Raises:
        GeoJSONNotFoundError: 文件不存在 / 不可读 / 编码不是 UTF-8 之外的 IO 问题。
        GeoJSONParseError: 不是合法 JSON，或顶层结构不受支持。
        GeoJSONGeometryError: 几何缺失、为空、非法（且未修复成功），或不含面。

    >>> path = "tests/fixtures/regions/square.geojson"   # doctest: +SKIP
    >>> doc = load_geojson_document(path)                # doctest: +SKIP
    >>> round(doc.geometry.area, 4)                      # doctest: +SKIP
    0.01
    """
    target = Path(path)
    document = _read_json(target)
    nodes, properties = _extract_features(document, target)

    if not nodes:
        raise GeoJSONGeometryError(f"{target} 里没有任何几何（features 为空？）")

    collected: list[BaseGeometry] = []
    for node in nodes:
        if node is None:
            # 单个 feature 缺 geometry：跳过而不是整体失败。
            # 一个 FeatureCollection 里混进一条没有几何的记录，不该让整份
            # 行政区边界不可用。
            continue
        if not isinstance(node, Mapping):
            raise GeoJSONGeometryError(
                f"{target} 的 geometry 必须是 JSON 对象，实际为 {type(node).__name__}"
            )
        try:
            geometry = shape(node)
        except Exception as exc:  # noqa: BLE001 - shapely 的异常类型不稳定
            raise GeoJSONGeometryError(
                f"{target} 的 geometry 无法解析为几何：{exc}"
            ) from exc
        collected.extend(_iter_polygonal(geometry))

    if not collected:
        raise GeoJSONGeometryError(
            f"{target} 不含任何面几何（Polygon / MultiPolygon）；"
            "点与线无法回答「哪些瓦片落在区域内」这个问题"
        )

    geometry = _merge(collected)

    if geometry.is_empty:
        raise GeoJSONGeometryError(f"{target} 的几何为空（empty）")

    if not geometry.is_valid:
        reason = ""
        try:
            reason = geometry.is_valid_reason()
        except Exception:  # noqa: BLE001 - 老版本 shapely 没有这个方法
            reason = "未提供原因"
        if not attempt_repair:
            raise GeoJSONGeometryError(
                f"{target} 的几何非法（{reason}）。本工具不会静默修复损坏的边界，"
                "请换用合法授权的正确数据；确需强行修复请显式开启 attempt_repair"
            )
        repaired = geometry.buffer(0)
        if repaired.is_empty or not repaired.is_valid:
            raise GeoJSONGeometryError(
                f"{target} 的几何非法（{reason}），尝试修复后仍然不可用"
            )
        geometry = repaired

    return GeoJSONDocument(
        geometry=geometry,
        properties=dict(properties),
        geojson_type=str(document.get("type", "")),
        feature_count=max(1, len(collected)),
        source=str(target),
    )


def load_geojson_geometry(
    path: str | Path,
    *,
    attempt_repair: bool = False,
) -> BaseGeometry:
    """加载 GeoJSON 并只返回几何（:func:`load_geojson_document` 的薄封装）。

    Raises:
        同 :func:`load_geojson_document`。
    """
    return load_geojson_document(path, attempt_repair=attempt_repair).geometry


def geometry_to_bounds(geometry: BaseGeometry) -> GeoBounds:
    """把 Shapely 几何的包络（envelope）转成 :class:`~app.geo.GeoBounds`。

    纬度按 Web 墨卡托极限 clamp（与 :func:`app.geo.lat_to_tile_y` 对极点的
    处理一致）：全球尺度的边界数据给出 ``(-90, 90)`` 时，直接拒绝会让"能算出
    瓦片的范围"变成一个无法表达的矩形，而 clamp 到 ±85.05112878 正是各家
    瓦片服务的通行做法。经度**不做**取模处理：跨国际日期变更线的数据在这里
    明确报错（本工具的 ``GeoBounds`` 不支持跨线，静默取模会算出完全错误的瓦片）。

    Raises:
        GeoJSONGeometryError: 几何为空。
        ValueError: 经度跨国际日期变更线（``west >= east``）。
    """
    if geometry.is_empty:
        raise GeoJSONGeometryError("无法从空几何计算范围")

    min_x, min_y, max_x, max_y = geometry.bounds
    south = max(-WEB_MERCATOR_MAX_LAT, min(WEB_MERCATOR_MAX_LAT, float(min_y)))
    north = max(-WEB_MERCATOR_MAX_LAT, min(WEB_MERCATOR_MAX_LAT, float(max_y)))
    try:
        return GeoBounds(west=float(min_x), south=south, east=float(max_x), north=north)
    except ValueError as exc:
        raise GeoJSONGeometryError(
            f"边界范围无法用于瓦片计算：{exc}"
        ) from exc
