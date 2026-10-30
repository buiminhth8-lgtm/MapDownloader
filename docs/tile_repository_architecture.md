# 统一瓦片仓库与独立打包引擎：现状与架构

这份文档回答一个问题：**把下载工具变成"一次获取、多次复用"的瓦片仓库，现有代码缺什么、补上去之后长什么样。**

先给结论，再给证据，最后给取舍。证据全部来自代码与实测产物，不引用 README 的功能描述。

---

## 0. 阅读前提

| 项 | 值 |
| --- | --- |
| 证据基线 | `D:\project\MapDownloader`，审查开始时 HEAD `d32fdf6`（工作区另有 22 个已修改 + 8 个未跟踪文件） |
| 兄弟仓库 | `D:\project\osmdroid`，HEAD `f5ceba5`（仅 `.idea/codeStyles/Project.xml` 未提交） |
| 行号口径 | **按审查时的文件内容计**；审查期间那些未提交改动被提交为 `aac5f3a`（作者 jiajing.lv，17:01），文件内容未变，因此行号仍然有效 |
| 本次变更范围 | 只新增 `docs/tile_repository_*.md` 三份文档 + 改 `docs/README.md` 导航；<br>不改业务代码、测试、依赖、配置，不改 Android 仓库，不下载、不打包，**不提交 Git** |

> 行号随代码变动会过期。复核方法：`grep -n "<符号名>" <文件>`，符号名在本文件里逐条给出。

### 审查开始时的 Git 快照（这些改动**不是**本次成果）

```
MapDownloader  d32fdf6  已修改 22 个 / 未跟踪 8 个
  M app/{downloader/download_manager,geo/region,geo/zoom_coverage,
        mbtiles/{diagnostics,reader,validator,writer},package/package_builder,
        package/package_validator,planning/storage_budget,
        services/map_download_service,tasks/task_store,ui/main_window,
        ui/package_dialog}.py
  M README.md  docs/{README,e2e_acceptance_checklist,map_package_v1,storage_budget}.md
  M scripts/{verify_stability,verify_zoom_coverage}.py  tests/test_zoom_coverage.py
  ?? app/services/mbtiles_optimizer.py  app/ui/storage_optimization_dialog.py
  ?? docs/{storage_optimization,zoom_coverage}.md
  ?? scripts/verify_storage_optimization.py
  ?? tests/test_storage_optimization{,_ui}.py  tests/test_zoom_coverage_ui.py
osmdroid       f5ceba5  M .idea/codeStyles/Project.xml
```

审查期间这批改动被提交为 `aac5f3a` 与 `c38c26b`（作者 jiajing.lv）。
**提交不是本次任务做的** —— 本次只写文档，未执行任何 `git add` / `git commit`。

---

## 1. 结论摘要

七条，先看这里：

1. **现在没有仓库层，只有交付物。** 瓦片字节从 HTTP 响应到 SQLite 全程在内存里，落盘点就是**最终交付文件**（`app/downloader/download_manager.py` 模块 docstring L41「本模块不写磁盘、不建库」；`app/downloader/models.py:TileDownloadResult.content` L144-168「不落盘」）。**没有任何中间态可以被复用。**
2. **原始图层从未被单独保存。** 勾了注记就是"取两层 → 合成一张 PNG → 丢掉两层"（`app/downloader/composite_tile_downloader.py:CompositeTileDownloader.download_tile` L119-225 → `app/composer/tile_composer.py:TileComposer.compose` L134-163）。**合成不可逆**：`Image.alpha_composite` 之后没有分离注记的代码路径，全仓也没有"拆层"实现。
3. **因此"一次获取多次复用"目前无法成立。** 想要"矢量无注记"和"矢量带注记"两份，只能下两遍（`app/services/map_download_service.py:_execute` L1299-1310 按 `merged` 决定 worker 类型，两条路互斥）。
4. **资产身份不存在。** 唯一的粗粒度键是 MBTiles metadata 里的 `map_type` / `labels` / `composition`（`app/mbtiles/models.py:MBTilesMetadata.as_dict` L301-338），没有 `layer`、`tile_size`、`projection`、内容摘要。同坐标不同内容在写入侧是 **`INSERT OR REPLACE` 无条件覆盖**（`app/mbtiles/writer.py:_INSERT_TILE_SQL` L115-118）。
5. **交付包 ID 不含内容。** `make_package_id(name, created_at)` 只吃"包名 + 日期"（`app/package/package_builder.py:make_package_id` L152-179）。同一天、同名、内容不同的两个包会得到**同一个 id**，而 Android 侧靠目录名等于 `manifest.id` 做登记（`OfflineMapRegistry.refresh` L204-208）。manifest 也没有自身摘要（`app/package/checksum.py` 只对 `map.mbtiles` 算 SHA256）。
6. **增量交付能力两头不对等。** Android 侧有完整的增量协议与安装器（`OfflinePackageUpdater.apply` L33-111，`delta.json` v1）；**Windows 侧没有生成器** —— 目前唯一的生成脚本在 Android 仓库里（`osmdroid/scripts/create_offline_delta.py`），且只接受"两个已经关档的包目录"。
7. **分层依赖目前是干净的。** `app/downloader`、`app/mbtiles`、`app/package`、`app/geo`、`app/planning`、`app/services`、`app/tasks` 对 `app.ui` **零 import**（grep 结果为空）。新模块继续保持这条线，禁止仓库依赖 UI。

---

## 2. 目标架构

```
                    ┌──────────────────────────────────────────────┐
                    │ 地图数据源（天地图 WMTS，4 个图层 × 2 个地图模式）│
                    └───────────────────┬──────────────────────────┘
                                        │ 原始图层获取（按 layer 分别取，不合层）
                                        ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ 统一瓦片仓库  app/tilerepo/                                                │
│  ┌────────────┐  ┌──────────────┐  ┌───────────────┐  ┌────────────────┐  │
│  │ 资产身份     │  │ 分片存储       │  │ 中央 catalog    │  │ 健康检查 / 对账  │  │
│  │ identity.py │  │ shard/store.py│  │ catalog.py     │  │ health.py      │  │
│  └────────────┘  └──────────────┘  └───────────────┘  └────────────────┘  │
│         ▲                ▲                  ▲                              │
│         │                │                  │                              │
│  资产导入（旧包入仓） 下载写入（缺口补抓）   查询/快照/预算                     │
└─────────┬────────────────┬─────────────────┬──────────────────────────────┘
          │                │                 │
  ┌───────┴──────┐  ┌──────┴───────┐  ┌──────┴─────────┐
  │ asset_import │  │  downloader  │  │ planning/budget │
  │ （Slice 43）  │  │ （Slice 44）  │  │ （Slice 46/47）  │
  └──────────────┘  └──────────────┘  └────────────────┘
                                        │
                              导出请求（区域 / 分层 zoom / 模式 / 注记）
                                        ▼
┌───────────────────────────────────────────────────────────────────┐
│ 独立离线打包引擎  app/exporter/（Slice 45）                          │
│  读稳定快照 → 按需合成注记 → 写 MBTiles → 写 manifest → 原子发布        │
└───────────────────────────────┬───────────────────────────────────┘
                                │ 交付物：MBTiles + manifest.json（+ delta.json）
                                ▼
                  ┌─────────────────────────────────┐
                  │ Android osmdroid 离线读取与更新    │
                  │ 多包 / 父级回退 / AUTO 选包 / 增量  │
                  └─────────────────────────────────┘
```

两条硬约束：

- **仓库只存原始图层**（`vec_w` / `cva_w` / `img_w` / `cia_w`），合成是导出期动作。理由见 §3.9 与 §6.3。
- **仓库格式 ≠ 交付格式。** 仓库用内容寻址 + XYZ，交付用 MBTiles 兼容布局 + TMS。翻转与布局差异只允许在导出器里发生**一次**（见 §7.4）。

---

## 3. 现状证据表

每行都是"以代码为准"的事实，附入口与限制。**这一节只陈述，不评价**；评价在 §4。

### 3.1 下载、限速、429 冷却、重试、暂停、取消、续传

