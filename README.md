# 天地图离线地图工具

> **一句话**：在 Windows 上把天地图的地图下载成标准的离线地图包，
> 传到 Android 手机上装进 osmdroid，**拔了网也能看地图**。
>
> | 版本 | 值 | 定义处 |
> | --- | --- | --- |
> | 工具版本（Windows Tool Version） | **0.1.0** | `app/config/settings.py` → `APP_VERSION` |
> | 地图包格式版本（Package Format Version） | **1** | `app/config/settings.py` → `PACKAGE_FORMAT_VERSION` |
> | Android 运行时版本（Android Runtime Version） | **1.0.0-android** | `osmdroid/.../offline/config/MapRuntimeConfig.java` → `OFFLINE_MAP_RUNTIME_VERSION` |
>
> 三个版本号**各自只有一个定义处**，别在别的地方再写一遍（会走样）。
> 两端的一致性由 `tests/test_package_protocol_v1.py`（Windows）与
> `OfflineMapManifestProtocolV1Test`（Android）跨仓库机械核对。

## 目录

- [**文档地图**](docs/README.md) —— 按"我想干什么"找文档（全部专题文档的入口）

**它是什么 / 怎么装**

- [这套东西是什么](#这套东西是什么) —— 架构与完整链路
- [快速开始](#快速开始) —— 五分钟跑通一次
- [当前进度](#当前进度) —— 已经做到哪一步
- [环境](#环境) · [安装](#安装) · [启动](#启动)
- [代码检查](#代码检查) —— ruff

**怎么用**

- [用法：测试天地图连接](#用法测试天地图连接)
- [用法：下载并生成 MBTiles](#用法下载并生成-mbtiles)
- [用法：断点续传（接着上次下）](#用法断点续传接着上次下)
- [用法：检查 MBTiles（诊断已有包）](#用法检查-mbtiles诊断一个已有地图包)
- [用法：生成离线地图包](#用法生成离线地图包)
- [用法：传输到 Android 设备](#用法传输到-android-设备)
- [Android 端：导入与使用](#android-端导入与使用)
- [三种底图模式：ONLINE / OFFLINE / AUTO](#三种底图模式online--offline--auto)
- [地图包格式](#地图包格式) —— 跨端契约

**怎么验**

- [测试：按模块跑](#测试按模块跑)
- [测试：全量与端到端脚本](#测试全量与端到端脚本)
- [文档自检](#文档自检)
- [端到端验收](#端到端验收)
- [常见问题](#常见问题)
- [后续计划](#后续计划)
- [开发记录（按 Slice）](#开发记录按-slice) —— **历史章节，按需查阅**

> 专题文档（行政区域与层级 / Zoom 策略 / 计划与调度 / 任务区 / 风险分析 /
> 行政区数据导入 / 分级离线体系 / 端到端验收清单 / 排查手册）统一从
> [docs/README.md](docs/README.md) 进 —— 那里按任务分组，并标了推荐阅读顺序。
> **不要按 Slice 编号顺序读那些文档**：编号是开发顺序，不是知识依赖顺序。

---

## 这套东西是什么

完整链路（从按下"下载"到手机上看见地图）：

```text
                    ┌───────────────────── Windows（本仓库） ─────────────────────┐
                    │                                                             │
  天地图 WMTS 服务   │   GeoBounds ──→ Tile Math ──→ TiandituProvider              │
  （在线，需 Token）  │       │             │                 │                     │
        │           │       │             │                 ▼                     │
        └───────────┼───────┼─────────────┼────────→ Downloader（并发/限速/重试）│
                    │       │             │                 │                     │
                    │       │             │                 ▼                     │
                    │       │             └──────→ MBTiles（SQLite 瓦片库）       │
                    │       │                               │                     │
                    │       │                               ▼                     │
                    │       │                      Composer（注记合成，可选）      │
                    │       │                               │                     │
                    │       │                               ▼                     │
                    │       │                        Inspector（结构检查）        │
                    │       │                               │                     │
                    │       │                               ▼                     │
                    │       └──────────────→ Package Builder（manifest + SHA256） │
                    │                                       │                     │
                    └───────────────────────────────────────┼─────────────────────┘
                                                            │
                          ① USB / adb push     ② U 盘 / 网盘 / 任意方式
                                                            │
                                                            ▼
                    ┌──────────────────── Android（osmdroid 仓库） ────────────────┐
                    │                                                             │
                    │  OfflineMapImporter ──→ 校验(size→SHA256→SQLite 结构)        │
                    │         │                                                   │
                    │         ▼                                                   │
                    │  OfflineMapRegistry（扫目录、判可用、清理残留）               │
                    │         │                                                   │
                    │         ▼                                                   │
                    │  MapResourceManager（ONLINE / OFFLINE / AUTO）              │
                    │         │                                                   │
                    │         ▼                                                   │
                    │  osmdroid Provider ──→ MapView（屏幕上看见地图）             │
                    │                                                             │
                    └─────────────────────────────────────────────────────────────┘
```

**关键点**：

- **单向数据流**。`manifest.json` 是两端的契约文件，Windows 只写、Android 只读。
  格式定义见 [`docs/map_package_v1.md`](docs/map_package_v1.md)。
- **离线不等于"缓存"**。OS 的 HTTP 缓存会过期，这套流程产出的是**真正的本地瓦片库**，
  拔网、断网、飞行模式下都能用。
- **传文件的方式不重要**。`adb push` 只是最方便的一种；拷到 SD 卡、
  发到网盘再从手机下载，效果完全一样。ADB 不是必需的。

## 快速开始

假设你只想尽快看到效果：

```bash
# 1. 准备环境（见下面「安装」）
cd D:/project/MapDownloader
.venv/Scripts/python.exe -m app.main

# 2. 在界面里填天地图 Token（偏好设置），选一块范围与层级，点下载
# 3. 下载完点「检查」，通过后点「生成地图包」
# 4. 把包目录传到手机
D:/develop/android/sdk/platform-tools/adb.exe push "<包目录>" /sdcard/Download/

# 5. 手机上：离线地图 → 导入 → 选到那个目录 → 切到 OFFLINE
```

**不想等下载？** 跑一遍离线冒烟测试，几秒内验证整条链路是通的：

```bash
.venv/Scripts/python.exe scripts/e2e_smoke_test.py
```

它用 Mock 瓦片源（不访问天地图、不需要 Token），把
`Tile Math → MBTiles → Inspector → Package → SHA256` 完整跑一遍。

---

## 项目目标

Windows端生成天地图离线地图包，后续导入Android供osmdroid使用。

## 当前进度

Slice 29：按**无人机图基底**重新标定 Zoom 预设。离线包导入 Android 设备后的用途是
无人机的地图基底，判据从"行政区多大"换成"地面分辨率 + 数据源精度"。新增
`app/planning/ground_resolution.py`（Zoom ↔ 米/像素换算、`UAV_ZOOM_PRESETS` 四档、
`UAV_BASELINE_ZOOM = 16`），界面「推荐说明」多一行"换算到地面"（纬度取当前范围中心）。
预设改三处：全国 Z4~10 → **Z5~10**（Z4 已是色块）、城市 Z12~16 → **Z12~15**（南京全域
Z16 要 1.4 GB）、县级 Z14~18 → **Z14~17**（农村影像原生只到约 Z16，Z18 多为插值）；
任务区保持 Z15~18 —— 它是无人机主力档且范围天然小。**要一张数就给 Z16。**

上一个 Slice：Slice 27 —— 行政区层级扩到**县**（全国 → 省 → 市 → 县四级）。`RegionLevel` 新增
`COUNTY`，`PARENT_LEVEL_RULES` 从"父等级唯一"改成**父等级集合** —— 县的上级可以是市
（普通地级市下辖区县）**也可以是省**（直辖市下辖区 / 省直辖县级市没有市级上级，硬凑一个
中间层就是编数据）。下载面板多出「县区」下拉，直辖市与省直辖县选完省后市级下拉**自动跳过**。
另配套 `scripts/import_china_admin_regions.py` 导入管线，实测 **省 34 / 市 336 / 县 2882
+ 合成全国 1 = 3253** 个区域。

上一个 Slice（Android 侧）：Slice 25 —— 分级离线地图体系 E2E + 稳定性收尾（把 Slice 16~24
那条线**接成一个整体**：一条可自动跑的 10 段全链路验收 `scripts/hierarchical_map_e2e.py`
覆盖层级目录 → Zoom 策略 → 多边形估算 → 任务区 → 风险 → 下载计划 → 调度 → MBTiles →
Inspector → 打包；并修掉两个跨端缺陷 —— **行政区包的 `region.level` 从来没写进包里**，
以及**目录里的多边形区域无法续传 / 无法被 Inspector 认出来**。详见
[`docs/hierarchical_offline_map.md`](docs/hierarchical_offline_map.md)）

再往前（Android 侧）：Slice 24 —— AUTO 按「位置 + Zoom + 区域等级」选包

再往前（Windows 侧）：Slice 23 —— 下载风险分析（把"这一单大不大"从**一个 1 万张的阈值**
升级成**一层结论**：按张数与容量两个维度分四档 —— 正常 / 较大 / 很大 / 危险，给出原因、
建议与逐 Zoom 明细；面板、日志、确认框读的是同一份报告。启动前按等级保护：较大与很大
各确认一次（问号 / 警告图标），危险档要求**手输确认词**；计划页有「风险」列与总风险，
开始计划时四选一（继续 / 采用推荐 Zoom / 停用危险项 / 返回修改）。阈值全在
`app/planning/risk_policy.py` 一处，界面里没有任何阈值数字）

完整链路：

```text
    ┌──────────────── 分级的三件事实（Slice 16~25）────────────────┐
    │ RegionCatalog          (Slice 18：行政层级 国 ⊃ 省 ⊃ 市 ⊃ 县， │
    │    ↓                    目录校验；边界数据不进仓库)            │
    │ RegionLevel            (Slice 17：country/province/city/       │
    │    ↓                    county/mission/custom —— 它是跨端契约， │
    │                         县的父级允许市**或**省，Slice 27)      │
    │ ZoomStrategyService    (Slice 17：等级 → 推荐 Zoom 区间，      │
    │                         全项目唯一的阈值来源；界面只显示，    │
    │                         绝不改写用户输入)                      │
    └───────────────────────────────────────────────────────────────┘
BoundingBox            (Slice 1：范围与合法性校验)
    ↓
validate_form          (Slice 7：唯一的下载前校验入口，一次报出全部问题)
    ↓
GeoJSON / Preset       (Slice 16：行政区域边界加载；缺文件就明确报错，绝不退回矩形)
    ↓
Tile Math              (Slice 1：算出瓦片编号区间)
    ↓
RegionTileEstimator    (Slice 16：候选 = 外包矩形；区域内 = 与区域相交的那些。
    ↓                   进度分母 / 大任务提示 / 容量预估**全部只用「区域内」**)
PlanBuilder            (Slice 20：把"国家 / 省 / 市"逐块加进一份计划；推荐 Zoom
    ↓                   一律来自策略层，自定义 Zoom 原样收下)
PlanEstimator          (Slice 20：后台线程逐项估算 + 汇总；多边形按**选中**张数，
    ↓                   进度信号 + 可取消)
PlanValidator          (Slice 20：错误拦下（区域没了 / 边界缺 / 没算过）/ 提醒放行
    ↓                   （超出推荐 / 规模偏大 / 目标文件已存在）)
PlanService.create_download_tasks()   ★ 只写 tasks.json，**不开始下载**
    ↓                                   单区域直下那条路仍然独立存在（不想用计划
    ↓                                   的人可以当它不存在）
DownloadTask × N       (Slice 5：任务记录，落盘到 data/tasks.json；Slice 6 增加 map_type / with_labels；
    ↓                   Slice 16 增加 region_type / region_id / boundary_version ——**不写 GeoJSON**)
ExistingTileScanWorker (Slice 7：后台线程数"已有多少张"——SQLite 不许碰主线程；
    ↓                   Slice 16：多边形任务只数"区域内"那些)
MapType → 两个图层      (Slice 6：MAP_TYPE_LAYERS 是全项目唯一的那张映射表)
    ↓
TiandituProvider       (Slice 2：拼 WMTS URL、判响应合法性)
    ↓
ExistingTileIndex      (Slice 5：查库里已有瓦片；默认只读坐标三列)

    ↓  region_filter.intersects_tile（Slice 16：区域外的瓦片在**发 HTTP 之前**丢掉，
    ↓                                不算失败、也不算跳过）
    ↓  should_skip_tile（在发 HTTP 之前判定）
Async Downloader       (Slice 3：并发 / 限速 / 重试 / 暂停取消)
    ↓
CompositeTileDownloader (Slice 6：底图 + 注记各请求一次 → TileComposer alpha 合成)
    ↓  on_tile 回调
MBTilesTileSink        (Slice 4：有界写队列 + 单消费者)
    ↓
MBTilesWriter          (Slice 4：批量事务写 SQLite，INSERT OR REPLACE；
    ↓                   Slice 16：metadata 里留下 region_type / region_id / boundary_version)
map.mbtiles
    ↓
Validator              (Slice 4：结构 + 抽样数据校验；Slice 6 增加合成信息自洽性)
    ↓
TaskService            (Slice 5：状态判定 + 落盘；启动时 DOWNLOADING → PAUSED)
    ↓
UiTaskState 矩阵        (Slice 7：把"状态"翻译成"哪几个按钮能点"，逐格可断言)
    ↓
MBTilesInspectionService (Slice 8：同一个检查工具，下载收尾时自动跑一次"快速"档；
    ↓                   Slice 16：多边形包按**几何**算期望值，边界不可用就标 UNKNOWN)
MBTilesInspectorDialog (Slice 8：手动打开的独立检查窗口，可快速 / 深度 / 导出报告)
    ↓
MapPackageService      (Slice 9：打包编排——输出目录命名、复用源检查结论、进度、结果)
    ↓  PackageBuildWorker（后台线程；GB 级文件不许冻住界面）
MapPackageBuilder      (Slice 9：copy 源文件 → 流式 SHA256 → 写 manifest.json
    ↓                   → 发布前自校验 → 临时目录 + 原子改名；源文件一个字节不动)
<输出目录>/<包名>/      { manifest.json, map.mbtiles }
    ↓
MapPackageValidator    (Slice 9：manifest 解析 / 文件存在 / 大小一致 / SHA256 对得上，
                       四道关 → 三行结论 + 人话失败原因)
    ↓
AndroidTransferService (Slice 10：设备发现 + 包校验 + 传输的编排入口；顺带记住
    ↓                   "这个目录刚校验过"，传输时不再重复哈希 GB 级文件)
AndroidDeviceWorker / AndroidTransferWorker
    ↓                   （Slice 10：后台线程；adb 命令与 push 绝不上 Qt 主线程）
AdbClient              (Slice 10：subprocess **列表参数**、禁用 shell、push 流式读
    ↓                   stdout+stderr、跨 chunk 解析百分比、取消即杀进程)
DeviceManager          (Slice 10：`adb devices -l` 解析 + getprop 补全；单台设备出错
    ↓                   不影响整表，只导 state=device 的设备)
MapPackageTransfer     (Slice 10：mkdir → push map.mbtiles.part → mv → push manifest.json
    ↓                   → 远端大小 + SHA256 校验；取消只删自己的 .part)
<设备>/sdcard/Download/TiandituOfflineMap/import/<manifest.id>/
                       { manifest.json, map.mbtiles }
    ↓
Android App 导入        (Slice 11，在 osmdroid 仓库)
```

**离线包的形状**（`formatVersion = 1`，这是给 Android 那侧看的契约）：

```text
<输出目录>/<包名>/
├── map.mbtiles      源文件的**副本**（不是移动、不是软链、不是引用）
└── manifest.json    camelCase、字段顺序固定、UTF-8、indent=2、末尾带换行
```

`manifest.json` 里有：`formatVersion` / `id` / `name` / `provider` / `mapType` /
`labels` / `composition` / `tileScheme` / `tileFormat` / `minZoom` / `maxZoom` /
`bounds { west, south, east, north }` / `tileCount` / `mbtiles { file, size, sha256 }` /
`createdAt`，以及**可选**的
`region { type, id, boundaryVersion, level [, bufferMeters, geometryVersion] }`
（Slice 16：多边形包带上"按哪块区域、哪版边界裁的"；Slice 25 起 `level` **所有区域都写** ——
它是唯一一条把"这是什么级别的区域"带过端的通道，Android 没有区域目录、反查不出来；
`bufferMeters` / `geometryVersion` 只有任务区有。老包没有这些键，读作矩形 + 未知等级）。三条硬约束：

- **不许出现绝对路径**——`mbtiles.file` 永远是 `map.mbtiles`，包换个盘符、换台机器
  都得能用；
- **不许出现 Token**；
- `sha256` 是对 **`map.mbtiles` 这一份文件**算的，不是对源文件算的。

`sha256` 的用途只有一个：**到了 Android 那侧、或者几个月后重新拿到这个包的时候，
能证明"这个 .mbtiles 就是我当初打进去的那一份"**——传输截断、磁盘静默损坏、有人
手改过，都过不了这一关。所以它必须是**流式**算的（4 MiB 一块），500 MB / 2 GB /
5 GB 都不能把文件读进内存；这条有 AST 级静态守卫 + 测试双重盯着。

## 开发记录（按 Slice）

以下是**按交付顺序**记录的历史章节，每节写的是"那一刀切了什么、为什么这么切"。
**不是必读**：想看现在有什么，读上面的[当前进度](#当前进度)；
想按任务找专题文档，读[文档地图](docs/README.md)。

读这些章节的正确姿势是**带着问题查**（"续传是怎么做的""打包为什么是原子的"），
而不是从头读到尾 —— 里面的命令与数字都是当时的快照。

---

## Slice 29：按无人机图基底重标定 Zoom 预设

用途变了：离线包不再只是"人在地图上浏览"，而是**导入 Android 设备做无人机的地图
基底**。这个用途下"该选几级"的判据是**地面分辨率**（米/像素），不是行政区大小 ——
用户在平板上关心的是"能不能看清那栋房子"，而 Zoom 编号说明不了这件事。

这一版补上了缺的那半套能力，并按两条硬事实重新标定预设：

**事实一：天地图的数据源到不了 Z18 那么精。** 主站 WMTS 的 `TILEMATRIX` 上限是
Z18，但"有瓦片"不等于"有数据"：地方节点公开的成果目录显示，**农村全域影像来自
高分一号 / 高分六号（2 米）**，城区才有高分二号 / 七号（0.8 米）。2 米影像对应
约 **Z16**，所以 Z17/Z18 在多数非城区只是插值放大，**只买体积，不买信息**。

**事实二：全国到 Z13 是 40 GB，不是"数 GB"。** 瓦片数每级 ×4，实测（矩形含海域）：
Z10 = 670 MB、Z12 = 10.4 GB、Z13 = **41 GB**。早先"全国 Z4~Z13"的估算是错的，
量级差了一个数量级。

标定结果：

| 等级 | 原 | 新 | 理由 |
| --- | --- | --- | --- |
| `COUNTRY` | Z4~10 | **Z5~10** | Z4 一像素 8.3 公里、全国只要 12 张瓦片，已是色块图 |
| `CITY` | Z12~16 | **Z12~15** | 南京全域 Z16 要 1.4 GB，Z15 只要 353 MB，且 Z15 够看清乡镇路网 |
| `COUNTY` | Z14~18 | **Z14~17** | Z18 多为插值；Z17 是"有真实增益"的最后一档 |
| `MISSION` | Z15~18 | **Z15~18**（不动） | 任务区是无人机主力档，范围天然小 —— 10 km² 到 Z18 才 178 MB |

新增 `app/planning/ground_resolution.py`：

- `meters_per_pixel(zoom, latitude)` —— Web 墨卡托换算，纬度影响 ±30%；
- `zoom_covering(meters, latitude)` —— 反解"要 1 米该用几级"；
- `describe_resolution(...)` —— 界面那一行"约 8.1 ~ 2.0 米/像素"；
- `UAV_ZOOM_PRESETS` —— 按用途的四档（转场 / 航线规划 / 作业区详查 / 取证）；
- `UAV_BASELINE_ZOOM = 16` —— **一张数就够时用它**。

界面上「推荐说明」多了一行换算，纬度取**当前范围的中心**（写死一个纬度会与实际
不符）。

一个副作用值得记：推荐区间收紧后，全国+省+市三级的默认配置从约 5.7 万张降到约
1.7 万张，**不再撞上"规模偏大"提醒**。推荐区间不只是建议，它同时是规模闸门 ——
`test_a_full_three_level_plan_no_longer_trips_the_size_reminder` 盯着这件事。

选档指南见 [docs/zoom_strategy.md](docs/zoom_strategy.md) 第 10 节（含完整体积表）。

---

## Slice 27：行政区层级扩到县（国 → 省 → 市 → 县）

Slice 26 打通了"授权数据能导进来"，但导进来的只有三层：国 ⊃ 省 ⊃ 市。真实下载
粒度到不了这里 —— 用户要的是"只要郑州惠济区"，而界面只能选到郑州市，于是把一个市
十几个区县的瓦片全下下来。这一版把层级补到县，并用**天地图官网真实数据**跑通了全链。

### 父级不再唯一：县的上级可以是市，也可以是省

原来 `PARENT_LEVEL_RULES` 是 `等级 → 唯一父等级`。县天然不满足这个模型：

- 普通地级市下辖区县 → 父级是**市**；
- 直辖市下辖区（北京朝阳区 / 上海浦东新区）→ **没有市级上级**；
- 省直辖县级市 / 县（济源 / 仙桃 / 海南直辖县 / 新疆兵团市）→ **也没有**。

给它们硬凑一个中间层就是**编数据**——编出来的层级会在估算、风险、计划三处一路
走样。所以规则改成**父等级集合**（`frozenset`），县允许挂市或省；父级必须真实
存在且等级在集合内，否则校验直接 FAIL。

### Zoom 策略：COUNTY 定在 Z14 ~ Z17

单县范围小，下到高 Zoom 才看得清街道，而总量不会失控。四个等级的推荐区间仍然
在 `app/planning/zoom_policy.py` **一处**定义，风险建议（`risk_policy`）与计划
顺序（`download_plan` 的 `PLAN_LEVEL_ORDER`）同步补上 COUNTY —— 少补一处的后果
是"计划里能选县、但排不进调度顺序"。

### 天地图三份 GeoJSON 怎么变成目录

```text
中国_省.geojson (42 个 Feature，含"境界线")
中国_市.geojson (388)
中国_县.geojson (2897)
  → 滤掉 gb 为空的"境界线"（国界 / 省界，不是行政区）
  → 省界 unary_union 合成全国条目（数据里没有全国边界，但省级必须挂在国家级下面；
     不凭空造矩形，用省界的并集当国界）
  → 父级从 gb 前缀推导：市 → gb[:3]+gb[3:5]+"0000"（省）
                        县 → gb[:3]+gb[3:7]+"00"（市），无市级上级时退到省
  → 走 Slice 26 的导入管线，原子发布到 resources/regions/generated/
```

`gb` 是 `"156" + 6 位 GB/T 2260 编码`，省码 `gb[3:5]`、市码 `gb[5:7]`、县码
`gb[7:9]`。**数据里没有 level、也没有 parent**，层级完全靠编码推。

### 三份文件互相重叠的单位，按编码规则消解

天地图把三类"行政级别有歧义"的单位同时放进了多个文件，直接合会撞 id：

| 重叠形态 | 个数 | 判据 | 归到 |
| --- | --- | --- | --- |
| 直辖市 / 港澳台（省 ∩ 市） | 7 | `gb` 已在省文件里 | 省（跳过市文件那份） |
| 澳门（省 ∩ 市 ∩ 县） | 1 | 同上 | 省 |
| 不设区的市（市 ∩ 县，东莞 / 中山 / 嘉峪关 / 儋州） | — | `gb[7:9] == "00"` | 市（跳过县那份） |
| 省直辖县级市 / 县（济源 / 仙桃 / 兵团市） | — | `gb[7:9] != "00"` | 县，挂省（跳过市那份） |

判据是**编码本身**（"后两位是不是 `00`"就是"设不设区"的编码表达），不是按名字
猜，所以将来数据更新也不会错。

### 界面：四级联动，该跳的级自动跳

下载面板多出「县区」下拉（首个条目是"全部（不限定县区）"）。选到直辖市或省直辖
县时，市级下拉**自动跳过**并给出说明文案，不会留一个空下拉让用户怀疑是不是坏了。

### 用真实数据验过的几何事实

| 检查 | 结果 |
| --- | --- |
| 区域总数 | **3253**（省 34 / 市 336 / 县 2882 / 全国 1） |
| 北京（直辖市） | 0 个市 → 16 个区，跳级正确 |
| 济源市 | `level=county`，父级是**河南省**，不是某个市 |
| 四级路径 | `中国/河南省/郑州市/惠济区` |

### 验收

```bash
# 全量单测
.venv/Scripts/python.exe -m pytest                                   # 全量（数字以实际输出为准）

# 主链路（离线）
.venv/Scripts/python.exe scripts/e2e_smoke_test.py                   # 6/6

# 分级全链路（离线）
.venv/Scripts/python.exe scripts/hierarchical_map_e2e.py             # 10/10

# 真实数据导入（先 dry-run，通过后去掉 --check 再发一次）
.venv/Scripts/python.exe scripts/import_china_admin_regions.py \
  --province "$HOME/Downloads/中国_省 (1).geojson" \
  --city     "$HOME/Downloads/中国_市.geojson" \
  --county   "$HOME/Downloads/中国_县.geojson" \
  --boundary-version 2026-10 --check
```

> 生成目录 `resources/regions/generated/` 整体 gitignore：边界数据的再分发条件
> 由提供方决定，换台机器要重跑一次导入脚本。

## Slice 26：行政区数据导入管线（Region Data Pipeline）

到 Slice 25 为止，分级链路的**数据**是手工维护的（`resources/regions/index.json` +
`province/*.geojson` + `city/*.geojson`）。真实工程不能靠人肉手写全国几百个省市边界。
这一版补的是"把授权数据导进目录"那条线。

### 一条管线：授权 GeoJSON → RegionCatalog 生成数据集

```text
Authorized Region Dataset (GeoJSON FeatureCollection)
  → SourceReader（字段名可配置，不假设外部数据用固定键名）
  → GeometryNormalizer（is_valid / WGS84 / make_valid 修轻伤记 warning）
  → HierarchyBuilder（COUNTRY→PROVINCE→CITY 校验：parent/duplicate/cycle/level）
  → CatalogGenerator（逐 Region GeoJSON + index.json + dataset manifest）
  → 全量校验（PASS / WARNING / FAIL）
  → Atomic Publish（临时目录 → 原子替换 resources/regions/generated/）
  → RegionCatalog（与手工数据同一套格式，无需知道来源）
```

### 关键取舍

- **字段名不假设**：`id_field / name_field / level_field / parent_field` 都可由用户配置；
  等级支持别名映射（`country/province/city`、`1/2/3`、`全国/省/市`、`直辖市→省级`）。
- **Region id 稳定且文件名安全**：优先用源数据里的行政区 code，不合
  `[A-Za-z0-9_.-]` 时统一 sanitize 并保留 `originalCode`，不拿中文名称随机生成。
- **默认不 simplify**：行政边界是"裁到哪个瓦片"的唯一依据，默认禁用几何简化，
  避免边界 Tile 漏下载。
- **原子发布**：source → 临时 build 目录 → 全量校验 → 原子替换，失败保留旧数据。
- **直辖市兼容**：直辖市经等级映射落省级；源数据没有市级层时省照常可用，**不伪造
  不存在的 City Feature**。
- **合规**：只提供导入工具，不下载、抓取或分发来源不明的行政边界数据；生成目录整体
  gitignore（含其 index.json/manifest.json）。

### 接入与 UI

- `RegionCatalog` 支持 `REGION_DATA_ROOT`（默认 `resources/regions/generated/`），
  `effective_region_root()` 选"已发布**且可解析**"的生成目录，否则回退手工目录；
  `RegionService.reload()` 失败保留旧 Catalog。
- GUI「行政区数据管理」（文件菜单）：字段映射下拉从源数据 properties 键动态填，
  [验证] 显示 Spec §19 统计（FAIL 禁导入）、[导入] 原子发布；提示框非模态。
- CLI `scripts/import_regions.py`：全参数、`--check` dry-run、失败 non-zero exit。

### 这一 Slice 抓出来并修掉的缺陷

1. **静态守卫冲突**：`test_zoom_policy.py` 用 AST 拦"planning 层外硬编码
   `RegionLevel.COUNTRY`"。正解不是加白名单，而是让 importer 复用单一权威 ——
   层级判断用谓词（`is_country/is_province/is_city`）、计数用 `ADMINISTRATIVE_LEVELS`
   推导、沿用条目用 `entry.level`；只有 `DEFAULT_LEVEL_ALIASES`（外部字符串→等级映射，
   定义性质）进白名单。
2. **对话框构造即崩**：`_set_result_text` 初始化少传 `verdict` 关键字，从没测过构造
   所以没暴露 —— UI 测试抓出来的。
3. **坏 generated/index.json 会让用户退化成扁平预设**：`effective_region_root()` 从
   "文件在就算已发布"改成"存在**且可解析**"，坏文件正确回退手工目录。

### 验收

```bash
# 全量单测（含 6 组 importer/UI 用例 + 11 个自建几何夹具，不依赖真实中国边界）
.venv/Scripts/python.exe -m pytest                                   # 全量（数字以实际输出为准）

# 主链路（离线）
.venv/Scripts/python.exe scripts/e2e_smoke_test.py                   # 6/6

# 分级全链路（离线）
.venv/Scripts/python.exe scripts/hierarchical_map_e2e.py             # 10/10

# CLI 导入夹具（10 项确认：Dataset PASS / GeoJSON / index.json / parent / bounds /
# reload / Zoom Z5~10 Z9~13 Z12~15 / Polygon 估算）
.venv/Scripts/python.exe scripts/import_regions.py \
  --source tests/fixtures/regions/import/sample.geojson \
  --boundary-version 2026-10 --output /tmp/out
```

## Slice 25：分级离线地图体系 E2E + 稳定性收尾

到 Slice 24 为止，分级这条线上的每一环单独都测过了：多边形估算、Zoom 策略、
任务区、风险、计划、调度、打包、Android 选包。**但没有一次"从一块地一路走到一个能导入
的包"的完整验收** —— 而断裂的恰恰是接缝处。这一版补的就是接缝。

### 一条可自动跑的 10 段全链路

```bash
cd D:/project/MapDownloader
.venv/Scripts/python.exe scripts/hierarchical_map_e2e.py     # 10 段全 PASS，退出码 0
```

| # | 段 | 验什么 |
| --- | --- | --- |
| ① | Region hierarchy | 国 ⊃ 省 ⊃ 市 严格嵌套且**非矩形**（轴对齐矩形会让"相交"退化成"在外包框里"） |
| ② | Zoom strategy | 四级推荐区间 == `DEFAULT_ZOOM_POLICIES`；自定义等级永不警告 |
| ③ | Polygon estimate | 候选 > 选中；边界瓦片按**相交**判而不是按中心点 |
| ④ | Mission region | 500m / 5km / 20km → 走廊面积与选中瓦片**严格单调** |
| ⑤ | Risk analysis | 全国 Z5~10 = 正常；全国 Z8~18 = 危险；小任务区高 Zoom **不**因 Zoom 高而危险 |
| ⑥ | Download plan | 顺序 全国 → 省 → 市 → 任务区；停用项不计入合计 |
| ⑦ | Scheduler | 严格串行（回调里断言"同时只有一项在跑"）；服务端收到的请求数 == 计划总量 |
| ⑧ | MBTiles | 四份产物落盘、张数与 Zoom 区间对得上 |
| ⑨ | Inspector | 四个包全 PASS，且**没有**"缺失 N 张"这种按矩形算出来的假结论 |
| ⑩ | Package | 行政区与任务区各打一个包，核对 `manifest.region` 的每一项 |

全程**离线**：`httpx.MockTransport` 供瓦片，不访问天地图、不需要 Token。
夹具是自造的合成几何（`tests/fixtures/regions/hierarchy/`），**不是真实国界**。
跑一遍约 22 秒；四段合计 2,647 张瓦片。

### 这一 Slice 抓出来并修掉的两个缺陷

**① 行政区包的 `region.level` 从来没写进包里。** Slice 22 只给**任务区**写等级，
理由是"行政区的等级能从 `region_id` 反查" —— 那句话只对 Windows 自己成立。
Android 没有区域目录：

```text
Windows:  region_id=hangzhou → 查区域目录 → CITY           ✅
Android:  region_id=hangzhou → 查不到     → UNKNOWN        ❌ 静默降级
```

后果**不报错**：包照常导入、照常显示，只是 `OfflineMapSelector` 的 §5-3
（等级越具体越优先）退化成"谁也不比谁优先"。修法是让产品把等级写进产物
（`metadata_rows()` / `manifest_region()` / `map_download_service` 三处同源），
矩形写 `custom` 而不是留空 —— 留空在 Android 侧与"老包没有这条信息"是同一个值，
而"用户画的一块矩形"与"不知道"不是一回事。

**② 目录里的多边形区域无法续传、也无法被 Inspector 认出来。**
`region_from_task` 只认内置预设（`china`）与任务区；从**区域目录**里选的行
（`hangzhou` 之类）走到续传时反查不到几何，于是报"在本版本找不到对应的行政区边界，
无法安全续传"。修法是给 `app.geo.region` 加一个**目录解析器**注册点，
由 `app.regions` 在界面入口处装上 —— 这样 `app.geo` 依然是纯计算层，
不需要反过来 import 业务层（那会立刻造出一个循环）。

两个都是**单测全绿也抓不到**的类型：一个跨端、一个只在"目录里的区域 + 续传"这条
组合路径上。这也是第 ⑩ 段必须打**真实包**、而不是断言一个字典的原因。

### 验收

```bash
# 全量单测（Windows）
cd D:/project/MapDownloader && .venv/Scripts/python.exe -m pytest -q          # 全量（数字以实际输出为准）

# 主链路（离线）
.venv/Scripts/python.exe scripts/e2e_smoke_test.py                            # 6 段

# 分级全链路（离线）
.venv/Scripts/python.exe scripts/hierarchical_map_e2e.py                      # 10 段

# 历史 Slice 回归（真窗口 / 真按钮）
for s in verify_slice7_ui verify_slice8_inspector verify_slice9_package \
         verify_slice10_android verify_slice16_polygon verify_slice17_zoom \
         verify_slice18_regions verify_slice19_region_ui verify_slice20_plan \
         verify_slice21_plan_execution verify_slice22_mission_region \
         verify_slice23_risk verify_resume; do
  .venv/Scripts/python.exe scripts/$s.py --no-shots
done

# Android
cd D:/project/osmdroid && ./gradlew :OpenStreetMapViewer:testDebugUnitTest :OpenStreetMapViewer:assembleDebug
```

文档：新增 [`docs/hierarchical_offline_map.md`](docs/hierarchical_offline_map.md)
（体系总览）、`docs/e2e_acceptance_checklist.md` 增加 D 段（D21–D25 分级验收）、
`docs/troubleshooting.md` 增加 4 条分级相关排查。

---

## Slice 23：下载风险分析

到 Slice 22 为止，这个工具已经能规划出"全国 Z8~18"这种规模的活儿了，而拦住它的是
一个 **1 万张** 的阈值 —— 几十万张和上亿张在它眼里是同一句话。这一版把"这一单大不大"
做成**一层结论**，而不是一个确认框：

```text
下载区域：示例北省（省级 / Z9~18）        [加入下载计划] [查看下载计划] [任务区域…]
                                        ↓ 点「计算」
┌ 风险提示 ──────────────────────────────────────────────────────────┐
│ 风险：较大（LARGE） ▲                                              │
│ 预计瓦片：100,000                                                  │
│ 预计容量：2.86 GB                                                  │
│ 实际 Zoom：Z9~18　　推荐 Zoom：Z9~13                                │
│ 高于推荐 Zoom 的瓦片：98.4%                                         │
│                          [查看详情] [采用推荐 Zoom]                 │
└────────────────────────────────────────────────────────────────────┘
        ↓ 点「开始下载」
┌ 任务规模确认 ──────────────────────────────────────────────────────┐
│ 风险：较大（LARGE） ▲  预计瓦片：100,000  预计容量：2.86 GB          │
│ 实际 Zoom：Z9~18　　推荐 Zoom：Z9~13                                │
│ 原因：… 建议：…                                                    │
│ 确认开始下载吗？（不会修改任何配置，仅作提醒）  [确认] [取消]        │
└────────────────────────────────────────────────────────────────────┘
```

四档阈值（张数与容量**两个维度取更高**）：

| 等级 | 瓦片数 | 容量 | 拦法 |
| --- | --- | --- | --- |
| 正常 NORMAL | < 100 K | < 2 GiB | 直接开始 |
| 较大 LARGE | 100 K ~ 1 M | 2 ~ 20 GiB | 确认一次（问号） |
| 很大 VERY_LARGE | 1 M ~ 10 M | 20 ~ 100 GiB | 警告后确认 |
| 危险 DANGEROUS | ≥ 10 M | ≥ 100 GiB | **手输确认词** |

最要紧的一条：**界面里没有任何阈值数字**。分级、要不要确认、要不要强确认，一律问
`app/planning/risk_policy.py` 的 `RiskPolicy`（全项目唯一一份）。判据一旦抄进界面，
"调阈值"就变成了"改好几个文件"，而漏掉的那个必然是用户真正会撞上的那一条。

其余几条关键取舍：

- **分析器绝不重新枚举瓦片。** `DownloadRiskAnalyzer` 只读估算对象已经算好的三个字段
  （`selected_tiles` / `estimated_size_bytes` / `per_zoom`，鸭子类型），
  既不用也不 `import` 枚举器与 `RegionTileEstimator`（有 AST 用例守着）。
  于是"风险分析的复杂度与区域多大无关"是**结构上**成立的 —— 点一下「计算」不会
  因为风险分析而变慢，也不会多跑一遍几千万瓦片。
- **只用「区域内」张数，不用外包矩形候选数。** 候选数在渤海湾、广州湾这类形状上能
  虚高一个数量级，拿它当风险依据就成了"你其实下不了这么多，但我按这么多警告你"。
- **等级只参与策略判断，不直接决定风险。** 任务区 Z15~18 的 Zoom 数字比全国 Z5~10
  高得多，但它只覆盖一条走廊 —— 判定一律看实际张数与容量。
- **两道闸门不叠加。** 新的风险闸门更严（10 万张起），所以两个都命中的任务必然走
  风险那一条；旧的 1 万张阈值**只在等级为「正常」时才走**，一个 20 万张的任务不会
  被连问两次。
- **「采用推荐 Zoom」= 切到推荐模式，不是硬写两个数。** 推荐区间会因为"换了一块地"
  而变，模式跟着走才一直对得上；而且**只有用户点了才会改**，分析 / 重绘 / 启动保护
  都不会碰用户的输入（改完立刻重算 —— 旧数字对应的已经不是当前 Zoom 了）。
- **危险档的强确认是手输确认词。** LARGE 那档已经把"闭眼点右边那个"教给用户了，
  危险档再用同款按钮等于没拦。确认词（`DANGEROUS_CONFIRM_WORD`）放在配置里，
  界面、测试与文档读的是同一个字面量；大小写敏感，且"敲对之前主按钮是灰的"。
- **计划级四选一，危险档不叠第二个框。** 「继续 / 采用推荐 Zoom / 停用危险项 /
  返回修改」一次问清：选"停用危险项"之后本来就没有危险档要确认了，分成两问只会让
  用户连答两次。默认焦点在「返回修改」上 —— 误按回车不该把一份危险计划跑起来。
- **计划表格的「风险」列追加在最后**，且代码里改用 `_COLUMN_STATUS` / `_COLUMN_RISK`
  两个索引常量指代列位（不再用 `len(_COLUMNS) - 1`）—— 否则执行符号会被写进风险列，
  而 Slice 21 的执行用例当场就抓到了这个。

### 这一 Slice 抓出来的缺陷

1. **`len(_COLUMNS) - 1` 是"最后一列"的假名字。** 加了风险列之后，状态列的
   `✓ 已完成` 被写进了风险列，而状态列退回"可参与下载"。是 Slice 21 的执行用例
   抓到的（它断言那一格以 `✓` 开头）—— 这正是"每个 Slice 收尾都要真跑前几个
   Slice 的验收脚本"的价值。
2. **计划级四个去处装不进 `QMessageBox`。** 危险档要在同一屏里塞一个"敲对才解锁
   主按钮"的输入框，`QMessageBox` 只能加一个复选框。改用自建
   `PlanRiskConfirmDialog`，退出码自己定义（`QMessageBox.StandardButton` 那一套
   在这里会把"返回修改"（`Rejected == 0`）读成别的东西）。
3. **计划执行线程拿到的可能是 `None`。** 界面已关、线程未起跑时
   `execution.status` 会抛 `AttributeError`；`PlanDownloadWorker._main` 补了
   一句 `if execution is None: return`。
4. **用例没显式给 `output_dir`，计划就落到用户的默认输出目录里。** 本 Slice 的
   计划页夹具第一版漏了这一项，于是"点开始计划"真的把假产物写进了
   `~/MapDownloader/output/offline_plan/`；那个 12 字节的残留文件随后让
   `PlanValidator` 的两条用例失败（"小计划没有任何提醒"——
   目标文件已存在本来是条**提醒**，不是错误）。夹具一律显式给输出目录，
   并且收尾时验一遍：**整套用例跑完之后 `~/MapDownloader/output` 必须是空的**。
   这类"产品是对的、环境被自己弄脏了"的失败最难查 —— 断言报的是"多了一条提醒"，
   指向的却是几百行以外的另一份夹具。

### 验收

```bash
# 策略表 + 分析器 + 单任务 UI + 计划级 UI
.venv/Scripts/python.exe -m pytest tests/test_risk_policy.py \
    tests/test_download_risk_analyzer.py tests/test_plan_risk_analysis.py \
    tests/test_risk_protection_ui.py tests/test_plan_risk_ui.py -q

# 实机验收：真窗口 / 真按钮 / 真确认框 / 截图落 docs/
.venv/Scripts/python.exe scripts/verify_slice23_risk.py
```

截图落在 `docs/images/slice23_risk_area_normal.png`（面板风险区："正常"档，按钮置灰）、
`docs/images/slice23_risk_area_large.png`（城市 Z12~17：较大 ▲ + 超出推荐占比 74.9%）、
`docs/images/slice23_risk_details.png`（逐 Zoom 明细四列 + 合计行）、
`docs/images/slice23_zoom_applied.png`（点「采用推荐 Zoom」后换模式并重算）、
`docs/images/slice23_confirm_very_large.png`（很大档：警告图标）、
`docs/images/slice23_dangerous_task.png`（危险档：手输确认词）、
`docs/images/slice23_plan_risk_column.png`（计划页风险列 / 合计行带总风险，状态列没被顶掉）、
`docs/images/slice23_plan_choice.png`（计划级四选一，默认「返回修改」）、
`docs/images/slice23_plan_dangerous.png`（计划级危险档：同一个窗口多出确认词输入框）。

细节见 [docs/download_risk_analysis.md](docs/download_risk_analysis.md)。

## Slice 22：任务区域（航线走廊的高精度离线图）

高精度离线图的代价是平方级增长：Z15 到 Z18 每升一级瓦片数翻四倍，于是
"给长三角下 Z15~18"意味着三千万张瓦片、上百 GB —— 绝大多数作业场景装不下也下不完。
而巡检 / 测绘 / 应急巡查真正需要的，往往只是**一条航线两侧的带状区域**。

这一版把"一条航线两侧多少米"变成一块可下载的地：

```text
下载区域：…        [加入下载计划] [查看下载计划] [任务区域…]  ← 本 Slice 新增的入口
                                                      ↓
┌ 任务区域 ─────────────────────────────────────────────────────────┐
│ 任务区名称  [西湖巡检航线            ]                             │
│ 航线文件    [D:/routes/west-lake.geojson        ] [浏览…]          │
│ Buffer 半径 [ 5.00 km]   ← 界面按 km，落盘按**米**（5000）          │
│ 已有任务区  [mission-001 西湖巡检航线（3 个航点 / 走廊 5.00 km）▾]  │
│                              [生成任务区]                          │
├ Zoom ─────────────────────────────────────────────────────────────┤
│ ● 使用推荐 Zoom   ○ 自定义 Zoom                                    │
│ Min [15] Max [18]      ← 推荐模式下**只读**，数字来自 Zoom 策略表    │
│ 推荐 Zoom：Z15~18                                                  │
├ 下一步 ───────────────────────────────────────────────────────────┤
│ [计算] [加入下载计划]                                              │
├ 任务区预估 ───────────────────────────────────────────────────────┤
│ 候选瓦片（外包矩形）：1,204                                        │
│ 区域内瓦片（多边形）：742                                          │
│ 过滤掉：462（38.37%）                                              │
│ 预计容量：21.74 MB                                                 │
└───────────────────────────────────────────────────────────────────┘
```

链路（**复用**既有下载系统，不是第二套）：

```text
航点（WGS84）
  │  route_loader：只认 GeoJSON LineString / Feature / FeatureCollection
  ▼
LineString
  │  ★ region_builder：以航线中心为原点的本地投影（AEQD，单位 = 米）
  ▼
米制平面上的线 —— buffer(5000) —— 这才是地面上 5 公里
  │  投回 WGS84
  ▼
Mission 多边形（RegionType.POLYGON / RegionLevel.MISSION）
  │  三个文件落盘：data/mission_regions/<id>/{route,region}.geojson + metadata.json
  ▼
RegionTileEstimator（逐张判相交 → 候选 / 走廊内 / 被过滤）
  │  推荐 Zoom Z15~18 来自 ZoomStrategyService
  ▼
DownloadRegion（region_type=polygon，与行政区**完全同构**）
  │
  ▼
DownloadPlan → PlanScheduler → DownloadManager    Slice 20 / 21 一行未改
   排在市级之后（全国 → 省 → 市 → 任务区 → 自定义）
```

最要紧的一条：**绝不在 WGS84 的"度"上 buffer**。"0.05 度"在广州是 5.5 km、
在漠河是 2.5 km —— 一个随纬度变化的缓冲半径不是半径，是一个 bug，而且它不会抛异常，
几何照样生成、瓦片照样算出来，只是"我设的 5 公里"在北边变成了 2.5 公里。
用例守的判据是**量出来的地面宽度**：同一个 5000 米半径在北纬 20° 与 60° 缓冲出来的
走廊地面宽度必须一样。

其余几条关键取舍：

- **任务区是"多边形区域的又一个来源"**：`DownloadRegion` 与行政区完全同构，
  于是 Slice 16 的估算 / 下载 / 续传 / Inspector / 打包一行都不用改。
  几何从"随程序分发的资源"变成"用户数据"这一处差异，靠 `app.geo.region` 里一张
  **动态解析注册表**补上（注册回调，不是 import —— 否则会成环）。
- **几何只算一次**：创建时缓冲一次并落盘，之后的估算 / 下载 / 续传 / Inspector
  全读同一份 `region.geojson`。续传**绝不重算**：重算会得到一个"几乎一样"的几何，
  差几行几列不会让任何断言失败，但会让 Inspector 报出一堆假缺失。
- **改航线或半径 = 新建一个任务区**：`geometryVersion` 会进任务记录与 MBTiles
  metadata，用来回答"这批瓦片是按哪一版几何裁的"；`load()` 还会把元数据记的范围
  与几何实际范围对一遍，对不上就报错。
- **Inspector 找得到几何就算，找不到就认错**：本机没有这个任务区时覆盖度标
  **UNKNOWN** 并说明原因，**绝不退回外包矩形** —— 按矩形算一个完整的走廊包会得出
  "缺了几十张"的结论，用户会照着这个数字去重下。
- **被引用时不许删**：任务 / 计划 / 执行记录里还有这个 id 就拒绝，并且弹的是
  **说明**而不是确认框（不骗用户点一次无意义的「是」）。
- **任务区层里没有 Zoom 数字**：推荐区间一律来自策略表，层级字面量收在
  `MISSION_REGION_LEVEL` 一处（两条都有 AST 静态守卫）。
- **不做地图编辑器**：航线在别的软件里画好导出 GeoJSON，这里只负责按米缓冲、
  算清代价、进计划。KML / GPX 明确拒绝并说明该转成什么。

### 验收

```bash
# 任务区全部用例（航线导入 / 米制 Buffer / 持久化 / 计划接线 / 窗口按钮）
.venv/Scripts/python.exe -m pytest tests/test_mission_route_loader.py \
    tests/test_mission_region_builder.py tests/test_mission_region_store.py \
    tests/test_mission_plan_integration.py tests/test_mission_region_dialog.py -q

# 实机验收：真窗口 / 真按钮 / 真包 / 截图落 docs/（93 项检查）
.venv/Scripts/python.exe scripts/verify_slice22_mission_region.py
```

细节见 [docs/mission_region.md](docs/mission_region.md)。

## Slice 21：计划批量调度

Slice 20 把"一次只规划一块地"升级成了**一份计划装多块地**，但那份计划**不会自己跑**——
点「生成任务」只造出一堆停在「已创建」的任务记录，下载要自己去「下载任务」列表里逐条发起。
这一版在计划页上加了一排执行按钮，把"一个接一个跑完整份计划"补上。

```text
[开始计划] [暂停] [继续] [取消执行] [重试当前] [跳过并继续]        [关闭]
──────────────────────────────────────────────────────────────────────
状态：执行中   当前：示例北省 Z9~13   任务：2 / 3
当前任务：63%   总体：57%（32,640 / 57,302 张）   速度：5.2 MB/s
```

### 严格串行，每一项都走 `resume_task`

调度器按计划里的层级顺序（全国 → 省 → 市）**串行**跑：任何时刻只有一个文件在写、只有一个
进度在动。每一项都调用 `MapDownloadService.resume_task()`——它同时覆盖"第一次跑"和
"暂停后继续"两种情形，于是"暂停后继续必须复用 MBTiles Resume、不能从头下载"成了这条路
的自然结果，而不是一句要人遵守的约定。单任务**内部**仍是并发下载（Slice 3 那条路一行未改）。

### 三份数据，执行态单独落盘

"跑到哪一步了"和"用户想下哪几块地"是两回事，所以新落地一份 `data/plan_executions.json`，
与 `plans.json` / `tasks.json` 各管各的。执行记录里只有两个**引用**（`plan_item_id` /
`task_id`），区域名、Zoom、张数全部现查——复制进来就多一个会过期的真相。

### 两个"状态"不能混

`PlanItemStatus`（Slice 20，可用性）和 `PlanItemRunStatus`（本 Slice，执行态）取值集合完全
不重叠却同名同层，import 错了不会报错只会静默走错——因此执行态**刻意换名**加 `Run`。整份
计划另有独立的 `PlanExecutionStatus`（含 `PARTIAL_FAILED` 这种计划独有概念），不复用任务状态。

### 暂停 = 安全停下 + 改写状态；恢复绝不自动联网

"暂停"实现成"请求引擎 `cancel()` 安全停下 → 等返回 → 把任务从磁盘上的 `CANCELLED` 改回
`PAUSED`"，于是暂停变成一个**可跨进程**的状态。程序异常退出后，启动时发现还停在 `RUNNING`
的执行一律降级成 `PAUSED`，**绝不自动联网继续**——自动下载会消耗用户的配额，必须由人拍板。

### 失败即停，决定权交还用户

一项失败就停下、整份落到 `PARTIAL_FAILED`（**不是终态**）：用户在「重试当前」与「跳过并继续」
间选一个才能往前走。`unresolved_count = FAILED + SKIPPED + CANCELLED > 0` 即判"部分失败"，
"留下任何一块没下成就是部分失败"。

### 进度按张数聚合，不是按条目数

整体进度条走 `selected_tiles` 加权的纯函数 `build_progress()`：分母取计划启用项之和、分子
按实际处理张数累计并收口（防 >100%）。速度只取当前任务、任务切换即归零——串行下不存在
"整份速度"这种独立量，否则界面会挂着上一个任务的残留速度。

### 这一版写测试抓出来的缺陷

1. **Qt 跨线程信号"慢一拍"让验收脚本误判失败**：worker 在线程里发 `status_changed` /
   `item_changed`，主线程槽（`refresh` → 重画进度条 / 状态行 / 表格符号 / 按钮可用性）会
   比 `execution().status` 落终态**晚几帧**。验收脚本若在那一瞬读控件值，会看到
   "进度条还是 3%、状态行还写执行中、表格符号还是 ▶"。修法：断言前先 `_wait_until`
   **控件真的重画到位**（进度条=100、状态行含"已完成"、表格全 ✓、按钮可用状态正确），
   而不是只等模型状态变。这条教训适用于所有"真窗口 + 真按钮"验收。
2. **跨场景复用服务集串味**：验收脚本里一个失败/跳过的场景若与另一子场景共用同一套
   `make_services()`，会因为 `plan.id` 相同而复用同一份执行记录，导致 `FakeRunner` 根本
   没被调用。修法：每个子场景建**独立**的服务集（`work_dir/"c1"` / `work_dir/"c2"`）。
3. **延迟的 `closeEvent` 拆掉活着的 worker**：上一个场景没完全收尾的 `closeEvent` 可能
   迟到进下一个场景，对仍在跑的 worker 调 `request_close()` → 循环因 `_closed` 断裂 →
   后续点击走到 `worker is None → start_plan()`（重跑整份计划）而不是 `retry_current()`。
   修法：场景切换前显式等 `is_running_plan() is False` 且 worker 收尾，再断言。

### 验收

```bash
# 调度层全部用例（模型 / 存储 / 调度器 / worker / 计划页执行 / 主窗口接线）
.venv/Scripts/python.exe -m pytest tests/test_plan_execution.py \
    tests/test_plan_execution_store.py tests/test_plan_scheduler.py \
    tests/test_plan_worker.py tests/test_download_plan_execution_ui.py -q

# 实机验收：真窗口 / 真按钮 / 截图落 docs/（五态各一张）
.venv/Scripts/python.exe scripts/verify_slice21_plan_execution.py
```

细节见 [docs/plan_scheduler.md](docs/plan_scheduler.md)。

## Slice 20：多级下载计划

Slice 19 之后，"选哪块地"已经很顺了，但**一次还是一次**：下完全国再选省、下完省
再选市，而且全程不知道总共要下多少张、要占多大。这一版在"选区域"和"开始下载"
之间加了一层**计划**：

```text
下载区域：[示例国 ▾] [示例北省 ▾] [示例河市 ▾]   ○ 推荐  ○ 自定义
                                    ↕
    [加入下载计划]  [查看下载计划]      ← 本 Slice 新增的两个入口
    下载计划：3 个区域 / 57,302 张 / 1.63 GB

        打开计划页 ↓

┌ 离线计划   输出目录：D:/out/离线计划 ────────────────────────────────────┐
│ 已形成三级离线地图覆盖：全国级 → 省级 → 市级                             │
│ ☑ 示例国    全国级  Z5~10     268 张    7.85 MB   可参与下载            │
│ ☑ 示例北省  省级    Z9~13   3,449 张  100.98 MB   可参与下载            │
│ ☑ 示例河市  市级    Z12~15 53,585 张    1.53 GB   可参与下载            │
│ 合计：3 个区域 / 57,302 张 / 1.63 GB                                    │
│ 提醒：计划规模偏大：57,302 张瓦片（超过 50,000 张）…                      │
│ [重新估算] [移除] [清空] [生成任务]                        [关闭]       │
└──────────────────────────────────────────────────────────────────────────┘
```

点「生成任务」→ 每个启用且可用的条目生成一条**现有形状**的下载任务记录，停在
**「已创建」**。**它不会开始下载**：下载仍由「下载任务」列表里逐条发起。

### 计划里只放"哪块地 + 哪段 Zoom"

`data/plans.json` 的一个条目只有七个字段：

```json
{ "id": "…", "regionId": "zhejiang", "regionName": "浙江省",
  "regionLevel": "province", "minZoom": 9, "maxZoom": 13, "enabled": true }
```

- **`region_id` 是唯一的权威引用**：名字会随数据更新变，几何更是几十 MB 起步；
- **不放瓦片列表**：市级 Z16 是几万到几百万行，而**算一次只要几十毫秒**；
- **不放结论**：状态与张数打开时重算 —— 磁盘上那句"上次算了 53,585 张"在边界
  数据更新之后就是错的，而显示一个错的数字比显示"尚未估算"更糟。

因此 `plans.json` 很小、打开很快，而且**永远是当前事实**。

### 一个坏条目不该拖垮整份计划

区域被删了、边界文件没了、`plans.json` 被手工改坏、甚至整个文件损坏：

| 情况 | 行为 |
| --- | --- |
| 计划文件不存在 | 新建一份空计划，**正常启动**（旧版本升级上来就是这个状态） |
| 计划文件损坏 | 隔离成 `plans.json.corrupt`，同样落到空计划，日志里说一声 |
| 某个区域不在目录里 | **只把这一条**标成不可用（页面里那一行写着原因），整份计划照常打开 |
| 边界文件缺失 | 同上；用户在页面里点「移除」就能清掉它 |
| 某条算不出瓦片 | 同上；「重新估算」是它恢复的唯一路径 |

反过来（让整份计划打不开）的后果是：用户连"把坏的那条删掉"都做不了。

### 估算在后台线程里跑，而且能取消

一个省 Z13、一个市 Z16 就是几十万张瓦片，多块地加起来的遍历时间是秒到分钟级。
主线程跑它 = 窗口"无响应"。所以估算走 `PlanEstimateWorker`（`QThread`），每算完
一项发一次进度（`正在估算 2/3：示例北省`），进度条走到 100%。

取消的检查点在**条目之间**，标志是一个 `threading.Event`（跨线程安全，立刻生效，
不必等信号槽往返）—— 用户点「取消估算」时看到的是立刻停下，而不是"点了没反应"。
取消后**计划配置一个字都不动**（Spec §21），已算完的条目保留结果，不产生任何
半成品。

### 多边形按"选中"张数汇总

`selected_tiles` 是裁剪之后的张数（Slice 16 的几何判定），页面里「预计瓦片」显示
的就是它。用外包矩形的候选数当总量，在渤海湾、广州湾这类形状上能差出一个数量级,
而用户会以为"程序漏下了"。

这条要求有个测试上的坑：合成夹具里的多边形全是**轴对齐矩形**，于是
`selected == candidate`，一个写错的实现能在那些数据上全绿。因此 conftest 里专门
加了一块**三角形**区域（外包 1°×1°，实际只有一半），把两个数明显拉开。

### 文件名与目录只有一处实现

```text
<输出目录>/<计划名>/<region_id>_z<下界>_<上界>.mbtiles
        D:/out/计划A/demoland_z4_10.mbtiles
```

`planned_output_paths()` 是唯一的实现：界面显示"会写到哪"、校验器查"是不是已经有
同名文件了"、生成任务时真的按它落盘 —— 三处共用同一个函数。各写一遍的后果是
用户在界面上看到 `a_z4_10.mbtiles`、实际写进了 `a_z4_10-2.mbtiles`。

目录名与文件名都先过 `sanitize_map_name()`：Windows 非法字符换成 `_`、保留名
（`CON` / `LPT1`）加后缀、空名字落到 `offline_plan`。**只算路径、不建目录** ——
本 Slice 只创建任务配置，目录留给下载阶段去建。

### 顺序即层级

生成出来的任务顺序固定是 **全国 → 省 → 市 → 任务区**，同级内保持你加入的顺序。
这不是为了好看：它是给下一个 Slice（批量调度）准备的 —— 全国概览先下完，用户就
已经能看到一张能用的图，而不必等最深的那块地。

### 这一 Slice 明确**不做**的事

- **没有「全部开始下载」**：计划页只生成任务记录。批量调度、失败重试、串行推进
  是 Slice 21。
- **没有 Mission 航线入口**：`MISSION` 等级在模型与策略表里预留，界面上还没有。
- **没有完整风险分析**：只有三条提醒（规模偏大 / 超出推荐 Zoom / 目标文件已存在）。
- **没动下载器**：`MapDownloadService` / `DownloadWorker` / MBTiles 格式一行未改，
  单区域下载那条路完全不变。

### 这一 Slice 写测试抓出来的缺陷

1. **计划页状态列慢一拍**：`refresh()` 里我先画表格、后调校验器，而校验器**带副
   作用**（会把"区域被删 / 边界缺失"的条目标成不可用）—— 于是那一格永远显示上一
   轮的状态，页面上出现自相矛盾的两行：状态列写"尚未估算"，下面却列着"缺边界文件"。
   修法：先校验、后画表。
2. **主窗口接线把两个属性当成了方法**：`panel.zoom_mode()` / `panel.zoom_spins()`
   —— 它们是 `@property`。这个缺陷的表现是**点了「加入下载计划」毫无反应**，
   数据层用例（哪怕几百个）永远看不见，因为异常在 Qt 槽里被静默吞掉了，
   只在 stderr 留下一行 traceback。抓到它的正是那条"点一下并断言结果"的用例。
3. **一条测试差点挂死整个套件**：写"超出推荐 Zoom"的用例时顺手拿了 4°×4° 的
   国家级区域配 Z4~18 —— 那是上千万张瓦片，估算要跑几分钟，表现为"pytest 疑似
   挂死"。改用 2°×2° 的省级区域（Z9~15），代价降了三个数量级。
   教训（Slice 18 就记过一次）：**验收脚本"真的点一下"的代价要按最坏输入估**。

### 验收

```bash
# 计划层全部用例（模型 / 构建 / 估算 / 校验 / 存储 / 任务生成 / 计划页 / 主窗口接线）
.venv/Scripts/python.exe -m pytest tests/test_download_plan.py tests/test_plan_builder.py \
    tests/test_plan_estimator.py tests/test_plan_validator.py tests/test_plan_service.py \
    tests/test_download_plan_dialog.py tests/test_main_window_download_plan.py -q

# 实机验收：真窗口 / 真按钮 / 截图落 docs/
.venv/Scripts/python.exe scripts/verify_slice20_plan.py
```

细节见 [docs/download_plan.md](docs/download_plan.md)。

## Slice 19：行政区域级联选择 + 推荐 Zoom

> 历史章节。这里写的是 Slice 19 当时的形状（三级）；Slice 27 已扩成
> **国 → 省 → 市 → 县区** 四级，当前状态见上面的「当前进度」。

Slice 18 把"一个中国预设"升级成了**层级目录**，但界面上还是**一条下拉列全部行政区**
—— 国家和省、市混在一起，用户看不出哪条挂在谁下面。这一版把它换成
**国家 → 省 → 市三级级联**，并让 Zoom 由区域等级决定：

```text
国家： [演示国 ▾]        ← 只列国家级
省份： [北省 ▾]          ← 跟着国家重填，首项是「全部（不限定省份）」
城市： [河城 ▾]          ← 跟着省份重填，首项是「全部（不限定城市）」
下载级别： 城市级 · 演示国 / 北省 / 河城
下载策略： ◉ 推荐 Zoom   ○ 自定义 Zoom
Zoom 建议： 推荐 Zoom: Z12~15    当前 Zoom: Z12~15
Min Zoom: 12（只读）    Max Zoom: 16（只读）
推荐说明： 城市级地图适合城市范围任务规划。
```

### 三段里只有一段是"生效"的

`selected_region_id = city or province or country`。于是"只选国家"与"选到市"走的是
**同一条**下游链路（`RegionService.get_region()` → 加载边界 → `DownloadRegion` →
候选瓦片估算）。界面**从不**自己拼一个 `DownloadRegion` —— 那样就会出现两处
"边界文件在哪、缺文件怎么办"的实现。

「全部」这一项的 `itemData` 是 `None` 而不是某个哨兵 id：`-1` 与任何合法下标都不相等，
这种判断到处都会漏（这个坑 Android 侧的 Spinner 已经踩过一次）。

### 推荐 Zoom 与自定义 Zoom

| | 推荐 Zoom（默认） | 自定义 Zoom |
| --- | --- | --- |
| Min / Max | 按等级从策略表取（Z5~10 / Z9~13 / Z12~15） | 用户说了算 |
| 可编辑 | ❌ 只读 | ✅ |
| 超范围 | 不可能超 | 给一句警告 |

推荐模式下把 Min/Max 设成**只读**，是为了堵住一类看不出来的错误：界面显示 `Z12~15`、
实际下载用的却是用户上次手打的 `8~17`。**自定义模式下超范围只警告、不改值**——
与 Slice 17 定下的"服务层没有 `clamp()`"是同一条规矩。

模式切换**不改值**：`推荐（Z12~15）→ 点自定义` 值仍是 12~16 只是可编辑了；
`自定义 8~17 → 点推荐` 值变成 12~16 并重新只读。控制器刻意**不缓存**用户的值——
`effective_range(min_zoom, max_zoom)` 把控件里的值当参数收进来按模式选一个返回，
少一个字段就少一处不同步。

### `RegionSelectionController` 是 Qt-free 的

`app/ui/region_selection.py` 不 import PySide6：它只持有三个 id 与一个模式，把目录查询
与策略查询转给 `RegionService` / `ZoomStrategyService`。于是级联、等级推导、模式切换、
异常分支这些容易写错的逻辑能在毫秒级的纯对象用例里断言，只有"控件行为"（只读、
可见性、信号有没有接）才落到 widget 用例里。

### 关窗重开还在那儿

| 键 | 值 |
| --- | --- |
| `download/region_type` | `rectangle` / `polygon` |
| `download/country_id` / `province_id` / `city_id` / `county_id` | 四级 id **分别**存 |
| `download/zoom_mode` | `recommended` / `custom` |
| `download/custom_min_zoom` / `custom_max_zoom` | 用户自己定的区间 |

四级分开存：界面是按四级恢复的，只存末端 id 就得反推"它的父亲是谁"——那是目录的职责。
`county_id` 是 Slice 28 补的：漏了它，选了县关窗再开，范围会**静默**放大成整个市（或省）。
Slice ≤18 的老键 `download/min_zoom` / `download/max_zoom` **只读不写**（升级后上次调好
的区间不会变成"从没设过"），`clear()` 会把老键一起删。偏好里存着不存在的 id 时**逐级
回退**：市 → 省 → 国，一个都不认识才落回自定义矩形。

恢复的**顺序**也有讲究：区域必须排在 Zoom 之前。恢复一个行政区域会把模式带到「推荐」，
而推荐模式会立刻把建议值填进 Min/Max；顺序反了的话用户上次设的 `8~17` 会被顶掉。

### 结果区多了两样

行政区域算完后结果区第一段是 `区域 / 级别 / 推荐 Zoom / 候选 / 区域内 / 过滤 / 比例`；
`Zoom 明细` 每行末尾多了「预计容量」（用生产的 `estimate_size_bytes()` + `format_size()`，
不在这里乘一个自己的常数）：

```text
Zoom 明细（候选 / 选中 / 过滤 / 预计容量）：
  z12  169 / 169 / 0  4.95 MB
  z13  600 / 600 / 0  17.58 MB
```

### 与矩形模式的兼容

矩形模式下这 14 个新增控件（三段下拉 / 级别 / 策略 / 推荐说明）**整块收起**，
`QGridLayout` 在整行都隐藏时会把行高压到 0，界面与 Slice ≤18 逐像素一致；
经纬度框照旧可编辑、Zoom 也不受策略影响（`zoom_mode` 只决定"下次切到行政区域时
用哪种策略"和"关窗存哪一项"）。

### 这一 Slice 写测试抓出来的三个缺陷

1. `UiPreferences.clear()` 只删了**新键**，没删 `download/min_zoom` / `max_zoom`
   —— 而 `zoom_range` 在新键缺失时会回退读老键。于是 `clear()` 之后"从没设过"
   这个事实被老配置悄悄推翻了（`assert preferences.zoom_range is None` 拿到 `(11, 13)`）。
2. `select_region()` 切换时没有先清掉 `_country_id/_province_id/_city_id`，于是
   "市 → 只选省"会留下上一次的市，等级算出来是 `CITY` 而界面上显示的是省。
3. **（实机验收脚本抓的）**面板一度把几何的**原始外包矩形**（9 位小数）交给下载器，
   而同一份几何写进 MBTiles metadata 时只留 6 位小数。夹具的边界恰好瓦片对齐时，
   Inspector 从 metadata 反推的候选范围**多出一圈**（多出来的那列 / 那行只是"碰到"
   了多边形边沿），于是**完整的包被判成"缺失 10 张"、打包器拒绝出包** ——
   一处纯粹由精度不一致造出来的假警报。改回传界面上那四个数（= 写进 metadata 的
   那一份），往返就自洽了，行为也与 Slice ≤18 一致。

详见 [docs/region_selection_ui.md](docs/region_selection_ui.md)。
实机验收：`python scripts/verify_slice19_region_ui.py`
（真 windows 平台插件、真窗口、真关窗再开新窗口、真点「计算」，90 项检查，
截图落 `docs/images/slice19_*.png`）。

## Slice 18：行政区域层级模型（中国 → 省 → 市）

Slice 16 给了"按行政区域裁剪"，Slice 17 给了"这一级该下到多少 Zoom"，
但两者都只覆盖到**全国**这一层：想下浙江省、想下杭州市，界面上没有东西可选。

这一版把"一个中国预设"升级成**可扩展的行政区域目录**。它本身不下载任何东西，
只提供一份统一的区域数据源，给 Slice 19（分层下载计划）和 Slice 20（区域管理界面）用。

```text
中国 (COUNTRY)                ← resources/regions/country/china.geojson
├── 浙江省 (PROVINCE)         ← resources/regions/province/zhejiang.geojson
│   ├── 杭州市 (CITY)         ← resources/regions/city/hangzhou.geojson
│   └── 宁波市 (CITY)
└── 北京市 (PROVINCE，直辖市)  ← 直辖市直接挂国家下，不凭空造一级
```

层级规则只有一份（在规划层）：`COUNTRY` 无父级、`PROVINCE` 挂 `COUNTRY`、
`CITY` 挂 `PROVINCE`；`MISSION` / `CUSTOM` 不属于行政层级，怎么挂都不检查。
坏挂载（国家级带父亲、`A→B→A` 的环）会被校验抓出来。

### 目录元数据进仓库，边界数据不进

```text
resources/regions/
├── index.json     ← 随仓库分发：有哪些区域、它们在哪个文件里
├── README.md
├── country/       ← 数据（*.geojson，被 .gitignore 忽略）
├── province/
└── city/
```

`index.json` 里**没有 Zoom 字段**，也不允许加：Zoom 建议由 `level` 在运行时查
Slice 17 那张策略表得到。存一份进目录就是第二个真相，改了策略还要回头改数据。
`resources/regions/**/*.geojson` 一个都不在仓库里 —— 与 Slice 16 同一条合规要求：
**法定地图内容必须使用带审图号的授权数据。** 这条有两个静态守卫盯着
（`scripts/verify_slice18_regions.py` 场景 A）。

### Lazy Load：建目录与查询都不碰边界文件

一个省几 MB 的 GeoJSON 是常态，放 34 个省 + 300 个市就是几百 MB。所以：
`RegionCatalog` 的构造与**全部查询**（`get` / `list_all` / `list_by_level` /
`list_children` / `get_parent` / `get_ancestors`）只读 `index.json`（几 KB），
只有真的要几何时才读盘。用例把 `load_geojson_document` 换成计数探针，
跑一遍所有查询后断言**计数为 0**。

### 两个 `RegionPreset` 是故意不合并的

| 类 | 是什么 | 有什么 |
| --- | --- | --- |
| `app.geo.region.RegionPreset` | Slice 16 的**扁平预设** | 只有文件（相对）名，没有层级 |
| `app.regions.RegionPreset` | 本 Slice 的**目录条目** | `parent_id` / `code` / 相对路径 |

合并会带来两个后果：`app.geo.region` 去 import `RegionCatalog` 就形成
`geo → regions → geo` 的环；而 `parent_id` 对"自定义矩形"这类根本不是目录条目的
预设毫无意义。所以只有**一条**转换方向 ——
`RegionLoader.to_geo_preset()`（目录 → 扁平），反向刻意不实现。

路径安全的实现也**只有一份**（`app/geo/region.py` 的三个函数），
`app.regions.models` 转出的是同一个函数对象（用例用 `is` 断言）。
理由很实际：`index.json` 是**可编辑的数据文件**，`"geojson": "../../../etc/passwd"`
或 `"C:/Windows/…"` 必须被拒，而且只能在一处被拒 —— 安全判断一旦有第二份实现，
错的通常是更宽松的那份。**拒绝发生在任何 IO 之前**（深度校验也不会去读越界文件，
这一条由"诱饵文件"用例守着）。

### 校验分两档

```bash
.venv/Scripts/python.exe -c "from app.regions import RegionService; print('\n'.join(RegionService().validate().as_lines()))"
```

| 档 | 做什么 | 能发现 |
| --- | --- | --- |
| 快速（默认） | 只 `stat` 文件 | 结构 / 层级 / 路径问题、文件在不在 |
| 深度（`deep=True`） | 真解析每份 GeoJSON | 上面全部 + 文件坏掉 / 声明的 `bounds` 与实际不符 |

**"缺边界文件"是 WARNING 不是 ERROR**：仓库本来就不带数据，"还没放文件"是正常
中间态。如果它算错误，`validate()` 在任何一台干净机器上都红，那这个命令就没人看了。

### 界面只改了一处

区域下拉框的**数据来源**从"内置常量"换成 `RegionService.list_presets()`，
条目类型没变，所以面板其余代码一行没动。于是"把资源目录指到别处"这件事对界面
同样成立。**没有**做层级下拉、省市选择器、区域管理页 —— 那些是 Slice 19 / 20。

> **Slice 19 更新**：层级下拉已经做了。那条"列全部行政区"的下拉被**级联下拉**
> 取代（Slice 19 是国/省/市三级，Slice 27 补了县区，现在四级），数据来源仍是
> 本 Slice 的 `RegionService`，等级也仍由目录条目给出。
> 见上面那一章与 [docs/region_selection_ui.md](docs/region_selection_ui.md)。

目录读不出来时（文件丢了 / JSON 语法错 / 版本号不认识）**不让界面崩**：
退回由内置扁平预设合成的目录，`catalog_error` 记下原因、日志留一行 ERROR。
此时"中国"照常可用，而层级查询返回空 —— 平铺预设没有父子关系，
凭空编一个出来才是更糟的谎。

详见 [docs/region_catalog.md](docs/region_catalog.md)。
实机验收：`python scripts/verify_slice18_regions.py`
（真 windows 平台插件、真窗口、三级目录、路径安全、截图落 `docs/images/slice18_regions_*.png`）。

## Slice 17：分级 Zoom 策略

Slice 16 解决了"下哪一块地"，这一版解决"下到**多少级**"。

在此之前，Zoom 只是两个输入框（默认 8~17），选中国还是选一个作业区用的是同一套数字。
但"全国 + Z18"和"作业区 + Z18"根本不是一件事：同一块 0.5° 见方的地，Z5~10 是
**17 张 / 510 KB**，Z12~18 是 **100,981 张 / 2.89 GB** —— 差三个数量级，而这还只是
一块巴掌大的地方。规则散在界面里，用户就只能靠猜。

策略表（唯一权威来源：`app/planning/zoom_policy.py`）：

| 等级 | 推荐 Zoom | 警告阈值 | 米/像素(@32°N) | 场景 |
| --- | --- | --- | --- | --- |
| 全国级 `COUNTRY` | **Z5 ~ Z10** | > Z10 | 4149 ~ 124 | 大范围概览 |
| 省级 `PROVINCE` | **Z9 ~ Z13** | > Z14 | 247 ~ 16 | 省域浏览、城市定位 |
| 城市级 `CITY` | **Z12 ~ Z15** | > Z16 | 32 ~ 4.1 | 城市范围任务规划 |
| 县级 `COUNTY` | **Z14 ~ Z17** | > Z18 | 8.1 ~ 1.0 | 区县级高精度 |
| 任务区 `MISSION` | **Z15 ~ Z18** | > Z18 | 4.1 ~ 0.5 | 高精度作业区（无人机主力） |
| 自定义 `CUSTOM` | 全支持范围 | 不警告 | 全范围 | 自定义矩形 / 老任务 |

> **Slice 29 按无人机图基底重新标定过这张表**（全国 Z4→Z5、城市 Z16→Z15、
> 县级 Z18→Z17）。判据从「行政区多大」换成「地面分辨率 + 数据源精度」，
> 详见 [docs/zoom_strategy.md](docs/zoom_strategy.md) 第 10 节。

三条设计要点：

- **只是推荐，绝不改写你的输入。** 选中国、设成 Z8~18，界面上会出现一句非阻塞的
  `警告：超出全国级推荐范围（当前 Z8~18），建议使用 Z5~10。…`，然后**照样按 8~18
  算、照样按 8~18 下**。偷偷替你改成推荐值的后果比多下 2.89 GB 更糟：你会以为程序
  漏下了 9 万张。（`ZoomStrategyService` 里干脆没有 `clamp()` / `enforce()` 这类方法。）
- **相邻层级刻意重叠**（全国 Z10 与省级 Z9 交叠）。真实边界不会落在瓦片级别上，
  重叠让"全国概览 + 几个重点省"能用同一套级别拼起来而不出现糊一层清一层。
- **等级不落盘。** `tasks.json` 与 manifest 里都没有 `region_level`，它由 `region_id`
  反查预设得到；老 Region / 老任务没有这个字段，一律读作 `CUSTOM`（不套用任何行政
  等级规则），行为与 Slice ≤16 逐位一致。

界面只做了最小接入：选中中国预设时显示「推荐 Zoom：Z5~10」，Zoom 一改就刷新，
自定义等级时整行收起。**界面里没有任何 Zoom 区间常量，也没有一句推荐文案的副本**
（三条静态守卫盯着这件事：`ZoomPolicy(` 只能在规划层构造、四个等级不许在别处写死、
推荐文案不许被抄走）。

容量预估走的是 Slice 16 那条**真实**链路（`RegionTileEstimator`，多边形逐张判相交），
策略层只负责把推荐区间递进去，不重新实现一遍多边形统计。

详见 [docs/zoom_strategy.md](docs/zoom_strategy.md)。实机验收：
`python scripts/verify_slice17_zoom.py`（51 项检查，截图落 `docs/images/slice17_zoom_*.png`）。

## Slice 16：GeoJSON 区域下载与中国行政区域预设

在此之前，"要下哪一块地"只有一种表达方式：**一个矩形**。而矩形与行政区从来不是
一回事 —— 北京主城的外包矩形会把固安、三河、涿州和出海口一起框进来，按矩形下出来的
"北京市"离线包，瓦片数比实际需要多四成，多出来的全是不该下的。多下不是"浪费一点
点"：它花天地图配额、占手机存储、拖长下载时间，而且**在界面上完全看不出来**。

这一版把"下哪一块地"统一成一个模型（`DownloadRegion`）：

| 名字 | 含义 | 谁用它 |
| --- | --- | --- |
| **候选瓦片**（candidate） | 外包矩形覆盖的瓦片数，**只是搜索面** | 只用来给用户做对照 |
| **区域内瓦片**（selected） | 候选里真正与区域几何相交的那些 | 进度分母 / 大任务提示 / 容量预估 / 任务记录 / 续传 / 地图包 |

矩形模式下两者恒等（**逐位不变**，Spec §7）；多边形模式下 selected 是 candidate 的
子集。**所有下游计算只认「区域内」**：拿候选数当分母，你会得到一个永远走不满的
进度条，以及一个虚高的容量预估。

### 判"相交"而不是判"中心点在不在里面"

用几何库预编译的 prepared geometry 做 `intersects`，**不是**"瓦片中心点是否落在
区域内"。按中心点判，边界上的瓦片会被整片丢掉 —— 地图边缘出现一圈锯齿状空洞。
按相交判，只要瓦片与区域有重叠就留下：**多留一张无害，少留一张是用户看不见的数据
缺失**，两者代价不对称。

### 中国国界数据由你自己提供（README 里说清这一条）

仓库**不内置、不生成、也不套用**任何中国国界数据：`resources/regions/country/china.geojson`
默认不存在（Slice 18 起边界文件按层级分目录摆放，路径写在 `resources/regions/index.json` 里）。
**中国国界属于法定地图内容，必须使用带审图号的合法授权 GeoJSON 数据。**
程序提供的是"规范路径 + 加载器 + 明确的缺失报错"：

```text
计算失败：行政区域不可用：行政区边界文件不可用：…\resources\regions\country\china.geojson。
中国国界属于法定地图内容，请使用带审图号的合法授权 GeoJSON 数据，
放到 …\resources\regions 下的 country/china.geojson（相对路径）后重试
```

**它不会退回按矩形下载。** 退回去的后果是静默下一大片境外瓦片，而界面上的数字
看起来一切正常。宁可停下来问，也不要悄悄多下。文件格式与授权要求见
[`resources/regions/README.md`](resources/regions/README.md)。

### 界面（Spec §15 / §17）

「区域类型」下拉两个选项：**自定义矩形** / **行政区域**。选行政区域后，行政区下拉
（当前只有「中国」）出现，四个经纬度框**自动填上外包矩形并变成只读** —— 范围由数据
文件决定，手改会让"界面显示的范围"与"实际裁剪依据"对不上，而用户看不出这一点。

点「计算」给出六个数字：候选 / 区域内 / 过滤 / 占比 / 预计请求量 / 预计容量，
外加逐级明细。**运行日志里是同一份文案**（`estimate.as_lines()` 一份产出两处用），
避免"界面写 3 行、日志写 4 行、数字还不一样"。

### 这一 Slice 抓出来的缺陷：旧结论会赖在界面上

本地实测抓到一条真缺陷，`pytest` 全绿也发现不了（没有用例会去切区域类型再看结果区）：

1. 先在「自定义矩形」下算一次 → 结果区写着 `预计瓦片：56`；
2. 切到「行政区域」，而 `china.geojson` 不在 → **结果区那句"预计瓦片 56"还在**，
   看起来就像是"行政区域也算出了 56 张"；
3. 再点「计算」会给出人话错误，但只要动一下输出目录（`output_config_changed`）
   或勾一下注记（`layer_plan_changed`），`_refresh_output_preview` 就会拿**缓存里的
   旧结论**把错误提示盖回去 —— 用户以为算成功了。

修复：区域变更时**作废预估缓存**（`DownloadPanel.region_changed` →
`MainWindow._on_region_changed`：清缓存、清结果区、状态栏退回占位、日志留一行）。
回归用例 3 条，见 `tests/test_ui_smoke.py` 末尾。

### 续传与兼容（Spec §13 / §19 / §21）

- `tasks.json` 只写 `region_type` / `region_id` / `boundary_version` 三个字段，
  **不写 GeoJSON 本身** —— 一份省级边界有几 MB，塞进任务记录会让每次进度落盘都
  变成一次大写入；恢复时按 `region_id` 回预设取路径即可；
- **多边形任务续传时，"已有瓦片"只数区域内的那些**：`_execute` 原本用
  `sum(counts_by_zoom)` 把区域外的也算了进去，会让"已有 70 张"这种数字出现，
  于是任务永远差着几十张。这同样是本次抓出来并修掉的缺陷；
- 边界版本对不上会给一条 WARNING：同一块地按 2026-03 与 2026-09 两版边界裁出来的
  瓦片集合可能不同，硬续会在区域边缘留下一圈"哪版都不算"的瓦片；
- 老任务记录（没有 `region_type`）读出来就是矩形，行为与 Slice ≤15 一致；
- 地图包 manifest 新增**可选** `region` 子对象（`{"type","id","boundaryVersion"}`），
  位置紧跟 `bounds`。**老文件打出来的包与以前逐字节一致** —— 这条有专用用例钉着。

### Inspector：多边形包的"期望值"按几何算（Spec §20）

按外包矩形算一个完整的中国包，会得出"缺了 35 张"的结论（期望 81 / 实际 46），
而那个包是**完整**的。所以：边界可用时按几何算期望值；边界不可用时把覆盖度标成
**未知（UNKNOWN）** 并说明原因，**绝不退回矩形口径**。误报比"不知道"糟糕得多 ——
用户会照着去重下几万张。

### 验收

```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe scripts/verify_slice16_polygon.py   # 真窗口 + 真链路 + 截图
```

验收脚本走五个场景（矩形基线 / 边界缺失 / 边界就位 / 下载→Inspector→打包 / 静态守卫），
截图落在 `docs/images/slice16_*.png`。完整说明与 13 步手工清单见
[`docs/polygon_region_download.md`](docs/polygon_region_download.md)。

### 这一 Slice 明确**不做**的事

- 不支持跨 180° 经线的几何（拒绝，而不是悄悄裁一半）；
- 不内置任何国界数据，也不做拼音/名称猜测（只内置「中国」一个预设）；
- 不做"按区域自动切分大任务"；
- 不动矩形路径的任何既有行为。

## Slice 10：ADB 设备管理与离线地图包传输

**Slice 9 把包做好了，但它还在 Windows 上。** Slice 10 干的事只有一件：把这个目录搬到
Android 设备的公共导入区，并且在设备侧**证明**搬对了。

```
本地 <包目录>/                          远端 <导入区>/<manifest.id>/
├── map.mbtiles        ──push──→        ├── map.mbtiles
└── manifest.json                        └── manifest.json
```

导入区默认是 `/sdcard/Download/TiandituOfflineMap/import`（配置在
`settings.DEFAULT_ANDROID_IMPORT_DIR`）。**不碰 `/data/data`、不用 root、不用
`run-as`、不 `adb install`、不 `adb remount`**——这是 Spec 点名的禁止项，也是本层
与 Slice 11 的分界线：这里只搬文件，端侧怎么导入是 Slice 11 的事。

分层与前面几个 Slice 一样，每层只回答一个问题：

| 模块 | 只回答 |
|---|---|
| `app/android/adb_client.py` | 怎么把 adb 命令**安全地**跑起来（列表参数、不 shell、流式输出） |
| `app/android/device_manager.py` | 怎么把 `adb devices -l` 与 `getprop` 的输出变成设备 |
| `app/android/transfer_models.py` | 传输计划 / 进度 / 结果长什么样；远端路径怎么算才安全 |
| `app/android/map_package_transfer.py` | 传输的**顺序**与失败分支（mkdir → push → mv → 校验） |
| `app/services/android_transfer_service.py` | 编排：设备 + 包校验 + 传输三件事收成一组界面能调的动作 |
| `app/workers/android_transfer_worker.py` | 线程胶水：QThread + 信号，主线程只收结果 |
| `app/ui/device_panel.py` | 显示 + 发请求（一个 adb 调用都没有） |

### 顺序是设计的一部分（Spec §19 / §20）

```text
mkdir 远端目录
push  map.mbtiles  →  map.mbtiles.part      ← 先传大文件，且不叫正式名字
mv    map.mbtiles.part  →  map.mbtiles      ← 原子改名：这一刻才算"传好了"
push  manifest.json                         ← 最后传，它是"包齐了"的信号
校验  远端大小 + 远端 SHA256（best-effort）
```

为什么不直接 `push map.mbtiles`：`adb push` **没有原子性**。如果直接写正式名字，Android
端（Slice 11）可能在文件只写了一半时就看到一个"文件名齐全"的包——它会认为传输完成了，
然后拿着一个截断的 MBTiles 去解析。`.part` + `mv` 让"文件是否存在"变成一个**可信的信号**。
`manifest.json` 同理放在最后：大文件还没传完时它不该出现。

### `-s` 绝不省略（Spec §21）

板子、手机、模拟器同时插着是常态。`AdbClient` 的 `shell()` / `push()` 在 serial 为空时
**直接抛 `ValueError`**，而不是退化成"adb 自己挑一台"——那会让包传到一个用户没看着的设备上。
验收脚本会从假 adb 记下的每一次真实调用里把 `-s` 后面的序列号抠出来，断言它们全是同一台。

### 删除只发生在两个**明确**的场景（Spec §23 / §24）

- **取消**：只删我们自己的 `<文件>.part`，**绝不碰**用户已有的完整包；
- **覆盖**：设备上已有同名包时先停下来问（`needs_confirmation`），用户点了「覆盖」
  才 `rm -rf <导入区>/<id>` 再重传。

设备掉线时**故意不清理** `.part`：那个残骸是排查"线是不是松了"的唯一实物证据（Spec §35）。
取消才清（那是用户自己要的干净）。

### 远端路径是攻击面（Spec §15 / §43）

`manifest.id` 是**文件里的内容**，它会被拼进设备端命令。所以它要过两道：白名单
（只留 `A-Z a-z 0-9 _ - .`，其余换 `_`）+ 纯点组合（`..`）单独挡掉，并且有长度上限与
兜底名；远端根目录也过 `is_safe_remote_root()`（`/`、空串、不含 `/` 的一律拒绝）。
验收脚本对一组危险输入（`../../etc`、`-rf /`、`a/../../b`、`..\..\win`、空串……）
逐个断言"拼出来的路径一定在导入区之内"。

### 这一 Slice 的三条"不做什么"

- **不实现 Android 端导入**（那是 Slice 11）、不碰 App 私有目录、不解析设备上的 MBTiles；
- **不做 sdcard 之外的事**：不 `install`、不 `root`、不 `remount`、不压缩、不改包内容；
- **不让 `adb` 跑到 Qt 主线程上**：`version` + `devices -l` + 每台设备四次 `getprop`
  是十几个进程往返，在事件循环里跑就是肉眼可见的卡顿。

### 这一 Slice 抓出来的四个缺陷

`pytest` 1299 条全绿的时候，下面这些还都活着——它们要么只在**真实子进程**下现形，
要么只在**真实窗口**下现形：

1. **取消路径的 `if` 顺序写反了。** `_run()` 里先判 `pushed is None` 再判
   `result.cancelled`，而取消时 `_push()` 返回的**也是** `None`——于是清理 `.part`
   的代码成了死代码。设备上会留下一个几 GB 的 `map.mbtiles.part`，而用户以为自己取消了。
   抓到它的是 `test_cancel_removes_part_and_keeps_complete_packages`（单元测试）。
2. **进度条在成功那一刻倒退回 0。** `TRANSFER_STAGE_RANGES` 里漏了 `done`，于是收尾帧
   落到默认区间 `(0, 100)` 的起点 —— 传输成功、进度条空。补上 `"done": (100, 100)`。
3. **[选择地图包] 点了没反应。** 槽里写的是 `self.download_panel.output_dir()`，而
   `DownloadPanel` **没有**这个方法 → `AttributeError`，Qt 把槽里的异常吞掉，
   表现就是"按钮点了什么都不发生"。**只有真的点一下才发现**——验收脚本抓到的。
4. **默认窗口下设备区要滚一下。** 真实字体 / DPI 下面板自然高度是 430px，而右列只给了
   410（视口 406）→ 默认尺寸就冒出 24px 的竖直滚动条，用户要滚才能看到 [传输到设备]。
   照着量出来的数字把配比改成 450。`offscreen` 字体偏小，这条**在 pytest 里看不见**。

## Slice 9：离线地图包构建与校验

**`.mbtiles` 只是"地图数据"，不是"能交付的包"。** 一个能交给 Android 的离线包，
除了数据本身，还得带上"这份数据是什么、多大、原本长什么样、有没有被改过"——也就是
`manifest.json`。Slice 9 把 Slice 4 产出的 `map.mbtiles` 收口成一个可交付、可验证的目录。

几层各管一件事（边界和 Slice 8 一样写死）：

| 模块 | 只回答 |
|---|---|
| `app/package/models.py` | manifest 长什么样：字段、顺序、序列化、解析、异常类型 |
| `app/package/checksum.py` | SHA256 怎么算、大文件怎么复制（**分块**，与业务无关） |
| `app/package/package_builder.py` | 怎么把源文件变成一个包（顺序、原子性、失败清理） |
| `app/package/package_reader.py` | 怎么在**不信任** manifest 的前提下安全地读出路径 |
| `app/package/package_validator.py` | 这个包还能不能信（只读：报告问题，不改东西） |
| `app/ui/package_dialog.py` | 显示、发请求、把失败说清楚（计算全在后台线程里） |

### 先写临时目录、再改名（Spec §15）

打包最怕的不是"失败"，是**"看起来成功了，其实是个半成品"**。所以整个构建过程都写在
`<输出目录>/<包名>.building/` 里，只有当 **`map.mbtiles` 复制完 + SHA256 算完 +
`manifest.json` 写完 + 发布前自校验 PASS** 这四件事全部成立，才 `os.replace()` 到正式
目录名。任何一步出问题，`.building` 直接删掉——**正式目录名要么不存在，要么就是完整
可用的**，没有中间态。

这条有测试逐个门守着：复制中途异常 / SHA256 抛错 / 复制失败 / 自校验不过，四条失败
路径都必须做到"正式目录不存在、`.building` 也没留下"。

### 源文件是 **copy**，不是 move（Spec §8）

用户的 `beijing.mbtiles` 是他自己的资产，打包**不动它一个字节**（验收脚本前后各算一次
SHA256 比对）。唯一例外：源文件本身已经就在 `<包>/map.mbtiles` 这个位置上——那时原地
构建，不自己复制给自己。

### 覆盖 = 旧包改名备份（Spec §16）

目标目录已存在时**不删**，先改名成带时间戳的备份，再建新的。理由很直接：覆盖是用户
手滑最贵的一次操作，而磁盘很便宜。测试里有两条反向用例：覆盖**失败**时旧包必须还在；
覆盖成功时**备份里必须还是原来那一份**（不是空壳）。

### manifest 里不许有"猜"出来的东西

`bounds` / `minZoom` / `maxZoom` / `tileCount` / `format` 全部来自 **Slice 8 那份检查
结论**，不是从文件名或界面参数推的——推出来的东西迟早和文件对不上，而"对不上"的那一刻
用户是不知道的。唯一需要推导的是 `composition`：老工具生成的 MBTiles 可能根本不写这个
字段，这时按 `labels` 推（`labels=true` → `merged`，否则 `single`），**并且在结果里挂
一条 warning 说明"这个值是我推的"**，而不是装作它本来就有。

### 读 manifest 也是攻击面（Spec §10）

`manifest.json` 是文本，`mbtiles.file` 是文本里来的路径。如果它写着
`../../../Windows/System32/...`，一个只会 `join` 的实现就会去动包**外面**的文件。
所以两道关：文件名先过白名单正则，解析出的路径再过一次 `resolve()` +
`ensure_inside()`（必须真的落在包目录内）。有测试直接拿 `../evil.mbtiles` 打过来。

### 构建器**不抛异常**

`MapPackageBuilder.build()` 的任何故障都变成 `PackageBuildResult.errors` 里的一行。
理由是调用方有三个（服务层 / GUI 后台线程 / 脚本），抛异常的话三处都要各写一遍 `try`，
而后台线程里漏掉一个就是整个进程的锅。用返回值表达失败：一处写，三处安全。

### 界面（Spec §17 / §20 / §21）

- **`[文件] → [生成地图包]`**：选源文件 → **自动**跑一次快速检查（后台线程）→
  「地图信息」区显示地图类型 / Zoom / 瓦片数；检查 FAIL 时 **[生成地图包] 直接禁用**，
  不让用户点到一半才发现没有数据。
- 构建跑在 `PackageBuildWorker`（`QThread`）里，进度按档位映射到总进度（检查 / 复制 /
  SHA256 / 写 manifest / 自校验 / 发布）；完成后「地图包内容」区给出**目录、大小、瓦片、
  Zoom、完整 SHA256、验证结论**，外加 `[打开目录]`。
- **`[验证地图包]` 是独立入口**（Spec §21）：主窗口菜单可以直接打开，也可以从打包完成
  视图去验一个**已经存在的包**——不重新打包、不经过网络。
- 需要用户拍板的两件事（源有警告 / 目标已存在）**不在构建器里弹窗**：构建器只返回
  `needs_confirmation` + `confirmation_reason`，由界面去问。构建器不 import Qt。

### 这一 Slice 写测试时抓出来的三个缺陷

都是"看着对、其实不对"的那种：

1. `WrappingLabel.setText()` 走的是 `QLabel.setText`，而 `text()` 返回的是自己存的
   `_source_text`——**写进去的和读出来的不是同一个东西**。界面显示占位符 `—`，而断言
   "已经填好了"的代码却是绿的。
2. 构建器在 metadata 没写 `composition` 时**默认填了 `single`**，没有按 `labels` 推导
   ——单图层包会被标成合成包（Spec §24）。
3. 校验完成后 `_on_validated` 先渲染四行结论、紧接着又渲染了一次空的诊断列表，**把刚
   画好的结论擦掉了**——用户看到的是一个空框。

> 验收脚本自己也吃过一次亏：它把瓦片数**写死**成 `7×6=42`，而 `BOUNDS` 在 z12 下实际是
> 7×8=**56**，于是每次都"失败"。现在这个数改成用生产同一套瓦片数学**算出来**的，断言变成
> "manifest 里的数字 == 算出来的数字"——脚本不会再跟着参数一起过期。

## Slice 8：MBTiles 完整检查与诊断工具

**"这个 .mbtiles 到底能不能用、哪里有问题"必须是一份看得见、写得下、导得出的结论**，
而不是让用户自己装个 SQLite 工具去看。Slice 8 把 Slice 4 那个只回答"能不能读"的
`validate_mbtiles`，升级成一个独立的诊断工具：`app/mbtiles/diagnostics.py` 负责
**判定规则**，`app/services/mbtiles_inspection_service.py` 负责**编排**（顺序、
进度、兜异常），`app/ui/mbtiles_inspector_dialog.py` 负责**展示**。

三层各管一件事，这是本 Slice 最重要的一条边界：Reader 只回答"库里有什么"（SQL
只出现在它那里），diagnostics 只回答"这算不算问题"，service 只回答"按什么顺序跑"。
好处是规则可以脱离 SQLite 单独断言 —— 构造一个假的 reader 就能测。

- **三档结论，`PASS` 必须是可达的** —— `errors` 有内容 → `FAIL`；只有 `warnings`
  → `WARNING`；都没有 → `PASS`。看起来平平无奇，但这里藏着一个真实的坑：Slice 4
  校验器会把"已抽样检查 N 张瓦片""抽样 N 张的图片格式为 [...]"这类**说明性文字**
  写进 `warnings`，如果直接并进来，一个完美文件也会被判成 `WARNING` —— **`PASS`
  将永远不可达**。于是诊断结果多了第三档 `notices`（普通提示）：它照样显示、照样
  进报告，但**不影响结论**。这也正是 Spec §20 那句"少个 description 不该判 FAIL"
  的落点（缺 `description` 只记 `WARNING`，文件仍然 `valid=True`）。

- **检查项**（Spec §3–§14）：文件存在 / 是普通文件 / 非空 → SQLite 可打开 →
  `metadata` 与 `tiles` 表存在、`tiles` 四个必需字段齐全 → `PRAGMA quick_check`
  （重量级的 `integrity_check` 只在深度检查时跑）→ metadata 诊断（`format` 读不读
  得动、`bounds` 是否落在 ±180 / ±85.05112878 之内、声明 zoom 与实际 zoom 是否一致）
  → Zoom 统计（一条 `GROUP BY`，**绝不读 `tile_data`**）→ TMS/XYZ 坐标越界 →
  重复坐标 → 空瓦片 → 图片抽样 → 覆盖完整性 → 可选全量深度扫描。

- **抽样是分层的，而且可复现** —— 每个 zoom 都抽一点，而不是 `ORDER BY zoom_level
  LIMIT 50`（那会全落在最小 zoom 上，"z17 的瓦片坏了"这种问题**永远抽不到**）；
  定位用 `OFFSET` 而不是 `ORDER BY random()`（后者要先给全表排序建临时 B-tree）。
  同一份库每次抽到同一批，结论才可能被复现 —— "有时 PASS 有时 FAIL"根本没法排查。

- **图片判定的两级** —— magic bytes（复用 Slice 2 的 `detect_image_format`）先否掉
  错误页/被截断的响应体；再用 Pillow `verify()` 真解一遍容器结构。**只有第二级才
  抓得到"PNG 文件头 + 垃圾正文"**，而这恰恰是下载链路最容易产生的一类损坏。

- **覆盖完整性复用 Slice 1 的 `calculate_tile_range`** —— 下载时算的目标集合与这里
  算的期望集合必须语义一致，否则"下完了却报缺失"这种自相矛盾的结论迟早会出现。
  缺失明细按 **XYZ** 输出（`z/x/y`），并且**封顶 100 条**：世界范围 z14 是 1.6 亿格，
  把一个坏库的缺失坐标全展开，报告就成了几百 MB 的垃圾。缺得多时报告里写"另有
  N 张未展开"。缺失**只记 WARNING，绝不判 FAIL** —— 结构与图片都有效时，文件仍然
  局部可用，用户拿它看已经下好的那一块完全没问题。

- **深度检查必须流式，而且必须能被放弃** —— `iter_tiles()` 走 `fetchmany(500)` 分批，
  **不是** `fetchall()`：50 万张瓦片每张几十 KB，一次性取回就是几个 GB 的常驻内存。
  用户关掉对话框时靠 `should_stop` 回调让扫描尽快收手，并按 `deep_scan_complete`
  标明"没扫完"—— 半截的损坏张数被当成全量结论，是"数字对不上"里最难查的一类 bug。

- **不许跑在 Qt 主线程**（Spec §18）—— 检查要跑几十秒甚至几分钟，全部在
  `MBTilesInspectionWorker`（`QThread`）里执行，经队列信号回主线程；
  对话框**绝不带 `WA_DeleteOnClose`**（后台线程还在跑时销毁它的 QThread 会让 Qt
  直接中止进程），关窗走 `shutdown()`：先置放弃标记、再有界 `wait()`。

- **只读**（Spec §23）—— 连接由 Reader 以 `mode=ro` 打开，诊断层只发
  `SELECT` / `PRAGMA`，一条写语句都没有。唯一的写盘是**旁边的报告文本**
  `xxx.mbtiles.report.txt`（UTF-8），检查工具绝不修库。

- **下载收尾顺手诊断一次**（Spec §22）—— 下载完成链路改调
  `MBTilesInspectionService.inspect(..., deep_scan=False, sample_size≈20)`，
  完成弹窗里因此能多报出"缺失瓦片"与"抽样损坏"两项；绝不会为了这个在每次下载结束
  后全量解一遍图片。老接口 `MBTilesValidationResult` 由 `to_validation()` 桥接，
  字段语义不变，既有调用方无需改动。

## Slice 7：Windows GUI 下载管理与交互完善

**按钮的可用性就是用户唯一能看到的那个状态机** —— 九个界面状态到四个动作的映射
集中在 `app/ui/task_actions.py` 的 `AVAILABLE_ACTIONS` 一张表里，`DownloadPanel`
只负责照着表 `setEnabled`。这张表是**纯 Python**（不 import Qt），因此每一格都能
脱离窗口断言。挑几个不那么显然的格子：`SCANNING` 放开「取消」（扫描几十万张要几秒
到几十秒，四个按钮全灰会让用户以为卡死）；`CONFIRMING` **只**放开「取消」（放开
「开始下载」等于绕过那次确认，而那正是拦住"静默启动几十万张瓦片"的东西）；
`PAUSED` / `FAILED` / `CANCELLED` 都放开「开始」（它们没占用下载通道，禁用它们会
逼用户先删掉记录才能下别的图）。

- **校验只有一个入口** —— `app/ui/validation.py` 的 `validate_form()`，[开始下载] 与
  [测试连接] 共用同一份规则，**一次报出全部问题**（让用户改一个再被拦一次是最容易
  让人关掉程序的交互）。它不 import Qt，所以"哪条规则在什么输入下触发"可以逐条单测。
  校验的边界是"这个任务能不能启动"，不是"这个任务值不值得"——几十万张瓦片不在这里拦
- **SQLite 绝不碰主线程** —— 「已有多少张瓦片」是一次 `GROUP BY zoom_level` 查询；
  快不代表可以在 UI 线程跑（它会随用户的库增长而变慢，且出现在两条高频路径上）。
  Slice 7 把它移进 `ExistingTileScanWorker`（`QThread`），结果经队列信号回主线程。
  扫描期间界面显示「正在扫描已有瓦片…」并只放开「取消」，而不是僵在那儿
- **暂停分两步** —— 点下暂停先进 `PAUSING`（显示"正在暂停…"），等引擎真的回了一帧进度
  （证明它收到了指令、在途请求也落地了）才落成 `PAUSED`。一上来就显示"已暂停"是在替
  引擎打包票，那一刻它可能还在跑好几张瓦片
- **取消要二次确认，且绝不删数据** —— 取消是不可逆的**动作**，所以先问一次；确认后
  停止下载、drain 写队列、flush、close，磁盘上留下一个**可用的（不完整的）**.mbtiles。
  「继续下载」「重新下载」「取消」三个出口的文案都写明了后果
- **完成小结带一个出口** —— 成功结束后自动跑 `MBTilesValidator`，然后把结论摆进
  非模态小结框，并带一个 **[打开目录]** 按钮（`os.startfile`）。地图下完之后用户想做的
  下一件事必然是"去文件夹里看看那个文件"，让他自己去资源管理器里翻是没道理的
- **失败也要留住成果** —— 失败时 GUI 不崩、给出原因，并提供 **[继续补齐]**；已经有
  瓦片不会被丢掉，点一下就接着下缺的那些
- **关窗先问、再安全收尾** —— 有任务在跑时弹非模态确认框；确认后把任务落盘成
  `PAUSED`（语义是"暂停"而不是"失败/取消"），取消 worker 并等线程回收，
  **绝不 `terminate()` QThread**（那会在 SQLite 写事务中间把线程砍掉）
- **启动恢复只降级、不自动下载** —— 上次异常退出遗留的 `DOWNLOADING` 一律改成 `PAUSED`，
  重启后用户在列表里看到"有几个任务可以继续"，而不是"程序自己开始跑几十万张瓦片去了"
- **任务列表扩到九列** —— 名称 / 地图类型 / Zoom / 状态 / 进度 / 成功 / 失败 / 更新时间 /
  输出文件，数据源仍然只有 `TaskService`（不新建第二份）。测试与调用方通过
  `row_cells(row)["状态"]` **按列名**取单元格——按行号取值会在插入一列后静默测到另一列
- **只记住"下次还能用"的东西** —— 输出目录 / 地图类型 / 缩放区间 / 窗口位置走
  `QSettings`（`app/ui/preferences.py`，Spec §22 明确说"不要做复杂用户配置系统"）。
  **Token 不在其中**：`SECRET_KEY_FRAGMENTS` 把"不存密钥"变成一条会用 `ValueError`
  说话的不变量。另外只恢复**仍然存在**的输出目录——照单全收一个已删除的临时目录，
  用户下次点「开始下载」会立刻撞上"输出目录不存在"的校验错误，一个自己造出来、
  却要用户去收拾的错误
- **文件名真的能落盘** —— `sanitize_map_name()` 把 Windows 非法字符（`\ / : * ? " < > |`）
  与控制字符换成 `_`，去掉结尾的点和空格（文件系统会**静默丢弃**它们，导致实际落盘位置
  与界面显示的「输出文件」对不上），命中 `CON` / `LPT1` 这类保留名时加后缀
- **地图名称为空时自动补一个** —— 用中心点坐标生成 `map_39.05N_116.05E`（不用时间戳或
  随机数）：同一块区域重复下载得到同一个文件名，"已有文件"的确认框才有意义
- **进度信号源头节流** —— `~0.15s`（约 6 次/秒），几十万张瓦片的任务不会把 Qt 事件队列塞满；
  日志里也**不逐张打 INFO**（只汇总），Token 在任何一条日志里都不会完整出现
- **关掉弹窗之后，主窗口还关得掉** —— 所有非模态弹窗都带 `WA_DeleteOnClose`：用户一点
  「关闭」，C++ 对象当场销毁，而 `MainWindow` 那边还攥着一个空壳。之后关主窗口时
  `_close_dialogs()` 对空壳调 `close()` 会抛
  `RuntimeError: Internal C++ object already deleted`，异常从 `closeEvent` 里窜出去，
  `super().closeEvent()` 根本执行不到——**用户看到的现象是「点了 X，窗口关不掉」**。
  修法是给弹窗的 `destroyed` 信号接一根线，销毁时把引用清干净，并在 `_close_dialogs`
  里留一层兜底。这个缺陷**只有在真实平台上点一遍才会出现**（见「界面留档」）
- **弹窗按钮一律中文** —— `QMessageBox` 的标准按钮（`Yes` / `No` / `Close` / `Ok`）
  文案来自 Qt 自己，没装 Qt 汉化目录时是英文。于是完成小结变成了
  「打开目录」和 "Close" 并排。凡是给用户点的标准按钮都显式 `setText` 成中文，
  文案集中在 `settings.py` 的「通用按钮文案」一段

> **本 Slice 不做**：ADB、Android、manifest、离线包级 SHA256、失败瓦片清单与单点重试、
> 速度曲线、替换 GUI 框架、重写 Downloader / MBTiles 核心、改动 XYZ/TMS 约定。

## Slice 6：底图 × 中文注记合成

- **「地图类型」下拉 + 「下载中文注记图层」复选框** —— 用户选的是业务概念（矢量 / 影像地图），底图是 `vec_w` 还是 `img_w` 由 `app/models/map_layer.py` 里 `MAP_TYPE_LAYERS` **唯一那张映射表**回答；UI / Service / Downloader 三处都不再各写一遍 `if 矢量: 用 vec_w`
- **一次下两层、合成一张** —— `CompositeTileDownloader` 对同一张瓦片并发请求底图与注记（**共用同一个 `TileDownloader`**：连接复用、限速、重试三件事与单层模式完全一致，因此"1 万张瓦片 ≈ 2 万次请求"里的每一次仍受 8 请求/秒约束），再用 Pillow 的 `alpha_composite` 合成
- **注记只覆盖它不透明的像素** —— 合成只做 alpha 混合，注记的透明区域原样保留底图；半透明注记与底图混色（这三条都有像素级断言锁住）
- **产物一律 PNG** —— 注记依赖透明通道，JPEG 存不下 alpha；`with_labels=True` 时传非 PNG 的 `image_format` 会**当场报错**，而不是落一个"metadata 说 jpg、内容其实是 png"的库
- **三类失败分开计数**（Spec §7）—— `composite_base_failed` / `composite_label_failed` / `composite_failed`。前两类是网络与配额问题（可重试），第三类是图片本身合不到一起（同样字节重试多少次都一样，因此**不进重试**）。合成失败不会被折叠成"下载失败"，用户能分辨到底哪儿坏了
- **写进 MBTiles metadata 的四个自有字段** —— `provider` / `map_type` / `labels` / `composition`。**只在有值时写入**：Slice 4/5 生成的文件不会被凭空补上这些键，校验器也不会为它们平白报"缺少可选字段"
- **续传按合成方式把关** —— 任务是 `merged`、文件是 `single`（或地图类型不同）时 `resume_task()` **直接拒绝**，并且**一张瓦片都不写**；往不属于这个任务的库里写瓦片，得到的既不是 A 也不是 B，只是一份"有的地块有字、有的没有"的坏数据
- **老文件按事实解读** —— 没有 `composition` 键的 Slice ≤5 文件视作 `single`（那个版本只可能下过单层底图）；反过来说，"要合成的任务续到老文件上"同样被拒绝
- **校验器读懂合成信息** —— `labels=true` 必须配 `composition=merged`、合成地图必须是 `format=png`，且抽样瓦片必须都是 PNG
- **请求量预估说清倍数** —— 面板上三行动态说明（请求哪些图层 / 产物形态 / 每张瓦片几次请求）、「预估结果」里的 `预计 WMTS 请求`、大任务确认框都用同一套数字；勾上注记后请求量与下载量翻倍，必须在**开工之前**就说清楚
- **单层模式一行代码没退化** —— `layer` 传 `MapLayerType` 时行为与 Slice ≤5 完全一致；`with_labels=True` 配单个图层会被拒绝（一块注记瓦片没法回答"它属于矢量还是影像"）

## Slice 5（仍有效）：断点续传、任务恢复与已有瓦片跳过

已支持：

- **任务持久化** —— `TaskStore` 把每个任务写成 `data/tasks.json` 里的**一条记录**（不是每个任务一个文件）；写入走 **临时文件 + `fsync` + `os.replace`** 的原子替换，进程被强杀也不会留下半截 JSON；文件损坏时**改名隔离**成 `tasks.json.corrupt.<时间戳>` 并继续用空表启动，**不静默删除**
- **字段白名单 + 敏感词黑名单** —— 落盘前只挑 `_TASK_FIELDS` 里允许的字段，并拒绝任何 key 含 `tk` / `token` 相关片段的字段；**Token 永远不进任务文件**
- **启动恢复** —— `TaskService.recover_interrupted_tasks()` 把上次遗留的 `DOWNLOADING` 一律改成 `PAUSED`，**不自动开始下载**（是否消耗配额必须由用户决定）；重复调用幂等
- **已有瓦片跳过** —— `ResumePolicy` + `ExistingTileIndex`，按目标规模自动二选一：
  - `SkipMode.CACHED`（目标 ≤ `DEFAULT_RESUME_CACHE_MAX_TILES = 50_000`）——**一次性**把已有坐标预载进内存集合
  - `SkipMode.QUERY`（目标更大）——不预载，逐张 `has_tile()` 查询
  - 两种模式**都只读 `zoom_level / tile_column / tile_row` 三列，默认从不 `SELECT tile_data`**（BLOB 动辄几百 GB，读了就是灾难）；只有 `verify_content=True` 时才按需读内容
- **发请求之前就跳过** —— 跳过判定发生在 HTTP 之前（`DownloadManager.download(should_skip_tile=...)`），命中的瓦片**根本不发请求**，不浪费天地图配额；跳过的瓦片计入 `DownloadSummary.skipped`，且**不逐张打 INFO 日志**
- **续传前的一致性校验** —— `MapDownloadService.resume_task()` 会拿已有 MBTiles 的 metadata 与任务参数逐项比对（西/东/南/北边界、最小/最大缩放、图层）；**任何一项不一致就直接拒绝**（`现有 MBTiles 与当前任务参数不一致，无法安全继续下载。`），并且**一张瓦片都不写**（有测试锁住"拒绝时库里张数不变"）
- **输出文件不存在 = 当全新下载** —— 目标 `.mbtiles` 被手工删掉时续传不会报错，按全新下载处理并在日志里说明
- **Tab 式续传语义** —— 写库统一 `INSERT OR REPLACE`，同一张瓦片续传多次也**只留一行**（不会因为重复下载把体积翻倍）
- **完成判定看数据库** —— 只有「跑完 + 零失败 + 文件通过校验 + **库里有效瓦片数 ≈ 任务目标**」才标 `COMPLETED`；少几张（服务端 404、重试耗尽）一律 `FAILED`，避免用户以为拿到了完整地图
- **部分失败保留 FAILED** —— 失败任务不因"下过一部分"就转成功；状态落盘为 `FAILED` 并带失败原因，之后可以再续传把缺口补上
- **「下载任务」列表** —— 右侧任务面板列出所有任务（名称 / 状态 / 进度 / 输出路径 / 更新时间）；支持**双击回填表单**（Token 需重填——任务记录里不保存 Token）、**继续下载**、**删除记录**
- **删除记录只删 JSON** —— 点「删除」只从 `tasks.json` 移除该条记录，**磁盘上的 `.mbtiles` 原样保留**，并在界面提示这一点
- **已有文件三向确认** —— 输出文件已存在时弹三选一：
  1. **继续下载** —— 保留旧文件，跳过已有瓦片接着下（默认，最省配额）
  2. **重新下载** —— 先把旧文件**改名备份**（`xxx.mbtiles.bak-YYYYMMDD-HHMMSS`），**绝不静默删除**；二次确认框里会显示备份路径
  3. **取消** —— 什么都不做
- **退出确认** —— 关闭程序时若还有任务在跑，弹**非模态**确认框；确认退出会把运行中的任务标成 `PAUSED`（语义是"暂停"而不是"失败/取消"，下次可以接着下）
- **进度节流** —— 任务进度落盘按 **1 秒或 100 张** 节流（两者取先到），但**状态变化**（暂停 / 继续 / 完成 / 失败）一律立刻写盘

> **本 Slice 不做**：manifest、离线包级 SHA256、ADB、Android、失败瓦片清单与单点重试、速度曲线。

- **`MBTilesWriter`** —— 标准 MBTiles 结构（`metadata` + `tiles` 两张表），自动创建父目录，`with` 上下文管理，参数化 SQL；写入前强制先写 metadata（缺 metadata 的 MBTiles 在 osmdroid 侧会直接加载失败）
- **批量事务** —— 默认 `batch_size = 200`（`executemany` + 显式 `BEGIN` / `COMMIT`），**不是每张瓦片 commit**；`flush()` / `close()` / 任务正常结束都保证剩余瓦片落盘
- **XYZ → TMS 只此一处** —— 对外（`TileCoordinate` / `write_tile` / `read_tile` / `has_tile` / 校验报错）统一使用 **XYZ**，落库时才用 Slice 1 的 `xyz_y_to_tms_y()` 翻一次；**未复制第二套转换公式**
- **`MBTilesTileSink`** —— 单 Writer 消费模型：HTTP worker 只管下载，成功瓦片进 **有界队列（默认 200，区间 100~500）**，由一个 writer 串行入库，杜绝多协程抢同一个 sqlite 连接；队列对下载端形成反压，避免"下载快于落盘"时把几万张瓦片堆在内存里
- **`MBTilesReader`** —— `get_metadata` / `get_tile_count` / `get_zoom_range` / `has_tile` / `read_tile`，入参同样是 XYZ、内部自动转 TMS
- **`validate_mbtiles`** —— 检查文件存在、SQLite 可打开、`metadata` / `tiles` 表齐全、metadata 基本字段、瓦片数 > 0、zoom 范围合理，并**抽样最多 20 张**瓦片校验 PNG/JPEG magic bytes（本 Slice 不做全量扫描）
- **`MapDownloadService`** —— 编排层（`app/services/`）：建 metadata → 开 Writer → 起 sink → 跑 `DownloadManager` → drain → flush → close → validate → 返回 `MapDownloadOutcome`（文件路径 / 真实瓦片数 / 体积 / zoom 区间 / 验证结论）。UI 只调这一层
- **取消即保留** —— 取消后停止下载、drain 写队列、flush、close，磁盘上留下一个**可用的（不完整的）** `.mbtiles`，**不自动删除**
- **WAL 的正确收尾** —— 写入期用 `journal_mode=WAL` + `synchronous=NORMAL` 换性能，收尾时 checkpoint 并切回 `DELETE` 模式，保证交付的是**单个自洽文件**（不会留下 `-wal` / `-shm` 旁挂文件）
- **GUI 真实落盘** —— 填「输出目录 + 地图名称」→ `[开始下载]` 真的产出 `xxx.mbtiles`；开始前显示预计瓦片数 / 预计容量 / 输出文件；下载中显示状态 / 当前 Zoom / 总数 / 完成 / 成功 / 失败 / 跳过 / 速度 / 已下载字节 / 总进度；完成后显示「文件 / 瓦片 / 大小 / Zoom / 验证」
- **大任务只提示不限制** —— 预计超过 **10,000** 张时弹一次非模态确认框（Slice 3 的 500 张硬上限已解除），确认后照常下载，规模不设上限
- **失败可读** —— 逐条失败原因 + 汇总里按张数聚合的「失败原因 Top 3」；网络类失败会沿异常原因链取出最深处的真实消息（例如 `网络错误：ConnectError：[SSL: UNEXPECTED_EOF_WHILE_READING] …`），不会只剩一句"网络错误："

> **本 Slice 不做**：已有瓦片跳过、真正的断点续传、任务持久化、图层合成、manifest、SHA256、ADB、Android。

Slice 3（仍有效）：

- **`TileDownloader`** —— `httpx.AsyncClient` 单实例复用（非每瓦片新建），内置超时、指数退避重试与限速；`async with` / `close()` 管理生命周期
- **`DownloadManager`** —— `asyncio.Queue`（有上限）+ **固定数量 worker**，瓦片惰性生成（不在内存里一次性铺开整个 zoom 区间），并发度默认 **6**、范围 **1 ~ 16**
- **`RetryPolicy`** —— 默认 `max_attempts=3`，退避 `0.5s → 1s → 2s`（上限 5s）；重试 408 / 425 / 429 / 5xx、网络错误、超时、空响应、非法图片；**不重试** 400 / 401 / 403 / 404（Token 无效或瓦片确实不存在，重试无意义）
- **`RateLimiter`** —— 默认 **8 请求/秒**（预留式计时，避免脉冲式突发），可整体关闭
- **暂停 / 继续 / 取消** —— 基于 `asyncio.Event`，可从中途任意时刻生效；取消后未跑完的瓦片计为 `skipped`，汇总里 `cancelled=True`
- **进度统计** —— `DownloadProgress`（总数 / 完成 / 成功 / 失败 / 跳过 / 已下载字节 / 实时速度 / 当前 zoom），回调按 **~0.15s** 节流（约 5~10 次/秒），不逐瓦片刷屏
- **`DownloadSummary`** —— 收尾汇总（耗时、平均速度、成功/失败/跳过、取消标志、失败原因聚合），`top_failures()` 取前 3 条按张数倒序展示
- **Token 不外泄** —— 日志与界面统一 `mask_url` 脱敏，`httpx` / `httpcore` 的 INFO 级日志被压制（它们会把带 `tk=` 的完整 URL 打到控制台）

Slice 2（仍有效）：

- **天地图 WMTS URL 生成** —— `TiandituProvider.build_tile_url(layer, zoom, x, y)`，完整 WMTS 1.0.0 参数（`SERVICE`/`REQUEST`/`VERSION`/`LAYER`/`STYLE`/`TILEMATRIXSET`/`FORMAT`/`TILEMATRIX`/`TILEROW`/`TILECOL`/`tk`）
- **四种图层** —— `vec_w`（矢量底图）、`cva_w`（矢量中文注记）、`img_w`（影像底图）、`cia_w`（影像中文注记），仅 Web 墨卡托 `_w`
- **t0 ~ t7 分片** —— 确定性策略 `server_index(x, y) = (x + y) % 8`（不用随机数，保证可复现与可测试）
- **Token 参数** —— 自动 URL 编码；日志/界面一律通过 `mask_url` / `mask_token` 脱敏（`tk=abcd****xyz`），Token 为空抛 `ValueError`
- **单瓦片连通性测试** —— `test_tile()` 只请求一张瓦片、**不写文件**，返回 `TileTestResult`（成功标志、HTTP 状态、Content-Type、体积、耗时、错误原因）
- **PNG/JPEG 合法性验证** —— `is_valid_tile_response()` 以文件 magic bytes（`89 50 4E 47` / `FF D8 FF`）为准，不只看 `Content-Type`；HTML / XML / JSON / `ServiceException` / 纯文本错误页、空响应、超限响应一律判失败
- **GUI 连接测试** —— 地图配置区 `[测试天地图连接]`：取当前范围中心点 → 调 Slice 1 算瓦片 → 后台线程（`QThread`）请求 → 展示 `连接成功 / 图层 / Zoom / HTTP / Size / 耗时`，失败显示明确原因

Slice 1（仍有效）：

- **GeoBounds** —— 带校验的经纬度包围盒（经度 ±180、纬度 ±85.05112878、`west < east`、`south < north`，非法参数抛 `ValueError`）
- **经纬度 → XYZ 瓦片编号** —— `lon_to_tile_x` / `lat_to_tile_y`，支持 zoom 0~22，纬度自动 clamp 到 Web 墨卡托极限
- **BoundingBox → TileRange** —— 单级缩放的瓦片编号区间与 `tile_count`
- **XYZ ↔ TMS 转换** —— `xyz_y_to_tms_y` / `tms_y_to_xyz_y`（MBTiles 前置）
- **Zoom 范围瓦片统计** —— `calculate_tile_ranges` / `estimate_tile_count`
- **容量估算** —— `estimate_size_bytes` / `format_size`
- 「计算」按钮接入 Tile Math，非法输入不崩溃

Slice 0 基础（仍有效）：

- 可运行的 PySide6 主窗口（分区布局：地图配置 / 下载状态 / Android设备 / 运行日志）
- 基础数据模型：图元类型、下载任务、离线包、Android 设备
- 统一配置项 `app/config/settings.py`

当时暂未实现（**尚未排期**，不对应任何具体 Slice 号）：

- ~~地图包 manifest、离线包级 Sha256 校验~~ —— **Slice 9 已交付**（见上）
- ~~ADB / Android 导入（设备发现、push）~~ —— **Slice 10 已交付**（见上）；
  设备侧导入（端侧读 manifest、交给 osmdroid 加载）**Slice 11 已交付**，
  后续 Slice 24 / 25 又补上了 AUTO 按等级选包与 `region.level` 跨端契约
- 失败瓦片清单与单点重试、速度曲线

> 这几项曾经被写进"Slice 7"的计划里，但 Slice 7 实际排给了 Windows GUI 下载管理，
> 因此不再用 Slice 号承诺它们。设备区曾经用一句 `UI_TEXT_DEVICE_HINT`
> （"本版本不执行 adb 命令…"）如实说明"点了不会执行 adb"——那个占位文案在 Slice 10
> 已经删掉，因为现在**真的会执行**了。

## 环境

Python 3.11+（当前使用 `httpx` 做异步 HTTP，`PySide6` 做界面）

## 安装

```bash
# 日常安装（依赖声明了版本范围，含大版本上限）
python -m pip install -r requirements.txt

# 要跑 lint / 类型检查（ruff + mypy）
python -m pip install -r requirements-dev.txt

# 排查"换台机器就跑不起来"时：装回验证过的**同一组**依赖（含传递依赖）
python -m pip install -r requirements-lock.txt
```

三份文件的**分工是刻意的**，别合成一份：

| 文件 | 记什么 | 什么时候用 |
| --- | --- | --- |
| `requirements.txt` | 允许的**范围**（`>=x,<下一个大版本`） | 日常安装 |
| `requirements-dev.txt` | 开发工具（ruff / mypy） | 本地 lint |
| `requirements-lock.txt` | 验证过的**确切版本**（`pip freeze` 快照） | 复现问题 |

范围上限不是形式主义：PySide6 / Pillow / shapely 的大版本升级都真的改过行为，
而测试是跟着当前大版本写的。没有上限时，`pip install -r` 会在别人机器上装出一个
本项目从没验证过的组合，失败原因还藏在依赖差异里。

## 代码检查

```bash
# lint（配置见 pyproject.toml 的 [tool.ruff]，**当前是绿基线**）
.venv/Scripts/python.exe -m ruff check app scripts tests
.venv/Scripts/python.exe -m ruff check app scripts tests --fix        # 自动修可修的

# 类型检查（配置见 [tool.mypy]，**当前不是绿基线**，见下）
.venv/Scripts/python.exe -m mypy app
```

规则集**写死在 `pyproject.toml`**，不用 ruff 的默认值 —— 默认集合会随版本变
（ruff 0.17 的默认就比早期大得多），一旦变了，同一个仓库在不同机器上会得出不同
结论，而原因藏在版本差异里。

`ruff` 抓到的第一个真问题是运行时崩溃级的：`main_window.py` 调用了
`region_target_tiles` 却从没导入它（点「继续」恢复老的多边形任务时 `NameError`）。
这类问题测不出来，只有静态检查能提前发现。

**mypy 目前约 110 条错误，故意不设成门禁。** 主要类别是 `arg-type` / `union-attr` /
`attr-defined`，都是真实的类型不精确，但修它们要动业务逻辑，属于独立的清理任务。
把一条**已知不绿**的检查设成门禁，唯一的效果是让所有人学会忽略它 —— 那比不设更糟。
现状与类别记在 `pyproject.toml` 的注释里，按模块逐块收紧。

## 启动

```bash
python -m app.main
```

## 测试：按模块跑

> 全量测试与端到端脚本在后面的[测试：全量与端到端脚本](#测试全量与端到端脚本)。

```bash
python -m pytest                # 全量（不写死数字：以实际输出为准）
python -m pytest tests/test_resume_download.py -q   # 只跑断点续传链路
python -m pytest tests/test_mbtiles_diagnostics.py tests/test_mbtiles_inspection_service.py -q
                                                    # 只跑 Slice 8 诊断规则 + 编排层
python -m pytest tests/test_checksum.py tests/test_package_builder.py \
                 tests/test_package_reader.py tests/test_package_validator.py -q
                    # 只跑 Slice 9 打包链路（原子构建 / 篡改检测 / 只读保证）
python -m pytest tests/test_adb_client.py tests/test_device_manager.py \
                 tests/test_map_package_transfer.py tests/test_android_transfer_service.py -q
                    # 只跑 Slice 10 传输链路（顺序 / 取消 / 覆盖 / 路径安全 / 不重复哈希）
python -m pytest tests/test_zoom_policy.py tests/test_zoom_strategy.py -q
                    # 只跑 Slice 17 Zoom 策略（数值 / 自洽 / 只提醒不改写 / 复用预估）
python -m pytest tests/test_region_catalog.py tests/test_region_loader.py \
                 tests/test_region_hierarchy.py tests/test_region_validation.py -q
                    # 只跑 Slice 18 行政区域（目录读取 / 层级 / 路径安全 / lazy load / 校验）
python -m pytest tests/test_region_selection_controller.py \
                 tests/test_download_panel_region_selection.py -q
                    # 只跑 Slice 19 级联与策略（选择状态 / 等级推导 / 只读 / 回退 / 持久化）
```

与续传 / 合成 / GUI 交互相关的用例分散在这些文件里：

| 文件 | 覆盖 |
|---|---|
| `tests/test_task_store.py` | 任务记录读写、原子替换、损坏隔离、状态机、Token 不落盘、进度节流 |
| `tests/test_resume.py` | `ResumePolicy` / `ExistingTileIndex` 的策略选择、`TileProbe` 依赖方向、坐标语义 |
| `tests/test_resume_download.py` | 端到端续传：跳过已有、不读 BLOB、一致性拒绝、部分失败→失败→续传补缺口、取消→暂停、日志行 |
| `tests/test_ui_smoke.py` | GUI：任务面板、启动恢复、已有文件三向确认、退出确认、取消 / 完成 / 失败三条收尾路径；图层说明与请求量预估、合成接线 |
| `tests/test_tile_composer.py` | 合成器：像素级 alpha 正确性、失败分类、尺寸阈值 |
| `tests/test_composite_downloader.py` | 合成下载器：每张瓦片两次请求、三类失败归类、生命周期约定 |
| `tests/test_composition_download.py` | 端到端合成：metadata 四字段、产物必须 PNG、参数组合拒绝、续传一致性把关 |
| `tests/test_task_actions.py` | **Slice 7** 状态 → 按钮矩阵逐格断言、持久化状态映射、瞬时状态不落盘 |
| `tests/test_ui_validation.py` | **Slice 7** 下载前校验：一次报全部问题、逐条规则、自动地图名称 |
| `tests/test_filename.py` | **Slice 7** 文件名净化：非法字符 / 控制字符 / 结尾点空格 / Windows 保留名 / 截断 |
| `tests/test_ui_preferences.py` | **Slice 7** 界面偏好：读写往返、"不存 Token"不变量、损坏值不崩、测试隔离 |
| `tests/test_tile_scan_worker.py` | **Slice 7** 后台扫描：计数正确性、**确实跑在另一个线程**、放弃标记、失败不冒泡 |
| `tests/test_download_worker.py` | **Slice 7** 线程胶水：进度先于完成、信号回主线程、暂停 / 取消、异常变信号、Token 无公开出口 |
| `tests/test_ui_widgets.py` | **Slice 7** 折行标签：逐字符断词、以及"次要文字色"在深浅两套调色板下的对比度 ≥ 4.5:1 |
| `tests/test_checksum.py` | **Slice 9** 流式 SHA256（分块、进度、可中止）、大文件复制、拒绝自我复制 |
| `tests/test_package_builder.py` | **Slice 9** 原子构建：四条失败路径都不留半成品、copy 不是 move、覆盖 = 改名备份、警告与 FAIL 两种"先问用户"、进度单调收在 100% |
| `tests/test_package_reader.py` | **Slice 9** manifest 解析与路径安全：`../` 穿越被拒、未来 `formatVersion` 被拒、损坏 JSON 只说人话 |
| `tests/test_package_validator.py` | **Slice 9** 四道关逐条断言、改一个字节 → FAIL、处理不了的异常一律不冒泡、结论与构建器一致 |
| `tests/test_adb_client.py` | **Slice 10** 参数永远是列表、源码里没有 `shell=True`（AST 守卫）、超时 / 命令起不来 / adb 不存在都变成结果、push 跨 chunk 解析百分比、stderr 必须被抽干 |
| `tests/test_device_manager.py` | **Slice 10** 真实 `adb devices -l` 输出解析、未授权 / 离线都显示、单个 `getprop` 失败不影响整台设备、没 adb 时不炸 |
| `tests/test_map_package_transfer.py` | **Slice 10** 顺序（mkdir < push(.part) < mv < push(manifest)）、`.part` 一定被清掉、取消只删自己的文件、覆盖前先问、远端大小 / SHA256 不一致要 FAIL、设备不支持 sha256 只是 WARNING、危险 id 拼不进路径 |
| `tests/test_android_transfer_service.py` | **Slice 10** 编排层：刚校验过就不再重复哈希、目录一变缓存立刻作废、所有方法都不抛异常、adb 不存在 / 设备未授权都给出人话 |
| `tests/test_geojson_loader.py` | **Slice 16** GeoJSON 四种顶层形态、坏数据逐类报错、自交几何只能显式修复、跨 180° 拒绝、UTF-8 要求 |
| `tests/test_tile_region_filter.py` | **Slice 16** `prepared + intersects`：压边瓦片必须命中、只碰一个角也算、洞要真被挖掉、prepared 只构造一次、空几何拒绝 |
| `tests/test_region_estimator.py` | **Slice 16** 候选 / 区域内 / 过滤三级统计、矩形 selected==candidate、逐 zoom 求和自洽、容量与请求量取自 selected |
| `tests/test_polygon_download.py` | **Slice 16** 端到端下载：候选 100 / 命中 60 → 库里 60；区域外的瓦片**一次请求都不发**；被裁掉的不进 failed/skipped |
| `tests/test_polygon_resume.py` | **Slice 16** 续传：只跳过区域内已有的那 40 张、tasks.json 里不出现几何、边界文件没了就拒绝续传、老任务读作矩形 |
| `tests/test_polygon_inspection.py` | **Slice 16** Inspector：多边形按几何算期望值；边界不可用标 UNKNOWN + WARNING，**不许**按外包矩形误报"缺失 N 张" |
| `tests/test_zoom_policy.py` | **Slice 17** 四级区间逐个数钉死、策略自洽与重叠带、CUSTOM 不警告、单一来源三条静态守卫、`app.geo` ↔ `app.planning` 导入顺序（子进程） |
| `tests/test_zoom_strategy.py` | **Slice 17** 推荐 / 评估（超范围只 warning、**不改写**用户 Zoom）、非法输入逐条拒绝、`recommend_with_estimate` 与直接调 `RegionTileEstimator` 逐字段一致 |
| `tests/test_region_selection_controller.py` | **Slice 19** 级联状态（换国家清省 / 市、只选国 / 省 / 市各自的等级）、推荐区间取自策略、自定义保原值、`ZoomMode.parse` 宽容、`download_region` 真的用边界、缺 / 坏边界安全返回 `None` |
| `tests/test_download_panel_region_selection.py` | **Slice 19** 控件行为：矩形模式逐像素兼容、三段下拉联动、推荐模式自动填值且只读、模式切换不改值、两个单选互斥、范围倒置只提示、估算用区域内张数、偏好往返与脏 id 回退 |

`tests/test_ui_smoke.py` 里还有两条**专门盯着"弹窗关掉之后"**的用例
（`test_dismissing_complete_dialog_then_closing_window_is_safe` /
`..._failed_dialog_...`）：它们断言弹窗销毁后主窗口仍然关得掉。
这个缺陷是**实机验收脚本**跑出来的，不是 pytest——细节见「界面留档」一节。

测试在 Qt ``offscreen`` 平台下运行（见 `tests/conftest.py`），无需真实显示器；
瓦片下载与落盘相关用例全部走 **MockTransport / 假数据**，**不访问真实网络**，
产物写到临时目录（`work_dir` 夹具，不用本机 `tmp_path`——它会被沙箱拦在 `WinError 5`）。
`tests/conftest.py` 在导入时就压制 `httpx` / `httpcore` 的 INFO 日志：它们会把带
`tk=<Token>` 的完整 URL 打到控制台，而日志里不允许出现真实 Token。

**界面偏好也必须注入临时实例**（`preferences` 夹具）：生产里 `UiPreferences()` 落在
注册表，而主窗口在 `closeEvent` 里会 `save_geometry` + `set_output_dir`。测试若用默认
实例，一个用例点过「开始下载」，下一个用例装配窗口时就会**恢复**上一个用例的窗口形状与
地图类型——断言默认值的用例开始成片地红，而失败信息指向"业务逻辑坏了"，完全看不出真凶
是注册表。`tests/test_ui_preferences.py` 有一条用例专门断言落点确实是 ini 文件。

异步用例依赖 `pytest-asyncio`，模式配置在仓库根的 `pytest.ini`（`asyncio_mode = auto`，
无需给每个用例手写 `@pytest.mark.asyncio`）。

## 界面留档

```bash
python scripts/capture_ui.py                   # 渲染主窗口为 docs/images/ui_main_window.png
python scripts/capture_ui.py --offscreen       # 无显示环境（离屏渲染）
python scripts/capture_ui.py --with-tasks -o docs/images/slice5_task_panel.png
```

`--with-tasks` 会预置几条不同状态的任务记录，用于留档 Slice 5 的「下载任务」面板
（见 `docs/images/slice5_task_panel.png`）。它写的是**临时目录**里的 `tasks.json`，
不会碰到你真实的 `data/tasks.json`。

Slice 17 的「Zoom 建议」行同样有实机留档 —— 那一行是**新增控件**，最容易被挤扁：

```bash
python scripts/verify_slice17_zoom.py              # 真 windows 插件 + 真窗口 + 真按钮 + 51 项检查
python scripts/verify_slice17_zoom.py --offscreen  # 无显示环境（CI）
python scripts/verify_slice17_zoom.py --no-shots   # 只跑检查，不截图
```

截图落在 `docs/images/slice17_zoom_rectangle_baseline.png`（自定义矩形：整行收起）、
`docs/images/slice17_zoom_recommended.png`（中国 + Z5~10：只有推荐与当前）、
`docs/images/slice17_zoom_warning.png`（中国 + Z8~18：警告出现，而 Zoom 仍是你设的 8~18）。
脚本最后一段还会**真的跑一次多边形下载**（MockTransport 供瓦片，不触网），
断言"库里张数 == 区域内张数"，证明策略层没有碰坏 Slice 16 的下载链路。

Slice 18 的三级行政区目录同样有实机留档 —— 下拉框是**新增的数据来源**，
最容易被"退回内置常量"这种改动悄悄破坏：

```bash
python scripts/verify_slice18_regions.py              # 真 windows 插件 + 三级目录 + 路径安全 + 79 项检查
python scripts/verify_slice18_regions.py --offscreen  # 无显示环境（CI）
python scripts/verify_slice18_regions.py --no-shots   # 只跑检查，不截图
```

截图落在 `docs/images/slice18_regions_dropdown.png`（下拉项全部来自目录）、
`docs/images/slice18_regions_city_zoom.png`（选中市级：建议 Z12~15、自定义 8~17 会报警）、
`docs/images/slice18_regions_three_levels.png`（按市级区域真的算一次）。
脚本在临时目录里造的是**合成方块**，不往仓库的 `resources/regions/` 写任何东西 ——
法定地图内容必须由使用者用授权数据自己放。

Slice 19 的级联与策略切换是**控件之间的关系**（三个下拉联动、两个单选互斥、只读切换，
以及"关窗重开还在不在"），这些恰好是 offscreen 下最容易失真的部分：

```bash
python scripts/verify_slice19_region_ui.py              # 真 windows 插件 + 真窗口 + 真关窗再开 + 90 项检查
python scripts/verify_slice19_region_ui.py --offscreen  # 无显示环境（CI）
python scripts/verify_slice19_region_ui.py --no-shots   # 只跑检查，不截图
```

截图落在 `docs/images/slice19_cascade_city.png`（级联选到市级；当时是三级，Slice 27 已补县区）、
`docs/images/slice19_zoom_recommended.png`（推荐模式：Min/Max 自动填 Z12~15 且只读）、
`docs/images/slice19_zoom_warning.png`（自定义 8~17：有警告，值没被改）、
`docs/images/slice19_polygon_estimate.png`（真点「计算」后的结果区，含逐级明细四列）。
恢复那一段是**真的关一次窗**（走 `closeEvent` → `_save_preferences`）再用同一个
偏好文件开一个新窗口 —— 写个 `set_region_id()` 再断言控件状态是自欺。

Slice 10 的设备区与三个弹窗同样有可重复的实机留档：

```bash
python scripts/verify_slice10_android.py              # 真 adb 替身 + 真窗口 + 118 项检查 + 截图
python scripts/verify_slice10_android.py --offscreen  # 无显示环境（CI）
python scripts/verify_slice10_android.py --no-shots   # 只跑检查，不截图
```

截图落在 `docs/images/slice10_device_panel.png`（刷新之后的三台设备 + 详情）、
`docs/images/slice10_transfer_confirm.png`（传输确认框）、
`docs/images/slice10_transfer_progress.png`（传输中，进度条真的在走）、
`docs/images/slice10_transfer_done.png`（完成）、
`docs/images/slice10_overwrite_confirm.png`（设备上已有同名包 → 覆盖确认）。

> "量出来，不要看" 这条经验在 Slice 10 又用了一次：设备区的滚动条**只能靠量**。
> `offscreen` 字体偏小，pytest 里 `verticalScrollBar().maximum()` 是 0；切到真实
> windows 平台插件就变成 24——默认窗口下用户要滚一下才能看到 [传输到设备]。修法不是
> "看图觉得还行"，而是量出面板自然高度 430px，把右列配比从 410 改成 450。

Slice 9 的打包与校验同样是**可重复的实机验收**：

```bash
python scripts/verify_slice9_package.py              # 真窗口 + 75 项检查 + 截图
python scripts/verify_slice9_package.py --offscreen  # 无显示环境（CI）
python scripts/verify_slice9_package.py --no-shots   # 只跑检查，不截图
```

四个场次：**A** 服务层跑完整条链路（构建 → 校验 → 改一个字节 → 必须 FAIL → 复原），
**B** 警告确认 / FAIL 拒包 / 覆盖备份，**C** 真窗口点按钮（构建 → 校验 → 篡改后重验），
**D** 静态守卫（对话框里没有 SQLite 调用、校验器里没有 `INSERT/UPDATE/DELETE/DROP`、
`checksum.py` 里没有整文件 `read_bytes`）。截图落在
`docs/images/slice9_package_{dialog,complete,validate_pass,verify_fail}.png` 与
`docs/images/slice9_validate_dialog.png`。

> 场次 C 里有一条断言是拿**几何量**做的，不是看图：`_layout_problems()` 会检查顶层
> 布局里兄弟控件的 `geometry()` 有没有相交、有没有控件越过内容区顶边或对话框底边。
> 起因是我盯着一张缩图，觉得第一个 group box 的标题压住了别的东西；逐行扫像素才发现
> `y=0~9` 完全是空的——**是缩图的抗锯齿骗了眼睛**。人眼会被骗，`geometry()` 不会。

Slice 8 的检查窗口用专门的验收脚本留档（它同时是一份**可重复的实机验收**）：

```bash
python scripts/verify_slice8_inspector.py              # 真窗口 + 52 项检查 + 截图
python scripts/verify_slice8_inspector.py --offscreen  # 无显示环境（CI）
python scripts/verify_slice8_inspector.py --no-shots   # 只跑检查，不截图
```

它真的造四种文件（正常 / 缺瓦片 / 含坏图片 / 非 SQLite）、真的点按钮、真的等后台
线程，然后按结论分别截 `docs/images/slice8_inspector_{pass,warning,fail,idle}.png`。

> 为什么不能只靠 `pytest`：`pytest` 跑在 Qt `offscreen` 平台上，没有真实字体、
> 没有 DPI 缩放、没有系统深浅色主题。而"检查"这个功能的价值**恰恰全在展示上**。
> 这套脚本确实抓出过两个 offscreen 测试看不见的问题：状态那一行被判成
> `PASS` 却在窄窗口里被裁掉一半；以及 Pillow 的原始报错
> （`cannot identify image file <_io.BytesIO object at 0x…>`）**连对象地址一起**
> 被写进了界面和报告 —— 用户看不懂，而且同一份坏文件每次的文案都不一样，
> 根本没法比对。

### 实机验收（Spec §28 场景 A–E）

```bash
python scripts/verify_slice7_ui.py                 # 真窗口 + 真按钮 + 49 项检查
python scripts/verify_slice7_ui.py --offscreen     # 无显示环境（CI）
python scripts/verify_slice7_ui.py --scenes acd    # 只跑指定场次
python scripts/verify_slice7_ui.py --no-shots      # 不生成截图

python scripts/verify_slice8_inspector.py          # Slice 8：真窗口 + 52 项检查
python scripts/verify_slice9_package.py           # Slice 9：真窗口 + 75 项检查
python scripts/verify_slice10_android.py          # Slice 10：真 adb 替身 + 118 项检查
```

**Slice 10 的脚本为什么不满足于 `FakeAdbClient`**：pytest 里的假 adb 是个 Python 对象，
**根本没有子进程**。于是"参数到底是列表还是字符串"、"`[ NN%]` 被 `read()` 边界切开时还
解析得对不对"、"stderr 管道写满导致子进程永久阻塞（表现为 adb push 卡死）"、
"取消时子进程真的被杀掉、半个 `.part` 留下又被删掉"——这四件事全在测试的盲区里。
所以这个脚本造一个**真的能执行**的 adb 替身（Windows 下是 `adb.cmd` → Python，
POSIX 下是带 shebang 的 `adb`），把设备端文件系统映射成宿主目录，让**生产的**
`AdbClient` 用真实子进程去跑它。于是传过去的字节真的落在磁盘上，脚本可以直接
比对 SHA256；`mkdir` / `mv` / `rm` / `stat` / `sha256sum` 真的改宿主目录；
`calls.jsonl` 记下每一次真实调用的 argv，顺序与 `-s` 可以被直接断言。

五个场次：A 探测与发现 / B 完整传输（逐字节比对 + 顺序 + 进度曲线）/ C 取消·覆盖·
空间不足·掉线·危险路径 / D 静态守卫（AST 查 `shell=True`、命令字符串、UI 不碰
`subprocess`）/ E 真窗口从 [刷新设备] 点到 [传输到设备]。

**为什么不能只跑 pytest**：pytest 跑在 Qt `offscreen` 平台上——那里没有真实字体、没有
DPI 缩放、没有系统深浅色主题，`QMessageBox` 也不是真的窗口，而这三样东西恰恰能藏住真实
缺陷。上面那两个缺陷（关窗抛 `RuntimeError`、弹窗按钮是英文）**全都是这个脚本抓出来的**，
当时 883 条 pytest 全绿。

脚本真的起 windows 平台插件、真的显示窗口、真的点按钮，全程用 `httpx.MockTransport`
假服务端（**不触网、不消耗天地图配额**），任务记录与界面偏好都写到临时目录。五个场次：

| 场次 | 验的是什么 | 产出 |
|---|---|---|
| A | 空闲 → 计算 → 预估结果六行（含 Zoom 区间与地图类型） | `docs/images/slice7_scene_a_estimate.png` |
| B | 预计 > 10,000 张 → 大任务确认框（只提示、不限制） | `docs/images/slice7_scene_b_large_task.png` |
| C | 目标 `.mbtiles` 已存在 → 三选一；再走一遍「重新下载」的二次确认 | `docs/images/slice7_scene_c_existing.png` / `docs/images/slice7_scene_c2_restart.png` |
| D | 下载中（限速 10 请求/秒，保证"进行中"是可观察的中间态）→ 暂停 / 继续 → 完成小结 | `docs/images/slice7_scene_d1_downloading.png` / `docs/images/slice7_scene_d2_complete.png` |
| E | 部分瓦片失败 → 失败小结 +「继续补齐」+ 任务落盘为 FAILED | `docs/images/slice7_scene_e_failed.png` |

它还会顺手查两件只有实机能查的事：**每个弹窗的按钮文案里有没有残留英文**
（`_english_buttons()`），以及流程跑完**主窗口必须关得掉**。数字对不上就以非 0 退出码结束。

## 目录结构

```text
tianditu-offline-tool/
├── app/
│   ├── main.py            入口（日志初始化 + QApplication + MainWindow）
│   ├── config/settings.py 全局常量，唯一魔数来源
│   ├── models/            纯数据模型（dataclass / enum），不依赖 UI
│   ├── ui/                界面层：主窗口 + 各面板 + 六个 QThread 桥
│   │                      （download_worker 跑编排 / connection_check 发 HTTP /
│   │                       tile_scan_worker 查 SQLite / mbtiles_inspector_dialog
│   │                       里的 MBTilesInspectionWorker 跑诊断 / package_dialog
│   │                       里的 PackageBuildWorker 与 PackageValidateWorker；
│   │                       Slice 10 的三个线程在 app/workers/ 下）
│   │                      外加纯逻辑模块：task_actions（按钮矩阵）/ validation（校验）/
│   │                       preferences（QSettings 偏好）/ widgets（折行标签 + 主题色）/ desktop（打开目录）
│   ├── providers/         瓦片源 Provider：base.py + tianditu.py（Slice 2 ✅）
│   ├── geo/               瓦片坐标计算：bounds.py + tile_math.py（Slice 1 ✅）+ region.py / region_estimator.py / region_filter.py / geojson_loader.py（Slice 16 ✅）
│   ├── planning/          Zoom 策略层（Slice 17 ✅）：region_level（等级 + 父级规则，唯一来源）/ models / zoom_policy（唯一权威策略表）/ zoom_strategy（推荐 + 预估）
│   ├── regions/           行政区域层级（Slice 18 ✅）：models（目录条目）/ catalog（读 index.json + 层级查询）/ loader（路径安全 + GeoJSON + DownloadRegion）/ validator（快速 + 深度）/ service（业务入口）
│   ├── downloader/        下载引擎：models / retry_policy / rate_limiter / tile_downloader / download_manager / resume / composite_tile_downloader（Slice 3 ✅ + Slice 5 ✅ + Slice 6 ✅）
│   ├── mbtiles/           MBTiles 读写：models / writer / reader / validator（Slice 4 ✅；Slice 6 增加合成信息读写与自洽性校验）+ diagnostics（Slice 8 ✅：全部判定规则与结论模型）
│   ├── tasks/             任务持久化：task_store（原子 JSON 读写）/ task_service（状态机 + 启动恢复）（Slice 5 ✅）
│   ├── composer/          瓦片合成：tile_composer.py（Pillow alpha 合成，无状态可复用）（Slice 6 ✅）
│   ├── services/          编排层：map_download_service.py（下载 → 落盘 → 校验 → 续传 一条链，Slice 4 ✅ + Slice 5 ✅ + Slice 6 ✅；Slice 7 增加 sanitize_map_name）+ mbtiles_inspection_service.py（Slice 8 ✅：检查编排 / 进度 / 报告落盘）+ map_package_service.py（Slice 9 ✅：打包编排 / 输出目录命名 / 校验入口）+ android_transfer_service.py（Slice 10 ✅：设备 + 包校验 + 传输的入口；缓存"刚校验过"以免重复哈希）
│   ├── package/           离线包打包与校验：models / checksum / package_builder / package_reader / package_validator（Slice 9 ✅）
│   ├── android/           ADB 设备管理与包传输（Slice 10 ✅）：adb_client（子进程封装）/ device_manager（设备发现 + getprop 补全）/ transfer_models（计划 / 进度 / 结果 / 远端路径安全）/ map_package_transfer（顺序 + 失败分支）
│   └── workers/           后台线程桥（Slice 10 ✅）：android_transfer_worker（设备刷新 / 包校验 / 传输三个 QThread）
├── tests/                  pytest 用例（1711 条；android_fakes.py 是 Slice 10 的假 adb 设备，不是测试文件）
├── data/tasks.json         任务记录（运行时生成，已 gitignore）
├── resources/regions/      行政区目录元数据（Slice 18 ✅）：index.json + README.md 随仓库分发；
│                           country/ province/ city/ 三个数据目录**只放 .gitkeep**，
│                           *.geojson 在 .gitignore 里（授权数据不进提交）
├── docs/                   设计文档 + 界面留档截图（docs/images/slice7_*.png / docs/images/slice8_inspector_*.png / docs/images/slice9_package_*.png / docs/images/slice10_*.png /
│                           docs/images/slice16_polygon_*.png / docs/images/slice17_zoom_*.png / docs/images/slice18_regions_*.png）
├── scripts/capture_ui.py   界面截图留档脚本
├── scripts/verify_slice19_region_ui.py Slice 19 级联 + 推荐 Zoom 验收（真窗口 + 真关窗再开 + 90 项检查 + 截图）
├── scripts/verify_slice18_regions.py Slice 18 行政区域层级验收（真窗口 + 三级目录 + 路径安全 + 79 项检查 + 截图）
├── scripts/verify_slice17_zoom.py Slice 17 分级 Zoom 策略验收（真窗口 + 真下载 + 51 项检查 + 截图）
├── scripts/verify_slice16_polygon.py Slice 16 多边形区域下载验收（真窗口 + 真下载 + 70 项检查 + 截图）
├── scripts/verify_slice10_android.py Slice 10 实机验收（真 adb 替身 + 真窗口 + 118 项检查 + 截图）
├── scripts/verify_slice9_package.py Slice 9 离线地图包实机验收（真窗口 + 75 项检查 + 截图）
├── scripts/verify_slice8_inspector.py Slice 8 检查工具实机验收（真窗口 + 52 项检查 + 截图）
├── scripts/verify_slice7_ui.py Slice 7 实机验收脚本（真窗口 + 49 项检查 + 截图）
├── scripts/verify_resume.py 断点续传端到端验证脚本（假服务端，不触网、不耗配额）
├── pytest.ini              pytest 配置（asyncio_mode = auto）
└── requirements.txt
```

依赖方向单向：`ui -> services -> downloader/mbtiles/tasks -> providers/geo -> models -> config`；
业务包之间通过 `models` / `geo` 传递数据，不在 UI 层写业务逻辑。
UI 需要瓦片数据时必须调用 `app.geo`，需要落盘时必须调用 `app.services`，
**SQLite 只出现在 `app/mbtiles` 里**（`DownloadManager` 与 `app.tasks` 都不依赖数据库）。
Slice 10 的 `app/android` 是同一套思路的延续：**设备面板不 import `AdbClient`**
（只 import `TransferPlan` / `TransferProgress` / `DeviceRefreshOutcome` 三个数据类），
**`app/ui` 里没有任何 `subprocess`**（例外是 `desktop.py`：它用 `open` / `xdg-open`
打开"输出目录"，与 ADB 无关）。这两条都有静态守卫。
`app.downloader.resume` 通过**结构化 Protocol**（`TileProbe`）判断"库里有没有这张瓦片"，
所以它能接收 `MBTilesWriter` / `MBTilesReader` 而**不必 import `app.mbtiles`**——依赖保持无环，
这条约束有专门的测试守着。
Slice 17 / 18 的依赖边同理，且方向是**单向**的：
`app.regions → app.geo` / `app.planning`，而 `app.geo.region` **不 import `app.regions`**
（否则就是 `geo → regions → geo` 的环）。等级与父级规则只在 `app.planning` 里定义
（`app.regions` 转出的是**同一个对象**），路径安全只在 `app.geo.region` 里实现
（同样转出同一个函数）—— 两条都有静态守卫。
`app/planning/zoom_strategy.py` 里的 `RegionTileEstimator` 是**函数内 import**：
挪到模块顶层，就会在"从 `app.geo` 那一侧发起导入"时撞上半初始化的 `app.geo.region`。

`app.ui` 里这几个模块刻意**不 import Qt 之外的任何东西**，因此可以脱离窗口单测：
`task_actions`（连 Qt 都不 import）、`validation`、`preferences`（只依赖 `QSettings`）。
界面层不直接做 IO——三件重活各有一个 `QThread`，`MainWindow` 只做装配与信号连接。

## 用法：测试天地图连接

1. 在「Token」填入天地图控制台申请的服务密钥（`tk`，输入框默认隐藏，可勾选「显示」临时查看）。
2. 选择地图类型（矢量地图 / 卫星影像）、填好范围与 Min Zoom。
3. 点 `[测试天地图连接]`：程序取范围中心点，换算成瓦片编号，只请求 **一张** 瓦片做验证。

成功时「天地图连接」区显示图层、Zoom、HTTP 状态、响应体积与耗时；失败时给出明确原因
（`HTTP 403 — Token 可能无效…` / `请求超时` / `响应不是有效的图片瓦片…`），
并同步写入运行日志。日志中的 URL 一律脱敏（`tk=abcd****xyz`），不会泄露完整 Token。

## 用法：下载并生成 MBTiles

界面布局：左边是「地图配置」+「下载状态」（当前任务），右边是「Android 设备」+
「下载任务」列表 +「运行日志」。

1. 填好 Token、地图类型、范围与 Zoom 区间（Min Zoom ~ Max Zoom）。
2. 填「输出目录」（可点 **[浏览]** 选）与「地图名称」——最终产物是
   `<输出目录>/<地图名称>.mbtiles`。名称会被清理成合法文件名：Windows 非法字符
   `\ / : * ? " < > |` 与控制字符替换为 `_`，结尾的点和空格去掉（文件系统会静默丢弃它们），
   命中 `CON` / `LPT1` 这类保留名时加后缀。**名称留空不会拦住你**——程序会用中心点坐标
   自动生成一个（`map_39.05N_116.05E`），并在日志里说明。
3. 点 `[开始下载]`。程序先跑一次**统一的校验**（Token / 经纬度顺序与范围 / Zoom 顺序与范围 /
   输出目录 / 地图名称），**一次报出全部问题**而不是改一个拦一次；通过后再算总瓦片数、
   预计容量、预计 WMTS 请求数与输出文件路径，这些都显示在「预估结果」区。
   - 若预计超过 **10,000** 张，会弹一次**确认框**（只提示、不限制规模）；
   - 若目标 `.mbtiles` **已经存在**，会先弹「继续下载 / 重新下载 / 取消」三选一，
     并且**先把文件里已有多少张瓦片数出来**——这次计数走后台线程，所以你会先看到
     「正在扫描已有瓦片…」而不是界面僵住（SQLite 查询不允许跑在主线程上）；
   - 确认后在后台线程里跑完整链路，进度区实时刷新十一个字段
     （任务名称 / 状态 / 当前 Zoom / 总瓦片 / 已完成 / 剩余 / 已有（跳过）/ 本次成功 /
     失败 / 下载速度 / 已下载），下面是一条总进度条。
4. 四个动作按钮**按状态启用**，不是永远可点：

   | 状态 | 可点 |
   |---|---|
   | 空闲 | 计算 · 开始下载 · 测试天地图连接 |
   | 扫描已有瓦片 / 等待确认 | 取消 |
   | 下载中 | 暂停 · 取消 |
   | 正在暂停 | 取消 |
   | 已暂停 | 继续 · 取消 |
   | 已完成 | 开始下载 |
   | 失败 / 已取消 | 开始下载 · 继续 |

   `[暂停]` 会先进「正在暂停…」——等引擎回一帧进度（证明它在途请求已落地）才变成
   「已暂停」。`[取消]` 要先确认一次，确认后已下载的瓦片**保留**。
5. 关窗时若任务还在跑，会弹非模态确认框；确认退出会把任务标成**已暂停**（不是取消），
   下次打开程序可以接着下。
6. 结束后「下载结果」区显示：

   ```text
   文件：D:\maps\beijing.mbtiles
   瓦片：12,345
   大小：352.40 MB
   Zoom：8~16
   验证：PASS
   ```

   并逐行写入运行日志；若有失败，还会给出 `失败原因：<原因>（N 张）`（最多 3 条，按张数倒序）。
7. **全部成功时会自动弹一个小结框**（`MBTilesValidator` 已经在后台跑完，这里只是把结论
   摆出来），框里带一个 **[打开目录]** 按钮，直接用资源管理器打开产物所在目录。
8. **失败时 GUI 不会崩**：弹出失败小结，说明原因，并给出 **[继续补齐]** 按钮——点一下就
   只下缺的那些瓦片；已经写进文件的数据原样保留。

取消任务会**保留**已经写入的瓦片，磁盘上留下一个可用的（不完整的）`.mbtiles`，不会自动删除。

下一次打开程序时，程序会**记住**你上次用的输出目录、地图类型、缩放区间与窗口位置
（存在 `QSettings` 里；**Token 永远不存**）。如果上次的输出目录已经被删掉，程序会退回
默认目录并在日志里说明——而不是让你在第一次点「开始下载」时撞上"输出目录不存在"。

## 用法：断点续传（接着上次下）

1. 第一次下载跑了一半（点了 `[暂停]`、或者直接关掉程序），磁盘上已经有一个不完整的
   `beijing.mbtiles`，`data/tasks.json` 里也留下了任务记录。
2. 重新打开程序：启动时会自动扫一遍任务记录，把上次遗留的「下载中」改成「已暂停」，
   并在日志里提示恢复了几个任务。**程序不会自动开始下载**（要不要花配额由你决定）。
3. 在右侧「下载任务」列表里找到那条任务：
   - **双击**该行 → 表单回填成这条任务的参数（需要**重新填 Token**——任务记录里不保存 Token）
   - 或者直接点该行的 **[继续下载]**
4. 点下去之后程序会：

   ```text
   读取任务记录 → 检查 .mbtiles 是否存在（不存在就当全新下载）
                → 后台线程扫描：打开已有 MBTiles 数已有瓦片
                     （界面显示「正在扫描已有瓦片…」，不僵住）
                → 一致性校验（边界 / 缩放 / 图层）  ← 不一致就拒绝，一张都不写
                → 计算完整瓦片区间
                → 检测已有瓦片（默认只读坐标三列，不读 BLOB）
                → 已有 → 跳过；缺失 → 下载
                → 写回同一个 .mbtiles（INSERT OR REPLACE，不会产生重复行）
   ```

   日志里会给出「已有：X / Y / 继续下载：Z」这类计数，不会逐张瓦片刷屏。

5. 如果目标 `.mbtiles` **已经存在**（比如你想重下一遍），点 `[开始下载]` 时会弹三选一：
   继续下载 / 重新下载 / 取消。「重新下载」会先把旧文件**改名备份**
   （`beijing.mbtiles.bak-20260915-102800`），**不会静默删除你的数据**。

任务状态一共五种：`等待中(PENDING)` / `下载中(DOWNLOADING)` / `已暂停(PAUSED)` /
`已完成(COMPLETED)` / `失败(FAILED)`（另有 `已取消(CANCELLED)`）。
只有「跑完 + 零失败 + 文件校验通过 + 库里瓦片数达标」才算 `已完成`；
差几张（服务端 404、重试耗尽）会是 `失败`，之后可以再点「继续下载」把缺口补上。

想手工验证整条链路（**用假服务端，不触网、不消耗真实配额**）：

```bash
python scripts/verify_resume.py
```

脚本跑三轮——「下到一半取消 → 重新打开只下缺的 → 全部已有再点一次」，把每轮的
请求数 / 库里张数 / 任务状态都打出来；数字对不上就以非 0 退出码结束。

## 用法：检查 MBTiles（诊断一个已有地图包）

三种打开方式，都是同一个检查窗口（非模态，检查期间主界面照常可用）：

- 菜单 **文件 → 检查 MBTiles…**
- 主界面右侧「下载任务」列里选中一条 → 点 **[检查]**（自动带上该任务的产物路径）
- 在窗口里点 **[选择文件…]** 自己挑任意 `.mbtiles`

窗口里两个按钮的区别就是"花多少时间"：

- **[快速检查]** —— 结构 + metadata + Zoom 统计 + 坐标 + 重复/空瓦片 + 覆盖完整性
  + **分层抽样**图片（默认 50 张）。秒级到十几秒，日常用这个。
- **[深度检查]** —— 在快速检查之外，**逐张**校验全部 `tile_data`（流式，不占内存），
  同时把 SQLite 自检升级成 `PRAGMA integrity_check`。几十万张的库是分钟级操作。
  窗口关掉可以随时放弃，结论会标明"没扫完"。

结果区三档：

| 结论 | 含义 | 判定 |
| --- | --- | --- |
| `PASS` | 没有发现问题 | 无 ERROR 且无 WARNING（普通提示不影响） |
| `WARNING` | 文件可用，但有可疑之处 | 例如：缺 `description`、覆盖缺几张、声明 zoom 与实际不一致、`format=webp` 本工具读不动 |
| `FAIL` | 文件有问题，不该直接用 | 例如：不是 SQLite、缺表/缺字段、坐标越界、空瓦片、图片无法解码、bounds 越界 |

**[导出报告]** 会在被检查文件**旁边**写一个 `xxx.mbtiles.report.txt`（UTF-8），
内容包含 metadata、逐 zoom 的期望/实际/缺失、缺失瓦片明细（XYZ，最多 100 条）、
抽样与深度扫描统计、错误/告警/提示三档清单、以及最终结论 —— 直接贴进 issue 就能用。

另外，**每次下载跑完都会自动跑一次"快速"档诊断**（`deep_scan=False`、抽样 20 张），
完成弹窗里因此会多出"缺失瓦片 / 抽样损坏"两项。

## 用法：生成离线地图包

一个 `.mbtiles` 是地图数据；一个**离线包**是可以交付、可以验证的目录：

```text
北京离线地图/
├── map.mbtiles      源文件的一份副本
└── manifest.json    这份数据是什么、多大、有没有被改过
```

三种入口，都是同一个对话框（非模态，打包期间主界面照常可用）：

- 菜单 **文件 → 生成地图包…**
- 主界面右侧「下载任务」列里选中一条任务 → 点 **[生成地图包]**（该任务的产物文件
  还不存在时这个按钮是灰的——包不该从半成品里出）
- 在窗口里点 **[选择文件…]** 自己挑任意 `.mbtiles`

进去之后：

1. 选好源 `.mbtiles`，程序**自动**跑一次快速检查（后台线程，几秒），
   「地图信息」区显示**地图类型 / Zoom / 瓦片数**。
   - 检查结果是 `FAIL` → **[生成地图包] 直接禁用**，状态栏说明原因；
   - 检查结果是 `WARNING` → 先问一句"缺了几张瓦片，还打吗"，你点了才继续。
2. 填「地图名称」（就是包名，也是目录名）与「输出目录」——默认帮你填上次用的目录。
3. 点 **[生成地图包]**。过程分六档，进度条按档位推进：
   检查源文件 → 复制 `map.mbtiles` → 计算 SHA256 → 写 `manifest.json` → 自校验 → 发布。
4. 完成后「地图包内容」区给出：

   ```text
   地图包目录：D:\maps\packages\北京离线地图
   MBTiles：  28.00 KB
   瓦片：      56
   Zoom：      12
   SHA256：    1054d10055ab22c90bf469348150d5daa4ccc25b523da95cb1d850ae6ec1bcf5
   结果：      PASS
   ```

   下面还有 **[打开目录]** 与 **[验证地图包]**。
5. 如果目标目录**已经存在**，会先问一次；确认后是**改名备份**旧包，不是删除。

`manifest.json` 的实际样子（字段顺序固定、UTF-8、`indent=2`、末尾带换行）：

```json
{
  "formatVersion": 1,
  "id": "pkg_20260916",
  "name": "北京离线地图",
  "provider": "tianditu",
  "mapType": "vector",
  "labels": true,
  "composition": "merged",
  "tileScheme": "web_mercator",
  "tileFormat": "png",
  "minZoom": 12,
  "maxZoom": 12,
  "bounds": {
    "west": 116.0,
    "south": 39.0,
    "east": 116.5,
    "north": 39.5
  },
  "tileCount": 56,
  "mbtiles": {
    "file": "map.mbtiles",
    "size": 28672,
    "sha256": "1054d10055ab22c90bf469348150d5daa4ccc25b523da95cb1d850ae6ec1bcf5"
  },
  "createdAt": "2026-09-16T11:09:14+08:00"
}
```

`id` 是 `<名字的 ASCII 化>_<创建日期>`（`beijing_20260916`）。名字是纯中文时回落成
`pkg_20260916`——**不猜拼音**：把中文转拼音要揣一本词典，而它猜错的时候没人会发现。
`id` 是**确定性**的：同一份输入永远得到同一个 id。

`mbtiles.file` **永远是相对文件名**。包整个拷进 U 盘、换个盘符、发到别人机器上，
Android 那侧照样能按它找到数据——所以 manifest 里**一行绝对路径都不许有**。

### 验证一个已有的包

菜单 **文件 → 验证地图包…**，或者从打包完成视图点 **[验证地图包]**。四道关逐条报：

```text
Manifest：PASS
MBTiles： PASS
SHA256：  PASS
Result：  PASS
```

任何一项不过就是 `FAIL`，失败原因写成人话，例如：

```text
MBTiles SHA256 mismatch：manifest 记 1054d100…bcf5，实际 b217e710…372a
```

**想自己验一遍篡改检测**：用十六进制工具把包里的 `map.mbtiles` 改**一个字节**
（长度别动），再点一次验证 —— `Manifest` 和 `MBTiles` 仍然 `PASS`（文件还在、大小没变），
但 `SHA256` 必须变成 `FAIL`。这正是"大小检查挡不住、SHA256 才挡得住"的那类损坏：
磁盘静默损坏、传输截断、被人手改过，都长这样。

## 用法：传输到 Android 设备

入口是主窗口右上「Android设备」区的 **[刷新设备]** / **[选择地图包]** / **[传输到设备]** 三个按钮
（**没有菜单项**：这一步有前置依赖——得先插上设备、先有一个包，放进菜单只会变成
"点了以后报一句错误"）。流程七步，前两步是环境，后面五步才是传输：

```text
1. ADB        adb 路径 / 版本 / 可用性      ← 未检测到时告诉你去装 Platform Tools
2. 刷新设备    list[设备]（含未授权 / 离线）
3. 选设备     只能选 state=device 的那台
4. 选地图包   选的是**包目录**（不是裸 .mbtiles），选中后立刻后台校验一次
5. 传         确认框（设备 / 地图 / 大小 / 远端落点）→ 开始 → 进度
6. 结果       文件大小 / SHA256 / 状态三行分开写
7. 远端落点   <导入区>/<manifest.id>/
```

### 配 ADB 路径

优先级：**界面偏好 > 环境变量 `ADB_PATH` > `PATH` 里的 `adb`**。
没装 Android SDK 也能用——只要 `adb` 在 `PATH` 里，或者设一个 `ADB_PATH`：

```bash
ADB_PATH=D:/platform-tools/adb.exe python -m app.main
```

设备列表里 `unauthorized` / `offline` 的**照常显示**：拔插了一半的线、或者还没在手机上
点"允许 USB 调试"，看到"没有设备"只会更困惑。如实显示，然后在 [传输到设备] 上拦住
（按钮是灰的，鼠标悬停有原因）。

### 远端落点

```text
<导入区>/<manifest.id>/
```

`manifest.id` 是**文件里的内容**，会被拼进设备端命令，因此过白名单
（`A-Z a-z 0-9 _ - .`，其余换 `_`，`..` 单独挡掉）。默认导入区是
`/sdcard/Download/TiandituOfflineMap/import`——**公共目录**，不需要 root，
也不是 `/data/data`。

### 常见情形

| 现象 | 程序怎么做 |
|---|---|
| 设备上已有同名包 | **先停下来问**，绝不静默覆盖；点了「覆盖」才删旧包重传 |
| 传输中想停止 | [取消传输] 杀掉 `adb push`，删掉自己的 `.part`，**不动**设备上已有的完整包 |
| 传输中拔线 | 报"设备断开"，并且**故意保留** `.part`——那是排查"线是不是松了"的实物证据 |
| 设备没有 `sha256sum` | 大小对得上就算成功，只给一条 WARNING（Android 版本差异，不是错误） |
| 空间不够 | 拦下，并写出"需要多少 / 可用多少" |
| `df` 输出解析不了 | 给一条 WARNING 然后**继续**——为解析失败拦住用户是本末倒置 |

## 地图包格式

一个地图包就是**一个目录**：

```
<package-id>/
├── manifest.json      # 契约文件，两端唯一的接口定义
└── map.mbtiles        # SQLite 瓦片库
```

`manifest.json` 有 16 个字段（`description` 是唯一的可选字段），
`formatVersion` 恒为 `1`。**完整定义见
[`docs/map_package_v1.md`](docs/map_package_v1.md)**，那里写了每个字段的类型、
取值范围、`bounds` 语义、XYZ/TMS 的关系、以及版本兼容策略。

生成之后**务必核对**这两项（它们是"传到手机能不能装"的唯一凭据）：

```bash
# 文件实际大小
(Get-Item map.mbtiles).Length
# 文件实际哈希
(Get-FileHash map.mbtiles -Algorithm SHA256).Hash.ToLower()
```

两个值都必须与 `manifest.json` 里的 `mbtiles.size` / `mbtiles.sha256` 一致。

> **为什么这么强调**：SHA 对不上，手机上必然导入失败。而那时你会怀疑是传输坏了，
> 于是重复传输好几遍 —— 真正的问题在打包环节就已经存在了。

---

## Android 端：导入与使用

Android 侧在**另一个仓库**：`D:\project\osmdroid`（fork 的 osmdroid，
业务在 `OpenStreetMapViewer` 模块，applicationId `org.osmdroid.map`）。

**导入流程**：

1. 把包目录传到设备（`adb push`，或拷到 SD 卡 / 网盘，都可以）；
2. 打开 App →「离线地图」→「导入」→ 选中那个目录；
3. App 会依次校验：**大小 → SHA256 → SQLite 结构 → zoom 与 manifest 是否一致**；
4. 全部通过后才搬到正式目录（先写 `.importing`，校验通过才改名，所以
   **中断不会留下半个能用的包**）；
5. 列表里出现新的一行，切到 `OFFLINE` 或设为默认地图即可使用。

**导入会拒绝的情况**（界面给中文说明，错误码只在日志里）：

| 情况 | 原因 |
| --- | --- |
| `formatVersion > 1` | 包版本高于 App 支持的版本 → 升级 App |
| 大小不符 | 传输截断了 |
| SHA256 不符 | 文件内容不对 |
| SQLite 结构不对 / zoom 冲突 | 包本身有问题 |
| 空间不足 | 需要包大小的 **2 倍以上**（导入过程两份并存） |

**列表里标"已损坏"是什么意思**：管理页在装配列表时会**真的打开一次**
`map.mbtiles` 读一张瓦片。能导入但读不出瓦片，就会被标出来。

> 这是刻意不做"装作看不见"：放过它，用户会一路点到"使用"，
> 然后得到一张空白地图，而且不知道该怪谁。

---

## 三种底图模式：ONLINE / OFFLINE / AUTO

| 模式 | 行为 | 什么时候用 |
| --- | --- | --- |
| **ONLINE** | 只用在线瓦片（要联网） | 日常、看实时路况 |
| **OFFLINE** | 只用指定的那一张本地包 | 明确知道要用哪张包、且要稳定 |
| **AUTO** | 按当前位置**自动挑**一张覆盖它的包；挑不到就退回在线 | 出门在外、不想手动切 |

**AUTO 的选包规则**（多张包都覆盖时按优先级）：

1. 覆盖当前点；
2. **范围框更小的优先**（更精确的那张）；
3. `maxZoom` 更高的优先；
4. `createdAt` 更新的优先。

**"AUTO 没选中离线地图"通常是正确行为** —— 说明当前位置不在任何包的范围内。
这时它会**安全退回在线底图**，而不是保持上一张、也不是白屏。

**断网 + AUTO**：有覆盖包 → 用本地包（"飞行模式 + 提前下好地图"的主场景）；
无覆盖包 → 保持在线（但显示不出瓦片，因为确实断网了）。

---

## 测试：全量与端到端脚本

```bash
# 全部单元测试
cd D:/project/MapDownloader
.venv/Scripts/python.exe -m pytest

# 端到端冒烟（离线，不访问天地图，几秒）
.venv/Scripts/python.exe scripts/e2e_smoke_test.py

# 分级全链路（离线）：层级 → Zoom → 估算 → 任务区 → 风险 → 计划 → 调度 → MBTiles → Inspector → 包
.venv/Scripts/python.exe scripts/hierarchical_map_e2e.py

# 冒烟 + 保留产物便于人工翻看
.venv/Scripts/python.exe scripts/e2e_smoke_test.py --work-dir ./smoke_keep
```

**冒烟测试的 6 项**（失败时非 0 退出，可直接进 CI）：

```text
[PASS] Tile Math   —— 范围 → 瓦片区间，并与 estimate_tile_count 交叉核对
[PASS] MBTiles     —— 写入后逐张读回比对（抓 TMS 翻错方向这类问题）
[PASS] Inspector   —— 结构检查必须 PASS
[PASS] Package     —— 生成包 + manifest 字段与 MBTiles 对得上
[PASS] SHA256      —— manifest 声明的哈希 == 文件真实哈希
[PASS] Validator   —— 从磁盘重新读一遍独立再验一次
```

**协议兼容测试**（跨仓库）：

```bash
.venv/Scripts/python.exe -m pytest tests/test_package_protocol_v1.py -q
```

它会**直接读 osmdroid 仓库的 Java 源码**，抽出 Android 侧所有
`requireXxx(json, "字段名")` 的键名，与 Windows 的 `MANIFEST_FIELD_ORDER`
做**双向差集**断言。任何一端单方面改字段，这个测试立刻变红。

Slice 25 在这条思路上又加了一层：**`region.level` 的取值集合两端一致**。
它同样读 Java 源码（`RegionLevel.java` 的枚举常量），断言 Windows 的值域
不超出 Android 的、且 Android 只多一个 `UNKNOWN`；还断言两端对"哪一级更具体"
的排序一致（Windows 的下载顺序 `PLAN_LEVEL_ORDER` 与 Android 的
`getSpecificity()` 在行政四级 + 任务区这段互为逆序）。

**跳过条件只有一个**：兄弟仓库**根本不存在**（`osmdroid` 目录不在）。
仓库在、但源码文件或字段找不到时是 **fail 而不是 skip**，报错里会列出搜索过的
路径 —— 早先那版是"读不到就 skip"，等于没有守卫：字段改名之后测试一直"绿"，
直到真机导入才炸。

---

## 文档自检

```bash
.venv/Scripts/python.exe scripts/check_docs.py
```

查 `docs/` 的结构：根目录有没有散落的截图、图片引用有没有断、文档间链接有没有断、
有没有孤儿文档。退出码非 0 就是没整理好。规则见
[docs/README.md](docs/README.md) 末尾的「文档规范」。

---

## 端到端验收

真机验收的完整流程（25 步，A1–A7 / B8–B11 / C12–C20 / **D21–D25**）见
[`docs/e2e_acceptance_checklist.md`](docs/e2e_acceptance_checklist.md)。

它覆盖了单测覆盖不到的部分：**真窗口、真设备、真的拔网**。

Windows 侧的自动化验收（都是**离线**的，不访问天地图）：

| 脚本 | 覆盖 |
| --- | --- |
| `scripts/e2e_smoke_test.py` | 主链路 6 段：Tile Math → MBTiles → Inspector → Package → SHA256 → Validator |
| `scripts/hierarchical_map_e2e.py` | **分级全链路 10 段**：层级 → Zoom → 估算 → 任务区 → 风险 → 计划 → 调度 → MBTiles → Inspector → 包 |
| `scripts/verify_slice7_ui.py` … `verify_slice23_risk.py` | 各 Slice 的**真窗口 / 真按钮**验收（13 个脚本） |
| `scripts/verify_resume.py` | 断点续传（两轮下载，第二轮零请求） |

Android 侧有配套的自动化脚本（在 osmdroid 仓库）：

| 脚本 | 覆盖 |
| --- | --- |
| `scripts/verify_slice11_offline_import.py` | 导入、目录结构、校验 |
| `scripts/verify_slice12_offline_runtime.py` | OFFLINE / AUTO / 拔网 |
| `scripts/verify_slice13_offline_management.py` | 删除、多地图 |
| `scripts/verify_slice14_offline_stability.py` | 长时间切换、压力 |
| `scripts/verify_slice24_auto_selection.py` | 真机 AUTO 阶梯（位置 + Zoom + 区域等级）与迟滞 |

---

## 常见问题

**完整版见 [`docs/troubleshooting.md`](docs/troubleshooting.md)**（18 类问题，含分级体系 4 条）。
这里是最常遇到的几条：

| 症状 | 先查这个 |
| --- | --- |
| 下载一开始就全失败 | Token（用界面上的「连接检查」，别去日志里找 —— 日志刻意不打 Token） |
| 工具里没有设备 | ADB 路径（偏好设置填绝对路径）。**这不影响你做地图** |
| 手机上导入报"校验失败" | 回包目录核对 `mbtiles.size` 与 `sha256` |
| 切离线后地图空白 | 当前地图页的缩放层级 / 视野位置，是否在包的 zoom 区间与 bounds 内 |
| 行政区下拉里没有自己加的省 | `resources/regions/index.json` 里补了记录没有；路径写成绝对路径或带 `..` 的条目会被**整条丢弃**并在日志里留一条 `unsafe_path` |
| 手机上行政区包显示"未知等级" | 包里 `manifest.region.level` 在不在（老包不补，重新打一次） |
| 续传说"找不到对应的行政区边界" | 那块地的 GeoJSON 还在不在 `resources/regions/` |
| 计划里后面的项一直"待下载" | 调度是**严格串行**的，这是设计如此 |

**几条"看起来像 bug 其实是设计如此"**：

- 环境检查里 **ADB 标 `[!]` 而不是 `[✗]`** —— ADB 只影响传输，不影响做地图。
- `manifest.json` 里**没有 `description`** —— 它是唯一可选字段，为空时不写这个键。
- AUTO 模式下从北京飞到上海底图变了 —— 正确行为。
- 删包时弹确认框 —— 删的是用户文件，必须二次确认。
- **行政区目录里"缺边界文件"只是提醒，不是错误** —— 仓库本来就不带边界数据，
  "还没放文件"是正常中间态；把它算错误，`validate()` 在任何一台干净机器上都会红。
- **`index.json` 的版本号不认识也不会让目录不可用** —— 那是前向兼容信号，
  已有字段照读，问题只在校验报告里出现。
- **推荐区间是 Z5~10，界面却允许你填 Z18** —— 推荐只提示、不限制，也**绝不改写**
  用户填的值。
- **计划里各项逐项串行、后面全写着"待下载"** —— 严格串行是设计（日志与进度不交错，
  失败归属明确），不是卡住。
- **只选中一个省、界面只显示一个范围** —— Slice 18 提供的是**数据源**，
  组合下载计划（全国 + 重点省）是 Slice 19 的事。

---

## 后续计划

- **分层下载计划** —— 拿 Slice 18 的目录做"全国 Z5~10 + 重点省 Z9~13"这种多区域
  组合计划：拆成若干子任务、合并估算总容量、给出磁盘占用与依赖顺序。
  Slice 19 只做了**选一个区域 + 定 Zoom**，一行下载编排都没写。
- **区域管理界面** —— 区域增删改查、校验报告进界面、导入授权数据。
  Slice 19 只消费了目录的**读取侧**。
- **Slice 11：Android 端导入** —— 端侧读 `<导入区>/<id>/manifest.json`、校验 SHA256、
  把 `map.mbtiles` 交给 osmdroid 加载。Slice 10 只负责把文件**搬到设备上并证明搬对了**，
  端侧怎么导入是下一 Slice 的事（`manifest.json` 最后传就是给它当"包齐了"的信号用的）
- 失败瓦片清单与单点重试、速度曲线
- 传输历史的可视化留档

> 以上**尚未排期**，不再用 Slice 号承诺时间。已经交付的是：Slice 8 = MBTiles 检查与
> 诊断工具，Slice 9 = 离线地图包构建与校验（manifest + 流式 SHA256 + 篡改检测），
> Slice 10 = ADB 设备管理与离线地图包传输（Windows → Android），
> Slice 16 = GeoJSON 区域下载与中国行政区域预设，Slice 17 = 分级 Zoom 策略，
> Slice 18 = 行政区域层级模型（中国 → 省 → 市），
> Slice 19 = 行政区域级联选择 + 推荐 Zoom。
> 每个 Slice 的 Spec 都会明确禁止"顺手做下一个"——Slice 10 的 Spec 就点名不许在这一
> 个里实现 Android 端导入、不许碰 App 私有目录、不许 `adb install` / `root` / `remount`、
> 不许压缩、不许改 MBTiles；Slice 19 的 Spec 同样点名不许实现多区域下载计划（Mission）、
> 不许动 Android 侧与下载器内核，也不许提前做区域管理界面。所以那些是**下一个**
> Slice 的事，不是这一个的。

## 说明与注意事项

- 天地图服务需要自行申请 Token，请遵守天地图服务条款与调用配额限制。
  **不要把真实 Token 写进代码、README 或测试**；本仓库中的 Token 一律是占位假值。
- **XYZ 与 TMS 的分工** —— MBTiles 内部采用 **TMS** 的 `tile_row`（原点在左下角），
  而本工具的 API / URL / 日志 / 校验报错**统一使用 XYZ**（原点在左上角）。
  翻转只发生在 `MBTilesWriter` / `MBTilesReader` 内部的边界上，且复用 Slice 1 的
  `xyz_y_to_tms_y()`，没有第二套公式。一致性由测试锁住：`z=3, x=4, y_xyz=2`
  必须写成 `tile_row = 2³ − 1 − 2 = 5`，并且能用 `read_tile(3, 4, 2)` 读回来。
  Slice 8 的诊断还多了一条推论：**坐标越界的行根本没有对应的 XYZ**，所以
  `tms_y_to_xyz_y()` 对它会按契约抛 `ValueError`。"列出越界样本"本身就是在处理
  越界行，因此那里必须容错（`_xyz_hint`）—— 否则一个专门用来报错的检查，会在
  格式化自己的结论时崩掉，用户只看到一句"坐标检查失败"，永远不知道是哪张瓦片坏了。
- **天地图 WMTS 的 `_w` 是"左上原点"** —— 请求 URL 里的 `TILEROW` 就是 **XYZ 的 `y`**，
  发请求时**不要**翻成 TMS；翻只翻存储那一次。写续传相关测试时这一点最容易搞错
  （把请求坐标也当成 TMS 去比对，会得到"跳过的瓦片又被请求了"的假故障）。
- **续传默认不读瓦片内容** —— 判断"库里有没有这张瓦片"只需要 `zoom_level / tile_column /
  tile_row` 三列。`tile_data` 是 BLOB，几万张就是几百 MB 起步，**默认路径一次都不读**；
  只有显式 `verify_content=True` 时才会按需读。这条有测试用 spy 守着（`read_tile` 调用次数必须为 0）。
- **Token 不进任务文件** —— `data/tasks.json` 只存任务参数与进度，**永远不存 Token**
  （落盘前过字段白名单 + 敏感词黑名单）；恢复任务时由界面重新输入。
  日志同理，`httpx` 的 INFO 日志被强制压到 WARNING。
- **不要用真实网络做批量压测**：下载引擎默认并发 6、限速 8 请求/秒、超时 10s、重试 3 次，
  这些默认值是为了「不把对方打挂、也不让自己被限流」。要调参请改 `app/config/settings.py`，
  不要在业务代码里散落魔数。
- 生成的离线地图数据仅限授权范围内使用；如需对外发布或商用，请自行确认测绘资质与数据合规要求。
