"""地理计算层。

Slice 1 已实现（Web 墨卡托瓦片坐标计算核心）：

- :class:`~app.geo.bounds.GeoBounds` —— 带校验的经纬度包围盒
- :class:`~app.geo.tile_math.TileRange` —— 单级缩放下的瓦片编号区间
- 经纬度 → XYZ 瓦片编号
- BoundingBox → TileRange / 多级缩放瓦片统计
- XYZ Y ↔ TMS Y 换算（MBTiles 前置）
- 下载容量预估与容量格式化

Slice 16 增加（GeoJSON Polygon 区域裁剪）：

- 瓦片编号 → 经纬度（:func:`~app.geo.tile_math.tile_bounds` 等）——
  Polygon 过滤需要"这张瓦片盖住哪块地"的答案
- :mod:`~app.geo.region` —— 统一区域模型（矩形 / 多边形）与行政区域预设
- :mod:`~app.geo.geojson_loader` —— GeoJSON → Shapely 几何（含校验）
- :mod:`~app.geo.tile_region_filter` —— prepared 几何 + ``intersects`` 判定
- :mod:`~app.geo.region_estimator` —— 候选 / 选中 / 过滤 三级统计

本层为纯函数 + 不可变值对象：不依赖 UI、不发起网络请求、不写磁盘
（行政区边界文件是**只读**资源）。可被单元测试完全覆盖。
UI 层做瓦片计算时必须调用本模块，禁止复制公式。
"""

from app.geo.bounds import GeoBounds
from app.geo.geojson_loader import (
    GeoJSONDocument,
    GeoJSONError,
    GeoJSONGeometryError,
    GeoJSONNotFoundError,
    GeoJSONParseError,
    geometry_to_bounds,
    load_geojson_document,
    load_geojson_geometry,
)
from app.geo.region import (
    CHINA_REGION_PRESET,
    REGION_PRESETS,
    DownloadRegion,
    RegionBoundaryMissing,
    RegionPreset,
    RegionType,
    get_region_preset,
    is_safe_region_relative_path,
    list_region_presets,
    normalize_region_relative_path,
    region_from_metadata,
    region_from_task,
    resolve_region_path,
)
from app.geo.region_estimator import (
    RegionTileEstimate,
    RegionTileEstimator,
    ZoomRegionStats,
    estimate_region_tiles,
)
from app.geo.tile_math import (
    TileRange,
    calculate_tile_range,
    calculate_tile_ranges,
    estimate_size_bytes,
    estimate_tile_count,
    format_size,
    lat_to_tile_y,
    lon_to_tile_x,
    tile_bounds,
    tile_x_to_lon,
    tile_y_to_lat,
    tms_y_to_xyz_y,
    validate_tile_axis,
    validate_zoom,
    xyz_y_to_tms_y,
)
from app.geo.tile_region_filter import (
    PolygonTileRegionFilter,
    ZoomTileFilterStats,
    build_region_filter,
)

__all__ = [
    # bounds
    "GeoBounds",
    # tile_math
    "TileRange",
    "lon_to_tile_x",
    "lat_to_tile_y",
    "tile_x_to_lon",
    "tile_y_to_lat",
    "tile_bounds",
    "calculate_tile_range",
    "calculate_tile_ranges",
    "estimate_tile_count",
    "validate_zoom",
    "validate_tile_axis",
    "xyz_y_to_tms_y",
    "tms_y_to_xyz_y",
    "estimate_size_bytes",
    "format_size",
    # region（Slice 16）
    "RegionType",
    "DownloadRegion",
    "RegionPreset",
    "RegionBoundaryMissing",
    "CHINA_REGION_PRESET",
    "REGION_PRESETS",
    "list_region_presets",
    "get_region_preset",
    "region_from_task",
    "region_from_metadata",
    # 资源路径规则（Slice 18）
    "normalize_region_relative_path",
    "is_safe_region_relative_path",
    "resolve_region_path",
    # geojson_loader（Slice 16）
    "GeoJSONError",
    "GeoJSONNotFoundError",
    "GeoJSONParseError",
    "GeoJSONGeometryError",
    "GeoJSONDocument",
    "load_geojson_geometry",
    "load_geojson_document",
    "geometry_to_bounds",
    # tile_region_filter（Slice 16）
    "PolygonTileRegionFilter",
    "ZoomTileFilterStats",
    "build_region_filter",
    # region_estimator（Slice 16）
    "ZoomRegionStats",
    "RegionTileEstimate",
    "RegionTileEstimator",
    "estimate_region_tiles",
]
