"""层级构建与校验（Slice 26，Spec §8 / §9）。

把一组标准化过的 Feature 变成一棵 **COUNTRY → PROVINCE → CITY** 的树，
并在挂错的那一刻拒绝整批导入。

硬规则（Spec §8）
----------------

- ``COUNTRY`` 的 parent 必须为空（有父级 = 层级错误）；
- ``PROVINCE`` 的 parent 必须是目录里的一个 ``COUNTRY``；
- ``CITY`` 的 parent 必须是目录里的一个 ``PROVINCE``。

检测并拒绝：parent 不存在、id 重复（normalizer 已做第一道）、环、
等级错误、孤儿市、**市直接挂国**。任一严重层级错误 → FAIL，
**不生成半正确 Catalog**（Spec §8 原话）。

直辖市兼容（Spec §9）
--------------------

不要求真实数据必须三层俱全：

- 源数据没有 CITY 层 → 省照常可用（记一条 warning，不是错误）；
- 直辖市 / 自治区 / 州 / 盟通过**等级映射**落到省级（默认表里已有，
  见 :data:`~app.regions.importer.models.DEFAULT_LEVEL_ALIASES`），
  其下属区域有数据就挂成市，没有就不造 —— **不伪造不存在的 City
  Feature**（Spec §9 原话）。

``country_id`` 覆写（Spec §3）
-----------------------------

源数据有全国级条目且配置给了 ``country_id`` 时，全国条目的 id 改成它
（数据里 code 是 ``100000``、用户想要 ``china`` 的场景），省级条目里
指向旧 id 的 parent 引用**一起改写** —— 否则会留下一堆断链，而断链的
报错信息（"parent 100000 不存在"）离根因（"你改了 country_id"）非常远。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.geo.bounds import GeoBounds
from app.planning.region_level import RegionLevel
from app.regions.importer.models import (
    RegionImportConfig,
    RegionImportIssue,
    RegionImportIssueCode,
    RegionSourceFeature,
    sanitize_region_id,
)
from app.regions.importer.normalizer import bounds_of

__all__ = ["HierarchyEntry", "build_hierarchy"]


@dataclass(frozen=True)
class HierarchyEntry:
    """层级树上的一条区域（build_hierarchy 的产出，catalog_generator 的输入）。

    Attributes:
        feature: 对应的源条目（几何、原始坐标都在它身上）。
        id: 最终的 region id（小写）。
        name: 展示名。
        level: 行政等级。
        parent_id: 父级 id；全国级为 ``None``。
        code: 原始行政区 code（``properties.code`` 用它）。
        original_code: 清洗前的 id 原值；没被清洗时为 ``None``。
        bounds: 来自几何的外包矩形（Spec §12：禁止人工填写）。
    """

    feature: RegionSourceFeature
    id: str
    name: str
    level: RegionLevel
    parent_id: str | None
    code: str
    original_code: str | None
    bounds: GeoBounds

    def geojson_properties(self, boundary_version: str) -> dict[str, Any]:
        """写进单个 GeoJSON 的 properties（Spec §11）。"""
        properties: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "level": self.level.value,
            "parentId": self.parent_id,
            "code": self.code,
            "boundaryVersion": boundary_version,
        }
        if self.original_code:
            properties["originalCode"] = self.original_code
        return properties


def _apply_country_override(
    features: list[RegionSourceFeature],
    config: RegionImportConfig,
    issues: list[RegionImportIssue],
) -> None:
    """``country_id`` 覆写：改全国条目 id，并改写指向旧 id 的 parent 引用。"""
    if not config.country_id:
        return
    wanted, _ = sanitize_region_id(config.country_id)
    if not wanted:
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.ID_SANITIZED,
                f"配置的 country_id {config.country_id!r} 清洗后为空，已忽略该覆写",
                "配置",
            )
        )
        return
    wanted = wanted.lower()

    countries = [item for item in features if item.level.is_country]
    if not countries:
        return  # 没有全国条目：由沿用逻辑 / NO_COUNTRY_ENTRY 负责，这里不置喙
    if len(countries) > 1:
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.MULTIPLE_COUNTRIES,
                f"源数据里有 {len(countries)} 个全国级条目"
                f"（{'、'.join(item.name for item in countries)}）："
                "层级只允许一个根",
                countries[1].name,
            )
        )
        return
    country = countries[0]
    if country.region_id == wanted:
        return
    old_id = country.region_id
    country.region_id = wanted
    # parent 引用改写： provincial 的 parent 写着旧 id 的，换成新 id。
    for item in features:
        if item.parent_ref and item.parent_ref.lower() == old_id:
            item.parent_ref = wanted


def _check_parent(
    feature: RegionSourceFeature,
    by_id: Mapping[str, RegionSourceFeature],
    issues: list[RegionImportIssue],
) -> str | None:
    """校验一条 Feature 的父级引用，返回解析后的父 id（错误时 ``None``）。

    所有分支都记 issue（Spec §8：层级错误必须被看见，不是被跳过）。
    """
    label = feature.name or feature.region_id
    parent_ref = feature.parent_ref.strip()
    level = feature.level
    assert level is not None  # reader 保证

    if level.is_country:
        if parent_ref:
            issues.append(
                RegionImportIssue(
                    RegionImportIssueCode.COUNTRY_HAS_PARENT,
                    f"全国级条目不该有父级（当前父级引用 {parent_ref!r}）",
                    label,
                )
            )
            return None
        return None

    if not parent_ref:
        if level.is_city:
            hint = "市级条目必须挂在一个省下面"
        elif level.is_county:
            hint = "县级条目必须挂在一个市或省下面"
        else:
            hint = "省级条目必须挂在全国下面"
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.PARENT_MISSING,
                f"{level.display_name}条目没有父级引用（{hint}）",
                label,
            )
        )
        return None

    parent_key = parent_ref.lower()
    if parent_key == feature.region_id.lower():
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.SELF_PARENT,
                "父级引用指向它自己",
                label,
            )
        )
        return None

    parent = by_id.get(parent_key)
    if parent is None:
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.PARENT_MISSING,
                f"父级 {parent_ref!r} 不在源数据里"
                "（检查父级字段是否指对了列、引用的 code 是否真的在文件里）",
                label,
            )
        )
        return None

    parent_level = parent.level
    assert parent_level is not None
    if level.is_province and not parent_level.is_country:
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.PARENT_LEVEL_INVALID,
                f"省级条目的父级必须是全国级，实际是{parent_level.display_name}"
                f"（{parent.name}）",
                label,
            )
        )
        return None
    if level.is_city and parent_level.is_country:
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.CITY_UNDER_COUNTRY,
                "市级条目直接挂在了全国下面：中间必须有一级省级"
                "（直辖市的下属区域也不例外 —— 它们挂在那个直辖市上）",
                label,
            )
        )
        return None
    if level.is_city and not parent_level.is_province:
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.PARENT_LEVEL_INVALID,
                f"市级条目的父级必须是省级，实际是{parent_level.display_name}"
                f"（{parent.name}）",
                label,
            )
        )
        return None
    if level.is_county:
        # 县级条目的父级是市或省（省直辖县 / 直辖市的下辖区）。
        # 直接挂全国 / 挂别的等级都是层级错误。
        if parent_level.is_country:
            issues.append(
                RegionImportIssue(
                    RegionImportIssueCode.PARENT_LEVEL_INVALID,
                    "县级条目直接挂在了全国下面：中间必须有市级或省级"
                    "（直辖市的下辖区挂在那个直辖市上）",
                    label,
                )
            )
            return None
        if not (parent_level.is_city or parent_level.is_province):
            issues.append(
                RegionImportIssue(
                    RegionImportIssueCode.PARENT_LEVEL_INVALID,
                    f"县级条目的父级必须是市级或省级，实际是{parent_level.display_name}"
                    f"（{parent.name}）",
                    label,
                )
            )
            return None
    return parent_key


def _check_cycles(
    entries: Mapping[str, RegionSourceFeature],
    issues: list[RegionImportIssue],
) -> None:
    """父链环检测（Spec §8 的 "cycle"）。

    有等级规则在，环其实长不出来（等级严格递减）；这道检查是**兜底** ——
    万一将来等级规则放宽（例如允许"省辖市"跨级挂），环不会静默变成
    一个查询时的死循环。
    """
    for region_id in entries:
        seen: set[str] = set()
        current: str | None = region_id
        while current is not None:
            if current in seen:
                issues.append(
                    RegionImportIssue(
                        RegionImportIssueCode.PARENT_CYCLE,
                        f"父级引用形成了环（{' → '.join(sorted(seen))} → {current}）",
                        region_id,
                    )
                )
                break
            seen.add(current)
            feature = entries.get(current)
            if feature is None:
                break
            parent_ref = feature.parent_ref.strip().lower()
            current = parent_ref or None


def build_hierarchy(
    features: tuple[RegionSourceFeature, ...] | list[RegionSourceFeature],
    config: RegionImportConfig,
    *,
    issues: list[RegionImportIssue],
) -> tuple[HierarchyEntry, ...]:
    """构建并校验 COUNTRY → PROVINCE → CITY 层级（Spec §8 / §9）。

    Args:
        features: normalize 之后的条目（id 已清洗、几何已校验）。
        config: 导入配置（``country_id`` 覆写在里生效）。
        issues: 问题收集列表（本函数只追加，不清空）。

    Returns:
        通过校验的层级条目（顺序与源数据一致）。**有任何 ERROR 级问题时，
        调用方必须 FAIL** —— 返回值本身不表达"能不能用"。
    """
    items = list(features)
    _apply_country_override(items, config, issues)

    by_id: dict[str, RegionSourceFeature] = {item.region_id.lower(): item for item in items}
    _check_cycles(by_id, issues)

    entries: list[HierarchyEntry] = []
    for feature in items:
        parent_id = _check_parent(feature, by_id, issues)
        if parent_id is None and not feature.level.is_country:
            continue  # 父级不成立：这条进不了目录（原因已记）
        if feature.level.is_country and feature.parent_ref.strip():
            continue  # 全国级带父级：同上
        level = feature.level
        assert level is not None
        entries.append(
            HierarchyEntry(
                feature=feature,
                id=feature.region_id,
                name=feature.name,
                level=level,
                parent_id=parent_id,
                code=feature.code,
                original_code=feature.code if feature.id_sanitized else None,
                bounds=bounds_of(feature.geometry),
            )
        )

    # Spec §9：没有市级层不是错误（省照常可用），但要说一声 ——
    # 用户可能以为自己导全了，结果界面上市下拉是空的。
    has_city = any(item.level.is_city for item in items)
    has_province = any(item.level.is_province for item in items)
    if has_province and not has_city:
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.NO_CITY_LAYER,
                "源数据没有市级条目：省级区域可正常使用，"
                "但界面上的「市」下拉会是空的（直辖市若无下属区域数据亦然）",
                "源数据",
                level=_warning(),
            )
        )
    if not has_province and has_city:
        # 市级条目的父级必然缺失（上面已逐条报错）；这里补一句总结，
        # 免得用户在几十条 PARENT_MISSING 里找不到根因。
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.NO_COUNTRY_ENTRY,
                "源数据只有市级条目、没有省级条目：市级条目无处可挂",
                "源数据",
            )
        )
    if not any(item.level.is_country for item in items):
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.NO_COUNTRY_ENTRY,
                "源数据没有全国级条目：省级条目的父级无处可挂"
                "（可提供包含全国边界的数据，或用 country_id 沿用输出目录里"
                "已发布的全国条目）",
                "源数据",
            )
        )

    return tuple(entries)


def _warning():
    """延迟取 WARNING 级（与 normalizer 同一理由：避免模块级循环）。"""
    from app.regions.importer.models import IssueLevel

    return IssueLevel.WARNING
