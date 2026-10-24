"""Slice 10 测试用的 adb 假件（不是测试文件：``pytest`` 不会收集它）。

为什么要一个"带虚拟文件系统的假 adb"
------------------------------------

``MapPackageTransfer`` 的核心价值在于**顺序**与**远端事实**：

- ``mkdir`` 必须在 ``push`` 之前（否则真机上 adb 会报 "No such file or directory"）；
- 大文件先落 ``.part``，传完再 ``mv``；
- ``manifest.json`` 必须最后；
- 传完之后远端的大小与 SHA256 必须与本地一致。

如果只 mock 出"每次调用都返回 rc=0"，上面每一条都测不出来——一个把顺序写反、
把 ``.part`` 忘掉、把 manifest 先传的实现同样能"通过"。

因此这里的假件维护一份设备端的状态（目录集合 + 路径 → 内容），并**真的执行**
mkdir / mv / rm / stat / sha256sum 的语义。于是：

- ``push`` 到不存在的父目录 → 失败（与真机一致）；
- ``mv`` / ``rm`` 真的改状态；
- ``stat -c %s`` 返回真实字节数；
- ``sha256sum`` 返回真实摘要。

配合 :class:`FakeAdbClient.pushes` 记录调用顺序，上面四条都能被直接断言。
"""

from __future__ import annotations

import hashlib
import posixpath
import threading
from dataclasses import dataclass, field
from pathlib import Path

from app.android.adb_client import (
    ERROR_ADB_MISSING,
    ERROR_CANCELLED,
    ERROR_DEVICE_DISCONNECTED,
    ERROR_START_FAILED,
    ERROR_TIMEOUT,
    AdbCommandResult,
    AdbPushProgress,
)

__all__ = [
    "FakeAdbClient",
    "FakeDevice",
    "devices_text",
    "fake_result",
    "sample_devices_l",
]


def fake_result(
    success: bool = True,
    *,
    stdout: str = "",
    stderr: str = "",
    return_code: int | None = None,
    error: str | None = None,
    error_code: str | None = None,
    cancelled: bool = False,
) -> AdbCommandResult:
    """造一个 :class:`AdbCommandResult`（成功时默认 rc=0）。"""
    return AdbCommandResult(
        success=success,
        return_code=return_code if return_code is not None else (0 if success else 1),
        stdout=stdout,
        stderr=stderr,
        duration_ms=1.0,
        error=error,
        error_code=error_code,
        cancelled=cancelled,
    )


def sample_devices_l() -> str:
    """一段**真实格式**的 ``adb devices -l`` 输出（多设备 + 未授权 + 离线）。

    字段取自真机（``model:Pixel_7`` 的下划线是 adb 的转义，不是笔误）。
    """
    return (
        "List of devices attached\n"
        "ABC123         device product:panther model:Pixel_7 device:panther "
        "transport_id:1\n"
        "XYZ456         unauthorized\n"
        "OLD789         offline transport_id:3\n"
        "\n"
    )


def devices_text(*lines: str) -> str:
    """按给定行拼一段 ``adb devices -l`` 输出（自动补表头）。"""
    return "List of devices attached\n" + "\n".join(lines) + "\n"


@dataclass
class FakeDevice:
    """一台假设备的状态：文件、目录、属性。"""

    serial: str
    #: 路径 → 内容（内容真的存下来：``sha256sum`` 要能算出真摘要）
    files: dict[str, bytes] = field(default_factory=dict)
    #: 已存在的目录
    dirs: set[str] = field(default_factory=set)
    #: ``getprop`` 的值
    props: dict[str, str] = field(default_factory=dict)
    #: ``df`` 报告的可用字节；``None`` 表示"解析不出来"
    available_bytes: int | None = 8 * 1024**3

    def mkdir_p(self, path: str) -> None:
        """``mkdir -p``：把路径上的每一级都建出来。"""
        current = path.rstrip("/") or "/"
        parts = [part for part in current.split("/") if part]
        walk = ""
        for part in parts:
            walk = f"{walk}/{part}"
            self.dirs.add(walk)

    def parent_exists(self, path: str) -> bool:
        """``path`` 的父目录是否存在。"""
        return posixpath.dirname(path.rstrip("/")) in self.dirs


