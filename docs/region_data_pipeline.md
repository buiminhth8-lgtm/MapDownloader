# Region Data Pipeline —— 行政区数据导入管线（Slice 26 / 27）

> 这份文档回答一个问题：**合法授权的行政区边界数据，怎么从 GeoJSON
> 变成 RegionCatalog 能直接用的「全国 → 省 → 市 → 县」目录。**
>
> 前置：Slice 16（Polygon 下载）、Slice 18（RegionCatalog / RegionService）、
> Slice 19（国家→省→市级联 UI）、Slice 25（分级离线地图 E2E 收尾）、
> Slice 27（新增「县」级 + 天地图省/市/县三份数据的专用导入脚本）。

---

## 1. 一句话：把"手工维护 index.json + 逐个手写边界文件"换成"导一份数据"

Slice 18 的 RegionCatalog 支持 `COUNTRY → PROVINCE → CITY`，Slice 27 起加
`COUNTY`（县 / 区 / 旗 / 县级市）一级，那套
`resources/regions/index.json` + `province/*.geojson` + `city/*.geojson` 是**手工
维护**的。真实工程不能靠人肉手写全国几百个省市县边界。

本 Slice 提供一条完整管线，把授权数据变成目录：

```
Authorized Region Dataset (GeoJSON FeatureCollection)
        │
        ▼
SourceReader          读 FeatureCollection，按字段映射取 id/name/level/parent
        │
        ▼
GeometryNormalizer    校验 is_valid / WGS84 范围 / 几何类型，make_valid 修轻伤
        │
        ▼
HierarchyBuilder      建 COUNTRY→PROVINCE→CITY→COUNTY，查 parent/duplicate/cycle/level
        │
        ▼
CatalogGenerator      逐 Region 写 GeoJSON + 生成 index.json + dataset manifest
        │
        ▼
Validation            全量校验（几何 / 层级 / 目录），给出 PASS / WARNING / FAIL
        │
        ▼
Atomic Publish        临时目录建好 → 原子替换 resources/regions/generated/
        │
        ▼
RegionCatalog         与手工数据用同一套格式，Catalog 无需知道来源
        │
        ▼
Country / Province / City / County UI（下载面板四级联下拉）
```

**本工具只提供导入能力，不自动下载、抓取或分发来源不明的行政边界数据。**
中国国界、省界、市界属于法定地图内容，必须使用带审图号的合法授权数据。

---

## 2. 输入格式

首版支持 **GeoJSON FeatureCollection**（不要求 Shapefile / KML / GPX）。

每个 Feature 至少通过**字段映射**取到四个值：

| 语义 | 字段 | 说明 |
| --- | --- | --- |
| id / code | `id_field` | region id 的稳定来源（优先行政区划 code） |
| 名称 | `name_field` | 界面展示名 |
| 级别 | `level_field` | country / province / city |
| 父级 | `parent_field` | 父级 id；全国级条目没有父级 |

字段名**不假设**：外部数据可能用 `GBCODE` / `NAME` / `LEVEL` / `PARENT`，也可能用
`code` / `name` / `level` / `parent`，导入时让用户按源数据真实键名配置。

`level_field` 支持别名映射：`country/province/city`、`1/2/3`、`全国/省/市`、
`直辖市`（映射到 PROVINCE 级）都认。

---

## 3. 核心模块（`app/regions/importer/`）

| 模块 | 职责 | Spec |
| --- | --- | --- |
| `models.py` | `RegionImportConfig` / `RegionDatasetManifest` / `RegionSourceFeature` / `RegionImportIssue` / id sanitize / level 映射 | §3 / §7 / §14 |
| `source_reader.py` | 读 FeatureCollection，逐 Feature 解析，支持 Polygon/MultiPolygon，拒绝 Point/LineString/空几何/缺字段 | §2 / §4 |
| `normalizer.py` | is_valid / 非空 / WGS84 范围 / lon-lat 顺序，make_valid 修轻伤并记 warning，不静默改严重异常 | §5 / §6 |
| `hierarchy_builder.py` | COUNTRY→PROVINCE→CITY 校验，查 parent 不存在 / duplicate id / cycle / level 错误 / orphan / city 直挂 country | §8 / §9 |
| `catalog_generator.py` | 逐 Region 写 GeoJSON + index.json + dataset manifest，bounds 来自 geometry.bounds | §10 / §11 / §12 / §14 |
| `region_exporter.py` | 临时 build 目录 → 全量校验 → 原子发布，失败保留旧数据 | §13 |
| `import_service.py` | 串联整条管线，暴露 `validate`（dry-run）与 `import_dataset`（发布） | §13 / §19 |

---

## 4. 关键设计取舍

### 4.1 Region id：稳定 + 文件名安全（§7）

最终 id 优先用源数据里的稳定行政区 code。源 code 不合 `[A-Za-z0-9_.-]` 时，
统一 sanitize（非法字符换成 `_`，纯中文/全非法则落到稳定哈希兜底），并保留
`originalCode` metadata —— **绝不拿中文名称随机生成 id**。

