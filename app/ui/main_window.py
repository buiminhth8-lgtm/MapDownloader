"""主窗口。

本文件只做三件事：**装配界面、连接信号、把用户动作转成日志与状态栏提示**。
任何计算 / 下载 / 打包 / 设备通信的逻辑都必须放到对应的业务包中，
通过 ``self.log_panel`` 汇报进展，禁止在此堆积实现细节。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

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
    QStyle,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import (
    APP_NAME,
    APP_VERSION,
    DEFAULT_DOWNLOAD_CONCURRENCY,
    DEFAULT_MAX_RETRIES,
    DEFAULT_REQUESTS_PER_SECOND,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_TILE_TIMEOUT_SECONDS,
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    GUI_LARGE_TASK_CONFIRM_TILES,
    UI_TEXT_CANCELLED_KEEP_TILES,
    UI_TEXT_EXIT_CANCEL,
    UI_TEXT_EXIT_CONFIRM,
    UI_TEXT_EXIT_WITH_TASK,
    UI_TEXT_EXIT_WITH_TASK_TITLE,
    UI_TEXT_LARGE_TASK_CONFIRM,
    UI_TEXT_LARGE_TASK_CONFIRM_TITLE,
    UI_TEXT_MAP_NAME_REQUIRED,
    UI_TEXT_MBTILES_EXISTS,
    UI_TEXT_MBTILES_EXISTS_CANCEL,
    UI_TEXT_MBTILES_EXISTS_CONTINUE,
    UI_TEXT_MBTILES_EXISTS_RESTART,
    UI_TEXT_MBTILES_EXISTS_TITLE,
    UI_TEXT_MBTILES_RESTART_CONFIRM,
    UI_TEXT_MBTILES_RESTART_TITLE,
    UI_TEXT_RESUME_TOKEN_REQUIRED,
    UI_TEXT_TASK_DELETE_HINT,
    UI_TEXT_TASK_RECOVERED,
    WINDOW_MIN_HEIGHT,
    WINDOW_MIN_WIDTH,
)
from app.downloader import DownloadProgress, ResumePolicy
from app.geo import (
    GeoBounds,
    calculate_tile_ranges,
    estimate_size_bytes,
    estimate_tile_count,
    format_size,
    lat_to_tile_y,
    lon_to_tile_x,
)
from app.models.download_task import DownloadTask, DownloadTaskStatus
from app.models.map_layer import MapLayerType
from app.providers import TiandituProvider, TileTestResult, mask_url
from app.services import (
    MapDownloadOutcome,
    build_output_path,
    count_existing_tiles,
)
from app.tasks import TaskService, TaskStoreError
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
from app.ui.task_panel import TaskPanel

#: 左侧配置列的偏好宽度（实际取值为它与"内容最小宽度"的较大者）
_LEFT_COLUMN_WIDTH = 460

#: 右侧设备 / 任务 / 日志列的最小宽度。任务表格有 5 列，360 会让「输出路径」
#: 只剩几十像素，因此抬到 420（900px 的最小窗口宽度仍能满足 460 + 420）。
_RIGHT_COLUMN_WIDTH = 420

#: 关闭窗口时等待后台线程退出的上限（毫秒）
_THREAD_SHUTDOWN_WAIT_MS = 3000

#: 关闭窗口时等待下载任务取消的上限（毫秒）。超时后就不再等，避免关窗卡死。
_DOWNLOAD_CANCEL_WAIT_MS = 5000


def _backup_path(path: Path) -> Path:
    """给一个已有文件取"不会撞车"的备份路径。

    形如 ``beijing.mbtiles.bak-20260915-095015``；同一秒内重复调用会追加
    序号，因此**不会**出现"备份把上一个备份覆盖掉"这种静默数据丢失。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{counter}")
        counter += 1
    return candidate

#: 各动作对应的说明（下载相关动作在 Slice 3 已实现，Slice 4 起落盘为 MBTiles，
#: Slice 5 起支持断点续传与任务恢复）
_ACTION_HINTS: dict[str, str] = {
    ACTION_CALCULATE: "按当前范围计算瓦片数量与预估容量（Slice 1 已实现）",
    ACTION_TEST_CONNECTION: "取范围中心点测试单瓦片连通性（Slice 2 已实现）",
    ACTION_START: "下载瓦片并写入 MBTiles（Slice 4；已有文件会提示续传，Slice 5）",
    ACTION_PAUSE: "暂停下载：不再发起新请求，已发出的请求允许完成（Slice 3 已实现）",
    ACTION_RESUME: "继续下载：放行当前任务的闸门（Slice 3；跨进程续传见「下载任务」）",
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
    "downloaded_bytes": "已下载字节",
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
    "downloaded_bytes": "0 B",
}


@dataclass(frozen=True)
class _DownloadPlan:
    """通过校验、等待启动的一次下载请求。

    存在的理由有两个：

    1. 大任务确认框与"已有文件怎么办"选择框都是**异步**的（非模态弹窗，
       不能阻塞事件循环），所以"校验完"和"真正启动"之间必须有个东西把参数托住；
    2. 全新下载与续传**共用同一条启动路径**，差别只在这几个字段。

    Attributes:
        resume_task: 非 ``None`` 表示"继续这个已有任务"——启动时走
            ``MapDownloadService.resume_task``，参数以任务记录为准。
        task: 需要被持续落盘的任务记录（全新下载时由 UI 先创建）。
        resume_policy: 跳过已有瓦片的策略；``None`` = 不跳过。
        existing_tiles: 启动前检测到的已有瓦片数（仅用于提示）。
        resume_existing: 用户对"已有文件"选择了**继续下载**——复用该文件，
            但任务记录本身是这一轮新建的（不是从任务列表里恢复的）。
        effective_tiles: 预计**真正要下载**的张数（用于大任务确认的阈值判断）。
            续传时它是"目标 - 库里已有"，否则等于 ``total``。
    """

    provider: TiandituProvider
    bounds: GeoBounds
    min_zoom: int
    max_zoom: int
    layer: MapLayerType
    output_path: Path
    map_name: str
    description: str
    total: int
    size_text: str
    resume_policy: ResumePolicy | None = None
    resume_task: DownloadTask | None = None
    task: DownloadTask | None = None
    existing_tiles: int = 0
    resume_existing: bool = False
    effective_tiles: int = 0

    @property
    def is_resume(self) -> bool:
        """是否走"继续某个已存在的任务记录"入口。"""
        return self.resume_task is not None

    @property
    def skips_existing(self) -> bool:
        """本次启动是否会跳过已有瓦片。"""
        return self.is_resume or self.resume_existing