| 能力 | 入口 | 现状事实（含限制） |
| --- | --- | --- |
| 下载编排 | `app/services/map_download_service.py:MapDownloadService._execute` L1123-1470 | 单文件顺序编排：建库 → 写 metadata → 建续传索引 → 建 sink → 建下载器 → `manager.download()`。**中途没有仓库写入点** |
| 并发模型 | `app/downloader/download_manager.py:DownloadManager._run` L487-568 | 1 个生产者 + N 个 worker（有界队列 `concurrency × QUEUE_CAPACITY_FACTOR`）。默认并发 3、上限 16（`app/config/settings.py:DEFAULT_DOWNLOAD_CONCURRENCY` L101 / `MAX_DOWNLOAD_CONCURRENCY` L106） |
| 限速 | `app/downloader/rate_limiter.py:RateLimiter.acquire` / `_acquire_slot` L121-143 | **匀速档位**，不是令牌桶：`interval = 1/effective_rate`（L102-103）。docstring L55 明确"不积累空闲额度"。默认 4 req/s（`DEFAULT_REQUESTS_PER_SECOND` L116）。`max_burst` 字段 L59 是**死参数**（声明、校验，从不读取） |
| 429 冷却 | `app/downloader/tile_downloader.py:_attempt` L362-373 + `RateLimiter.on_throttled` L145-168 | 只看 `status_code == 429`；`Retry-After` 解析支持秒数与 HTTP 日期（`parse_retry_after` L29-46）。冷却 = `max(server, local)` + jitter，同时**速率减半**。作用域 = 同一个 `RateLimiter` 实例（跨 service 实例不共享） |
| 冷却耗尽 | `download_manager.py:_worker` L680-685 | `RateLimitExceeded` → 置 `_stop_reason` + `cancel_event.set()` + `gate.set()` → **整批停**，不退避续跑；恢复只能靠用户手动续传 |
| 重试 | `app/downloader/retry_policy.py:RetryPolicy.delay_after_attempt` L120-147 | 纯指数 0.5 / 1 / 2 / 4 / 5 / 5…；`max_attempts` 含首次（默认 3）。可重试：`NETWORK/TIMEOUT/EMPTY_RESPONSE/INVALID_CONTENT/RESPONSE_TOO_LARGE/UNEXPECTED` + `>=500` + `408/425/429`（`RETRYABLE_FAILURE_KINDS` L48-57、`is_retryable_status_code` L60-83）。合成失败三态是**终态不重试**（`app/downloader/models.py:TileFailureKind` L106-110） |
| 暂停 | `download_manager.py:pause/resume` L315-325 → `_set_paused` L343-356 | 只在"发起下一个请求之前"生效，已发出的请求跑完。asyncio.Event 闸门，跨线程用 `loop.call_soon_threadsafe`（`_request` L327-341） |
| 取消 | `download_manager.py:_do_cancel` L358-370 | 取消时**同时抬起闸门**，否则暂停态 worker 与满队列生产者会互锁（注释 L366-367）。取消后仍需排空队列并 `drain()`（`_execute` L1360-1378） |
| 续传 | `app/downloader/resume.py:ExistingTileIndex` L176-342 | **唯一事实来源是目标 mbtiles 的 `tiles` 表**，没有独立"已下载清单"。≤50000 张走内存缓存（`CACHED`），否则逐张查询（`QUERY`）。前置一致性校验 `check_resume_compatibility`（`map_download_service.py` L215-324）：范围容差 1e-6、zoom、图层、地图类型、合成方式（硬拦）、区域 |
| 续传的盲区 | `app/mbtiles/writer.py` 注释 L388-390 | `write_tile` 是批量缓冲，**未提交的瓦片查不到** → "边下边查重"在当前实现下不成立 |

### 3.2 下载互斥与文件写入保护

| 能力 | 入口 | 现状事实（含限制） |
| --- | --- | --- |
| 进程内互斥 | `download_manager.py:download` L425-426 | `if self.is_running: raise ValueError` —— **只保护同一个 manager 实例** |
| UI 级互斥 | `app/ui/main_window.py` L1416-1420 / L3065-3068 / L4404-4405 | 靠 `worker.isRunning()` 保证同时只有一个下载线程。**这是界面判断，不是文件锁** |
| 文件锁 | — | `app/` 下 `threading.Lock` / `msvcrt` / `fcntl` / lockfile **零命中**。同一 mbtiles 允许多个写入者从代码层面发起，只剩 SQLite 自身在兜底 |
| 唯一写者（进程内） | `app/mbtiles/writer.py:MBTilesTileSink` L596-617 | 单消费者协程持有连接，HTTP worker 从不碰库；队列有界（容量 100~500，`app/config/settings.py` L164-168） |
| 原子写 | `map_download_service.py:_execute` L1238-1241 | **没有**。直接 `MBTilesWriter(final_path)` 打开最终路径 → 下载全程磁盘上有一个"半成品最终文件" |
| 失败清理 | `_execute` 的 `finally` L1360-1378 | 只 `abort()` / `close()`，**不删半成品文件**。要重新下载时由 UI 把旧文件改名备份（`main_window.py:_on_restart_answered` L2099-2110） |
| WAL 收尾 | `app/mbtiles/writer.py:_PRAGMAS` L130-134 / `close()` L497-523 | 写时 WAL + `synchronous=NORMAL`；关闭前 `wal_checkpoint(TRUNCATE)` 再 `journal_mode=DELETE`，保证交付单文件不残留 `-wal/-shm` |

### 3.3 损坏瓦片检查与修复

| 能力 | 入口 | 现状事实（含限制） |
| --- | --- | --- |
| 快检（下载时） | `app/providers/tianditu.py:is_valid_tile_response` L301-321 | **只看 magic bytes + Content-Type**，不做解码。一张"PNG 头 + 截断数据"会被判成功并写库 |
| 深检（单张） | `app/providers/image_validation.py:verify_tile_image` L15-28 | `image.verify()` 后**再 `load()` 一次**（注释 L25-27：verify 不读 JPEG 像素、也识别不了 CRC 正确但压缩流坏掉的 PNG） |
| 结构校验 | `app/mbtiles/validator.py:validate_mbtiles` L112-176 | 文件存在 / 可打开 / 必需表 / metadata 必需键 / bounds 合法性 / zoom 区间 / 瓦片数非 0 / 抽样 magic bytes。**"损坏"只有两条判据**：`tile_data` 为空、magic bytes 既非 PNG 也非 JPEG |
| 诊断 | `app/mbtiles/diagnostics.py:probe_image` L748-777 | 两级：magic bytes → Pillow `verify()`；无 Pillow 时退回一级并返回 `ok=True`。另有 `collect_zoom_stats` L582-618、`check_coordinates` L665-707、`check_duplicates` L710-729、`check_coverage` L897-1050、`deep_scan_tiles` L1216-1338（可中止） |
| 修复 | `app/downloader/resume.py:ResumePolicy.repair` L90-93 + `ExistingTileIndex.should_skip` L292-318 | 已有瓦片读 BLOB → `is_decodable_tile` 失败 → **不跳过 = 触发重下** → `INSERT OR REPLACE` 覆盖。替换来源是同一个 provider，写回同一个文件 |
| 修复的粒度 | — | **没有"按给定坐标清单重下"的接口**，只能全量遍历时顺带判定。只读产物（`export_readonly=true`）明确拒绝修复（`writer.py:open` L289-294） |
| 复核 | `map_download_service.py` L1394-1412 | 修复模式强制 `deep_scan=True`；未扫完则 `valid=False` |

### 3.4 GeoJSON 筛选、航线米制缓冲、混合 Zoom 覆盖

