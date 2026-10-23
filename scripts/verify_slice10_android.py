"""Slice 10 真实平台验收脚本：ADB 设备管理 + 离线地图包传输。

**为什么不能只跑 pytest**
------------------------

``pytest`` 里的设备是 :class:`tests.android_fakes.FakeAdbClient`：它是个
duck-typed 的 Python 对象，**根本没有子进程**。于是下面这些只有实机才看得见的
东西，全都在测试的盲区里：

1. ``subprocess`` 的参数到底是列表还是字符串（``shell=True`` 会让设备上的
   文件名变成 shell 语法）；
2. ``adb push`` 的输出是 **流式** 读的——``[ NN%]`` 跨 ``read()`` 边界被切开时
   百分比解析还对不对；
3. stderr 管道写满导致子进程**永久阻塞**（经典陷阱：表现为"adb push 卡死"）；
4. 取消时子进程真的被杀掉、半个 ``.part`` 真的留在设备上、随后真的被删掉；
5. 真窗口下"设备 / 地图包 / 传输进度"三个区在真实字体与 DPI 下有没有被裁掉。

所以本脚本造一个**真的能执行**的 adb 替身（Windows 下是 ``adb.cmd`` → Python，
POSIX 下是带 shebang 的 ``adb``），把设备端文件系统映射成宿主目录，然后让
**生产的** :class:`~app.android.AdbClient` 用真实子进程去跑它。于是：

- 传过去的字节真的落在磁盘上，脚本可以直接比对 SHA256；
- ``mkdir`` / ``mv`` / ``rm`` / ``stat`` / ``sha256sum`` 真的改宿主目录；
- ``calls.jsonl`` 记下每一次真实调用的 argv，顺序与 ``-s`` 可以被直接断言。

用法::

    python scripts/verify_slice10_android.py
    python scripts/verify_slice10_android.py --offscreen   # 无显示环境（CI）
    python scripts/verify_slice10_android.py --no-shots    # 只跑检查，不截图

**全程只碰临时目录**：假设备根、地图包、任务记录、界面偏好都在
``%TEMP%\\verify-slice10-*`` 下，跑完就删。
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DOCS = PROJECT_ROOT / "docs" / "images"

#: 当前 ``QApplication``；``_shot`` 需要它来空转事件循环。
_APP = None

#: 假设备上的设备列表（故意含未授权与离线各一台，Spec §8）
DEVICES = [
    {
        "serial": "ABC123",
        "state": "device",
        "product": "panther",
        "model": "Pixel_7",
        "device": "panther",
        "transport_id": "1",
    },
    {"serial": "XYZ456", "state": "unauthorized"},
    {"serial": "OLD789", "state": "offline", "transport_id": "3"},
]

#: 可传输的那台
SERIAL = "ABC123"

#: 假设备上 ``getprop`` 能读到的属性（Spec §7）
PROPS = {
    SERIAL: {
        "ro.product.manufacturer": "Google",
        "ro.product.model": "Pixel 7",
        "ro.build.version.release": "13",
        "ro.build.version.sdk": "33",
    }
}

BOUNDS = (116.0, 39.0, 116.5, 39.5)
#: 覆盖 z12~z13：约 56 + 224 = 280 张瓦片，几百 KB，
#: 小到秒级跑完，又**大到**能看见进度条真的在动。
ZOOM_MIN = 12
ZOOM_MAX = 13


# --------------------------------------------------------------------------- #
# 假 adb 的源码：写进临时目录，由一个 .cmd / shell 壳脚本调用
# --------------------------------------------------------------------------- #
#:
#: 它只做一件事：把 ``/sdcard/...`` 这样的设备路径映射到
#: ``<FAKE_ADB_ROOT>/devices/<serial>/sdcard/...``，然后**真的**执行。
#: 每一次调用都会追加一行到 ``<FAKE_ADB_ROOT>/calls.jsonl``。
_FAKE_ADB_SOURCE = r'''"""假 adb（由 scripts/verify_slice10_android.py 生成，不参与生产）。

设备端文件系统 = 宿主目录 ``<FAKE_ADB_ROOT>/devices/<serial>/``。
失败注入靠根目录下的标记文件：
  fail_mkdir / fail_move / fail_push / no_sha256 / no_stat / no_size
  no_df / push_no_percent / disconnect_on_push / space_kb / size_delta / push_delay