### 4.2 几何：默认不简化（§6）

行政边界是"这块地裁到哪个瓦片"的唯一依据，默认 `simplify_tolerance=None`
（禁用）。只有用户明确勾选"启用几何简化"才简化，避免边界 Tile 漏下载。

### 4.3 原子发布（§13）

`source → 临时 build 目录 → 全量 validation → catalog validation → publish`，
只有全部通过才原子替换生成数据集；失败保留旧数据，**不边生成边覆盖**。

### 4.4 目录来源透明（§16 / §17）

`RegionCatalog` 用同一套 index.json 格式，**无需知道数据是手工还是 importer
生成**。目录选择只发生在"选哪个根"这一刻（`effective_region_root()`）：
`resources/regions/generated/` 已发布（index.json 存在**且能解析**）就优先用它，
否则回退 `resources/regions/`（手工/发行态）。发布成功后 `RegionService.reload()`
无需重启即可看到新数据；reload 失败保留旧 Catalog。

### 4.5 性能（§21）

导入是**预处理阶段**：不做 Tile 枚举、不调 RegionTileEstimator、不读不写
MBTiles。几何运算逐 Feature 进行（is_valid / bounds / 可选 simplify），几千条
Feature 是秒级。静态守卫 `test_importer_does_not_touch_tile_math` 钉死这一条。

---

## 5. 怎么用

### 5.1 CLI（`scripts/import_regions.py`）

```bash
python scripts/import_regions.py \
  --source regions.geojson \
  --id-field code --name-field name --level-field level --parent-field parent \
  --boundary-version 2026-10 \
  --source-name "某授权数据 2026 年版" --source-license "审图号 GS(2026)xxxx" \
  --output resources/regions/generated
```

`--check` 只做 dry-run 校验不发布。失败以 non-zero exit code 退出。

### 5.2 GUI（主窗口 → 文件 → 行政区数据管理）

选源文件 → 字段映射自动按源数据键名填好 → 填边界版本 / 来源 / 授权说明 →
点「验证」看统计（总区域 / COUNTRY / PROVINCE / CITY / COUNTY / Geometry Invalid /
Missing Parent / Duplicate ID + PASS/WARNING/FAIL）→ FAIL 时导入按钮禁用 →
点「导入」原子发布。

### 5.3 天地图中国行政区划（`scripts/import_china_admin_regions.py`，Slice 27）

天地图官网的行政区划数据是**省 / 市 / 县三份独立文件**，字段只有 ``{name, gb}``
（没有 level / parent）。这个脚本把三份文件整理成一份四级 FeatureCollection 再导入：

```bash
python scripts/import_china_admin_regions.py \
  --province "Downloads/中国_省 (1).geojson" \
  --city "Downloads/中国_市.geojson" \
  --county "Downloads/中国_县.geojson" \
  --boundary-version 2026-10 \
  --source-license "天地图官网下载" \
  --check          # 先验证；去掉 --check 真正发布到 resources/regions/generated/
```

脚本做的整理（见脚本 docstring 与 `tests/test_import_china_admin_regions.py`）：

- 过滤 ``gb`` 为空的"境界线"Feature（国界 / 省界线）；
- 合成全国条目（``id=china``，几何 = 34 个省几何的 ``unary_union``，不造矩形）；
- 父级从 ``gb`` 前缀推导：市 → 省、县 → 市；
- **行政级别有歧义的单位去重**：直辖市 / 港澳台（省文件里已是省级）跳过市文件
  那份；不设区的市（东莞 / 中山 / 嘉峪关 / 儋州，gb 后两位 ``00``）归市级；
  省直辖县级市 / 县（济源 / 仙桃 / 海南直辖县 / 新疆兵团市）归县级、挂省。

---

## 6. 授权与合规

- 本工具**只提供导入工具**，不自动下载、抓取或分发来源不明的行政边界数据。
- 导入时必须记录 `sourceName` / `sourceLicense` / `boundaryVersion`（写进
  manifest，不记录任何敏感 Token）。
- 测试全部使用**自建几何**（`test_country ⊃ test_province_a/b ⊃ test_city_a1/a2`），
  不依赖真实中国边界。

---

## 7. 验证入口

| 层 | 命令 | 覆盖 |
| --- | --- | --- |
| 单元/集成 | `python -m pytest tests/test_region_*.py` | Reader / Normalizer / Hierarchy / Generator / Service / UI |
| 全量回归 | `python -m pytest` | 全仓库，含旧 Region 体系无回归 |
| CLI 冒烟 | `python scripts/import_regions.py --source tests/fixtures/regions/import/sample.geojson --output /tmp/out` | 端到端发布 |
| 主链路 E2E | `python scripts/e2e_smoke_test.py` | 下载链路无回归 |
| 分级 E2E | `python scripts/hierarchical_map_e2e.py` | 分级离线链路无回归 |
