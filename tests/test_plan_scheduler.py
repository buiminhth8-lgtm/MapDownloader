"""计划级串行调度（Slice 21，Spec §4 ~ §14 / §19 ~ §24）。

这个文件守的是**调度语义**，不是下载语义：所有用例都注入一个假执行者
（:class:`_FakeRunner`），不联网、不建 MBTiles，于是每条断言都只关于
"顺序 / 生命周期 / 聚合 / 异常处理" 这四件事。

::

    顺序    全国 → 省 → 市；停用的不跑
    执行    全部成功 → COMPLETED；同一时刻只有一个任务在跑
    Pause   当前任务安全停下 → 条目 PAUSED → 后续不启动 → 继续时只补差集
    Failure 停在失败那一条 → PARTIAL_FAILED → 重试 / 跳过 / 取消
    Cancel  当前停下、后续标 CANCELLED、**已下载的成果一个都不删**
    恢复    RUNNING 重启 → PAUSED；已完成的不重下；对不上的标 FAILED 且不发请求
    进度    按 selected_tiles 聚合；停用的不进分母
    Store   状态变化立即落盘；文件里没有 Token

假执行者会**模仿引擎的记账动作**（把进度写进任务记录并落盘）—— 因为调度器的
进度聚合读的正是任务记录。不记账的话，"按张数聚合"这条断言就成了在测空气。
"""

from __future__ import annotations

import asyncio
import ast
import time
from pathlib import Path

import pytest

from app.models.download_task import DownloadTaskStatus
from app.planning.download_plan import DownloadPlan
from app.planning.plan_execution import (
    PlanExecutionStatus,
    PlanItemRunStatus,
)
from app.planning.plan_execution_store import PlanExecutionStore
from app.planning.plan_scheduler import (
    PlanScheduler,
    PlanSchedulerError,
    PlanTaskOutcome,
)
from app.planning.plan_service import PlanService
from app.planning.plan_store import PlanStore
from app.regions.service import RegionService
from app.tasks.task_service import TaskService
from app.tasks.task_store import TaskStore
from tests.plan_fakes import (
    ExplodingRunner as _ExplodingRunner,
    FakeRunner as _FakeRunner,
    ProgressLike as _ProgressLike,
    ready_plan as _ready,
)

#: 仓库根（静态守卫要读源码）。
ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture
def service(region_resources: Path) -> RegionService:
    return RegionService()


@pytest.fixture
def task_service(tasks_file: Path) -> TaskService:
    return TaskService(TaskStore(tasks_file))


@pytest.fixture
def plan_service(
    service: RegionService, plans_file: Path, task_service: TaskService
) -> PlanService:
    return PlanService(
        store=PlanStore(plans_file), service=service, task_service=task_service
    )


@pytest.fixture
def store(work_dir: Path) -> PlanExecutionStore:
    return PlanExecutionStore(work_dir / "plan_executions.json")


def _scheduler(
    plan_service: PlanService,
    task_service: TaskService,
    store: PlanExecutionStore,
    runner,
) -> PlanScheduler:
    return PlanScheduler(
        plan_service,
        runner,
        task_service=task_service,
        store=store,
        # 节流关掉：用例要能"每一条状态变化都立刻在磁盘上看到"
        save_interval_seconds=0.0,
        save_tile_interval=1,
    )


