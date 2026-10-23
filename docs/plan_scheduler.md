# 计划批量调度（Slice 21）

> Slice 20 把"一次只能规划一块地"升级成**一份计划装多块地**，但那份计划**不会自己跑**——
> 点「生成任务」只造出一堆停在「已创建」的任务记录，下载要自己去「下载任务」列表里
> 逐条发起。这一版把"一个接一个跑完整份计划"补上：串行执行、暂停 / 继续 / 取消、
> 失败重试 / 跳过，以及**可跨进程恢复**的执行记录。

## 这一版解决什么问题

Slice 20 之后的体验是：攒好一份"全国 + 三省 + 五市"的计划，点「生成任务」，然后
要在「下载任务」列表里**手动一条一条点开始**，还得分清哪条属于这份计划、哪条是别的东西。
想中途停下喝杯咖啡、回来接着下？得自己记住跑到第几条了。

这一版在计划页上加了一排**执行按钮**：

```text
[开始计划] [暂停] [继续] [取消执行] [重试当前] [跳过并继续]        [关闭]
──────────────────────────────────────────────────────────────────────
状态：执行中
当前：示例北省 Z9~13
任务：2 / 3
当前任务：63%
总体：57%（32,640 / 57,302 张）
速度：5.2 MB/s
```

点「开始计划」之后，调度器按计划里的层级顺序**串行**把每一项跑完，期间全程只看一个
进度、只写一个文件。

## 三份数据，各管各的（Spec §3）

为什么执行态没有写进 `plans.json`？因为"跑到哪一步了"和"用户想下哪几块地"是
**两回事**，硬塞在一起会有两个直接后果：

- 执行进度按秒写盘，会把用户的计划配置一起反复重写；
- 一份计划可以被执行很多次（跑完 → 改了再跑），"上一次跑到哪"这条历史不该跟着计划走。

于是落地成三份互相独立的数据：

| 文件 | 负责 | 谁写 |
| --- | --- | --- |
| `data/plans.json` | 用户**想要**什么（可随时改，改完仍是同一份计划） | Slice 20 的计划页 |
| `data/tasks.json` | 每一块地的单任务记录（下载器反复改写进度） | 下载器（Slice 5 那条路） |
| `data/plan_executions.json` | 这份计划**跑到哪一步了**（本 Slice） | 调度器 |

执行记录里关于"一块地"的全部信息只有两个**引用**：`plan_item_id`（指向计划里的那一行）
和 `task_id`（指向 `tasks.json` 里的那条任务）。区域名、Zoom、张数全部**现查**——
复制一份进来就多一个会过期的真相。

引用而不是复制，是"界面上的数字"与"真正在下的东西"不会分家的唯一办法：
计划页里写"示例北省 3,449 张"，执行时下载器报上来的完成数就是那 3,449 张的成果。

## 两个"状态"不能混（Spec §2）

Spec 把执行态枚举命名为 `PlanItemStatus`，但这个名字在 Slice 20 已经被用了——
它指的是"这一项**能不能用**"（`VALID` / `INVALID`：区域在不在目录里、边界文件在不在）。
两者是完全不同的问题，取值集合也完全不重叠（前者两个、后者七个），一旦有人 import
错了不会报错，只会在某条分支上静默走错。

因此这里**刻意换名**为 `PlanItemRunStatus`（多一个 `Run`），并在模块文档里写明
两个名字的分工：

| 枚举 | 回答的问题 | 取值 |
| --- | --- | --- |
| `DownloadPlan.PlanItemStatus`（Slice 20） | 这项**能不能用**？（配置问题） | `VALID` / `INVALID` |
| `PlanItemRunStatus`（本 Slice） | 这项**跑到哪了**？（执行态） | `WAITING` / `RUNNING` / `PAUSED` / `COMPLETED` / `FAILED` / `CANCELLED` / `SKIPPED` |

整份计划还有一个独立的 `PlanExecutionStatus`，**不复用** `DownloadTaskStatus`：
任务状态描述"一个下载器在干什么"，这里描述"一份计划走到哪了"，合法迁移完全不同
（例如计划可以被"暂停"很多次，而"部分失败"是计划独有的概念）。

### 状态符号（列表行首，字符而非图标）

用**字符**而不是 `QIcon`/`QPixmap` 资源：这一列只是"一眼看出哪一行在跑"，为它引入
图片资源会让打包与高 DPI 缩放都多一件要管的事。这几个字符在 Windows / macOS / Linux
的常见字体里都有字形。

| 状态 | 符号 | 含义 |
| --- | --- | --- |
| `WAITING` | ○ | 还没轮到 |
| `RUNNING` | ▶ | 正在下 |
| `PAUSED` | Ⅱ | 被暂停（可继续） |
| `COMPLETED` | ✓ | 拿到了 |
| `FAILED` | ✕ | 失败（可重试 / 跳过） |
| `CANCELLED` | – | 被取消（整份取消时后续条目标记） |
| `SKIPPED` | » | 用户主动跳过并继续 |

