# 分级离线地图链路夹具（Slice 25）

这一组夹具只服务一件事：给 `scripts/hierarchical_map_e2e.py` 提供一条
**可跑得动的、严格嵌套的**「全国 → 省 → 市」链路，外加一条任务区航线。

```
democountry   COUNTRY   多边形（切掉东南角）
└── demoprovince  PROVINCE  多边形（切掉东北角）
    └── democity  CITY      多边形（切掉东北角）
mission-hierarchy MISSION   LineString（由 mission_route.geojson 做米制 Buffer 生成）
```

## 为什么要自己画

`docs/polygon_region_download.md` 与 `resources/regions/README.md` 已经把同一条
规矩写过一遍：**真实行政边界是要授权的数据，不进仓库**。这里的多边形是
"随便画一个"的示例几何，只保证三件事：

1. **严格嵌套** —— `city ⊂ province ⊂ country`（用凸性推的，不是目测）；
2. **不是矩形** —— 每个多边形都切掉一个角，否则"多边形过滤"与"外包矩形枚举"
   结果相同，任何"忘了做相交过滤"的实现都会静默通过；
3. **跑得动** —— 坐标跨度按 E2E 脚本要真下载的 Zoom 区间反推，让整条链路
   在秒级完成（详见脚本头部的规模表）。

## 文件

| 文件 | 用途 |
| --- | --- |
| `index.json` | 三级行政区目录，供 `RegionService(root=...)` 加载 |
| `country.geojson` | 全国级多边形 |
| `province.geojson` | 省级多边形 |
| `city.geojson` | 市级多边形 |
| `mission_route.geojson` | 任务区航线（`LineString`，**不**在 index.json 里） |

`mission_route.geojson` 刻意不登记进 `index.json`：任务区不是行政区目录的成员，
它由 `app.mission` 在运行时从用户坐标生成并落到用户数据目录（Slice 22）。
把它塞进目录会让"行政区"与"任务区"两个层次的边界变模糊。

## 几何嵌套是怎么保证的

三个多边形都是**凸**的，因此"顶点都在父多边形内"就等价于"整个多边形在父多边形内"。
（对外包矩形则是 110~116E / 30~36N ⊃ 111~115E / 31~35N ⊃ 112~114E / 32~34N。）
`tests/test_hierarchical_e2e.py` 里有一条用例把这件事**真的算一遍**，
而不是靠这份文档的承诺。
