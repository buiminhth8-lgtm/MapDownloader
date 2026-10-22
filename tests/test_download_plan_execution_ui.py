"""下载计划页的"开始 / 暂停 / 继续 / 取消"（Slice 21，Spec §15 ~ §18）。

这一层测的是"**用户真的点得动吗**"：Spec §15 列的六个执行按钮，每一个都要有
一条"点一下并断言结果"的用例。理由（Slice 14 的教训）：Qt 槽里抛的异常会被
静默吞掉，于是"按钮不好使"在数据层用例里永远看不见 —— 而执行区最容易出的
缺陷恰恰是"点了没反应"或"点了两次开了两份执行"。

同时守住三条界面契约：

- **进度条与状态行不自己算数**：数字全部来自 ``PlanProgress``（Spec §13）；
- **执行期间改不动计划**（Spec §18）：表格与"移除 / 清空 / 生成任务"变灰；
- **关页不是杀掉**（Spec §25）：关页把计划落成"可继续"，下次打开能接着下。

真线程、真 Qt 信号；单任务执行者由 :mod:`tests.plan_fakes` 提供。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox

from app.models.download_task import DownloadTaskStatus
from app.planning.plan_execution import (
    PlanExecutionStatus,
    PlanItemRunStatus,
)
from app.planning.plan_execution_store import PlanExecutionStore
from app.planning.plan_scheduler import PlanScheduler
from app.planning.plan_service import PlanService
from app.ui.download_plan_dialog import (
    PLAN_ACTION_CANCEL_RUN,
    PLAN_ACTION_CLEAR,
    PLAN_ACTION_CLOSE,
    PLAN_ACTION_GENERATE,
    PLAN_ACTION_PAUSE,
    PLAN_ACTION_REMOVE,
    PLAN_ACTION_RESUME,
    PLAN_ACTION_RETRY,
    PLAN_ACTION_SKIP,
    PLAN_ACTION_START,
    DownloadPlanDialog,
)
from tests.plan_fakes import FakeRunner, ready_plan

# 夹具复用既有那两份（同一个来源，改一处不会两边不一致）。
from tests.test_download_plan_dialog import (  # noqa: F401  - 夹具注入
    _add,
    _flush,
    plan_service,
    service,
    task_service,
)

#: 状态列（表格最后一列）
_STATUS_COLUMN = 5


@pytest.fixture
def store(work_dir: Path) -> PlanExecutionStore:
    return PlanExecutionStore(work_dir / "plan_executions.json")


def _factory(plan_service: PlanService, task_service, store, runner):
    """给对话框注入的调度器工厂（真调度器 + 假执行者）。"""

    def build() -> PlanScheduler:
        return PlanScheduler(
            plan_service,
            runner,
            task_service=task_service,
            store=store,
            # 节流关掉：用例要能"每一条状态变化都立刻在磁盘上看到"
            save_interval_seconds=0.0,
            save_tile_interval=1,
        )

    return build


def _dialog(
    plan_service: PlanService,
    task_service,
    store,
    runner=None,
    *,
    with_scheduler: bool = True,
) -> DownloadPlanDialog:
    """**不自动估算**的对话框：把"估算"这一步握在用例手里。"""
    factory = None
    if with_scheduler:
        factory = _factory(plan_service, task_service, store, runner or FakeRunner())
    return DownloadPlanDialog(
        plan_service, auto_estimate=False, scheduler_factory=factory
    )


def _wait_until(qapp, predicate, timeout_s: float = 30.0) -> bool:
    """轮询等待条件成立，期间持续处理 Qt 事件（主线程不阻塞）。

    条件一成立**还要再settle 几圈**事件循环才返回：调度器在后台线程里推进，
    状态落到 ``execution()`` 与"界面上那行字被重画"是两次独立的事件投递
    （后者由排队信号触发）。predicate 只看前者时，返回得太早就会读到
    上一帧的文案 —— 表现为"状态明明已经部分失败，标签还写着执行中"，
    而且是否复现取决于机器快慢（Slice 21 收尾时的间歇性失败就出在这里）。
    多转几圈不改变任何被测行为，只是把"界面已经跟上"这件事等实。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            for _ in range(8):
                qapp.processEvents()
                time.sleep(0.005)
            return True
        time.sleep(0.02)
    return False


