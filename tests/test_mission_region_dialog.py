"""任务区域窗口的"点一下"（Slice 22，Spec §19）。

为什么这个文件必须存在（而不是"验收脚本里有"）：**Qt 槽里抛的异常会被静默
吞掉** —— 界面上表现成"点了没反应"，而 pytest 里 `button.click()` 是同步调用，
异常会冒出来。所以"这个按钮真的能点出结果"这件事只能由"点一下 + 断言结果"的
用例来守，光看源码看不出漏接的槽。

这里所有断言都站在**界面这一侧**读：结果区文本、下拉里有没有这一项、
计划里有没有这一条。不去直接调服务 —— 那测的是服务，不是窗口。

窗口的每个动作都是即时生效的（没有"提交"这一步），因此提示框一律是
**非模态**的，用例可以真的把「是」/「否」点掉（``exec()`` 的框在自动化里
只能靠另一条线程点，那属于"为了显示信息付的额外代价"）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox, QRadioButton

from app.config.settings import (
    DEFAULT_MISSION_BUFFER_METERS,
    UI_TEXT_MISSION_DIALOG_TITLE,
    UI_TEXT_MISSION_EMPTY,
)
from app.mission import (
    CompositeRegionService,
    MissionRegionService,
    MissionRegionStore,
)
from app.mission.models import MISSION_REGION_LEVEL
from app.planning.plan_service import PlanService
from app.planning.plan_store import PlanStore
from app.planning.zoom_strategy import ZoomStrategyService
from app.regions.service import RegionService
from app.tasks.task_service import TaskService
from app.tasks.task_store import TaskStore
from app.ui.mission_region_dialog import MissionRegionDialog

ROUTE = [(120.00, 30.00), (120.05, 30.02), (120.10, 30.01)]


@pytest.fixture
def route_file(work_dir: Path) -> Path:
    """一份真的 GeoJSON 航线文件（窗口是"真读文件"的）。"""
    path = work_dir / "route.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {"name": "西湖巡检"},
                "geometry": {"type": "LineString", "coordinates": [list(p) for p in ROUTE]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def mission_service(work_dir: Path, plans_file: Path, tasks_file: Path):
    return MissionRegionService(
        MissionRegionStore(work_dir / "mission_regions"),
        reference_files=(
            ("下载任务记录", tasks_file),
            ("下载计划", plans_file),
        ),
    )


@pytest.fixture
def plan_service(mission_service, region_resources: Path, plans_file: Path, tasks_file: Path):
    """与主窗口**同一种接法**：计划服务拿到的必须是合并后的区域服务。

    这一条不是"顺手"：任务区不在静态目录里，只给它一个普通的
    ``RegionService``，``add_region("mission-001")`` 会以"区域不在目录里"失败
    —— 而窗口只会说一句"加入下载计划失败"。夹具接错了，用例就会把
    一个真缺陷测成"通过"或者把正确的实现测成"坏的"。
    """
    strategy = ZoomStrategyService()
    service = CompositeRegionService(
        RegionService(strategy=strategy), mission_service
    )
    return PlanService(
        store=PlanStore(plans_file),
        service=service,
        strategy=strategy,
        task_service=TaskService(TaskStore(tasks_file)),
    )


@pytest.fixture
def dialog(qapp, mission_service, plan_service):
    window = MissionRegionDialog(mission_service, plan_service)
    window.show()
    yield window
    window.close()


def _pump(rounds: int = 8) -> None:
    app = QApplication.instance()
    assert app is not None
    for _ in range(rounds):
        app.processEvents()


def _custom_radio(dialog) -> QRadioButton:
    return next(
        radio
        for radio in dialog.findChildren(QRadioButton)
        if radio.text() == "自定义 Zoom"
    )


def _fill(dialog, route_file: Path, *, name: str = "西湖巡检航线", km: float = 3.0) -> None:
    dialog.name_edit().setText(name)
    dialog.route_edit().setText(str(route_file))
    dialog.buffer_spin().setValue(km)


def _answer(dialog, standard_button) -> bool:
    """点掉当前挂着的提示框；没有框就返回 ``False``。"""
    box = dialog.message_box()
    if box is None:
        return False
    box.button(standard_button).click()
    return True


# --------------------------------------------------------------------------- #
# 初始状态
# --------------------------------------------------------------------------- #
def test_the_window_opens_in_recommended_zoom_mode_with_readonly_spins(dialog, qapp) -> None:
    """默认就是推荐模式：两个数字框**只读**，而且里面填的就是推荐区间。

    这条用例抓到过一个真缺陷：窗口打开时两个框是可编辑的、显示的是
    ``QSpinBox`` 的缺省值（1/1），而下一行就写着"推荐 Zoom：Z15~18"。
    成因是"初始状态没套用" —— 切换信号不会为构造时就勾好的那个 radio 发一次。
    """
    assert dialog.windowTitle() == UI_TEXT_MISSION_DIALOG_TITLE
    assert dialog.zoom_mode() == "recommended"
    for spin in dialog.zoom_spins():
        assert spin.isReadOnly() is True
    recommendation = dialog.mission_service.strategy.recommend(MISSION_REGION_LEVEL)
    assert (dialog.zoom_spins()[0].value(), dialog.zoom_spins()[1].value()) == (
        recommendation.min_zoom,
        recommendation.max_zoom,
    )
    assert dialog.buffer_spin().value() == pytest.approx(
        DEFAULT_MISSION_BUFFER_METERS / 1000.0
    )


def test_the_first_region_list_is_empty_and_says_so(dialog) -> None:
    """一个任务区都没有时，下拉里是一条说明而不是空白。"""
    assert dialog.region_combo().count() == 1
    assert dialog.region_combo().currentData() is None
    assert dialog.current_region() is None
    assert dialog.region_combo().itemText(0) == UI_TEXT_MISSION_EMPTY


def test_every_button_says_what_it_does_in_chinese(dialog) -> None:
    """按钮文案全中文、且**都可用**（灰掉不解释只会让用户以为程序坏了）。"""
    buttons = dialog.buttons()
    assert set(buttons) == {"create", "estimate", "add", "delete", "browse"}
    labels = {key: button.text() for key, button in buttons.items()}
    assert labels["create"] == "生成任务区"
    assert labels["estimate"] == "计算"
    assert labels["add"] == "加入下载计划"
    assert all(button.isEnabled() for button in buttons.values())


def test_the_zoom_hint_comes_from_the_policy_table(dialog) -> None:
    """提示行里的区间来自策略表（任务区层一个 Zoom 常量都没有）。"""
    text = dialog.zoom_hint_text()
    assert "15" in text and "18" in text
    recommendation = dialog.mission_service.strategy.recommend(MISSION_REGION_LEVEL)
    assert str(recommendation.min_zoom) in text
    assert str(recommendation.max_zoom) in text


# --------------------------------------------------------------------------- #
# 输入校验：说清缺什么，且什么都不创建
# --------------------------------------------------------------------------- #
def test_creating_without_a_name_explains_and_creates_nothing(dialog, route_file, mission_service) -> None:
    _fill(dialog, route_file, name="")
    dialog.buttons()["create"].click()
    _pump()

    assert mission_service.list_all() == ()
    assert "任务区名称" in dialog.result_lines()[0]
    assert _answer(dialog, QMessageBox.StandardButton.Ok) is True


def test_creating_without_a_route_file_explains_and_creates_nothing(
    dialog, mission_service
) -> None:
    dialog.name_edit().setText("没有航线的任务区")
    dialog.buttons()["create"].click()

    assert mission_service.list_all() == ()
    assert "航线文件" in dialog.result_lines()[0]


def test_a_broken_route_file_reports_the_reason_from_the_loader(
    dialog, work_dir: Path, mission_service
) -> None:
    """坏航线（这里是一份行政区边界）→ 结果区给出**加载器说的那句话**。"""
    bad = work_dir / "polygon.geojson"
    bad.write_text(
        json.dumps(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[120.0, 30.0], [120.1, 30.0], [120.1, 30.1], [120.0, 30.0]]],
                },
            }
        ),
        encoding="utf-8",
    )
    _fill(dialog, bad)
    dialog.buttons()["create"].click()

    assert mission_service.list_all() == ()
    assert "LineString" in dialog.result_lines()[0]


# --------------------------------------------------------------------------- #
# 生成任务区（真读文件 / 真投影 / 真落盘）
# --------------------------------------------------------------------------- #
def test_creating_a_mission_really_writes_it_and_shows_it(
    dialog, route_file, mission_service
) -> None:
    """点「生成任务区」→ 三个文件落盘 + 下拉里出现 + 结果区说清版本与范围。"""
    _fill(dialog, route_file, km=3.0)
    dialog.buttons()["create"].click()

    regions = mission_service.list_all()
    assert len(regions) == 1
    region = regions[0]
    assert region.buffer_meters == pytest.approx(3000.0)
    assert region.waypoint_count == len(ROUTE)
    assert Path(region.geojson_path).is_file()
    assert dialog.region_combo().currentData() == region.id
    assert dialog.current_region().id == region.id

    text = "\n".join(dialog.result_lines())
    assert "已生成" in text
    assert region.geometry_version in text
    assert region.geojson_path in text


def test_the_buffer_is_entered_in_km_and_stored_in_meters(dialog, route_file, mission_service) -> None:
    """界面输入 5 → 盘上是 5000 米（换算只允许在服务层那一个地方做）。"""
    _fill(dialog, route_file, km=5.0)
    dialog.buttons()["create"].click()

    region = mission_service.list_all()[0]
    assert region.buffer_meters == pytest.approx(5000.0)
    assert region.buffer_km == pytest.approx(5.0)


# --------------------------------------------------------------------------- #
# 计算（候选 / 选中 / 过滤 三个数都要在，且与生产同源）
# --------------------------------------------------------------------------- #
def test_the_estimate_button_fills_the_result_area_with_three_numbers(
    dialog, route_file, mission_service
) -> None:
    """Spec §20：候选矩形 / 多边形选中 / 被过滤掉，三者都要显示出来。

    只显示一个"张数"是不够的：用户看不出"走廊到底省掉了多少张"，
    而那正是本 Slice 存在的理由。
    """
    _fill(dialog, route_file, km=3.0)
    dialog.buttons()["create"].click()
    dialog.buttons()["estimate"].click()

    estimate = dialog.last_estimate()
    assert estimate is not None
    text = "\n".join(dialog.result_lines())
    assert "候选瓦片（外包矩形）" in text
    assert "区域内瓦片（多边形）" in text
    assert "过滤掉" in text
    assert "预计容量" in text
    # 三个数字都真的写进去了（不是只有标签）
    assert f"{estimate.estimate.candidate_tiles:,}" in text
    assert f"{estimate.estimate.selected_tiles:,}" in text
    # 逐级明细四行（Z15~18）
    assert text.count("  z") == 4


def test_the_window_reports_exactly_what_the_service_computes(
    dialog, route_file, mission_service
) -> None:
    """窗口里的数字必须与**服务层**算出来的一模一样（没有第二套算法）。"""
    _fill(dialog, route_file, km=3.0)
    dialog.buttons()["create"].click()
    dialog.buttons()["estimate"].click()

    reported = dialog.last_estimate()
    mission_id = dialog.current_region().id
    expected = mission_service.estimate(mission_id)

    assert reported.estimate.selected_tiles == expected.estimate.selected_tiles
    assert reported.estimate.candidate_tiles == expected.estimate.candidate_tiles
    assert reported.estimate.filtered_tiles == expected.estimate.filtered_tiles
    assert reported.used_recommended_zoom is True


def test_a_custom_zoom_never_rewrites_the_user_numbers(
    dialog, route_file
) -> None:
    """自定义模式下用户填 10~13 → 两个数字**原样留着**，提示行里两个区间都写出来。

    注意这里刻意**不**断言 ``warning``：MISSION 策略的 ``warning_max_zoom``
    就等于推荐上界（18，也就是 Tile Math 的上限），因此"超出"这件事在任务区上
    按构造不可能发生 —— 能提示的只有"推荐是哪个区间、你填的是哪个区间"。
    写一条永远不可能为真的断言，等于给后面的人留一个假的坑。
    """
    _fill(dialog, route_file, km=3.0)
    dialog.buttons()["create"].click()

    _custom_radio(dialog).setChecked(True)
    _pump()
    dialog.zoom_spins()[0].setValue(10)
    dialog.zoom_spins()[1].setValue(13)
    dialog.buttons()["estimate"].click()

    assert dialog.zoom_spins()[0].value() == 10
    assert dialog.zoom_spins()[1].value() == 13
    assert dialog.last_estimate().estimated_min_zoom == 10
    assert dialog.last_estimate().estimated_max_zoom == 13
    assert dialog.last_estimate().used_recommended_zoom is False
    hint = dialog.zoom_hint_text()
    assert "Z15~18" in hint  # 推荐区间
    assert "Z10~13" in hint  # 用户填的（原样显示，没有被改）
    for spin in dialog.zoom_spins():
        assert spin.isReadOnly() is False  # 自定义模式下真的能改


def test_calculating_without_a_mission_says_what_to_do_first(dialog) -> None:
    """列表空着就点「计算」→ 说"请先选择或生成一个任务区"，而不是静默什么都不干。"""
    dialog.buttons()["estimate"].click()

    assert dialog.last_estimate() is None
    assert "请先选择或生成一个任务区" in dialog.result_lines()[0]


# --------------------------------------------------------------------------- #
# 加入下载计划
# --------------------------------------------------------------------------- #
def test_adding_to_the_plan_creates_a_mission_entry_with_the_recommended_zoom(
    dialog, route_file, plan_service, mission_service
) -> None:
    """点「加入下载计划」→ 计划里多一条 MISSION 条目，Zoom 是推荐区间。"""
    _fill(dialog, route_file, km=3.0)
    dialog.buttons()["create"].click()
    mission_id = dialog.current_region().id

    dialog.buttons()["add"].click()

    item = plan_service.plan.item(mission_id)
    assert item is not None
    assert item.region_level is MISSION_REGION_LEVEL
    recommendation = mission_service.strategy.recommend(MISSION_REGION_LEVEL)
    assert (item.min_zoom, item.max_zoom) == (
        recommendation.min_zoom,
        recommendation.max_zoom,
    )
    assert "已加入下载计划" in dialog.plan_hint_text()


def test_adding_the_same_mission_twice_updates_instead_of_duplicating(
    dialog, route_file, plan_service
) -> None:
    """同一个任务区再加一次是「更新」，不是多一行。"""
    _fill(dialog, route_file, km=3.0)
    dialog.buttons()["create"].click()
    mission_id = dialog.current_region().id

    dialog.buttons()["add"].click()
    dialog.buttons()["add"].click()

    assert plan_service.plan.count == 1
    assert plan_service.plan.item(mission_id) is not None
    assert "已更新下载计划" in dialog.plan_hint_text()


def test_adding_without_a_mission_says_what_to_do_first(dialog, plan_service) -> None:
    dialog.buttons()["add"].click()

    assert plan_service.plan.count == 0
    assert "请先选择或生成一个任务区" in dialog.result_lines()[0]


# --------------------------------------------------------------------------- #
# 删除（先确认 / 被引用时直接拒绝）
# --------------------------------------------------------------------------- #
def test_deleting_asks_first_and_no_keeps_the_mission(
    dialog, route_file, mission_service
) -> None:
    """点「删除任务区」→ 先问一句；答「否」→ 一个文件都不许少。"""
    _fill(dialog, route_file, km=3.0)
    dialog.buttons()["create"].click()
    mission_id = dialog.current_region().id

    dialog.buttons()["delete"].click()
    box = dialog.message_box()
    assert box is not None and "删除" in box.windowTitle()
    box.button(QMessageBox.StandardButton.No).click()

    assert mission_service.exists(mission_id) is True


def test_deleting_with_yes_removes_it_from_disk_and_from_the_list(
    dialog, route_file, mission_service
) -> None:
    _fill(dialog, route_file, km=3.0)
    dialog.buttons()["create"].click()
    mission_id = dialog.current_region().id

    dialog.buttons()["delete"].click()
    _answer(dialog, QMessageBox.StandardButton.Yes)

    assert mission_service.exists(mission_id) is False
    assert dialog.region_combo().currentData() is None
    assert "已删除任务区" in dialog.result_lines()[0]


def test_a_referenced_mission_is_refused_before_any_confirmation(
    dialog, route_file, plan_service, mission_service
) -> None:
    """被计划引用 → **不弹确认框**，直接说清是谁在引用（Spec §22）。

    让用户先点一次「是」再被拒绝，是把"删不掉"这条信息藏在一层点击之后 ——
    而那个「是」的含义是"我确定要删"，程序却做不到。
    """
    _fill(dialog, route_file, km=3.0)
    dialog.buttons()["create"].click()
    mission_id = dialog.current_region().id
    dialog.buttons()["add"].click()
    assert plan_service.plan.item(mission_id) is not None

    dialog.buttons()["delete"].click()

    box = dialog.message_box()
    assert box is not None
    assert "正在被下载计划引用" in box.text()
    # 是"说明"而不是"确认"：只有 Ok，没有 Yes/No
    assert box.button(QMessageBox.StandardButton.Yes) is None
    assert mission_service.exists(mission_id) is True


def test_deleting_without_a_mission_says_what_to_do_first(dialog) -> None:
    dialog.buttons()["delete"].click()

    assert "请先选择要删除的任务区" in dialog.result_lines()[0]


# --------------------------------------------------------------------------- #
# 刷新与选择
# --------------------------------------------------------------------------- #
def test_selecting_a_region_resets_the_previous_estimate(dialog, route_file) -> None:
    """换一个任务区看 → 上一次算的数字必须作废（否则界面显示的是别人的数）。"""
    _fill(dialog, route_file, km=3.0)
    dialog.buttons()["create"].click()
    dialog.buttons()["estimate"].click()
    assert dialog.last_estimate() is not None
    first = dialog.current_region().id

    _fill(dialog, route_file, name="第二条航线", km=1.0)
    dialog.buttons()["create"].click()
    second = dialog.current_region().id
    assert second != first

    assert dialog.select_region(first) is True
    assert dialog.last_estimate() is None


def test_the_list_survives_reopening_the_window(qapp, mission_service, plan_service, route_file) -> None:
    """关掉再打开 → 任务区还在（它落盘了，不是窗口里的内存状态）。"""
    first = MissionRegionDialog(mission_service, plan_service)
    first.show()
    _fill(first, route_file, km=3.0)
    first.buttons()["create"].click()
    mission_id = first.current_region().id
    first.close()

    second = MissionRegionDialog(mission_service, plan_service)
    second.show()
    assert second.select_region(mission_id) is True
    assert second.current_region().id == mission_id
    second.close()
