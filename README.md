# 天地图离线地图工具

## 项目目标

Windows端生成天地图离线地图包，后续导入Android供osmdroid使用。

## 当前进度

Slice 4：MBTiles Writer 与下载链路落盘（边下载边写入 `xxx.mbtiles`）

完整链路：

```text
BoundingBox            (Slice 1：范围与合法性校验)
    ↓
Tile Math              (Slice 1：算出瓦片编号区间)
    ↓
TiandituProvider       (Slice 2：拼 WMTS URL、判响应合法性)
    ↓
Async Downloader       (Slice 3：并发 / 限速 / 重试 / 暂停取消)
    ↓  on_tile 回调
MBTilesTileSink        (Slice 4：有界写队列 + 单消费者)
    ↓
MBTilesWriter          (Slice 4：批量事务写 SQLite)
    ↓
map.mbtiles
    ↓
Validator              (Slice 4：结构 + 抽样数据校验)
```

已支持：

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

暂未实现：

- 从已有 MBTiles 断点续传（已下载瓦片跳过、任务持久化）—— Slice 5
- 底图与注记图层合成（本版本只单独下载单层）
- 地图包 manifest、Sha256 校验、离线包体积统计 —— Slice 5
- ADB / Android 导入 —— Slice 6

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
python -m pytest
```

测试在 Qt ``offscreen`` 平台下运行（见 `tests/conftest.py`），无需真实显示器；
瓦片下载与落盘相关用例全部走 **MockTransport / 假数据**，**不访问真实网络**，
产物写到临时目录（`work_dir` 夹具，不用本机 `tmp_path`——它会被沙箱拦在 `WinError 5`）。

异步用例依赖 `pytest-asyncio`，模式配置在仓库根的 `pytest.ini`（`asyncio_mode = auto`，
无需给每个用例手写 `@pytest.mark.asyncio`）。

## 界面留档

```bash
python scripts/capture_ui.py                   # 渲染主窗口为 ui_main_window.png
python scripts/capture_ui.py --offscreen       # 无显示环境（离屏渲染）
```

## 目录结构

```text
tianditu-offline-tool/
├── app/
│   ├── main.py            入口（日志初始化 + QApplication + MainWindow）
│   ├── config/settings.py 全局常量，唯一魔数来源
│   ├── models/            纯数据模型（dataclass / enum），不依赖 UI
│   ├── ui/                界面层：主窗口 + 各面板 + download_worker.py（QThread 桥接 asyncio）
│   ├── providers/         瓦片源 Provider：base.py + tianditu.py（Slice 2 ✅）
│   ├── geo/               瓦片坐标计算：bounds.py + tile_math.py（Slice 1 ✅）
│   ├── downloader/        下载引擎：models / retry_policy / rate_limiter / tile_downloader / download_manager（Slice 3 ✅）
│   ├── mbtiles/           MBTiles 读写：models / writer / reader / validator（Slice 4 ✅）
│   ├── services/          编排层：map_download_service.py（下载 → 落盘 → 校验 一条链，Slice 4 ✅）
│   ├── package/           离线包打包与校验（Slice 5）
│   └── android/           ADB 设备管理与导入（Slice 6）
├── tests/                  pytest 用例
├── docs/                   设计文档
├── scripts/capture_ui.py   界面截图留档脚本
├── pytest.ini              pytest 配置（asyncio_mode = auto）
└── requirements.txt
```

依赖方向单向：`ui -> services -> downloader/mbtiles -> providers/geo -> models -> config`；
业务包之间通过 `models` / `geo` 传递数据，不在 UI 层写业务逻辑。
UI 需要瓦片数据时必须调用 `app.geo`，需要落盘时必须调用 `app.services`，
**SQLite 只出现在 `app/mbtiles` 里**（`DownloadManager` 不依赖数据库）。

## 用法：测试天地图连接

1. 在「Token」填入天地图控制台申请的服务密钥（`tk`，输入框默认隐藏，可勾选「显示」临时查看）。
2. 选择地图类型（矢量地图 / 卫星影像）、填好范围与 Min Zoom。
3. 点 `[测试天地图连接]`：程序取范围中心点，换算成瓦片编号，只请求 **一张** 瓦片做验证。

成功时「天地图连接」区显示图层、Zoom、HTTP 状态、响应体积与耗时；失败时给出明确原因
（`HTTP 403 — Token 可能无效…` / `请求超时` / `响应不是有效的图片瓦片…`），
并同步写入运行日志。日志中的 URL 一律脱敏（`tk=abcd****xyz`），不会泄露完整 Token。

## 用法：下载并生成 MBTiles

1. 填好 Token、地图类型、范围与 Zoom 区间（Min Zoom ~ Max Zoom）。
2. 填「输出目录」与「地图名称」——最终产物是 `<输出目录>/<地图名称>.mbtiles`
   （名称会被清理成合法文件名，Windows 非法字符 `\ / : * ? " < > |` 替换为 `_`）。