def _click(dialog: DownloadPlanDialog, action: str, qapp) -> None:
    """点一个执行区按钮（点 **按钮**，不调方法：要证明按钮真的接上了）。"""
    dialog.run_buttons()[action].click()
    _flush(qapp)


# --------------------------------------------------------------------------- #
# 按钮初始状态（Spec §15）
# --------------------------------------------------------------------------- #
def test_run_buttons_are_disabled_without_a_scheduler(
    plan_service: PlanService, qapp
) -> None:
    """没拿到调度入口时「开始计划」是灰的（点不动比点了报错更好）。"""
    dialog = _dialog(plan_service, None, None, with_scheduler=False)
    _add(plan_service, "demoland")
    dialog.refresh()

    buttons = dialog.run_buttons()
    assert buttons[PLAN_ACTION_START].isEnabled() is False
    assert all(
        not buttons[key].isEnabled()
        for key in (
            PLAN_ACTION_PAUSE,
            PLAN_ACTION_RESUME,
            PLAN_ACTION_CANCEL_RUN,
            PLAN_ACTION_RETRY,
            PLAN_ACTION_SKIP,
        )
    )
    assert dialog.table().isEnabled() is True, "没有执行时不该锁住编辑"
    dialog.close()


def test_only_start_is_available_before_the_plan_runs(
    plan_service: PlanService, task_service, store, qapp
) -> None:
    ready_plan(plan_service, task_service.store.path.parent, "demoland")
    dialog = _dialog(plan_service, task_service, store)
    dialog.refresh()

    buttons = dialog.run_buttons()
    assert buttons[PLAN_ACTION_START].isEnabled() is True
    assert buttons[PLAN_ACTION_PAUSE].isEnabled() is False
    assert buttons[PLAN_ACTION_RESUME].isEnabled() is False
    assert buttons[PLAN_ACTION_CANCEL_RUN].isEnabled() is False
    assert buttons[PLAN_ACTION_RETRY].isEnabled() is False
    assert buttons[PLAN_ACTION_SKIP].isEnabled() is False
    assert dialog.plan_progress_bar().value() == 0
    assert "还没开始" in dialog.run_status_lines()[0]
    dialog.close()


# --------------------------------------------------------------------------- #
# 开始：点一下真的会下，而且会下完
# --------------------------------------------------------------------------- #
def test_clicking_start_runs_the_whole_plan(
    plan_service: PlanService, task_service, store, work_dir: Path, qapp
) -> None:
    """**核心用例**：点「开始计划」→ 两个区域依次跑完 → 界面显示已完成。"""
    ready_plan(plan_service, work_dir, "demoland", "northland")
    runner = FakeRunner()
    dialog = _dialog(plan_service, task_service, store, runner)
    dialog.refresh()

    _click(dialog, PLAN_ACTION_START, qapp)
    assert dialog.wait_for_plan(30_000) is True, "计划线程没有跑完"
    _flush(qapp)

    assert runner.calls == ["demoland", "northland"], "层级顺序不该被界面打乱"
    assert dialog.execution().status is PlanExecutionStatus.COMPLETED
    assert dialog.plan_progress_bar().value() == 100
    assert "已完成" in dialog.run_status_lines()[0]
    # 状态列换成了执行符号（Spec §18）
    for row in range(dialog.table().rowCount()):
        text = dialog.table().item(row, _STATUS_COLUMN).text()
        assert text.startswith("✓"), f"第 {row} 行状态列是 {text!r}"
    # 跑完之后回到"只能开始"的状态
    assert dialog.run_buttons()[PLAN_ACTION_START].isEnabled() is True
    assert dialog.run_buttons()[PLAN_ACTION_PAUSE].isEnabled() is False
    dialog.close()


def test_the_status_area_shows_the_six_lines_of_the_progress(
    plan_service: PlanService, task_service, store, work_dir: Path, qapp
) -> None:
    """状态区照 ``PlanProgress`` 画（Spec §16）：状态 / 当前 / 任务 / 张数 / 速度。"""
    plan = ready_plan(plan_service, work_dir, "demoland")
    dialog = _dialog(plan_service, task_service, store, FakeRunner())
    dialog.refresh()

    _click(dialog, PLAN_ACTION_START, qapp)
    assert dialog.wait_for_plan(30_000) is True
    _flush(qapp)

    lines = dialog.run_status_lines()
    joined = "\n".join(lines)
    assert "状态：" in joined
    assert "任务：" in joined
    assert "张" in joined
    assert str(plan.total_tiles) in joined.replace(",", "")
    dialog.close()


