# 天地图离线地图工具

## 项目目标

Windows端生成天地图离线地图包，后续导入Android供osmdroid使用。

## 当前进度

Slice 1：Web Mercator Tile Math

已支持：

- **GeoBounds** —— 带校验的经纬度包围盒（经度 ±180、纬度 ±85.05112878、`west < east`、`south < north`，非法参数抛 `ValueError`）
- **经纬度 → XYZ 瓦片编号** —— `lon_to_tile_x` / `lat_to_tile_y`，支持 zoom 0~22，纬度自动 clamp 到 Web 墨卡托极限
- **BoundingBox → TileRange** —— 单级缩放的瓦片编号区间与 `tile_count`
- **XYZ ↔ TMS 转换** —— `xyz_y_to_tms_y` / `tms_y_to_xyz_y`（MBTiles 前置）
- **Zoom 范围瓦片统计** —— `calculate_tile_ranges` / `estimate_tile_count`
- **容量估算** —— `estimate_size_bytes` / `format_size`

Slice 0 基础（仍有效）：

- 可运行的 PySide6 主窗口（分区布局：地图配置 / 下载状态 / Android设备 / 运行日志）
- 基础数据模型：图元类型、下载任务、离线包、Android 设备
- 统一配置项 `app/config/settings.py`
- 「计算」按钮已接入 Tile Math：读取表单范围与缩放，显示预计瓦片数量与预计容量，非法输入不崩溃

暂未实现：

- 天地图网络请求（无 HTTP 客户端、无 Token 校验）
- 瓦片下载
- MBTiles
- ADB

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
│   ├── providers/         瓦片源 Provider（Slice 2+）
│   ├── geo/               瓦片坐标计算：bounds.py + tile_math.py（Slice 1 ✅）
│   ├── downloader/        下载调度与断点续传（Slice 2+）
│   ├── mbtiles/           MBTiles 读写（Slice 3+）
│   ├── package/           离线包打包与校验（Slice 3+）
│   └── android/           ADB 设备管理与导入（Slice 4+）
├── tests/                  pytest 用例
├── docs/                   设计文档
├── scripts/capture_ui.py   界面截图留档脚本
└── requirements.txt
```

依赖方向单向：`ui -> geo/models -> config`；业务包之间通过 `models` / `geo` 传递数据，
不在 UI 层写业务逻辑。UI 需要瓦片数据时必须调用 `app.geo`，禁止复制公式。

## 后续计划

- Tianditu Provider（WMTS URL 模板、并发与重试）
- Downloader（任务队列、暂停/继续/取消）
- MBTiles（SQLite 落库、元数据表）
- 断点续传（失败清单持久化）
- GUI 下载管理（实时进度、速度、失败重试）
- ADB导入（设备发现、push、设备侧校验）
- Android / osmdroid（端侧读取离线包）

## 说明与注意事项

- 天地图服务需要自行申请 Token，请遵守天地图服务条款与调用配额限制。
- 生成的离线地图数据仅限授权范围内使用；如需对外发布或商用，请自行确认测绘资质与数据合规要求。
