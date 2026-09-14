"""GUI 冒烟测试。

在 offscreen 平台下装配主窗口，覆盖 Slice 0 的验收点：
区域齐全、默认值正确、占位按钮点击不崩、启动日志可写入。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.geo import GeoBounds, estimate_size_bytes, estimate_tile_count, format_size
from app.main import STARTUP_MESSAGES, _report_startup
from app.models import MapLayerType
from app.ui.device_panel import DevicePanel
from app.ui.download_panel import (
    ACTION_CALCULATE,
    ACTION_PAUSE,
    ACTION_RESUME,
    ACTION_START,
    DownloadPanel,
)
from app.ui.log_panel import LogPanel
from app.ui.main_window import MainWindow


@pytest.fixture()
def window(qapp) -> MainWindow:
    """每个用例一个全新的主窗口。"""
    win = MainWindow()
    yield win
    win.close()
    win.deleteLater()


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #
def test_main_window_basics(window: MainWindow) -> None:
    assert window.windowTitle() == "天地图离线地图工具"
    assert window.width() == settings.DEFAULT_WINDOW_WIDTH
    assert window.height() == settings.DEFAULT_WINDOW_HEIGHT


def test_all_sections_present(window: MainWindow) -> None:
    assert isinstance(window.download_panel, DownloadPanel)
    assert isinstance(window.device_panel, DevicePanel)
    assert isinstance(window.log_panel, LogPanel)
    assert window.status_group.title() == "下载状态"

    # 五个区域都能从窗口树上找到
    assert window.findChild(DownloadPanel) is window.download_panel
    assert window.findChild(DevicePanel) is window.device_panel
    assert window.findChild(LogPanel) is window.log_panel


def test_status_group_shows_defaults(window: MainWindow) -> None:
    values = window._status_values
    assert set(values) == {
        "task_status",
        "current_zoom",
        "tile_total",
        "tile_done",
        "tile_failed",
        "speed",
    }
    assert values["task_status"].text() == "空闲"
    assert values["tile_total"].text() == "0"
    assert values["tile_done"].text() == "0"
    assert values["tile_failed"].text() == "0"
    assert window.progress_bar.value() == 0


def test_device_panel_empty_state(window: MainWindow) -> None:
    lst = window.device_panel.device_list
    assert lst.count() == 1
    assert lst.item(0).text() == settings.UI_TEXT_DEVICE_EMPTY


# --------------------------------------------------------------------------- #
# DownloadPanel 表单
# --------------------------------------------------------------------------- #
def test_get_form_data_defaults(window: MainWindow) -> None:
    data = window.download_panel.get_form_data()
    assert set(data) == {
        "token",
        "map_type",
        "with_labels",
        "west",
        "south",
        "east",
        "north",
        "min_zoom",
        "max_zoom",
        "output_dir",
    }
    assert data["token"] == ""
    assert data["map_type"] is MapLayerType.VECTOR
    assert data["with_labels"] is True
    assert data["min_zoom"] == settings.DEFAULT_MIN_ZOOM
    assert data["max_zoom"] == settings.DEFAULT_MAX_ZOOM
    assert isinstance(data["output_dir"], Path)
    assert data["west"] < data["east"]
    assert data["south"] < data["north"]


def test_form_data_reflects_edits(window: MainWindow) -> None:
    panel = window.download_panel
    panel._token_edit.setText("  my-token  ")
    panel._map_type_combo.setCurrentIndex(1)
    panel._labels_check.setChecked(False)
    panel._min_zoom_spin.setValue(10)
    panel._max_zoom_spin.setValue(12)

    data = panel.get_form_data()
    assert data["token"] == "my-token"  # 首尾空白被裁掉
    assert data["map_type"] is MapLayerType.IMAGE
    assert data["with_labels"] is False
    assert data["min_zoom"] == 10
    assert data["max_zoom"] == 12


# --------------------------------------------------------------------------- #
# 占位按钮
# --------------------------------------------------------------------------- #
def test_action_buttons_exist_and_enabled(window: MainWindow) -> None:
    buttons = window.download_panel.action_buttons
    assert list(buttons) == [ACTION_CALCULATE, ACTION_START, ACTION_PAUSE, ACTION_RESUME]
    assert [b.text() for b in buttons.values()] == ["计算", "开始下载", "暂停", "继续"]
    assert all(b.isEnabled() for b in buttons.values())


def test_clicking_action_buttons_does_not_crash(window: MainWindow) -> None:
    for action, button in window.download_panel.action_buttons.items():
        button.click()
        assert f"触发动作：{action}" in window.log_panel.to_text()


def test_calculate_logs_form_snapshot(window: MainWindow) -> None:
    window.download_panel.action_buttons[ACTION_CALCULATE].click()
    text = window.log_panel.to_text()
    assert "当前配置：" in text
    assert "token=未填写" in text


def test_device_buttons_do_not_crash(window: MainWindow) -> None:
    refresh_button, import_button = window.device_panel.buttons
    refresh_button.click()
    import_button.click()
    text = window.log_panel.to_text()
    assert "Slice 4" in text


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def test_log_panel_append_and_clear(window: MainWindow) -> None:
    panel = window.log_panel
    panel.append_log("Application started")
    panel.append_log("boom", "ERROR")

    text = panel.to_text()
    assert "Application started" in text
    assert "[ERROR] boom" in text

    panel.clear_log()
    assert panel.to_text() == ""


def test_startup_messages_are_reported(window: MainWindow) -> None:
    assert STARTUP_MESSAGES == ("Application started", "Slice 0 project initialized")
    _report_startup(window)
    text = window.log_panel.to_text()
    for message in STARTUP_MESSAGES:
        assert message in text


# --------------------------------------------------------------------------- #
# "计算" 按钮接入 Tile Math（Slice 1）
# --------------------------------------------------------------------------- #
def test_calculate_button_renders_estimate(window: MainWindow) -> None:
    window.download_panel.action_buttons[ACTION_CALCULATE].click()

    text = window.download_panel.result_label.text()
    assert "预计瓦片：" in text
    assert "预计容量：" in text
    assert "计算失败" not in text


def test_calculate_result_matches_geo_module(window: MainWindow) -> None:
    """UI 展示的数字必须来自 app.geo，而不是在界面层重算。"""
    panel = window.download_panel
    panel._min_zoom_spin.setValue(8)
    panel._max_zoom_spin.setValue(17)
    window.download_panel.action_buttons[ACTION_CALCULATE].click()

    data = panel.get_form_data()
    bounds = GeoBounds(data["west"], data["south"], data["east"], data["north"])
    expected_count = estimate_tile_count(bounds, 8, 17)
    expected_size = format_size(estimate_size_bytes(expected_count))

    assert panel.result_label.text() == (
        f"预计瓦片：{expected_count:,}\n预计容量：{expected_size}"
    )
    assert window._status_values["tile_total"].text() == f"{expected_count:,}"
    assert "瓦片计算完成" in window.log_panel.to_text()


def test_calculate_logs_per_zoom_breakdown(window: MainWindow) -> None:
    panel = window.download_panel
    panel._min_zoom_spin.setValue(9)
    panel._max_zoom_spin.setValue(11)
    panel.action_buttons[ACTION_CALCULATE].click()

    text = window.log_panel.to_text()
    assert "各级瓦片数：" in text
    for zoom in (9, 10, 11):
        assert f"z{zoom}=" in text


def test_calculate_without_running_the_ui_does_not_crash(window: MainWindow, monkeypatch) -> None:
    """默认值非法时不弹模态框、不崩溃，错误信息落到结果区与日志。"""
    monkeypatch.setattr(window, "_show_error_dialog", lambda *a, **k: None)
    panel = window.download_panel
    # 把东边界挪到西边界左侧，构造非法范围
    panel._east_spin.setValue(100.0)

    panel.action_buttons[ACTION_CALCULATE].click()

    assert "计算失败" in panel.result_label.text()
    assert "west 必须小于 east" in panel.result_label.text()
    assert "瓦片计算失败" in window.log_panel.to_text()


def test_calculate_invalid_zoom_range_reports_error(window: MainWindow, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window, "_show_error_dialog", lambda title, message: calls.append((title, message))
    )

    panel = window.download_panel
    panel._min_zoom_spin.setValue(17)
    panel._max_zoom_spin.setValue(8)
    panel.action_buttons[ACTION_CALCULATE].click()

    assert "min_zoom 不能大于 max_zoom" in panel.result_label.text()
    assert calls and calls[0][0] == "瓦片计算失败"


def test_calculate_success_does_not_open_error_dialog(window: MainWindow, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window, "_show_error_dialog", lambda title, message: calls.append((title, message))
    )

    window.download_panel.action_buttons[ACTION_CALCULATE].click()

    assert calls == []
    assert "预计瓦片：" in window.download_panel.result_label.text()


def test_slice0_panels_survive_calculate(window: MainWindow) -> None:
    """Slice 1 不能破坏 Slice 0 的其他区域。"""
    window.download_panel.action_buttons[ACTION_CALCULATE].click()

    assert window.status_group.title() == "下载状态"
    assert window.device_panel.device_list.count() == 1
    assert window.device_panel.device_list.item(0).text() == settings.UI_TEXT_DEVICE_EMPTY
    refresh_button, _ = window.device_panel.buttons
    refresh_button.click()  # Slice 0 的占位动作仍然可用
    assert "Slice 4" in window.log_panel.to_text()