def test_clicking_start_twice_does_not_open_two_executions(
    plan_service: PlanService, task_service, store, work_dir: Path, qapp
) -> None:
    """连点两次「开始计划」不该变成两份执行（按钮在跑起来后就是灰的）。"""
    ready_plan(plan_service, work_dir, "demoland")
    runner = FakeRunner(hang=True)
    dialog = _dialog(plan_service, task_service, store, runner)
    dialog.refresh()

    _click(dialog, PLAN_ACTION_START, qapp)
    assert _wait_until(qapp, lambda: runner.calls), "第一项该已经开工"
    assert dialog.run_buttons()[PLAN_ACTION_START].isEnabled() is False

    _click(dialog, PLAN_ACTION_START, qapp)  # 再点一次（按钮是灰的，点不动）
    assert runner.calls == ["demoland"], "第二份执行不该被开起来"

    dialog.run_buttons()[PLAN_ACTION_CANCEL_RUN].click()
    _flush(qapp)
    box = dialog.message_box()
    assert box is not None
    box.button(QMessageBox.StandardButton.Yes).click()
    assert dialog.wait_for_plan(30_000) is True
    dialog.close()


# --------------------------------------------------------------------------- #
# 暂停 / 继续（Spec §8 / §9 / §18）
# --------------------------------------------------------------------------- #
def test_pause_then_resume_from_the_buttons(
    plan_service: PlanService, task_service, store, work_dir: Path, qapp
) -> None:
    """暂停 → 表格被锁、继续可用；继续 → 接着跑完（复用 Resume 补差集）。"""
    ready_plan(plan_service, work_dir, "demoland", "northland")
    runner = FakeRunner(hang_once=True)
    dialog = _dialog(plan_service, task_service, store, runner)
    dialog.refresh()

    _click(dialog, PLAN_ACTION_START, qapp)
    assert _wait_until(qapp, lambda: runner.calls), "第一项该已经开工"

    _click(dialog, PLAN_ACTION_PAUSE, qapp)
    assert _wait_until(
        qapp, lambda: dialog.execution() is not None
        and dialog.execution().status is PlanExecutionStatus.PAUSED
    ), "界面没有进入「已暂停」"

    assert "已暂停" in dialog.run_status_lines()[0]
    assert dialog.run_buttons()[PLAN_ACTION_RESUME].isEnabled() is True
    assert dialog.run_buttons()[PLAN_ACTION_PAUSE].isEnabled() is False
    # Spec §18：暂停期间改不动计划
    assert dialog.table().isEnabled() is False
    assert dialog.buttons()[PLAN_ACTION_REMOVE].isEnabled() is False
    assert dialog.buttons()[PLAN_ACTION_CLEAR].isEnabled() is False
    assert dialog.buttons()[PLAN_ACTION_GENERATE].isEnabled() is False

    _click(dialog, PLAN_ACTION_RESUME, qapp)
    assert dialog.wait_for_plan(30_000) is True, "继续之后没有跑完"
    _flush(qapp)

    assert runner.calls == ["demoland", "demoland", "northland"], (
        "暂停的那一条要接着跑（已有瓦片跳过），然后才是下一条"
    )
    assert dialog.execution().status is PlanExecutionStatus.COMPLETED
    assert dialog.table().isEnabled() is True, "跑完之后编辑该解锁"
    dialog.close()


