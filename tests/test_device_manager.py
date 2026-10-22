"""设备发现与详情补全的测试（Slice 10，Spec §5 ~ §10 / §38 / §39）。

解析是纯函数，因此大部分用例直接喂**真实格式**的 ``adb devices -l`` 输出；
与设备交互的部分用 :class:`tests.android_fakes.FakeAdbClient`，不碰真进程。
"""

from __future__ import annotations

from app.android.device_manager import DeviceManager, parse_devices_output
from app.config.settings import (
    ANDROID_PROP_ANDROID_VERSION,
    ANDROID_PROP_MANUFACTURER,
    ANDROID_PROP_MODEL,
    ANDROID_PROP_SDK_VERSION,
)
from app.models.device import AndroidDevice, Device, DeviceState
from tests.android_fakes import FakeAdbClient, devices_text, fake_result, sample_devices_l


def _props(
    manufacturer: str = "Xiaomi",
    model: str = "Pixel 7",
    release: str = "13",
    sdk: str = "33",
) -> dict[str, str]:
    """一台设备的四个 ``getprop`` 属性。"""
    return {
        ANDROID_PROP_MANUFACTURER: manufacturer,
        ANDROID_PROP_MODEL: model,
        ANDROID_PROP_ANDROID_VERSION: release,
        ANDROID_PROP_SDK_VERSION: sdk,
    }


# --------------------------------------------------------------------------- #
# 解析（Spec §6）
# --------------------------------------------------------------------------- #
def test_parse_no_devices() -> None:
    assert parse_devices_output("List of devices attached\n\n") == []
    assert parse_devices_output("") == []


def test_parse_daemon_noise_is_ignored() -> None:
    """``* daemon not running`` 这类提示行不是设备。"""
    text = devices_text("* daemon not running; starting now at tcp:5037") + "\nABC device\n"
    devices = parse_devices_output(text)
    assert [d.serial for d in devices] == ["ABC"]


def test_parse_single_device_with_labels() -> None:
    """``-l`` 的字段要逐个落到模型上（Spec §6）。"""
    text = devices_text(
        "ABC123 device product:panther model:Pixel_7 device:panther transport_id:1"
    )
    (device,) = parse_devices_output(text)

    assert device.serial == "ABC123"
    assert device.state is DeviceState.DEVICE
    assert device.product == "panther"
    assert device.model == "Pixel_7"
    assert device.device == "panther"
    assert device.transport_id == "1"
    assert device.is_ready is True


def test_parse_never_assumes_a_single_device() -> None:
    """多设备全部返回，状态各不相同（Spec §6 / §8）。"""
    devices = parse_devices_output(sample_devices_l())
    assert [d.serial for d in devices] == ["ABC123", "XYZ456", "OLD789"]
    assert [d.state for d in devices] == [
        DeviceState.DEVICE,
        DeviceState.UNAUTHORIZED,
        DeviceState.OFFLINE,
    ]
    assert [d.is_ready for d in devices] == [True, False, False]


def test_parse_unknown_state_falls_back() -> None:
    """认不出来的状态归 ``unknown``，不抛异常（驱动/ROM 差异很常见）。"""
    devices = parse_devices_output(devices_text("A1 no permissions", "A2 recovery"))
    assert [d.state for d in devices] == [DeviceState.UNKNOWN, DeviceState.UNKNOWN]
    assert all(not d.is_ready for d in devices)


def test_parse_ignores_unknown_labels() -> None:
    """adb 将来加字段不该让解析崩，也不该污染已知字段。"""
    text = devices_text("ABC device model:M device:codename future:whatever")
    (device,) = parse_devices_output(text)
    assert device.model == "M"
    assert device.device == "codename"


def test_parse_skips_single_column_lines() -> None:
    """只有一列的行不是设备记录（可能是 adb 的告警文本）。"""
    assert parse_devices_output("List of devices attached\nerror\n") == []


def test_parse_two_token_unknown_state_is_a_device() -> None:
    """两列的行**就是**一条设备记录，状态认不出时归 ``unknown``。

    刻意不做"状态白名单过滤"：Spec §5 要求支持 ``unknown``，而 adb 在驱动异常
    时会报 ``no permissions`` 这类状态——过滤掉它反而让用户看不到那台设备，
    而"看到了但显示未知"才是他排查问题的起点。
    """
    devices = parse_devices_output(devices_text("something went"))
    assert [d.serial for d in devices] == ["something"]
    assert devices[0].state is DeviceState.UNKNOWN
    assert devices[0].is_ready is False


def test_state_parse_and_display() -> None:
    assert DeviceState.parse("device") is DeviceState.DEVICE
    assert DeviceState.parse(" UNAUTHORIZED ") is DeviceState.UNAUTHORIZED
    assert DeviceState.parse("weird") is DeviceState.UNKNOWN
    assert DeviceState.UNAUTHORIZED.display_name == "未授权"

    device = AndroidDevice(serial="A", model="Pixel_7", state=DeviceState.DEVICE)
    # adb 用下划线转义空格，显示时要换回来
    assert device.display_model == "Pixel 7"
    assert device.short_name == "Pixel 7 (A)"
    assert device.display_name == "Pixel 7 (A) — 已连接"
    assert device.state_value == "device"


