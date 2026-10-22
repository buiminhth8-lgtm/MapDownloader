"""下载计划的逐项风险与启动保护（Slice 23，Spec §15 / §16）。

三块：

1. **风险列与合计**：表格每一行给出等级（带记号），合计行带**总风险**
   （合计档与逐项最高档取高者）；禁用项不进合计，不可用项显示 ``—``
   （它连估算都没有，"正常"会是在替一块算不出来的地打包票）。
2. **启动闸门**：``DANGEROUS`` 走手输确认词，``LARGE`` 走四选一
   （仍然继续 / 采用推荐 Zoom / 停用危险项 / 返回修改），``NORMAL`` 直接跑。
3. **两个"改计划"的去处真的会改计划**：采用推荐 Zoom 会改区间并**作废估算**
   （所以立刻重算），停用危险项会让合计与风险立刻变小。改完都**不启动** ——
   用户拿到的是一个小了/清楚了的计划，要不要跑由他再点一次决定。

夹具把"已估算"的数字直接写在条目上（:func:`_stub_estimate`）：本文件守的是
"风险 → 界面 + 闸门"，不是估算链路本身（那是 ``test_plan_estimator.py`` 的事）。
数字都取**阈值两侧**的真实值，因此策略表改了这里会跟着失效并暴露出来。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config.settings import (
    DANGEROUS_CONFIRM_WORD,
    UI_TEXT_BUTTON_APPLY_RECOMMENDED_ALL,
    UI_TEXT_BUTTON_BACK_TO_EDIT,
    UI_TEXT_BUTTON_CONTINUE_ANYWAY,
    UI_TEXT_BUTTON_DISABLE_DANGEROUS,
    UI_TEXT_RISK_PLAN_CONFIRM_TITLE,
)
from app.geo.region_estimator import ZoomRegionStats
from app.geo.tile_math import estimate_size_bytes
from app.planning.plan_service import PlanService
from app.planning.risk_level import RiskLevel
from app.planning.risk_policy import DANGEROUS_TILE_THRESHOLD, LARGE_TILE_THRESHOLD
from app.ui.download_plan_dialog import (
    PLAN_ACTION_START,
    DownloadPlanDialog,
)
from app.ui.risk_details_dialog import PlanRiskConfirmDialog
from tests.plan_fakes import FakeRunner
from tests.test_download_plan_dialog import (  # noqa: F401 - 夹具注入
    _add,
    _flush,
    plan_service,
    service,
    task_service,
)
from tests.test_download_plan_execution_ui import _wait_until

#: 表格里各列的下标（与 ``_COLUMNS`` 一致）
_STATUS_COLUMN = 5
_RISK_COLUMN = 6

#: 两次用例都用的两个规模：一个正常，一个危险（阈值之上）
_SMALL_TILES = 500
_DANGEROUS_TILES = DANGEROUS_TILE_THRESHOLD  # 1000 万张 → 危险
_LARGE_TILES = LARGE_TILE_THRESHOLD  # 10 万张 → 较大（容量也过线）


def _stub_estimate(
    plan_service: PlanService,
    region_id: str,
    tiles: int,
    *,
    min_zoom: int = 4,
    max_zoom: int = 18,
) -> None:
    """把"这一项已经估算过"这件事直接写在条目上。

    风险分析与合计读的就是这几个字段（``selected_tiles`` / ``estimated_bytes`` /
    ``per_zoom``），因此这里不必真跑估算器，也就不必等后台线程。
    """
    item = plan_service.plan.item(region_id)
    assert item is not None, region_id
    item.min_zoom = min_zoom
    item.max_zoom = max_zoom
    item.selected_tiles = int(tiles)
    item.estimated_bytes = estimate_size_bytes(int(tiles))
    item.candidate_tiles = int(tiles)
    item.per_zoom = (
        ZoomRegionStats(
            zoom=max_zoom,
            candidate_tiles=int(tiles),
            selected_tiles=int(tiles),
            filtered_tiles=0,
        ),
    )
    item.mark_valid()
    plan_service.plan.recalculate_totals()


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def dialog(plan_service: PlanService, task_service, work_dir: Path, runner, qapp):
    """带调度器（假执行者）、**不自动估算**的计划页。

    先把计划的名称与输出目录补齐：校验器（``check_output=True``）会把"没有
    输出目录"判成不通过，而**不可用的条目不参与合计** —— 那样一测就全成了
    "计划没有任何风险"，用例反而会在错误的地方通过。
    """
    from app.planning.plan_execution_store import PlanExecutionStore
    from app.planning.plan_scheduler import PlanScheduler

    plan_service.apply_settings(name="离线计划", output_dir=work_dir / "output")
    store = PlanExecutionStore(work_dir / "plan_executions.json")

    def factory() -> PlanScheduler:
        return PlanScheduler(
            plan_service,
            runner,
            task_service=task_service,
            store=store,
            save_interval_seconds=0.0,
            save_tile_interval=1,
        )

    page = DownloadPlanDialog(
        plan_service, auto_estimate=False, scheduler_factory=factory
    )
    yield page
    if page.risk_confirm_dialog() is not None:
        page.risk_confirm_dialog().close()
    page.close()
    qapp.processEvents()


def _risk_cell(dialog: DownloadPlanDialog, region_id: str) -> str:
    row = dialog.row_of(region_id)
    assert row >= 0, region_id
    return dialog.table().item(row, _RISK_COLUMN).text()


def _risk_box(dialog: DownloadPlanDialog) -> PlanRiskConfirmDialog:
    """当前挂着的那一屏风险确认（没弹出来就直接失败，别静默跳过）。

    计划级的确认**不是** ``QMessageBox``：那一屏里要装一个确认词输入框，
    标准按钮拼不出来（见 :class:`PlanRiskConfirmDialog` 的说明）。
    """
    box = dialog.risk_confirm_dialog()
    assert isinstance(box, PlanRiskConfirmDialog), "没有弹出计划风险确认框"
    return box


def _click(dialog: DownloadPlanDialog, button_of, qapp) -> None:
    """点确认框上的某个按钮（按**按钮**点，证明它真的接上了）。

    ``button_of`` 是"从一个确认窗口取出要点的那个按钮"的函数，
    因此用例里写的是 ``lambda box: box.apply_zoom_button()`` ——
    与界面上那个按钮是同一个对象，不是按退出码伪造一次"用户选了它"。
    """
    button_of(_risk_box(dialog)).click()
    _flush(qapp)


# --------------------------------------------------------------------------- #
# 风险列与合计（Spec §15）
# --------------------------------------------------------------------------- #
def test_the_table_has_a_risk_column_with_the_level_and_symbol(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    """每一行给出等级 + 记号（Spec §11：不许只靠颜色）。"""
    _add(plan_service, "demoland", "northland")
    _stub_estimate(plan_service, "demoland", _SMALL_TILES)
    _stub_estimate(plan_service, "northland", _DANGEROUS_TILES)
    dialog.refresh()

    headers = [
        dialog.table().horizontalHeaderItem(index).text()
        for index in range(dialog.table().columnCount())
    ]
    assert headers[_RISK_COLUMN] == "风险"
    assert _risk_cell(dialog, "demoland") == "正常 ✓"
    assert _risk_cell(dialog, "northland") == "危险 ✕"


def test_the_totals_line_carries_the_plan_risk_and_names_the_dangerous_items(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    """合计行要说清总风险，并**点名**是谁把等级抬起来的（Spec §15 / §16）。"""
    _add(plan_service, "demoland", "northland")
    _stub_estimate(plan_service, "demoland", _SMALL_TILES)
    _stub_estimate(plan_service, "northland", _DANGEROUS_TILES)
    dialog.refresh()

    text = dialog.totals_label().text()
    risk = dialog.plan_risk()
    assert risk is not None
    assert risk.level is RiskLevel.DANGEROUS
    assert "风险：危险（DANGEROUS）" in text
    assert risk.level.symbol in text
    assert "危险任务：" in text
    assert "示例北省" in text


def test_a_disabled_item_leaves_the_plan_risk_but_keeps_its_own_badge(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    """停用项不进合计（它不会下载），但那一行的等级照旧显示。

    显示它是有意的：用户停用一块地之后往往还要回头看一眼"到底是哪一块那么大"。
    "不参与合计"与"看不出来有多大"是两件事。
    """
    _add(plan_service, "demoland", "northland")
    _stub_estimate(plan_service, "demoland", _SMALL_TILES)
    _stub_estimate(plan_service, "northland", _DANGEROUS_TILES)
    dialog.refresh()
    assert dialog.plan_risk().level is RiskLevel.DANGEROUS

    plan_service.set_enabled("northland", False)
    dialog.refresh()

    risk = dialog.plan_risk()
    assert risk is not None
    assert risk.level is RiskLevel.NORMAL
    assert "风险：正常（NORMAL）" in dialog.totals_label().text()
    assert "危险任务：" not in dialog.totals_label().text()
    assert _risk_cell(dialog, "northland") == "危险 ✕"
    assert _risk_cell(dialog, "demoland") == "正常 ✓"


def test_an_invalid_item_shows_a_dash_in_the_risk_column(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    """不可用项连估算都没有 → ``—``（不许替一块算不出来的地写"正常"）。"""
    _add(plan_service, "demoland", "northland")
    _stub_estimate(plan_service, "demoland", _SMALL_TILES)
    _stub_estimate(plan_service, "northland", _DANGEROUS_TILES)
    plan_service.plan.item("northland").mark_invalid("边界文件缺失")
    dialog.refresh()

    assert _risk_cell(dialog, "northland") == "—"
    assert dialog.plan_risk().level is RiskLevel.NORMAL


# --------------------------------------------------------------------------- #
# 启动闸门（Spec §12 / §16）
# --------------------------------------------------------------------------- #
def test_a_normal_plan_starts_without_asking(
    plan_service: PlanService, dialog: DownloadPlanDialog, runner, qapp
) -> None:
    """``NORMAL``：不弹框，直接跑（而且**真的跑到了那一项**）。"""
    _add(plan_service, "demoland")
    _stub_estimate(plan_service, "demoland", _SMALL_TILES)
    dialog.refresh()

    dialog.run_buttons()[PLAN_ACTION_START].click()
    _flush(qapp)

    assert dialog._message_box is None
    assert dialog.risk_confirm_dialog() is None
    # 线程是异步起的：等它真的开工并把这一项跑掉。只断言"没弹窗"分辨不出
    # "闸门放行了"与"根本没启动"—— 后者在界面上同样是"点了没反应"。
    assert _wait_until(qapp, lambda: runner.calls) is True, "计划没有真的开跑"
    assert dialog.wait_for_plan(30_000) is True, "计划线程没有跑完"
    _flush(qapp)
    assert runner.calls == ["demoland"]
    assert dialog.execution() is not None


def test_a_large_plan_offers_four_ways_out(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    """``LARGE``：四选一，按钮文案写清动作与范围。"""
    _add(plan_service, "northland")
    _stub_estimate(plan_service, "northland", _LARGE_TILES, min_zoom=9, max_zoom=13)
    dialog.refresh()

    dialog.run_buttons()[PLAN_ACTION_START].click()
    _flush(qapp)

    box = _risk_box(dialog)
    assert box.windowTitle() == UI_TEXT_RISK_PLAN_CONFIRM_TITLE
    assert box.continue_button().text() == UI_TEXT_BUTTON_CONTINUE_ANYWAY
    assert box.apply_zoom_button().text() == UI_TEXT_BUTTON_APPLY_RECOMMENDED_ALL
    assert box.disable_button().text() == UI_TEXT_BUTTON_DISABLE_DANGEROUS
    assert box.back_button().text() == UI_TEXT_BUTTON_BACK_TO_EDIT
    # ``LARGE`` 只问"怎么处理"，不该顺带多问一遍确认词：手输确认词是危险档的事
    assert box.requires_word is False
    assert box.word_edit() is None
    assert box.continue_button().isEnabled() is True
    # 默认落在"返回修改"：误按回车不该把一份大计划跑起来
    assert box.back_button().isDefault() is True
    assert dialog.is_running_plan() is False

    _click(dialog, lambda box: box.continue_button(), qapp)
    assert dialog.risk_confirm_dialog() is None
    assert _wait_until(qapp, lambda: dialog.execution() is not None) is True


def test_returning_to_edit_changes_nothing(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    """「返回修改」：一个数字都不动，也不启动。"""
    _add(plan_service, "northland")
    _stub_estimate(plan_service, "northland", _LARGE_TILES, min_zoom=9, max_zoom=13)
    dialog.refresh()
    before = dialog.plan.total_tiles

    dialog.run_buttons()[PLAN_ACTION_START].click()
    _flush(qapp)
    _click(dialog, lambda box: box.back_button(), qapp)

    assert dialog.plan.total_tiles == before
    assert dialog.plan.item("northland").max_zoom == 13
    assert dialog.is_running_plan() is False
    assert dialog.execution() is None


def test_applying_the_recommended_zoom_rewrites_the_plan_and_re_estimates(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    """「采用推荐 Zoom」：换成推荐区间、作废旧估算、立刻重算，本轮不启动。"""
    _add(plan_service, "northland")  # 省级，推荐 Z9~13
    _stub_estimate(plan_service, "northland", _LARGE_TILES, min_zoom=9, max_zoom=18)
    dialog.refresh()
    assert dialog.plan_risk().level is not RiskLevel.NORMAL

    dialog.run_buttons()[PLAN_ACTION_START].click()
    _flush(qapp)
    _click(dialog, lambda box: box.apply_zoom_button(), qapp)

    item = dialog.plan.item("northland")
    assert (item.min_zoom, item.max_zoom) == (9, 13)
    # 改 Zoom 作废旧估算：这些数字属于旧区间，留着比"—"更危险
    assert item.selected_tiles == 0
    assert dialog.execution() is None, "这一轮不该启动下载"
    # 而且必须**立刻**重新估算（否则用户看着旧数字再点一次开始）
    assert dialog.is_estimating() is True
    dialog.cancel_estimate()


def test_disabling_dangerous_items_shrinks_the_plan(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    """「停用危险项」：危险项被停用，合计与风险立刻变小，本轮不启动。"""
    _add(plan_service, "demoland", "northland")
    _stub_estimate(plan_service, "demoland", _SMALL_TILES)
    _stub_estimate(plan_service, "northland", _DANGEROUS_TILES)
    dialog.refresh()

    dialog.run_buttons()[PLAN_ACTION_START].click()
    _flush(qapp)
    _click(dialog, lambda box: box.disable_button(), qapp)

    assert dialog.plan.item("northland").enabled is False
    assert dialog.plan.total_tiles == _SMALL_TILES
    assert dialog.plan_risk().level is RiskLevel.NORMAL
    assert dialog.execution() is None


def test_starting_a_dangerous_plan_needs_the_confirmation_word(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    """``DANGEROUS``：手输确认词才开跑；执行期间不再弹任何框（Spec §16）。"""
    _add(plan_service, "northland")
    _stub_estimate(plan_service, "northland", _DANGEROUS_TILES)
    dialog.refresh()

    dialog.run_buttons()[PLAN_ACTION_START].click()
    _flush(qapp)

    window = _risk_box(dialog)
    assert window.report.level is RiskLevel.DANGEROUS
    assert window.requires_word is True
    assert dialog.is_running_plan() is False
    assert dialog._message_box is None, "危险档不该只弹一个普通的确认框"
    # 四个去处照样在（危险档不是"只有一个确认键"）
    assert window.apply_zoom_button().isEnabled() is True
    assert window.disable_button().isEnabled() is True

    # 没敲对就不给走
    assert window.continue_button().isEnabled() is False
    window.word_edit().setText("nope")
    assert window.continue_button().isEnabled() is False
    assert dialog.is_running_plan() is False

    window.word_edit().setText(DANGEROUS_CONFIRM_WORD)
    assert window.continue_button().isEnabled() is True
    window.continue_button().click()
    _flush(qapp)

    assert _wait_until(qapp, lambda: dialog.execution() is not None) is True
    # 执行中不再弹窗（"确认过的计划"不该被自己的提示打断）
    assert dialog.risk_confirm_dialog() is None
    assert dialog._message_box is None


def test_cancelling_the_strong_confirmation_never_starts(
    plan_service: PlanService, dialog: DownloadPlanDialog, qapp
) -> None:
    """强确认框上点「返回修改」（或直接关掉）＝不执行。"""
    _add(plan_service, "northland")
    _stub_estimate(plan_service, "northland", _DANGEROUS_TILES)
    dialog.refresh()

    dialog.run_buttons()[PLAN_ACTION_START].click()
    _flush(qapp)
    _click(dialog, lambda box: box.back_button(), qapp)

    assert dialog.is_running_plan() is False
    assert dialog.execution() is None


# --------------------------------------------------------------------------- #
# 计划层：改 Zoom（Spec §12）
# --------------------------------------------------------------------------- #
def test_set_zoom_rewrites_the_range_and_drops_the_estimate(
    plan_service: PlanService
) -> None:
    _add(plan_service, "northland")
    _stub_estimate(plan_service, "northland", _LARGE_TILES, min_zoom=9, max_zoom=18)

    assert plan_service.set_zoom("northland", 9, 13) is True

    item = plan_service.plan.item("northland")
    assert (item.min_zoom, item.max_zoom) == (9, 13)
    assert item.selected_tiles == 0  # 估算作废（改 Zoom 就是换了一批瓦片）


def test_set_zoom_is_a_no_op_when_the_range_is_unchanged(
    plan_service: PlanService
) -> None:
    """值没变就**什么都不做** —— 不许顺手把已经算好的估算清掉。"""
    _add(plan_service, "northland")
    _stub_estimate(plan_service, "northland", _LARGE_TILES, min_zoom=9, max_zoom=13)

    assert plan_service.set_zoom("northland", 9, 13) is False

    item = plan_service.plan.item("northland")
    assert item.selected_tiles == _LARGE_TILES
    assert plan_service.set_zoom("nowhere", 9, 13) is False


def test_set_zoom_validates_before_touching_anything(
    plan_service: PlanService
) -> None:
    """非法 Zoom 必须先报错、后动手：否则会留下"配置没变、估算没了"的条目。"""
    _add(plan_service, "northland")
    _stub_estimate(plan_service, "northland", _LARGE_TILES, min_zoom=9, max_zoom=13)

    with pytest.raises(ValueError):
        plan_service.set_zoom("northland", 18, 4)

    item = plan_service.plan.item("northland")
    assert (item.min_zoom, item.max_zoom) == (9, 13)
    assert item.selected_tiles == _LARGE_TILES