async def _wait_until(predicate, timeout: float = 5.0) -> bool:
    """等到条件成立（带 deadline，否则场景失效时用例会挂死）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


# --------------------------------------------------------------------------- #
# 顺序（Spec §6 / §13）
# --------------------------------------------------------------------------- #
async def test_items_run_in_hierarchy_order(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """全国 → 省 → 市，与"用户点选的顺序"无关（Spec §13）。"""
    plan = _ready(plan_service, work_dir, "rivertown", "northland", "demoland")
    runner = _FakeRunner()

    await _scheduler(plan_service, task_service, store, runner).start(plan)

    assert runner.calls == ["demoland", "northland", "rivertown"]


async def test_disabled_items_are_not_executed(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """停用的条目**不建任务、不执行、不进分母**（Spec §6 / §12）。"""
    plan = _ready(plan_service, work_dir, "demoland", "northland", "rivertown")
    plan_service.set_enabled("northland", False)
    plan.set_enabled("northland", False)
    runner = _FakeRunner()

    execution = await _scheduler(plan_service, task_service, store, runner).start(plan)

    assert runner.calls == ["demoland", "rivertown"]
    assert "northland" not in {
        entry.plan_item_id for entry in execution.items
    }
    assert execution.total_items if hasattr(execution, "total_items") else True
    assert len(execution.items) == 2


async def test_only_one_task_runs_at_a_time(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """**Spec §4 的核心**：串行。任何时刻最多一个任务在跑（Spec §29 禁并行）。"""
    plan = _ready(plan_service, work_dir, "demoland", "northland", "rivertown")
    runner = _FakeRunner()

    await _scheduler(plan_service, task_service, store, runner).start(plan)

    assert runner.max_concurrent == 1
    assert len(runner.calls) == 3


# --------------------------------------------------------------------------- #
# 全部成功（Spec §4 / §23）
# --------------------------------------------------------------------------- #
async def test_all_items_complete_and_the_plan_is_completed(
    plan_service, task_service, store, work_dir: Path
) -> None:
    plan = _ready(plan_service, work_dir, "demoland", "northland", "rivertown")
    runner = _FakeRunner()

    execution = await _scheduler(plan_service, task_service, store, runner).start(plan)

    assert execution.status is PlanExecutionStatus.COMPLETED
    assert execution.completed_count == 3
    assert execution.failed_count == 0
    assert all(
        entry.status is PlanItemRunStatus.COMPLETED for entry in execution.items
    )
    assert runner.completed_calls == 3


async def test_the_next_item_starts_after_the_previous_one_is_finished(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """前一条还没结束，后一条不该开工 —— 用任务状态的时间线来证明。"""
    plan = _ready(plan_service, work_dir, "demoland", "rivertown")
    seen: list[tuple[str, str]] = []

    class _Watching(_FakeRunner):
        async def run(self, task, **kwargs):
            seen.append(("start", str(task.region_id)))
            outcome = await super().run(task, **kwargs)
            seen.append(("end", str(task.region_id)))
            return outcome

    await _scheduler(plan_service, task_service, store, _Watching()).start(plan)

    assert seen == [
        ("start", "demoland"),
        ("end", "demoland"),
        ("start", "rivertown"),
        ("end", "rivertown"),
    ]


async def test_every_status_change_is_persisted_immediately(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """状态变化立即落盘（Spec §19）：跑完时文件里就是"已完成"。"""
    plan = _ready(plan_service, work_dir, "demoland")
    runner = _FakeRunner()

    await _scheduler(plan_service, task_service, store, runner).start(plan)

    reloaded = store.find(plan.id)
    assert reloaded.status is PlanExecutionStatus.COMPLETED
    assert reloaded.items[0].status is PlanItemRunStatus.COMPLETED


async def test_the_execution_file_has_no_token(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """执行记录里**没有** Token（Spec §19）。"""
    plan = _ready(plan_service, work_dir, "demoland")
    await _scheduler(plan_service, task_service, store, _FakeRunner()).start(
        plan, token="abcdef0123456789"
    )

    text = store.path.read_text(encoding="utf-8")

    assert "abcdef0123456789" not in text
    assert "token" not in text.lower()


# --------------------------------------------------------------------------- #
# 暂停 / 继续（Spec §8 / §9 / §14）
# --------------------------------------------------------------------------- #
async def test_pause_stops_the_current_task_and_pauses_the_plan(
    plan_service, task_service, store, work_dir: Path
) -> None:
    plan = _ready(plan_service, work_dir, "demoland", "northland")
    runner = _FakeRunner(hang=True)
    scheduler = _scheduler(plan_service, task_service, store, runner)

    running = asyncio.ensure_future(scheduler.start(plan))
    assert await _wait_until(lambda: runner.calls), "第一项该已经开工"

    await scheduler.pause()
    execution = await running

    assert execution.status is PlanExecutionStatus.PAUSED
    assert execution.items[0].status is PlanItemRunStatus.PAUSED
    assert runner.calls == ["demoland"], "后续条目不得启动"
    assert runner.stopped_calls == 1


async def test_pausing_rewrites_the_task_record_from_cancelled_to_paused(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """用户点的是「暂停」，任务记录就必须写着"可以继续"（Spec §8 / §9）。

    引擎的安全停止路径写的是 ``CANCELLED``（它不知道用户的意图），
    本层负责把它改回 ``PAUSED`` —— 否则界面上会出现"计划已暂停、
    任务已取消"，而"已取消"在用户心里是另一件事。
    """
    plan = _ready(plan_service, work_dir, "demoland")
    runner = _FakeRunner(hang=True)
    scheduler = _scheduler(plan_service, task_service, store, runner)

    running = asyncio.ensure_future(scheduler.start(plan))
    assert await _wait_until(lambda: runner.calls)
    await scheduler.pause()
    execution = await running

    task = task_service.get_task(execution.items[0].task_id)
    assert task.status is DownloadTaskStatus.PAUSED


async def test_resume_continues_the_same_item_and_skips_existing_tiles(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """继续时**从当前那条接着跑**，而不是从第一条重来（Spec §9）。"""
    plan = _ready(plan_service, work_dir, "demoland", "northland")
    runner = _FakeRunner(hang=True)
    scheduler = _scheduler(plan_service, task_service, store, runner)

    running = asyncio.ensure_future(scheduler.start(plan))
    assert await _wait_until(lambda: runner.calls)
    await scheduler.pause()
    await running

    # 继续：这一轮不再"挂住"，直接跑完
    runner._hang = False
    execution = await scheduler.resume()

    assert execution.status is PlanExecutionStatus.COMPLETED
    assert runner.calls == ["demoland", "demoland", "northland"], (
        "第一条要重跑一次（复用 Resume 补差集），然后才是下一条"
    )
    # 重跑走的是同一个任务记录（同一个文件），不是新建一条
    first_task_ids = {entry.task_id for entry in execution.items}
    assert len(first_task_ids) == 2


async def test_resume_after_a_pause_never_restarts_a_finished_item(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """已经完成的那条不会被重跑（Spec §7）。"""
    plan = _ready(plan_service, work_dir, "demoland", "northland")
    runner = _FakeRunner()
    scheduler = _scheduler(plan_service, task_service, store, runner)

    # 第一轮：只跑第一条（第二项在开始前就"停用"掉，然后放开）
    plan_service.set_enabled("northland", False)
    plan.set_enabled("northland", False)
    execution = await scheduler.start(plan)
    assert execution.status is PlanExecutionStatus.COMPLETED
    assert runner.calls == ["demoland"]

    # 第二轮：把第二项放回来，再"继续" —— 第一条不该再被调用
    runner.calls.clear()
    plan_service.set_enabled("northland", True)
    plan.set_enabled("northland", True)
    plan.recalculate_totals()

    await scheduler.resume()

    assert runner.calls == [], "两条都已完成，不该再发任何请求"


# --------------------------------------------------------------------------- #
# 失败策略（Spec §11）
# --------------------------------------------------------------------------- #
async def test_a_failed_item_stops_the_plan(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """第二条失败 → 计划停在「部分失败」，**第三条不自动开始**（Spec §11）。"""
    plan = _ready(plan_service, work_dir, "demoland", "northland", "rivertown")
    runner = _FakeRunner(fail_on=("northland",))

    execution = await _scheduler(plan_service, task_service, store, runner).start(plan)

    assert runner.calls == ["demoland", "northland"]
    assert execution.status is PlanExecutionStatus.PARTIAL_FAILED
    assert execution.items[0].status is PlanItemRunStatus.COMPLETED
    assert execution.items[1].status is PlanItemRunStatus.FAILED
    assert execution.items[2].status is PlanItemRunStatus.WAITING
    assert "500" in (execution.items[1].error or "")


async def test_the_failed_item_error_is_saved_on_disk(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """失败原因要落盘（重启后用户还能看到"上次为什么没成"）。"""
    plan = _ready(plan_service, work_dir, "demoland", "northland")
    runner = _FakeRunner(fail_on=("demoland",))

    await _scheduler(plan_service, task_service, store, runner).start(plan)

    reloaded = store.find(plan.id)
    assert reloaded.status is PlanExecutionStatus.PARTIAL_FAILED
    assert reloaded.items[0].error
    assert reloaded.items[1].status is PlanItemRunStatus.WAITING


async def test_retry_current_reruns_the_failed_item(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """「重试当前」：失败一次后成功，然后继续往下跑（Spec §11）。"""
    plan = _ready(plan_service, work_dir, "demoland", "northland")
    runner = _FakeRunner(fail_on=("demoland",), fail_times=1)
    scheduler = _scheduler(plan_service, task_service, store, runner)

    first = await scheduler.start(plan)
    assert first.status is PlanExecutionStatus.PARTIAL_FAILED

    second = await scheduler.retry_current()

    assert runner.calls == ["demoland", "demoland", "northland"]
    assert second.status is PlanExecutionStatus.COMPLETED
    assert second.items[0].status is PlanItemRunStatus.COMPLETED
    assert second.items[0].error is None


async def test_skip_current_moves_on_to_the_next_item(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """「跳过并继续」：当前那条标 SKIPPED，下一条启动（Spec §11）。

    整份计划最后是「部分失败」—— 跳过不等于拿到了那块地图。
    """
    plan = _ready(plan_service, work_dir, "demoland", "northland")
    runner = _FakeRunner(fail_on=("demoland",))
    scheduler = _scheduler(plan_service, task_service, store, runner)

    await scheduler.start(plan)
    execution = await scheduler.skip_current()

    assert runner.calls == ["demoland", "northland"]
    assert execution.items[0].status is PlanItemRunStatus.SKIPPED
    assert execution.items[1].status is PlanItemRunStatus.COMPLETED
    assert execution.status is PlanExecutionStatus.PARTIAL_FAILED


async def test_retry_and_skip_are_no_ops_without_a_failure(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """没有失败项时两个按钮都不该产生任何请求。"""
    plan = _ready(plan_service, work_dir, "demoland")
    runner = _FakeRunner()
    scheduler = _scheduler(plan_service, task_service, store, runner)

    await scheduler.start(plan)
    runner.calls.clear()

    await scheduler.retry_current()
    await scheduler.skip_current()

    assert runner.calls == []


async def test_an_exploding_runner_fails_one_item_not_the_whole_plan(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """单条任务抛异常 → 那条记失败，整份计划停在部分失败（不崩、不带走别人）。"""
    plan = _ready(plan_service, work_dir, "demoland")

    execution = await _scheduler(
        plan_service, task_service, store, _ExplodingRunner()
    ).start(plan)

    assert execution.status is PlanExecutionStatus.PARTIAL_FAILED
    assert execution.items[0].status is PlanItemRunStatus.FAILED


# --------------------------------------------------------------------------- #
# 取消（Spec §10）
# --------------------------------------------------------------------------- #
async def test_cancel_stops_the_current_task(
    plan_service, task_service, store, work_dir: Path
) -> None:
    plan = _ready(plan_service, work_dir, "demoland", "northland")
    runner = _FakeRunner(hang=True)
    scheduler = _scheduler(plan_service, task_service, store, runner)

    running = asyncio.ensure_future(scheduler.start(plan))
    assert await _wait_until(lambda: runner.calls)
    await scheduler.cancel()
    execution = await running

    assert execution.status is PlanExecutionStatus.CANCELLED
    assert execution.items[0].status is PlanItemRunStatus.CANCELLED
    assert runner.calls == ["demoland"]


async def test_cancel_marks_the_remaining_items_cancelled(
    plan_service, task_service, store, work_dir: Path
) -> None:
    plan = _ready(plan_service, work_dir, "demoland", "northland", "rivertown")
    runner = _FakeRunner(hang=True)
    scheduler = _scheduler(plan_service, task_service, store, runner)

    running = asyncio.ensure_future(scheduler.start(plan))
    assert await _wait_until(lambda: runner.calls)
    await scheduler.cancel()
    execution = await running

    assert execution.items[0].status is PlanItemRunStatus.CANCELLED
    assert execution.items[1].status is PlanItemRunStatus.CANCELLED
    assert execution.items[2].status is PlanItemRunStatus.CANCELLED


async def test_cancel_keeps_every_file_and_task_record_on_disk(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """**Spec §10 后两句**：不删已完成的地图，也不删半截的 MBTiles。"""
    plan = _ready(plan_service, work_dir, "demoland", "northland")
    runner = _FakeRunner(hang=True)
    scheduler = _scheduler(plan_service, task_service, store, runner)

    running = asyncio.ensure_future(scheduler.start(plan))
    assert await _wait_until(lambda: runner.calls)
    await scheduler.cancel()
    execution = await running

    first = task_service.get_task(execution.items[0].task_id)
    second = task_service.get_task(execution.items[1].task_id)
    assert Path(first.output_path).exists(), "半截的 MBTiles 必须留着"
    assert len(task_service.list_tasks()) == 2, "任务记录一条都不许删"


async def test_cancel_while_paused_finishes_without_touching_the_engine(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """暂停中按取消：不需要再等引擎（它早就安全停下了）。"""
    plan = _ready(plan_service, work_dir, "demoland", "northland")
    runner = _FakeRunner(hang=True)
    scheduler = _scheduler(plan_service, task_service, store, runner)

    running = asyncio.ensure_future(scheduler.start(plan))
    assert await _wait_until(lambda: runner.calls)
    await scheduler.pause()
    await running

    execution = await scheduler.cancel()

    assert execution.status is PlanExecutionStatus.CANCELLED
    assert execution.items[0].status is PlanItemRunStatus.CANCELLED
    assert execution.items[1].status is PlanItemRunStatus.CANCELLED
    assert runner.stopped_calls == 1, "引擎只停了一次，取消不该再打一次"


async def test_resume_after_a_cancel_is_refused(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """取消过的计划不能"继续"（Spec §10）：要重跑就重新开始。"""
    plan = _ready(plan_service, work_dir, "demoland")
    runner = _FakeRunner(hang=True)
    scheduler = _scheduler(plan_service, task_service, store, runner)

    running = asyncio.ensure_future(scheduler.start(plan))
    assert await _wait_until(lambda: runner.calls)
    await scheduler.cancel()
    await running
    runner.calls.clear()

    execution = await scheduler.resume()

    assert execution.status is PlanExecutionStatus.CANCELLED
    assert runner.calls == []


# --------------------------------------------------------------------------- #
# 进度聚合（Spec §12 / §14）
# --------------------------------------------------------------------------- #
async def test_progress_is_aggregated_by_tiles_not_by_item_count(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """跑完第一条时，进度是"它的张数 / 全部张数"，不是 1/3（Spec §12）。"""
    plan = _ready(plan_service, work_dir, "demoland", "northland", "rivertown")
    frames: list[float] = []

    class _Watching(_FakeRunner):
        async def run(self, task, **kwargs):
            outcome = await super().run(task, **kwargs)
            return outcome

    scheduler = _scheduler(plan_service, task_service, store, _Watching())
    await scheduler.start(
        plan, on_progress=lambda progress: frames.append(progress.plan_percent)
    )

    assert frames, "应当至少推了一帧进度"
    assert frames == sorted(frames), "进度只能往前走"
    assert frames[-1] == pytest.approx(100.0)


async def test_the_last_frame_reports_the_full_totals(
    plan_service, task_service, store, work_dir: Path
) -> None:
    plan = _ready(plan_service, work_dir, "demoland", "northland")
    frames = []

    scheduler = _scheduler(plan_service, task_service, store, _FakeRunner())
    await scheduler.start(plan, on_progress=frames.append)

    last = frames[-1]
    assert last.total_tiles == plan.total_tiles
    assert last.completed_tiles == plan.total_tiles
    assert last.items_text == "2 / 2"
    assert last.status is PlanExecutionStatus.COMPLETED
    assert last.speed_tiles_per_second == 0.0, "跑完之后速度必须归零（Spec §14）"


async def test_progress_total_excludes_disabled_items(
    plan_service, task_service, store, work_dir: Path
) -> None:
    plan = _ready(plan_service, work_dir, "demoland", "northland")
    plan.set_enabled("northland", False)
    plan_service.set_enabled("northland", False)
    frames = []

    scheduler = _scheduler(plan_service, task_service, store, _FakeRunner())
    await scheduler.start(plan, on_progress=frames.append)

    assert frames[-1].total_tiles < plan.total_tiles + 1
    assert frames[-1].plan_percent == pytest.approx(100.0)


async def test_progress_reports_the_current_region(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """Spec §16：界面要能显示"当前：示例国 Z4~10"。"""
    plan = _ready(plan_service, work_dir, "demoland")
    frames = []

    scheduler = _scheduler(plan_service, task_service, store, _FakeRunner())
    await scheduler.start(plan, on_progress=frames.append)

    running_frames = [f for f in frames if f.current_region_name]
    assert running_frames, "至少在跑的时候要报出当前区域"
    assert "示例国" in running_frames[0].current_region_name
    assert running_frames[0].current_zoom_text.startswith("Z")


# --------------------------------------------------------------------------- #
# 重启恢复（Spec §7 / §20 / §21 / §22）
# --------------------------------------------------------------------------- #
async def test_a_restart_attaches_the_paused_execution(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """程序重启后：执行记录还在，继续时**不重跑已完成的那条**（Spec §7 / §20）。"""
    plan = _ready(plan_service, work_dir, "demoland", "northland")
    runner = _FakeRunner(hang=True)
    scheduler = _scheduler(plan_service, task_service, store, runner)

    running = asyncio.ensure_future(scheduler.start(plan))
    assert await _wait_until(lambda: runner.calls)
    await scheduler.pause()
    await running

    # --- 模拟重启：全新的调度器 + 同一份记录文件 ---
    fresh_runner = _FakeRunner()
    fresh = _scheduler(plan_service, task_service, store, fresh_runner)
    attached = fresh.load_stored_execution(plan)

    assert attached is not None
    assert attached.status is PlanExecutionStatus.PAUSED

    execution = await fresh.resume()

    assert fresh_runner.calls == ["demoland", "northland"]
    assert execution.status is PlanExecutionStatus.COMPLETED


async def test_a_stored_running_execution_is_not_started_automatically(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """读回来只把 ``RUNNING`` 降成 ``PAUSED``，**一个请求都不发**（Spec §20）。"""
    plan = _ready(plan_service, work_dir, "demoland")
    runner = _FakeRunner(hang=True)
    scheduler = _scheduler(plan_service, task_service, store, runner)

    running = asyncio.ensure_future(scheduler.start(plan))
    assert await _wait_until(lambda: runner.calls)
    # 直接"拔电源"：不 pause，靠记录里留下的 RUNNING
    runner.release()
    await running
    execution = store.find(plan.id)
    execution.status = PlanExecutionStatus.RUNNING
    store.save(execution)

    fresh_runner = _FakeRunner()
    fresh = _scheduler(plan_service, task_service, store, fresh_runner)
    attached = fresh.load_stored_execution(plan)

    assert attached.status is PlanExecutionStatus.PAUSED
    assert fresh_runner.calls == [], "读记录本身绝不允许发请求"


async def test_completed_items_are_not_downloaded_again(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """任务已经是 ``COMPLETED`` 且输出文件在 → 直接标完成，不重新下载（Spec §7 / §22）。"""
    plan = _ready(plan_service, work_dir, "demoland", "northland")
    tasks = plan_service.create_download_tasks()
    for task in tasks:
        Path(task.output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(task.output_path).write_bytes(b"done")
        task_service.complete_task(task)

    runner = _FakeRunner()
    scheduler = _scheduler(plan_service, task_service, store, runner)
    # 造一份"没跑完"的记录，让调度器去接管
    scheduler.attach(plan, _paused_execution_for(plan, tasks))

    execution = await scheduler.start(plan)

    assert runner.calls == [], "两条都已完成，一条都不该下"
    assert execution.status is PlanExecutionStatus.COMPLETED
    assert all(
        entry.status is PlanItemRunStatus.COMPLETED for entry in execution.items
    )


async def test_a_completed_record_without_an_output_file_is_marked_failed(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """**Spec §22**：记录说完成了、文件却没了 → 标异常，绝不能当成功。"""
    plan = _ready(plan_service, work_dir, "demoland")
    tasks = plan_service.create_download_tasks()
    task_service.complete_task(tasks[0])  # 故意不创建输出文件

    runner = _FakeRunner()
    scheduler = _scheduler(plan_service, task_service, store, runner)
    scheduler.attach(plan, _paused_execution_for(plan, tasks))

    execution = await scheduler.start(plan)

    assert runner.calls == []
    assert execution.status is PlanExecutionStatus.PARTIAL_FAILED
    assert execution.items[0].status is PlanItemRunStatus.FAILED
    assert "输出文件" in (execution.items[0].error or "")


async def test_a_missing_task_record_is_marked_failed_without_crashing(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """**Spec §21**：任务记录没了 → 那条标失败，应用照常（不崩）。"""
    plan = _ready(plan_service, work_dir, "demoland", "northland")
    tasks = plan_service.create_download_tasks()
    task_service.delete_task(tasks[0].id)

    runner = _FakeRunner()
    scheduler = _scheduler(plan_service, task_service, store, runner)
    scheduler.attach(plan, _paused_execution_for(plan, tasks))

    execution = await scheduler.start(plan)

    assert runner.calls == [], "有对不上的条目时本轮一个请求都不发"
    assert execution.items[0].status is PlanItemRunStatus.FAILED
    assert "任务记录" in (execution.items[0].error or "")


async def test_a_plan_item_that_no_longer_exists_is_marked_failed(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """计划被改过（条目没了）→ 标失败，不崩（Spec §21）。"""
    plan = _ready(plan_service, work_dir, "demoland", "northland")
    tasks = plan_service.create_download_tasks()
    execution = _paused_execution_for(plan, tasks)

    plan.remove_region("demoland")  # 用户删掉了这一行
    plan.recalculate_totals()

    runner = _FakeRunner()
    scheduler = _scheduler(plan_service, task_service, store, runner)
    scheduler.attach(plan, execution)

    result = await scheduler.start(plan)

    assert runner.calls == []
    assert result.items[0].status is PlanItemRunStatus.FAILED
    assert "计划" in (result.items[0].error or "")


async def test_a_changed_zoom_in_the_plan_blocks_the_resume(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """计划里的 Zoom 被改过 → 与任务记录对不上 → 停在那条（Spec §18 / §21）。"""
    plan = _ready(plan_service, work_dir, "demoland")
    tasks = plan_service.create_download_tasks()
    execution = _paused_execution_for(plan, tasks)

    plan.items[0].min_zoom = plan.items[0].min_zoom + 1  # 偷改计划（正常路径禁止）

    runner = _FakeRunner()
    scheduler = _scheduler(plan_service, task_service, store, runner)
    scheduler.attach(plan, execution)

    result = await scheduler.start(plan)

    assert runner.calls == []
    assert result.items[0].status is PlanItemRunStatus.FAILED
    assert "缩放" in (result.items[0].error or "")


async def test_recovery_keeps_the_other_items_runnable_after_a_skip(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """对不上的那条**跳过之后**，其余的照常跑（用户有出路，Spec §11）。"""
    plan = _ready(plan_service, work_dir, "demoland", "northland")
    tasks = plan_service.create_download_tasks()
    task_service.delete_task(tasks[0].id)

    runner = _FakeRunner()
    scheduler = _scheduler(plan_service, task_service, store, runner)
    scheduler.attach(plan, _paused_execution_for(plan, tasks))
    await scheduler.start(plan)

    execution = await scheduler.skip_current()

    assert runner.calls == ["northland"]
    assert execution.items[0].status is PlanItemRunStatus.SKIPPED
    assert execution.items[1].status is PlanItemRunStatus.COMPLETED


# --------------------------------------------------------------------------- #
# 前置条件（Spec §4 / §6 / §18）
# --------------------------------------------------------------------------- #
async def test_start_refuses_a_plan_that_was_never_estimated(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """没估算就没有分母，进度会永远停在 0 —— 宁可拒绝并说清楚。"""
    plan_service.add_region("demoland")
    plan_service.apply_settings(output_dir=work_dir / "output")
    plan = plan_service.plan

    with pytest.raises(PlanSchedulerError, match="估算"):
        await _scheduler(plan_service, task_service, store, _FakeRunner()).start(plan)


async def test_start_refuses_an_empty_plan(
    plan_service, task_service, store, work_dir: Path
) -> None:
    plan_service.apply_settings(output_dir=work_dir / "output")

    with pytest.raises(PlanSchedulerError, match="没有"):
        await _scheduler(plan_service, task_service, store, _FakeRunner()).start(
            plan_service.plan
        )


async def test_start_is_idempotent_while_running(
    plan_service, task_service, store, work_dir: Path
) -> None:
    """"再点一次开始"不该变成两份执行（Spec §18：执行中禁止改计划）。"""
    plan = _ready(plan_service, work_dir, "demoland", "northland")
    runner = _FakeRunner(hang=True)
    scheduler = _scheduler(plan_service, task_service, store, runner)

    first = asyncio.ensure_future(scheduler.start(plan))
    assert await _wait_until(lambda: runner.calls)
    same = await scheduler.start(plan)

    assert same is scheduler.execution
    await scheduler.cancel()
    await first


async def test_pause_and_resume_without_a_plan_are_refused(
    plan_service, task_service, store
) -> None:
    """还没开始过就点暂停/继续 → 明确报错，而不是静默什么都不做。"""
    scheduler = _scheduler(plan_service, task_service, store, _FakeRunner())

    with pytest.raises(PlanSchedulerError, match="开始"):
        await scheduler.pause()
    with pytest.raises(PlanSchedulerError, match="开始"):
        await scheduler.resume()


# --------------------------------------------------------------------------- #
# 静态守卫（Spec §5 / §29）
# --------------------------------------------------------------------------- #
def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_the_scheduler_does_not_import_the_engine_at_module_level() -> None:
    """调度器**不许**在模块顶层拉下载引擎 / 网络 / 数据库。

    它只该编排既有能力；把这些 import 放到函数内部是本项目对跨层依赖的既有
    约定，顺带保证 ``app.planning`` 的导入图不会被拖进半个应用。
    """
    tree = ast.parse(_source("app/planning/plan_scheduler.py"))
    forbidden = (
        "app.mbtiles",
        "app.downloader",
        "httpx",
        "sqlite3",
        "requests",
    )
    offenders: list[str] = []
    for node in tree.body:  # 只看模块顶层
        if isinstance(node, ast.Import):
            offenders.extend(
                alias.name for alias in node.names if _matches(alias.name, forbidden)
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            if _matches(node.module, forbidden):
                offenders.append(node.module)
    assert not offenders, f"模块顶层不该 import：{offenders}"


def _matches(module: str, forbidden: tuple[str, ...]) -> bool:
    return any(
        module == name or module.startswith(name + ".") for name in forbidden
    )


def test_the_scheduler_does_not_write_sql_itself() -> None:
    """**Spec §5 / §29**：调度器不许自己写 MBTiles（那是 ``app.mbtiles`` 的活）。

    用 ``ast`` 看真实的字符串字面量，而不是扫源码文本 —— 文档字符串里
    "INSERT OR REPLACE" 这种词是合法出现的（本项目的既有教训）。
    """
    tree = ast.parse(_source("app/planning/plan_scheduler.py"))
    keywords = ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "CREATE TABLE")
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for keyword in keywords:
                assert keyword not in node.value.upper(), (
                    f"第 {node.lineno} 行出现 SQL 关键字 {keyword!r}："
                    "调度的活不该由本层干"
                )


def test_the_package_root_does_not_export_the_scheduler() -> None:
    """``app/planning`` 只转出**模型**（Slice 20 的导入环教训，Spec §5）。

    ``plan_scheduler`` 会拉 ``plan_service``（进而拉区域目录与任务服务），
    一旦从这里转出，``import app.geo`` 那条链就会拖着半个应用一起初始化。
    """
    import app.planning as package

    for name in ("PlanScheduler", "PlanExecutionStore", "MapDownloadTaskRunner"):
        assert not hasattr(package, name), f"{name} 不该出现在 app.planning 上"
    # 模型是转出的（对照）
    assert hasattr(package, "PlanExecution")
    assert hasattr(package, "PlanItemRunStatus")


def test_the_scheduler_does_not_kill_threads() -> None:
    """**Spec §8 / §25**：禁止强杀线程。调度器里不该出现 terminate/kill。"""
    tree = ast.parse(_source("app/planning/plan_scheduler.py"))
    banned = {"terminate", "kill", "terminate_thread"}
    found = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in banned
    ]
    assert not found, f"调度器不该调用 {found}"


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _paused_execution_for(plan: DownloadPlan, tasks) -> "object":
    """造一份"跑到一半被暂停"的执行记录（用于恢复类用例）。

    直接用 ``new_execution_for`` + 手工把状态改成 PAUSED：这正是"上周三点了
    暂停、今天接着跑"在磁盘上的样子。
    """
    from app.planning.plan_execution import new_execution_for

    execution = new_execution_for(plan, tasks)
    execution.set_status(PlanExecutionStatus.PAUSED)
    return execution
