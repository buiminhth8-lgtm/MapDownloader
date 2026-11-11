# 统一瓦片仓库：使用与验收（Slice 42 落地）

这份文档回答一个问题：**仓库层现在到底能干什么、边界在哪、坏了怎么修。**

它是 [tile_repository_architecture](tile_repository_architecture.md)（为什么这么设计）与
[tile_repository_contracts](tile_repository_contracts.md)（字段与接口冻结约定）的
**落地补充**：设计里"打算这么做"的，这里写"实际这么做"；两者不一致的地方在
§12 逐条列出，不藏在正文里。

**当前能力边界一句话**：仓库能存、能读、能自检、能恢复到崩溃前的状态；
它**还没有**接下载（Slice 44）、没有导出（Slice 45）、没有导入旧包（Slice 43）、
没有 UI。打开开关也不会改变任何现有行为（`TILE_REPO_ENABLED` 默认 `False`）。

---

## 0. 三分钟上手

```python
from app.tilerepo import TileRepository

with TileRepository.open(r"D:\maps\tile_repo", create=True) as repo:
    repo.ensure_asset("tianditu/vec_w")                       # 先登记资产身份
    report = repo.put_tiles("tianditu/vec_w", [
        (12, 3412, 1567, png_bytes),                          # (z, x, y, 字节)  XYZ 顺序
        (12, 3412, 1568, other_png),
    ])
    print(report.written, report.conflicts, report.published)  # 2 [] True

    lookup = repo.read_tile("tianditu/vec_w", 12, 3412, 1567)
    print(lookup.status, len(lookup.record.data))             # present 12345
    # lookup.record.data 就是原始 PNG 字节，由调用方决定写到哪（Slice 45 写进 mbtiles）

    print("\n".join(repo.stats().as_lines()))
    print("\n".join(repo.health_check().as_lines()))
```

命令行（调试用，不是产品入口。尖括号是占位符，换成你自己的路径）：

```bash
python -m app.tilerepo.cli --root D:\maps\tile_repo init
python -m app.tilerepo.cli --root D:\maps\tile_repo put  --asset tianditu/vec_w --z 12 --x 3412 --y 1567 --file <一张 256x256 PNG 的路径>
python -m app.tilerepo.cli --root D:\maps\tile_repo read --asset tianditu/vec_w --z 12 --x 3412 --y 1567 --out <输出文件路径>
python -m app.tilerepo.cli --root D:\maps\tile_repo health --full --json
python -m app.tilerepo.cli --root D:\maps\tile_repo stats
```

---

## 1. 磁盘结构与版本号

```
<repo_root>/                     # 默认 <DEFAULT_OUTPUT_DIR 的父目录>/tile_repo
├── catalog.sqlite               # 中央索引（可重建；不含逐瓦片行）
├── shards/
│   └── tianditu/EPSG_3857/256/vec_w/z00-06.sqlite
│            │        │      │      └── 分片带名 z<zmin 两位>-<zmax 两位>
│            │        │      └── 瓦片边长
│            │        └── 投影
│            └── source_id
├── snapshots/<fingerprint>.json # 冻结清单（一行一条，可流式读）
├── repair/                      # 隔离/修复产物（本 Slice 建目录，未使用）
├── staging/                     # 导出暂存（Slice 45 用；本 Slice 只做残留检测）
└── lock/tianditu-vec_w-z00-06.lock   # 写租约（心跳 = 文件 mtime）
```

三个**互相独立**的版本号，别混：

| 版本 | 存放位置 | 当前值 | 不匹配时的行为 |
| --- | --- | --- | --- |
| 分片 schema | 每个分片 `shard_meta.schema_version` | `1` | 打开/写入时抛 `SchemaUnsupported`，**不迁移、不重建** |
| catalog schema | `catalog_meta.schema_version` | `1` | 打开时抛 `SchemaUnsupported`，文件原样保留 |
| 快照格式 | 快照文件头 `version` | `1` | 读时抛 `Corrupt` |

分片带（`zoom_bands` 表，默认值见 `settings.TILE_REPO_ZOOM_BANDS`）：
`0-6 / 7-11 / 12-14 / 15-18`。**改 band 等于改分片文件名**，需要重建（本 Slice 不提供迁移）。

---

## 2. 三类身份：谁、哪张、哪份字节

