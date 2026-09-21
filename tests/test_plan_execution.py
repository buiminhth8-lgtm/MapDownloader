"""计划执行的模型与聚合（Slice 21，Spec §2 / §3 / §12 / §13 / §14）。

守四件事：

1. **两个"状态"不混用**：``PlanItemStatus`` 是**可用性**（Slice 20，可用/不可用），
   ``PlanItemRunStatus`` 是**执行态**（等待/执行中/…）。本文件同时钉住两者的取值
   集合，免得将来有人"顺手合并"。
2. **进度按张数聚合，不按条目数**（Spec §12）。这是本 Slice 最容易写错的一条：
   用"完成 1 个 / 共 3 个 = 33%"也能通过所有"看起来对"的断言 —— 所以这里用
   "一个大区域 + 两个小区域"的夹具把两种算法**拉开距离**。
3. **速度只在当前任务真的在跑时才有值**（Spec §14）：任务切换、暂停、结束都要
   归零，否则界面上会挂着上一个任务的残留速度。
4. **聚合是纯函数**：不碰 Qt、不碰磁盘、不认识 ``DownloadTask`` 类型
   （用鸭子类型），于是每一条都能脱离窗口断言。

不需要真实区域目录：这些用例只造计划条目与任务替身。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.planning.download_plan import (
    DownloadPlan,
    DownloadPlanItem,
    PlanItemStatus,
)
from app.planning.plan_execution import (
    PlanExecution,
    PlanExecutionStatus,
    PlanItemExecution,
    PlanItemRunStatus,
    PlanProgress,
    build_progress,
    compute_plan_totals,
    item_tile_contribution,
    new_execution_for,
    plan_region_lookup,
    recover_running_execution,
)
from app.planning.region_level import RegionLevel


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _item(
    item_id: str,
    region_id: str,
    *,
    level: RegionLevel = RegionLevel.PROVINCE,
    tiles: int = 100,
    size: int = 1000,
    enabled: bool = True,
    valid: bool = True,
    min_zoom: int = 9,
    max_zoom: int = 13,
) -> DownloadPlanItem:
    """造一个计划条目（``valid=False`` 的条目会带上不可用原因）。"""
    item = DownloadPlanItem(
        id=item_id,
        region_id=region_id,
        region_name=f"{region_id}省",
        region_level=level,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        selected_tiles=tiles,
        estimated_bytes=size,
        candidate_tiles=tiles,
        enabled=enabled,
    )
    if not valid:
        item.mark_invalid("区域不在目录里")
    return item


def _task(completed: int = 0, total: int = 0) -> SimpleNamespace:
    """任务替身：聚合只读这几个属性（鸭子类型，不是真的 ``DownloadTask``）。"""
    return SimpleNamespace(completed_tiles=completed, total_tiles=total)


def _exec(plan: DownloadPlan, *task_ids: str) -> PlanExecution:
    """按计划条目的顺序，把 ``task_ids`` 配给启用条目（数量不足时留空）。

    ``new_execution_for`` 本身是**按 region_id 配对**的（不按下标），这里只是
    给用例省几行：造出 ``id`` / ``region_id`` 两个字段齐全的任务替身。
    """
    tasks = [
        SimpleNamespace(id=task_id, region_id=item.region_id)
        for item, task_id in zip(plan.ordered_enabled_items(), task_ids)
    ]
    return new_execution_for(plan, tasks)


def _small_plan() -> DownloadPlan:
    """一块"大区域 + 两块小区域"的计划。

    比例刻意分开：大区域 1000 张、两块小区域各 100 张。跑完大区域时，
    "按张数"是 83%，"按条目数"是 33% —— 一个写错的实现藏不住。
    """
    return DownloadPlan(
        id="plan-1",
        name="离线计划",
        items=[
            _item("i-china", "china", level=RegionLevel.COUNTRY, tiles=1000, size=10_000),
            _item("i-zj", "zhejiang", tiles=100, size=1_000),
            _item("i-hz", "hangzhou", level=RegionLevel.CITY, tiles=100, size=1_000),
        ],
    )


# --------------------------------------------------------------------------- #
# 两个状态枚举的分工（Spec §2）
# --------------------------------------------------------------------------- #
def test_the_two_item_status_enums_do_not_overlap() -> None:
    """可用性枚举与执行态枚举必须**取值不重叠**。

    它们同名容易混（Spec 把执行态也叫 PlanItemStatus），一旦有人 import 错，
    不会报错、只会在某条分支上静默走错 —— 所以这里钉住"取值完全分开"。
    """
    availability = {status.value for status in PlanItemStatus}
    execution = {status.value for status in PlanItemRunStatus}

    assert availability == {"valid", "invalid"}
    assert execution == {
        "waiting",
        "running",
        "paused",
        "completed",
        "failed",
        "cancelled",
        "skipped",
    }
    assert availability.isdisjoint(execution)


def test_run_status_has_chinese_names_and_symbols() -> None:
    """界面用的是中文名与符号，**不显示枚举名**（Slice 14 的教训）。"""
    assert PlanItemRunStatus.RUNNING.display_name == "执行中"
    assert PlanItemRunStatus.COMPLETED.display_name == "已完成"
    assert PlanItemRunStatus.SKIPPED.display_name == "已跳过"
    assert PlanItemRunStatus.COMPLETED.symbol == "✓"
    assert PlanItemRunStatus.RUNNING.symbol == "▶"
    assert PlanItemRunStatus.WAITING.symbol == "○"
    assert PlanItemRunStatus.FAILED.symbol == "✕"
    assert PlanItemRunStatus.PAUSED.symbol == "Ⅱ"
    assert len({status.symbol for status in PlanItemRunStatus}) == len(
        list(PlanItemRunStatus)
    )


def test_paused_is_pending_but_not_finished() -> None:
    """``PAUSED`` 既不是"结束"也不该被丢掉 —— 它是"继续时要捡回来的那条"。"""
    assert PlanItemRunStatus.PAUSED.is_pending is True
    assert PlanItemRunStatus.PAUSED.is_finished is False
    assert PlanItemRunStatus.WAITING.is_pending is True
    assert PlanItemRunStatus.RUNNING.is_pending is False
    assert PlanItemRunStatus.SKIPPED.is_finished is True
    assert PlanItemRunStatus.COMPLETED.is_success is True
    assert PlanItemRunStatus.SKIPPED.is_success is False


def test_the_two_execution_status_enums_do_not_overlap() -> None:
    """计划执行态与单任务状态同样**不复用取值**（Spec §2 明确要求）。"""
    from app.models.download_task import DownloadTaskStatus

    plan_values = {status.value for status in PlanExecutionStatus}
    task_values = {status.value for status in DownloadTaskStatus}

    assert "partial_failed" in plan_values
    assert "partial_failed" not in task_values
    assert "packaging" not in plan_values
    assert "validating" not in plan_values


def test_execution_status_helpers() -> None:
    """``RUNNING`` / ``PAUSED`` 是"活得"（期间禁止改计划，Spec §18）。"""
    assert PlanExecutionStatus.RUNNING.is_active is True
    assert PlanExecutionStatus.PAUSED.is_active is True
    assert PlanExecutionStatus.COMPLETED.is_active is False
    assert PlanExecutionStatus.COMPLETED.is_terminal is True
    assert PlanExecutionStatus.PARTIAL_FAILED.is_terminal is False
    assert PlanExecutionStatus.PARTIAL_FAILED.can_resume is True
    assert PlanExecutionStatus.CANCELLED.can_resume is False
    assert PlanExecutionStatus.RUNNING.display_name == "执行中"
    assert PlanExecutionStatus.PARTIAL_FAILED.display_name == "部分失败"


# --------------------------------------------------------------------------- #
# 构造与恢复（Spec §6 / §20）
# --------------------------------------------------------------------------- #
def test_new_execution_follows_the_plan_order() -> None:
    """执行记录的顺序 = 计划条目按层级排序后的顺序（Spec §13）。"""
    plan = DownloadPlan(
        id="p",
        name="n",
        items=[
            _item("i-hz", "hangzhou", level=RegionLevel.CITY, tiles=50),
            _item("i-china", "china", level=RegionLevel.COUNTRY, tiles=500),
            _item("i-zj", "zhejiang", tiles=100),
        ],
    )

    execution = _exec(plan, "t-china", "t-zj", "t-hz")

    assert execution.plan_id == "p"
    assert execution.status is PlanExecutionStatus.CREATED
    assert execution.current_item_index == -1
    assert execution.item_ids() == ["i-china", "i-zj", "i-hz"]
    assert [entry.task_id for entry in execution.items] == ["t-china", "t-zj", "t-hz"]
    assert all(
        entry.status is PlanItemRunStatus.WAITING for entry in execution.items
    )
    assert execution.total_tiles == 650


def test_new_execution_matches_tasks_by_region_not_by_position() -> None:
    """任务与条目的配对按 ``region_id``，**不按下标**。

    按"调用方恰好按计划顺序传"这种默契配对，断掉之后不会报错 ——
    表现是"任务被安到了另一块地上"，而计划照跑、界面照显示。
    这里刻意把任务**乱序**传进来，断言配对仍然正确。
    """
    plan = _small_plan()
    shuffled = [
        SimpleNamespace(id="t-hz", region_id="hangzhou"),
        SimpleNamespace(id="t-china", region_id="china"),
        SimpleNamespace(id="t-zj", region_id="zhejiang"),
    ]

    execution = new_execution_for(plan, shuffled)

    assert execution.item_ids() == ["i-china", "i-zj", "i-hz"]
    assert [entry.task_id for entry in execution.items] == ["t-china", "t-zj", "t-hz"]


def test_new_execution_skips_disabled_and_invalid_items() -> None:
    """未启用 / 不可用的条目**压根不进执行记录**（Spec §6 / §12）。"""
    plan = DownloadPlan(
        id="p",
        name="n",
        items=[
            _item("i-a", "a", tiles=100),
            _item("i-b", "b", tiles=200, enabled=False),
            _item("i-c", "c", tiles=300, valid=False),
        ],
    )

    execution = _exec(plan, "t-a")

    assert execution.item_ids() == ["i-a"]
    assert execution.total_tiles == 100


def test_new_execution_survives_a_short_task_id_list() -> None:
    """任务少给了不该炸 —— 缺的那条 ``task_id`` 保持 ``None``（"还没建任务"）。"""
    plan = _small_plan()

    execution = _exec(plan, "t-1")

    assert execution.items[0].task_id == "t-1"
    assert execution.items[1].task_id is None
    assert execution.items[2].task_id is None


def test_recovering_a_running_execution_downgrades_to_paused() -> None:
    """规则：遗留 ``RUNNING`` → ``PAUSED``，**绝不自动联网**（Spec §20）。"""
    execution = _exec(_small_plan(), "t1", "t2", "t3")
    execution.set_status(PlanExecutionStatus.RUNNING)
    execution.current_item_index = 1
    execution.mark(0, PlanItemRunStatus.COMPLETED)
    execution.mark(1, PlanItemRunStatus.RUNNING)

    recovered, changed = recover_running_execution(execution)

    assert changed is True
    assert recovered.status is PlanExecutionStatus.PAUSED
    assert recovered.items[1].status is PlanItemRunStatus.PAUSED
    # 已经完成的那条一个字都不动 —— 恢复不是"重来一遍"
    assert recovered.items[0].status is PlanItemRunStatus.COMPLETED
    assert recovered.items[2].status is PlanItemRunStatus.WAITING


def test_recovering_an_already_paused_execution_changes_nothing() -> None:
    """幂等：已经是 ``PAUSED`` 时 ``changed=False``（调用方据此决定要不要提示）。"""
    execution = _exec(_small_plan(), "t1", "t2", "t3")
    execution.set_status(PlanExecutionStatus.PAUSED)

    recovered, changed = recover_running_execution(execution)

    assert changed is False
    assert recovered.status is PlanExecutionStatus.PAUSED


# --------------------------------------------------------------------------- #
# 逐条贡献（Spec §12）
# --------------------------------------------------------------------------- #
def test_a_finished_item_contributes_its_full_share() -> None:
    item = _item("i", "r", tiles=500)
    assert item_tile_contribution(item, _task(completed=500, total=500)) == 500


def test_a_half_done_item_contributes_what_it_has() -> None:
    """半截的成果也是成果：失败在 90% 记 0 会让界面像"什么都没干"。"""
    item = _item("i", "r", tiles=500)
    assert item_tile_contribution(item, _task(completed=450, total=500)) == 450


def test_an_item_without_a_task_contributes_nothing() -> None:
    assert item_tile_contribution(_item("i", "r", tiles=500), None) == 0


def test_contribution_is_capped_by_the_denominator() -> None:
    """分子来自任务记录、分母来自估算，两者不一致时不能算出 >100%。"""
    item = _item("i", "r", tiles=100)
    assert item_tile_contribution(item, _task(completed=999, total=999)) == 100


def test_an_unestimated_item_is_not_capped() -> None:
    """没估过的条目（分母 0）不做收口，否则它的进度永远是 0。"""
    item = _item("i", "r", tiles=0)
    assert item_tile_contribution(item, _task(completed=37, total=37)) == 37


def test_plan_totals_follow_tiles_not_item_counts() -> None:
    """**Spec §12 的核心**：进度按张数聚合，不是"完成几个 / 共几个"。

    跑完 1000 张的那一块、另外两块各 100 张没跑 → 应为 83%，而不是 33%。
    """
    plan = _small_plan()
    execution = _exec(plan, "t1", "t2", "t3")
    execution.mark(0, PlanItemRunStatus.COMPLETED)
    tasks = {"t1": _task(completed=1000, total=1000)}

    total, completed = compute_plan_totals(plan, execution, tasks)

    assert total == 1200
    assert completed == 1000
    assert completed / total == pytest.approx(0.8333, abs=1e-4)
    # 按条目数算会是 1/3 —— 明确断言两者不同，免得将来被"优化"回去
    assert completed / total != pytest.approx(1 / 3, abs=0.1)


def test_disabled_items_are_not_in_the_denominator() -> None:
    """被停用的条目既不在分子也不在分母（Spec §12）。"""
    plan = DownloadPlan(
        id="p",
        name="n",
        items=[_item("i-a", "a", tiles=100), _item("i-b", "b", tiles=900, enabled=False)],
    )
    execution = _exec(plan, "t-a")

    total, completed = compute_plan_totals(plan, execution, {})

    assert total == 100
    assert completed == 0


def test_items_missing_from_the_plan_are_ignored_on_both_sides() -> None:
    """计划里已经没有那一项时，两个数都别算它（同源才可比）。"""
    plan = _small_plan()
    execution = _exec(plan, "t1", "t2", "t3")
    execution.mark(0, PlanItemRunStatus.COMPLETED)
    plan.remove_region("china")

    total, completed = compute_plan_totals(plan, execution, {"t1": _task(1000, 1000)})

    assert total == 200
    assert completed == 0


# --------------------------------------------------------------------------- #
# 聚合进度（Spec §12 / §13 / §14）
# --------------------------------------------------------------------------- #
def test_progress_shows_the_current_item_and_the_whole_plan() -> None:
    """Spec §16 的那一屏：当前任务 50%、整个计划 23% —— 两个数字都要有。"""
    plan = _small_plan()
    execution = _exec(plan, "t1", "t2", "t3")
    execution.set_status(PlanExecutionStatus.RUNNING)
    execution.current_item_index = 1
    execution.mark(0, PlanItemRunStatus.COMPLETED)
    execution.mark(1, PlanItemRunStatus.RUNNING)
    tasks = {
        "t1": _task(completed=1000, total=1000),
        "t2": _task(completed=50, total=100),
    }

    progress = build_progress(
        plan,
        execution,
        tasks=tasks,
        current_task_percent=50.0,
        current_speed_tiles_per_second=12.0,
    )

    assert progress.total_items == 3
    assert progress.items_text == "2 / 3"
    assert progress.current_region_name == "zhejiang省"
    assert progress.current_zoom_text == "Z9~13"
    assert progress.current_text == "zhejiang省 Z9~13"
    assert progress.current_task_percent == 50.0
    assert progress.total_tiles == 1200
    assert progress.completed_tiles == 1050
    assert progress.plan_percent == pytest.approx(87.5, abs=1e-6)
    assert progress.status is PlanExecutionStatus.RUNNING


def test_speed_is_zero_when_the_current_item_is_not_running() -> None:
    """Spec §14：计划速度 = **当前任务**速度；没有当前任务就是 0。

    任务切换 / 暂停 / 结束之后残留的速度会让用户以为"还在下"。
    """
    plan = _small_plan()
    execution = _exec(plan, "t1", "t2", "t3")
    execution.current_item_index = 0
    execution.mark(0, PlanItemRunStatus.COMPLETED)

    progress = build_progress(
        plan,
        execution,
        tasks={"t1": _task(1000, 1000)},
        current_speed_tiles_per_second=99.0,
    )

    assert progress.speed_tiles_per_second == 0.0
    assert progress.speed_bytes_per_second == 0.0
    assert progress.speed_text == "—"


def test_speed_uses_the_current_items_average_tile_size() -> None:
    """字节速度 = 瓦片速度 × 当前项的平均瓦片大小（估算的产物，界面只当参考）。"""
    plan = _small_plan()
    execution = _exec(plan, "t1", "t2", "t3")
    execution.set_status(PlanExecutionStatus.RUNNING)
    execution.current_item_index = 0
    execution.mark(0, PlanItemRunStatus.RUNNING)

    progress = build_progress(
        plan,
        execution,
        tasks={"t1": _task(100, 1000)},
        current_speed_tiles_per_second=100.0,
    )

    # 第一项：1000 张 / 10,000 字节 → 每张 10 字节；100 张/秒 → 1000 字节/秒
    assert progress.speed_bytes_per_second == pytest.approx(1000.0)
    assert progress.speed_text.endswith("/s")


def test_progress_percent_is_100_when_everything_is_done() -> None:
    plan = _small_plan()
    execution = _exec(plan, "t1", "t2", "t3")
    for index in range(3):
        execution.mark(index, PlanItemRunStatus.COMPLETED)
    tasks = {
        "t1": _task(1000, 1000),
        "t2": _task(100, 100),
        "t3": _task(100, 100),
    }

    progress = build_progress(plan, execution, tasks=tasks)

    assert progress.completed_tiles == progress.total_tiles == 1200
    assert progress.plan_percent == pytest.approx(100.0)


def test_progress_without_a_denominator_is_zero_not_a_crash() -> None:
    """一份没估算过的计划不该让界面崩在除零上。"""
    plan = DownloadPlan(id="p", name="n", items=[_item("i", "r", tiles=0)])

    progress = build_progress(plan, _exec(plan, "t1"), tasks={})

    assert progress.total_tiles == 0
    assert progress.plan_percent == 0.0
    assert progress.current_text == "—"


def test_downloaded_bytes_adds_measured_and_estimated() -> None:
    """已完成的写实测大小，在跑的按比例估算（两个来源在结果里相加）。"""
    plan = _small_plan()
    execution = _exec(plan, "t1", "t2", "t3")
    execution.set_status(PlanExecutionStatus.RUNNING)
    execution.current_item_index = 1
    execution.mark(0, PlanItemRunStatus.COMPLETED)
    execution.mark(1, PlanItemRunStatus.RUNNING)
    tasks = {"t1": _task(1000, 1000), "t2": _task(50, 100)}

    progress = build_progress(
        plan,
        execution,
        tasks=tasks,
        downloaded_bytes_by_task={"t1": 9_876},
    )

    # 第一项实测 9,876；第二项按 50% × 1,000 字节估算 = 500
    assert progress.downloaded_bytes == 9_876 + 500


def test_progress_lines_are_human_readable() -> None:
    """``as_lines`` 是给界面与验收脚本看的那一屏（Spec §16）。"""
    plan = _small_plan()
    execution = _exec(plan, "t1", "t2", "t3")
    execution.set_status(PlanExecutionStatus.RUNNING)
    execution.current_item_index = 0
    execution.mark(0, PlanItemRunStatus.RUNNING)

    progress = build_progress(
        plan,
        execution,
        tasks={"t1": _task(400, 1000)},
        current_task_percent=40.0,
    )
    text = "\n".join(progress.as_lines())

    assert "状态：执行中" in text
    assert "当前：china省 Z9~13" in text
    assert "任务：1 / 3" in text
    assert "当前任务：40%" in text
    assert "总体：33%" in text
    for machine in ("PlanExecutionStatus", "PlanItemRunStatus", "running", "None"):
        assert machine not in text


# --------------------------------------------------------------------------- #
# 记录本身的行为
# --------------------------------------------------------------------------- #
def test_pending_index_returns_the_first_item_that_still_needs_running() -> None:
    execution = _exec(_small_plan(), "t1", "t2", "t3")
    execution.mark(0, PlanItemRunStatus.COMPLETED)

    assert execution.pending_index() == 1

    execution.mark(1, PlanItemRunStatus.PAUSED)
    assert execution.pending_index() == 1, "暂停过的条目要被捡回来（Spec §9）"

    execution.mark(1, PlanItemRunStatus.SKIPPED)
    assert execution.pending_index() == 2

    execution.mark(2, PlanItemRunStatus.CANCELLED)
    assert execution.pending_index() == -1


def test_reset_for_retry_makes_the_item_runnable_again() -> None:
    execution = _exec(_small_plan(), "t1", "t2", "t3")
    execution.mark(1, PlanItemRunStatus.FAILED, "网络断了")

    assert execution.reset_for_retry(1) is True

    assert execution.items[1].status is PlanItemRunStatus.PAUSED
    assert execution.items[1].error is None, "重试要先清掉上一次的失败原因"
    assert execution.current_item_index == 1
    assert execution.reset_for_retry(9) is False


def test_counters_classify_items() -> None:
    execution = _exec(_small_plan(), "t1", "t2", "t3")
    execution.mark(0, PlanItemRunStatus.COMPLETED)
    execution.mark(1, PlanItemRunStatus.FAILED, "x")
    execution.mark(2, PlanItemRunStatus.SKIPPED)
    execution.current_item_index = 2

    assert execution.completed_count == 1
    assert execution.failed_count == 1
    assert execution.unresolved_count == 2
    assert execution.is_finished is True
    assert execution.items_text == "3 / 3"


def test_items_text_never_shows_zero_based_numbers() -> None:
    """界面上不该出现「0 / 3」这种下标 —— 用户数的是第几个，不是下标。"""
    execution = _exec(_small_plan(), "t1", "t2", "t3")
    assert execution.items_text == "1 / 3"

    empty = PlanExecution(plan_id="p")
    assert empty.items_text == "0 / 0"


def test_item_execution_describe_has_no_machine_codes() -> None:
    entry = PlanItemExecution(
        "i-1", "t-1", PlanItemRunStatus.FAILED, "磁盘满了"
    )

    text = entry.describe(_item("i-1", "zhejiang", tiles=100))

    assert "失败" in text and "磁盘满了" in text
    assert "failed" not in text
    assert "PlanItemRunStatus" not in text


def test_execution_describe_is_one_line() -> None:
    execution = _exec(_small_plan(), "t1", "t2", "t3")
    text = execution.describe()

    assert "\n" not in text
    assert "计划 plan-1" in text


def test_statuses_parse_leniently_when_read_back() -> None:
    """从文件读回来的那一侧要宽容：认不出来就落到"还没开始"。

    这两个字段本来就不写盘（状态是每次打开重新判定的），因此认不出来只可能
    是有人手工改了文件 —— 那时"按未开始处理"比抛异常好。
    """
    assert (
        PlanItemExecution("i", "t", "running").status is PlanItemRunStatus.RUNNING
    )
    assert PlanItemExecution("i", "t", "不认识").status is PlanItemRunStatus.WAITING
    assert PlanItemExecution("i", "t", None).status is PlanItemRunStatus.WAITING
    assert (
        PlanExecution(plan_id="p", status="paused").status
        is PlanExecutionStatus.PAUSED
    )
    assert (
        PlanExecution(plan_id="p", status="???").status
        is PlanExecutionStatus.CREATED
    )


def test_mark_clears_a_stale_error() -> None:
    entry = PlanItemExecution("i", "t", PlanItemRunStatus.FAILED, "旧原因")
    entry.mark(PlanItemRunStatus.RUNNING)
    assert entry.error is None


def test_progress_defaults_are_empty_but_safe() -> None:
    progress = PlanProgress()

    assert progress.items_text == "0 / 0"
    assert progress.current_text == "—"
    assert progress.speed_text == "—"
    assert progress.tiles_text == "0 / 0"


def test_plan_region_lookup_matches_the_execution_order() -> None:
    plan = _small_plan()
    lookup = plan_region_lookup(plan)

    assert list(lookup) == ["i-china", "i-zj", "i-hz"]
    assert lookup["i-zj"] is plan.item("zhejiang")