| 能力 | 入口 | 现状事实（含限制） |
| --- | --- | --- |
| 几何判据 | `app/geo/tile_region_filter.py:PolygonTileRegionFilter.intersects_tile_xyz` L136-146 | **瓦片矩形 × 区域几何 `intersects`**，构造时 `prep()` 一次（L97）。模块 docstring L5-14 明确说明**不用中心点判据**（会在边界漏块） |
| 点在多边形内 | — | **没有 PIP 实现**（`app/` 下相关 grep 零命中）。这是有意选择，不是遗漏 |
| 孔洞 | — | `app/` 下 `interiors|holes` **零命中**。孔洞行为完全依赖 Shapely `intersects` 语义，仓库内没有自己的孔洞逻辑，也没有针对孔洞的测试钩子 |
| 空间索引 | — | `STRtree|SpatialIndex|Rtree|quadtree` **零命中**。唯一加速是 Shapely `prep()` + `CoverageTileFilter._active_cache`（`app/geo/zoom_coverage.py` L193-197）。多边形枚举是**纯双层 range 全枚举 + 逐张判相交** |
| 航线缓冲 | `app/mission/region_builder.py:build_geometry` L241-280 | **不是测地缓冲**：投影到 AEQD 米制平面 → `buffer(radius, quad_segs=16, cap_style=round, join_style=round)` → 逆变换。半径米制，`100 ~ 50000`（`app/config/settings.py` L423-424），默认 5000（L420）。经纬跨度 > 10°（`MISSION_MAX_ROUTE_SPAN_DEGREES` L435）直接拒绝（`_check_span` L364-381） |
| 混合 Zoom | `app/geo/zoom_coverage.py:CoverageTileFilter` L181-242 | 每条规则自带 `(minZoom, maxZoom, bbox, prepared_geometry)`；按 zoom 惰性算 active 集合；`row_intervals` L199-217 把 Y 段切分 + X 区间合并。**允许 zoom 空洞**（Z5~8 + Z12~15 合法，中间级无瓦片）；`coverage_zoom_range` L131-133 只给全局包络 |
| 覆盖快照 | `zoom_coverage.py:save_coverage_snapshot` L143-158 | `<输出目录>/.coverage/<fingerprint>.json`，`.tmp` + `os.replace`；目标已存在且内容不同 → 抛错 |
| 换算位置 | `app/geo/tile_math.py:xyz_y_to_tms_y` L461-471 | `tiles - 1 - row`。**目前翻转发生在两处**：写（`writer.py:write_tile` L363）与读（`reader.py:_tms_params` L879-885） |

### 3.5 容量预测、已有瓦片扣除、磁盘空间检查

| 能力 | 入口 | 现状事实（含限制） |
| --- | --- | --- |
| 每瓦片字节 | `app/planning/storage_budget.py:_samples` L216-261 | **只从本地同目录 MBTiles 抽样**，不联网：每个 zoom 取 `tile_column` 区间等分 24 个列，每列取一条 `length(tile_data)`，只查长度不读 BLOB。样本须 `map_type/labels/composition/format` 与请求一致（`_matches` L208-213），最多再取 12 个同目录文件 |
| 无样本回退 | `storage_budget.py:assess_budget` L340-348 | 影像 `(128 if labels else 96) KiB`、矢量 `30 KiB`，`uncertainty=2.0` |
| 已有扣除 | `storage_budget.py:_existing` L264-293 | 仅 `resume` 且目标存在时；只读 URI + `PRAGMA query_only=ON`；逐行回算 XYZ 后**必须过 `intersects_tile_xyz`** 才计入；已有字节 = `st_size` + 同名 `-wal` |
| 磁盘检查 | `storage_budget.py:_disk` L296-305 | `shutil.disk_usage(path).free`，按盘符/`st_dev` 聚合，同盘多条目**累加** required、free 取 min（L353-366）。只覆盖**输出目录所在卷** |
| 口径 | `storage_budget.py:GIB` L15 | `1024**3`，文案写 "GiB"；但展示函数 `app/geo/tile_math.py:format_size` L513-530 除以 1024 却把单位标成 `GB` → **界面上的 "GB" 实际是 GiB** |
| 无来源常数 | `storage_budget.py` L20-21 | `DATABASE_OVERHEAD = 1.08`、`DATABASE_BASE_BYTES = 24 KiB` 无依据说明 |
| 预算判定 | `storage_budget.py:BudgetReport` L150-160 | 只有三个布尔：`over_budget` / `disk_ok` / `needs_confirmation`。**没有"四类空间"概念**，也没有"不可行"结论 |

### 3.6 MBTiles 写入、读取、压缩、内容去重、区域分包

| 能力 | 入口 | 现状事实（含限制） |
| --- | --- | --- |
| 表结构（下载产物） | `app/mbtiles/writer.py:_CREATE_TILES_SQL` L97-108 | 标准 flat：`tiles(zoom_level, tile_column, tile_row(TMS), tile_data)`，三列联合主键。`metadata(name PK, value)` L89-93 |
| 索引 | `writer.py` 注释 L152-155 | **有意不建任何自定义索引**（主键隐式唯一索引除外） |
| 写入 | `writer.py:write_tile` L336-378 → `flush` L473-495 | 内存缓冲攒 `batch_size`（默认 200，`settings.py` L156）→ `BEGIN/executemany/COMMIT`（`_execute_in_transaction` L572-590）。`INSERT OR REPLACE`，重复坐标**覆盖不报错** |
| 读取 | `app/mbtiles/reader.py:open` L285-286 | `mode=ro` + `uri=True` 强制只读。单张 `TILE_DATA_SQL` L102-105；存在性 `TILE_EXISTS_SQL` L96-99（不读 BLOB）；范围只有**计数**与**按列列行号**（`count_tiles_in_range` L508-529、`iter_tile_rows_in_column` L531-555），**没有按 bbox 批量取内容** |
| 压缩 | `app/services/mbtiles_optimizer.py:_encode` L248-275 | PNG：`optimize=True, compress_level=9` + **像素等价校验**（L266-269），无调色板量化；JPEG：仅在显式要求且 alpha 全 255 时重编码（L258-262），否则**保留原字节**（L271-272） |
| 内容去重 | `mbtiles_optimizer.py:_TargetWriter.add` L299-320 | SHA-256 + **命中后比较真实字节**（碰撞即 `ValueError`，L310-311）。去重范围 = **单个输出文件内、跨全部 zoom**（`images.hash UNIQUE` L289）。不跨文件 |
| 去重布局 | `mbtiles_optimizer.py:_TargetWriter.__init__` L278-294 | normalized：`map(z,x,tile_row,tile_id)` + `images(tile_id, hash UNIQUE, tile_data)` + `CREATE VIEW tiles`。另有 `storage_layout` 标记（`normalized-v1` / `flat-export-v1`，L42-43）与 `export_readonly='true'` |
| 逆操作 | — | **没有** normalized → flat 的展开实现；且 `export_readonly` 会让 `MBTilesWriter.open` 直接拒绝写入（L291-294） |
| 区域分包 | `mbtiles_optimizer.py:prepare_export` L174-239 | 三种范围（整包 / 按覆盖规则拆 / 按计划区域拆），切分维度是**区域**，不是 zoom；`MAX_TARGETS = 128`（L46）。输出 `001_<name>.mbtiles`（L391）。发布走"临时目录 → `os.rename`"（L363、L469），失败 `rmtree`（L475-476） |
| 分包后的合法性 | `execute_export` L430-446 | 三道闸门：osmdroid 查询回放 + `PRAGMA quick_check`（`verify_osmdroid_reads` L327-350）、`MBTilesInspectionService.inspect` 要求覆盖齐全、`MapPackageService.build+validate` |

### 3.7 manifest、校验、传输、包身份

