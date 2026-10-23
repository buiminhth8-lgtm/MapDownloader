# 行政区域层级模型（Slice 18）

这份文档回答一个问题：**"省和市从哪来？"**

Slice 16 只有一个「中国」预设（`app/geo/region.py` 里的 `CHINA_REGION_PRESET`），
Slice 17 给了它一条 Zoom 建议。两者都只覆盖到"全国"这一层 —— 想下浙江省、
想下杭州市，界面上没有东西可选。

这个 Slice 把"一个中国预设"升级成**可扩展的行政区域目录**：
中国 → 省 / 直辖市 → 市。它本身**不下载任何东西**，只提供一份统一的区域数据源，
给 Slice 19（分层下载计划）和 Slice 20（区域管理界面）用。

---

## 1. 层级长什么样

```text
中国 (COUNTRY)                     ← resources/regions/country/china.geojson
│
├── 浙江省 (PROVINCE)              ← resources/regions/province/zhejiang.geojson
│   ├── 杭州市 (CITY)              ← resources/regions/city/hangzhou.geojson
│   └── 宁波市 (CITY)
│
├── 北京市 (PROVINCE，直辖市)       ← 直辖市直接挂在国家下，不凭空造一级
│   └── 北京市 (CITY)
│
└── 广东省 (PROVINCE)
    └── 广州市 (CITY)
```

三条规则（`app/planning/region_level.py::PARENT_LEVEL_RULES`）：

| 等级 | 允许的父级 | 说明 |
| --- | --- | --- |
| `COUNTRY` | **没有父级** | 写了 `parentId` 直接报错 |
| `PROVINCE` | `COUNTRY` | 省、自治区、直辖市 |
| `CITY` | `PROVINCE` | 地级市 |
| `MISSION` / `CUSTOM` | **豁免** | 不属于行政层级，怎么挂都不检查 |

规则只有一份，写在规划层里；`app/regions` 只是**转调**它，自己一个等级字面量都没有
（`tests/test_zoom_policy.py` 的 AST 静态守卫盯着这件事）。

---

## 2. `resources/regions/` 的目录结构

```text
resources/regions/
├── index.json                 ← 随仓库分发（元数据）
├── README.md                  ← 授权要求与文件格式
├── country/                   ← 数据（.geojson，不进提交）
│   └── china.geojson
├── province/
│   └── zhejiang.geojson
└── city/
    └── hangzhou.geojson
```

**为什么分目录而不是全平铺**：一个省一个 `province/zhejiang.geojson`，
一眼能从路径读出等级；将来给市加区县（`district/`）也不用重新排目录。
`country/province/city` 三个目录里各放一个 `.gitkeep`，空目录也能进提交。

**谁在提交里，谁不在**：

| 文件 | 进提交？ | 为什么 |
| --- | --- | --- |
| `index.json` | ✅ | 它是**元数据**（id / 名称 / 层级 / 相对路径），不是边界数据 |
| `README.md` | ✅ | 说明 |
| `**/*.geojson` | ❌ | `.gitignore` 里是 `resources/regions/**/*.geojson` |

`index.json`（当前只有一个中国）：

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

几个设计取舍：

- **`index.json` 里没有 Zoom。** Zoom 建议由 `level` 在**运行时**查 Slice 17 的策略表
  得到。存一份进 index 就是第二个真相，改了策略还要回头改数据。
- **`bounds` 可以不写。** 写了就当作**声明值**（对象形式
  `{"west":…,"south":…,"east":…,"north":…}`），深度校验时会跟真边界算出来的比；
  不写就从边界文件读。它不是缓存，只是一个可选的一致性锚点。
- **`id` 是稳定标识**：任务的 `region_id`、manifest 的 `region.id` 都用它。
  改名可以，改 id 等于换了一个区域。

---

## 3. 代码结构