# --------------------------------------------------------------------------- #
# 取消（Spec §10 / §15）
# --------------------------------------------------------------------------- #
def test_cancel_asks_first_and_keeps_the_partial_file(
    plan_service: PlanService, task_service, store, work_dir: Path, qapp
) -> None:
    """取消：先问一句 → 当前停下 → 后续标已取消 → **半截的文件还在**。"""
    ready_plan(plan_service, work_dir, "demoland", "northland")
    runner = FakeRunner(hang=True)
    dialog = _dialog(plan_service, task_service, store, runner)
    dialog.refresh()

    _click(dialog, PLAN_ACTION_START, qapp)
    assert _wait_until(qapp, lambda: runner.calls)

    _click(dialog, PLAN_ACTION_CANCEL_RUN, qapp)
    box = dialog.message_box()
    assert box is not None, "取消是要问一句的（这是放弃剩下的）"
    assert "保留" in box.text()
    box.button(QMessageBox.StandardButton.Yes).click()

    assert dialog.wait_for_plan(30_000) is True
    _flush(qapp)

    execution = dialog.execution()
    assert execution.status is PlanExecutionStatus.CANCELLED
    assert [item.status for item in execution.items] == [
        PlanItemRunStatus.CANCELLED,
        PlanItemRunStatus.CANCELLED,
    ]
    assert list((work_dir / "output").rglob("*.mbtiles")), "半截的 MBTiles 不该被删"
    assert "已取消" in dialog.run_status_lines()[0]
    dialog.close()


def test_saying_no_to_the_cancel_question_keeps_the_plan_running(
    plan_service: PlanService, task_service, store, work_dir: Path, qapp
) -> None:
    """问一句时点「否」→ 计划照旧在跑（不能"点了否却停了"）。"""
    ready_plan(plan_service, work_dir, "demoland")
    runner = FakeRunner(hang=True)
    dialog = _dialog(plan_service, task_service, store, runner)
    dialog.show()
    dialog.refresh()

    _click(dialog, PLAN_ACTION_START, qapp)
    assert _wait_until(qapp, lambda: runner.calls)
    _click(dialog, PLAN_ACTION_CANCEL_RUN, qapp)
    box = dialog.message_box()
    assert box is not None
    box.button(QMessageBox.StandardButton.No).click()
    _flush(qapp)

    assert dialog.execution().status is PlanExecutionStatus.RUNNING
    assert runner.stopped_calls == 0, "点了「否」不该去停引擎"
    assert dialog.is_running_plan() is True

    dialog.buttons()[PLAN_ACTION_CLOSE].click()  # 关页会把计划安全暂停
    assert dialog.wait_for_plan(30_000) is True


# --------------------------------------------------------------------------- #
# 失败：重试 / 跳过（Spec §11 / §16）
# --------------------------------------------------------------------------- #
def test_a_failed_item_offers_retry_and_skip(
    plan_service: PlanService, task_service, store, work_dir: Path, qapp
) -> None:
    """失败 → 停在「部分失败」→ 只给重试 / 跳过 / 取消。"""
    ready_plan(plan_service, work_dir, "demoland", "northland")
    runner = FakeRunner(fail_on=("demoland",), fail_times=1)
    dialog = _dialog(plan_service, task_service, store, runner)
    dialog.refresh()

    _click(dialog, PLAN_ACTION_START, qapp)
    assert _wait_until(
        qapp, lambda: dialog.execution() is not None
        and dialog.execution().status is PlanExecutionStatus.PARTIAL_FAILED
    ), "界面没有停在「部分失败」"

    buttons = dialog.run_buttons()
    assert buttons[PLAN_ACTION_RETRY].isEnabled() is True
    assert buttons[PLAN_ACTION_SKIP].isEnabled() is True
    assert buttons[PLAN_ACTION_CANCEL_RUN].isEnabled() is True
    assert buttons[PLAN_ACTION_START].isEnabled() is False, "没重试/跳过前不该再开一轮"
    assert buttons[PLAN_ACTION_PAUSE].isEnabled() is False
    # 失败原因写在状态列的 tooltip 上（不是让用户去翻日志）
    assert "500" in dialog.table().item(0, _STATUS_COLUMN).toolTip()
    assert "部分失败" in dialog.run_status_lines()[0]

    _click(dialog, PLAN_ACTION_RETRY, qapp)
    assert dialog.wait_for_plan(30_000) is True
    _flush(qapp)

    assert dialog.execution().status is PlanExecutionStatus.COMPLETED
    assert runner.calls == ["demoland", "demoland", "northland"]
    dialog.close()


