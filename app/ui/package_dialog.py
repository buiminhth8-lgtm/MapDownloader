"""离线地图包界面（Slice 9，Spec §16 ~ §21）。

界面结构
--------

::

    ┌──────────────────────────────────────────────────────────┐
    │ 源 MBTiles：[D:\\maps\\beijing.mbtiles]      [选择文件…]      │
    ├──────────────────────────────────────────────────────────┤
    │ 地图信息   地图类型：矢量 + 中文注记                        │
    │            Zoom：8 ~ 17        瓦片：52,381               │
    ├──────────────────────────────────────────────────────────┤
    │ 地图名称：[北京离线地图]                                   │
    │ 输出目录：[D:\\maps\\packages]                [选择目录…]    │
    ├──────────────────────────────────────────────────────────┤
    │ 地图包内容  目录 / MBTiles / 瓦片 / Zoom / SHA256 / 验证     │
    ├──────────────────────────────────────────────────────────┤
    │ 检查与验证结果  [文本区]                                   │
    ├──────────────────────────────────────────────────────────┤
    │ [====进度条====]  正在复制地图文件…1.20 GB / 4.00 GB        │
    │ [生成地图包][验证地图包][打开目录][关闭]                    │
    └──────────────────────────────────────────────────────────┘

三个必须守住的东西
------------------

**1. 界面不自己算地图信息（Spec §6）。** 「地图类型 / Zoom / 瓦片」三行全部
来自后台的 MBTiles 快速检查（:class:`~app.ui.mbtiles_inspector_dialog.MBTilesInspectionWorker`
—— 与 Slice 8 的检查窗口共用同一个 worker），界面既不解析 metadata 也不数瓦片。
用户能改的只有包名：那是"包叫什么"，不是"包里是什么"。

**2. 大文件操作一律不占主线程（Spec §18 / §19）。** 复制 4 GB 与算 SHA256
在这里各是一个后台线程；主线程只收进度、收结果、把结论画出来。
**后台线程绝不碰任何 QWidget** —— 它只 emit 信号。

**3. 需要用户拍板的两件事都问，而且都非模态（Spec §16 / §23）。**
「MBTiles 有警告，继续吗？」与「地图包已存在：[覆盖][选择其他名称][取消]」
都是 ``show()`` + 按钮 ``clicked`` 回调，**绝不用 exec()** —— 无头测试下
``exec()`` 会把用例直接挂死（Slice 4 起的老规矩）。
覆盖也不是删除：构建器会把旧包改名备份（Spec §16）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import (
    DIAGNOSTICS_STATUS_FAIL,
    DIAGNOSTICS_STATUS_PASS,
    PACKAGE_PRECHECK_SAMPLE_SIZE,
    UI_TEXT_BUTTON_CANCEL,
    UI_TEXT_BUTTON_CLOSE,
    UI_TEXT_INSPECT_UNKNOWN,
    UI_TEXT_OPEN_DIRECTORY,
    UI_TEXT_OPEN_DIRECTORY_FAILED,
    UI_TEXT_PACKAGE_BACKUP_DONE,
    UI_TEXT_PACKAGE_BODY_PLACEHOLDER,
    UI_TEXT_PACKAGE_BUTTON,
    UI_TEXT_PACKAGE_BUTTON_CONTINUE,
    UI_TEXT_PACKAGE_BUTTON_HINT,
    UI_TEXT_PACKAGE_BUTTON_OVERWRITE,
    UI_TEXT_PACKAGE_BUTTON_RENAME,
    UI_TEXT_PACKAGE_BUTTON_VALIDATE,
    UI_TEXT_PACKAGE_BUTTON_VALIDATE_HINT,
    UI_TEXT_PACKAGE_CHOOSE_DIR,
    UI_TEXT_PACKAGE_CHOOSE_FILE,
    UI_TEXT_PACKAGE_COLOR_FAIL,
    UI_TEXT_PACKAGE_COLOR_PASS,
    UI_TEXT_PACKAGE_COMPLETE_TITLE,
    UI_TEXT_PACKAGE_EXISTS_BODY,
    UI_TEXT_PACKAGE_EXISTS_TITLE,
    UI_TEXT_PACKAGE_FAILED,
    UI_TEXT_PACKAGE_FILE_FILTER,
    UI_TEXT_PACKAGE_GROUP_DIAG,
    UI_TEXT_PACKAGE_GROUP_INFO,
    UI_TEXT_PACKAGE_GROUP_PACKAGE,
    UI_TEXT_PACKAGE_GROUP_SOURCE,
    UI_TEXT_PACKAGE_MAP_TYPE_LABEL,
    UI_TEXT_PACKAGE_MBTILES_LABEL,
    UI_TEXT_PACKAGE_NAME_LABEL,
    UI_TEXT_PACKAGE_NEED_DIR,
    UI_TEXT_PACKAGE_NEED_NAME,
    UI_TEXT_PACKAGE_NEED_PACKAGE,
    UI_TEXT_PACKAGE_NEED_SOURCE,
    UI_TEXT_PACKAGE_OUTPUT_LABEL,
    UI_TEXT_PACKAGE_PACKAGE_LABEL,
    UI_TEXT_PACKAGE_RESULT_LABEL,
    UI_TEXT_PACKAGE_RUNNING,
    UI_TEXT_PACKAGE_SHA256_LABEL,
    UI_TEXT_PACKAGE_SOURCE_LABEL,
    UI_TEXT_PACKAGE_TILES_LABEL,
    UI_TEXT_PACKAGE_TITLE,
    UI_TEXT_PACKAGE_UNKNOWN,
    UI_TEXT_PACKAGE_VALIDATE_BODY,
    UI_TEXT_PACKAGE_VALIDATE_DONE_TITLE,
    UI_TEXT_PACKAGE_VALIDATE_FAILED,
    UI_TEXT_PACKAGE_VALIDATE_TITLE,
    UI_TEXT_PACKAGE_VALIDATING,
    UI_TEXT_PACKAGE_WARNING_BODY,
    UI_TEXT_PACKAGE_WARNING_TITLE,
    UI_TEXT_PACKAGE_ZOOM_LABEL,
)
from app.geo import format_size
from app.mbtiles import MBTilesDiagnosticsResult
from app.models.map_layer import MapType, describe_map_type
from app.package import (
    PackageBuildResult,
    PackageProgress,
    PackageValidationResult,
)
from app.providers import describe_exception
from app.services import MapPackageService
from app.ui.desktop import open_directory
from app.ui.mbtiles_inspector_dialog import MBTilesInspectionWorker
from app.ui.widgets import WrappingLabel, apply_secondary_text, status_text_color

__all__ = [
    "PackageBuildWorker",
    "PackageDialog",
    "PackageValidateDialog",
    "PackageValidateWorker",
]

_logger = logging.getLogger(__name__)

#: 关窗时最多等后台线程多久（毫秒）。构建的中止是"每块检查一次"，
#: 因此正常情况下几十毫秒内就会收手；这里给的是兜底上限。
_ABANDON_WAIT_MS = 3000


# --------------------------------------------------------------------------- #
# 后台线程
# --------------------------------------------------------------------------- #
class PackageBuildWorker(QThread):
    """在后台线程里构建一个地图包（Spec §19）。

    信号名字刻意避开 ``finished``（``QThread`` 自己占用了它）；结果用
    ``object`` 传自定义 dataclass，不需要向 Qt 注册元类型 —— 与 Slice 3/7/8
    的 worker 保持同一套写法。
    """

    #: 进度更新，携带 :class:`~app.package.PackageProgress`
    progress_changed = Signal(object)
    #: 当前在做什么（一句话，直接显示在进度条下面）
    status_changed = Signal(str)
    #: 构建成功（**校验也通过**），携带 :class:`~app.package.PackageBuildResult`
    package_completed = Signal(object)
    #: 构建失败（预期之外的异常），携带错误文本
    package_failed = Signal(str)
    #: 需要用户拍板才能继续（Spec §16 目标已存在 / §23 MBTiles 有警告），
    #: 携带 :class:`~app.package.PackageBuildResult`（``needs_confirmation=True``）。
    #: 单独一条信号而不是复用 ``package_completed``：这不是"完成"，界面上
    #: 绝不能把它显示成"生成失败"或"生成完成"。
    confirmation_required = Signal(object)
    #: 供主窗口日志区展示的一行，参数为 ``(文本, 级别)``
    log_message = Signal(str, str)

    def __init__(
        self,
        mbtiles_path: str | Path,
        output_dir: str | Path,
        package_name: str,
        *,
        service: MapPackageService | None = None,
        diagnostics: MBTilesDiagnosticsResult | None = None,
        accept_warnings: bool = False,
        overwrite: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        """
        Args:
            mbtiles_path: 源 ``.mbtiles``。
            output_dir: 输出目录。
            package_name: 包名（也是目录名）。
            service: 打包服务；``None`` 时用默认实例（测试会注入）。
            diagnostics: 界面已经算好的源文件检查结论（省一次 ``quick_check``）。
            accept_warnings: 用户已在确认框里选了"继续生成"。
            overwrite: 用户已在确认框里选了「覆盖」。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._path = str(mbtiles_path)
        self._output_dir = str(output_dir)
        self._package_name = str(package_name)
        self._service = service or MapPackageService()
        self._diagnostics = diagnostics
        self._accept_warnings = accept_warnings
        self._overwrite = overwrite
        self._abandoned = False

    # ------------------------------------------------------------------ #
    # 只读状态
    # ------------------------------------------------------------------ #
    @property
    def path(self) -> str:
        """源 ``.mbtiles`` 路径。"""
        return self._path

    @property
    def accept_warnings(self) -> bool:
        """本次是否已带上"用户同意忽略警告"。"""
        return self._accept_warnings

    @property
    def overwrite(self) -> bool:
        """本次是否已带上"用户同意覆盖"。"""
        return self._overwrite

    @property
    def abandoned(self) -> bool:
        """是否已被调用方放弃。"""
        return self._abandoned

    def abandon(self) -> None:
        """放弃本次构建（用户关掉了对话框）。

        标记立好之后，复制与 SHA256 会在**下一块**就收手；构建器捕获中止异常，
        把 ``.building`` 临时目录清掉，正式目录一个字节都没写。
        """
        self._abandoned = True

    def _should_stop(self) -> bool:
        """给构建器用的中止条件。"""
        return self._abandoned

    # ------------------------------------------------------------------ #
    # 线程入口
    # ------------------------------------------------------------------ #
    def run(self) -> None:  # noqa: D102 - QThread 约定的入口
        name = Path(self._path).name
        self.log_message.emit(
            f"开始生成地图包：{name} → {self._package_name}", "INFO"
        )
        try:
            result = self._service.build(
                self._path,
                self._output_dir,
                self._package_name,
                self._on_progress,
                accept_warnings=self._accept_warnings,
                overwrite=self._overwrite,
                diagnostics=self._diagnostics,
                should_stop=self._should_stop,
            )
        except Exception as exc:  # noqa: BLE001 - 绝不让异常冒泡进 Qt 事件循环
            message = describe_exception(exc)
            self.log_message.emit(f"生成地图包失败：{message}", "ERROR")
            self.package_failed.emit(message)
            return

        if result.needs_confirmation:
            self.log_message.emit(
                f"等待确认：{result.errors[0] if result.errors else '需要用户拍板'}",
                "WARNING",
            )
            self.confirmation_required.emit(result)
            return

        if not result.success:
            for line in result.as_lines():
                self.log_message.emit(line, "ERROR")
            self.package_failed.emit(
                result.errors[0] if result.errors else "生成地图包失败"
            )
            return

        self.log_message.emit(
            f"地图包生成完成：{result.package_dir}"
            f"（{result.tile_count:,} 张瓦片，验证 {result.validation_status}）",
            "INFO",
        )
        self.package_completed.emit(result)

    def _on_progress(self, progress: PackageProgress) -> None:
        """进度回调：转成两条信号发回主线程。"""
        self.progress_changed.emit(progress)
        self.status_changed.emit(progress.message)


