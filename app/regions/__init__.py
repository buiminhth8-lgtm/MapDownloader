"""行政区域层级层（Slice 18）：中国 → 省/直辖市 → 市。

这一层回答的是"**有哪些区域、谁属于谁、它们的边界文件在哪**"，
并把条目变成 Slice 16 已有的 :class:`~app.geo.DownloadRegion` 交给下载链路。

模块划分
--------

=================================  ================================================
模块                               职责
=================================  ================================================
:mod:`~app.regions.models`         :class:`RegionPreset`（目录条目）、问题与校验结果模型
:mod:`~app.regions.catalog`        :class:`RegionCatalog` —— 读 ``index.json``、层级查询
:mod:`~app.regions.loader`         :class:`RegionLoader` —— 路径安全、GeoJSON、DownloadRegion
:mod:`~app.regions.validator`      :func:`validate_catalog` —— 快速 / 深度两档校验
:mod:`~app.regions.service`        :class:`RegionService` —— 业务入口（层级查询 + Zoom 建议）
=================================  ================================================

用法::

    from app.regions import RegionService

    service = RegionService()
    service.list_countries()                 # 全部全国级区域
    service.list_provinces("china")          # 中国下面有哪些省
    service.list_cities("zhejiang")          # 浙江省下面有哪些市
    service.get_zoom_recommendation("china")            # 推荐 Z4~10
    service.get_download_region("china")                # → app.geo.DownloadRegion

三条硬约束
----------

1. **不持有几何**（Spec §2）：目录只有元数据，几何在
   :class:`~app.regions.loader.RegionLoader` 里按需读（Spec §5 的 lazy load）。
   全国省市数据加起来几百 MB，启动时全读一遍会让程序几十秒打不开。
2. **不存 Zoom 数值**（Spec §9）：只存
   :class:`~app.planning.RegionLevel`，区间由
   :data:`~app.planning.DEFAULT_ZOOM_POLICIES` 那张唯一权威表决定。
3. **不伪造边界**（Spec §8 / §19）：仓库只带元数据（``index.json``），
   不带任何真实的行政边界 GeoJSON —— 法定地图内容必须用带审图号的授权数据。
   测试用的三层结构是 ``tests/fixtures/regions/catalog/`` 下几个自建的多边形。

依赖方向
--------

``app.regions`` → ``app.geo`` / ``app.planning``，**单向**。``app.geo.region``
（Slice 16 的扁平预设）只共享 :func:`~app.geo.region.resolve_region_path`
等路径工具，不 import 本包，因此不存在循环导入。
"""

from app.regions.catalog import (
    INDEX_FILENAME,
    SUPPORTED_INDEX_VERSIONS,
    RegionCatalog,
    region_resource_root,
)
from app.regions.loader import RegionLoader
from app.regions.models import (
    ADMINISTRATIVE_LEVELS,
    PARENT_LEVEL_RULES,
    CatalogIssue,
    CatalogIssueCode,
    CatalogValidationResult,
    IssueSeverity,
    RegionCatalogError,
    RegionPreset,
    is_safe_region_relative_path,
    normalize_region_relative_path,
    resolve_region_path,
)
from app.regions.service import RegionService
from app.regions.validator import find_parent_cycles, validate_catalog

__all__ = [
    # 模型
    "RegionPreset",
    "RegionCatalogError",
    "CatalogIssue",
    "CatalogIssueCode",
    "CatalogValidationResult",
    "IssueSeverity",
    "ADMINISTRATIVE_LEVELS",
    "PARENT_LEVEL_RULES",
    # 路径规则
    "normalize_region_relative_path",
    "is_safe_region_relative_path",
    "resolve_region_path",
    # 目录
    "RegionCatalog",
    "INDEX_FILENAME",
    "SUPPORTED_INDEX_VERSIONS",
    "region_resource_root",
    # 加载器
    "RegionLoader",
    # 校验
    "validate_catalog",
    "find_parent_cycles",
    # 业务入口
    "RegionService",
]
