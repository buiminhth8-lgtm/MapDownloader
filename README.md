# 天地图离线地图工具

## 项目目标

Windows端生成天地图离线地图包，后续导入Android供osmdroid使用。

## 当前进度

Slice 8：MBTiles 完整检查与诊断工具（独立的可视化检查窗口、PASS/WARNING/FAIL
三档结论、覆盖完整性与缺失瓦片定位、图片抽样与可选深度扫描、文本报告导出）

完整链路：

```text
BoundingBox            (Slice 1：范围与合法性校验)
    ↓
validate_form          (Slice 7：唯一的下载前校验入口，一次报出全部问题)
    ↓
Tile Math              (Slice 1：算出瓦片编号区间)
    ↓
DownloadTask           (Slice 5：任务记录，落盘到 data/tasks.json；Slice 6 增加 map_type / with_labels)
    ↓
ExistingTileScanWorker (Slice 7：后台线程数"已有多少张"——SQLite 不许碰主线程)
    ↓
MapType → 两个图层      (Slice 6：MAP_TYPE_LAYERS 是全项目唯一的那张映射表)
    ↓
TiandituProvider       (Slice 2：拼 WMTS URL、判响应合法性)
    ↓
ExistingTileIndex      (Slice 5：查库里已有瓦片；默认只读坐标三列)
    ↓  should_skip_tile（在发 HTTP 之前判定）
Async Downloader       (Slice 3：并发 / 限速 / 重试 / 暂停取消)
    ↓
CompositeTileDownloader (Slice 6：底图 + 注记各请求一次 → TileComposer alpha 合成)
    ↓  on_tile 回调
MBTilesTileSink        (Slice 4：有界写队列 + 单消费者)
    ↓
MBTilesWriter          (Slice 4：批量事务写 SQLite，INSERT OR REPLACE)
    ↓
map.mbtiles
    ↓
Validator              (Slice 4：结构 + 抽样数据校验；Slice 6 增加合成信息自洽性)
    ↓
TaskService            (Slice 5：状态判定 + 落盘；启动时 DOWNLOADING → PAUSED)
    ↓
UiTaskState 矩阵        (Slice 7：把"状态"翻译成"哪几个按钮能点"，逐格可断言)
    ↓
MBTilesInspectionService (Slice 8：同一个检查工具，下载收尾时自动跑一次"快速"档)
    ↓
MBTilesInspectorDialog (Slice 8：手动打开的独立检查窗口，可快速 / 深度 / 导出报告)
```

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

暂未实现（**尚未排期**，不对应任何具体 Slice 号）：

- 地图包 manifest、离线包级 Sha256 校验、离线包体积统计
- ADB / Android 导入（设备发现、push、设备侧校验）
- 失败瓦片清单与单点重试、速度曲线

> 这几项曾经被写进"Slice 7"的计划里，但 Slice 7 实际排给了 Windows GUI 下载管理，
> 因此不再用 Slice 号承诺它们。界面上的设备区会如实说明"本版本不执行 adb 命令"
> （`app/config/settings.py` 的 `UI_TEXT_DEVICE_HINT`），而不是假装在工作。

## 环境

Python 3.11+（当前使用 `httpx` 做异步 HTTP，`PySide6` 做界面）

## 安装

```bash
python -m pip install -r requirements.txt
```

## 启动

```bash
python -m app.main
```

## 测试

```bash
python -m pytest                # 973 passed
python -m pytest tests/test_resume_download.py -q   # 只跑断点续传链路
python -m pytest tests/test_mbtiles_diagnostics.py tests/test_mbtiles_inspection_service.py -q
                                                    # 只跑 Slice 8 诊断规则 + 编排层
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
python scripts/capture_ui.py                   # 渲染主窗口为 ui_main_window.png
python scripts/capture_ui.py --offscreen       # 无显示环境（离屏渲染）
python scripts/capture_ui.py --with-tasks -o docs/slice5_task_panel.png
```

`--with-tasks` 会预置几条不同状态的任务记录，用于留档 Slice 5 的「下载任务」面板
（见 `docs/slice5_task_panel.png`）。它写的是**临时目录**里的 `tasks.json`，
不会碰到你真实的 `data/tasks.json`。

Slice 8 的检查窗口用专门的验收脚本留档（它同时是一份**可重复的实机验收**）：

```bash
python scripts/verify_slice8_inspector.py              # 真窗口 + 52 项检查 + 截图
python scripts/verify_slice8_inspector.py --offscreen  # 无显示环境（CI）
python scripts/verify_slice8_inspector.py --no-shots   # 只跑检查，不截图
```