class FakeAdbClient:
    """duck-typed 的 :class:`~app.android.AdbClient`（接口一致，不碰真进程）。

    所有"失败注入"都做成构造参数：测试只声明"我要 mkdir 失败"，
    不必去 monkeypatch 方法。
    """

    def __init__(
        self,
        *,
        adb_available: bool = True,
        adb_version: str = "1.0.41",
        devices_out: str | None = None,
        devices: dict[str, FakeDevice] | None = None,
        sha256_mode: str = "ok",
        size_mode: str = "stat",
        fail_mkdir: bool = False,
        fail_push_mbtiles: bool = False,
        fail_push_manifest: bool = False,
        fail_move: bool = False,
        fail_remove: bool = False,
        disconnect_on_push: bool = False,
        cancel_push: bool = False,
        push_percent: bool = True,
        version_error_code: str | None = None,
        fail_getprop: tuple[str, ...] = (),
        size_override: int | None = None,
    ) -> None:
        """
        Args:
            adb_available: ``False`` 时每条命令都返回"找不到 adb"（Spec §10）。
            adb_version: ``adb version`` 假装报的版本。
            devices_out: ``adb devices -l`` 的原始输出；``None`` 时用
                :func:`sample_devices_l`。
            devices: 显式的设备状态表（键是序列号）；缺省时按 ``devices_out``
                里的序列号各造一台。
            sha256_mode: ``ok`` / ``unavailable``（没有 sha256sum，Spec §26）
                / ``broken``（返回一个不匹配的摘要，Spec §41）。
            size_mode: ``stat``（``stat -c %s`` 可用）/ ``wc``（只有 ``wc -c``）
                / ``unavailable``（都读不到，Spec §25）。
            fail_mkdir: ``mkdir`` 失败（Spec §16 必须立刻停）。
            fail_push_mbtiles: 大文件 push 失败。
            fail_push_manifest: manifest push 失败。
            fail_move: ``mv`` 失败（Spec §20）。
            fail_remove: ``rm -rf`` 失败（覆盖旧包删不掉）。
            disconnect_on_push: push 时设备掉线（Spec §35）。
            cancel_push: push 期间"用户取消"（Spec §23）。
            push_percent: push 是否输出 ``[ NN%]`` 进度行（Spec §22 的两种 adb）。
            version_error_code: ``adb version`` 失败时给哪个错误码。
            fail_getprop: 这些属性名上的 ``getprop`` 会失败（Spec §7：单个属性
                失败不能让整台设备消失）。
            size_override: 强制 ``stat`` 报这个字节数——用来造出"远端大小与本地
                不一致"（Spec §25 / §41）。``None`` 表示如实上报。
        """
        self._adb_path = "C:/fake/platform-tools/adb.exe"
        self._available = adb_available
        self._version = adb_version
        self._version_error_code = version_error_code
        self._devices_out = devices_out
        self._sha256_mode = sha256_mode
        self._size_mode = size_mode
        self._fail_mkdir = fail_mkdir
        self._fail_push_mbtiles = fail_push_mbtiles
        self._fail_push_manifest = fail_push_manifest
        self._fail_move = fail_move
        self._fail_remove = fail_remove
        self._disconnect_on_push = disconnect_on_push
        self._cancel_push = cancel_push
        self._push_percent = push_percent
        self._fail_getprop = set(fail_getprop)
        self._size_override = size_override

        self.devices: dict[str, FakeDevice] = devices or {}
        for serial in self._serials():
            self.devices.setdefault(serial, FakeDevice(serial=serial))

        #: 全部调用的记录：``(命令, 参数...)``。测试用它断言**顺序**与 ``-s``
        self.calls: list[tuple] = []
        #: push 的记录：``(本地, 远端)``
        self.pushes: list[tuple[str, str]] = []
        self.version_calls = 0

    # ------------------------------------------------------------------ #
    # 构造辅助
    # ------------------------------------------------------------------ #
    def _serials(self) -> list[str]:
        text = self._devices_out if self._devices_out is not None else sample_devices_l()
        serials = []
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and not line.startswith(("List", "*")):
                serials.append(parts[0])
        return serials

    def device(self, serial: str) -> FakeDevice:
        """取（必要时创建）一台设备的状态。"""
        return self.devices.setdefault(serial, FakeDevice(serial=serial))

    def set_sha256_supported(self, supported: bool) -> None:
        """运行中切换"设备端是否支持 sha256sum"（Spec §42）。"""
        self._sha256_mode = "ok" if supported else "unavailable"

    # ------------------------------------------------------------------ #
    # AdbClient 接口
    # ------------------------------------------------------------------ #
    @property
    def adb_path(self) -> str:
        return self._adb_path

    def set_adb_path(self, path: str | None) -> None:
        self._adb_path = path or "adb"

    def is_available(self) -> bool:
        return self._available

    @property
    def version_text(self) -> str | None:
        return self._version if self._available else None

    def version(self) -> AdbCommandResult:
        self.version_calls += 1
        self.calls.append(("version",))
        if not self._available:
            return fake_result(
                False,
                error="未找到 adb 可执行文件：adb",
                error_code=self._version_error_code or ERROR_ADB_MISSING,
            )
        return fake_result(stdout=f"Android Debug Bridge version {self._version}\n")

    def list_devices(self) -> AdbCommandResult:
        self.calls.append(("devices", "-l"))
        if not self._available:
            return fake_result(
                False, error="未找到 adb", error_code=ERROR_ADB_MISSING
            )
        text = self._devices_out if self._devices_out is not None else sample_devices_l()
        return fake_result(stdout=text)

    def shell(self, serial: str, args, timeout: float = 20.0) -> AdbCommandResult:
        argv = [str(a) for a in args]
        self.calls.append(("shell", serial, list(argv)))
        return self._dispatch(serial, argv)

    def getprop(self, serial: str, prop: str) -> AdbCommandResult:
        self.calls.append(("getprop", serial, prop))
        if not self._available:
            return fake_result(False, error="未找到 adb", error_code=ERROR_ADB_MISSING)
        if prop in self._fail_getprop:
            return fake_result(False, stderr=f"getprop: {prop}: failed", error="failed")
        value = self.device(serial).props.get(prop)
        if value is None:
            # 真机上不存在的属性会输出空行（不是失败）
            return fake_result(stdout="\n")
        return fake_result(stdout=f"{value}\n")

    def mkdir(self, serial: str, remote_path: str) -> AdbCommandResult:
        return self.shell(serial, ["mkdir", "-p", remote_path])

    def remove(self, serial: str, remote_path: str) -> AdbCommandResult:
        return self.shell(serial, ["rm", "-rf", remote_path])

    def move(self, serial: str, source: str, target: str) -> AdbCommandResult:
        return self.shell(serial, ["mv", source, target])

    def exists(self, serial: str, remote_path: str) -> bool:
        device = self.device(serial)
        return remote_path in device.files or remote_path.rstrip("/") in device.dirs

    def file_size(self, serial: str, remote_path: str) -> int | None:
        device = self.device(serial)
        if remote_path not in device.files:
            return None
        if self._size_mode == "unavailable":
            return None
        if self._size_override is not None:
            return self._size_override
        return len(device.files[remote_path])

    def remote_sha256(self, serial: str, remote_path: str) -> str | None:
        device = self.device(serial)
        if self._sha256_mode == "unavailable":
            return None
        data = device.files.get(remote_path)
        if data is None:
            return None
        digest = hashlib.sha256(data).hexdigest()
        if self._sha256_mode == "broken":
            # 返回一个"格式对但内容错"的摘要：模拟传坏 / 被改过
            return "0" * 64 if digest != "0" * 64 else "1" * 64
        return digest

    def available_space(self, serial: str, remote_path: str) -> int | None:
        self.calls.append(("df", serial, remote_path))
        return self.device(serial).available_bytes

    def push(
        self,
        serial: str,
        local_path,
        remote_path: str,
        *,
        timeout: float | None = None,
        progress_callback=None,
        cancel_event: threading.Event | None = None,
    ):
        local = Path(local_path)
        name = local.name
        self.calls.append(("push", serial, str(remote_path)))
        self.pushes.append((str(local), str(remote_path)))

        if self._cancel_push or (cancel_event is not None and cancel_event.is_set()):
            # 模拟"用户点了取消"：子进程被杀，`.part` 只写了一半。
            # 刻意**不要求** ``cancel_event`` 存在：``cancel_push`` 这个开关本身
            # 就代表"push 期间用户取消"，测试不必为了触发它再编一个事件对象。
            self.device(serial).files[remote_path] = local.read_bytes()[: max(1, local.stat().st_size // 2)]
            return fake_result(
                False,
                error="transfer cancelled by user",
                error_code=ERROR_CANCELLED,
                cancelled=True,
            )

        if self._disconnect_on_push:
            return fake_result(
                False,
                stderr="adb: error: failed to copy: device offline",
                error="device offline",
                error_code=ERROR_DEVICE_DISCONNECTED,
            )

        fail = (name.endswith(".mbtiles") and self._fail_push_mbtiles) or (
            name.endswith(".json") and self._fail_push_manifest
        )
        if fail:
            return fake_result(
                False, stderr="adb: error: failed to copy", error="failed to copy"
            )

        device = self.device(serial)
        if not device.parent_exists(remote_path):
            # 与真机一致：父目录不存在时 adb push 会失败
            return fake_result(
                False,
                stderr="adb: error: failed to copy: No such file or directory",
                error="No such file or directory",
            )

        data = local.read_bytes()
        if progress_callback is not None:
            total = len(data)
            if self._push_percent:
                for percent in (10, 55, 100):
                    progress_callback(
                        AdbPushProgress(
                            percent=float(percent),
                            bytes_transferred=total * percent // 100,
                            total_bytes=total,
                        )
                    )
            else:
                # 没有百分比的 adb（Spec §22 明确承认这种可能）
                progress_callback(
                    AdbPushProgress(percent=None, bytes_transferred=0, total_bytes=total)
                )
        device.files[remote_path] = data
        return fake_result(stdout=f"{local} -> {remote_path}\n")

    # ------------------------------------------------------------------ #
    # 内部：把 shell 命令映射到虚拟设备
    # ------------------------------------------------------------------ #
    def _dispatch(self, serial: str, argv: list[str]) -> AdbCommandResult:
        if not self._available:
            return fake_result(False, error="未找到 adb", error_code=ERROR_ADB_MISSING)
        if not argv:
            return fake_result(False, error="空命令")

        device = self.device(serial)
        head = argv[0]

        if head == "mkdir":
            path = argv[-1]
            if self._fail_mkdir:
                return fake_result(
                    False,
                    stderr="mkdir: '/sdcard': Permission denied",
                    error="Permission denied",
                )
            device.mkdir_p(path)
            return fake_result()

        if head == "rm":
            path = argv[-1]
            if self._fail_remove:
                return fake_result(False, stderr="rm: Permission denied", error="Permission denied")
            # ``rm -rf <path>``：既要删 ``path`` 本身（文件或目录），也要删它
            # **底下的所有内容**。只按前缀删目录会把 ``<dir>/stale.bin`` 这类
            # 文件留下——而"覆盖旧包"这条路径正是靠它把上一次的残留清干净。
            prefix = path.rstrip("/")
            if path in device.files:
                del device.files[path]
            device.files = {
                key: value
                for key, value in device.files.items()
                if not key.startswith(f"{prefix}/")
            }
            device.dirs = {d for d in device.dirs if d != prefix and not d.startswith(f"{prefix}/")}
            return fake_result()

        if head == "mv":
            source, target = argv[-2], argv[-1]
            if self._fail_move:
                return fake_result(False, stderr="mv: Permission denied", error="Permission denied")
            if source in device.dirs:
                # 与 adb mv 一样移动整棵目录；目标必须不存在。
                if target in device.dirs or target in device.files:
                    return fake_result(False, error="target exists")
                device.files = {
                    (target + key[len(source):] if key.startswith(source + "/") else key): value
                    for key, value in device.files.items()
                }
                device.dirs = {
                    target + key[len(source):]
                    if key == source or key.startswith(source + "/") else key
                    for key in device.dirs
                }
                return fake_result()
            data = device.files.pop(source, None)
            if data is None:
                return fake_result(
                    False, stderr=f"mv: {source}: No such file or directory", error="No such file"
                )
            device.files[target] = data
            return fake_result()

        if head == "ls":
            path = argv[-1]
            if path in device.files or path.rstrip("/") in device.dirs:
                return fake_result(stdout=f"{path}\n")
            return fake_result(
                False, stderr=f"ls: {path}: No such file or directory", error="No such file"
            )

        if head == "stat":
            if self._size_mode == "wc":
                return fake_result(False, stderr="stat: Unknown option", error="Unknown option")
            data = device.files.get(argv[-1])
            if data is None:
                return fake_result(False, stderr="stat: No such file", error="No such file")
            return fake_result(stdout=f"{len(data)}\n")

        if head == "wc":
            if self._size_mode == "stat":
                # 真机上先试 stat，成功了就不会走到 wc；这里保持"也支持"以免掩盖问题
                pass
            data = device.files.get(argv[-1])
            if data is None:
                return fake_result(False, stderr="wc: No such file", error="No such file")
            return fake_result(stdout=f"{len(data)} {argv[-1]}\n")

        if head in {"sha256sum", "toybox", "busybox"}:
            path = argv[-1]
            if self._sha256_mode == "unavailable":
                return fake_result(
                    False, stderr=f"{head}: not found", error="not found"
                )
            digest = self.remote_sha256(serial, path)
            if digest is None:
                return fake_result(False, stderr="No such file", error="No such file")
            return fake_result(stdout=f"{digest}  {path}\n")

        if head == "df":
            available = device.available_bytes
            if available is None:
                return fake_result(stdout="df: not parsable\n")
            return fake_result(
                stdout=(
                    "Filesystem     1K-blocks     Used Available Use% Mounted on\n"
                    f"/dev/fuse      117311488 7234112 {available // 1024}   7% /storage/emulated\n"
                )
            )

        return fake_result(False, stderr=f"{head}: unknown command", error="unknown command")


#: 便捷常量：几个常用错误码（测试里少写点模板字面量）
TIMEOUT = ERROR_TIMEOUT
START_FAILED = ERROR_START_FAILED
