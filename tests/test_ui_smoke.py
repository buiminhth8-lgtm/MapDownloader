"""GUI 冒烟测试。

在 offscreen 平台下装配主窗口，覆盖：

- Slice 0：区域齐全、默认值正确、占位按钮点击不崩、启动日志可写入
- Slice 1："计算"按钮接入 Tile Math
- Slice 2："测试天地图连接"按钮接入 Provider（网络用 MockTransport 打桩）
- Slice 3：[开始下载]/[暂停]/[继续] 接入异步下载引擎
  （网络同样用 MockTransport 打桩，测试不触网）
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from PySide6.QtCore import QThread

import app.ui.download_worker as download_worker_module
import app.ui.main_window as main_window_module
from app.config import settings
from app.downloader import RateLimiter, RetryPolicy
from app.geo import (
    GeoBounds,
    estimate_size_bytes,
    estimate_tile_count,
    format_size,
    lat_to_tile_y,
    lon_to_tile_x,
)
from app.main import STARTUP_MESSAGES, _report_startup
from app.models import MapLayerType
from app.providers import PNG_SIGNATURE, TiandituProvider
from app.ui.connection_check import ConnectionTestWorker
from app.ui.device_panel import DevicePanel
from app.ui.download_panel import (
    ACTION_CALCULATE,
    ACTION_PAUSE,
    ACTION_RESUME,
    ACTION_START,
    ACTION_TEST_CONNECTION,
    DownloadPanel,
)
from app.ui.download_worker import DownloadWorker
from app.ui.log_panel import LogPanel
from app.ui.main_window import MainWindow

#: 假 Token（禁止替换为真实密钥）
FAKE_TOKEN = "abcdefghijklmn"

#: 只需文件头正确，校验逻辑按 magic bytes 判定
FAKE_PNG = PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + b"tile-payload" * 32

#: 退避为 0 的重试策略：行为与生产一致，但测试不用真的等
FAST_POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=0.0, max_delay_seconds=0.0)


@pytest.fixture()
def window(qapp) -> MainWindow:
    """每个用例一个全新的主窗口。"""
    win = MainWindow()
    yield win
    win.close()
    win.deleteLater()


@pytest.fixture()
def error_dialogs(monkeypatch, window: MainWindow) -> list[tuple[str, str]]:
    """把非模态错误弹窗替换成记录器，便于断言（同时避免用例弹窗）。"""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window, "_show_error_dialog", lambda title, message: calls.append((title, message))
    )
    return calls


def _png_handler(
    status_code: int = 200,
    content: bytes = FAKE_PNG,
    content_type: str = "image/png",
    delay: float = 0.0,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if delay:
            time.sleep(delay)
        return httpx.Response(
            status_code, content=content, headers={"content-type": content_type}
        )

    return handler


def _stub_transport(monkeypatch, handler) -> None:
    """让 MainWindow 内部创建的 Provider 走 MockTransport（不触网）。"""

    def factory(token: str, *args: object, **kwargs: object) -> TiandituProvider:
        return TiandituProvider(token, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(main_window_module, "TiandituProvider", factory)


def _fill_token(window: MainWindow, token: str = FAKE_TOKEN) -> None:
    window.download_panel._token_edit.setText(token)


def _wait_for_worker(window: MainWindow, qapp) -> None:
    """等待后台测试线程结束并把结果送回主线程。"""
    worker = window._connection_worker
    assert worker is not None
    assert worker.wait(10_000) is True
    for _ in range(5):
        qapp.processEvents()


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
        "tile_succeeded",
        "tile_failed",
        "tile_skipped",
        "speed",
    }
    assert values["task_status"].text() == "空闲"
    assert values["tile_total"].text() == "0"
    assert values["tile_done"].text() == "0"
    assert values["tile_succeeded"].text() == "0"
    assert values["tile_failed"].text() == "0"
    assert values["tile_skipped"].text() == "0"
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
def test_action_buttons_exist(window: MainWindow) -> None:
    buttons = window.download_panel.action_buttons
    assert list(buttons) == [
        ACTION_CALCULATE,
        ACTION_START,
        ACTION_PAUSE,
        ACTION_RESUME,
        ACTION_TEST_CONNECTION,
    ]
    assert [b.text() for b in buttons.values()] == [
        "计算",
        "开始下载",
        "暂停",
        "继续",
        "测试天地图连接",
    ]
    # 空闲态：暂停 / 继续没有意义（Slice 3 起按下载状态启用）
    assert buttons[ACTION_CALCULATE].isEnabled() is True
    assert buttons[ACTION_START].isEnabled() is True
    assert buttons[ACTION_TEST_CONNECTION].isEnabled() is True
    assert buttons[ACTION_PAUSE].isEnabled() is False
    assert buttons[ACTION_RESUME].isEnabled() is False


def test_clicking_action_buttons_does_not_crash(
    window: MainWindow, error_dialogs: list[tuple[str, str]]
) -> None:
    """所有**可点击**的按钮都不能把 UI 点崩。

    暂停 / 继续在空闲态是禁用的（点了也不会发出信号），因此它们不参与这一轮；
    它们各自的守卫分支由 ``test_pause_without_running_download_is_reported``
    与 ``test_resume_without_running_download_is_reported`` 直接调用槽来覆盖。
    """
    for action, button in window.download_panel.action_buttons.items():
        if not button.isEnabled():
            continue
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


# --------------------------------------------------------------------------- #
# "测试天地图连接" 接入 TiandituProvider（Slice 2）
# --------------------------------------------------------------------------- #
def test_connection_area_starts_empty(window: MainWindow) -> None:
    assert window.download_panel.connection_label.text() == "尚未测试"
    assert window.download_panel.test_connection_button.isEnabled()
    assert window._connection_worker is None


def test_connection_test_success_updates_ui(
    window: MainWindow, monkeypatch, qapp, error_dialogs
) -> None:
    _stub_transport(monkeypatch, _png_handler())
    _fill_token(window)

    window.download_panel.action_buttons[ACTION_TEST_CONNECTION].click()
    _wait_for_worker(window, qapp)

    text = window.download_panel.connection_label.text()
    assert text.startswith("连接成功")
    assert "图层：vec_w" in text
    assert f"Zoom：{settings.DEFAULT_MIN_ZOOM}" in text
    assert "HTTP：200" in text
    assert f"Size：{len(FAKE_PNG)} bytes" in text
    assert "耗时：" in text

    log = window.log_panel.to_text()
    assert "天地图连接测试成功" in log
    assert "请求 URL：" in log

    assert error_dialogs == []
    assert window.download_panel.test_connection_button.isEnabled()


def test_connection_test_uses_center_tile_of_current_bounds(
    window: MainWindow, monkeypatch, qapp, error_dialogs
) -> None:
    """请求的瓦片必须等于 Slice 1 算出的中心点瓦片（UI 不自己算坐标）。"""
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, content=FAKE_PNG, headers={"content-type": "image/png"})

    _stub_transport(monkeypatch, handler)
    panel = window.download_panel
    _fill_token(window)
    panel._west_spin.setValue(116.0)
    panel._south_spin.setValue(39.0)
    panel._east_spin.setValue(117.0)
    panel._north_spin.setValue(40.0)
    panel._min_zoom_spin.setValue(10)

    panel.action_buttons[ACTION_TEST_CONNECTION].click()
    _wait_for_worker(window, qapp)

    data = panel.get_form_data()
    bounds = GeoBounds(data["west"], data["south"], data["east"], data["north"])
    center_lat, center_lon = bounds.center
    expected = TiandituProvider(FAKE_TOKEN).build_tile_url(
        MapLayerType.VECTOR,
        10,
        lon_to_tile_x(center_lon, 10),
        lat_to_tile_y(center_lat, 10),
    )
    assert len(seen) == 1
    assert seen[0].query.decode() == httpx.URL(expected).query.decode()


def test_connection_test_does_not_block_ui_thread(
    window: MainWindow, monkeypatch, qapp, error_dialogs
) -> None:
    """网络请求必须发生在后台线程：点击后立即返回，界面不冻结。"""
    _stub_transport(monkeypatch, _png_handler(delay=0.4))
    _fill_token(window)

    window.download_panel.action_buttons[ACTION_TEST_CONNECTION].click()

    # 点击已返回但请求仍在进行中 —— 说明没有阻塞 UI 线程
    worker = window._connection_worker
    assert isinstance(worker, QThread)
    assert worker.isRunning() is True
    assert worker.isCurrentThread() is False
    assert window.download_panel.connection_label.text() == "正在测试天地图连接…"
    assert window.download_panel.test_connection_button.isEnabled() is False

    _wait_for_worker(window, qapp)
    assert window.download_panel.connection_label.text().startswith("连接成功")


def test_connection_test_reports_http_error(
    window: MainWindow, monkeypatch, qapp, error_dialogs
) -> None:
    _stub_transport(
        monkeypatch,
        _png_handler(status_code=403, content=b"<html>denied</html>", content_type="text/html"),
    )
    _fill_token(window)

    window.download_panel.action_buttons[ACTION_TEST_CONNECTION].click()
    _wait_for_worker(window, qapp)

    text = window.download_panel.connection_label.text()
    assert text.startswith("连接失败：HTTP 403")
    assert "Token" in text
    assert "HTTP：403" in text
    assert "天地图连接测试失败" in window.log_panel.to_text()
    assert error_dialogs and error_dialogs[0][0] == "天地图连接失败"


def test_connection_test_detects_error_page(
    window: MainWindow, monkeypatch, qapp, error_dialogs
) -> None:
    """HTTP 200 的错误页不能当成"连接成功"。"""
    _stub_transport(
        monkeypatch,
        _png_handler(content=b"<html>Invalid Token</html>", content_type="text/html"),
    )
    _fill_token(window)

    window.download_panel.action_buttons[ACTION_TEST_CONNECTION].click()
    _wait_for_worker(window, qapp)

    text = window.download_panel.connection_label.text()
    assert text.startswith("连接失败：")
    assert "不是有效的图片瓦片" in text


def test_connection_test_without_token_never_starts_worker(
    window: MainWindow, error_dialogs
) -> None:
    window.download_panel.action_buttons[ACTION_TEST_CONNECTION].click()

    assert window._connection_worker is None
    text = window.download_panel.connection_label.text()
    assert text.startswith("连接失败：")
    assert "token 不能为空" in text
    assert "无法发起" in window.log_panel.to_text()


def test_connection_test_with_invalid_bounds(
    window: MainWindow, monkeypatch, qapp, error_dialogs
) -> None:
    _stub_transport(monkeypatch, _png_handler())
    _fill_token(window)
    window.download_panel._east_spin.setValue(100.0)  # east < west

    window.download_panel.action_buttons[ACTION_TEST_CONNECTION].click()

    assert window._connection_worker is None
    assert "west 必须小于 east" in window.download_panel.connection_label.text()


def test_connection_log_never_leaks_full_token(
    window: MainWindow, monkeypatch, qapp, error_dialogs
) -> None:
    _stub_transport(monkeypatch, _png_handler())
    _fill_token(window)

    window.download_panel.action_buttons[ACTION_TEST_CONNECTION].click()
    _wait_for_worker(window, qapp)

    log = window.log_panel.to_text()
    assert FAKE_TOKEN not in log
    assert "abcd****lmn" in log


def test_calculate_still_works_after_connection_test(
    window: MainWindow, monkeypatch, qapp, error_dialogs
) -> None:
    """Slice 2 不能破坏 Slice 0/1 的功能。"""
    _stub_transport(monkeypatch, _png_handler())
    _fill_token(window)

    window.download_panel.action_buttons[ACTION_TEST_CONNECTION].click()
    _wait_for_worker(window, qapp)
    window.download_panel.action_buttons[ACTION_CALCULATE].click()

    assert "预计瓦片：" in window.download_panel.result_label.text()
    assert window.status_group.title() == "下载状态"
    assert window.device_panel.device_list.item(0).text() == settings.UI_TEXT_DEVICE_EMPTY


# --------------------------------------------------------------------------- #
# ConnectionTestWorker：线程胶水自身的兜底行为
# --------------------------------------------------------------------------- #
def test_connection_worker_converts_unexpected_exception(qapp) -> None:
    """非 httpx 的意外异常也必须变成结果，不能冒泡进 Qt 事件循环。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("boom")

    provider = TiandituProvider(FAKE_TOKEN, transport=httpx.MockTransport(handler))
    results: list[object] = []
    worker = ConnectionTestWorker(provider, MapLayerType.VECTOR, 12, 3372, 1552)
    worker.result_ready.connect(results.append)

    worker.run()  # 同步执行，用例不依赖事件循环

    assert len(results) == 1
    result = results[0]
    assert result.success is False
    assert "测试异常" in (result.error or "")


