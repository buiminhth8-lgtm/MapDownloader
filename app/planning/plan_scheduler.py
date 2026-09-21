"""计划级串行调度器（Slice 21，Spec §4 / §5 / §6 / §7 / §8 / §9 / §10 / §11）。

一份计划里的任务**一个接一个**跑，绝不并行
------------------------------------------

::

    DownloadPlan ──► create_download_tasks() ──► [Task1, Task2, Task3]
                                                      │
    PlanScheduler:   Task1 ──完成──► Task2 ──完成──► Task3 ──完成──► COMPLETED

首版严格串行（Spec §4 / §29）：多个地图任务同时跑会互相抢网络与磁盘，
结果是"三个都慢、三个都看不出来进展"。串行的代价是总时长，换来的是
"任何时刻只有一个文件在写、只有一个进度在动"——用户随时知道自己在等什么。

**单任务内部仍然是并发下载的**（Slice 3 的 N 个 worker + 限速 + 重试），
本模块一个字都没改那条路：串行是"任务之间"的约束，不是"任务之内"的。

调度器只做四件事，别的一律不碰（Spec §5）
------------------------------------------

==============================  ================================================
本模块负责                      本模块**绝不**重新实现
==============================  ================================================
任务顺序（Spec §13 的层级序）    下载 / 重试 / 限速（``app.downloader``）
生命周期（起 / 停 / 继续 / 取消）  Resume 的跳过判定（``app.downloader.resume``）
状态聚合（Spec §12）             MBTiles 写入与事务（``app.mbtiles``）
异常处理（失败即停，Spec §11）    Inspector / 完成判定（``app.services``）
==============================  ================================================

每个条目都走 :meth:`MapDownloadService.resume_task` —— 这是**有意的**，
不是为了省事：``resume_task`` 已经同时覆盖了两种情形（Spec §7）：

- 输出文件不存在 → 当全新下载（内部有日志说明）；
- 输出文件存在 → 读 metadata 做一致性校验 → 跳过已有瓦片 → 补差集。

于是"第一次跑"和"暂停后继续"在单任务层面是**同一条路**，本层不需要分支 ——
而"暂停后继续必须复用 MBTiles Resume，不能从头下载"（Spec §9）就成了
这条路的自然结果，而不是一句需要人去遵守的约定。

暂停是怎么做到"安全停下还能继续"的
----------------------------------

引擎已有的"安全停下"只有一条路：``cancel()`` —— 它停止生产新瓦片、
让 worker 消费完队列里残留的项、drain 写队列、flush 并关闭数据库，
于是磁盘上留下一个**完整可用的（不完整的）MBTiles**。这条路已经被
``scripts/verify_resume.py`` 验证过（下到一半取消 → 继续 → 只补差集）。

因此本层把"暂停"实现成：**请求引擎安全停下 → 等它真的返回 → 把任务记录
从引擎写的 ``CANCELLED`` 改回 ``PAUSED``**。多出来的那一次状态改写是刻意的：
"用户点了暂停"和"用户点了取消"在**磁盘上**是同一个动作，
区别只在后续意图 —— 而那个意图只有本层知道，所以由本层补写。

这样做而不是"用闸门挂住引擎"（``pause()`` 只清闸门、协程不返回）的理由：
挂住的话，暂停期间写队列与数据库连接都还开着，程序退出时仍要强收；
而"停下并落盘"让"暂停"变成一个**可跨进程**的状态（Spec §20 / §25）。

不自动忽略失败（Spec §11 / §29）
-------------------------------

任何一条 ``FAILED`` 都会让循环**停在它那里**，计划变成
:attr:`~app.planning.plan_execution.PlanExecutionStatus.PARTIAL_FAILED`，
后面的条目不启动。用户可以「重试当前」「跳过并继续」「取消计划」——
三个都是**明确动作**，而不是调度器替他做的决定。

反过来 ``SKIPPED`` / ``CANCELLED`` 会被跨过去：那两个是用户自己的决定，
不是"没跑成"。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

from app.config.settings import (
    DEFAULT_PLAN_SAVE_INTERVAL_SECONDS,
    DEFAULT_PLAN_SAVE_TILE_INTERVAL,
)
from app.planning.download_plan import DownloadPlan, DownloadPlanItem
from app.planning.plan_execution import (
    PlanExecution,
    PlanExecutionStatus,
    PlanItemRunStatus,
    PlanProgress,
    build_progress,
    new_execution_for,
    plan_region_lookup,
)
from app.planning.plan_execution_store import (
    PlanExecutionStore,
    PlanExecutionStoreError,
)

if TYPE_CHECKING:  # pragma: no cover - 只给类型检查器看
    from app.planning.plan_service import PlanService
    from app.tasks.task_service import TaskService

__all__ = [
    "MapDownloadTaskRunner",
    "PlanScheduler",
    "PlanSchedulerError",
    "PlanTaskOutcome",
    "PlanTaskRunner",
]


_logger = logging.getLogger(__name__)


class PlanSchedulerError(RuntimeError):
    """计划没法开始执行（还没估算、没有可用条目、任务记录建不出来）。

    与 :class:`~app.planning.plan_service.PlanTaskCreationError` 的关系：
    后者是"生成任务"这一步的失败，本类在它外面再包一层，让界面只需要认识
    一个异常类型（而且消息本身就是给用户看的）。
    """


@dataclass
class PlanTaskOutcome:
    """**一个**计划条目的执行结果（本层与单任务之间的边界对象）。

    存在的理由是解耦：调度器需要知道的只有"成没成、下了多少、为什么不成"，
    不需要认识 :class:`~app.services.MapDownloadOutcome` 的十几个字段。
    测试因此可以注入一个假 runner 而完全不碰网络与 SQLite。

    Attributes:
        status: 结论。只有 ``COMPLETED`` / ``FAILED`` / ``CANCELLED`` 三种
            （``CANCELLED`` 表示"被计划级暂停或取消打断"，由调度器结合自己的
            意图改成 ``PAUSED`` 或 ``CANCELLED``）。
        completed_tiles: 任务实际处理的张数（含跳过的已有瓦片）。
        total_tiles: 任务目标张数。
        downloaded_bytes: 输出文件大小（已落盘的字节数）。
        error: 失败原因（人话，直接给用户看）。
        stopped: 是否是被"停止请求"打断的（而不是自然结束）。
    """

    status: PlanItemRunStatus = PlanItemRunStatus.FAILED
    completed_tiles: int = 0
    total_tiles: int = 0
    downloaded_bytes: int = 0
    error: str = ""
    stopped: bool = False


@runtime_checkable
class PlanTaskRunner(Protocol):
    """跑一个任务的能力（本层只依赖这三件事）。"""

    async def run(
        self,
        task: Any,
        *,
        token: str = "",
        on_progress: Callable[[Any], None] | None = None,
        task_service: "TaskService | None" = None,
    ) -> PlanTaskOutcome:
        """把一条 :class:`~app.models.DownloadTask` 跑到结束。"""
        ...

    def request_stop(self, *, pause: bool) -> None:
        """请求安全停下当前任务（``pause`` 只影响调用方如何解读，引擎侧一样）。"""
        ...


class MapDownloadTaskRunner:
    """真实 runner：把一条任务交给**既有的** :class:`MapDownloadService`。

    Spec §5 的落点就是这里 —— 本类不做下载，只做"把参数搬过去、把结果搬回来"。

    ``MapDownloadService`` 与 :mod:`app.downloader` 的 import 放在**函数内部**
    （本项目对跨层依赖的既有约定，见 :mod:`app.planning.zoom_strategy` 的说明）：
    本模块的导入图于是只依赖 ``app.config`` 与 ``app.planning`` 自己，
    任何导入顺序都不会撞上半初始化的模块。
    """

    def __init__(
        self,
        provider: Any,
        *,
        task_service: "TaskService | None" = None,
        transport: Any = None,
        resume_policy: Any = None,
        concurrency: int | None = None,
        timeout: float | None = None,
        retry_policy: Any = None,
        rate_limiter: Any = None,
        queue_capacity: int | None = None,
        batch_size: int | None = None,
        progress_interval_seconds: float | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            provider: 瓦片源（持有 Token，不落盘）。
            task_service: 任务服务：进度与最终状态由服务层直接落盘（Spec §19）。
            transport: 传输层，**仅供测试注入** ``httpx.MockTransport``。
            resume_policy: 跳过策略；``None`` 用引擎默认（跳过已有、不校验内容）。
            concurrency / timeout / retry_policy / rate_limiter / queue_capacity /
                batch_size / progress_interval_seconds: 引擎参数；``None`` 时用
                ``app.config.settings`` 的默认值（与单任务下载完全一致）。
            logger: 可选日志器。
        """
        self._provider = provider
        self._task_service = task_service
        self._transport = transport
        self._resume_policy = resume_policy
        self._concurrency = concurrency
        self._timeout = timeout
        self._retry_policy = retry_policy
        self._rate_limiter = rate_limiter
        self._queue_capacity = queue_capacity
        self._batch_size = batch_size
        self._progress_interval = progress_interval_seconds
        self._logger = logger or _logger
        #: 当前在跑的服务实例（``request_stop`` 要打到它身上）
        self._service: Any = None
        #: 是否已经请求停下（含"还没起服务就点了暂停"的情况）
        self._stop_requested = False

    @property
    def stop_requested(self) -> bool:
        """是否已请求停下。"""
        return self._stop_requested

    def request_stop(self, *, pause: bool = True) -> None:
        """请求安全停下当前任务。

        与 :meth:`app.ui.download_worker.DownloadWorker.cancel` 同一路数：
        引擎未就绪时先把意图记下来，等第一次进度回调落地 —— 那一帧恰好发生在
        闸门与取消事件都就绪之后。
        """
        self._stop_requested = True
        service = self._service
        if service is not None:
            service.cancel()

    def _make_service(self) -> Any:
        """按 settings 默认值建一个下载服务（与单任务下载同一套参数）。"""
        from app.config.settings import (
            DEFAULT_DOWNLOAD_CONCURRENCY,
            DEFAULT_MBTILES_BATCH_SIZE,
            DEFAULT_MBTILES_QUEUE_CAPACITY,
            DEFAULT_REQUEST_TIMEOUT,
            PROGRESS_EMIT_INTERVAL_SECONDS,
        )
        from app.services import MapDownloadService

        return MapDownloadService(
            self._provider,
            concurrency=(
                DEFAULT_DOWNLOAD_CONCURRENCY
                if self._concurrency is None
                else self._concurrency
            ),
            timeout=DEFAULT_REQUEST_TIMEOUT if self._timeout is None else self._timeout,
            retry_policy=self._retry_policy,
            rate_limiter=self._rate_limiter,
            queue_capacity=(
                DEFAULT_MBTILES_QUEUE_CAPACITY
                if self._queue_capacity is None
                else self._queue_capacity
            ),
            batch_size=(
                DEFAULT_MBTILES_BATCH_SIZE
                if self._batch_size is None
                else self._batch_size
            ),
            progress_interval_seconds=(
                PROGRESS_EMIT_INTERVAL_SECONDS
                if self._progress_interval is None
                else self._progress_interval
            ),
            transport=self._transport,
        )

    async def run(
        self,
        task: Any,
        *,
        token: str = "",
        on_progress: Callable[[Any], None] | None = None,
        task_service: "TaskService | None" = None,
    ) -> PlanTaskOutcome:
        """跑一条任务（走既有的 :meth:`MapDownloadService.resume_task`）。"""
        from app.mbtiles import MBTilesWriteError
        from app.services import MapResumeError

        # 每次 run 都从"没被请求停下"开始：上一次的停止意图属于上一条任务，
        # 带进这一次会让"暂停后再继续"立刻又停一次。
        self._stop_requested = False
        service = self._make_service()
        self._service = service
        try:
            outcome = await service.resume_task(
                task,
                token,
                on_progress=on_progress,
                task_service=task_service if task_service is not None else self._task_service,
                resume_policy=self._resume_policy,
            )
        except (MBTilesWriteError, MapResumeError) as exc:
            # 这两种异常的 message 本身就是给用户看的（写盘失败 / 无法安全续传）
            return PlanTaskOutcome(
                status=PlanItemRunStatus.FAILED,
                total_tiles=int(getattr(task, "total_tiles", 0) or 0),
                completed_tiles=int(getattr(task, "completed_tiles", 0) or 0),
                error=str(exc),
                stopped=self._stop_requested,
            )
        except Exception as exc:  # noqa: BLE001 - 单条任务失败不该带走整份计划
            self._logger.exception("计划条目执行异常 task=%s", getattr(task, "id", "?"))
            return PlanTaskOutcome(
                status=PlanItemRunStatus.FAILED,
                total_tiles=int(getattr(task, "total_tiles", 0) or 0),
                completed_tiles=int(getattr(task, "completed_tiles", 0) or 0),
                error=f"下载异常：{exc}",
                stopped=self._stop_requested,
            )
        finally:
            self._service = None

        if outcome.cancelled:
            # 被"停止请求"打断。是暂停还是取消，由调度器结合自己的意图决定。
            return PlanTaskOutcome(
                status=PlanItemRunStatus.CANCELLED,
                completed_tiles=int(getattr(outcome, "tile_count", 0) or 0),
                total_tiles=int(getattr(outcome, "target_tiles", 0) or 0),
                downloaded_bytes=int(getattr(outcome, "file_size", 0) or 0),
                stopped=True,
            )
        if outcome.is_success:
            # Spec §23：完成判定用既有的那一条（校验通过 + 零失败 + 张数达标），
            # 本层不另立一套"我觉得它下完了"。
            return PlanTaskOutcome(
                status=PlanItemRunStatus.COMPLETED,
                completed_tiles=int(getattr(outcome, "tile_count", 0) or 0),
                total_tiles=int(getattr(outcome, "target_tiles", 0) or 0),
                downloaded_bytes=int(getattr(outcome, "file_size", 0) or 0),
            )
        return PlanTaskOutcome(
            status=PlanItemRunStatus.FAILED,
            completed_tiles=int(getattr(outcome, "tile_count", 0) or 0),
            total_tiles=int(getattr(outcome, "target_tiles", 0) or 0),
            downloaded_bytes=int(getattr(outcome, "file_size", 0) or 0),
            error=outcome.failure_text or "下载未完成",
            stopped=self._stop_requested,
        )


