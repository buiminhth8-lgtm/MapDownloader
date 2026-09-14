# 天地图离线地图工具

## 项目目标

Windows端生成天地图离线地图包，后续导入Android供osmdroid使用。

## 当前进度

Slice 2：Tianditu Provider（天地图 WMTS URL 与单瓦片连通性验证）

已支持：

- **天地图 WMTS URL 生成** —— `TiandituProvider.build_tile_url(layer, zoom, x, y)`，完整 WMTS 1.0.0 参数（`SERVICE`/`REQUEST`/`VERSION`/`LAYER`/`STYLE`/`TILEMATRIXSET`/`FORMAT`/`TILEMATRIX`/`TILEROW`/`TILECOL`/`tk`）
- **四种图层** —— `vec_w`（矢量底图）、`cva_w`（矢量中文注记）、`img_w`（影像底图）、`cia_w`（影像中文注记），仅 Web 墨卡托 `_w`
- **t0 ~ t7 分片** —— 确定性策略 `server_index(x, y) = (x + y) % 8`（不用随机数，保证可复现与可测试）
- **Token 参数** —— 自动 URL 编码；日志/界面一律通过 `mask_url` / `mask_token` 脱敏（`tk=abcd****xyz`），Token 为空抛 `ValueError`
- **单瓦片连通性测试** —— `test_tile()` 只请求一张瓦片、**不写文件**，返回 `TileTestResult`（成功标志、HTTP 状态、Content-Type、体积、耗时、错误原因）
- **PNG/JPEG 合法性验证** —— `is_valid_tile_response()` 以文件 magic bytes（`89 50 4E 47` / `FF D8 FF`）为准，不只看 `Content-Type`；HTML / XML / JSON / `ServiceException` / 纯文本错误页、空响应、超限响应一律判失败
- **GUI 连接测试** —— 地图配置区新增 `[测试天地图连接]`：取当前范围中心点 → 调 Slice 1 算瓦片 → 后台线程（`QThread`）请求 → 展示 `连接成功 / 图层 / Zoom / HTTP / Size / 耗时`，失败显示明确原因，全程不冻结界面，Token 不出现在日志里

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

- 批量瓦片下载（当前只有单瓦片连通性验证）
- 并发下载 / 下载队列 / 断点续传
- MBTiles
- ADB / Android 导入
- 底图与注记图层合成（本版本只单独请求单层）

## 环境

Python 3.11+

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

测试在 Qt ``offscreen`` 平台下运行（见 `tests/conftest.py`），无需真实显示器。

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
│   ├── ui/                界面层：主窗口与三个面板
│   ├── providers/         瓦片源 Provider：base.py + tianditu.py（Slice 2 ✅）
│   ├── geo/               瓦片坐标计算：bounds.py + tile_math.py（Slice 1 ✅）
│   ├── downloader/        下载调度与断点续传（后续 Slice）
│   ├── mbtiles/           MBTiles 读写（后续 Slice）
│   ├── package/           离线包打包与校验（后续 Slice）
│   └── android/           ADB 设备管理与导入（后续 Slice）
├── tests/                  pytest 用例
├── docs/                   设计文档
├── scripts/capture_ui.py   界面截图留档脚本
└── requirements.txt
```

依赖方向单向：`ui -> geo/models -> config`；业务包之间通过 `models` / `geo` 传递数据，
不在 UI 层写业务逻辑。UI 需要瓦片数据时必须调用 `app.geo`，禁止复制公式。

## 用法：测试天地图连接

1. 在「Token」填入天地图控制台申请的服务密钥（`tk`，输入框默认隐藏，可勾选「显示」临时查看）。
2. 选择地图类型（矢量地图 / 卫星影像）、填好范围与 Min Zoom。
3. 点 `[测试天地图连接]`：程序取范围中心点，换算成瓦片编号，只请求 **一张** 瓦片做验证。

成功时「天地图连接」区显示图层、Zoom、HTTP 状态、响应体积与耗时；失败时给出明确原因
（`HTTP 403 — Token 可能无效…` / `请求超时` / `响应不是有效的图片瓦片…`），
并同步写入运行日志。日志中的 URL 一律脱敏（`tk=abcd****xyz`），不会泄露完整 Token。

## 后续计划

- Downloader（任务队列、暂停/继续/取消、断点续传）
- MBTiles（SQLite 落库、元数据表）
- 离线包打包与校验（Sha256、体积）
- GUI 下载管理（实时进度、速度、失败重试）
- ADB导入（设备发现、push、设备侧校验）
- Android / osmdroid（端侧读取离线包）

## 说明与注意事项

- 天地图服务需要自行申请 Token，请遵守天地图服务条款与调用配额限制。
  **不要把真实 Token 写进代码、README 或测试**；本仓库中的 Token 一律是占位假值。
- 生成的离线地图数据仅限授权范围内使用；如需对外发布或商用，请自行确认测绘资质与数据合规要求。