class PackageValidateWorker(QThread):
    """在后台线程里校验一个地图包（Spec §21）。

    与构建分开一个类：校验要**重算 SHA256**（GB 级文件要读遍全盘），
    而且它可以被独立触发（主界面的「验证地图包」入口）。
    """

    #: 进度更新，携带 :class:`~app.package.PackageProgress`
    progress_changed = Signal(object)
    #: 当前在做什么
    status_changed = Signal(str)
    #: 校验结束，携带 :class:`~app.package.PackageValidationResult`
    validation_completed = Signal(object)
    #: 校验本身炸了（预期之外的异常）
    validation_failed = Signal(str)
    #: 日志行 ``(文本, 级别)``
    log_message = Signal(str, str)

    def __init__(
        self,
        package_dir: str | Path,
        *,
        service: MapPackageService | None = None,
        verify_checksum: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._package_dir = str(package_dir)
        self._service = service or MapPackageService()
        self._verify_checksum = verify_checksum
        self._abandoned = False

    @property
    def package_dir(self) -> str:
        """被校验的地图包目录。"""
        return self._package_dir

    @property
    def verify_checksum(self) -> bool:
        """是否重算 SHA256。"""
        return self._verify_checksum

    @property
    def abandoned(self) -> bool:
        """是否已被调用方放弃。"""
        return self._abandoned

    def abandon(self) -> None:
        """放弃本次校验。"""
        self._abandoned = True

    def run(self) -> None:  # noqa: D102 - QThread 约定的入口
        name = Path(self._package_dir).name
        self.log_message.emit(f"开始验证地图包：{name}", "INFO")
        try:
            result = self._service.validate(
                self._package_dir,
                verify_checksum=self._verify_checksum,
                progress_callback=self._on_progress,
                should_stop=lambda: self._abandoned,
            )
        except Exception as exc:  # noqa: BLE001 - 绝不让异常冒泡
            message = describe_exception(exc)
            self.log_message.emit(f"验证地图包失败：{message}", "ERROR")
            self.validation_failed.emit(message)
            return

        self.log_message.emit(
            f"地图包验证完成：{name} → {result.status}", "INFO" if result.valid else "ERROR"
        )
        self.validation_completed.emit(result)

    def _on_progress(self, progress: PackageProgress) -> None:
        self.progress_changed.emit(progress)
        self.status_changed.emit(progress.message)


# --------------------------------------------------------------------------- #
# 对话框
# --------------------------------------------------------------------------- #
class PackageDialog(QDialog):
    """生成离线地图包（Spec §17 / §18 / §20）。

    只做三件事：**显示、发请求、把失败说清楚**。所有计算都在后台线程里。
    """

    #: 想写进主窗口日志区的一行，参数为 ``(文本, 级别)``
    log_message = Signal(str, str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        service: MapPackageService | None = None,
        mbtiles_path: str | Path | None = None,
        output_dir: str | Path | None = None,
    ) -> None:
        """
        Args:
            parent: Qt 父对象（通常是主窗口）。
            service: 打包服务；``None`` 时用默认实例（测试会注入）。
            mbtiles_path: 预填的源 ``.mbtiles``（会立即开始一次快速检查）。
            output_dir: 预填的输出目录。
        """
        super().__init__(parent)
        self.setWindowTitle(UI_TEXT_PACKAGE_TITLE)
        self.resize(880, 640)
        self.setMinimumSize(640, 520)

        self._service = service or MapPackageService()
        self._build_worker: PackageBuildWorker | None = None
        self._inspection_worker: MBTilesInspectionWorker | None = None
        self._validate_worker: PackageValidateWorker | None = None
        #: 最近一次源文件检查的结论（Spec §6：界面上的地图信息全部来自它）
        self.diagnostics: MBTilesDiagnosticsResult | None = None
        #: 最近一次构建的结果
        self.result: PackageBuildResult | None = None
        #: 用户是否已经同意忽略警告 / 覆盖（两个确认框各对应一个）
        self._accept_warnings = False
        self._overwrite = False
        self._confirm_box: QMessageBox | None = None
        self._last_directory = ""

        self._build_widgets()
        default_output = self._service.default_output_dir()
        self.set_output_dir(str(default_output))
        if mbtiles_path:
            self.set_source(mbtiles_path)
        if output_dir:
            self.set_output_dir(str(output_dir))

    # ------------------------------------------------------------------ #
    # 构建界面
    # ------------------------------------------------------------------ #
    def _build_widgets(self) -> None:
        # --- 源文件行 ---
        self.path_edit = QLineEdit(self)
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText(UI_TEXT_PACKAGE_NEED_SOURCE)
        self.choose_file_button = QPushButton(UI_TEXT_PACKAGE_CHOOSE_FILE, self)

        source_group = QGroupBox(UI_TEXT_PACKAGE_GROUP_SOURCE, self)
        source_row = QHBoxLayout(source_group)
        source_label = QLabel(UI_TEXT_PACKAGE_SOURCE_LABEL, source_group)
        apply_secondary_text(source_label)
        source_row.addWidget(source_label)
        source_row.addWidget(self.path_edit, 1)
        source_row.addWidget(self.choose_file_button)

        # --- 地图信息（只读，全部来自后台检查）---
        self.info_values: dict[str, QLabel] = {}
        info_group = QGroupBox(UI_TEXT_PACKAGE_GROUP_INFO, self)
        info_form = QFormLayout(info_group)
        for key, title in (
            ("map_type", UI_TEXT_PACKAGE_MAP_TYPE_LABEL),
            ("zoom", UI_TEXT_PACKAGE_ZOOM_LABEL),
            ("tiles", UI_TEXT_PACKAGE_TILES_LABEL),
        ):
            value = WrappingLabel(UI_TEXT_INSPECT_UNKNOWN, info_group)
            self.info_values[key] = value
            info_form.addRow(title, value)

        # --- 包名 + 输出目录 ---
        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText(UI_TEXT_PACKAGE_NEED_NAME)
        self.output_edit = QLineEdit(self)
        self.choose_dir_button = QPushButton(UI_TEXT_PACKAGE_CHOOSE_DIR, self)

        name_row = QHBoxLayout()
        name_label = QLabel(UI_TEXT_PACKAGE_NAME_LABEL, self)
        apply_secondary_text(name_label)
        name_row.addWidget(name_label)
        name_row.addWidget(self.name_edit, 1)

        output_row = QHBoxLayout()
        output_label = QLabel(UI_TEXT_PACKAGE_OUTPUT_LABEL, self)
        apply_secondary_text(output_label)
        output_row.addWidget(output_label)
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.choose_dir_button)

        # --- 地图包内容（Spec §20 的完成视图）---
        self.package_title = QLabel(UI_TEXT_PACKAGE_GROUP_PACKAGE, self)
        self.package_values: dict[str, QLabel] = {}
        package_group = QGroupBox(UI_TEXT_PACKAGE_GROUP_PACKAGE, self)
        package_form = QFormLayout(package_group)
        for key, title in (
            ("directory", UI_TEXT_PACKAGE_PACKAGE_LABEL),
            ("size", UI_TEXT_PACKAGE_MBTILES_LABEL),
            ("tiles", UI_TEXT_PACKAGE_TILES_LABEL),
            ("zoom", UI_TEXT_PACKAGE_ZOOM_LABEL),
            ("sha256", UI_TEXT_PACKAGE_SHA256_LABEL),
            ("validation", UI_TEXT_PACKAGE_RESULT_LABEL),
        ):
            value = WrappingLabel(UI_TEXT_PACKAGE_UNKNOWN, package_group)
            self.package_values[key] = value
            package_form.addRow(title, value)

        # --- 诊断文本区 ---
        diag_group = QGroupBox(UI_TEXT_PACKAGE_GROUP_DIAG, self)
        self.diagnostics_text = QPlainTextEdit(diag_group)
        self.diagnostics_text.setReadOnly(True)
        self.diagnostics_text.setMinimumHeight(96)
        diag_layout = QVBoxLayout(diag_group)
        diag_layout.addWidget(self.diagnostics_text)

        # --- 进度 ---
        progress_container = QWidget(self)
        self.progress_bar = QProgressBar(progress_container)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_label = QLabel("", progress_container)
        self.progress_label.setWordWrap(True)
        progress_layout = QVBoxLayout(progress_container)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.addWidget(self.progress_bar)
        progress_layout.addWidget(self.progress_label)

        # --- 按钮 ---
        self.buttons: dict[str, QPushButton] = {
            "build": QPushButton(UI_TEXT_PACKAGE_BUTTON, self),
            "validate": QPushButton(UI_TEXT_PACKAGE_BUTTON_VALIDATE, self),
            "open_dir": QPushButton(UI_TEXT_OPEN_DIRECTORY, self),
            "close": QPushButton(UI_TEXT_BUTTON_CLOSE, self),
        }
        self.buttons["build"].setToolTip(UI_TEXT_PACKAGE_BUTTON_HINT)
        self.buttons["validate"].setToolTip(UI_TEXT_PACKAGE_BUTTON_VALIDATE_HINT)
        button_row = QHBoxLayout()
        for button in self.buttons.values():
            button.setProperty("action_id", button.text())
            button_row.addWidget(button)
        button_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(source_group)
        layout.addWidget(info_group)
        layout.addLayout(name_row)
        layout.addLayout(output_row)
        layout.addWidget(package_group)
        layout.addWidget(diag_group, 1)
        layout.addWidget(progress_container)
        layout.addLayout(button_row)

        self.choose_file_button.clicked.connect(self._on_choose_file)
        self.choose_dir_button.clicked.connect(self._on_choose_directory)
        self.buttons["build"].clicked.connect(self.start_build)
        self.buttons["validate"].clicked.connect(self._on_validate_clicked)
        self.buttons["open_dir"].clicked.connect(self._on_open_directory)
        self.buttons["close"].clicked.connect(self._on_close_clicked)

        self.package_title.setText(UI_TEXT_PACKAGE_BODY_PLACEHOLDER)
        apply_secondary_text(self.package_title)
        self.set_running(False)

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    @property
    def is_running(self) -> bool:
        """是否有后台任务在跑（构建或检查）。"""
        return (
            self._build_worker is not None
            and self._build_worker.isRunning()
        ) or (
            self._inspection_worker is not None
            and self._inspection_worker.isRunning()
        ) or (
            self._validate_worker is not None and self._validate_worker.isRunning()
        )

    @property
    def is_building(self) -> bool:
        """是否正在构建（构建期间 [生成地图包] 必须禁用）。"""
        return self._build_worker is not None and self._build_worker.isRunning()

    @property
    def build_worker(self) -> PackageBuildWorker | None:
        """当前的后台构建线程（没有则为 ``None``）。"""
        return self._build_worker

    @property
    def inspection_worker(self) -> MBTilesInspectionWorker | None:
        """当前的后台检查线程（没有则为 ``None``）。"""
        return self._inspection_worker

    @property
    def path(self) -> str:
        """当前选中的源 ``.mbtiles``（未选为 ``""``）。"""
        return self.path_edit.text().strip()

    @property
    def package_name(self) -> str:
        """当前填写的包名（已去空白）。"""
        return self.name_edit.text().strip()

    @property
    def output_dir(self) -> str:
        """当前填写的输出目录（已去空白）。"""
        return self.output_edit.text().strip()

    def set_source(self, path: str | Path) -> None:
        """设置源 ``.mbtiles``，并**立刻**开始一次快速检查（Spec §17）。

        检查是必须的：界面上那三行地图信息与 [生成地图包] 的可用性都依赖它，
        而它是唯一允许的地图信息来路（Spec §6）。
        """
        text = str(path)
        self.path_edit.setText(text)
        target = Path(text)
        if target.exists():
            self._last_directory = str(target.parent)
        self.diagnostics = None
        self._accept_warnings = False
        self._overwrite = False
        self._render_diagnostics([])
        self._set_info_unknown()
        self.start_source_check()

    def set_output_dir(self, path: str | Path) -> None:
        """设置输出目录。"""
        self.output_edit.setText(str(path))

    def start_source_check(self) -> None:
        """跑一次源文件快速检查（Spec §23 用的就是同一个结论）。"""
        if self.is_running:
            return
        path = self.path
        if not path:
            self._set_status(UI_TEXT_PACKAGE_NEED_SOURCE, "WARNING")
            return
        if not Path(path).is_file():
            self._set_status(UI_TEXT_PACKAGE_NEED_SOURCE, "WARNING")
            self.buttons["build"].setEnabled(False)
            return

        self.buttons["build"].setEnabled(False)
        self.progress_bar.setRange(0, 0)
        self._set_status(UI_TEXT_PACKAGE_RUNNING, "INFO")

        worker = MBTilesInspectionWorker(
            path,
            service=self._service.inspection_service,
            sample_size=PACKAGE_PRECHECK_SAMPLE_SIZE,
            check_coverage=True,
            deep_scan=False,
            parent=self,
        )
        worker.progress_changed.connect(self._on_inspect_progress)
        worker.inspection_completed.connect(self._on_source_checked)
        worker.inspection_failed.connect(self._on_source_check_failed)
        worker.log_message.connect(self.log_message.emit)
        self._inspection_worker = worker
        worker.start()

    def start_build(self) -> None:
        """[生成地图包]（Spec §17 / §18）。

        参数不全时只提示、不起线程。检查没过（FAIL）时**禁止**构建 —— 这是
        Spec §23 的硬要求，与构建器内部那道闸是两层保险：界面不让点，
        后台也不会因此产出任何文件。
        """
        if self.is_running:
            return
        if not self.path:
            self._set_status(UI_TEXT_PACKAGE_NEED_SOURCE, "WARNING")
            return
        if not Path(self.path).is_file():
            self._set_status(UI_TEXT_PACKAGE_NEED_SOURCE, "WARNING")
            return
        if not self.package_name:
            self._set_status(UI_TEXT_PACKAGE_NEED_NAME, "WARNING")
            return
        if not self.output_dir:
            self._set_status(UI_TEXT_PACKAGE_NEED_DIR, "WARNING")
            return
        if self.diagnostics is None:
            # 检查还没跑完 / 跑失败了：先检查，别让用户点到一半发现没数据。
            self._set_status(UI_TEXT_PACKAGE_RUNNING, "WARNING")
            self.start_source_check()
            return
        if self.diagnostics.status == DIAGNOSTICS_STATUS_FAIL:
            self._set_status(
                UI_TEXT_PACKAGE_FAILED.format(
                    error=self.diagnostics.errors[0] if self.diagnostics.errors else ""
                ),
                "ERROR",
            )
            return

        self._start_build_worker()

    def start_validation(self, package_dir: str | Path | None = None) -> None:
        """[验证地图包]（Spec §21）。

        不传目录时用最近一次构建成功的包目录；都没有就提示先选一个。
        """
        target = str(
            package_dir
            or (self.result.package_dir if self.result is not None else "")
            or ""
        ).strip()
        if not target:
            self._set_status(UI_TEXT_PACKAGE_NEED_PACKAGE, "WARNING")
            return

        self.progress_bar.setRange(0, 0)
        self._set_status(UI_TEXT_PACKAGE_VALIDATING, "INFO")
        worker = PackageValidateWorker(target, service=self._service, parent=self)
        worker.progress_changed.connect(self._on_validate_progress)
        worker.status_changed.connect(lambda text: self._set_status(text, "INFO"))
        worker.validation_completed.connect(self._on_validated)
        worker.validation_failed.connect(self._on_validate_failed)
        worker.log_message.connect(self.log_message.emit)
        self._validate_worker = worker
        worker.start()

    def shutdown(self, timeout_ms: int = _ABANDON_WAIT_MS) -> None:
        """放弃并等待所有后台线程（主窗口关窗时调用）。"""
        for worker in (
            self._build_worker,
            self._inspection_worker,
            self._validate_worker,
        ):
            if worker is None:
                continue
            worker.abandon()
            if worker.isRunning():
                worker.wait(timeout_ms)
        self._build_worker = None
        self._inspection_worker = None
        self._validate_worker = None
        self.set_running(False)

    # ------------------------------------------------------------------ #
    # 内部：启动
    # ------------------------------------------------------------------ #
    def _start_build_worker(self) -> None:
        """起一个构建线程。

        ``accept_warnings`` / ``overwrite`` 取自对话框上的两个"用户已经同意"
        标记 —— 用户在确认框里点过「继续生成」或「覆盖」之后它们才会是 ``True``，
        于是"重新起一个 worker"这个动作天然带着用户的授权（Spec §16 / §23）。
        """
        self.set_running(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self._set_status(UI_TEXT_PACKAGE_RUNNING, "INFO")
        self._render_package(None)

        worker = PackageBuildWorker(
            self.path,
            self.output_dir,
            self.package_name,
            service=self._service,
            diagnostics=self.diagnostics,
            accept_warnings=self._accept_warnings,
            overwrite=self._overwrite,
            parent=self,
        )
        worker.progress_changed.connect(self._on_build_progress)
        worker.status_changed.connect(lambda text: self._set_status(text, "INFO"))
        worker.package_completed.connect(self._on_built)
        worker.package_failed.connect(self._on_build_failed)
        worker.confirmation_required.connect(self._on_confirmation_required)
        worker.log_message.connect(self.log_message.emit)
        self._build_worker = worker
        worker.start()

    def set_running(self, running: bool) -> None:
        """构建期间只留 [验证地图包]（它作用在另一个目录上）与 [关闭]。"""
        self.buttons["build"].setEnabled(not running and self.diagnostics is not None)
        self.buttons["validate"].setEnabled(not running)
        self.choose_file_button.setEnabled(not running)
        self.choose_dir_button.setEnabled(not running)
        self.name_edit.setEnabled(not running)
        self.output_edit.setEnabled(not running)

    # ------------------------------------------------------------------ #
    # 槽：源文件检查
    # ------------------------------------------------------------------ #
    @Slot(object)
    def _on_inspect_progress(self, progress) -> None:
        self._set_status(getattr(progress, "message", ""), "INFO")

    @Slot(object)
    def _on_source_checked(self, diagnostics: MBTilesDiagnosticsResult) -> None:
        """源文件检查完成：填地图信息 + 决定 [生成地图包] 能否点（Spec §17）。"""
        self._inspection_worker = None
        self.diagnostics = diagnostics
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self._render_source_info(diagnostics)
        self._render_diagnostics(
            diagnostics.errors + diagnostics.warnings + diagnostics.notices
        )

        usable = diagnostics.status != DIAGNOSTICS_STATUS_FAIL
        self.buttons["build"].setEnabled(usable)
        if not usable:
            self._set_status(
                UI_TEXT_PACKAGE_FAILED.format(
                    error=diagnostics.errors[0] if diagnostics.errors else ""
                ),
                "ERROR",
            )
            return
        if not self.package_name:
            suggested = self._service.suggest_package_name(self.path, diagnostics)
            if suggested:
                self.name_edit.setText(suggested)
        self._set_status(
            f"{diagnostics.status}：{self.path}", "INFO"
        )

    @Slot(str)
    def _on_source_check_failed(self, message: str) -> None:
        self._inspection_worker = None
        self.diagnostics = None
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.buttons["build"].setEnabled(False)
        self._set_status(UI_TEXT_PACKAGE_FAILED.format(error=message), "ERROR")

    # ------------------------------------------------------------------ #
    # 槽：构建
    # ------------------------------------------------------------------ #
    @Slot(object)
    def _on_build_progress(self, progress: PackageProgress) -> None:
        """把进度画出来（``percent < 0`` 时切回不确定模式）。"""
        if not isinstance(progress, PackageProgress):  # pragma: no cover - 防御
            return
        if progress.percent < 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(progress.percent)

    @Slot(object)
    def _on_built(self, result: PackageBuildResult) -> None:
        """构建成功：铺完成视图（Spec §20）。"""
        self.result = result
        self._build_worker = None
        self.set_running(False)
        # 收尾把进度条拉满：停在 90% 会让人以为"还没跑完就出结论了"。
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self._render_package(result)
        self._render_diagnostics(result.warnings)
        if result.backup_path:
            # 覆盖不是删除：必须让用户知道旧包去哪了（Spec §16）。
            self.log_message.emit(
                UI_TEXT_PACKAGE_BACKUP_DONE.format(path=result.backup_path), "WARNING"
            )
        self._set_status(
            f"{UI_TEXT_PACKAGE_COMPLETE_TITLE}：{result.package_dir}", "INFO"
        )

    @Slot(str)
    def _on_build_failed(self, message: str) -> None:
        self._build_worker = None
        self.set_running(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self._set_status(UI_TEXT_PACKAGE_FAILED.format(error=message), "ERROR")

    @Slot(object)
    def _on_confirmation_required(self, result: PackageBuildResult) -> None:
        """需要用户拍板（Spec §16 / §23）——弹一个**非模态**确认框。

        非模态是硬要求：``exec()`` 会自己开一个嵌套事件循环，在无头测试里
        直接把用例挂死；而且用户也没道理不能一边看这个框一边去翻别的任务。
        """
        self._build_worker = None
        self.set_running(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        if result.confirmation_reason == "exists":
            self._confirm_overwrite(result)
        else:
            self._confirm_warnings(result)

    def _confirm_warnings(self, result: PackageBuildResult) -> None:
        """「MBTiles 存在警告，是否继续生成地图包？」（Spec §23）"""
        warnings = "\n".join(result.warnings[:3]) or "（没有更多说明）"
        box = self._make_box(
            QMessageBox.Icon.Warning,
            UI_TEXT_PACKAGE_WARNING_TITLE,
            UI_TEXT_PACKAGE_WARNING_BODY.format(warnings=warnings),
        )
        continue_button = box.addButton(
            UI_TEXT_PACKAGE_BUTTON_CONTINUE, QMessageBox.ButtonRole.AcceptRole
        )
        cancel_button = box.addButton(
            UI_TEXT_BUTTON_CANCEL, QMessageBox.ButtonRole.RejectRole
        )
        continue_button.clicked.connect(self._on_warn_accepted)
        cancel_button.clicked.connect(self._on_confirm_cancelled)
        self._set_status(UI_TEXT_PACKAGE_WARNING_TITLE, "WARNING")

    def _confirm_overwrite(self, result: PackageBuildResult) -> None:
        """「地图包已存在：[覆盖][选择其他名称][取消]」（Spec §16）。"""
        box = self._make_box(
            QMessageBox.Icon.Question,
            UI_TEXT_PACKAGE_EXISTS_TITLE,
            UI_TEXT_PACKAGE_EXISTS_BODY.format(
                path=result.package_dir or self.output_dir
            ),
        )
        overwrite_button = box.addButton(
            UI_TEXT_PACKAGE_BUTTON_OVERWRITE, QMessageBox.ButtonRole.AcceptRole
        )
        rename_button = box.addButton(
            UI_TEXT_PACKAGE_BUTTON_RENAME, QMessageBox.ButtonRole.ActionRole
        )
        cancel_button = box.addButton(
            UI_TEXT_BUTTON_CANCEL, QMessageBox.ButtonRole.RejectRole
        )
        overwrite_button.clicked.connect(self._on_overwrite_accepted)
        rename_button.clicked.connect(self._on_rename_requested)
        cancel_button.clicked.connect(self._on_confirm_cancelled)
        self._set_status(UI_TEXT_PACKAGE_EXISTS_TITLE, "WARNING")

    def _make_box(self, icon, title: str, text: str) -> QMessageBox:
        """造一个非模态、关闭即销毁的确认框，并记住它的引用（防 GC）。"""
        box = QMessageBox(icon, title, text, parent=self)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        # 关闭即销毁（WA_DeleteOnClose）时，属性里攥着的是个空壳对象；
        # 主窗口关窗时逐个 close 会抛 "Internal C++ object already deleted"。
        # 所以销毁时顺手把引用清掉（Slice 7 踩过的坑）。
        box.destroyed.connect(
            lambda *_, _box=box: setattr(self, "_confirm_box", None)
            if self._confirm_box is _box
            else None
        )
        self._confirm_box = box
        box.show()
        return box

    @Slot()
    def _on_warn_accepted(self) -> None:
        """用户选了"继续生成"：带上授权重跑一次（Spec §23）。"""
        self._accept_warnings = True
        self.log_message.emit("用户确认忽略 MBTiles 警告，继续生成地图包", "WARNING")
        self._start_build_worker()

    @Slot()
    def _on_overwrite_accepted(self) -> None:
        """用户选了「覆盖」：带上授权重跑一次（Spec §16）。"""
        self._overwrite = True
        self.log_message.emit("用户确认覆盖已存在的地图包（旧包将被改名备份）", "WARNING")
        self._start_build_worker()

    @Slot()
    def _on_rename_requested(self) -> None:
        """用户选了「选择其他名称」：只把焦点给包名框，不动磁盘。"""
        self.name_edit.setFocus()
        self.name_edit.selectAll()
        self._set_status(UI_TEXT_PACKAGE_NEED_NAME, "WARNING")

    @Slot()
    def _on_confirm_cancelled(self) -> None:
        self._set_status("已取消，未生成地图包", "INFO")

    # ------------------------------------------------------------------ #
    # 槽：验证
    # ------------------------------------------------------------------ #
    @property
    def validate_worker(self) -> PackageValidateWorker | None:
        """当前的后台校验线程（没有则为 ``None``）。"""
        return self._validate_worker

    @Slot(object)
    def _on_validate_progress(self, progress: PackageProgress) -> None:
        if not isinstance(progress, PackageProgress):  # pragma: no cover - 防御
            return
        if progress.percent < 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(progress.percent)

    @Slot(object)
    def _on_validated(self, result: PackageValidationResult) -> None:
        """验证结束：把四行结论摆出来（Spec §21）。

        刻意**只调** ``_render_validation()``：它内部已经把四行结论 + 错误 +
        警告一起写进文本区了。这里如果顺手再补一句
        ``_render_diagnostics(result.errors + result.warnings)``，就等于
        "先画好再擦掉"——没有错误与警告的**正常包**会显示成一个空的文本区，
        用户点完 [验证地图包] 什么也看不见（Slice 9 的 GUI 测试抓到的缺陷）。
        """
        self._validate_worker = None
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self._render_validation(result)
        self._set_status(
            UI_TEXT_PACKAGE_VALIDATE_DONE_TITLE, "INFO" if result.valid else "ERROR"
        )

    @Slot(str)
    def _on_validate_failed(self, message: str) -> None:
        self._validate_worker = None
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self._set_status(UI_TEXT_PACKAGE_VALIDATE_FAILED.format(error=message), "ERROR")

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def _set_info_unknown(self) -> None:
        for label in self.info_values.values():
            label.set_text(UI_TEXT_INSPECT_UNKNOWN)

    def _render_source_info(self, diagnostics: MBTilesDiagnosticsResult) -> None:
        """填「地图信息」三行（数据全部来自检查结论，Spec §6）。"""
        map_type = MapType.parse(diagnostics.map_type)
        self.info_values["map_type"].set_text(
            describe_map_type(
                map_type,
                bool(diagnostics.labels),
                missing=diagnostics.map_type or UI_TEXT_INSPECT_UNKNOWN,
            )
        )
        self.info_values["zoom"].set_text(diagnostics.zoom_text)
        self.info_values["tiles"].set_text(f"{diagnostics.tile_count:,}")

    def _render_package(self, result: PackageBuildResult | None) -> None:
        """填「地图包内容」（Spec §20）。"""
        if result is None:
            self.package_title.setText(UI_TEXT_PACKAGE_BODY_PLACEHOLDER)
            apply_secondary_text(self.package_title)
            for label in self.package_values.values():
                label.set_text(UI_TEXT_PACKAGE_UNKNOWN)
            return
        self.package_title.setText(UI_TEXT_PACKAGE_COMPLETE_TITLE)
        self.package_values["directory"].set_text(result.package_dir or UI_TEXT_PACKAGE_UNKNOWN)
        self.package_values["size"].set_text(result.mbtiles_size_text)
        self.package_values["tiles"].set_text(f"{result.tile_count:,}")
        self.package_values["zoom"].set_text(result.zoom_text)
        self.package_values["sha256"].set_text(
            result.sha256 or UI_TEXT_PACKAGE_UNKNOWN
        )
        validation = self.package_values["validation"]
        validation.set_text(result.validation_status)
        color = (
            UI_TEXT_PACKAGE_COLOR_PASS
            if result.validation_status == DIAGNOSTICS_STATUS_PASS
            else UI_TEXT_PACKAGE_COLOR_FAIL
        )
        validation.setStyleSheet(
            f"color: {status_text_color(validation.palette(), color).name()};"
            "font-weight: bold;"
        )

    def _render_validation(self, result: PackageValidationResult) -> None:
        """把校验结论铺到「地图包内容」区（Spec §21 的四行）。"""
        manifest = result.manifest
        self.package_title.setText(UI_TEXT_PACKAGE_VALIDATE_DONE_TITLE)
        self.package_values["directory"].set_text(result.package_dir)
        if manifest is not None:
            self.package_values["size"].set_text(
                f"{format_size(result.mbtiles_size or 0)}（manifest "
                f"{manifest.size_text}）"
            )
            self.package_values["tiles"].set_text(f"{manifest.tile_count:,}")
            self.package_values["zoom"].set_text(manifest.zoom_text)
        self.package_values["sha256"].set_text(
            result.actual_sha256[:16] + "…" if result.actual_sha256 else UI_TEXT_PACKAGE_UNKNOWN
        )
        self.package_values["validation"].set_text(result.status)
        # 四步各自的结论（Spec §21 明确要求 Manifest / MBTiles / SHA256 / Result）
        lines = UI_TEXT_PACKAGE_VALIDATE_BODY.format(
            manifest=_pass_text(result.manifest_valid),
            mbtiles=_pass_text(result.mbtiles_valid),
            checksum=_pass_text(result.checksum_valid),
            result=result.status,
        )
        self._render_diagnostics(lines.splitlines() + result.errors + result.warnings)

    def _render_diagnostics(self, lines) -> None:
        """写诊断文本区（空列表 = 清空）。"""
        text = "\n".join(str(line) for line in lines if str(line).strip())
        self.diagnostics_text.setPlainText(text)

    def _set_status(self, text: str, level: str = "INFO") -> None:
        """更新进度条下面那一行 + 记一条日志。"""
        self.progress_label.setText(text)
        if text:
            self.log_message.emit(text, level)

    # ------------------------------------------------------------------ #
    # 按钮槽
    # ------------------------------------------------------------------ #
    @Slot()
    def _on_choose_file(self) -> None:
        """选源 ``.mbtiles``。

        静态 ``QFileDialog``：不起嵌套事件循环（``getOpenFileName`` 内部会），
        因此无头测试里可以 monkeypatch 掉这个静态方法直接返回路径。
        """
        start = self._last_directory or str(Path(self.path).parent if self.path else "")
        selected, _ = QFileDialog.getOpenFileName(
            self, UI_TEXT_PACKAGE_SOURCE_LABEL, start, UI_TEXT_PACKAGE_FILE_FILTER
        )
        if selected:
            self.result = None
            self.set_source(selected)

    @Slot()
    def _on_choose_directory(self) -> None:
        """选输出目录。"""
        start = self.output_dir or self._last_directory
        selected = QFileDialog.getExistingDirectory(
            self, UI_TEXT_PACKAGE_OUTPUT_LABEL, start
        )
        if selected:
            self.set_output_dir(selected)

    @Slot()
    def _on_validate_clicked(self) -> None:
        """[验证地图包]：没产物时让用户先挑一个目录（Spec §21）。"""
        if self.result is not None and self.result.package_dir:
            self.start_validation(self.result.package_dir)
            return
        selected = QFileDialog.getExistingDirectory(
            self, UI_TEXT_PACKAGE_PACKAGE_LABEL, self.output_dir or self._last_directory
        )
        if not selected:
            return
        self.set_output_dir_for_validation(selected)
        self.start_validation(selected)

    def set_output_dir_for_validation(self, path: str) -> None:
        """把"要验证的包目录"显示到包内容区（不改输出目录输入框）。

        刻意**不**写进 ``output_edit``：那是"生成到哪去"，与"验证哪一个"
        是两件不同的事；写进去的话用户下次点生成会把包生成到包里面去。
        """
        self.package_values["directory"].set_text(path)
        self.package_title.setText(UI_TEXT_PACKAGE_VALIDATE_DONE_TITLE)

    @Slot()
    def _on_open_directory(self) -> None:
        """打开产物目录（Spec §20）。"""
        target = ""
        if self.result is not None and self.result.package_dir:
            target = self.result.package_dir
        elif self.package_values["directory"].text() not in ("", UI_TEXT_PACKAGE_UNKNOWN):
            target = self.package_values["directory"].text()
        elif self.output_dir:
            target = self.output_dir
        if not target:
            self._set_status(UI_TEXT_OPEN_DIRECTORY_FAILED, "WARNING")
            return
        ok, error = open_directory(target)
        if not ok:
            self._set_status(UI_TEXT_OPEN_DIRECTORY_FAILED, "WARNING")
            self.log_message.emit(f"打开目录失败：{error}", "WARNING")

    @Slot()
    def _on_close_clicked(self) -> None:
        """[关闭]：先收掉后台线程，再关窗。"""
        self.shutdown()
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名约定
        """关窗先收线程（后台跑着时直接销毁 QThread 会中止整个进程）。"""
        self.shutdown()
        super().closeEvent(event)


# --------------------------------------------------------------------------- #
# 独立的验证对话框（Spec §21 的"主界面 → 验证地图包"入口）
# --------------------------------------------------------------------------- #
class PackageValidateDialog(QDialog):
    """选择一个地图包目录并校验它（Spec §21）。

    与 :class:`PackageDialog` 里的那个 [验证地图包] 是同一件事的两个入口：
    那边验的是"我刚生成的包"，这边验的是"任意一个包"（比如从别人那儿拷来的）。
    **共用一个 worker**（:class:`PackageValidateWorker`），不重复实现。
    """

    #: 日志行 ``(文本, 级别)``
    log_message = Signal(str, str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        service: MapPackageService | None = None,
        package_dir: str | Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(UI_TEXT_PACKAGE_VALIDATE_TITLE)
        self.resize(760, 460)
        self.setMinimumSize(560, 380)

        self._service = service or MapPackageService()
        self._worker: PackageValidateWorker | None = None
        self.result: PackageValidationResult | None = None
        self._last_directory = ""

        self.path_edit = QLineEdit(self)
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText(UI_TEXT_PACKAGE_NEED_PACKAGE)
        self.choose_button = QPushButton(UI_TEXT_PACKAGE_CHOOSE_DIR, self)

        row = QHBoxLayout()
        label = QLabel(UI_TEXT_PACKAGE_PACKAGE_LABEL, self)
        apply_secondary_text(label)
        row.addWidget(label)
        row.addWidget(self.path_edit, 1)
        row.addWidget(self.choose_button)

        self.result_text = QPlainTextEdit(self)
        self.result_text.setReadOnly(True)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_label = QLabel("", self)
        self.progress_label.setWordWrap(True)

        self.validate_button = QPushButton(UI_TEXT_PACKAGE_BUTTON_VALIDATE, self)
        self.close_button = QPushButton(UI_TEXT_BUTTON_CLOSE, self)
        button_row = QHBoxLayout()
        button_row.addWidget(self.validate_button)
        button_row.addWidget(self.close_button)
        button_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(self.result_text, 1)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.progress_label)
        layout.addLayout(button_row)

        self.choose_button.clicked.connect(self._on_choose)
        self.validate_button.clicked.connect(self.start_validation)
        self.close_button.clicked.connect(self._on_close_clicked)

        if package_dir:
            self.set_package_dir(package_dir)

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    @property
    def package_dir(self) -> str:
        """当前选中的地图包目录。"""
        return self.path_edit.text().strip()

    @property
    def is_running(self) -> bool:
        """是否正在校验。"""
        return self._worker is not None and self._worker.isRunning()

    @property
    def validation_worker(self) -> PackageValidateWorker | None:
        """当前的后台校验线程。"""
        return self._worker

    def set_package_dir(self, path: str | Path) -> None:
        """设置要校验的目录。"""
        text = str(path)
        self.path_edit.setText(text)
        target = Path(text)
        if target.exists():
            self._last_directory = str(target.parent if target.is_file() else target)

    def start_validation(self) -> None:
        """[验证地图包]。"""
        if self.is_running:
            return
        target = self.package_dir
        if not target:
            self._set_status(UI_TEXT_PACKAGE_NEED_PACKAGE, "WARNING")
            return
        self.progress_bar.setRange(0, 0)
        self._set_status(UI_TEXT_PACKAGE_VALIDATING, "INFO")
        worker = PackageValidateWorker(target, service=self._service, parent=self)
        worker.progress_changed.connect(self._on_progress)
        worker.status_changed.connect(lambda text: self._set_status(text, "INFO"))
        worker.validation_completed.connect(self._on_completed)
        worker.validation_failed.connect(self._on_failed)
        worker.log_message.connect(self.log_message.emit)
        self._worker = worker
        self.validate_button.setEnabled(False)
        worker.start()

    def shutdown(self, timeout_ms: int = _ABANDON_WAIT_MS) -> None:
        """放弃并等待后台线程。"""
        worker = self._worker
        if worker is None:
            return
        worker.abandon()
        if worker.isRunning():
            worker.wait(timeout_ms)
        self._worker = None
        self.validate_button.setEnabled(True)

    # ------------------------------------------------------------------ #
    # 槽
    # ------------------------------------------------------------------ #
    @Slot(object)
    def _on_progress(self, progress: PackageProgress) -> None:
        if not isinstance(progress, PackageProgress):  # pragma: no cover - 防御
            return
        if progress.percent < 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(progress.percent)

    @Slot(object)
    def _on_completed(self, result: PackageValidationResult) -> None:
        self.result = result
        self._worker = None
        self.validate_button.setEnabled(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        lines = [
            UI_TEXT_PACKAGE_VALIDATE_DONE_TITLE,
            UI_TEXT_PACKAGE_VALIDATE_BODY.format(
                manifest=_pass_text(result.manifest_valid),
                mbtiles=_pass_text(result.mbtiles_valid),
                checksum=_pass_text(result.checksum_valid),
                result=result.status,
            ),
        ]
        if result.manifest is not None:
            lines.append(f"名称：{result.manifest.name}")
            lines.append(f"Zoom：{result.manifest.zoom_text}")
            lines.append(f"瓦片：{result.manifest.tile_count:,}")
            lines.append(f"SHA256：{result.manifest.mbtiles_sha256}")
        lines.extend(result.warnings)
        lines.extend(result.errors)
        self.result_text.setPlainText("\n".join(lines))
        self._set_status(UI_TEXT_PACKAGE_VALIDATE_DONE_TITLE, "INFO" if result.valid else "ERROR")

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self._worker = None
        self.validate_button.setEnabled(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self._set_status(UI_TEXT_PACKAGE_VALIDATE_FAILED.format(error=message), "ERROR")

    @Slot()
    def _on_choose(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, UI_TEXT_PACKAGE_PACKAGE_LABEL, self._last_directory
        )
        if selected:
            self.set_package_dir(selected)

    @Slot()
    def _on_close_clicked(self) -> None:
        self.shutdown()
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名约定
        self.shutdown()
        super().closeEvent(event)

    def _set_status(self, text: str, level: str = "INFO") -> None:
        self.progress_label.setText(text)
        if text:
            self.log_message.emit(text, level)


def _pass_text(value: bool) -> str:
    """三行结论的取值：``PASS`` / ``FAIL``。"""
    return DIAGNOSTICS_STATUS_PASS if value else DIAGNOSTICS_STATUS_FAIL
