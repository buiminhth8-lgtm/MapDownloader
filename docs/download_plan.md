# 多级下载计划（Slice 20）

> 全国 / 省 / 市 一次规划，一次算清总量，再一次性生成多个下载任务。

## 这一版解决什么问题

之前一次只能规划**一块地**：选好一个区域 → 算一遍 → 开始下载 → 等它下完 →
再选下一块。想下"全国概览 + 三个省 + 五个市"，就得把上面那一轮重复九次，
而且**始终不知道总共要下多少、要占多大**。

Slice 20 在"选区域"和"开始下载"之间加了一层**计划**：

```text
Region Selection        DownloadPanel 的三级级联（Slice 19）
（国家 / 省 / 市）        selected_region_id = 市 or 省 or 国
        │
        ▼
Zoom Strategy           ZoomStrategyService（Slice 17）
（推荐区间 / 自定义）     COUNTRY Z4~10 / PROVINCE Z9~13 / CITY Z12~16 / MISSION Z15~18
        │               推荐模式下**唯一**的来源；自定义模式原样收下，只警告
        ▼
DownloadPlan            region_id + 等级 + min/max zoom + enabled
（多块地，一份配置）      **不放 Geometry、不放瓦片列表** —— 只放"哪块地 + 哪段 Zoom"
        │
        ▼
PlanEstimator           RegionService.get_download_region → RegionTileEstimator（Slice 16）
（逐项估算 + 汇总）      → selected_tiles / estimated_bytes（多边形按**选中**张数）
        │                后台线程 + 进度信号 + 可取消
        ▼
PlanValidator           错误（不修没法生成）：空计划 / 区域不在 / 边界缺 / Zoom 非法 /
（能不能生成）           没算过 / 输出目录不可用
        │               提醒（不拦）：超出推荐 / 规模偏大 / 目标文件已存在
        ▼
PlanService.create_download_tasks()
        │               ★ 只写 tasks.json，**不开始下载**
        ▼
DownloadTask × N        现有的任务记录模型（Slice 5），形状与单区域下载完全一样
        │               顺序：全国 → 省 → 市（为串行执行做准备）
        ▼
（Slice 21）批量调度     本 Slice **不实现**：怎么一个接一个跑、失败了怎么办
```

## 怎么用

1. 在「下载区域」里选好一块地（国家 / 省 / 市），Zoom 按推荐或自定义；
2. 点 **「加入下载计划」**——面板下面那行摘要会立刻变成"下载计划：3 个区域 /
   57,302 张 / 1.63 GB"；
3. 重复 1~2，把所有想下的地块攒进同一份计划；
4. 点 **「查看下载计划」**打开计划页：

   ```text
   ┌ 示例计划   输出目录：D:/out/离线计划 ─────────────────────────────────┐
   │ 已形成三级离线地图覆盖：全国级 → 省级 → 市级                          │
   │ ☑ 示例国    全国级  Z4~10     268 张    7.85 MB   可参与下载         │
   │ ☑ 示例北省  省级    Z9~13   3,449 张  100.98 MB   可参与下载         │
   │ ☑ 示例河市  市级    Z12~16 53,585 张    1.53 GB   可参与下载         │
   │ 合计：3 个区域 / 57,302 张 / 1.63 GB                                 │
   │ 提醒：计划规模偏大：57,302 张瓦片（超过 50,000 张）…                   │
   │ [重新估算] [移除] [清空] [生成任务]                     [关闭]        │
   └──────────────────────────────────────────────────────────────────────┘
   ```

5. 勾选框逐项启用 / 停用（停用的不入合计、不生成任务，但**留在计划里**）；
6. 点 **「生成任务」**——每个启用且可用的条目生成一条下载任务记录，停在
   **「已创建」**。**下载要你自己去「下载任务」列表里逐条开始。**

## 几条刻意的设计

### 计划里不放几何，也不放瓦片列表

`plans.json` 里每个条目只有：`id` / `region_id` / `region_name` / `region_level` /
`min_zoom` / `max_zoom` / `enabled`。

- **`region_id` 是唯一的权威引用**：区域名会随数据更新变，几何更是几十 MB 起步；
- **不存瓦片坐标**：一个市级 Z16 的瓦片列表是几万到几百万行，存下来只会让
  计划文件变成一个慢得打不开的怪物，而**算一次只要几十毫秒**；
- **不存结论**：状态（可用 / 不可用）与张数打开时重算 —— 磁盘上的"上次算了
  53,585 张"在边界数据更新之后就是错的，而界面上显示错的数字比显示"尚未估算"更糟。

### 同一个区域只允许一条

`DownloadPlanBuilder.add_region()` 对已有的 `region_id` 是**更新**而不是追加
（Spec §5）。校验器还会再查一遍重复（`plans.json` 是人可编辑的文件，也可能来自
更早的版本）—— 重复项会变成两个指向同一块地、写到同一个文件的任务。

