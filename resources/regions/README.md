# 行政区边界数据（`resources/regions/`）

这个目录是**放数据的地方**。仓库里只带**元数据**（`index.json`）和这份说明，
**不带任何边界文件**。

## 目录布局

```text
resources/regions/
├── index.json     ← 目录：有哪些区域、它们在哪个文件里（随仓库分发）
├── README.md      ← 你正在读的这一份（随仓库分发）
├── country/       ← 国家级边界（.geojson，不进提交）
├── province/      ← 省级边界（.geojson，不进提交）
└── city/          ← 市级边界（.geojson，不进提交）
```

文件名与 id 无关，**由 `index.json` 决定**。分目录只是为了让路径本身能读出等级。

## `index.json` 长什么样

```json
{
  "version": 1,
  "regions": [
    {
      "id": "china",
      "name": "中国",
      "level": "country",
      "parentId": null,
      "geojson": "country/china.geojson",
      "boundaryVersion": null
    }
  ]
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | ✅ | 稳定标识。任务的 `region_id`、地图包 manifest 的 `region.id` 都用它 |
| `name` | ✅ | 界面上显示的名字 |
| `level` | ✅ | `country` / `province` / `city`（`mission` / `custom` 不走目录） |
| `parentId` | ✅ | 父级 id；国家级必须是 `null`。省挂国、市挂省 |
| `geojson` | ✅ | 相对本目录的路径，**必须**带子目录（如 `province/zhejiang.geojson`） |
| `boundaryVersion` | ➖ | 期望的边界版本；实际值以文件里的 `properties.boundaryVersion` 为准 |
| `bounds` | ➖ | 声明的外包矩形，**对象**形式 `{"west":…,"south":…,"east":…,"north":…}`；深度校验时会跟几何实际范围比一比 |

**`index.json` 里没有 Zoom 字段**，也**不允许**加：Zoom 建议由 `level` 在运行时
查 `app/planning` 的策略表得到（见 `docs/zoom_strategy.md`）。存一份进来就是
第二个真相。

## 加一个省 / 市

1. 把授权 GeoJSON 放到对应目录，比如 `resources/regions/province/zhejiang.geojson`；
2. 在 `index.json` 的 `regions` 里加一条：

   ```json
   {
     "id": "zhejiang",
     "name": "浙江省",
     "level": "province",
     "parentId": "china",
     "geojson": "province/zhejiang.geojson"
   }
   ```

3. 自测：

   ```bash
   .venv/Scripts/python.exe -c "from app.regions import RegionService; s=RegionService(); print(s.path_text('zhejiang')); print(s.get_zoom_recommendation('zhejiang').range_text); print(s.validate().as_lines())"
   ```

   想连边界内容一起验，把 `validate()` 换成 `validate(deep=True)`（会真解析每个文件，
   慢一些，但能查出文件坏掉 / 声明的 `bounds` 对不上）。

放好之后，界面上「区域类型 → 行政区域」的下拉里就会出现它；
选中后会显示 `中国 / 浙江省` 这样的路径和对应的 Zoom 建议。

## 授权要求（重要）

> 中国国界、省界、市界都属于**法定地图内容**，必须使用带**审图号**的合法授权
> GeoJSON 数据。

本项目**不内置、不生成、也不套用**任何行政边界数据。原因很直接：

- 国界画错了不是"精度问题"，是**地图合规问题**；
- 从网上随手拿一份 `china.geojson` 塞进来，没有人能保证它的来源与审图号；
- 因此本工具只提供"规范路径 + 加载器 + 缺失时的明确报错"，
  数据由使用者自己按授权渠道取得。

缺文件时程序会**明确报错**并告诉你文件该叫什么、放哪，**不会**退化成按外包矩形下载
—— 那会静默下一大片境外瓦片，而你在界面上看不出来。

## 文件格式

标准 GeoJSON，顶层可以是 `FeatureCollection` / `Feature` / 几何对象本身；
面几何支持 `Polygon` 与 `MultiPolygon`。建议在 `properties` 里写上版本，
便于日后追溯"这批瓦片是按哪版边界裁的"：

```json
{
  "type": "Feature",
  "properties": {
    "name": "浙江省",
    "id": "zhejiang",
    "boundaryVersion": "2026-09"
  },
  "geometry": { "type": "MultiPolygon", "coordinates": [ [ [ [120.0, 30.0], "…" ] ] ] }
}
```

几条约定：

- **必须是 UTF-8**（无 BOM 也可以）。带别的编码会直接报错，不做猜测转码。
- `properties.boundaryVersion` 会随任务记录与 MBTiles metadata 一起写下去，
  最后进地图包 manifest 的 `region.boundaryVersion`。**两批瓦片能不能续着下、
  一个包是不是按同一版边界裁的，就靠它**。
- **不接受跨 180° 经线的几何**：本工具的瓦片数学是单块的 Web Mercator，
  跨经线的区域会被拒绝（而不是悄悄裁掉一半）。
- 自相交的几何默认拒收；确实需要时可以走"尝试修复"（buffer(0)），
  但修复结果会如实记进日志。

## 自测

放好文件后，不必先跑一次真下载：

```bash
# 目录读不读得动、路径对不对、层级通不通
.venv/Scripts/python.exe -c "from app.regions import RegionService; s=RegionService(); print(s.validate(deep=True).as_lines())"

# 只看某个区域的外包矩形
.venv/Scripts/python.exe -c "from app.regions import RegionService; s=RegionService(); print(s.get_region('zhejiang').name, s.loader().load_bounds('zhejiang'))"
```

或者在界面上点一次「计算」—— 它会把候选 / 区域内 / 过滤三个数字都列出来。

## 不要提交

`resources/regions/**/*.geojson` 已在 `.gitignore` 里。授权数据的再分发条件由
提供方决定，别顺手把它提交进仓库。

`index.json` **要**提交 —— 它是元数据（"有哪些区域、文件该放哪"），
不含任何边界坐标。`scripts/verify_slice16_polygon.py` 的场景 E 会检查这件事。
