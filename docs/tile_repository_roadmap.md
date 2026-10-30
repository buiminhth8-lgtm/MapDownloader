# 统一瓦片仓库：实施路线（42 ~ 49）

这份文档回答一个问题：**要把仓库层建起来，接下来的八个任务各做什么、先后依赖什么、怎么验收、怎么退回去。**

> 下面的 `Slice NN` 是**计划编号**，不是进度声明。做完一项就把它的状态改成"已交付"，
> 并在这里补一行实际产出与验收数字（[文档规范](README.md#文档规范改文档前先看) 第 2 条）。

架构与取舍见 [tile_repository_architecture](tile_repository_architecture.md)，
字段与接口见 [tile_repository_contracts](tile_repository_contracts.md)。

---

## 0. 全局约束

| 约束 | 说明 |
| --- | --- |
| 一个 Slice 只做它写的事 | 后续 Slice 的能力一行都不提前实现 |
| 不破坏现有链路 | 每个 Slice 结束时 `pytest` 全绿、`e2e_smoke` 与 `hierarchical_map_e2e` 无回归 |
| 默认关闭新路径 | 新流程一律走设置开关，默认值按 §1 的表格逐 Slice 推进 |
| 不下载 | 除 Slice 44/48 的验收场景需要真实抓取外，其余用夹具；**任何 Slice 都不得在自动化测试里联网** |
| 跨仓库 | 42~47 只碰 Windows；48 需要与 Android 对齐（契约测试，不改 Android 代码）；49 只碰 Windows |
| 数据迁移 | 每项都写明"要不要迁移old数据"；默认**不迁移**，由用户显式执行导入 |

### 0.1 开关推进表

| 阶段 | 开关（示例名） | 默认值 | 生效于 |
| --- | --- | --- | --- |
| A | `tile_repo.enabled` | `false` | Slice 42 结束 |
| B | `tile_repo.dual_write` | `false` | Slice 44 结束 |
| C | `tile_repo.export_engine` | `legacy` | Slice 45 结束（可选 `repo`） |
| D | （移除开关） | — | Slice 49 |

开关是**设置项**，不是环境变量 —— 用户要能在界面里改回去。

---

## 1. 依赖关系

```
42 仓库基础 ──┬──▶ 43 旧资产入仓 ──┐
              │                     ├──▶ 45 独立打包引擎 ──▶ 46 严格预算 ──▶ 47 分级资源/模板
              └──▶ 44 下载接入仓库 ──┘                                    │
                                                                          ▼
                                                              48 版本与增量交付 ──▶ 49 维护与整体验收
```

硬依赖：

- 45 需要 42（读仓库）+ 43（有真实数据）+ 44（缺口可补）
- 46 需要 45（要知道真实导出字节才能谈峰值）
- 47 需要 43/45（要真实分层数据才能定模板）
- 48 需要 45（两个已发布的包才能 diff）
- 49 需要全部

可并行的：43 与 44 之间无依赖，可交换顺序 —— 但**建议先 43**，因为它能立刻验证"仓库读路径"是否正确，且不涉及下载（风险低）。

---

## Slice 42：统一瓦片仓库基础

**目标**：仓库能存、能读、能自检；不接下载、不接导出。

**范围**

- `app/tilerepo/` 新模块：`identity.py`（`CoordKey` / `ContentDigest` / `AssetId` 值对象与校验）、`shard.py`（band 路由、路径、DDL、租约）、`store.py`（`put_tiles` / `read_tile` / `query_existence` / `verify_asset`）、`catalog.py`（建表、登记、epoch）、`health.py`（`health_check` / `rebuild_index` / `prune(dry_run)`）
- `app/config/settings.py` 加常量：仓库根默认路径、band 表默认值、租约超时、批次大小、`schema_version`
- 一条 AST 守卫测试：`app/tilerepo` 不得 import `app.ui` / `app.package`

**不包含**

- 不导入任何旧文件（Slice 43）
- 不改下载链路（Slice 44）
- 不做导出（Slice 45）
- 不做预算（Slice 46）
- 不建 UI 页面（只给 service 接口 + 一个调试用 CLI）

**预计修改模块**：`app/tilerepo/*`（新）、`app/config/settings.py`、`tests/test_tilerepo_*.py`（新）、`pyproject.toml`（如需要，仅 ruff per-file-ignores）

**依赖**：无

**数据迁移**：无（仓库初始为空）

**风险**

| 风险 | 缓解 |
| --- | --- |
| 分片 band 选错，日后要重建 | band 存表不写死；`rebuild_index` 从第一天就有 |
| 租约在 Windows 上行为异常（文件句柄/删除语义） | 租约用"文件存在 + 心跳 mtime"，不依赖操作系统锁；专门测"进程被杀后接管" |
| `WITHOUT ROWID` + 大 BLOB 的性能反直觉 | 坐标与内容分表（架构 §6.3），并在本 Slice 用 10 万条合成数据跑一次计时 |
| 过早优化 schema | `schema_version` 写在 `shard_meta`，允许"检测到旧版本 → 拒绝服务并提示重建"，不写迁移代码 |

**验收场景**

1. 建一个空仓库 → `health_check('quick')` 通过、0 分片
2. 写入 1000 张（合成数据，`sha256` 有重复）→ `query_existence` 数字正确；`contents` 行数 < 1000（去重生效）
3. 同坐标同摘要写两次 → 第二次 `Unchanged`（契约测试 C1）
4. 同坐标不同摘要 → `Conflict`，旧值不变（C2）
5. 授权替换 → 旧行 `superseded`（C3）
6. 两个进程同时写同一分片 → 第二个 `Busy`（C13）
7. 手动删一个 `contents` 行 → `health_check` 报 `Corrupt`（C14）
8. `prune(dry_run=True)` 前后字节完全不变（C15）
9. `pytest` 全量无回归；`e2e_smoke` 6/6；`hierarchical_map_e2e` 10/10

**回退方式**：`tile_repo.enabled = false`（默认就是 false）；删仓库目录即可，不影响任何现有文件。

---

## Slice 43：已有地图资产入仓

**目标**：把现有的 `output/*.mbtiles` 只读扫描导入仓库，源文件一个字节都不动。

**范围**

- `app/asset_import/` 新模块：`scanner.py`（`scan`）、`classifier.py`（`classify` → `AssetKind`，架构 §3.9 的判定规则）、`importer.py`（`import_asset`，含水位与幂等）
- `catalog.legacy_assets` / `import_state` 表落地
- `repair/ledger.sqlite`（隔离与冲突台账）
- CLI：`scripts/import_to_tile_repo.py`（单文件 / 目录批量 / `--deep` / `--dry-run`）
- 一条 e2e 脚本：扫描 → 导入 → 对账 → 抽样回读

**不包含**

- 不下载、不修复瓦片（只标记 `quarantined`）
- 不改变旧文件（不移动、不改名、不删）
- 不做"合成资产拆层"（明确做不到，见架构 §3.9）

**预计修改模块**：`app/asset_import/*`（新）、`app/tilerepo/{catalog,store}.py`（扩展写入批次）、`scripts/import_to_tile_repo.py`（新）、`tests/test_asset_import_*.py`（新）、`docs/tile_repository_architecture.md`（回填实测数字）

**依赖**：42

**数据迁移**：**有，但是显式动作**。用户运行 CLI 或点按钮才迁；不自动、不在启动时扫描。

**风险**

| 风险 | 缓解 |
| --- | --- |
| 误判合成包为原始图层 → 后续叠出重影 | `classify` 兜底方向是 `SYNTHETIC_MERGED`（宁可错杀）；导入后打印判定结果与依据 |
| 源库有活跃写入者（`-wal` 非空）→ 导入不完整 | 检测 `-wal` → 警告 + 要求确认（契约 §4.1） |
| 大库导入时间长、内存爆 | 流式 `fetchmany`；水位每 5000 张落一次 |
| 导入把磁盘写满 | 导入前用 §7 的 `repository_bytes` 预估 + 磁盘检查；不足则拒绝 |
| 用户以为"导入 = 可以删原文件了" | 报告里明确写"源文件未改动，也未删除" |

**验收场景**

1. 对 `output/offline_map.mbtiles`（实测 1,884,770,304 B / 14,497 张）执行 `scan` → 判定 `SYNTHETIC_MERGED`、`format=png`、zoom 5~10
2. 导入后：`query_existence` 的 `present` 与源库 `count(*)` 一致；`coords` 无重复
3. **源文件 mtime 与大小导入前后完全相同**（契约测试 C6）
4. 中断（导入到一半 kill）→ 重跑 → 无重复行，`already_present` 递增（C7）
5. 有 `-wal` 的库 → 需确认才继续（C8）
6. 混入一张"PNG 头 + 坏数据"的瓦片 → 进 `quarantined`，报告计数正确
7. 同一文件导入两次 → 第二次全部 `already_present`
8. 对合成的 `normalized-v1` 包导入 → 走 view 读，结果与 flat 包一致

**回退方式**：删仓库目录 + 台账；源文件从未被改动，所以"回退"没有数据损失。

---

## Slice 44：下载与修复接入仓库

**目标**：新下载的瓦片进仓库（按图层分存，不合层）；修复按坐标清单定向重下。

**范围**

- `app/downloader/` 改为"写仓库"而不是"写交付文件"：新增 `RepositoryTileSink`（替掉 `MBTilesTileSink` 的落点，队列/单消费者模式沿用）
- `MapDownloadService` 增 `target=repo|legacy` 分支；`dual_write` 模式下两边都写
- 缺口计算改为对仓库（`compute_gap`），续传索引改为对仓库（`ExistingTileIndex` 的 `TileProbe` 换实现）
- `repair_coords(asset, coords)`：**按清单定向重下**（现状只能全量遍历顺带判定）
- 4 个图层分别入仓（`vec_w`/`cva_w`/`img_w`/`cia_w`），合成延后到导出期

**不包含**

- 不做导出（Slice 45）
- 不改合成算法（`TileComposer` 原样复用）
- 不改限速/429/重试策略（现有行为保留；只在"整批停"上加一条"已入仓的部分不回滚"的说明）

**预计修改模块**：`app/downloader/{download_manager,tile_downloader,resume}.py`、`app/services/map_download_service.py`、`app/mbtiles/writer.py`（可能拆出 sink 接口）、`app/tilerepo/store.py`、`app/ui/{download_panel,main_window}.py`（开关与提示）、`tests/test_download_to_repo*.py`（新）、`scripts/verify_*.py`

**依赖**：42（必需）、43（建议先做，便于对照）

**数据迁移**：无（新下载进仓库；旧文件已由 43 处理）

**风险**

| 风险 | 缓解 |
| --- | --- |
| 双写期间磁盘占用翻倍，用户被"莫名其妙少了一半空间" | 双写开启时预算里同时算两份，并在界面显示 |
| 续传语义变化（从"查交付文件"变"查仓库"）→ 老任务无法续 | 老任务（`tasks.json` 里没有 `repo_asset` 字段）继续走 legacy 路径，不迁移 |
| 四图层分开下载 → 请求数翻倍（原来是两层合一次 | 其实原来也是分两次请求，只是即时合成；总请求数不变，但**写入次数**翻倍 |
| 修复只能按清单 → 用户找不到"全部重下"的入口 | UI 提供"对整块区域重下"= 用 `query_existence` 生成清单再调 `repair_coords` |
| 仓库写入慢于下载 → 队列反压拖慢整体 | 沿用现有有界队列 + 单消费者；`put_tiles` 批量提交（batch ≥ 现状 200） |

**验收场景**

1. 下载一个小区域（Z12 单级、矢量带注记）到仓库 → 仓库里有 `vec_w` 与 `cva_w` **两个资产**，同一坐标各有内容
2. 同一区域再用"无注记"导出一份 → **不产生任何下载**（复用 `vec_w`）—— 这是"一次获取多次复用"的唯一硬证据
3. `dual_write` 开启 → 交付文件与仓库内容一致（抽 100 张比对 `sha256`）
4. `repair_coords` 对 10 个已知坏坐标重下 → 10 个全部 `current`，`quarantined` 归零
5. 取消下载 → 已入仓的瓦片保留（不回滚），水位正确
6. 429 冷却触发 → 整批停，已入仓部分可续
7. `pytest` 全绿；两个 e2e 无回归

**回退方式**：`tile_repo.dual_write = false` + `tile_repo.enabled = false` → 完全回到旧路径；仓库残留可保留（不占逻辑影响）或删除。

---

## Slice 45：独立离线打包引擎

**目标**：从仓库导出交付包，三种缺图策略可选，完全不联网。

**范围**

- `app/exporter/` 新模块：`planner.py`（`plan_export`）、`engine.py`（`run_export`）、`snapshot.py`（冻结/读取）、`layout.py`（XYZ→TMS、flat/normalized 两种交付布局）、`staging.py`（暂存 + 原子发布 + 失败清理）
- 合成调用 `TileComposer`（复用）
- 发布前校验链（契约 §6.3 五步）
- 缺图报告（含"父级回退可用"但**不计入齐全**）
- UI：导出对话框（区域 / 分层 zoom / 模式 / 注记 / 压缩 / 分包 / 缺图策略）
- 依赖守卫测试：`app/exporter` 不得 import `app.downloader`（禁止隐式联网）

**不包含**

- 不做预算决策（只回报数字，Slice 46 才做严格判定）
- 不做增量（Slice 48）
- 不改 manifest 字段（可选字段留到 48）
- 不做资源模板（Slice 47）

**预计修改模块**：`app/exporter/*`（新）、`app/tilerepo/{snapshot,store}.py`、`app/package/{package_builder,package_validator}.py`（复用，可能小改接口）、`app/ui/*`（新对话框）、`tests/test_exporter_*.py`（新）、`scripts/verify_slice45_export.py`

**依赖**：42、43、44

**数据迁移**：无

**风险**

| 风险 | 缓解 |
| --- | --- |
| 二次翻转（仓库 XYZ → 交付 TMS）写错 | 写完抽样回读断言（契约测试 C9）；这条断言必须有，否则只有人眼能发现 |
| 归一化布局缺 `tiles` 视图 → Android 读不到 | 导出 normalized 时强制建视图；校验链第 4 步用 Android 的查询语句回放 |
| 快照漂移（导出中途仓库被写） | `BEGIN` 读快照 + `read_snapshot` 只认 entries；内容缺失即 `SnapshotExpired` |
| 合成让交付字节远超预估 | 导出完成回报实际字节，与估算并列（契约 §7.4） |
| 部分成功被当成成功 | 任一步失败 → 不发布；`.building` 清理 |

**验收场景**

1. 从仓库导出"矢量 + 注记，Z5~10，全国" → 生成的包能通过 `package_validator` 与 `validate_mbtiles`
2. 同一仓库导出"矢量无注记" → **零下载**，交付内容是纯底图（`composition=single`、`labels=false`、`format` 允许 jpg）
3. `reject_if_missing` + 人为造缺口 → 中止，**不产生任何 staging 目录**（C10）
4. `local_only` + 缺口 → 照常发布，报告列出缺口坐标（C11）
5. 制造一次发布失败（如目标只读）→ final 不动、`.building` 已清（C12）
6. 抽样 100 张回读 → 坐标与内容都对（C9）
7. 静态检查：`grep -r "app.downloader" app/exporter/` 无命中
8. 导出前后仓库字节不变（只读语义）

**回退方式**：`tile_repo.export_engine = legacy` → 回到现有"下载即交付 + 压缩/分包"链路。

---

## Slice 46：严格容量预算与自动方案

**目标**：四类空间口径分开算；超预算能暂停、能报"不可行"、能给自动降级方案。

**范围**

- `app/planning/tile_budget.py`（新，或扩展 `storage_budget.py`）：`repository_bytes` / `delivery_bytes` / `peak_process_bytes` / `device_install_bytes`
- 预算状态机（`ok` / `warn` / `over` / `insufficient_disk` / `infeasible`）
- 自动方案：在预算内最大化覆盖（逐级降 zoom / 缩小区域），**每个方案都要显示被降了什么**
- 实际值回报：导出后回填 `actual_bytes`
- 修正 `format_size` 的单位标注（GB → GiB，数值不变）

**不包含**

- 不做资源模板与任务预设（Slice 47）
- 不改估算模型（仍用本地抽样，但补上 `DATABASE_OVERHEAD` 的实测来源）

**预计修改模块**：`app/planning/{storage_budget,tile_budget}.py`、`app/geo/tile_math.py`（`format_size` 单位）、`app/exporter/engine.py`（回报实际）、`app/ui/*`（预算面板）、`tests/test_tile_budget*.py`

**依赖**：45（需要真实导出字节）

**数据迁移**：无

**风险**

| 风险 | 缓解 |
| --- | --- |
| 四类数字互相矛盾（用户看不出该信哪个） | 界面上并列展示并各写一句定义；不做汇总成一个数 |
| 峰值估算偏保守 → 动不动就暂停 | 峰值只在"导出前"判定；给出"至少需要 X"的具体数字 |
| 自动降级被当成"偷偷少下" | 每个降级方案都必须**用户点选**，且方案卡片上写清"少哪几级/少哪块" |
| `DATABASE_OVERHEAD` 换掉导致历史预估跳变 | 先并行显示"旧系数/新系数"一轮，再切换 |

**验收场景**

1. 同一请求在三种预算下分别得到 `ok` / `over` / `infeasible`，且 `infeasible` 给出最低可行覆盖
2. 磁盘只剩 5 GiB、需求 20 GiB → `insufficient_disk`，给出"需要 20 GiB，可用 5 GiB"
3. 导出完成后报告里同时有估算值与实际值
4. 单位标注改成 GiB 后，界面数字不变
5. 自动方案：预算只够 Z4~10 时，方案显示"Z11~13 将被省略，预计省 X"

**回退方式**：预算面板可关（只提示不阻断）；`storage_budget.py` 保持原函数签名不变。

---

## Slice 47：分级资源与任务模板

**目标**：把"全国低精度 / 省市中精度 / 任务区高精度"变成可点选的一等资源，而不是每次手填 zoom。

**范围**

- `app/planning/presets.py`：资源模板（`nation_base` / `province_mid` / `mission_high`），每个含 `region_level`、分层 zoom、图层、注记、目标设备
- 模板与仓库实际覆盖联动：选模板即显示"已有 X% / 缺 Y 张 / 预计补 Z GiB"
- 一条"有效范围"规则落地：`允许边界 ∩ (任务区 ∪ 航线缓冲)`（架构 §8.2）
- 孔洞测试补齐（现状没有显式孔洞用例）

**不包含**

- 不做"自动决定模板"（让用户选）
- 不做多地市批量模板实例化（保持"一次一块地"）

**预计修改模块**：`app/planning/{presets,zoom_policy}.py`、`app/geo/zoom_coverage.py`（如需要 `union` 辅助）、`app/mission/region_builder.py`（只读复用）、`app/ui/download_panel.py`、`tests/test_presets*.py`

**依赖**：43、45（要有真实覆盖数据才能显示"已有 X%"）

**数据迁移**：无

**风险**

| 风险 | 缓解 |
| --- | --- |
| 模板硬编码 zoom，日后策略变了要改代码 | 模板值来自 `DEFAULT_ZOOM_POLICIES`，模板只引用不复制 |
| "行政边界 ∪ 任务区" 又把范围扩到整县 | 显式实现为 `intersect`，并加一条专项测试（一条航线跨县，断言下载范围不超过缓冲宽度） |
| 孔洞区域被当成实心 | 补孔洞用例（一个带内环的省界，断言内环里的瓦片不入选） |

**验收场景**

1. 选 `nation_base` → 显示仓库已有比例、缺口、预计补抓字节
2. 选 `mission_high` + 一条 20 km 航线 + 5 km 缓冲 → 下载范围宽度 ≈ 10 km 对应瓦片数，不铺满整个县
3. 带孔洞的 GeoJSON → 孔洞内瓦片不入选（新用例）
4. 模板切换不改变已有下载任务

**回退方式**：模板只是"填表助手"，关掉后回到手填 zoom（现有行为）。

---

## Slice 48：版本与增量交付

**目标**：同一个包的两次发布之间只传差异；包身份能区分内容。

**范围**

- `app/exporter/delta.py`：把 `osmdroid/scripts/create_offline_delta.py` 的能力迁进 Windows（同协议、同字段、同拒绝规则），并补 `contentId`
- manifest 新增**可选** `contentId`（`sha256(map.mbtiles)` 前 16 位），**不升** `formatVersion`
- 增量前置校验：`DeliveryIdentity` 完全相同才允许 diff（契约 §9.3）
- `provider` 变更语义：与 Android 对齐（当前 Updater 未校验，生成器算进 identity）
- 两端契约测试：Windows 生成的 `delta.json` 能被 Android 的规则通过（不改 Android 代码，只跑现有测试或等价断言）

**不包含**

- 不改 Android 代码
- 不做"服务端增量协商 / HTTP Range"
- 不做包签名

**预计修改模块**：`app/exporter/delta.py`（新）、`app/package/models.py`（可选字段）、`app/models/naming.py`（如涉及命名）、`tests/test_delta*.py`、`tests/test_package_protocol_v1.py`（跨端断言可能扩展）、`docs/map_package_v1.md`（补可选字段）

**依赖**：45

**数据迁移**：无（只影响新发布的包）

**风险**

| 风险 | 缓解 |
| --- | --- |
| Windows 与 Android 对 `contentSha256` 定义不一致 → 增量永远失败 | 直接复用 Android 的算法定义（契约 §9.4），并用同一份夹具在两端跑出相同摘要 |
| `contentId` 让老客户端误判 | 它是可选字段；Android 忽略未知键（`OfflineMapManifest` 注释已明确这条约定） |
| 增量包比整包还大（改动太多） | diff 后比较 `patchSize` 与整包大小，超过阈值直接建议整包 |
| 用户拿错 base 包 | `baseSha256` 不匹配即拒绝（Android 已实现），生成器也要前置校验 |

**验收场景**

1. 同一区域发布 v1、改一小块再发布 v2 → 生成 delta → `patchSize` 远小于整包（**记录实测比例**，不写死）
2. 改 `labels`（加注记）后再 diff → **拒绝**，提示"要求整包"
3. 改 `region.id` → 拒绝
4. 同一 `updateId` 重复安装 → 拒绝（Android 行为，验一条即可）
5. `contentId` 相同的两个包 → 字节一致；`contentId` 不同的同名同日期包 → 可区分
6. 两端对同一夹具算出的 `contentSha256` 相同

**回退方式**：不生成 delta，继续整包传输（现状行为）。`contentId` 是可选的，不写也能发布。

---

## Slice 49：仓库维护与整体验收

**目标**：仓库能长期跑下去（回收、对账、重建），并端到端验收一次。

**范围**

- `prune` 真正执行（三条前置条件，架构 §7.5）、`superseded` 过期回收、`staging` 清理
- `rebuild_index`、`health_check('full')`、`repair_coords` 的 UI 入口（"仓库维护"页）
- 端到端验收脚本：仓库 → 导出 → ADB → Android 导入 → 增量更新（沿用 `e2e_acceptance_checklist` 的分段结构新增一节）
- 移除 §0.1 的开关（或把默认值改成新路径并保留只读回退说明）
- 文档回填：把架构文档里的假设 A1~A6 换成实测结论

**不包含**

- 不做多机/多用户共享仓库（单机单写者假设不变）
- 不做"瓦片历史版本 UI"（只有 `prune` 与台账）

**预计修改模块**：`app/tilerepo/health.py`、`app/ui/*`（维护页）、`scripts/e2e_tile_repository.py`（新）、`docs/tile_repository_*.md`（回填）、`docs/e2e_acceptance_checklist.md`（新增一节）、`README.md`（当前进度段）

**依赖**：42~48

**数据迁移**：可选 —— 建议把旧 `output/*.mbtiles` 全部导入后删掉（**用户手动确认**，工具不自动删）

**风险**

| 风险 | 缓解 |
| --- | --- |
| `prune` 删掉正在被导出引用的内容 | 三条前置条件（无引用、无活跃快照、不在台账）；`dry_run` 默认开 |
| 长期运行后 catalog 与分片漂移 | `health_check` 提供对账报告；`rebuild_index` 幂等 |
| 端到端验收要真机、要时间 | 复用既有设备清单与脚本结构，验收文档分段可单独跑 |

**验收场景（整体）**

1. 一条完整链路：导入旧包 → 补抓缺口 → 导出两个不同注记策略的包 → 两个包都过 `package_validator`
2. ADB 传输 → Android 导入 → 地图上可见；**同一个仓库产物导出两份，只有一份需要补抓**
3. 对包做一次内容更新 → 生成 delta → Android 增量安装成功
4. 跑一次 `prune`（先 dry-run）→ 报告可回收字节；真跑后 `health_check('full')` 全绿
5. 全量 `pytest` 与两个 e2e 无回归
6. 文档：假设表 A1~A6 全部换成实测值，或明确标注"仍未测量，原因 X"

**回退方式**：仓库目录可整体备份/回滚；分片文件独立，删一个只影响该资产该 band。

---

## 2. 验收矩阵

"标准"= 本文件对应 Slice 的验收场景编号；"证据形态"= 交付验收时要留下什么。

| # | 目标要求 | 满足它的 Slice | 可测标准 | 证据形态 |
| --- | --- | --- | --- | --- |
| 1 | 一次获取，多次复用 | 44、45 | 同区域导出"带注记/无注记"两次，第二次零下载 | 下载计数日志 + 两份 manifest |
| 2 | 原始图层分开管理 | 42、44 | 仓库里同坐标存在 `vec_w` 与 `cva_w` 两条记录 | `query_existence` 输出 |
| 3 | 导出时按需合成 | 45 | `composition=single` 与 `merged` 两份包，字节不同 | 两份 manifest + 抽样图 |
| 4 | 三类空间分别统计 | 46 | 报告里 `repository_bytes` / `delivery_bytes` / `peak_process_bytes` 三个数都有且互不相等 | 预算报告截图/JSON |
| 5 | 已有地图渐进迁移 | 43 | 导入后源文件 mtime/大小不变 | `stat` 前后对比 |
| 6 | 全国低/省市中/任务区高 | 45、47 | 一次导出里 Z4~10 全国 + Z15~18 走廊，中间级不要求 | `ExportPlan` 与覆盖统计 |
| 7 | 不重造已有能力 | 全 | 合成/校验/打包/传输均复用既有模块，无重复实现 | 代码评审 + 依赖图 |
| 8 | 资产身份与版本 | 42 | 同坐标不同内容默认 `Conflict`；授权替换可追溯 | 契约测试 C1~C3 |
| 9 | Token 不进身份与报告 | 42、48 | `grep -i token` 在 manifest/报告/文件名里零命中 | 脚本化 grep |
| 10 | 同坐标冲突规则 | 42 | 默认拒绝、显式允许、旧值可追溯 | C2/C3 |
| 11 | ETag 不可用也有完整流程 | 44 | 无 ETag 时"重取 + 比对摘要"走通，相同则不新建版本 | 更新任务报告 |
| 12 | 分片规则有论证 | 42 | band 表可改、路由是纯函数、单库规模实测 | 架构 §6.2 + 实测字节 |
| 13 | 跨库一致性不靠原子提交 | 42 | 中途 kill 后 `rebuild_index` 能修回 | 中断测试 |
| 14 | 并发与崩溃恢复 | 42 | 两进程写同分片，第二个 `Busy` | C13 |
| 15 | 孤立数据检查与索引重建 | 42、49 | 删内容行 → `Corrupt`；重建幂等 | C14 + 两次重建报告 |
| 16 | 去重范围与冲突校验 | 42 | 同分片内去重；摘要相同仍比字节 | C1 + 碰撞构造用例 |
| 17 | 安全清理条件 | 49 | 三条前置条件全满足才删；`dry_run` 不删 | C15 |
| 18 | catalog 不建逐瓦片索引 | 42 | `catalog.sqlite` 表清单里没有逐坐标表 | `sqlite_master` 输出 |
| 19 | 四图层 vs 合成资产分开 | 43 | `classify` 把现有包判为 `SYNTHETIC_MERGED` | 扫描报告 |
| 20 | 合成不可逆被承认 | 43 | 导入合成包后不允许参与新合成 | 错误码/提示 |
| 21 | 支持 flat 与 view 两种布局 | 43 | 两种源都导入成功且结果一致 | 两份导入报告 |
| 22 | XYZ/TMS 只翻转一次 | 45 | 抽样回读断言 | C9 |
| 23 | 导入只读、幂等、可续 | 43 | C6/C7/C8 | 三次导入报告 |
| 24 | 源有 WAL 的处理 | 43 | 需确认才继续 | C8 |
| 25 | 缺覆盖几何的处理 | 43 | `coverage_quality=bbox_only`，导出不据此判齐全 | 报告字段 |
| 26 | 八组接口有输入输出与失败语义 | —（本文件） | 契约文档 §5 齐备 | 文档评审 |
| 27 | 三种缺图策略，不隐式联网 | 45 | C10/C11 + 依赖守卫测试 | 测试输出 |
| 28 | 缺图报告与父级回退 | 45 | 回退可用但不计入齐全 | 报告样例 |
| 29 | 暂存、原子发布、失败清理 | 45 | C12 | 测试输出 |
| 30 | 版本兼容（新增字段不升版） | 48 | Android 契约测试仍通过 | C16 |
| 31 | 四类预算口径 | 46 | 三个数互不相等、设备侧预留单独一项 | 预算报告 |
| 32 | GiB 统一 | 46 | 界面与报告单位一致 | 截图 + 代码 |
| 33 | 严格预算的暂停/超限/不可行 | 46 | `over` / `insufficient_disk` / `infeasible` 三态可复现 | 三份报告 |
| 34 | 不可行不静默降级 | 46 | `infeasible` 必须给"最低可行"而不是悄悄减级 | 报告文案断言 |
| 35 | 有效范围 = 允许边界 ∩ 任务区 | 47 | 跨县航线不铺满全县 | 范围统计 |
| 36 | 孔洞与并集 | 47 | 孔洞内不入选 | 新用例 |
| 37 | 保留整张相交瓦片 | 47 | 边界瓦片完整写入 | 抽样 |
| 38 | 四种身份分开 | 48 | 资产/覆盖策略/包 id/版本号互不混淆 | 契约测试 |
| 39 | 可增量 vs 必须整包 | 48 | 改 `labels` 被拒、改瓦片内容可增量 | 四条拒绝用例 |
| 40 | 复用 Android 已有能力 | 45、48 | 不新增 Android 代码即完成端到端 | 端到端脚本 |

**已知不满足 / 需后续处理的**（写在这里，避免被当成遗漏）：

| 项 | 原因 | 计划 |
| --- | --- | --- |
| 孔洞无显式测试 | 现状依赖 Shapely 语义 | Slice 47 补 |
| 覆盖缺失只 WARNING，永不 FAIL | `diagnostics._report_coverage` 明确的设计（Spec §21） | 不改校验语义；改在"导出许可"（Slice 45） |
| `DATABASE_OVERHEAD` 无来源 | 现状硬编码 1.08 | Slice 46 用实测替换并保留旧系数对照 |
| 无 ETag 时更新 = 全量重下 | 服务端是否支持未验证 | Slice 44 记录实测；Slice 46 在预算里体现 |
| Android 无分层显示模型 | 现状单链 provider | 不在本路线内；交付仍按"一包一合成图" |
| `package id` 不含内容 | 目录名已装包依赖它 | 用可选 `contentId` 补（Slice 48） |

---

## 3. 建议的首个可实施任务

**Slice 42，第一步限定为"身份 + 分片 + `put/read/query` + `health`"四件，不含 catalog 的 coverage/epochs 完整记账。**

拆成三个可独立验收的小步：

1. `identity.py` + `shard.py`（路由与 DDL）+ 单测：给定 `(asset, z)` 算出唯一的路径；band 边界上的 z 归入哪一带要写死一条用例
2. `store.py`（`put_tiles` / `read_tile` / `query_existence`）+ 契约测试 C1/C2/C3
3. `health.py`（`health_check` / `rebuild_index` / `prune(dry_run)`）+ C14/C15

理由：这三步都不依赖下载、不依赖导出、不依赖 UI，**每一步都能用合成数据完整验收**，而它们恰好是后续六个 Slice 的地基。做完这三步，Slice 43 的导入就能直接落地（它只需要 `put_tiles` + 幂等）。

**开始前要确认的一件事**：仓库根默认放在哪（建议 `%USERPROFILE%\Documents\TiandituTileRepo` 或与 `output/` 同盘的一个独立目录 —— **必须与交付输出同盘**，否则暂存与发布的 `os.replace` 不是原子操作）。这条是 Slice 42 唯一需要用户拍板的配置。