def test_connection_worker_reports_success(qapp) -> None:
    provider = TiandituProvider(
        FAKE_TOKEN, transport=httpx.MockTransport(_png_handler())
    )
    results: list[object] = []
    worker = ConnectionTestWorker(provider, MapLayerType.IMAGE, 8, 105, 48)
    worker.result_ready.connect(results.append)

    worker.run()

    assert len(results) == 1
    result = results[0]
    assert result.success is True
    assert result.status_code == 200
    assert result.content_length == len(FAKE_PNG)


# --------------------------------------------------------------------------- #
# [开始下载] / [暂停] / [继续] 接入下载引擎（Slice 3）
# --------------------------------------------------------------------------- #
def _stub_download_transport(
    monkeypatch,
    handler,
    *,
    rate_limiter=None,
    policy: RetryPolicy = FAST_POLICY,
) -> None:
    """让 DownloadWorker 内部的 TileDownloader 走 MockTransport（不触网）。

    生产代码里没有任何测试钩子，因此这里替换的是 worker 模块引用的
    ``TileDownloader`` 名字——顺带把限速/退避调成"测试用"的档位。
    """
    original = download_worker_module.TileDownloader

    def factory(provider: TiandituProvider, **kwargs: object):
        kwargs["transport"] = httpx.MockTransport(handler)
        kwargs["retry_policy"] = policy
        kwargs["rate_limiter"] = (
            rate_limiter if rate_limiter is not None else RateLimiter.disabled()
        )
        return original(provider, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(download_worker_module, "TileDownloader", factory)


def _fill_small_region(window: MainWindow, zoom: int = 12) -> None:
    """把表单填成一个"小到能瞬间跑完"的区域。"""
    panel = window.download_panel
    panel._token_edit.setText(FAKE_TOKEN)
    panel._west_spin.setValue(116.0)
    panel._south_spin.setValue(39.0)
    panel._east_spin.setValue(116.1)
    panel._north_spin.setValue(39.1)
    panel._min_zoom_spin.setValue(zoom)
    panel._max_zoom_spin.setValue(zoom)


def _expected_tile_count(window: MainWindow) -> int:
    data = window.download_panel.get_form_data()
    return estimate_tile_count(
        GeoBounds(data["west"], data["south"], data["east"], data["north"]),
        data["min_zoom"],
        data["max_zoom"],
    )


def _wait_for_download(window: MainWindow, qapp, timeout: int = 20_000) -> None:
    """等待后台下载线程结束并把信号送回主线程。"""
    worker = window._download_worker
    assert isinstance(worker, QThread)
    assert worker.wait(timeout) is True
    for _ in range(6):
        qapp.processEvents()


def test_download_test_mode_notice_is_visible(window: MainWindow) -> None:
    """Spec：UI 必须写明"当前为下载引擎测试模式，Slice 4 起写入 MBTiles"。"""
    notice = window.download_panel.mode_notice.text()
    assert "下载引擎测试模式" in notice
    assert "MBTiles" in notice
    assert "不写入磁盘" in notice


def test_start_download_updates_status_and_log(
    window: MainWindow, monkeypatch, qapp, error_dialogs
) -> None:
    _stub_download_transport(monkeypatch, _png_handler())
    _fill_small_region(window)
    expected = _expected_tile_count(window)

    assert window._download_worker is None
    window.download_panel.action_buttons[ACTION_START].click()
    _wait_for_download(window, qapp)

    values = window._status_values
    assert values["task_status"].text() == "已完成"
    assert values["tile_total"].text() == f"{expected:,}"
    assert values["tile_done"].text() == f"{expected:,}"
    assert values["tile_succeeded"].text() == f"{expected:,}"
    assert values["tile_failed"].text() == "0"
    assert values["tile_skipped"].text() == "0"
    assert values["current_zoom"].text() == "12"
    assert "瓦片/秒" in values["speed"].text()
    assert window.progress_bar.value() == 100

    log = window.log_panel.to_text()
    assert "开始下载" in log
    assert "下载完成" in log
    assert "瓦片未落盘" in log
    assert error_dialogs == []

    # 结束后恢复空闲态
    assert window._download_worker is None
    assert window.download_panel.start_button.isEnabled() is True
    assert window.download_panel.pause_button.isEnabled() is False


def test_download_does_not_block_ui_thread(
    window: MainWindow, monkeypatch, qapp, error_dialogs
) -> None:
    """网络请求必须发生在后台线程：点击后立即返回，界面不冻结。"""
    _stub_download_transport(
        monkeypatch,
        _png_handler(delay=0.3),
        rate_limiter=RateLimiter.disabled(),
    )
    _fill_small_region(window)

    window.download_panel.action_buttons[ACTION_START].click()

    worker = window._download_worker
    assert isinstance(worker, QThread)
    assert worker.isRunning() is True
    assert worker.isCurrentThread() is False
    # 运行期间会冲突的按钮都被禁用
    assert window.download_panel.start_button.isEnabled() is False
    assert window.download_panel.action_buttons[ACTION_CALCULATE].isEnabled() is False
    assert window.download_panel.action_buttons[
        ACTION_TEST_CONNECTION
    ].isEnabled() is False
    assert window.download_panel.pause_button.isEnabled() is True

    _wait_for_download(window, qapp)
    assert window._status_values["task_status"].text() == "已完成"


def test_download_reports_failures(
    window: MainWindow, monkeypatch, qapp, error_dialogs
) -> None:
    _stub_download_transport(
        monkeypatch,
        _png_handler(
            status_code=403, content=b"<html>denied</html>", content_type="text/html"
        ),
    )
    _fill_small_region(window)
    expected = _expected_tile_count(window)

    window.download_panel.action_buttons[ACTION_START].click()
    _wait_for_download(window, qapp)

    values = window._status_values
    assert values["task_status"].text() == "已完成（有失败）"
    assert values["tile_failed"].text() == f"{expected:,}"
    assert values["tile_succeeded"].text() == "0"
    assert "下载完成" in window.log_panel.to_text()


def test_download_without_token_never_starts_worker(
    window: MainWindow, error_dialogs
) -> None:
    window.download_panel.action_buttons[ACTION_START].click()

    assert window._download_worker is None
    assert "token 不能为空" in window.log_panel.to_text()
    assert error_dialogs and error_dialogs[0][0] == "无法开始下载"


def test_download_with_invalid_bounds_never_starts_worker(
    window: MainWindow, error_dialogs
) -> None:
    _fill_small_region(window)
    window.download_panel._east_spin.setValue(100.0)  # east < west

    window.download_panel.action_buttons[ACTION_START].click()

    assert window._download_worker is None
    assert "west 必须小于 east" in window.log_panel.to_text()
    assert error_dialogs and error_dialogs[0][0] == "无法开始下载"


def test_download_exceeding_test_limit_is_refused(
    window: MainWindow, monkeypatch, qapp, error_dialogs
) -> None:
    """Spec：超出测试上限必须明确拒绝，**不能静默截断**。"""
    _stub_download_transport(monkeypatch, _png_handler())

    panel = window.download_panel
    panel._token_edit.setText(FAKE_TOKEN)
    panel._west_spin.setValue(116.0)
    panel._south_spin.setValue(39.0)
    panel._east_spin.setValue(117.0)
    panel._north_spin.setValue(40.0)
    panel._min_zoom_spin.setValue(8)
    panel._max_zoom_spin.setValue(17)

    total = _expected_tile_count(window)
    assert total > settings.GUI_TEST_MAX_TILES

    panel.action_buttons[ACTION_START].click()
    qapp.processEvents()

    # 一个请求都没发
    assert window._download_worker is None
    assert window.progress_bar.value() == 0

    log = window.log_panel.to_text()
    assert f"超过 Slice 3 测试上限 {settings.GUI_TEST_MAX_TILES} 张" in log
    assert "当前 Slice 3 为下载引擎验证模式" in log
    assert f"最多允许测试 {settings.GUI_TEST_MAX_TILES} 张瓦片" in log
    assert "Slice 4 接入 MBTiles 后再开放完整任务" in log
    # 明确提示，而不是悄悄把任务裁小
    assert "截断" not in log
    assert error_dialogs and error_dialogs[0][0] == "超出测试上限"
    assert (
        f"最多允许测试 {settings.GUI_TEST_MAX_TILES} 张瓦片"
        in error_dialogs[0][1]
    )


def test_test_limit_matches_settings() -> None:
    assert settings.GUI_TEST_MAX_TILES == 500
    assert settings.UI_TEXT_TEST_LIMIT_EXCEEDED.startswith(
        "当前 Slice 3 为下载引擎验证模式"
    )


def test_pause_and_resume_buttons_drive_the_engine(
    window: MainWindow, monkeypatch, qapp, error_dialogs
) -> None:
    """用低速限速器把任务拉长到足以观察暂停 / 继续。"""
    _stub_download_transport(
        monkeypatch,
        _png_handler(),
        rate_limiter=RateLimiter(requests_per_second=3),
    )
    _fill_small_region(window)
    expected = _expected_tile_count(window)

    window.download_panel.action_buttons[ACTION_START].click()
    qapp.processEvents()

    worker = window._download_worker
    assert isinstance(worker, DownloadWorker)
    assert worker.isRunning() is True

    window.download_panel.action_buttons[ACTION_PAUSE].click()
    qapp.processEvents()
    assert window._status_values["task_status"].text() == "已暂停"
    assert "下载已暂停" in window.log_panel.to_text()
    assert window.download_panel.pause_button.isEnabled() is False
    assert window.download_panel.resume_button.isEnabled() is True

    window.download_panel.action_buttons[ACTION_RESUME].click()
    qapp.processEvents()
    assert window._status_values["task_status"].text() == "下载中"
    assert "下载已继续" in window.log_panel.to_text()
    assert window.download_panel.pause_button.isEnabled() is True

    _wait_for_download(window, qapp)
    assert window._status_values["tile_succeeded"].text() == f"{expected:,}"


def test_pause_without_running_download_is_reported(window: MainWindow) -> None:
    """暂停按钮在空闲态是禁用的，但槽本身也要有守卫（信号可能来自别处）。"""
    assert window.download_panel.pause_button.isEnabled() is False
    window._pause_download()
    assert "当前没有正在运行的下载任务" in window.log_panel.to_text()


def test_resume_without_running_download_is_reported(window: MainWindow) -> None:
    assert window.download_panel.resume_button.isEnabled() is False
    window._resume_download()
    assert "当前没有可继续的下载任务" in window.log_panel.to_text()


def test_closing_window_cancels_running_download(
    window: MainWindow, monkeypatch, qapp, error_dialogs
) -> None:
    """Spec：窗口关闭时能够取消任务，且不留后台线程。"""
    _stub_download_transport(
        monkeypatch,
        _png_handler(delay=0.2),
        rate_limiter=RateLimiter.disabled(),
    )
    panel = window.download_panel
    panel._token_edit.setText(FAKE_TOKEN)
    panel._west_spin.setValue(116.0)
    panel._south_spin.setValue(39.0)
    panel._east_spin.setValue(116.5)
    panel._north_spin.setValue(39.5)
    panel._min_zoom_spin.setValue(12)
    panel._max_zoom_spin.setValue(12)
    assert _expected_tile_count(window) > 20  # 确保关窗时任务还在跑

    panel.action_buttons[ACTION_START].click()
    qapp.processEvents()
    worker = window._download_worker
    assert isinstance(worker, QThread)
    assert worker.isRunning() is True

    window.close()
    qapp.processEvents()

    assert worker.isRunning() is False  # 线程已回收，不会留下孤儿
    assert window._download_worker is None


def test_download_log_never_leaks_full_token(
    window: MainWindow, monkeypatch, qapp, error_dialogs
) -> None:
    _stub_download_transport(monkeypatch, _png_handler())
    _fill_small_region(window)

    window.download_panel.action_buttons[ACTION_START].click()
    _wait_for_download(window, qapp)

    assert FAKE_TOKEN not in window.log_panel.to_text()


def test_slice0_to_2_features_survive_download(
    window: MainWindow, monkeypatch, qapp, error_dialogs
) -> None:
    """Slice 3 不能破坏前几个 Slice 的功能。"""
    # 下载走 MockTransport；连通性测试走 Slice 2 的那套打桩，两条链路互不影响
    _stub_download_transport(monkeypatch, _png_handler())
    _stub_transport(monkeypatch, _png_handler())
    _fill_small_region(window)

    window.download_panel.action_buttons[ACTION_START].click()
    _wait_for_download(window, qapp)

    # Slice 1：计算
    window.download_panel.action_buttons[ACTION_CALCULATE].click()
    assert "预计瓦片：" in window.download_panel.result_label.text()
    # Slice 2：连通性测试
    window.download_panel.action_buttons[ACTION_TEST_CONNECTION].click()
    _wait_for_worker(window, qapp)
    assert window.download_panel.connection_label.text().startswith("连接成功")
    # Slice 0：其他区域
    assert window.status_group.title() == "下载状态"
    assert window.device_panel.device_list.item(0).text() == settings.UI_TEXT_DEVICE_EMPTY

