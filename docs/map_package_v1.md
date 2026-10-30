# 离线地图包格式 V1（Map Package Format V1）

> **这是 Windows 侧工具与 Android 侧应用之间的契约。**
> 改了 `manifest.json` 就等于改接口：一端改了字段，另一端的导入会直接失败。
>
> | 项 | 值 |
> | --- | --- |
> | `formatVersion` | **1** |
> | 协议起点 | Slice 9（Windows 打包）/ Slice 11（Android 导入） |
> | 定义处（Windows） | `app/package/models.py` — `MANIFEST_FIELD_ORDER` / `MANIFEST_BOUNDS_FIELDS` / `MANIFEST_MBTILES_FIELDS` |
> | 定义处（Android） | `openstreetmap/.../offline/model/OfflineMapManifest.java` — `SUPPORTED_FORMAT_VERSION` |
> | 样例 | `tests/fixtures/map_package_v1/manifest.json`（两端测试共用同一份） |
> | 兼容性测试 | Windows：`tests/test_package_protocol_v1.py`；Android：`OfflineMapManifestProtocolV1Test` |

---

## 1. 目录结构

一个地图包就是**一个目录**：

```
<package-id>/
├── manifest.json      # 契约文件，唯一入口
└── map.mbtiles        # SQLite 瓦片库（含 metadata 表）
```

约定：

- 目录名**必须等于** `manifest.json` 里的 `id`。Android 导入时以此判断目标路径。
- `manifest.json` 与 `map.mbtiles` 文件名固定，**不可改名**（`mbtiles.file` 必须等于
  `map.mbtiles`，Android 侧会按该字段找文件，但传输与导入流程只认这个固定名）。
- 目录里**不允许**再嵌套子目录。Android 导入会遍历目录，遇到子目录即报
  `ImportError.MANIFEST_INVALID`。
- `map.mbtiles` 必须是**真实的 SQLite 文件**（头 16 字节为 `SQLite format 3\0`），
  且有 `tiles` 表与非空的 `metadata` 表。

---

## 2. 字段定义

字段**顺序**也是协议的一部分：`to_dict()` 按 `MANIFEST_FIELD_ORDER` 显式重排，
保证同一份数据每次生成的字节序稳定（增量比对脚本依赖它）。

| # | 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | `formatVersion` | `int` | ✅ | 协议版本，V1 恒为 `1`。**唯一的版本开关。** |
| 2 | `id` | `string` | ✅ | 包标识；同时是 Android 侧的目录名与 Registry 主键。ASCII 安全字符（`[A-Za-z0-9_-]`），不含路径分隔符 |
| 3 | `name` | `string` | ✅ | 展示名，可含中文 |
| 4 | `description` | `string` | ❌ | **唯一可选字段**。为 `null`/空时该键**不写入** JSON |
| 5 | `provider` | `string` | ✅ | 数据源标识，当前恒为 `"tianditu"` |
| 6 | `mapType` | `string` | ✅ | `"vector"` 或 `"image"`。决定包的语义（见 §2.1） |
| 7 | `labels` | `bool` | ✅ | 是否**已合成**注记层（见 §2.1） |
| 8 | `composition` | `string` | ✅ | `"merged"` 或 `"separate"`（见 §2.1） |
| 9 | `tileScheme` | `string` | ✅ | 瓦片编号方案，当前恒为 `"web_mercator"` |
| 10 | `tileFormat` | `string` | ✅ | 瓦片图片格式，`"png"` / `"jpg"`，小写不带点 |
| 11 | `minZoom` | `int` | ✅ | 包内最小层级（闭区间） |
| 12 | `maxZoom` | `int` | ✅ | 包内最大层级（闭区间）；`minZoom <= maxZoom` |
| 13 | `bounds` | `object` | ✅ | 地理范围，见 §3 |
| 14 | `tileCount` | `int` | ✅ | 包内瓦片总数；必须等于 MBTiles 里 `tiles` 表的行数 |
| 15 | `mbtiles` | `object` | ✅ | 瓦片库元信息，见 §4 |
| 16 | `createdAt` | `string` | ✅ | ISO 8601 **带时区偏移**的生成时间，例如 `2026-09-18T12:00:00+08:00` |

> **`description` 是唯一的可选字段。** Windows 侧 `to_dict()` 在
> `description` 为 `None` 时**不写这个键**；Android 侧 `requireString` 对它做了
> 例外处理。其余 15 个字段缺任何一个，两端都会拒绝。

### 2.1 `mapType` / `labels` / `composition` 的语义

三者描述的是**同一个包的内容形态**，不是三个独立开关：

| 组合 | 含义 | 典型来源 |
| --- | --- | --- |
| `vector` + `labels=false` + `merged` | 矢量底图，**未**合成注记 | 只下底图 |
| `vector` + `labels=true` + `merged` | 矢量底图 + 注记**已烘进同一套瓦片** | 底图与注记合成（Slice 6） |
| `image` + `labels=false` + `separate` | 纯影像，无注记 | 只下影像 |