| 文件 | 职责 | 依赖 |
| --- | --- | --- |
| `app/regions/models.py` | `RegionPreset`（目录条目）、`CatalogIssue`、`CatalogValidationResult` | 转出路径安全与等级规则 |
| `app/regions/catalog.py` | `RegionCatalog`：读 `index.json`、查询、层级遍历、`stale()` | 无 UI、无网络 |
| `app/regions/loader.py` | `RegionLoader`：目录条目 → 文档 / 几何 / `DownloadRegion` | 复用 `app.geo.geojson_loader` |
| `app/regions/validator.py` | `validate_catalog()`：快速（只看文件在不在）+ 深度（真解析） | 无副作用 |
| `app/regions/service.py` | `RegionService`：**业务入口**，界面只认它 | 兜底 + 策略层 |

### 3.1 为什么有两个 `RegionPreset`

```text
app.geo.region.RegionPreset     app.regions.models.RegionPreset
─────────────────────────────   ─────────────────────────────
扁平的、给下载链路用的          带 parentId / code 的目录条目
geojson_filename: str           geojson_path: str（可带子目录）
region_level: RegionLevel       level: RegionLevel
无父级概念                       parent_id: str | None
```

它们**故意不合并**：

1. `app.geo.region` 是下载链路的核心模块，让它去 import `RegionCatalog` 会形成
   `geo → regions → geo` 的**环**；
2. 合并后 `parent_id` 对"自定义矩形"这类根本不是目录条目的预设毫无意义。

两个类之间**只有一条转换方向**：`RegionLoader.to_geo_preset(entry)`（目录 → 扁平）。
反向（扁平 → 目录）**刻意没有实现** —— 有了它就会有人拿它造目录，层级就白做了。

`app/geo/region.py` 的 `RegionPreset` 加了一个 `root` 参数和一个 `_resource_root()`
方法，好让两种来源算出**同一个**绝对路径。`tests/test_region_hierarchy.py` 里有一条
"两个 china 是一致的"的用例钉着这件事。

### 3.2 路径安全只有一个来源

`app/geo/region.py` 里的三个函数是**唯一**的实现：

```python
normalize_region_relative_path(raw)     # 反斜杠转正斜杠、去 ./、折叠重复斜杠
is_safe_region_relative_path(raw)       # 非空 / 非绝对 / 无 ../ / 无盘符前缀
resolve_region_path(root, raw)          # → 绝对路径，越界抛 RegionPathError
```

`app.regions.models` 里 `from app.geo.region import normalize_region_relative_path`
是**同一个函数对象**（`tests/test_region_catalog.py` 用 `is` 断言）。为什么单独强调：
`index.json` 是**可编辑的数据文件**，`"geojson": "../../../../etc/passwd"` 或
`"C:\\Windows\\..."` 必须被拒 —— 而且只能在一处被拒，不然早晚有一处漏了。

---

## 4. Lazy Load：目录初始化不读边界

一个省几 MB 的 GeoJSON 是常态。如果 `RegionCatalog(...)` 就把所有文件解析一遍，
将来放 34 个省 + 300 个市，光是打开界面就要几十秒。

**契约**：

- `RegionCatalog.from_index()` / `.load()` 只读 `index.json`（几 KB）；
- 查询（`get` / `list_all` / `list_by_level` / `list_children` / `get_parent` /
  `get_ancestors`）**全部**只碰内存里的条目；
- 只有 `RegionLoader.load_document()` / `load_bounds()` / `to_download_region()`
  才真的去读文件。

被 `tests/test_region_catalog.py` 钉着守：把 `load_geojson_document` monkeypatch 成
"调用一次就计数/爆炸"，构造目录 + 跑一遍所有查询，断言计数为 **0**。

`RegionService.catalog` 是懒建的，并带 `stale()` 检查（比对 `index.json` 的
`(mtime_ns, size)`）—— 用户在运行中换了数据文件，下一次访问自动重建。

---

## 5. Bounds 从哪来

三种来源，优先级从高到低：

1. `index.json` 里写的 `bounds`（声明值）；
2. `Loader.load_bounds()` 从边界文件算出来的实际外包矩形；
3. 都没有 → `None`，界面按"边界不可用"处理（**不许退化成矩形**，这条是 Slice 16 定的）。