| 能力 | 入口 | 现状事实（含限制） |
| --- | --- | --- |
| 契约版本 | `app/config/settings.py:PACKAGE_FORMAT_VERSION` L927 = **1** | 全仓**没有** `schemaVersion`；"schema 版本"就是 `formatVersion`。Android 侧另有 `SUPPORTED_SCHEMA_VERSION = 2`（`OfflineMapManifest` L37），即 Android 同时支持 Windows V1 与它自己的 schema 2 |
| 字段 | `app/package/models.py:MANIFEST_FIELD_ORDER` L115-133（17 个） | `formatVersion, id, name, description, provider, mapType, labels, composition, tileScheme, tileFormat, minZoom, maxZoom, bounds, region, tileCount, mbtiles, createdAt`。`region` 子对象 7 键（L144-152），`mbtiles` 3 键（L159） |
| 包身份 | `app/package/package_builder.py:make_package_id` L152-179 | `f"{清洗后的名字}_{YYYYMMDD}"`，**不哈希、不含 token、不含区域/zoom/图层/瓦片数**。签名即证据 |
| 校验 | `app/package/package_validator.py:validate` L161-301 | 目录 → manifest 可解析 → mbtiles 在包内且防穿越 → 大小一致 → SHA256 → MBTiles 诊断 → 与 metadata 比对（coverage 规则 / zoom / tileCount / format / bounds 是 **error**，mapType/labels/composition/provider 是 **warning**，L405-428） |
| 摘要范围 | `app/package/checksum.py:calculate_sha256` L99-152 | 流式 4 MiB 分块，**只覆盖 `map.mbtiles`**。**manifest 自身没有摘要** |
| 打包 | `package_builder.py:build` L349-539 | 输入是**单个 mbtiles**；复制（非移动）到 `<out>/<name>/`；不压缩；有阶段化进度（`PACKAGE_BUILD_STAGE_RANGES` L957-963）；manifest 原子写（`_write_text_atomic` L1008-1021）；发布 = 旧目录改 `.backup-<ts>` + `os.replace`（L762-780）；失败只清 `.building`（`_cleanup` L869-879） |
| 传输 | `app/android/map_package_transfer.py:_upload_package` L518-610 | 目标 `/sdcard/Download/TiandituOfflineMap/import/<id>/`（`settings.py:DEFAULT_ANDROID_IMPORT_DIR` L1148）。先传 `map.mbtiles.part` → `mv` → 最后传 `manifest.json` → 校验远端大小 + best-effort SHA256（取不到只 warning）。**不支持断点续传**：`adb push` argv 固定（`adb_client.py:push` L384-420），残留 `.part` 一律先删（L733-751） |

### 3.8 Android 多包读取、父级回退、保留策略、缓存配额、增量更新

| 能力 | 入口（`D:\project\osmdroid`） | 现状事实 |
| --- | --- | --- |
| 安装 | `osmdroid-android/.../offline/installer/OfflinePackageInstaller.java:runImport` L219-470 | 两种来源（`importFromDirectory` L160-166 / `importFromTreeUri` L170-177）。校验：manifest 存在 → 解析 → 语义校验 → mbtiles 在 → 字节数一致 → 同名占用 → 配额 → 磁盘空间 → 复制时**同流算 SHA256** → 快速校验 → 原子 `safeMove` → 失败回滚 |
| 落点 | `.../offline/storage/OfflineMapStorage.java` L49-55、`forApp` L88-97 | `getExternalFilesDir(null)/offline_maps/<id>/`，staging `.importing`，备份 `.backup` |
| 多包发现 | `OfflineMapStorage.listInstalledDirectories` L205-226 + `OfflineMapRegistry.refresh` L159-262 | **扫盘 + 每包 manifest.json**，没有中心索引；注释 L26-35 明确拒绝引入数据库。目录名必须等于 `manifest.id`（L204-208） |
| 父级回退 | `.../tileprovider/modules/MapTileApproximater.java:approximateTileFromLowerZoom` L135-143、L178-257 | `zoomDiff` 从 1 递增找第一个命中的祖先，`x>>zoomDiff` 取父瓦片，裁子矩形**拉伸到 256×256** + 打 `SCALED` 标记。**不改变 MapView 的 zoom** |
| 包级回退 | `OpenStreetMapViewer/.../selector/MapSelectionPolicy.java:select` L108-124 | 没有任何包支持当前 zoom 时，选 `maxZoom < zoom` 的包做 anchor，reason = `PARENT_TILE_FALLBACK`，但有 **`zoom <= 22` 硬边界**（L108） |
| AUTO 选包 | `MapSelectionPolicy.isBetterChoice` L200-227 | 排序键：默认包 → **RegionLevel specificity 大者胜** → 离 zoom 中心近 → bounds 面积小 → maxZoom 大 → id 字典序。替换需 specificity 严格更大 + 过迟滞（`canReplaceCurrent` L157-166） |
| 等级 | `.../offline/model/RegionLevel.java` L40-58 | `MISSION 5 / COUNTY 4 / CITY 3 / PROVINCE 2 / COUNTRY 1 / CUSTOM 0 / UNKNOWN -1` |
| 配额 | `.../offline/storage/OfflineStorageMaintenance.java` L13-15 | 包配额 16 GiB、缓存配额 512 MiB、任务包 30 天；`maintain` 只删**已过期 task 包**，base/在用/pinned 一律保护（L115-155） |
| 瓦片缓存 | `.../tileprovider/modules/SqlTileWriter.java` L717-782 | 超限裁剪到 trim 值，淘汰顺序按 **`expires ASC`**（L741-746），**不是 LRU**；配额 600/500 MiB（`DefaultConfigurationProvider` L50-51） |
| 增量协议 | `.../offline/installer/OfflinePackageUpdater.java:apply` L33-111 | `delta.json` v1（`deltaVersion` / `updateId` / `baseSha256` / `baseContentSha256` / `targetContentSha256` / `patchSha256` / `patchSize`）；**拒绝**地图模式/格式/范围/Zoom 变化（L50-54）、**拒绝** `regionId`/`regionBoundaryVersion` 变化（L60-62）、**拒绝** 同一 `updateId` 重复安装（L58-59）；`applyTiles` L146-203 支持 flat 与 normalized 两种写入并清孤立 `images` |
| normalized 读取 | `MBTilesFileArchive.java:getInputStream` L60-84 | 只 `query("tiles", ...)`；SQLite 对 VIEW 透明 → **包内必须有 `tiles` 视图**，否则读取与探测都失败（`MbTilesProbe.PROBE_SQL` L69、`MbTilesValidator` L117-118） |
| zip 离线包 | — | 归档层支持 zip（`ArchiveFileFactory` L19），但离线包协议强制 `map.mbtiles`（`MapPackageValidator` L193-197），**安装/加载链路不接受 zip** |

### 3.9 现有 MBTiles 是原始图层还是合成资产

**结论：全部是合成资产，一张原始图层瓦片都没有单独保存过。**

判定依据（代码）：

- 勾注记时走 `CompositeTileDownloader`，两层取回后立刻 `compose()` 成一张 PNG，两层字节随函数返回被丢弃（`composite_tile_downloader.py:download_tile` L163-225）。
- `TileComposer.compose` 只有 `Image.alpha_composite` 一条路径（`tile_composer.py` L154），**没有反解**。
- metadata 写入 `composition=merged` + `labels=true`（`map_download_service.py` L1199-1237 与 `app/mbtiles/models.py:as_dict` L318-324），且 `merged` 时强制 `format=png`（service L1199-1204；validator L334-339 也会拒 absolute 非 png 的 merged 包）。
- metadata 里**没有 `layers` 键**（全仓写入路径无此键），所以一个 mbtiles 无法声明"我装了哪几个图层"。

实测（只读打开 `output/`，2026-10-30）：

| 文件 | 字节 | 瓦片数 | 平均 | `map_type` | `labels` | `composition` | 表对象 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `offline_map.mbtiles` | 1,884,770,304 | 14,497 | ≈ 127 KiB/张 | `image` | `true` | `merged` | `metadata` + `tiles` |
| `offline_map_vec_cn.mbtiles` | 110,161,920 | 9,999 | ≈ 10.8 KiB/张 | `vector` | `true` | `merged` | `metadata` + `tiles` |

两者 `minzoom=5 / maxzoom=10 / region_level=country`。**注意**：两者内容不同（影像 vs 矢量），平均字节不能直接对比；张数也不同，不能据此推断覆盖完整度（可能是中断的部分下载）。这两条是实测值，不是模型估算。

**识别规则（建议写进 Slice 43 的判定函数）**：

```
merged + labels=true            → SYNTHETIC_MERGED   合成资产，不可拆层
single + labels=false           → SYNTHETIC_SINGLE   仅底图（仍不可拆层，因为没有分层存储）
任何带 layers 键的多层容器        → 本仓库尚不存在此形态（无识别依据）
```

三条限制必须写进产品行为：合成包**不得**被当作原始底图参与新合成；**不得**假定可逆拆层；**不得**因为"反正是同一张图"而与原始层混写（`app/models/map_layer.py` docstring L28-29 已经警告过混写后果）。

