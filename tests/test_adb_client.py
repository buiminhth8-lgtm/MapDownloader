"""``AdbClient`` 的单元测试（Slice 10，Spec §3 / §4 / §38）。

全部通过 monkeypatch 打桩 ``subprocess``：本机装没装 adb、有没有插设备，
都不该影响这套用例的结论。真机验证在 ``scripts/verify_slice10_android.py``。

覆盖：无设备 / 多设备 / offline / unauthorized 的输出解析（在
``test_device_manager.py`` 里另有专门用例）、adb 不存在、超时、启动失败、
``shell=True`` 静态禁令、``-s`` 不省略、``push`` 的进度与取消、
``stat``/``wc`` 与三种 ``sha256sum`` 的兼容回退。
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import threading
from pathlib import Path

import pytest

import app.android.adb_client as adb_client_module
from app.android.adb_client import (
    ERROR_ADB_MISSING,
    ERROR_CANCELLED,
    ERROR_START_FAILED,
    ERROR_TIMEOUT,
    AdbClient,
    AdbPushProgress,
    parse_df_available_bytes,
    resolve_adb_path,
)
from app.config.settings import ADB_PATH_ENV_VAR


class FakeRun:
    """``subprocess.run`` 的替身：记录调用、按脚本返回。"""

    def __init__(
        self,
        *,
        stdout: bytes | str = b"",
        stderr: bytes | str = b"",
        returncode: int = 0,
        raise_exc: BaseException | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.raise_exc = raise_exc
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        if self.raise_exc is not None:
            raise self.raise_exc
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)

    @property
    def last_argv(self) -> list[str]:
        """最后一次调用的命令行（列表形式）。"""
        return self.calls[-1][0]

    @property
    def last_kwargs(self) -> dict:
        """最后一次调用的关键字参数。"""
        return self.calls[-1][1]


class FakeStream:
    """``BufferedReader`` 的最小替身：``read1()`` 每次吐一块。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read1(self, size: int = -1) -> bytes:  # noqa: ARG002 - 与真实签名一致
        return self._chunks.pop(0) if self._chunks else b""


class FakePopen:
    """``subprocess.Popen`` 的替身（``push`` 用它）。"""

    def __init__(
        self,
        argv,
        *,
        stdout_chunks: list[bytes] | None = None,
        stderr_chunks: list[bytes] | None = None,
        returncode: int = 0,
        never_finish: bool = False,
    ) -> None:
        self.argv = list(argv)
        self.stdout = FakeStream(stdout_chunks or [])
        self.stderr = FakeStream(stderr_chunks or [])
        self._final_returncode = returncode
        self._never_finish = never_finish
        self.returncode: int | None = None
        self.killed = False
        self.polls = 0

    def poll(self):
        self.polls += 1
        if not self._never_finish:
            self.returncode = self._final_returncode
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        return self.returncode if self.returncode is not None else 0


def _fake_adb(work_dir: Path) -> str:
    """造一个**真的存在**的假 adb 文件。

    ``AdbClient`` 在跑命令之前会检查可执行文件在不在（含分隔符时走
    ``Path.is_file()``）。用真实文件而不是打桩那个检查，顺带把这条逻辑也测了。
    """
    path = work_dir / "adb.exe"
    path.write_bytes(b"fake")
    return str(path)


@pytest.fixture()
def adb(work_dir: Path) -> str:
    """一个存在的假 adb 路径。"""
    return _fake_adb(work_dir)


# --------------------------------------------------------------------------- #
# 路径解析（Spec §2）
# --------------------------------------------------------------------------- #
def test_resolve_adb_path_priority(monkeypatch) -> None:
    """用户配置 > 环境变量 > 默认 ``"adb"``。"""
    monkeypatch.delenv(ADB_PATH_ENV_VAR, raising=False)
    assert resolve_adb_path() == "adb"
    assert resolve_adb_path("") == "adb"
    assert resolve_adb_path("   ") == "adb"

    monkeypatch.setenv(ADB_PATH_ENV_VAR, "C:/env/adb.exe")
    assert resolve_adb_path() == "C:/env/adb.exe"
    # 用户配置压过环境变量
    assert resolve_adb_path("D:/user/adb.exe") == "D:/user/adb.exe"


