"""计划级串行下载的后台执行器（Slice 21，Spec §17 / §25）。

这是薄薄的**线程 + 事件循环胶水**：把 :class:`~app.planning.plan_scheduler.PlanScheduler`
的异步编排搬到 UI 主线程之外，并把"计划状态 / 条目状态 / 聚合进度 / 日志"
通过 Qt 信号送回主线程。

分工说明（与本项目既有的 Worker 一致）
------------------------------------

- 顺序、暂停/继续/取消语义、进度聚合、执行记录落盘 → 全在 ``app.planning``；
- 单个任务的下载、重试、限速、Resume、MBTiles、Inspector → 全在既有的
  ``app.services`` / ``app.downloader`` / ``app.mbtiles``（Spec §5 禁止重写）；
- **本类只负责**："起线程 → 建事件循环 → 转发信号 → 把界面指令安全地投进
  那个循环"。

为什么一个 Worker 要活一整轮"执行会话"
---------------------------------------

暂停之后还要能"继续"，而"继续"必须落在**同一个** :class:`PlanScheduler` 实例上
（它内存里持有那份 ``PAUSED`` 执行记录，且事件循环是它跑起来的那一个）。
因此本类的线程不随"跑完一轮"退出，而是**等**到计划进入终态
（``COMPLETED`` / ``FAILED`` / ``CANCELLED``）或者界面要求关页为止。

线程模型（Spec §17 / §25）
-------------------------

- 后台线程**绝不**直接改 QWidget：只发信号；
- 界面的指令（暂停 / 继续 / 取消 / 重试 / 跳过）是**协作式**的：只把协程
  投进 worker 线程的事件循环，**从不** ``terminate()`` 线程；
- 事件循环还没起来时收到的指令先排队，等循环一起来就补发 —— 用户手快
  （或测试跑得快）不该丢掉一次点击。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QThread, Signal

from app.planning.plan_execution import PlanExecutionStatus

if TYPE_CHECKING:  # pragma: no cover - 只为了类型标注，不增加导入图
    from app.planning.download_plan import DownloadPlan
    from app.planning.plan_execution import PlanExecution, PlanProgress
    from app.planning.plan_scheduler import PlanScheduler

__all__ = ["PlanDownloadWorker"]

_logger = logging.getLogger(__name__)

#: 可以投给调度器的指令（同时也是 :class:`PlanScheduler` 的方法名）。
_COMMANDS: tuple[str, ...] = (
    "pause",
    "resume",
    "cancel",
    "retry_current",
    "skip_current",
)


class PlanDownloadWorker(QThread):
    """在后台线程里串行执行一份下载计划。

    信号名刻意避开 ``QThread`` 自带的 ``finished``（与
    :class:`app.ui.download_worker.DownloadWorker` 同一约定）。
    """

    #: 计划状态变化，携带 :class:`~app.planning.plan_execution.PlanExecution`
    status_changed = Signal(object)
    #: 条目状态变化，携带 :class:`~app.planning.plan_execution.PlanExecution`
    item_changed = Signal(object)
    #: 聚合进度，携带 :class:`~app.planning.plan_execution.PlanProgress`
    progress = Signal(object)
    #: 一行关键事件日志（Spec §24）
    log = Signal(str)
    #: 整份计划跑完（``COMPLETED``），携带执行记录
    completed = Signal(object)
    #: 计划已暂停（``PAUSED``），携带执行记录
    paused = Signal(object)
    #: 计划已取消（``CANCELLED``），携带执行记录
    cancelled = Signal(object)
    #: 这一轮根本起不来 / 意外终止，携带人话原因
    failed = Signal(str)

    def __init__(
        self,
        scheduler: "PlanScheduler",
        plan: "DownloadPlan",
        *,
        token: str = "",
        parent: Any = None,
    ) -> None:
        """
        Args:
            scheduler: 计划调度器（**由界面持有**，跨轮次复用 —— 见模块 docstring）。
            plan: 要执行的计划（本层只持有引用，不复制）。
            token: 天地图 Token（只在内存里过一道，绝不落盘，Spec §19）。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._scheduler = scheduler
        self._plan = plan
        self._token = token

        #: worker 线程里那个事件循环；未起来前为 ``None``
        self._loop: asyncio.AbstractEventLoop | None = None
        #: 一次"指令处理完毕"的信号（让常驻循环知道该看一眼状态了）
        self._activity: asyncio.Event | None = None
        #: 事件循环还没起来时收到的指令（按顺序补发一次）
        self._pending: list[str] = []
        #: 界面要求关页 / 退出程序：安全收尾后线程就结束
        self._closed = False
        #: 最后一次发布出去的执行记录（终态信号要用）
        self._execution: "PlanExecution | None" = None
        #: 上一次发布的状态（语义信号只在**变化**的那一帧发）
        self._last_status: PlanExecutionStatus | None = None

    # ------------------------------------------------------------------ #
    # 只读状态
    # ------------------------------------------------------------------ #
    @property
    def plan_id(self) -> str:
        """本次执行的是哪份计划。"""
        return str(getattr(self._plan, "id", ""))

    @property
    def execution(self) -> "PlanExecution | None":
        """最近一次发布出去的执行记录（``None`` 表示还没开始过）。"""
        return self._execution

    @property
    def close_requested(self) -> bool:
        """是否已经请求关页收尾。"""
        return self._closed

    @property
    def queued_commands(self) -> list[str]:
        """还没投出去的指令（事件循环尚未起来时收到的那些）。

        只读的快照 —— 界面用它来显示"指令已收到，稍后生效"，测试用它来确认
        "点得快也不会丢"（Spec §8 / §9）。
        """
        return list(self._pending)

    # ------------------------------------------------------------------ #
    # 控制（由 UI 主线程调用）
    # ------------------------------------------------------------------ #
    def pause(self) -> None:
        """暂停计划：当前任务安全停下，后续条目不启动（Spec §8）。"""
        self._submit("pause")

    def resume(self) -> None:
        """继续计划：从当前暂停的那一条接着跑（Spec §9）。"""
        self._submit("resume")

    def cancel(self) -> None:
        """取消计划：当前停下、后续一律取消、**已下载的成果一个都不删**（Spec §10）。"""
        self._submit("cancel")

    def retry_current(self) -> None:
        """重试当前失败的那一条（Spec §11）。"""
        self._submit("retry_current")

    def skip_current(self) -> None:
        """跳过当前失败的那一条并继续（Spec §11）。"""
        self._submit("skip_current")

    def request_close(self, *, pause: bool = True) -> None:
        """界面要关页 / 程序要退出：安全收尾，然后让线程结束（Spec §25）。

        正在跑的时候**先暂停**（而不是取消）：Spec §25 要的是"把已写入的
        MBTiles flush 掉、执行记录落成 ``PAUSED``"，下次启动就能继续。
        调用方随后应当 ``wait()`` 等本线程真的收干净，否则 Qt 会在销毁一个
        还在跑的 ``QThread`` 时中止进程。

        Args:
            pause: ``False`` 时什么都不投（调用方自己已经安排好收尾）。
        """
        self._closed = True
        if pause:
            self._submit("pause")
        self._wake()

    # ------------------------------------------------------------------ #
    # 线程入口
    # ------------------------------------------------------------------ #
    def run(self) -> None:  # noqa: D102 - QThread 约定的入口
        try:
            asyncio.run(self._main())
        except Exception as exc:  # noqa: BLE001 - 绝不让异常冒泡进 Qt 事件循环
            _logger.exception("计划执行线程异常终止")
            self.failed.emit(f"计划执行异常终止：{exc}")

    async def _main(self) -> None:
        """在 worker 线程的独立事件循环里跑完整轮"执行会话"。"""
        from app.planning.plan_scheduler import PlanSchedulerError

        loop = asyncio.get_running_loop()
        self._loop = loop
        self._activity = asyncio.Event()
        try:
            execution: "PlanExecution | None" = None
            try:
                if not self._closed:
                    execution = await self._scheduler.start(
                        self._plan,
                        token=self._token,
                        on_progress=self._on_progress,
                        on_item_changed=self._on_item_changed,
                        on_log=self._on_log,
                    )
            except PlanSchedulerError as exc:
                # "还没估算 / 没有可用条目" —— 消息本身就是给用户看的
                self._logger_info(str(exc))
                self.failed.emit(str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                _logger.exception("开始执行计划失败")
                self.failed.emit(f"计划执行异常终止：{exc}")
                return

            if execution is None:
                # 线程还没起跑，界面就已经把这一页关掉了（Spec §25：关页不是
                # 杀掉，但也没什么可发布的了）。**必须显式挡一下**：直接往下走
                # 会拿 ``None`` 去 :meth:`_publish` 读 ``status``，抛出的
                # AttributeError 会被 ``run()`` 记成"计划执行线程异常终止" ——
                # 用户看到一条没有意义的报错，而实际上什么都没发生。
                return

            self._publish(execution)
            execution = await self._replay_pending(execution)
            # 常驻：等界面的下一条指令，直到计划进入终态或界面要求收尾
            # （Spec §8 / §9 —— 暂停之后还要能"继续"，所以线程不能随本轮退出）
            while not execution.status.is_terminal and not self._closed:
                # 已经"收尾"的「部分失败」：没有任何「失败」项（不用重试 / 跳过）、
                # 也没有任何还停着（PAUSED）的项（用户还能「继续」）。剩下的只有
                # 已跳过 / 已取消 / 已完成的条目 —— 再守着线程也没事可做，让线程退出，
                # 否则 wait() 永远等不到终态（Spec §11 / §25）。
                # 注意：只判 ``failed_count == 0`` 会把"条目自己停下（PAUSED）"那种
                # PARTIAL_FAILED 误杀——那一条还等着用户「继续」，线程必须活着。
                if (
                    execution.status is PlanExecutionStatus.PARTIAL_FAILED
                    and execution.failed_count == 0
                    and not any(item.status.is_pending for item in execution.items)
                ):
                    break
                await self._wait_for_activity()
                if self._closed:
                    break
                current = self._scheduler.execution
                execution = current if current is not None else execution
                self._publish(execution)
        finally:
            self._loop = None
            self._activity = None

    async def _replay_pending(self, execution: "PlanExecution") -> "PlanExecution":
        """补发"事件循环还没起来时"收到的指令。

        那一段窗口只有几毫秒（``QThread.start()`` 到本线程的事件循环跑起来），
        但用户手快或测试点得快就会撞上 —— 丢掉一次点击比晚一点生效更糟。
        补发时机放在**第一轮之后**：此时执行记录已经存在，"暂停 / 取消 / 继续"
        才有对象可言。
        """
        pending, self._pending = self._pending, []
        for command in pending:
            if execution.status.is_terminal or self._closed:
                break
            await self._run_command(command)
            current = self._scheduler.execution
            if current is not None:
                execution = current
        return execution

    # ------------------------------------------------------------------ #
    # 指令投递
    # ------------------------------------------------------------------ #
    def _submit(self, command: str) -> None:
        """把一条指令投进 worker 线程的事件循环（线程安全）。"""
        if command not in _COMMANDS:  # pragma: no cover - 内部调用点都是常量
            raise ValueError(f"未知的计划指令：{command}")
        if self._closed and command != "pause":
            # 已经在收尾了，别再往后接活（退出路径只允许 pause 落地）
            return
        loop = self._loop
        activity = self._activity
        if loop is None or loop.is_closed() or activity is None:
            # 事件循环还没起来（或已经结束）：记下来，起来时补发
            if command not in self._pending:
                self._pending.append(command)
            return
        activity.set()  # 先唤醒常驻循环，避免"指令已执行但循环还在睡"
        asyncio.run_coroutine_threadsafe(self._run_command(command), loop)

    async def _run_command(self, command: str) -> None:
        """执行一条指令，并把结果发布出去。"""
        from app.planning.plan_scheduler import PlanSchedulerError

        try:
            await getattr(self._scheduler, command)()
        except PlanSchedulerError as exc:
            # "现在做不了这个动作"（例如没开始就点继续）—— 记一行日志就好，
            # 这不是执行失败，别把计划判成坏的（Spec §17）。
            self.log.emit(f"⚠ {exc}")
        except Exception as exc:  # noqa: BLE001 - 指令出错不该带走整个线程
            _logger.exception("计划指令执行失败 command=%s", command)
            self.failed.emit(f"计划指令「{command}」执行失败：{exc}")
        finally:
            current = self._scheduler.execution
            if current is not None:
                self._publish(current)
            self._wake()

    def _wake(self) -> None:
        """叫醒常驻循环（让它重新看一眼计划状态）。"""
        activity = self._activity
        if activity is not None:
            activity.set()

    async def _wait_for_activity(self) -> None:
        """等一条指令处理完；界面收了页面（``_closed``）时也不再干等。"""
        activity = self._activity
        if activity is None:  # pragma: no cover - 防御性分支
            return
        activity.clear()
        if self._closed:
            return
        await activity.wait()

    # ------------------------------------------------------------------ #
    # 回调（在 worker 线程的事件循环里执行）
    # ------------------------------------------------------------------ #
    def _on_progress(self, progress: "PlanProgress") -> None:
        """聚合进度（Spec §12 / §13）。"""
        self.progress.emit(progress)

    def _on_item_changed(self, execution: "PlanExecution") -> None:
        """条目状态变化（Spec §18）。"""
        self._execution = execution
        self.item_changed.emit(execution)

    def _on_log(self, message: str) -> None:
        """关键事件日志（Spec §24）。"""
        self.log.emit(message)

    # ------------------------------------------------------------------ #
    # 内部：发布
    # ------------------------------------------------------------------ #
    def _publish(self, execution: "PlanExecution") -> None:
        """把执行记录的一帧推给界面，并按状态**变化**发对应的语义信号。

        语义信号只在状态**真的变了**的那一帧发（``_last_status`` 比对）：
        一帧常常被推两次（指令处理完一次、常驻循环醒来看一眼又一次），
        不去重的话界面会收到两遍"已暂停"。
        """
        self._execution = execution
        self.status_changed.emit(execution)
        self.item_changed.emit(execution)

        status = execution.status
        if status is self._last_status:
            return
        self._last_status = status
        if status is PlanExecutionStatus.COMPLETED:
            self.completed.emit(execution)
        elif status is PlanExecutionStatus.CANCELLED:
            self.cancelled.emit(execution)
        elif status is PlanExecutionStatus.PAUSED:
            self.paused.emit(execution)

    def _logger_info(self, message: str) -> None:
        """用户级提示：既进日志，也进界面那一行。"""
        _logger.info("计划调度：%s", message)
        self.log.emit(message)