---

## 4. 差距分析

按目标架构的 10 条原则逐条对照，"缺"的定位到层：

| # | 目标原则 | 现状 | 缺口所在层 |
| --- | --- | --- | --- |
| 1 | 一次获取，多次复用 | 下载直接产出交付物，中间态不可复用 | **缺仓库层**（整层缺失） |
| 2 | 原始图层分开管理，导出时按需合成 | 四图层从未分存；合成不可逆 | **缺原始图层存储 + 合成在导出期** |
| 3 | 仓库/交付/峰值分别统计 | 只有"本次任务"一个数字 | 预算模型缺三个口径 |
| 4 | 已有地图渐进迁移，不要求重下 | 无导入通道；旧包只能整包复用 | 缺资产导入 |
| 5 | 全国低 / 省市中 / 任务区高 | 有 `CoverageTileFilter` 分层 zoom，但绑定在"一次下载"里 | 覆盖策略可复用，缺"仓库级"表达 |
| 6 | 不重复开发已有能力 | 见 §3 全表 | 复用清单见 §5.3 |
| 7 | 资产可识别、可版本化 | 无资产身份、无内容摘要、包 id 无内容 | 缺身份与版本 |
| 8 | 冲突/更新有规则 | `INSERT OR REPLACE` 无条件覆盖 | 缺冲突规则 |
| 9 | 缺图不得静默 | 覆盖缺失**只记 WARNING 永不 FAIL**（`diagnostics.py:_report_coverage` L1179-1213，注释明确"Spec §21"） | 与"缺失则拒绝"策略**语义冲突**（见 §10 待决 1） |
| 10 | 导出不隐式联网 | 现在下载与打包是两个独立动作，没有"导出时自动补图"的路径 | 新引擎要**主动禁止**隐式联网 |

---

## 5. 模块划分与依赖

### 5.1 分层

```
ui/            ← 只调 services/，不碰 tilerepo 内部
services/      ← 编排：把"用户动作"翻译成 仓库/下载/导出 的调用序列
tilerepo/      ← 资产身份、分片、catalog、健康检查（不 import ui、不 import package）
asset_import/  ← 旧 MBTiles → 仓库（只读源文件）
exporter/      ← 仓库 → MBTiles + manifest（不联网）
downloader/    ← 已有，改为"写仓库"而不是"写交付文件"
mbtiles/       ← 已有，保留读取/校验/诊断能力；交付布局由 exporter 负责
composer/      ← 已有，合成只在 exporter 内调用
package/       ← 已有，manifest 与包校验复用
planning/      ← 已有，预算/zoom 策略扩展为四类空间口径
geo/ mission/  ← 已有，几何与覆盖原样复用
```

依赖方向（约束，Slice 42 加守卫测试）：

```
ui → services → {tilerepo, exporter, downloader, asset_import, planning}
exporter → {tilerepo, composer, mbtiles, package}
downloader → {tilerepo, geo, mission}
tilerepo → {geo, mbtiles(read-only helpers)}
✗ tilerepo → ui        ✗ tilerepo → package      ✗ exporter → downloader（不得隐式联网）
```

现状已满足第一条：非 UI 模块对 `app.ui` 零 import（§1 第 7 条）。新模块要**保持**，并加一条 AST 守卫测试（参照 `tests/test_zoom_policy.py` 已有的静态守卫写法）。

### 5.2 UI 如何调用服务

三层，UI 只写第一层：

1. **UI 回调**：按钮 → `services` 的一个方法，带 `progress_callback` 与 `should_stop`（沿用 `package_builder.build` 的签名风格 L349-362）。
2. **services 编排**：拆成"算缺口 → （可选）补抓 → 导出 → 校验 → 发布"，每一步都可取消、都往任务面板写进度。
3. **tilerepo/exporter**：只认领域对象，不认识 Qt。异常上抛成有分类的错误（见契约文档 §5）。

### 5.3 旧流程兼容路径与切换步骤

**不做大爆炸切换。** 四条兼容路径：

| 阶段 | 下载写哪里 | 导出从哪里读 | 旧产物 |
| --- | --- | --- | --- |
| A（Slice 42~44 完成后） | 仓库（新增） | 旧流程不变（直接写 mbtiles） | 完全不动 |
| B（Slice 44 收尾，开关） | 仓库 + 交付文件（双写） | 旧流程读交付文件 | 完全不动；双写期间磁盘占用上升，需预算提示 |
| C（Slice 45，默认） | 仓库 | 仓库（新导出引擎） | 旧文件只读；可作为"legacy 资产"被导入 |
| D（Slice 49） | 仓库 | 仓库 | 移除双写开关；旧文件由用户经"导入"迁移 |

**回退方式**：每一步都保留"用旧路径跑"的开关（设置项，不是环境变量），开关切换不影响已有文件。双写期间若仓库出错，交付文件仍然正确 —— 这是 B 阶段存在的唯一理由。

---

## 6. 仓库数据模型

### 6.1 目录

```
tile_repo/                                  ← 仓库根（用户可选，默认见 Slice 42 设置项）
  catalog.sqlite                            ← 中央目录，唯一权威
  shards/
    tianditu/epsg3857/256/vec_w/z00-06.sqlite
    tianditu/epsg3857/256/vec_w/z07-11.sqlite
    ...
  snapshots/
    <fingerprint>.json                      ← 导出快照清单（水位 + 坐标/摘要）
  repair/
    ledger.sqlite                           ← 故障坐标台账（隔离/待重下/已修）
  staging/                                  ← 导入与导出的临时区（同盘，保证 rename 原子）
  lock/                                     ← 写者租约（Slice 42 落地）
```

### 6.2 分片规则：四种方案对比

| 方案 | 文件数 | 单库规模 | 查询路由 | 导出友好度 | 结论 |
| --- | --- | --- | --- | --- | --- |
| 单库 + 表分区 | 1 | 无限增长，VACUUM 不可行 | 无 | 好 | **否** —— 全国 Z4~13 会到 GB 级以上，单文件不可维护 |
| 按 zoom 一级一库 | 4 × 19 = 76 | 高 zoom 单库仍大 | 纯函数 | 好 | 勉强，但文件多、连接频繁开关 |
| 按 (layer, zoom 带) | 4 × 4 = 16 | 每带跨度 4~5 级，规模被切开 | 纯函数 | **最好**（导出只要打开需要的 layer 带） | **推荐** |
| 按坐标哈希分桶 | 4 × N | 均衡但无业务含义 | 需哈希 | 差（每个导出都要开全部桶） | 否 |

**推荐：按 `(layer, zoom_band)` 分片**，band 由配置表定义（设计默认）：

| band | zoom | 用途 |
| --- | --- | --- |
| `z00-06` | 0~6 | 全国低精度底图 |
| `z07-11` | 7~11 | 省市级中精度 |
| `z12-14` | 12~14 | 城市/县 |
| `z15-18` | 15~18 | 任务区高精度 |

理由三条：① 与"原始图层分开管理 + 按需合成"天然对齐，导出只开需要的 layer；② 瓦片数随 zoom 指数增长，按带切开后**单库规模量级可控**；③ 分片数固定且小（最多 16），路由是纯函数，不需要跨库查询。

`band` 边界**不写死**在代码里，放 `catalog.sqlite` 的 `zoom_bands` 表；改 band 是一次显式的"重建索引"动作（Slice 49）。

### 6.3 分片内表结构

```sql
-- 每个分片文件独立建这几张表
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;

-- 坐标 → 内容摘要。行很小（约 50 字节），PK 紧凑
CREATE TABLE coords (
  z        INTEGER NOT NULL,
  x        INTEGER NOT NULL,
  y        INTEGER NOT NULL,          -- 一律 XYZ（不是 TMS，见 §7.4）
  sha256   BLOB    NOT NULL,          -- 32 字节原始摘要
  state    INTEGER NOT NULL,          -- 0=current 1=superseded 2=quarantined
  fetched_at INTEGER NOT NULL,
  epoch_id INTEGER NOT NULL,          -- 所属写入批次
  PRIMARY KEY (z, x, y, state)
) WITHOUT ROWID;

-- 内容池（内容寻址）。同一分片内自然去重
CREATE TABLE contents (
  sha256    BLOB PRIMARY KEY,
  size      INTEGER NOT NULL,
  encoding  TEXT    NOT NULL,         -- 'png' | 'jpg'
  first_seen INTEGER NOT NULL,
  data      BLOB    NOT NULL
);

-- 分片自己的元信息（不重复 catalog 的真值，只做自述与快速自检）
CREATE TABLE shard_meta (name TEXT PRIMARY KEY, value TEXT);
-- layer / projection / tile_size / z_min / z_max / schema_version
```

