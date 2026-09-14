"""Android 设备面板。

只做展示与用户动作转发，不调用 adb（Slice 4 起由 ``app.android`` 提供设备列表，
本面板通过 ``set_devices`` 被动接收数据）。
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import UI_TEXT_DEVICE_EMPTY
from app.models.device import Device

#: 设备列表最小高度，保证空状态下也有明显区域
_LIST_MIN_HEIGHT = 120


class DevicePanel(QGroupBox):
    """Android 设备区域。"""

    #: 用户点击"刷新设备"
    refresh_requested = Signal()
    #: 用户点击"导入到Android"
    import_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Android设备", parent)

        self._list = QListWidget(self)
        self._list.setMinimumHeight(_LIST_MIN_HEIGHT)
        self._list.setAlternatingRowColors(True)

        self._refresh_button = QPushButton("刷新设备", self)
        self._import_button = QPushButton("导入到Android", self)

        self._hint_label = QLabel("", self)
        self._hint_label.setWordWrap(True)

        button_row = QHBoxLayout()
        button_row.addWidget(self._refresh_button)
        button_row.addWidget(self._import_button)
        button_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self._list, 1)
        layout.addWidget(self._hint_label)
        layout.addLayout(button_row)

        self._refresh_button.clicked.connect(self._on_refresh_clicked)
        self._import_button.clicked.connect(self._on_import_clicked)

        self.clear_devices()

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def set_devices(self, devices: Sequence[Device]) -> None:
        """用给定的设备列表刷新显示；空列表则回到"暂无设备"占位状态。"""
        if not devices:
            self.clear_devices()
            return

        self._list.clear()
        for device in devices:
            item = QListWidgetItem(device.display_name, self._list)
            item.setData(Qt.ItemDataRole.UserRole, device.serial)
        self._hint_label.setText(f"共 {len(devices)} 台设备")

    def clear_devices(self) -> None:
        """清空列表并显示占位文案。"""
        self._list.clear()
        placeholder = QListWidgetItem(UI_TEXT_DEVICE_EMPTY, self._list)
        placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
        self._hint_label.setText("Slice 0 不执行 adb 命令，设备列表待 Slice 4 接入")

    def set_busy(self, busy: bool) -> None:
        """下载/导入进行中时禁用操作按钮，避免重复触发。"""
        for button in (self._refresh_button, self._import_button):
            button.setEnabled(not busy)

    @property
    def device_list(self) -> QListWidget:
        """暴露设备列表控件。"""
        return self._list

    @property
    def buttons(self) -> tuple[QPushButton, QPushButton]:
        """(刷新设备, 导入到Android) 按钮。"""
        return (self._refresh_button, self._import_button)

    # ------------------------------------------------------------------ #
    # 内部槽
    # ------------------------------------------------------------------ #
    @Slot()
    def _on_refresh_clicked(self) -> None:
        self.refresh_requested.emit()

    @Slot()
    def _on_import_clicked(self) -> None:
        self.import_requested.emit()
