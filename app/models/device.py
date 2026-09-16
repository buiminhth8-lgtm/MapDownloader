"""Android 设备模型（Slice 0 起；Slice 10 补齐字段）。

字段与 ``adb devices -l`` 的输出维度对齐。本模块**不执行任何 adb 命令**：
解析在 :mod:`app.android.device_manager`，进程调用在
:mod:`app.android.adb_client`，这里只有数据。

Slice 10 的补充
---------------

原先只有 ``serial / model / state / android_version`` 四个字段——够 GUI 画一行
占位文本，不够回答"这台设备能不能收地图"。Spec §5 要求补齐:

- ``product`` / ``device``：``adb devices -l`` 里与 ``model`` 并列的两个字段
  （它们通常不同：``model:Pixel_7`` 而 ``device:panther``）；
- ``sdk_version`` / ``manufacturer``：来自 ``getprop``，用于显示；
- ``transport_id``：``adb -t`` 定位设备用，多设备同时在线时能避免歧义。

字段默认值全是空串而不是 ``None``：面板上"没有这一项"统一显示占位符
（:data:`app.config.settings.UI_TEXT_PACKAGE_UNKNOWN`），调用方少一层判空。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

__all__ = ["Device", "AndroidDevice", "DeviceState"]


class DeviceState(Enum):
    """设备连接状态，取值与 adb 汇报的状态一致（Spec §5）。

    ``adb devices`` 还会输出 ``no permissions`` / ``recovery`` / ``sideload``
    等少数状态；它们在本工具里没有区别——都归到 :attr:`UNKNOWN`，
    因为对"能不能传文件"这个问题的答案都是"不能，得先去处理设备"。
    """

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
        """设备是否处于可直接操作的可用状态。

        只有 ``device`` 为真。``offline`` / ``unauthorized`` 都会被界面显示出来
        （用户需要知道"我的设备被看到了、但还不能用"），但传输入口会关掉
        （Spec §8）。
        """
        return self is DeviceState.DEVICE

    @classmethod
    def parse(cls, raw: str) -> "DeviceState":
        """把 adb 给的原始状态串转成枚举；认不出来的一律归 ``UNKNOWN``。

        不抛异常：``adb`` 的输出来自设备与驱动，多一种怪状态不该让整个设备
        列表跟着崩（Spec §7 的同一条原则）。
        """
        text = str(raw or "").strip().lower()
        for state in cls:
            if state.value == text:
                return state
        return cls.UNKNOWN


#: 状态的中文展示名。放在模块级而不是枚举体内，是为了让 ``Enum`` 的成员
#: 保持"纯数据"（``display_name`` 通过它查表）。
_STATE_DISPLAY_NAMES: dict[DeviceState, str] = {
    DeviceState.DEVICE: "已连接",
    DeviceState.OFFLINE: "离线",
    DeviceState.UNAUTHORIZED: "未授权",
    DeviceState.UNKNOWN: "未知",
}


@dataclass
class Device:
    """一台通过 ADB 可见的 Android 设备（Spec §5）。

    ``state`` 刻意保留枚举而不是 Spec 里写的裸 ``str``：界面、校验、日志三处
    都要判"能不能用"，用枚举能在类型层面挡住 "device "（带空格）这类意外，
    而 :meth:`state_value` 给出 adb 的原始字符串，需要原样上报时用它。
    """

    serial: str = ""
    model: str = ""
    state: DeviceState = DeviceState.UNKNOWN
    android_version: str = ""

    #: ``adb devices -l`` 里的 ``product:`` / ``device:``（Spec §6）
    product: str = ""
    device: str = ""

    #: ``getprop`` 补充的字段（Spec §7）
    sdk_version: str = ""
    manufacturer: str = ""

    #: ``adb devices -l`` 尾部的 ``transport_id:``（Spec §6）
    transport_id: str = ""

    #: 非核心字段
    last_seen: datetime = field(default_factory=datetime.now)

    @property
    def state_value(self) -> str:
        """adb 汇报的原始状态串（``"device"`` / ``"offline"`` …）。"""
        return self.state.value

    @property
    def is_ready(self) -> bool:
        """是否可以直接向这台设备传文件（Spec §8）。"""
        return self.state.is_usable

    @property
    def display_model(self) -> str:
        """给人看的型号。

        ``adb devices -l`` 会把型号里的空格写成下划线（``model:Pixel_7``）——
        那是它的转义约定，不是设备真的叫这个名字。显示时换回空格，
        原始值仍留在 :attr:`model` 里（要拿去比对 / 落日志的场合用原值）。
        """
        return self.model.replace("_", " ").strip()

    @property
    def display_name(self) -> str:
        """UI 列表里展示的一行文本。

        优先 ``model``（用户认得的是"我的那台 Pixel"而不是序列号），
        没有就退回序列号，都没有才说"未知设备"。后面的状态是**中文**——
        用户看到 ``unauthorized`` 只会一脸问号，而"未授权"他知道该去手机上点一下。
        """
        return f"{self.short_name} — {self.state.display_name}"

    @property
    def short_name(self) -> str:
        """不带状态的名字，供"设备："这类已经另有状态行的场合使用。"""
        model = self.display_model
        if model and self.serial:
            return f"{model} ({self.serial})"
        return model or self.serial or "未知设备"


#: Spec §5 把这个模型称作 ``AndroidDevice``。项目里从 Slice 0 起就叫 ``Device``，
#: 十几个调用点与测试都用它，因此保留原名、加一个别名——不为一个叫法改一地代码。
AndroidDevice = Device
