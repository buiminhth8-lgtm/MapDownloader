"""MBTiles 检查工具界面（Slice 8，Spec §16 ~ §19）。

界面结构
--------

::

    ┌──────────────────────────────────────────────┐
    │ 文件：[D:\\maps\\beijing.mbtiles]  [选择文件…] │
    │ 大小：1.42 GB            状态：PASS           │
    ├──────────────────────────────────────────────┤
    │ 基本信息   Name / Format / Bounds / Zoom / …  │
    ├──────────────────────────────────────────────┤
    │ Zoom 统计  Zoom | Tiles | Expected | Missing  │
    ├──────────────────────────────────────────────┤
    │ 图片抽样   Samples / Valid / Corrupt / Empty  │
    ├──────────────────────────────────────────────┤
    │ Errors / Warnings / Notices                   │
    ├──────────────────────────────────────────────┤
    │ [====进度条====]                              │
    │ [快速检查][深度检查][导出报告][打开目录][关闭] │
    └──────────────────────────────────────────────┘

为什么必须有后台线程（Spec §18）
--------------------------------

一次检查会做：``PRAGMA quick_check``（读遍整个文件的页）、若干 ``GROUP BY``、
可选的全量图片解码。这几件事在几十万张瓦片的库上都是秒级到分钟级，
**放在 Qt 主线程就是界面卡死**（连进度条都画不出来，因为主线程在等 SQL）。

因此本模块把检查整体交给 :class:`MBTilesInspectionWorker`（``QThread``），
主线程只收三件事：进度、结果、失败。``MainWindow`` 里不出现任何
``sqlite3`` / ``PIL`` 符号，这条规则由测试守着。

为什么非模态（``show()`` 而不是 ``exec()``）
--------------------------------------------

与 Slice 7 的四个弹窗一致：``exec()`` 会自己开一个嵌套事件循环，
在无头（offscreen）测试里会**直接把用例挂死**，而且用户也没法一边看检查结果
一边去点主窗口里的别的东西。关窗、复制文本、再开一次检查都不受影响。

关掉对话框 ≠ 杀掉检查
---------------------

用户关掉窗口时，后台线程可能正在读第 30 万张瓦片。本模块的做法是：

1. 调 :meth:`MBTilesInspectionWorker.abandon` 立一个"放弃"标记；
2. 检查循环每处理一批就查一次这个标记，尽快收手（见
   :func:`app.mbtiles.diagnostics.deep_scan_tiles` 的 ``should_stop``）；
3. 有界等待（``wait(3000)``）之后无论结果如何都放行。

**绝不**让 ``QThread`` 对象在线程还跑着的时候被销毁 —— 那会让 Qt 打一行
``QThread: Destroyed while thread is still running`` 然后**中止整个进程**。
对话框由主窗口持有（不带 ``WA_DeleteOnClose``），线程对象因此始终有主。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import (
    DIAGNOSTICS_DEEP_SAMPLE_SIZE,
    DIAGNOSTICS_DEFAULT_SAMPLE_SIZE,
    DIAGNOSTICS_STATUS_FAIL,
    DIAGNOSTICS_STATUS_PASS,
    DIAGNOSTICS_STATUS_WARNING,
    UI_TEXT_BUTTON_CLOSE,
    UI_TEXT_INSPECT_CHOOSE_FILE,
    UI_TEXT_INSPECT_COLOR_FAIL,
    UI_TEXT_INSPECT_COLOR_PASS,
    UI_TEXT_INSPECT_COLOR_WARNING,
    UI_TEXT_INSPECT_COLUMN_EXPECTED,
    UI_TEXT_INSPECT_COLUMN_MISSING,
    UI_TEXT_INSPECT_COLUMN_TILES,
    UI_TEXT_INSPECT_COLUMN_ZOOM,
    UI_TEXT_INSPECT_DEEP,
    UI_TEXT_INSPECT_DEEP_HINT,
    UI_TEXT_INSPECT_EMPTY_HINT,
    UI_TEXT_INSPECT_EXPORT,
    UI_TEXT_INSPECT_EXPORT_DONE,
    UI_TEXT_INSPECT_EXPORT_FAILED,
    UI_TEXT_INSPECT_FAILED,
    UI_TEXT_INSPECT_FIELD_BOUNDS,
    UI_TEXT_INSPECT_FIELD_CHECK,
    UI_TEXT_INSPECT_FIELD_COMPOSITION,
    UI_TEXT_INSPECT_FIELD_FORMAT,
    UI_TEXT_INSPECT_FIELD_MAP_TYPE,
    UI_TEXT_INSPECT_FIELD_NAME,
    UI_TEXT_INSPECT_FIELD_SCHEMA,
    UI_TEXT_INSPECT_FIELD_TILES,
    UI_TEXT_INSPECT_FIELD_ZOOM,
    UI_TEXT_INSPECT_FILE_FILTER,
    UI_TEXT_INSPECT_FILE_LABEL,
    UI_TEXT_INSPECT_GROUP_BASIC,
    UI_TEXT_INSPECT_GROUP_DIAG,
    UI_TEXT_INSPECT_GROUP_SAMPLE,
    UI_TEXT_INSPECT_GROUP_ZOOM,
    UI_TEXT_INSPECT_NEED_FILE,
    UI_TEXT_INSPECT_QUICK,
    UI_TEXT_INSPECT_RUNNING,
    UI_TEXT_INSPECT_SAMPLE_FORMATS,
    UI_TEXT_INSPECT_SAMPLE_SUMMARY,
    UI_TEXT_INSPECT_SIZE_LABEL,
    UI_TEXT_INSPECT_STATUS_LABEL,
    UI_TEXT_INSPECT_TITLE,
    UI_TEXT_INSPECT_UNKNOWN,
    UI_TEXT_OPEN_DIRECTORY,
    UI_TEXT_OPEN_DIRECTORY_FAILED,
)
from app.geo import format_size
from app.mbtiles import MBTilesDiagnosticsResult, InspectionProgress
from app.providers import describe_exception
from app.services import MBTilesInspectionService
from app.ui.desktop import open_directory
from app.ui.widgets import WrappingLabel, apply_secondary_text, status_text_color

__all__ = ["MBTilesInspectionWorker", "MBTilesInspectorDialog"]


_logger = logging.getLogger(__name__)

#: 关窗时最多等后台线程多久（毫秒）。深度检查每处理一批就查一次放弃标记，
#: 因此正常情况下一两个批次内就会退出；这里给的是一个兜底上限。
_ABANDON_WAIT_MS = 3000

#: 三个结论对应的颜色。深浅主题通吃靠的是"中等亮度 + 加粗 + 比正文大一号"：
#: 任何颜色都不可能同时对纯白与 #1e1e1e 都达到 4.5:1（数学上不可行），
#: 因此这里按 WCAG 的**大号文字**阈值（3:1）守，并且颜色只是辅助信号 ——
#: 文字本身已经把 PASS / WARNING / FAIL 写出来了。
_STATUS_COLORS: dict[str, str] = {
    DIAGNOSTICS_STATUS_PASS: UI_TEXT_INSPECT_COLOR_PASS,
    DIAGNOSTICS_STATUS_WARNING: UI_TEXT_INSPECT_COLOR_WARNING,
    DIAGNOSTICS_STATUS_FAIL: UI_TEXT_INSPECT_COLOR_FAIL,
}

#: 基本信息区的字段顺序：(取值来源 key, 显示名)
_BASIC_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", UI_TEXT_INSPECT_FIELD_NAME),
    ("format", UI_TEXT_INSPECT_FIELD_FORMAT),
    ("bounds", UI_TEXT_INSPECT_FIELD_BOUNDS),
    ("zoom", UI_TEXT_INSPECT_FIELD_ZOOM),
    ("tiles", UI_TEXT_INSPECT_FIELD_TILES),
    ("file_size", UI_TEXT_INSPECT_SIZE_LABEL),
    ("map_type", UI_TEXT_INSPECT_FIELD_MAP_TYPE),
    ("composition", UI_TEXT_INSPECT_FIELD_COMPOSITION),
    ("tables", UI_TEXT_INSPECT_FIELD_SCHEMA),
    ("sqlite_check", UI_TEXT_INSPECT_FIELD_CHECK),
)


class MBTilesInspectionWorker(QThread):
    """在后台线程里跑一次检查，把进度、结果、失败经信号送回主线程。

    信号名字刻意避开 ``finished``（``QThread`` 自己已经占用了它）。
    结果用 ``object`` 传：自定义 dataclass 走 ``Signal(object)``
    不需要向 Qt 注册元类型（与 Slice 3/7 的 worker 保持同一套写法）。
    """

    #: 进度更新，携带 :class:`~app.mbtiles.InspectionProgress`
    progress_changed = Signal(object)
    #: 检查结束，携带 :class:`~app.mbtiles.MBTilesDiagnosticsResult`
    inspection_completed = Signal(object)
    #: 检查本身炸了（预期之外的异常），携带错误文本
    inspection_failed = Signal(str)
    #: 供主窗口日志区展示的一行，参数为 ``(文本, 级别)``
    log_message = Signal(str, str)

    def __init__(
        self,
        path: str | Path,
        *,
        service: MBTilesInspectionService | None = None,
        sample_size: int = DIAGNOSTICS_DEFAULT_SAMPLE_SIZE,
        check_coverage: bool = True,
        deep_scan: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        """
        Args:
            path: 要检查的 ``.mbtiles``。
            service: 检查服务；``None`` 时用默认实例（测试会注入）。
            sample_size: 图片抽样张数。
            check_coverage: 是否做覆盖完整性检查。
            deep_scan: 是否做全量图片扫描。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._path = str(path)
        self._service = service or MBTilesInspectionService()
        self._sample_size = sample_size
        self._check_coverage = check_coverage
        self._deep_scan = deep_scan
        self._abandoned = False

    # ------------------------------------------------------------------ #
    # 只读状态
    # ------------------------------------------------------------------ #
    @property
    def path(self) -> str:
        """被检查的文件路径。"""
        return self._path

    @property
    def deep_scan(self) -> bool:
        """本次是不是深度检查。"""
        return self._deep_scan

    @property
    def abandoned(self) -> bool:
        """是否已被调用方放弃。"""
        return self._abandoned

    # ------------------------------------------------------------------ #
    # 控制
    # ------------------------------------------------------------------ #
    def abandon(self) -> None:
        """放弃本次检查（用户关掉了对话框）。

        不试图中断正在执行的那一条 SQL —— 单条查询最多几秒，而
        ``sqlite3.Connection.interrupt()`` 需要另一条线程去调、代价更大。
        标记立好之后，检查循环会在**下一批**就收手。
        """
        self._abandoned = True

    def _should_stop(self) -> bool:
        """给检查服务用的中止条件。"""
        return self._abandoned

    # ------------------------------------------------------------------ #
    # 线程入口
    # ------------------------------------------------------------------ #
    def run(self) -> None:  # noqa: D102 - QThread 约定的入口
        name = Path(self._path).name
        mode = "深度检查" if self._deep_scan else "快速检查"
        self.log_message.emit(f"开始{mode}：{name}", "INFO")
        try:
            result = self._service.inspect(
                self._path,
                self._sample_size,
                self._check_coverage,
                self._deep_scan,
                self.progress_changed.emit,
                self._should_stop,
            )
        except Exception as exc:  # noqa: BLE001 - 绝不让异常冒泡进 Qt 事件循环
            message = describe_exception(exc)
            self.log_message.emit(f"{mode}失败：{message}", "ERROR")
            self.inspection_failed.emit(message)
            return

        self.log_message.emit(
            f"{mode}完成：{name} → {result.status}"
            f"（瓦片 {result.tile_count:,}，缺失 {result.missing_text}）",
            "INFO" if result.valid else "WARNING",
        )
        self.inspection_completed.emit(result)


