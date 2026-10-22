"""源数据读取（Slice 26，Spec §2 / §4）。

把用户给的 GeoJSON 文件读成一组 :class:`~app.regions.importer.models.RegionSourceFeature`。

这一层只回答"**文件里有什么、每条成不成立**"，不做任何几何修复、
不清洗 id、不建层级 —— 那些是 normalizer 与 hierarchy_builder 的事，
混在一起会让"读错"和"修错"分不清。

字段名不可假设（Spec §2）
------------------------

外部数据的 properties 键叫什么完全取决于导出它的那套系统：
``code`` / ``gbcode`` / ``adcode`` / ``ID`` 都见过。所以四个字段名
（id / name / level / parent）全部来自 :class:`RegionImportConfig`，
本模块**没有任何默认键名**。配置写错时的报错会带上源数据里真实存在的
键名 —— "字段 ``adcode`` 不在源数据里；可用的是：code、name、level、
parent" 比 "读取失败" 有用一个数量级。

拒绝与静默（Spec §4）
--------------------

Point / LineString / 空几何 / 缺必要字段的 Feature **一律拒绝**，但每一条
拒绝都记一条 :class:`RegionImportIssue`（ERROR 级）—— "不要静默忽略大量
错误数据"。拒绝的 Feature 不进后续管线；只要有一条 ERROR，整个导入就是
FAIL（见 models 的 verdict），用户必须在结果里看见"第几条、为什么"。

顶层结构
--------

首版支持 **FeatureCollection**（Spec §2"首版至少支持"）。单个 ``Feature``
也接受（GeoJSON 规范里的合法文档，且"只导入一个区域"是合理的小场景）；
其余顶层类型（裸几何、``GeometryCollection`` 文档）拒绝 —— 没有
properties 就谈不上字段映射，放进来的每一秒都会在更远的地方报一个
更难懂的错。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.regions.importer.models import (
    IssueLevel,
    RegionImportConfig,
    RegionImportError,
    RegionImportIssue,
    RegionImportIssueCode,
    RegionSourceFeature,
    map_level,
)

__all__ = ["read_source", "peek_property_keys"]

#: 本层认得的顶层文档类型。
_SUPPORTED_TOP_TYPES = frozenset({"FeatureCollection", "Feature"})

#: 接受的几何类型（Spec §4：支持 Polygon / MultiPolygon）。
_SUPPORTED_GEOMETRY_TYPES = frozenset({"Polygon", "MultiPolygon"})

#: 探测字段名时最多合并前多少条 Feature 的键（大数据文件不必全扫：
#: properties 的键在同一份数据里是稳定的）。
_KEY_PROBE_FEATURES = 50


def _load_json(path: Path) -> Any:
    """读 UTF-8 JSON 文件。

    Raises:
        RegionImportError: 文件不存在 / 读不出 / 不是 UTF-8 / 不是合法 JSON。
            （**不降级**：带 BOM 或别的编码会直接报错，不做猜测转码 ——
            与 ``app.geo.geojson_loader`` 同一条规矩。）
    """
    if not path.is_file():
        raise RegionImportError(f"源数据文件不存在：{path}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RegionImportError(
            f"源数据文件不是 UTF-8：{path}（{exc}）。请先用工具转成 UTF-8 再导入"
        ) from exc
    except OSError as exc:
        raise RegionImportError(f"源数据文件读不出来：{path}（{exc}）") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RegionImportError(
            f"源数据文件不是合法 JSON：{path}（第 {exc.lineno} 行：{exc.msg}）"
        ) from exc


def _extract_features(document: Any, path: Path) -> list[Mapping[str, Any]]:
    """把顶层文档拆成 Feature 列表。

    Raises:
        RegionImportError: 顶层类型不支持，或 ``features`` 不是数组。
    """
    if not isinstance(document, Mapping):
        raise RegionImportError(
            f"源数据顶层必须是一个 JSON 对象，实际是 {type(document).__name__}"
        )
    top_type = str(document.get("type") or "")
    if top_type not in _SUPPORTED_TOP_TYPES:
        raise RegionImportError(
            f"源数据顶层类型是 {top_type!r}，本导入器只支持 "
            + "、".join(sorted(_SUPPORTED_TOP_TYPES))
            + "（面几何的 properties 才能做字段映射）"
        )
    if top_type == "Feature":
        return [document]
    features = document.get("features")
    if features is None:
        raise RegionImportError(f"源数据的 FeatureCollection 缺少 features 数组：{path}")
    if not isinstance(features, list):
        raise RegionImportError(
            f"源数据的 features 必须是数组，实际是 {type(features).__name__}"
        )
    return features


def peek_property_keys(path: str | Path, *, limit: int = _KEY_PROBE_FEATURES) -> tuple[str, ...]:
    """探测源数据里出现过的 properties 键（界面填字段映射用）。

    只读前 ``limit`` 条 Feature 的键合并起来 —— 同一份数据里键是稳定的，
    为一个下拉框扫完几千条纯属浪费。文件读不出来时返回空元组（调用方
    自行决定怎么提示；探测是尽力而为的服务，不是硬校验）。

    Returns:
        出现过的键名（保持首次出现的顺序）。
    """
    try:
        document = _load_json(Path(path))
        features = _extract_features(document, Path(path))
    except RegionImportError:
        return ()
    keys: list[str] = []
    seen: set[str] = set()
    for feature in features[:limit]:
        if not isinstance(feature, Mapping):
            continue
        properties = feature.get("properties")
        if not isinstance(properties, Mapping):
            continue
        for key in properties:
            name = str(key)
            if name and name not in seen:
                seen.add(name)
                keys.append(name)
    return tuple(keys)


def _check_field_present(
    field_name: str,
    config_value: str,
    probe_keys: tuple[str, ...],
    path: Path,
    issues: list[RegionImportIssue],
    *,
    required: bool = True,
) -> bool:
    """配置的字段名在源数据里到底存不存在（Spec §2：不许假设字段名）。

    探测不到键（空文件 / 前 50 条恰好都没 properties）时**放过** ——
    宁可让后面的 MISSING_FIELD 按条报，也不要在这里误判。

    ``required=False`` 用于父级字段：源数据**没有父级列**是合法形态
    （只有全国级条目、或层级靠别的机制表达），此时所有条目都会当成
    "没有父级"—— 那对全国条目无损，对省市条目会在层级校验里现形
    （PARENT_MISSING），所以这里只记一条 warning 把根因往前挪一步，
    不单独判 FAIL。
    """
    if not probe_keys:
        return True
    if config_value in probe_keys:
        return True
    issues.append(
        RegionImportIssue(
            RegionImportIssueCode.FIELD_NOT_IN_SOURCE,
            f"配置的{field_name}字段 {config_value!r} 在源数据里不存在；"
            f"可用的是：{'、'.join(probe_keys[:20])}"
            + ("…" if len(probe_keys) > 20 else "")
            + ("" if required else "（所有条目将按“没有父级”处理）"),
            level=IssueLevel.ERROR if required else IssueLevel.WARNING,
        )
    )
    return False


def _read_feature(
    feature: Any,
    index: int,
    config: RegionImportConfig,
    issues: list[RegionImportIssue],
) -> RegionSourceFeature | None:
    """读一条 Feature；不成立返回 ``None``（原因已记进 issues）。"""
    label = f"第 {index + 1} 条"
    if not isinstance(feature, Mapping):
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.MISSING_FIELD,
                f"{label} 不是一个 JSON 对象，已跳过",
                label,
            )
        )
        return None

    properties = feature.get("properties")
    if properties is None:
        properties = {}
    if not isinstance(properties, Mapping):
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.MISSING_FIELD,
                f"{label} 的 properties 不是对象，已跳过",
                label,
            )
        )
        return None

    def _value(field_name: str) -> str:
        raw = properties.get(field_name)
        return str(raw if raw is not None else "").strip()

    raw_id = _value(config.id_field)
    raw_name = _value(config.name_field)
    raw_level = _value(config.level_field)
    raw_parent = _value(config.parent_field) if config.parent_field else ""

    # 必要字段：id / name / level（parent 允许缺 —— 全国级条目没有父级）。
    missing = [
        name
        for name, value in (
            (config.id_field, raw_id),
            (config.name_field, raw_name),
            (config.level_field, raw_level),
        )
        if not value
    ]
    if missing:
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.MISSING_FIELD,
                f"{label} 缺少必要字段：{'、'.join(missing)}，已跳过",
                label,
            )
        )
        return None

    # 等级必须可映射（Spec §3）。认不出来 → 拒绝，不猜。
    level = map_level(raw_level, config.level_aliases)
    if level is None:
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.UNKNOWN_LEVEL,
                f"{label} 的等级 {raw_level!r} 不在映射表里"
                "（可选：country/province/city、1/2/3、全国/省/市等；"
                "特殊情况可在导入配置的等级映射里追加）",
                raw_id or label,
            )
        )
        return None

    geometry_node = feature.get("geometry")
    if geometry_node is None:
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.EMPTY_GEOMETRY,
                f"{label}（{raw_name}）没有 geometry，已跳过",
                raw_id or label,
            )
        )
        return None
    if not isinstance(geometry_node, Mapping):
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.UNSUPPORTED_GEOMETRY,
                f"{label}（{raw_name}）的 geometry 不是对象，已跳过",
                raw_id or label,
            )
        )
        return None
    geometry_type = str(geometry_node.get("type") or "")
    if geometry_type not in _SUPPORTED_GEOMETRY_TYPES:
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.UNSUPPORTED_GEOMETRY,
                f"{label}（{raw_name}）的几何类型是 {geometry_type!r}，"
                "只支持 Polygon / MultiPolygon"
                "（点与线无法回答「哪些瓦片落在区域内」）",
                raw_id or label,
            )
        )
        return None

    # 几何解析交给 shapely。坐标错得离谱（嵌套层级不对、数字写成字符串…
    # 这里就现形，报错带序号。
    try:
        from shapely.geometry import shape

        geometry = shape(dict(geometry_node))
    except Exception as exc:  # noqa: BLE001 - shapely 的异常类型不稳定
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.UNPARSEABLE_GEOMETRY,
                f"{label}（{raw_name}）的几何无法解析：{exc}",
                raw_id or label,
            )
        )
        return None

    if geometry.is_empty:
        issues.append(
            RegionImportIssue(
                RegionImportIssueCode.EMPTY_GEOMETRY,
                f"{label}（{raw_name}）的几何是空的（空环 / 空坐标数组），已跳过",
                raw_id or label,
            )
        )
        return None

    return RegionSourceFeature(
        source_index=index,
        raw_properties=dict(properties),
        raw_geometry=dict(geometry_node),
        geometry=geometry,
        region_id=raw_id,
        level=level,
        parent_ref=raw_parent,
        name=raw_name,
        code=raw_id,
    )


def read_source(
    config: RegionImportConfig,
) -> tuple[tuple[RegionSourceFeature, ...], list[RegionImportIssue]]:
    """读源数据（Spec §4）。

    Args:
        config: 导入配置（字段名、等级映射都在里面）。

    Returns:
        ``(features, issues)`` —— 通过初筛的 Feature（顺序与源文件一致），
        以及全部问题（含被拒 Feature 的每一条理由）。

    Raises:
        RegionImportError: 文件级别的问题（不存在 / 不是 UTF-8 / 不是 JSON /
            顶层结构不支持）。那是"这次导入跑不完"，不是"数据不合格"。
    """
    issues: list[RegionImportIssue] = []
    path = Path(config.source_file)
    document = _load_json(path)
    features = _extract_features(document, path)

    if not features:
        raise RegionImportError(f"源数据里没有任何 Feature：{path}")

    # 字段名体检（Spec §2）：配置与数据对不上时尽早说，并告诉用户有哪些键。
    probe_keys = peek_property_keys(path)
    _check_field_present("ID", config.id_field, probe_keys, path, issues)
    _check_field_present("名称", config.name_field, probe_keys, path, issues)
    _check_field_present("级别", config.level_field, probe_keys, path, issues)
    if config.parent_field:
        _check_field_present(
            "父级", config.parent_field, probe_keys, path, issues, required=False
        )

    accepted: list[RegionSourceFeature] = []
    for index, feature in enumerate(features):
        entry = _read_feature(feature, index, config, issues)
        if entry is not None:
            accepted.append(entry)
    return tuple(accepted), issues