为什么 `coords` 不带 `data`：坐标表行若含大 BLOB，PK 索引会被溢出页撑爆（`WITHOUT ROWID` 更是把整行塞进索引）。拆开后坐标查询是纯索引查找，内容只在真需要时读 `contents`。

**不用 MBTiles 布局做仓库**：`tiles` + `tile_row(TMS)` 是交付契约，仓库用 XYZ + 内容寻址，两者语义不同。混用会让"翻转发生几次"变成不可控（现状已在两处翻转，见 §3.4）。

### 6.4 中央 catalog

```sql
CREATE TABLE sources (            -- 数据源（不含任何凭证）
  source_id          TEXT PRIMARY KEY,    -- 'tianditu'
  provider           TEXT NOT NULL,
  projection         TEXT NOT NULL,       -- 'EPSG:3857'
  tile_size          INTEGER NOT NULL,    -- 256
  config_fingerprint TEXT NOT NULL        -- 对 base_url 模板+图层集合做的 sha256，**不含 token**
);

CREATE TABLE assets (             -- 资产 = (source, layer) 的一条逻辑流
  asset_id   TEXT PRIMARY KEY,      -- 'tianditu/vec_w'
  source_id  TEXT NOT NULL,
  layer      TEXT NOT NULL,         -- vec_w | cva_w | img_w | cia_w
  role       TEXT NOT NULL          -- base | label
);

CREATE TABLE shards (             -- 分片注册（存在的才登记）
  asset_id  TEXT NOT NULL,
  z_min     INTEGER NOT NULL,
  z_max     INTEGER NOT NULL,
  path      TEXT NOT NULL,
  bytes     INTEGER NOT NULL,
  tile_count INTEGER NOT NULL,
  checked_at INTEGER,
  PRIMARY KEY (asset_id, z_min)
);

CREATE TABLE zoom_bands ( z_min INTEGER PRIMARY KEY, z_max INTEGER NOT NULL );

CREATE TABLE coverage (           -- 粗粒度覆盖矩形，不是逐瓦片
  asset_id TEXT NOT NULL, z INTEGER NOT NULL,
  x_min INTEGER, x_max INTEGER, y_min INTEGER, y_max INTEGER,
  PRIMARY KEY (asset_id, z)
);

CREATE TABLE epochs (             -- 写入批次/水位
  id INTEGER PRIMARY KEY, at INTEGER NOT NULL, kind TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE snapshots (          -- 导出快照清单
  fingerprint TEXT PRIMARY KEY, created_at INTEGER NOT NULL,
  epoch_id INTEGER NOT NULL, path TEXT NOT NULL
);

CREATE TABLE legacy_assets (      -- 合成资产/未知来源，只登记不入仓
  id TEXT PRIMARY KEY, path TEXT NOT NULL,
  kind TEXT NOT NULL,             -- synthetic_merged | synthetic_single | unknown
  metadata_json TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
);
```

**catalog 里没有逐瓦片行**，这是硬约束。规模算一下就清楚：满覆盖时 Z13 单级全球 `4^13 = 67,108,864` 张；把每张都放进中央索引，索引本身就会超过瓦片数据量级，而且导入/导出每次都要跨库 JOIN。逐瓦片事实由分片的主键回答，catalog 只回答"哪些分片存在、覆盖哪几级、粗范围在哪"。

---

## 7. 关键机制

### 7.1 状态机

**资产状态**（`legacy_assets.kind` 之外的运行态）：

```
        ┌──────────┐  导入/下载      ┌──────────┐
        │  absent  │ ───────────────▶│ partial  │
        └──────────┘                 └────┬─────┘
                                          │ 覆盖率达到阈值
                                          ▼
                                     ┌──────────┐
                                     │ complete │◀── 更新写入（epoch+1）
                                     └────┬─────┘
                                          │ 对账发现缺/坏
                                          ▼
                                     ┌──────────┐
                                     │ degraded │── 修复 ──▶ complete
                                     └──────────┘
```

`partial` / `degraded` 是**推导值**（由 `coverage` 表 + 健康检查算出来），不落库 —— 落库就会与事实漂移。

**单张瓦片状态**（`coords.state`）：

```
quarantined(2) ──重下成功──▶ current(0) ──授权替换──▶ superseded(1)
        ▲                        │                        │
        └──── 深检判坏 ───────────┘                引用归零→可清理
```

**导出状态**：

```
planned → filling(可选) → building → verifying → published
                │             │          │
                └─ cancelled ─┴─ failed ─┴─▶ staging 清理，final 不动
```

### 7.2 一致性：catalog 与分片

**前提（必须写进实现注释）**：SQLite **不支持跨多个数据库文件的原子提交**。`ATTACH` 可以跨库写，但事务在任一库崩溃就各库各自回滚，没有两阶段提交。所以规则是：

1. **分片是事实，catalog 是缓存。** 任何"catalog 说 A、分片说 B"的冲突，一律以分片为准，并触发该分片的重建登记。
2. **写入顺序固定**：先写分片并提交 → 再更新 catalog（`shards` / `coverage` / `epochs`）。中断在第二步只造成 catalog 落后，可由 `rebuild` 修回。
3. **幂等补偿**：`rebuild_index(asset_id, z_min)` 只读打开分片，用 `SELECT COUNT(*), MIN/MAX` 重算 `tile_count/bytes`，与 catalog 比对后修正。
4. **不跨分片事务**。一次导入/导出被设计成"每个分片一个独立事务 + 全局 epoch 记账"，部分成功是**允许**状态（下次续做）。

### 7.3 并发读写与崩溃恢复

| 场景 | 规则 |
| --- | --- |
| 多个写入者同一分片 | **禁止**。分片级写租约：`lock/<asset>-<band>.lock`（内容 = 进程 id + 启动时间 + 心跳时间戳）。抢不到租约 → 返回 `Busy`，不阻塞等待 |
| 写进程崩溃 | 租约过期（默认 90 秒无心跳）即可被接管；接管方先做 `PRAGMA quick_check`，失败转 `Corrupt` |
| 读与写并发 | 分片开 WAL，读不阻塞写；导出**必须**在 `BEGIN` 后建立读快照（沿用 `mbtiles_optimizer.py:execute_export` L396 的写法），保证一次导出看到一致的 epoch |
| catalog 写入 | 单进程内串行；跨进程用同一个租约机制（catalog 级租约） |
| 崩溃留下的 staging | 启动时按 mtime 清理超过 24 小时的 `staging/*`；**不动** `shards/` |
| 半写分片 | `shard_meta` 里的 `schema_version` 缺失或与代码不符 → 该分片标记 `Corrupt`，不参与导出（不静默跳过，要在报告里列出） |

### 7.4 XYZ / TMS：只允许一次翻转

**规则**：仓库内部**一律 XYZ**。TMS 只存在于"写 MBTiles 的那一行代码"。

```
provider(z,x,y XYZ) → 仓库 coords(z,x,y XYZ)
                          │
                          └─ exporter：y_tms = 2**z - 1 - y   ← 唯一翻转点
                                          │
                                          ▼
                                    交付 MBTiles（TMS）
```

落地要求：exporter 写完抽样回读（复用 `mbtiles_optimizer.py:verify_osmdroid_reads` L327-350 的思路），断言"写入的 y 与回读的 y 互为翻转"。没有这条断言，二次翻转只会表现为"地图上下颠倒"，而且要等人肉看图才能发现。

### 7.5 内容去重范围与清理条件