它真的造四种文件（正常 / 缺瓦片 / 含坏图片 / 非 SQLite）、真的点按钮、真的等后台
线程，然后按结论分别截 `docs/slice8_inspector_{pass,warning,fail,idle}.png`。

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
```

**为什么不能只跑 pytest**：pytest 跑在 Qt `offscreen` 平台上——那里没有真实字体、没有
DPI 缩放、没有系统深浅色主题，`QMessageBox` 也不是真的窗口，而这三样东西恰恰能藏住真实
缺陷。上面那两个缺陷（关窗抛 `RuntimeError`、弹窗按钮是英文）**全都是这个脚本抓出来的**，
当时 883 条 pytest 全绿。

脚本真的起 windows 平台插件、真的显示窗口、真的点按钮，全程用 `httpx.MockTransport`
假服务端（**不触网、不消耗天地图配额**），任务记录与界面偏好都写到临时目录。五个场次：

| 场次 | 验的是什么 | 产出 |
|---|---|---|
| A | 空闲 → 计算 → 预估结果六行（含 Zoom 区间与地图类型） | `slice7_scene_a_estimate.png` |
| B | 预计 > 10,000 张 → 大任务确认框（只提示、不限制） | `slice7_scene_b_large_task.png` |
| C | 目标 `.mbtiles` 已存在 → 三选一；再走一遍「重新下载」的二次确认 | `slice7_scene_c_existing.png` / `slice7_scene_c2_restart.png` |
| D | 下载中（限速 10 请求/秒，保证"进行中"是可观察的中间态）→ 暂停 / 继续 → 完成小结 | `slice7_scene_d1_downloading.png` / `slice7_scene_d2_complete.png` |
| E | 部分瓦片失败 → 失败小结 +「继续补齐」+ 任务落盘为 FAILED | `slice7_scene_e_failed.png` |

它还会顺手查两件只有实机能查的事：**每个弹窗的按钮文案里有没有残留英文**
（`_english_buttons()`），以及流程跑完**主窗口必须关得掉**。数字对不上就以非 0 退出码结束。

## 目录结构

```text
tianditu-offline-tool/
├── app/
│   ├── main.py            入口（日志初始化 + QApplication + MainWindow）
│   ├── config/settings.py 全局常量，唯一魔数来源
│   ├── models/            纯数据模型（dataclass / enum），不依赖 UI
│   ├── ui/                界面层：主窗口 + 各面板 + 四个 QThread 桥
│   │                      （download_worker 跑编排 / connection_check 发 HTTP /
│   │                       tile_scan_worker 查 SQLite / mbtiles_inspector_dialog
│   │                       里的 MBTilesInspectionWorker 跑诊断）
│   │                      外加纯逻辑模块：task_actions（按钮矩阵）/ validation（校验）/
│   │                       preferences（QSettings 偏好）/ widgets（折行标签 + 主题色）/ desktop（打开目录）
│   ├── providers/         瓦片源 Provider：base.py + tianditu.py（Slice 2 ✅）
│   ├── geo/               瓦片坐标计算：bounds.py + tile_math.py（Slice 1 ✅）
│   ├── downloader/        下载引擎：models / retry_policy / rate_limiter / tile_downloader / download_manager / resume / composite_tile_downloader（Slice 3 ✅ + Slice 5 ✅ + Slice 6 ✅）
│   ├── mbtiles/           MBTiles 读写：models / writer / reader / validator（Slice 4 ✅；Slice 6 增加合成信息读写与自洽性校验）+ diagnostics（Slice 8 ✅：全部判定规则与结论模型）
│   ├── tasks/             任务持久化：task_store（原子 JSON 读写）/ task_service（状态机 + 启动恢复）（Slice 5 ✅）
│   ├── composer/          瓦片合成：tile_composer.py（Pillow alpha 合成，无状态可复用）（Slice 6 ✅）
│   ├── services/          编排层：map_download_service.py（下载 → 落盘 → 校验 → 续传 一条链，Slice 4 ✅ + Slice 5 ✅ + Slice 6 ✅；Slice 7 增加 sanitize_map_name）+ mbtiles_inspection_service.py（Slice 8 ✅：检查编排 / 进度 / 报告落盘）
│   ├── package/           离线包打包与校验（尚未排期）
│   └── android/           ADB 设备管理与导入（尚未排期；目前只有包文档，无实现）
├── tests/                  pytest 用例（973 条）
├── data/tasks.json         任务记录（运行时生成，已 gitignore）
├── docs/                   设计文档 + 界面留档截图（slice7_*.png / slice8_inspector_*.png）
├── scripts/capture_ui.py   界面截图留档脚本
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
`app.downloader.resume` 通过**结构化 Protocol**（`TileProbe`）判断"库里有没有这张瓦片"，
所以它能接收 `MBTilesWriter` / `MBTilesReader` 而**不必 import `app.mbtiles`**——依赖保持无环，
这条约束有专门的测试守着。

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

## 后续计划

- **离线包打包与校验** —— 地图包 manifest、Sha256 校验、离线包体积统计
- **ADB 导入** —— 设备发现、push、设备侧校验（`app/android/` 目前只有包文档，无实现）
- 失败瓦片清单与单点重试、速度曲线
- Android / osmdroid（端侧读取离线包）

> 以上都**尚未排期**，不再用 Slice 号承诺。Slice 8 排给了 MBTiles 检查与诊断工具，
> 而 Spec 明确禁止在那个 Slice 里实现 ADB / 打包 / 自动修复 MBTiles——即不许用设备
> 功能或"顺手修一下"挤掉本 Slice 的交付。检查工具**只读**：它报告问题，不改库。

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