| 身份 | 类型 | 例子 | 进入文件名？ | 进入日志/报告？ |
| --- | --- | --- | --- | --- |
| 资产 | `AssetId(source_id, layer)` | `tianditu/vec_w` | 是（分片路径） | 是 |
| 坐标 | `CoordKey(z, x, y)` | `(12, 3412, 1567)`，**XYZ** | 否 | 是 |
| 内容 | `ContentDigest(sha256, size, encoding)` | `sha256:…` | 否 | 只记前 16 位十六进制 |

三条硬规则：

1. **Token 不进任何身份**。`AssetId.parse("tianditu/vec_w?tk=abc")` 直接抛
   `ValueError`；分片路径只由 `source_id / projection / tile_size / layer / band` 决定。
2. **底图与注记永不互相覆盖**。`vec_w` 与 `cva_w` 是不同资产 → 不同分片文件；
   同坐标写两层只会得到两个文件，不是"后写的盖掉前面的"。
3. **只有 `RAW_*` 的身份由仓库登记**；`classify_asset_kind(metadata)` 判的是
   **旧包**能不能当原始层复用（契约 §3.2），它永不返回 `RAW_*` —— 因为 metadata
   无法证明"这份图没被叠过字"。

### 2.1 状态机（`coords.state`）

```
(无) ──put──▶ current(0) ──授权替换──▶ superseded(1)
                 │  ▲                      │
      深检判坏  │  │ 修复                  │ prune（Slice 49）
                 ▼  │                      ▼
          quarantined(2)                (删除)
```

**同一坐标只保留一代历史**（`current` + 至多一行 `superseded`）。这不是省事，是
主键 `(z, x, y, state)` 的直接结果：第二次替换时若直接
`UPDATE ... state=1`，会撞 `UNIQUE constraint failed: coords.z,x,y,state`。
落地实现是"替换前先删掉上一代 superseded 行"，于是：

- `iter_versions()` 最多返回 2 条；
- 被丢掉的那一代的字节会变成 `prune()` 报告里的孤儿内容（**报告不删除**）；
- 需要"多代历史"就得改主键（`+ epoch_id`），那是另一次 schema 变更，不由本 Slice 偷偷做。

---

## 3. 写入语义

| 场景 | 结果 | 字段 |
| --- | --- | --- |
| 新坐标 | 插入 current | `written += 1` |
| 同坐标、同摘要 | 只刷新 `fetched_at` | `unchanged += 1` |
| 同坐标、不同摘要、未授权 | **不写**，记一条冲突 | `conflicts += [{coord, old, new, epoch}]` |
| 同坐标、不同摘要、`allow_replace=True` | 旧行 → superseded，新行 current | `replaced += 1` |
| 非法坐标 / 非 bytes / 空 / magic 不对 / 尺寸不符 | **不落盘**，记一条拒绝 | `rejected += [{coord, reason, code}]` |
| 同一批里重复坐标 | 以**最后一次**为准 | `duplicates_dropped += 1` |

配套约束：

- **校验**：写入前做快检（PNG/JPEG magic + 头部宽高 == `tile_size`）。`deep=True`
  时再走 `app.providers.image_validation.verify_tile_image`（完整解码，导入验收才开）。
- **摘要冲突**：`contents` 里已有同 sha256 但字节不同 / 长度不同 → 抛 `Corrupt`，
  该批事务回滚。摘要命中时**仍然逐字节比对**（撞车不是概率问题，是 bug）。
- **去重范围 = 单个分片内**。跨分片同内容会各存一份 —— 分片是独立的数据库文件，
  跨文件去重没有便宜的实现，也不该在写入路径上做跨库 JOIN。
- **失败/取消不留半成品**：每批一个 `BEGIN IMMEDIATE` 事务；异常 → `ROLLBACK`。
  取消（`should_stop`）**不回滚已提交的批次**（契约 §5），报告里 `cancelled=True`
  且照常发布已提交的部分。
- **发布水位**：`shards.published_epoch`。读默认只认 `epoch_id <= 水位`，
  于是"分片已提交、catalog 没发布"的行会被明确标成 `UNPUBLISHED`，
  既不会被当成资产，也不会被静默丢弃（见 §5）。

`PutReport` 的 `accepted = written + replaced + unchanged`——被拒绝和被冲突的不算。

---

## 4. 读取与遍历

