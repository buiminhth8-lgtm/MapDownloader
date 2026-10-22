"""``AndroidTransferService`` 的单元测试（Slice 10，Spec §28 / §43）。

编排层最容易"看起来对"的两件事，正好也是最贵的两件事：

1. **不要重复哈希。** 用户点过 [验证地图包]（GB 级文件要算几分钟 SHA256），
   紧接着点 [传输到设备] —— 如果服务层又算一遍，用户会以为程序卡死了。
   这条**只能靠计数**验证：真跑起来只会看到"慢"，看不到"算了两次"。
   所以这里的假校验器带一个调用计数器。
2. **目录变了，缓存必须作废。** 用户校验完 A 包、转头选了 B 包（同一个目录），
   拿着旧结论跳过校验就会把一个坏包推上设备。指纹（大小 + mtime）就是为此存在。

其余用例守的是"这一层不抛异常"：底层三层都不抛，编排层更不该在后台线程里
炸出一个 traceback。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from app.android.transfer_models import join_remote, remote_dir_for
from app.config.settings import (
    DEFAULT_ANDROID_IMPORT_DIR,
    PACKAGE_MANIFEST_FILENAME,
    PACKAGE_MBTILES_FILENAME,
)
from app.package import MapPackageBuilder, MapPackageValidator
from app.services import AdbStatus, AndroidTransferService
from tests.android_fakes import FakeAdbClient
from tests.test_package_builder import _make_mbtiles

#: 可传输的设备（``device`` 状态）
SERIAL = "ABC123"
#: 未授权的设备（Spec §8：显示出来，但不许传）
UNAUTHORIZED = "XYZ456"
#: 离线的设备
OFFLINE = "OLD789"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _make_package(work_dir: Path, name: str = "beijing") -> Path:
    """走生产同一条构建器造一个**真的**离线地图包。"""
    source = _make_mbtiles(work_dir / f"{name}.mbtiles", name=name)
    result = MapPackageBuilder().build(source, work_dir / "packages", package_name=name)
    assert result.success, f"夹具自己失败了：{result.errors}"
    return Path(result.package_dir)


class CountingValidator:
    """包了真校验器的假件：只多记一个"被调用了几次"。

    刻意**不**替换掉真校验逻辑——那会让"传输前到底校验没校验"这件事失去意义
    （一个永远返回 PASS 的假件，无论有没有被跳过都看不出来）。
    """

    def __init__(self) -> None:
        self._inner = MapPackageValidator()
        self.count = 0
        self.dirs: list[str] = []

    def validate(self, package_dir, **kwargs):
        self.count += 1
        self.dirs.append(str(package_dir))
        return self._inner.validate(package_dir, **kwargs)


def _service(work_dir: Path, **client_kwargs):
    """一个接好假 adb 的服务实例。"""
    client = FakeAdbClient(**client_kwargs)
    client.device(SERIAL).available_bytes = 4 * 1024**3
    validator = CountingValidator()
    service = AndroidTransferService(client=client, validator=validator)
    return service, client, validator


def _remote_dir(package_dir: Path) -> str:
    import json

    manifest = json.loads((package_dir / PACKAGE_MANIFEST_FILENAME).read_text("utf-8"))
    return remote_dir_for(DEFAULT_ANDROID_IMPORT_DIR, manifest["id"])


# --------------------------------------------------------------------------- #
# ADB 探测（Spec §2 / §10）
# --------------------------------------------------------------------------- #
def test_adb_status_available(work_dir: Path) -> None:
    """正常情况下报告路径 + 版本。"""
    service, _client, _validator = _service(work_dir)
    status = service.adb_status()

    assert isinstance(status, AdbStatus)
    assert status.available is True
    assert status.status_text == "可用"
    assert status.version == "1.0.41"
    assert status.path


def test_adb_status_when_adb_missing(work_dir: Path) -> None:
    """没装 adb：可用性为假，且必须带一句可读的错（Spec §10 的文案来源）。"""
    service, _client, _validator = _service(work_dir, adb_available=False)
    status = service.adb_status()

    assert status.available is False
    assert status.status_text == "不可用"
    assert status.error
    assert status.error_code


def test_adb_status_survives_exception(work_dir: Path) -> None:
    """探测本身炸了也是一种"不可用"，不该冒泡（否则后台线程直接出 traceback）。"""
    service, client, _validator = _service(work_dir)

    def boom():
        raise RuntimeError("adb 进程被安全软件杀了")

    client.version = boom  # type: ignore[method-assign]
    status = service.adb_status()

    assert status.available is False
    assert "安全软件" in (status.error or "")


# --------------------------------------------------------------------------- #
# 设备（Spec §6 / §8 / §10）
# --------------------------------------------------------------------------- #
def test_refresh_devices_reports_every_state(work_dir: Path) -> None:
    """三台设备全部显示；只有 ``device`` 那台算"可以传"（Spec §8）。"""
    service, _client, _validator = _service(work_dir)
    outcome = service.refresh_devices()

    assert outcome.adb_available is True
    assert [device.serial for device in outcome.devices] == [SERIAL, UNAUTHORIZED, OFFLINE]
    assert [device.serial for device in outcome.ready_devices] == [SERIAL]
    assert outcome.error is None


def test_refresh_devices_when_adb_missing(work_dir: Path) -> None:
    """没 adb 时：没有设备，但有一条明确的错（界面据此提示装 Platform Tools）。"""
    service, _client, _validator = _service(work_dir, adb_available=False)
    outcome = service.refresh_devices()

    assert outcome.devices == []
    assert outcome.adb_available is False
    assert outcome.error


def test_refresh_devices_survives_exception(work_dir: Path) -> None:
    """刷新炸了 → 一个空结果，不抛。"""
    service, _client, _validator = _service(work_dir)

    def boom():
        raise RuntimeError("adb devices 超时")

    service.device_manager.refresh = boom  # type: ignore[method-assign]
    outcome = service.refresh_devices()

    assert outcome.devices == []
    assert outcome.adb_available is False
    assert outcome.error


def test_is_ready_only_for_device_state(work_dir: Path) -> None:
    """未授权 / 离线 / 不认识的序列号一律 ``False``（Spec §8）。"""
    service, _client, _validator = _service(work_dir)
    service.refresh_devices()

    assert service.is_ready(SERIAL) is True
    assert service.is_ready(UNAUTHORIZED) is False
    assert service.is_ready(OFFLINE) is False
    assert service.is_ready("NOPE") is False


def test_get_device_returns_none_for_unknown(work_dir: Path) -> None:
    service, _client, _validator = _service(work_dir)
    service.refresh_devices()

    assert service.get_device(SERIAL) is not None
    assert service.get_device("NOPE") is None
    assert service.get_device("") is None


def test_devices_property_is_a_copy(work_dir: Path) -> None:
    """``devices`` 返回副本：界面拿到列表后乱改不该影响管理器。"""
    service, _client, _validator = _service(work_dir)
    service.refresh_devices()

    snapshot = service.devices
    snapshot.clear()
    assert len(service.devices) == 3


# --------------------------------------------------------------------------- #
# 地图包校验（Spec §12 / §28 的缓存）
# --------------------------------------------------------------------------- #
def test_validate_package_passes_for_a_good_package(work_dir: Path) -> None:
    service, _client, validator = _service(work_dir)
    result = service.validate_package(_make_package(work_dir))

    assert result.valid is True
    assert result.status == "PASS"
    assert validator.count == 1


def test_validate_package_reports_a_broken_package(work_dir: Path) -> None:
    """坏包给出 FAIL，且**不抛**。"""
    package = _make_package(work_dir)
    (package / PACKAGE_MBTILES_FILENAME).write_bytes(b"not an mbtiles")

    service, _client, _validator = _service(work_dir)
    result = service.validate_package(package)

    assert result.valid is False
    assert result.errors


def test_validate_package_survives_exception(work_dir: Path) -> None:
    """校验器抛了 → 一个 FAIL 结论，不冒泡。"""
    service, _client, validator = _service(work_dir)

    def boom(*_args, **_kwargs):
        raise RuntimeError("磁盘掉了")

    validator.validate = boom  # type: ignore[method-assign]
    result = service.validate_package(work_dir)

    assert result.valid is False
    assert any("磁盘掉了" in error for error in result.errors)


def test_cached_validation_is_none_before_validating(work_dir: Path) -> None:
    service, _client, _validator = _service(work_dir)
    assert service.cached_validation(_make_package(work_dir)) is None


def test_cached_validation_returns_the_last_verdict(work_dir: Path) -> None:
    package = _make_package(work_dir)
    service, _client, _validator = _service(work_dir)
    service.validate_package(package)

    cached = service.cached_validation(package)
    assert cached is not None
    assert cached.valid is True


def test_cached_validation_expires_when_the_package_changes(work_dir: Path) -> None:
    """目录内容变了 → 旧结论作废（用户可能刚换了一个包）。"""
    package = _make_package(work_dir)
    service, _client, _validator = _service(work_dir)
    service.validate_package(package)
    assert service.cached_validation(package) is not None

    # 用户手工动过 mbtiles（复制到一半 / 换成了另一个库）
    with (package / PACKAGE_MBTILES_FILENAME).open("ab") as handle:
        handle.write(b"\x00" * 4096)

    assert service.cached_validation(package) is None


def test_cached_validation_ignores_a_failed_verdict(work_dir: Path) -> None:
    """FAIL 不进缓存：跳过校验的前提是"刚校验过**并通过**"。"""
    package = _make_package(work_dir)
    (package / PACKAGE_MBTILES_FILENAME).write_bytes(b"broken")

    service, _client, _validator = _service(work_dir)
    service.validate_package(package)

    assert service.cached_validation(package) is None


# --------------------------------------------------------------------------- #
# 传输（Spec §18 / §28 / §31）
# --------------------------------------------------------------------------- #
def test_transfer_skips_the_second_hash(work_dir: Path) -> None:
    """先校验再传输 → 只算一次哈希（本层存在的理由之一）。"""
    package = _make_package(work_dir)
    service, _client, validator = _service(work_dir)
    service.refresh_devices()

    service.validate_package(package)
    assert validator.count == 1

    result = service.transfer_package(SERIAL, package)

    assert result.success is True, result.errors
    assert validator.count == 1, "传输时又算了一遍哈希"


def test_transfer_verifies_when_not_validated_before(work_dir: Path) -> None:
    """没校验过就直接传输 → 老老实实校验一次。"""
    package = _make_package(work_dir)
    service, _client, validator = _service(work_dir)
    service.refresh_devices()

    result = service.transfer_package(SERIAL, package)

    assert result.success is True, result.errors
    assert validator.count == 1


def test_transfer_hashes_again_after_the_package_changed(work_dir: Path) -> None:
    """目录指纹变了 → 旧结论作废，传输时必须重算。

    刻意只动 mtime、**不动大小**：只按大小判断的话这个用例会漏掉——而
    "用户把同名包换成了另一个"正是靠 mtime 才能发现。
    """
    package = _make_package(work_dir)
    service, _client, validator = _service(work_dir)
    service.refresh_devices()

    service.validate_package(package)
    assert validator.count == 1

    mbtiles = package / PACKAGE_MBTILES_FILENAME
    stat = mbtiles.stat()
    os.utime(mbtiles, ns=(stat.st_atime_ns + 10**9, stat.st_mtime_ns + 10**9))

    result = service.transfer_package(SERIAL, package)

    assert result.success is True, result.errors
    assert validator.count == 2, "目录被动过了还在用旧结论"


def test_transfer_refuses_a_package_broken_after_validation(work_dir: Path) -> None:
    """校验之后 mbtiles 被改大 → 传输必须拦下（Spec §12）。"""
    package = _make_package(work_dir)
    service, _client, _validator = _service(work_dir)
    service.refresh_devices()

    service.validate_package(package)
    with (package / PACKAGE_MBTILES_FILENAME).open("ab") as handle:
        handle.write(b"\x00" * 1024)

    result = service.transfer_package(SERIAL, package)

    assert result.success is False, "改坏的包不该传成功"
    assert PACKAGE_MBTILES_FILENAME in " ".join(result.errors)


def test_transfer_verify_package_true_forces_a_check(work_dir: Path) -> None:
    """显式要求校验时，即使刚校验过也要重来（用户点的是"再验一次"）。"""
    package = _make_package(work_dir)
    service, _client, validator = _service(work_dir)
    service.refresh_devices()

    service.validate_package(package)
    service.transfer_package(SERIAL, package, verify_package=True)

    assert validator.count == 2


def test_transfer_verify_package_false_never_checks(work_dir: Path) -> None:
    """显式取消校验：一次都不算（界面自己已经负责过了）。"""
    package = _make_package(work_dir)
    service, _client, validator = _service(work_dir)
    service.refresh_devices()

    result = service.transfer_package(SERIAL, package, verify_package=False)

    assert result.success is True, result.errors
    assert validator.count == 0


def test_transfer_pushes_exactly_the_two_package_files(work_dir: Path) -> None:
    """端到端：设备上最终只有包内的两个文件（Spec §14 / §19）。"""
    package = _make_package(work_dir)
    service, client, _validator = _service(work_dir)
    service.refresh_devices()

    result = service.transfer_package(SERIAL, package)

    assert result.success is True, result.errors
    remote_dir = _remote_dir(package)
    assert set(client.device(SERIAL).files) == {
        join_remote(remote_dir, PACKAGE_MBTILES_FILENAME),
        join_remote(remote_dir, PACKAGE_MANIFEST_FILENAME),
    }
    assert result.remote_package_dir == remote_dir


def test_transfer_package_forwards_progress_and_cancel(work_dir: Path) -> None:
    """进度回调与取消信号都必须透传到底层（后台线程就靠这两个口子）。"""
    package = _make_package(work_dir)
    service, _client, _validator = _service(work_dir)
    service.refresh_devices()

    events: list = []
    cancel = threading.Event()
    result = service.transfer_package(
        SERIAL, package, progress_callback=events.append, cancel_event=cancel
    )

    assert result.success is True, result.errors
    assert events
    assert [event.percent for event in events] == sorted(event.percent for event in events)


def test_transfer_package_reports_cancellation(work_dir: Path) -> None:
    """取消 → ``cancelled``，且不抛。"""
    package = _make_package(work_dir)
    service, _client, _validator = _service(work_dir, cancel_push=True)
    service.refresh_devices()

    result = service.transfer_package(SERIAL, package, verify_package=False)

    assert result.cancelled is True
    assert result.success is False


def test_transfer_package_to_an_unauthorized_device_fails(work_dir: Path) -> None:
    """包再好，设备不能用就不传（Spec §8）。"""
    package = _make_package(work_dir)
    service, client, _validator = _service(work_dir)
    service.refresh_devices()

    result = service.transfer_package(UNAUTHORIZED, package, verify_package=False)

    assert result.success is False
    assert client.pushes == []


def test_transfer_package_without_adb_fails_cleanly(work_dir: Path) -> None:
    """没 adb → 一句可读的错，不是 traceback（Spec §10）。"""
    package = _make_package(work_dir)
    service, _client, _validator = _service(work_dir, adb_available=False)
    service.refresh_devices()

    result = service.transfer_package(SERIAL, package, verify_package=False)

    assert result.success is False
    assert result.errors


def test_transfer_uses_an_explicit_remote_root(work_dir: Path) -> None:
    """显式给的远端根目录优先于默认值。"""
    package = _make_package(work_dir)
    service, client, _validator = _service(work_dir)
    service.refresh_devices()

    custom = "/sdcard/Documents/offline-map/import"
    result = service.transfer_package(SERIAL, package, remote_root=custom, verify_package=False)

    assert result.success is True, result.errors
    assert result.remote_package_dir.startswith(custom + "/")
    assert all(remote.startswith(custom) for _local, remote in client.pushes)


# --------------------------------------------------------------------------- #
# 计划与远端查询（Spec §24 / §31）
# --------------------------------------------------------------------------- #
def test_plan_describes_the_transfer(work_dir: Path) -> None:
    """确认框要的那几行：设备是谁、包叫什么、多大、落到哪（Spec §31）。"""
    package = _make_package(work_dir)
    service, _client, _validator = _service(work_dir)
    service.refresh_devices()

    plan = service.plan(SERIAL, package)

    assert plan.error is None
    assert plan.serial == SERIAL
    assert plan.remote_dir == _remote_dir(package)
    assert plan.mbtiles_size > 0
    assert plan.manifest_size > 0
    assert plan.tile_count > 0
    assert plan.total_bytes == plan.mbtiles_size + plan.manifest_size


def test_plan_for_an_unknown_device_is_an_error(work_dir: Path) -> None:
    """设备列表过期（用户刚插上还没刷新）→ 计划本身就说清原因。"""
    service, _client, _validator = _service(work_dir)
    service.refresh_devices()

    plan = service.plan("NOPE", _make_package(work_dir))

    assert plan.error
    assert plan.remote_dir == ""


def test_plan_survives_exception(work_dir: Path) -> None:
    """计划阶段炸了也只是一个带 error 的计划。"""
    service, _client, _validator = _service(work_dir)
    service.refresh_devices()

    def boom(*_args, **_kwargs):
        raise RuntimeError("reader 挂了")

    service.transfer.plan = boom  # type: ignore[method-assign]
    plan = service.plan(SERIAL, work_dir)

    assert "reader 挂了" in plan.error


def test_remote_has_package(work_dir: Path) -> None:
    """远端已有同名包 → ``True``（界面据此提前提示要覆盖）。"""
    package = _make_package(work_dir)
    remote_dir = _remote_dir(package)

    service, client, _validator = _service(work_dir)
    assert service.remote_has_package(SERIAL, remote_dir) is False

    device = client.device(SERIAL)
    device.mkdir_p(remote_dir)
    device.files[join_remote(remote_dir, PACKAGE_MANIFEST_FILENAME)] = b"{}"

    assert service.remote_has_package(SERIAL, remote_dir) is True


def test_remote_has_package_survives_exception(work_dir: Path) -> None:
    """查询失败就当作"没有"：这只是提前提示，不该拦住用户。"""
    service, _client, _validator = _service(work_dir)

    def boom(*_args, **_kwargs):
        raise RuntimeError("adb 卡住")

    service.transfer.remote_has_package = boom  # type: ignore[method-assign]
    assert service.remote_has_package(SERIAL, "/sdcard/x") is False


def test_remote_package_dir_follows_the_id(work_dir: Path) -> None:
    """界面在用户选包之后就能显示落点，不必先算整个计划。"""
    service, _client, _validator = _service(work_dir)

    assert service.remote_package_dir("beijing_20260916") == (
        f"{DEFAULT_ANDROID_IMPORT_DIR}/beijing_20260916"
    )
    # 危险字符照旧被规整掉（Spec §15）
    assert ".." not in service.remote_package_dir("../../etc")


# --------------------------------------------------------------------------- #
# 配置与工具
# --------------------------------------------------------------------------- #
def test_default_remote_root_is_the_settings_value(work_dir: Path) -> None:
    service, _client, _validator = _service(work_dir)

    assert service.default_remote_root() == DEFAULT_ANDROID_IMPORT_DIR
    assert service.remote_root == DEFAULT_ANDROID_IMPORT_DIR


def test_set_remote_root_falls_back_to_default(work_dir: Path) -> None:
    """空串不能变成"设备的根目录"——那会把包倒进 ``/``。"""
    service, _client, _validator = _service(work_dir)

    service.set_remote_root("/sdcard/Elsewhere")
    assert service.remote_root == "/sdcard/Elsewhere"

    service.set_remote_root("")
    assert service.remote_root == DEFAULT_ANDROID_IMPORT_DIR


def test_set_adb_path_reaches_the_client(work_dir: Path) -> None:
    """用户在界面上改了 adb 路径 → 之后的命令都用新的。"""
    service, client, _validator = _service(work_dir)

    service.set_adb_path("D:/platform-tools/adb.exe")
    assert client.adb_path == "D:/platform-tools/adb.exe"

    service.set_adb_path(None)
    assert client.adb_path == "adb"


def test_package_files_and_format_error(work_dir: Path) -> None:
    """两个小工具（界面与测试共用）。"""
    assert AndroidTransferService.package_files() == (
        PACKAGE_MBTILES_FILENAME,
        PACKAGE_MANIFEST_FILENAME,
    )
    assert "boom" in AndroidTransferService.format_error(RuntimeError("boom"))
    assert AndroidTransferService.format_error("就是一句话") == "就是一句话"


def test_service_builds_its_own_dependencies() -> None:
    """不传依赖时自己装配（界面只写 ``AndroidTransferService()``）。"""
    service = AndroidTransferService(adb_path="C:/fake/adb.exe")

    assert service.client is not None
    assert service.device_manager is not None
    assert service.transfer is not None
    assert service.validator is not None
    assert service.client.adb_path == "C:/fake/adb.exe"


@pytest.mark.parametrize("package_dir", ["", "C:/definitely/not/here"])
def test_validate_package_never_raises_for_bad_paths(work_dir: Path, package_dir: str) -> None:
    """路径不存在 / 是空串：一个 FAIL 结论，不是异常。"""
    service, _client, _validator = _service(work_dir)
    result = service.validate_package(package_dir)

    assert result.valid is False
    assert result.errors