class PlanScheduler:
    """把一份计划的条目**按顺序**跑完（Spec §4）。

    典型用法（全部在同一个事件循环里）::

        scheduler = PlanScheduler(plan_service, runner, task_service=task_service)
        await scheduler.start(plan, token=token, on_progress=...)
        await scheduler.pause()      # 用户点了暂停
        await scheduler.resume()     # 用户点了继续（复用 MBTiles Resume）
        await scheduler.cancel()     # 用户点了取消

    Attributes:
        plan_service: 计划编排入口（建任务用，Spec §6）。
        runner: 单任务执行者。
        task_service: 任务服务（查任务、落盘进度）。
        store: 执行记录持久化（Spec §19）。
    """

    def __init__(
        self,
        plan_service: "PlanService",
        runner: PlanTaskRunner,
        *,
        task_service: "TaskService | None" = None,
        store: PlanExecutionStore | None = None,
        save_interval_seconds: float = DEFAULT_PLAN_SAVE_INTERVAL_SECONDS,
        save_tile_interval: int = DEFAULT_PLAN_SAVE_TILE_INTERVAL,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            plan_service: 计划编排入口。
            runner: 单任务执行者（真实实现是 :class:`MapDownloadTaskRunner`）。
            task_service: 任务服务；``None`` 时取 ``plan_service.task_service``。
            store: 执行记录存储；``None`` 时用默认路径。
            save_interval_seconds / save_tile_interval: 进度落盘节流（Spec §19）。
            logger: 可选日志器。
        """
        self._plan_service = plan_service
        self._runner = runner
        self._task_service = (
            task_service if task_service is not None else plan_service.task_service
        )
        self._store = store if store is not None else PlanExecutionStore()
        self._save_interval = max(0.0, float(save_interval_seconds))
        self._save_tile_interval = max(1, int(save_tile_interval))
        self._logger = logger or _logger

        self._plan: DownloadPlan | None = None
        self._execution: PlanExecution | None = None
        self._progress: PlanProgress | None = None

        self._running = False
        self._pause_requested = False
        self._cancel_requested = False
        #: 恢复检查发现"有条目对不上"时为 ``True`` —— 本轮不发任何请求（Spec §21）
        self._recovery_blocked = False

        self._token = ""
        self._on_progress: Callable[[PlanProgress], None] | None = None
        self._on_item_changed: Callable[[PlanExecution], None] | None = None
        self._on_log: Callable[[str], None] | None = None

        #: 当前任务那一帧的原始数字（Spec §14：计划速度 = 当前任务速度）
        self._current_percent = 0.0
        self._current_speed = 0.0
        #: 已完成条目的**实测**字节数（``file_size``），用来算 downloaded_bytes
        self._bytes_by_task: dict[str, int] = {}
        self._last_save_at = 0.0
        self._last_save_tiles = 0

    # ------------------------------------------------------------------ #
    # 只读
    # ------------------------------------------------------------------ #
    @property
    def execution(self) -> PlanExecution | None:
        """当前执行记录（还没开始过则为 ``None``）。"""
        return self._execution

    @property
    def progress(self) -> PlanProgress | None:
        """最近一帧聚合进度。"""
        return self._progress

    @property
    def is_running(self) -> bool:
        """是否正在跑（有协程在 ``_run_loop`` 里）。"""
        return self._running

    @property
    def pause_requested(self) -> bool:
        """是否已请求暂停（含引擎尚未响应）。"""
        return self._pause_requested

    @property
    def cancel_requested(self) -> bool:
        """是否已请求取消。"""
        return self._cancel_requested

    @property
    def plan(self) -> DownloadPlan | None:
        """当前计划。"""
        return self._plan

    @property
    def task_service(self) -> "TaskService":
        """任务服务。"""
        return self._task_service

    @property
    def store(self) -> PlanExecutionStore:
        """执行记录存储。"""
        return self._store

    # ------------------------------------------------------------------ #
    # 启动 / 继续 / 暂停 / 取消
    # ------------------------------------------------------------------ #
    async def start(
        self,
        plan: DownloadPlan,
        *,
        token: str = "",
        on_progress: Callable[[PlanProgress], None] | None = None,
        on_item_changed: Callable[[PlanExecution], None] | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> PlanExecution:
        """开始执行一份计划（Spec §4 / §6）。

        两种入口合成一个方法，是因为它们在用户眼里是同一个动作（「开始计划」）：

        - 磁盘上有一份**没跑完**的执行记录 → 接管它继续（这就是程序重启后的
          Spec §20 流程）；
        - 没有（或上次已经跑完）→ 按 Spec §6 建一套新任务，从头跑。

        Args:
            plan: 计划（本层不复制它，只是持有引用 —— 计划是配置的唯一来源）。
            token: 天地图 Token（只在内存里过一道，绝不落盘，Spec §19）。
            on_progress: 聚合进度回调（已在帧率上被引擎节流）。
            on_item_changed: 条目状态变化回调（载荷是执行记录）。
            on_log: 一行日志回调（Spec §24 的那些关键事件）。

        Returns:
            结束时的执行记录。

        Raises:
            PlanSchedulerError: 计划为空 / 还没估算 / 任务记录建不出来。
        """
        self._plan = plan
        self._token = token
        self._on_progress = on_progress
        self._on_item_changed = on_item_changed
        self._on_log = on_log

        if self._running:
            # 幂等：已经在跑了，"再点一次开始"不该变成两份执行。
            assert self._execution is not None
            return self._execution

        self._prepare(plan)
        if self._execution is None:  # pragma: no cover - _prepare 保证非空
            raise PlanSchedulerError("计划没有可执行的条目")
        if self._recovery_blocked:
            # 恢复时发现有条目对不上：本轮一个请求都不发（Spec §21 / §29）。
            return self._execution
        return await self._run_loop()

    async def resume(self) -> PlanExecution:
        """继续执行（Spec §4 / §9）。

        从**当前暂停的那一条**接着跑：那条的 ``task_id`` 还在，于是走
        :meth:`MapDownloadService.resume_task` —— 已有瓦片被跳过，只补差集。
        """
        execution = self._require_execution()
        if self._running:
            return execution
        if execution.status is PlanExecutionStatus.CANCELLED:
            self._log("计划已经取消，不能继续：请重新「开始计划」")
            return execution
        self._pause_requested = False
        self._cancel_requested = False
        self._log("用户点了「继续计划」")
        return await self._run_loop()

    async def pause(self) -> PlanExecution:
        """暂停（Spec §8）。

        行为：请求当前任务**安全停下**（引擎 drain 写队列 → flush → 关库），
        等它真的返回后，本层把任务记录从引擎写的 ``CANCELLED`` 改回 ``PAUSED``，
        并把当前条目与整份计划都标成 ``PAUSED``。后续 ``WAITING`` 条目不启动。

        **不强杀线程**（Spec §8 末句）：这里只是设一个意图 + 通知引擎，
        真正的收尾在 :meth:`_run_loop` 里按正常路径走完。
        """
        execution = self._require_execution()
        if not self._running:
            # 已经停在 PAUSED 上了：再点一次暂停什么都不用做。
            self._log("计划当前没有在跑，忽略暂停")
            return execution
        self._pause_requested = True
        self._runner.request_stop(pause=True)
        self._log("用户点了「暂停计划」，正在安全停下当前任务…")
        return execution

    async def cancel(self) -> PlanExecution:
        """取消（Spec §10）。

        当前任务安全停下、**保留已经写入的 partial MBTiles**，后续 ``WAITING``
        条目一律标成 ``CANCELLED``，计划变成 ``CANCELLED``。

        **绝不删除任何东西**：已完成的地图、半截的 MBTiles、任务记录全部保留
        （Spec §10 后两句）。
        """
        execution = self._require_execution()
        self._cancel_requested = True
        self._pause_requested = False
        self._log("用户点了「取消计划」")
        if self._running:
            self._runner.request_stop(pause=False)
            return execution
        # 不在跑（暂停中 / 停在失败上）：直接收尾，不需要等引擎。
        self._sweep_remaining(PlanItemRunStatus.CANCELLED, redo_paused=True)
        execution.set_status(PlanExecutionStatus.CANCELLED)
        self._refresh(immediate=True)
        self._log("计划已取消：已完成的成果与半截的 MBTiles 都保留在磁盘上")
        return execution

    async def retry_current(self) -> PlanExecution:
        """重试当前失败的那一条（Spec §11）。

        "使用 Resume" —— 也就是把那条退回"该跑"，让 :meth:`_run_loop` 重新
        走一遍 ``resume_task``（已有瓦片跳过）。
        """
        execution = self._require_execution()
        index = self._blocking_index(execution)
        if index < 0:
            self._log("当前没有失败的条目，忽略「重试当前」")
            return execution
        execution.reset_for_retry(index)
        self._pause_requested = False
        self._cancel_requested = False
        self._log(f"用户点了「重试当前」：第 {index + 1} 项重新开始（跳过已有瓦片）")
        self._refresh(immediate=True)
        return await self._run_loop()

    async def skip_current(self) -> PlanExecution:
        """跳过当前失败的那一条并继续（Spec §11）。

        当前条目 → ``SKIPPED``，然后启动下一个 ``WAITING`` 条目。
        """
        execution = self._require_execution()
        index = self._blocking_index(execution)
        if index < 0:
            self._log("当前没有失败的条目，忽略「跳过并继续」")
            return execution
        execution.mark(index, PlanItemRunStatus.SKIPPED)
        execution.current_item_index = index
        self._pause_requested = False
        self._cancel_requested = False
        self._log(f"用户点了「跳过并继续」：第 {index + 1} 项已跳过")
        self._refresh(immediate=True)
        return await self._run_loop()

    # ------------------------------------------------------------------ #
    # 接入 / 恢复
    # ------------------------------------------------------------------ #
    def load_stored_execution(self, plan: DownloadPlan) -> PlanExecution | None:
        """把磁盘上这份计划的执行记录读进来（并把遗留的 ``RUNNING`` 降级）。

        供界面在**启动时**调用：拿到 ``PAUSED`` 的记录就显示"上次计划未完成"，
        不会自动开始（Spec §20）。
        """
        execution, _changed = self._store.load_for_recovery(plan.id)
        if execution is not None:
            self._plan = plan
            self._execution = execution
        return execution

    def attach(self, plan: DownloadPlan, execution: PlanExecution) -> None:
        """直接接管一份执行记录（测试与界面「继续」用）。"""
        self._plan = plan
        self._execution = execution

    # ------------------------------------------------------------------ #
    # 内部：准备
    # ------------------------------------------------------------------ #
    def _prepare(self, plan: DownloadPlan) -> None:
        """定出这次要跑的执行记录：接管旧的，或者建一套新的（Spec §6 / §7）。"""
        self._recovery_blocked = False
        stored = self._execution
        if stored is not None and stored.plan_id != plan.id:
            stored = None
        if stored is None:
            stored, _changed = self._store.load_for_recovery(plan.id)
        if stored is not None and not stored.status.is_terminal and stored.items:
            self._execution = stored
            self._log(
                f"接管未完成的计划执行：{stored.status.display_name} "
                f"{stored.items_text}（不会自动开始下载，按本轮继续）"
            )
            self._recovery_blocked = not self._verify_recoverable(plan, stored)
            return

        if plan.needs_estimate():
            raise PlanSchedulerError(
                "计划还没估算，无法开始：先点「重新估算」算出每个区域的张数"
                "（进度需要它作为分母）"
            )
        if not plan.ordered_enabled_items():
            raise PlanSchedulerError("计划里没有一个启用且可用的区域，无法开始")

        self._execution = self._create_fresh_execution(plan)

    def _create_fresh_execution(self, plan: DownloadPlan) -> PlanExecution:
        """按 Spec §6 建任务并造一份全新执行记录。"""
        from app.planning.plan_service import PlanTaskCreationError

        try:
            tasks = self._plan_service.create_download_tasks()
        except PlanTaskCreationError as exc:
            raise PlanSchedulerError(str(exc)) from exc

        items = plan.ordered_enabled_items()
        if len(tasks) != len(items):  # pragma: no cover - 服务层保证同序同数
            raise PlanSchedulerError(
                f"生成的任务数与计划条目数不一致（{len(tasks)} != {len(items)}），"
                "为避免下错区域，本次不执行"
            )
        execution = new_execution_for(plan, tasks)
        self._bytes_by_task.clear()
        self._store.save(execution)
        self._log(
            f"开始执行计划「{plan.name or plan.id}」："
            f"{len(items)} 项 / {execution.total_tiles:,} 张，按层级顺序串行执行"
        )
        return execution

    def _verify_recoverable(self, plan: DownloadPlan, execution: PlanExecution) -> bool:
        """恢复前的一致性检查（Spec §21 / §22）。

        逐条验四件事：计划里还有这一项吗、任务记录还在吗、任务参数还对得上吗、
        （已完成的那条）输出文件还在吗。任何一条不过 → 那条标 ``FAILED``，
        整份计划 ``PARTIAL_FAILED``，并且**本轮不启动任何下载** ——
        用户看到问题、自己决定重试还是跳过（Spec §11 / §29）。

        不在本层重复实现的检查：任务参数与**已有 MBTiles** 的比对（bounds /
        zoom / 图层）由 :meth:`MapDownloadService.resume_task` 负责 ——
        它才是那个知道 MBTiles 里到底写了什么的层（Spec §5 禁止复制）。本层
        只验"计划与任务记录之间"这一层，两者互不重叠。

        Returns:
            ``True`` 表示可以继续跑；``False`` 表示已经标了失败、本轮不该启动。
        """
        lookup = plan_region_lookup(plan)
        problems: list[tuple[int, str]] = []
        for index, entry in enumerate(execution.items):
            plan_item = lookup.get(entry.plan_item_id)
            if plan_item is None:
                problems.append((index, "计划里已经没有这一项了（计划被改过）"))
                continue
            task = (
                self._task_service.get_task(entry.task_id) if entry.task_id else None
            )
            if task is None:
                problems.append((index, "找不到对应的下载任务记录"))
                continue
            mismatch = _task_plan_mismatch(plan, plan_item, task)
            if mismatch:
                problems.append((index, mismatch))
                continue
            if entry.status is PlanItemRunStatus.COMPLETED:
                output = getattr(task, "output_path", None)
                if output is None or not _path_exists(output):
                    problems.append(
                        (index, "记录显示已完成，但输出文件不存在，无法确认成果")
                    )
        if not problems:
            return True

        for index, reason in problems:
            execution.mark(index, PlanItemRunStatus.FAILED, reason)
            self._logger.warning(
                "计划条目无法恢复 plan=%s index=%d：%s", plan.id, index, reason
            )
        execution.set_status(PlanExecutionStatus.PARTIAL_FAILED)
        self._execution = execution
        self._refresh(immediate=True)
        self._log(
            f"上次的计划执行有 {len(problems)} 项无法恢复，已停在"
            f"「部分失败」：{problems[0][1]}"
        )
        return False

    # ------------------------------------------------------------------ #
    # 内部：主循环
    # ------------------------------------------------------------------ #
    async def _run_loop(self) -> PlanExecution:
        """串行跑完该跑的条目（Spec §4 / §11）。"""
        execution = self._require_execution()
        plan = self._require_plan()

        self._running = True
        self._pause_requested = False
        self._cancel_requested = False
        self._current_percent = 0.0
        # Spec §14：任务切换时速度归零，别让上一个任务的速度挂在界面上。
        self._current_speed = 0.0
        execution.set_status(PlanExecutionStatus.RUNNING)
        self._refresh(immediate=True)
        try:
            while True:
                if self._cancel_requested:
                    self._sweep_remaining(
                        PlanItemRunStatus.CANCELLED, redo_paused=True
                    )
                    execution.set_status(PlanExecutionStatus.CANCELLED)
                    self._log("计划已取消：已完成的成果与半截的 MBTiles 都保留在磁盘上")
                    break
                if self._pause_requested:
                    execution.set_status(PlanExecutionStatus.PAUSED)
                    self._log(
                        f"计划已暂停在第 {execution.current_item_index + 1} 项："
                        "点「继续计划」会从这一项接着下（跳过已有瓦片）"
                    )
                    break

                action, index = self._next_step(execution)
                if action == "done":
                    execution.set_status(self._final_status(execution))
                    self._log(_final_message(execution, plan))
                    break
                if action == "blocked":
                    execution.current_item_index = index
                    execution.set_status(PlanExecutionStatus.PARTIAL_FAILED)
                    self._log(
                        f"计划停在「部分失败」：第 {index + 1} 项"
                        f"（{execution.items[index].error or '执行未完成'}）。"
                        "可以用「重试当前」或「跳过并继续」"
                    )
                    break

                if not await self._run_item(plan, execution, index):
                    # 先看是不是"被暂停/取消打断" —— 那两种情况的收尾在循环开头，
                    # 而且状态必须落成 PAUSED / CANCELLED 而不是"部分失败"。
                    if self._cancel_requested or self._pause_requested:
                        continue
                    execution.current_item_index = index
                    execution.set_status(PlanExecutionStatus.PARTIAL_FAILED)
                    self._log(
                        f"计划停在「部分失败」：第 {index + 1} 项"
                        f"（{execution.items[index].error or '执行未完成'}）。"
                        "可以用「重试当前」或「跳过并继续」"
                    )
                    break
        finally:
            self._running = False
            self._current_speed = 0.0
            self._refresh(immediate=True)
        return execution

    def _next_step(self, execution: PlanExecution) -> tuple[str, int]:
        """决定下一步：``("run", i)`` / ``("blocked", i)`` / ``("done", -1)``。

        规则（Spec §11）：

        - ``COMPLETED`` / ``SKIPPED`` / ``CANCELLED`` → **跨过去**。
          后两者是用户自己的决定，不是"没跑成"。
        - ``FAILED`` → **停在它那里**（``blocked``）：不自动忽略、不自动往下走。
        - ``WAITING`` / ``PAUSED`` → 跑它（``PAUSED`` 就是"接着跑这一条"）。
        """
        for index, item in enumerate(execution.items):
            if item.status is PlanItemRunStatus.FAILED:
                return ("blocked", index)
            if item.status.is_pending:
                return ("run", index)
        return ("done", -1)

    async def _run_item(
        self, plan: DownloadPlan, execution: PlanExecution, index: int
    ) -> bool:
        """跑一条条目。返回 ``True`` 表示"可以继续下一条"。"""
        entry = execution.items[index]
        plan_item = plan_region_lookup(plan).get(entry.plan_item_id)
        if plan_item is None:
            execution.mark(index, PlanItemRunStatus.FAILED, "计划里已经没有这一项了")
            self._log_item(plan, execution, index, "失败：计划里已经没有这一项了")
            return False

        task = self._task_service.get_task(entry.task_id) if entry.task_id else None
        if task is None:
            execution.mark(index, PlanItemRunStatus.FAILED, "找不到对应的下载任务记录")
            self._log_item(plan, execution, index, "失败：找不到对应的下载任务记录")
            return False

        # Spec §7 / §22：任务已经是 COMPLETED 的，**绝不重新下载**。
        if _task_completed(task):
            output = getattr(task, "output_path", None)
            if output is None or not _path_exists(output):
                execution.mark(
                    index,
                    PlanItemRunStatus.FAILED,
                    "任务记录已完成，但输出文件不存在，无法确认成果",
                )
                self._log_item(
                    plan, execution, index, "失败：记录已完成但输出文件不存在"
                )
                return False
            execution.mark(index, PlanItemRunStatus.COMPLETED)
            self._bytes_by_task[entry.task_id] = _file_size(output)
            self._log_item(plan, execution, index, "已完成过，跳过重新下载")
            return True

        execution.current_item_index = index
        execution.mark(index, PlanItemRunStatus.RUNNING)
        self._current_percent = 0.0
        self._current_speed = 0.0
        self._refresh(immediate=True)
        self._log_item(plan, execution, index, "开始")

        try:
            outcome = await self._runner.run(
                task,
                token=self._token,
                on_progress=self._on_task_progress,
                task_service=self._task_service,
            )
        except asyncio.CancelledError:  # pragma: no cover - 由外层取消语义决定
            raise
        except Exception as exc:  # noqa: BLE001 - 单条任务的异常不许带走整份计划
            # Spec §5 / §24：一条任务炸了只记它自己 —— 计划停在它上面，
            # 用户可以「重试当前」或「跳过并继续」。
            text = f"任务执行异常：{exc}" if str(exc) else "任务执行异常"
            self._logger.warning(
                "计划条目执行异常 plan=%s index=%d task=%s：%s",
                plan.id,
                index,
                entry.task_id,
                exc,
            )
            self._fail_underlying_task(task, text)
            execution.mark(index, PlanItemRunStatus.FAILED, text)
            self._log_item(plan, execution, index, f"失败：{text}")
            return False

        if outcome.stopped:
            if self._cancel_requested:
                execution.mark(index, PlanItemRunStatus.CANCELLED)
                self._log_item(plan, execution, index, "已取消（半截的成果保留）")
            else:
                execution.mark(index, PlanItemRunStatus.PAUSED)
                # 引擎在取消路径上把任务记成了 CANCELLED；用户点的是"暂停"，
                # 记录必须如实反映"可以继续"（Spec §8 / §9）。
                self._rewrite_task_status(task, pause=True)
                self._log_item(plan, execution, index, "已暂停（已写入的瓦片保留）")
            self._refresh(immediate=True)
            return False

        self._bytes_by_task[entry.task_id] = int(outcome.downloaded_bytes or 0)
        if outcome.status is PlanItemRunStatus.COMPLETED:
            execution.mark(index, PlanItemRunStatus.COMPLETED)
            self._log_item(plan, execution, index, "完成")
            return True

        execution.mark(
            index, PlanItemRunStatus.FAILED, outcome.error or "下载未完成"
        )
        self._log_item(
            plan, execution, index, f"失败：{outcome.error or '下载未完成'}"
        )
        return False

    def _sweep_remaining(
        self, status: PlanItemRunStatus, *, redo_paused: bool = False
    ) -> None:
        """把还没跑的条目一律标成 ``status``（取消 / 收尾用）。

        Args:
            status: 要落成的条目状态。
            redo_paused: 真时连 ``PAUSED`` 的一起改。取消计划时必须为真 ——
                取消是终态，记录里留一个 ``PAUSED`` 等于对用户撒谎
                （"还能继续"），而 :meth:`resume` 其实已经拒绝继续了
                （Spec §10）。
        """
        execution = self._execution
        if execution is None:  # pragma: no cover - 防御性分支
            return
        for index, item in enumerate(execution.items):
            if item.status is PlanItemRunStatus.WAITING:
                execution.mark(index, status)
            elif redo_paused and item.status is PlanItemRunStatus.PAUSED:
                execution.mark(index, status)

    @staticmethod
    def _final_status(execution: PlanExecution) -> PlanExecutionStatus:
        """全部条目跑完之后计划是什么状态（Spec §11）。"""
        if execution.unresolved_count:
            return PlanExecutionStatus.PARTIAL_FAILED
        return PlanExecutionStatus.COMPLETED

    def _blocking_index(self, execution: PlanExecution) -> int:
        """当前"卡住"的那一条下标（第一个 ``FAILED``）；没有返回 ``-1``。"""
        for index, item in enumerate(execution.items):
            if item.status is PlanItemRunStatus.FAILED:
                return index
        return -1

    # ------------------------------------------------------------------ #
    # 内部：状态与进度
    # ------------------------------------------------------------------ #
    def _rewrite_task_status(self, task: Any, *, pause: bool) -> None:
        """把任务记录从引擎写的 ``CANCELLED`` 改写成"暂停"（Spec §8）。

        写盘失败只记日志：引擎已经把瓦片安全落盘了，不能因为一个 JSON 写不动
        就把"暂停"判成失败。
        """
        if not pause:  # pragma: no cover - 引擎写的 CANCELLED 正是我们要的
            return
        try:
            self._task_service.pause_task(task)
        except Exception as exc:  # noqa: BLE001 - 磁盘问题不该带走整份计划
            self._logger.error("把任务状态改回「已暂停」失败 task=%s：%s", task.id, exc)

    def _fail_underlying_task(self, task: Any, text: str) -> None:
        """把任务记录标成失败（尽力而为，写不动只记日志）。

        引擎正常路径上自己会写；这条是给"引擎/执行者直接抛异常"那条路兜底的 ——
        否则任务记录会永远停在 ``created``，界面上看不出到底怎么了。
        """
        try:
            self._task_service.fail_task(task, text)
        except Exception as exc:  # noqa: BLE001 - 磁盘问题不该带走整份计划
            self._logger.error("把任务状态标成「失败」失败 task=%s：%s", task.id, exc)

    def _on_task_progress(self, progress: Any) -> None:
        """单任务进度回调（在调度器所在的事件循环里执行）。"""
        self._current_percent = float(getattr(progress, "percent", 0.0) or 0.0)
        self._current_speed = float(
            getattr(progress, "tiles_per_second", 0.0) or 0.0
        )
        self._refresh(immediate=False)

    def _build_progress(self, message: str = "") -> PlanProgress:
        """聚合当前进度（Spec §12 / §13）。"""
        plan = self._require_plan()
        execution = self._require_execution()
        return build_progress(
            plan,
            execution,
            tasks=self._tasks_by_id(),
            current_task_percent=self._current_percent,
            current_speed_tiles_per_second=self._current_speed,
            downloaded_bytes_by_task=self._bytes_by_task,
            message=message,
        )

    def _tasks_by_id(self) -> dict[str, Any]:
        """``{task_id: 任务}``（只查这次执行真的引用的那些）。"""
        execution = self._execution
        if execution is None:  # pragma: no cover - 防御性分支
            return {}
        result: dict[str, Any] = {}
        for entry in execution.items:
            if not entry.task_id or entry.task_id in result:
                continue
            task = self._task_service.get_task(entry.task_id)
            if task is not None:
                result[entry.task_id] = task
        return result

    def _refresh(self, *, immediate: bool) -> None:
        """更新聚合数字、按节流落盘、把这一帧推给界面（Spec §13 / §19）。"""
        execution = self._execution
        if execution is None or self._plan is None:  # pragma: no cover - 防御性分支
            return
        progress = self._build_progress()
        self._progress = progress
        execution.total_tiles = progress.total_tiles
        execution.completed_tiles = progress.completed_tiles
        execution.downloaded_bytes = progress.downloaded_bytes

        if immediate or self._should_save():
            self._save()
        if immediate and self._on_item_changed is not None:
            self._on_item_changed(execution)
        if self._on_progress is not None:
            self._on_progress(progress)

    def _should_save(self) -> bool:
        """进度落盘节流：满足任一即写（与任务记录同一套规则）。"""
        now = time.monotonic()
        if now - self._last_save_at >= self._save_interval:
            return True
        execution = self._execution
        if execution is None:  # pragma: no cover - 防御性分支
            return False
        return execution.completed_tiles - self._last_save_tiles >= self._save_tile_interval

    def _save(self) -> None:
        """写执行记录（失败只记日志：磁盘问题不该让调度停下来）。"""
        execution = self._execution
        if execution is None:  # pragma: no cover - 防御性分支
            return
        try:
            self._store.save(execution)
        except (PlanExecutionStoreError, OSError) as exc:
            self._logger.error("写入计划执行记录失败：%s", exc)
            return
        self._last_save_at = time.monotonic()
        self._last_save_tiles = execution.completed_tiles

    def _log(self, message: str) -> None:
        """关键事件日志（Spec §24）。**每张瓦片都不打**（那是引擎的事）。"""
        self._logger.info("计划调度：%s", message)
        if self._on_log is not None:
            self._on_log(message)

    def _log_item(
        self,
        plan: DownloadPlan,
        execution: PlanExecution,
        index: int,
        tail: str,
    ) -> None:
        """条目级日志：``[2/3] 浙江省（省级）Z9~13 开始``。"""
        entry = execution.items[index]
        plan_item = plan_region_lookup(plan).get(entry.plan_item_id)
        head = (
            plan_item.describe()
            if plan_item is not None
            else entry.plan_item_id
        )
        self._log(f"[{index + 1}/{len(execution.items)}] {head} {tail}")

    # ------------------------------------------------------------------ #
    # 内部：前置条件
    # ------------------------------------------------------------------ #
    def _require_execution(self) -> PlanExecution:
        execution = self._execution
        if execution is None:
            raise PlanSchedulerError("还没有开始过这份计划：请先「开始计划」")
        return execution

    def _require_plan(self) -> DownloadPlan:
        plan = self._plan
        if plan is None:
            raise PlanSchedulerError("调度器还没有绑定计划：请先「开始计划」")
        return plan


# --------------------------------------------------------------------------- #
# 纯函数辅助
# --------------------------------------------------------------------------- #
def _path_exists(path: object) -> bool:
    """``Path.exists()`` 的容错版（输出路径可能是个字符串 / 不可访问）。"""
    from pathlib import Path

    try:
        return Path(str(path)).exists()
    except OSError:  # pragma: no cover - 权限 / 超长路径
        return False


def _file_size(path: object) -> int:
    """文件大小（拿不到就返回 0：体积只用于展示，不该让流程出错）。"""
    from pathlib import Path

    try:
        return int(Path(str(path)).stat().st_size)
    except OSError:  # pragma: no cover - 文件刚被删 / 无权限
        return 0


def _task_completed(task: Any) -> bool:
    """任务是否已经完成（用枚举的 ``value`` 比，不 import 模型）。

    ``app.models.download_task`` 是依赖图的叶子，本模块引它也不会成环；
    但这里用 ``value`` 比较是**刻意**的：它同时容忍"传进来的是个鸭子类型"
    （测试里的假任务），而语义完全一样。
    """
    status = getattr(task, "status", None)
    return getattr(status, "value", status) == "completed"


def _task_plan_mismatch(
    plan: DownloadPlan, plan_item: DownloadPlanItem, task: Any
) -> str:
    """任务记录与计划条目对不上的第一处（Spec §21 的"参数匹配"）。

    只比本层能看到的那几样（缩放区间 / 图层 / 注记）：与**已有 MBTiles** 的
    比对不在这里 —— 那是 :meth:`MapDownloadService.resume_task` 的职责，
    它才知道文件里写了什么（Spec §5）。
    """
    task_min = int(getattr(task, "min_zoom", plan_item.min_zoom) or 0)
    task_max = int(getattr(task, "max_zoom", plan_item.max_zoom) or 0)
    if (task_min, task_max) != (plan_item.min_zoom, plan_item.max_zoom):
        return (
            f"计划里的缩放范围已改变（计划 Z{plan_item.min_zoom}~{plan_item.max_zoom}，"
            f"任务 Z{task_min}~{task_max}）"
        )
    task_labels = bool(getattr(task, "with_labels", False))
    if task_labels != bool(plan.with_labels):
        return "计划里的注记设置已改变，无法安全继续同一个文件"
    task_layer = str(getattr(task, "map_layer", "") or "")
    plan_layer = str(plan.map_layer or "")
    if plan_layer and task_layer and plan_layer != task_layer:
        return f"计划里的图层已改变（计划 {plan_layer}，任务 {task_layer}）"
    return ""


def _final_message(execution: PlanExecution, plan: DownloadPlan) -> str:
    """全部跑完时的一句话（Spec §24 的 "Plan completed"）。"""
    name = plan.name or plan.id
    if execution.status is PlanExecutionStatus.COMPLETED:
        return (
            f"计划「{name}」全部完成：{execution.completed_count} / "
            f"{len(execution.items)} 项，{execution.completed_tiles:,} 张"
        )
    return (
        f"计划「{name}」结束：完成 {execution.completed_count} 项、"
        f"失败 {execution.failed_count} 项、未取得成果 "
        f"{execution.unresolved_count} 项"
    )