- `labels=true` 时，`composition` 必然是 `merged` —— 注记已经合进瓦片里，
  地图端**不需要**再叠加注记图层。
- `labels=false` + `separate` 时，地图端若想要注记得自己叠（当前 Android 侧不做）。
- Android 侧只用这三个字段做**展示与提示**（列表摘要、离线说明），
  不据此改变瓦片读取方式。

---

## 3. `bounds` 语义

```json
"bounds": { "west": 116.2, "south": 39.7, "east": 116.6, "north": 40.1 }
```

| 字段 | 类型 | 单位 | 范围 |
| --- | --- | --- | --- |
| `west` | `number` | 经度（度） | `[-180, 180]` |
| `east` | `number` | 经度（度） | `[-180, 180]` |
| `south` | `number` | 纬度（度） | `[-85.0511, 85.0511]` |
| `north` | `number` | 纬度（度） | `[-85.0511, 85.0511]` |

约定：

- **WGS84 经纬度**，十进制度，不是投影坐标、不是像素。
- 必须满足 `west <= east` 且 `south <= north`。
- 这是**用户请求的范围**，不是"包内瓦片的并集"：瓦片是整块对齐的，
  实际覆盖范围一般**略大于** `bounds`。地图端把 `bounds` 当作
  "可以放心平移的范围"，而不是"精确裁剪线"。
- **不跨 180° 经线**。需要跨时请拆成两个包。
- 纬度上下限是 Web Mercator 的自然边界（±85.0511°），不是 ±90°。

---

## 4. `mbtiles` 语义

