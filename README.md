# 天地图离线地图工具

## 项目目标

Windows端生成天地图离线地图包，后续导入Android供osmdroid使用。

## 当前进度

Slice 0：工程骨架

- 可运行的 PySide6 主窗口（分区布局：地图配置 / 下载状态 / Android设备 / 运行日志）
- 基础数据模型：图元类型、下载任务、离线包、Android 设备
- 统一配置项 `app/config/settings.py`
- 尚未实现：瓦片下载、MBTiles、ADB

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

## 目录结构

```text
tianditu-offline-tool/
├── app/
│   ├── main.py            入口（日志初始化 + QApplication + MainWindow）
│   ├── config/settings.py 全局常量，唯一魔数来源
│   ├── models/            纯数据模型（dataclass / enum），不依赖 UI
│   ├── ui/                界面层：主窗口与三个面板
│   ├── providers/         瓦片源 Provider（Slice 1+）
│   ├── geo/               瓦片编号 / 经纬度换算（Slice 1+）
│   ├── downloader/        下载调度与断点续传（Slice 2+）
│   ├── mbtiles/           MBTiles 读写（Slice 3+）
│   ├── package/           离线包打包与校验（Slice 3+）
│   └── android/           ADB 设备管理与导入（Slice 4+）
├── tests/                  pytest 用例
├── docs/                   设计文档
├── scripts/               辅助脚本
└── requirements.txt
```

依赖方向单向：`ui -> models -> config`；业务包之间通过 `models` 传递数据，
不在 UI 层写业务逻辑。

## 后续计划

- Tile Math（经纬度 ↔ 瓦片编号、范围与总数计算）
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