| 方法 | 用途 | 关键语义 |
| --- | --- | --- |
| `read_tile(asset, z, x, y)` | 单张 | 不存在返回 `ABSENT`（**不抛异常**）；损坏 `CORRUPT`；未发布 `UNPUBLISHED`；隔离 `QUARANTINED` |
| `read_tile(..., epoch=E)` | 指定版本 | 只匹配那一个 epoch；历史版本也能读 |
| `iter_tiles(asset, with_data=…)` | 流式遍历 | 按 band 升序、每分片 `ORDER BY z,x,y` 吃主键、`fetchmany(1000)`，**不整级载入** |
| `iter_tiles(..., should_stop=…)` | 可取消 | 返回 True 即正常结束并释放连接 |
| `iter_versions(asset, z, x, y)` | 历史 | `(state, epoch, digest, fetched_at)`，按 epoch 升序，至多 2 条 |
| `query_existence(assets, (z0,z1), bounds, region_filter=…)` | 缺口 | `missing` 只数**目标级**缺口；父级回退不计入 present（契约 §6.6） |

`query_existence` 的样例（`missing_sample`）是**按列扫、有上限**的：扫到
`max_sample_columns` 列就停，并置 `sample_truncated=True`。报告里必须如实标注，
否则"样例里没有"会被读成"那里不缺"。

---

## 5. 可见性与"已提交未发布"

```
put_tiles → ① 写分片事务提交   ② catalog 事务：统计 + coverage + 水位 + 操作状态
                        └── 崩在这里 → 行在磁盘上，但 epoch > 水位 → UNPUBLISHED
```

- 普通读取/遍历看不到 `UNPUBLISHED` 的行（那不算资产）；
- `read_tile(..., include_unpublished=True)` 是**维护开关**，只给恢复/验收用；
- `recover()` 会把它们补发布（不是删掉）。删掉等于"静默丢弃已经写进磁盘的写入"。

---

## 6. 快照（稳定读取视图）

```python
meta = repo.freeze_snapshot(assets=["tianditu/vec_w"], zoom_range=(5, 12))
with repo.open_snapshot(meta.fingerprint) as view:
    for record in view:            # 逐条流式读，record.data 是冻结那一刻的字节
        ...
```

语义：

1. **跨分片一致性靠逻辑水位**：取 `E = max_epoch`，逐分片读
   `state=current AND epoch_id <= min(分片发布水位, E)`，把
   `(asset, z, x, y, sha256, size)` 写进清单。**不是**"开一个 SQLite 连接当快照"——
   一个连接只能给一个文件的一致性。
2. **冻结后按 sha256 读内容**，所以之后发生的替换、下架都不影响这份视图。
3. **内容被回收 → 抛 `SnapshotExpired`**，绝不静默跳过（跳过会产出"少几张但报告说齐全"的包）。
4. **指纹可复现**：`entriesDigest = sha256(逐条 JSON 行的字节流)`，
   `fingerprint = sha256(canonical({version, epoch, selector, entriesDigest, entryCount}))`。
   同一份数据两次冻结 → 同一个指纹，且第二次**不重写文件**。
5. **取消不留半份**：先写临时文件，取消/异常即删除；发布用 `os.replace`。

限制（明确写在这里，避免被误用）：

- 冻结过程**不加写锁**：它读的是"发出冻结那一刻的水位"，期间新发布的数据不会进来，
  但也不保证与某个具体时刻的全局状态字节级一致（要更强就得让写入停摆，代价更大）。
- 清单里不记 `state`：冻结之后变成 `superseded` 的行仍在清单里（契约 §1.5）。
- 清单**不删内容**：内容是否还在由读取时逐条判定。

---

## 7. 并发与资源生命周期

| 层级 | 机制 | 边界 |
| --- | --- | --- |
| 进程内 · 同一分片写入 | `threading.RLock`（`shard.shard_write_lock`） | 串行化，避免两个线程同时 `PRAGMA journal_mode=WAL` / `BEGIN` |
| 进程内 · catalog 写入 | SQLite 写者锁 + `busy_timeout=5000ms` | 不加文件租约（多一层锁就多一种不一致） |
| 跨进程 · 同一分片写入 | 文件租约 `lock/<source>-<layer>-zNN-NN.lock` | 抢不到抛 `Busy`（带 `holder.pid/host/label`），**不排队** |
| 跨进程 · 不同分片 | 各写各的 | 天然并行 |
| 惰性连接 | 每个分片 + catalog 各留一个**空闲常驻连接** | 只用于阻止 `-wal`/`-shm` 被"最后一个关闭者"拆掉（见 §11.1） |

租约判定：