```json
"mbtiles": {
  "file": "map.mbtiles",
  "size": 16384,
  "sha256": "0f9c…（64 位十六进制小写）"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `file` | `string` | 文件名，恒为 `"map.mbtiles"` |
| `size` | `int` | 文件字节数，**必须 > 0** |
| `sha256` | `string` | 对整个 `map.mbtiles` 求的 SHA-256，**64 位小写十六进制** |

- SHA-256 覆盖**整个文件字节**，不是瓦片内容的哈希。
- `size` 与 `sha256` 都在**打包完成后**回填 —— 也就是"先有 mbtiles，再有 manifest"。
- Android 导入时**先比 size 再算 SHA-256**：size 不符直接报
  `ImportError.CHECKSUM_MISMATCH`，省掉一次全文件读取。

---

## 5. XYZ 与 TMS

这是最容易出错的地方，明确写死：

| 场景 | 编号方案 |
| --- | --- |
| **工具 / API / Package 协议 / 用户输入的层级与行列** | **XYZ**（原点在左上，`tile_row` 向下递增） |
| **MBTiles 内部的 `tile_row` 列** | **TMS**（原点在左下，`tile_row` 向上递增） |

- 翻转只发生在**一处**：写 MBTiles 时由 `MbTilesWriter` 换，
  读 MBTiles 时由 **osmdroid 的 Provider** 换。
- 因此：**`manifest.json` 里没有任何行列号字段**，也就不存在"协议里该用 XYZ 还是 TMS"
  的歧义 —— 协议只描述"哪些层级、哪个经纬度范围"。
- 排错时若发现"地图上下颠倒 / 位置偏一半"，先查这一层翻转，
  而不是查 `bounds`。详见 `troubleshooting.md` 的「地图上下颠倒」。

`tileScheme = "web_mercator"` 表示标准 Web Mercator（EPSG:3857）切片。
未来若引入别的方案，靠新增 `tileScheme` 取值区分，**不改 `formatVersion`**。

---

## 6. 版本兼容策略

`formatVersion` 是**唯一**的版本判据。规则：

| 情况 | Windows 侧行为 | Android 侧行为 |
| --- | --- | --- |
| `formatVersion == 1` | 正常读/写 | 正常导入 |
| `formatVersion > 1` | `from_dict()` 抛 `UnsupportedFormatVersion`，错误信息**同时带上两端版本号** | `parse()` 抛 `ImportError.UNSUPPORTED_FORMAT_VERSION`，界面显示"该地图包版本过高，请升级应用" |
| `formatVersion < 1`（如 0、负数） | 抛 `UnsupportedFormatVersion` | 同样拒绝 |

设计原则：

1. **向后不兼容的改动必须升 `formatVersion`**，且大于当前版本的包一律拒绝，
   不做"尽力而为"的解析 —— 猜错字段语义比直接拒绝更危险（会给用户一张错位的空白地图）。
2. **加可选字段不升版本**：两端解析器都**忽略未知字段**（Android 侧有
   `test_android_accepts_unknown_extra_fields` 守着，禁止出现遍历 `json.keys()`
   做严格校验的写法）。这样新版工具产出的包，旧版应用仍能读。
3. **改名 = 破坏性改动**：字段名是 camelCase 的固定字符串，改名等于升 `formatVersion`。
4. 两端**不允许各自维护一份语义解释**。字段清单的交叉比对由
   `tests/test_package_protocol_v1.py` 机械完成：它直接读 Android 的
   `OfflineMapManifest.java` 源码，抽出所有 `requireXxx(json, "键名")` 的键名，
   与 Windows 的 `MANIFEST_FIELD_ORDER` 做**双向差集**断言。任何一端单方面改字段，
   这个测试立刻变红。

### 6.1 未来加字段的正确姿势

若 V2 要加 `attribution`（必填行）：

1. `formatVersion` 升到 `2`；
2. Windows：`MANIFEST_FIELD_ORDER` 增项 + `PACKAGE_FORMAT_VERSION = 2`；
3. Android：`SUPPORTED_FORMAT_VERSION = 2`，并在 `parse()` 里读取新字段；
4. 更新本文件与 `tests/fixtures/` 下的样例；
5. 跑两端协议测试。

若只是加**可选**字段（如 `sourceUrl`）：版本不动，两端都不校验它的存在性即可。

---

## 7. 样例文件

`tests/fixtures/map_package_v1/manifest.json` 是**协议样例**，由 Windows 侧
生产代码（`MapPackageBuilder`）真实生成后把三个与环境相关的值冻结为确定值：

| 字段 | 冻结值 | 原因 |
| --- | --- | --- |
| `createdAt` | `2026-09-18T12:00:00+08:00` | 否则每次生成都变，diff 噪声 |
| `mbtiles.size` | `16384` | 同上（真实值随文件系统波动） |
| `mbtiles.sha256` | `0000…0000`（64 个 0） | 同上 |

**它不是用来导入的真包**（`sha256` 是假的），只用于：

- Windows：验证 `parse_manifest_dict()` 能读通；
- Android：`OfflineMapManifestProtocolV1Test` 验证 `OfflineMapManifest.parse()` 能读通；
- 两端：验证字段清单、顺序、类型、取值形态一致。

样例覆盖的形态：矢量底图 / `labels=true` / `merged` / Zoom 11~13 /
北京主城范围 / 12 张瓦片。

---

## 8. 快速自检清单

发布一个新包之前，逐条确认：

- [ ] 目录名 == `manifest.json` 的 `id`
- [ ] `formatVersion` == 1
- [ ] 16 个字段齐（`description` 可选），大小写与顺序正确
- [ ] `bounds` 有四个字段、`west<=east`、`south<=north`、不跨 180°
- [ ] `minZoom <= maxZoom`，且都在 MBTiles 里真实存在
- [ ] `tileCount` == `SELECT COUNT(*) FROM tiles`
- [ ] `tileFormat` 与实际瓦片字节一致（PNG 头 / JPEG 头）
- [ ] `mbtiles.size` == 文件字节数
- [ ] `mbtiles.sha256` == `sha256sum map.mbtiles`
- [ ] `createdAt` 带时区

自动化版本：`python scripts/e2e_smoke_test.py`（见 README「端到端冒烟」）。


## Slice 36：单包多级覆盖扩展

旧 manifest 字段保持原样。新单包使用 `region.type = "coverage"`，可选
`region.coverage` 含 `version: 1`、`fingerprint` 及 `rules` 数组。每条规则包括
`id`、`name`、`level`、`boundaryVersion`、`bufferMeters`、`minZoom`、`maxZoom`、
`bounds`（west/south/east/north 数组）。它描述逐级稀疏覆盖，顶层 bounds 仅为总包围盒。

完整几何快照保存在 MBTiles `metadata.coverage_data`（UTF-8 JSON → zlib → Base64），
不在 manifest 中重复几何。其原始 JSON 规范化后的 SHA-256 与 `boundary_version`
及摘要 `fingerprint` 一致。验证器同时核对规则摘要、范围、Zoom、快照和已有文件校验值；
规则损坏时不能退回矩形覆盖判定。图片、标准 `tiles` 表、索引和 XYZ→TMS 约定保持一致。

读取端应将缺少高 Zoom 区域理解为计划内稀疏覆盖，不能只看总 bounds/minZoom/maxZoom
推断全域高清已齐全。Android 的父级瓦片回退和多包优先级由 Slice 39 实现与验收。


## Slice 38：内容去重与只读导出

优化文件保留标准 `metadata` 及 `tiles(zoom_level,tile_column,tile_row,tile_data)` 读取接口。
`storage_layout=normalized-v1` 使用 `map` 坐标表、`images` 图片表、`tiles` JOIN 视图；
`storage_layout=flat-export-v1` 使用普通 `tiles` 表。内容哈希采用 SHA-256，命中后仍比较
图片原字节，坐标一律 TMS 行号。当前读取器、诊断器和打包验证器接受两种结构。

扩展键 `export_readonly=true`、`export_tile_count`、`optimization` 标记派生产物、固化
瓦片数与压缩参数。源下载器拒绝往这些文件续传或修复，须修复原图后重新导出。
诊断额外检查 map 图片引用、坐标唯一性、tiles 视图计数与固化计数，防止 JOIN 隐藏坏引用。
图片压缩不改变格式版本；PNG 注记仍使用 PNG，JPEG 仅允许明确选择的卫星纯底图。
manifest 中 MBTiles 大小、SHA256 与瓦片数取导出文件实际结果，覆盖规则沿用 Slice 36。
