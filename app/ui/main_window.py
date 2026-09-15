"""主窗口。

本文件只做三件事：**装配界面、连接信号、把用户动作转成日志与状态栏提示**。
任何计算 / 下载 / 打包 / 设备通信的逻辑都必须放到对应的业务包中，
通过 ``self.log_panel`` 汇报进展，禁止在此堆积实现细节。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
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
    DEFAULT_DOWNLOAD_CONCURRENCY,
    DEFAULT_MAX_RETRIES,
    DEFAULT_REQUESTS_PER_SECOND,
    DEFAULT_TILE_TIMEOUT_SECONDS,
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    GUI_TEST_MAX_TILES,
    UI_TEXT_TEST_LIMIT_EXCEEDED,
    WINDOW_MIN_HEIGHT,
    WINDOW_MIN_WIDTH,
)
from app.downloader import DownloadProgress, DownloadSummary
from app.geo import (
    GeoBounds,
    calculate_tile_ranges,
    estimate_size_bytes,
    estimate_tile_count,
    format_size,
    lat_to_tile_y,
    lon_to_tile_x,
)
from app.models.map_layer import MapLayerType
from app.providers import TiandituProvider, TileTestResult, mask_url
from app.ui.connection_check import ConnectionTestWorker
from app.ui.device_panel import DevicePanel
from app.ui.download_panel import (
    ACTION_CALCULATE,
    ACTION_PAUSE,
    ACTION_RESUME,
    ACTION_START,
    ACTION_TEST_CONNECTION,
    DownloadPanel,
)
from app.ui.download_worker import DownloadWorker
from app.ui.log_panel import LogPanel

#: 左侧配置列的偏好宽度（实际取值为它与"内容最小宽度"的较大者）
_LEFT_COLUMN_WIDTH = 460

#: 右侧设备/日志列的最小宽度
_RIGHT_COLUMN_WIDTH = 360

#: 关闭窗口时等待后台线程退出的上限（毫秒）
_THREAD_SHUTDOWN_WAIT_MS = 3000

#: 关闭窗口时等待下载任务取消的上限（毫秒）。超时后就不再等，避免关窗卡死。
_DOWNLOAD_CANCEL_WAIT_MS = 5000

#: 各动作对应的说明（下载相关动作在 Slice 3 已实现）
_ACTION_HINTS: dict[str, str] = {
    ACTION_CALCULATE: "按当前范围计算瓦片数量与预估容量（Slice 1 已实现）",
    ACTION_TEST_CONNECTION: "取范围中心点测试单瓦片连通性（Slice 2 已实现）",
    ACTION_START: "启动批量下载引擎（Slice 3 已实现）",
    ACTION_PAUSE: "暂停下载：不再发起新请求，已发出的请求允许完成（Slice 3 已实现）",
    ACTION_RESUME: "继续下载（Slice 3 已实现）",
}


#: 下载状态区的行标题（键顺序即展示顺序）
_STATUS_LABELS: dict[str, str] = {
    "task_status": "任务状态",
    "current_zoom": "当前 Zoom",
    "tile_total": "瓦片总数",
    "tile_done": "已完成",
    "tile_succeeded": "成功",
    "tile_failed": "失败数量",
    "tile_skipped": "已跳过",
    "speed": "下载速度",
}

#: 下载状态区的初始值
_STATUS_DEFAULTS: dict[str, str] = {
    "task_status": "空闲",
    "current_zoom": "—",
    "tile_total": "0",
    "tile_done": "0",
    "tile_succeeded": "0",
    "tile_failed": "0",
    "tile_skipped": "0",
    "speed": "0 瓦片/秒",
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

        #: 后台连通性测试线程（同一时刻最多一个；持有引用避免被 GC 提前回收）
        self._connection_worker: ConnectionTestWorker | None = None
        #: 最近一次测试的上下文，用于结果展示：(图层, zoom, x, y, 脱敏 URL)
        self._connection_context: tuple[MapLayerType, int, int, int, str] | None = None

        #: 后台下载线程（同一时刻最多一个）
        self._download_worker: DownloadWorker | None = None

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
        """下载状态区。Slice 0 全部展示默认值，Slice 3 起由下载器驱动。"""
        self._status_values: dict[str, QLabel] = {}

        group = QGroupBox("下载状态", self)
        form = QFormLayout(group)
        for key, text in _STATUS_DEFAULTS.items():
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
        if action == ACTION_TEST_CONNECTION:
            self._start_connection_test()
            return
        if action == ACTION_START:
            self._start_download()
            return
        if action == ACTION_PAUSE:
            self._pause_download()
            return
        if action == ACTION_RESUME:
            self._resume_download()
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
    # 槽：天地图连通性测试（Slice 2）
    # ------------------------------------------------------------------ #
    @Slot()
    def _start_connection_test(self) -> None:
        """取当前范围中心点，对单张瓦片做一次连通性测试。

        链路：读表单 → ``app.geo`` 算中心点瓦片 → ``app.providers`` 造 URL 与
        发请求（在后台线程）→ 回主线程展示。UI 层不拼接任何 URL 参数。
        """
        running = self._connection_worker
        if running is not None and running.isRunning():
            self.log_panel.append_log("上一次连通性测试尚未结束，已忽略本次请求", "WARNING")
            return

        data = self.download_panel.get_form_data()
        try:
            provider = TiandituProvider(data["token"])
            # 中文注记由复选框控制，本 Slice 只测底图（注记图层可在后续版本单列入口）
            layer = data["map_type"].base_layer
            zoom = int(data["min_zoom"])
            bounds = GeoBounds(
                west=data["west"],
                south=data["south"],
                east=data["east"],
                north=data["north"],
            )
            center_lat, center_lon = bounds.center
            x = lon_to_tile_x(center_lon, zoom)
            y = lat_to_tile_y(center_lat, zoom)
            url = mask_url(provider.build_tile_url(layer, zoom, x, y))
        except ValueError as exc:
            self._report_connection_error(str(exc))
            return

        self._connection_context = (layer, zoom, x, y, url)
        self.download_panel.set_connection_testing(True)
        self.log_panel.append_log(
            f"开始测试天地图连接：图层={layer.value}，Zoom={zoom}，"
            f"中心点瓦片=({x}, {y})，超时={DEFAULT_TILE_TIMEOUT_SECONDS:.0f}s"
        )
        # URL 已在本层脱敏（不显示完整 Token）
        self.log_panel.append_log(f"请求 URL：{url}")
        self.statusBar().showMessage("正在测试天地图连接…")

        worker = ConnectionTestWorker(
            provider,
            layer,
            zoom,
            x,
            y,
            timeout=DEFAULT_TILE_TIMEOUT_SECONDS,
            parent=self,
        )
        worker.result_ready.connect(self._on_connection_result)
        self._connection_worker = worker
        worker.start()

    @Slot(object)
    def _on_connection_result(self, result: TileTestResult) -> None:
        """后台线程测试结束，回到主线程更新界面。"""
        self.download_panel.set_connection_testing(False)
        layer, zoom, _x, _y, _url = self._connection_context or (None, None, 0, 0, "")
        layer_text = layer.value if layer is not None else "—"
        zoom_text = str(zoom) if zoom is not None else "—"

        if result.success:
            self.download_panel.set_connection_result(
                [
                    "连接成功",
                    f"图层：{layer_text}",
                    f"Zoom：{zoom_text}",
                    f"HTTP：{result.status_code}",
                    f"Size：{result.content_length} bytes",
                    f"耗时：{result.elapsed_ms:.0f} ms",
                ]
            )
            self.log_panel.append_log(
                f"天地图连接测试成功：图层={layer_text}，Zoom={zoom_text}，"
                f"HTTP={result.status_code}，体积={result.content_length} bytes"
                f"（{result.content_type or '未知类型'}），耗时={result.elapsed_ms:.0f} ms"
            )
            self.statusBar().showMessage(
                f"天地图连接正常 · HTTP {result.status_code} · {result.elapsed_ms:.0f} ms",
                8000,
            )
            return

        error = result.error or "未知错误"
        lines = [f"连接失败：{error}", f"图层：{layer_text}", f"Zoom：{zoom_text}"]
        if result.has_response:
            lines.append(f"HTTP：{result.status_code}")
        lines.append(f"耗时：{result.elapsed_ms:.0f} ms")
        self.download_panel.set_connection_result(lines)

        self.log_panel.append_log(f"天地图连接测试失败：{error}（{result.summary()}）", "ERROR")
        self.statusBar().showMessage(f"天地图连接失败：{error}", 8000)
        self._show_error_dialog("天地图连接失败", error)

    def _report_connection_error(self, message: str) -> None:
        """请求未能发起的错误出口（表单非法 / Token 为空）。"""
        self.download_panel.set_connection_testing(False)
        self.download_panel.set_connection_error(message)
        self.log_panel.append_log(f"天地图连接测试无法发起：{message}", "ERROR")
        self.statusBar().showMessage(f"天地图连接测试无法发起：{message}", 8000)
        self._show_error_dialog("天地图连接测试失败", message)

    # ------------------------------------------------------------------ #
    # 槽：批量下载（Slice 3）
    # ------------------------------------------------------------------ #
    @Slot()
    def _start_download(self) -> None:
        """启动下载引擎。

        链路：读表单 → ``app.geo`` 统计瓦片数 → 安全上限检查 →
        ``app.downloader`` 在后台线程跑 → 进度/结果回主线程展示。

        本方法不含任何瓦片公式、URL 拼接或并发逻辑。
        """
        running = self._download_worker
        if running is not None and running.isRunning():
            self.log_panel.append_log("上一次下载尚未结束，已忽略本次请求", "WARNING")
            return

        data = self.download_panel.get_form_data()
        try:
            provider = TiandituProvider(data["token"])
            bounds = GeoBounds(
                west=data["west"],
                south=data["south"],
                east=data["east"],
                north=data["north"],
            )
            layer = data["map_type"].base_layer
            min_zoom = int(data["min_zoom"])
            max_zoom = int(data["max_zoom"])
            # 先算总量再决定要不要开工——不能"跑起来才发现太大"
            total = estimate_tile_count(bounds, min_zoom, max_zoom)
        except ValueError as exc:
            self._report_download_error(str(exc), dialog_title="无法开始下载")
            return

        if total > GUI_TEST_MAX_TILES:
            self._report_test_limit_exceeded(total)
            return

        self.download_panel.set_download_running(True, paused=False)
        self._reset_status_for_download(total)
        self.log_panel.append_log(
            f"开始下载：图层={layer.value}，范围={bounds.as_tuple()}，"
            f"缩放={min_zoom}~{max_zoom}，瓦片总数={total:,}，"
            f"并发={DEFAULT_DOWNLOAD_CONCURRENCY}，"
            f"限速={DEFAULT_REQUESTS_PER_SECOND:g} 请求/秒，"
            f"超时={DEFAULT_TILE_TIMEOUT_SECONDS:g}s，"
            f"最大尝试={DEFAULT_MAX_RETRIES} 次"
        )
        self.log_panel.append_log(
            f"注意：本 Slice 为下载引擎测试模式，共 {total:,} 张瓦片"
            f"（上限 {GUI_TEST_MAX_TILES} 张），瓦片不会写入磁盘。",
            "WARNING",
        )
        self.statusBar().showMessage(f"下载中… 0/{total:,}")

        worker = DownloadWorker(
            provider,
            bounds,
            min_zoom,
            max_zoom,
            layer,
            concurrency=DEFAULT_DOWNLOAD_CONCURRENCY,
            timeout=DEFAULT_TILE_TIMEOUT_SECONDS,
            parent=self,
        )
        worker.progress_changed.connect(self._on_download_progress)
        worker.completed.connect(self._on_download_finished)
        worker.failed.connect(self._on_download_failed)
        self._download_worker = worker
        worker.start()

    def _report_test_limit_exceeded(self, total: int) -> None:
        """区域超出 Slice 3 的测试上限：明确告知，**不做静默截断**。"""
        self.log_panel.append_log(
            f"下载未开始：当前范围共 {total:,} 张瓦片，"
            f"超过 Slice 3 测试上限 {GUI_TEST_MAX_TILES} 张。",
            "ERROR",
        )
        for line in UI_TEXT_TEST_LIMIT_EXCEEDED.splitlines():
            self.log_panel.append_log(line)
        self.statusBar().showMessage("下载未开始：超出测试上限", 8000)
        self._show_error_dialog("超出测试上限", UI_TEXT_TEST_LIMIT_EXCEEDED)

    def _reset_status_for_download(self, total: int) -> None:
        """开工前把状态区清成"从零开始"，避免上一轮的残留数字造成误读。"""
        self._status_values["task_status"].setText("下载中")
        self._status_values["current_zoom"].setText("—")
        self._status_values["tile_total"].setText(f"{total:,}")
        self._status_values["tile_done"].setText("0")
        self._status_values["tile_succeeded"].setText("0")
        self._status_values["tile_failed"].setText("0")
        self._status_values["tile_skipped"].setText("0")
        self._status_values["speed"].setText("0 瓦片/秒")
        self.progress_bar.setValue(0)

    @Slot(object)
    def _on_download_progress(self, progress: DownloadProgress) -> None:
        """后台进度更新（已由引擎节流到约 6 次/秒），回主线程刷界面。"""
        values = self._status_values
        values["tile_total"].setText(f"{progress.total:,}")
        values["tile_done"].setText(f"{progress.completed:,}")
        values["tile_succeeded"].setText(f"{progress.succeeded:,}")
        values["tile_failed"].setText(f"{progress.failed:,}")
        values["tile_skipped"].setText(f"{progress.skipped:,}")
        values["speed"].setText(f"{progress.tiles_per_second:.1f} 瓦片/秒")
        if progress.current_zoom is not None:
            values["current_zoom"].setText(str(progress.current_zoom))
        self.progress_bar.setValue(int(progress.percent))
        self.statusBar().showMessage(
            f"下载中… {progress.completed:,}/{progress.total:,}"
            f"（成功 {progress.succeeded:,} · 失败 {progress.failed:,}）"
        )

    @Slot(object)
    def _on_download_finished(self, summary: DownloadSummary) -> None:
        """下载结束，展示汇总并恢复按钮状态。"""
        self._download_worker = None
        self.download_panel.set_download_running(False)

        status_text = "已取消" if summary.cancelled else (
            "已完成" if summary.failed == 0 else "已完成（有失败）"
        )
        self._status_values["task_status"].setText(status_text)
        self.progress_bar.setValue(int(min(100.0, summary.total and
                                            summary.completed / summary.total * 100.0 or 0)))

        for line in summary.as_lines():
            self.log_panel.append_log(line)

        if summary.cancelled:
            self.log_panel.append_log(
                f"下载已取消：已下载 {summary.completed:,} 张，"
                f"跳过 {summary.skipped:,} 张（本 Slice 不支持断点续传，不会补下）",
                "WARNING",
            )
            self.statusBar().showMessage("下载已取消", 8000)
            return

        level = "INFO" if summary.failed == 0 else "WARNING"
        self.log_panel.append_log(
            f"下载完成：成功 {summary.succeeded:,} 张，失败 {summary.failed:,} 张，"
            f"共 {format_size(summary.bytes_downloaded)}，"
            f"耗时 {summary.duration_seconds:.2f}s，"
            f"平均 {summary.average_tiles_per_second:.1f} 瓦片/秒。"
            "（瓦片未落盘，Slice 4 起写入 MBTiles）",
            level,
        )
        self.statusBar().showMessage(
            f"下载完成：成功 {summary.succeeded:,} · 失败 {summary.failed:,}"
            f" · {summary.duration_seconds:.2f}s",
            10000,
        )

    @Slot(str)
    def _on_download_failed(self, message: str) -> None:
        """下载线程异常终止（不应发生，但必须有出口）。"""
        self._download_worker = None
        self.download_panel.set_download_running(False)
        self._status_values["task_status"].setText("失败")
        self._report_download_error(message, dialog_title="下载失败")

    def _report_download_error(self, message: str, *, dialog_title: str) -> None:
        """下载相关的错误出口：日志 + 状态栏 + 非模态弹窗。"""
        self.log_panel.append_log(f"{dialog_title}：{message}", "ERROR")
        self.statusBar().showMessage(f"{dialog_title}：{message}", 8000)
        self._show_error_dialog(dialog_title, message)

    @Slot()
    def _pause_download(self) -> None:
        """暂停：不再发起新请求，已发出的请求允许完成。"""
        worker = self._download_worker
        if worker is None or not worker.isRunning():
            self.log_panel.append_log("当前没有正在运行的下载任务", "WARNING")
            self.statusBar().showMessage("当前没有正在运行的下载任务", 5000)
            return
        worker.pause()
        self.download_panel.set_download_running(True, paused=True)
        self._status_values["task_status"].setText("已暂停")
        self.log_panel.append_log("下载已暂停（已发出的请求会正常完成）")
        self.statusBar().showMessage("下载已暂停")

    @Slot()
    def _resume_download(self) -> None:
        """继续：放行闸门。"""
        worker = self._download_worker
        if worker is None or not worker.isRunning():
            self.log_panel.append_log("当前没有可继续的下载任务", "WARNING")
            self.statusBar().showMessage("当前没有可继续的下载任务", 5000)
            return
        worker.resume()
        self.download_panel.set_download_running(True, paused=False)
        self._status_values["task_status"].setText("下载中")
        self.log_panel.append_log("下载已继续")
        self.statusBar().showMessage("下载已继续")

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
            "当前进度：Slice 3（异步瓦片下载引擎；瓦片暂不落盘）。",
        )

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名约定
        """关闭前取消下载并等待后台线程结束。

        否则 ``QThread`` 可能仍在运行时被销毁，Qt 会直接中止进程。
        """
        worker = self._connection_worker
        if worker is not None and worker.isRunning():
            self.log_panel.append_log("等待连接测试线程结束…")
            worker.wait(_THREAD_SHUTDOWN_WAIT_MS)

        download = self._download_worker
        if download is not None and download.isRunning():
            self.log_panel.append_log("正在取消下载任务并等待后台线程结束…")
            download.cancel()
            if not download.wait(_DOWNLOAD_CANCEL_WAIT_MS):
                # 极端情况下（网络卡住）不能再等，否则关窗会一直挂住。
                self.log_panel.append_log(
                    "后台下载线程未能及时结束，已放弃等待", "WARNING"
                )
        self._download_worker = None
        super().closeEvent(event)