整份计划状态：`CREATED` 未开始 / `RUNNING` 执行中 / `PAUSED` 已暂停 /
`COMPLETED` 已完成 / `PARTIAL_FAILED` 部分失败 / `FAILED` 失败 / `CANCELLED` 已取消。

## 严格串行（Spec §4 / §29）

一份计划里的任务**一个接一个**跑，绝不并行：

```text
DownloadPlan ──► create_download_tasks() ──► [Task1, Task2, Task3]
                                                      │
PlanScheduler:   Task1 ──完成──► Task2 ──完成──► Task3 ──完成──► COMPLETED
```

多个地图任务同时跑会互相抢网络与磁盘，结果是"三个都慢、三个都看不出来进展"。串行的
代价是总时长，换来的是"任何时刻只有一个文件在写、只有一个进度在动"——用户随时知道
自己在等什么。

**单任务内部仍然是并发下载的**（Slice 3 的 N 个 worker + 限速 + 重试），本模块一个字
都没改那条路：串行是"任务之间"的约束，不是"任务之内"的。

### 每一项都走 `resume_task`——有意为之（Spec §7 / §9）

调度器对**每一个条目**都调用 `MapDownloadService.resume_task()`，不是为了省事，而是
因为它同时覆盖了两种情形：

- 输出文件不存在 → 当全新下载；
- 输出文件存在 → 读 metadata 做一致性校验 → 跳过已有瓦片 → 补差集。

于是"第一次跑"和"暂停后继续"在单任务层面是**同一条路**，本层不需要分支——
而"暂停后继续必须复用 MBTiles Resume、不能从头下载"（Spec §9）就成了这条路的自然结果，
而不是一句需要人去遵守的约定。

## 七个动作与按钮可用性（Spec §15 / §18）

执行区有七个动作，`PLAN_ACTION_*` 的值即按钮字典的键（验收脚本按这个键取按钮）：

| 动作（常量值） | 含义 | 按钮可点时（来自 `_apply_run_state`） |
| --- | --- | --- |
| `start`（开始计划） | 从头跑整份计划 | 没有执行记录，或记录已到终态，且不在估算中 |
| `pause`（暂停） | 安全停下当前项 | 状态为 `RUNNING` |
| `resume`（继续） | 从暂停处接着跑 | 状态为 `PAUSED` |
| `cancel_run`（取消执行） | 当前项安全停下，后续一律标 `CANCELLED` | 状态为 `RUNNING` / `PAUSED` / `PARTIAL_FAILED` |
| `retry_current`（重试当前） | 把失败的那一条退回可跑、从它重跑 | 状态为 `PARTIAL_FAILED` |
| `skip_current`（跳过并继续） | 把失败的那一条标记 `SKIPPED`、接着跑下一条 | 状态为 `PARTIAL_FAILED` |
| `close`（关闭） | 关窗；若正在跑则先请求暂停再等 worker 收尾 | 永远可点 |

`RUNNING` / `PAUSED` / `PARTIAL_FAILED` 之外不显示任何执行按钮（`start` 之外全部置灰）。
注意 `PARTIAL_FAILED` **不是终态**——它停在某条失败/跳过的项上，用户在「重试当前」与
「跳过并继续」之间选一个，计划才能往前走。

### 执行期间锁住编辑（Spec §18）

`RUNNING` / `PAUSED` 都算"这次执行还活着"，期间**编辑类按钮与表格被锁**：加项、移除、
清空、改 Zoom、启用/停用一律置灰，原因写进 tooltip（"执行中：暂停或结束后才能修改这份计划"）。
否则用户在跑的过程中删掉一行，执行记录里那条就再也对不上计划了。执行区那排按钮不受此限——
它们正是用来改变这个状态的。

## 暂停 = 安全停下 + 改写状态（Spec §9）

引擎已有的"安全停下"只有一条路：`cancel()` —— 它停止生产新瓦片、让 worker 消费完
队列里残留的项、drain 写队列、flush 并关闭数据库，于是磁盘上留下一个**完整可用的
（不完整的）MBTiles**。

本层把"暂停"实现成：**请求引擎安全停下 → 等它真的返回 → 把任务记录从引擎写的
`CANCELLED` 改回 `PAUSED`**。多出来的那一次状态改写是刻意的："用户点了暂停"和"用户点了
取消"在**磁盘上**是同一个动作，区别只在后续意图——而那个意图只有本层知道，所以由本层补写。

这样做而不是"用闸门挂住引擎"（`pause()` 只清闸门、协程不返回）的理由：挂住的话，暂停期间
写队列与数据库连接都还开着，程序退出时仍要强收；而"停下并落盘"让"暂停"变成一个
**可跨进程**的状态（见下节）。

## 继续 / 恢复：绝不自动联网（Spec §20 / §25）

