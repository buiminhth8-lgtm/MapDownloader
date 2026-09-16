"""ADB 客户端：把 ``adb`` 的进程调用收在一个地方（Slice 10，Spec §2 ~ §10）。

为什么要有这一层
----------------

界面、传输层、设备管理都要调 adb。如果各自写一遍 ``subprocess.run``：

1. 出错处理会不一致——某处漏接 ``FileNotFoundError``，用户就看到 traceback；
2. ``shell=True`` 会不知不觉溜进来（Spec §3 明令禁止）；
3. 超时值会被复制成三份，改一处漏两处。

因此**全项目的 adb 调用只走本模块**，公开方法一律返回
:class:`AdbCommandResult`，绝不向调用方抛异常——"adb 没装 / 设备掉线 /
命令超时"都是**预期内的结果**，不是异常（Spec §4）。

安全约束（Spec §3 / §37）
------------------------

- 只允许 ``subprocess.run([...])`` / ``subprocess.Popen([...])``：完整命令行
  以**列表**传入，操作系统直接 ``exec``，没有 shell 参与解析，因此
  ``;`` ``&&`` ``|`` ``$()`` 都只是普通字符；
- 禁止 ``shell=True``，禁止把命令拼成一个字符串再执行；
- 不执行 ``adb root`` / ``adb remount`` / ``adb install`` / ``run-as``：
  本 Slice 只做标准文件传输（Spec §37）。这些命令在整个模块里**没有入口**。

``adb shell`` 的说明
--------------------

``adb shell <args...>`` 在**设备端**必然经过设备的 ``sh``——这是 ADB 协议的
行为，不是我们的选择，也无法避免。本模块能保证且必须保证的是**本地**不经过
shell：``["adb", "-s", "SER", "shell", "mkdir", "-p", "/sdcard/x"]`` 里的每个
元素都是独立 argv，设备端拿到的是已经分开的单词，不会被本地 shell 二次解析。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.config.settings import (
    ADB_DEVICES_TIMEOUT_SECONDS,
    ADB_DF_TIMEOUT_SECONDS,
    ADB_DISCONNECT_HINTS,
    ADB_FILE_SIZE_TIMEOUT_SECONDS,
    ADB_PATH_ENV_VAR,
    ADB_PUSH_POLL_INTERVAL_SECONDS,
    ADB_REMOTE_DELETE_TIMEOUT_SECONDS,
    ADB_SHA256_COMMAND_CANDIDATES,
    ADB_SHA256_HEX_LENGTH,
    ADB_SHA256_TIMEOUT_SECONDS,
    ADB_SHELL_TIMEOUT_SECONDS,
    ADB_SIZE_COMMAND_CANDIDATES,
    ADB_VERSION_TIMEOUT_SECONDS,
    DEFAULT_ADB_EXECUTABLE,
    LOG_ADB_DETECTED,
    LOG_ADB_MISSING,
)

__all__ = [
    "AdbClient",
    "AdbCommandResult",
    "AdbPushProgress",
    "PushCallback",
    "resolve_adb_path",
]

_logger = logging.getLogger(__name__)

#: 错误分类（Spec §4：ADB 不存在 / timeout / 启动失败 / 设备离线要能被区分开）。
#: 界面据此选文案，而不是去猜 ``error`` 字符串里有没有某个词。
ERROR_ADB_MISSING: str = "adb_missing"
ERROR_TIMEOUT: str = "timeout"
ERROR_START_FAILED: str = "start_failed"
ERROR_DEVICE_DISCONNECTED: str = "device_disconnected"
ERROR_CANCELLED: str = "cancelled"

#: ``adb push`` 的百分比：``[ 45%] /sdcard/x``。
#: 不同版本的 adb 也输出 ``[ 45%] 1234/5678``（``-p`` 模式），
#: 因此只认方括号里的数字，后面的路径/计数一概不管。
_PERCENT_RE = re.compile(rb"\[\s*(\d+)\s*%\]")

#: ``sha256sum`` 的输出：``<64 位十六进制>  <文件名>``
_SHA256_RE = re.compile(rf"\b([0-9a-fA-F]{{{ADB_SHA256_HEX_LENGTH}}})\b")

#: 判定"这个字符串看起来像本地路径"：有分隔符就当显式路径处理。
_PATH_SEPARATORS = ("/", "\\")

#: 读子进程输出的块大小（1 KiB 足够频繁地刷新进度，又不会被 syscall 拖慢）
_READ_CHUNK = 1024


def resolve_adb_path(configured: str | None = None) -> str:
    """按 Spec §2 的优先级解析 ``adb`` 可执行文件。

    1. 用户配置（``configured``，来自界面偏好）；
    2. 环境变量 ``ADB_PATH``；
    3. ``"adb"``（交给系统 ``PATH`` 去找）。

    Args:
        configured: 用户显式配置的路径。空串 / ``None`` 表示没配。

    Returns:
        可执行文件名或绝对路径。**不校验它是否存在**——"配了但配错了"与
        "没配"要给出不同的提示，校验放在 :meth:`AdbClient.is_available`。
    """
    explicit = str(configured or "").strip()
    if explicit:
        return explicit
    from_env = str(os.environ.get(ADB_PATH_ENV_VAR, "") or "").strip()
    if from_env:
        return from_env
    return DEFAULT_ADB_EXECUTABLE


@dataclass
class AdbCommandResult:
    """一次 adb 调用的结果（Spec §4）。

    所有失败路径都会填 :attr:`error`，因此调用方**永远不需要** ``try``：
    adb 不存在、超时、启动失败、设备离线，都是这个对象的一种取值。

    Attributes:
        success: 命令是否成功（``return_code == 0`` 且没被中断）。
        return_code: 进程退出码；进程根本没起来时为 ``None``。
        stdout: 标准输出（已按 UTF-8 解码，非法字节替换而不是抛异常）。
        stderr: 标准错误。
        duration_ms: 墙钟耗时（毫秒）。用来回答"慢在哪一步"。
        error: 失败原因（人话）。成功时为 ``None``。
        error_code: 机器可读的失败分类（见模块级 ``ERROR_*``）。界面按它选文案，
            不去猜 :attr:`error` 里有没有某个词。
        cancelled: 是否因用户取消而终止。取消**不算**失败原因以外的特殊状态，
            但调用方需要把"用户点的"与"设备坏了"分开报（Spec §23）。
    """

    success: bool
    return_code: int | None
    stdout: str
    stderr: str
    duration_ms: float
    error: str | None = None
    error_code: str | None = None
    cancelled: bool = False

    @property
    def output(self) -> str:
        """``stdout`` 与 ``stderr`` 的合并文本（诊断用）。"""
        return "\n".join(part for part in (self.stdout, self.stderr) if part)

    def looks_disconnected(self) -> bool:
        """stderr/stdout 里有没有"设备掉了"的特征（Spec §35）。"""
        text = self.output.lower()
        return any(hint in text for hint in ADB_DISCONNECT_HINTS)


@dataclass(frozen=True)
class AdbPushProgress:
    """``adb push`` 的一次进度观测。

    :attr:`percent` 为 ``None`` 表示**这个 adb 版本没给百分比**（Spec §22 明确
    承认这种可能）。此时调用方应当退回"阶段级进度"，而不是自己猜一个数字——
    猜出来的进度条会在最后一个字节上停留很久然后突然跳满。
    """

    percent: float | None
    bytes_transferred: int
    total_bytes: int


#: ``push`` 的进度回调签名
PushCallback = Callable[[AdbPushProgress], None]


class AdbClient:
    """``adb`` 的进程封装（Spec §3）。

    无状态：同一个实例可以并发服务多台设备（虽然本 Slice 同一时刻只传一个包）。
    所有方法都不抛异常。
    """

    def __init__(
        self,
        adb_path: str | None = None,
        *,
        logger: logging.Logger | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """
        Args:
            adb_path: 显式指定的 adb；``None`` 时走 :func:`resolve_adb_path`。
            logger: 可选日志器。
            env: 子进程环境变量；``None`` 时继承当前进程。
        """
        self._adb = resolve_adb_path(adb_path)
        self._logger = logger or _logger
        #: 最后一次"探测"的结果缓存（``None`` = 还没探测过）
        self._version_cache: str | None = None
        self._available_cache: bool | None = None
        self._env = env

    # ------------------------------------------------------------------ #
    # 只读状态
    # ------------------------------------------------------------------ #
    @property
    def adb_path(self) -> str:
        """当前使用的 adb（文件名或绝对路径）。"""
        return self._adb

    def set_adb_path(self, path: str | None) -> None:
        """换一个 adb 并清掉探测缓存（用户改了配置之后调用）。"""
        self._adb = resolve_adb_path(path)
        self._version_cache = None
        self._available_cache = None

    def is_available(self) -> bool:
        """``adb`` 是否**能找到**（Spec §10）。

        命中缓存：探测结果在一次界面刷新里会被问很多次（每台设备一次），
        每次都去 ``shutil.which`` 是浪费。换路径时由 :meth:`set_adb_path` 清缓存。
        """
        if self._available_cache is None:
            self._available_cache = _executable_exists(self._adb)
        return self._available_cache

    def version(self) -> AdbCommandResult:
        """``adb version``（Spec §10 的探测命令）。

        失败时 ``error_code`` 是 :data:`ERROR_ADB_MISSING`（找不到可执行文件）
        或 :data:`ERROR_START_FAILED`（找到了但起不来，例如权限不对）。
        """
        result = self._run(["version"], timeout=ADB_VERSION_TIMEOUT_SECONDS)
        if result.success:
            # ``Android Debug Bridge version 1.0.41`` → 取冒号后面那段
            first = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
            self._version_cache = first.split("version", 1)[-1].strip(" :") or first
            self._logger.info(LOG_ADB_DETECTED.format(path=self._adb, version=self._version_cache))
        else:
            self._logger.warning(
                LOG_ADB_MISSING.format(path=self._adb, error=result.error or "unknown")
            )
        return result

    @property
    def version_text(self) -> str | None:
        """上次 :meth:`version` 解析出的版本号；没探测过则 ``None``。"""
        return self._version_cache

    # ------------------------------------------------------------------ #
    # 设备
    # ------------------------------------------------------------------ #
    def list_devices(self) -> AdbCommandResult:
        """``adb devices -l``（Spec §6）。

        返回**原始结果**而不是解析后的列表：解析规则属于
        :mod:`app.android.device_manager`，这样"adb 输出格式变了"只影响一处，
        而且测试可以拿真实输出串直接喂解析函数。
        """
        return self._run(["devices", "-l"], timeout=ADB_DEVICES_TIMEOUT_SECONDS)

    # ------------------------------------------------------------------ #
    # 通用
    # ------------------------------------------------------------------ #
    def shell(
        self,
        serial: str,
        args: Sequence[str],
        timeout: float = ADB_SHELL_TIMEOUT_SECONDS,
    ) -> AdbCommandResult:
        """``adb -s SERIAL shell <args...>``。

        Args:
            serial: 目标设备。**必填**：Spec §21 要求多设备在线时绝不省略 ``-s``。
            args: 设备端命令的各个参数（已经是分开的列表，不是一整条字符串）。
            timeout: 超时（秒）。
        """
        if not serial:
            raise ValueError("shell() 必须指定 serial（Spec §21：不允许省略 -s）")
        return self._run(
            ["-s", str(serial), "shell", *[str(a) for a in args]], timeout=timeout
        )

    def getprop(self, serial: str, prop: str) -> AdbCommandResult:
        """``adb -s SERIAL shell getprop <prop>``（Spec §7）。"""
        return self.shell(serial, ["getprop", str(prop)])

    # ------------------------------------------------------------------ #
    # 远端文件操作（兼容逻辑全部收在这里，Spec §25 / §26）
    # ------------------------------------------------------------------ #
    def mkdir(self, serial: str, remote_path: str) -> AdbCommandResult:
        """``mkdir -p``（Spec §16）。失败必须被调用方看见——不能继续 push。"""
        return self.shell(serial, ["mkdir", "-p", str(remote_path)])

    def remove(self, serial: str, remote_path: str) -> AdbCommandResult:
        """``rm -rf``（Spec §24 覆盖时删旧包、Spec §23 取消时删 ``.part``）。

        只由传输层在**明确的**场景下调用：覆盖前删目标包目录、取消后删临时文件。
        绝不删除用户已经在设备上的其它内容。
        """
        return self.shell(
            serial,
            ["rm", "-rf", str(remote_path)],
            timeout=ADB_REMOTE_DELETE_TIMEOUT_SECONDS,
        )

    def move(self, serial: str, source: str, target: str) -> AdbCommandResult:
        """``mv``（Spec §20：``map.mbtiles.part`` → ``map.mbtiles``）。"""
        return self.shell(serial, ["mv", str(source), str(target)])

    def exists(self, serial: str, remote_path: str) -> bool:
        """远端路径是否存在（文件或目录都可以）。

        用 ``ls -d`` 而不是 shell 内置的 ``[ -e ]``：``[`` 是 shell 关键字，
        不同 Android 版本的 toybox 对它支持不一，而 ``ls`` 一定在。
        """
        result = self.shell(serial, ["ls", "-d", str(remote_path)])
        return result.success

    def file_size(self, serial: str, remote_path: str) -> int | None:
        """远端文件的字节数；读不到返回 ``None``（Spec §25）。

        依次尝试 ``stat -c %s`` 与 ``wc -c``：toybox 的 ``stat`` 不一定支持
        ``-c``（会回一句 ``stat: Unknown option``），而 ``wc -c`` 几乎总能work。
        **兼容逻辑封在这里**，界面与传输层只看到一个整数或 ``None``。
        """
        for candidate in ADB_SIZE_COMMAND_CANDIDATES:
            result = self.shell(
                serial,
                [*candidate, str(remote_path)],
                timeout=ADB_FILE_SIZE_TIMEOUT_SECONDS,
            )
            if not result.success:
                continue
            size = _parse_leading_int(result.stdout)
            if size is not None:
                return size
        return None

    def remote_sha256(self, serial: str, remote_path: str) -> str | None:
        """远端文件的 SHA256，取不到返回 ``None``（Spec §26，best-effort）。

        依次尝试 ``sha256sum`` / ``toybox sha256sum`` / ``busybox sha256sum``。
        Android 上可能一个都没有——**那是正常情况**，调用方据此把
        ``checksum_verified`` 置 ``False`` 并给 WARNING，而**不是**判失败。
        """
        for candidate in ADB_SHA256_COMMAND_CANDIDATES:
            result = self.shell(
                serial,
                [*candidate, str(remote_path)],
                timeout=ADB_SHA256_TIMEOUT_SECONDS,
            )
            if not result.success:
                continue
            digest = _parse_sha256(result.stdout)
            if digest is not None:
                return digest
        return None

    def available_space(self, serial: str, remote_path: str) -> int | None:
        """远端挂载点的可用字节数；解析不出来返回 ``None``（Spec §17）。

        ``None`` 的含义是"**不知道**"，不是"没有空间"：传输层据此给一句
        WARNING 然后继续，而不是拦下用户。
        """
        for argv in (["df", "-k", str(remote_path)], ["df", str(remote_path)]):
            result = self.shell(serial, argv, timeout=ADB_DF_TIMEOUT_SECONDS)
            if not result.success:
                continue
            available = parse_df_available_bytes(result.stdout)
            if available is not None:
                return available
        return None

    # ------------------------------------------------------------------ #
    # push（唯一一个需要流式读输出的命令）
    # ------------------------------------------------------------------ #
    def push(
        self,
        serial: str,
        local_path: str | Path,
        remote_path: str,
        *,
        timeout: float | None = None,
        progress_callback: PushCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> AdbCommandResult:
        """``adb -s SERIAL push LOCAL REMOTE``（Spec §21 / §22 / §23）。

        用 ``Popen`` 而不是 ``run``：要把进度回调**边传边发**给界面。百分比从
        adb 自己的输出里读（Spec §22 明确禁止为了进度去重新实现 ADB 协议）；
        读不到百分比时回调收到 ``percent=None``，由上层退回阶段级进度。

        Args:
            serial: 目标设备。**必须复用用户选中的那台**，绝不省略 ``-s``。
            local_path: 本地文件。
            remote_path: 远端目标路径。
            timeout: 整体超时（秒）；``None`` 表示按本地文件大小估算一个。
            progress_callback: 进度回调（在**读输出的线程**里被调用）。
            cancel_event: 用户取消信号；置位后立刻终止子进程（Spec §23）。

        Returns:
            :class:`AdbCommandResult`。被取消时 ``cancelled=True``，
            ``error_code`` 是 :data:`ERROR_CANCELLED`。
        """
        if not serial:
            raise ValueError("push() 必须指定 serial（Spec §21：不允许省略 -s）")

        local = Path(local_path)
        total = _safe_size(local)
        if timeout is None:
            timeout = _push_timeout_seconds(total)

        argv = ["-s", str(serial), "push", str(local), str(remote_path)]
        started = time.monotonic()

        # 起不来的情况（adb 没了 / 路径不对）在 _popen 里就被转换成结果
        popen, failure = self._popen(argv)
        if failure is not None:
            return failure

        assert popen is not None  # 给类型检查看；failure 为 None 时一定有进程
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        seen_percent = _PercentTracker()
        reader = threading.Thread(
            target=_drain_stream,
            args=(popen.stdout, stdout_chunks, seen_percent, progress_callback, total),
            daemon=True,
        )
        # stderr 也必须被抽干：管道写满（默认 64 KiB）时子进程会**永久阻塞**，
        # 表现为"adb push 卡死"，而实际是我们没读它。这是个经典陷阱。
        stderr_reader = threading.Thread(
            target=_drain_stream, args=(popen.stderr, stderr_chunks), daemon=True
        )
        reader.start()
        stderr_reader.start()

        cancelled = False
        timed_out = False
        deadline = started + timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _kill(popen)
                break
            if popen.poll() is not None:
                break
            if time.monotonic() > deadline:
                timed_out = True
                _kill(popen)
                break
            time.sleep(ADB_PUSH_POLL_INTERVAL_SECONDS)

        # 让读线程把尾部输出收干净（进程已经结束了，这里只等它们的最后几个字节）
        reader.join(timeout=2.0)
        stderr_reader.join(timeout=2.0)

        duration_ms = (time.monotonic() - started) * 1000.0
        stdout = _decode(b"".join(stdout_chunks))
        stderr = _decode(b"".join(stderr_chunks))
        return_code = popen.returncode

        if cancelled:
            return AdbCommandResult(
                success=False,
                return_code=return_code,
                stdout=stdout,
                stderr=stderr,
                duration_ms=duration_ms,
                error="transfer cancelled by user",
                error_code=ERROR_CANCELLED,
                cancelled=True,
            )
        if timed_out:
            return AdbCommandResult(
                success=False,
                return_code=return_code,
                stdout=stdout,
                stderr=stderr,
                duration_ms=duration_ms,
                error=f"adb push 超时（{timeout:.0f} 秒）",
                error_code=ERROR_TIMEOUT,
            )

        success = return_code == 0
        result = AdbCommandResult(
            success=success,
            return_code=return_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            error=None if success else (stderr.strip() or stdout.strip() or "adb push 失败"),
        )
        if not success and result.looks_disconnected():
            result.error_code = ERROR_DEVICE_DISCONNECTED
        return result

    # ------------------------------------------------------------------ #
    # 内部：进程调用
    # ------------------------------------------------------------------ #
    def _command(self, args: Sequence[str]) -> list[str]:
        """把参数拼成**列表形式**的完整命令行（永不返回字符串，Spec §3）。"""
        return [self._adb, *[str(a) for a in args]]

    def _popen(self, args: Sequence[str]) -> tuple[subprocess.Popen | None, AdbCommandResult | None]:
        """起进程；失败时返回一个填好的结果，而不是抛异常。"""
        argv = self._command(args)
        if not _executable_exists(self._adb):
            return None, AdbCommandResult(
                success=False,
                return_code=None,
                stdout="",
                stderr="",
                duration_ms=0.0,
                error=f"未找到 adb 可执行文件：{self._adb}",
                error_code=ERROR_ADB_MISSING,
            )
        try:
            popen = subprocess.Popen(  # noqa: S603 - 参数是列表，且禁用 shell
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                env=self._env,
                **_no_window_kwargs(),
            )
        except (OSError, ValueError) as exc:
            return None, AdbCommandResult(
                success=False,
                return_code=None,
                stdout="",
                stderr="",
                duration_ms=0.0,
                error=f"无法启动 adb：{exc}",
                error_code=ERROR_START_FAILED,
            )
        return popen, None

    def _run(self, args: Sequence[str], timeout: float) -> AdbCommandResult:
        """跑一条**不长跑**的命令，把一切异常折叠成结果（Spec §4）。"""
        argv = self._command(args)
        if not _executable_exists(self._adb):
            return AdbCommandResult(
                success=False,
                return_code=None,
                stdout="",
                stderr="",
                duration_ms=0.0,
                error=f"未找到 adb 可执行文件：{self._adb}",
                error_code=ERROR_ADB_MISSING,
            )

        started = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603 - 参数是列表，且禁用 shell
                argv,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=self._env,
                **_no_window_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            return AdbCommandResult(
                success=False,
                return_code=None,
                stdout=_decode(exc.stdout or b""),
                stderr=_decode(exc.stderr or b""),
                duration_ms=(time.monotonic() - started) * 1000.0,
                error=f"adb 命令超时（{timeout:.0f} 秒）：{args[0] if args else ''}",
                error_code=ERROR_TIMEOUT,
            )
        except (OSError, ValueError) as exc:
            return AdbCommandResult(
                success=False,
                return_code=None,
                stdout="",
                stderr="",
                duration_ms=(time.monotonic() - started) * 1000.0,
                error=f"无法启动 adb：{exc}",
                error_code=ERROR_START_FAILED,
            )

        duration_ms = (time.monotonic() - started) * 1000.0
        stdout = _decode(completed.stdout)
        stderr = _decode(completed.stderr)
        success = completed.returncode == 0
        result = AdbCommandResult(
            success=success,
            return_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            error=None if success else (stderr.strip() or stdout.strip() or "adb 命令失败"),
        )
        if not success and result.looks_disconnected():
            result.error_code = ERROR_DEVICE_DISCONNECTED
        return result


# --------------------------------------------------------------------------- #
# 模块级工具（纯函数，便于单测）
# --------------------------------------------------------------------------- #
def _no_window_kwargs() -> dict[str, object]:
    """Windows 上隐藏控制台窗口。

    不加这个的话，每次 ``adb`` 调用都会在用户屏幕上闪一个黑框——一个每秒
    闪好几次的黑框足以让人以为中了病毒。非 Windows 平台返回空字典。
    """
    if os.name == "nt":  # pragma: no cover - 平台分支
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _executable_exists(adb_path: str) -> bool:
    """``adb_path`` 是否可执行。

    含分隔符时当作显式路径（``Path.is_file()``）；否则交给 ``shutil.which``
    在 ``PATH`` 里找。
    """
    text = str(adb_path or "").strip()
    if not text:
        return False
    if any(sep in text for sep in _PATH_SEPARATORS):
        return Path(text).is_file()
    return shutil.which(text) is not None


def _safe_size(path: Path) -> int:
    """本地文件大小；读不到算 0（进度计算的分母，0 表示"未知"）。"""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _push_timeout_seconds(total_bytes: int) -> float:
    """按文件大小估一个超时。

    基准 6 MB/s（USB 2.0 上 adb 的常见下限），再加 60 秒固定余量：小文件不会被
    卡在超时上，大文件也不会被误杀。GB 级文件因此能拿到分钟级的额度。
    """
    return 60.0 + max(0, total_bytes) / (6 * 1024 * 1024)


def _decode(raw: bytes | str) -> str:
    """把子进程输出解成文本。

    用 ``errors="replace"`` 而不是严格解码：设备端输出里混进非 UTF-8 字节
    （中文文件名常见）不该让整个结果变成异常。
    """
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8", errors="replace")


def _kill(popen: subprocess.Popen) -> None:
    """尽力终止子进程；失败也不抛（正在退出的进程 kill 会报错，那是正常的）。"""
    try:
        popen.kill()
    except OSError:  # pragma: no cover - 进程刚好自己退了
        pass
    try:
        popen.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):  # pragma: no cover
        pass


class _PercentTracker:
    """``push`` 输出的百分比跟踪器（跨 chunk 解析）。

    为什么要"跨 chunk"：adb 的行与我们的读块边界**不对齐**。``[ 45%]`` 完全可能
    被切成 ``[ 4`` 和 ``5%]`` 两块。因此保留一个尾巴缓冲，只在明确的
    ``]`` 之后才消费。
    """

    #: 保留多少字节的尾巴（一行百分比最长不过几十字节）
    _TAIL = 64

    def __init__(self) -> None:
        self._buffer = b""
        self.last_percent: float | None = None

    def feed(self, chunk: bytes) -> float | None:
        """喂一块数据，返回本块里**最后一个**百分比（没有则 ``None``）。"""
        self._buffer += chunk
        matches = _PERCENT_RE.findall(self._buffer)
        if matches:
            self.last_percent = float(matches[-1])
        # 留下尾巴：最后一行可能还没收完
        self._buffer = self._buffer[-self._TAIL :]
        return self.last_percent


def _drain_stream(
    stream,
    sink: list[bytes],
    tracker: _PercentTracker | None = None,
    callback: PushCallback | None = None,
    total_bytes: int = 0,
) -> None:
    """把子进程的一个流抽干，并顺路解析进度。

    用 ``read1()`` 而不是 ``read(n)``：``read(n)`` 会一直等到攒够 n 字节或遇到
    EOF，那样"传了一半"的百分比要等到结束才出现一次——进度条等于没有。
    ``read1()`` 做一次底层读就返回，因此百分比是**实时**的。
    """
    reader = getattr(stream, "read1", None) or getattr(stream, "read", None)
    if reader is None:  # pragma: no cover - 防御性分支
        return
    try:
        while True:
            chunk = reader(_READ_CHUNK)
            if not chunk:
                break
            sink.append(chunk)
            if tracker is not None:
                percent = tracker.feed(chunk)
                if callback is not None:
                    callback(
                        AdbPushProgress(
                            percent=percent,
                            bytes_transferred=int(total_bytes * (percent or 0) / 100.0),
                            total_bytes=total_bytes,
                        )
                    )
    except (OSError, ValueError):  # pragma: no cover - 管道在 kill 时关闭
        pass


def _parse_leading_int(text: str) -> int | None:
    """取输出里第一个非负整数（``stat`` / ``wc`` 的兼容解析）。

    ``wc -c`` 的输出是 ``  12345 /sdcard/x``，``stat -c %s`` 是 ``12345``；
    两种都取得到。
    """
    match = re.search(r"\d+", text or "")
    if match is None:
        return None
    try:
        return int(match.group(0))
    except ValueError:  # pragma: no cover - 正则已经保证是数字
        return None


def _parse_sha256(text: str) -> str | None:
    """从 ``sha256sum`` 的输出里取出 64 位十六进制摘要（小写）。"""
    match = _SHA256_RE.search(text or "")
    if match is None:
        return None
    return match.group(1).lower()


def parse_df_available_bytes(text: str) -> int | None:
    """解析 ``df`` 输出里的"可用空间"字节数（Spec §17）。

    没找到"可用"列就返回 ``None``——传输层据此给 WARNING 并**继续**，
    Spec §17 明确要求"无法解析 df 时允许继续"。

    实现的兼容点：

    - 表头里若有 ``Available`` / ``Avail``，按列名定位（最稳）；
    - 否则退回第 4 列（``Filesystem 1K-blocks Used Available ...`` 的固定位置）；
    - ``df -k`` 的块大小是 1024，因此乘 1024 换算成字节；
    - 多行输出（路径跨了多个挂载点）时取**最后一行**——那是最贴近目标路径的。
    """
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return None

    header_index: int | None = None
    column: int | None = None
    for line in lines[:1]:
        cells = line.split()
        for index, cell in enumerate(cells):
            if cell.strip().lower() in {"available", "avail"}:
                header_index = index
                column = index
                break

    candidates = lines[1:] if (header_index is not None or len(lines) > 1) else lines
    for line in reversed(candidates):
        cells = line.split()
        if header_index is not None and column is not None:
            if len(cells) <= column:
                continue
            target = cells[column]
        else:
            if len(cells) < 4:
                continue
            target = cells[3]
        if not target.isdigit():
            continue
        return int(target) * 1024
    return None
