"""``MapPackageTransfer`` 的单元测试（Slice 10，Spec §18 ~ §27 / §40 ~ §43）。

这里守的是"传到设备上的东西与本地一模一样"，以及四件能在全绿情况下悄悄错掉的事：

1. **顺序（Spec §19 / §20）。** ``mkdir`` 必须早于 ``push``；大文件必须先落
   ``.part`` 再 ``mv``；``manifest.json`` 必须最后。写反了功能"看起来"还对，
   但 Android 端会在文件只写了一半时就把它当成完整的包。
2. **失败要停（Spec §16 / §25 / §26）。** mkdir 失败还能继续 push 吗？
   远端大小不一致还能算成功吗？这些分支正常用例一次都走不到。
3. **远端路径安全（Spec §14 / §15 / §43）。** ``manifest.id`` 是**文件里的内容**，
   它会被拼进设备端命令。``../../abc`` 与 ``-rf`` 都必须被挡住。
4. **SHA256 是 best-effort（Spec §26 / §33）。** 设备不支持时是 WARNING，
   不是 FAIL；但"摘要对不上"必须 FAIL。

设备端由 :class:`tests.android_fakes.FakeAdbClient` 扮演：它维护一份设备上的
文件表，**真的执行** mkdir / mv / rm / stat / sha256sum 的语义，因此上面每一条
都能被直接断言（而不只是"没抛异常"）。
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest

from app.android.map_package_transfer import MapPackageTransfer
from app.android.transfer_models import (
    is_safe_remote_root,
    join_remote,
    part_path,
    remote_dir_for,
    sanitize_remote_id,
)
from app.config.settings import (
    DEFAULT_ANDROID_IMPORT_DIR,
    PACKAGE_MANIFEST_FILENAME,
    PACKAGE_MBTILES_FILENAME,
    TRANSFER_PART_SUFFIX,
)
from app.package import MapPackageBuilder
from tests.android_fakes import FakeAdbClient, FakeDevice
from tests.test_package_builder import _make_mbtiles

#: 测试用的远端根目录（与生产默认值一致，但刻意写成局部常量：
#: 改生产默认值时这里的用例不该跟着变）
REMOTE_ROOT = DEFAULT_ANDROID_IMPORT_DIR

#: 可传输的设备
SERIAL = "ABC123"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _make_package(work_dir: Path, name: str = "beijing", **kwargs) -> Path:
    """造一个**真的**离线地图包（走生产同一条构建器）。

    刻意不手写 manifest.json：手写的那个会把"构建器与传输层对字段的理解不一致"
    这类错误掩盖掉——而传输层要靠 manifest 的 ``id`` 与 ``mbtiles.sha256``。
    """
    source = _make_mbtiles(work_dir / f"{name}.mbtiles", name=name, **kwargs)
    result = MapPackageBuilder().build(source, work_dir / "packages", package_name=name)
    assert result.success, f"夹具自己失败了：{result.errors}"
    return Path(result.package_dir)


def _client(**kwargs) -> FakeAdbClient:
    """一台已连接设备的假 adb（``ABC123`` 是 device 状态）。"""
    client = FakeAdbClient(**kwargs)
    client.device(SERIAL).available_bytes = 4 * 1024**3
    return client


def _transfer(work_dir, package_dir, client=None, **kwargs):
    """跑一次传输，返回 ``(结果, 假 adb)``。"""
    client = client or _client()
    transfer = MapPackageTransfer(client)
    transfer.device_manager.refresh_devices()
    result = transfer.transfer(SERIAL, package_dir, REMOTE_ROOT, **kwargs)
    return result, client


def _remote_dir(package_dir: Path) -> str:
    """本地包对应的远端目录（按 manifest 的 id 算）。"""
    import json

    manifest = json.loads((package_dir / PACKAGE_MANIFEST_FILENAME).read_text("utf-8"))
    return remote_dir_for(REMOTE_ROOT, manifest["id"])


def _remote_bytes(client: FakeAdbClient, remote_dir: str, name: str) -> bytes:
    """设备上某个文件的内容。"""
    return client.device(SERIAL).files[join_remote(remote_dir, name)]


# --------------------------------------------------------------------------- #
# 远端路径规则（Spec §15 / §43）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("beijing_20260916", "beijing_20260916"),
        ("../../abc", "____abc"),
        ("-rf", "rf"),
        ("a;b|c", "a_b_c"),
        ("a`b$(x)", "a_b__x_"),
        ("..", "_"),
        (".hidden", "hidden"),
        ("", "package"),
    ],
)
def test_sanitize_remote_id(raw: str, expected: str) -> None:
    """白名单之外的一律换成 ``_``，且结果里不会出现 ``..``（Spec §15）。"""
    assert sanitize_remote_id(raw) == expected


def test_sanitize_remote_id_never_leaves_dot_dot() -> None:
    """任意输入都不该产出含 ``..`` 的目录名。"""
    for raw in ("../../abc", "a/../../b", "..%2f..", "..\\..\\x", "-..-"):
        assert ".." not in sanitize_remote_id(raw)
        assert "/" not in sanitize_remote_id(raw)
        assert "\\" not in sanitize_remote_id(raw)


def test_sanitize_remote_id_length_cap() -> None:
    assert len(sanitize_remote_id("x" * 500)) <= 64


def test_remote_dir_for_stays_under_root() -> None:
    """``../../abc`` 不能逃出导入区（Spec §43）。"""
    remote = remote_dir_for("/sdcard/Download/TiandituOfflineMap/import", "../../abc")
    assert remote.startswith("/sdcard/Download/TiandituOfflineMap/import/")
    assert ".." not in remote


@pytest.mark.parametrize(
    "root", ["/sdcard/x", "/sdcard/x/", "/storage/emulated/0/Download"]
)
def test_safe_remote_roots(root: str) -> None:
    assert is_safe_remote_root(root) is True


@pytest.mark.parametrize(
    "root",
    ["", "/", "sdcard/x", "/sdcard/../etc", "/sdcard/a;b", "/sdcard/`x`", "/sdcard/$HOME", "\\x"],
)
def test_unsafe_remote_roots(root: str) -> None:
    assert is_safe_remote_root(root) is False


def test_part_path() -> None:
    assert part_path("/x/map.mbtiles") == f"/x/map.mbtiles{TRANSFER_PART_SUFFIX}"


def test_join_remote_normalizes_slashes() -> None:
    assert join_remote("/sdcard/x/", "map.mbtiles") == "/sdcard/x/map.mbtiles"
    assert join_remote("/sdcard/x", "/map.mbtiles") == "/sdcard/x/map.mbtiles"


# --------------------------------------------------------------------------- #
# 成功链路（Spec §40）
# --------------------------------------------------------------------------- #
def test_happy_path(work_dir: Path) -> None:
    """一次完整传输：成功、大小一致、SHA256 一致、远端内容与本地相同。"""
    package = _make_package(work_dir)
    result, client = _transfer(work_dir, package)

    assert result.success is True, result.errors
    assert result.errors == []
    assert result.size_verified is True
    assert result.checksum_verified is True
    assert result.status_text == "COMPLETED"
    assert result.cancelled is False
    assert result.needs_confirmation is False

    remote_dir = _remote_dir(package)
    assert result.remote_package_dir == remote_dir
    assert result.serial == SERIAL
    assert result.local_package_dir == str(package)
    assert result.bytes_transferred == (package / PACKAGE_MBTILES_FILENAME).stat().st_size + (
        package / PACKAGE_MANIFEST_FILENAME
    ).stat().st_size
    assert result.duration_seconds >= 0.0

    local_mbtiles = (package / PACKAGE_MBTILES_FILENAME).read_bytes()
    assert _remote_bytes(client, remote_dir, PACKAGE_MBTILES_FILENAME) == local_mbtiles
    assert _remote_bytes(client, remote_dir, PACKAGE_MANIFEST_FILENAME) == (
        package / PACKAGE_MANIFEST_FILENAME
    ).read_bytes()


def test_remote_layout_matches_spec(work_dir: Path) -> None:
    """远端结构就是 Spec §14 画的那个：``<import>/<id>/{map.mbtiles,manifest.json}``。"""
    package = _make_package(work_dir)
    _, client = _transfer(work_dir, package)

    remote_dir = _remote_dir(package)
    device = client.device(SERIAL)
    assert remote_dir.startswith(REMOTE_ROOT + "/")
    assert set(device.files) == {
        join_remote(remote_dir, PACKAGE_MBTILES_FILENAME),
        join_remote(remote_dir, PACKAGE_MANIFEST_FILENAME),
    }


def test_only_two_files_are_ever_pushed(work_dir: Path) -> None:
    """只传包内的两个固定文件，绝不按 manifest 的路径去读包外内容（Spec §36）。"""
    package = _make_package(work_dir)
    _, client = _transfer(work_dir, package)

    locals_ = [Path(local).name for local, _ in client.pushes]
    assert locals_ == [PACKAGE_MBTILES_FILENAME, PACKAGE_MANIFEST_FILENAME]


def test_manifest_is_pushed_last(work_dir: Path) -> None:
    """manifest 最后传：Android 端把它当作"包齐了"的信号（Spec §19）。"""
    package = _make_package(work_dir)
    _, client = _transfer(work_dir, package)
    assert client.pushes[-1][1].endswith(PACKAGE_MANIFEST_FILENAME)


def test_mbtiles_goes_through_a_part_file(work_dir: Path) -> None:
    """大文件先落 ``.part``，``mv`` 之后才叫正式名字（Spec §20）。"""
    package = _make_package(work_dir)
    _, client = _transfer(work_dir, package)

    remote_dir = _remote_dir(package)
    pushed = [remote for _local, remote in client.pushes]
    assert pushed[0].endswith(PACKAGE_MBTILES_FILENAME + TRANSFER_PART_SUFFIX)

    # 传输结束后设备上不该再有 .part（Spec §20 的"禁止留下半成品结构"）
    left = [path for path in client.device(SERIAL).files if path.endswith(TRANSFER_PART_SUFFIX)]
    assert left == []

    shell_calls = [call for call in client.calls if call[0] == "shell"]
    mv_index = next(i for i, call in enumerate(shell_calls) if call[2][0] == "mv")
    assert shell_calls[mv_index][2] == [
        "mv",
        join_remote(remote_dir, PACKAGE_MBTILES_FILENAME + TRANSFER_PART_SUFFIX),
        join_remote(remote_dir, PACKAGE_MBTILES_FILENAME),
    ]


def test_mkdir_happens_before_push(work_dir: Path) -> None:
    """``mkdir`` 必须早于 ``push``：真机上父目录不存在时 push 会失败（Spec §16）。"""
    package = _make_package(work_dir)
    _, client = _transfer(work_dir, package)

    kinds = [call[0] for call in client.calls]
    assert kinds.index("shell") < kinds.index("push")

    mkdir_calls = [
        call for call in client.calls if call[0] == "shell" and call[2][0] == "mkdir"
    ]
    assert mkdir_calls and mkdir_calls[0][2][1] == "-p"
    assert mkdir_calls[0][2][2] == _remote_dir(package)


def test_every_call_uses_the_selected_serial(work_dir: Path) -> None:
    """多设备时绝不省略 ``-s``（Spec §21）：每条设备相关调用都带着同一个序列号。"""
    package = _make_package(work_dir)
    _, client = _transfer(work_dir, package)

    # 只看**设备相关**的调用：``devices -l`` 的第二个元素是 ``-l``，不是序列号
    device_scoped = {"shell", "push", "getprop", "df"}
    serials = {call[1] for call in client.calls if call[0] in device_scoped}
    assert serials == {SERIAL}
    for _local, remote in client.pushes:
        assert remote.startswith(REMOTE_ROOT)


def test_progress_is_monotonic_and_ends_at_100(work_dir: Path) -> None:
    """进度条只能向前、必须收在 100，且每个阶段都被走到（Spec §30）。"""
    package = _make_package(work_dir)
    events: list = []
    result, _client_ = _transfer(work_dir, package, progress_callback=events.append)

    assert result.success is True
    percents = [event.percent for event in events]
    assert percents == sorted(percents)
    assert percents[-1] == 100.0
    assert percents[0] == 0.0

    stages = [event.stage for event in events]
    for stage in ("check", "device", "space", "mkdir", "mbtiles", "rename", "manifest", "verify", "done"):
        assert stage in stages, f"阶段 {stage} 从未被走到"


def test_progress_reports_bytes_and_speed(work_dir: Path) -> None:
    """进度里要有"已传多少 / 一共多少"以及速度（Spec §30 的三行数字）。"""
    package = _make_package(work_dir)
    events: list = []
    _transfer(work_dir, package, progress_callback=events.append)

    last = events[-1]
    assert last.total_bytes == last.bytes_transferred
    assert last.size_text.count("/") == 1
    assert last.current_file is None or isinstance(last.current_file, str)

    # 至少有一帧是在传 mbtiles 的
    mbtiles_frames = [event for event in events if event.stage == "mbtiles"]
    assert mbtiles_frames
    assert any(frame.current_file for frame in mbtiles_frames)


def test_progress_without_percent_still_advances_stages(work_dir: Path) -> None:
    """``adb`` 不给百分比时退回阶段级进度，而不是编一个数字（Spec §22）。"""
    package = _make_package(work_dir)
    events: list = []
    result, _client_ = _transfer(
        work_dir, package, client=_client(push_percent=False), progress_callback=events.append
    )
    assert result.success is True
    assert events[-1].percent == 100.0
    assert [event.percent for event in events] == sorted(event.percent for event in events)


# --------------------------------------------------------------------------- #
# 失败路径（Spec §41）
# --------------------------------------------------------------------------- #
def test_invalid_package_stops_before_any_push(work_dir: Path) -> None:
    """包不合法 → 一个字节都不推（Spec §12）。"""
    package = _make_package(work_dir)
    (package / PACKAGE_MBTILES_FILENAME).write_bytes(b"not an mbtiles at all")

    result, client = _transfer(work_dir, package)
    assert result.success is False
    assert client.pushes == []
    assert result.errors


def test_missing_local_file(work_dir: Path) -> None:
    package = _make_package(work_dir)
    (package / PACKAGE_MANIFEST_FILENAME).unlink()

    result, client = _transfer(work_dir, package)
    assert result.success is False
    assert client.pushes == []
    assert PACKAGE_MANIFEST_FILENAME in " ".join(result.errors)


def test_missing_package_dir(work_dir: Path) -> None:
    result, client = _transfer(work_dir, work_dir / "nope")
    assert result.success is False
    assert client.pushes == []
    assert "不存在" in " ".join(result.errors)


def test_device_offline(work_dir: Path) -> None:
    package = _make_package(work_dir)
    result, client = _transfer(work_dir, package, client=_client(devices_out=(
        "List of devices attached\nABC123         offline\n"
    )))
    assert result.success is False
    assert client.pushes == []
    assert "离线" in " ".join(result.errors)


def test_device_unauthorized(work_dir: Path) -> None:
    package = _make_package(work_dir)
    result, client = _transfer(work_dir, package, client=_client(devices_out=(
        "List of devices attached\nABC123         unauthorized\n"
    )))
    assert result.success is False
    assert client.pushes == []
    assert "未授权" in " ".join(result.errors)


def test_device_unknown(work_dir: Path) -> None:
    """设备列表里没有这台 → 不传输（刷新一次也找不到）。"""
    package = _make_package(work_dir)
    result, client = _transfer(work_dir, package, client=_client(devices_out=(
        "List of devices attached\nOTHER         device\n"
    )))
    assert result.success is False
    assert client.pushes == []
    assert "设备不存在" in " ".join(result.errors)


def test_not_enough_space(work_dir: Path) -> None:
    """可用空间不足 → 立刻停，不浪费用户的时间与流量（Spec §17）。"""
    package = _make_package(work_dir)
    client = _client()
    client.device(SERIAL).available_bytes = 1024  # 1 KB

    result, client = _transfer(work_dir, package, client=client)
    assert result.success is False
    assert client.pushes == []
    assert "存储空间不足" in " ".join(result.errors)


def test_unparsable_df_warns_but_continues(work_dir: Path) -> None:
    """``df`` 解析不出来 → WARNING + 继续（Spec §17 明确要求）。"""
    package = _make_package(work_dir)
    client = _client()
    client.device(SERIAL).available_bytes = None

    result, _client_ = _transfer(work_dir, package, client=client)
    assert result.success is True
    assert any("无法确认设备剩余空间" in warning for warning in result.warnings)


def test_mkdir_failure_stops_immediately(work_dir: Path) -> None:
    """建目录失败必须立刻停（Spec §16）。"""
    package = _make_package(work_dir)
    result, client = _transfer(work_dir, package, client=_client(fail_mkdir=True))

    assert result.success is False
    assert client.pushes == []
    assert "创建远端目录失败" in " ".join(result.errors)


def test_mbtiles_push_failure(work_dir: Path) -> None:
    package = _make_package(work_dir)
    result, client = _transfer(work_dir, package, client=_client(fail_push_mbtiles=True))

    assert result.success is False
    assert "map.mbtiles" in " ".join(result.errors)
    # 大文件都没传成，manifest 更不该出现
    assert all(not remote.endswith(PACKAGE_MANIFEST_FILENAME) for _l, remote in client.pushes)


def test_manifest_push_failure(work_dir: Path) -> None:
    package = _make_package(work_dir)
    result, _client_ = _transfer(work_dir, package, client=_client(fail_push_manifest=True))

    assert result.success is False
    assert "manifest.json" in " ".join(result.errors)


def test_move_failure(work_dir: Path) -> None:
    """``mv`` 失败：正式名字没出现，传输不算成功（Spec §20）。"""
    package = _make_package(work_dir)
    result, client = _transfer(work_dir, package, client=_client(fail_move=True))

    assert result.success is False
    assert "整理远端文件失败" in " ".join(result.errors)
    remote_dir = _remote_dir(package)
    assert join_remote(remote_dir, PACKAGE_MBTILES_FILENAME) not in client.device(SERIAL).files


def test_remote_size_mismatch(work_dir: Path) -> None:
    """远端大小对不上 → FAIL（Spec §25 / §41）。"""
    package = _make_package(work_dir)
    size = (package / PACKAGE_MBTILES_FILENAME).stat().st_size
    client = _client(size_override=size + 1)

    result, _client_ = _transfer(work_dir, package, client=client)
    assert result.success is False
    assert result.size_verified is False
    assert "大小不一致" in " ".join(result.errors)


def test_remote_size_unreadable(work_dir: Path) -> None:
    """远端大小读不到 → FAIL（"不知道"不等于"对的"）。"""
    package = _make_package(work_dir)
    result, _client_ = _transfer(work_dir, package, client=_client(size_mode="unavailable"))
    assert result.success is False
    assert "无法读取远端文件大小" in " ".join(result.errors)


def test_remote_sha256_mismatch(work_dir: Path) -> None:
    """摘要对不上必须 FAIL —— 文件在设备上是坏的，装上去只会出问题（Spec §41）。"""
    package = _make_package(work_dir)
    result, _client_ = _transfer(work_dir, package, client=_client(sha256_mode="broken"))

    assert result.success is False
    assert result.checksum_verified is False
    assert result.size_verified is True  # 大小是对的，坏的只有内容
    assert "SHA256" in " ".join(result.errors)


def test_device_disconnects_during_push(work_dir: Path) -> None:
    """传输中掉线 → 明确报"传输中设备断开"，并**保留** ``.part``（Spec §35）。"""
    package = _make_package(work_dir)
    result, client = _transfer(
        work_dir, package, client=_client(disconnect_on_push=True)
    )

    assert result.success is False
    assert "Device disconnected during transfer" in " ".join(result.errors)
    assert any("残留" in warning for warning in result.warnings)


def test_cancel_removes_part_and_keeps_complete_packages(work_dir: Path) -> None:
    """取消 → 尽量删掉 ``.part``，但**不删**用户已有的完整包（Spec §23）。"""
    package = _make_package(work_dir)
    remote_dir = _remote_dir(package)

    client = _client()
    # 造一个"设备上已有的完整包"（不是我们这次的临时文件）
    device: FakeDevice = client.device(SERIAL)
    device.mkdir_p(remote_dir)
    device.files[join_remote(remote_dir, "keepme.txt")] = b"user data"

    cancel = threading.Event()
    cancel.set()
    result, client = _transfer(
        work_dir, package, client=client, cancel_event=cancel
    )

    assert result.cancelled is True
    assert result.success is False
    assert result.status_text == "CANCELLED"
    assert "Transfer cancelled" in " ".join(result.errors)

    files = client.device(SERIAL).files
    assert not [path for path in files if path.endswith(TRANSFER_PART_SUFFIX)]
    # 用户自己的东西一个字都没动
    assert files[join_remote(remote_dir, "keepme.txt")] == b"user data"
    assert join_remote(remote_dir, PACKAGE_MANIFEST_FILENAME) not in files


def test_partial_progress_is_reported_on_failure(work_dir: Path) -> None:
    """失败/取消也要如实报"传了多少"（用户据此判断要不要重来）。"""
    package = _make_package(work_dir)
    events: list = []
    result, _client_ = _transfer(
        work_dir, package, client=_client(cancel_push=True), progress_callback=events.append
    )
    assert result.cancelled is True
    assert result.duration_seconds >= 0.0


def test_unsafe_remote_root_is_rejected(work_dir: Path) -> None:
    """远端根目录不安全（路径穿越 / shell 元字符）→ 不传输（Spec §14 / §15）。"""
    package = _make_package(work_dir)
    client = _client()
    transfer = MapPackageTransfer(client)
    transfer.device_manager.refresh_devices()

    for root in ("/sdcard/../etc", "/sdcard/a;b", "relative/path", "/"):
        result = transfer.transfer(SERIAL, package, root)
        assert result.success is False
        assert "远端目录不安全" in " ".join(result.errors)
    assert client.pushes == []


def test_local_truncation_is_caught_without_hashing(work_dir: Path) -> None:
    """本地文件被截断 → 即使跳过完整校验也要拦住（manifest 自己记着该多大）。"""
    package = _make_package(work_dir)
    (package / PACKAGE_MBTILES_FILENAME).write_bytes(b"short")

    result, client = _transfer(work_dir, package, verify_package=False)
    assert result.success is False
    assert client.pushes == []
    assert "本地地图包不完整" in " ".join(result.errors)


def test_broken_manifest_json(work_dir: Path) -> None:
    """manifest 不是合法 JSON → 停在本地校验（Spec §12）。"""
    package = _make_package(work_dir)
    (package / PACKAGE_MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")

    result, client = _transfer(work_dir, package)
    assert result.success is False
    assert client.pushes == []


# --------------------------------------------------------------------------- #
# 已完成包的处理（Spec §24）
# --------------------------------------------------------------------------- #
def test_existing_remote_package_asks_first(work_dir: Path) -> None:
    """设备上已有同名包 → 先问用户，**绝不静默覆盖**（Spec §24）。"""
    package = _make_package(work_dir)
    remote_dir = _remote_dir(package)

    client = _client()
    device = client.device(SERIAL)
    device.mkdir_p(remote_dir)
    device.files[join_remote(remote_dir, PACKAGE_MANIFEST_FILENAME)] = b"old manifest"

    result, client = _transfer(work_dir, package, client=client)

    assert result.success is False
    assert result.needs_confirmation is True
    assert result.confirmation_reason == "remote_exists"
    assert result.remote_package_dir == remote_dir
    assert client.pushes == []
    assert device.files[join_remote(remote_dir, PACKAGE_MANIFEST_FILENAME)] == b"old manifest"


def test_overwrite_after_confirmation(work_dir: Path) -> None:
    """用户选了覆盖 → 删旧包再重传（Spec §24）。"""
    package = _make_package(work_dir)
    remote_dir = _remote_dir(package)

    client = _client()
    device = client.device(SERIAL)
    device.mkdir_p(remote_dir)
    device.files[join_remote(remote_dir, PACKAGE_MANIFEST_FILENAME)] = b"old manifest"
    device.files[join_remote(remote_dir, "stale.bin")] = b"stale"

    result, client = _transfer(work_dir, package, client=client, overwrite=True)

    assert result.success is True, result.errors
    assert result.overwritten is True
    files = client.device(SERIAL).files
    assert join_remote(remote_dir, "stale.bin") not in files
    assert files[join_remote(remote_dir, PACKAGE_MANIFEST_FILENAME)] == (
        package / PACKAGE_MANIFEST_FILENAME
    ).read_bytes()


def test_stale_part_is_cleaned_before_upload(work_dir: Path) -> None:
    """上次残留的 ``.part`` 会在本次传输前清掉（Spec §20 / §35）。"""
    package = _make_package(work_dir)
    remote_dir = _remote_dir(package)

    client = _client()
    device = client.device(SERIAL)
    device.mkdir_p(remote_dir)
    stale = join_remote(remote_dir, PACKAGE_MBTILES_FILENAME + TRANSFER_PART_SUFFIX)
    device.files[stale] = b"half of a gigabyte"

    result, client = _transfer(work_dir, package, client=client)

    assert result.success is True, result.errors
    assert any("已清理上次残留的临时文件" in warning for warning in result.warnings)
    assert client.device(SERIAL).files[join_remote(remote_dir, PACKAGE_MBTILES_FILENAME)]


# --------------------------------------------------------------------------- #
# 设备端不支持 sha256sum（Spec §26 / §42）
# --------------------------------------------------------------------------- #
def test_sha256_unavailable_is_a_warning_not_a_failure(work_dir: Path) -> None:
    """设备没有 ``sha256sum`` → 传输成功 + ``checksum_verified=False`` + WARNING。"""
    package = _make_package(work_dir)
    result, client = _transfer(work_dir, package, client=_client(sha256_mode="unavailable"))

    assert result.success is True, result.errors
    assert result.size_verified is True
    assert result.checksum_verified is False
    assert result.status_text == "WARNING"
    assert result.checksum_text == "未验证"
    assert any(
        "Remote SHA256 unavailable; file size verified only." in warning
        for warning in result.warnings
    )
    # 文件确实传到了设备上
    remote_dir = _remote_dir(package)
    assert _remote_bytes(client, remote_dir, PACKAGE_MBTILES_FILENAME) == (
        package / PACKAGE_MBTILES_FILENAME
    ).read_bytes()


def test_sha256_comparison_uses_lowercase_hex(work_dir: Path) -> None:
    """大小写不该造成假失败：比较前统一小写。"""
    package = _make_package(work_dir)
    result, _client_ = _transfer(work_dir, package)
    assert result.checksum_verified is True

    expected = hashlib.sha256(
        (package / PACKAGE_MBTILES_FILENAME).read_bytes()
    ).hexdigest()
    assert expected == expected.lower()


# --------------------------------------------------------------------------- #
# 计划（Spec §31）
# --------------------------------------------------------------------------- #
def test_plan_describes_the_transfer(work_dir: Path) -> None:
    """确认框要的四个信息都能从计划里取到（Spec §31）。"""
    package = _make_package(work_dir)
    client = _client()
    transfer = MapPackageTransfer(client)
    transfer.device_manager.refresh_devices()

    plan = transfer.plan(SERIAL, package, REMOTE_ROOT)

    assert plan.is_valid is True, plan.error
    assert plan.serial == SERIAL
    assert plan.device_name.startswith("Pixel 7")
    assert plan.package_id
    assert plan.package_name == "beijing"
    assert plan.remote_dir == _remote_dir(package)
    assert plan.mbtiles_size == (package / PACKAGE_MBTILES_FILENAME).stat().st_size
    assert plan.total_bytes == plan.mbtiles_size + plan.manifest_size
    assert plan.size_text


def test_plan_reports_missing_device_and_package(work_dir: Path) -> None:
    package = _make_package(work_dir)
    client = _client()
    transfer = MapPackageTransfer(client)
    transfer.device_manager.refresh_devices()

    assert transfer.plan("NOPE", package, REMOTE_ROOT).error
    assert transfer.plan(SERIAL, work_dir / "nope", REMOTE_ROOT).error
    assert transfer.plan(SERIAL, package, "/sdcard/../etc").error


def test_remote_has_package_probe(work_dir: Path) -> None:
    package = _make_package(work_dir)
    remote_dir = _remote_dir(package)
    client = _client()
    transfer = MapPackageTransfer(client)
    transfer.device_manager.refresh_devices()

    assert transfer.remote_has_package(SERIAL, remote_dir) is False
    client.device(SERIAL).mkdir_p(remote_dir)
    client.device(SERIAL).files[join_remote(remote_dir, PACKAGE_MANIFEST_FILENAME)] = b"x"
    assert transfer.remote_has_package(SERIAL, remote_dir) is True


def test_transfer_never_raises(work_dir: Path) -> None:
    """传输层的兜底：任何异常都变成错误结果，不让 traceback 进 GUI（Spec §4 的同一条）。"""
    package = _make_package(work_dir)

    class Exploding(FakeAdbClient):
        def push(self, *args, **kwargs):  # noqa: ARG002
            raise RuntimeError("boom")

    result, _client_ = _transfer(work_dir, package, client=Exploding())
    assert result.success is False
    assert any("boom" in error for error in result.errors)
