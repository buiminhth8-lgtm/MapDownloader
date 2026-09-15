# 天地图离线地图工具

## 项目目标

Windows端生成天地图离线地图包，后续导入Android供osmdroid使用。

## 当前进度

Slice 3：天地图基础瓦片下载引擎（可控并发 / 超时 / 重试 / 进度统计 / 暂停继续取消）

已支持：

- **`TileDownloader`** —— `httpx.AsyncClient` 单实例复用（非每瓦片新建），内置超时、指数退避重试与限速；`async with` / `close()` 管理生命周期
- **`DownloadManager`** —— `asyncio.Queue`（有上限）+ **固定数量 worker**，瓦片惰性生成（不在内存里一次性铺开整个 zoom 区间），并发度默认 **6**、范围 **1 ~ 16**
- **`RetryPolicy`** —— 默认 `max_attempts=3`，退避 `0.5s → 1s → 2s`（上限 5s）；重试 408 / 425 / 429 / 5xx、网络错误、超时、空响应、非法图片；**不重试** 400 / 401 / 403 / 404（Token 无效或瓦片确实不存在，重试无意义）
- **`RateLimiter`** —— 默认 **8 请求/秒**（预留式计时，避免脉冲式突发），可整体关闭
- **暂停 / 继续 / 取消** —— 基于 `asyncio.Event`，可从中途任意时刻生效；取消后未跑完的瓦片计为 `skipped`，汇总里 `cancelled=True`
- **进度统计** —— `DownloadProgress`（总数 / 完成 / 成功 / 失败 / 跳过 / 已下载字节 / 实时速度 / 当前 zoom），回调按 **~0.15s** 节流（约 5~10 次/秒），不逐瓦片刷屏
- **`on_tile` 回调** —— 每张成功瓦片通过回调交出 `TileDownloadResult`（含 `content` 字节），**为 Slice 4 的 MBTilesWriter 预留的接入点**
- **`DownloadSummary`** —— 收尾汇总（耗时、平均速度、成功/失败/跳过、取消标志），并把失败原因按张数聚合进 `failures_by_reason`，`top_failures()` 取前 3 条展示——用户看到的不是光秃秃的「失败 312 张」，而是「失败原因：HTTP 403 — Token 可能无效，或该服务未在控制台开通（312 张）」
- **GUI 下载测试接入** —— `[开始下载]` / `[暂停]` / `[继续]` 已连到 `DownloadManager`，后台 `QThread` 跑独立事件循环，界面不冻结；并显示 **「当前为下载引擎测试模式，Slice 4 起写入 MBTiles」** 提示，**上限 500 张瓦片**（超出直接拒绝并说明原因，不静默截断）；关窗时自动取消下载任务
- **Token 不外泄** —— 日志与界面统一 `mask_url` 脱敏，`httpx` / `httpcore` 的 INFO 级日志被压制（它们会把带 `tk=` 的完整 URL 打到控制台）

> 本 Slice **只下载、不落盘**：瓦片仅通过 `on_tile` 回调消费，不写文件、不生成 MBTiles、不做断点续传。

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

- 瓦片落盘 / MBTiles 落库（Slice 4）
- 断点续传（已下载瓦片跳过、任务持久化）
- 底图与注记图层合成（本版本只单独下载单层）
- 离线包打包与校验（Sha256、体积）
- ADB / Android 导入

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
瓦片下载相关用例全部走 **MockTransport / 假数据**，不访问真实网络、不写任何文件。

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
│   ├── mbtiles/           MBTiles 读写（Slice 4，待做）
│   ├── package/           离线包打包与校验（后续 Slice）
│   └── android/           ADB 设备管理与导入（后续 Slice）
├── tests/                  pytest 用例
├── docs/                   设计文档
├── scripts/capture_ui.py   界面截图留档脚本
├── pytest.ini              pytest 配置（asyncio_mode = auto）
└── requirements.txt
```

依赖方向单向：`ui -> downloader -> providers/geo -> models -> config`；
业务包之间通过 `models` / `geo` 传递数据，不在 UI 层写业务逻辑。
UI 需要瓦片数据时必须调用 `app.geo`，禁止复制公式。

## 用法：测试天地图连接

1. 在「Token」填入天地图控制台申请的服务密钥（`tk`，输入框默认隐藏，可勾选「显示」临时查看）。
2. 选择地图类型（矢量地图 / 卫星影像）、填好范围与 Min Zoom。
3. 点 `[测试天地图连接]`：程序取范围中心点，换算成瓦片编号，只请求 **一张** 瓦片做验证。

成功时「天地图连接」区显示图层、Zoom、HTTP 状态、响应体积与耗时；失败时给出明确原因
（`HTTP 403 — Token 可能无效…` / `请求超时` / `响应不是有效的图片瓦片…`），
并同步写入运行日志。日志中的 URL 一律脱敏（`tk=abcd****xyz`），不会泄露完整 Token。

## 用法：批量下载瓦片（测试模式）

1. 填好 Token、范围与 Zoom 区间（Min Zoom ~ Max Zoom）。
2. 点 `[开始下载]`：程序先算出总瓦片数，
   - 若超过 **500** 张，会**直接拒绝**并提示原因（不静默截断），需要缩小范围或降低 zoom；
   - 否则在后台线程里跑 `DownloadManager`，状态区实时刷新 `总数 / 完成 / 成功 / 失败 / 跳过 / 速度`。
3. `[暂停]` / `[继续]` 可随时切换；关窗会自动取消下载任务。
4. 结束后日志区会逐行给出汇总；若有失败，最后几行里有 **`失败原因：<原因>（N 张）`**
   （最多 3 条，按张数倒序）。Token 无效、瓦片不存在、超时、响应不是图片都会分别归类，
   而且失败原因只由状态码/异常类型推导，**不会带出完整 Token**。

> **当前为下载引擎测试模式**：瓦片下载完**直接丢弃**，Slice 4 起通过 `on_tile` 写入 MBTiles。
> 所以本版本跑完不会在磁盘上留下任何瓦片文件。

## 后续计划

- **Slice 4：MBTiles Writer** —— 把 `on_tile` 回调收到的瓦片写进 SQLite/MBTiles（`tiles` + `metadata` 表），元数据记录图层、zoom 范围、边界、来源与时间
- 断点续传（已下载瓦片跳过、任务持久化）
- 离线包打包与校验（Sha256、体积）
- GUI 下载管理完善（失败瓦片清单与单点重试、速度曲线）
- ADB导入（设备发现、push、设备侧校验）
- Android / osmdroid（端侧读取离线包）

## 说明与注意事项

- 天地图服务需要自行申请 Token，请遵守天地图服务条款与调用配额限制。
  **不要把真实 Token 写进代码、README 或测试**；本仓库中的 Token 一律是占位假值。
- **不要用真实网络做批量压测**：下载引擎默认并发 6、限速 8 请求/秒、超时 10s、重试 3 次，
  这些默认值是为了「不把对方打挂、也不让自己被限流」。要调参请改 `app/config/settings.py`，
  不要在业务代码里散落魔数。
- 生成的离线地图数据仅限授权范围内使用；如需对外发布或商用，请自行确认测绘资质与数据合规要求。