def test_resolve_adb_path_never_uses_developer_path(monkeypatch) -> None:
    """默认值里不含任何开发者机器的绝对路径（Spec §2）。"""
    monkeypatch.delenv(ADB_PATH_ENV_VAR, raising=False)
    default = resolve_adb_path()
    assert ":" not in default and "\\" not in default


# --------------------------------------------------------------------------- #
# version / 找不到 adb（Spec §10）
# --------------------------------------------------------------------------- #
def test_version_parses_and_caches(monkeypatch, adb: str) -> None:
    fake = FakeRun(stdout=b"Android Debug Bridge version 1.0.41\nRevision abc\n")
    monkeypatch.setattr(subprocess, "run", fake)

    client = AdbClient(adb)
    result = client.version()

    assert result.success is True
    assert result.return_code == 0
    assert client.version_text == "1.0.41"
    assert isinstance(result.duration_ms, float)


def test_missing_adb_is_a_result_not_an_exception(monkeypatch, work_dir: Path) -> None:
    """adb 不存在 → 清晰的失败结果，绝不是 traceback（Spec §4 / §10）。"""
    called = FakeRun()
    monkeypatch.setattr(subprocess, "run", called)

    client = AdbClient(str(work_dir / "nope" / "adb.exe"))
    assert client.is_available() is False

    result = client.version()
    assert result.success is False
    assert result.return_code is None
    assert result.error_code == ERROR_ADB_MISSING
    assert "adb" in (result.error or "").lower()
    # 关键：根本没去起进程
    assert called.calls == []


def test_start_failure_is_reported(monkeypatch, adb: str) -> None:
    monkeypatch.setattr(subprocess, "run", FakeRun(raise_exc=OSError("拒绝访问")))
    result = AdbClient(adb).version()
    assert result.success is False
    assert result.error_code == ERROR_START_FAILED
    assert "拒绝访问" in (result.error or "")


def test_timeout_is_reported(monkeypatch, adb: str) -> None:
    def raise_timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    result = AdbClient(adb).list_devices()
    assert result.success is False
    assert result.error_code == ERROR_TIMEOUT
    assert "超时" in (result.error or "")


def test_adb_missing_in_every_command(monkeypatch, work_dir: Path) -> None:
    """任何命令在 adb 缺失时都是同一种失败，且都不抛异常。"""
    client = AdbClient(str(work_dir / "missing-adb"))
    calls = [
        client.version(),
        client.list_devices(),
        client.shell("ABC", ["ls"]),
    ]
    for result in calls:
        assert result.success is False
        assert result.error_code == ERROR_ADB_MISSING
    # 读类型接口在失败时给 None / False 而不是炸
    assert client.file_size("ABC", "/x") is None
    assert client.remote_sha256("ABC", "/x") is None
    assert client.available_space("ABC", "/x") is None
    assert client.exists("ABC", "/x") is False


# --------------------------------------------------------------------------- #
# 命令构造（Spec §3 / §21）
# --------------------------------------------------------------------------- #
def test_commands_are_lists_with_dash_s(monkeypatch, adb: str) -> None:
    """每条设备相关的命令都带 ``-s SERIAL``，且命令行是**列表**（Spec §3 / §21）。"""
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    client = AdbClient(adb)

    client.list_devices()
    client.getprop("ABC123", "ro.product.model")
    client.mkdir("ABC123", "/sdcard/x")
    client.file_size("ABC123", "/sdcard/x/map.mbtiles")

    for argv, kwargs in fake.calls:
        assert isinstance(argv, list)
        assert all(isinstance(item, str) for item in argv)
        assert "shell" not in kwargs  # 绝不允许 shell=True
        assert kwargs.get("capture_output") is True

    # devices -l 不带 -s（它列的是全部设备）
    assert fake.calls[0][0][1:] == ["devices", "-l"]
    assert fake.calls[1][0][1:4] == ["-s", "ABC123", "shell"]
    assert fake.calls[2][0][1:3] == ["-s", "ABC123"]


