"""Windows → Android 的离线地图包传输（Slice 10，Spec §14 ~ §27）。

一件事：把本地一个标准离线地图包搬到设备的公共导入区，并在设备侧**证明**
搬对了::

    本地 <package_dir>/                    远端 <import>/<manifest.id>/
    ├── map.mbtiles        ──push──→       ├── map.mbtiles
    └── manifest.json                      └── manifest.json

顺序是设计的一部分（Spec §19 / §20）
------------------------------------

::

    mkdir 远端目录
    push  map.mbtiles  →  map.mbtiles.part      ← 先传大文件，且不叫正式名字
    mv    map.mbtiles.part  →  map.mbtiles      ← 原子改名：这一刻才算"传好了"
    push  manifest.json                         ← 最后传，它是"包齐了"的信号
    校验 远端大小 + 远端 SHA256（best-effort）

为什么不直接 ``push map.mbtiles``：``adb push`` 在传输过程中**没有原子性**。
如果直接写正式名字，Android 端（Slice 11）可能在文件只写了一半时就看到一个
"文件名齐全"的包——它会认为传输完成了，然后拿着一个截断的 MBTiles 去解析。
``.part`` + ``mv`` 让"文件是否存在"变成一个可信的信号。

为什么 manifest 最后传：Slice 11 可以把 ``manifest.json`` 的存在当作
"包传输基本完成"的标志。大文件还没传完时它不该出现（Spec §19）。

这一层不做的事（Spec §37 / §47）
--------------------------------

不 ``adb install``、不 ``adb root``、不 ``adb remount``、不用 ``run-as``、
不碰 App 私有目录、不压缩、不改 MBTiles、不实现 Android 端的导入。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from app.android.adb_client import ERROR_CANCELLED, ERROR_DEVICE_DISCONNECTED, AdbClient
from app.android.device_manager import DeviceManager
from app.android.transfer_models import (
    TransferPlan,
    TransferProgress,
    TransferResult,
    is_safe_remote_root,
    part_path,
    remote_dir_for,
)
from app.config.settings import (
    LOG_TRANSFER_COMPLETED,
    LOG_TRANSFER_FAILED,
    LOG_TRANSFER_PART_CLEANED,
    LOG_TRANSFER_PUSH,
    LOG_TRANSFER_REMOTE_MKDIR,
    LOG_TRANSFER_SHA256_OK,
    LOG_TRANSFER_SHA256_SKIPPED,
    LOG_TRANSFER_SIZE_OK,
    PACKAGE_MANIFEST_FILENAME,
    PACKAGE_MBTILES_FILENAME,
    PROGRESS_SPEED_WINDOW_SECONDS,
    TRANSFER_ERROR_CANCELLED,
    TRANSFER_ERROR_DEVICE_NOT_READY,
    TRANSFER_ERROR_DEVICE_OFFLINE,
    TRANSFER_ERROR_DEVICE_UNAUTHORIZED,
    TRANSFER_ERROR_DEVICE_UNKNOWN,
    TRANSFER_ERROR_DISCONNECTED,
    TRANSFER_ERROR_LOCAL_FILE_MISSING,
    TRANSFER_ERROR_LOCAL_SIZE_MISMATCH,
    TRANSFER_ERROR_MKDIR,
    TRANSFER_ERROR_NO_SPACE,
    TRANSFER_ERROR_NOT_DIR,
    TRANSFER_ERROR_PACKAGE_DIR_MISSING,
    TRANSFER_ERROR_PUSH_MANIFEST,
    TRANSFER_ERROR_PUSH_MBTILES,
    TRANSFER_ERROR_REMOTE_ROOT,
    TRANSFER_ERROR_RENAME,
    TRANSFER_ERROR_SHA256_MISMATCH,
    TRANSFER_ERROR_SIZE_MISMATCH,
    TRANSFER_ERROR_SIZE_UNKNOWN,
    TRANSFER_STAGE_RANGES,
    TRANSFER_WARNING_DF_UNPARSABLE,
    TRANSFER_WARNING_OLD_PART_CLEANED,
    TRANSFER_WARNING_PART_LEFT,
    TRANSFER_WARNING_SHA256_UNAVAILABLE,
)
from app.geo import format_size
from app.models.device import DeviceState
from app.package import MapPackageReader, MapPackageValidator, PackageError

__all__ = ["MapPackageTransfer", "ProgressCallback"]

_logger = logging.getLogger(__name__)

#: 传输进度回调
ProgressCallback = Callable[[TransferProgress], None]


class _ProgressReporter:
    """把"阶段 + 已传字节"翻译成"整体百分比 + 速度"（Spec §22 / §30）。

    为什么不让传输逻辑自己算百分比：阶段权重（:data:`TRANSFER_STAGE_RANGES`）
    是个策略问题，跟"先 push 哪个文件"是两件事。分开了才能单独测进度曲线
    （单调、结束于 100、每个阶段都被走到）。

    速度用**滑动窗口**而不是"总量 / 总耗时"：后者会把开头那段 ``mkdir`` /
    ``df`` 的时间摊到速率上，传完时显示的"平均速度"比用户看到的瞬时速度低很多。
    """

    def __init__(
        self,
        callback: ProgressCallback | None,
        clock: Callable[[], float],
        *,
        window_seconds: float = PROGRESS_SPEED_WINDOW_SECONDS,
    ) -> None:
        self._callback = callback
        self._clock = clock
        self._window = window_seconds
        self._samples: deque[tuple[float, int]] = deque()
        self._stage = "check"
        self._current_file: str | None = None
        self._done = 0
        self._stage_done = 0
        self._total = 0
        #: 最近一次发出的进度（结果里要拿它算总字节）
        self.last: TransferProgress | None = None

    @property
    def stage(self) -> str:
        """当前阶段名。"""
        return self._stage

    @property
    def total_bytes(self) -> int:
        """本次要传的总字节。"""
        return self._total

    def set_total(self, total: int) -> None:
        """设定总字节（本地两个文件之和）。"""
        self._total = max(0, int(total))

    def stage_start(
        self,
        stage: str,
        *,
        current_file: str | None = None,
        message: str | None = None,
        done: int | None = None,
    ) -> None:
        """进入一个新阶段：把进度条推到该阶段的**起点**。

        每个阶段都发一次，界面上的进度条才会一段一段往前走，而不是在
        ``0%`` 上停到 push 开始。``done`` 用来把"已完成文件的字节"带进新阶段。
        """
        self._stage = stage
        self._current_file = current_file
        self._stage_done = 0
        if done is not None:
            self._done = max(self._done, int(done))
        self._emit(0.0, message=message)

    def advance(
        self,
        stage_done: int,
        *,
        stage_total: int,
        current_file: str | None = None,
        message: str | None = None,
        absolute_done: int | None = None,
    ) -> None:
        """在一个阶段内部推进。

        Args:
            stage_done: 本阶段已完成的字节。
            stage_total: 本阶段的字节总量（0 表示"与字节无关的阶段"，按满进度算）。
            current_file: 当前文件名。
            message: 附加说明。
            absolute_done: 直接指定"整体已传字节"（用于 push 的百分比换算）。
        """
        if current_file is not None:
            self._current_file = current_file
        self._stage_done = max(0, int(stage_done))
        if absolute_done is not None:
            self._done = max(0, int(absolute_done))
        fraction = 1.0 if stage_total <= 0 else min(1.0, self._stage_done / stage_total)
        self._emit(fraction, message=message)

    def _emit(self, fraction: float, *, message: str | None = None) -> None:
        low, high = TRANSFER_STAGE_RANGES.get(self._stage, (0, 100))
        percent = float(low) + max(0.0, min(1.0, fraction)) * float(high - low)
        now = self._clock()
        self._samples.append((now, self._done))
        # 丢掉窗口外的样本（至少留两个，否则首帧算不出速度）
        while len(self._samples) > 2 and now - self._samples[0][0] > self._window:
            self._samples.popleft()
        speed = 0.0
        if len(self._samples) >= 2:
            first_t, first_bytes = self._samples[0]
            span = now - first_t
            if span > 0:
                speed = max(0.0, (self._done - first_bytes) / span)
        progress = TransferProgress(
            stage=self._stage,
            current_file=self._current_file,
            bytes_transferred=self._done,
            total_bytes=self._total,
            percent=percent,
            speed_bytes_per_second=speed,
            message=message,
        )
        self.last = progress
        if self._callback is not None:
            self._callback(progress)


class MapPackageTransfer:
    """把标准离线地图包推到 Android 设备的公共导入区（Spec §18）。

    用法::

        transfer = MapPackageTransfer()
        plan = transfer.plan("ABC123", "D:/maps/packages/beijing", REMOTE_ROOT)
        if plan.is_valid:
            result = transfer.transfer("ABC123", plan.package_dir, REMOTE_ROOT)

    **从不抛异常**：所有失败路径都写进 :class:`TransferResult.errors`。
    """

    def __init__(
        self,
        client: AdbClient | None = None,
        *,
        reader: MapPackageReader | None = None,
        validator: MapPackageValidator | None = None,
        device_manager: DeviceManager | None = None,
        logger: logging.Logger | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            client: adb 客户端。
            reader: 地图包读取器（Slice 9）。
            validator: 地图包校验器（Slice 9）。
            device_manager: 设备管理器；``None`` 时用 ``client`` 自建一个。
            logger: 可选日志器。
            clock: 时钟（测试注入假时钟以断言耗时）。
        """
        self._client = client or AdbClient()
        self._reader = reader or MapPackageReader()
        self._validator = validator or MapPackageValidator()
        self._devices = device_manager or DeviceManager(self._client)
        self._logger = logger or _logger
        self._clock = clock

    # ------------------------------------------------------------------ #
    # 只读依赖
    # ------------------------------------------------------------------ #
    @property
    def client(self) -> AdbClient:
        """底层 adb 客户端。"""
        return self._client

    @property
    def reader(self) -> MapPackageReader:
        """地图包读取器。"""
        return self._reader

    @property
    def device_manager(self) -> DeviceManager:
        """设备管理器。"""
        return self._devices

    # ------------------------------------------------------------------ #
    # 计划（Spec §31 的确认框）
    # ------------------------------------------------------------------ #
    def remote_package_dir(self, remote_root: str, package_id: str) -> str:
        """``<远端根>/<规整后的 id>``（Spec §14 / §15）。"""
        return remote_dir_for(remote_root, package_id)

    def plan(self, serial: str, package_dir: str | Path, remote_root: str) -> TransferPlan:
        """算出让用户确认的那几行：设备是谁、包叫什么、多大、落到哪（Spec §31）。

        **不碰设备**（除了读已经刷过的设备列表）：确认框要在用户点"开始"之前
        出现，此时不该有任何副作用。

        Returns:
            :class:`TransferPlan`；不可用时 ``plan.error`` 写明原因。
        """
        plan = TransferPlan(
            serial=str(serial or ""),
            package_dir=str(package_dir or ""),
            remote_dir="",
        )
        device = self._devices.get_device(plan.serial)
        if device is None:
            plan.error = TRANSFER_ERROR_DEVICE_UNKNOWN.format(serial=plan.serial)
            return plan
        plan.device = device
        plan.device_name = device.short_name

        if not is_safe_remote_root(remote_root):
            plan.error = TRANSFER_ERROR_REMOTE_ROOT.format(root=remote_root)
            return plan

        directory = Path(package_dir)
        if not directory.is_dir():
            plan.error = (
                TRANSFER_ERROR_PACKAGE_DIR_MISSING.format(path=directory)
                if not directory.exists()
                else TRANSFER_ERROR_NOT_DIR.format(path=directory)
            )
            return plan

        try:
            manifest, mbtiles_path = self._reader.read_package(directory)
        except PackageError as exc:
            plan.error = str(exc)
            return plan

        plan.package_id = manifest.id
        plan.package_name = manifest.name
        plan.remote_dir = self.remote_package_dir(remote_root, manifest.id)
        plan.tile_count = manifest.tile_count
        plan.mbtiles_size = _size_of(mbtiles_path)
        plan.manifest_size = _size_of(directory / PACKAGE_MANIFEST_FILENAME)
        if plan.mbtiles_size <= 0:
            plan.error = TRANSFER_ERROR_LOCAL_FILE_MISSING.format(path=mbtiles_path)
        return plan

    def remote_has_package(self, serial: str, remote_dir: str) -> bool:
        """设备上 ``<remote_dir>/manifest.json`` 是否已存在（Spec §24）。

        用 ``manifest.json`` 而不是目录本身来判断"包已经在那儿了"：目录可能
        是上次取消留下的空壳。``manifest.json`` 是 Spec §19 定义的"传完了"标志。
        """
        remote_manifest = f"{remote_dir.rstrip('/')}/{PACKAGE_MANIFEST_FILENAME}"
        return self._client.exists(serial, remote_manifest)

    # ------------------------------------------------------------------ #
    # 传输
    # ------------------------------------------------------------------ #
    def transfer(
        self,
        serial: str,
        package_dir: str | Path,
        remote_root: str,
        progress_callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        *,
        overwrite: bool = False,
        verify_package: bool = True,
    ) -> TransferResult:
        """执行一次传输（Spec §18）。

        Args:
            serial: 目标设备序列号。
            package_dir: 本地地图包目录（内含 ``map.mbtiles`` 与 ``manifest.json``）。
            remote_root: 设备上的导入区根目录。
            progress_callback: 进度回调（在调用方线程里同步调用）。
            cancel_event: 取消信号（Spec §23）。
            overwrite: 用户在"设备已有同名包"确认框里选了「覆盖」之后才传 ``True``
                （Spec §24）。默认 ``False`` 表示**先停下来问**。
            verify_package: 是否在传输前跑一次完整的包校验（含本地 SHA256）。
                界面已经校验过同一个目录时可以传 ``False``——那一次哈希没有必要
                做两遍（GB 级文件是分钟级的成本）。

        Returns:
            :class:`TransferResult`。**不抛异常。**
            ``needs_confirmation=True`` 时它不是失败，而是在等用户拍板。
        """
        reporter = _ProgressReporter(progress_callback, self._clock)
        started = self._clock()
        result = TransferResult(serial=str(serial or ""), local_package_dir=str(package_dir or ""))
        try:
            self._run(
                serial,
                Path(package_dir),
                remote_root,
                reporter,
                cancel_event,
                overwrite=overwrite,
                verify_package=verify_package,
                result=result,
            )
        except Exception as exc:  # noqa: BLE001 - 传输层绝不让异常冒泡（Spec §4 同理）
            result.success = False
            result.errors.append(f"传输异常终止：{exc}")
            self._logger.exception("传输过程中出现未预期异常")
        finally:
            result.duration_seconds = max(0.0, self._clock() - started)
            if result.bytes_transferred == 0 and reporter.last is not None:
                # 失败/取消也要如实报"传了多少"：这个数字是用户判断
                # "要不要重来 / 空间够不够"的依据，只在成功路径上填等于撒谎。
                result.bytes_transferred = reporter.last.bytes_transferred

        if result.success:
            self._logger.info(LOG_TRANSFER_COMPLETED.format(remote=result.remote_package_dir))
        elif not result.needs_confirmation:
            reason = result.errors[0] if result.errors else "unknown"
            self._logger.warning(LOG_TRANSFER_FAILED.format(reason=reason))
        return result

    # ------------------------------------------------------------------ #
    # 内部：主流程
    # ------------------------------------------------------------------ #
    def _run(
        self,
        serial: str,
        directory: Path,
        remote_root: str,
        reporter: _ProgressReporter,
        cancel_event: threading.Event | None,
        *,
        overwrite: bool,
        verify_package: bool,
        result: TransferResult,
    ) -> None:
        """按 Spec §18 的顺序走一遍；任何一步失败就停下并把原因写进 ``result``。"""
        # ---- 1. 本地包（Spec §12 / §36）--------------------------------
        reporter.stage_start("check", message="正在验证本地地图包…")
        loaded = self._load_local_package(directory, verify_package=verify_package, result=result)
        if loaded is None:
            return
        manifest, mbtiles_path, manifest_path, mbtiles_size, manifest_size = loaded

        if not is_safe_remote_root(remote_root):
            result.errors.append(TRANSFER_ERROR_REMOTE_ROOT.format(root=remote_root))
            return

        remote_dir = self.remote_package_dir(remote_root, manifest.id)
        result.remote_package_dir = remote_dir
        total_bytes = mbtiles_size + manifest_size
        reporter.set_total(total_bytes)

        # ---- 2. 设备状态（Spec §8 / §35）-------------------------------
        reporter.stage_start("device", message="正在检查设备状态…")
        if not self._ensure_device_ready(serial, result):
            return

        # 旧包保持可用，先确认覆盖并检查额外空间，再上传到独立暂存目录。
        replacing = self.remote_has_package(serial, remote_dir)
        if replacing and not overwrite:
            result.needs_confirmation = True
            result.confirmation_reason = "remote_exists"
            return
        reporter.stage_start("space", message="正在检查设备存储空间…")
        if not self._check_space(serial, remote_root, total_bytes, result):
            return
        if cancel_event is not None and cancel_event.is_set():
            result.cancelled = True
            result.errors.append(TRANSFER_ERROR_CANCELLED)
            return

        upload_dir = remote_dir
        backup_dir = ""
        if replacing:
            nonce = uuid4().hex
            safe_id = remote_dir.rsplit("/", 1)[-1]
            upload_dir = f"{remote_root.rstrip('/')}/.{safe_id}.upload-{nonce}"
            backup_dir = f"{remote_root.rstrip('/')}/.{safe_id}.backup-{nonce}"
        else:
            self._cleanup_stale_parts(serial, remote_dir, mbtiles_size, result)
        try:
            self._upload_package(
                serial,
                upload_dir,
                manifest,
                mbtiles_path,
                manifest_path,
                mbtiles_size,
                manifest_size,
                reporter,
                cancel_event,
                result,
            )
            if not result.success:
                return
            result.success = False
            if cancel_event is not None and cancel_event.is_set():
                result.cancelled = True
                result.errors.append(TRANSFER_ERROR_CANCELLED)
                return
            if replacing:
                reporter.stage_start("verify", message="校验通过，正在替换旧地图包…")
                saved = self._client.move(serial, remote_dir, backup_dir)
                if not saved.success:
                    result.errors.append(f"无法备份旧地图包，旧包已保留：{saved.error}")
                    return
                published = False
                try:
                    moved = self._client.move(serial, upload_dir, remote_dir)
                    if not moved.success:
                        result.errors.append(f"替换地图包失败：{moved.error}")
                        return
                    published = True
                finally:
                    if not published:
                        restored = self._client.move(serial, backup_dir, remote_dir)
                        if not restored.success:
                            result.errors.append(
                                f"自动恢复旧包失败，旧包仍保留在 {backup_dir}：{restored.error}"
                            )
                result.overwritten = True
                self._remove_quietly(serial, backup_dir, result)
            result.success = True
            reporter.stage_start("done", current_file=None, done=total_bytes)
        finally:
            if replacing:
                self._remove_quietly(serial, upload_dir, result)

    def _upload_package(
        self,
        serial,
        remote_dir,
        manifest,
        mbtiles_path,
        manifest_path,
        mbtiles_size,
        manifest_size,
        reporter,
        cancel_event,
        result,
    ) -> None:
        """上传并验证新包；覆盖时此方法只接触暂存目录。"""
        # ---- 4. 建目录（Spec §16）-------------------------------------
        reporter.stage_start("mkdir", message="正在创建远端目录…")
        mkdir = self._client.mkdir(serial, remote_dir)
        if not mkdir.success:
            result.errors.append(
                TRANSFER_ERROR_MKDIR.format(remote=remote_dir, error=mkdir.error or "unknown")
            )
            return
        self._logger.info(LOG_TRANSFER_REMOTE_MKDIR.format(remote=remote_dir))

        # ---- 5. push map.mbtiles → .part（Spec §20 / §21 / §22）-------
        remote_mbtiles = f"{remote_dir}/{PACKAGE_MBTILES_FILENAME}"
        remote_part = part_path(remote_mbtiles)
        reporter.stage_start("mbtiles", current_file=f"{PACKAGE_MBTILES_FILENAME}.part")
        pushed = self._push(
            serial,
            mbtiles_path,
            remote_part,
            reporter,
            cancel_event,
            stage="mbtiles",
            stage_base_bytes=0,
            stage_total=mbtiles_size,
            result=result,
        )
        if result.cancelled:
            # Spec §23：只删我们自己的临时文件，绝不碰用户已有的完整包。
            # 这个分支必须在 ``pushed is None`` 之前判断——取消时 :meth:`_push`
            # 返回的也是 ``None``，先判 ``None`` 会让清理代码变成死代码，
            # 设备上就留下一个几 GB 的 ``.part``。
            self._remove_quietly(serial, remote_part, result)
            return
        if pushed is None:
            return
        reporter.advance(0, stage_total=0, absolute_done=mbtiles_size, current_file=remote_part)

        # ---- 6. 改名（Spec §20）--------------------------------------
        reporter.stage_start("rename", current_file=PACKAGE_MBTILES_FILENAME)
        moved = self._client.move(serial, remote_part, remote_mbtiles)
        if not moved.success:
            result.errors.append(TRANSFER_ERROR_RENAME.format(error=moved.error or "unknown"))
            return

        # ---- 7. push manifest.json（Spec §19：最后传）-----------------
        reporter.stage_start("manifest", current_file=PACKAGE_MANIFEST_FILENAME, done=mbtiles_size)
        pushed_manifest = self._push(
            serial,
            manifest_path,
            f"{remote_dir}/{PACKAGE_MANIFEST_FILENAME}",
            reporter,
            cancel_event,
            stage="manifest",
            stage_base_bytes=mbtiles_size,
            stage_total=manifest_size,
            result=result,
        )
        if result.cancelled:
            # manifest 是最后一步：取消时把它删掉，大文件保持已改名的完整形态。
            # 与 mbtiles 同理，取消判断要排在 ``pushed_manifest is None`` 前面。
            self._remove_quietly(serial, f"{remote_dir}/{PACKAGE_MANIFEST_FILENAME}", result)
            return
        if pushed_manifest is None:
            return

        # ---- 8 / 9. 远端校验（Spec §25 / §26）-------------------------
        reporter.stage_start("verify", current_file=None)
        self._verify_remote(
            serial,
            remote_dir,
            manifest,
            mbtiles_size,
            manifest_size,
            result,
        )
        if result.errors:
            return

        result.bytes_transferred = mbtiles_size + manifest_size
        result.success = True

    # ------------------------------------------------------------------ #
    # 内部：各步骤
    # ------------------------------------------------------------------ #
    def _load_local_package(
        self,
        directory: Path,
        *,
        verify_package: bool,
        result: TransferResult,
    ) -> tuple | None:
        """读 manifest + 确认两个文件都在 + （可选）跑一次完整校验（Spec §12）。

        返回值是 ``(manifest, mbtiles_path, manifest_path, mbtiles_size, manifest_size)``；
        失败返回 ``None``（原因已写进 ``result.errors``）。

        **只认 ``package_dir`` 下的两个固定文件名**（Spec §36）：即使 manifest
        里写着 ``mbtiles.file = "../../etc/hosts"``，Slice 9 的读取器也会拒绝，
        这里再取一次 :meth:`MapPackageReader.resolve_mbtiles` 的结果，
        两处都保证不了"跑到包外面去"。
        """
        if not directory.is_dir():
            result.errors.append(
                TRANSFER_ERROR_PACKAGE_DIR_MISSING.format(path=directory)
                if not directory.exists()
                else TRANSFER_ERROR_NOT_DIR.format(path=directory)
            )
            return None

        try:
            manifest, mbtiles_path = self._reader.read_package(directory)
        except PackageError as exc:
            result.errors.append(str(exc))
            return None

        manifest_path = directory / PACKAGE_MANIFEST_FILENAME
        if not manifest_path.is_file():
            result.errors.append(TRANSFER_ERROR_LOCAL_FILE_MISSING.format(path=manifest_path))
            return None
        if not mbtiles_path.is_file():
            result.errors.append(TRANSFER_ERROR_LOCAL_FILE_MISSING.format(path=mbtiles_path))
            return None

        mbtiles_size = _size_of(mbtiles_path)
        manifest_size = _size_of(manifest_path)
        if mbtiles_size <= 0:
            result.errors.append(TRANSFER_ERROR_LOCAL_FILE_MISSING.format(path=mbtiles_path))
            return None

        # 便宜且值得的一次核对：manifest 自己写着 mbtiles 应该多大。
        # 用户手工删过、复制到一半断电、杀毒软件隔离过一半，都会在这里现形——
        # 而这些情况在 verify_package=False（界面刚校验过）时不跑完整哈希，
        # 只靠"文件存在"是查不出来的。
        if manifest.mbtiles_size and manifest.mbtiles_size != mbtiles_size:
            result.errors.append(
                TRANSFER_ERROR_LOCAL_SIZE_MISMATCH.format(
                    file=PACKAGE_MBTILES_FILENAME,
                    expected=manifest.mbtiles_size,
                    actual=mbtiles_size,
                )
            )
            return None

        if verify_package:
            check = self._validator.validate(directory, verify_checksum=True, inspect_mbtiles=True)
            result.warnings.extend(check.warnings)
            if not check.valid:
                reasons = "；".join(check.errors) or "校验未通过"
                result.errors.append(reasons)
                return None

        return manifest, mbtiles_path, manifest_path, mbtiles_size, manifest_size

    def _ensure_device_ready(self, serial: str, result: TransferResult) -> bool:
        """设备必须存在、且状态为 ``device``（Spec §8）。

        三种"不能用"分开报：未授权（去手机上点允许）、离线（重插线）、
        不存在（列表过期了）。混成一句"设备不可用"对用户毫无帮助。
        """
        device = self._devices.get_device(serial)
        if device is None:
            # 设备列表可能过期（用户刚插上还没刷新）——刷新一次再判断
            self._devices.refresh_devices()
            device = self._devices.get_device(serial)
        if device is None:
            result.errors.append(TRANSFER_ERROR_DEVICE_UNKNOWN.format(serial=serial))
            return False
        if device.state is DeviceState.UNAUTHORIZED:
            result.errors.append(TRANSFER_ERROR_DEVICE_UNAUTHORIZED.format(serial=serial))
            return False
        if device.state is DeviceState.OFFLINE:
            result.errors.append(TRANSFER_ERROR_DEVICE_OFFLINE.format(serial=serial))
            return False
        if not device.is_ready:
            result.errors.append(
                TRANSFER_ERROR_DEVICE_NOT_READY.format(state=device.state_value, serial=serial)
            )
            return False
        return True

    def _check_space(
        self, serial: str, remote_root: str, needed: int, result: TransferResult
    ) -> bool:
        """可用空间够不够（Spec §17）。

        解析不出来（``None``）就**继续**，只留一句 WARNING —— Android 版本太多，
        ``df`` 的输出格式没法穷举，为了一个解析失败拦住用户是本末倒置。
        """
        available = self._client.available_space(serial, remote_root)
        if available is None:
            self._logger.warning(TRANSFER_WARNING_DF_UNPARSABLE)
            result.warnings.append(TRANSFER_WARNING_DF_UNPARSABLE)
            return True
        if available < needed:
            result.errors.append(
                TRANSFER_ERROR_NO_SPACE.format(
                    needed=format_size(needed), available=format_size(available)
                )
            )
            return False
        return True

    def _cleanup_stale_parts(
        self, serial: str, remote_dir: str, size: int, result: TransferResult
    ) -> None:
        """清掉上次留下的 ``*.part``（Spec §20 / §35）。

        只在**我们自己的目录**里动手，而且只删 ``.part`` 结尾的文件——
        "上次传到一半"是常态（拔线、关机、取消），留着一个几 GB 的残骸会让
        下一次传输白白撞上"空间不足"。
        """
        for name in (f"{PACKAGE_MBTILES_FILENAME}.part", f"{PACKAGE_MANIFEST_FILENAME}.part"):
            path = f"{remote_dir.rstrip('/')}/{name}"
            if not self._client.exists(serial, path):
                continue
            removed = self._client.remove(serial, path)
            if removed.success:
                self._logger.info(LOG_TRANSFER_PART_CLEANED.format(path=path))
                result.warnings.append(TRANSFER_WARNING_OLD_PART_CLEANED.format(path=path))
            else:
                result.warnings.append(TRANSFER_WARNING_PART_LEFT.format(path=path))

    def _push(
        self,
        serial: str,
        local_path: Path,
        remote_path: str,
        reporter: _ProgressReporter,
        cancel_event: threading.Event | None,
        *,
        stage: str,
        stage_base_bytes: int,
        stage_total: int,
        result: TransferResult,
    ):
        """一次 ``adb push``，把输出里的百分比接到进度上报器上。

        Returns:
            成功时返回 ``AdbCommandResult``；失败/取消时返回 ``None``
            （原因已写进 ``result``）。
        """
        self._logger.info(LOG_TRANSFER_PUSH.format(file=local_path.name))

        def on_progress(event) -> None:
            """``adb push`` 的进度 → 整体进度。

            ``event.percent`` 为 ``None`` 时（adb 没给百分比）只更新"当前文件"，
            不伪造字节数：Spec §22 说得很清楚，宁可退回阶段级进度，
            也不要为了好看编一个数字。
            """
            if event.percent is None:
                reporter.advance(
                    0, stage_total=stage_total, current_file=remote_path
                )
                return
            done = int(stage_total * event.percent / 100.0)
            reporter.advance(
                done,
                stage_total=stage_total,
                current_file=remote_path,
                absolute_done=stage_base_bytes + done,
            )

        pushed = self._client.push(
            serial,
            local_path,
            remote_path,
            progress_callback=on_progress,
            cancel_event=cancel_event,
        )

        if pushed.cancelled or pushed.error_code == ERROR_CANCELLED:
            result.cancelled = True
            result.errors.append(TRANSFER_ERROR_CANCELLED)
            self._logger.info("Transfer cancelled by user")
            return None
        if not pushed.success:
            self._record_push_failure(pushed, remote_path, stage=stage, result=result)
            return None
        return pushed

    def _record_push_failure(self, pushed, remote_path: str, *, stage: str, result: TransferResult) -> None:
        """push 失败：先分清"设备掉了"与"命令报错"（Spec §35）。

        设备断开时**故意**不清理 ``.part``：那个残骸是排查"线是不是松了"的唯一
        实物证据。取消才清（那是用户自己要的干净）。
        """
        if pushed.error_code == ERROR_DEVICE_DISCONNECTED or pushed.looks_disconnected():
            result.errors.append(TRANSFER_ERROR_DISCONNECTED)
            result.warnings.append(TRANSFER_WARNING_PART_LEFT.format(path=remote_path))
            return
        template = (
            TRANSFER_ERROR_PUSH_MBTILES if stage == "mbtiles" else TRANSFER_ERROR_PUSH_MANIFEST
        )
        result.errors.append(template.format(error=pushed.error or "unknown"))

    def _verify_remote(
        self,
        serial: str,
        remote_dir: str,
        manifest,
        mbtiles_size: int,
        manifest_size: int,
        result: TransferResult,
    ) -> None:
        """远端大小 + 远端 SHA256（Spec §25 / §26）。"""
        remote_mbtiles = f"{remote_dir}/{PACKAGE_MBTILES_FILENAME}"
        remote_manifest = f"{remote_dir}/{PACKAGE_MANIFEST_FILENAME}"

        remote_mbtiles_size = self._client.file_size(serial, remote_mbtiles)
        remote_manifest_size = self._client.file_size(serial, remote_manifest)
        result.remote_mbtiles_size = remote_mbtiles_size
        result.remote_manifest_size = remote_manifest_size

        if remote_mbtiles_size is None:
            result.errors.append(TRANSFER_ERROR_SIZE_UNKNOWN.format(file=PACKAGE_MBTILES_FILENAME))
        elif remote_mbtiles_size != mbtiles_size:
            result.errors.append(
                TRANSFER_ERROR_SIZE_MISMATCH.format(
                    file=PACKAGE_MBTILES_FILENAME,
                    expected=mbtiles_size,
                    actual=remote_mbtiles_size,
                )
            )
        if remote_manifest_size is None:
            result.errors.append(
                TRANSFER_ERROR_SIZE_UNKNOWN.format(file=PACKAGE_MANIFEST_FILENAME)
            )
        elif remote_manifest_size != manifest_size:
            result.errors.append(
                TRANSFER_ERROR_SIZE_MISMATCH.format(
                    file=PACKAGE_MANIFEST_FILENAME,
                    expected=manifest_size,
                    actual=remote_manifest_size,
                )
            )

        if result.errors:
            return
        result.size_verified = True
        self._logger.info(
            LOG_TRANSFER_SIZE_OK.format(file=PACKAGE_MBTILES_FILENAME, size=mbtiles_size)
        )

        expected = str(manifest.mbtiles_sha256 or "").lower()
        actual = self._client.remote_sha256(serial, remote_mbtiles)
        if actual is None:
            # Spec §26 / §33：设备不支持远端 sha256sum 是**正常情况**，
            # 不是传输失败。大小已经对上了，给 WARNING 收尾。
            result.checksum_verified = False
            result.warnings.append(TRANSFER_WARNING_SHA256_UNAVAILABLE)
            self._logger.info(LOG_TRANSFER_SHA256_SKIPPED)
            return
        if actual != expected:
            result.checksum_verified = False
            # 内容对不上是**硬失败**：文件在设备上是坏的，装上去只会出问题
            result.errors.append(
                TRANSFER_ERROR_SHA256_MISMATCH.format(
                    file=PACKAGE_MBTILES_FILENAME, expected=expected, actual=actual
                )
            )
            return
        result.checksum_verified = True
        self._logger.info(LOG_TRANSFER_SHA256_OK.format(file=PACKAGE_MBTILES_FILENAME))

    def _remove_quietly(self, serial: str, remote_path: str, result: TransferResult) -> None:
        """尽力删一个文件；删不掉只记一条警告。

        取消路径上不留异常：用户已经点了"取消"，这时候再弹一个"清理失败"
        只会让人以为出了更大的事。
        """
        removed = self._client.remove(serial, remote_path)
        if not removed.success:
            if remote_path.endswith(".part"):
                result.warnings.append(TRANSFER_WARNING_PART_LEFT.format(path=remote_path))
            else:
                result.warnings.append(f"远端临时目录或备份未能清理，请手动检查：{remote_path}")


def _size_of(path: Path) -> int:
    """本地文件大小；读不到返回 0（0 在调用方被当作"文件不可用"）。"""
    try:
        return path.stat().st_size
    except OSError:
        return 0