`Loader` **不做 bounds 缓存**（Spec §11 的明确要求）：同一份边界文件在两个地方
被读成两个不同的矩形，比慢一点糟糕得多。`to_download_region()` 在没显式传入
bounds / document / geometry 时**总是重读文件**。

---

## 6. 校验：快速 + 深度

```python
service.validate()            # 快速：只 stat 文件
service.validate(deep=True)   # 深度：真解析每份 GeoJSON
```

| 问题 | 严重度 | 说明 |
| --- | --- | --- |
| `index_version_unsupported` | ERROR | `version` 不在支持列表里 |
| `duplicate_id` / `missing_id` / `missing_name` | ERROR | 条目本身不合法 |
| `unknown_level` / `missing_level` | ERROR | 等级必须能被严格解析（**不**走宽松的 `RegionLevel.parse`） |
| `unsafe_path` / `missing_geojson_path` | ERROR | 路径安全，见 §3.2 |
| `parent_missing` / `self_parent` / `country_has_parent` / `parent_level_invalid` | ERROR | 层级规则 |
| `parent_cycle` | ERROR | `A→B→A` 这种环 |
| `bounds_malformed` | ERROR | `bounds` 结构不对 |
| **`geojson_not_found`** | **WARNING** | 数据文件还没放 —— 是**部署状态**，不是仓库缺陷 |
| `boundary_unreadable` / `bounds_mismatch` | ERROR | 只有深度校验才会出现 |

**为什么"缺文件"是 WARNING**：仓库里本来就不带边界数据（见 §7），
"没放文件"是正常中间态。如果它算 ERROR，`validate()` 在任何一台干净的机器上都红，
那这个命令就没人看了。

---

## 7. 授权要求（重要，与 Slice 16 同一条）

> 中国国界、省界、市界都属于**法定地图内容**，必须使用带**审图号**的合法授权
> GeoJSON 数据。

本项目**不内置、不生成、也不套用**任何行政边界数据。`index.json` 里那条
`{"id": "china"}` 只是**目录条目**（"有这么个区域、它的文件该放哪"），
**不是边界数据**。`country/`、`province/`、`city/` 三个目录在仓库里都是空的
（只有 `.gitkeep`）。

缺文件时程序**明确报错**并告诉你该放到哪个路径，**不会**退化成按外包矩形下载
—— 那会静默下一大片境外瓦片，而你在界面上看不出来。

`scripts/verify_slice16_polygon.py` 的场景 E 会把这件事当成静态守卫来查：
仓库里不许出现内置边界数据。

---

## 8. 代码怎么用

```python
from app.regions import RegionService
from app.planning import RegionLevel

service = RegionService()

# 层级导航
service.list_countries()                 # (中国,)
service.list_provinces("china")          # (浙江省, 北京市, …)
service.list_cities("zhejiang")          # (杭州市, 宁波市, …)
service.children("china")                # 等价于 list_provinces
service.ancestors("hangzhou")            # (中国, 浙江省, 杭州市)
service.path_text("hangzhou")            # '中国 / 浙江省 / 杭州市'

# Zoom 建议（完全复用 Slice 17，没有第二份策略表）
rec = service.get_zoom_recommendation("hangzhou")
rec.range_text                           # 'Z12~16'

# 拿去做下载（复用 Slice 16 的链路）
region = service.get_download_region("hangzhou")
region.geometry, region.bounds           # 交给既有的估算器 / 过滤器

# 界面要的扁平预设（唯一转换方向由 service 内部完成）
[preset.name for preset in service.list_presets()]
```

### 兜底行为

`index.json` 读不出来（文件没了 / JSON 语法错 / `version` 不支持）时，
`RegionService` **不让界面崩**：它退化成一个由 `app.geo.region.list_region_presets()`
合成出来的目录，`catalog_error` 记下原因，日志里留一行 ERROR。此时：

