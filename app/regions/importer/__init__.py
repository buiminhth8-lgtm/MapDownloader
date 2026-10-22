"""行政区数据导入管线（Slice 26）。

合法授权的行政区数据（GeoJSON FeatureCollection）→ 导入 → 校验 → 标准化 →
层级构建 → 目录生成 → 原子发布 → RegionCatalog 直接使用。

模块划分
--------

=====================================  ==========================================
模块                                   职责
=====================================  ==========================================
:mod:`~app.regions.importer.models`    配置 / 源条目 / 问题 / 结果 / manifest
:mod:`~app.regions.importer.source_reader`   读 FeatureCollection、字段映射、初筛
:mod:`~app.regions.importer.normalizer`      几何校验与修复、id 清洗
:mod:`~app.regions.importer.hierarchy_builder`  COUNTRY→PROVINCE→CITY 层级校验
:mod:`~app.regions.importer.catalog_generator`  单区域 GeoJSON + index.json + manifest
:mod:`~app.regions.importer.region_exporter`   临时目录 → 全量校验 → 原子发布
:mod:`~app.regions.importer.import_service`    管线编排（validate / import_dataset）
=====================================  ==========================================

三条边界（Spec §29）
-------------------

1. **不抓数据**：本包只处理用户提供的文件，不在线抓取行政边界、不调用
   第三方地图接口下载边界；
2. **不算瓦片**：没有 Tile 枚举、不碰 ``RegionTileEstimator``、不读不写
   MBTiles（Spec §21：导入是预处理阶段）；
3. **不存 Zoom**：生成的目录里一个 Zoom 字段都没有，区间运行时查
   ``app.planning.DEFAULT_ZOOM_POLICIES``（Spec §22）。

用法::

    from app.regions.importer import RegionImportService, RegionImportConfig

    service = RegionImportService()
    config = RegionImportConfig(
        source_file=Path("regions.geojson"),
        id_field="code", name_field="name",
        level_field="level", parent_field="parent",
        output_root=service.output_root,
        boundary_version="2026-10",
    )
    result = service.validate(config)      # dry-run：PASS / WARNING / FAIL
    report = service.import_dataset(config)  # 通过才原子发布
"""

from app.regions.importer.catalog_generator import GeneratedDataset, generate_dataset
from app.regions.importer.hierarchy_builder import HierarchyEntry, build_hierarchy
from app.regions.importer.import_service import (
    RegionImportService,
    count_source_features,
)
from app.regions.importer.models import (
    DATASET_MANIFEST_FILENAME,
    DATASET_MANIFEST_FORMAT_VERSION,
    DEFAULT_LEVEL_ALIASES,
    ImportReport,
    ImportVerdict,
    IssueLevel,
    RegionDatasetManifest,
    RegionImportConfig,
    RegionImportError,
    RegionImportIssue,
    RegionImportIssueCode,
    RegionImportResult,
    RegionSourceFeature,
    map_level,
    sanitize_region_id,
)
from app.regions.importer.normalizer import (
    WGS84_LAT_RANGE,
    WGS84_LON_RANGE,
    normalize_features,
    normalize_geometry,
)
from app.regions.importer.region_exporter import (
    PublishError,
    discard_build,
    publish_dataset,
    validate_built_catalog,
)
from app.regions.importer.source_reader import peek_property_keys, read_source

__all__ = [
    # 服务
    "RegionImportService",
    "count_source_features",
    # 配置与模型
    "RegionImportConfig",
    "RegionSourceFeature",
    "RegionImportIssue",
    "RegionImportIssueCode",
    "IssueLevel",
    "RegionImportResult",
    "ImportVerdict",
    "RegionDatasetManifest",
    "ImportReport",
    "DATASET_MANIFEST_FILENAME",
    "DATASET_MANIFEST_FORMAT_VERSION",
    "DEFAULT_LEVEL_ALIASES",
    "map_level",
    "sanitize_region_id",
    # 各阶段（测试与排查会单独用）
    "read_source",
    "peek_property_keys",
    "normalize_features",
    "normalize_geometry",
    "WGS84_LON_RANGE",
    "WGS84_LAT_RANGE",
    "build_hierarchy",
    "HierarchyEntry",
    "generate_dataset",
    "GeneratedDataset",
    "publish_dataset",
    "discard_build",
    "validate_built_catalog",
    "PublishError",
    "RegionImportError",
]
