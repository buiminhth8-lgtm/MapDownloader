"""计划级下载线程胶水（Slice 21，Spec §17 / §25）。

:class:`~app.workers.plan_download_worker.PlanDownloadWorker` 本身**不含调度逻辑**：
顺序 / 暂停语义 / 聚合 / 落盘全在 :mod:`app.planning.plan_scheduler`。它只做四件事：

1. 在另一个线程里起一个 ``asyncio`` 事件循环（Qt 主线程的事件循环不是 asyncio 循环）；
2. 把计划状态 / 条目状态 / 聚合进度 / 日志通过 Qt 信号送回主线程；
3. 把界面的指令（暂停 / 继续 / 取消 / 重试 / 跳过）**线程安全地**投进那个循环，
   包括"事件循环还没起来就被点了"；
4. **绝不**让异常冒泡进 Qt 事件循环、**绝不**强杀线程。

这四条正是本文件要测的全部内容。串行顺序与暂停语义本身由
:mod:`tests.test_plan_scheduler` 覆盖，这里不重复，只测"过线程之后还成不成立"。

跑真线程、真事件循环、真 Qt 信号（``offscreen`` 平台），不跑真网络：
单任务执行者由 :mod:`tests.plan_fakes` 提供。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from PySide6.QtCore import QThread

from app.planning.plan_execution import (
    PlanExecutionStatus,
    PlanItemRunStatus,
)
from app.planning.plan_execution_store import PlanExecutionStore
from app.planning.plan_scheduler import PlanScheduler
from app.workers.plan_download_worker import PlanDownloadWorker
from tests.plan_fakes import ExplodingRunner, FakeRunner, ready_plan

# 夹具直接复用调度器那份（同一个来源，改一处不会两边不一致）。
from tests.test_plan_scheduler import (  # noqa: F401  - 夹具注入
    plan_service,
    service,
    store,
    task_service,
)

#: 仓库根（静态守卫要读源码）。
ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _scheduler(plan_service, task_service, store, runner) -> PlanScheduler:
    return PlanScheduler(
        plan_service,
        runner,
        task_service=task_service,
        store=store,
        # 节流关掉：用例要能"每一条状态变化都立刻在磁盘上看到"
        save_interval_seconds=0.0,
        save_tile_interval=1,
    )


def _wait_until(qapp, predicate, timeout_s: float = 20.0) -> bool:
    """轮询等待条件成立，期间持续处理 Qt 事件（主线程不阻塞）。"""
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _run_to_completion(worker: PlanDownloadWorker, qapp, timeout: int = 30_000) -> None:
    """启动并等到线程自己结束（只对"会走到终态"的计划成立）。"""
    worker.start()
    assert worker.wait(timeout) is True, "计划线程没有在超时前结束"
    for _ in range(8):
        qapp.processEvents()


class _Recorder:
    """把 worker 的信号收进几个列表（列表里同时记下"在哪个线程收到的"）。"""

    def __init__(self, worker: PlanDownloadWorker) -> None:
        self.frames: list[object] = []
        self.items: list[object] = []
        self.progress: list[object] = []
        self.logs: list[str] = []
        self.completed: list[object] = []
        self.paused: list[object] = []
        self.cancelled: list[object] = []
        self.failed: list[str] = []
        self.threads: list[QThread] = []
        worker.status_changed.connect(self._on_status)
        worker.item_changed.connect(self.items.append)
        worker.progress.connect(self.progress.append)
        worker.log.connect(self.logs.append)
        worker.completed.connect(self.completed.append)
        worker.paused.connect(self.paused.append)
        worker.cancelled.connect(self.cancelled.append)
        worker.failed.connect(self.failed.append)

    def _on_status(self, execution) -> None:
        self.frames.append(execution)
        self.threads.append(QThread.currentThread())

    # --- 只读小工具 --------------------------------------------------- #
    @property
    def last_status(self) -> PlanExecutionStatus:
        return self.frames[-1].status if self.frames else None


# --------------------------------------------------------------------------- #
# 一条完整的计划：真的在后台线程里跑完
# --------------------------------------------------------------------------- #
def test_a_whole_plan_runs_in_the_worker_thread(
    qapp, plan_service, task_service, store, work_dir: Path
) -> None:
    """整份计划跑完 → ``completed`` 信号带回一份 ``COMPLETED`` 的执行记录。"""
    plan = ready_plan(plan_service, work_dir, "demoland", "northland")
    runner = FakeRunner()
    worker = PlanDownloadWorker(
        _scheduler(plan_service, task_service, store, runner), plan
    )
    seen = _Recorder(worker)

    _run_to_completion(worker, qapp)

    assert seen.failed == []
    assert len(seen.completed) == 1
    assert seen.completed[0].status is PlanExecutionStatus.COMPLETED
    assert runner.calls == ["demoland", "northland"], "层级顺序不该被线程打乱"
    assert store.find(plan.id).status is PlanExecutionStatus.COMPLETED


def test_signals_are_delivered_on_the_main_thread(
    qapp, plan_service, task_service, store, work_dir: Path
) -> None:
    """**Spec §17 的核心**：后台线程只发信号，槽在主线程跑（界面只能在主线程被改）。"""
    plan = ready_plan(plan_service, work_dir, "demoland", "northland")
    worker = PlanDownloadWorker(
        _scheduler(plan_service, task_service, store, FakeRunner()), plan
    )
    seen = _Recorder(worker)

    _run_to_completion(worker, qapp)

    assert seen.threads, "一帧状态都没收到"
    assert set(seen.threads) == {qapp.thread()}, "有信号是在后台线程里被处理的"


def test_progress_frames_describe_the_whole_plan(
    qapp, plan_service, task_service, store, work_dir: Path
) -> None:
    """聚合进度按**张数**汇总，最后一帧是满的（Spec §12 / §13）。"""
    plan = ready_plan(plan_service, work_dir, "demoland", "northland")
    expected = sum(item.selected_tiles for item in plan.ordered_enabled_items())
    worker = PlanDownloadWorker(
        _scheduler(plan_service, task_service, store, FakeRunner()), plan,
        token="should-never-be-written",
    )
    seen = _Recorder(worker)

    _run_to_completion(worker, qapp)

    assert seen.progress, "一帧进度都没收到"
    assert seen.progress[-1].total_tiles == expected
    assert seen.progress[-1].completed_tiles == expected
    assert seen.progress[-1].plan_percent == pytest.approx(100.0)
    assert store.path.read_text(encoding="utf-8").find("should-never-be-written") < 0


def test_key_events_are_reported_as_log_lines(
    qapp, plan_service, task_service, store, work_dir: Path
) -> None:
    """关键事件（开始 / 完成 / 收尾）有日志行，而且**每张瓦片不刷屏**（Spec §24）。"""
    plan = ready_plan(plan_service, work_dir, "demoland")
    worker = PlanDownloadWorker(
        _scheduler(plan_service, task_service, store, FakeRunner()), plan
    )
    seen = _Recorder(worker)

    _run_to_completion(worker, qapp)

    assert any("开始" in line for line in seen.logs)
    assert any("完成" in line for line in seen.logs)
    # 条目级日志是"每条一行"，不是"每张一行"
    assert len(seen.logs) < 20


# --------------------------------------------------------------------------- #
# 暂停 / 继续：同一个线程、同一个事件循环
# --------------------------------------------------------------------------- #
def test_pause_and_resume_stay_in_the_same_thread(
    qapp, plan_service, task_service, store, work_dir: Path
) -> None:
    """暂停之后线程**不能死** —— 继续必须还落在那一个事件循环上（Spec §8 / §9）。"""
    plan = ready_plan(plan_service, work_dir, "demoland", "northland")
    runner = FakeRunner(hang_once=True)
    worker = PlanDownloadWorker(
        _scheduler(plan_service, task_service, store, runner), plan
    )
    seen = _Recorder(worker)

    worker.start()
    assert _wait_until(qapp, lambda: runner.calls), "第一项该已经开工"

    worker.pause()
    assert _wait_until(qapp, lambda: seen.paused), "没有收到「已暂停」"
    assert seen.paused[0].status is PlanExecutionStatus.PAUSED
    assert worker.isRunning() is True, "线程不该随暂停一起结束"
    assert runner.calls == ["demoland"], "后续条目不得启动"

    worker.resume()

    assert worker.wait(30_000) is True
    for _ in range(8):
        qapp.processEvents()
    assert seen.completed, "继续之后没有跑完"
    assert runner.calls == ["demoland", "demoland", "northland"], (
        "暂停的那一条要接着跑（复用 Resume 补差集），然后才是下一条"
    )


def test_a_command_sent_before_the_loop_is_ready_is_queued_then_replayed(
    qapp, plan_service, task_service, store, work_dir: Path
) -> None:
    """事件循环起来之前收到的指令先排队，之后补发 —— 点得快不该丢一次点击。"""
    plan = ready_plan(plan_service, work_dir, "demoland")
    # 第一条失败（第一次）→ 调度器收成「部分失败」，停在那儿等人决定
    runner = FakeRunner(fail_on=("demoland",), fail_times=1)
    worker = PlanDownloadWorker(
        _scheduler(plan_service, task_service, store, runner), plan
    )
    seen = _Recorder(worker)

    worker.retry_current()
    assert worker.queued_commands == ["retry_current"], "指令该被排队而不是丢掉"

    worker.start()
    assert _wait_until(qapp, lambda: seen.frames), "该收到第一轮的状态帧"
    assert worker.wait(30_000) is True, "补发的「重试当前」该让计划跑完"
    for _ in range(8):
        qapp.processEvents()

    assert seen.completed, "补发的「重试当前」没有生效"
    assert runner.calls == ["demoland", "demoland"], "失败的那一条该被重跑一次"
    assert runner.completed_calls == 1


def test_an_item_that_stopped_on_its_own_blocks_instead_of_relooping(
    qapp, plan_service, task_service, store, work_dir: Path
) -> None:
    """没人点暂停，条目却停了 → 收成「部分失败」等人决定，**不是**原地死循环。"""
    plan = ready_plan(plan_service, work_dir, "demoland", "northland")
    runner = FakeRunner(stop_once=True)
    worker = PlanDownloadWorker(
        _scheduler(plan_service, task_service, store, runner), plan
    )
    seen = _Recorder(worker)

    worker.start()
    assert _wait_until(
        qapp, lambda: seen.frames and seen.last_status is PlanExecutionStatus.PARTIAL_FAILED
    ), "该停在「部分失败」"
    assert runner.calls == ["demoland"], "停下来的那一条不该被自动重跑"
    assert any("部分失败" in line for line in seen.logs)
    assert worker.isRunning() is True, "等人决定期间线程还活着"

    worker.request_close()
    assert worker.wait(30_000) is True


# --------------------------------------------------------------------------- #
# 取消 / 关页
# --------------------------------------------------------------------------- #
def test_cancel_stops_the_plan_and_keeps_the_partial_file(
    qapp, plan_service, task_service, store, work_dir: Path
) -> None:
    """取消：当前任务安全停下、后续一律取消、**半截的 MBTiles 留在磁盘上**（Spec §10）。"""
    plan = ready_plan(plan_service, work_dir, "demoland", "northland")
    runner = FakeRunner(hang=True)
    worker = PlanDownloadWorker(
        _scheduler(plan_service, task_service, store, runner), plan
    )
    seen = _Recorder(worker)

    worker.start()
    assert _wait_until(qapp, lambda: runner.calls)
    worker.cancel()

    assert worker.wait(30_000) is True, "取消之后线程该收干净"
    for _ in range(8):
        qapp.processEvents()

    assert seen.cancelled, "没有收到「已取消」"
    execution = store.find(plan.id)
    assert execution.status is PlanExecutionStatus.CANCELLED
    assert [item.status for item in execution.items] == [
        PlanItemRunStatus.CANCELLED,
        PlanItemRunStatus.CANCELLED,
    ], "当前与后续都该是「已取消」"
    assert runner.stopped_calls == 1, "引擎只被停了一次"

    # 半截的文件必须在（Spec §10 末句：绝不删除已下载的成果）
    output = work_dir / "output"
    assert output.exists()
    partials = list(output.rglob("*.mbtiles"))
    assert partials, "取消不该把半截的 MBTiles 删掉"


def test_closing_the_page_pauses_instead_of_killing_the_thread(
    qapp, plan_service, task_service, store, work_dir: Path
) -> None:
    """**Spec §25**：程序退出 / 关页时是"安全暂停 + 落盘"，不是强杀线程。"""
    plan = ready_plan(plan_service, work_dir, "demoland", "northland")
    runner = FakeRunner(hang=True)
    worker = PlanDownloadWorker(
        _scheduler(plan_service, task_service, store, runner), plan
    )
    seen = _Recorder(worker)

    worker.start()
    assert _wait_until(qapp, lambda: runner.calls)

    worker.request_close()

    assert worker.wait(30_000) is True, "线程没有安全收干净"
    for _ in range(8):
        qapp.processEvents()

    execution = store.find(plan.id)
    assert execution.status is PlanExecutionStatus.PAUSED, "退出时该落成「可继续」而不是「已取消」"
    assert execution.items[0].status is PlanItemRunStatus.PAUSED
    assert execution.items[1].status is PlanItemRunStatus.WAITING
    assert runner.stopped_calls == 1, "该请引擎安全停下（而不是被强杀）"
    # 任务记录也必须是"可继续"：下次启动能直接复用 Resume
    task = task_service.get_task(execution.items[0].task_id)
    from app.models.download_task import DownloadTaskStatus

    assert task.status is DownloadTaskStatus.PAUSED


def test_close_after_the_plan_is_done_is_harmless(
    qapp, plan_service, task_service, store, work_dir: Path
) -> None:
    """已经跑完的计划再关页：不报错、不改状态。"""
    plan = ready_plan(plan_service, work_dir, "demoland")
    worker = PlanDownloadWorker(
        _scheduler(plan_service, task_service, store, FakeRunner()), plan
    )

    _run_to_completion(worker, qapp)
    worker.request_close()
    assert worker.wait(10_000) is True

    assert store.find(plan.id).status is PlanExecutionStatus.COMPLETED


# --------------------------------------------------------------------------- #
# 起不来 / 炸了：都必须是"给用户看的一句话"，不能冒泡进 Qt
# --------------------------------------------------------------------------- #
def test_an_unestimated_plan_reports_a_readable_failure(
    qapp, plan_service, task_service, store, work_dir: Path
) -> None:
    """还没估算就点开始 → ``failed`` 带一句人话（而不是一个异常）。"""
    plan_service.add_region("demoland")
    plan_service.apply_settings(name="没估算的计划", output_dir=work_dir / "output")
    plan = plan_service.plan  # 故意不 estimate()
    worker = PlanDownloadWorker(
        _scheduler(plan_service, task_service, store, FakeRunner()), plan
    )
    seen = _Recorder(worker)

    _run_to_completion(worker, qapp)

    assert len(seen.failed) == 1
    assert "估算" in seen.failed[0]
    assert seen.completed == []


def test_an_exploding_runner_reports_the_item_and_not_a_crash(
    qapp, plan_service, task_service, store, work_dir: Path
) -> None:
    """单条任务抛异常 → 计划停在它上面（``failed`` 信号一次都不该发）Spec §5。"""
    plan = ready_plan(plan_service, work_dir, "demoland")
    worker = PlanDownloadWorker(
        _scheduler(plan_service, task_service, store, ExplodingRunner()), plan
    )
    seen = _Recorder(worker)

    worker.start()
    assert _wait_until(
        qapp, lambda: seen.frames and seen.last_status is PlanExecutionStatus.PARTIAL_FAILED
    )
    assert seen.failed == [], "单条任务的异常不该被当成「这一轮起不来」"
    assert any("异常" in line for line in seen.logs)

    worker.request_close()
    assert worker.wait(30_000) is True


# --------------------------------------------------------------------------- #
# 静态守卫（Spec §17 / §25 / §29）
# --------------------------------------------------------------------------- #
def _worker_source() -> tuple[str, ast.Module]:
    path = ROOT / "app" / "workers" / "plan_download_worker.py"
    text = path.read_text(encoding="utf-8")
    return text, ast.parse(text)


def _referenced_names(tree: ast.Module) -> set[str]:
    """源码里**真的被引用**的名字（不含注释与字符串）。

    本项目的既有约定：静态守卫看 ``ast`` 的真实节点，而不是 ``in text`` ——
    模块文档里合法地出现 "QWidget" 这个词（"绝不直接改 QWidget"），
    用文本匹配就会把一句解释当成违规。
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _imported_modules(tree: ast.Module) -> set[str]:
    """源码里 ``import`` 进来的顶层模块名。"""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_the_worker_never_kills_the_thread() -> None:
    """不许 ``terminate()`` / ``kill()``：强杀线程会让 MBTiles 处在写一半的状态。"""
    _text, tree = _worker_source()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        assert name not in {"terminate", "kill"}, f"worker 里不许调 {name}()"
    assert "threading" not in _imported_modules(tree), (
        "计划 worker 不需要线程原语：指令统一走事件循环（Spec §17）"
    )


def test_the_worker_never_touches_widgets() -> None:
    """后台线程不许碰控件：只发信号，控件只在主线程的槽里被碰。"""
    _text, tree = _worker_source()
    forbidden = {"QWidget", "QMessageBox", "QDialog", "QApplication"}
    assert not (_referenced_names(tree) & forbidden), "worker 里引用了控件类"


def test_the_worker_is_exported_from_the_package() -> None:
    """``app.workers`` 要转出它（界面只从这里拿线程桥）。"""
    import app.workers as package

    assert "PlanDownloadWorker" in package.__all__
    assert package.PlanDownloadWorker is PlanDownloadWorker
