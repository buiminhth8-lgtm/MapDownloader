"""Android 设备模型。

字段与 ``adb devices -l`` 的输出维度对齐，便于 Slice 4 直接映射。
本模块不执行任何 adb 命令。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class DeviceState(Enum):
    """设备连接状态，取值与 adb 汇报的状态一致。"""

    DEVICE = "device"
    OFFLINE = "offline"
    UNAUTHORIZED = "unauthorized"
    UNKNOWN = "unknown"

    @property
    def display_name(self) -> str:
        """中文展示名，供 UI 使用。"""
        return _STATE_DISPLAY_NAMES[self]

    @property
    def is_usable(self) -> bool:
        """设备是否处于可直接操作的可用状态。"""
        return self is DeviceState.DEVICE


_STATE_DISPLAY_NAMES: dict[DeviceState, str] = {
    DeviceState.DEVICE: "已连接",
    DeviceState.OFFLINE: "离线",
    DeviceState.UNAUTHORIZED: "未授权",
    DeviceState.UNKNOWN: "未知",
}


@dataclass
class Device:
    """一台通过 ADB 可见的 Android 设备。"""

    serial: str = ""
    model: str = ""
    state: DeviceState = DeviceState.UNKNOWN
    android_version: str = ""

    #: 非核心字段
    last_seen: datetime = field(default_factory=datetime.now)

    @property
    def display_name(self) -> str:
        """UI 列表里展示的一行文本。"""
        name = self.model or self.serial or "未知设备"
        if self.model and self.serial:
            name = f"{self.model} ({self.serial})"
        return f"{name} — {self.state.display_name}"