class MBTilesInspectorDialog(QDialog):
    """MBTiles 检查对话框（Spec §17）。

    只做三件事：**显示结果、发按钮请求、把失败说清楚**。
    所有计算都在 :class:`MBTilesInspectionWorker` 里。
    """

    #: 想写进主窗口日志区的一行，参数为 ``(文本, 级别)``
    log_message = Signal(str, str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        service: MBTilesInspectionService | None = None,
        path: str | Path | None = None,
    ) -> None:
        """
        Args:
            parent: Qt 父对象（通常是主窗口）。
            service: 检查服务；``None`` 时用默认实例（测试会注入一个假服务）。
            path: 预填的文件路径。
        """
        super().__init__(parent)
        self.setWindowTitle(UI_TEXT_INSPECT_TITLE)
        self.resize(860, 720)
        self.setMinimumSize(620, 520)

        self._service = service or MBTilesInspectionService()
        self._worker: MBTilesInspectionWorker | None = None
        #: 最近一次检查的结论（没检查过为 ``None``）
        self.result: MBTilesDiagnosticsResult | None = None
        #: 打开文件选择对话框时的起始目录（会话内记住，不落盘）
        self._last_directory = ""

        self._build_widgets()
        if path:
            self.set_path(path)

    # ------------------------------------------------------------------ #
    # 构建界面
    # ------------------------------------------------------------------ #
    def _build_widgets(self) -> None:
        self.path_edit = QLineEdit(self)
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText(UI_TEXT_INSPECT_EMPTY_HINT)
        self.choose_button = QPushButton(UI_TEXT_INSPECT_CHOOSE_FILE, self)

        file_row = QHBoxLayout()
        file_label = QLabel(UI_TEXT_INSPECT_FILE_LABEL, self)
        apply_secondary_text(file_label)
        file_row.addWidget(file_label)
        file_row.addWidget(self.path_edit, 1)
        file_row.addWidget(self.choose_button)

        self.size_value = QLabel(UI_TEXT_INSPECT_UNKNOWN, self)
        self.status_value = QLabel(UI_TEXT_INSPECT_UNKNOWN, self)
        self._make_status_font(self.status_value)
        self._paint_status(UI_TEXT_INSPECT_UNKNOWN)

        summary_row = QHBoxLayout()
        size_label = QLabel(UI_TEXT_INSPECT_SIZE_LABEL, self)
        status_label = QLabel(UI_TEXT_INSPECT_STATUS_LABEL, self)
        apply_secondary_text(size_label)
        apply_secondary_text(status_label)
        summary_row.addWidget(size_label)
        summary_row.addWidget(self.size_value)
        summary_row.addStretch(1)
        summary_row.addWidget(status_label)
        summary_row.addWidget(self.status_value)

        layout = QVBoxLayout(self)
        layout.addLayout(file_row)
        layout.addLayout(summary_row)
        layout.addWidget(self._build_basic_group())
        layout.addWidget(self._build_zoom_group())
        layout.addWidget(self._build_sample_group())
        layout.addWidget(self._build_diagnostics_group(), 1)
        layout.addWidget(self._build_progress_row())
        layout.addLayout(self._build_button_row())

        self.choose_button.clicked.connect(self._on_choose_file)
        self.buttons["quick"].clicked.connect(self.start_quick_check)
        self.buttons["deep"].clicked.connect(self.start_deep_check)
        self.buttons["export"].clicked.connect(self._on_export_report)
        self.buttons["open_dir"].clicked.connect(self._on_open_directory)
        self.buttons["close"].clicked.connect(self._on_close_clicked)

        self._reset_view()
        self.set_running(False)

    @staticmethod
    def _make_status_font(widget: QLabel) -> None:
        """结论文字用**加粗 + 大一号**：颜色是辅助信号，字重与字号才是主力。"""
        font = widget.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 2)
        widget.setFont(font)

    def _build_basic_group(self) -> QGroupBox:
        group = QGroupBox(UI_TEXT_INSPECT_GROUP_BASIC, self)
        form = QFormLayout(group)
        self.info_values: dict[str, QLabel] = {}
        for key, title in _BASIC_FIELDS:
            value = WrappingLabel(UI_TEXT_INSPECT_UNKNOWN, group)
            self.info_values[key] = value
            form.addRow(title, value)
        return group

    def _build_zoom_group(self) -> QGroupBox:
        group = QGroupBox(UI_TEXT_INSPECT_GROUP_ZOOM, self)
        self.zoom_table = QTableWidget(0, 4, group)
        self.zoom_table.setHorizontalHeaderLabels(
            [
                UI_TEXT_INSPECT_COLUMN_ZOOM,
                UI_TEXT_INSPECT_COLUMN_TILES,
                UI_TEXT_INSPECT_COLUMN_EXPECTED,
                UI_TEXT_INSPECT_COLUMN_MISSING,
            ]
        )
        self.zoom_table.verticalHeader().setVisible(False)
        self.zoom_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.zoom_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.zoom_table.setAlternatingRowColors(True)
        # 表格自己不得决定对话框宽度（长数字列会把窗口顶宽）
        self.zoom_table.setSizeAdjustPolicy(
            QTableWidget.SizeAdjustPolicy.AdjustIgnored
        )
        self.zoom_table.setMinimumHeight(96)
        self.zoom_table.setMaximumHeight(170)
        header = self.zoom_table.horizontalHeader()
        for index in range(4):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)

        layout = QVBoxLayout(group)
        layout.addWidget(self.zoom_table)
        return group

    def _build_sample_group(self) -> QGroupBox:
        group = QGroupBox(UI_TEXT_INSPECT_GROUP_SAMPLE, self)
        self.sample_value = WrappingLabel(
            UI_TEXT_INSPECT_SAMPLE_SUMMARY.format(
                samples=UI_TEXT_INSPECT_UNKNOWN,
                valid=UI_TEXT_INSPECT_UNKNOWN,
                corrupt=UI_TEXT_INSPECT_UNKNOWN,
                empty=UI_TEXT_INSPECT_UNKNOWN,
            ),
            group,
        )
        self.sample_formats_value = WrappingLabel(
            UI_TEXT_INSPECT_SAMPLE_FORMATS.format(formats=UI_TEXT_INSPECT_UNKNOWN),
            group,
        )
        apply_secondary_text(self.sample_formats_value)

        layout = QVBoxLayout(group)
        layout.addWidget(self.sample_value)
        layout.addWidget(self.sample_formats_value)
        return group

    def _build_diagnostics_group(self) -> QGroupBox:
        group = QGroupBox(UI_TEXT_INSPECT_GROUP_DIAG, self)
        self.diagnostics_text = QPlainTextEdit(group)
        self.diagnostics_text.setReadOnly(True)
        self.diagnostics_text.setMinimumHeight(110)
        layout = QVBoxLayout(group)
        layout.addWidget(self.diagnostics_text)
        return group

    def _build_progress_row(self) -> QWidget:
        container = QWidget(self)
        self.progress_bar = QProgressBar(container)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_label = QLabel("", container)
        self.progress_label.setWordWrap(True)

        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.progress_label)
        return container

    def _build_button_row(self) -> QHBoxLayout:
        self.buttons: dict[str, QPushButton] = {
            "quick": QPushButton(UI_TEXT_INSPECT_QUICK, self),
            "deep": QPushButton(UI_TEXT_INSPECT_DEEP, self),
            "export": QPushButton(UI_TEXT_INSPECT_EXPORT, self),
            "open_dir": QPushButton(UI_TEXT_OPEN_DIRECTORY, self),
            "close": QPushButton(UI_TEXT_BUTTON_CLOSE, self),
        }
        # Spec §17 明确要求：深度检查可能耗时较长，界面上必须先说清楚。
        self.buttons["deep"].setToolTip(UI_TEXT_INSPECT_DEEP_HINT)
        self.buttons["quick"].setToolTip("统计 + metadata + 抽样 —— 日常用这个")

        row = QHBoxLayout()
        for button in self.buttons.values():
            button.setProperty("action_id", button.text())
            row.addWidget(button)
        row.addStretch(1)
        return row

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    @property
    def inspection_worker(self) -> MBTilesInspectionWorker | None:
        """当前的后台检查线程（没有则为 ``None``）；主窗口关窗时要用。"""
        return self._worker

    @property
    def is_running(self) -> bool:
        """是否正在检查。"""
        return self._worker is not None and self._worker.isRunning()

    @property
    def path(self) -> str:
        """当前选中的文件路径（未选为 ``""``）。"""
        return self.path_edit.text().strip()

    def set_path(self, path: str | Path) -> None:
        """设置要检查的文件（不清空已有结果 —— 用户可能想对照着看）。"""
        text = str(path)
        self.path_edit.setText(text)
        target = Path(text)
        if target.exists():
            self._last_directory = str(target.parent)

    def start_quick_check(self) -> None:
        """[快速检查]：结构 + metadata + 统计 + 坐标 + 覆盖 + 抽样图片。"""
        self._start_inspection(deep=False)

    def start_deep_check(self) -> None:
        """[深度检查]：在快速检查之外，逐张校验全部 ``tile_data``。"""
        self._start_inspection(deep=True)

    def shutdown(self, timeout_ms: int = _ABANDON_WAIT_MS) -> None:
        """放弃并等待后台线程（主窗口关窗时调用）。

        有界等待：深度检查每批都查放弃标记，正常情况下一两个批次就退出；
        万一某条 SQL 正卡着，也绝不能让"关程序"变成"程序无响应"。
        """
        worker = self._worker
        if worker is None:
            return
        worker.abandon()
        if worker.isRunning():
            worker.wait(timeout_ms)
        self._worker = None
        self.set_running(False)

    # ------------------------------------------------------------------ #
    # 内部：启动
    # ------------------------------------------------------------------ #
    def _start_inspection(self, *, deep: bool) -> None:
        if self.is_running:
            return
        path = self.path
        if not path:
            self._set_status_line(UI_TEXT_INSPECT_NEED_FILE, "WARNING")
            self.log_message.emit(UI_TEXT_INSPECT_NEED_FILE, "WARNING")
            return

        self._reset_view()
        self.set_running(True)
        self.progress_bar.setRange(0, 0)  # 不确定模式：还不知道总共有多少活
        self._set_status_line(UI_TEXT_INSPECT_RUNNING, "INFO")

        worker = MBTilesInspectionWorker(
            path,
            service=self._service,
            sample_size=(
                DIAGNOSTICS_DEEP_SAMPLE_SIZE if deep else DIAGNOSTICS_DEFAULT_SAMPLE_SIZE
            ),
            check_coverage=True,
            deep_scan=deep,
            parent=self,
        )
        worker.progress_changed.connect(self._on_progress)
        worker.inspection_completed.connect(self._on_completed)
        worker.inspection_failed.connect(self._on_failed)
        worker.log_message.connect(self.log_message.emit)
        self._worker = worker
        worker.start()

    @Slot(object)
    def _on_progress(self, progress: InspectionProgress) -> None:
        """把进度画到进度条上（``percent < 0`` 时切回不确定模式）。"""
        if not isinstance(progress, InspectionProgress):  # pragma: no cover - 防御
            return
        percent = progress.percent
        if percent < 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(percent)
        self._set_status_line(progress.message, "INFO")

    @Slot(object)
    def _on_completed(self, result: MBTilesDiagnosticsResult) -> None:
        """检查结束：把结论铺到各个区域。"""
        self.result = result
        self._worker = None
        self.set_running(False)
        # 收尾时把进度条拉到满格：不看一眼进度条的话，停在 60% 会让人以为
        # "还没跑完就已经出结论了"。最后那条进度事件的总量是 0（不可量化），
        # 会把进度条切回不确定模式，所以这里必须显式定住。
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self._render(result)
        self._set_status_line(f"检查完成：{result.status}", "INFO")

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        """检查线程崩了：界面必须说清楚，而且不能装成"文件没问题"。"""
        self._worker = None
        self.set_running(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_value.setText(DIAGNOSTICS_STATUS_FAIL)
        self._paint_status(DIAGNOSTICS_STATUS_FAIL)
        self._set_status_line(UI_TEXT_INSPECT_FAILED.format(error=message), "ERROR")
        self.diagnostics_text.setPlainText(UI_TEXT_INSPECT_FAILED.format(error=message))

    # ------------------------------------------------------------------ #
    # 内部：渲染结果
    # ------------------------------------------------------------------ #
    def _reset_view(self) -> None:
        """清掉上一次的结论（开跑新一次检查之前调用）。"""
        self.result = None
        self.size_value.setText(UI_TEXT_INSPECT_UNKNOWN)
        self.status_value.setText(UI_TEXT_INSPECT_UNKNOWN)
        self._paint_status(UI_TEXT_INSPECT_UNKNOWN)
        for value in self.info_values.values():
            value.set_text(UI_TEXT_INSPECT_UNKNOWN)
        self.zoom_table.setRowCount(0)
        self.sample_value.set_text(
            UI_TEXT_INSPECT_SAMPLE_SUMMARY.format(
                samples=UI_TEXT_INSPECT_UNKNOWN,
                valid=UI_TEXT_INSPECT_UNKNOWN,
                corrupt=UI_TEXT_INSPECT_UNKNOWN,
                empty=UI_TEXT_INSPECT_UNKNOWN,
            )
        )
        self.sample_formats_value.set_text(
            UI_TEXT_INSPECT_SAMPLE_FORMATS.format(formats=UI_TEXT_INSPECT_UNKNOWN)
        )
        self.diagnostics_text.setPlainText(UI_TEXT_INSPECT_EMPTY_HINT)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_label.setText("")

    def _render(self, result: MBTilesDiagnosticsResult) -> None:
        """把一个 :class:`MBTilesDiagnosticsResult` 铺到界面上。"""
        self.size_value.setText(
            format_size(result.file_size) if result.file_size else UI_TEXT_INSPECT_UNKNOWN
        )
        self.status_value.setText(result.status)
        self._paint_status(result.status)

        values = {
            "name": result.metadata.get("name", ""),
            "format": result.format or "",
            "bounds": result.bounds_text or "",
            "zoom": result.zoom_text,
            "tiles": f"{result.tile_count:,}",
            "file_size": format_size(result.file_size) if result.file_size else "",
            "map_type": result.map_type or "",
            "composition": result.composition or "",
            "tables": "、".join(result.tables) if result.tables else "",
            "sqlite_check": result.sqlite_check or "",
        }
        for key, value in values.items():
            self.info_values[key].set_text(value.strip() or UI_TEXT_INSPECT_UNKNOWN)

        self._render_zoom_table(result)
        self.sample_value.set_text(
            UI_TEXT_INSPECT_SAMPLE_SUMMARY.format(
                samples=result.sample_size,
                valid=result.sample_valid,
                corrupt=result.sample_corrupt,
                empty=result.sample_empty,
            )
        )
        self.sample_formats_value.set_text(
            UI_TEXT_INSPECT_SAMPLE_FORMATS.format(formats=result.sampled_formats_text)
        )
        self.diagnostics_text.setPlainText("\n".join(self._diagnostic_lines(result)))

    def _render_zoom_table(self, result: MBTilesDiagnosticsResult) -> None:
        """Zoom 统计表：一行一个缩放级别，Expected / Missing 没算就是 ``—``。"""
        stats = result.zoom_stats
        self.zoom_table.setRowCount(len(stats))
        for row, item in enumerate(stats):
            cells = (
                str(item.zoom),
                f"{item.tile_count:,}",
                f"{item.expected_tiles:,}" if item.expected_tiles is not None else "—",
                f"{item.missing_tiles:,}" if item.missing_tiles is not None else "—",
            )
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                cell.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                self.zoom_table.setItem(row, column, cell)

    @staticmethod
    def _diagnostic_lines(result: MBTilesDiagnosticsResult) -> list[str]:
        """错误 / 告警 / 提示三段文字。

        「提示」单独一段是必要的：Spec §20 的 PASS 允许存在普通提示，
        把它们混进告警会让用户以为文件有问题。

        前缀用中文（而不是 ``[ERROR]`` / ``[INFO]``）是为了跟界面其余部分一致
        —— 一份通篇中文的检查结果里夹几个英文方括号，看起来像漏改的调试输出。
        """
        if not (result.errors or result.warnings or result.notices):
            return [f"{result.status}（没有发现任何问题）"]
        lines: list[str] = []
        lines.extend(f"[错误] {message}" for message in result.errors)
        lines.extend(f"[告警] {message}" for message in result.warnings)
        lines.extend(f"[提示] {message}" for message in result.notices)
        return lines

    def _paint_status(self, status: str) -> None:
        """按结论给状态文字上色。

        颜色经 :func:`app.ui.widgets.status_text_color` 按当前主题调过明度 ——
        固定色值在深色主题下会糊成一片（绿 3.45:1 / 琥珀 2.16:1 都是
        "看得见控件、看不见字"的量级）。颜色只是辅助信号，文字本身
        已经把 PASS / WARNING / FAIL 写出来了。
        """
        base = _STATUS_COLORS.get(status)
        if base is None:
            self.status_value.setStyleSheet("")
            return
        color = status_text_color(self.status_value.palette(), base)
        self.status_value.setStyleSheet(f"color: {color.name()};")

    def _set_status_line(self, text: str, level: str) -> None:
        """底部一行的状态说明。"""
        self.progress_label.setText(text)
        if level in ("WARNING", "ERROR"):
            self.log_message.emit(text, level)

    # ------------------------------------------------------------------ #
    # 内部：按钮槽
    # ------------------------------------------------------------------ #
    @Slot()
    def _on_choose_file(self) -> None:
        """选一个 .mbtiles。文件对话框是**静态**方法，测试里可以直接换掉。"""
        selected, _ = QFileDialog.getOpenFileName(
            self,
            UI_TEXT_INSPECT_CHOOSE_FILE,
            self._last_directory or self.path,
            UI_TEXT_INSPECT_FILE_FILTER,
        )
        if not selected:
            return
        self.set_path(selected)
        self.log_message.emit(f"已选择文件：{selected}", "INFO")

    @Slot()
    def _on_export_report(self) -> None:
        """把当前结论导出成 ``xxx.mbtiles.report.txt``（Spec §26）。"""
        if self.result is None:
            self._set_status_line("还没有检查结果，先点「快速检查」", "WARNING")
            return
        try:
            target = self._service.export_report(self.result)
        except OSError as exc:
            message = UI_TEXT_INSPECT_EXPORT_FAILED.format(error=describe_exception(exc))
            self._set_status_line(message, "ERROR")
            return
        message = UI_TEXT_INSPECT_EXPORT_DONE.format(path=target)
        self._set_status_line(message, "INFO")
        self.log_message.emit(message, "INFO")

    @Slot()
    def _on_close_clicked(self) -> None:
        """[关闭]：走 ``close()``，于是清理逻辑统一落在 :meth:`closeEvent`。"""
        self.close()

    @Slot()
    def _on_open_directory(self) -> None:
        """打开被检查文件所在目录。"""
        path = self.path
        if not path:
            self._set_status_line(UI_TEXT_INSPECT_NEED_FILE, "WARNING")
            return
        ok, detail = open_directory(Path(path).parent)
        if not ok:
            self._set_status_line(
                UI_TEXT_OPEN_DIRECTORY_FAILED.format(path=Path(path).parent, error=detail),
                "WARNING",
            )

    # ------------------------------------------------------------------ #
    # 运行中 / 空闲的按钮态
    # ------------------------------------------------------------------ #
    def set_running(self, running: bool) -> None:
        """检查过程中禁用"再开一次"，但**保留「关闭」**。

        关掉对话框必须随时可用（用户有权不等），关的时候会
        :meth:`MBTilesInspectionWorker.abandon` 让后台尽快收手。
        """
        self.buttons["quick"].setEnabled(not running)
        self.buttons["deep"].setEnabled(not running)
        self.buttons["export"].setEnabled(not running)
        self.choose_button.setEnabled(not running)
        self.buttons["close"].setEnabled(True)

    # ------------------------------------------------------------------ #
    # 关窗
    # ------------------------------------------------------------------ #
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名约定
        """关掉对话框时放弃正在跑的检查，并**有界等待**线程退出。

        不等待就直接让对话框带着一个 running 的 ``QThread`` 被销毁，Qt 会打
        ``QThread: Destroyed while thread is still running`` 并**中止进程**
        —— 用户看到的是"点了个关闭，整个程序没了"。
        """
        try:
            self.shutdown()
        except Exception as exc:  # noqa: BLE001 - 关窗路径不许抛异常
            _logger.warning("关闭检查对话框时清理后台线程失败：%s", exc)
        super().closeEvent(event)
