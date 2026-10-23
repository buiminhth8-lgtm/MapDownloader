"""风险界面与启动保护（Slice 23，Spec §10 ~ §14）。

三块，各守一件事：

1. **面板的「风险提示」区**：只在有结论时出现（与「下载结果」同一约定），
   等级行带**机器可读名与记号**（``风险：危险（DANGEROUS）✕``，
   Spec §11 要求不许只靠颜色），「采用推荐 Zoom」在"没有推荐区间 / 已在推荐
   区间"时置灰；**用户换区域就作废**（旧结论不能配新配置）。
2. **详情窗口**：逐 Zoom 明细表来自报告的 ``per_zoom``（**不重新遍历瓦片**），
   四列表头与「推荐 / 高于推荐 / —」三态；表里的数字必须与面板同一份格式化。
3. **启动保护**：``NORMAL`` 直接跑、``LARGE`` 普通确认、``VERY_LARGE`` 警告确认、
   ``DANGEROUS`` **必须手输确认词**；而且每份任务**只过一道闸门**
   （10 万张的任务不该被连问两次）。

关于"注入报告 / 注入策略"
------------------------

等级判定本身由 ``test_risk_policy.py`` / ``test_download_risk_analyzer.py``
守，这里守的是**等级 → 界面行为**这条链路。因此绝大多数用例用一个
"真实报告 + 指定等级"（:func:`_report_at`）来驱动，另有一条
（``test_a_real_strict_policy_reaches_the_strong_confirmation``）从真阈值出发
走完整条链路，防止"注入得太假，真链路接不上"。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox

import app.ui.main_window as main_window_module
from app.config.settings import DANGEROUS_CONFIRM_WORD
from app.geo import RegionType
from app.geo.region_estimator import ZoomRegionStats
from app.geo.tile_math import estimate_size_bytes, format_size, format_tile_count
from app.planning.download_risk_analyzer import DownloadRiskAnalyzer
from app.planning.region_level import RegionLevel
from app.planning.risk_level import RiskLevel
from app.planning.risk_models import DownloadRiskReport
from app.planning.risk_policy import DEFAULT_RISK_POLICY, RiskPolicy
from app.planning.zoom_strategy import ZoomStrategyService
from app.tasks.task_service import TaskService, TaskStore
from app.ui.download_panel import (
    ACTION_CALCULATE,
    ACTION_RISK_APPLY_ZOOM,
    ACTION_RISK_DETAILS,
    ACTION_START,
    DownloadPanel,
)
from app.ui.main_window import MainWindow
from app.ui.region_selection import ZoomMode
from app.ui.risk_details_dialog import DangerousTaskConfirmDialog, RiskDetailsDialog

FAKE_TOKEN = "0" * 32


# --------------------------------------------------------------------------- #
# 夹具与构造工具
# --------------------------------------------------------------------------- #
@pytest.fixture
def window(qapp, tasks_file: Path, preferences, region_resources: Path) -> MainWindow:
    """主窗口 + 合成区域目录 + 临时任务文件（与 Slice 20 的 UI 用例同一套）。

    ``region_resources`` 必须在**构造窗口之前**生效：面板的区域服务跟随
    ``app.geo.region.REGION_RESOURCE_DIR`` 这个全局，构造后再换就等于
    "界面上的目录"与"服务读的目录"不是同一个。
    """
    win = MainWindow(
        task_service=TaskService(TaskStore(tasks_file)), preferences=preferences
    )
    yield win
    win._exit_confirmed = True
    for dialog in (win._risk_dialog, win._confirm_dialog, win._dangerous_dialog):
        if dialog is not None:
            dialog.close()
    win.close()
    qapp.processEvents()


@pytest.fixture
def error_dialogs(monkeypatch, window: MainWindow) -> list[tuple[str, str]]:
    """把非模态错误弹窗替换成记录器（避免用例被弹窗干扰）。"""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window,
        "_show_error_dialog",
        lambda title, message: calls.append((title, message)),
    )
    return calls


def _flush(qapp, rounds: int = 4) -> None:
    for _ in range(rounds):
        qapp.processEvents()


def _fill_small_rectangle(window: MainWindow, work_dir: Path) -> None:
    """一个 0.1°×0.1° / Z12 的小矩形（**6 张**瓦片，纯算术、不枚举）。

    矩形区域是 CUSTOM 等级（不套用等级建议），因此这条夹具天然覆盖
    "自定义区域没有推荐区间"的那条分支；需要推荐区间的用例走
    :func:`_city_report`。
    """
    panel = window.download_panel
    panel._token_edit.setText(FAKE_TOKEN)
    panel._west_spin.setValue(116.0)
    panel._south_spin.setValue(39.0)
    panel._east_spin.setValue(116.1)
    panel._north_spin.setValue(39.1)
    panel._min_zoom_spin.setValue(12)
    panel._max_zoom_spin.setValue(12)
    panel._output_edit.setText(str(work_dir))
    panel.map_name_edit.setText("risk-demo")


class _FakeEstimate:
    """鸭子类型的估算对象：只有风险分析器要读的三个字段（Spec §21 的契约）。

    写这个而不是去搭一个真区域 + 真几何，是因为这里要的不是"估算对不对"
    （那是 ``test_plan_estimator.py`` 的事），而是"逐 Zoom 明细直接来自
    ``per_zoom``"。字段少了分析器会抛 ValueError —— 那条由分析器自己的用例守。
    """

    def __init__(self, tiles_per_zoom: list[tuple[int, int]]) -> None:
        self.per_zoom = tuple(
            ZoomRegionStats(
                zoom=zoom, candidate_tiles=tiles, selected_tiles=tiles, filtered_tiles=0
            )
            for zoom, tiles in tiles_per_zoom
        )
        self.selected_tiles = sum(tiles for _zoom, tiles in tiles_per_zoom)
        self.estimated_size_bytes = estimate_size_bytes(self.selected_tiles)
        self.region_name = "示例河市"


def _city_report(max_zoom: int = 18) -> DownloadRiskReport:
    """城市级（推荐 Z12~15）的真实报告：逐级张数往上翻，因此 Z16 起超推荐。"""
    per_zoom = [(zoom, 1_000 * (zoom - 11)) for zoom in range(12, max_zoom + 1)]
    return DownloadRiskAnalyzer(strategy=ZoomStrategyService()).analyze(
        RegionLevel.CITY, 12, max_zoom, _FakeEstimate(per_zoom)
    )


def _report_at(level: RiskLevel, monkeypatch: pytest.MonkeyPatch, window: MainWindow):
    """把"这次的风险结论"强制成指定等级（真实报告，只改等级与确认标记）。

    确认标记也一起从策略层取（``DEFAULT_RISK_POLICY``），而不是在用例里写
    ``requires_confirmation=True`` —— 那两个布尔值是**策略推出来的**，
    用例手写就等于绕开了被测的那条推导。
    """
    report = replace(
        _city_report(),
        level=level,
        requires_confirmation=DEFAULT_RISK_POLICY.requires_confirmation(level),
        requires_strong_confirmation=DEFAULT_RISK_POLICY.requires_strong_confirmation(
            level
        ),
    )
    monkeypatch.setattr(window, "_plan_risk", lambda plan: report)
    return report


# --------------------------------------------------------------------------- #
# 面板：风险提示区（Spec §10 / §11）
# --------------------------------------------------------------------------- #
def test_the_risk_area_is_hidden_until_there_is_a_result(qapp) -> None:
    """没有结论时不占地方（与「下载结果」「天地图连接」同一处理）。"""
    panel = DownloadPanel()

    assert panel.risk_group().isHidden() is True
    assert panel.risk_label().text() == "尚未分析（请先点「计算」）"
    assert panel.risk_buttons()[ACTION_RISK_APPLY_ZOOM].isEnabled() is False

    panel.set_risk_report(_city_report())
    assert panel.risk_group().isHidden() is False
    assert panel.risk_buttons()[ACTION_RISK_APPLY_ZOOM].isEnabled() is True

    panel.clear_calculation_result()
    assert panel.risk_group().isHidden() is True
    assert panel.risk_label().text() == "尚未分析（请先点「计算」）"
    assert panel.risk_buttons()[ACTION_RISK_APPLY_ZOOM].isEnabled() is False


def test_the_risk_line_carries_the_level_name_and_symbol(qapp) -> None:
    """等级行要有机器可读名与记号：Spec §11 不许只靠颜色表达风险。"""
    panel = DownloadPanel()
    report = _city_report()
    panel.set_risk_report(report)

    first_line = panel.risk_label().text().splitlines()[0]
    assert first_line == f"{report.level_line} {report.level.symbol}"
    assert report.level.name in first_line  # 例：LARGE / VERY_LARGE / DANGEROUS
    assert "预计瓦片：" in panel.risk_label().text()
    assert report.tiles_text in panel.risk_label().text()
    assert report.size_text in panel.risk_label().text()
    # 真的超出推荐区间时，那一行要在（用户才知道"大是从哪一级来的"）
    assert "高于推荐 Zoom 的瓦片：" in panel.risk_label().text()


def test_the_apply_button_is_grey_without_a_recommendation(qapp) -> None:
    """自定义区域不套用等级建议 → 没有可采用的推荐值，按钮置灰（不隐藏）。"""
    panel = DownloadPanel()
    report = DownloadRiskAnalyzer().analyze(
        RegionLevel.CUSTOM, 12, 18, _FakeEstimate([(zoom, 500) for zoom in range(12, 19)])
    )
    assert report.has_recommendation is False

    panel.set_risk_report(report)
    assert panel.risk_buttons()[ACTION_RISK_APPLY_ZOOM].isEnabled() is False
    assert "无需调整" in panel.risk_buttons()[ACTION_RISK_APPLY_ZOOM].toolTip()


def test_the_panel_shows_the_risk_right_after_calculating(
    window: MainWindow, work_dir: Path, qapp, error_dialogs
) -> None:
    """「计算」之后风险提示立刻出来（不必等到点「开始下载」）。"""
    _fill_small_rectangle(window, work_dir)
    window.download_panel.action_buttons[ACTION_CALCULATE].click()
    _flush(qapp)

    report = window._last_risk
    assert report is not None
    assert window.download_panel.risk_group().isHidden() is False
    assert report.level_line in window.download_panel.risk_label().text()
    # 同一份结论也要进日志（用户事后能翻到"当时为什么说是正常"）
    log = window.log_panel.to_text()
    assert "风险分析：" in log
    assert report.level_text in log


def test_switching_the_region_throws_the_risk_away(
    window: MainWindow, work_dir: Path, qapp, error_dialogs
) -> None:
    """旧结论不能配新配置：换区域类型 → 风险区立刻收起（Spec §16 的同一规则）。"""
    _fill_small_rectangle(window, work_dir)
    window.download_panel.action_buttons[ACTION_CALCULATE].click()
    _flush(qapp)
    assert window.download_panel.risk_group().isHidden() is False

    assert window.download_panel.set_region_type(RegionType.POLYGON) is True
    _flush(qapp)

    assert window._last_risk is None
    assert window.download_panel.risk_group().isHidden() is True


# --------------------------------------------------------------------------- #
# 详情窗口（Spec §14）
# --------------------------------------------------------------------------- #
def test_the_details_dialog_lists_every_zoom(qapp) -> None:
    """四列表头 + 每级一行 + 合计一行，数字与面板同一份格式化。"""
    report = _city_report()
    dialog = RiskDetailsDialog(report)
    rows = dialog.table_text()

    assert rows[0] == ["Zoom", "选中瓦片", "预计容量", "推荐"]
    assert len(rows) == len(report.per_zoom) + 2  # 表头 + 逐级 + 合计
    first = report.per_zoom[0]
    assert rows[1][0] == f"z{int(first.zoom)}"
    assert rows[1][1] == format_tile_count(int(first.selected_tiles))
    assert rows[1][2] == format_size(estimate_size_bytes(int(first.selected_tiles)))
    assert rows[-1][0] == "合计"
    assert rows[-1][1] == report.tiles_text
    assert rows[-1][2] == report.size_text


def test_the_recommended_column_has_three_states(qapp) -> None:
    """推荐列只说策略判据：推荐 / 高于推荐 / —（自定义区域不评价）。"""
    city = _city_report()
    dialog = RiskDetailsDialog(city)
    marks = {row[0]: row[3] for row in dialog.table_text()[1:-1]}

    assert marks["z12"] == "推荐"
    assert marks[f"z{city.recommended_max_zoom}"] == "推荐"
    assert marks[f"z{city.recommended_max_zoom + 1}"] == "高于推荐"

    custom = DownloadRiskAnalyzer().analyze(
        RegionLevel.CUSTOM, 12, 13, _FakeEstimate([(12, 500), (13, 900)])
    )
    custom_dialog = RiskDetailsDialog(custom)
    assert all(row[3] == "—" for row in custom_dialog.table_text()[1:-1])
    # 合计行不评价（它不是一个 Zoom 级别）
    assert custom_dialog.table_text()[-1][3] == ""


def test_the_details_entry_reuses_one_dialog_and_follows_the_new_report(
    window: MainWindow, work_dir: Path, qapp, error_dialogs
) -> None:
    """详情窗口由面板按钮打开，结论换了就地换成新报告（不留旧数字）。"""
    _fill_small_rectangle(window, work_dir)
    window.download_panel.action_buttons[ACTION_CALCULATE].click()
    _flush(qapp)

    window.download_panel.risk_buttons()[ACTION_RISK_DETAILS].click()
    _flush(qapp)
    dialog = window._risk_dialog
    assert isinstance(dialog, RiskDetailsDialog)
    assert dialog.report is window._last_risk

    # 再点一次：复用同一个窗口（位置与大小都还在）
    window.download_panel.risk_buttons()[ACTION_RISK_DETAILS].click()
    _flush(qapp)
    assert window._risk_dialog is dialog

    # 重新计算（换了 Zoom）之后再打开：窗口里的报告必须跟着换
    window.download_panel._max_zoom_spin.setValue(13)
    window._last_risk = None
    window.download_panel.action_buttons[ACTION_CALCULATE].click()
    _flush(qapp)
    window.download_panel.risk_buttons()[ACTION_RISK_DETAILS].click()
    _flush(qapp)
    assert window._risk_dialog.report is window._last_risk
    assert window._risk_dialog.report is not dialog.report


# --------------------------------------------------------------------------- #
# 「采用推荐 Zoom」（Spec §11：只有用户点才会改）
# --------------------------------------------------------------------------- #
def test_applying_the_recommended_zoom_only_happens_on_click(
    window: MainWindow, qapp, error_dialogs, monkeypatch
) -> None:
    """分析本身不改用户的输入；点了按钮才切到「按推荐值」并重算。"""
    assert window.download_panel.set_region_id("rivertown") is True
    _flush(qapp)
    panel = window.download_panel
    panel.set_zoom_range(12, 18)  # 用户自己填的一对值（CUSTOM 模式）

    recomputed: list[int] = []
    monkeypatch.setattr(window, "_run_tile_math", lambda: recomputed.append(1))

    report = _city_report()
    window._last_risk = report
    panel.set_risk_report(report)

    # 只"看"了一眼结论：两个 Zoom 输入框一个都没动
    assert [spin.value() for spin in panel.zoom_spins] == [12, 18]
    assert panel.zoom_mode is ZoomMode.CUSTOM
    assert recomputed == []

    panel.risk_buttons()[ACTION_RISK_APPLY_ZOOM].click()
    _flush(qapp)

    assert panel.zoom_mode is ZoomMode.RECOMMENDED
    assert [spin.value() for spin in panel.zoom_spins] == [12, 15]
    assert recomputed == [1]  # 改完立刻重算（否则界面会停在旧张数上）
    assert "采用推荐 Zoom" in window.log_panel.to_text()


# --------------------------------------------------------------------------- #
# 启动保护（Spec §12）
# --------------------------------------------------------------------------- #
def test_a_normal_task_starts_without_asking(
    window: MainWindow, work_dir: Path, qapp, error_dialogs, monkeypatch
) -> None:
    """``NORMAL``：一个弹窗都不弹，直接开跑。"""
    launched: list[object] = []
    monkeypatch.setattr(window, "_launch_download", launched.append)

    _fill_small_rectangle(window, work_dir)
    window.download_panel.action_buttons[ACTION_START].click()
    _flush(qapp)

    assert len(launched) == 1
    assert window._confirm_dialog is None
    assert window._dangerous_dialog is None


def test_a_large_task_asks_once_before_starting(
    window: MainWindow, work_dir: Path, qapp, error_dialogs, monkeypatch
) -> None:
    """``LARGE``：普通确认（问号图标 + 报告的正文 + 一句"要不要开始"）。"""
    report = _report_at(RiskLevel.LARGE, monkeypatch, window)
    launched: list[object] = []
    monkeypatch.setattr(window, "_launch_download", launched.append)

    _fill_small_rectangle(window, work_dir)
    window.download_panel.action_buttons[ACTION_START].click()
    _flush(qapp)

    dialog = window._confirm_dialog
    assert dialog is not None
    assert launched == []  # 还没跑，等用户回答
    assert dialog.icon() == QMessageBox.Icon.Question
    text = dialog.text()
    assert report.level_line in text
    assert "确认开始下载吗？" in text
    assert report.reasons[0] in text  # 原因逐条带出来，不是一句"你点多了"

    dialog.button(QMessageBox.StandardButton.Yes).click()
    _flush(qapp)
    assert len(launched) == 1
    assert window._pending_plan is None


def test_a_very_large_task_is_a_warning_not_a_question(
    window: MainWindow, work_dir: Path, qapp, error_dialogs, monkeypatch
) -> None:
    """``VERY_LARGE``：图标与标题都要变成"警告"（Spec §12 分了三档）。"""
    _report_at(RiskLevel.VERY_LARGE, monkeypatch, window)
    _fill_small_rectangle(window, work_dir)
    window.download_panel.action_buttons[ACTION_START].click()
    _flush(qapp)

    dialog = window._confirm_dialog
    assert dialog is not None
    assert dialog.icon() == QMessageBox.Icon.Warning
    assert dialog.windowTitle() == "任务规模警告"

    dialog.button(QMessageBox.StandardButton.No).click()
    _flush(qapp)
    assert window._download_worker is None
    assert "用户取消了下载" in window.log_panel.to_text()


def test_only_one_gate_fires_per_task(
    window: MainWindow, work_dir: Path, qapp, error_dialogs, monkeypatch
) -> None:
    """风险闸门与旧的大任务闸门**不叠加**：确认一次就开跑。

    把旧阈值的常量改成 1 张（让两道闸门同时"想"拦这个任务），确认之后必须是
    "已启动"而不是"又弹了一个框" —— 否则一个 20 万张的任务会被连问两次。
    """
    monkeypatch.setattr(main_window_module, "GUI_LARGE_TASK_CONFIRM_TILES", 1)
    _report_at(RiskLevel.LARGE, monkeypatch, window)
    launched: list[object] = []
    monkeypatch.setattr(window, "_launch_download", launched.append)

    _fill_small_rectangle(window, work_dir)
    window.download_panel.action_buttons[ACTION_START].click()
    _flush(qapp)

    dialog = window._confirm_dialog
    assert dialog is not None
    dialog.button(QMessageBox.StandardButton.Yes).click()
    _flush(qapp)

    assert len(launched) == 1
    assert window._confirm_dialog is None
    assert window._pending_plan is None


def test_a_dangerous_task_needs_the_confirmation_word(
    window: MainWindow, work_dir: Path, qapp, error_dialogs, monkeypatch
) -> None:
    """``DANGEROUS``：强确认 —— 输入框不对就点不动「仍然继续」。"""
    _report_at(RiskLevel.DANGEROUS, monkeypatch, window)
    launched: list[object] = []
    monkeypatch.setattr(window, "_launch_download", launched.append)

    _fill_small_rectangle(window, work_dir)
    window.download_panel.action_buttons[ACTION_START].click()
    _flush(qapp)

    dialog = window._dangerous_dialog
    assert isinstance(dialog, DangerousTaskConfirmDialog)
    assert launched == []
    # 普通确认框**不**应该出现（否则用户会以为点一下就过去了）
    assert window._confirm_dialog is None

    # 一开始就不给点；写错、写小写都不给点
    assert dialog.continue_button().isEnabled() is False
    dialog.word_edit().setText("yes")
    assert dialog.continue_button().isEnabled() is False
    dialog.word_edit().setText(DANGEROUS_CONFIRM_WORD.lower())
    assert dialog.continue_button().isEnabled() is False

    # 确认词正确之后才能点，点了才真的开跑
    dialog.word_edit().setText(DANGEROUS_CONFIRM_WORD)
    assert dialog.continue_button().isEnabled() is True
    dialog.continue_button().click()
    _flush(qapp)

    assert len(launched) == 1
    assert "用户已通过强确认" in window.log_panel.to_text()


def test_a_dangerous_task_rejected_never_starts(
    window: MainWindow, work_dir: Path, qapp, error_dialogs, monkeypatch
) -> None:
    """取消（或直接关窗）＝不下载：一个请求都不发，任务记录也不留。"""
    _report_at(RiskLevel.DANGEROUS, monkeypatch, window)
    launched: list[object] = []
    monkeypatch.setattr(window, "_launch_download", launched.append)

    _fill_small_rectangle(window, work_dir)
    window.download_panel.action_buttons[ACTION_START].click()
    _flush(qapp)

    dialog = window._dangerous_dialog
    assert isinstance(dialog, DangerousTaskConfirmDialog)
    dialog.cancel_button().click()
    _flush(qapp)

    assert launched == []
    assert window._pending_plan is None
    assert window._download_worker is None
    # 一个 .mbtiles 都不该出现（``work_dir/regions`` 是区域夹具，不算产物）
    assert [p.name for p in work_dir.glob("*.mbtiles")] == []
    assert "用户放弃了危险任务下载" in window.log_panel.to_text()
    assert window.task_service.list_tasks() == []


def test_a_real_strict_policy_reaches_the_strong_confirmation(
    window: MainWindow, work_dir: Path, qapp, error_dialogs, monkeypatch
) -> None:
    """不 mock 报告：从真估算出发，把阈值收紧到"这次就是危险"，走完整条链路。

    上面几条用例注入的是"指定等级的报告"，它们证明不了"真分析器算出来的等级
    也能触发强确认"。这条从 ``_run_tile_math`` 开始，一路到强确认窗口。
    """
    _fill_small_rectangle(window, work_dir)
    window.download_panel.action_buttons[ACTION_CALCULATE].click()
    _flush(qapp)

    report = window._last_risk
    assert report is not None
    # 阈值卡在"这次刚好是危险"的位置（张数与容量两个维度都要过线）
    window.risk_analyzer = DownloadRiskAnalyzer(
        policy=RiskPolicy(
            large_tiles=1,
            very_large_tiles=2,
            dangerous_tiles=max(3, report.total_tiles),
            large_bytes=1,
            very_large_bytes=2,
            dangerous_bytes=max(3, report.estimated_bytes),
        ),
        strategy=window.strategy,
    )
    launched: list[object] = []
    monkeypatch.setattr(window, "_launch_download", launched.append)

    window.download_panel.action_buttons[ACTION_START].click()
    _flush(qapp)

    assert isinstance(window._dangerous_dialog, DangerousTaskConfirmDialog)
    assert window._dangerous_dialog.report.level is RiskLevel.DANGEROUS
    assert window._dangerous_dialog.report.total_tiles == report.total_tiles
    assert launched == []
