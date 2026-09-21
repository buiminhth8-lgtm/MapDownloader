"""下载计划页（Slice 20，Spec §10 / §11 / §12 / §19 / §21）。

这一层测的是"**用户真的点得动吗**"——Spec §11 列的那几个按钮，每一个都要
有一条"点一下并断言结果"的用例。理由（Slice 14 的教训）：Qt 槽里抛的异常会被
静默吞掉，于是"按钮不好使"在数据层用例里永远看不见；而计划页最容易出的缺陷
恰恰是"点了没反应"（选中态没拿到、勾选框的回调被 `_refreshing` 挡掉、
生成任务时校验没通过却什么都不说）。

同时守住两条界面契约：

- **不出现机器码**（Spec §9 / Slice 14）：站在真实控件上，把所有可见文本扫一遍
  ``PlanIssueCode`` 的每个 ``value``。
- **表格里显示的是人话**（Spec §11）：状态列写"已停用（不参与本次生成）"，
  而不是 ``INVALID``。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from app.models.download_task import DownloadTaskStatus
from app.planning.download_plan import DownloadPlanItem
from app.planning.plan_estimator import DownloadPlanEstimator
from app.planning.plan_service import PlanService
from app.planning.plan_store import PlanStore
from app.planning.plan_validator import PlanIssueCode
from app.regions.service import RegionService
from app.tasks.task_service import TaskStore
from app.tasks.task_service import TaskService
from app.ui.download_plan_dialog import (
    PLAN_ACTION_CLEAR,
    PLAN_ACTION_ESTIMATE,
    PLAN_ACTION_GENERATE,
    PLAN_ACTION_REMOVE,
    DownloadPlanDialog,
)


class _PacedEstimator(DownloadPlanEstimator):
    """真估算，但每次检查取消之前停一下。

    目的只有一个：让"点取消"这件事在用例里**可复现**。否则三个合成区域的估算
    几毫秒就跑完，取消信号无论多早发出都晚了一步，用例会变成偶发红
    （而且失败时看起来像"取消失效"这个产品缺陷）。
    """

    #: 每次迭代前的停顿（秒）
    pause = 0.25

    def estimate(self, plan, *, should_cancel=None, progress=None):  # type: ignore[override]
        def slowed() -> bool:
            time.sleep(self.pause)
            return bool(should_cancel()) if should_cancel is not None else False

        return super().estimate(plan, should_cancel=slowed, progress=progress)


@pytest.fixture
def service(region_resources: Path) -> RegionService:
    return RegionService()


@pytest.fixture
def task_service(tasks_file: Path) -> TaskService:
    return TaskService(TaskStore(tasks_file))


def _make_service(
    service: RegionService,
    plans_file: Path,
    task_service: TaskService,
    *,
    paced: bool = False,
) -> PlanService:
    estimator = _PacedEstimator(service) if paced else DownloadPlanEstimator(service)
    return PlanService(
        store=PlanStore(plans_file),
        service=service,
        task_service=task_service,
        estimator=estimator,
    )


@pytest.fixture
def plan_service(
    service: RegionService, plans_file: Path, task_service: TaskService
) -> PlanService:
    return _make_service(service, plans_file, task_service)


@pytest.fixture
def dialog(plan_service: PlanService, qapp) -> DownloadPlanDialog:
    """**不自动估算**的对话框：把"估算"这一步握在用例手里。"""
    page = DownloadPlanDialog(plan_service, auto_estimate=False)
    yield page
    page.close()


def _add(plan_service: PlanService, *region_ids: str) -> None:
    for region_id in region_ids:
        plan_service.add_region(region_id)


def _flush(qapp, rounds: int = 4) -> None:
    for _ in range(rounds):
        qapp.processEvents()


# --------------------------------------------------------------------------- #
# 列表与合计（Spec §11 / §12）
# --------------------------------------------------------------------------- #
def test_rows_follow_the_hierarchy_order(
    plan_service: PlanService, dialog: DownloadPlanDialog
) -> None:
    _add(plan_service, "rivertown", "demoland", "northland")

    dialog.refresh()

    assert dialog.row_region_ids() == ["demoland", "northland", "rivertown"]
    assert dialog.table().rowCount() == 3
    assert dialog.table().item(0, 0).text() == "示例国"
    assert dialog.table().item(1, 1).text() == "省级"
    assert dialog.table().item(2, 2).text() == "Z12~16"


def test_unestimated_rows_show_a_dash_not_a_zero(
    plan_service: PlanService, dialog: DownloadPlanDialog
) -> None:
    """还没算过就显示 ``—``：写 ``0 张`` 会让用户以为"这块地没有瓦片"。"""
    _add(plan_service, "demoland")

    dialog.refresh()

    assert dialog.table().item(0, 3).text() == "—"
    assert dialog.table().item(0, 4).text() == "—"
    assert dialog.table().item(0, 5).text() == "尚未估算"


def test_checkbox_toggles_the_item_and_the_totals(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    """勾选框 = 启用 / 停用（Spec §12）：点一下要真的落到计划上。"""
    _add(plan_service, "demoland", "northland")
    dialog.refresh()
    assert plan_service.plan.enabled_count == 2

    dialog.table().item(1, 0).setCheckState(Qt.CheckState.Unchecked)
    _flush(qapp)

    assert plan_service.plan.item("northland").enabled is False
    assert plan_service.plan.enabled_count == 1
    assert "另有 1 项未启用" in dialog.totals_label().text()
    # 关掉的那一行还在（Spec §12：留在计划里，随时能开回来）
    assert dialog.row_region_ids() == ["demoland", "northland"]
    assert "已停用" in dialog.table().item(1, 5).text()

    dialog.table().item(1, 0).setCheckState(Qt.CheckState.Checked)
    _flush(qapp)

    assert plan_service.plan.item("northland").enabled is True
    assert plan_service.plan.enabled_count == 2


def test_checkbox_change_is_saved_to_disk(
    plan_service: PlanService, plans_file: Path, dialog: DownloadPlanDialog, qapp
) -> None:
    """界面上勾一下也要落盘 —— 否则重启后用户的选择凭空失效。"""
    _add(plan_service, "demoland", "northland")
    dialog.refresh()

    dialog.table().item(1, 0).setCheckState(Qt.CheckState.Unchecked)
    _flush(qapp)

    reloaded = PlanStore(plans_file).load_all()[-1]
    assert reloaded.item("northland").enabled is False


def test_totals_reflect_the_estimates(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    _add(plan_service, "demoland", "northland")
    dialog.estimate()
    assert dialog.wait_for_estimate() is True
    _flush(qapp)

    plan = plan_service.plan
    assert plan.total_tiles_text in dialog.totals_label().text()
    assert plan.size_text in dialog.totals_label().text()


def test_coverage_hint_appears_for_three_levels(
    plan_service: PlanService, dialog: DownloadPlanDialog
) -> None:
    """Spec §19：三级都在时说一句"已形成三级覆盖"（只是提示，不是硬依赖）。"""
    _add(plan_service, "demoland", "northland", "rivertown")

    dialog.refresh()

    assert "三级" in dialog._coverage_label.text()
    assert "全国级" in dialog._coverage_label.text()


def test_no_coverage_hint_for_a_single_level(
    plan_service: PlanService, dialog: DownloadPlanDialog
) -> None:
    _add(plan_service, "northland")

    dialog.refresh()

    assert dialog._coverage_label.text() == ""


# --------------------------------------------------------------------------- #
# 估算（Spec §20 / §21）
# --------------------------------------------------------------------------- #
def test_estimate_button_fills_every_row(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    """点「重新估算」→ 每一行都从 ``—`` 变成真实数字，进度条走完。"""
    _add(plan_service, "demoland", "northland", "rivertown")
    dialog.refresh()

    dialog.buttons()[PLAN_ACTION_ESTIMATE].click()
    _flush(qapp)
    assert dialog.wait_for_estimate() is True
    _flush(qapp)

    assert dialog.progress_bar().value() == 100
    assert "估算完成" in dialog.status_label().text()
    for row in range(dialog.table().rowCount()):
        assert dialog.table().item(row, 3).text() != "—"
        assert dialog.table().item(row, 5).text() == "可参与下载"
    assert plan_service.plan.needs_estimate() is False


def test_auto_estimate_on_open(plan_service: PlanService, qapp) -> None:
    """首次打开自动算一次：一页全是 ``—`` 的界面几乎等于没有信息。"""
    _add(plan_service, "northland")
    page = DownloadPlanDialog(plan_service, auto_estimate=True)
    try:
        page.show()
        _flush(qapp)
        assert page.wait_for_estimate() is True
        _flush(qapp)
        assert page.table().item(0, 3).text() != "—"
    finally:
        page.close()


def test_the_estimate_button_is_disabled_while_running(
    service: RegionService,
    plans_file: Path,
    task_service: TaskService,
    qapp,
) -> None:
    """估算期间换班：重新估算禁用、取消估算可用（Spec §21 的入口）。"""
    plan_service = _make_service(service, plans_file, task_service, paced=True)
    _add(plan_service, "demoland", "northland")
    page = DownloadPlanDialog(plan_service, auto_estimate=False)
    try:
        page.estimate()
        _flush(qapp)
        assert page.is_estimating() is True
        assert page.buttons()[PLAN_ACTION_ESTIMATE].isEnabled() is False
        assert page._cancel_estimate_button.isEnabled() is True
        page.cancel_estimate()
        assert page.wait_for_estimate() is True
        _flush(qapp)
        assert page.is_estimating() is False
        assert page.buttons()[PLAN_ACTION_ESTIMATE].isEnabled() is True
    finally:
        page.close()


def test_cancel_keeps_the_plan_configuration(
    service: RegionService,
    plans_file: Path,
    task_service: TaskService,
    qapp,
) -> None:
    """Spec §21：取消估算**不是**取消计划 —— 条目 / Zoom / 启用状态一个字都不动。"""
    plan_service = _make_service(service, plans_file, task_service, paced=True)
    _add(plan_service, "demoland", "northland", "rivertown")
    page = DownloadPlanDialog(plan_service, auto_estimate=False)
    try:
        before = [
            (item.region_id, item.min_zoom, item.max_zoom, item.enabled)
            for item in plan_service.plan.items
        ]
        page.estimate()
        _flush(qapp)
        page.cancel_estimate()
        assert page.wait_for_estimate() is True
        _flush(qapp)

        after = [
            (item.region_id, item.min_zoom, item.max_zoom, item.enabled)
            for item in plan_service.plan.items
        ]
        assert after == before
        assert "已取消估算" in page.status_label().text()
        assert "计划配置" in page.status_label().text() or "未改动" in page.status_label().text()
        assert page.table().rowCount() == 3
        assert page.buttons()[PLAN_ACTION_ESTIMATE].isEnabled() is True
    finally:
        page.close()


def test_estimate_on_an_empty_plan_says_what_to_do(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    """空计划点「重新估算」：给一句话，而不是起一个什么都不会做的线程。"""
    dialog.buttons()[PLAN_ACTION_ESTIMATE].click()
    _flush(qapp)

    assert dialog.is_estimating() is False
    assert "还没有区域" in dialog.status_label().text()


# --------------------------------------------------------------------------- #
# 移除 / 清空（Spec §11）
# --------------------------------------------------------------------------- #
def test_remove_button_drops_the_selected_row(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    _add(plan_service, "demoland", "northland", "rivertown")
    dialog.refresh()
    dialog.table().selectRow(1)

    dialog.buttons()[PLAN_ACTION_REMOVE].click()
    _flush(qapp)

    assert dialog.row_region_ids() == ["demoland", "rivertown"]
    assert plan_service.plan.item("northland") is None
    assert "已移除" in dialog.status_label().text()


def test_remove_without_a_selection_changes_nothing(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    """没选中就点「移除」：说一句怎么用，别默默什么都不干。"""
    _add(plan_service, "demoland", "northland")
    dialog.refresh()
    dialog.table().clearSelection()
    dialog.table().setCurrentCell(-1, -1)

    dialog.buttons()[PLAN_ACTION_REMOVE].click()
    _flush(qapp)

    assert plan_service.plan.count == 2
    assert "选中一行" in dialog.status_label().text()


def test_clear_asks_before_wiping(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    """清空是不可撤销的 → 先问一句；答"否"时一个区域都不许少。"""
    _add(plan_service, "demoland", "northland")
    dialog.refresh()

    dialog.buttons()[PLAN_ACTION_CLEAR].click()
    _flush(qapp)

    box = dialog.message_box()
    assert box is not None, "清空前应当弹确认框"
    assert "清空" in box.windowTitle()
    box.button(QMessageBox.StandardButton.No).click()
    _flush(qapp)

    assert plan_service.plan.count == 2

    dialog.buttons()[PLAN_ACTION_CLEAR].click()
    _flush(qapp)
    dialog.message_box().button(QMessageBox.StandardButton.Yes).click()
    _flush(qapp)

    assert plan_service.plan.is_empty is True
    assert dialog.table().rowCount() == 0
    assert "还没有区域" in dialog.totals_label().text()


def test_clear_on_an_empty_plan_does_not_ask(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    dialog.buttons()[PLAN_ACTION_CLEAR].click()
    _flush(qapp)

    assert dialog.message_box() is None
    assert "本来就是空的" in dialog.status_label().text()


# --------------------------------------------------------------------------- #
# 生成任务（Spec §14 / §15 / §16）
# --------------------------------------------------------------------------- #
def test_generate_button_creates_one_task_per_enabled_item(
    plan_service: PlanService,
    task_service: TaskService,
    dialog: DownloadPlanDialog,
    qapp,
) -> None:
    """点「生成任务」→ 每条启用项一条记录，并且**停在"已创建"**。"""
    _add(plan_service, "demoland", "northland")
    dialog.estimate()
    assert dialog.wait_for_estimate() is True
    _flush(qapp)
    created: list[list] = []
    dialog.tasks_created.connect(created.append)

    dialog.buttons()[PLAN_ACTION_GENERATE].click()
    _flush(qapp)

    assert len(created) == 1
    tasks = created[0]
    assert [task.region_id for task in tasks] == ["demoland", "northland"]
    assert all(task.status is DownloadTaskStatus.CREATED for task in tasks)
    assert len(task_service.list_tasks()) == 2
    box = dialog.message_box()
    assert box is not None
    assert "2 条下载任务" in box.text()
    assert "尚未开始下载" in dialog.status_label().text()


def test_generate_never_creates_the_output_directory(
    plan_service: PlanService, dialog: DownloadPlanDialog, work_dir: Path, qapp
) -> None:
    """Spec §16：只创建任务配置，连目录都留给下载阶段去建。"""
    plan_service.apply_settings(name="计划A", output_dir=work_dir / "out")
    _add(plan_service, "demoland")
    dialog.estimate()
    assert dialog.wait_for_estimate() is True
    _flush(qapp)

    dialog.buttons()[PLAN_ACTION_GENERATE].click()
    _flush(qapp)

    assert (work_dir / "out").exists() is False
    assert list(work_dir.glob("**/*.mbtiles")) == []


def test_generate_button_is_disabled_when_the_plan_is_not_ready(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    """没估算过 → 生成按钮是灰的（点不动比点了报错更好）。"""
    _add(plan_service, "demoland")
    dialog.refresh()

    assert dialog.buttons()[PLAN_ACTION_GENERATE].isEnabled() is False


def test_generate_reports_a_validation_failure_without_creating_tasks(
    plan_service: PlanService,
    task_service: TaskService,
    dialog: DownloadPlanDialog,
    qapp,
) -> None:
    """校验不过时直接调（绕过按钮禁用）→ 必须说清原因，且一条都不建。"""
    _add(plan_service, "demoland")
    plan_service.plan.add_item(
        DownloadPlanItem(region_id="nowhere", region_name="不存在的地区")
    )
    dialog.refresh()

    dialog.generate_tasks()
    _flush(qapp)

    assert task_service.list_tasks() == []
    box = dialog.message_box()
    assert box is not None
    assert "无法生成下载任务" in box.windowTitle()
    assert "不存在的地区" in box.text()
    assert "一个任务都没有创建" in dialog.status_label().text()


# --------------------------------------------------------------------------- #
# 借来的坏条目要显示人话（Spec §11 / §18）
# --------------------------------------------------------------------------- #
def test_invalid_item_shows_the_human_reason(
    plan_service: PlanService,
    dialog: DownloadPlanDialog,
    region_resources: Path,
    qapp,
) -> None:
    _add(plan_service, "demoland", "northland")
    (region_resources / "province" / "northland.geojson").unlink()

    dialog.refresh()
    _flush(qapp)

    cell = dialog.table().item(1, 5).text()
    assert cell != "可参与下载"
    assert "边界" in cell  # 人话里带着原因，而不是 INVALID
    assert "INVALID" not in cell


def test_the_dialog_text_never_contains_a_machine_code(
    plan_service: PlanService,
    dialog: DownloadPlanDialog,
    region_resources: Path,
) -> None:
    """站在**界面**位置扫描所有可见文本（Spec §9 / Slice 14 的教训）。

    把表格每一格、合计行、问题清单、状态行、进度条文案拼起来，再拿
    ``PlanIssueCode`` 的每个 ``value`` 去扫 —— 机器码在模型层是合理的，
    只有"进了界面"才算违规。
    """
    _add(plan_service, "demoland", "rivertown")
    plan_service.plan.items.append(
        DownloadPlanItem(region_id="nowhere", region_name="不存在的地区")
    )
    (region_resources / "city" / "rivertown.geojson").unlink()
    dialog.refresh()

    visible: list[str] = []
    for row in range(dialog.table().rowCount()):
        for column in range(dialog.table().columnCount()):
            item = dialog.table().item(row, column)
            if item is not None:
                visible.append(item.text())
    visible.extend(
        [
            dialog.totals_label().text(),
            dialog.issues_label().text(),
            dialog.status_label().text(),
            dialog._header_label.text(),
            dialog._coverage_label.text(),
            dialog.progress_bar().format(),
            *[button.text() for button in dialog.buttons().values()],
        ]
    )
    text = "\n".join(visible)
    assert "边界" in text  # 夹具确实造出了"缺边界"这个错误
    for code in PlanIssueCode:
        assert code.value not in text, f"界面里出现了机器码 {code.value}"


def test_issues_area_lists_errors_and_warnings(
    plan_service: PlanService, dialog: DownloadPlanDialog
) -> None:
    """问题清单要按"错误 / 提醒"分栏（Spec §9 的 errors + warnings）。"""
    _add(plan_service, "demoland")

    dialog.refresh()
    assert dialog.issues_label().text().startswith("错误：")

    dialog.estimate()
    assert dialog.wait_for_estimate() is True
    dialog.refresh()
    assert "检查通过" in dialog.issues_label().text()


# --------------------------------------------------------------------------- #
# 关闭
# --------------------------------------------------------------------------- #
def test_closing_the_page_does_not_touch_the_plan(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    _add(plan_service, "demoland", "northland")
    dialog.refresh()

    dialog.close()
    _flush(qapp)

    assert plan_service.plan.count == 2


def test_all_buttons_are_labelled_in_chinese(
    plan_service: PlanService, dialog: DownloadPlanDialog
) -> None:
    """按钮文案必须是中文 —— 尤其「关闭」。

    这条是**验收脚本看截图**才发现的（`pytest` 里同样是英文，因为 offscreen
    与真实平台都不带 ``qt_zh_CN.qm``）：原本用的是
    ``QDialogButtonBox.StandardButton.Close``，Qt 找不到翻译文件就把按钮画成
    ``Close``。项目既有窗口（Slice 9 / 10）都是用显式
    ``QPushButton(UI_TEXT_BUTTON_CLOSE)`` 绕开的，这里必须一致。
    """
    from PySide6.QtWidgets import QDialogButtonBox

    from app.config.settings import UI_TEXT_BUTTON_CLOSE
    from app.ui.download_plan_dialog import PLAN_ACTION_CLOSE

    texts = {button.text() for button in dialog.buttons().values()}
    assert all(any("\u4e00" <= char <= "\u9fff" for char in text) for text in texts), texts
    assert dialog.buttons()[PLAN_ACTION_CLOSE].text() == UI_TEXT_BUTTON_CLOSE
    assert dialog.findChildren(QDialogButtonBox) == [], "不该再用标准按钮盒子"


def test_the_close_button_closes_the_page_and_stops_the_estimate(
    service: RegionService, plans_file: Path, task_service: TaskService, qapp
) -> None:
    """点「关闭」要真的走 ``closeEvent``：否则估算线程会在页面关掉之后继续跑。"""
    from app.ui.download_plan_dialog import PLAN_ACTION_CLOSE

    plan_service = _make_service(service, plans_file, task_service, paced=True)
    _add(plan_service, "demoland", "northland")
    page = DownloadPlanDialog(plan_service, auto_estimate=False)
    page.show()
    page.estimate()
    _flush(qapp)
    assert page.is_estimating() is True

    page.buttons()[PLAN_ACTION_CLOSE].click()
    _flush(qapp)

    assert page.is_estimating() is False
    assert page.isVisible() is False
    assert plan_service.plan.count == 2


def test_closing_while_estimating_stops_the_thread(
    service: RegionService, plans_file: Path, task_service: TaskService, qapp
) -> None:
    """关页面时线程必须收干净：销毁一个还在跑的 ``QThread`` 会直接崩进程。"""
    plan_service = _make_service(service, plans_file, task_service, paced=True)
    _add(plan_service, "demoland", "northland")
    page = DownloadPlanDialog(plan_service, auto_estimate=False)
    page.estimate()
    _flush(qapp)
    assert page.is_estimating() is True

    page.close()
    _flush(qapp)

    assert page.is_estimating() is False
    assert plan_service.plan.count == 2
