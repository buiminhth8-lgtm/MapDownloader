# 区域边界测试夹具（自建合成数据）

这里的 `.geojson` **全部是手写的合成图形**（方框、蝴蝶结、越界线……），
尺寸只有几度的量级，唯一的用途是覆盖 `app.geo.geojson_loader` 的解析与校验分支。

**它们不是任何真实行政区边界**，也**不构成** `resources/regions/china.geojson` 的替代品。
产品运行时用的 `china.geojson` 必须由用户自行提供合法授权数据 —— 详见仓库根
`README.md` 的「行政区域下载」一节。

| 文件 | 顶层形态 | 用途 |
| --- | --- | --- |
| `square.geojson` | `Feature` + `Polygon` | 最常规的一条记录：四个角点 + 属性齐全 |
| `bare_polygon.geojson` | `Polygon` | 裸几何，没有 properties 可收集 |
| `feature_collection.geojson` | `FeatureCollection`（2 个不相邻的方框） | 需要 `unary_union` 合并成一个 MultiPolygon |
| `multipolygon.geojson` | `MultiPolygon`（2 个不相邻的方框） | 与上一条同构，但走裸几何分支 |
| `geometry_collection.geojson` | `GeometryCollection`（Point + Polygon） | 点必须被丢掉，只保留面 |
| `point_only.geojson` | `Feature` + `Point` | 不含面 → 必须报错 |
| `line_string.geojson` | `Feature` + `LineString` | 顶层受支持，但几何不含面 → 必须报错 |
| `missing_geometry.geojson` | `Feature`，`geometry: null` | 几何缺失 → 必须报错 |
| `empty_features.geojson` | `FeatureCollection`，`features: []` | 没有任何几何 → 必须报错 |
| `broken_json.geojson` | —— | 非法 JSON → `GeoJSONParseError` |
| `no_type.geojson` | JSON 对象但没有 `type` | → `GeoJSONParseError` |
| `unsupported_type.geojson` | `type: "Topology"` | 顶层类型不支持 → `GeoJSONParseError` |
| `bowtie.geojson` | 自相交的 `Polygon` | 非法几何 → `GeoJSONGeometryError`（默认不静默修复） |
| `across_antimeridian.geojson` | 经度 170 → 190 | 包络落不进 `GeoBounds` → `GeoJSONGeometryError` |
| `polar.geojson` | 纬度到 ±89 | 纬度必须被 clamp 到墨卡托极限 |

改动这些夹具时请一并跑 `tests/test_geojson_loader.py`：夹具的用途写在用例名里，
改了形状却忘了改用例，比不改更糟。
