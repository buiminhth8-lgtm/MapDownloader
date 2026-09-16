"""主窗口。

本文件只做三件事：**装配界面、连接信号、把用户动作转成日志与状态栏提示**。
任何计算 / 下载 / 打包 / 设备通信的逻辑都必须放到对应的业务包中，
通过 ``self.log_panel`` 汇报进展，禁止在此堆积实现细节。

Qt / asyncio 边界（Slice 7，Spec §7）
-------------------------------------

主线程里**不允许**出现：HTTP 请求、SQLite 查询、Pillow 编解码、MBTiles 校验、
大量瓦片计算、等待 asyncio。本模块据此把三件事全部推给后台线程：

==================  ==========================================  ==============================
工作                 为什么不能在主线程                          落在哪个 worker
==================  ==========================================  ==============================
连通性测试           发 HTTP                                     ``ConnectionTestWorker``
瓦片下载             发 HTTP / SQLite / Pillow / asyncio 事件循环  ``DownloadWorker``
已有瓦片扫描         SQLite 计数查询                              ``ExistingTileScanWorker``
MBTiles 检查（§8）   SQLite 自检 / 聚合 / Pillow 解码             ``MBTilesInspectionWorker``
==================  ==========================================  ==============================

主线程这边只剩"读表单 → 校验 → 起线程 → 收信号 → 改控件"。
``MainWindow`` 里不出现 ``httpx`` / ``sqlite3`` / ``PIL`` 的任何符号，
这条规则由 ``tests/test_ui_smoke.py`` 里的一致性断言守着。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
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
    DEFAULT_OUTPUT_DIR,
    DEFAULT_REQUESTS_PER_SECOND,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_TILE_TIMEOUT_SECONDS,
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    GUI_LARGE_TASK_CONFIRM_TILES,
    LOG_DEVICE_SELECTED,
    LOG_PACKAGE_INVALID,
    LOG_PACKAGE_VALIDATED,
    LOG_TRANSFER_STARTED,
    PACKAGE_MANIFEST_FILENAME,
    TRANSFER_ERROR_PACKAGE_DIR_MISSING,
    UI_TEXT_BUTTON_CANCEL,
    UI_TEXT_BUTTON_CLOSE,
    UI_TEXT_BUTTON_CONFIRM,
    UI_TEXT_BUTTON_OK,
    UI_TEXT_CANCELLED_KEEP_TILES,
    UI_TEXT_CANCEL_CONFIRM,
    UI_TEXT_CANCEL_TITLE,
    UI_TEXT_COMPLETE_BODY,
    UI_TEXT_COMPLETE_TITLE,
    UI_TEXT_COMPOSITION_MERGED,
    UI_TEXT_COMPOSITION_SINGLE,
    UI_TEXT_DEVICE_ADB_MISSING,
    UI_TEXT_DEVICE_HINT,
    UI_TEXT_EXIT_CANCEL,
    UI_TEXT_EXIT_CONFIRM,
    UI_TEXT_EXIT_WITH_TASK,
    UI_TEXT_EXIT_WITH_TASK_TITLE,
    UI_TEXT_FAILED_BODY,
    UI_TEXT_FAILED_TITLE,
    UI_TEXT_INSPECT_BUTTON,
    UI_TEXT_INSPECT_BUTTON_HINT,
    UI_TEXT_LARGE_TASK_CONFIRM,
    UI_TEXT_LARGE_TASK_CONFIRM_TITLE,
    UI_TEXT_MAP_NAME_AUTO_GENERATED,
    UI_TEXT_MBTILES_EXISTS,
    UI_TEXT_MBTILES_EXISTS_CANCEL,
    UI_TEXT_MBTILES_EXISTS_CONTINUE,
    UI_TEXT_MBTILES_EXISTS_RESTART,
    UI_TEXT_MBTILES_EXISTS_TITLE,
    UI_TEXT_MBTILES_RESTART_CONFIRM,
    UI_TEXT_MBTILES_RESTART_TITLE,
    UI_TEXT_OPEN_DIRECTORY,
    UI_TEXT_OPEN_DIRECTORY_FAILED,
    UI_TEXT_OPEN_DIRECTORY_NEED_PATH,
    UI_TEXT_PACKAGE_BUTTON,
    UI_TEXT_PACKAGE_BUTTON_HINT,
    UI_TEXT_PACKAGE_BUTTON_VALIDATE,
    UI_TEXT_PACKAGE_BUTTON_VALIDATE_HINT,
    UI_TEXT_RESUME_TOKEN_REQUIRED,
    UI_TEXT_SCANNING_EXISTING,
    UI_TEXT_TASK_DELETE_HINT,
    UI_TEXT_TASK_RECOVERED,
    UI_TEXT_TRANSFER_BUTTON_OVERWRITE,
    UI_TEXT_TRANSFER_CANCELLED_BODY,
    UI_TEXT_TRANSFER_CANCELLED_TITLE,
    UI_TEXT_TRANSFER_CHOOSE_PACKAGE_TITLE,
    UI_TEXT_TRANSFER_COMPLETE_BODY,
    UI_TEXT_TRANSFER_COMPLETE_FILES,
    UI_TEXT_TRANSFER_COMPLETE_TITLE,
    UI_TEXT_TRANSFER_CONFIRM_BODY,
    UI_TEXT_TRANSFER_CONFIRM_CANCEL,
    UI_TEXT_TRANSFER_CONFIRM_START,
    UI_TEXT_TRANSFER_CONFIRM_TITLE,
    UI_TEXT_TRANSFER_EXISTS_BODY,
    UI_TEXT_TRANSFER_EXISTS_TITLE,
    UI_TEXT_TRANSFER_FAILED_BODY,
    UI_TEXT_TRANSFER_FAILED_TITLE,
    UI_TEXT_TRANSFER_NEED_PACKAGE,
    UI_TEXT_TRANSFER_NEED_DEVICE,
    UI_TEXT_TRANSFER_RUNNING,
    UI_TEXT_TRANSFER_SHA256_UNAVAILABLE,
    UI_TEXT_TRANSFER_STATUS_WAITING_IMPORT,
    UI_TEXT_VALIDATION_TITLE,
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
from app.models.map_layer import MapLayerType, MapType, describe_map_type
from app.providers import TiandituProvider, TileTestResult, mask_url
from app.services import (
    AdbStatus,
    AndroidTransferService,
    MapDownloadOutcome,
    build_output_path,
)
from app.tasks import TaskService, TaskStoreError
from app.ui.connection_check import ConnectionTestWorker
from app.ui.desktop import open_directory
from app.ui.device_panel import (
    TRANSFER_STATE_CANCELLED,
    TRANSFER_STATE_DONE,
    TRANSFER_STATE_FAILED,
    TRANSFER_STATE_READY,
    TRANSFER_STATE_RUNNING,
    DevicePanel,
)
from app.ui.download_panel import (
    ACTION_CALCULATE,
    ACTION_CANCEL,
    ACTION_PAUSE,
    ACTION_RESUME,
    ACTION_START,
    ACTION_TEST_CONNECTION,
    DownloadPanel,
)
from app.ui.download_worker import DownloadWorker
from app.ui.log_panel import LogPanel
from app.ui.mbtiles_inspector_dialog import MBTilesInspectorDialog
from app.ui.package_dialog import PackageDialog, PackageValidateDialog
from app.ui.preferences import UiPreferences
from app.ui.task_actions import UiTaskState
from app.ui.task_panel import TaskPanel
from app.ui.task_progress_panel import TaskProgressPanel
from app.ui.tile_scan_worker import ExistingTileScan, ExistingTileScanWorker
from app.ui.validation import validate_form
from app.workers import (
    AndroidDeviceWorker,
    AndroidPackageCheckWorker,
    AndroidTransferWorker,
)

#: 左侧配置列的偏好宽度（实际取值为它与"内容最小宽度"的较大者）
_LEFT_COLUMN_WIDTH = 460

#: 右侧设备 / 任务 / 日志列的最小宽度。任务表格有 5 列，360 会让「输出路径」
#: 只剩几十像素，因此抬到 420（900px 的最小窗口宽度仍能满足 460 + 420）。
_RIGHT_COLUMN_WIDTH = 420

#: 右列三段（设备 / 任务 / 日志）的初始高度分配（Spec §11 的面板变高了）。
#:
#: 450 是**量出来的**，不是估的：设备面板内容的自然高度在真实字体 / DPI 下是
#: 430px（三个 QGroupBox 168 + 122 + 128，加两条 6px 间隔），而 ``setWidgetResizable``
#: 会把面板撑到 ``sizeHint``——给 410 时视口只有 406，于是默认窗口下就出现了一条
#: 24px 的竖直滚动条，用户要滚一下才能看到 [传输到设备]。
#:
#: 注意这是**初始**配比：设备区选了设备但没选包时，面板会多出一行提示
#: （+30px，约 466），那时滚动区接管是**对的**——面板高度是动态的，硬要它
#: 永远不滚就只能把日志区压没。判断标准是"真正要操作的那一刻不用滚"。
_RIGHT_COLUMN_SIZES: tuple[int, int, int] = (450, 130, 130)

#: 设备区的滚动区最小高度：小窗口下这块可以滚，但不能把日志区挤没
_DEVICE_AREA_MIN_HEIGHT = 190

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


def _describe_plan(
    map_type: MapType,
    with_labels: bool,
    min_zoom: int,
    max_zoom: int,
    bounds: GeoBounds,
) -> str:
    """拼出写进 MBTiles ``description`` 的一句话。

    合成模式把**两个图层标识**都写进去（``vec_w + cva_w``）：续传一致性校验会
    从 ``description`` 里提取图层标识作为兜底线索，只写底图的话，
    "任务是合成、文件里其实只有底图"这类不一致就少了一道本该有的证据。

    只在这里拼一次：服务层 ``_default_description`` 用的是同样的格式，
    两边必须一致，否则人工比对描述时会误判成"参数不匹配"。
    """
    layers = " + ".join(item.value for item in map_type.layers(with_labels))
    return f"天地图 {layers} z{min_zoom}~{max_zoom} {bounds.as_tuple()}"

#: 各动作对应的说明（下载相关动作在 Slice 3 已实现，Slice 4 起落盘为 MBTiles，
#: Slice 5 起支持断点续传与任务恢复，Slice 7 起补上取消与完整的按钮状态机）
_ACTION_HINTS: dict[str, str] = {
    ACTION_CALCULATE: "按当前范围计算瓦片数量与预估容量（Slice 1 已实现）",
    ACTION_TEST_CONNECTION: "取范围中心点测试单瓦片连通性（Slice 2 已实现）",
    ACTION_START: "下载瓦片并写入 MBTiles（Slice 4；已有文件会提示续传，Slice 5）",
    ACTION_PAUSE: "暂停下载：不再发起新请求，已发出的请求允许完成（Slice 3 已实现）",
    ACTION_RESUME: "继续下载：放行当前任务的闸门（Slice 3；跨进程续传见「下载任务」）",
    ACTION_CANCEL: "取消下载：已写入的瓦片保留，任务可以稍后再继续（Slice 7）",
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
        map_type: 地图类型（矢量 / 影像，Slice 6）。续传时取自任务记录。
        with_labels: 是否把中文注记合成进瓦片（Slice 6）。
        requests: 预计 WMTS 请求次数 = ``effective_tiles × 每张瓦片请求次数``。
            开启中文注记后每张瓦片要请求底图 + 注记两次，因此这个数字是
            ``effective_tiles`` 的两倍 —— 大任务提示必须给出它，
            否则用户看到"配额消耗"时无法预估真实开销。
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
    map_type: MapType | None = None
    with_labels: bool = False
    requests: int = 0

    @property
    def is_resume(self) -> bool:
        """是否走"继续某个已存在的任务记录"入口。"""
        return self.resume_task is not None

    @property
    def skips_existing(self) -> bool:
        """本次启动是否会跳过已有瓦片。"""
        return self.is_resume or self.resume_existing

    @property
    def download_target(self) -> "MapLayerType | MapType":
        """交给下载引擎的目标（``MapType`` 或单个 ``MapLayerType``）。

        合成模式下必须把 ``MapType`` 本身传下去——服务层要靠它才知道
        该用哪个注记图层；只传底图的话"合成"就无从谈起。
        认不出地图类型时退回单个底图图层，保证老任务仍能跑。
        """
        if self.with_labels and self.map_type is not None:
            return self.map_type
        return self.layer

    @property
    def composition_text(self) -> str:
        """一行合成说明（界面日志与结果区共用同一句话）。"""
        return UI_TEXT_COMPOSITION_MERGED if self.with_labels else UI_TEXT_COMPOSITION_SINGLE


class MainWindow(QMainWindow):
    """应用主窗口：1200x760，左右分栏布局。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        task_service: TaskService | None = None,
        preferences: UiPreferences | None = None,
        android_service: AndroidTransferService | None = None,
    ) -> None:
        """
        Args:
            parent: Qt 父对象。
            task_service: 任务服务；``None`` 时用默认的
                ``~/MapDownloader/data/tasks.json``。
                测试会注入一个指向临时目录的实例，避免污染用户主目录。
            preferences: 界面偏好（Slice 7）；``None`` 时用默认的
                ``QSettings(ORG_NAME, APP_NAME)``——在 Windows 上落到注册表。
                测试会注入一个 ``IniFormat`` 的临时实例，理由同 ``task_service``。
            android_service: ADB 与设备传输的编排层（Slice 10）；``None`` 时自建。
                测试与验收脚本注入假的实现，于是**测试不依赖本机真的装了 adb**
                ——一台没装 Android SDK 的 CI 机器上也必须能跑全套用例。
        """
        super().__init__(parent)

        self.setWindowTitle(APP_NAME)
        self.resize(DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)
        self.setMinimumSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)

        #: 任务持久化服务（Slice 5）
        self.task_service = task_service or TaskService()
        #: 界面偏好（Slice 7，Spec §22）。**不存 Token。**
        self.preferences = preferences or UiPreferences()
        #: Android 传输编排层（Slice 10）。界面不直接碰 adb。
        self.android_service = android_service or AndroidTransferService()

        # 四大面板 + 当前任务进度区
        self.download_panel = DownloadPanel(self)
        #: 当前任务进度面板（Slice 7，Spec §5）。
        self.task_progress = TaskProgressPanel(self)
        self.device_panel = DevicePanel(self)
        self.task_panel = TaskPanel(self)
        self.log_panel = LogPanel(self)

        #: 后台连通性测试线程（同一时刻最多一个；持有引用避免被 GC 提前回收）
        self._connection_worker: ConnectionTestWorker | None = None
        #: 最近一次测试的上下文，用于结果展示：(图层, zoom, x, y, 脱敏 URL)
        self._connection_context: tuple[MapLayerType, int, int, int, str] | None = None

        #: 后台下载线程（同一时刻最多一个）
        self._download_worker: DownloadWorker | None = None
        #: 后台已有瓦片扫描线程（Slice 7；同一时刻最多一个）
        self._scan_worker: ExistingTileScanWorker | None = None
        #: 正在进行的任务记录（用于收尾时写状态、关窗时写 PAUSED）
        self._current_task: DownloadTask | None = None
        #: 等待用户确认的下载计划（大任务确认 / 已有文件选择 / 取消，都是非模态）
        self._pending_plan: _DownloadPlan | None = None
        #: 用户已经在"取消吗"的弹窗里变成确认后，等待扫描结果的那份计划
        self._scan_followup = None
        #: 已请求暂停、但引擎还没确认（Spec §13 的 PAUSING 阶段）
        self._pause_pending = False
        #: 当前界面状态（Spec §6 的按钮矩阵据此驱动）
        self._task_state = UiTaskState.IDLE
        #: 非模态弹窗实例（持有引用避免被 GC 提前回收）
        self._confirm_dialog: QMessageBox | None = None
        self._exists_dialog: QMessageBox | None = None
        self._restart_dialog: QMessageBox | None = None
        self._exit_dialog: QMessageBox | None = None
        self._cancel_dialog: QMessageBox | None = None
        self._complete_dialog: QMessageBox | None = None
        self._failed_dialog: QMessageBox | None = None
        #: MBTiles 检查窗口（Slice 8）。刻意**不**用 ``WA_DeleteOnClose``：
        #: 后台线程还在读瓦片时销毁它，Qt 会中止整个进程。
        self._inspector_dialog: MBTilesInspectorDialog | None = None
        #: Slice 9：离线地图包的两个窗口。同样由主窗口持有、**不带
        #: WA_DeleteOnClose**（打包线程跑着时销毁 QThread 会中止进程），
        #: 关掉之后再打开时复用同一个实例。
        self._package_dialog: PackageDialog | None = None
        self._package_validator_dialog: PackageValidateDialog | None = None

        # --- Slice 10：Android 传输的三个后台线程与三个非模态弹窗 --------
        #: 设备刷新线程（adb version + devices -l + getprop）
        self._device_worker: AndroidDeviceWorker | None = None
        #: 地图包校验线程（会算一次完整的 SHA256）
        self._package_check_worker: AndroidPackageCheckWorker | None = None
        #: 传输线程（adb push）
        self._transfer_worker: AndroidTransferWorker | None = None
        #: Slice 10 的三个弹窗（都带 WA_DeleteOnClose，由 _forget_dialog_on_destroy
        #: 在销毁时把属性清成 None，见那个方法的说明）
        self._transfer_confirm_dialog: QMessageBox | None = None
        self._transfer_exists_dialog: QMessageBox | None = None
        self._transfer_done_dialog: QMessageBox | None = None
        self._transfer_failed_dialog: QMessageBox | None = None
        #: 确认框里那两个按钮的引用。**必须在装配时就落好默认值**：它们只在
        #: 对应弹窗被创建时才被赋值，而验收脚本 / 测试会在"还没弹过窗"的时刻读它们
        #: ——读一个不存在的属性会 ``AttributeError``（Qt 槽里抛异常会被吞掉）。
        self._transfer_confirm_start: QPushButton | None = None
        self._transfer_overwrite_button: QPushButton | None = None
        #: 用户已经在"覆盖设备上的同名包吗"里点过「覆盖」，等新一轮传输
        self._transfer_overwrite = False
        #: 关窗确认结果（Spec §17：先提示，用户确认后才真正退出）
        self._exit_confirmed = False
        #: 最近一次"计算"的结果，用于输出路径变化时重绘「输出文件」行
        self._last_calculation: tuple[int, str] | None = None

        self._build_menu_bar()
        self._build_central_widget()
        self._build_status_bar()
        self._connect_signals()
        self._apply_preferences()
        self._recover_tasks()

    # ------------------------------------------------------------------ #
    # 兼容入口（Slice 3~6 的调用点与测试都用这几个名字）
    # ------------------------------------------------------------------ #
    @property
    def status_group(self) -> TaskProgressPanel:
        """当前任务进度区（Slice 7 起是 :class:`TaskProgressPanel` 的实例）。"""
        return self.task_progress

    @property
    def _status_values(self) -> dict[str, QLabel]:
        """进度区的字段标签表（旧名字，转发到进度面板）。"""
        return self.task_progress.values

    @property
    def progress_bar(self):
        """总进度条（旧名字，转发到进度面板）。"""
        return self.task_progress.progress_bar

    # ------------------------------------------------------------------ #
    # 界面装配
    # ------------------------------------------------------------------ #
    def _build_menu_bar(self) -> None:
        file_menu = self.menuBar().addMenu("文件(&F)")

        #: Slice 8：检查任意 .mbtiles。按钮在「下载任务」面板上，菜单里再给一份
        #: —— 检查是个独立工具，不该逼用户先找到那个面板。
        self.inspect_action = QAction(f"{UI_TEXT_INSPECT_BUTTON}(&I)…", self)
        self.inspect_action.setToolTip(UI_TEXT_INSPECT_BUTTON_HINT)
        # 用 lambda 吞掉 ``triggered`` 的 ``checked`` 参数：直接连过去的话
        # 那个布尔值会落到 ``path`` 上（碰巧也能跑，但语义是错的）。
        self.inspect_action.triggered.connect(lambda: self.open_mbtiles_inspector())
        file_menu.addAction(self.inspect_action)

        #: Slice 9：把 .mbtiles 打包成离线地图包 / 校验一个已有的包。
        #: 两个都是独立工具，因此与检查一样放在「文件」菜单里（§17 / §21），
        #: 下载任务面板上另有 [生成地图包] 按钮（选中任务时可用）。
        self.package_action = QAction(f"{UI_TEXT_PACKAGE_BUTTON}(&P)…", self)
        self.package_action.setToolTip(UI_TEXT_PACKAGE_BUTTON_HINT)
        self.package_action.triggered.connect(lambda: self.open_package_dialog())
        file_menu.addAction(self.package_action)

        self.validate_package_action = QAction(
            f"{UI_TEXT_PACKAGE_BUTTON_VALIDATE}(&V)…", self
        )
        self.validate_package_action.setToolTip(UI_TEXT_PACKAGE_BUTTON_VALIDATE_HINT)
        self.validate_package_action.triggered.connect(
            lambda: self.open_package_validator()
        )
        file_menu.addAction(self.validate_package_action)
        file_menu.addSeparator()

        quit_action = QAction("退出(&Q)", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu("帮助(&H)")
        about_action = QAction("关于(&A)", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)

    def _build_central_widget(self) -> None:
        # 左列：配置 + 状态，放在滚动区里以适配小屏 / 高 DPI
        left_content = QWidget(self)
        left_layout = QVBoxLayout(left_content)
        left_layout.addWidget(self.download_panel)
        left_layout.addWidget(self.task_progress)
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
        #
        # Slice 10：设备区从一块 120px 的占位变成了真面板（ADB 状态 + 设备 +
        # 地图包 + 传输进度），自然高度约 350px。与左列同样的处理——套一层滚动区，
        # 于是小窗口下它自己滚，而不会把下面的日志区挤没。
        self._device_scroll = QScrollArea(self)
        self._device_scroll.setWidgetResizable(True)
        self._device_scroll.setWidget(self.device_panel)
        self._device_scroll.setMinimumHeight(_DEVICE_AREA_MIN_HEIGHT)

        right_splitter = QSplitter(Qt.Orientation.Vertical, self)
        right_splitter.addWidget(self._device_scroll)
        right_splitter.addWidget(self.task_panel)
        right_splitter.addWidget(self.log_panel)
        right_splitter.setStretchFactor(0, 0)
        right_splitter.setStretchFactor(1, 0)
        right_splitter.setStretchFactor(2, 1)
        right_splitter.setSizes(list(_RIGHT_COLUMN_SIZES))

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
        # Slice 6：切换地图类型 / 勾选中文注记会改变每张瓦片的请求次数，
        # 已算出的「预估结果」要跟着重画那一行请求量。
        self.download_panel.layer_plan_changed.connect(self._refresh_output_preview)
        self.device_panel.refresh_requested.connect(self._on_refresh_devices)
        # Slice 10：名字沿用 Slice 0 的 import_requested，语义变成"传输到设备"
        self.device_panel.import_requested.connect(self._on_transfer_to_android)
        self.device_panel.cancel_requested.connect(self._on_cancel_transfer)
        self.device_panel.validate_requested.connect(self._on_validate_android_package)
        self.device_panel.choose_package_requested.connect(self._on_choose_android_package)
        self.device_panel.device_changed.connect(self._on_device_changed)
        # Slice 5：任务列表
        self.task_panel.resume_requested.connect(self._on_task_resume_requested)
        self.task_panel.delete_requested.connect(self._on_task_delete_requested)
        self.task_panel.task_activated.connect(self._on_task_activated)
        # Slice 7：选中任务后，四个动作按钮要跟着那个任务的状态走（Spec §6）
        self.task_panel.selection_changed.connect(self._on_task_selection_changed)
        # Slice 8：检查工具入口（按钮 + 菜单各一条，都走同一个槽）
        self.task_panel.inspect_requested.connect(self.open_mbtiles_inspector)
        # Slice 9：打包入口（任务列表里的 [生成地图包] 带着选中任务的产物路径）
        self.task_panel.package_requested.connect(self._on_task_package_requested)

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
            self._fill_auto_map_name()
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
        if action == ACTION_CANCEL:
            self._cancel_download()
            return
        self.statusBar().showMessage(hint, 5000)

    def _fill_auto_map_name(self) -> str | None:
        """地图名称为空时补一个并写回输入框（Spec §2.7），返回被补上的名字。

        「计算」与「开始下载」都会先调它，于是用户在**看到预估结果的同时**
        也看到了将来那个文件叫什么；不会出现"下载完了才发现文件名叫 map_39.05N_116.05E"。
        """
        generated = self.download_panel.ensure_map_name()
        if generated:
            self.log_panel.append_log(
                UI_TEXT_MAP_NAME_AUTO_GENERATED.format(name=generated)
            )
        return generated

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
        """非模态错误 / 警告提示（Spec §20 要求"用 QMessageBox 警告用户"）。

        刻意**不用** ``QMessageBox.warning()``：那个静态方法是模态的（内部
        ``exec()``），会阻塞 Qt 事件循环——在无头测试里直接把用例挂死，
        在有头环境里也会让"下载进行中"的弹窗把整个界面冻住（Slice 0 踩过这个坑）。
        这里用 :class:`QMessageBox` 的 ``Icon.Warning`` + ``show()``：
        对用户而言同样是"一个必须看见的警告框"，但事件循环照常转。
        与此同时，信息已经落到结果区 / 日志 / 状态栏，不会被漏掉。
        """
        dialog = QMessageBox(QMessageBox.Icon.Warning, title, message, parent=self)
        # 不显式建按钮的话 Qt 会给一个英文 "OK"（同"通用按钮文案"一节的理由）
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        dialog.button(QMessageBox.StandardButton.Ok).setText(UI_TEXT_BUTTON_OK)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def _report_validation_error(self, validation) -> None:
        """下载前校验失败的统一出口（Spec §20）。

        逐条写进日志（用户事后能翻），弹窗里是编号列表（用户当下要照着改）。
        """
        for error in validation.errors:
            self.log_panel.append_log(error, "ERROR")
        self.statusBar().showMessage("无法开始下载：请先修正表单上的问题", 8000)
        self._show_error_dialog(UI_TEXT_VALIDATION_TITLE, validation.message)

    def _ask_confirmation(
        self,
        title: str,
        message: str,
        *,
        on_answered,
        attr: str,
        yes_text: str | None = None,
        no_text: str | None = None,
        default_yes: bool = False,
        log: str = "",
    ) -> QMessageBox:
        """弹一个"是 / 否"确认框（**非模态**），把结果交给回调。

        抽出来的理由：取消、退出、重新下载三处要的完全是同一种交互，
        而"非模态 + ``finished`` 信号 + 断开连接"这套写法有足够多的细节
        （信号重连、被 GC 回收、关窗时假日志）值得只写一遍。

        Args:
            title: 弹窗标题。
            message: 正文。
            on_answered: 收到 ``finished(int)`` 时的槽。
            attr: 存放该弹窗的实例属性名（防止被 GC 提前回收）。
            yes_text / no_text: 两个按钮的文案；``None`` 表示用 Qt 默认。
            default_yes: 默认按钮是"是"还是"否"。破坏性操作应默认"否"。
            log: 写进日志的一句话。

        Returns:
            弹窗对象（已 ``show()``）。
        """
        dialog = QMessageBox(QMessageBox.Icon.Question, title, message, parent=self)
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        default = (
            QMessageBox.StandardButton.Yes if default_yes else QMessageBox.StandardButton.No
        )
        dialog.setDefaultButton(default)
        if yes_text:
            dialog.button(QMessageBox.StandardButton.Yes).setText(yes_text)
        if no_text:
            dialog.button(QMessageBox.StandardButton.No).setText(no_text)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(on_answered)
        setattr(self, attr, dialog)
        if log:
            self.log_panel.append_log(log, "WARNING")
        dialog.show()
        return dialog

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
    # 槽：批量下载（Slice 3；Slice 7 起校验与扫描都走了正确的线程）
    # ------------------------------------------------------------------ #
    def _busy_worker(self):
        """当前占着下载通道的后台线程（下载优先），没有则返回 ``None``。

        Spec §6 的"不要允许用户重复点击开始创建多个相同 Worker"靠它兜底：
        按钮已经被状态矩阵禁掉了，但信号还可能从别处来（快捷键、程序化调用），
        因此真正的闸门必须在这里，而不是只在界面上。
        """
        download = self._download_worker
        if download is not None and download.isRunning():
            return download
        scan = self._scan_worker
        if scan is not None and scan.isRunning():
            return scan
        return None

    @Slot()
    def _start_download(self) -> None:
        """启动"下载 → 写入 MBTiles"。

        链路：读表单 → 统一校验（:func:`app.ui.validation.validate_form`）→
        建计划（纯计算，不碰磁盘）→ **后台**扫描已有瓦片 → 已有文件选择 /
        大任务提示 → ``app.services`` 在后台线程跑完整落盘流程。

        Slice 5 时"数一数已有多少张"是在这里同步做的；Slice 7 把它移到了
        :class:`~app.ui.tile_scan_worker.ExistingTileScanWorker`——那是一次
        SQLite 查询，而 Spec §7 明确禁止在 Qt 主线程里碰 SQLite。

        本方法不含任何瓦片公式、URL 拼接、并发或 SQLite 逻辑。
        """
        if self._busy_worker() is not None:
            self.log_panel.append_log("上一次任务尚未结束，已忽略本次请求", "WARNING")
            self.statusBar().showMessage("当前有任务正在进行，无法同时开始另一个", 5000)
            return

        # Spec §2.7：名称为空先补一个，用户才不会在"输出文件"里看到陌生文件名
        self._fill_auto_map_name()

        plan = self._prepare_download()
        if plan is None:
            return

        if plan.output_path.exists():
            # Spec §15：绝不静默覆盖（更不静默删除）用户上次下载的成果，
            # 先把选择权交出去。但要问"继续 / 重下"，得先知道里面有多少张。
            self._begin_scan(plan, self._on_prepare_scan_done, purpose="检查已有文件")
            return

        self._continue_plan(plan)

    def _on_prepare_scan_done(self, plan: _DownloadPlan, scan: ExistingTileScan) -> None:
        """"要开始下载时发现文件已存在"这条路径的扫描回调。"""
        plan = self._apply_scan(plan, scan)
        self.log_panel.append_log(
            f"已有文件：{plan.output_path}（{plan.existing_tiles:,} 张瓦片）"
        )
        self._ask_existing_file(plan)

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

    # ------------------------------------------------------------------ #
    # 后台扫描已有瓦片（Slice 7，Spec §7 / §11）
    # ------------------------------------------------------------------ #
    def _begin_scan(self, plan: _DownloadPlan, on_done, *, purpose: str) -> None:
        """在后台数一数目标文件里已有多少张瓦片。

        Args:
            plan: 当前计划；扫描结果会 patch 到它的
                ``existing_tiles`` / ``effective_tiles`` / ``requests``。
            on_done: 扫描结束后的回调 ``(plan, scan) -> None``，在**主线程**执行。
            purpose: 日志里说明这次扫描是为了什么（"检查已有文件" /
                "恢复任务"），否则用户会看到两处一模一样的"正在扫描已有瓦片"。
        """
        self._scan_followup = (on_done, plan)
        self.download_panel.set_scanning(True)
        self.task_progress.reset(plan.total, name=plan.map_name)
        self.task_progress.show_scanning()
        self.log_panel.append_log(f"{UI_TEXT_SCANNING_EXISTING}（{purpose}）")
        self.statusBar().showMessage(f"{UI_TEXT_SCANNING_EXISTING}（{purpose}）")

        worker = ExistingTileScanWorker(
            plan.output_path,
            list(range(plan.min_zoom, plan.max_zoom + 1)),
            parent=self,
        )
        worker.scanned.connect(self._on_scan_finished)
        self._scan_worker = worker
        worker.start()

    @Slot(object)
    def _on_scan_finished(self, scan: ExistingTileScan) -> None:
        """扫描线程结束，回主线程继续原来的流程。"""
        self._scan_worker = None
        self.download_panel.set_scanning(False)
        followup = self._scan_followup
        self._scan_followup = None
        if followup is None:
            # 用户在扫描期间点了「取消」：结果照收，但不再往下走。
            self.log_panel.append_log("已放弃本次下载（扫描结果不再使用）", "WARNING")
            self._refresh_action_state()
            return

        on_done, plan = followup
        if scan.error:
            # 扫不出来不该阻断下载：当作"没有已有瓦片"，最多多下几张。
            # 写库是 INSERT OR REPLACE，重复写同一张是安全的。
            self.log_panel.append_log(f"{scan.error}（按无已有瓦片继续）", "WARNING")
        on_done(plan, scan)

    @staticmethod
    def _apply_scan(plan: _DownloadPlan, scan: ExistingTileScan) -> _DownloadPlan:
        """把扫描结果并进计划（已有张数 → 真正要下的张数 → 预计请求数）。"""
        existing = max(0, int(scan.tiles))
        effective = max(0, plan.total - existing)
        map_type = plan.map_type or MapType.from_layer(plan.layer)
        return replace(
            plan,
            existing_tiles=existing,
            effective_tiles=effective,
            requests=effective * map_type.request_count_per_tile(plan.with_labels),
        )

    def _prepare_download(self) -> _DownloadPlan | None:
        """读表单 + 统一校验 + 算总量与输出路径 + 建任务记录。

        失败时统一走 :meth:`_report_validation_error` /
        :meth:`_report_download_error` 并把原因返回给用户，
        返回 ``None`` 表示"没准备好，别启动"。

        任务记录在这里就落盘（状态 ``CREATED``）：之后无论是"大任务确认"、
        "已有文件选择"还是"取消"，用户放弃时都只是删掉一条 ``CREATED`` 记录，
        而不会出现"下载跑起来却没有任务记录"这种反向缺口。

        **不查磁盘**：已有瓦片数由调用方在后台扫描后补进来（Spec §7）。
        """
        data = self.download_panel.get_form_data()

        # Spec §20：一个统一的校验入口，一次报出全部问题
        validation = validate_form(data)
        if not validation.ok:
            self._report_validation_error(validation)
            return None

        try:
            provider = TiandituProvider(data["token"])
            bounds = GeoBounds(
                west=data["west"],
                south=data["south"],
                east=data["east"],
                north=data["north"],
            )
            map_type = data["map_type"]
            with_labels = bool(data["with_labels"])
            # 任务记录里存的是**底图图层**（历史字段，续传校验与老版本兼容都靠它）；
            # "是否合成注记"由 with_labels 单独承载（Slice 6）。
            layer = map_type.base_layer
            min_zoom = int(data["min_zoom"])
            max_zoom = int(data["max_zoom"])
            # 先算总量——不能"跑起来才发现是几十万张"
            total = estimate_tile_count(bounds, min_zoom, max_zoom)
            output_path = build_output_path(data["output_dir"], data["map_name"])
        except ValueError as exc:
            self._report_download_error(str(exc), dialog_title=UI_TEXT_VALIDATION_TITLE)
            return None

        try:
            task = self.task_service.create_task(
                name=data["map_name"],
                bounds=bounds,
                min_zoom=min_zoom,
                max_zoom=max_zoom,
                map_layer=layer,
                output_path=output_path,
                total_tiles=total,
                with_labels=with_labels,
            )
        except (TaskStoreError, OSError) as exc:
            self._report_download_error(
                f"无法创建任务记录（{self.task_service.store.path}）：{exc}",
                dialog_title=UI_TEXT_VALIDATION_TITLE,
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
            description=_describe_plan(
                map_type, with_labels, min_zoom, max_zoom, bounds
            ),
            total=total,
            size_text=format_size(estimate_size_bytes(total)),
            task=task,
            # 已有张数未知，先当作 0；扫描完成后由 _apply_scan 修正
            existing_tiles=0,
            # 全新下载默认"不跳过"：目标文件本来就不该有内容，跳过只会掩盖问题
            resume_policy=ResumePolicy.disabled(),
            effective_tiles=total,
            map_type=map_type,
            with_labels=with_labels,
            requests=total * map_type.request_count_per_tile(with_labels),
        )

    def _discard_plan(self, plan: _DownloadPlan) -> None:
        """用户取消了这次下载：删掉刚建的任务记录，不留半截状态。

        只删"这一轮新建的"记录。从任务列表恢复的任务是从磁盘读出来的既有记录，
        用户取消它只是"这次不继续"，记录本身还要留在列表里等下次。
        """
        if plan.resume_task is not None:
            self._refresh_action_state()
            return
        task = plan.task
        if task is None:  # pragma: no cover - 防御性分支
            self._refresh_action_state()
            return
        try:
            self.task_service.delete_task(task.id)
        except (TaskStoreError, OSError) as exc:  # pragma: no cover - 磁盘异常
            self.log_panel.append_log(f"清理任务记录失败：{exc}", "WARNING")
        self._refresh_task_list()
        self._refresh_action_state()

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
        self._refresh_action_state()
        dialog = QMessageBox(
            QMessageBox.Icon.Question,
            UI_TEXT_LARGE_TASK_CONFIRM_TITLE,
            UI_TEXT_LARGE_TASK_CONFIRM.format(
                total=plan.effective_tiles,
                size=plan.size_text,
                requests=plan.requests,
            ),
            parent=self,
        )
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        # 标准按钮默认是英文（Yes / No），显式改成中文（理由见 settings 里的
        # 「通用按钮文案」一段）
        dialog.button(QMessageBox.StandardButton.Yes).setText(UI_TEXT_BUTTON_CONFIRM)
        dialog.button(QMessageBox.StandardButton.No).setText(UI_TEXT_BUTTON_CANCEL)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(self._on_large_task_answered)
        self._confirm_dialog = dialog
        self.log_panel.append_log(
            f"预计需要下载 {plan.effective_tiles:,} 张瓦片（约 {plan.size_text}，"
            f"约 {plan.requests:,} 次 WMTS 请求），"
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
        self._refresh_action_state()
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
        self._refresh_action_state()
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
        dialog.button(QMessageBox.StandardButton.Yes).setText(UI_TEXT_BUTTON_CONFIRM)
        dialog.button(QMessageBox.StandardButton.Cancel).setText(UI_TEXT_BUTTON_CANCEL)
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

        self.download_panel.clear_download_result()
        self._current_task = task
        self._reset_status_for_download(plan)
        self._pause_pending = False
        self._refresh_action_state("续传中" if plan.skips_existing else "下载中")
        try:
            self.task_service.start_downloading(task, total_tiles=plan.total)
        except (TaskStoreError, OSError) as exc:  # pragma: no cover - 磁盘异常
            self.log_panel.append_log(f"任务状态落盘失败：{exc}", "WARNING")
        self._refresh_task_list(select_id=task.id)

        token = self.download_panel.get_form_data()["token"]
        type_text = describe_map_type(plan.map_type, plan.with_labels, missing=plan.layer.value)
        self.log_panel.append_log(
            f"开始下载：地图类型={type_text}，图层={plan.layer.value}，"
            f"范围={plan.bounds.as_tuple()}，"
            f"缩放={plan.min_zoom}~{plan.max_zoom}，瓦片总数={plan.total:,}，"
            f"并发={DEFAULT_DOWNLOAD_CONCURRENCY}，"
            f"限速={DEFAULT_REQUESTS_PER_SECOND:g} 请求/秒，"
            f"超时={DEFAULT_REQUEST_TIMEOUT:g}s，"
            f"最大尝试={DEFAULT_MAX_RETRIES} 次"
        )
        # Spec §23：开工前一次性说清"这次到底在下什么"。合成模式与纯底图模式的
        # 产物、请求量、体积都不一样，只报一个"瓦片总数"是交代不清楚的。
        self.log_panel.append_log(f"合成方式：{plan.composition_text}")
        if plan.with_labels and plan.map_type is not None:
            self.log_panel.append_log(
                f"合成模式：每张瓦片请求 {plan.map_type.base_layer.value} + "
                f"{plan.map_type.label_layer.value} 各一次，"
                f"按 RGBA alpha 合成后存为 PNG；"
                f"本次预计 {plan.requests:,} 次 WMTS 请求"
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
            plan.download_target,
            plan.output_path,
            with_labels=plan.with_labels,
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
        """开工前把进度区清成"从零开始"，避免上一轮的残留数字造成误读。

        **不设状态文案、不动按钮**：那两件事统一由 :meth:`_refresh_action_state`
        按状态矩阵给（否则"这里写死一句 下载中、那里又按状态算一遍"迟早对不上）。
        """
        self.task_progress.reset(
            plan.total, name=plan.map_name, skipped=plan.existing_tiles
        )

    @Slot(object)
    def _on_download_progress(self, progress: DownloadProgress) -> None:
        """后台进度更新（已由引擎节流到约 6 次/秒），回主线程刷界面。

        节流发生在**数据源头**（``DownloadService`` 的进度转发器，间隔
        ``PROGRESS_EMIT_INTERVAL_SECONDS`` ≈ 0.15s），因此这里每收到一帧就是
        真的该刷一帧——几十万张瓦片的任务不会把 Qt 事件队列塞满（Spec §8）。
        """
        self.task_progress.apply_progress(progress)
        # 引擎已经报告过一帧，说明它确实收到了暂停指令、在途请求也落地了：
        # 此刻才把 PAUSING 落成 PAUSED（Spec §13）。
        if self._pause_pending and self._download_worker is not None:
            self._pause_pending = False
            self._refresh_action_state()
            self.log_panel.append_log("下载已暂停（已写入的瓦片已保留，可随时继续）")
            self.statusBar().showMessage("下载已暂停", 5000)

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
        """任务结束：先把任务状态落盘，再展示实测结果（Spec §15 / §16 / §17）。"""
        self._download_worker = None
        self._pause_pending = False
        task = self._current_task
        self._current_task = None
        self._finalize_task(task, outcome)

        if not outcome.cancelled:
            self.task_progress.set_percent(100)
        # 按钮与状态文案统一由状态机算：此刻没有 worker 了，选中任务的状态
        # 刚被 _finalize_task 写成终态，于是按钮正好落在矩阵里该有的那一格。
        self._refresh_action_state()

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

        summary = outcome.summary
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

        if outcome.is_success:
            # Spec §16：成功结束后弹出完成小结（MBTilesValidator 已经由 service
            # 在后台跑完，这里只是把它的结论摆出来，外加一个「打开目录」的出口）
            self._show_complete_dialog(outcome)
            self.statusBar().showMessage(
                f"下载完成 · {outcome.tile_count:,} 张 · {outcome.size_text}"
                f" · 验证 {outcome.validation.status_text}",
                10000,
            )
            return

        # §17：失败时 GUI 不能崩，还要把"为什么"和"成果还在"一起说清楚
        reason = outcome.failure_text or "未达到完成条件"
        self.log_panel.append_log(
            f"任务未完成：{reason}。可在「下载任务」里点「继续」补齐缺失瓦片。",
            "WARNING",
        )
        self._show_failed_dialog(outcome, reason)
        self.statusBar().showMessage(f"任务未完成：{reason}", 10000)

    def _show_complete_dialog(self, outcome: MapDownloadOutcome) -> None:
        """下载完成小结（Spec §16）。

        非模态（同 :meth:`_show_error_dialog` 的理由），并且**带一个「打开目录」
        按钮**：地图下完之后用户想做的下一件事必然是"去文件夹里看看那个文件"，
        让他自己去资源管理器里翻是没道理的。
        """
        box = QMessageBox(QMessageBox.Icon.Information, UI_TEXT_COMPLETE_TITLE, "", parent=self)
        # Slice 8（Spec §22）：完成小结里带上"缺失瓦片 / 抽样损坏"。
        # 诊断在 service 里已经跑完（快速档，不做全量图片扫描），
        # 这里只是把结论摆出来；没跑过（老调用方）时显示 "—" 而不是 "0"——
        # "不知道"和"是 0"必须能分辨。
        diagnostics = outcome.diagnostics
        box.setText(UI_TEXT_COMPLETE_BODY.format(
            path=outcome.output_path,
            map_type=describe_map_type(
                MapType.parse(outcome.map_type), outcome.with_labels,
                missing=outcome.map_type or "—",
            ),
            zooms=outcome.validation.zoom_text,
            tiles=outcome.tile_count,
            size=outcome.size_text,
            validation=(
                diagnostics.status if diagnostics is not None
                else outcome.validation.status_text
            ),
            missing=(
                diagnostics.missing_text if diagnostics is not None else "—"
            ),
            corrupt=(
                f"{diagnostics.sample_corrupt}"
                if diagnostics is not None and diagnostics.sample_size
                else "—"
            ),
        ))
        open_button = box.addButton(UI_TEXT_OPEN_DIRECTORY, QMessageBox.ButtonRole.ActionRole)
        close_button = box.addButton(QMessageBox.StandardButton.Close)
        close_button.setText(UI_TEXT_BUTTON_CLOSE)
        open_button.clicked.connect(
            lambda: self._open_output_directory(outcome.output_path)
        )
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._complete_dialog = box
        self._forget_dialog_on_destroy("_complete_dialog", box)
        box.show()

    def _show_failed_dialog(self, outcome: MapDownloadOutcome, reason: str) -> None:
        """任务失败提示（Spec §17）。

        按钮给「继续」而不是「关闭」：失败几乎总是"缺了一部分瓦片"，用户接下来
        唯一想做的事就是补上——把入口直接放在他眼前。
        """
        box = QMessageBox(QMessageBox.Icon.Warning, UI_TEXT_FAILED_TITLE, "", parent=self)
        box.setText(UI_TEXT_FAILED_BODY.format(
            reason=reason,
            succeeded=outcome.summary.succeeded,
            failed=outcome.summary.failed,
        ))
        resume_button = box.addButton("继续补齐", QMessageBox.ButtonRole.ActionRole)
        close_button = box.addButton(QMessageBox.StandardButton.Close)
        close_button.setText(UI_TEXT_BUTTON_CLOSE)
        resume_button.clicked.connect(self._on_failed_dialog_resume)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._failed_dialog = box
        self._forget_dialog_on_destroy("_failed_dialog", box)
        box.show()

    # ------------------------------------------------------------------ #
    # MBTiles 检查工具（Slice 8）
    # ------------------------------------------------------------------ #
    @Slot()
    @Slot(str)
    def open_mbtiles_inspector(self, path: str = "") -> None:
        """打开 MBTiles 检查窗口（Spec §16）。

        刻意**复用同一个对话框实例**：用户往往会"检查完这个再看那个"，
        每点一次就新建一个窗口，屏幕上会攒出一堆只能逐个关掉的窗口。
        复用时不清空上一次的结果 —— 新文件还没检查之前，旧结论仍然有参考价值
        （用户随时可以在里面点「选择文件…」换目标）。

        非模态（``show()``）：检查要跑几十秒甚至几分钟，把主窗口锁住没有道理。
        """
        dialog = self._inspector_dialog
        if dialog is None:
            # 对话框由 MainWindow 持有，**绝不带 WA_DeleteOnClose**：
            # 后台线程还在跑时销毁它的 QThread 会让 Qt 直接中止进程
            # （见该模块的模块级说明）。关掉之后再点「检查」复用同一个实例。
            dialog = MBTilesInspectorDialog(self)
            dialog.log_message.connect(self.log_panel.append_log)
            self._inspector_dialog = dialog

        target = (path or "").strip()
        if target:
            dialog.set_path(target)

        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self.statusBar().showMessage("MBTiles 检查工具", 3000)

    # ------------------------------------------------------------------ #
    # 离线地图包（Slice 9）
    # ------------------------------------------------------------------ #
    @Slot()
    @Slot(str)
    def open_package_dialog(self, path: str = "") -> None:
        """打开「生成地图包」窗口（Spec §17）。

        与检查窗口同一套规矩：**复用同一个实例**、**非模态**（打包 GB 级的文件
        要几分钟，把主窗口锁住没有道理）、由主窗口持有引用而**不带**
        ``WA_DeleteOnClose``（后台线程还在跑时销毁它的 ``QThread`` 会让 Qt
        直接中止进程）。

        传进来的 ``path`` 是**源 .mbtiles**：对话框收到之后会立刻在后台跑一次
        快速检查，界面上那几行地图信息与 [生成地图包] 的可用性都由它决定
        （Spec §6 / §17：界面不自己算地图信息）。
        """
        dialog = self._package_dialog
        if dialog is None:
            dialog = PackageDialog(self)
            dialog.log_message.connect(self.log_panel.append_log)
            self._package_dialog = dialog

        target = (path or "").strip()
        if target and target != dialog.path:
            dialog.set_source(target)

        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self.statusBar().showMessage(UI_TEXT_PACKAGE_BUTTON, 3000)

    @Slot()
    @Slot(str)
    def open_package_validator(self, path: str = "") -> None:
        """打开「验证地图包」窗口（Spec §21）。

        ``path`` 是**地图包目录**（不是 .mbtiles）。同一个实例复用。
        """
        dialog = self._package_validator_dialog
        if dialog is None:
            dialog = PackageValidateDialog(self)
            dialog.log_message.connect(self.log_panel.append_log)
            self._package_validator_dialog = dialog

        target = (path or "").strip()
        if target:
            dialog.set_package_dir(target)

        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self.statusBar().showMessage(UI_TEXT_PACKAGE_BUTTON_VALIDATE, 3000)

    @Slot(str)
    def _on_task_package_requested(self, path: str) -> None:
        """任务列表里的 [生成地图包]：带着选中任务的产物路径打开窗口。

        路径为空（没选中任务 / 任务没有产物）时不打开窗口，只在日志里说清楚
        —— Spec §17 要求这个入口"只有当前 MBTiles 存在时才可用"。
        """
        target = (path or "").strip()
        if not target:
            self.log_panel.append_log(
                "请先在「下载任务」里选中一条任务（需要已有 .mbtiles）", "WARNING"
            )
            return
        self.open_package_dialog(target)

    @Slot()
    def _on_failed_dialog_resume(self) -> None:
        """失败提示框里的「继续补齐」：与列表里的「继续」走同一条路。"""
        task = self.task_panel.selected_task_id()
        if task:
            self._on_task_resume_requested(task)
            return
        self.log_panel.append_log("没有可继续的任务：请在「下载任务」里选中它", "WARNING")

    def _open_output_directory(self, path) -> None:
        """打开输出文件所在的目录（Spec §16）。

        打不开不是错误——用户可能已经把文件挪走了。这里只把原因说出来，
        不弹错误框：他点「打开目录」是为了看文件，不是为了看另一个错误。
        """
        if path is None:
            self.log_panel.append_log(UI_TEXT_OPEN_DIRECTORY_NEED_PATH, "WARNING")
            self.statusBar().showMessage(UI_TEXT_OPEN_DIRECTORY_NEED_PATH, 5000)
            return
        ok, error = open_directory(path)
        if ok:
            self.log_panel.append_log(f"已打开输出目录：{Path(str(path)).parent}")
            return
        message = UI_TEXT_OPEN_DIRECTORY_FAILED.format(path=path, error=error)
        self.log_panel.append_log(message, "WARNING")
        self.statusBar().showMessage(message.replace("\n", " "), 8000)

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
        """下载线程异常终止（含写库失败这类无法继续的错误）。

        Spec §17：GUI **不能崩**，而且要把"任务失败 / 原因 / 已成功多少 /
        已有数据已保留 / 可以继续"一次说完。这里的"继续"按钮直接指向补齐
        缺失瓦片——失败几乎总是缺了一部分瓦片。
        """
        self._download_worker = None
        self._pause_pending = False
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

        self._refresh_action_state()
        self._report_download_error(message, dialog_title=UI_TEXT_FAILED_TITLE)

    def _report_download_error(self, message: str, *, dialog_title: str) -> None:
        """下载相关的错误出口：日志 + 状态栏 + 非模态弹窗。"""
        self.log_panel.append_log(f"{dialog_title}：{message}", "ERROR")
        self.statusBar().showMessage(f"{dialog_title}：{message}", 8000)
        self._show_error_dialog(dialog_title, message)

    @Slot()
    def _pause_download(self) -> None:
        """暂停：不再发起新请求，已发出的请求允许完成（Spec §13）。

        界面状态先进入 ``PAUSING``，等到下一个进度帧（说明引擎确实收到了暂停
        指令、在途请求也已落地）再落成 ``PAUSED``。一上来就显示"已暂停"是
        在替引擎打包票：那一刻它可能还在跑好几张瓦片。
        """
        worker = self._download_worker
        if worker is None or not worker.isRunning():
            self.log_panel.append_log("当前没有正在运行的下载任务", "WARNING")
            self.statusBar().showMessage("当前没有正在运行的下载任务", 5000)
            self._refresh_action_state()
            return
        worker.pause()
        self._pause_pending = True
        self._refresh_action_state("正在暂停")
        self._set_current_task_status(DownloadTaskStatus.PAUSED)
        self.log_panel.append_log("正在暂停：已发出的请求会正常完成，然后停下来")
        self.statusBar().showMessage("正在暂停…")

    @Slot()
    def _resume_download(self) -> None:
        """继续：放行闸门。

        Spec §14 要求恢复的依据是 **TaskStore + MBTiles 已有内容**，而不是内存里
        的旧状态。因此这里分两种情况：

        - 有一个暂停中的后台线程 → 放行它（同一个任务、同一份内存状态）；
        - 没有后台线程（程序重启过，或任务是从列表里恢复的）→ 走
          :meth:`_on_task_resume_requested`，由它从任务记录 + 已有文件重建计划。

        两条路都通向"跳过已有瓦片、只下缺的那些"，因此用户看到的按钮行为一致。
        """
        worker = self._download_worker
        if worker is not None and worker.isRunning():
            worker.resume()
            self._pause_pending = False
            self._refresh_action_state("下载中")
            self._set_current_task_status(DownloadTaskStatus.DOWNLOADING)
            self.log_panel.append_log("下载已继续")
            self.statusBar().showMessage("下载已继续")
            return

        task = self._selected_task()
        if task is not None and task.can_resume:
            self.log_panel.append_log(
                f"从任务记录恢复下载：{task.name}（{task.status.display_name}）"
            )
            self._on_task_resume_requested(task.id)
            return

        if task is not None:
            self.log_panel.append_log(
                f"任务已完成，无需继续：{task.name}", "WARNING"
            )
            self.statusBar().showMessage("该任务已完成", 5000)
            return

        self.log_panel.append_log("当前没有可继续的下载任务", "WARNING")
        self.statusBar().showMessage("当前没有可继续的下载任务", 5000)
        self._refresh_action_state()

    @Slot()
    def _cancel_download(self) -> None:
        """取消当前任务（Spec §15）。

        必须先确认：取消是不可逆的**动作**（虽然数据不丢，但这一轮不会再继续跑）。
        确认后恢复的路径有两条——下载线程在跑就取消它；只是停在扫描/确认框阶段
        就把计划丢掉、扫描结果作废。

        无论如何都**不删** ``.mbtiles``：那是用户已经花掉的配额与时间。
        """
        worker = self._download_worker
        running = worker is not None and worker.isRunning()
        scan = self._scan_worker
        scanning = scan is not None and scan.isRunning()
        pending = self._pending_plan is not None

        if not (running or scanning or pending):
            self.log_panel.append_log("当前没有可取消的任务", "WARNING")
            self.statusBar().showMessage("当前没有可取消的任务", 5000)
            self._refresh_action_state()
            return

        self._cancel_dialog = self._ask_confirmation(
            UI_TEXT_CANCEL_TITLE,
            UI_TEXT_CANCEL_CONFIRM,
            on_answered=self._on_cancel_answered,
            attr="_cancel_dialog",
            yes_text="取消任务",
            no_text="继续下载",
            default_yes=False,
            log="已弹出取消确认框（已下载的数据会保留）",
        )

    @Slot(int)
    def _on_cancel_answered(self, result: int) -> None:
        """取消确认的答复。"""
        self._cancel_dialog = None
        if result != int(QMessageBox.StandardButton.Yes):
            self.log_panel.append_log("已取消「取消」：任务继续")
            self.statusBar().showMessage("任务继续", 5000)
            return

        worker = self._download_worker
        if worker is not None and worker.isRunning():
            self.log_panel.append_log("正在取消下载任务…")
            worker.cancel()
            # 线程会在收尾（drain 队列 → flush → 关闭数据库）后发出 completed，
            # 由 _on_download_finished 统一落状态；这里只把界面切到"正在取消"。
            self._status_values["task_status"].setText("正在取消")
            self.statusBar().showMessage("正在取消下载任务…")
            return

        scan = self._scan_worker
        if scan is not None and scan.isRunning():
            self.log_panel.append_log("已取消：扫描结果不再使用，未写入任何数据")
            scan.abandon()
            self._scan_followup = None
            self.download_panel.set_scanning(False)
        else:
            self.log_panel.append_log("已取消：未写入任何数据")

        plan = self._pending_plan
        if plan is not None:
            self._pending_plan = None
            self._discard_plan(plan)
        self._refresh_action_state()
        self.statusBar().showMessage("已取消下载", 5000)

    # ------------------------------------------------------------------ #
    # 动作状态机（Spec §6）
    # ------------------------------------------------------------------ #
    def _selected_task(self):
        """任务列表里当前选中的任务对象（没有选中时 ``None``）。

        取的是面板**内存中**的那一份，不重读 ``tasks.json``：
        选中一行不该产生磁盘 IO。
        """
        task_id = self.task_panel.selected_task_id()
        if not task_id:
            return None
        return self.task_panel.task(task_id)

    def _refresh_action_state(self, status_text: str | None = None) -> None:
        """按当前现实重算按钮状态（Spec §6）。

        状态的唯一真相源是"有没有后台线程在跑"，再加上"列表里选中的是哪条任务"。
        统一在这里算、而不是在每个槽里各写一遍 ``if/else``——六个槽各写一遍的
        结果是某天只改了五处，而漏掉的那处只在特定顺序下才会显形。

        Args:
            status_text: 状态行的文案；``None`` 表示用状态本身的名字。
                暂停瞬间要显示"正在暂停"而不是"已暂停"，所以留了这个出口。
        """
        download = self._download_worker
        if download is not None and download.isRunning():
            if self._pause_pending:
                state = UiTaskState.PAUSING
            elif download.pause_requested:
                state = UiTaskState.PAUSED
            else:
                state = UiTaskState.DOWNLOADING
        elif self._scan_worker is not None and self._scan_worker.isRunning():
            state = UiTaskState.SCANNING
        elif self._pending_plan is not None:
            # 弹着确认框：这会儿放开「开始下载」就等于绕过那次确认
            state = UiTaskState.CONFIRMING
        else:
            task = self._selected_task()
            state = UiTaskState.IDLE if task is None else UiTaskState.from_status(task.status)

        self._task_state = state
        self.download_panel.apply_task_state(state)
        self.task_progress.set_status(status_text or state.display_name)

    @Slot(str)
    def _on_task_selection_changed(self, task_id: str) -> None:
        """任务列表选中项变化（Spec §10）：按钮跟着那条任务的状态走。"""
        if not task_id:
            self._refresh_action_state()
            return
        task = self.task_panel.task(task_id)
        if task is not None:
            self.log_panel.append_log(
                f"选中任务：{task.name}（{task.status.display_name}，"
                f"{task.progress_text()}）"
            )
        self._refresh_action_state()

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

        Slice 7 起，"数一数已有多少张"这一步走后台（Spec §11 要求先显示
        "正在扫描已有瓦片…"，而不是让界面僵在原地）。
        """
        if self._busy_worker() is not None:
            self.log_panel.append_log("上一次任务尚未结束，已忽略本次请求", "WARNING")
            self.statusBar().showMessage("当前有任务正在进行，无法同时继续另一个任务", 5000)
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

        # Slice 6：地图类型与"要不要合成"都从任务记录还原。
        # 老记录（Slice ≤5）没有这两项 —— 那时的产物一定是单层底图，
        # 因此 source_map_type 空串、with_labels 为 False 都是**正确**的还原结果。
        map_type = task.source_map_type
        if map_type is None:
            map_type = MapType.from_layer(layer)
        with_labels = bool(getattr(task, "with_labels", False))

        path = Path(task.output_path)
        total = task.total_tiles or estimate_tile_count(
            bounds, task.min_zoom, task.max_zoom
        )

        self.log_panel.append_log(f"Resume task: {task.id}")
        plan = _DownloadPlan(
            provider=TiandituProvider(token),
            bounds=bounds,
            min_zoom=int(task.min_zoom),
            max_zoom=int(task.max_zoom),
            layer=layer,
            output_path=path,
            map_name=task.name,
            description=_describe_plan(
                map_type,
                with_labels,
                int(task.min_zoom),
                int(task.max_zoom),
                bounds,
            ),
            total=total,
            size_text=format_size(estimate_size_bytes(total)),
            resume_policy=ResumePolicy(),
            resume_task=task,
            task=task,
            existing_tiles=0,
            effective_tiles=total,
            map_type=map_type,
            with_labels=with_labels,
            requests=total * map_type.request_count_per_tile(with_labels),
        )
        # 扫描走后台：它是一次 SQLite 查询，而 Spec §7 禁止在 Qt 主线程里做。
        # 目标文件不存在时没有任何可扫的东西，直接按"全新下载"往下走。
        if path.is_file():
            self._begin_scan(plan, self._on_resume_scan_done, purpose="恢复任务")
            return
        self._continue_plan(plan)

    def _on_resume_scan_done(self, plan: _DownloadPlan, scan: ExistingTileScan) -> None:
        """"从任务列表继续"这条路径的扫描回调（Spec §11）。"""
        plan = self._apply_scan(plan, scan)
        # Spec §11 要求把三件事同时说清楚：已有多少 / 目标多少 / 还要下多少
        self._log_resume_counts(plan.existing_tiles, plan.total)
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
    # 槽：设备与传输（Slice 10，Spec §3 ~ §35）
    # ------------------------------------------------------------------ #
    @Slot()
    def _on_refresh_devices(self) -> None:
        """[刷新设备]：在后台探测 ADB 并拉设备列表（Spec §6 / §10）。

        **必须离开主线程**：``adb version`` + ``adb devices -l`` + 每台设备四次
        ``getprop`` 是十几个进程往返，在主线程跑就是肉眼可见的卡顿。
        """
        worker = self._device_worker
        if worker is not None and worker.isRunning():
            self.log_panel.append_log("上一次设备刷新尚未结束，已忽略本次请求", "WARNING")
            return

        # 用户可能刚刚装好 Platform Tools / 改了 ADB_PATH，因此这里顺手清掉探测缓存
        self.device_panel.set_adb_path(self.android_service.client.adb_path)
        self.device_panel.set_busy(True)

        worker = AndroidDeviceWorker(self.android_service, self)
        worker.devices_refreshed.connect(self._on_devices_refreshed)
        worker.log_message.connect(self.log_panel.append_log)
        worker.finished.connect(lambda: self.device_panel.set_busy(False))
        self._device_worker = worker
        self.statusBar().showMessage("正在刷新设备…", 3000)
        worker.start()

    @Slot(object)
    def _on_devices_refreshed(self, outcome) -> None:
        """设备刷新回来了：显示 ADB 状态、设备列表，并在日志里说清结果。"""
        self.device_panel.set_adb_path(self.android_service.client.adb_path)
        self.device_panel.apply_outcome(outcome)

        if not outcome.adb_available:
            # Spec §10：没有 ADB 要说清"怎么办"，而不是只报一句不可用
            self.log_panel.append_log(UI_TEXT_DEVICE_ADB_MISSING, "WARNING")
            self.statusBar().showMessage(UI_TEXT_DEVICE_ADB_MISSING, 5000)
        elif not outcome.devices:
            self.log_panel.append_log("未发现 Android 设备（请确认已连接并开启 USB 调试）")
            self.statusBar().showMessage("未发现 Android 设备", 5000)
        else:
            names = "、".join(device.short_name for device in outcome.devices)
            self.log_panel.append_log(f"已发现 {len(outcome.devices)} 台设备：{names}")
            self.statusBar().showMessage(f"已发现 {len(outcome.devices)} 台设备", 5000)

        for warning in outcome.warnings:
            self.log_panel.append_log(warning, "WARNING")

    @Slot(str)
    def _on_device_changed(self, serial: str) -> None:
        """下拉框换了设备：设备信息已经在面板里了，这里只补一条日志。"""
        if not serial:
            return
        device = self.device_panel.selected_device()
        name = device.short_name if device is not None else serial
        self.log_panel.append_log(LOG_DEVICE_SELECTED.format(serial=serial, name=name))

    @Slot()
    def _on_choose_android_package(self) -> None:
        """[选择地图包]：选一个**包目录**（不是裸 .mbtiles，Spec §12）。"""
        # 起点目录：上次选的包 → 下载输出目录（用户的地图包一般就在那儿旁边）。
        # 取的是 ``output_edit`` 的文本：``DownloadPanel`` 没有 ``output_dir()``
        # 这个方法，写成 ``self.download_panel.output_dir()`` 会在点击时抛
        # ``AttributeError`` —— 而 Qt 会把槽里的异常吞掉，用户看到的是
        # "按钮点了没反应"。（这是 Slice 10 实机验收脚本抓到的真 bug。）
        start_dir = (
            self.device_panel.package_dir
            or self.download_panel.output_edit.text().strip()
            or str(DEFAULT_OUTPUT_DIR)
        )
        chosen = QFileDialog.getExistingDirectory(
            self, UI_TEXT_TRANSFER_CHOOSE_PACKAGE_TITLE, start_dir
        )
        if not chosen:
            return
        self.device_panel.set_package_dir(chosen)
        self.device_panel.reset_transfer()
        # Spec §12：选中之后就要校验；不校验通过不许传。校验会算完整的 SHA256，
        # 因此同样走后台线程。
        self._start_android_package_check(chosen)

    @Slot()
    def _on_validate_android_package(self) -> None:
        """[验证地图包]（Spec §12）。"""
        package_dir = self.device_panel.package_dir
        if not package_dir:
            self.log_panel.append_log(UI_TEXT_TRANSFER_NEED_PACKAGE, "WARNING")
            return
        self._start_android_package_check(package_dir)

    def _start_android_package_check(self, package_dir: str) -> None:
        """起一个后台校验线程（同一时刻最多一个）。"""
        worker = self._package_check_worker
        if worker is not None and worker.isRunning():
            self.log_panel.append_log("地图包校验正在进行，已忽略本次请求", "WARNING")
            return

        self.device_panel.set_package_status("正在验证地图包…")
        self.device_panel.set_busy(True)
        worker = AndroidPackageCheckWorker(self.android_service, package_dir, self)
        worker.check_completed.connect(self._on_android_package_checked)
        worker.log_message.connect(self.log_panel.append_log)
        worker.finished.connect(lambda: self.device_panel.set_busy(False))
        self._package_check_worker = worker
        worker.start()

    @Slot(object)
    def _on_android_package_checked(self, result) -> None:
        """包校验结论：PASS 才允许传输；FAIL 把原因写清楚（Spec §12）。"""
        if result.valid:
            self.device_panel.set_package_status("PASS", "PASS")
            self.log_panel.append_log(
                LOG_PACKAGE_VALIDATED.format(path=result.package_dir)
            )
            self._refresh_remote_dir_preview()
            return

        reasons = "；".join(result.errors) or "校验未通过"
        self.device_panel.set_package_status(f"FAIL：{reasons}", "FAIL")
        self.log_panel.append_log(
            LOG_PACKAGE_INVALID.format(reasons=reasons), "ERROR"
        )

    def _refresh_remote_dir_preview(self) -> None:
        """用 manifest 里的 ``id`` 算出远端目录并显示（Spec §11 / §14 / §15）。

        读 manifest 是纯本地小文件操作（几十字节），不需要再开线程。
        """
        package_dir = self.device_panel.package_dir
        if not package_dir:
            return
        try:
            manifest = self.android_service.transfer.reader.read_manifest(package_dir)
        except Exception as exc:  # noqa: BLE001 - 读不出来就让用户去点"验证地图包"
            self.log_panel.append_log(f"读取 manifest 失败：{exc}", "WARNING")
            self.device_panel.set_remote_dir("")
            return
        self.device_panel.set_remote_dir(
            self.android_service.remote_package_dir(manifest.id)
        )

    @Slot()
    def _on_transfer_to_android(self) -> None:
        """[传输到设备]：先算计划 → 让用户确认 → 再起后台线程（Spec §31）。"""
        serial = self.device_panel.selected_serial
        package_dir = self.device_panel.package_dir

        if not serial:
            self.log_panel.append_log(UI_TEXT_TRANSFER_NEED_DEVICE, "WARNING")
            return
        if not package_dir:
            self.log_panel.append_log(UI_TEXT_TRANSFER_NEED_PACKAGE, "WARNING")
            return
        running = self._transfer_worker
        if running is not None and running.isRunning():
            self.log_panel.append_log(UI_TEXT_TRANSFER_RUNNING, "WARNING")
            return

        plan = self.android_service.plan(serial, package_dir)
        if not plan.is_valid:
            self.device_panel.set_transfer_result(TRANSFER_STATE_FAILED, plan.error or "")
            self.log_panel.append_log(plan.error or "无法开始传输", "ERROR")
            self._show_error_dialog(UI_TEXT_TRANSFER_FAILED_TITLE, plan.error or "")
            return

        self.device_panel.apply_plan(plan)
        self._ask_transfer_confirmation(plan)

    def _ask_transfer_confirmation(self, plan) -> None:
        """传输前的确认框（Spec §31）：设备 / 地图 / 大小 / 远端，四样都要写清。"""
        self._close_dialog("_transfer_confirm_dialog")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(UI_TEXT_TRANSFER_CONFIRM_TITLE)
        box.setText(UI_TEXT_TRANSFER_CONFIRM_BODY.format(
            serial=plan.serial,
            device=plan.device_name,
            package=plan.package_name or plan.package_id,
            size=plan.size_text,
            remote=plan.remote_dir,
        ))
        start = box.addButton(UI_TEXT_TRANSFER_CONFIRM_START, QMessageBox.ButtonRole.AcceptRole)
        box.addButton(UI_TEXT_TRANSFER_CONFIRM_CANCEL, QMessageBox.ButtonRole.RejectRole)
        box.setModal(False)
        # 保存按钮引用而不是把 lambda 连上去：``_close_dialogs`` 需要在关窗时
        # ``disconnect(finished)``，而 lambda 没有可引用的对象。不这么做的话，
        # 关主窗口会顺手触发一次"已取消传输"的假日志。
        self._transfer_confirm_start = start
        box.finished.connect(self._on_transfer_confirmed)
        # 弹窗带 WA_DeleteOnClose：用户关掉它时 C++ 对象就没了，Python 这边必须
        # 同步把属性清掉，否则关主窗口时会对一个空壳调 close()（Slice 7 的教训）。
        self._forget_dialog_on_destroy("_transfer_confirm_dialog", box)
        self._transfer_confirm_dialog = box
        box.show()

    @Slot()
    def _on_transfer_confirmed(self) -> None:
        """用户点了「开始」才真的开始传（Spec §31）。"""
        box = self._transfer_confirm_dialog
        clicked = box is not None and box.clickedButton() is self._transfer_confirm_start
        self._close_dialog("_transfer_confirm_dialog")
        if not clicked:
            self.log_panel.append_log("已取消传输", "INFO")
            return
        self._start_transfer(overwrite=False)

    def _start_transfer(self, *, overwrite: bool) -> None:
        """起传输线程（Spec §29：``adb push`` 绝不跑在 Qt 主线程）。"""
        serial = self.device_panel.selected_serial
        package_dir = self.device_panel.package_dir
        self._transfer_overwrite = overwrite

        worker = AndroidTransferWorker(
            self.android_service,
            serial,
            package_dir,
            self.android_service.remote_root,
            overwrite=overwrite,
            parent=self,
        )
        worker.progress_changed.connect(self._on_transfer_progress)
        worker.status_changed.connect(self._on_transfer_status)
        worker.log_message.connect(self.log_panel.append_log)
        worker.transfer_completed.connect(self._on_transfer_completed)
        worker.transfer_failed.connect(self._on_transfer_failed)
        worker.transfer_cancelled.connect(self._on_transfer_cancelled)
        worker.confirmation_required.connect(self._on_transfer_needs_confirmation)
        worker.finished.connect(self._on_transfer_thread_finished)
        self._transfer_worker = worker

        self.device_panel.reset_transfer(TRANSFER_STATE_RUNNING)
        self.device_panel.set_busy(True)
        self.device_panel.set_cancellable(True)
        self.log_panel.append_log(
            LOG_TRANSFER_STARTED.format(
                package_id=Path(package_dir).name,
                remote=self.device_panel.remote_dir or self.android_service.remote_root,
            )
        )
        self.statusBar().showMessage(UI_TEXT_TRANSFER_RUNNING, 0)
        worker.start()

    @Slot(object)
    def _on_transfer_progress(self, progress) -> None:
        """一帧进度：交给面板显示（Spec §30）。"""
        self.device_panel.set_transfer_progress(progress)

    @Slot(str)
    def _on_transfer_status(self, text: str) -> None:
        """阶段文案（"正在传输 map.mbtiles…"），写在状态栏而不是覆盖状态行。

        状态行显示的是 READY / TRANSFERRING / COMPLETED 这类**状态机取值**
        （Spec §30 明确列出），阶段文案则是"正在做什么"。两者混在一行会让
        "状态"变成一句会变来变去的句子，反而看不出进行到哪一步了。
        """
        self.statusBar().showMessage(text, 0)

    @Slot()
    def _on_cancel_transfer(self) -> None:
        """[取消传输]（Spec §23）：协作式取消，不 terminate 线程。"""
        worker = self._transfer_worker
        if worker is None or not worker.isRunning():
            return
        worker.cancel()
        self.log_panel.append_log("已请求取消传输…", "WARNING")
        self.device_panel.set_cancellable(False)

    @Slot(object)
    def _on_transfer_completed(self, result) -> None:
        """传输完成（Spec §32 / §33）。"""
        self.device_panel.set_transfer_result(TRANSFER_STATE_DONE)
        note = "" if result.checksum_verified else UI_TEXT_TRANSFER_SHA256_UNAVAILABLE
        if note:
            self.device_panel.set_transfer_result(TRANSFER_STATE_DONE, note)
        self._show_transfer_done_dialog(result)
        self.statusBar().showMessage("地图包传输完成", 5000)

    @Slot(object)
    def _on_transfer_cancelled(self, result) -> None:
        """用户取消（Spec §23）：如实说明设备上可能残留 ``.part``。"""
        self.device_panel.set_transfer_result(TRANSFER_STATE_CANCELLED)
        self.statusBar().showMessage("传输已取消", 5000)
        self._close_dialog("_transfer_confirm_dialog")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(UI_TEXT_TRANSFER_CANCELLED_TITLE)
        box.setText(UI_TEXT_TRANSFER_CANCELLED_BODY)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.setModal(False)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._forget_dialog_on_destroy("_transfer_done_dialog", box)
        self._transfer_done_dialog = box
        box.show()

    @Slot(str)
    def _on_transfer_failed(self, reason: str) -> None:
        """传输失败（Spec §35 / §41）。"""
        self.device_panel.set_transfer_result(TRANSFER_STATE_FAILED, reason)
        self.log_panel.append_log(f"传输失败：{reason}", "ERROR")
        self.statusBar().showMessage("地图包传输失败", 5000)
        self._show_error_dialog(
            UI_TEXT_TRANSFER_FAILED_TITLE,
            UI_TEXT_TRANSFER_FAILED_BODY.format(reason=reason),
        )

    @Slot(object)
    def _on_transfer_needs_confirmation(self, result) -> None:
        """设备上已有同名包 → 先问用户（Spec §24）。**绝不静默覆盖。**"""
        self.device_panel.set_transfer_result(TRANSFER_STATE_READY)
        self._close_dialog("_transfer_exists_dialog")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(UI_TEXT_TRANSFER_EXISTS_TITLE)
        box.setText(UI_TEXT_TRANSFER_EXISTS_BODY.format(
            package_id=Path(result.remote_package_dir or "").name,
            remote=result.remote_package_dir or "",
        ))
        overwrite = box.addButton(UI_TEXT_TRANSFER_BUTTON_OVERWRITE, QMessageBox.ButtonRole.AcceptRole)
        box.addButton(UI_TEXT_BUTTON_CANCEL, QMessageBox.ButtonRole.RejectRole)
        box.setModal(False)
        # 理由同上：具名槽 + 保存按钮引用，关窗时才能干净地断开
        self._transfer_overwrite_button = overwrite
        box.finished.connect(self._on_transfer_overwrite_answered)
        self._forget_dialog_on_destroy("_transfer_exists_dialog", box)
        self._transfer_exists_dialog = box
        box.show()

    @Slot()
    def _on_transfer_overwrite_answered(self) -> None:
        """用户选了「覆盖」→ 删掉设备上的旧包重新传（Spec §24）。"""
        box = self._transfer_exists_dialog
        clicked = box is not None and box.clickedButton() is self._transfer_overwrite_button
        self._close_dialog("_transfer_exists_dialog")
        if not clicked:
            self.log_panel.append_log("设备中已有同名地图包，已取消传输", "INFO")
            self.device_panel.set_transfer_result(TRANSFER_STATE_READY)
            return
        self._start_transfer(overwrite=True)

    @Slot()
    def _on_transfer_thread_finished(self) -> None:
        """传输线程结束：把面板从"忙"里放出来。

        刻意**不**在这里把 ``_transfer_worker`` 置 ``None``：主窗口持有引用是
        防止 QThread 在还没被 Qt 回收完时被 Python GC 掉（Slice 3 的坑）。
        """
        self.device_panel.set_busy(False)
        self.device_panel.set_cancellable(False)

    def _show_transfer_done_dialog(self, result) -> None:
        """完成框（Spec §32 / §33）：文件大小 / SHA256 / 状态三行必须分开写。"""
        self._close_dialog("_transfer_done_dialog")
        box = QMessageBox(self)
        # SHA256 不可用时是**警告**不是失败——图标也跟着变（Spec §33）
        box.setIcon(
            QMessageBox.Icon.Information
            if result.checksum_verified
            else QMessageBox.Icon.Warning
        )
        box.setWindowTitle(UI_TEXT_TRANSFER_COMPLETE_TITLE)
        device = self.device_panel.selected_device()
        box.setText(UI_TEXT_TRANSFER_COMPLETE_BODY.format(
            device=device.short_name if device is not None else result.serial,
            remote=result.remote_package_dir or "",
            files=UI_TEXT_TRANSFER_COMPLETE_FILES,
            size_status=result.size_status_text,
            checksum_status=(
                "PASS" if result.checksum_verified else UI_TEXT_TRANSFER_SHA256_UNAVAILABLE
            ),
            status=UI_TEXT_TRANSFER_STATUS_WAITING_IMPORT,
        ))
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.setModal(False)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._forget_dialog_on_destroy("_transfer_done_dialog", box)
        self._transfer_done_dialog = box
        box.show()

    def _close_dialog(self, attribute: str) -> None:
        """关掉并清空一个非模态弹窗（``None`` 或已被 Qt 销毁都要能安全处理）。"""
        box = getattr(self, attribute, None)
        if box is None:
            return
        setattr(self, attribute, None)
        try:
            box.close()
        except RuntimeError:  # pragma: no cover - C++ 对象已被销毁
            pass

    # ------------------------------------------------------------------ #
    # 槽：帮助
    # ------------------------------------------------------------------ #
    @Slot()
    def _on_about(self) -> None:
        QMessageBox.about(
            self,
            f"关于 {APP_NAME}",
            f"<b>{APP_NAME}</b><br>版本 {APP_VERSION}<br><br>"
            "Windows 端生成天地图离线地图包，导入 Android 供 osmdroid 使用。<br>"
            "当前进度：Slice 10（ADB 设备管理与离线地图包传输）。<br>"
            "Android 端的地图包安装（Slice 11）尚未实现。",
        )

    # ------------------------------------------------------------------ #
    # 界面偏好（Slice 7，Spec §22）
    # ------------------------------------------------------------------ #
    def _apply_preferences(self) -> None:
        """启动时恢复上次的输出目录 / 地图类型 / 缩放区间 / 窗口位置。

        恢复**逐项独立**：某一项读不出来或已经被删除（比如上次用的盘符没了）
        只跳过那一项，不影响其余。偏好是锦上添花，不该有任何一条路径让程序起不来。

        Token 不在其中——它从来没被写进偏好里（Spec §9 / §22）。
        """
        prefs = self.preferences

        output_dir = prefs.output_dir
        # 只恢复**仍然存在**的目录。上次用过的临时目录 / 被删掉的外接盘符如果照单
        # 全收，用户下次点「开始下载」会立刻撞上"输出目录不存在"的校验错误——
        # 一个自己造出来的、却要用户去收拾的错误。跳过它，退回默认目录即可。
        if output_dir and os.path.isdir(output_dir):
            self.download_panel.set_output_dir(output_dir)
        elif output_dir:
            self.log_panel.append_log(
                f"上次的输出目录已不存在，已退回默认目录：{output_dir}", "WARNING"
            )

        map_type = MapType.parse(prefs.map_type)
        if map_type is not None:
            self.download_panel.set_map_type(map_type)

        zoom = prefs.zoom_range
        if zoom is not None:
            self.download_panel.set_zoom_range(zoom[0], zoom[1])

        if prefs.restore_geometry(self):
            self.log_panel.append_log("已恢复上次的窗口位置与大小")

    def _save_preferences(self) -> None:
        """关闭时记下这次的输出目录 / 地图类型 / 缩放区间 / 窗口位置。

        存的是**当前界面上的值**（而不是下载任务记录里的值）：用户下次打开
        想看到的是"我上次调到哪儿了"，而不是"上次跑的那个任务用的参数"。
        """
        prefs = self.preferences
        data = self.download_panel.get_form_data()
        prefs.set_output_dir(str(data["output_dir"]))
        prefs.set_map_type(data["map_type"].value)
        prefs.set_zoom_range(data["min_zoom"], data["max_zoom"])
        prefs.save_geometry(self)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名约定
        """关闭前收尾：确认、取消下载、写盘、等线程结束。

        顺序不能换：

        1. **Spec §23**：还有任务在下载时先问一次"退出后任务将暂停，是否退出？"
           用户说"不"就 ``ignore()`` 掉这次关闭，下载继续跑。
        2. 真正退出时取消后台下载（含扫描线程）并 ``wait()``。否则 ``QThread``
           可能在运行时被销毁，Qt 会直接中止进程。取消**不会**丢弃已写入的瓦片：
           service 会 drain 写队列、flush 并关闭数据库，磁盘上留下一个可用的
           （不完整的）``.mbtiles``；任务记录写成 ``PAUSED``，下次启动可以继续。
        3. 最后落下界面偏好（Spec §22）。
        """
        # 还开着的非模态弹窗先关掉，避免它们引用着已销毁的父窗口。
        # 关之前要断开 finished 连接：``close()`` 会发出 Rejected，
        # 不挡掉的话会跑出一堆"已取消下载"的假日志。
        pending = self._pending_plan
        self._pending_plan = None
        self._close_dialogs()
        if pending is not None:
            self._discard_plan(pending)

        # Slice 8：检查窗口可能正在读一个几十万张瓦片的库。关掉它并等它有界
        # 退出——让一个 running 的 QThread 随着父窗口一起被销毁，Qt 会打一行
        # "QThread: Destroyed while thread is still running" 然后中止进程。
        inspector = self._inspector_dialog
        if inspector is not None:
            self.log_panel.append_log("关闭 MBTiles 检查窗口…")
            inspector.shutdown()
            inspector.close()

        # Slice 9：打包窗口可能正在复制一个几 GB 的文件或算 SHA256。与检查窗口
        # 同样的处理：先置中止标记让它尽快收手，再有界等待 —— 构建器收到中止会
        # 把 .building 临时目录清掉，正式地图包一个字节都不会被污染。
        package = self._package_dialog
        if package is not None:
            self.log_panel.append_log("关闭离线地图包窗口…")
            package.shutdown()
            package.close()

        validator = self._package_validator_dialog
        if validator is not None:
            validator.shutdown()
            validator.close()

        # Slice 10：传输线程可能在 push 一个几 GB 的文件。先请求取消（它会 kill
        # 掉 adb push 子进程并清理远端 .part），再有界等待——让一个 running 的
        # QThread 随着父窗口一起销毁，Qt 会中止整个进程。
        transfer = self._transfer_worker
        if transfer is not None and transfer.isRunning():
            self.log_panel.append_log("正在取消地图包传输…")
            transfer.cancel()
            transfer.wait(_DOWNLOAD_CANCEL_WAIT_MS)

        # 设备刷新 / 地图包校验都是只读的短任务，等它们自己结束就行
        for attribute in ("_device_worker", "_package_check_worker"):
            worker = getattr(self, attribute)
            if worker is not None and worker.isRunning():
                if attribute == "_package_check_worker":
                    worker.request_stop()
                worker.wait(_THREAD_SHUTDOWN_WAIT_MS)

        download = self._download_worker
        running = download is not None and download.isRunning()
        if running and not self._exit_confirmed:
            self._ask_exit_with_running_task(event)
            return

        worker = self._connection_worker
        if worker is not None and worker.isRunning():
            self.log_panel.append_log("等待连接测试线程结束…")
            worker.wait(_THREAD_SHUTDOWN_WAIT_MS)

        # 扫描线程还没结束的话，关窗时它可能正在读那个 .mbtiles。
        # 它只读不写，等它一下比让它握着已析构的父窗口更安全。
        scan = self._scan_worker
        if scan is not None and scan.isRunning():
            self.log_panel.append_log("等待已有瓦片扫描结束…")
            scan.abandon()
            scan.wait(_THREAD_SHUTDOWN_WAIT_MS)
        self._scan_worker = None
        self._scan_followup = None

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

        if self._exit_confirmed or not running:
            # 正常退出才写偏好：用户点"取消退出"时窗口还开着，没必要写一遍。
            self._save_preferences()

        super().closeEvent(event)

    def _forget_dialog_on_destroy(self, attr: str, dialog: QMessageBox) -> None:
        """弹窗被销毁时把持有它的属性清成 ``None``。

        这一条不是洁癖，是**实机验收跑出来的缺陷**：所有非模态弹窗都带
        ``WA_DeleteOnClose``，用户一点「关闭」（或右上角 X）C++ 对象当场就没了，
        但 Python 这边的属性还攥着一个壳。之后关主窗口时
        :meth:`_close_dialogs` 会对这个壳调 ``close()``，抛
        ``RuntimeError: Internal C++ object already deleted`` ——异常从
        ``closeEvent`` 里窜出去，``super().closeEvent()`` 根本执行不到，
        用户看到的现象是「点了 X，窗口关不掉」。
        """
        dialog.destroyed.connect(
            lambda *_args, _attr=attr, _box=dialog: (
                setattr(self, _attr, None)
                if getattr(self, _attr, None) is _box
                else None
            )
        )

    def _close_dialogs(self) -> None:
        """关掉所有还开着的非模态弹窗（并断开它们的 finished 连接）。"""
        for attr, slot in (
            ("_confirm_dialog", self._on_large_task_answered),
            ("_exists_dialog", self._on_existing_file_answered),
            ("_restart_dialog", self._on_restart_answered),
            ("_exit_dialog", self._on_exit_confirmed),
            ("_cancel_dialog", self._on_cancel_answered),
            # 完成 / 失败提示没有连接 finished（它们用的是自定义按钮的角色信号），
            # 但仍要把引用清掉，否则父窗口析构后会留着悬空的 QWidget 指针。
            ("_complete_dialog", None),
            ("_failed_dialog", None),
            # Slice 10：传输的四个弹窗。前两个连着具名槽，必须断开——
            # 否则关主窗口时会顺手触发一次"已取消传输"的假日志。
            ("_transfer_confirm_dialog", self._on_transfer_confirmed),
            ("_transfer_exists_dialog", self._on_transfer_overwrite_answered),
            # 完成 / 取消提示只把引用清掉（它们不改变任何状态）
            ("_transfer_done_dialog", None),
            ("_transfer_failed_dialog", None),
        ):
            dialog = getattr(self, attr)
            if dialog is None:
                continue
            setattr(self, attr, None)
            if slot is not None:
                try:
                    dialog.finished.disconnect(slot)
                except (RuntimeError, TypeError):  # pragma: no cover - 本来就没连上
                    pass
            # 兜底：万一还有哪条路径漏了销毁回调，也绝不能让异常从 closeEvent
            # 里抛出去——那会让主窗口关不掉（比丢一个弹窗严重得多）。
            try:
                dialog.close()
            except RuntimeError:  # pragma: no cover - 见 _forget_dialog_on_destroy
                pass

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
