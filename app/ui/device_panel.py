"""Android 设备面板（Slice 0 占位 → Slice 10 真实实现）。

本面板只做三件事：**显示**、**收集用户的意图**、**转发信号**。
它不调 adb、不读 manifest、不算校验和——那些分别是 ``app.android`` 与
``app.services.AndroidTransferService`` 的事（Spec §28）。

布局（Spec §11 / §30）::

    Android设备
    ──────────────────────────────────────────────
    ADB：C:\\...\\adb.exe   版本：1.0.41   状态：可用
    设备：[ Pixel 7 (ABC123) — 已连接 ▼ ]
      状态 已连接      厂商 Xiaomi
      型号 Pixel 7     Android 13
      SDK  33          序列号 ABC123
                                          [刷新设备]
    ──────────────────────────────────────────────
    地图包：D:\\maps\\packages\\北京离线地图
                                            [选择地图包] [验证地图包]
    远端目录：/sdcard/.../import/beijing_20260916
    ──────────────────────────────────────────────
    传输状态 READY      当前文件 —
    速度 —             已传输 — / —
    [████████████ 73%]
                                    [传输到设备] [取消传输]

为什么设备详情用两列网格而不是五行
----------------------------------

右列是"设备 + 任务 + 日志"三段式：设备区给多了日志没地方，给少了又看不全
型号 / 版本 / SDK。两列网格在同样高度里塞下同样多的字段，代价只是阅读顺序从
"上到下"变成"Z 字"，而这几项本来就不需要顺序阅读。

为什么设备列表是下拉框而不是列表控件
------------------------------------

Spec §11 的画法是下拉框，Spec §6 要求"不要假设只有一个设备"。下拉框同时满足
两者：多设备全在下拉里（打开就能看到哪台是"未授权"），却只占一行高度。

Slice 10 的契约变更（重要）
---------------------------

Slice 0~9 里这块地方是"占位但如实"的：一个 QListWidget 写着"暂无设备"，点了
只打印一句"本版本不执行 adb 命令"。Slice 10 把它换成了真东西，因此：

- 面板基类从 ``QGroupBox`` 改成 ``QWidget``（内部三个分区各有自己的标题，
  外层再套一个同名分组框会出现"Android设备 / Android设备"两层框）；
- 设备列表从 ``QListWidget`` 换成 ``QComboBox``，``device_list`` 属性随之取消，
  改为 ``device_combo``；
- ``clear_devices()`` 仍在，含义变成"清空下拉框 + 显示占位文案"。
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Signal, Slot

from app.android.device_manager import DeviceRefreshOutcome
from app.android.transfer_models import TransferPlan, TransferProgress
from app.config.settings import (
    UI_TEXT_DEVICE_ADB_AVAILABLE,
    UI_TEXT_DEVICE_ADB_MISSING,
    UI_TEXT_DEVICE_ADB_UNAVAILABLE,
    UI_TEXT_DEVICE_BUTTON_CANCEL,
    UI_TEXT_DEVICE_BUTTON_CHOOSE_PACKAGE,
    UI_TEXT_DEVICE_BUTTON_IMPORT,
    UI_TEXT_DEVICE_BUTTON_REFRESH,
    UI_TEXT_DEVICE_BUTTON_VALIDATE,
    UI_TEXT_DEVICE_EMPTY,
    UI_TEXT_DEVICE_FIELD_ADB,
    UI_TEXT_DEVICE_FIELD_ADB_STATUS,
    UI_TEXT_DEVICE_FIELD_ANDROID,
    UI_TEXT_DEVICE_FIELD_MANUFACTURER,
    UI_TEXT_DEVICE_FIELD_MODEL,
    UI_TEXT_DEVICE_FIELD_SDK,
    UI_TEXT_DEVICE_FIELD_SERIAL,
    UI_TEXT_DEVICE_FIELD_STATE,
    UI_TEXT_DEVICE_GROUP,
    UI_TEXT_DEVICE_GROUP_PACKAGE,
    UI_TEXT_DEVICE_GROUP_TRANSFER,
    UI_TEXT_DEVICE_HINT,
    UI_TEXT_DEVICE_OFFLINE,
    UI_TEXT_DEVICE_PACKAGE_LABEL,
    UI_TEXT_DEVICE_REMOTE_LABEL,
    UI_TEXT_DEVICE_UNAUTHORIZED,
    UI_TEXT_PACKAGE_UNKNOWN,
    UI_TEXT_TRANSFER_FIELD_BYTES,
    UI_TEXT_TRANSFER_FIELD_FILE,
    UI_TEXT_TRANSFER_FIELD_SPEED,
    UI_TEXT_TRANSFER_FIELD_STATUS,
    UI_TEXT_TRANSFER_NEED_PACKAGE,
    UI_TEXT_TRANSFER_PROGRESS_BAR,
    UI_TEXT_TRANSFER_SPEED_UNKNOWN,
    UI_TEXT_TRANSFER_STATUS_CANCELLED,
    UI_TEXT_TRANSFER_STATUS_DONE,
    UI_TEXT_TRANSFER_STATUS_FAILED,
    UI_TEXT_TRANSFER_STATUS_READY,
    UI_TEXT_TRANSFER_STATUS_RUNNING,
)
from app.models.device import AndroidDevice, DeviceState
from app.ui.widgets import WrappingLabel, apply_secondary_text, status_text_color

__all__ = [
    "DevicePanel",
    "TRANSFER_STATE_CANCELLED",
    "TRANSFER_STATE_DONE",
    "TRANSFER_STATE_FAILED",
    "TRANSFER_STATE_READY",
    "TRANSFER_STATE_RUNNING",
]

#: 传输状态机的取值（Spec §30）。用常量而不是裸字符串：
#: 界面、日志、测试三处都引用它们，拼错一个就有一条路径永远匹配不上。
TRANSFER_STATE_READY: str = UI_TEXT_TRANSFER_STATUS_READY
TRANSFER_STATE_RUNNING: str = UI_TEXT_TRANSFER_STATUS_RUNNING
TRANSFER_STATE_DONE: str = UI_TEXT_TRANSFER_STATUS_DONE
TRANSFER_STATE_FAILED: str = UI_TEXT_TRANSFER_STATUS_FAILED
TRANSFER_STATE_CANCELLED: str = UI_TEXT_TRANSFER_STATUS_CANCELLED

#: 结论配色（与 Slice 8 / 9 同一套语义色，实际显示时按主题压一下亮度）
_COLOR_OK = "#2f9e44"
_COLOR_WARN = "#e8a33d"
_COLOR_ERROR = "#e03131"

#: 占位符（"没有这一项"与"这一项是空串"必须能分辨）
_DASH = UI_TEXT_PACKAGE_UNKNOWN

#: 设备详情字段（显示顺序即"两列网格里左列填满再填右列"的顺序）
_DETAIL_FIELDS: tuple[tuple[str, str], ...] = (
    ("state", UI_TEXT_DEVICE_FIELD_STATE),
    ("manufacturer", UI_TEXT_DEVICE_FIELD_MANUFACTURER),
    ("model", UI_TEXT_DEVICE_FIELD_MODEL),
    ("android", UI_TEXT_DEVICE_FIELD_ANDROID),
    ("sdk", UI_TEXT_DEVICE_FIELD_SDK),
    ("serial", UI_TEXT_DEVICE_FIELD_SERIAL),
)

#: 传输区字段
_TRANSFER_FIELDS: tuple[tuple[str, str], ...] = (
    ("status", UI_TEXT_TRANSFER_FIELD_STATUS),
    ("file", UI_TEXT_TRANSFER_FIELD_FILE),
    ("speed", UI_TEXT_TRANSFER_FIELD_SPEED),
    ("bytes", UI_TEXT_TRANSFER_FIELD_BYTES),
)


class DevicePanel(QWidget):
    """Android 设备区域（Spec §11）。

    对外只有信号与显示方法，没有任何 adb 调用——这条边界正是 Spec §28 的
    "UI 不直接组合 adb / package reader / checksum"。
    """

    #: 用户点击 [刷新设备]
    refresh_requested = Signal()
    #: 用户点击 [传输到设备]。
    #: 名字沿用 Slice 0 起的 ``import_requested``：Spec §11 的按钮叫"传输到设备"，
    #: 但"导入到 Android"是同一件事，为改名牵动主窗口与测试没有收益。
    import_requested = Signal()
    #: 用户点击 [取消传输]
    cancel_requested = Signal()
    #: 用户点击 [验证地图包]
    validate_requested = Signal()
    #: 用户点击 [选择地图包]
    choose_package_requested = Signal()
    #: 下拉框换了设备（携带序列号；没有设备时是空串）
    device_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # 先落一批默认状态：几个 set_xxx 会互相触发 _refresh_action_state，
        # 而它要读这些属性。先有值再装配，就不必在每个方法里写 getattr 兜底。
        self._devices: list[AndroidDevice] = []
        self._package_dir = ""
        self._remote_dir = ""
        self._adb_path = ""
        self._adb_available = False
        self._busy = False
        self._cancellable = False
        self._transfer_state = TRANSFER_STATE_READY
        self._outcome_note = ""
        self._transfer_data: dict[str, str] = {}

        self._build_device_box()
        self._build_package_box()
        self._build_transfer_box()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        outer.addWidget(self._device_box)
        outer.addWidget(self._package_box)
        outer.addWidget(self._transfer_box)

        self._connect()
        self.clear_devices()
        self.set_remote_dir("")
        self.reset_transfer()

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #
    def _build_device_box(self) -> None:
        """ADB 状态 + 设备下拉 + 设备详情。"""
        self._adb_label = WrappingLabel("", self)
        apply_secondary_text(self._adb_label)

        self._device_combo = QComboBox(self)
        self._device_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._device_combo.setMinimumContentsLength(16)

        self._refresh_button = QPushButton(UI_TEXT_DEVICE_BUTTON_REFRESH, self)

        self._detail_labels = _grid_of_labels(self, _DETAIL_FIELDS, columns=2)

        self._device_hint = WrappingLabel("", self)
        apply_secondary_text(self._device_hint)

        layout = QVBoxLayout()
        layout.setSpacing(4)
        layout.addWidget(self._adb_label)
        layout.addWidget(self._device_combo)
        layout.addLayout(_details_layout(self._detail_labels, _DETAIL_FIELDS, columns=2))
        layout.addWidget(self._device_hint)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self._refresh_button)
        layout.addLayout(row)

        self._device_box = QGroupBox(UI_TEXT_DEVICE_GROUP, self)
        self._device_box.setLayout(layout)

    def _build_package_box(self) -> None:
        """地图包目录 + 远端目录（Spec §11 的下半部分）。"""
        self._package_label = WrappingLabel(_package_line(""), self)
        self._package_label.setToolTip(
            "标准离线地图包目录（内含 map.mbtiles 与 manifest.json）"
        )
        self._package_status = WrappingLabel("", self)
        apply_secondary_text(self._package_status)

        self._choose_package_button = QPushButton(UI_TEXT_DEVICE_BUTTON_CHOOSE_PACKAGE, self)
        self._validate_button = QPushButton(UI_TEXT_DEVICE_BUTTON_VALIDATE, self)

        self._remote_label = WrappingLabel(_remote_line(""), self)
        self._remote_label.setToolTip(
            "设备上的公共导入区。传输完成后由 Android 端（Slice 11）导入 App。"
        )
        apply_secondary_text(self._remote_label)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self._choose_package_button)
        button_row.addWidget(self._validate_button)

        layout = QVBoxLayout()
        layout.setSpacing(4)
        layout.addWidget(self._package_label)
        layout.addWidget(self._package_status)
        layout.addLayout(button_row)
        layout.addWidget(self._remote_label)

        self._package_box = QGroupBox(UI_TEXT_DEVICE_GROUP_PACKAGE, self)
        self._package_box.setLayout(layout)

    def _build_transfer_box(self) -> None:
        """传输状态 / 进度 / 两个动作按钮（Spec §30）。"""
        for key, _title in _TRANSFER_FIELDS:
            self._transfer_data[key] = _DASH
        self._transfer_labels = _grid_of_labels(self, _TRANSFER_FIELDS, columns=2)

        self._progress_bar = QProgressBar(self)
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setFormat(UI_TEXT_TRANSFER_PROGRESS_BAR)

        self._transfer_button = QPushButton(UI_TEXT_DEVICE_BUTTON_IMPORT, self)
        self._cancel_button = QPushButton(UI_TEXT_DEVICE_BUTTON_CANCEL, self)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self._transfer_button)
        button_row.addWidget(self._cancel_button)

        layout = QVBoxLayout()
        layout.setSpacing(4)
        layout.addLayout(_details_layout(self._transfer_labels, _TRANSFER_FIELDS, columns=2))
        layout.addWidget(self._progress_bar)
        layout.addLayout(button_row)

        self._transfer_box = QGroupBox(UI_TEXT_DEVICE_GROUP_TRANSFER, self)
        self._transfer_box.setLayout(layout)
        self._render_transfer()

    def _connect(self) -> None:
        self._refresh_button.clicked.connect(self._on_refresh_clicked)
        self._transfer_button.clicked.connect(self._on_transfer_clicked)
        self._cancel_button.clicked.connect(self._on_cancel_clicked)
        self._validate_button.clicked.connect(self._on_validate_clicked)
        self._choose_package_button.clicked.connect(self._on_choose_package_clicked)
        self._device_combo.currentIndexChanged.connect(self._on_device_index_changed)

    # ------------------------------------------------------------------ #
    # 对外接口：ADB 与设备
    # ------------------------------------------------------------------ #
    def set_adb_status(
        self,
        available: bool,
        path: str = "",
        version: str | None = None,
        error: str = "",
    ) -> None:
        """显示 ADB 路径 / 版本 / 状态（Spec §10）。

        ``available=False`` 时把"怎么办"那句一起放出来——只显示"不可用"等于把
        问题原封不动地留给用户去猜。
        """
        status = UI_TEXT_DEVICE_ADB_AVAILABLE if available else UI_TEXT_DEVICE_ADB_UNAVAILABLE
        lines = [f"{UI_TEXT_DEVICE_FIELD_ADB}：{path or _DASH}"]
        if version:
            lines.append(f"版本：{version}")
        lines.append(f"{UI_TEXT_DEVICE_FIELD_ADB_STATUS}：{status}")
        if not available:
            lines.append(error or UI_TEXT_DEVICE_ADB_MISSING)
        self._adb_label.set_text("\n".join(lines))
        self._adb_available = bool(available)
        self._refresh_action_state()

    def set_adb_path(self, path: str) -> None:
        """记住 ADB 路径（供 :meth:`apply_outcome` 回显）。"""
        self._adb_path = str(path or "")

    def set_devices(self, devices: Sequence[AndroidDevice]) -> None:
        """用给定的设备列表刷新下拉框与详情。

        空列表回到"暂无设备"占位——**不隐藏**面板：用户需要看到"ADB 是好的、
        只是没插线"，那正是他要的信息。
        """
        self._devices = list(devices)
        self._device_combo.blockSignals(True)
        self._device_combo.clear()
        if not self._devices:
            self._device_combo.addItem(UI_TEXT_DEVICE_EMPTY)
            self._device_combo.setEnabled(False)
            self._device_combo.blockSignals(False)
            self._render_device_details(None)
            self._refresh_action_state()
            return

        for device in self._devices:
            self._device_combo.addItem(device.display_name, device.serial)
        self._device_combo.setEnabled(True)
        self._device_combo.setCurrentIndex(0)
        self._device_combo.blockSignals(False)
        self._render_device_details(self._devices[0])
        self._refresh_action_state()

    def clear_devices(self) -> None:
        """清空下拉框并回到占位文案。"""
        self._devices = []
        self._outcome_note = ""
        self._device_combo.blockSignals(True)
        self._device_combo.clear()
        self._device_combo.addItem(UI_TEXT_DEVICE_EMPTY)
        self._device_combo.setEnabled(False)
        self._device_combo.blockSignals(False)
        self._render_device_details(None)
        self._refresh_action_state()

    def apply_outcome(self, outcome: DeviceRefreshOutcome) -> None:
        """一次性吃下刷新的完整结果（主窗口的槽直接转发过来）。

        这是界面显示"为什么没有设备"的**唯一**入口：没有 ADB、有 ADB 但没插线、
        有设备但未授权——三种情况在这里分支，面板上永远能看出是哪一种。
        """
        self.set_adb_status(
            outcome.adb_available,
            path=self._adb_path,
            version=outcome.adb_version,
            error=outcome.error or "",
        )
        if not outcome.adb_available:
            self._outcome_note = outcome.error or UI_TEXT_DEVICE_ADB_MISSING
        elif not outcome.devices:
            self._outcome_note = UI_TEXT_DEVICE_HINT
        else:
            self._outcome_note = _state_note(outcome)
        self.set_devices(outcome.devices)

    @property
    def devices(self) -> list[AndroidDevice]:
        """当前列表里的设备（副本）。"""
        return list(self._devices)

    @property
    def selected_serial(self) -> str:
        """当前选中设备的序列号；没有设备时是空串。"""
        data = self._device_combo.currentData()
        return str(data) if data else ""

    def selected_device(self) -> AndroidDevice | None:
        """当前选中的设备对象；没有则 ``None``。"""
        serial = self.selected_serial
        for device in self._devices:
            if device.serial == serial:
                return device
        return None

    def select_serial(self, serial: str) -> bool:
        """按序列号选中一台设备（测试与"记住上次的设备"用）。"""
        index = self._device_combo.findData(str(serial))
        if index < 0:
            return False
        self._device_combo.setCurrentIndex(index)
        return True

    # ------------------------------------------------------------------ #
    # 对外接口：地图包
    # ------------------------------------------------------------------ #
    def set_package_dir(self, path: str) -> None:
        """显示选中的地图包目录（并清掉上一条校验结论——它属于上一个包）。"""
        self._package_dir = str(path or "")
        self._package_label.set_text(_package_line(self._package_dir))
        self._package_status.set_text("")
        self._package_status.setStyleSheet("")
        self._refresh_action_state()

    def set_package_status(self, text: str, level: str = "INFO") -> None:
        """显示包校验的结论（``PASS`` / ``FAIL`` / ``WARNING``）。

        没有结论时**收起**这一行：一个空标签也会占掉一行高度，而设备区的每一行
        都是从日志区借来的。
        """
        self._package_status.set_text(text)
        self._package_status.setVisible(bool(text))
        color = {"PASS": _COLOR_OK, "FAIL": _COLOR_ERROR, "WARNING": _COLOR_WARN}.get(level)
        if color:
            self._package_status.setStyleSheet(
                f"color: {status_text_color(self.palette(), color).name()};"
            )
        else:
            self._package_status.setStyleSheet("")

    def set_remote_dir(self, remote_dir: str) -> None:
        """显示远端目标目录（Spec §11 / §31）。"""
        self._remote_dir = str(remote_dir or "")
        self._remote_label.set_text(_remote_line(self._remote_dir))

    def apply_plan(self, plan: TransferPlan) -> None:
        """把服务层算好的传输计划显示出来（Spec §11 / §31）。"""
        if plan.package_dir and plan.package_dir != self._package_dir:
            self.set_package_dir(plan.package_dir)
        if plan.remote_dir:
            self.set_remote_dir(plan.remote_dir)

    @property
    def package_dir(self) -> str:
        """当前选中的地图包目录。"""
        return self._package_dir

    @property
    def remote_dir(self) -> str:
        """当前显示的远端目录。"""
        return self._remote_dir

    # ------------------------------------------------------------------ #
    # 对外接口：传输状态
    # ------------------------------------------------------------------ #
    def reset_transfer(self, state: str = TRANSFER_STATE_READY) -> None:
        """把传输区恢复成初始状态。"""
        self._progress_bar.setValue(0)
        self._transfer_data = {
            "status": state,
            "file": _DASH,
            "speed": UI_TEXT_TRANSFER_SPEED_UNKNOWN,
            "bytes": f"{_DASH} / {_DASH}",
        }
        self._transfer_state = state
        self._cancellable = False
        self._render_transfer()
        self._refresh_action_state()

    def set_transfer_state(self, state: str) -> None:
        """设置传输状态（READY / TRANSFERRING / COMPLETED / FAILED / CANCELLED）。"""
        self._transfer_state = state
        self._transfer_data["status"] = state
        self._render_transfer()
        self._refresh_action_state()

    def set_transfer_progress(self, progress: TransferProgress) -> None:
        """用一帧进度刷新进度条与三个数字（Spec §30）。"""
        self._progress_bar.setValue(max(0, min(100, int(progress.percent))))
        self._transfer_data["file"] = progress.current_file or _DASH
        self._transfer_data["speed"] = progress.speed_text
        self._transfer_data["bytes"] = progress.size_text
        self._render_transfer()

    def set_transfer_result(self, state: str, message: str = "") -> None:
        """收尾：状态 + 一条说明（失败原因 / SHA256 未验证 / 已取消…）。"""
        self._transfer_state = state
        self._transfer_data["status"] = f"{state}（{message}）" if message else state
        self._render_transfer()
        self._cancellable = False
        self._refresh_action_state()

    def set_busy(self, busy: bool) -> None:
        """刷新 / 传输进行中时禁用会冲突的按钮。"""
        self._busy = bool(busy)
        self._refresh_action_state()

    def set_cancellable(self, cancellable: bool) -> None:
        """传输是否可以取消（只有真的在传时才让 [取消传输] 可点）。"""
        self._cancellable = bool(cancellable)
        self._refresh_action_state()

    # ------------------------------------------------------------------ #
    # 对外接口：控件（供主窗口接线与测试）
    # ------------------------------------------------------------------ #
    @property
    def device_combo(self) -> QComboBox:
        """设备下拉框。"""
        return self._device_combo

    @property
    def device_details(self) -> dict[str, QLabel]:
        """设备详情标签表（``state`` / ``manufacturer`` / ``model`` …）。"""
        return dict(self._detail_labels)

    @property
    def transfer_values(self) -> dict[str, QLabel]:
        """传输区标签表（``status`` / ``file`` / ``speed`` / ``bytes``）。"""
        return dict(self._transfer_labels)

    @property
    def progress_bar(self) -> QProgressBar:
        """传输进度条。"""
        return self._progress_bar

    @property
    def refresh_button(self) -> QPushButton:
        """[刷新设备]。"""
        return self._refresh_button

    @property
    def transfer_button(self) -> QPushButton:
        """[传输到设备]。"""
        return self._transfer_button

    @property
    def cancel_button(self) -> QPushButton:
        """[取消传输]。"""
        return self._cancel_button

    @property
    def validate_button(self) -> QPushButton:
        """[验证地图包]。"""
        return self._validate_button

    @property
    def choose_package_button(self) -> QPushButton:
        """[选择地图包]。"""
        return self._choose_package_button

    @property
    def buttons(self) -> tuple[QPushButton, QPushButton]:
        """(刷新设备, 传输到设备)。保留 Slice 0 起的二元形状，调用点不用改。"""
        return (self._refresh_button, self._transfer_button)

    @property
    def transfer_state(self) -> str:
        """当前传输状态。"""
        return self._transfer_state

    @property
    def hint_text(self) -> str:
        """提示行文本（测试与验收脚本读它）。"""
        return self._device_hint.text()

    @property
    def adb_text(self) -> str:
        """ADB 状态区文本。"""
        return self._adb_label.text()

    @property
    def package_status_text(self) -> str:
        """地图包校验结论那一行（``PASS`` / ``FAIL`` / 空）。测试与验收脚本读它。"""
        return self._package_status.text()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _render_transfer(self) -> None:
        """把 ``_transfer_data`` 画到四个标签上。"""
        for key, title in _TRANSFER_FIELDS:
            self._transfer_labels[key].setText(
                f"{title}：{self._transfer_data.get(key, _DASH)}"
            )

    def _render_device_details(self, device: AndroidDevice | None) -> None:
        """把一台设备的字段填进详情标签。"""
        values = {
            "state": device.state.display_name if device else "",
            "manufacturer": (device.manufacturer if device else ""),
            "model": (device.display_model if device else ""),
            "android": (device.android_version if device else ""),
            "sdk": (device.sdk_version if device else ""),
            "serial": (device.serial if device else ""),
        }
        for key, title in _DETAIL_FIELDS:
            self._detail_labels[key].setText(f"{title}：{values.get(key) or _DASH}")

    def _refresh_action_state(self) -> None:
        """按"设备能不能用 / 有没有选包 / 忙不忙"统一决定按钮可用性与提示行。

        这是整个面板**唯一**的按钮策略入口。把 ``setEnabled`` 分散在几处
        ``set_xxx`` 里，迟早在某条路径上漏掉一个，用户就会遇到"点了没反应"。
        """
        device = self.selected_device()
        ready = bool(device is not None and device.is_ready)

        # [刷新设备] 只看"忙不忙"，**不看** ``_adb_available``：它正是发现
        # ADB 是否可用的唯一入口。用可用性来禁用它等于把门锁上再找钥匙——
        # 装配时 ``_adb_available`` 是 ``False``，按钮就永远是灰的。
        self._refresh_button.setEnabled(not self._busy)
        self._choose_package_button.setEnabled(not self._busy)
        self._validate_button.setEnabled(bool(self._package_dir) and not self._busy)
        self._transfer_button.setEnabled(ready and bool(self._package_dir) and not self._busy)
        self._cancel_button.setEnabled(self._cancellable)

        if device is not None and not device.is_ready:
            note = (
                UI_TEXT_DEVICE_UNAUTHORIZED
                if device.state is DeviceState.UNAUTHORIZED
                else UI_TEXT_DEVICE_OFFLINE
            )
        elif device is not None and not self._package_dir:
            note = UI_TEXT_TRANSFER_NEED_PACKAGE
        elif device is None:
            note = self._outcome_note
        else:
            note = ""
        self._device_hint.set_text(note)
        # 空提示行也占高度：设备区每多一行都是从日志区借来的
        self._device_hint.setVisible(bool(note))

    # ------------------------------------------------------------------ #
    # 槽
    # ------------------------------------------------------------------ #
    @Slot()
    def _on_refresh_clicked(self) -> None:
        self.refresh_requested.emit()

    @Slot()
    def _on_transfer_clicked(self) -> None:
        self.import_requested.emit()

    @Slot()
    def _on_cancel_clicked(self) -> None:
        self.cancel_requested.emit()

    @Slot()
    def _on_validate_clicked(self) -> None:
        self.validate_requested.emit()

    @Slot()
    def _on_choose_package_clicked(self) -> None:
        self.choose_package_requested.emit()

    @Slot(int)
    def _on_device_index_changed(self, index: int) -> None:
        self._render_device_details(self.selected_device())
        self._refresh_action_state()
        self.device_changed.emit(self.selected_serial)


# --------------------------------------------------------------------------- #
# 模块级小工具（纯拼装，便于测试直接读）
# --------------------------------------------------------------------------- #
def _grid_of_labels(
    parent: QWidget, fields: tuple[tuple[str, str], ...], *, columns: int
) -> dict[str, QLabel]:
    """按"左列填满再填右列"的顺序建一批标签。

    返回 ``{key: QLabel}``。**行数 = ceil(字段数 / 列数)**，这样两列时是
    3 行 × 2 列，而不是 6 行 × 1 列——后者会把面板撑高一倍。
    """
    labels: dict[str, QLabel] = {}
    for key, title in fields:
        label = QLabel(f"{title}：{_DASH}", parent)
        apply_secondary_text(label)
        labels[key] = label
    return labels


def _details_layout(
    labels: dict[str, QLabel], fields: tuple[tuple[str, str], ...], *, columns: int
) -> QGridLayout:
    """把标签摆进 ``columns`` 列的网格（列优先填充）。"""
    grid = QGridLayout()
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(16)
    grid.setVerticalSpacing(2)
    rows = (len(fields) + columns - 1) // columns
    for index, (key, _title) in enumerate(fields):
        grid.addWidget(labels[key], index % rows, index // rows)
    return grid


def _package_line(path: str) -> str:
    """``地图包：<路径>``。"""
    return f"{UI_TEXT_DEVICE_PACKAGE_LABEL}：{path or _DASH}"


def _remote_line(path: str) -> str:
    """``远端目录：<路径>``。"""
    return f"{UI_TEXT_DEVICE_REMOTE_LABEL}：{path or _DASH}"


def _state_note(outcome: DeviceRefreshOutcome) -> str:
    """刷新之后的提醒：有未授权 / 离线设备就提醒一句，否则空串。"""
    if outcome.error:
        return outcome.error
    for device in outcome.devices:
        if device.state is DeviceState.UNAUTHORIZED:
            return UI_TEXT_DEVICE_UNAUTHORIZED
        if device.state is DeviceState.OFFLINE:
            return UI_TEXT_DEVICE_OFFLINE
    return ""