class MainWindow(QMainWindow):
    """应用主窗口：1200x760，左右分栏布局。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        task_service: TaskService | None = None,
    ) -> None:
        """
        Args:
            parent: Qt 父对象。
            task_service: 任务服务；``None`` 时用默认的
                ``~/MapDownloader/data/tasks.json``。
                测试会注入一个指向临时目录的实例，避免污染用户主目录。
        """
        super().__init__(parent)

        self.setWindowTitle(APP_NAME)
        self.resize(DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)
        self.setMinimumSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)

        #: 任务持久化服务（Slice 5）
        self.task_service = task_service or TaskService()

        # 四大面板 + 下载状态区
        self.download_panel = DownloadPanel(self)
        self.status_group = self._build_status_group()
        self.device_panel = DevicePanel(self)
        self.task_panel = TaskPanel(self)
        self.log_panel = LogPanel(self)

        #: 后台连通性测试线程（同一时刻最多一个；持有引用避免被 GC 提前回收）
        self._connection_worker: ConnectionTestWorker | None = None
        #: 最近一次测试的上下文，用于结果展示：(图层, zoom, x, y, 脱敏 URL)
        self._connection_context: tuple[MapLayerType, int, int, int, str] | None = None

        #: 后台下载线程（同一时刻最多一个）
        self._download_worker: DownloadWorker | None = None
        #: 正在进行的任务记录（用于收尾时写状态、关窗时写 PAUSED）
        self._current_task: DownloadTask | None = None
        #: 等待用户确认的下载计划（大任务确认 / 已有文件选择，都是非模态）
        self._pending_plan: _DownloadPlan | None = None
        #: 非模态弹窗实例（持有引用避免被 GC 提前回收）
        self._confirm_dialog: QMessageBox | None = None
        self._exists_dialog: QMessageBox | None = None
        self._restart_dialog: QMessageBox | None = None
        self._exit_dialog: QMessageBox | None = None
        #: 关窗确认结果（Spec §17：先提示，用户确认后才真正退出）
        self._exit_confirmed = False
        #: 最近一次"计算"的结果，用于输出路径变化时重绘「输出文件」行
        self._last_calculation: tuple[int, str] | None = None

        self._build_menu_bar()
        self._build_central_widget()
        self._build_status_bar()
        self._connect_signals()
        self._recover_tasks()

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
        #: 供 ``_reveal_download_result`` 把结果区滚进可视范围
        self._left_scroll = left_scroll

        # 左列宽度自适应：取"偏好宽度"与"内容实际最小宽度"的较大值。
        # 控件最小宽度会随字体 / DPI / 本地化文案变化，写死常量容易出现横向滚动条。
        left_content.layout().activate()
        #: 竖直滚动条会吃掉的可视宽度。不预留的话，一旦内容高于窗口（纵向滚动条
        #: 出现），可视宽度立刻少 ~17px，横向滚动条就必然跟着冒出来——两者互相
        #: 触发、一起常驻。Slice 4 内容变高后踩到的就是这条。
        scrollbar_allowance = left_scroll.style().pixelMetric(
            QStyle.PixelMetric.PM_ScrollBarExtent
        )
        left_width = (
            max(
                _LEFT_COLUMN_WIDTH,
                left_content.minimumSizeHint().width() + 2 * left_scroll.frameWidth() + 2,
            )
            + scrollbar_allowance
        )
        left_scroll.setMinimumWidth(left_width)

        # 右列：设备（上）+ 任务（中）+ 日志（下），日志占主要空间。
        # 任务列表刻意放右列而不是配置列：配置列在 760px 高的窗口里已经排到折叠线，
        # 再塞一块 180px 高的表就会把「总进度」挤到可视区外（Slice 4 花过力气
        # 保证进度条可见，不能在这里破功）。
        right_splitter = QSplitter(Qt.Orientation.Vertical, self)
        right_splitter.addWidget(self.device_panel)
        right_splitter.addWidget(self.task_panel)
        right_splitter.addWidget(self.log_panel)
        right_splitter.setStretchFactor(0, 0)
        right_splitter.setStretchFactor(1, 0)
        right_splitter.setStretchFactor(2, 1)
        right_splitter.setSizes([190, 210, 360])

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
        self.download_panel.output_config_changed.connect(self._refresh_output_preview)
        self.device_panel.refresh_requested.connect(self._on_refresh_devices)
        self.device_panel.import_requested.connect(self._on_import_to_android)
        # Slice 5：任务列表
        self.task_panel.resume_requested.connect(self._on_task_resume_requested)
        self.task_panel.delete_requested.connect(self._on_task_delete_requested)
        self.task_panel.task_activated.connect(self._on_task_activated)

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

        output_path = self._resolve_output_path(data)
        self.download_panel.set_calculation_result(total, size_text, output_path)
        self._last_calculation = (total, size_text)
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

    def _resolve_output_path(self, data: dict) -> Path | None:
        """由表单里的"输出目录 + 地图名称"得到输出文件路径。

        地图名称为空时返回 ``None``：此时输出文件还不确定，UI 显示占位提示
        （**不**替用户编一个文件名，免得实际落盘位置与预期不符）。
        """
        if not data.get("map_name"):
            return None
        return build_output_path(data["output_dir"], data["map_name"])

    @Slot()
    def _refresh_output_preview(self) -> None:
        """输出目录 / 地图名称变化时，重绘预估结果里的「输出文件」行。

        只重绘已经算过的结果，不主动触发重算——用户改一个字就重算一遍
        纯属浪费（真正的重算是「计算」按钮的职责）。
        """
        cached = self._last_calculation
        if cached is None:
            return
        total, size_text = cached
        self.download_panel.set_calculation_result(
            total, size_text, self._resolve_output_path(self.download_panel.get_form_data())
        )

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
        output_path = self._resolve_output_path(data)
        self.log_panel.append_log(
            "当前配置："
            f"token={token}，"
            f"地图类型={data['map_type'].value}，"
            f"中文注记={'是' if data['with_labels'] else '否'}，"
            f"范围={bounds}，"
            f"缩放={data['min_zoom']}~{data['max_zoom']}，"
            f"输出文件={output_path or '（未填写地图名称）'}"
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
        """启动"下载 → 写入 MBTiles"。

        链路：读表单 → ``app.geo`` 统计瓦片数 → 若目标 ``.mbtiles`` 已存在则先问
        "继续 / 重下 / 取消" → 大任务风险提示 → ``app.services`` 在后台线程跑完整
        落盘流程 → 进度 / 结果回主线程展示。

        本方法不含任何瓦片公式、URL 拼接、并发或 SQLite 逻辑。
        """
        running = self._download_worker
        if running is not None and running.isRunning():
            self.log_panel.append_log("上一次下载尚未结束，已忽略本次请求", "WARNING")
            return

        plan = self._prepare_download()
        if plan is None:
            return

        if plan.output_path.exists():
            # Spec §15：绝不静默覆盖（更不静默删除）用户上次下载的成果，
            # 先把选择权交出去。
            self._ask_existing_file(plan)
            return

        self._continue_plan(plan)

    def _continue_plan(self, plan: _DownloadPlan) -> None:
        """"已有文件"这一关过了之后：先看规模，再决定要不要弹大任务确认框。

        阈值判断用的是 ``effective_tiles``（真正要下的张数）而不是 ``total``：
        续传一个"已有 19 万、只缺 3 千"的任务不该被当成大任务来吓唬。
        """
        if plan.effective_tiles > GUI_LARGE_TASK_CONFIRM_TILES:
            # 只提示，不限制：用户确认后照常下载，规模上不封顶。
            self._confirm_large_task(plan)
            return

        self._launch_download(plan)

    def _prepare_download(self) -> _DownloadPlan | None:
        """读表单 + 校验 + 算总量与输出路径 + 建任务记录。

        失败时统一走 :meth:`_report_download_error` 并把原因返回给用户，
        返回 ``None`` 表示"没准备好，别启动"。

        任务记录在这里就落盘（状态 ``CREATED``）：之后无论是"大任务确认"还是
        "已有文件选择"，用户点取消都只是删掉一条 ``CREATED`` 记录，
        而不会出现"下载跑起来却没有任务记录"这种反向缺口。
        """
        data = self.download_panel.get_form_data()
        if not data["map_name"]:
            self._report_download_error(
                UI_TEXT_MAP_NAME_REQUIRED, dialog_title="无法开始下载"
            )
            return None

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
            # 先算总量——不能"跑起来才发现是几十万张"
            total = estimate_tile_count(bounds, min_zoom, max_zoom)
            output_path = build_output_path(data["output_dir"], data["map_name"])
        except ValueError as exc:
            self._report_download_error(str(exc), dialog_title="无法开始下载")
            return None

        # 已有文件时顺手数一下里面有多少张（GROUP BY 计数，**不读 BLOB**）
        existing = (
            count_existing_tiles(output_path, range(min_zoom, max_zoom + 1))
            if output_path.exists()
            else 0
        )

        try:
            task = self.task_service.create_task(
                name=data["map_name"],
                bounds=bounds,
                min_zoom=min_zoom,
                max_zoom=max_zoom,
                map_layer=layer,
                output_path=output_path,
                total_tiles=total,
            )
        except (TaskStoreError, OSError) as exc:
            self._report_download_error(
                f"无法创建任务记录（{self.task_service.store.path}）：{exc}",
                dialog_title="无法开始下载",
            )
            return None

        return _DownloadPlan(
            provider=provider,
            bounds=bounds,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            layer=layer,
            output_path=output_path,
            map_name=data["map_name"],
            description=(
                f"天地图 {layer.value} z{min_zoom}~{max_zoom} {bounds.as_tuple()}"
            ),
            total=total,
            size_text=format_size(estimate_size_bytes(total)),
            task=task,
            existing_tiles=existing,
            # 全新下载默认"不跳过"：目标文件本来就不该有内容，跳过只会掩盖问题
            resume_policy=ResumePolicy.disabled(),
            effective_tiles=max(0, total - existing),
        )

    def _discard_plan(self, plan: _DownloadPlan) -> None:
        """用户取消了这次下载：删掉刚建的任务记录，不留半截状态。

        只删"这一轮新建的"记录。从任务列表恢复的任务是从磁盘读出来的既有记录，
        用户取消它只是"这次不继续"，记录本身还要留在列表里等下次。
        """
        if plan.resume_task is not None:
            return
        task = plan.task
        if task is None:  # pragma: no cover - 防御性分支
            return
        try:
            self.task_service.delete_task(task.id)
        except (TaskStoreError, OSError) as exc:  # pragma: no cover - 磁盘异常
            self.log_panel.append_log(f"清理任务记录失败：{exc}", "WARNING")
        self._refresh_task_list()

    def _log_resume_counts(self, existing: int, total: int) -> None:
        """Spec §16 要求的三段信息：**已有多少 / 目标多少 / 还要下多少**。

        这三个数字是"我到底在等它干什么"的全部答案：只有"继续下载"一句话时，
        用户根本判断不出该不该点——是差 5 张还是差 5 万张，体感完全不同。
        """
        remaining = max(0, total - existing)
        self.log_panel.append_log(
            f"已有：{existing:,} / {total:,} / 继续下载：{remaining:,}"
        )

    def _confirm_large_task(self, plan: _DownloadPlan) -> None:
        """大任务风险提示（**非模态**）。

        刻意不用 ``QMessageBox.question()``：它会 ``exec()`` 阻塞事件循环，
        在无头测试里直接把用例挂死（Slice 0 踩过的坑）。这里用 ``show()`` +
        ``finished`` 信号，把"用户点了哪个按钮"变成一次正常的事件回调。
        """
        self._pending_plan = plan
        dialog = QMessageBox(
            QMessageBox.Icon.Question,
            UI_TEXT_LARGE_TASK_CONFIRM_TITLE,
            UI_TEXT_LARGE_TASK_CONFIRM.format(
                total=plan.effective_tiles, size=plan.size_text
            ),
            parent=self,
        )
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(self._on_large_task_answered)
        self._confirm_dialog = dialog
        self.log_panel.append_log(
            f"预计需要下载 {plan.effective_tiles:,} 张瓦片（约 {plan.size_text}），"
            "已弹出大任务确认框，等待用户确认",
            "WARNING",
        )
        self.statusBar().showMessage("等待确认大任务…")
        dialog.show()

    @Slot(int)
    def _on_large_task_answered(self, result: int) -> None:
        """用户对"是否继续大任务"做出选择。"""
        plan = self._pending_plan
        self._pending_plan = None
        self._confirm_dialog = None
        if plan is None:  # pragma: no cover - 防御性分支
            return

        if result == int(QMessageBox.StandardButton.Yes):
            self.log_panel.append_log("用户确认继续大任务")
            self._launch_download(plan)
            return

        self.log_panel.append_log(
            f"用户取消了大任务下载（预计 {plan.effective_tiles:,} 张瓦片）", "WARNING"
        )
        self._discard_plan(plan)
        self.statusBar().showMessage("已取消下载", 5000)

    # ------------------------------------------------------------------ #
    # 槽：目标文件已存在时的三选一（Slice 5，Spec §15）
    # ------------------------------------------------------------------ #
    def _ask_existing_file(self, plan: _DownloadPlan) -> None:
        """目标 ``.mbtiles`` 已存在 → 问"继续下载 / 重新下载 / 取消"。

        复用 / 重下 / 放弃三件事的后果差别巨大（复用会**跳过**已有瓦片，
        重下要先把旧文件挪走），所以既不能默认复用、也不能默默删掉重下。
        同样是**非模态**弹窗，理由见 :meth:`_confirm_large_task`。
        """
        self._pending_plan = plan
        dialog = QMessageBox(
            QMessageBox.Icon.Question,
            UI_TEXT_MBTILES_EXISTS_TITLE,
            UI_TEXT_MBTILES_EXISTS.format(
                path=plan.output_path, existing=plan.existing_tiles
            ),
            parent=self,
        )
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.Yes)
        # 用标准按钮 + 改文案：这样"用户点了哪个"就是 ``finished`` 的整型结果，
        # 非模态回调里判起来最稳（不必去比对按钮对象）。
        dialog.button(QMessageBox.StandardButton.Yes).setText(
            UI_TEXT_MBTILES_EXISTS_CONTINUE
        )
        dialog.button(QMessageBox.StandardButton.No).setText(
            UI_TEXT_MBTILES_EXISTS_RESTART
        )
        dialog.button(QMessageBox.StandardButton.Cancel).setText(
            UI_TEXT_MBTILES_EXISTS_CANCEL
        )
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(self._on_existing_file_answered)
        self._exists_dialog = dialog
        self.log_panel.append_log(
            f"检测到已有地图文件（{plan.existing_tiles:,} 张瓦片）：{plan.output_path}，"
            "已弹出「继续下载 / 重新下载 / 取消」选择框",
            "WARNING",
        )
        self.statusBar().showMessage("等待确认：已有地图文件如何处理…")
        dialog.show()

    @Slot(int)
    def _on_existing_file_answered(self, result: int) -> None:
        """用户对"已有文件怎么办"做出选择。"""
        plan = self._pending_plan
        self._pending_plan = None
        self._exists_dialog = None
        if plan is None:  # pragma: no cover - 防御性分支
            return

        yes = int(QMessageBox.StandardButton.Yes)
        no = int(QMessageBox.StandardButton.No)

        if result == yes:
            # 复用该文件：走续传路径（服务层会再校验一次参数一致性，Spec §12）
            self.log_panel.append_log("选择继续下载：复用已有地图文件，已有瓦片将被跳过")
            self._log_resume_counts(plan.existing_tiles, plan.total)
            self._continue_plan(
                replace(
                    plan,
                    resume_existing=True,
                    resume_policy=ResumePolicy(),
                    effective_tiles=max(0, plan.total - plan.existing_tiles),
                )
            )
            return

        if result == no:
            self._confirm_restart(plan)
            return

        self.log_panel.append_log("已取消：未对已有文件做任何改动", "WARNING")
        self._discard_plan(plan)
        self.statusBar().showMessage("已取消下载", 5000)

    def _confirm_restart(self, plan: _DownloadPlan) -> None:
        """"重新下载"的二次确认：先告诉用户旧文件会被挪到哪儿。"""
        self._pending_plan = plan
        backup = _backup_path(plan.output_path)
        dialog = QMessageBox(
            QMessageBox.Icon.Warning,
            UI_TEXT_MBTILES_RESTART_TITLE,
            UI_TEXT_MBTILES_RESTART_CONFIRM.format(backup=backup, total=plan.total),
            parent=self,
        )
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(self._on_restart_answered)
        self._restart_dialog = dialog
        self.log_panel.append_log(
            f"用户选择重新下载，已弹出二次确认（旧文件将备份为 {backup}）", "WARNING"
        )
        self.statusBar().showMessage("等待确认重新下载…")
        dialog.show()

    @Slot(int)
    def _on_restart_answered(self, result: int) -> None:
        """"重新下载"二次确认的答复。"""
        plan = self._pending_plan
        self._pending_plan = None
        self._restart_dialog = None
        if plan is None:  # pragma: no cover - 防御性分支
            return

        if result != int(QMessageBox.StandardButton.Yes):
            self.log_panel.append_log("已取消重新下载：原文件保持原样", "WARNING")
            self._discard_plan(plan)
            self.statusBar().showMessage("已取消下载", 5000)
            return

        # 备份而不是删除：这可能是用户几个小时下载的成果。
        backup = _backup_path(plan.output_path)
        try:
            plan.output_path.replace(backup)
        except OSError as exc:
            self._report_download_error(
                f"备份已有文件失败（{plan.output_path}）：{exc}",
                dialog_title="无法重新下载",
            )
            self._discard_plan(plan)
            return

        self.log_panel.append_log(f"已把原文件备份为：{backup}")
        self.log_panel.append_log("将从零重新下载全部瓦片（不跳过任何瓦片）")
        self._continue_plan(
            replace(
                plan,
                existing_tiles=0,
                resume_existing=False,
                resume_policy=ResumePolicy.disabled(),
                effective_tiles=plan.total,
            )
        )

    def _launch_download(self, plan: _DownloadPlan) -> None:
        """各确认框都已答复后真正起线程。

        全新下载、复用已有文件、从任务列表恢复——三种入口在这里合流：
        差别只是"要不要传 ``resume_task`` / ``resume_policy``"。
        """
        task = plan.resume_task or plan.task
        if task is None:  # pragma: no cover - 防御性分支
            self._report_download_error(
                "内部错误：缺少任务记录，无法启动下载", dialog_title="无法开始下载"
            )
            return

        # 续传模式下服务的参数以**任务记录**为准，界面只补充 Token
        resume_task = task if plan.skips_existing else None

        self.download_panel.set_download_running(True, paused=False)
        self.download_panel.clear_download_result()
        self._current_task = task
        self._reset_status_for_download(plan)
        try:
            self.task_service.start_downloading(task, total_tiles=plan.total)
        except (TaskStoreError, OSError) as exc:  # pragma: no cover - 磁盘异常
            self.log_panel.append_log(f"任务状态落盘失败：{exc}", "WARNING")
        self._refresh_task_list(select_id=task.id)

        token = self.download_panel.get_form_data()["token"]
        self.log_panel.append_log(
            f"开始下载：图层={plan.layer.value}，范围={plan.bounds.as_tuple()}，"
            f"缩放={plan.min_zoom}~{plan.max_zoom}，瓦片总数={plan.total:,}，"
            f"并发={DEFAULT_DOWNLOAD_CONCURRENCY}，"
            f"限速={DEFAULT_REQUESTS_PER_SECOND:g} 请求/秒，"
            f"超时={DEFAULT_REQUEST_TIMEOUT:g}s，"
            f"最大尝试={DEFAULT_MAX_RETRIES} 次"
        )
        if resume_task is not None:
            self.log_panel.append_log(
                f"续传模式：已有 {plan.existing_tiles:,} 张瓦片会自动跳过，"
                f"本次预计下载 {plan.effective_tiles:,} 张"
            )
        self.log_panel.append_log(
            f"输出文件：{plan.output_path}"
            f"（预计容量 {plan.size_text}，分批事务写入，边下载边落盘）"
        )
        self.statusBar().showMessage(f"下载中… 0/{plan.total:,}")

        worker = DownloadWorker(
            plan.provider,
            plan.bounds,
            plan.min_zoom,
            plan.max_zoom,
            plan.layer,
            plan.output_path,
            map_name=plan.map_name,
            description=plan.description,
            concurrency=DEFAULT_DOWNLOAD_CONCURRENCY,
            timeout=DEFAULT_REQUEST_TIMEOUT,
            resume_policy=plan.resume_policy,
            resume_task=resume_task,
            # Token 只在内存里过一道（Spec §9：任务记录不存 Token）
            token=token,
            # 续传时数据源就是 ``resume_task`` 本身，不要再传一次
            task=None if resume_task is not None else task,
            task_service=self.task_service,
            parent=self,
        )
        worker.progress_changed.connect(self._on_download_progress)
        worker.completed.connect(self._on_download_finished)
        worker.failed.connect(self._on_download_failed)
        self._download_worker = worker
        worker.start()

    def _reset_status_for_download(self, plan: _DownloadPlan) -> None:
        """开工前把状态区清成"从零开始"，避免上一轮的残留数字造成误读。"""
        values = self._status_values
        values["task_status"].setText("续传中" if plan.skips_existing else "下载中")
        values["current_zoom"].setText("—")
        values["tile_total"].setText(f"{plan.total:,}")
        values["tile_done"].setText("0")
        values["tile_succeeded"].setText("0")
        values["tile_failed"].setText("0")
        values["tile_skipped"].setText("0")
        values["speed"].setText("0 瓦片/秒")
        values["downloaded_bytes"].setText("0 B")
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
        values["downloaded_bytes"].setText(format_size(progress.bytes_downloaded))
        if progress.current_zoom is not None:
            values["current_zoom"].setText(str(progress.current_zoom))
        self.progress_bar.setValue(int(progress.percent))
        tail = f"（成功 {progress.succeeded:,} · 失败 {progress.failed:,}"
        if progress.skipped:
            tail += f" · 跳过 {progress.skipped:,}"
        tail += "）"
        self.statusBar().showMessage(
            f"下载中… {progress.completed:,}/{progress.total:,}{tail}"
        )
        # 同步任务列表那一行。**只刷界面、不写盘**——落盘在 service 的进度
        # 转发器里（数据流出源头那一层），在这里再写一次就是同一份数据两处写。
        task = self._current_task
        if task is not None:
            self.task_panel.update_task(task)

    @Slot(object)
    def _on_download_finished(self, outcome: MapDownloadOutcome) -> None:
        """任务结束：先把任务状态落盘，再展示实测结果。"""
        self._download_worker = None
        self.download_panel.set_download_running(False)
        task = self._current_task
        self._current_task = None
        self._finalize_task(task, outcome)

        summary = outcome.summary
        if outcome.cancelled:
            status_text = "已取消"
        elif outcome.is_success:
            status_text = "已完成"
        else:
            status_text = "已完成（有失败）"
        self._status_values["task_status"].setText(status_text)
        if not outcome.cancelled:
            self.progress_bar.setValue(100)

        lines = outcome.as_lines()
        self.download_panel.set_download_result(lines)
        for line in lines:
            self.log_panel.append_log(line)
        # 结果区在配置列下方，可能落在可视区之外——主动滚过去
        self._reveal_download_result()

        if outcome.cancelled:
            for line in UI_TEXT_CANCELLED_KEEP_TILES.splitlines():
                self.log_panel.append_log(line, "WARNING")
            self.statusBar().showMessage(
                f"下载已取消 · 已保留 {outcome.tile_count:,} 张瓦片 · {outcome.size_text}",
                10000,
            )
            return

        level = "INFO" if outcome.is_success else "WARNING"
        self.log_panel.append_log(
            f"下载完成：成功 {summary.succeeded:,} 张，失败 {summary.failed:,} 张，"
            + (f"已有跳过 {summary.skipped:,} 张，" if summary.skipped else "")
            + f"入库 {outcome.tile_count:,} 张（{outcome.size_text}），"
            f"耗时 {summary.duration_seconds:.2f}s，"
            f"平均 {summary.average_tiles_per_second:.1f} 瓦片/秒，"
            f"验证 {outcome.validation.status_text}。",
            level,
        )
        if not outcome.is_success:
            # §18/§19：没达标就把"还差什么"说清楚，用户才知道再点一次「继续」有没有用
            reason = outcome.failure_text or "未达到完成条件"
            self.log_panel.append_log(
                f"任务未完成：{reason}。可在「下载任务」里点「继续」补齐缺失瓦片。",
                "WARNING",
            )
        self.statusBar().showMessage(
            f"下载完成 · {outcome.tile_count:,} 张 · {outcome.size_text}"
            f" · 验证 {outcome.validation.status_text}",
            10000,
        )

    def _finalize_task(
        self, task: DownloadTask | None, outcome: MapDownloadOutcome
    ) -> None:
        """把最终结果写进任务记录（Spec §18/§19 的判定结果）。

        - ``COMPLETED``：只有 ``outcome.verdict`` 说达标才给（库中瓦片数达标 +
          校验通过 + 零失败）。
        - ``FAILED``：**保留失败态**并记下原因，下次「继续」只补缺失 / 失败的那些。
        - ``CANCELLED``：用户主动取消，同样可以再继续。
        """
        if task is None:  # pragma: no cover - 防御性分支
            return
        summary = outcome.summary
        # 用最终汇总把计数补齐：进度回调是按秒节流的，最后一帧不一定是终值
        task.total_tiles = max(task.total_tiles, outcome.target_tiles)
        task.completed_tiles = max(task.completed_tiles, summary.completed)
        task.succeeded_tiles = max(task.succeeded_tiles, summary.succeeded)
        task.failed_tiles = summary.failed
        task.skipped_tiles = max(task.skipped_tiles, summary.skipped)

        verdict = outcome.verdict
        try:
            if verdict is DownloadTaskStatus.COMPLETED:
                self.task_service.complete_task(task)
            elif verdict is DownloadTaskStatus.CANCELLED:
                self.task_service.cancel_task(task)
            else:
                self.task_service.fail_task(
                    task, outcome.failure_text or "未达到完成条件"
                )
        except (TaskStoreError, OSError) as exc:  # pragma: no cover - 磁盘异常
            self.log_panel.append_log(f"任务状态落盘失败：{exc}", "WARNING")

        self._refresh_task_list(select_id=task.id)
        self.log_panel.append_log(
            f"任务状态：{verdict.display_name}"
            f"（已入库 {outcome.tile_count:,} / 目标 {outcome.target_tiles:,} 张）"
            f" · 见「下载任务」列表"
        )

    def _reveal_download_result(self) -> None:
        """把左侧配置列滚到「下载结果」，免得结果落在可视区之外。

        配置列内容已经比窗口高，结果区默认在折叠线以下；不滚过去的话，
        用户会以为"下载完了但界面上什么都没变"。
        """
        scroll = self._left_scroll
        if scroll is not None:
            scroll.ensureWidgetVisible(self.download_panel.download_result_group, 0, 0)

    @Slot(str)
    def _on_download_failed(self, message: str) -> None:
        """下载线程异常终止（含写库失败这类无法继续的错误）。"""
        self._download_worker = None
        self.download_panel.set_download_running(False)
        self._status_values["task_status"].setText("失败")
        self.download_panel.set_download_result_error(message)
        self._reveal_download_result()

        task = self._current_task
        self._current_task = None
        if task is not None:
            # 保留 FAILED 与原因：用户下次点「继续」时只补缺失/失败的瓦片（§18）
            try:
                self.task_service.fail_task(task, message)
            except (TaskStoreError, OSError) as exc:  # pragma: no cover - 磁盘异常
                self.log_panel.append_log(f"任务状态落盘失败：{exc}", "WARNING")
            self._refresh_task_list(select_id=task.id)

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
        self._set_current_task_status(DownloadTaskStatus.PAUSED)
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
        self._set_current_task_status(DownloadTaskStatus.DOWNLOADING)
        self.log_panel.append_log("下载已继续")
        self.statusBar().showMessage("下载已继续")

    def _set_current_task_status(self, status: DownloadTaskStatus) -> None:
        """把"当前任务"的状态**立即**落盘（暂停 / 继续不走节流）。

        状态变化是稀缺事件（用户手动点一次），而进程可能在任意时刻被强杀；
        为省一次写盘换来"重启后看到的状态是错的"，不划算。
        """
        task = self._current_task
        if task is None:
            return
        try:
            if status is DownloadTaskStatus.PAUSED:
                self.task_service.pause_task(task)
            elif status is DownloadTaskStatus.DOWNLOADING:
                self.task_service.resume_task(task)
            else:  # pragma: no cover - 目前只有这两个调用点
                self.task_service.set_status(task, status)
        except (TaskStoreError, OSError) as exc:  # pragma: no cover - 磁盘异常
            self.log_panel.append_log(f"任务状态落盘失败：{exc}", "WARNING")
        self.task_panel.update_task(task)

    # ------------------------------------------------------------------ #
    # 槽：任务列表（Slice 5）
    # ------------------------------------------------------------------ #
    def _recover_tasks(self) -> None:
        """启动时读一次任务记录，并把"上次异常退出遗留的运行中任务"降级为 PAUSED。

        **绝不自动开始下载**（Spec §10）：打开程序时用户应该看到"有几个任务可以
        继续"，而不是"程序自己开始跑几十万张瓦片去了"。

        这一步挂了（tasks.json 不可写 / 目录只读）不能让程序起不来——任务持久化
        是增强功能，不是启动的前置条件，因此这里吞掉异常只记日志。
        """
        recovered: list[DownloadTask] = []
        try:
            recovered = self.task_service.recover_interrupted_tasks()
        except (TaskStoreError, OSError) as exc:
            self.log_panel.append_log(f"读取任务记录失败：{exc}", "ERROR")

        self._refresh_task_list()
        if recovered:
            for line in UI_TEXT_TASK_RECOVERED.splitlines():
                self.log_panel.append_log(line, "WARNING")
            names = "、".join(task.name for task in recovered)
            self.log_panel.append_log(
                f"已将 {len(recovered)} 个未完成任务标记为「已暂停」：{names}"
            )
            self.statusBar().showMessage(
                f"发现 {len(recovered)} 个未完成任务，已在「下载任务」里标记为已暂停"
            )

    def _refresh_task_list(self, select_id: str | None = None) -> None:
        """整体刷新任务列表（读 tasks.json）。只在状态变化时调用，不放进进度回调。"""
        try:
            tasks = self.task_service.list_tasks()
        except (TaskStoreError, OSError) as exc:  # pragma: no cover - 磁盘异常
            self.log_panel.append_log(f"读取任务列表失败：{exc}", "ERROR")
            return
        self.task_panel.set_tasks(tasks)
        if select_id:
            self.task_panel.select_task(select_id)

    @Slot(str)
    def _on_task_resume_requested(self, task_id: str) -> None:
        """任务列表里的「继续」：以**任务记录**为准重建计划并启动续传。

        参数只来自任务记录（Spec §16）：表单里的范围 / 缩放不参与——否则用户
        改了表单再点「继续」，会以为"改了范围它就会少下一半"，而实际行为完全
        取决于记录（这是对的，但必须让人看得出来）。
        """
        running = self._download_worker
        if running is not None and running.isRunning():
            self.log_panel.append_log("上一次下载尚未结束，已忽略本次请求", "WARNING")
            self.statusBar().showMessage("当前有下载正在进行，无法同时继续另一个任务", 5000)
            return

        task = self.task_service.get_task(task_id)
        if task is None:
            self.log_panel.append_log(f"任务不存在或记录已损坏：{task_id}", "WARNING")
            self._refresh_task_list()
            return

        if task.status is DownloadTaskStatus.COMPLETED:
            self.log_panel.append_log(f"任务已完成，无需继续：{task.name}", "WARNING")
            self.statusBar().showMessage("该任务已完成", 5000)
            return

        # Token 不落盘（Spec §9），所以恢复时必须让用户重新填一次
        token = self.download_panel.get_form_data()["token"]
        if not token:
            self.download_panel.set_form_data(task)
            for line in UI_TEXT_RESUME_TOKEN_REQUIRED.splitlines():
                self.log_panel.append_log(line, "WARNING")
            self.statusBar().showMessage("继续任务需要天地图 Token", 8000)
            self._show_error_dialog("无法继续任务", UI_TEXT_RESUME_TOKEN_REQUIRED)
            return

        try:
            bounds = GeoBounds(task.west, task.south, task.east, task.north)
            layer = MapLayerType(task.map_layer)
        except ValueError as exc:
            self._report_download_error(
                f"任务参数无法还原，记录可能已损坏：{exc}", dialog_title="无法继续任务"
            )
            return

        path = Path(task.output_path)
        total = task.total_tiles or estimate_tile_count(
            bounds, task.min_zoom, task.max_zoom
        )
        existing = (
            count_existing_tiles(path, task.zoom_levels) if path.is_file() else 0
        )
        remaining = max(0, total - existing)

        # Spec §16 要求的三行信息：已有多少 / 目标多少 / 还要下多少
        self.log_panel.append_log(f"Resume task: {task.id}")
        self._log_resume_counts(existing, total)

        plan = _DownloadPlan(
            provider=TiandituProvider(token),
            bounds=bounds,
            min_zoom=int(task.min_zoom),
            max_zoom=int(task.max_zoom),
            layer=layer,
            output_path=path,
            map_name=task.name,
            description=f"天地图 {layer.value} z{task.min_zoom}~{task.max_zoom} "
            f"{bounds.as_tuple()}",
            total=total,
            size_text=format_size(estimate_size_bytes(total)),
            resume_policy=ResumePolicy(),
            resume_task=task,
            task=task,
            existing_tiles=existing,
            effective_tiles=remaining,
        )
        self._continue_plan(plan)

    @Slot(str)
    def _on_task_delete_requested(self, task_id: str) -> None:
        """「删除记录」：只删 ``tasks.json`` 里的那条，**不动 ``.mbtiles``**（Spec §20）。"""
        task = self.task_service.get_task(task_id)
        name = task.name if task is not None else task_id
        try:
            removed = self.task_service.delete_task(task_id)
        except (TaskStoreError, OSError) as exc:
            self._report_download_error(
                f"删除任务记录失败：{exc}", dialog_title="无法删除任务记录"
            )
            return

        if removed:
            self.log_panel.append_log(
                f"已删除任务记录：{name}（{UI_TEXT_TASK_DELETE_HINT}）"
            )
            self.statusBar().showMessage(f"已删除任务记录：{name}", 5000)
        else:
            self.log_panel.append_log(f"任务记录不存在：{task_id}", "WARNING")
        self._refresh_task_list()

    @Slot(str)
    def _on_task_activated(self, task_id: str) -> None:
        """双击任务行 → 把参数回填到配置面板（Spec §14）。"""
        task = self.task_service.get_task(task_id)
        if task is None:
            self.log_panel.append_log(f"任务不存在：{task_id}", "WARNING")
            self._refresh_task_list()
            return

        layer_ok = self.download_panel.set_form_data(task)
        self.log_panel.append_log(
            f"已把任务「{task.name}」的参数回填到配置面板"
            f"（状态 {task.status.display_name}，"
            f"进度 {task.progress_text()}）"
        )
        self.log_panel.append_log(
            "Token 需要重新填写：任务记录里不保存 Token（密钥不落盘）"
        )
        if not layer_ok:
            # 认不出图层不是致命错误，但必须说出来：静默保留旧选项会让人以为"回填成功了"
            self.log_panel.append_log(
                f"图层 {task.map_layer!r} 不在当前下拉选项里，已保留界面上的原选项",
                "WARNING",
            )
        self.statusBar().showMessage(
            "已回填任务参数；Token 需要重新填写（任务记录里不保存 Token）", 8000
        )

    # ------------------------------------------------------------------ #
    # 槽：设备动作
    # ------------------------------------------------------------------ #
    @Slot()
    def _on_refresh_devices(self) -> None:
        message = "刷新设备：本版本不执行 adb，设备发现将在 Slice 6 实现"
        self.log_panel.append_log(message)
        self.device_panel.clear_devices()
        self.statusBar().showMessage(message, 5000)

    @Slot()
    def _on_import_to_android(self) -> None:
        message = "导入到 Android：需要 adb 设备管理，将在 Slice 6 实现"
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
            "当前进度：Slice 5（断点续传、任务恢复与已有瓦片跳过）。",
        )

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名约定
        """关闭前收尾：确认、取消下载、写盘、等线程结束。

        两件事，顺序不能换：

        1. **Spec §17**：还有任务在下载时先问一次"退出后任务将暂停，是否退出？"
           用户说"不"就 ``ignore()`` 掉这次关闭，下载继续跑。
        2. 真正退出时取消后台下载并 ``wait()``。否则 ``QThread`` 可能在运行时被
           销毁，Qt 会直接中止进程。取消**不会**丢弃已写入的瓦片：service 会
           drain 写队列、flush 并关闭数据库，磁盘上留下一个可用的（不完整的）
           ``.mbtiles``；任务记录写成 ``PAUSED``，下次启动可以继续。
        """
        # 还开着的非模态弹窗先关掉，避免它们引用着已销毁的父窗口。
        # 关之前要断开 finished 连接：``close()`` 会发出 Rejected，
        # 不挡掉的话会跑出一堆"已取消下载"的假日志。
        pending = self._pending_plan
        self._pending_plan = None
        self._close_dialogs()
        if pending is not None:
            self._discard_plan(pending)

        download = self._download_worker
        running = download is not None and download.isRunning()
        if running and not self._exit_confirmed:
            self._ask_exit_with_running_task(event)
            return

        worker = self._connection_worker
        if worker is not None and worker.isRunning():
            self.log_panel.append_log("等待连接测试线程结束…")
            worker.wait(_THREAD_SHUTDOWN_WAIT_MS)

        if running and download is not None:
            self.log_panel.append_log("任务将暂停：正在收尾并写盘…")
            download.cancel()
            if download.wait(_DOWNLOAD_CANCEL_WAIT_MS):
                self._mark_current_task_paused()
            else:
                # 极端情况下（网络卡住 / 磁盘极慢）不能再等，否则关窗会一直挂住。
                # 任务记录仍是 DOWNLOADING，下次启动会被自动降级为 PAUSED。
                self.log_panel.append_log(
                    "后台下载线程未能及时结束，已放弃等待", "WARNING"
                )
        self._download_worker = None
        self._current_task = None
        super().closeEvent(event)

    def _close_dialogs(self) -> None:
        """关掉所有还开着的非模态弹窗（并断开它们的 finished 连接）。"""
        for attr, slot in (
            ("_confirm_dialog", self._on_large_task_answered),
            ("_exists_dialog", self._on_existing_file_answered),
            ("_restart_dialog", self._on_restart_answered),
            ("_exit_dialog", self._on_exit_confirmed),
        ):
            dialog = getattr(self, attr)
            if dialog is None:
                continue
            setattr(self, attr, None)
            try:
                dialog.finished.disconnect(slot)
            except (RuntimeError, TypeError):  # pragma: no cover - 本来就没连上
                pass
            dialog.close()

    def _ask_exit_with_running_task(self, event: QCloseEvent) -> None:
        """Spec §17：还有任务在下载 → 先问一次（非模态，理由同大任务确认）。"""
        event.ignore()  # 先别关，等用户回答
        existing = self._exit_dialog
        if existing is not None:  # pragma: no cover - 连点两次关闭
            existing.raise_()
            return

        dialog = QMessageBox(
            QMessageBox.Icon.Question,
            UI_TEXT_EXIT_WITH_TASK_TITLE,
            UI_TEXT_EXIT_WITH_TASK,
            parent=self,
        )
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        dialog.button(QMessageBox.StandardButton.Yes).setText(UI_TEXT_EXIT_CONFIRM)
        dialog.button(QMessageBox.StandardButton.No).setText(UI_TEXT_EXIT_CANCEL)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(self._on_exit_confirmed)
        self._exit_dialog = dialog
        self.log_panel.append_log(
            "检测到任务仍在下载，已弹出退出确认框（退出后任务将暂停）", "WARNING"
        )
        self.statusBar().showMessage("等待确认退出…")
        dialog.show()

    @Slot(int)
    def _on_exit_confirmed(self, result: int) -> None:
        """退出确认的答复。"""
        self._exit_dialog = None
        if result != int(QMessageBox.StandardButton.Yes):
            self.log_panel.append_log("已取消退出，下载继续")
            self.statusBar().showMessage("已取消退出", 5000)
            return

        self._exit_confirmed = True
        self.log_panel.append_log("用户确认退出：任务将暂停，已完成瓦片保留")
        self.close()

    def _mark_current_task_paused(self) -> None:
        """把当前任务写成 ``PAUSED`` 并落盘（退出时的收尾）。

        退出走的是 ``cancel()``（为了让线程尽快结束），但**语义上**是"暂停"：
        用户关掉程序不等于放弃这张地图，下次还要接着下。
        """
        task = self._current_task
        if task is None:  # pragma: no cover - 防御性分支
            return
        try:
            self.task_service.pause_task(task)
        except (TaskStoreError, OSError) as exc:  # pragma: no cover - 磁盘异常
            self.log_panel.append_log(f"任务状态落盘失败：{exc}", "WARNING")
        # 顺手刷一下列表那一行：关窗之后测试/诊断还会读它，让界面与磁盘一致
        self.task_panel.update_task(task)
