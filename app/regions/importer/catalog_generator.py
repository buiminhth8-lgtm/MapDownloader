"""目录生成（Slice 26，Spec §10 / §11 / §12 / §14）。

把层级条目写成一个**可直接被 Slice 18 目录层加载**的数据集：

.. text::

    <build_dir>/
    ├── country/<id>.geojson     ← 单区域 Feature（properties 带全套元数据）
    ├── province/<id>.geojson
    ├── city/<id>.geojson
    ├── index.json               ← RegionCatalog 唯一认得的目录元数据
    └── manifest.json            ← RegionDatasetManifest（版本与来源身份证）

与 Slice 18 的兼容是**结构性**的，不是"顺手兼容"：生成的 index.json 用的
就是 ``app.regions.catalog`` 认得的同一套键（``id`` / ``name`` / ``level`` /
``parentId`` / ``geojson`` / ``boundaryVersion`` / ``code`` / ``bounds``），
因此 ``RegionCatalog.load(root=<build_dir>)`` 不需要任何特殊分支 ——
Spec §16"RegionCatalog 无需知道数据是手工还是 Importer 生成"落在这里。

三条硬规则
----------

1. **bounds 一律来自几何**（Spec §12"禁止人工填写"）：写的是
   ``geometry.bounds`` 的精确值。不做四舍五入 —— 目录层的深度校验容差是
   ``1e-9``，砍一刀精度就会让"发布的数据集"在自己的校验里报范围不符。
2. **几何原样保留**（Spec §6）：几何没被修复 / 简化时，写回的是源数据里的
   **原始坐标字典**（见 :meth:`RegionSourceFeature.output_geometry`），
   不经 JSON round-trip —— 那会动浮点精度。
3. **一个 Zoom 字段都不写**（Spec §22 / §29）：区间只存在于
   ``app.planning.DEFAULT_ZOOM_POLICIES``。导入器写等级，运行时查策略。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.planning.region_level import ADMINISTRATIVE_LEVELS
from app.regions.catalog import INDEX_FILENAME
from app.regions.importer.hierarchy_builder import HierarchyEntry
from app.regions.importer.models import (
    DATASET_MANIFEST_FILENAME,
    RegionDatasetManifest,
    RegionImportConfig,
)

__all__ = ["GeneratedDataset", "generate_dataset"]


@dataclass(frozen=True)
class GeneratedDataset:
    """一次生成的产物（写进 build 目录的内容清单）。"""

    root: Path
    entries: tuple[HierarchyEntry, ...]
    index_path: Path
    manifest_path: Path
    manifest: RegionDatasetManifest
    geojson_paths: tuple[Path, ...]

    @property
    def geojson_count(self) -> int:
        """边界文件数（不含 index.json / manifest.json）。"""
        return len(self.geojson_paths)


def _write_text(path: Path, text: str) -> None:
    """原子写一个 UTF-8 文本文件（tmp → replace）。

    发布本身是原子的（见 region_exporter），但**构建目录内部**也该原子：
    一个写了一半的 index.json 会在"发布前校验"里现形，而那时的报错
    （"目录文件不是合法 JSON：第 3 行"）离真正的原因（上次构建被 kill）
    非常远。同一文件系统内 replace 是原子的，代价可以忽略。
    """
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
    tmp.replace(path)


def _write_geojson(path: Path, entry: HierarchyEntry, boundary_version: str) -> None:
    """写单个区域的 GeoJSON（Spec §11）。

    顶层是 **Feature**（不是 FeatureCollection）：一个文件一个区域，
    ``RegionLoader.load_document`` 两种都认，但"一个省一个文件"时
    Feature 更直白，也让"这个文件就是这块地"在文件系统层面可读。
    """
    geometry = entry.feature.output_geometry()
    if geometry is None:  # pragma: no cover - reader 已保证几何非空
        raise ValueError(f"{entry.id} 没有可写出的几何")
    feature: dict[str, Any] = {
        "type": "Feature",
        "properties": entry.geojson_properties(boundary_version),
        "geometry": geometry,
    }
    _write_text(path, json.dumps(feature, ensure_ascii=False, indent=2) + "\n")


def _write_index(path: Path, entries: tuple[HierarchyEntry, ...], config: RegionImportConfig) -> None:
    """写 index.json（Spec §12）。

    顶层多一个 ``boundaryVersion``：整批数据的版本号。它是**提示性**的
    （每条记录自己也有），写在这里是为了"这批数据是哪一版"能一次看完 ——
    Slice 18 的目录解析器忽略不认识的顶层键，老版本程序读它不受影响。
    """
    regions: list[dict[str, Any]] = []
    for entry in entries:
        record: dict[str, Any] = {
            "id": entry.id,
            "name": entry.name,
            "level": entry.level.value,
            "parentId": entry.parent_id,
            "geojson": f"{entry.level.value}/{entry.id}.geojson",
            "boundaryVersion": config.boundary_version,
        }
        if entry.code:
            record["code"] = entry.code
        record["bounds"] = {
            "west": entry.bounds.west,
            "south": entry.bounds.south,
            "east": entry.bounds.east,
            "north": entry.bounds.north,
        }
        regions.append(record)
    payload = {
        "version": 1,
        "boundaryVersion": config.boundary_version,
        "regions": regions,
    }
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _write_manifest(
    path: Path,
    entries: tuple[HierarchyEntry, ...],
    config: RegionImportConfig,
) -> RegionDatasetManifest:
    """写 dataset manifest（Spec §14）。"""
    # 只数四个行政等级（ADMINISTRATIVE_LEVELS 就是 COUNTRY/PROVINCE/CITY/COUNTY，
    # 从 PARENT_LEVEL_RULES 推导 —— 不在这里手写等级成员名）。
    counts = dict.fromkeys(ADMINISTRATIVE_LEVELS, 0)
    for entry in entries:
        if entry.level in counts:
            counts[entry.level] += 1
    # manifest 的四个计数字段是持久化契约（countryCount/provinceCount/
    # cityCount/countyCount），按 ADMINISTRATIVE_LEVELS 的固定顺序取值 ——
    # 顺序来自 PARENT_LEVEL_RULES 的插入序（COUNTRY → PROVINCE → CITY →
    # COUNTY），加断言钉死。
    assert [level.value for level in ADMINISTRATIVE_LEVELS] == [
        "country", "province", "city", "county"
    ], "行政等级顺序变了，manifest 计数字段会错位"
    country_count, province_count, city_count, county_count = (
        counts.get(level, 0) for level in ADMINISTRATIVE_LEVELS
    )
    manifest = RegionDatasetManifest(
        boundary_version=config.boundary_version,
        source_name=config.source_name,
        created_at=datetime.now().isoformat(timespec="seconds"),
        region_count=len(entries),
        country_count=country_count,
        province_count=province_count,
        city_count=city_count,
        county_count=county_count,
        source_license=config.source_license,
        source_description=config.source_description,
    )
    _write_text(path, json.dumps(manifest.as_dict(), ensure_ascii=False, indent=2) + "\n")
    return manifest


def generate_dataset(
    entries: tuple[HierarchyEntry, ...],
    config: RegionImportConfig,
    build_dir: Path,
) -> GeneratedDataset:
    """在 ``build_dir`` 里生成整套数据集（Spec §10 ~ §14）。

    这一步**只写 build 目录**，不碰发布目录 —— 发布（原子替换）由
    :mod:`app.regions.importer.region_exporter` 负责，"边生成边覆盖生产
    Catalog"在这里没有入口（Spec §13）。

    Args:
        entries: 通过层级校验的条目。
        config: 导入配置（版本号、字段映射的产物写进每个文件）。
        build_dir: 构建目录（调用方负责创建；本函数只往里写）。

    Returns:
        :class:`GeneratedDataset`（产物清单 + manifest）。
    """
    build_dir = Path(build_dir)
    geojson_paths: list[Path] = []
    for entry in entries:
        directory = build_dir / entry.level.value
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{entry.id}.geojson"
        _write_geojson(target, entry, config.boundary_version)
        geojson_paths.append(target)

    index_path = build_dir / INDEX_FILENAME
    _write_index(index_path, entries, config)

    manifest_path = build_dir / DATASET_MANIFEST_FILENAME
    manifest = _write_manifest(manifest_path, entries, config)

    return GeneratedDataset(
        root=build_dir,
        entries=entries,
        index_path=index_path,
        manifest_path=manifest_path,
        manifest=manifest,
        geojson_paths=tuple(geojson_paths),
    )
