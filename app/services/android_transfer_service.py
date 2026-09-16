"""Android 传输的编排层（Slice 10，Spec §28）。

把三件事收成一组**界面可以直接调**的动作::

    刷新设备      DeviceManager            → 设备列表（含 adb 在不在）
    校验地图包    Slice 9 MapPackageValidator → PASS / FAIL
    传输地图包    MapPackageTransfer        → TransferResult

为什么还要一层服务（界面直接拼这三样不就行了）
---------------------------------------------

理由与 Slice 9 的 :class:`~app.services.MapPackageService` 完全一样，再加一条
本层独有的:

1. **默认接线只写一处**：``MapPackageTransfer`` 需要 ``AdbClient`` /
   ``MapPackageReader`` / ``MapPackageValidator`` / ``DeviceManager`` 四个依赖，
   装配顺序不该出现在界面代码里；
2. **界面不碰业务细节**：远端目录默认放哪、设备为什么不能用、包能不能传——
   都是策略；
3. **统一兜异常**：本层每个方法都保证不抛（底层三层也都是），后台线程里就只剩
   "emit 结果"这一件事；
4. **不重复哈希**（本层独有）：用户点 [验证地图包] 会算一次完整 SHA256（GB 级
   文件是分钟级）。紧接着点 [传输到设备] 时如果**再**算一遍，等于让用户白等。
   本层记住"这个目录在这个状态下刚校验过"，传输时据此跳过重复哈希——
   同时仍然核对 manifest 记录的大小，所以"校验完又被改坏"依然拦得住。

本层**不碰界面、不碰 Qt**：``AndroidTransferWorker`` 负责线程与信号。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.android.adb_client import AdbClient
from app.android.device_manager import DeviceManager, DeviceRefreshOutcome
from app.android.map_package_transfer import MapPackageTransfer, ProgressCallback
from app.android.transfer_models import TransferPlan, TransferResult
from app.config.settings import (
    DEFAULT_ANDROID_IMPORT_DIR,
    PACKAGE_MANIFEST_FILENAME,
    PACKAGE_MBTILES_FILENAME,
)
from app.package import MapPackageValidator, PackageValidationResult
from app.providers import describe_exception

__all__ = ["AdbStatus", "AndroidTransferService"]

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdbStatus:
    """ADB 的可用性快照（Spec §10 与 §11 的"ADB / 状态"两行）。

    ``available=False`` 时 :attr:`error` 一定非空——界面据此显示"未检测到 ADB，
    请安装 Android Platform Tools，或配置 ADB_PATH"。
    """

    available: bool
    path: str
    version: str | None = None
    error: str | None = None
    error_code: str | None = None

    @property
    def status_text(self) -> str:
        """``可用`` / ``不可用``（界面直接用，不在界面里写判断）。"""
        return "可用" if self.available else "不可用"


class AndroidTransferService:
    """设备发现 + 地图包校验 + 传输的编排入口（Spec §28）。

    用法::

        service = AndroidTransferService()
        status = service.adb_status()
        outcome = service.refresh_devices()
        check = service.validate_package("D:/maps/packages/beijing")
        plan = service.plan("ABC123", "D:/maps/packages/beijing")
        result = service.transfer_package("ABC123", plan.package_dir)

    所有方法都**不抛异常**。
    """

    def __init__(
        self,
        *,
        adb_path: str | None = None,
        client: AdbClient | None = None,
        device_manager: DeviceManager | None = None,
        validator: MapPackageValidator | None = None,
        transfer: MapPackageTransfer | None = None,
        remote_root: str = DEFAULT_ANDROID_IMPORT_DIR,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            adb_path: 用户配置的 adb 路径（优先级最高，Spec §2）。
            client: adb 客户端；``None`` 时按 ``adb_path`` 自建。
            device_manager: 设备管理器；``None`` 时用 ``client`` 自建。
            validator: 地图包校验器（Slice 9）。
            transfer: 传输器；``None`` 时用上面这些依赖自建。
            remote_root: 设备端导入区根目录。
            logger: 可选日志器。
        """
        self._logger = logger or _logger
        self._client = client or AdbClient(adb_path)
        self._devices = device_manager or DeviceManager(self._client)
        self._validator = validator or MapPackageValidator()
        self._transfer = transfer or MapPackageTransfer(
            self._client,
            validator=self._validator,
            device_manager=self._devices,
        )
        self._remote_root = str(remote_root or DEFAULT_ANDROID_IMPORT_DIR)
        #: 已校验过的包：``目录 → (指纹, 结论)``。指纹变了就作废。
        self._validated: dict[str, tuple[tuple[int, int, int, int], PackageValidationResult]] = {}

    # ------------------------------------------------------------------ #
    # 只读依赖
    # ------------------------------------------------------------------ #
    @property
    def client(self) -> AdbClient:
        """底层 adb 客户端。"""
        return self._client

    @property
    def device_manager(self) -> DeviceManager:
        """设备管理器。"""
        return self._devices

    @property
    def validator(self) -> MapPackageValidator:
        """地图包校验器。"""
        return self._validator

    @property
    def transfer(self) -> MapPackageTransfer:
        """传输器。"""
        return self._transfer

    @property
    def remote_root(self) -> str:
        """设备端导入区根目录（Spec §14）。"""
        return self._remote_root

    def set_remote_root(self, remote_root: str) -> None:
        """换一个远端根目录（用户在界面上改过路径时）。"""
        self._remote_root = str(remote_root or DEFAULT_ANDROID_IMPORT_DIR)

    def set_adb_path(self, adb_path: str | None) -> None:
        """换一个 adb 并让设备管理器跟着换（用户改了配置之后）。"""
        self._client.set_adb_path(adb_path)

    # ------------------------------------------------------------------ #
    # 设备
    # ------------------------------------------------------------------ #
    def adb_status(self) -> AdbStatus:
        """探测 ADB（Spec §2 / §10）。

        **不抛异常**：探测本身就是"可能找不到"的一个动作。
        """
        try:
            result = self._client.version()
            if result.success:
                return AdbStatus(
                    available=True,
                    path=self._client.adb_path,
                    version=self._client.version_text,
                )
            return AdbStatus(
                available=False,
                path=self._client.adb_path,
                error=result.error,
                error_code=result.error_code,
            )
        except Exception as exc:  # noqa: BLE001 - 探测失败也是一种结果
            return AdbStatus(
                available=False,
                path=self._client.adb_path,
                error=describe_exception(exc),
            )

    def refresh_devices(self) -> DeviceRefreshOutcome:
        """刷新设备列表（Spec §6 / §8）。**不抛异常。**"""
        try:
            return self._devices.refresh()
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("刷新设备失败：%s", exc)
            return DeviceRefreshOutcome(adb_available=False, error=describe_exception(exc))

    @property
    def devices(self):
        """最近一次刷新得到的设备（副本）。"""
        return self._devices.devices

    def get_device(self, serial: str):
        """按序列号取设备；没有则 ``None``。"""
        return self._devices.get_device(serial)

    def is_ready(self, serial: str) -> bool:
        """这台设备现在能不能收地图（Spec §8）。"""
        return self._devices.is_ready(serial)

    # ------------------------------------------------------------------ #
    # 地图包
    # ------------------------------------------------------------------ #
    def validate_package(
        self,
        package_dir: str | Path,
        progress_callback: Callable[[object], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> PackageValidationResult:
        """校验一个离线地图包（Spec §12，复用 Slice 9 的校验器）。

        **不抛异常。** 结论会被缓存（含目录指纹），传输时据此跳过重复哈希。
        """
        directory = str(Path(package_dir))
        try:
            result = self._validator.validate(
                directory,
                verify_checksum=True,
                inspect_mbtiles=True,
                progress_callback=progress_callback,
                should_stop=should_stop,
            )
        except Exception as exc:  # noqa: BLE001 - 校验器本身也是不抛的，双保险
            self._logger.warning("地图包校验异常：%s", exc)
            result = PackageValidationResult(
                valid=False,
                package_dir=directory,
                errors=[describe_exception(exc)],
            )
        self._validated[directory] = (_fingerprint(directory), result)
        return result

    def cached_validation(self, package_dir: str | Path) -> PackageValidationResult | None:
        """取"目录内容没变"的那次校验结论；没有 / 已过期则 ``None``。"""
        directory = str(Path(package_dir))
        cached = self._validated.get(directory)
        if cached is None:
            return None
        fingerprint, result = cached
        if fingerprint != _fingerprint(directory):
            # 目录里的大小/时间戳变了 → 结论作废（用户可能刚换了一个包）
            self._validated.pop(directory, None)
            return None
        return result if result.valid else None

    # ------------------------------------------------------------------ #
    # 传输
    # ------------------------------------------------------------------ #
    def plan(
        self,
        serial: str,
        package_dir: str | Path,
        remote_root: str | None = None,
    ) -> TransferPlan:
        """算出让用户确认的那几行（Spec §31）。**不抛异常。**"""
        try:
            return self._transfer.plan(
                serial, package_dir, remote_root or self._remote_root
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("生成传输计划失败：%s", exc)
            return TransferPlan(error=describe_exception(exc))

    def transfer_package(
        self,
        serial: str,
        package_dir: str | Path,
        remote_root: str | None = None,
        progress_callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        *,
        overwrite: bool = False,
        verify_package: bool | None = None,
    ) -> TransferResult:
        """把地图包传到设备（Spec §18 / §28）。**不抛异常。** 供后台线程调用。

        Args:
            serial: 目标设备。
            package_dir: 本地地图包目录。
            remote_root: 远端导入区根目录；``None`` 时用当前默认值。
            progress_callback: 进度回调。
            cancel_event: 取消信号（Spec §23）。
            overwrite: 用户在"设备已有同名包"确认框里选了「覆盖」之后才传 ``True``
                （Spec §24）。
            verify_package: 是否在传输前跑完整包校验。``None``（默认）表示
                **智能判断**：这一轮里刚校验过同一个目录、且目录没变，就跳过
                （省掉一次 GB 级的哈希），否则老老实实校验。

        Returns:
            :class:`TransferResult`。``needs_confirmation=True`` 时是在等用户
            拍板，不是失败。
        """
        directory = Path(package_dir)
        skip_verify = (
            self.cached_validation(directory) is not None
            if verify_package is None
            else not verify_package
        )
        return self._transfer.transfer(
            serial,
            directory,
            remote_root or self._remote_root,
            progress_callback,
            cancel_event,
            overwrite=overwrite,
            verify_package=not skip_verify,
        )

    def remote_package_dir(self, package_id: str, remote_root: str | None = None) -> str:
        """远端包目录（Spec §14）。界面在用户选包之后就能显示它。"""
        return self._transfer.remote_package_dir(
            remote_root or self._remote_root, package_id
        )

    def remote_has_package(self, serial: str, remote_dir: str) -> bool:
        """设备上是否已有同名包（Spec §24，界面用来提前提示）。"""
        try:
            return self._transfer.remote_has_package(serial, remote_dir)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("检查远端地图包失败：%s", exc)
            return False

    # ------------------------------------------------------------------ #
    # 小工具
    # ------------------------------------------------------------------ #
    def default_remote_root(self) -> str:
        """默认远端导入区（Spec §2 / §14）。"""
        return DEFAULT_ANDROID_IMPORT_DIR

    @staticmethod
    def package_files() -> tuple[str, str]:
        """包内的两个固定文件名（界面显示与测试共用）。"""
        return (PACKAGE_MBTILES_FILENAME, PACKAGE_MANIFEST_FILENAME)

    @staticmethod
    def format_error(error: BaseException | str) -> str:
        """把异常变成一行可读文本（沿用 Slice 4 的异常链还原）。"""
        if isinstance(error, BaseException):
            return describe_exception(error)
        return str(error)


def _fingerprint(package_dir: str) -> tuple[int, int, int, int]:
    """地图包目录的"内容指纹"：两个文件的大小 + 修改时间。

    刻意**不**去读内容（那是哈希的活，正是因为不想重复哈希才要这个函数）。
    它只需要回答一个问题："用户点的这两次校验之间，目录被换过吗？"
    """
    directory = Path(package_dir)
    values: list[int] = []
    for name in (PACKAGE_MBTILES_FILENAME, PACKAGE_MANIFEST_FILENAME):
        try:
            stat = (directory / name).stat()
            values.extend([int(stat.st_size), int(stat.st_mtime_ns)])
        except OSError:
            values.extend([-1, -1])
    return (values[0], values[1], values[2], values[3])
