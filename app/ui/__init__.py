"""界面层（PySide6）。

分工约定：
- ``download_panel``      地图配置 / 下载区域，负责收集输入（``get_form_data``），
                          以及按状态矩阵切换动作按钮
- ``task_progress_panel`` 当前任务的实时进度（Slice 7）
- ``task_panel``          任务列表与选中任务信息（Slice 5 / Slice 7）
- ``device_panel``        Android 设备区域，被动接收设备列表
- ``log_panel``           运行日志区域，``append_log`` 可跨线程投递
- ``main_window``         只做装配与信号连接

**线程边界**：界面层不直接做 IO。三件重活各有自己的 ``QThread``：
``connection_check`` 发 HTTP、``download_worker`` 跑完整下载编排、
``tile_scan_worker`` 查 SQLite（Slice 7）。

界面层允许依赖 ``models`` / ``config``；业务包（providers、geo、downloader、
composer、mbtiles、package、android）不得依赖本层。
"""

from app.ui.device_panel import DevicePanel
from app.ui.download_panel import DownloadPanel
from app.ui.log_panel import LogPanel
from app.ui.main_window import MainWindow
from app.ui.task_panel import TaskPanel
from app.ui.task_progress_panel import TaskProgressPanel

__all__ = [
    "MainWindow",
    "DownloadPanel",
    "DevicePanel",
    "LogPanel",
    "TaskPanel",
    "TaskProgressPanel",
]
