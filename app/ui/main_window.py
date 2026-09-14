"""主窗口。

本文件只做三件事：**装配界面、连接信号、把用户动作转成日志与状态栏提示**。
任何计算 / 下载 / 打包 / 设备通信的逻辑都必须放到对应的业务包中，
通过 ``self.log_panel`` 汇报进展，禁止在此堆积实现细节。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import (
    APP_NAME,
    APP_VERSION,
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    WINDOW_MIN_HEIGHT,
    WINDOW_MIN_WIDTH,
)
from app.geo import (
    GeoBounds,
    calculate_tile_ranges,
    estimate_size_bytes,
    format_size,
)
from app.ui.device_panel import DevicePanel
from app.ui.download_panel import (
    ACTION_CALCULATE,
    ACTION_PAUSE,
    ACTION_RESUME,
    ACTION_START,
    DownloadPanel,
)
from app.ui.log_panel import LogPanel

#: 左侧配置列的偏好宽度（实际取值为它与"内容最小宽度"的较大者）
_LEFT_COLUMN_WIDTH = 460

#: 右侧设备/日志列的最小宽度
_RIGHT_COLUMN_WIDTH = 360

#: 各占位动作对应的"未来实现位置"说明
_ACTION_HINTS: dict[str, str] = {
    ACTION_CALCULATE: "按当前范围计算瓦片数量与预估容量（Slice 1 已实现）",
    ACTION_START: "瓦片下载器将在 Slice 2 实现",
    ACTION_PAUSE: "暂停 / 继续将在 Slice 2 随下载器一起实现",
    ACTION_RESUME: "暂停 / 继续将在 Slice 2 随下载器一起实现",
}


#: 下载状态区的行标题（键顺序即展示顺序）
_STATUS_LABELS: dict[str, str] = {
    "task_status": "任务状态",
    "current_zoom": "当前 Zoom",
    "tile_total": "瓦片总数",
    "tile_done": "已完成",
    "tile_failed": "失败数量",
    "speed": "下载速度",
}


class MainWindow(QMainWindow):
    """应用主窗口：1200x760，左右分栏布局。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.setWindowTitle(APP_NAME)
        self.resize(DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)
        self.setMinimumSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)

        # 三大面板 + 下载状态区
        self.download_panel = DownloadPanel(self)
        self.status_group = self._build_status_group()
        self.device_panel = DevicePanel(self)
        self.log_panel = LogPanel(self)

        self._build_menu_bar()
        self._build_central_widget()
        self._build_status_bar()
        self._connect_signals()

    # ------------------------------------------------------------------ #
    # 界面装配
    # ------------------------------------------------------------------ #
    def _build_menu_bar(self) -> None:
        file_menu = self.menuBar().addMenu("文件(&F)")

        quit_action = QAction("退出(&Q)", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu("帮助(&H)")
        about_action = QAction("关于(&A)", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)

    def _build_status_group(self) -> QGroupBox:
        """下载状态区。Slice 0 全部展示默认值，Slice 2 起由下载器驱动。"""
        self._status_values: dict[str, QLabel] = {}

        group = QGroupBox("下载状态", self)
        form = QFormLayout(group)
        for key, text in (
            ("task_status", "空闲"),
            ("current_zoom", "—"),
            ("tile_total", "0"),
            ("tile_done", "0"),
            ("tile_failed", "0"),
            ("speed", "0 瓦片/秒"),
        ):
            value_label = QLabel(text, group)
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            self._status_values[key] = value_label
            form.addRow(_STATUS_LABELS[key], value_label)

        self.progress_bar = QProgressBar(group)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("总进度 %p%")
        form.addRow("总进度", self.progress_bar)

        return group

    def _build_central_widget(self) -> None:
        # 左列：配置 + 状态，放在滚动区里以适配小屏 / 高 DPI
        left_content = QWidget(self)
        left_layout = QVBoxLayout(left_content)
        left_layout.addWidget(self.download_panel)
        left_layout.addWidget(self.status_group)
        left_layout.addStretch(1)

        left_scroll = QScrollArea(self)
        left_scroll.setWidgetResizable(True)
        left_scroll.setWidget(left_content)

        # 左列宽度自适应：取"偏好宽度"与"内容实际最小宽度"的较大值。
        # 控件最小宽度会随字体 / DPI / 本地化文案变化，写死常量容易出现横向滚动条。
        left_content.layout().activate()
        left_width = max(
            _LEFT_COLUMN_WIDTH,
            left_content.minimumSizeHint().width() + 2 * left_scroll.frameWidth() + 2,
        )
        left_scroll.setMinimumWidth(left_width)

        # 右列：设备（上）+ 日志（下），日志占主要空间
        right_splitter = QSplitter(Qt.Orientation.Vertical, self)
        right_splitter.addWidget(self.device_panel)
        right_splitter.addWidget(self.log_panel)
        right_splitter.setStretchFactor(0, 0)
        right_splitter.setStretchFactor(1, 1)
        right_splitter.setSizes([220, 480])

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(left_scroll)
        splitter.addWidget(right_splitter)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes(
            [left_width, max(DEFAULT_WINDOW_WIDTH - left_width, _RIGHT_COLUMN_WIDTH)]
        )

        self.splitter = splitter
        self.setCentralWidget(splitter)

    def _build_status_bar(self) -> None:
        self.statusBar().showMessage(f"就绪 · {APP_NAME} v{APP_VERSION}")

    def _connect_signals(self) -> None:
        self.download_panel.action_requested.connect(self._on_download_action)
        self.device_panel.refresh_requested.connect(self._on_refresh_devices)
        self.device_panel.import_requested.connect(self._on_import_to_android)

    # ------------------------------------------------------------------ #
    # 槽：下载动作
    # ------------------------------------------------------------------ #
    @Slot(str)
    def _on_download_action(self, action: str) -> None:
        hint = _ACTION_HINTS.get(action, "该动作尚未实现")
        self.log_panel.append_log(f"触发动作：{action} — {hint}")
        if action in (ACTION_CALCULATE, ACTION_START):
            self._log_form_snapshot()
        if action == ACTION_CALCULATE:
            self._run_tile_math()
            return
        self.statusBar().showMessage(hint, 5000)

    # ------------------------------------------------------------------ #
    # 槽：瓦片计算（Slice 1）
    # ------------------------------------------------------------------ #
    @Slot()
    def _run_tile_math(self) -> None:
        """把当前表单交给 ``app.geo`` 计算瓦片数量与预估容量。

        本方法只做"读表单 → 调 geo → 展示"，不复制任何瓦片公式。
        """
        data = self.download_panel.get_form_data()
        try:
            bounds = GeoBounds(
                west=data["west"],
                south=data["south"],
                east=data["east"],
                north=data["north"],
            )
            ranges = calculate_tile_ranges(bounds, data["min_zoom"], data["max_zoom"])
            total = sum(item.tile_count for item in ranges)
            size_text = format_size(estimate_size_bytes(total))
        except ValueError as exc:
            self._report_calculation_error(str(exc))
            return

        self.download_panel.set_calculation_result(total, size_text)
        self._status_values["tile_total"].setText(f"{total:,}")
        self.log_panel.append_log(
            f"瓦片计算完成：范围={bounds.as_tuple()}，"
            f"缩放={data['min_zoom']}~{data['max_zoom']}（{len(ranges)} 级），"
            f"预计瓦片 {total:,}，预计容量 {size_text}"
        )
        self.log_panel.append_log(
            "各级瓦片数：" + "，".join(f"z{r.zoom}={r.tile_count:,}" for r in ranges)
        )
        self.statusBar().showMessage(f"预计瓦片 {total:,} · 预计容量 {size_text}", 8000)

    def _report_calculation_error(self, message: str) -> None:
        """统一的错误出口：结果区 + 日志 + 状态栏 + 弹窗。"""
        self.download_panel.set_calculation_error(message)
        self.log_panel.append_log(f"瓦片计算失败：{message}", "ERROR")
        self.statusBar().showMessage(f"瓦片计算失败：{message}", 8000)
        self._show_error_dialog("瓦片计算失败", message)

    def _show_error_dialog(self, title: str, message: str) -> None:
        """非模态错误提示。

        刻意不用 ``QMessageBox.warning()``（它会阻塞事件循环，
        在无头测试环境里会直接把用例挂死）；这里用 ``show()`` 走非阻塞路径，
        错误信息同时已经落到结果区、日志和状态栏，不会被漏掉。
        """
        dialog = QMessageBox(QMessageBox.Icon.Warning, title, message, parent=self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def _log_form_snapshot(self) -> None:
        """把当前表单内容落到日志，验证 UI -> 数据模型 的读取链路。"""
        try:
            data = self.download_panel.get_form_data()
        except Exception as exc:  # pragma: no cover - 防御性分支
            self.log_panel.append_log(f"读取表单失败：{exc}", "ERROR")
            return

        token = "已填写" if data["token"] else "未填写"
        bounds = (data["west"], data["south"], data["east"], data["north"])
        self.log_panel.append_log(
            "当前配置："
            f"token={token}，"
            f"地图类型={data['map_type'].value}，"
            f"中文注记={'是' if data['with_labels'] else '否'}，"
            f"范围={bounds}，"
            f"缩放={data['min_zoom']}~{data['max_zoom']}，"
            f"输出={data['output_dir']}"
        )

    # ------------------------------------------------------------------ #
    # 槽：设备动作
    # ------------------------------------------------------------------ #
    @Slot()
    def _on_refresh_devices(self) -> None:
        message = "刷新设备：Slice 0 不执行 adb，设备发现将在 Slice 4 实现"
        self.log_panel.append_log(message)
        self.device_panel.clear_devices()
        self.statusBar().showMessage(message, 5000)

    @Slot()
    def _on_import_to_android(self) -> None:
        message = "导入到 Android：需要先生成离线包，Slice 4 实现"
        self.log_panel.append_log(message, "WARNING")
        self.statusBar().showMessage(message, 5000)

    # ------------------------------------------------------------------ #
    # 槽：帮助
    # ------------------------------------------------------------------ #
    @Slot()
    def _on_about(self) -> None:
        QMessageBox.about(
            self,
            f"关于 {APP_NAME}",
            f"<b>{APP_NAME}</b><br>版本 {APP_VERSION}<br><br>"
            "Windows 端生成天地图离线地图包，后续导入 Android 供 osmdroid 使用。<br>"
            "当前进度：Slice 0（工程骨架）。",
        )
