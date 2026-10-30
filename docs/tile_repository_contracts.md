# 统一瓦片仓库：数据契约与服务接口

这份文档回答一个问题：**仓库里的一条瓦片"是谁"、怎么存、怎么读写、怎么导出，各方的硬约定是什么。**

架构与取舍见 [tile_repository_architecture](tile_repository_architecture.md)；这里只写契约。

**本文不包含可执行实现。** 伪代码只用来固定输入输出与失败语义，不代表已有代码。

---

## 0. 契约的效力

| 级别 | 含义 | 改它需要 |
| --- | --- | --- |
| **冻结** | 跨仓库/跨版本依赖，改了就会静默坏 | 升版本号 + 两端同步 + 契约测试 |
| **稳定** | 仓库内部依赖，改了只需同步本仓库 | 改代码 + 改本文件 |
| **设计期** | 尚未实现，实现时以代码常量为唯一真值 | 实现时回填真实常量 |

本文件里的**常量默认值**都是 `设计期` 级别。实现落地后，数字的真值来源是 `app/config/settings.py`，本文件只解释含义，不复制数值（[文档规范](README.md#文档规范改文档前先看) 第 3 条）。

---

## 1. 资产身份与版本

### 1.1 三个不同的"身份"

| 概念 | 回答什么 | 组成 | 变它会怎样 |
| --- | --- | --- | --- |
| **逻辑坐标身份** `CoordKey` | 这张瓦片画的是地球上的哪一块、哪一层 | `source_id` + `layer` + `z` + `x` + `y`(XYZ) | 是另一张瓦片 |
| **资产版本** `ContentVersion` | 这个坐标上的内容处于第几代 | `epoch_id`（写入批次，单调递增） | 只是更新，坐标不变 |
| **内容摘要** `ContentDigest` | 字节到底是哪一份 | `sha256(data)` + `size` + `encoding` | 内容变了 |

`projection` 与 `tile_size` **不进 `CoordKey`**，它们是 `source_id` 的属性（同一数据源内不会变）。理由：`z/x/y` 的语义只在本投影与瓦片尺寸下成立；换投影就是换数据源，不该在每一行里重复表达一个恒定值。

```
source_id  = 'tianditu'          # 产品标识，不是凭证
projection = 'EPSG:3857'         # 记在 sources 表
tile_size  = 256                 # 记在 sources 表
layer      ∈ {vec_w, cva_w, img_w, cia_w}
z ∈ [0, 22]   x ∈ [0, 2^z)   y ∈ [0, 2^z)   # XYZ，不是 TMS
```

### 1.2 编码（`encoding`）的位置

`encoding ∈ {png, jpg}`。它**不进 `CoordKey`**，是 `contents` 行的属性。

规则（冻结）：

1. 同一 `CoordKey` 在当前状态下**只有一个** `current` 内容，因此只有一个 `encoding`。
2. 编码变化 ⇒ 字节必然变化 ⇒ `sha256` 变化 ⇒ 走 §1.4 的更新规则，**不允许**在同一个 `ContentDigest` 下"换个编码"。
3. 导出期的格式转换（例如"把 PNG 转成 JPEG"）**不写回仓库**，只在交付物里发生。理由：转换是有损的，写回会让仓库再也拿不到原始字节。

### 1.3 Token 永不进入身份（冻结）

- `source_id` 是产品级标识（`tianditu`），与凭证无关。
- `sources.config_fingerprint` 是对 **`base_url` 模板 + 图层集合 + 投影 + 瓦片尺寸** 做的 `sha256`，**不含 token**。
- 任务记录、日志、报告、导出文件名、manifest 里**任何一处**都不出现 token（现状已有防线：`app/tasks/task_store.py:_assert_no_secret_keys` L285-303 拒绝写含 token/secret 的键；本契约沿用同一列表）。
- 更改 token 不得改变任何 `config_fingerprint`，也不得使任何资产"失效"。

### 1.4 同坐标不同内容：冲突与更新规则

| 场景 | 判定 | 动作 | 幂等 |
| --- | --- | --- | --- |
| 同坐标、同 `sha256` | 重复写入 | 只更新 `fetched_at`，不新增内容行，返回 `Unchanged` | ✅ 可重放 |
| 同坐标、不同 `sha256`、`allow_replace=False`（默认） | **冲突** | 保留旧 `current`，新值不落库，记 `Conflict` 事件 + 返回坐标 | ✅ 可重放 |
| 同坐标、不同 `sha256`、`allow_replace=True` | 授权更新 | 旧行 `state=superseded`，新行 `state=current`，`epoch_id+1` | ✅ 可重放（第二次同摘要变 `Unchanged`） |
| 同坐标、当前是 `quarantined`、新值合法 | 修复 | 新行 `current`，旧行保持 `quarantined` 供审计 | ✅ |

**为什么默认拒绝**：现状写入是 `INSERT OR REPLACE` 无条件覆盖（`app/mbtiles/writer.py:_INSERT_TILE_SQL` L115-118），一个"注记包"和"纯底图包"写进同一文件不会报任何错 —— 结果是一张图上有的地块有字、有的没有。仓库层必须把这件事变成显式选择。

### 1.5 版本保留与导出快照

**保留策略（设计期默认）**：

- `current`：永久（直到被显式删除或替换）
- `superseded`：保留最近 1 世代，超过 `RETENTION_DAYS`（默认 30）或内容池超配额时优先回收
- `quarantined`：保留到 `repair` 处理完（成功重下或人工清除）

**导出快照不是"保留历史"**。快照是：

```json
{
  "version": 1,
  "fingerprint": "<sha256 of canonical(payload)>",
  "epoch": 1287,
  "createdAt": "2026-10-30T08:00:00+08:00",
  "selector": { "assetIds": ["tianditu/vec_w"], "zoomRange": [4, 13],
                "region": { "type": "coverage", "fingerprint": "…" } },
  "entries": [
    { "asset": "tianditu/vec_w", "z": 12, "x": 3412, "y": 1567, "sha256": "…", "size": 18342 }
  ]
}
```

不变式（冻结）：

1. `entries` 的 `(asset, z, x, y)` 唯一且有序（按 `asset, z, x, y`），否则拒绝。
2. 导出**只能**读快照里列出的坐标，不得在遍历时"顺手读到别的"。
3. 导出前检查每条 `sha256` 的 `contents` 行是否还在；任一缺失 → 快照标记 `expired`，**中止导出**并要求重新冻结（不静默跳过）。
4. 快照只记 `sha256`，不记 `state` —— 快照一旦冻结，后续的 `superseded` 不影响它。

### 1.6 ETag / Last-Modified 不可用时的完整流程

现状：全仓对 `ETag` / `If-None-Match` / `Last-Modified` **零处理**（`app/` 下 grep 无命中）。天地图 WMTS 是否稳定返回这些头，也没有在任何地方验证过。

因此把"条件请求"设计成**可选加速**，主流程不依赖它：

```
更新任务（对已有坐标重新获取）：
  1. 若响应带 ETag/Last-Modified → 记入 sources 的 etag_cache（可选表），下次带 If-None-Match
  2. 收到 304 → 只更新 fetched_at，ContentVersion 不变
  3. 收到 200 → 计算 sha256
        ├─ 与 current 相同 → Unchanged（不新增、不替换）
        └─ 不同 → allow_replace ？替换 : Conflict
  4. 无 ETag（现状默认）：
        → 无条件重新取字节并比对 sha256（这不是"退化成盲下"，比对的收益就是省下替换与去重）
```

**必须显式承认的代价**：无 ETag 时更新一轮 = 完整重下流量。这条要写进 UI 提示，不能只写在文档里。

---

## 2. 仓库存储契约

### 2.1 逻辑表与主键

分片 DDL 见[架构 §6.3](tile_repository_architecture.md#63-分片内表结构)。这里只固定不变式：

| 对象 | 主键 | 不变式 |
| --- | --- | --- |
| `coords` | `(z, x, y, state)` | 同一 `(z,x,y)` 至多一行 `state=current`；`y` 是 **XYZ** |
| `contents` | `sha256` | `size == length(data)`；`encoding` 与数据的 magic bytes 一致 |
| `shard_meta` | `name` | 必须含 `layer/projection/tile_size/z_min/z_max/schema_version` |
| `catalog.shards` | `(asset_id, z_min)` | `path` 必须落在仓库根内（防穿越） |
| `catalog.coverage` | `(asset_id, z)` | 只是粗矩形，**不是**"齐全"的证据 |

### 2.2 状态转换（`coords.state`）

```
                 put(新坐标)                replace 授权
   (无) ─────────────────▶ current(0) ─────────────────▶ superseded(1)
                              │  ▲                            │
              深检判坏 / 导入 │  │ 修复成功                    │ prune
                              ▼  │                            ▼
                        quarantined(2)                     (删除)
```

允许的转换只有这五条。其它（`superseded → quarantined` 等）一律拒绝并报 `Corrupt`。

### 2.3 catalog 与分片的一致性

**不能假设多个 SQLite 文件有跨库原子提交。** `ATTACH` 能跨库写，但崩溃时各库各自回滚，没有两阶段提交。

规则（冻结）：

1. **分片是事实，catalog 是缓存。** 冲突以分片为准，并触发该分片重建登记。
2. **写序固定**：先提交分片事务 → 再更新 catalog。
3. **中断允许**：中断在第二步只造成 catalog 落后，`rebuild_index` 可修。
4. **不跨分片事务**：一次导入/导出 = "每分片一个事务 + 一个全局 epoch"，部分成功是合法状态。
5. **`epochs` 只追加**，不修改、不删除。

### 2.4 并发约束

| 操作 | 并发 |
| --- | --- |
| 写同一分片 | **单写者**，靠 `lock/<asset>-<band>.lock` 租约（内容：pid + 启动时间 + 心跳） |
| 写不同分片 | 可并行（不同文件、不同租约） |
| 读分片 | 任意多读者；WAL 下不阻塞写 |
| 导出 | 必须 `BEGIN` 建读快照；**禁止**在导出期间对该分片做 `prune` |
| 写 catalog | catalog 级租约，串行 |

抢不到租约 → 返回 `Busy`（不排队、不阻塞），由调用方决定重试或提示。

### 2.5 崩溃恢复与孤立数据

| 现象 | 检测 | 处理 |
| --- | --- | --- |
| 租约残留（进程已死） | 心跳超过 90 秒 | 接管前先 `PRAGMA quick_check`；失败 → `Corrupt` |
| 分片写到一半（`shard_meta` 缺 `schema_version`） | 打开即检查 | 标记 `Corrupt`，不参与导出，报告里列出 |
| `contents` 行无人引用 | `NOT EXISTS (SELECT 1 FROM coords WHERE coords.sha256 = contents.sha256)` | 只在 `prune` 时删（三条前置条件见架构 §7.5） |
| `coords` 指向不存在的 `contents` | `LEFT JOIN … IS NULL` | **立即报 `Corrupt`**，该分片退出服务 |
| `staging/` 残留 | mtime > 24h | 启动清理，**不动** `shards/` |
| catalog 落后 | `shards.tile_count != 分片实际 COUNT(*)` | `rebuild_index` 修正并记 epoch |

### 2.6 索引重建

`rebuild_index(asset_id=None)`（稳定契约）：

- 输入：可选资产过滤；只读。
- 输出：`RebuildReport{fixed_shards, dropped_entries, still_corrupt, duration}`。
- 行为：重算 `shards.tile_count/bytes`、重算 `coverage` 矩形、把 `Corrupt` 分片从 `shards` 表摘除但**不删文件**。
- 幂等：连跑两次，第二次报告全零。
- 禁止：重建过程**不写任何分片**。

---

## 3. 资产类型：原始图层与合成资产

### 3.1 类型定义

```
AssetKind
├─ RAW_BASE     原始底图：vec_w / img_w
├─ RAW_LABEL    原始注记：cva_w / cia_w
├─ SYNTHETIC_SINGLE   仅底图的合成产物（仍是单层，无法拆）
├─ SYNTHETIC_MERGED   底图 + 注记已合成进同一张 PNG
└─ UNKNOWN      无法判定（缺 metadata / 未知键值）
```

只有 `RAW_*` 能进仓库当前代。`SYNTHETIC_*` 进 `catalog.legacy_assets`，可以整块导出复用，但**不参与新合成**。

### 3.2 判定（实现见 Slice 43）

```
classify(metadata) -> AssetKind:
    comp = metadata.get('composition')
    labels = metadata.get('labels') == 'true'
    map_type = metadata.get('map_type')
    if comp == 'merged' and labels:  return SYNTHETIC_MERGED
    if comp == 'single' and not labels: return SYNTHETIC_SINGLE
    if comp is None or map_type is None: return UNKNOWN
    return SYNTHETIC_MERGED   # 兜底：宁可当合成，不可当原始
```

**兜底方向是刻意的**：把合成包误判成原始底图，会让"加注记"变成"给已经带字的图再叠一层字"——错得看不出来。反过来只是错过一次复用机会。

### 3.3 合成参数与来源追踪

合成发生在**导出期**，参数必须记进交付物，不能只活在内存里：

| 参数 | 落点 | 说明 |
| --- | --- | --- |
| `composition` | manifest + MBTiles metadata（已有） | `single` / `merged` |
| `labels` | 同上（已有） | 是否有注记 |
| `format` | 同上（已有） | merged 强制 `png`（`app/package/package_validator.py` L334-339） |
| `sources`（新增，可选） | 交付 manifest 的**新可选字段** | 形如 `[{"asset":"tianditu/vec_w","epoch":1287,"sha256":"…"}]`，用于回答"这份包是用哪一代原始图层做的" |

`sources` 是**可选**字段，老客户端忽略它（Android `from_dict` 忽略未知键），不升 `formatVersion`。

### 3.4 兼容性判断（能否混用）

| A | B | 可混用 | 原因 |
| --- | --- | --- | --- |
| `RAW_BASE(vec_w)` | `RAW_LABEL(cva_w)` | ✅ | 同一地图模式的底图/注记 |
| `RAW_BASE(vec_w)` | `RAW_LABEL(cia_w)` | ❌ | 矢量底图叠影像注记，语义错 |
| `SYNTHETIC_MERGED` | 任何 | ❌ | 已带注记，再合成即重影 |
| 不同 `tile_size` | 任何 | ❌ | 尺寸不等无法 alpha 合成（现状 `TileComposer._check_sizes` 会抛） |
| 不同 `projection` | 任何 | ❌ | 坐标不可比 |

**Windows 存四个原始图层，不代表 Android 必须四层叠加显示。** Android 侧现状是"打开一个 `.mbtiles` 用一条 provider 链"（`OfflineTileProvider` L36-59），没有多图层叠加的显示模型。因此交付仍然按"一个包 = 一张合成图"产出；四图层分存的价值在**仓库**（换注记策略不用重下），不在设备端。

---

## 4. 已有数据导入契约

### 4.1 只读与源文件保护（冻结）

1. 源文件**只能**以 `mode=ro` + `uri=True` 打开（现状 `app/mbtiles/reader.py:open` L285-286 已是此写法）。
2. 导入过程**不写、不改名、不移动、不删除**源文件。导入结束后源文件 mtime 必须与导入前一致（可作为验收断言）。
3. 源文件存在非空 `-wal` → 说明可能有活跃写入者 → **警告并要求确认**；不自动合并 WAL。
4. 源文件是 `export_readonly=true` 或 normalized 布局 → 允许导入（只读），但导入后的资产标记 `immutable_source=true`（只作来源记录，不影响仓库写入）。

### 4.2 支持的布局

| 布局 | 识别 | 读取列 |
| --- | --- | --- |
| flat | `tiles` 是 `table` | `zoom_level, tile_column, tile_row, tile_data` |
| normalized | `tiles` 是 `view` + `map`/`images` 存在 | 通过 view 读同样四列（SQLite 对 VIEW 透明） |

判定用 `sqlite_master.type`（现状 `reader.list_tables` L783-790 已同时收录 table 与 view；`normalized_storage_errors` L792-821 已能检悬空引用/重复坐标）。

### 4.3 XYZ / TMS：转换只在这里发生一次

源是 **TMS**（`tile_row`），仓库是 **XYZ**：

```
y_xyz = (2**z - 1) - tile_row
```

这条式子**只允许出现在 importer 与 exporter 两处**，且每处只出现一次。任何第三处出现即视为缺陷（可用静态守卫测试：全仓 `2\*\*z - 1 -` 与 `xyz_y_to_tms_y` 的调用点白名单）。

### 4.4 幂等与冲突

- **幂等键**：`(asset_id, z, x, y, sha256)`。
- 重复导入 → `already_present`，不产生新行、不改 `epoch`。
- 同坐标不同摘要 → `conflict`，**不覆盖**，写进报告与 `repair/ledger.sqlite`。
- 中断后重放安全：因为幂等，重复扫描只会增加 `already_present` 计数。

### 4.5 损坏处理

| 级别 | 判据 | 动作 |
| --- | --- | --- |
| 快检 | magic bytes 既非 PNG 也非 JPEG，或 `tile_data` 为空 | `quarantined` 入仓 + ledger |
| 深检（可选，慢） | Pillow `verify()` + `load()` 抛错 | 同上，另记原因 |
| 坐标非法 | `z∉[0,22]` / `x,y∉[0,2^z)` | 跳过 + ledger（不入仓） |

默认只做快检（导入要快）；`--deep` 才做深检。**两种模式的报告要分开标注**，不能混成一个"损坏数"（现状 validator 与 diagnostics 的口径本来就不同，见架构 §3.3）。

### 4.6 中断续导

`import_state` 表（在 catalog 内）：

```sql
CREATE TABLE import_state (
  source_fingerprint TEXT PRIMARY KEY,  -- 源文件 (path, size, mtime) 的 sha256
  asset_id TEXT NOT NULL,
  last_z INTEGER, last_x INTEGER, last_y INTEGER,
  done_tiles INTEGER NOT NULL, epoch_id INTEGER NOT NULL,
  finished INTEGER NOT NULL DEFAULT 0
);
```

续导规则：从 `(last_z,last_x,last_y)` 之后继续；因为遍历是 `ORDER BY z,x,tile_row`，水位是**全序**的，不需要每张都记账（每 5000 张落一次水位）。

### 4.7 缺少覆盖几何

旧包若只有 `bounds`、没有 `coverage_data`：

- 按外接矩形入仓
- `coverage` 表记该 zoom 的矩形
- 资产标签 `coverage_quality = bbox_only`
- **导出时不得**用它断言"覆盖齐全"（矩形会把空白当齐全）

---

## 5. 服务接口契约

统一约定（全组适用）：

- **取消语义**：所有长操作接受 `should_stop: Callable[[], bool]`，返回 `Cancelled` 而不是异常；已提交的批次不回滚（幂等保证可重做）。
- **幂等性**：每个接口显式标注 `Idempotent` / `Not idempotent`。
- **错误分类**：`RepoError` 子类，见 §5.9。
- **并发约束**：显式写出"是否需要租约"。
- **进度**：`progress: Callable[[Progress], None]`，`Progress` 只含已完成/总数/阶段名/字节，**不含 token、不含绝对路径**（路径由调用方拼）。

### 5.1 仓库查询与可用性检查

```python
def query_existence(
    assets: Sequence[str],                  # ['tianditu/vec_w', ...]
    zoom_range: tuple[int, int],            # 闭区间
    geometry: ShapelyGeometry | None,       # None = 整个 bounds
    region_filter=None,                     # 复用 geo.tile_region_filter
    should_stop=None,
) -> ExistenceReport
```

- 输出：逐 zoom 的 `{total, present, missing, missing_sample[:N], by_asset}`；`present` 只数 `state=current` 且 `sha256` 可解析的坐标。
- 幂等：只读，天然幂等。
- 并发：任意多读者，不需要租约。
- 错误：`NotFound`（资产未注册）、`Corrupt`（分片坏）。
- **不返回 BLOB**。要内容用 §5.4。

### 5.2 资产导入

```python
def scan(source_path: Path) -> ScanReport
    # 只读；返回 layout / AssetKind / metadata 全键 / zoom / tile_count /
    #      bytes / has_wal / has_coverage / warnings
    # Idempotent。不加租约（不写）。

def import_asset(
    source_path: Path,
    mode: Literal['fast', 'deep'] = 'fast',
    allow_replace: bool = False,
    should_stop=None,
    progress=None,
) -> ImportReport
    # 需要分片写租约（按 asset+band 逐个获取，拿不到就 Busy）
    # Idempotent（幂等键见 §4.4）
    # 输出：imported / already_present / conflicts[] / quarantined[] /
    #       skipped_invalid_coords / epochs_created / cancelled
```

### 5.3 瓦片写入与校验

```python
def put_tiles(
    asset_id: str,
    items: Iterable[tuple[int, int, int, bytes]],   # (z, x, y, data)
    allow_replace: bool = False,
    epoch_id: int | None = None,                    # None = 新建 epoch
    should_stop=None,
) -> PutReport
    # 需要分片写租约。Idempotent（同摘要 → Unchanged）
    # 批内去重：同一批里重复坐标以最后一次为准（与现状 batch 语义一致）
    # 输出：written / unchanged / replaced / conflicts[] / rejected[]

def verify_asset(asset_id: str, sample: int, deep: bool) -> VerifyReport
    # 只读。Idempotent
    # 输出：checked / corrupt / empty / missing_contents / by_zoom
```

### 5.4 版本或快照读取

```python
def freeze_snapshot(selector: SnapshotSelector) -> SnapshotMeta
    # 写 snapshots/*.json（原子：.tmp + os.replace）。Not idempotent
    #   （同一 selector 两次冻结会得到不同 fingerprint —— 时间/epoch 不同）
    # 但内容等价时 fingerprint 相同（fingerprint 只对 canonical(payload) 取摘要，
    #   payload 不含 createdAt）

def read_snapshot(fingerprint: str) -> Iterator[TileRecord]
    # TileRecord = (asset, z, x, y, sha256, size, data)
    # 只读。Idempotent。按 entries 顺序产出，不重排
    # 内容缺失 → 抛 SnapshotExpired（不静默跳过）
```

**`freeze_snapshot` 的 `payload` 不含 `createdAt`**，这样同一份数据两次冻结得到同一个 fingerprint —— 这是"导出可复现"的前提。

### 5.5 下载缺口计算

```python
def compute_gap(
    request: ExportRequest,        # 区域 + 分层 zoom + 模式 + 注记
    snapshot: SnapshotMeta | None, # None = 对当前仓库状态算
    should_stop=None,
) -> GapReport
    # 只读。Idempotent
    # 输出：逐 zoom/逐图层 {required, present, missing, missing_tiles[]（有上限）,
    #       parent_fallback_available_by_zoom}
    # 关键语义：missing 只数"目标级"缺口；
    #           parent_fallback 只用于提示，**不计入 present**
```

### 5.6 导出计划与执行

```python
def plan_export(request: ExportRequest) -> ExportPlan
    # 只读。Idempotent（同一 request 同一仓库 → 同一 plan）
    # 输出：packages[]（每个含 region/zoom 分层/估算字节/目标路径）,
    #       missing_policy, gap: GapReport, budget: BudgetReport
    # 不联网、不写盘

def run_export(
    plan: ExportPlan,
    missing_policy: Literal['local_only', 'fill_then_export', 'reject_if_missing'],
    should_stop=None,
    progress=None,
) -> ExportReport
    # 写 staging + 原子发布。Not idempotent（发布后重跑会撞已存在，
    #   需 overwrite=True 或换目标）
    # reject_if_missing 且 gap 非空 → 直接 InsufficientRequiredTiles，不建 staging
    # 失败 → 清 staging，final 目录不动
```

### 5.7 容量评估

```python
def assess(request: ExportRequest, strict: bool) -> BudgetReport
    # 只读。Idempotent
    # 见 §7
```

### 5.8 健康检查与恢复

```python
def health_check(scope: Literal['quick', 'full']) -> HealthReport
    # 只读。Idempotent。quick = 表结构+quick_check；full = 抽样内容解码
    # 输出：shards[] {path, ok, tile_count, bytes, problems[]},
    #       orphan_contents, dangling_refs, stale_staging, leases[]

def rebuild_index(asset_id: str | None = None) -> RebuildReport   # §2.6
def prune(dry_run: bool = True) -> PruneReport                    # §2.5 / 架构 §7.5
def repair_coords(
    asset_id: str,
    coords: Sequence[CoordKey],
    allow_replace: bool = True,
    should_stop=None,
) -> RepairReport
    # 需要写租约。Idempotent。**这是现有实现缺的能力**：
    # 现状只能全量遍历时顺带判定（resume.py:should_skip），无法按清单重下
```

### 5.9 错误分类

```
RepoError
├─ NotFound            资产/分片/快照不存在
├─ Corrupt             分片结构坏、摘要不符、悬空引用
├─ Conflict            同坐标不同内容且未授权替换（携带坐标清单）
├─ Busy                租约被占用（携带持有者信息，不带 token）
├─ InsufficientRequiredTiles  缺图且策略为 reject
├─ InsufficientBudget  超预算/磁盘不足（携带四类口径的数字）
├─ SnapshotExpired     快照引用的内容已被回收
├─ Cancelled           调用方中止
└─ Internal            其它（必须带 trace_id，不带 token）
```

**所有异常消息禁项**（冻结）：token、完整 URL 的查询串、用户绝对路径中的用户名段。

---

## 6. 打包契约

### 6.1 三种缺图策略（冻结语义）

| 策略 | 缺图时 | 是否联网 | 适用 |
| --- | --- | --- | --- |
| `local_only` | 照常导出，缺的部分没有；报告列出缺口 | ❌ | "先出一版看看" |
| `fill_then_export` | **先由调用方显式确认**、执行补抓、重新冻结快照、再导出 | 仅在此策略且用户确认后 | 常规交付 |
| `reject_if_missing` | 直接中止，不建 staging | ❌ | 验收/发版 |

**不得隐式联网**：`exporter` 的模块依赖里没有 `downloader`（架构 §5.1 的禁止边）。`fill_then_export` 的实现方式是"services 层先跑下载，再调 `plan_export`"，不是 exporter 内部去下载。

### 6.2 缺图报告（`GapReport` 的可读形式）

必须包含：

1. 逐 zoom 的 `required / present / missing` 三个数（**目标级**）
2. 逐图层的缺失（merged 模式下两层各自的缺口不是同一个数）
3. 缺失坐标样例（上限截断，注明"另有 N 张未展开"）
4. **父级回退可用性**：某 zoom 缺失但 `z-1` 齐全 → 显示"可降级显示"，**并明确写"这不等于目标级齐全"**
5. 若 `coverage_quality=bbox_only`，加一条"覆盖来源为外接矩形，齐全性仅供参考"

### 6.3 发布前校验（顺序固定）

1. `validate_mbtiles`（结构 + metadata + 抽样内容）
2. 覆盖检查（对**本次请求的分层 zoom**，不是对全库）
3. `package_validator`（manifest ↔ mbtiles 逐项比对）
4. osmdroid 查询回放（沿用 `mbtiles_optimizer.verify_osmdroid_reads` 的思路：按 Android 的查询语句抽 N 张）
5. 断言 XYZ/TMS 只翻转一次（写入行与回读行互为翻转）

任一步失败 → 不发布。

### 6.4 暂存与原子发布

```
<output>/<package_name>.building/     ← 暂存（同盘，保证 rename 原子）
        map.mbtiles
        manifest.json
（校验通过）
<output>/<package_name>/              ← 正式
```

发布 = `final.replace(backup)` → `os.replace(building, final)`（沿用 `app/package/package_builder.py:_publish` L762-780 的既有做法）。失败清理只清 `.building`，**不动**已存在的正式目录。

### 6.5 manifest 版本兼容

| 变更 | 升 `formatVersion` | 依据 |
| --- | --- | --- |
| 新增可选字段（如 `contentId`、`region.sources`） | 否 | Android 忽略未知键 |
| 新增必填字段 | 是 | 老客户端丢语义 |
| 改同名字段含义 | 是（且强烈不建议） | 最危险 |
| 删字段 | 是 | 客户端按"可选"处理会静默降级 |

现状 `formatVersion = 1` 是**两端唯一的契约号**（Windows `PACKAGE_FORMAT_VERSION`；Android `SUPPORTED_FORMAT_VERSION = 1`），没有独立的 schema 版本（全仓 `schemaVersion` 零命中；Android 侧的 `SUPPORTED_SCHEMA_VERSION = 2` 是它自己的变体，不是本项目的契约号）。

### 6.6 父级回退不得计入齐全（冻结）

Android 的 `MapTileApproximater` 会用 `z-1` 的瓦片裁切放大充数（`MapTileApproximater.java:approximateTileFromLowerZoom` L178-257），这是**显示级**降级。导出侧的"齐全"判定必须只看目标级坐标存在性，否则会产出"宣称 Z16 齐全、实际全是 Z14 放大"的包 —— 而用户要 Z16 就是为了看得更细。

---

## 7. 预算契约

### 7.1 四类口径（互相不可替代）

| 口径 | 定义 | 什么时候需要 | 现状 |
| --- | --- | --- | --- |
| `repository_bytes` | 仓库长期占用 = Σ`contents.size` + 索引 + WAL 峰值 | 选仓库位置、决定是否要扩容 | **不存在** |
| `delivery_bytes` | 单个交付包的最终大小 = 导出后 `map.mbtiles` 实际字节 | 传输、设备容量 | 有（`ItemPrediction.final_bytes`） |
| `peak_process_bytes` | Windows 处理峰值 = 快照读 + 编码缓冲 + staging 副本 | 导出时磁盘/内存告警 | **不存在**（只算最终） |
| `device_install_bytes` | Android 安装/更新所需预留 | 传输前判断能否装下 | 部分（Android 侧 `requiredSpaceFor` L534-538） |

**四者的关系不是倍数关系。** 合并注记会让 `delivery_bytes` 明显大于原始图层之和对应的体积（PNG vs 原生 JPEG 的编码差异，见架构 §3.9 的量级对比），而 `peak_process_bytes` 至少是 `2 × delivery_bytes`（staging + final 并存）。

### 7.2 口径统一

- **一律 GiB = 1024³**，界面与报告文案写 `GiB`。
- 现状不一致：预算侧常量是 `GIB = 1024**3` 且文案写 "GiB"，但展示函数 `format_size` 除以 1024 却把单位标成 `GB`（`app/geo/tile_math.py` L513-530）→ 界面上的 "GB" 实际是 GiB。**修文案，不改数值**（见架构 §11 待决 2）。
- 瓦片**张数**用 1000 进制（`format_tile_count`，注释已明确"是'多少个'不是字节"），不要与字节混用。

### 7.3 必须覆盖的项

| 项 | 为什么 |
| --- | --- |
| 双模式（vector / image） | 同区域体积差一个量级，不能取平均 |
| 原始四图层 | 仓库要四份；交付只要一份（合成后） |
| 合成输出 | 合成后编码变大（PNG） |
| 索引与 WAL | 分片主键 + `contents` 索引；WAL 峰值可达数 MB/分片 |
| 暂存与副本 | staging 与 final 并存期间的峰值 |
| 覆盖快照文件 | `.coverage/*.json` 的实际字节（现状已计入，`storage_budget.py` L355-361） |

### 7.4 估算值 vs 实际值（必须有字段区分）

```
Estimate: { value_bytes, uncertainty, source }   # source='sample' | 'model'
Actual:   { value_bytes, measured_at }
```

- 估算带 `uncertainty`（现状 `uncertainty = max(1.3|1.8, min(3, spread))`，`storage_budget.py` L340-348）。
- 导出完成后必须回报**实际值**，并与估算并列展示。**不允许**只显示估算就当作"用掉了多少"。

### 7.5 严格预算语义

| 状态 | 触发 | 行为 |
| --- | --- | --- |
| `ok` | 估算上界 ≤ 预算 且 磁盘 ≥ required | 继续 |
| `warn` | 估算下界 > 预算 但上界 ≤ 预算 × 容差 | 提示，需确认 |
| `over` | 估算**上界** > 预算 | **暂停**，要求用户改范围或改预算 |
| `insufficient_disk` | 磁盘 free < required | 暂停；给出"需要多少、可释放多少" |
| `infeasible` | 即使把 zoom 降到最低可行值仍超预算 | **明确报"不可行"** + 最低可行覆盖建议 |

**"不可行"不许静默降级**：不能"悄悄少下几级"然后报告成功。用户要的是"Z4~13 全国"，砍到 Z4~10 必须由用户看见并同意。

### 7.6 超限与回退

- 导出中途实际字节超上界 → 继续写完（半途中止会留下不完整包），但报告标 `over_actual`，且**不自动发布**，由用户决定发布或删除 staging。
- 预算失败**不回滚**已下载/已入仓的数据（那是资产，不是垃圾）。
- 最终验收：`实际交付字节` 与 `实际仓库增量` 都要出现在报告里，两者相加不等于磁盘总变化（因为有暂存与 WAL）。

---

## 8. 几何覆盖契约

### 8.1 行政层级与 Zoom 是预设，不是约束

`RegionLevel` 的推荐 zoom 区间（`app/planning/zoom_policy.py:DEFAULT_ZOOM_POLICIES` L66-136）是**建议值**，用户可以改。仓库与导出**不得**把"县级就该 Z14~18"当成校验规则；越界只提示。

### 8.2 高精度有效范围

```
有效范围 = 允许下载边界 ∩ (任务区 ∪ 航线缓冲区)
```

- **不是** `行政边界 ∪ 任务区`。后者会把整个县都变成高精度，这是本项目最容易犯的错，也是"下了 200GB 却只用了一小块"的常见成因。
- 多区域是**并集**（union），孔洞保留（Shapely `intersects` 语义已正确处理内环；但没有显式孔洞测试，Slice 45 要补一条）。
- 缓冲单位：**米**，AEQD 局部平面近似（现状 `mission/region_builder.py:build_geometry` L241-280），跨度 > 10° 拒绝（`_check_span` L364-381）。这个近似在 1100 km 量级内的误差未测量 → 标注为假设。

### 8.3 瓦片级规则

| 规则 | 说明 |
| --- | --- |
| 保留整张相交瓦片 | 不声称像素级裁切；边界瓦片会带区域外的内容 |
| 边界判定用 `intersects` | 不用中心点（会漏块），现状已是此选择 |
| 瓦片号区间用 `calculate_tile_range` | 边界落线时会多纳一行/列（保守），文档与报告要承认"可能略超" |
| 跨边界不特殊处理 | 一张瓦片要么全要要么全不要 |

---

## 9. Android 与增量交付兼容

### 9.1 可直接复用的能力（不要重写）

| 能力 | Android 现状 | 复用方式 |
| --- | --- | --- |
| 多包共存 | `OfflineMapStorage.listInstalledDirectories` + `OfflineMapRegistry.refresh` | 不改 |
| 父级回退 | `MapTileApproximater` | 不改（但导出侧不得据它算齐全） |
| AUTO 选包 | `MapSelectionPolicy`（specificity 排序 + 迟滞） | 导出必须让 `region.level` 正确，否则选包排序错 |
| 包配额/保留 | `OfflineStorageMaintenance`（16 GiB / 512 MiB / 30 天） | 导出侧要按配额提示"这次会占掉多少" |
| 增量更新 | `OfflinePackageUpdater` + `delta.json` v1 | **复用协议**，Windows 补生成器 |
| normalized 读取 | `MBTilesFileArchive` 只查 `tiles`（VIEW 透明） | 导出必须建 `tiles` 视图（若用 normalized） |

### 9.2 四种身份（不要混）

| 身份 | 载体 | 变它 |
| --- | --- | --- |
| 资产身份 | 仓库内部（本设计新增） | 不影响交付 |
| 覆盖策略身份 `DeliveryIdentity` | `mapType / labels / composition / tileScheme / tileFormat / zoom / bounds / region` | **必须整包替换** |
| 交付包 ID | `make_package_id(name, date)` → 目录名 | 影响已装包识别 |
| 协议版本 | `formatVersion = 1` | 影响客户端接受与否 |

### 9.3 可增量 vs 必须整包

**Android 已经在代码里强制了这条边界**（`OfflinePackageUpdater.apply` L50-62），Windows 侧只要对齐：

| 变化 | 可增量 | 依据 |
| --- | --- | --- |
| 瓦片内容变化（同坐标） | ✅ | 走 `patch.mbtiles` 的 `tiles` 表 |
| 瓦片被删除 | ✅ | 走 `deleted_tiles` 表 |
| `mapType` / `label` / `composition` / `tileScheme` / `tileFormat` | ❌ | Updater L50-54 |
| zoom 区间 | ❌ | 同上 |
| `region.id` / `region.boundaryVersion` | ❌ | Updater L60-62 |
| `formatVersion` | ❌ | 协议层 |
| `provider` | ⚠️ 需确认 | delta 生成器把它算进 identity（`create_offline_delta.py` L29-35），Updater 未单独校验 |

### 9.4 增量协议字段（冻结，已实现）

```
delta.json
  deltaVersion      1
  updateId          ^[a-z0-9][a-z0-9_-]{0,63}$   同 ID 重复安装会被拒
  baseSha256        64 hex   基础包 map.mbtiles 摘要
  baseContentSha256 64 hex   基础包内容摘要（与页布局无关）
  targetContentSha256 64 hex 目标包内容摘要
  patchSha256       64 hex   patch.mbtiles 摘要
  patchSize         int      字节

patch.mbtiles
  tiles(z, x, tile_row(TMS), tile_data)          新增/变更
  deleted_tiles(z, x, tile_row(TMS))             删除
```

内容摘要 `contentSha256` 的定义（Android 已实现，`OfflinePackageUpdater.contentSha256` L123-135）：按 `(z,x,y_tms)` 升序遍历，把 `f"{z}/{x}/{y}:"` 与每张瓦片的 `sha256(data)` 依次喂进一个 `sha256()`。**它不依赖页布局**，所以 flat 与 normalized 两种布局的同内容包摘要相同。

### 9.5 真实应用仍需接入的部分（对 Android 的待办，本次不改）

1. 若采用"一个包 = 一张合成图"之外的分层交付，Android 需要有分层显示模型 —— **当前没有**（`OfflineTileProvider` 是单链 provider）。
2. `contentId`（新增可选字段）需要 Android 侧在管理页展示，否则用户仍无法区分同名同日期不同内容的包。
3. 增量包的 `provider` 变更语义未在 Updater 里校验，需两端对齐。

---

## 10. 契约测试清单（Slice 42 起逐条落地）

| # | 断言 | 类型 |
| --- | --- | --- |
| C1 | 同坐标同摘要写入两次 → 第二次 `Unchanged`，`coords` 仍一行 | 幂等 |
| C2 | 同坐标不同摘要、未授权 → `Conflict`，旧内容不变 | 冲突 |
| C3 | 授权替换后旧行 `superseded`、`contents` 两行都在 | 版本 |
| C4 | 快照冻结两次（同 selector、同数据）→ 同 fingerprint | 可复现 |
| C5 | 快照引用的内容被删 → `read_snapshot` 抛 `SnapshotExpired` | 快照 |
| C6 | 导入后源文件 mtime 与大小不变 | 只读 |
| C7 | 导入中断再跑 → 无重复行，`already_present` 递增 | 幂等 |
| C8 | `HAS WAL` 的源库 → 需确认才继续 | 安全 |
| C9 | 导出后抽样回读 `y` 与写入 `y` 互为翻转 | 一次翻转 |
| C10 | `reject_if_missing` + 缺口非空 → 不产生 staging 目录 | 策略 |
| C11 | `local_only` + 缺口 → 照常发布，报告含缺口 | 策略 |
| C12 | 发布失败 → final 不动、`.building` 已清 | 原子性 |
| C13 | 两个进程写同一分片 → 第二个 `Busy`（不写坏） | 并发 |
| C14 | 删一个分片文件的 `contents` 行 → `health_check` 报 `Corrupt` | 恢复 |
| C15 | `prune(dry_run=True)` 不删任何东西 | 安全 |
| C16 | manifest 加 `contentId` 后，Android 侧契约测试仍通过 | 跨端 |
| C17 | delta 生成的包经 Android 契约测试（双端元组一致） | 跨端 |
