"""几何与 id 标准化（Slice 26，Spec §5 / §6 / §7）。

这一层做三件事，全部**逐 Feature** 进行（Spec §21：不枚举瓦片、不读
MBTiles、不做无意义 deep copy —— 行政区导入是预处理阶段，不是下载）：

1. **几何校验**（Spec §5）：非空、WGS84 合理范围、``is_valid``。
   轻微可修复的（自相交）走 ``make_valid``（buffer(0)）并记 **warning**；
   修不好或范围越界 → error，整批 FAIL。**绝不静默修改**严重异常数据。
2. **几何简化**（Spec §6）：默认关闭。只有配置显式给了
   ``simplify_tolerance`` 才简化，且同样记 warning —— 简化会改变边界，
   而边界变了"哪些瓦片在区域内"就变了，用户必须知情。
3. **id 清洗**（Spec §7）：稳定的行政区 code 原样使用；不合法的走统一
   sanitize 并保留 ``originalCode``。中文名称**不**用来生成 id。

经纬度顺序
----------

Shapely 的 ``shape()`` 按 GeoJSON 规范解析坐标数组，顺序天然是
``[lon, lat]``；写回时优先用源数据的原始坐标字典（见
:meth:`RegionSourceFeature.output_geometry`），因此"保持 lon/lat 顺序"
在这里是**结构性保证**，不靠某个函数记得swap。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from shapely.geometry import Polygon, MultiPolygon

from app.geo.bounds import GeoBounds
from app.planning.region_level import RegionLevel
from app.regions.importer.models import (
    RegionImportIssue,
    RegionImportIssueCode,
    RegionSourceFeature,
    sanitize_region_id,
)

__all__ = ["normalize_features", "WGS84_LON_RANGE", "WGS84_LAT_RANGE"]

#: WGS84 经度合理范围（Spec §5）。
WGS84_LON_RANGE: tuple[float, float] = (-180.0, 180.0)

#: WGS84 纬度合理范围（Spec §5）。
WGS84_LAT_RANGE: tuple[float, float] = (-90.0, 90.0)


def _polygonal(geometry: Any) -> bool:
    """是不是面几何（Polygon / MultiPolygon）。

    修复后的几何可能是 GeometryCollection（buffer(0) 的常见产物），
    里面混着线 —— 那种"修好了"也不能用，必须拒绝。
    """
    return isinstance(geometry, (Polygon, MultiPolygon))


def _check_wgs84(geometry: Any) -> str:
    """检查坐标范围；返回错误说明（空串 = 通过）。"""
    min_x, min_y, max_x, max_y = geometry.bounds
    if not (WGS84_LON_RANGE[0] <= min_x and max_x <= WGS84_LON_RANGE[1]):
        return (
            f"经度超出 WGS84 范围（{[WGS84_LON_RANGE[0], WGS84_LON_RANGE[1]]}）："
            f"实际西到 {min_x:.6f}、东到 {max_x:.6f}"
        )
    if not (WGS84_LAT_RANGE[0] <= min_y and max_y <= WGS84_LAT_RANGE[1]):
        return (
            f"纬度超出 WGS84 范围（{[WGS84_LAT_RANGE[0], WGS84_LAT_RANGE[1]]}）："
            f"实际南到 {min_y:.6f}、北到 {max_y:.6f}"
        )
    if min_x >= max_x:
        return (
            f"边界范围东西颠倒或为零（西 {min_x:.6f} ≥ 东 {max_x:.6f}）："
            "跨国际日期变更线的数据本工具不支持"
        )
    return ""


def normalize_geometry(
    feature: RegionSourceFeature,
    *,
    simplify_tolerance: float | None = None,
    issues: list[RegionImportIssue] | None = None,
) -> bool:
    """标准化一条 Feature 的几何（Spec §5 / §6）。

    就地更新 ``feature.geometry`` / ``feature.geometry_modified`` /
    ``feature.geometry_note``；问题记进 ``issues``。

    Args:
        feature: 待标准化的条目（``geometry`` 已由 reader 填好）。
        simplify_tolerance: 简化容差（度）；``None`` = 不简化（默认）。
        issues: 问题收集列表。

    Returns:
        ``True`` = 几何可用；``False`` = 不可用（调用方应拒绝该条）。
    """
    sink = issues if issues is not None else []
    geometry = feature.geometry
    label = feature.label

    if geometry is None or geometry.is_empty:  # pragma: no cover - reader 已拦
        sink.append(
            RegionImportIssue(
                RegionImportIssueCode.EMPTY_GEOMETRY,
                "几何为空",
                label,
            )
        )
        return False

    range_error = _check_wgs84(geometry)
    if range_error:
        sink.append(
            RegionImportIssue(
                RegionImportIssueCode.GEOMETRY_OUT_OF_RANGE,
                range_error,
                label,
            )
        )
        return False

    if not geometry.is_valid:
        # 轻微可修复（典型：自相交）→ make_valid 并记 warning（Spec §5）。
        # 修复结果必须仍是面几何且合法，否则按严重异常拒绝。
        try:
            reason = geometry.is_valid_reason()
        except Exception:  # noqa: BLE001 - 老版本 shapely 没有这个方法
            reason = "未提供原因"
        repaired = geometry.buffer(0)
        if repaired.is_empty or not repaired.is_valid or not _polygonal(repaired):
            sink.append(
                RegionImportIssue(
                    RegionImportIssueCode.GEOMETRY_INVALID,
                    f"几何非法（{reason}），自动修复后仍不可用",
                    label,
                )
            )
            return False
        feature.geometry = repaired
        feature.geometry_modified = True
        feature.geometry_note = f"几何非法（{reason}），已按 make_valid 修复"
        sink.append(
            RegionImportIssue(
                RegionImportIssueCode.GEOMETRY_REPAIRED,
                feature.geometry_note,
                label,
                level=_warning_level(),
            )
        )
        geometry = repaired

    if simplify_tolerance is not None and simplify_tolerance > 0:
        # 用户显式启用才简化（Spec §6）。简化结果不可用就保留原几何，
        # 并如实说"简化被跳过"—— 静默改边界比不简化危险得多。
        simplified = geometry.simplify(float(simplify_tolerance))
        if simplified.is_empty or not simplified.is_valid or not _polygonal(simplified):
            sink.append(
                RegionImportIssue(
                    RegionImportIssueCode.GEOMETRY_SIMPLIFIED,
                    f"请求按容差 {simplify_tolerance} 简化，但简化结果不可用，"
                    "已保留原始几何",
                    label,
                    level=_warning_level(),
                )
            )
        else:
            feature.geometry = simplified
            feature.geometry_modified = True
            note = (
                f"已按容差 {simplify_tolerance}（度）简化几何；"
                "简化会改变边界形状，可能影响「哪些瓦片落在区域内」"
            )
            feature.geometry_note = (
                f"{feature.geometry_note}；{note}" if feature.geometry_note else note
            )
            sink.append(
                RegionImportIssue(
                    RegionImportIssueCode.GEOMETRY_SIMPLIFIED,
                    note,
                    label,
                    level=_warning_level(),
                )
            )

    return True


def _warning_level():
    """延迟 import :class:`IssueLevel`（避免模块级循环引用的小代价）。"""
    from app.regions.importer.models import IssueLevel

    return IssueLevel.WARNING


def normalize_ids(
    features: tuple[RegionSourceFeature, ...] | list[RegionSourceFeature],
    *,
    issues: list[RegionImportIssue],
) -> tuple[RegionSourceFeature, ...]:
    """清洗 region id 并查重（Spec §7）。

    全局唯一性在这里第一次成型：清洗后的 id 若重复，**保留第一条**、
    其余拒绝（记 DUPLICATE_ID）。为什么不是"自动加后缀"：id 是跨层契约
    （tasks.json / MBTiles metadata / manifest 都写它），偷偷改一个用户的
    code 会让"同一个省"在不同批次里变成两个 id，续传直接对不上。

    Returns:
        通过 id 校验的条目（顺序不变）。
    """
    accepted: list[RegionSourceFeature] = []
    seen: dict[str, RegionSourceFeature] = {}
    for feature in features:
        region_id, sanitized = sanitize_region_id(feature.region_id)
        if not region_id:
            issues.append(
                RegionImportIssue(
                    RegionImportIssueCode.ID_EMPTY,
                    f"id/code 原值 {feature.code!r} 清洗后仍为空，已跳过",
                    f"第 {feature.source_index + 1} 条",
                )
            )
            continue
        normalized = region_id.lower()
        if normalized in seen:
            first = seen[normalized]
            issues.append(
                RegionImportIssue(
                    RegionImportIssueCode.DUPLICATE_ID,
                    f"id {region_id!r} 与第 {first.source_index + 1} 条"
                    f"（{first.name}）重复，已保留先出现的那一条",
                    feature.name or f"第 {feature.source_index + 1} 条",
                )
            )
            continue
        feature.region_id = normalized
        feature.id_sanitized = sanitized
        if sanitized:
            issues.append(
                RegionImportIssue(
                    RegionImportIssueCode.ID_SANITIZED,
                    f"id/code 原值 {feature.code!r} 含文件名不安全字符，"
                    f"已统一清洗为 {region_id!r}（原值保留在 "
                    "properties.originalCode）",
                    feature.name or region_id,
                    level=_warning_level(),
                )
            )
        seen[normalized] = feature
        accepted.append(feature)
    return tuple(accepted)


def normalize_features(
    features: tuple[RegionSourceFeature, ...] | list[RegionSourceFeature],
    *,
    simplify_tolerance: float | None = None,
    issues: list[RegionImportIssue],
) -> tuple[RegionSourceFeature, ...]:
    """标准化一整批 Feature（Spec §5 / §6 / §7）。

    顺序：先几何、后 id。几何不过关的条目直接出局（不占 id 名额 ——
    一条坏几何不该把"它想用的那个 id"堵死）。
    """
    alive: list[RegionSourceFeature] = []
    for feature in features:
        if normalize_geometry(feature, simplify_tolerance=simplify_tolerance, issues=issues):
            alive.append(feature)
    return normalize_ids(alive, issues=issues)


def bounds_of(geometry: Any) -> GeoBounds:
    """几何的外包矩形（给 catalog_generator 算 index.json 的 bounds）。

    Raises:
        ValueError: 经度跨国际日期变更线（与 Slice 16 的加载器同一规矩：
            静默取模会算出完全错误的瓦片）。
    """
    from app.geo.geojson_loader import geometry_to_bounds

    return geometry_to_bounds(geometry)


def count_levels(
    features: tuple[RegionSourceFeature, ...] | list[RegionSourceFeature],
) -> dict[RegionLevel, int]:
    """按等级数条目（验证预览的 COUNTRY / PROVINCE / CITY 三行）。"""
    counts: dict[RegionLevel, int] = {}
    for feature in features:
        level = feature.level
        if level is None:  # pragma: no cover - reader 已保证有等级
            continue
        counts[level] = counts.get(level, 0) + 1
    return counts