- **`PAUSED` 继续**：用户点「继续」才发下一个请求。
- **程序异常退出后的恢复**：启动时若发现一份执行记录还停在 `RUNNING`（上次崩了/被杀），
  一律把它和那条在跑的条目都降级成 `PAUSED`，**绝不自动联网继续**。理由与单任务恢复
  完全相同：自动下载会消耗用户的配额，那是必须由人拍板的动作。用户下次打开计划页会看到
  "上次计划未完成"，点「继续」即可从中断处接着下（MBTiles Resume 只补差集）。

这套恢复语义由纯函数 `recover_running_execution()` 实现，可脱离窗口与网络单独断言。

## 取消（Spec §11）

点「取消执行」：当前项被安全停下（同样走 `cancel()` 落盘），**后续所有条目一律标记
`CANCELLED`**，整份计划落到 `CANCELLED` 终态。已跑完的条目保持 `COMPLETED`，半截的
那一条保持它实际下到的成果——取消不是"删掉一切"，而是"到此为止"。

## 失败即停 + 重试当前 / 跳过并继续（Spec §11 / §29）

调度器**不在单任务层面自动忽略失败**（与单任务下载"失败也继续"不同）：一项失败就停下，
整份计划落到 `PARTIAL_FAILED`，把决定权交还用户。

- **重试当前**：把失败的那一条退回 `PAUSED` 并设为当前项，从它重跑（仍走 `resume_task`，
  已下的瓦片被跳过）。
- **跳过并继续**：把失败的那一条标记 `SKIPPED`，接着跑下一条。

`unresolved_count = FAILED + SKIPPED + CANCELLED`；只要 `unresolved_count > 0`，
整份计划的最终状态就是 `PARTIAL_FAILED` 而不是 `COMPLETED`——"留下任何一块没下成就是
部分失败"。`failed_count` 只数 `FAILED`（不含用户主动跳过的），界面用来显示"✕N"。

## 进度按张数聚合，不是按条目数（Spec §12 / §13 / §14）

整体进度条走的是**瓦片张数加权**，不是"跑完几项就加百分之几"：

- `total_tiles` = 所有启用项 `selected_tiles` 之和（与 Slice 20 汇总口径一致，多边形按选中张数）；
- `completed_tiles` = 每条目按实际处理张数累计，并以该项的 `selected_tiles` 收口
  （`min(done, target)`），防止"计划被改过 / 估算被重跑过"之后分子>分母算出 >100%；
- 速度**只取当前任务**（任务是串行的，不存在"整份计划的速度"这种独立量），任务切换时归零——
  否则界面上会挂着上一个任务的残留速度，看着像还在下。

聚合逻辑集中在纯函数 `build_progress()`：界面每收到一帧就照着画、**不做任何计算**，
于是进度条与状态行不会各算一遍对不上。该函数不 import Qt、不碰磁盘，可单独断言。

## 验收

```bash
# 单元 / 集成用例（模型 / 存储 / 调度器 / worker / 计划页执行 / 主窗口接线）
.venv/Scripts/python.exe -m pytest tests/test_plan_execution.py \
    tests/test_plan_execution_store.py tests/test_plan_scheduler.py \
    tests/test_plan_worker.py tests/test_download_plan_execution_ui.py -q

# 实机验收：真窗口 / 真按钮 / 截图落 docs/（五种状态各一张）
.venv/Scripts/python.exe scripts/verify_slice21_plan_execution.py
```

五种执行状态的截图：

| 状态 | 截图 |
| --- | --- |
| 已完成 | `docs/images/slice21_plan_completed.png` |
| 已暂停 | `docs/images/slice21_plan_paused.png` |
| 部分失败 | `docs/images/slice21_plan_partial_failed.png` |
| 已取消 | `docs/images/slice21_plan_cancelled.png` |
| 恢复（上次未完成） | `docs/images/slice21_plan_recovery.png` |

## 代码位置

| 文件 | 职责 |
| --- | --- |
| `app/planning/plan_execution.py` | 执行模型（`PlanExecution` / `PlanItemExecution`）+ 状态枚举 + 纯函数进度聚合（`build_progress` / `compute_plan_totals` / `recover_running_execution`） |
| `app/planning/plan_scheduler.py` | 串行调度器（`PlanScheduler`）：顺序 / 生命周期 / 失败即停 / 复用 `resume_task` / 落盘节流 |
| `app/planning/plan_execution_store.py` | `plan_executions.json` 读写（白名单字段、原子写、坏文件隔离成 `.corrupt`） |
| `app/workers/plan_download_worker.py` | 后台线程（`QThread` + `asyncio`），把调度器包成可在 Qt 里跑的 worker，命令队列（`pause`/`resume`/`cancel`/`retry_current`/`skip_current`） |
| `app/ui/download_plan_dialog.py` | 计划页执行区：七个动作按钮、状态行、进度条、表格符号列、执行期间锁编辑 |

> 与 Slice 20 同样的不变量：`app/planning/__init__.py` 只再导出模型，绝不导出服务模块，
> 否则会撞上"半初始化的模块"导入环（有一条例 subprocess 用例守着）。