def test_shell_requires_a_serial(adb: str) -> None:
    """省略 ``-s`` 是硬错误：多设备时 adb 会报 "more than one device"（Spec §21）。"""
    client = AdbClient(adb)
    with pytest.raises(ValueError):
        client.shell("", ["ls"])
    with pytest.raises(ValueError):
        client.push("", "a", "/b")


def test_no_shell_true_in_source() -> None:
    """静态守卫：模块里没有 ``shell=True``、也没有把命令拼成字符串去执行（Spec §3）。

    用 ``ast`` 而不是字符串匹配：这个模块的**文档字符串里**恰好写着
    "禁止 ``shell=True``"，纯字符串匹配必然把注释当成违规（Slice 9 踩过）。
    """
    source = Path(inspect.getfile(adb_client_module)).read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            assert keyword.arg != "shell" or not (
                isinstance(keyword.value, ast.Constant) and keyword.value.value is True
            ), "禁止 shell=True"
        # subprocess.run / Popen 的第一个位置参数不许是字符串字面量
        func = node.func
        name = getattr(func, "attr", None)
        if name in {"run", "Popen", "call", "check_output"} and node.args:
            first = node.args[0]
            assert not isinstance(first, ast.Constant), (
                f"{name}() 的参数必须是列表，不是字符串"
            )
            assert not isinstance(first, ast.JoinedStr), (
                f"{name}() 的参数不能是 f-string（等于把命令拼成字符串）"
            )


# --------------------------------------------------------------------------- #
# 远端大小 / SHA256 / df 的兼容回退（Spec §25 / §26 / §17）
# --------------------------------------------------------------------------- #
def test_file_size_falls_back_to_wc(monkeypatch, adb: str) -> None:
    """``stat -c %s`` 不支持时退到 ``wc -c``（toybox 的真实行为，Spec §25）。"""
    argv_seen: list[list[str]] = []

    def run(argv, **kwargs):  # noqa: ARG001
        argv_seen.append(list(argv))
        if "stat" in argv:
            # toybox 的 stat 不认 -c
            return subprocess.CompletedProcess(argv, 1, b"", b"stat: Unknown option")
        return subprocess.CompletedProcess(argv, 0, b"  12345 /x/map.mbtiles\n", b"")

    monkeypatch.setattr(subprocess, "run", run)
    assert AdbClient(adb).file_size("ABC", "/x/map.mbtiles") == 12345
    assert any("wc" in argv for argv in argv_seen), "应当回退到 wc -c"


def test_file_size_returns_none_when_unreadable(monkeypatch, adb: str) -> None:
    """两种方式都读不到 → ``None``（上层据此报"无法读取远端大小"）。"""
    monkeypatch.setattr(
        subprocess, "run", FakeRun(returncode=1, stderr=b"stat: No such file")
    )
    assert AdbClient(adb).file_size("ABC", "/x/map.mbtiles") is None


def test_sha256_tries_all_candidates(monkeypatch, adb: str) -> None:
    """``sha256sum`` 不在时试 ``toybox sha256sum``（Spec §26）。"""
    calls: list[list[str]] = []

    def run(argv, **kwargs):  # noqa: ARG001
        calls.append(list(argv))
        if argv[-2:] == ["sha256sum", "/x/map.mbtiles"] and "toybox" not in argv:
            return subprocess.CompletedProcess(argv, 1, b"", b"sha256sum: not found")
        return subprocess.CompletedProcess(argv, 0, b"a" * 64 + b"  /x/map.mbtiles\n", b"")

    monkeypatch.setattr(subprocess, "run", run)
    digest = AdbClient(adb).remote_sha256("ABC", "/x/map.mbtiles")

    assert digest == "a" * 64
    assert any("toybox" in argv for argv in calls)