"""
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(os.environ["FAKE_ADB_ROOT"])
VERSION = "1.0.41"


def marker(name):
    """标记文件存在则返回它的内容（没有则 None）。"""
    path = ROOT / name
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip()


def has(name):
    return (ROOT / name).exists()


def log(argv):
    try:
        line = json.dumps({"argv": list(argv), "t": time.time()}, ensure_ascii=False)
        with (ROOT / "calls.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def out(text):
    sys.stdout.buffer.write(text.encode("utf-8", "replace"))
    sys.stdout.buffer.flush()


def err(text):
    sys.stderr.buffer.write(text.encode("utf-8", "replace"))
    sys.stderr.buffer.flush()


def device_root(serial):
    return ROOT / "devices" / serial


def host_path(serial, remote):
    """设备路径 → 宿主路径。"""
    relative = remote.replace("\\", "/").lstrip("/")
    return device_root(serial) / relative


def main():
    argv = sys.argv[1:]
    log(argv)
    if not argv:
        err("adb: no command\n")
        return 1

    if argv[0] == "version":
        out("Android Debug Bridge version " + VERSION + "\n")
        out("Installed as C:\\fake\\platform-tools\\adb.exe\n")
        return 0

    if argv[0] == "devices":
        devices = json.loads((ROOT / "devices.json").read_text(encoding="utf-8"))
        out("List of devices attached\n")
        for item in devices:
            line = item["serial"] + "\t" + item["state"]
            for key in ("product", "model", "device", "transport_id"):
                if key in item:
                    line += " " + key + ":" + str(item[key])
            out(line + "\n")
        out("\n")
        return 0

    if argv[0] == "start-server" or argv[0] == "kill-server":
        return 0

    serial = None
    rest = argv
    if argv[0] == "-s":
        serial = argv[1]
        rest = argv[2:]
    if not rest:
        err("adb: no command\n")
        return 1

    command = rest[0]
    args = rest[1:]

    if command == "push":
        return do_push(serial, args)
    if command == "shell":
        return do_shell(serial, args)
    err("adb: unknown command " + command + "\n")
    return 1


def do_push(serial, args):
    if len(args) < 2:
        err("adb: push needs two arguments\n")
        return 1
    local = Path(args[0])
    remote = args[1]
    target = host_path(serial, remote)

    if has("disconnect_on_push"):
        err("adb: error: failed to copy: device offline\n")
        return 1
    if has("fail_push"):
        err("adb: error: failed to copy\n")
        return 1
    if not target.parent.is_dir():
        err("adb: error: failed to copy: No such file or directory\n")
        return 1
    if not local.is_file():
        err("adb: error: cannot find local file\n")
        return 1

    data = local.read_bytes()
    total = len(data)
    delay = float(marker("push_delay") or 0.0)
    show_percent = not has("push_no_percent")

    # 一块一块地写：这样"取消"能真的落在传输中间，留下半个 .part
    chunk = max(1, total // 20)
    written = 0
    with target.open("wb") as handle:
        while written < total:
            if (ROOT / "cancelled").exists():
                # 取消标记。为什么假 adb 要"配合"取消：Windows 上生产代码 kill 掉的
                # 是 ``adb.cmd``（cmd.exe），Python 孙子进程会被**孤儿化**继续跑完 ——
                # 那样半成品会被自己写完整，取消路径就测不准了。
                err("adb: transfer cancelled by user\n")
                return 1
            piece = data[written : written + chunk]
            handle.write(piece)
            handle.flush()
            written += len(piece)
            if show_percent:
                percent = min(100, int(written * 100 / max(1, total)))
                out("\r[%3d%%] %s\n" % (percent, local))
            if delay:
                time.sleep(delay)
    out(local.name + ": 1 file pushed. " + str(total) + " bytes\n")
    return 0


def do_shell(serial, args):
    if not args:
        err("adb: shell needs a command\n")
        return 1
    head = args[0]

    if head == "getprop":
        prop = args[-1]
        props = json.loads((ROOT / "props.json").read_text(encoding="utf-8"))
        value = props.get(serial, {}).get(prop, "")
        out(value + "\n")
        return 0

    path = args[-1]
    target = host_path(serial, path)

    if head == "mkdir":
        if has("fail_mkdir"):
            err("mkdir: '/sdcard': Permission denied\n")
            return 1
        os.makedirs(target, exist_ok=True)
        return 0

    if head == "rm":
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()
        return 0

    if head == "mv":
        if has("fail_move"):
            err("mv: Permission denied\n")
            return 1
        source = host_path(serial, args[-2])
        if not source.is_file():
            err("mv: " + args[-2] + ": No such file or directory\n")
            return 1
        shutil.move(str(source), str(target))
        return 0

    if head == "ls":
        if target.exists():
            out(path + "\n")
            return 0
        err("ls: " + path + ": No such file or directory\n")
        return 1

    if head == "stat":
        if has("no_stat") or has("no_size"):
            err("stat: Unknown option\n")
            return 1
        if not target.is_file():
            err("stat: No such file or directory\n")
            return 1
        out(str(target.stat().st_size + int(marker("size_delta") or 0)) + "\n")
        return 0

    if head == "wc":
        if has("no_size"):
            err("wc: cannot read\n")
            return 1
        if not target.is_file():
            err("wc: No such file or directory\n")
            return 1
        out(str(target.stat().st_size + int(marker("size_delta") or 0)) + " " + path + "\n")
        return 0

    if head in ("sha256sum", "md5sum") or head in ("toybox", "busybox"):
        if has("no_sha256"):
            err(head + ": not found\n")
            return 1
        if not target.is_file():
            err(head + ": No such file or directory\n")
            return 1
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        out(digest + "  " + path + "\n")
        return 0

    if head == "df":
        if has("no_df"):
            out("df: warning: something odd\n")
            return 0
        kilobytes = int(marker("space_kb") or (8 * 1024 * 1024))
        out("Filesystem     1K-blocks     Used Available Use% Mounted on\n")
        out("/dev/fuse     117311488 10240000 " + str(kilobytes) + "  7% /storage/emulated\n")
        return 0

    err(head + ": unknown command\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
'''

#: POSIX 壳脚本
_SHELL_WRAPPER = '#!/bin/sh\nexec "{python}" "{script}" "$@"\n'

#: Windows 壳脚本。``%*`` 原样转发参数（adbd 参数里没有空格，
#: 临时目录也不含空格），退出码由 cmd 透传。
_CMD_WRAPPER = '@echo off\n"{python}" "{script}" %*\n'


# --------------------------------------------------------------------------- #
# 检查结果收集
# --------------------------------------------------------------------------- #
class Checker:
    """收集检查结果，最后统一汇报并以退出码表达结论。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  [OK]   {label}")
        else:
            self.failures.append(f"{label}{f' — {detail}' if detail else ''}")
            print(f"  [FAIL] {label}{f' — {detail}' if detail else ''}")
        return bool(ok)

    def section(self, title: str) -> None:
        print(f"\n=== {title} ===")


def _pump(app, times: int = 8) -> None:
    for _ in range(times):
        app.processEvents()