- **去重范围**：单个分片内（内容池按 `sha256` 主键）。不跨分片、不跨 layer —— 跨 layer 的"同字节"几乎不存在，跨分片共享会引入引用计数的跨库一致性问题（比省下的空间更贵）。
- **哈希冲突**：`sha256` 是 256 位，冲突概率可忽略但仍要校验 —— 命中后**比较字节长度 + 前 512 字节 + 全字节**（沿用 `mbtiles_optimizer.py` L310-311 的"命中后比真实字节"做法），不一致直接 `Corrupt`，不静默接受。
- **安全清理条件**（三条全满足才删 `contents` 行）：
  1. 该 `sha256` 在 `coords` 中**没有被任何 `state` 引用**；
  2. 该分片**没有活跃读快照**（`snapshots` 里最近 N 份引用的 epoch 不含它）；
  3. 不在 `repair/ledger.sqlite` 的待办里。
- 清理是**显式动作**（Slice 49 的 `prune`），不是后台自动跑 —— 自动回收会在"用户正要导出"的时候把内容删掉。

### 7.6 资产冲突与更新规则

| 情况 | 规则 | 理由 |
| --- | --- | --- |
| 同坐标、`sha256` 相同 | 只更新 `fetched_at`，不新增内容行 | 幂等前提；无 ETag 时靠它判断"内容没变" |
| 同坐标、`sha256` 不同、未授权替换 | **拒绝**并记 `Conflict`（该坐标保留旧值） | 现状 `INSERT OR REPLACE` 无条件覆盖，正是"注记混写"的土壤 |
| 同坐标、`sha256` 不同、`allow_replace=True` | 旧行转 `superseded`，新行 `current`，`epoch+1` | 修复/更新任务的合法路径 |
| 同一坐标出现两个不同 `encoding` | 视为不同内容（`sha256` 必然不同）→ 走上面两条 | PNG/JPEG 字节不可互换 |
| 导入旧包时的冲突 | 记 `conflict` 并**不覆盖**，写进报告 | 旧包来源不可信，不该悄悄改仓库 |

版本保留策略：**默认只保留 `current` + 一世代 `superseded`**，`superseded` 超过 30 天或超过配额由 `prune` 回收。**导出快照不靠"保留历史"实现**，靠 `snapshots/<fingerprint>.json` 里的 `(coord → sha256)` 清单；清单在、内容在，就能复现；内容被回收则快照标记 `expired`（导出前显式检查，不静默降级）。

---

## 8. 关键流程

### 8.1 下载 → 入仓（Slice 44）

```
请求（区域 + 分层 zoom + 图层 + 模式）
  → 算缺口（对仓库查询，不是对目标文件查询）
  → 下载 worker：按 layer 分别取，**不合层**
        ├─ 成功 → tilerepo.put(asset, coords, bytes)
        └─ 失败 → 记 repair ledger
  → 全部完成 → 更新 coverage / epochs
  → （可选）触发导出
```

与现状的差异只有两处，但都是根因级：**写哪里**（交付文件 → 仓库）、**写什么**（合成图 → 原始层）。

### 8.2 旧包导入（Slice 43）

```
只读打开源文件（mode=ro + query_only）
  → 读 metadata → classify() → RAW / SYNTHETIC_* / UNKNOWN
  → 建立扫描水位（import_state）
  → 流式遍历 tiles（fetchmany，不 materialize）
        ├─ magic bytes 不合法 → quarantine，记 ledger
        ├─ 与仓库同坐标同摘要 → already_present
        └─ 其余 → 写分片
  → 结束：对账（catalog 登记 + 抽样回读）
```

**幂等键** = `(asset_id, z, x, y, sha256)`。中断后重放安全：已入仓的会命中 `already_present`。

### 8.3 导出（Slice 45）

```
导出计划（区域/分层 zoom/模式/注记/压缩/分包）
  → 冻结快照（epoch 水位 + 坐标清单）
  → 缺口计算
        ├─ local_only      → 报告缺图但继续（缺的部分不写）
        ├─ fill_then_export→ 必须先由用户确认调用下载；下载完重新冻结快照
        └─ reject_if_missing → 缺口非空即中止
  → 按包写 staging：逐坐标取内容 → 需要注记则 compose() → 写 TMS → 写 manifest
  → 校验（复用 validate_mbtiles + package_validator）
  → 原子发布（os.replace）
  → 失败：清 staging，final 不动
```

**不隐式联网**：`exporter` 的依赖里**没有** `downloader`（见 §5.1 的禁止边）。要补图必须是调用方在导出**之前**显式执行，并且有独立的用户确认。

### 8.4 增量交付（Slice 48）

复用 Android 已验证的协议（`OfflinePackageUpdater` L33-111），补 Windows 侧生成器：

```
两个已发布包目录（base / target）
  → 校验 DeliveryIdentity 完全相同（否则拒绝，要求整包）
  → 逐坐标 diff：changed / deleted
  → 写 patch.mbtiles + delta.json（deltaVersion=1）
  → 算 baseContentSha256 / targetContentSha256（内容摘要，与页布局无关）
  → 原子发布
```

`DeliveryIdentity` 的取值集合不是新设计 —— 它就是 Android 已经在强制的那组（模式/格式/范围/zoom/region id/boundary version），见 `create_offline_delta.py` L29-35 的元组。

---

## 9. 设计决策

| # | 决策 | 理由 | 被否方案 |
| --- | --- | --- | --- |
| D1 | 仓库只存**原始图层**，合成在导出期 | 合成不可逆（`alpha_composite`）；分存才能"一次获取多次复用"；合成后的 PNG 明显大于原生 JPEG（见 §3.9 实测的量级差异） | 存合成结果 —— 等于每个"注记组合"都要重下一遍 |
| D2 | 仓库格式**不复用 MBTiles 表结构** | MBTiles 是交付契约（TMS + flat/normalized 两套布局）；仓库用 XYZ + 内容寻址，语义不同 | 直接用 MBTiles 当仓库 —— 翻转点会再次扩散，normalized/flat 两套布局会渗进仓库 |
| D3 | 按 `(layer, zoom_band)` 分片 | 16 个文件封顶、路由纯函数、导出只开需要的带 | 按 z 一级一库（76 文件）、单库分区（不可 VACUUM）、哈希分桶（导出要开全部桶） |
| D4 | catalog **不建逐瓦片索引** | Z13 单级全球 6,710 万张（`4^13` 算术值），逐条索引的规模与代价超过收益 | 中央全量索引 —— 导入/导出都要跨库 JOIN |
| D5 | 分片内 = `coords` + `contents` 分离 | 坐标表行小、PK 紧凑；内容天然按摘要去重 | 单表含 BLOB —— PK 索引被溢出页撑爆 |
| D6 | 内容寻址（`sha256`）而不是自增 id | 天然去重、天然幂等、可与交付包摘要对齐 | 自增 id —— 去重要额外索引，且无法与 manifest 摘要对齐 |
| D7 | 同坐标不同内容**默认拒绝** | 现状无条件覆盖会静默混写；显式 `allow_replace` 才允许 | 保持 REPLACE —— 无法区分"更新"与"串了图层" |
| D8 | 版本保留 = current + 一世代 superseded | 仓库主目标是复用，不是"瓦片 Git"；历史代价线性增长且无回收依据 | 全历史保留 / 不保留任何历史（前者失控，后者无法回滚刚发布的版本） |
| D9 | 快照靠**清单**而不是保留历史 | 清单小、可审计、可在导出前显式检查 `expired` | 靠"导出时假设没变" —— 现在就是这么做的，只是没人验证 |
| D10 | 写租约（文件锁）而不是"靠 SQLite 报错" | 现状零锁（§3.2），多进程只在 `database is locked` 时才发现 | 继续无锁 —— 交付文件被两个进程写就等于静默损坏 |
| D11 | 翻转只在 exporter 发生一次 | 现状已两处翻转（写/读），再加仓库层就是三处 | 仓库也存 TMS —— 每层都要记得翻 |
| D12 | 预算拆四类口径 | "仓库占用/交付大小/处理峰值/设备预留"数量级与失败模式都不同，一个数字回答不了 | 沿用单 `BudgetReport` —— 峰值被低估，设备侧预留完全缺失 |
| D13 | `fill_then_export` 必须显式确认 | 目标原则"不隐式联网"；用户点"导出"不等于同意"再去下载几万张" | 导出时自动补图 —— 会在地铁里、在计费网络里悄悄跑起来 |
| D14 | 缺图**不影响 PASS**，但影响**导出许可** | 现状：覆盖缺失只 WARNING（`diagnostics.py` L1179-1213）。校验（"这个文件坏不坏"）与导出（"这份交付能不能发"）是两件事 | 把缺失改成 FAIL —— 会让所有局部下载的历史文件变成"不合法" |

