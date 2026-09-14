"""地理计算层（Slice 1+）。

计划内容：
- ``lonlat_to_tile`` / ``tile_to_lonlat``：Web 墨卡托（EPSG:3857）瓦片编号换算。
- ``tile_range``：按 (west, south, east, north, zoom) 算出 x/y 编号范围。
- ``count_tiles``：按缩放级别统计瓦片总数。
- ``GeoBounds``：带校验的包围盒值对象。

本模块是纯函数 + 值对象，不依赖 UI、不发起网络请求，可被单元测试完全覆盖。
"""