def _wait_until(app, predicate, timeout_s: float = 60.0) -> bool:
    """轮询 + 空转事件循环。

    不能只 ``worker.wait()``：**线程结束**与**完成信号投递到主线程**之间有一个
    真实的窗口 —— 那时 ``isRunning()`` 已经是 ``False``，但界面上还是空的。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    app.processEvents()
    return bool(predicate())


def _shot(widget, name: str, enabled: bool) -> None:
    """把任意控件存成 ``docs/images/<name>``（先 ``activate()`` 再空转，见 Slice 9 的教训）。"""
    if not enabled or widget is None:
        return
    layout = widget.layout()
    if layout is not None:
        layout.activate()
    if _APP is not None:
        _pump(_APP, 12)
    DOCS.mkdir(parents=True, exist_ok=True)
    target = DOCS / name
    if widget.grab().save(str(target)):
        print(f"  [shot] {target.relative_to(PROJECT_ROOT)}")


# --------------------------------------------------------------------------- #
# 造"真的能执行的 adb"
# --------------------------------------------------------------------------- #
def _install_fake_adb(work_dir: Path) -> tuple[str, Path]:
    """把假 adb 装成一个**可执行文件**，返回 ``(adb_path, 假设备根)``。

    为什么不是一个 Python 对象：Spec §3 / §4 的三条硬性要求
    （参数是列表、禁用 ``shell=True``、输出流式读）**只有在真的起进程时才被检验**。
    一个 in-process 的假件永远不会因为 ``shell=True`` 而失败。
    """
    root = work_dir / "fake_device"
    (root / "devices").mkdir(parents=True, exist_ok=True)
    (root / "devices.json").write_text(
        json.dumps(DEVICES, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (root / "props.json").write_text(
        json.dumps(PROPS, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for device in DEVICES:
        (root / "devices" / device["serial"]).mkdir(parents=True, exist_ok=True)

    script = root / "fake_adb.py"
    script.write_text(_FAKE_ADB_SOURCE, encoding="utf-8")

    if os.name == "nt":
        wrapper = root / "adb.cmd"
        wrapper.write_text(
            _CMD_WRAPPER.format(python=sys.executable, script=script), encoding="utf-8"
        )
    else:
        wrapper = root / "adb"
        wrapper.write_text(
            _SHELL_WRAPPER.format(python=sys.executable, script=script), encoding="utf-8"
        )
        wrapper.chmod(0o755)
    return str(wrapper), root


def _calls(root: Path) -> list[list[str]]:
    """假 adb 记下的全部调用（argv 列表）。"""
    path = root / "calls.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line)["argv"] for line in path.read_text("utf-8").splitlines() if line]


def _reset_calls(root: Path) -> None:
    (root / "calls.jsonl").unlink(missing_ok=True)


def _mark(root: Path, name: str, value: str | None = None) -> None:
    """放一个失败注入标记。"""
    path = root / name
    path.write_text("" if value is None else value, encoding="utf-8")


def _unmark(root: Path, *names: str) -> None:
    for name in names:
        (root / name).unlink(missing_ok=True)


def _device_file(root: Path, remote: str, serial: str = SERIAL) -> Path:
    """设备上的路径 → 宿主路径。"""
    return root / "devices" / serial / remote.replace("\\", "/").lstrip("/")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# 造测试数据
# --------------------------------------------------------------------------- #
def _real_png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (32, 32), (30, 120, 200, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def _build_mbtiles(path: Path, *, name: str = "beijing") -> Path:
    """用**生产同一条 Writer** 造一个覆盖完整的 MBTiles（z12~z13）。"""
    from app.geo import GeoBounds, calculate_tile_range
    from app.mbtiles import MBTilesMetadata, MBTilesWriter
    from app.models import composition_for

    path.parent.mkdir(parents=True, exist_ok=True)
    bounds = GeoBounds(*BOUNDS)
    writer = MBTilesWriter(path)
    writer.open()
    writer.write_metadata(
        MBTilesMetadata.for_bounds(
            bounds,
            ZOOM_MIN,
            ZOOM_MAX,
            name=name,
            provider="tianditu",
            source_map_type="vector",
            with_labels=False,
            composition=composition_for(False),
        )
    )
    data = _real_png()
    for zoom in range(ZOOM_MIN, ZOOM_MAX + 1):
        tile_range = calculate_tile_range(bounds, zoom)
        for x in range(tile_range.min_x, tile_range.max_x + 1):
            for y in range(tile_range.min_y, tile_range.max_y + 1):
                writer.write_tile(zoom, x, y, data)
    writer.close()
    return path


def _build_package(work_dir: Path, *, name: str = "beijing", package_id: str | None = None) -> Path:
    """走生产同一条构建器造一个离线地图包。"""
    from app.package import MapPackageBuilder

    source = _build_mbtiles(work_dir / f"{name}.mbtiles", name=name)
    result = MapPackageBuilder().build(
        source, work_dir / "packages", package_name=name, package_id=package_id
    )
    if not result.success:
        raise SystemExit(f"夹具自己失败了：{result.errors}")
    return Path(result.package_dir)


def _make_client(adb_path: str, root: Path):
    """生产 :class:`AdbClient`，但指向假 adb。"""
    from app.android.adb_client import AdbClient

    env = dict(os.environ)
    env["FAKE_ADB_ROOT"] = str(root)
    return AdbClient(adb_path, env=env)


def _make_service(adb_path: str, root: Path):
    from app.services import AndroidTransferService

    return AndroidTransferService(client=_make_client(adb_path, root))


def _expected_remote_dir(package_dir: Path) -> str:
    from app.android.transfer_models import remote_dir_for
    from app.config.settings import DEFAULT_ANDROID_IMPORT_DIR

    manifest = json.loads((package_dir / "manifest.json").read_text("utf-8"))
    return remote_dir_for(DEFAULT_ANDROID_IMPORT_DIR, manifest["id"])


# --------------------------------------------------------------------------- #
# 场景 A：真子进程的 ADB 探测与设备发现
# --------------------------------------------------------------------------- #
def _scene_discovery(checker: Checker, adb_path: str, root: Path) -> None:
    """``adb version`` / ``devices -l`` / ``getprop`` 都是**真的子进程**。"""
    from app.config.settings import (
        ANDROID_PROP_MANUFACTURER,
        ANDROID_PROP_MODEL,
        DEFAULT_ANDROID_IMPORT_DIR,
    )

    checker.section("场景 A：ADB 探测与设备发现（真实子进程）")
    service = _make_service(adb_path, root)

    status = service.adb_status()
    checker.check(status.available is True, "adb 被识别为可用", str(status.error))
    checker.check(
        status.version == "1.0.41",
        "版本号从 `adb version` 的真实输出里解析出来",
        repr(status.version),
    )
    checker.check(
        status.path.endswith(("adb.cmd", "adb")),
        "使用的是配置里的 adb 路径",
        status.path,
    )

    outcome = service.refresh_devices()
    serials = [device.serial for device in outcome.devices]
    checker.check(serials == ["ABC123", "XYZ456", "OLD789"], "三台设备都被发现", str(serials))
    ready = [device.serial for device in outcome.ready_devices]
    checker.check(ready == [SERIAL], "只有 state=device 的那台可传输（Spec §8）", str(ready))

    device = service.get_device(SERIAL)
    checker.check(
        device is not None and device.manufacturer == "Google",
        "getprop 补全了厂商（Spec §7）",
        device.manufacturer if device else "None",
    )
    checker.check(
        device is not None and device.android_version == "13" and device.sdk_version == "33",
        "getprop 补全了 Android 版本与 SDK",
        f"{device.android_version if device else ''}/{device.sdk_version if device else ''}",
    )
    checker.check(
        service.is_ready("XYZ456") is False and service.is_ready("OLD789") is False,
        "未授权 / 离线的设备不许传输",
    )

    shell_calls = [call for call in _calls(root) if "shell" in call]
    serials_used = {call[call.index("-s") + 1] for call in shell_calls if "-s" in call}
    checker.check(
        serials_used == {SERIAL} or serials_used == set(),
        "每条设备相关命令都带 -s（Spec §21）",
        str(serials_used),
    )

    # getprop 走的是 ANDROID_PROP_* 常量，不是界面里写死的字符串
    props_read = {
        call[call.index("getprop") + 1]
        for call in shell_calls
        if "getprop" in call and call.index("getprop") + 1 < len(call)
    }
    checker.check(
        {ANDROID_PROP_MANUFACTURER, ANDROID_PROP_MODEL} <= props_read,
        "读的是 settings 里定义的属性名",
        str(sorted(props_read)),
    )
    checker.check(
        bool(DEFAULT_ANDROID_IMPORT_DIR.startswith("/sdcard/")),
        "远端导入区在公共目录（不是 /data/data）",
        DEFAULT_ANDROID_IMPORT_DIR,
    )


# --------------------------------------------------------------------------- #
# 场景 B：完整传输链路（真实字节落盘）
# --------------------------------------------------------------------------- #
def _scene_success(checker: Checker, adb_path: str, root: Path, package_dir: Path) -> None:
    """一次完整传输：真 push、真 mv、真校验，然后逐字节比对。"""
    from app.config.settings import PACKAGE_MANIFEST_FILENAME, PACKAGE_MBTILES_FILENAME

    checker.section("场景 B：完整传输链路（Spec §19 / §20 / §25 / §26）")
    _reset_calls(root)
    service = _make_service(adb_path, root)
    service.refresh_devices()

    events: list = []
    result = service.transfer_package(SERIAL, package_dir, progress_callback=events.append)

    checker.check(result.success is True, "传输成功", "；".join(result.errors))
    checker.check(result.checksum_verified is True, "远端 SHA256 校验通过")
    checker.check(result.size_verified is True, "远端大小校验通过")

    remote_dir = _expected_remote_dir(package_dir)
    checker.check(
        result.remote_package_dir == remote_dir,
        "远端落点 = <导入区>/<manifest.id>（Spec §14）",
        result.remote_package_dir,
    )
    checker.check(
        remote_dir.startswith("/sdcard/Download/TiandituOfflineMap/import/"),
        "远端落点在公共导入区",
        remote_dir,
    )

    local_mbtiles = package_dir / PACKAGE_MBTILES_FILENAME
    local_manifest = package_dir / PACKAGE_MANIFEST_FILENAME
    remote_mbtiles = _device_file(root, f"{remote_dir}/{PACKAGE_MBTILES_FILENAME}")
    remote_manifest = _device_file(root, f"{remote_dir}/{PACKAGE_MANIFEST_FILENAME}")

    checker.check(remote_mbtiles.is_file(), "设备上有 map.mbtiles")
    checker.check(remote_manifest.is_file(), "设备上有 manifest.json")
    checker.check(
        remote_mbtiles.is_file() and _sha256(remote_mbtiles) == _sha256(local_mbtiles),
        "设备上的 mbtiles 与本地**逐字节**相同",
    )
    checker.check(
        remote_manifest.is_file() and remote_manifest.read_bytes() == local_manifest.read_bytes(),
        "设备上的 manifest 与本地完全相同",
    )
    checker.check(
        result.remote_mbtiles_size == local_mbtiles.stat().st_size,
        "上报的远端大小与真实文件一致",
    )

    # 设备目录里只有两个文件：没有 `.part` 残骸、没有多余产物
    device_dir = remote_dir.replace("\\", "/").lstrip("/")
    names = sorted(item.name for item in (root / "devices" / SERIAL / device_dir).iterdir())
    checker.check(
        names == sorted([PACKAGE_MBTILES_FILENAME, PACKAGE_MANIFEST_FILENAME]),
        "设备目录里只剩正式的两个文件（无 .part 残骸）",
        str(names),
    )

    # ---- 顺序：这是"文件存在"不可信的根因（Spec §19 / §20）---------------
    calls = _calls(root)
    pushes = [call for call in calls if "push" in call]
    pushed_remotes = [call[-1] for call in pushes]
    checker.check(
        len(pushes) == 2,
        "只 push 了两个文件（Spec §36）",
        str(pushed_remotes),
    )
    checker.check(
        bool(pushed_remotes) and pushed_remotes[0].endswith(PACKAGE_MBTILES_FILENAME + ".part"),
        "大文件先落 .part",
        str(pushed_remotes[:1]),
    )
    checker.check(
        bool(pushed_remotes) and pushed_remotes[-1].endswith(PACKAGE_MANIFEST_FILENAME),
        "manifest.json 最后传（它是「包齐了」的信号）",
        str(pushed_remotes[-1:]),
    )

    def _index(predicate) -> int:
        for index, call in enumerate(calls):
            if predicate(call):
                return index
        return -1

    mkdir_at = _index(lambda call: "mkdir" in call)
    push_at = _index(lambda call: "push" in call)
    mv_at = _index(lambda call: "mv" in call)
    checker.check(
        0 <= mkdir_at < push_at,
        "mkdir 早于 push（真机上父目录不存在时 push 必失败，Spec §16）",
        f"mkdir@{mkdir_at} push@{push_at}",
    )
    checker.check(
        0 <= push_at < mv_at,
        "先传 .part 再 mv 改名（Spec §20）",
        f"push@{push_at} mv@{mv_at}",
    )
    mv_call = calls[mv_at] if mv_at >= 0 else []
    checker.check(
        len(mv_call) >= 2
        and mv_call[-2].endswith(PACKAGE_MBTILES_FILENAME + ".part")
        and mv_call[-1].endswith(PACKAGE_MBTILES_FILENAME),
        "mv 的源是 .part、目标是正式名字",
        str(mv_call[-2:]),
    )

    # ---- 进度曲线（Spec §22 / §30）---------------------------------------
    percents = [event.percent for event in events]
    checker.check(bool(percents) and percents == sorted(percents), "进度单调不减（Spec §30）")
    checker.check(bool(percents) and percents[0] == 0.0, "进度从 0 开始", str(percents[:1]))
    checker.check(bool(percents) and percents[-1] == 100.0, "进度收在 100", str(percents[-1:]))
    stages = {event.stage for event in events}
    expected_stages = {
        "check",
        "device",
        "space",
        "mkdir",
        "mbtiles",
        "rename",
        "manifest",
        "verify",
        "done",
    }
    checker.check(expected_stages <= stages, "每个阶段都被走到", str(sorted(expected_stages - stages)))
    percent_events = [event for event in events if event.percent and 18.0 < event.percent < 88.0]
    checker.check(
        len(percent_events) >= 2,
        "push 的真实百分比被流式读进进度（Spec §22：不重新实现 ADB 协议）",
        f"{len(percent_events)} 帧落在 mbtiles 区间",
    )
    checker.check(
        events[-1].bytes_transferred == events[-1].total_bytes > 0,
        "结束帧的「已传 / 总量」对得上",
    )


# --------------------------------------------------------------------------- #
# 场景 C：取消、覆盖、以及各种"设备不配合"
# --------------------------------------------------------------------------- #
def _scene_edge_cases(
    checker: Checker, adb_path: str, root: Path, work_dir: Path, package_dir: Path
) -> None:
    from app.android.transfer_models import is_safe_remote_root, remote_dir_for
    from app.config.settings import (
        DEFAULT_ANDROID_IMPORT_DIR,
        PACKAGE_MANIFEST_FILENAME,
        PACKAGE_MBTILES_FILENAME,
        TRANSFER_ERROR_DISCONNECTED,
        TRANSFER_ERROR_REMOTE_ROOT,
    )

    checker.section("场景 C：取消 / 覆盖 / 设备不配合（Spec §23 / §24 / §17 / §25 / §26）")
    remote_dir = _expected_remote_dir(package_dir)

    # ---- C1. 取消（真实杀进程）------------------------------------------
    _unmark(root, "fail_mkdir", "no_sha256", "size_delta", "cancelled")
    _mark(root, "push_delay", "0.2")
    service = _make_service(adb_path, root)
    service.refresh_devices()

    # 把上一场景留下的包清掉，只留一个"用户自己的文件"：
    # 取消路径要证明的正是"只删自己的 .part，不碰用户的东西"（Spec §23）
    device_dir = root / "devices" / SERIAL / remote_dir.replace("\\", "/").lstrip("/")
    shutil.rmtree(device_dir, ignore_errors=True)
    device_dir.mkdir(parents=True, exist_ok=True)
    keep = device_dir / "keepme.bin"
    keep.write_bytes(b"user data")

    cancel = threading.Event()

    def _cancel_on_progress(event) -> None:
        # 等到大文件真的传起来了再取消：在 `mbtiles` 阶段中途动手，
        # 设备上才会真的留着半个 `.part`（Spec §23 要清理的就是它）
        if event.stage == "mbtiles" and event.percent >= 30.0:
            cancel.set()
            _mark(root, "cancelled")

    cancelled = service.transfer_package(
        SERIAL, package_dir, progress_callback=_cancel_on_progress, cancel_event=cancel
    )
    _unmark(root, "cancelled", "push_delay")
    checker.check(cancelled.cancelled is True, "取消被如实上报为 cancelled")
    checker.check(cancelled.success is False, "取消不算成功")
    checker.check(
        sorted(item.name for item in device_dir.iterdir()) == ["keepme.bin"],
        "取消后设备上只剩用户自己的文件（.part 已删，Spec §23）",
        str(sorted(item.name for item in device_dir.iterdir())),
    )
    checker.check(keep.read_bytes() == b"user data", "取消**不碰**用户已有的文件（Spec §23）")
    checker.check(
        _device_file(root, f"{remote_dir}/{PACKAGE_MBTILES_FILENAME}").exists() is False,
        "取消后没有留下一个「看起来完整」的 mbtiles",
    )
    checker.check(
        cancelled.bytes_transferred > 0,
        "取消也如实上报「传了多少」（用户据此决定要不要重来）",
        str(cancelled.bytes_transferred),
    )

    # ---- C2. 远端已有同名包 → 先问；选覆盖 → 删旧再传 --------------------
    service = _make_service(adb_path, root)
    service.refresh_devices()
    first = service.transfer_package(SERIAL, package_dir)
    checker.check(first.success is True, "第一次传输成功", "；".join(first.errors))

    stale = _device_file(root, f"{remote_dir}/stale.bin")
    stale.write_bytes(b"stale")

    asked = service.transfer_package(SERIAL, package_dir)
    checker.check(
        asked.needs_confirmation is True and asked.success is False,
        "目标已存在同名包 → 先停下来问（Spec §24）",
        asked.confirmation_reason,
    )
    checker.check(
        _device_file(root, f"{remote_dir}/{PACKAGE_MANIFEST_FILENAME}").read_bytes()
        == (package_dir / PACKAGE_MANIFEST_FILENAME).read_bytes(),
        "未确认时不覆盖旧包",
    )

    again = service.transfer_package(SERIAL, package_dir, overwrite=True)
    checker.check(again.success is True, "选「覆盖」之后传成功", "；".join(again.errors))
    checker.check(again.overwritten is True, "结果里标明了这次是覆盖")
    checker.check(stale.exists() is False, "覆盖时旧包目录被清干净（Spec §24）")

    # ---- C3. 设备不支持 sha256sum → WARNING 而不是失败（Spec §26）--------
    _mark(root, "no_sha256")
    service = _make_service(adb_path, root)
    service.refresh_devices()
    no_sha = service.transfer_package(SERIAL, package_dir, overwrite=True)
    checker.check(no_sha.success is True, "设备没有 sha256sum 时仍然算成功")
    checker.check(no_sha.checksum_verified is False, "checksum_verified 如实置 False")
    checker.check(bool(no_sha.warnings), "给出一条 WARNING 而不是错误", str(no_sha.warnings))
    _unmark(root, "no_sha256")

    # ---- C4. 远端大小对不上 → 硬失败（Spec §25）--------------------------
    _mark(root, "size_delta", "1024")
    service = _make_service(adb_path, root)
    service.refresh_devices()
    bad_size = service.transfer_package(SERIAL, package_dir, overwrite=True)
    checker.check(bad_size.success is False, "远端大小不一致必须失败（Spec §25）")
    checker.check(bool(bad_size.errors), "失败原因写进了 errors", str(bad_size.errors))
    _unmark(root, "size_delta")

    # ---- C5. mkdir 失败 → 一个字节都不推（Spec §16）---------------------
    _mark(root, "fail_mkdir")
    _reset_calls(root)
    service = _make_service(adb_path, root)
    service.refresh_devices()
    no_mkdir = service.transfer_package(SERIAL, package_dir, overwrite=True)
    checker.check(no_mkdir.success is False, "mkdir 失败时传输失败（Spec §16）")
    checker.check(
        not [call for call in _calls(root) if "push" in call],
        "mkdir 失败后一个 push 都没发（Spec §16）",
    )
    _unmark(root, "fail_mkdir")

    # ---- C6. 空间不足 → 拦下；df 解析不出来 → 只警告（Spec §17）----------
    # 阈值按**真实包大小**算出来，不写死："64 KB 够不够"取决于包多大，
    # 而包的大小会随着 BOUNDS / ZOOM 改。写死的数字迟早变成"这次又没拦住"。
    needed = (package_dir / PACKAGE_MBTILES_FILENAME).stat().st_size
    needed += (package_dir / PACKAGE_MANIFEST_FILENAME).stat().st_size
    _mark(root, "space_kb", str(max(1, needed // 1024 // 2)))  # 只给一半
    service = _make_service(adb_path, root)
    service.refresh_devices()
    no_space = service.transfer_package(SERIAL, package_dir, overwrite=True)
    checker.check(no_space.success is False, "空间不足时拦下（Spec §17）")
    checker.check("空间" in " ".join(no_space.errors), "错误里说清了是空间问题")
    checker.check(
        not [call for call in _calls(root) if "push" in call],
        "空间不足时一个字节都不推（Spec §17 第 2 步）",
    )
    _unmark(root, "space_kb")

    _mark(root, "no_df")
    service = _make_service(adb_path, root)
    service.refresh_devices()
    bad_df = service.transfer_package(SERIAL, package_dir, overwrite=True)
    checker.check(bad_df.success is True, "df 解析不出来时继续传输（Spec §17）")
    checker.check(bool(bad_df.warnings), "df 解析失败给 WARNING")
    _unmark(root, "no_df")

    # ---- C7. 设备掉线 ----------------------------------------------------
    _mark(root, "disconnect_on_push")
    service = _make_service(adb_path, root)
    service.refresh_devices()
    dropped = service.transfer_package(SERIAL, package_dir, overwrite=True)
    checker.check(dropped.success is False, "设备掉线时传输失败")
    checker.check(
        TRANSFER_ERROR_DISCONNECTED in dropped.errors,
        "错误里说清了是设备断开（Spec §35）",
        str(dropped.errors),
    )
    checker.check(
        any(PACKAGE_MBTILES_FILENAME + ".part" in warning for warning in dropped.warnings)
        or _device_file(root, f"{remote_dir}/{PACKAGE_MBTILES_FILENAME}.part").exists(),
        "掉线时刻意保留 .part（那是排查「线是不是松了」的实物证据，Spec §35）",
        str(dropped.warnings),
    )
    _unmark(root, "disconnect_on_push")

    # ---- C8. 远端路径安全（Spec §15 / §43）-------------------------------
    hostile = ["../../etc", "..", "-rf /", "a/../../b", "", "..\\..\\win", "a;b|c", "$(x)"]
    escaped = []
    for raw in hostile:
        target = remote_dir_for(DEFAULT_ANDROID_IMPORT_DIR, raw)
        if (
            not target.startswith(DEFAULT_ANDROID_IMPORT_DIR + "/")
            or ".." in target.replace("\\", "/").split("/")
        ):
            escaped.append((raw, target))
    checker.check(not escaped, "危险 id 一律被规整到导入区之内（Spec §15）", str(escaped))

    checker.check(is_safe_remote_root("/") is False, "拒绝把设备根目录当导入区")
    checker.check(
        is_safe_remote_root(DEFAULT_ANDROID_IMPORT_DIR) is True,
        "默认导入区是安全的",
    )

    service = _make_service(adb_path, root)
    service.refresh_devices()
    bad_root = service.transfer_package(SERIAL, package_dir, remote_root="/")
    checker.check(bad_root.success is False, "远端根目录不安全时拒绝传输")
    checker.check(
        TRANSFER_ERROR_REMOTE_ROOT.format(root="/") in bad_root.errors, "给出的是根目录错误"
    )

    # 带恶意 id 的包：**第 1 道防线**是构建器本身（id 有白名单校验）
    hostile_rejected = False
    try:
        _build_package(work_dir, name="hostile", package_id="../../etc/passwd")
    except SystemExit:
        hostile_rejected = True
    checker.check(hostile_rejected, "构建器拒绝带 ../ 的 package id（第 1 道防线）")

    # **第 2 / 3 道防线**：手工改 manifest 绕过构建器 —— 读取器要么拒绝，
    # 要么规整函数必须把它拽回导入区之内。两道里至少有一道要生效。
    forged = work_dir / "packages" / "forged"
    shutil.rmtree(forged, ignore_errors=True)
    shutil.copytree(package_dir, forged)
    forged_manifest = forged / PACKAGE_MANIFEST_FILENAME
    payload = json.loads(forged_manifest.read_text("utf-8"))
    payload["id"] = "../../etc/passwd"
    forged_manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    forged_plan = service.plan(SERIAL, forged)
    inside = forged_plan.remote_dir.startswith(
        DEFAULT_ANDROID_IMPORT_DIR + "/"
    ) and ".." not in forged_plan.remote_dir
    checker.check(
        (not forged_plan.is_valid) or inside,
        "手工改过的 manifest.id 要么被读取器拒绝，要么被规整进导入区（Spec §15 / §43）",
        forged_plan.error or forged_plan.remote_dir,
    )
    checker.check(
        _device_file(root, "/etc/passwd").exists() is False,
        "设备上没有被写到导入区之外的文件",
    )

    # ---- C9. 设备未授权 / 离线 → 不传 ------------------------------------
    service = _make_service(adb_path, root)
    service.refresh_devices()
    for serial in ("XYZ456", "OLD789"):
        blocked = service.transfer_package(serial, package_dir, verify_package=False)
        checker.check(
            blocked.success is False and bool(blocked.errors),
            f"{serial}（未授权/离线）被拦住且给出原因",
            str(blocked.errors[:1]),
        )

    # ---- C10. adb 不存在 -------------------------------------------------
    missing_path = str(root / "definitely-not-adb.exe")
    missing = _make_service(missing_path, root)
    status = missing.adb_status()
    checker.check(
        status.available is False and bool(status.error),
        "adb 不存在时给出可读的错误（Spec §10）",
        str(status.error),
    )
    outcome = missing.refresh_devices()
    checker.check(
        outcome.devices == [] and outcome.error,
        "adb 不存在时设备列表为空且不抛异常",
    )


# --------------------------------------------------------------------------- #
# 场景 D：静态守卫
# --------------------------------------------------------------------------- #
def _find_call_lines(source: str, func_names: set[str], keyword: str) -> list[int]:
    """找出 ``func_names`` 里带了 ``keyword=`` 这个关键字的调用所在行号。

    刻意用 ``ast`` 而不是 ``"shell=True" in source``：字符串匹配会把注释、
    文档字符串和别的文件里的字面量一起算进来（Slice 9 的 ``checksum.py``
    就在文档里写过"别用 read_bytes()"，字符串匹配必然误报）。
    """
    tree = ast.parse(source)
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ""
        if isinstance(node.func, ast.Attribute):
            name = node.func.attr
        elif isinstance(node.func, ast.Name):
            name = node.func.id
        if name not in func_names:
            continue
        if any(item.arg == keyword for item in node.keywords):
            found.append(node.lineno)
    return found


def _scene_static(checker: Checker) -> None:
    from PySide6.QtCore import QThread

    from app.workers import AndroidDeviceWorker, AndroidPackageCheckWorker, AndroidTransferWorker

    checker.section("场景 D：静态守卫（Spec §3 / §28 / §29）")

    android_dir = PROJECT_ROOT / "app" / "android"
    sources = {path.name: path.read_text(encoding="utf-8") for path in android_dir.glob("*.py")}
    sources["android_transfer_service.py"] = (
        PROJECT_ROOT / "app" / "services" / "android_transfer_service.py"
    ).read_text(encoding="utf-8")
    sources["android_transfer_worker.py"] = (
        PROJECT_ROOT / "app" / "workers" / "android_transfer_worker.py"
    ).read_text(encoding="utf-8")

    offenders: list[str] = []
    for name, source in sources.items():
        for line in _find_call_lines(
            source, {"run", "Popen", "call", "check_output", "check_call"}, "shell"
        ):
            offenders.append(f"{name}:{line}")
    checker.check(not offenders, "没有任何 shell=True 调用（Spec §3）", ", ".join(offenders))

    # 子进程调用的第一个参数必须是列表字面量 / 列表变量，不能是一整条字符串。
    string_commands: list[str] = []
    for name, source in sources.items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called not in {"run", "Popen"} or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                string_commands.append(f"{name}:{node.lineno}")
    checker.check(
        not string_commands,
        "没有把整条命令当字符串传给 subprocess（Spec §3）",
        ", ".join(string_commands),
    )

    ui_sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in (PROJECT_ROOT / "app" / "ui").glob("*.py")
    }
    # 界面层不许直接起子进程 —— 例外是 ``desktop.py``：它用 ``open`` / ``xdg-open``
    # 打开"输出目录"，跟 ADB 无关（Slice 0 就有的功能）。ADB 路径上的三个文件
    # 一个都不许碰 ``subprocess``。
    adb_ui_files = ("device_panel.py", "main_window.py")
    ui_subprocess = [
        name
        for name in adb_ui_files
        if name in ui_sources and "subprocess" in ui_sources[name]
    ]
    checker.check(
        not ui_subprocess,
        "设备 / 传输相关的界面文件不碰 subprocess（Spec §28）",
        str(ui_subprocess),
    )

    panel = ui_sources.get("device_panel.py", "")
    forbidden = [
        keyword
        for keyword in ("AdbClient", "MapPackageTransfer", "MapPackageValidator", "adb.push")
        if keyword in panel
    ]
    checker.check(
        not forbidden,
        "设备面板不组合 adb / 传输器 / 校验器（Spec §28）",
        str(forbidden),
    )

    checker.check(
        issubclass(AndroidTransferWorker, QThread)
        and issubclass(AndroidDeviceWorker, QThread)
        and issubclass(AndroidPackageCheckWorker, QThread),
        "三种后台任务都是 QThread（Spec §29）",
    )
    checker.check(
        "adb_path" in sources["adb_client.py"] and "ADB_PATH" in sources["adb_client.py"],
        "adb 路径可按用户配置 / 环境变量解析（Spec §2）",
    )
    checker.check(
        "CREATE_NO_WINDOW" in sources["adb_client.py"],
        "Windows 上 adb 子进程不弹黑窗口",
    )
    checker.check(
        "stderr" in sources["adb_client.py"] and "stderr_reader" in sources["adb_client.py"],
        "push 时同时抽干 stdout 与 stderr（管道写满会死锁）",
    )


# --------------------------------------------------------------------------- #
# 场景 E：真实窗口
# --------------------------------------------------------------------------- #
def _scene_gui(app, checker: Checker, work_dir: Path, adb_path: str, root: Path, shots: bool) -> None:
    """真窗口、真按钮、真线程：从 [刷新设备] 到 [传输到设备] 走一遍。"""
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QFileDialog, QGroupBox

    from app.config.settings import (
        DEFAULT_WINDOW_HEIGHT,
        DEFAULT_WINDOW_WIDTH,
        PACKAGE_MANIFEST_FILENAME,
        PACKAGE_MBTILES_FILENAME,
        UI_TEXT_TRANSFER_CONFIRM_TITLE,
    )
    from app.tasks import TaskService, TaskStore
    from app.ui.main_window import MainWindow
    from app.ui.preferences import UiPreferences

    checker.section("场景 E：真实窗口（Spec §11 / §30 / §31）")

    package_dir = _build_package(work_dir, name="guimap")
    remote_dir = _expected_remote_dir(package_dir)

    # 取消掉上一轮留下的注入标记，并让 push 慢一点，好抓住"传输中"那一帧
    _unmark(root, *["fail_mkdir", "fail_move", "fail_push", "no_sha256", "size_delta", "no_df"])
    _mark(root, "push_delay", "0.15")
    _reset_calls(root)

    preferences = UiPreferences(QSettings(str(work_dir / "ui.ini"), QSettings.Format.IniFormat))
    window = MainWindow(
        task_service=TaskService(TaskStore(work_dir / "tasks.json")),
        preferences=preferences,
        android_service=_make_service(adb_path, root),
    )
    window.show()
    _pump(app)

    # ---- E0. 布局护栏：右列不该出现滚动条 -------------------------------
    checker.check(
        window.width() == DEFAULT_WINDOW_WIDTH and window.height() == DEFAULT_WINDOW_HEIGHT,
        "默认窗口尺寸没变（Slice 0 的约定）",
        f"{window.width()}x{window.height()}",
    )
    checker.check(
        window._device_scroll.verticalScrollBar().maximum() == 0,
        "默认尺寸下设备区没有竖直滚动条（内容没被挤出去）",
        str(window._device_scroll.verticalScrollBar().maximum()),
    )
    checker.check(
        window._device_scroll.horizontalScrollBar().maximum() == 0,
        "默认尺寸下设备区没有水平滚动条",
    )

    panel = window.device_panel
    boxes = sorted(panel.findChildren(QGroupBox), key=lambda item: item.geometry().top())
    checker.check(len(boxes) >= 3, "设备区至少有三个分组（设备 / 地图包 / 传输）", str(len(boxes)))
    overlaps = [
        (a.title(), b.title())
        for index, a in enumerate(boxes)
        for b in boxes[index + 1 :]
        if a.geometry().intersects(b.geometry())
    ]
    checker.check(not overlaps, "分组之间没有几何重叠", str(overlaps))
    escaped = [
        box.title()
        for box in boxes
        if box.geometry().right() > panel.width() or box.geometry().bottom() > panel.height()
    ]
    checker.check(not escaped, "分组没有越出面板边界", str(escaped))

    # ---- E1. [刷新设备] --------------------------------------------------
    panel.refresh_button.click()
    checker.check(
        _wait_until(app, lambda: panel.device_combo.count() == 3, timeout_s=30),
        "[刷新设备] 后下拉框出现三台设备",
        str(panel.device_combo.count()),
    )
    checker.check(panel.selected_serial == SERIAL, "默认选中第一台可用设备", panel.selected_serial)
    checker.check(
        panel.selected_device() is not None and panel.selected_device().manufacturer == "Google",
        "设备详情里显示了厂商（getprop 的结果真的进了界面）",
    )
    checker.check(panel.transfer_button.isEnabled() is False, "还没选包时 [传输到设备] 是禁用的")
    _shot(panel, "slice10_device_panel.png", shots)

    # ---- E2. [选择地图包]（桩掉系统对话框）-------------------------------
    original = QFileDialog.getExistingDirectory
    QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: str(package_dir))
    try:
        panel.choose_package_button.click()
    finally:
        QFileDialog.getExistingDirectory = original

    checker.check(panel.package_dir == str(package_dir), "选中的包目录显示出来了", panel.package_dir)
    # Spec §12：选中之后**立刻**后台校验，校验期间不许传
    checker.check(
        panel.transfer_button.isEnabled() is False,
        "包还没校验完 → [传输到设备] 先灰着（Spec §12）",
    )
    checker.check(
        _wait_until(
            app,
            lambda: not (
                window._package_check_worker and window._package_check_worker.isRunning()
            ),
            timeout_s=60,
        ),
        "后台校验线程跑完了",
    )
    _pump(app, 20)
    checker.check(
        panel.package_status_text.startswith("PASS"),
        "界面上给出 PASS 结论（Spec §12）",
        panel.package_status_text,
    )
    checker.check(
        panel.transfer_button.isEnabled() is True,
        "设备可用 + 包校验通过 → [传输到设备] 可用",
    )

    # ---- E3. [传输到设备] → 确认框 ---------------------------------------
    panel.transfer_button.click()
    _pump(app, 20)
    dialog = window._transfer_confirm_dialog
    checker.check(dialog is not None, "弹出了传输确认框（Spec §31）")
    body = dialog.text() if dialog is not None else ""
    checker.check(
        dialog is not None and dialog.windowTitle() == UI_TEXT_TRANSFER_CONFIRM_TITLE,
        "确认框标题正确",
        dialog.windowTitle() if dialog is not None else "",
    )
    checker.check(SERIAL in body, "确认框写清了设备序列号", body[:80])
    checker.check(remote_dir in body, "确认框写清了远端落点", body[:120])
    checker.check("KB" in body or "MB" in body or "B" in body, "确认框写清了要传多少")
    _shot(dialog, "slice10_transfer_confirm.png", shots)

    # ---- E4. 点「开始」→ 真的开始传 --------------------------------------
    start_button = window._transfer_confirm_start
    checker.check(start_button is not None, "确认框有「开始」按钮")
    if start_button is not None:
        start_button.click()
    _pump(app, 20)

    # 等进度真的走进 mbtiles 区间再抓图：6% 是"正在检查设备"那一档，
    # 那时"当前文件 / 速度"本来就该是占位符——抓早了截到的是空状态。
    checker.check(
        _wait_until(app, lambda: 20 <= panel.progress_bar.value() < 95, timeout_s=30),
        "传输中进度条真的在走（Spec §30）",
        str(panel.progress_bar.value()),
    )
    transfer_values = panel.transfer_values
    checker.check(
        PACKAGE_MBTILES_FILENAME in transfer_values["file"].text(),
        "传输中显示了当前文件名",
        transfer_values["file"].text(),
    )
    checker.check(
        _wait_until(app, lambda: transfer_values["speed"].text() != "—", timeout_s=15),
        "传输中显示了速度（Spec §30 的三行数字）",
        transfer_values["speed"].text(),
    )
    checker.check(
        "/" in transfer_values["bytes"].text(),
        "传输中显示了「已传 / 总量」",
        transfer_values["bytes"].text(),
    )
    _shot(panel, "slice10_transfer_progress.png", shots)
    checker.check(
        panel.transfer_button.isEnabled() is False,
        "传输中 [传输到设备] 被禁用（防止点两次）",
    )
    checker.check(panel.cancel_button.isEnabled() is True, "传输中 [取消传输] 可用")

    checker.check(
        _wait_until(app, lambda: panel.progress_bar.value() == 100, timeout_s=60),
        "传输走到 100%",
        str(panel.progress_bar.value()),
    )
    _wait_until(
        app,
        lambda: not (window._transfer_worker and window._transfer_worker.isRunning()),
        timeout_s=30,
    )
    _pump(app, 30)

    state = panel.transfer_state
    checker.check(state == "COMPLETED", "界面收在 COMPLETED 状态", state)
    log_text = window.log_panel.to_text()
    checker.check(remote_dir in log_text or "传输完成" in log_text, "日志里说清了传输结果")
    _shot(panel, "slice10_transfer_done.png", shots)

    # ---- E5. 真的落盘了 --------------------------------------------------
    remote_mbtiles = _device_file(root, f"{remote_dir}/{PACKAGE_MBTILES_FILENAME}")
    local_mbtiles = package_dir / PACKAGE_MBTILES_FILENAME
    checker.check(
        remote_mbtiles.is_file() and _sha256(remote_mbtiles) == _sha256(local_mbtiles),
        "界面上点出来的这次传输，字节真的落到了设备上",
    )
    checker.check(
        _device_file(root, f"{remote_dir}/{PACKAGE_MANIFEST_FILENAME}").is_file(),
        "manifest.json 也在设备上",
    )

    # ---- E6. 第二次传输 → 覆写确认 → 真的覆盖（Spec §24）----------------
    # 在设备上放一个"上一次留下的东西"，它必须活到用户点「覆盖」为止
    stale = _device_file(root, f"{remote_dir}/stale.bin")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"stale")

    _close_dialogs(window)
    panel.transfer_button.click()
    checker.check(
        _wait_until(app, lambda: window._transfer_confirm_dialog is not None, timeout_s=20),
        "再次点击仍然先出确认框",
    )
    if window._transfer_confirm_start is not None:
        window._transfer_confirm_start.click()
    checker.check(
        _wait_until(app, lambda: window._transfer_exists_dialog is not None, timeout_s=30),
        "设备上已有同名包 → 弹出覆盖确认（Spec §24）",
    )
    exists_dialog = window._transfer_exists_dialog
    checker.check(
        exists_dialog is not None and remote_dir in exists_dialog.text(),
        "覆盖确认框写清了设备上那份包在哪",
        exists_dialog.text()[:120] if exists_dialog is not None else "",
    )
    checker.check(stale.exists(), "没点「覆盖」之前，设备上的旧内容原封不动（Spec §24）")
    _shot(exists_dialog, "slice10_overwrite_confirm.png", shots)

    overwrite_button = window._transfer_overwrite_button
    checker.check(overwrite_button is not None, "覆盖框有「覆盖」按钮")
    if overwrite_button is not None:
        overwrite_button.click()
    checker.check(
        _wait_until(app, lambda: panel.progress_bar.value() == 100, timeout_s=60),
        "点「覆盖」之后重新传了一遍",
        str(panel.progress_bar.value()),
    )
    _wait_until(
        app,
        lambda: not (window._transfer_worker and window._transfer_worker.isRunning()),
        timeout_s=30,
    )
    _pump(app, 30)
    checker.check(stale.exists() is False, "覆盖时旧内容被清干净（Spec §24）")
    checker.check(
        _sha256(_device_file(root, f"{remote_dir}/{PACKAGE_MBTILES_FILENAME}"))
        == _sha256(local_mbtiles),
        "覆盖之后的字节仍然与本地一致",
    )

    _close_dialogs(window)
    window._exit_confirmed = True
    worker = window._transfer_worker
    if worker is not None and worker.isRunning():
        worker.cancel()
        worker.wait(10_000)
    window.close()
    _pump(app)


def _close_dialogs(window) -> None:
    """把还开着的对话框关掉（不等用户点）。"""
    for attribute in (
        "_transfer_confirm_dialog",
        "_transfer_exists_dialog",
        "_transfer_done_dialog",
    ):
        dialog = getattr(window, attribute, None)
        if dialog is not None:
            with contextlib.suppress(RuntimeError):
                dialog.close()
    _pump(_APP, 4)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slice 10 ADB 传输实机验收")
    parser.add_argument(
        "--offscreen",
        action="store_true",
        help="使用 Qt offscreen 平台插件（适合无显示环境；中文字体可能缺失）",
    )
    parser.add_argument("--no-shots", action="store_true", help="只跑检查，不截图")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    shots = not args.no_shots

    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    from PySide6.QtWidgets import QApplication

    from app.main import setup_logging

    setup_logging()

    work_dir = Path(tempfile.mkdtemp(prefix="verify-slice10-"))
    app = QApplication(sys.argv[:1])
    global _APP
    _APP = app
    checker = Checker()

    adb_path, root = _install_fake_adb(work_dir)
    print(f"platform={app.platformName()}  work_dir={work_dir}")
    print(f"fake adb = {adb_path}")

    try:
        _scene_discovery(checker, adb_path, root)
        package_dir = _build_package(work_dir)
        _scene_success(checker, adb_path, root, package_dir)
        _scene_edge_cases(checker, adb_path, root, work_dir, package_dir)
        _scene_static(checker)
        _scene_gui(app, checker, work_dir, adb_path, root, shots)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    print("\n" + "=" * 60)
    if checker.failures:
        print(f"结果：{len(checker.failures)} 项失败 / 共 {checker.passed} 项通过")
        for failure in checker.failures:
            print(f"  - {failure}")
        return 1
    print(f"结果：全部通过（{checker.passed} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
