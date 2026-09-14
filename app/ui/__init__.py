"""界面层（PySide6）。

分工约定：
- ``download_panel``  地图配置 / 下载区域，负责收集输入（``get_form_data``）
- ``device_panel``    Android 设备区域，被动接收设备列表
- ``log_panel``       运行日志区域，``append_log`` 可跨线程投递
- ``main_window``     只做装配与信号连接

界面层允许依赖 ``models`` / ``config``；业务包（providers、geo、downloader、
mbtiles、package、android）不得依赖本层。
"""

from app.ui.device_panel import DevicePanel
from app.ui.download_panel import DownloadPanel
from app.ui.log_panel import LogPanel
from app.ui.main_window import MainWindow

__all__ = ["MainWindow", "DownloadPanel", "DevicePanel", "LogPanel"]
