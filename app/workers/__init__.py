"""后台 Worker 桥。

Slice 10 起，需要"离开 Qt 主线程"的阻塞动作统一放这里：线程只发信号，
控件只在主线程被碰。详见 :mod:`app.workers.android_transfer_worker`。

Slice 21 起，**计划级**串行下载也走这里（:mod:`app.workers.plan_download_worker`）：
它桥的是 :class:`~app.planning.plan_scheduler.PlanScheduler` 的异步编排。
"""

from app.workers.android_transfer_worker import (
    AndroidDeviceWorker,
    AndroidPackageCheckWorker,
    AndroidTransferWorker,
)
from app.workers.plan_download_worker import PlanDownloadWorker

__all__ = [
    "AndroidDeviceWorker",
    "AndroidPackageCheckWorker",
    "AndroidTransferWorker",
    "PlanDownloadWorker",
]