3. 点 `[开始下载]`：程序先算出总瓦片数与预计容量，并显示输出文件路径；
   - 若预计超过 **10,000** 张，会弹一次**确认框**（只提示、不限制规模）；
   - 确认后在后台线程里跑完整链路，状态区实时刷新
     `状态 / 当前 Zoom / 总数 / 完成 / 成功 / 失败 / 跳过 / 速度 / 已下载字节 / 总进度`。
4. `[暂停]` / `[继续]` 可随时切换；关窗会自动取消任务。
5. 结束后「下载结果」区显示：

   ```text
   文件：D:\maps\beijing.mbtiles
   瓦片：12,345
   大小：352.40 MB
   Zoom：8~16
   验证：PASS
   ```

   并逐行写入运行日志；若有失败，还会给出 `失败原因：<原因>（N 张）`（最多 3 条，按张数倒序）。

取消任务会**保留**已经写入的瓦片，磁盘上留下一个可用的（不完整的）`.mbtiles`，不会自动删除；
从已有 MBTiles 继续下载（跳过已有瓦片）属于 Slice 5。

## 后续计划

- **Slice 5：断点续传、任务恢复、已有瓦片跳过** —— 打开已有 `.mbtiles`，跳过已入库瓦片继续下载；下载任务可持久化、可恢复
- 地图包 manifest、Sha256 校验、离线包体积统计
- GUI 下载管理完善（失败瓦片清单与单点重试、速度曲线）
- ADB导入（设备发现、push、设备侧校验）
- Android / osmdroid（端侧读取离线包）

## 说明与注意事项

- 天地图服务需要自行申请 Token，请遵守天地图服务条款与调用配额限制。
  **不要把真实 Token 写进代码、README 或测试**；本仓库中的 Token 一律是占位假值。
- **XYZ 与 TMS 的分工** —— MBTiles 内部采用 **TMS** 的 `tile_row`（原点在左下角），
  而本工具的 API / URL / 日志 / 校验报错**统一使用 XYZ**（原点在左上角）。
  翻转只发生在 `MBTilesWriter` / `MBTilesReader` 内部的边界上，且复用 Slice 1 的
  `xyz_y_to_tms_y()`，没有第二套公式。一致性由测试锁住：`z=3, x=4, y_xyz=2`
  必须写成 `tile_row = 2³ − 1 − 2 = 5`，并且能用 `read_tile(3, 4, 2)` 读回来。
- **不要用真实网络做批量压测**：下载引擎默认并发 6、限速 8 请求/秒、超时 10s、重试 3 次，
  这些默认值是为了「不把对方打挂、也不让自己被限流」。要调参请改 `app/config/settings.py`，
  不要在业务代码里散落魔数。
- 生成的离线地图数据仅限授权范围内使用；如需对外发布或商用，请自行确认测绘资质与数据合规要求。
