"""设备发现与详情补全（Slice 10，Spec §5 ~ §10）。

分工：:mod:`app.android.adb_client` 负责"把命令跑起来"，本模块负责
"把输出变成设备"。解析是**纯函数**（:func:`parse_devices_output`），
可以拿真实 ``adb devices -l`` 输出串直接单测，不需要进程、不需要设备。

三条必须守住的规则
------------------

1. **不假设只有一台设备**（Spec §6）。板子、手机、模拟器同时插着是常态；
   解析函数要么全部返回，要么一台不返回。
2. **单个 getprop 失败不影响整台设备**（Spec §7），**单台设备出错不影响整表**。
   设备信息是"锦上添花"：拿不到型号也照样能传文件，界面上少一行而已。
3. **``offline`` / ``unauthorized`` 也要显示出来**（Spec §8）。
   用户拔插了一半的线、或者还没在手机上点"允许"，看到"没有设备"只会更困惑——
   如实显示状态，然后在传输入口上拦住（:meth:`DeviceManager.is_ready`）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.android.adb_client import ERROR_ADB_MISSING, AdbClient
from app.config.settings import (
    ADB_DEVICES_HEADER,
    ANDROID_PROP_ANDROID_VERSION,
    ANDROID_PROP_MANUFACTURER,
    ANDROID_PROP_MODEL,
    ANDROID_PROP_SDK_VERSION,
    LOG_DEVICES_REFRESHED,
)
from app.models.device import AndroidDevice, DeviceState

__all__ = ["DeviceManager", "DeviceRefreshOutcome", "parse_devices_output"]

_logger = logging.getLogger(__name__)

#: ``adb devices -l`` 里 ``key:value`` 形式的附加字段（Spec §6）
_LABEL_KEYS: dict[str, str] = {
    "product": "product",
    "model": "model",
    "device": "device",
    "transport_id": "transport_id",
}


def parse_devices_output(text: str) -> list[AndroidDevice]:
    """把 ``adb devices -l`` 的输出解析成设备列表（Spec §6）。

    输入长这样::

        List of devices attached
        ABC123         device product:panther model:Pixel_7 device:panther transport_id:1
        XYZ456         unauthorized
        * daemon not running; starting now at tcp:5037

    规则：

    - 空行、``*`` 开头的 adb 提示行、以及表头 ``List of devices attached`` 跳过；
    - 每行前两个词是 ``serial`` 与 ``state``；
    - 其余 ``key:value`` 按 :data:`_LABEL_KEYS` 收进对应字段，认不出来的键忽略
      （adb 未来加字段不该让解析崩）；
    - 状态用 :meth:`DeviceState.parse`，认不出的归 ``unknown``。

    Args:
        text: adb 的**标准输出**。传 ``stderr`` 进来只会得到空列表。

    Returns:
        设备列表；没有设备时是空列表（不是 ``None``）。
    """
    devices: list[AndroidDevice] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("*"):
            continue
        if line.startswith(ADB_DEVICES_HEADER):
            continue

        parts = line.split()
        if len(parts) < 2:
            # 只有一列的行不是设备记录（可能是 adb 的告警文本），跳过而不是瞎猜
            continue

        serial, state_text = parts[0], parts[1]
        extras: dict[str, str] = {}
        for token in parts[2:]:
            if ":" not in token:
                continue
            key, _, value = token.partition(":")
            target = _LABEL_KEYS.get(key.strip().lower())
            if target is not None:
                extras[target] = value.strip()

        devices.append(
            AndroidDevice(
                serial=serial,
                model=extras.get("model", ""),
                state=DeviceState.parse(state_text),
                product=extras.get("product", ""),
                device=extras.get("device", ""),
                transport_id=extras.get("transport_id", ""),
            )
        )
    return devices


@dataclass
class DeviceRefreshOutcome:
    """一次设备刷新的完整结果（Spec §10：界面要能分辨"没有设备"与"没有 adb"）。

    :meth:`DeviceManager.refresh_devices` 按 Spec 只返回设备列表；界面需要的
    那点上下文（adb 在不在、错在哪）在这里。
    """

    devices: list[AndroidDevice] = field(default_factory=list)
    #: adb 可执行文件是否可用
    adb_available: bool = False
    #: adb 版本号（探测成功时有值）
    adb_version: str | None = None
    #: 整体失败原因（例如"未找到 adb"）；成功时为 ``None``
    error: str | None = None
    #: 机器可读的错误分类（见 ``adb_client.ERROR_*``）
    error_code: str | None = None
    #: 逐条警告（某台设备的 getprop 失败等）。这些**不影响**结果可用性。
    warnings: list[str] = field(default_factory=list)

    @property
    def has_devices(self) -> bool:
        """是否至少发现一台设备（不论状态）。"""
        return bool(self.devices)

    @property
    def ready_devices(self) -> list[AndroidDevice]:
        """状态为 ``device``（可传输）的设备。"""
        return [device for device in self.devices if device.is_ready]


class DeviceManager:
    """设备列表的持有者与刷新入口（Spec §8）。

    用法::

        manager = DeviceManager()                 # 用默认 adb
        manager.refresh_devices()                 # 刷新（界面点 [刷新设备] 时）
        if manager.is_ready("ABC123"):
            ...                                   # 允许传输
    """

    def __init__(
        self,
        client: AdbClient | None = None,
        *,
        enrich: bool = True,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            client: adb 客户端；``None`` 时自建一个。
            enrich: 是否对 ``device`` 状态的设备补 ``getprop``（Spec §7）。
                测试里关掉它就可以只断言解析，不必 mock 四个属性。
            logger: 可选日志器。
        """
        self._client = client or AdbClient()
        self._enrich = bool(enrich)
        self._logger = logger or _logger
        self._devices: list[AndroidDevice] = []
        self._outcome = DeviceRefreshOutcome()

    # ------------------------------------------------------------------ #
    # 只读状态
    # ------------------------------------------------------------------ #
    @property
    def client(self) -> AdbClient:
        """底层 adb 客户端。"""
        return self._client

    @property
    def devices(self) -> list[AndroidDevice]:
        """最近一次刷新得到的设备（副本）。"""
        return list(self._devices)

    @property
    def outcome(self) -> DeviceRefreshOutcome:
        """最近一次刷新的完整结果。"""
        return self._outcome

    @property
    def adb_available(self) -> bool:
        """adb 是否可用。"""
        return self._client.is_available()

    @property
    def adb_version(self) -> str | None:
        """上次探测到的 adb 版本。"""
        return self._client.version_text

    @property
    def adb_path(self) -> str:
        """当前使用的 adb 路径 / 名字。"""
        return self._client.adb_path

    # ------------------------------------------------------------------ #
    # 刷新
    # ------------------------------------------------------------------ #
    def refresh_devices(self) -> list[AndroidDevice]:
        """刷新设备列表（Spec §6 / §8）。

        **不抛异常**：adb 没装、命令超时、输出是空的，都会得到一个空列表 +
        一条可读的 :attr:`DeviceRefreshOutcome.error`。

        Returns:
            设备列表（副本）。同时更新 :attr:`outcome` 供界面读取上下文。
        """
        self._outcome = self._refresh()
        self._devices = self._outcome.devices
        self._logger.info(LOG_DEVICES_REFRESHED.format(count=len(self._devices)))
        return list(self._devices)

    def refresh(self) -> DeviceRefreshOutcome:
        """与 :meth:`refresh_devices` 同一件事，但返回带上下文的完整结果。

        界面用这个：它需要区分"没有设备"（提示插线）与"没有 adb"（提示装
        Platform Tools）——这两种情况在 Spec §10 里是两套文案。
        """
        self.refresh_devices()
        return self._outcome

    def _refresh(self) -> DeviceRefreshOutcome:
        """真正干活的部分。"""
        version = self._client.version()
        if not version.success:
            # 探测失败就**停在这里**：后面每条命令都会以同样的方式失败，
            # 继续跑只会把同一个错误刷进日志。
            return DeviceRefreshOutcome(
                adb_available=False,
                error=version.error,
                error_code=version.error_code or ERROR_ADB_MISSING,
            )

        listed = self._client.list_devices()
        if not listed.success:
            return DeviceRefreshOutcome(
                adb_available=True,
                adb_version=self._client.version_text,
                error=listed.error or "adb devices 执行失败",
                error_code=listed.error_code,
            )

        devices = parse_devices_output(listed.stdout)
        warnings: list[str] = []
        if self._enrich:
            for device in devices:
                warnings.extend(self._enrich_device(device))

        return DeviceRefreshOutcome(
            devices=devices,
            adb_available=True,
            adb_version=self._client.version_text,
            warnings=warnings,
        )

    # ------------------------------------------------------------------ #
    # 详情补全（Spec §7）
    # ------------------------------------------------------------------ #
    def _enrich_device(self, device: AndroidDevice) -> list[str]:
        """给一台**可用**设备补厂商 / 型号 / Android 版本 / SDK。

        只对 ``state == device`` 的设备做：``unauthorized`` / ``offline`` 上
        ``getprop`` 一定失败，那是四次注定白跑的往返。

        Returns:
            本次补全过程中产生的警告（每一项 getprop 失败一条，带序列号与属性名）。
        """
        if not device.is_ready:
            return []

        warnings: list[str] = []
        for prop, attribute in (
            (ANDROID_PROP_MANUFACTURER, "manufacturer"),
            (ANDROID_PROP_MODEL, "model"),
            (ANDROID_PROP_ANDROID_VERSION, "android_version"),
            (ANDROID_PROP_SDK_VERSION, "sdk_version"),
        ):
            value = self._read_prop(device.serial, prop)
            if value is None:
                warnings.append(f"{device.serial}: getprop {prop} 失败，该字段留空")
                continue
            if value:
                # 型号可能已经在 ``adb devices -l`` 里出现过；getprop 的值更"真"
                # （不带 adb 的下划线转义），因此以它为准。
                setattr(device, attribute, value)
        return warnings

    def _read_prop(self, serial: str, prop: str) -> str | None:
        """读一个属性；命令失败返回 ``None``，读到了空串返回 ``""``。

        这两种情况必须分开：属性存在但为空（模拟器上常见）不该被记成警告。
        """
        result = self._client.getprop(serial, prop)
        if not result.success:
            return None
        return result.stdout.strip()

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def get_device(self, serial: str) -> AndroidDevice | None:
        """按序列号取设备；没有则 ``None``。"""
        target = str(serial or "").strip()
        if not target:
            return None
        for device in self._devices:
            if device.serial == target:
                return device
        return None

    def is_ready(self, serial: str) -> bool:
        """这台设备现在能不能收地图（Spec §8）。

        找不到设备也算 ``False``——"我不认识这台机器"和"这台机器不能用"对
        调用方是同一个答案。
        """
        device = self.get_device(serial)
        return bool(device is not None and device.is_ready)
