"""目录校验（Slice 18，Spec §6 / §13）。

两档强度，刻意分开：

===========  ==================================================================
快速（默认）  只看**元数据**：id 重复、parent 指不到人、层级不对、成环、
             路径越界、边界文件在不在。一次 stat 都不用读文件内容，
             在几百上千条记录的目录上也是毫秒级。
深度         额外**解析每一个 GeoJSON**：文件坏没坏、几何空不空、
             ``index.json`` 里声明的 bounds 与几何实际范围对不对得上。
             一百个省的数据要读几百 MB，因此只在显式要求时跑
             （命令行排查、验收脚本、CI），**绝不在启动路径上跑**。
===========  ==================================================================

"边界文件不存在"是 **warning 不是 error**：Slice 16 定下的姿态是"缺数据是
可预期的部署状态"——仓库不内置任何行政边界，用户按授权渠道自己放。把它当 error 会让
``validate_catalog()`` 在一份刚从 git clone 下来的仓库上永远失败，那样这个工具就
没人看了。真正"目录自己坏了"的（重复 id、parent 不存在、成环、路径越界）才是 error。
"""

from __future__ import annotations

from app.geo.geojson_loader import GeoJSONError, geometry_to_bounds
from app.regions.catalog import RegionCatalog
from app.regions.loader import RegionLoader
from app.regions.models import (
    PARENT_LEVEL_RULES,
    CatalogIssue,
    CatalogIssueCode,
    CatalogValidationResult,
    IssueSeverity,
    RegionPreset,
)

__all__ = ["find_parent_cycles", "validate_catalog"]

#: 声明 bounds 与几何实际范围比对的容差。两者本来是同一次计算的产物，
#: 任何可测量的差异都说明 ``index.json`` 里的值是手抄的（Spec §11 禁止的事）。
_BOUNDS_TOLERANCE: float = 1e-9


def validate_catalog(
    catalog: RegionCatalog,
    *,
    deep: bool = False,
    loader: RegionLoader | None = None,
) -> CatalogValidationResult:
    """校验一份目录（Spec §13）。

    Args:
        catalog: 待校验的目录。
        deep: 是否解析每个 GeoJSON（默认否，见模块文档）。
        loader: 深度模式用的加载器；缺省按目录现建一个。

    Returns:
        :class:`~app.regions.models.CatalogValidationResult`。``ok`` 只表示
        "没有 error"，warning（例如边界文件还没放）不算失败。
    """
    if not isinstance(catalog, RegionCatalog):
        raise ValueError(f"catalog 必须是 RegionCatalog，实际为 {type(catalog).__name__}")

    issues: list[CatalogIssue] = list(catalog.issues)
    _check_hierarchy(catalog, issues)
    _check_files(catalog, issues)

    if deep:
        _check_boundaries(
            catalog, issues, loader if loader is not None else RegionLoader(catalog)
        )

    return CatalogValidationResult(issues=tuple(issues), deep=deep, checked=len(catalog))


# --------------------------------------------------------------------------- #
# 快速档
# --------------------------------------------------------------------------- #
def _check_hierarchy(catalog: RegionCatalog, issues: list[CatalogIssue]) -> None:
    """层级规则（Spec §6）：COUNTRY 无父；PROVINCE 的父必须是 COUNTRY；
    CITY 的父必须是 PROVINCE；MISSION / CUSTOM 不受等级规则约束。"""
    for entry in catalog:
        parent_id = entry.parent_id
        if parent_id is None:
            continue

        if parent_id == entry.id:
            issues.append(
                CatalogIssue(
                    CatalogIssueCode.SELF_PARENT,
                    "parentId 指向了自己",
                    entry.id,
                )
            )
            continue

        parent = catalog.get(parent_id)
        if parent is None:
            issues.append(
                CatalogIssue(
                    CatalogIssueCode.PARENT_MISSING,
                    f"上级 {parent_id!r} 不在目录里（是不是漏了那条记录，或 id 拼错了？）",
                    entry.id,
                )
            )
            continue

        if not entry.is_administrative:
            # MISSION / CUSTOM 允许挂在任何区域下，等级规则对它们不适用（Spec §6）
            continue

        allowed_parents = PARENT_LEVEL_RULES.get(entry.level)
        if not allowed_parents:
            # 空集合 = 必须是层级的根（全国级）。
            issues.append(
                CatalogIssue(
                    CatalogIssueCode.COUNTRY_HAS_PARENT,
                    f"{entry.level.display_name}区域不该有上级（parentId={parent_id!r}）；"
                    "全国级是层级的根",
                    entry.id,
                )
            )
            continue
        if parent.level not in allowed_parents:
            allowed_text = " / ".join(
                item.display_name
                for item in sorted(allowed_parents, key=lambda level: level.value)
            )
            issues.append(
                CatalogIssue(
                    CatalogIssueCode.PARENT_LEVEL_INVALID,
                    f"{entry.level.display_name}区域的上级必须是"
                    f"{allowed_text}之一，但 {parent_id!r} 是"
                    f"{parent.level.display_name}",
                    entry.id,
                )
            )

    for region_id, cycle in find_parent_cycles(catalog).items():
        issues.append(
            CatalogIssue(
                CatalogIssueCode.PARENT_CYCLE,
                "上级关系成环：" + " → ".join(cycle),
                region_id,
            )
        )