- "中国" 这个预设仍然可用（Slice 16 / 17 的功能一点不少）；
- 层级查询（`list_provinces` 等）返回空元组 —— 平铺预设里没有父子关系，
  凭空编一个出来才是更糟的谎。

---

## 9. 界面接了什么

本 Slice 只接了**最小的一环**：区域下拉框的选项**从 `RegionService.list_presets()` 来**，
不再是直接读 `REGION_PRESETS` 常量。于是"把 `REGION_RESOURCE_DIR` 指到别处"
这件事对界面同样成立（`tests/test_ui_smoke.py` 里有一条重定向后下拉跟着变的用例）。

**没有**做层级下拉、省市选择器、区域管理页 —— 那些是 Slice 19 / 20。

> **Slice 19 更新（2026-09-20）**：层级导航已经接上了。那个"一条下拉列全部行政区"
> 的控件被**级联下拉**（国家 / 省 / 市，Slice 27 起多一段「县区」）取代，数据来源
> 仍是本 Slice 的 `RegionService`（`list_countries()` / `list_provinces()` /
> `list_cities()` / `list_counties()`），等级也仍由目录条目给出。
> 细节见 [region_selection_ui.md](region_selection_ui.md)。

---

## 10. 怎么验收

```bash
# 单元测试
.venv/Scripts/python.exe -m pytest tests/test_region_catalog.py \
    tests/test_region_loader.py tests/test_region_hierarchy.py \
    tests/test_region_validation.py -q
.venv/Scripts/python.exe -m pytest -q          # 全量，确认无回归

# 实机验收（真 windows 平台插件、真窗口、真点按钮、截图落 docs/）
.venv/Scripts/python.exe scripts/verify_slice18_regions.py
.venv/Scripts/python.exe scripts/verify_slice18_regions.py --offscreen
.venv/Scripts/python.exe scripts/verify_slice18_regions.py --no-shots
```

`scripts/verify_slice18_regions.py` 覆盖 Spec §19 的手工验收步骤，
截图落在 `docs/images/slice18_regions_*.png`：

| 截图 | 内容 |
| --- | --- |
| `images/slice18_regions_dropdown.png` | 下拉框来自目录，中国还在 |
| `images/slice18_regions_three_levels.png` | 三级目录可用 + 路径文本 |
| `images/slice18_regions_city_zoom.png` | 选中市级区域时的 Zoom 建议来自策略层 |

---

## 11. 下一步

### Slice 19 已经用掉了什么（2026-09-20）

| 能力 | 状态 |
| --- | --- |
| 目录条目 + 层级查询 | ✅ Slice 18 → Slice 19 的级联下拉 |
| 路径安全 / 校验 | ✅ Slice 18 |
| `level` → Zoom 建议 | ✅ Slice 17，Slice 19 接进界面（推荐 / 自定义策略） |
| **按层级生成多区域下载计划** | ❌ 仍待做 |
| **区域管理界面（增删改查）** | ❌ 仍待做 |
| 磁盘占用预估 / 依赖检查 | ❌ 仍待做 |

Slice 19 只消费了目录的**读取侧**（`list_countries` / `list_provinces` /
`list_cities` / `get_region`）。目录写入、校验报告进界面、多区域计划都还没做。

放数据的步骤没变，只是路径从平铺改成分层：

1. 把授权 GeoJSON 放到 `resources/regions/<level>/<id>.geojson`；
2. 在 `resources/regions/index.json` 的 `regions` 里加一条，写清
   `id` / `name` / `level` / `parentId` / `geojson`；
3. 跑一次 `service.validate(deep=True)` 确认没有 ERROR。

**加完之后界面不用改**：三段下拉是遍历目录来的，等级也来自条目。补一条省级记录，
省份下拉就多一项；补一条市级记录，选到它的省时城市下拉就多一项 —— 只有一件事要
注意：新条目的 `parentId` 必须指到同文件里某个条目的 `id`，否则它不会出现在任何
一级的下拉里（`validate()` 会报 `parent_missing`）。

策略核心、加载器、界面提示都不需要改动。