def test_skipping_a_failed_item_continues_with_the_next_one(
    plan_service: PlanService, task_service, store, work_dir: Path, qapp
) -> None:
    """跳过 → 那一项标「已跳过」→ 接着跑下一项。"""
    ready_plan(plan_service, work_dir, "demoland", "northland")
    runner = FakeRunner(fail_on=("demoland",))
    dialog = _dialog(plan_service, task_service, store, runner)
    dialog.refresh()

    _click(dialog, PLAN_ACTION_START, qapp)
    assert _wait_until(
        qapp, lambda: dialog.execution() is not None
        and dialog.execution().status is PlanExecutionStatus.PARTIAL_FAILED
    )
    _click(dialog, PLAN_ACTION_SKIP, qapp)
    assert dialog.wait_for_plan(30_000) is True
    _flush(qapp)

    assert runner.calls == ["demoland", "northland"], "跳过之后该接着跑下一项"
    assert dialog.table().item(0, _STATUS_COLUMN).text() == "» 已跳过"
    assert dialog.table().item(1, _STATUS_COLUMN).text() == "✓ 已完成"
    dialog.close()


# --------------------------------------------------------------------------- #
# 关页 = 安全暂停（Spec §25）
# --------------------------------------------------------------------------- #
def test_closing_the_page_pauses_the_plan(
    plan_service: PlanService, task_service, store, work_dir: Path, qapp
) -> None:
    """关页**不是杀掉**：计划落成"可继续"，任务记录也是"已暂停"。"""
    plan = ready_plan(plan_service, work_dir, "demoland", "northland")
    runner = FakeRunner(hang=True)
    dialog = _dialog(plan_service, task_service, store, runner)
    dialog.show()  # 关页要走 closeEvent —— 得先真的显示出来
    dialog.refresh()

    _click(dialog, PLAN_ACTION_START, qapp)
    assert _wait_until(qapp, lambda: runner.calls)

    dialog.buttons()[PLAN_ACTION_CLOSE].click()
    for _ in range(200):
        qapp.processEvents()
        if not dialog.is_running_plan():
            break
        time.sleep(0.02)

    execution = store.find(plan.id)
    assert execution.status is PlanExecutionStatus.PAUSED
    assert execution.items[0].status is PlanItemRunStatus.PAUSED
    assert execution.items[1].status is PlanItemRunStatus.WAITING
    task = task_service.get_task(execution.items[0].task_id)
    assert task.status is DownloadTaskStatus.PAUSED
    assert dialog.is_running_plan() is False


# --------------------------------------------------------------------------- #
# 程序重启后的恢复（Spec §20）
# --------------------------------------------------------------------------- #
def test_a_stored_unfinished_execution_is_shown_but_not_started(
    plan_service: PlanService, task_service, store, work_dir: Path, qapp
) -> None:
    """打开这一页只把"上次没跑完"画出来，**一个请求都不发**。"""
    plan = ready_plan(plan_service, work_dir, "demoland", "northland")
    # 先跑一半：第一项跑完，第二项还没开始，然后"进程重启"
    runner = FakeRunner(hang=True)
    first = _dialog(plan_service, task_service, store, runner)
    first.refresh()
    _click(first, PLAN_ACTION_START, qapp)
    assert _wait_until(qapp, lambda: runner.calls)
    first.close()
    assert _wait_until(
        qapp, lambda: store.find(plan.id) is not None
        and store.find(plan.id).status is PlanExecutionStatus.PAUSED
    )

    # 新的一个页面 = 重新打开程序
    quiet = FakeRunner()
    second = _dialog(plan_service, task_service, store, quiet)
    second.show()  # showEvent 里读回记录
    _flush(qapp)

    assert quiet.calls == [], "读记录本身绝不允许发请求"
    execution = second.execution()
    assert execution is not None
    assert execution.status is PlanExecutionStatus.PAUSED
    assert second.run_buttons()[PLAN_ACTION_RESUME].isEnabled() is True
    assert second.run_buttons()[PLAN_ACTION_START].isEnabled() is False
    assert "已暂停" in second.run_status_lines()[0]

    # 点「继续」接着跑完
    _click(second, PLAN_ACTION_RESUME, qapp)
    assert second.wait_for_plan(30_000) is True
    _flush(qapp)
    assert quiet.calls == ["demoland", "northland"] or quiet.calls == ["northland"], (
        f"继续时只该补没下完的那些，实际跑了 {quiet.calls}"
    )
    assert second.execution().status is PlanExecutionStatus.COMPLETED
    second.close()
