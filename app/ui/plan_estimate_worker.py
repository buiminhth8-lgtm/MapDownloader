"""计划估算线程（Slice 20，Spec §20 / §21）。

为什么估算必须离开主线程
------------------------

估算一个条目 = 遍历它的候选瓦片范围、逐张做多边形相交判定（复用
:class:`~app.geo.RegionTileEstimator`，Slice 16）。合成目录里的省级 2°×2° 区域
在 Z9~13 下不过几千张，但**真实数据不是这样**：一个省 Z13、一个市 Z16，
量级直接到几十万；计划里再放上"全国 + 五个省 + 十个市"，主线程会被锁住
几十秒到几分钟 —— 用户看到的是"窗口没反应"，Windows 还会在标题栏写上
"无响应"。Spec §20 因此要求进度信号，Spec §21 要求可取消。

同一个线程里既跑估算又管取消
----------------------------

:meth:`cancel` 只是把 ``threading.Event`` 置位；真正的检查点在估算器的条目之间与单条目遍历期间
（:meth:`~app.planning.plan_estimator.DownloadPlanEstimator.estimate` 的
``should_cancel`` 回调）。这样取消是**立刻**生效的原子操作，不必等一次
信号槽往返 —— 而往返正是用户"点了取消没反应"的典型来源。

取消后计划配置一个字都没动（Spec §21 的硬要求，估算器那侧保证），
因此本线程的三种结局可以分得很干净：

=========================  ==============================================
``estimated``              算完了（``DownloadPlan`` 已就地刷新）
``cancelled``              用户取消（已算完的条目保留结果）
``failed``                 真的出错了（未知异常，必须让人看见）
=========================  ==============================================

**不在此线程里碰任何 Qt 控件**：只发信号。界面收到信号后在主线程刷新
（与 :class:`~app.ui.tile_scan_worker.ExistingTileScanWorker` 同一套模式）。
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, QThread, Signal

from app.planning.plan_estimator import PlanEstimateCancelled
from app.planning.plan_service import PlanService

__all__ = ["PlanEstimateWorker"]


_logger = logging.getLogger(__name__)


class PlanEstimateWorker(QThread):
    """在后台线程里估算整个计划，边算边报进度。"""

    #: 每算完一个条目发一次，载荷是
    #: :class:`~app.planning.plan_estimator.PlanEstimateProgress`
    progressed = Signal(object)

    #: 算完了，载荷是刷新过的 :class:`~app.planning.download_plan.DownloadPlan`
    estimated = Signal(object)

    #: 用户取消了，载荷是一句给界面显示的话
    cancelled = Signal(str)

    #: 估算真的出错了（未知异常），载荷是一句给界面显示的话
    failed = Signal(str)

    def __init__(self, plan_service: PlanService, parent: QObject | None = None) -> None:
        """
        Args:
            plan_service: 计划编排入口（估算经由它的 ``estimate()`` 走，
                这样界面与线程用的是同一份计划对象）。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._plan_service = plan_service
        #: 跨线程安全的取消标志：估算线程读，主线程写（Spec §21）
        self._stop = threading.Event()

    # ------------------------------------------------------------------ #
    # 只读
    # ------------------------------------------------------------------ #
    @property
    def plan_service(self) -> PlanService:
        """被估算的计划入口。"""
        return self._plan_service

    @property
    def cancel_requested(self) -> bool:
        """用户是否已经请求取消。"""
        return self._stop.is_set()

    # ------------------------------------------------------------------ #
    # 控制
    # ------------------------------------------------------------------ #
    def cancel(self) -> None:
        """请求取消（**立刻**返回，不等线程结束）。

        调用方随后会收到 ``cancelled`` 信号；若估算恰好在置位前一刻算完，
        收到的就是 ``estimated`` —— 两者都不会让界面卡在"正在估算"上。
        """
        self._stop.set()

    # ------------------------------------------------------------------ #
    # 线程入口
    # ------------------------------------------------------------------ #
    def run(self) -> None:  # noqa: D102 - QThread 约定的入口
        try:
            plan = self._plan_service.estimate(
                should_cancel=self._stop.is_set,
                progress=self.progressed.emit,
            )
        except PlanEstimateCancelled as exc:
            _logger.info("用户取消了计划估算：%s", exc)
            self.cancelled.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 绝不让异常冒泡进 Qt 事件循环
            # 估算器刻意只吞"这一项算不出来"的那几种原因，因此走到这里说明
            # 是个真 bug。把它说出来（而不是留一个空白的进度条）——Spec §20
            # 要的进度反馈，反面就是"永远停在 60%"。
            _logger.exception("计划估算失败")
            self.failed.emit(f"估算失败：{exc}")
            return
        self.estimated.emit(plan)