- 心跳 = 文件 mtime，`TILE_REPO_LEASE_HEARTBEAT_SECONDS=15`、`TIMEOUT=90`；
- **持有者进程已死则立即接管**（同主机用 pid 判活），不必干等 90 秒 ——
  否则用户重启程序后还要白等一整个超时；
- 接管走 `*.takeover` 二次互斥，避免两个进程同时"抢同一个死锁"都以为自己拿到了。

`close()` 做什么：释放本实例持有的全部租约、关闭全部常驻连接、关闭 catalog。
之后任何操作抛 `RepoClosed`；`close()` 幂等。**仓库开着时，Windows 不允许外部删除
分片文件**（句柄还在），这既是行为也是保护：运行期间数据不会被别的进程拽走。

---

## 8. 崩溃恢复与索引重建

### 8.1 四个阶段与恢复动作

| 分片里有没有该 epoch 的行 | 说明 | `recover()` 动作 |
| --- | --- | --- |
| 没有 | 崩在写分片之前/事务内（已回滚） | 标 `aborted` |
| 有、catalog 没发布 | 崩在"分片提交后、发布前" | 补发布 + `done` |
| 有、catalog 已发布 | 崩在"发布后、打 done 前" | 只补 `done` |
| 有、但操作是 cancelled | 取消时已提交的批次（契约：不回滚） | 补发布 + `cancelled` |

`recover()` **幂等**：第二次运行 `changed == 0`（第一次处理过的已是终态）。

### 8.2 恢复步骤（真实故障处置顺序）

```bash
python -m app.tilerepo.cli --root <repo> health            # 1. 先看，不改
python -m app.tilerepo.cli --root <repo> recover           # 2. 接管未完成的操作
python -m app.tilerepo.cli --root <repo> rebuild           # 3. catalog 落后/丢失时重建登记
python -m app.tilerepo.cli --root <repo> health --full     # 4. 复算摘要、统计孤立内容
```

`rebuild_index()` 能从分片**重建**的信息（都在 `shard_meta` 里）：
资产身份、投影、瓦片尺寸、zoom 带、行数、覆盖矩形、最大 epoch（→ 水位）。

**不可重建**的信息（报告 `notes` 里会明说）：操作历史（哪次写入属于哪个用户动作）、
原先水位的演进过程、快照与 epoch 的对应关系（只能取文件头里记的）。

`prune(dry_run=True)` 只报告孤立内容与过期历史；`dry_run=False` 抛
`UnsupportedOperation` —— 自动回收会与"用户正要导出"撞车（契约 §7.5 前置条件在 Slice 49）。

---

## 9. 容量与状态统计口径

`repo.stats()` 一行一个资产：

| 字段 | 定义 | 容易搞错的地方 |
| --- | --- | --- |
| `current` | 可见坐标数（`state=current` 且 `epoch_id <= 水位`） | 不含未发布行 |
| `superseded` / `quarantined` | 历史行 / 隔离行 | 每坐标至多 1 条 superseded |
| `contents` | **去重后的内容实体数** | 不是坐标数 |
| `logical_bytes` | 可见坐标按引用次数累加的字节数 | 同一内容被 20 个坐标引用 = 20 份 |
| `dedup_bytes` | `contents` 里实际 blob 的字节和 | 这才是"内容占用" |
| `shard_bytes` / `wal_bytes` / `shm_bytes` | sqlite 主文件 / WAL / 共享内存 | |
| `physical_bytes` | 上面三者之和 | 磁盘上真实占用（不含文件系统块对齐） |

`RepoStats` 另给 `catalog_bytes` / `snapshot_bytes` / `staging_bytes`，以及
`notes`（例如"有 N 张已提交但未发布"）。

**去重范围 = 分片内**，所以 `logical_bytes - dedup_bytes` 只代表**单个分片内**
被重复引用省下的量，跨分片同内容不计入。

---

## 10. 持久性承诺（写清楚，别含糊）

| 数据库 | journal | synchronous | 进程崩溃（`os._exit`/任务管理器） | 断电/蓝屏 |
| --- | --- | --- | --- | --- |
| 分片 | WAL | `NORMAL` | **已提交事务不丢** | 可能丢最后几个已提交事务 |
| catalog | WAL | `FULL` | 不丢 | 不丢 |

理由：分片是**大块可重下的数据**，`FULL` 会让每次提交多一次 fsync；
catalog 是**权威记账**（epoch、水位、操作日志），多花一次 fsync 换"崩了也知道崩到哪"。

