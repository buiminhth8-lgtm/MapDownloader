"""后台 Worker 桥（Slice 10 起）。

这里放"必须离开 Qt 主线程才能做"的胶水：把 :class:`app.services.AndroidTransferService`
的阻塞动作搬到 ``QThread`` 上，并用 Qt 信号把结果送回主线程。

为什么单独一个包
----------------

Slice 3~9 的 Worker 都住在 ``app.ui``（``download_worker.py`` /
``tile_scan_worker.py`` / ``package_dialog.py`` 里的两个）。Slice 10 的 Spec
明确要求 ``app/workers/android_transfer_worker.py``，因此本 Slice 起把
"线程桥"收进这里；``app.ui`` 下那几个是历史位置，本次**不动**它们
（Spec §45：不许顺手改别的 Slice 的交付物）。

三条必须守住的规则（Spec §29）
------------------------------

1. ``adb push`` 绝不在 Qt 主线程执行——一条 ``adb push`` 是分钟级的，
   在主线程跑等于界面卡死；
2. 后台线程**绝不**直接改 QWidget（Qt 不允许，且会随机崩）。线程只发信号，
   控件只在主线程的槽里被碰；
3. 取消是**协作式**的：worker 只置一个 ``threading.Event``，由传输层在两次
   读输出之间检查它——不 ``terminate()`` 线程（那会让 ``adb push`` 的子进程
   变成孤儿，而孤儿进程还会继续往设备里写文件）。
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.config.settings import TRANSFER_STAGE_CANCELLED_TEXT, TRANSFER_STAGE_TEXT
from app.services import AndroidTransferService

__all__ = ["AndroidDeviceWorker", "AndroidPackageCheckWorker", "AndroidTransferWorker"]

#: 日志级别（与 ``LogPanel.append_log`` 的取值一致）
_LEVEL_INFO = "INFO"
_LEVEL_WARNING = "WARNING"
_LEVEL_ERROR = "ERROR"


class AndroidDeviceWorker(QThread):
    """在后台跑一次"探测 ADB + 刷新设备"（Spec §3 / §6）。

    ``adb version`` + ``adb devices -l`` + 每台设备最多四次 ``getprop`` ——
    设备多的时候是十几个往返，全在主线程跑就是肉眼可见的白屏。
    """

    #: 刷新完成，携带 :class:`~app.android.DeviceRefreshOutcome`
    devices_refreshed = Signal(object)
    #: 日志（消息, 级别）
    log_message = Signal(str, str)

    def __init__(self, service: AndroidTransferService, parent=None) -> None:
        super().__init__(parent)
        self._service = service

    def run(self) -> None:  # noqa: D102 - QThread 约定的入口
        try:
            outcome = self._service.refresh_devices()
        except Exception as exc:  # noqa: BLE001 - 绝不让异常冒泡进 Qt 事件循环
            self.log_message.emit(f"刷新设备异常：{exc}", _LEVEL_ERROR)
            return
        self.devices_refreshed.emit(outcome)


class AndroidPackageCheckWorker(QThread):
    """在后台校验一个离线地图包（Spec §12）。

    校验会算一次完整 SHA256——GB 级文件是分钟级，因此**必须**离开主线程。
    """

    #: 进度（:class:`app.package.PackageProgress`）
    progress_changed = Signal(object)
    #: 校验完成，携带 :class:`~app.package.PackageValidationResult`
    check_completed = Signal(object)
    #: 日志（消息, 级别）
    log_message = Signal(str, str)

    def __init__(
        self, service: AndroidTransferService, package_dir: str | Path, parent=None
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._package_dir = str(package_dir)
        #: 请求中止（关窗口时置位）——校验器会在下一个检查点收手
        self._stop = False

    def request_stop(self) -> None:
        """请求尽快中止（关窗口 / 用户换了另一个包）。"""
        self._stop = True

    def run(self) -> None:  # noqa: D102
        try:
            result = self._service.validate_package(
                self._package_dir,
                progress_callback=self.progress_changed.emit,
                should_stop=lambda: self._stop,
            )
        except Exception as exc:  # noqa: BLE001
            self.log_message.emit(f"地图包校验异常：{exc}", _LEVEL_ERROR)
            return
        self.check_completed.emit(result)


class AndroidTransferWorker(QThread):
    """在后台执行一次"地图包 → 设备"的传输（Spec §29 / §30）。

    信号刻意与 :class:`app.ui.package_dialog.PackageBuildWorker` 保持同一种形状
    （``progress_changed`` / ``log_message`` / ``xxx_completed`` / ``xxx_failed``），
    于是界面处理进度的写法在整个项目里是一致的。
    """

    #: 进度更新，携带 :class:`~app.android.TransferProgress`
    progress_changed = Signal(object)
    #: 状态行文案（阶段的中文说明）
    status_changed = Signal(str)
    #: 日志（消息, 级别）
    log_message = Signal(str, str)
    #: 传输成功，携带 :class:`~app.android.TransferResult`
    transfer_completed = Signal(object)
    #: 传输失败，携带人话原因
    transfer_failed = Signal(str)
    #: 用户取消，携带 :class:`~app.android.TransferResult`
    transfer_cancelled = Signal(object)
    #: 设备上已有同名包，等用户拍板（Spec §24），携带
    #: :class:`~app.android.TransferResult`（``needs_confirmation=True``）
    confirmation_required = Signal(object)

    def __init__(
        self,
        service: AndroidTransferService,
        serial: str,
        package_dir: str | Path,
        remote_root: str | None = None,
        *,
        overwrite: bool = False,
        parent=None,
    ) -> None:
        """
        Args:
            service: 编排层。
            serial: 目标设备（**必须复用用户选中的那台**，Spec §21）。
            package_dir: 本地地图包目录。
            remote_root: 远端导入区；``None`` 时用服务层的默认值。
            overwrite: 用户在"设备已有同名包"确认框里选了「覆盖」之后才传 ``True``。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._service = service
        self._serial = serial
        self._package_dir = str(package_dir)
        self._remote_root = remote_root
        self._overwrite = overwrite
        self._cancel_event = None

    # ------------------------------------------------------------------ #
    # 控制（主线程调用）
    # ------------------------------------------------------------------ #
    def cancel(self) -> None:
        """请求取消（Spec §23）。

        只是置一个事件：真正的终止发生在传输层读到它的时候——那里会 ``kill``
        掉 ``adb push`` 子进程并清理远端 ``.part``。**不做** ``terminate()``：
        强杀线程会让子进程变孤儿，孤儿 adb 会继续往设备里写文件。
        """
        event = self._cancel_event
        if event is not None:
            event.set()

    @property
    def cancel_requested(self) -> bool:
        """是否已请求取消。"""
        event = self._cancel_event
        return bool(event is not None and event.is_set())

    # ------------------------------------------------------------------ #
    # 线程入口
    # ------------------------------------------------------------------ #
    def run(self) -> None:  # noqa: D102
        self._cancel_event = threading.Event()
        try:
            result = self._service.transfer_package(
                self._serial,
                self._package_dir,
                self._remote_root,
                progress_callback=self._on_progress,
                cancel_event=self._cancel_event,
                overwrite=self._overwrite,
            )
        except Exception as exc:  # noqa: BLE001 - 服务层也不抛，这里是双保险
            self.transfer_failed.emit(f"传输异常终止：{exc}")
            return

        if result.cancelled:
            self.log_message.emit(_cancelled_text(), _LEVEL_WARNING)
            self.transfer_cancelled.emit(result)
            return
        if result.needs_confirmation:
            # 不是失败：等用户在对话框里选「覆盖」还是「取消」
            self.confirmation_required.emit(result)
            return
        if result.success:
            for warning in result.warnings:
                self.log_message.emit(warning, _LEVEL_WARNING)
            self.transfer_completed.emit(result)
            return
        reason = result.errors[0] if result.errors else "未知原因"
        self.transfer_failed.emit(reason)

    # ------------------------------------------------------------------ #
    # 进度（在 worker 线程里被传输层同步调用）
    # ------------------------------------------------------------------ #
    def _on_progress(self, progress) -> None:
        """把进度转成 Qt 信号发回主线程（Spec §30）。

        这里**只发信号**：任何 ``QWidget`` 操作都必须发生在主线程，否则 Qt 的
        表现是"大多数时候没事，偶尔整个进程消失"。
        """
        self.progress_changed.emit(progress)
        text = TRANSFER_STAGE_TEXT.get(progress.stage)
        if text:
            self.status_changed.emit(text)


def _cancelled_text() -> str:
    """取消时写进日志的那一句（走常量表，界面不另写一份）。"""
    return TRANSFER_STAGE_CANCELLED_TEXT