def _check_files(catalog: RegionCatalog, issues: list[CatalogIssue]) -> None:
    """边界文件在不在（只看 stat，不读内容）。"""
    root = catalog.root
    for entry in catalog:
        if not entry.geojson_path:
            continue  # 载入时已经记过 MISSING_GEOJSON_PATH
        try:
            path = entry.path_under(root)
        except ValueError as exc:
            issues.append(
                CatalogIssue(CatalogIssueCode.UNSAFE_PATH, str(exc), entry.id)
            )
            continue
        if not path.is_file():
            issues.append(
                CatalogIssue(
                    CatalogIssueCode.GEOJSON_NOT_FOUND,
                    f"边界文件还没放：{path}（放好之后这个提醒会消失）",
                    entry.id,
                    severity=IssueSeverity.WARNING,
                )
            )


# --------------------------------------------------------------------------- #
# 深度档
# --------------------------------------------------------------------------- #
def _check_boundaries(
    catalog: RegionCatalog, issues: list[CatalogIssue], loader: RegionLoader
) -> None:
    """真的把每个 GeoJSON 读一遍（只在 deep 模式调用）。

    只报"读不出可用的几何"这一类（:data:`~app.regions.models.CatalogIssueCode.BOUNDARY_UNREADABLE`）：
    坏 JSON、缺字段、只有点/线、空几何，加载器都会抛 :class:`GeoJSONError`
    并说清是哪种，这里把它的原话带上即可 —— 目录层**不重新判一遍**
    （判两遍就一定会有一遍是错的，而且通常更宽松的那一遍会被信）。
    """
    for entry in catalog:
        if not entry.geojson_path or not loader.available(entry.id):
            continue  # 文件不在：快速档已经提醒过了，深度档不重复报
        try:
            document = loader.load_document(entry.id)
        except Exception as exc:  # noqa: BLE001 - 坏数据的形式太多，一律变成"这条有问题"
            issues.append(
                CatalogIssue(CatalogIssueCode.BOUNDARY_UNREADABLE, str(exc), entry.id)
            )
            continue

        if entry.bounds is None:
            continue
        try:
            actual = geometry_to_bounds(document.geometry)
        except (GeoJSONError, ValueError) as exc:
            issues.append(
                CatalogIssue(CatalogIssueCode.BOUNDARY_UNREADABLE, str(exc), entry.id)
            )
            continue
        if not _bounds_match(entry, actual):
            issues.append(
                CatalogIssue(
                    CatalogIssueCode.BOUNDS_MISMATCH,
                    f"index.json 里声明的范围 {_bounds_text(entry)} "
                    f"与几何实际范围 {_bounds_text_geo(actual)} 不一致；"
                    "建议直接删掉那条 bounds，程序会从几何自己算",
                    entry.id,
                )
            )


def _bounds_match(entry: RegionPreset, actual: object) -> bool:
    """声明值是否等于实际值（容差 :data:`_BOUNDS_TOLERANCE`）。"""
    assert entry.bounds is not None
    declared = entry.bounds
    return all(
        abs(getattr(declared, field) - getattr(actual, field)) <= _BOUNDS_TOLERANCE
        for field in ("west", "south", "east", "north")
    )


def _bounds_text(entry: RegionPreset) -> str:
    assert entry.bounds is not None
    b = entry.bounds
    return f"({b.west}, {b.south}, {b.east}, {b.north})"


def _bounds_text_geo(bounds: object) -> str:
    return (
        f"({bounds.west}, {bounds.south}, "
        f"{bounds.east}, {bounds.north})"
    )


# --------------------------------------------------------------------------- #
# 环检测
# --------------------------------------------------------------------------- #
def find_parent_cycles(catalog: RegionCatalog) -> dict[str, tuple[str, ...]]:
    """找出所有上级关系上的环（Spec §13）。

    Returns:
        ``{报告挂在哪个 id 上: (环上的 id 顺序, 回到起点)}``。每个环只报一次，
        挂在该环里 id 最小的那个区域上 —— 报几次没有意义，报少了才要命。

    自环（``parentId`` 指向自己）不在这里报：它由
    :data:`~app.regions.models.CatalogIssueCode.SELF_PARENT` 单独说，
    消息更清楚。
    """
    reported: dict[str, tuple[str, ...]] = {}
    seen_cycles: set[frozenset[str]] = set()

    for entry in catalog:
        if entry.parent_id is None or entry.parent_id == entry.id:
            continue
        path: list[str] = [entry.id]
        in_path: set[str] = {entry.id}
        current = entry
        while current.parent_id is not None:
            parent = catalog.get(current.parent_id)
            if parent is None or parent.id == current.id:
                break
            if parent.id in in_path:
                start = path.index(parent.id)
                cycle = path[start:]
                key = frozenset(cycle)
                if key not in seen_cycles:
                    seen_cycles.add(key)
                    reported[min(cycle)] = (*cycle, parent.id)
                break
            path.append(parent.id)
            in_path.add(parent.id)
            current = parent

    return reported