### 禁用项照样算，只是不入合计

汇总口径是"启用 **且** 可用"，但估算对**所有**条目都做一遍：这样"关掉一项"
能立刻给出新总量，而那一行仍然显示自己的容量 —— 用户正是靠这个数字决定
要不要把它重新打开。

### 多边形看"选中"张数，不是候选

`selected_tiles` 是裁剪之后的张数。用外包矩形的候选数当总量，在渤海湾、
广州湾这类形状上能差出一个数量级，而用户会以为"程序漏下了"。
计划页里 `预计瓦片` 那一列显示的就是 `selected_tiles`。

### 文件命名只有一处实现

```text
<输出目录>/<计划名>/<region_id>_z<下界>_<上界>.mbtiles
        D:/out/计划A/demoland_z4_10.mbtiles
```

规则在 `app/planning/download_plan.py` 的 `planned_output_paths()` 里，
界面显示、校验"是不是已经存在"、生成任务三处**共用同一个函数**。
各写一遍的后果是：用户在界面上看到 `a_z4_10.mbtiles`，实际写进了
`a_z4_10-2.mbtiles` —— 而那是他自己磁盘上的东西，没有比这更让人不信任的体验。

计划名会先过 `sanitize_map_name()`：Windows 非法字符（`\ / : * ? " < > |`）会被
换成 `_`，保留名（`CON` / `LPT1`）会加后缀，空名字落到 `offline_plan`。

### 估算在后台线程里跑，而且能取消

一个省 Z13、一个市 Z16 就是几十万张瓦片，多个条目加起来的遍历时间是秒到分钟级。
主线程跑它 = 窗口"无响应"。因此：

- 估算走 `PlanEstimateWorker`（`QThread`），每算完一项发一次进度；
- 取消的检查点在**条目之间**，取消标志是 `threading.Event`（跨线程安全，
  立刻生效，不必等信号槽往返）；
- 取消后**计划配置一个字都不动**，已算完的条目保留结果，不产生任何半成品。

### 启动时没有计划文件是正常的

`data/plans.json` 不存在（旧版本升级上来、首次运行）→ 新建一份空计划，
程序照常启动。文件损坏 → 隔离成 `plans.json.corrupt` 并同样落到空计划。
区域被删 / 边界文件缺失 → **只把那一条标成不可用**，整份计划照常打开 ——
否则用户连"把坏的那条删掉"都做不了。

## 这一版**不做**的事

- **不提供「全部开始下载」**：本 Slice 只生成任务记录，开始下载仍在任务列表里
  逐条发起。批量调度是 Slice 21。
- **不做 Mission 航线 / 作业区**：`MISSION` 等级在模型里预留，但没有界面入口。
- **不做完整风险分析**：只有"规模偏大 / 超出推荐 / 目标文件已存在"三条提醒。
- **不动下载器**：`MapDownloadService` / `DownloadWorker` / MBTiles 格式一行未改，
  单区域下载那条路完全不变（不想用计划的人可以当它不存在）。

## 代码位置

| 文件 | 职责 |
| --- | --- |
| `app/planning/download_plan.py` | 计划与条目的模型 + **输出命名**（唯一实现） |
| `app/planning/plan_builder.py` | 编辑：加 / 删 / 启停 / 定 Zoom（推荐值取自策略层） |
| `app/planning/plan_estimator.py` | 逐项估算 + 汇总（复用 `RegionTileEstimator`） |
| `app/planning/plan_validator.py` | 错误 / 提醒清单（带机器码给日志，人话给界面） |
| `app/planning/plan_service.py` | 编排入口；**唯一**会碰 `TaskService` 的地方 |
| `app/planning/plan_store.py` | `plans.json` 读写（白名单字段、原子写、坏文件隔离） |
| `app/ui/download_plan_dialog.py` | 计划页（表格 / 合计 / 问题清单 / 四个动作） |
| `app/ui/plan_estimate_worker.py` | 估算线程（进度信号 + 取消） |

> `app/planning/__init__.py` **只再导出模型**，绝不导出上面这些服务模块：
> `app.regions` → `app.geo.region` → `app.planning.region_level` 已经是一条导入边，
> 再让 `app.planning` 去拉服务模块就会撞上"半初始化的模块"（有一条子进程用例守着）。

## 验收

```bash
# 单元测试（模型 / 构建 / 估算 / 校验 / 存储 / 任务生成 / 计划页 / 主窗口接线）
.venv/Scripts/python.exe -m pytest tests/test_download_plan.py tests/test_plan_builder.py \
    tests/test_plan_estimator.py tests/test_plan_validator.py tests/test_plan_service.py \
    tests/test_download_plan_dialog.py tests/test_main_window_download_plan.py -q

# 实机验收（真窗口 / 真按钮 / 截图落 docs/）
.venv/Scripts/python.exe scripts/verify_slice20_plan.py
```