def test_sha256_unavailable_returns_none(monkeypatch, adb: str) -> None:
    """一个候选都没有 → ``None``（不是失败，Spec §26 / §33）。"""
    monkeypatch.setattr(
        subprocess, "run", FakeRun(returncode=127, stderr=b"not found")
    )
    assert AdbClient(adb).remote_sha256("ABC", "/x") is None


def test_df_parsing_variants() -> None:
    """``df`` 的两套表头都要能解析；解析不出来返回 ``None``（Spec §17）。"""
    generic = (
        "Filesystem     1K-blocks     Used Available Use% Mounted on\n"
        "/dev/fuse      117311488  7234112 110077376   7% /storage/emulated\n"
    )
    assert parse_df_available_bytes(generic) == 110077376 * 1024

    short = (
        "Filesystem 1K-blocks Used Avail Use% Mounted on\n"
        "/dev/fuse 100 20 80 20% /sdcard\n"
    )
    assert parse_df_available_bytes(short) == 80 * 1024

    # 中文 / 异常输出 → 认不出来就返回 None，由上层给 WARNING 并继续
    assert parse_df_available_bytes("df: /sdcard: Permission denied") is None
    assert parse_df_available_bytes("") is None


# --------------------------------------------------------------------------- #
# push：进度、取消、失败（Spec §21 ~ §23）
# --------------------------------------------------------------------------- #
def _patch_popen(monkeypatch, popen: FakePopen):
    """把 ``subprocess.Popen`` 换成给定的假进程，并把**真实** argv 记进它。

    记真实 argv 才能断言"命令里确实带了 ``-s SERIAL``"——这正是 Spec §21
    （多设备时不许省略 ``-s``）要守的那条。
    """

    def factory(argv, **kwargs):  # noqa: ARG001
        popen.argv = list(argv)
        return popen

    monkeypatch.setattr(subprocess, "Popen", factory)
    return popen


def test_push_reports_percent_progress(monkeypatch, adb: str, work_dir: Path) -> None:
    """``adb push`` 的 ``[ NN%]`` 输出要被翻译成进度（Spec §22）。"""
    source = work_dir / "map.mbtiles"
    source.write_bytes(b"x" * 1000)
    popen = FakePopen(
        [],
        stdout_chunks=[b"[  0%] /sdcard/x\n", b"[ 55%] /sdcard/x\n", b"[100%] /sdcard/x\n"],
    )
    _patch_popen(monkeypatch, popen)

    seen: list[AdbPushProgress] = []
    result = AdbClient(adb).push(
        "ABC123", source, "/sdcard/x", progress_callback=seen.append
    )

    assert result.success is True
    assert result.return_code == 0
    assert [event.percent for event in seen] == [0.0, 55.0, 100.0]
    assert seen[-1].bytes_transferred == 1000
    assert popen.argv[1:5] == ["-s", "ABC123", "push", str(source)]


def test_push_percent_split_across_chunks(monkeypatch, adb: str, work_dir: Path) -> None:
    """百分比可能被切在两个读块之间——这正是要跨块解析的原因（Spec §22）。"""
    source = work_dir / "map.mbtiles"
    source.write_bytes(b"x" * 100)
    popen = FakePopen([], stdout_chunks=[b"progress [  4", b"2%] /x\n"])
    _patch_popen(monkeypatch, popen)

    seen: list[AdbPushProgress] = []
    AdbClient(adb).push("ABC", source, "/x", progress_callback=seen.append)
    assert seen and seen[-1].percent == 42.0


