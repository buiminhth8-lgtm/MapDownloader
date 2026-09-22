"""下载计划与主窗口的接线（Slice 20，Spec §10 / §11 / §14 / §18）。

这一层测的是**两个入口真的通到业务层**：

- 面板上的「加入下载计划」→ 当前选中的区域进计划（含 Zoom 模式的取舍）；
- 面板上的「查看下载计划」→ 计划页打开，页里点「生成任务」→ 任务列表多出记录。

以及两条"接线时最容易搞错"的约定：

- ``plans.json`` 落在**任务文件旁边**（否则测试会顺手写进用户主目录）；
- 没有计划文件时**照常启动**（Spec §18：旧版本升级上来就是这个状态）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from app.planning.region_level import RegionLevel
from app.regions.service import RegionService
from app.tasks.task_service import TaskService, TaskStore
from app.ui.download_panel import (
    ACTION_MISSION_OPEN,
    ACTION_PLAN_ADD,
    ACTION_PLAN_OPEN,
)
from app.ui.main_window import MainWindow
from app.ui.region_selection import ZoomMode

_PLAN_FILE = "plans.json"


@pytest.fixture
def task_service(tasks_file: Path) -> TaskService:
    return TaskService(TaskStore(tasks_file))


@pytest.fixture
def window(qapp, task_service: TaskService, preferences, region_resources: Path) -> MainWindow:
    """主窗口 + 合成区域目录 + 临时任务文件（于是计划文件也在临时目录里）。"""
    win = MainWindow(task_service=task_service, preferences=preferences)
    yield win
    win._exit_confirmed = True
    page = win._plan_dialog
    if page is not None:
        page.close()
    win.close()
    qapp.processEvents()


def _flush(qapp, rounds: int = 4) -> None:
    for _ in range(rounds):
        qapp.processEvents()


def _add_current_region(window: MainWindow, region_id: str, qapp) -> None:
    """选中一块地，然后点面板上的「加入下载计划」。"""
    assert window.download_panel.set_region_id(region_id) is True
    _flush(qapp)
    window.download_panel.plan_buttons()[ACTION_PLAN_ADD].click()
    _flush(qapp)


# --------------------------------------------------------------------------- #
# 入口（Spec §10）
# --------------------------------------------------------------------------- #
def test_the_panel_exposes_both_plan_entries(window: MainWindow) -> None:
    """计划两个入口（Slice 20）+ 任务区域入口（Slice 22）。

    断言"恰好这三个 id"是有意的：它同时守住"没有多出别的按钮"。
    加第四个入口时要连这条一起改 —— 那一刻正是让人**再确认一遍**
    "这些入口都还通着吗"的时刻。
    """
    buttons = window.download_panel.plan_buttons()

    assert set(buttons) == {ACTION_PLAN_ADD, ACTION_PLAN_OPEN, ACTION_MISSION_OPEN}
    assert buttons[ACTION_PLAN_ADD].text() == "加入下载计划"
    assert buttons[ACTION_PLAN_OPEN].text() == "查看下载计划"
    assert buttons[ACTION_MISSION_OPEN].text() == "任务区域…"
    assert all(button.isEnabled() for button in buttons.values())


def test_summary_starts_empty_and_is_shown_on_the_panel(window: MainWindow) -> None:
    """启动时没有计划文件也要正常显示（Spec §18）。"""
    assert window.plan_service.plan.is_empty is True
    assert window.download_panel.plan_summary_label().text() == "下载计划：暂无区域"


def test_add_button_puts_the_selected_region_into_the_plan(
    window: MainWindow, qapp
) -> None:
    _add_current_region(window, "northland", qapp)

    plan = window.plan_service.plan
    assert plan.item("northland") is not None
    assert plan.item("northland").region_name == "示例北省"
    assert "1 个区域" in window.download_panel.plan_summary_label().text()
    assert "加入下载计划" in window.log_panel.to_text()


def test_recommended_mode_takes_the_zoom_from_the_strategy(
    window: MainWindow, region_resources: Path, qapp
) -> None:
    """推荐模式下计划里的 Zoom 必须**等于策略层算出来的值**（不是碰巧相同的常量）。"""
    window.download_panel.set_zoom_mode(ZoomMode.RECOMMENDED)
    _add_current_region(window, "northland", qapp)

    item = window.plan_service.plan.item("northland")
    expected = RegionService().strategy.recommend(RegionLevel.PROVINCE)
    assert (item.min_zoom, item.max_zoom) == (expected.min_zoom, expected.max_zoom)


def test_custom_mode_carries_the_users_numbers_verbatim(
    window: MainWindow, qapp
) -> None:
    """自定义模式：用户填什么就存什么（Spec §6，只警告不改值）。"""
    window.download_panel.set_zoom_range(9, 15)
    assert window.download_panel.zoom_mode is ZoomMode.CUSTOM

    _add_current_region(window, "northland", qapp)

    item = window.plan_service.plan.item("northland")
    assert (item.min_zoom, item.max_zoom) == (9, 15)


def test_adding_the_same_region_twice_updates_instead_of_duplicating(
    window: MainWindow, qapp
) -> None:
    """Spec §5：再加一次是"更新"，不是多一行。"""
    _add_current_region(window, "northland", qapp)
    window.download_panel.set_zoom_range(10, 15)
    _add_current_region(window, "northland", qapp)

    plan = window.plan_service.plan
    assert plan.count == 1
    assert (plan.item("northland").min_zoom, plan.item("northland").max_zoom) == (10, 15)
    assert "更新下载计划" in window.log_panel.to_text()


def test_plan_settings_follow_the_panel(window: MainWindow, work_dir: Path, qapp) -> None:
    """名字 / 输出目录 / 注记都要进计划（Spec §14 / §16）。"""
    window.download_panel.set_output_dir(work_dir / "out")
    window.download_panel.set_map_name("华东离线包")
    window.download_panel.set_with_labels(True)

    _add_current_region(window, "demoland", qapp)

    plan = window.plan_service.plan
    assert plan.name == "华东离线包"
    assert plan.output_dir == str(work_dir / "out")
    assert plan.with_labels is True
    assert plan.map_layer  # 图层跟着面板走（矢量 / 影像的底图）


def test_three_levels_produce_three_items(window: MainWindow, qapp) -> None:
    """Spec §4：国家 + 省 + 市 可以一起攒在同一份计划里。"""
    for region_id in ("demoland", "northland", "rivertown"):
        _add_current_region(window, region_id, qapp)

    plan = window.plan_service.plan
    assert plan.count == 3
    assert plan.coverage_levels() == (
        RegionLevel.COUNTRY,
        RegionLevel.PROVINCE,
        RegionLevel.CITY,
    )


# --------------------------------------------------------------------------- #
# 计划页（Spec §11 / §14）
# --------------------------------------------------------------------------- #
def test_open_button_opens_the_page_once_and_reuses_it(
    window: MainWindow, qapp
) -> None:
    window.download_panel.plan_buttons()[ACTION_PLAN_OPEN].click()
    _flush(qapp)

    page = window._plan_dialog
    assert page is not None, "点「查看下载计划」应当打开计划页"
    assert page.isVisible() is True

    window.download_panel.plan_buttons()[ACTION_PLAN_OPEN].click()
    _flush(qapp)

    assert window._plan_dialog is page, "重复打开应当复用同一个实例"


def test_the_page_shows_what_the_panel_added(window: MainWindow, qapp) -> None:
    _add_current_region(window, "northland", qapp)

    window.download_panel.plan_buttons()[ACTION_PLAN_OPEN].click()
    _flush(qapp)
    page = window._plan_dialog
    assert page is not None
    page.refresh()

    assert page.row_region_ids() == ["northland"]
    assert page.table().item(0, 0).text() == "示例北省"


def test_generating_from_the_page_fills_the_task_list(
    window: MainWindow, qapp
) -> None:
    """页里点「生成任务」→ 下载任务列表里真的多出记录，且**都没开始下载**。"""
    for region_id in ("demoland", "northland"):
        _add_current_region(window, region_id, qapp)
    window.download_panel.plan_buttons()[ACTION_PLAN_OPEN].click()
    _flush(qapp)
    page = window._plan_dialog
    assert page is not None

    page.estimate()
    assert page.wait_for_estimate() is True
    _flush(qapp)
    page.generate_tasks()
    _flush(qapp)

    tasks = window.task_service.list_tasks()
    assert len(tasks) == 2
    assert window.task_panel.table.rowCount() == 2
    assert all(task.status.value == "created" for task in tasks)
    assert "已生成 2 条下载任务" in window.log_panel.to_text()


def test_disabling_a_row_on_the_page_keeps_it_out_of_the_tasks(
    window: MainWindow, qapp
) -> None:
    """Spec §12：停用一项 → 不生成任务，但它还留在计划里。"""
    for region_id in ("demoland", "northland"):
        _add_current_region(window, region_id, qapp)
    window.download_panel.plan_buttons()[ACTION_PLAN_OPEN].click()
    _flush(qapp)
    page = window._plan_dialog
    assert page is not None

    page.table().item(1, 0).setCheckState(Qt.CheckState.Unchecked)
    _flush(qapp)
    page.estimate()
    assert page.wait_for_estimate() is True
    _flush(qapp)
    page.generate_tasks()
    _flush(qapp)

    assert [task.region_id for task in window.task_service.list_tasks()] == ["demoland"]
    assert page.row_region_ids() == ["demoland", "northland"]


# --------------------------------------------------------------------------- #
# 持久化与启动（Spec §17 / §18）
# --------------------------------------------------------------------------- #
def test_the_plans_file_lives_next_to_the_tasks_file(
    window: MainWindow, tasks_file: Path
) -> None:
    """一个来源决定数据放哪：测试注入的临时 ``tasks.json`` 旁边就是计划文件。

    这条断言是有代价的教训：两处各写各的"默认路径"时，生产里恰好是同一个
    ``data/`` 目录，而用例注入临时 tasks 文件之后，计划仍会写进**用户主目录**。
    """
    assert window.plan_service.store.path.name == _PLAN_FILE
    assert window.plan_service.store.path.parent == tasks_file.parent


def test_the_plan_survives_a_restart(
    window: MainWindow, task_service: TaskService, preferences, region_resources: Path, qapp
) -> None:
    """Spec §17 / §18：重启后计划还在（区域 / Zoom 都在）。"""
    window.download_panel.set_zoom_range(10, 13)
    _add_current_region(window, "northland", qapp)
    window.close()

    fresh = MainWindow(task_service=task_service, preferences=preferences)
    try:
        plan = fresh.plan_service.plan
        assert plan.item("northland") is not None
        assert (plan.item("northland").min_zoom, plan.item("northland").max_zoom) == (
            10,
            13,
        )
        assert "1 个区域" in fresh.download_panel.plan_summary_label().text()
    finally:
        fresh._exit_confirmed = True
        fresh.close()
        qapp.processEvents()


def test_a_deleted_region_does_not_block_startup(
    window: MainWindow,
    task_service: TaskService,
    preferences,
    region_resources: Path,
    qapp,
) -> None:
    """Spec §18：区域被删 → 那条标成不可用，**整份计划照常打开**。"""
    _add_current_region(window, "demoland", qapp)
    _add_current_region(window, "northland", qapp)
    window.close()

    (region_resources / "province" / "northland.geojson").unlink()

    fresh = MainWindow(task_service=task_service, preferences=preferences)
    try:
        plan = fresh.plan_service.plan
        assert plan.count == 2, "计划打不开 = 用户连把坏的那条删掉都做不了"
        result = fresh.plan_service.validate()
        assert result.ok is False
        assert plan.item("northland").is_valid is False
        assert fresh.download_panel.plan_summary_label().text() != ""
    finally:
        fresh._exit_confirmed = True
        fresh.close()
        qapp.processEvents()


def test_a_corrupt_plans_file_does_not_block_startup(
    window: MainWindow,
    task_service: TaskService,
    preferences,
    region_resources: Path,
    qapp,
) -> None:
    """计划文件被写坏（断电 / 手工编辑）→ 隔离它并**正常启动**。"""
    path = window.plan_service.store.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    window.close()

    fresh = MainWindow(task_service=task_service, preferences=preferences)
    try:
        assert fresh.plan_service.plan.is_empty is True
        assert path.exists() is False, "坏文件应当被隔离（挪走）而不是留在原地"
    finally:
        fresh._exit_confirmed = True
        fresh.close()
        qapp.processEvents()


def test_no_token_is_written_into_the_plan_file(window: MainWindow, qapp) -> None:
    """Spec §17：计划文件里**没有 Token**（与 tasks.json 同一条红线）。"""
    window.download_panel._token_edit.setText("secret-token-value")
    _add_current_region(window, "demoland", qapp)

    text = window.plan_service.store.path.read_text(encoding="utf-8")

    assert "secret-token-value" not in text
    assert "token" not in text.lower()


def test_the_main_window_is_alive_after_the_whole_flow(window: MainWindow, qapp) -> None:
    """一条龙冒烟：加两块地 → 开页 → 估算 → 生成 → 关页 → 窗口仍可用。

    这个顺序里每一步都在真实控件上走一遍，专门用来抓"点了没反应"这一类缺陷
    （Qt 槽里抛的异常会被静默吞掉，数据层用例永远看不见）。
    """
    for region_id in ("demoland", "rivertown"):
        _add_current_region(window, region_id, qapp)
    window.download_panel.plan_buttons()[ACTION_PLAN_OPEN].click()
    _flush(qapp)
    page = window._plan_dialog
    assert page is not None
    page.estimate()
    assert page.wait_for_estimate() is True
    _flush(qapp)
    page.generate_tasks()
    _flush(qapp)
    page.close()
    _flush(qapp)

    # 走完这一串之后：结论都在，窗口没有被槽里抛出的异常搞坏
    assert window.plan_service.plan.count == 2
    assert window.plan_service.plan.needs_estimate() is False
    assert window.download_panel.plan_summary_label().text().startswith(
        "下载计划：2 个区域"
    )
    assert len(window.task_service.list_tasks()) == 2
