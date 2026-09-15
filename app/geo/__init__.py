"""地理计算层。

Slice 1 已实现（Web 墨卡托瓦片坐标计算核心）：

- :class:`~app.geo.bounds.GeoBounds` —— 带校验的经纬度包围盒
- :class:`~app.geo.tile_math.TileRange` —— 单级缩放下的瓦片编号区间
- 经纬度 → XYZ 瓦片编号
- BoundingBox → TileRange / 多级缩放瓦片统计
- XYZ Y ↔ TMS Y 换算（MBTiles 前置）
- 下载容量预估与容量格式化

本层为纯函数 + 不可变值对象：不依赖 UI、不发起网络请求、不读写磁盘，
可被单元测试完全覆盖。UI 层做瓦片计算时必须调用本模块，禁止复制公式。
"""

from app.geo.bounds import GeoBounds
from app.geo.tile_math import (
    TileRange,
    calculate_tile_range,
    calculate_tile_ranges,
    estimate_size_bytes,
    estimate_tile_count,
    format_size,
    lat_to_tile_y,
    lon_to_tile_x,
    tms_y_to_xyz_y,
    validate_tile_axis,
    validate_zoom,
    xyz_y_to_tms_y,
)

__all__ = [
    # bounds
    "GeoBounds",
    # tile_math
    "TileRange",
    "lon_to_tile_x",
    "lat_to_tile_y",
    "calculate_tile_range",
    "calculate_tile_ranges",
    "estimate_tile_count",
    "validate_zoom",
    "validate_tile_axis",
    "xyz_y_to_tms_y",
    "tms_y_to_xyz_y",
    "estimate_size_bytes",
    "format_size",
]