def test_push_without_percent_gives_none(monkeypatch, adb: str, work_dir: Path) -> None:
    """有的 adb 不给百分比：回调收到 ``percent=None``，由上层退回阶段进度。"""
    source = work_dir / "map.mbtiles"
    source.write_bytes(b"x" * 10)
    _patch_popen(monkeypatch, FakePopen([], stdout_chunks=[b"sending...\n"]))

    seen: list[AdbPushProgress] = []
    AdbClient(adb).push("ABC", source, "/x", progress_callback=seen.append)
    assert seen and all(event.percent is None for event in seen)


def test_push_cancel_kills_the_process(monkeypatch, adb: str, work_dir: Path) -> None:
    """取消必须真的杀掉子进程，并给出 ``cancelled=True``（Spec §23）。"""
    source = work_dir / "map.mbtiles"
    source.write_bytes(b"x" * 10)
    popen = FakePopen([], never_finish=True)
    _patch_popen(monkeypatch, popen)

    cancel = threading.Event()
    cancel.set()
    result = AdbClient(adb).push("ABC", source, "/x", cancel_event=cancel)

    assert result.cancelled is True
    assert result.success is False
    assert result.error_code == ERROR_CANCELLED
    assert popen.killed is True


def test_push_timeout_kills_the_process(monkeypatch, adb: str, work_dir: Path) -> None:
    """超时同样要杀进程，并报 ERROR_TIMEOUT。"""
    source = work_dir / "map.mbtiles"
    source.write_bytes(b"x" * 10)
    popen = FakePopen([], never_finish=True)
    _patch_popen(monkeypatch, popen)

    result = AdbClient(adb).push("ABC", source, "/x", timeout=0.0)
    assert result.success is False
    assert result.error_code == ERROR_TIMEOUT
    assert popen.killed is True


def test_push_failure_carries_stderr(monkeypatch, adb: str, work_dir: Path) -> None:
    """push 失败要把 adb 的原话带回来（排查全靠它）。"""
    source = work_dir / "map.mbtiles"
    source.write_bytes(b"x" * 10)
    _patch_popen(
        monkeypatch,
        FakePopen(
            [],
            stderr_chunks=[b"adb: error: failed to copy: No space left on device\n"],
            returncode=1,
        ),
    )
    result = AdbClient(adb).push("ABC", source, "/x")
    assert result.success is False
    assert "No space left" in (result.error or "")


def test_push_disconnect_detection(monkeypatch, adb: str, work_dir: Path) -> None:
    """设备掉线会被认出来（Spec §35：与"文件写坏了"必须分开报）。"""
    source = work_dir / "map.mbtiles"
    source.write_bytes(b"x" * 10)
    _patch_popen(
        monkeypatch,
        FakePopen([], stderr_chunks=[b"adb: device offline\n"], returncode=1),
    )
    result = AdbClient(adb).push("ABC", source, "/x")
    assert result.success is False
    assert result.error_code == "device_disconnected"
    assert result.looks_disconnected() is True


def test_stderr_is_drained(monkeypatch, adb: str, work_dir: Path) -> None:
    """stderr 也必须被读干——管道写满会让子进程永久阻塞（"push 卡死"的真因）。"""
    source = work_dir / "map.mbtiles"
    source.write_bytes(b"x" * 10)
    big = [b"warning: %d\n" % index for index in range(500)]
    popen = FakePopen([], stderr_chunks=big)
    _patch_popen(monkeypatch, popen)

    result = AdbClient(adb).push("ABC", source, "/x")
    assert result.success is True
    assert result.stderr.count("warning") == 500


def test_set_adb_path_clears_probe_cache(monkeypatch, adb: str, work_dir: Path) -> None:
    """换了 adb 之后可用性要重新探测，不能沿用旧结论。"""
    client = AdbClient(adb)
    assert client.is_available() is True
    client.set_adb_path(str(work_dir / "missing"))
    assert client.is_available() is False
    assert client.adb_path == str(work_dir / "missing")