不承诺的事：不做跨库两阶段提交（SQLite 没有），所以"catalog 与分片"之间只保证
**最终一致**（靠发布水位 + `recover()`），不保证同一瞬间一致。

---

## 11. 已知限制（踩过才知道的那些）

### 11.1 Windows：`-wal`/`-shm` 会被"最后一个关闭者"删掉

一边频繁开关连接、一边在写，实测会偶发
`sqlite3.OperationalError: attempt to write a readonly database`
（与权限无关，是清理竞态）。本机压测 300 轮写 + 并发读 churn：不留常驻连接 **6/300 失败**，
留一个空闲连接 **0/300**。所以 `Catalog` 与每个分片都留一个**不做任何查询**的常驻连接。

副作用：仓库开着时外部删不掉分片文件（见 §7）。真实运维里"分片被删"只能发生在
仓库关闭之后 —— 这也让 `health_check` 的 `missing_shard` 只可能是"上次运行留下的状态"。

### 11.2 判活只看 pid

`lease_holder_alive` 用 `OpenProcess` + `GetExitCodeProcess`（Windows）/
`os.kill(pid, 0)`（POSIX）。**绝不能**在 Windows 用 `os.kill(pid, 0)`：CPython 会把它
映射成 `TerminateProcess(handle, 0)`，真的把别人的写入者杀掉。pid 复用只会导致
"误判成活着"→ 退化成等心跳超时，方向是安全的。

### 11.3 SQLite 没有跨库原子提交

分片与 catalog 是两个文件。写入顺序固定为"先分片、后 catalog"，中断只会让
catalog 落后，不会丢数据；反过来（先 catalog）会造出"登记了但没数据"的假资产。

### 11.4 历史只有一代

见 §2.1。需要多代就得改主键。

### 11.5 查询计划依赖主键顺序

`coords` 主键 `(z, x, y, state)`，所以 `WHERE z=? AND x=? AND y=?`、
`ORDER BY z,x,y` 都能走主键。测试里用 `EXPLAIN QUERY PLAN` 钉住这一条 ——
后面谁加`ORDER BY y,x` 都会被抓出来。

---

## 12. 与 Slice 41 设计的落地对照

| # | 设计（Slice 41） | 落地（Slice 42） | 原因 |
| --- | --- | --- | --- |
| 1 | catalog 级用文件租约（契约 §2.4） | 只用 SQLite 写者锁 + `busy_timeout` | SQLite 写者锁本身跨进程互斥；再加一层会出现"两个锁不一致"的新故障模式 |
| 2 | 版本 = current + 一世代 superseded（D8） | 一致，且**明确**"再替换丢弃更早的一代" | 主键 `(z,x,y,state)` 决定的，不是实现偷懒 |
| 3 | `fingerprint = sha256(canonical(payload))`，payload 含 entries | `entriesDigest = sha256(逐条 JSON 行)`，payload 只含 `entriesDigest` | 百万级 entries 全塞内存算一次摘要 = 导出前先 OOM；语义（同数据同指纹）不变 |
| 4 | `read_snapshot` 名字 | `open_snapshot()` 返回 `SnapshotView`（可迭代、需 `close()`） | 显式生命周期，避免"忘了关连接" |
| 5 | `verify_asset` | `health_check(scope='quick'/'full')` + `read_tile(verify=True)` | 断言式接口对"只读诊断"太强；健康检查要能一次报出全部问题 |
| 6 | 发布靠"catalog 与分片一致" | 新增 **`shards.published_epoch` 发布水位** | 让"已提交未发布"变成显式状态，而不是靠"有没有登记"猜 |
| 7 | 错误分类 | `app/tilerepo/errors.py` 十一个类型（`Busy`/`Corrupt`/`Conflict`/`NotFound`/`SnapshotExpired`/`SchemaUnsupported`/`Cancelled`/`InvalidTile`/`RepoClosed`/`UnsupportedOperation`/`Insufficient*`） | 调用方要能按类型决定"重试还是隔离" |
| 8 | 租约"90 秒无心跳可接管" | 另加"**持有者进程已死则立即接管**" | 崩溃用户不该白等 90 秒 |
| 9 | `prune(dry_run)` | 一致，`dry_run=False` 抛 `UnsupportedOperation` | 自动回收要等 Slice 49 的前置条件（无活跃读快照） |
| 10 | CLI 调试入口 | `python -m app.tilerepo.cli`（10 个子命令，`--json`） | 无 UI 时也要能验 |