def test_android_device_alias_is_the_same_class() -> None:
    """Spec §5 叫 ``AndroidDevice``，项目里从 Slice 0 起叫 ``Device``。"""
    assert AndroidDevice is Device


# --------------------------------------------------------------------------- #
# 刷新（Spec §7 / §8）
# --------------------------------------------------------------------------- #
def test_refresh_devices_enriches_details() -> None:
    client = FakeAdbClient()
    client.device("ABC123").props = _props()

    manager = DeviceManager(client)
    devices = manager.refresh_devices()

    device = manager.get_device("ABC123")
    assert device is not None
    assert device.manufacturer == "Xiaomi"
    assert device.model == "Pixel 7"
    assert device.android_version == "13"
    assert device.sdk_version == "33"
    assert manager.is_ready("ABC123") is True
    assert len(devices) == 3


def test_refresh_does_not_enrich_unusable_devices() -> None:
    """未授权 / 离线的设备不做 getprop：那四次往返注定失败（Spec §7）。"""
    client = FakeAdbClient()
    manager = DeviceManager(client)
    manager.refresh_devices()

    probed = {call[1] for call in client.calls if call[0] == "getprop"}
    assert probed == {"ABC123"}


def test_partial_getprop_failure_keeps_the_device() -> None:
    """单个属性读不到 → 该字段留空 + 一条警告，**设备仍然在列表里**（Spec §7）。"""
    client = FakeAdbClient(fail_getprop=(ANDROID_PROP_MODEL, ANDROID_PROP_SDK_VERSION))
    client.device("ABC123").props = _props()

    outcome = DeviceManager(client).refresh()

    device = next(d for d in outcome.devices if d.serial == "ABC123")
    assert device.model == "Pixel_7"  # 回落成 devices -l 里的值
    assert device.sdk_version == ""
    assert device.android_version == "13"  # 其他属性照常
    assert len(outcome.warnings) == 2
    assert all("ABC123" in warning for warning in outcome.warnings)


def test_empty_prop_is_not_a_warning() -> None:
    """属性存在但为空（模拟器常见）不算失败，不该记警告。"""
    client = FakeAdbClient()
    client.device("ABC123").props = _props(model="")
    outcome = DeviceManager(client).refresh()
    device = next(d for d in outcome.devices if d.serial == "ABC123")
    assert device.model == "Pixel_7"  # getprop 返回空 → 保留 devices -l 的值
    assert outcome.warnings == []


def test_enrich_can_be_disabled() -> None:
    """``enrich=False`` 时一次 getprop 都不发（测试与"只看列表"的场景用）。"""
    client = FakeAdbClient()
    DeviceManager(client, enrich=False).refresh_devices()
    assert not [call for call in client.calls if call[0] == "getprop"]


def test_refresh_without_adb() -> None:
    """adb 不存在：空列表 + 可读的 error，且**不抛异常**（Spec §10）。"""
    client = FakeAdbClient(adb_available=False)
    outcome = DeviceManager(client).refresh()

    assert outcome.devices == []
    assert outcome.adb_available is False
    assert outcome.error
    assert outcome.error_code == "adb_missing"


def test_refresh_when_devices_command_fails() -> None:
    """``adb version`` 成功但 ``devices`` 失败 → 仍要给出可读错误（Spec §7 的原则）。"""
    client = FakeAdbClient()
    client.list_devices = lambda: fake_result(
        False, stderr="adb server is out of date", error="adb server is out of date"
    )

    outcome = DeviceManager(client).refresh()
    assert outcome.devices == []
    assert outcome.adb_available is True
    assert "out of date" in (outcome.error or "")


def test_no_devices_is_not_an_error() -> None:
    """空列表不是错误：ADB 好着呢，只是没插线。"""
    outcome = DeviceManager(FakeAdbClient(devices_out="List of devices attached\n\n")).refresh()
    assert outcome.devices == []
    assert outcome.adb_available is True
    assert outcome.error is None
    assert outcome.has_devices is False
    assert outcome.ready_devices == []


def test_ready_devices_filters_unusable() -> None:
    manager = DeviceManager(FakeAdbClient())
    outcome = manager.refresh()
    assert [d.serial for d in outcome.ready_devices] == ["ABC123"]


def test_get_device_and_is_ready_edge_cases() -> None:
    manager = DeviceManager(FakeAdbClient())
    manager.refresh_devices()

    assert manager.get_device("XYZ456") is not None
    assert manager.is_ready("XYZ456") is False  # 未授权
    assert manager.is_ready("OLD789") is False  # 离线
    assert manager.get_device("NOPE") is None
    assert manager.is_ready("NOPE") is False  # 不认识 = 不能用
    assert manager.get_device("") is None
    assert manager.is_ready("") is False


def test_devices_property_returns_a_copy() -> None:
    """外部改返回的列表不能影响管理器内部状态。"""
    manager = DeviceManager(FakeAdbClient())
    manager.refresh_devices()
    listed = manager.devices
    listed.clear()
    assert len(manager.devices) == 3


def test_adb_path_is_exposed() -> None:
    client = FakeAdbClient()
    manager = DeviceManager(client)
    assert manager.adb_path == client.adb_path
    assert manager.adb_available is True