---

## 10. 兼容矩阵

### 10.1 仓库能力 vs 现有产物

| 产物形态 | 可导入 | 可参与新合成 | 可导出复用 | 备注 |
| --- | --- | --- | --- | --- |
| flat `tiles` + `composition=single` | ✅ | ❌ | ✅（整块复用） | 只有底图，但仍是"合成后只剩一层"，无法与注记配对 |
| flat `tiles` + `composition=merged` | ✅（标 `SYNTHETIC_MERGED`） | ❌ | ✅（整块） | **不得**当原始底图 |
| normalized（`map`+`images`+`tiles` VIEW） | ✅ | ❌ | ✅ | 同 merged 限制；且 `export_readonly=true` 不可原地修复 |
| 带 `coverage_data` 的多级包 | ✅ | ❌ | ✅ | 覆盖规则可读出，用于"已知覆盖"判断 |
| 缺 metadata 的野包 | ⚠️ 需人工确认 | ❌ | ⚠️ | `legacy_meta` 存原键值；`coverage_quality=unknown` |
| zip 归档 | ❌ | ❌ | ❌ | 不是本项目产物；Android 离线协议也不收（§3.8） |

### 10.2 manifest 字段兼容

| 变更 | 是否升 `formatVersion` | 依据 |
| --- | --- | --- |
| 新增可选字段 | 否 | Android `from_dict` 忽略未知键（`OfflineMapManifest` 注释：要么显式支持、要么显式忽略） |
| 新增必填字段 | 是 | 老客户端会忽略它 → 语义丢失 |
| 改字段语义（同名字段换含义） | 是 | 最危险的一种，禁止 |
| 删字段 | 是 | 客户端读到缺失会按"可选"处理 → 静默降级 |

### 10.3 四种身份必须分开（Android 侧已实现三套，别混）

| 身份 | 作用域 | 现状载体 | 是否含内容 |
| --- | --- | --- | --- |
| **资产身份** `AssetIdentity` | 仓库内 | 不存在（本设计新增） | 否（坐标身份）+ 是（内容摘要） |
| **覆盖策略身份** `DeliveryIdentity` | 交付包之间 | manifest 的 `mapType/labels/composition/tileScheme/tileFormat/zoom/bounds/region`（`create_offline_delta.py` L29-35 的元组） | 否 |
| **交付包 ID** `package id` | Android 目录名 | `make_package_id(name, date)`（`package_builder.py` L152-179） | **否** ← 缺口 |
| **版本号** | 协议 | `formatVersion=1`（Windows）/ `SUPPORTED_FORMAT_VERSION=1`（Android） | 否 |

**推荐**：`package id` 保持现状（它是目录名，改它会破坏已装包的识别），**新增可选字段 `contentId`** = `sha256(map.mbtiles)` 前 16 位，写进 manifest。这样"同名同日期不同内容"能被区分，且老客户端忽略它不影响任何行为。

---

## 11. 待决事项与推荐默认值

| # | 问题 | 现状证据 | 推荐默认值 | 影响面 |
| --- | --- | --- | --- | --- |
| 1 | 覆盖缺失到底算 FAIL 还是 WARNING | `diagnostics.py:_report_coverage` L1179-1213 明确"绝不判 FAIL"，注释引用 Spec §21 | **保持 WARNING**（校验语义），新增"导出许可"独立判定：`reject_if_missing` 策略下缺口非空即拒绝导出 | 若改成 FAIL，历史局部下载的文件全部变为不合法 |
| 2 | GB 还是 GiB | 预算是 `1024**3` 且写 "GiB"，展示函数除以 1024 却写 "GB"（`tile_math.py` L513-530） | 统一显示 **GiB**，改文案不改数值 | 用户看到的数字不变，单位变准确 |
| 3 | `DATABASE_OVERHEAD=1.08` 依据 | `storage_budget.py` L20 无说明 | 保留 1.08 作为**默认值**，但标注"经验系数，待 Slice 46 用实测替换" | 改它会让所有历史预估跳变，不宜顺手改 |
| 4 | 分片 band 边界 0-6 / 7-11 / 12-14 / 15-18 | 本设计 | 采纳，写入 `zoom_bands` 表可改 | 改 band 需要重建索引 |
| 5 | superseded 保留期 | 本设计 | 30 天 / 或内容池超配额先删 superseded | 影响可回滚窗口 |
| 6 | 写租约超时 | 本设计 | 90 秒无心跳可接管 | 太短会误抢，太长会卡住崩溃恢复 |
| 7 | 导入时源库存在 `-wal` | 现状无处理 | **警告 + 要求确认**，不自动合并；确认后按只读快照导入 | 误读活跃库会导入不完整数据 |
| 8 | 导出时"目标级齐全"的定义 | `diagnostics.check_coverage` L897-1050 用 `calculate_tile_range` + 几何相交 | 目标级 = 该 zoom 的 `selected_tiles` 全部在快照里；**父级回退不计入** | 把回退算成齐全会产出"以为有高清其实没有"的包 |
| 9 | `package id` 是否加内容维度 | `make_package_id` L152-179 只吃名字+日期 | 不改 id，新增可选 `contentId`（§10.3） | 改 id 会破坏已装包识别 |
| 10 | 增量生成器放哪个仓库 | 现在在 `osmdroid/scripts/create_offline_delta.py` | 迁到 Windows `app/exporter/delta.py`，Android 侧保留同一个协议测试 | 跨仓库脚本是当前的"单点"，Windows 侧无法自测 |

---

## 12. 假设清单（未测量，不能当事实引用）

| # | 假设 | 影响 | 验证方法 |
| --- | --- | --- | --- |
| A1 | 分片内内容去重能省多少空间 | 决定 `contents` 表的价值 | Slice 42 收尾时对 1~2 个真实分片跑 `sum(size) vs sum(distinct size)` |
| A2 | 原始四图层总占用 ≈ 现合成包的多少倍 | 决定"仓库比交付大多少"的用户预期 | Slice 43 导入一个真实包后实测 |
| A3 | `coords` + `contents` 的查询性能足以支撑导出 | 决定是否需要额外索引 | Slice 45 用真实分片跑 1 万坐标导出计时 |
| A4 | 按 band 分片后单库规模落在百 MB~GB 级 | 分片规则的合理性 | Slice 44 全国 Z4~13 实测各分片字节 |
| A5 | 增量包平均只有全量的几个百分点 | 增量交付的价值 | Slice 48 用两次真实导出做 diff |
| A6 | 无 ETag 时的"重取+比对"成本可接受 | 更新任务的可行性 | Slice 48 对 1 万张做更新一轮，计时 |

**明确不属于假设的实测值**：§3.9 表格里的字节数、瓦片数、平均字节，以及 §3 各处引用的默认常量（都能在 `app/config/settings.py` 找到行号）。

---

## 13. 相关文档

- 字段级契约、接口草案、预算与打包契约：[tile_repository_contracts](tile_repository_contracts.md)
- Slice 42~49 实施计划与验收矩阵：[tile_repository_roadmap](tile_repository_roadmap.md)
- 现有打包与跨端字段：[map_package_v1](map_package_v1.md)
- 现有压缩/去重/分包：[storage_optimization](storage_optimization.md)
- 现有混合 Zoom 覆盖：[zoom_coverage](zoom_coverage.md)
- 现有容量预算：[storage_budget](storage_budget.md)
- 现有损坏检查与定向重下：[tile_repair](tile_repair.md)
- 分级体系总纲（含 Android AUTO 选包）：[hierarchical_offline_map](hierarchical_offline_map.md)