---

## 13. 与 43 / 44 / 45 的接口约定

**给 Slice 43（旧包导入）**

- 只读源文件：导入侧自己开 `MBTilesReader`，仓库只接受 `put_tiles` 的字节流；
- 先 `classify_asset_kind(metadata)`（已有实现）决定是否允许当原始层；
- 批量导入用 `put_tiles(..., deep=True)` + `progress=` 回调；中途取消用 `should_stop`；
- **不要**在导入时构造 `epoch_id`（由仓库分配），也不要绕过 `put_tiles` 直接写分片。

**给 Slice 44（下载/修复接入）**

- 抓到的字节先过 `validate_tile_bytes`（`put_tiles` 内部已做，重复校验无副作用）；
- 同坐标不同内容默认 `Conflict` → 更新任务要显式 `allow_replace=True`；
- 修坏瓦片：`read_tile(verify=True)` 判 `CORRUPT` → 重下 → `put_tiles(allow_replace=True)`；
- 跨进程并发写同一分片要处理 `Busy`（提示"稍后重试"，**不要**当损坏）。

**给 Slice 45（导出/打包）**

- 只从 `open_snapshot()` 的清单读（契约 §1.5 不变式 2：不得顺手读别的坐标）；
- 读到 `SnapshotExpired` → 中止并重新冻结，**不许跳过那几张**；
- 缺口用 `query_existence()`，父级回退不计入 present；
- 暂存目录用 `repo.staging_dir`（`repair/` 留给隔离产物）。

---

## 14. 验收怎么做

| 入口 | 覆盖 | 耗时 |
| --- | --- | --- |
| `pytest tests/test_tilerepo_*.py` | 8 个文件、156 条用例（含真子进程崩溃、并发写、1.2 万张批量） | ~21s |
| `pytest`（全量） | 回归：现有 2790 条 + 新增，共 2946 条 | ~3min |
| `python scripts/verify_tile_repository.py` | 101 项端到端断言（临时目录、不联网、机器可读报告） | ~6s |
| `python scripts/verify_tile_repository.py --quick` | 跳过子进程场景（无进程权限时用） | ~5s |

验收脚本自己会声明"做了什么、没做什么"：

```
结论：PASS　执行 101 项（通过 101 / 失败 0 / 跳过 0）　用时 6.08s
进程中断验收：已执行（阶段：crash.after_operation, …, crash.during_shard_write）
索引重建验收：已执行（catalog 丢失后重建：是；重建幂等：是）
联网：未使用（全部为合成瓦片 + 临时目录）
```

`--report path.json` 里的 `process_interruption` / `index_rebuild` 两个布尔块就是
"本次到底有没有真的做进程中断与重建验收"的机器可读答案（跳过时是 `false`，
不会把"没跑"写成"通过"）。

**不做的事**：脚本和用例都不联网、不读 `output/`、不碰任何已有地图文件；
`TILE_REPO_ENABLED` 保持 `False`。

---

## 15. 未实现能力（本 Slice 明确不做）

| 能力 | 归属 |
| --- | --- |
| 旧 MBTiles 批量导入、`HAS WAL` 源库确认、源文件只读校验 | Slice 43 |
| 下载/续传/修复接入仓库，`Busy` 重试策略 | Slice 44 |
| 导出、合成、压缩、打包、staging 原子发布 | Slice 45 |
| 严格预算（四类口径）、自动方案 | Slice 46 |
| 分级资源与任务模板 | Slice 47 |
| 版本演进与增量交付（`contentId` / delta） | Slice 48 |
| `prune` 真正删除、superseded 过期回收、隔离区修复流程 | Slice 49 |
| UI 页面、全国预下载、Android 侧任何改动 | 不在本 Slice 范围 |
| ETag / Last-Modified 缓存表 | 契约 §1.6，随更新任务（Slice 48） |

---

## 16. 相关文档

- 为什么这么设计、现状证据：[tile_repository_architecture](tile_repository_architecture.md)
- 字段与接口冻结约定、契约测试 C1~C17：[tile_repository_contracts](tile_repository_contracts.md)
- Slice 42~49 计划与验收矩阵：[tile_repository_roadmap](tile_repository_roadmap.md)
- 交付包字段（仓库的下游）：[map_package_v1](map_package_v1.md)
- 现有容量口径（仓库的上游参照）：[storage_budget](storage_budget.md)
