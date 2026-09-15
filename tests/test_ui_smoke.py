"""GUI 冒烟测试。

在 offscreen 平台下装配主窗口，覆盖：

- Slice 0：区域齐全、默认值正确、占位按钮点击不崩、启动日志可写入
- Slice 1："计算"按钮接入 Tile Math
- Slice 2："测试天地图连接"按钮接入 Provider（网络用 MockTransport 打桩）
- Slice 3：[开始下载]/[暂停]/[继续] 接入异步下载引擎
  （网络同样用 MockTransport 打桩，测试不触网）
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path

import httpx
import pytest
from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import QLabel, QMessageBox

import app.ui.download_worker as download_worker_module
import app.ui.main_window as main_window_module
from app.config import settings
from app.downloader import RateLimiter, RetryPolicy
from app.geo import (
    GeoBounds,
    calculate_tile_ranges,
    estimate_size_bytes,
    estimate_tile_count,
    format_size,
    lat_to_tile_y,
    lon_to_tile_x,
)
from app.main import STARTUP_MESSAGES, _report_startup
from app.mbtiles import MBTilesMetadata, MBTilesReader, MBTilesWriter
from app.models import DownloadTaskStatus, MapLayerType
from app.providers import PNG_SIGNATURE, TiandituProvider
from app.services import build_output_path
from app.tasks import TaskService, TaskStore
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
def window(qapp, tasks_file: Path) -> MainWindow:
    """每个用例一个全新的主窗口。

    任务记录注入临时路径：默认的 ``~/MapDownloader/data/tasks.json`` 会污染
    真实用户目录，而且用例之间会互相看到对方留下的任务。
    """
    win = MainWindow(task_service=TaskService(TaskStore(tasks_file)))
    yield win
    # 用例可能留下正在跑的下载。这里把"退出确认"直接跳过（那是有头交互的产物，
    # 不该由夹具去点按钮），改为取消并等线程回收——QThread 在运行时被销毁
    # 会让 Qt 直接中止进程。
    win._exit_confirmed = True
    worker = win._download_worker
    if worker is not None and worker.isRunning():
        worker.cancel()
        worker.wait(10_000)
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
        "downloaded_bytes",
    }
    assert values["task_status"].text() == "空闲"
    assert values["tile_total"].text() == "0"
    assert values["tile_done"].text() == "0"
    assert values["tile_succeeded"].text() == "0"
    assert values["tile_failed"].text() == "0"
    assert values["tile_skipped"].text() == "0"
    assert values["downloaded_bytes"].text() == "0 B"
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
        "map_name",
    }
    assert data["token"] == ""
    assert data["map_type"] is MapLayerType.VECTOR
    assert data["with_labels"] is True
    assert data["min_zoom"] == settings.DEFAULT_MIN_ZOOM
    assert data["max_zoom"] == settings.DEFAULT_MAX_ZOOM
    assert isinstance(data["output_dir"], Path)
    assert data["map_name"] == settings.DEFAULT_MAP_NAME
    assert data["west"] < data["east"]
    assert data["south"] < data["north"]


def test_form_data_reflects_edits(window: MainWindow) -> None:
    panel = window.download_panel
    panel._token_edit.setText("  my-token  ")
    panel._map_type_combo.setCurrentIndex(1)
    panel._labels_check.setChecked(False)
    panel._min_zoom_spin.setValue(10)
    panel._max_zoom_spin.setValue(12)
    panel.map_name_edit.setText("  beijing  ")

    data = panel.get_form_data()
    assert data["token"] == "my-token"  # 首尾空白被裁掉
    assert data["map_type"] is MapLayerType.IMAGE
    assert data["with_labels"] is False
    assert data["min_zoom"] == 10
    assert data["max_zoom"] == 12
    assert data["map_name"] == "beijing"


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
    # 设备管理属于 Slice 6；本 Slice 只要"点了不崩 + 如实说明"
    assert "Slice 6" in text
    assert "adb" in text


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
        f"预计瓦片：{expected_count:,}\n预计容量：{expected_size}\n"
        f"输出文件：{build_output_path(data['output_dir'], data['map_name'])}"
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
    assert "Slice 6" in window.log_panel.to_text()


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
# [开始下载] / [暂停] / [继续]：下载并写入 MBTiles（Slice 3 引擎 + Slice 4 落盘）
# --------------------------------------------------------------------------- #
def _stub_download_service(
    monkeypatch,
    handler,
    *,
    rate_limiter=None,
    policy: RetryPolicy = FAST_POLICY,
    batch_size: int = 7,
) -> None:
    """让 DownloadWorker 内部的 MapDownloadService 走 MockTransport（不触网）。

    生产代码里没有任何测试钩子，因此这里替换的是 worker 模块引用的
    ``MapDownloadService`` 名字——顺带把限速/退避/批次调成"测试用"的档位。
    替换 service 而不是 ``TileDownloader``：Slice 4 起下载器由 service 自己创建，
    在 service 这一层注入才是唯一拦得住的入口。
    """
    original = download_worker_module.MapDownloadService

    def factory(provider: TiandituProvider, **kwargs: object):
        kwargs["transport"] = httpx.MockTransport(handler)
        kwargs["retry_policy"] = policy
        kwargs["rate_limiter"] = (
            rate_limiter if rate_limiter is not None else RateLimiter.disabled()
        )
        kwargs["batch_size"] = batch_size
        return original(provider, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(download_worker_module, "MapDownloadService", factory)


def _fill_small_region(
    window: MainWindow, work_dir: Path | None = None, zoom: int = 12
) -> None:
    """把表单填成一个"小到能瞬间跑完"的区域。

    Slice 4 起必须显式指定输出目录：否则会往用户主目录里写文件。
    传 ``None`` 只用于"不打算真的下载"的用例（校验失败、大任务确认等）。
    """
    panel = window.download_panel
    panel._token_edit.setText(FAKE_TOKEN)
    panel._west_spin.setValue(116.0)
    panel._south_spin.setValue(39.0)
    panel._east_spin.setValue(116.1)
    panel._north_spin.setValue(39.1)
    panel._min_zoom_spin.setValue(zoom)
    panel._max_zoom_spin.setValue(zoom)
    if work_dir is not None:
        panel._output_edit.setText(str(work_dir))
    panel.map_name_edit.setText("beijing")


def _expected_output_path(window: MainWindow) -> Path:
    data = window.download_panel.get_form_data()
    return build_output_path(data["output_dir"], data["map_name"])


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


def _wait_until(qapp, predicate, timeout_s: float = 8.0) -> bool:
    """轮询等待条件成立，期间持续处理 Qt 事件（主线程不阻塞）。

    用途：取消类用例需要"任务已经真的写出了一些瓦片"这个中间状态，
    否则取消发生在第一个请求返回之前，测试就退化成了"取消一个空任务"。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_download_output_notice_is_visible(window: MainWindow) -> None:
    """Spec §12：UI 要说明产物是 MBTiles 单文件（不再是"测试模式"）。"""
    notice = window.download_panel.mode_notice.text()
    assert "MBTiles" in notice
    assert ".mbtiles" in notice
    assert "osmdroid" in notice
    assert "测试模式" not in notice


def test_start_download_writes_mbtiles(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """Spec §12/§19：点一次「开始下载」必须真的产出可用的 ``.mbtiles``。"""
    _stub_download_service(monkeypatch, _png_handler())
    _fill_small_region(window, work_dir)
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
    assert values["downloaded_bytes"].text() != "0 B"
    assert window.progress_bar.value() == 100

    # 产物本身
    output = _expected_output_path(window)
    assert output.exists()
    assert output.suffix == ".mbtiles"
    # 单文件交付：WAL / SHM 旁挂文件必须已被清理
    assert sorted(item.name for item in work_dir.iterdir()) == [output.name]

    with MBTilesReader(output) as reader:
        assert reader.get_tile_count() == expected
        assert reader.get_zoom_range() == (12, 12)
        assert reader.get_metadata()["name"] == "beijing"

    # 结果区展示 Spec §12 要求的那几行
    result = window.download_panel.download_result_label.text()
    assert "文件：" in result
    assert f"瓦片：{expected:,}" in result
    assert "大小：" in result
    assert "Zoom：12~12" in result
    assert "验证：PASS" in result

    log = window.log_panel.to_text()
    assert "开始下载" in log
    assert "下载完成" in log
    assert f"输出文件：{output}" in log
    assert "验证 PASS" in log
    assert error_dialogs == []

    # 结束后恢复空闲态
    assert window._download_worker is None
    assert window.download_panel.start_button.isEnabled() is True
    assert window.download_panel.pause_button.isEnabled() is False


def test_download_does_not_block_ui_thread(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """网络请求必须发生在后台线程：点击后立即返回，界面不冻结。"""
    _stub_download_service(
        monkeypatch,
        _png_handler(delay=0.3),
        rate_limiter=RateLimiter.disabled(),
    )
    _fill_small_region(window, work_dir)

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
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """整片失败时：状态、结果区、日志都要说清楚，且库里应当是 0 张瓦片。"""
    _stub_download_service(
        monkeypatch,
        _png_handler(
            status_code=403, content=b"<html>denied</html>", content_type="text/html"
        ),
    )
    _fill_small_region(window, work_dir)
    expected = _expected_tile_count(window)

    window.download_panel.action_buttons[ACTION_START].click()
    _wait_for_download(window, qapp)

    values = window._status_values
    assert values["task_status"].text() == "已完成（有失败）"
    assert values["tile_failed"].text() == f"{expected:,}"
    assert values["tile_succeeded"].text() == "0"

    result = window.download_panel.download_result_label.text()
    assert "验证：FAIL" in result          # 零瓦片的库不是可用地图
    assert "失败原因：" in result
    assert "403" in result
    # Token 无效是常见原因，提示里必须点到
    assert "Token" in result

    assert "下载完成" in window.log_panel.to_text()


def test_download_without_token_never_starts_worker(
    window: MainWindow, error_dialogs
) -> None:
    window.download_panel.action_buttons[ACTION_START].click()

    assert window._download_worker is None
    assert "token 不能为空" in window.log_panel.to_text()
    assert error_dialogs and error_dialogs[0][0] == "无法开始下载"


def test_download_with_invalid_bounds_never_starts_worker(
    window: MainWindow, error_dialogs, work_dir: Path
) -> None:
    _fill_small_region(window, work_dir)
    window.download_panel._east_spin.setValue(100.0)  # east < west

    window.download_panel.action_buttons[ACTION_START].click()

    assert window._download_worker is None
    assert "west 必须小于 east" in window.log_panel.to_text()
    assert error_dialogs and error_dialogs[0][0] == "无法开始下载"
    # 连数据库都不该被创建
    assert list(work_dir.iterdir()) == []


def test_download_without_map_name_is_refused(
    window: MainWindow, error_dialogs, work_dir: Path
) -> None:
    """地图名称决定文件名，为空时必须先让用户填，而不是替他编一个。"""
    _fill_small_region(window, work_dir)
    window.download_panel.map_name_edit.setText("")

    assert window._resolve_output_path(window.download_panel.get_form_data()) is None

    window.download_panel.action_buttons[ACTION_START].click()

    assert window._download_worker is None
    assert settings.UI_TEXT_MAP_NAME_REQUIRED in window.log_panel.to_text()
    assert error_dialogs and error_dialogs[0] == (
        "无法开始下载",
        settings.UI_TEXT_MAP_NAME_REQUIRED,
    )
    assert list(work_dir.iterdir()) == []


def test_output_path_preview_follows_map_name(
    window: MainWindow, work_dir: Path
) -> None:
    """改地图名称 → 预估结果里的「输出文件」行跟着变（不用重算瓦片数）。"""
    _fill_small_region(window, work_dir)
    window.download_panel.action_buttons[ACTION_CALCULATE].click()
    total_text = window.download_panel.result_label.text().splitlines()[0]
    assert f"输出文件：{work_dir / 'beijing.mbtiles'}" in (
        window.download_panel.result_label.text()
    )

    window.download_panel.map_name_edit.setText("shanghai")

    text = window.download_panel.result_label.text()
    assert f"输出文件：{work_dir / 'shanghai.mbtiles'}" in text
    # 瓦片数没有被重新计算（只是重绘了路径行）
    assert text.splitlines()[0] == total_text

    window.download_panel.map_name_edit.setText("")
    assert "（请填写地图名称）" in window.download_panel.result_label.text()


def test_map_name_is_sanitized_into_filename(
    window: MainWindow, error_dialogs, work_dir: Path
) -> None:
    """Windows 非法字符不能原样进文件名（否则落盘位置与预期不符）。"""
    _fill_small_region(window, work_dir)
    window.download_panel.map_name_edit.setText("北京/2024:地图")

    prepared = window._prepare_download()

    assert prepared is not None
    assert prepared.output_path.name == "北京_2024_地图.mbtiles"
    assert prepared.output_path.parent == work_dir


def _fill_large_region(window: MainWindow, work_dir: Path) -> int:
    """把表单填成"预计超过 10,000 张"的大任务，返回预计瓦片数。"""
    panel = window.download_panel
    panel._token_edit.setText(FAKE_TOKEN)
    panel._west_spin.setValue(116.0)
    panel._south_spin.setValue(39.0)
    panel._east_spin.setValue(117.0)
    panel._north_spin.setValue(40.0)
    panel._min_zoom_spin.setValue(8)
    panel._max_zoom_spin.setValue(17)
    panel._output_edit.setText(str(work_dir))
    panel.map_name_edit.setText("big")
    return _expected_tile_count(window)


def test_large_task_asks_for_confirmation_without_starting(
    window: MainWindow, qapp, error_dialogs, work_dir: Path
) -> None:
    """Spec §12：预计 >10000 张时弹确认框，**只提示、不限制**。"""
    total = _fill_large_region(window, work_dir)
    assert total > settings.GUI_LARGE_TASK_CONFIRM_TILES

    window.download_panel.action_buttons[ACTION_START].click()
    qapp.processEvents()

    # 任务还没跑起来，但已经被"托住"等用户回答
    assert window._download_worker is None
    assert window.progress_bar.value() == 0
    assert window._pending_plan is not None
    assert window._pending_plan.total == total
    assert window._confirm_dialog is not None
    assert list(work_dir.iterdir()) == []

    # 提示里带上总量与容量，且明确说明"不限制规模"
    text = window._confirm_dialog.text()
    assert f"{total:,}" in text
    assert "仅作提醒" in text
    assert "确认继续下载吗" in text

    log = window.log_panel.to_text()
    assert "大任务确认框" in log


def test_large_task_confirmed_launches_download(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """点「是」→ 照常启动（这里把真正的启动换掉，避免真跑二十万张）。"""
    launched: list[object] = []
    monkeypatch.setattr(window, "_launch_download", launched.append)

    total = _fill_large_region(window, work_dir)
    window.download_panel.action_buttons[ACTION_START].click()
    qapp.processEvents()

    dialog = window._confirm_dialog
    assert dialog is not None
    dialog.button(QMessageBox.StandardButton.Yes).click()
    qapp.processEvents()

    assert len(launched) == 1
    assert launched[0].total == total  # type: ignore[attr-defined]
    assert window._pending_plan is None
    assert window._confirm_dialog is None
    assert "用户确认继续大任务" in window.log_panel.to_text()


def test_large_task_rejected_does_not_start(
    window: MainWindow, qapp, error_dialogs, work_dir: Path
) -> None:
    """点「否」→ 一个请求都不发，也不留下半个文件。"""
    _fill_large_region(window, work_dir)
    window.download_panel.action_buttons[ACTION_START].click()
    qapp.processEvents()

    dialog = window._confirm_dialog
    assert dialog is not None
    dialog.button(QMessageBox.StandardButton.No).click()
    qapp.processEvents()

    assert window._download_worker is None
    assert window._pending_plan is None
    assert list(work_dir.iterdir()) == []
    assert "用户取消了大任务下载" in window.log_panel.to_text()


def test_large_task_confirm_dialog_does_not_block_event_loop(
    window: MainWindow, qapp, error_dialogs, work_dir: Path
) -> None:
    """确认框必须**不阻塞事件循环**。

    模态 ``exec()`` 会卡住事件循环，在无头环境里直接把用例挂死
    （Slice 0 踩过的坑）。这里从两个角度锁住这个行为：运行期确认它已经显示
    且事件仍在流转，外加一条源码级护栏，防止以后有人把 ``show()`` 改成 ``exec()``。
    """
    _fill_large_region(window, work_dir)
    window.download_panel.action_buttons[ACTION_START].click()
    qapp.processEvents()

    dialog = window._confirm_dialog
    assert dialog is not None
    assert dialog.isVisible() is True

    # 事件循环没被阻塞：还能继续处理事件、驱动别的动作
    window.log_panel.append_log("event-loop-alive")
    qapp.processEvents()
    assert "event-loop-alive" in window.log_panel.to_text()

    source = inspect.getsource(main_window_module.MainWindow._confirm_large_task)
    assert ".exec()" not in source
    assert ".show()" in source

    dialog.button(QMessageBox.StandardButton.No).click()
    qapp.processEvents()


def test_large_task_threshold_matches_settings() -> None:
    """阈值与文案都在 settings 里，UI 不得自己写死。"""
    assert settings.GUI_LARGE_TASK_CONFIRM_TILES == 10_000
    assert "{total" in settings.UI_TEXT_LARGE_TASK_CONFIRM
    assert "仅作提醒" in settings.UI_TEXT_LARGE_TASK_CONFIRM
    # Slice 3 的 500 张硬上限已经不存在了
    assert not hasattr(settings, "GUI_TEST_MAX_TILES")


def test_pause_and_resume_buttons_drive_the_engine(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """用低速限速器把任务拉长到足以观察暂停 / 继续。"""
    _stub_download_service(
        monkeypatch,
        _png_handler(),
        rate_limiter=RateLimiter(requests_per_second=3),
    )
    _fill_small_region(window, work_dir)
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
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """Spec §17：关窗时先问一次，确认后取消任务、不留后台线程；已写入的瓦片保留。"""
    _stub_download_service(
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
    panel._output_edit.setText(str(work_dir))
    panel.map_name_edit.setText("beijing")
    assert _expected_tile_count(window) > 20  # 确保关窗时任务还在跑

    window.show()  # 让 isVisible 这条断言有意义（offscreen 下默认不显示）
    qapp.processEvents()
    panel.action_buttons[ACTION_START].click()
    qapp.processEvents()
    worker = window._download_worker
    assert isinstance(worker, QThread)
    assert worker.isRunning() is True

    # 先让任务真的写出一些瓦片，否则"取消后文件里有数据"这条断言没意义
    assert _wait_until(
        qapp, lambda: window._status_values["tile_succeeded"].text() not in ("0", "—")
    ), "下载在超时前没有写出任何瓦片"

    window.close()
    qapp.processEvents()

    # Slice 5：不是直接退出，而是先弹一个非模态确认框，下载还在跑
    dialog = window._exit_dialog
    assert dialog is not None
    assert dialog.isVisible() is True
    assert worker.isRunning() is True
    assert window.isVisible() is True
    assert "退出后任务将暂停" in dialog.text()

    dialog.button(QMessageBox.StandardButton.Yes).click()
    qapp.processEvents()

    assert worker.isRunning() is False  # 线程已回收，不会留下孤儿
    assert window._download_worker is None
    # 取消不删除文件：磁盘上留一个可用的（不完整的）mbtiles
    output = work_dir / "beijing.mbtiles"
    assert output.exists()
    with MBTilesReader(output) as reader:
        assert reader.get_tile_count() > 0


def test_closing_window_can_be_cancelled(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """Spec §17：用户在退出确认里点"取消"→ 窗口留着，下载继续跑。"""
    _stub_download_service(
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
    panel._output_edit.setText(str(work_dir))
    panel.map_name_edit.setText("beijing")
    assert _expected_tile_count(window) > 20

    panel.action_buttons[ACTION_START].click()
    qapp.processEvents()
    worker = window._download_worker
    assert isinstance(worker, QThread)
    assert _wait_until(
        qapp, lambda: window._status_values["tile_succeeded"].text() not in ("0", "—")
    ), "下载在超时前没有写出任何瓦片"

    window.close()
    qapp.processEvents()
    dialog = window._exit_dialog
    assert dialog is not None

    dialog.button(QMessageBox.StandardButton.No).click()
    qapp.processEvents()

    assert window._exit_dialog is None
    assert window._exit_confirmed is False
    assert worker.isRunning() is True  # 下载没被打断
    assert "已取消退出，下载继续" in window.log_panel.to_text()

    # 收尾：让夹具能干净地关掉窗口
    worker.cancel()
    _wait_for_download(window, qapp)


def test_cancelled_download_keeps_partial_mbtiles(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """Spec §13：取消后停止下载、drain 写队列、flush、close，**保留**已有数据。"""
    _stub_download_service(
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
    panel._output_edit.setText(str(work_dir))
    panel.map_name_edit.setText("beijing")
    total = _expected_tile_count(window)
    assert total > 20

    panel.action_buttons[ACTION_START].click()
    qapp.processEvents()
    worker = window._download_worker
    assert isinstance(worker, QThread)

    assert _wait_until(
        qapp, lambda: window._status_values["tile_succeeded"].text() not in ("0", "—")
    ), "下载在超时前没有写出任何瓦片"
    worker.cancel()
    _wait_for_download(window, qapp)

    output = work_dir / "beijing.mbtiles"
    assert output.exists()
    assert sorted(item.name for item in work_dir.iterdir()) == [output.name]

    with MBTilesReader(output) as reader:
        written = reader.get_tile_count()
    assert 0 < written < total          # 部分完成，且确实落了盘

    assert window._status_values["task_status"].text() == "已取消"
    result = window.download_panel.download_result_label.text()
    assert "已取消" in result
    assert f"瓦片：{written:,}" in result

    log = window.log_panel.to_text()
    assert settings.UI_TEXT_CANCELLED_KEEP_TILES.splitlines()[0] in log
    # Slice 5 起：取消后的提示要指向"继续下载"这条真实可用的路径
    assert settings.UI_TEXT_CANCELLED_KEEP_TILES.splitlines()[1] in log
    assert "继续" in log
    # 但界面上不能出现"会自己补下"这类承诺——续传永远需要用户点一次
    assert "自动补下" not in log


def test_download_log_never_leaks_full_token(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    _stub_download_service(monkeypatch, _png_handler())
    _fill_small_region(window, work_dir)

    window.download_panel.action_buttons[ACTION_START].click()
    _wait_for_download(window, qapp)

    assert FAKE_TOKEN not in window.log_panel.to_text()
    # 结果区同样不能带出 Token
    assert FAKE_TOKEN not in window.download_panel.download_result_label.text()


def test_slice0_to_2_features_survive_download(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """Slice 3/4 不能破坏前几个 Slice 的功能。"""
    # 下载走 MockTransport；连通性测试走 Slice 2 的那套打桩，两条链路互不影响
    _stub_download_service(monkeypatch, _png_handler())
    _stub_transport(monkeypatch, _png_handler())
    _fill_small_region(window, work_dir)

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


# --------------------------------------------------------------------------- #
# 布局护栏（Slice 4 新增了地图名称 / 下载结果 / 已下载字节）
# --------------------------------------------------------------------------- #
def test_left_column_has_no_scrollbars_when_idle(window: MainWindow, qapp) -> None:
    """左列不得挤出滚动条。

    Slice 4 往配置列里加了「地图名称」行和「下载结果」分组，两个真实风险：

    - 结果区要显示 Windows 绝对路径（无空格的长 token），一旦让它可以撑宽布局，
      就会连带把横向滚动条和纵向滚动条一起拽出来（两者会互相触发）；
    - 内容一旦高于窗口，纵向滚动条会吃掉 ~17px 可视宽度，横向滚动条随之出现。

    因此这里同时断言两个方向都不溢出。
    """
    window.resize(settings.DEFAULT_WINDOW_WIDTH, settings.DEFAULT_WINDOW_HEIGHT)
    window.show()
    for _ in range(6):
        qapp.processEvents()

    scroll = window._left_scroll
    assert scroll.horizontalScrollBar().maximum() == 0
    assert scroll.verticalScrollBar().maximum() == 0


def test_left_column_keeps_no_horizontal_scroll_with_long_output_path(
    window: MainWindow, qapp, work_dir: Path
) -> None:
    """填上一条很长的输出路径后，仍然不允许出现横向滚动条。"""
    window.resize(settings.DEFAULT_WINDOW_WIDTH, settings.DEFAULT_WINDOW_HEIGHT)
    window.show()
    _fill_small_region(window, work_dir)
    window.download_panel.map_name_edit.setText("a" * 60)
    window.download_panel.action_buttons[ACTION_CALCULATE].click()
    window.download_panel.set_download_result(
        [
            "文件：" + str(work_dir / ("a" * 60 + ".mbtiles")),
            "瓦片：12,345",
            "大小：352.40 MB",
            "Zoom：8~16",
            "验证：PASS",
        ]
    )
    for _ in range(6):
        qapp.processEvents()

    assert window._left_scroll.horizontalScrollBar().maximum() == 0


def test_download_result_group_hidden_until_first_result(window: MainWindow) -> None:
    """「下载结果」分组在没有结果时要收起，否则会挤掉下方的进度条。"""
    group = window.download_panel.download_result_group
    assert group.isHidden() is True

    window.download_panel.set_download_result(["文件：x.mbtiles", "验证：PASS"])
    assert group.isHidden() is False
    assert "验证：PASS" in window.download_panel.download_result_label.text()

    window.download_panel.clear_download_result()
    assert group.isHidden() is True

    window.download_panel.set_download_result_error("磁盘已满")
    assert group.isHidden() is False
    assert window.download_panel.download_result_label.text() == "下载失败：磁盘已满"


def test_progress_bar_stays_visible_while_downloading(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """下载进行中，「总进度」必须落在可视范围内。

    这是把「下载结果」分组做成按需显示的直接原因：多占 120px 就会把状态区
    的尾部（含进度条）顶到折叠线以下，而进度条恰恰是下载时最该看见的东西。
    """
    _stub_download_service(
        monkeypatch,
        _png_handler(delay=0.25),
        rate_limiter=RateLimiter.disabled(),
    )
    window.resize(settings.DEFAULT_WINDOW_WIDTH, settings.DEFAULT_WINDOW_HEIGHT)
    window.show()
    _fill_small_region(window, work_dir)
    window.download_panel.action_buttons[ACTION_START].click()
    qapp.processEvents()
    assert window._download_worker is not None
    assert window._download_worker.isRunning() is True

    viewport = window._left_scroll.viewport()
    bottom = window.progress_bar.mapTo(
        viewport, window.progress_bar.rect().bottomLeft()
    ).y()
    assert bottom <= viewport.height()
    assert window._left_scroll.verticalScrollBar().maximum() == 0

    _wait_for_download(window, qapp)



def test_wrapping_label_never_clips_long_paths(window: MainWindow, qapp) -> None:
    r"""显示用的长路径必须**在字符处断行**，而不是被横向裁掉。

    ``QLabel.setWordWrap(True)`` 只在单词边界断行，Windows 绝对路径这类
    超长无空格 token 会被整段裁掉（实测"文件：C:\...\map.mbtiles"只显示到
    一半，强制重排也无效）。面板因此改用自己按字体宽度折行的标签，
    这里断言"每一行的宽度都不超过标签宽度"——即不可能发生横向裁切。
    """
    panel = window.download_panel
    window.resize(settings.DEFAULT_WINDOW_WIDTH, settings.DEFAULT_WINDOW_HEIGHT)
    window.show()
    long_path = "文件：C:\\" + "a-fairly-long-directory-segment\\" * 8 + "map.mbtiles"
    panel.set_download_result([long_path, "验证：PASS"])
    for _ in range(8):
        qapp.processEvents()

    label = panel.download_result_label
    rendered = QLabel.text(label)  # 显示用文本（含自动插入的换行）
    assert "\n" in rendered, "超长路径没有被折行"

    metrics = label.fontMetrics()
    for line in rendered.splitlines():
        assert metrics.horizontalAdvance(line) <= label.width(), line

    # text() 仍返回原始文本：折行不污染调用方拿到的内容
    assert label.text() == long_path + "\n验证：PASS"
    assert panel.download_result_label.text().count("\n") == 1


def test_wrap_anywhere_handles_edge_cases(qapp) -> None:
    from app.ui.download_panel import _wrap_anywhere

    metrics = QLabel().fontMetrics()
    assert _wrap_anywhere("abc", 0, metrics) == "abc"      # 宽度无效时原样返回
    assert _wrap_anywhere("", 100, metrics) == ""          # 空文本不炸
    assert _wrap_anywhere("ab\ncd", 1000, metrics) == "ab\ncd"  # 已有换行保留


# --------------------------------------------------------------------------- #
# 任务列表 / 断点续传（Slice 5）
# --------------------------------------------------------------------------- #
def _seed_existing_map(window: MainWindow, tiles: int = 3) -> Path:
    """按当前表单的路径预置一个"上次没下完"的地图包（含若干真瓦片）。

    走真正的 Writer 而不是手拼 SQLite：预置文件必须与生产环境**同构**，
    否则"续传能不能跳过已有瓦片"这件事根本没被验证。
    """
    data = window.download_panel.get_form_data()
    bounds = GeoBounds(data["west"], data["south"], data["east"], data["north"])
    path = build_output_path(data["output_dir"], data["map_name"])
    path.parent.mkdir(parents=True, exist_ok=True)

    writer = MBTilesWriter(path, batch_size=7)
    writer.open()
    writer.write_metadata(
        MBTilesMetadata.for_bounds(
            bounds,
            data["min_zoom"],
            data["max_zoom"],
            name=data["map_name"],
            description="",
        )
    )
    coords: list[tuple[int, int, int]] = []
    for item in calculate_tile_ranges(bounds, data["min_zoom"], data["max_zoom"]):
        for x in range(item.min_x, item.max_x + 1):
            for y in range(item.min_y, item.max_y + 1):
                coords.append((item.zoom, x, y))
    for z, x, y in coords[:tiles]:
        writer.write_tile(z, x, y, FAKE_PNG)
    writer.close()
    return path


def _existing_dialog(window: MainWindow) -> QMessageBox:
    """取出"已有地图文件"三选一弹窗（不存在就断言失败）。"""
    dialog = window._exists_dialog
    assert dialog is not None, "应当弹出「已有地图文件」选择框"
    return dialog


def _panel_task_id(window: MainWindow, row: int = 0) -> str:
    """取任务列表第 ``row`` 行挂着的 task_id。"""
    item = window.task_panel.table.item(row, 0)
    assert item is not None
    task_id = item.data(Qt.ItemDataRole.UserRole)
    assert isinstance(task_id, str)
    return task_id


def _task_status(window: MainWindow, task_id: str) -> str:
    fresh = window.task_service.get_task(task_id)
    assert fresh is not None
    return fresh.status.value


class _FakeProgress:
    """引擎进度的鸭子类型替身（``DownloadTask.apply_progress`` 只认字段名）。"""

    def __init__(self, total: int = 0, completed: int = 0, skipped: int = 0) -> None:
        self.total = total
        self.completed = completed
        self.succeeded = completed - skipped
        self.failed = 0
        self.skipped = skipped
        self.current_zoom = 12
        self.tiles_per_second = 8.0


def test_task_panel_exists_and_starts_empty(window: MainWindow) -> None:
    """Spec §14：界面上有一块任务区域；没有任务时按钮不可用。"""
    panel = window.task_panel
    assert panel.title() == "下载任务"
    assert panel.task_count == 0
    assert panel.empty_label.text() == settings.UI_TEXT_TASK_PANEL_EMPTY
    assert panel.resume_button.isEnabled() is False
    assert panel.delete_button.isEnabled() is False


def test_task_panel_lists_persisted_tasks_on_startup(
    window: MainWindow, tasks_file: Path, work_dir: Path, qapp
) -> None:
    """重启程序后，磁盘上的任务记录要出现在列表里（Spec §8 / §14）。"""
    task = window.task_service.create_task(
        name="上海地图",
        bounds=GeoBounds(121.0, 31.0, 121.2, 31.2),
        min_zoom=10,
        max_zoom=12,
        map_layer=MapLayerType.VECTOR,
        output_path=work_dir / "shanghai.mbtiles",
        total_tiles=500,
    )
    window.task_service.set_status(task, DownloadTaskStatus.PAUSED)
    window.task_service.update_progress(task, _FakeProgress(total=500, completed=321))
    window.task_service.save_now(task)

    second = MainWindow(task_service=TaskService(TaskStore(tasks_file)))
    try:
        assert second.task_panel.task_count == 1
        row = second.task_panel.row_text(0)
        assert row[0] == "上海地图"
        assert row[1] == "已暂停"
        assert "321" in row[2] and "500" in row[2]
        assert row[3] == str(work_dir / "shanghai.mbtiles")
    finally:
        second._exit_confirmed = True
        second.close()
        second.deleteLater()


def test_interrupted_task_is_recovered_as_paused_on_startup(
    tasks_file: Path, work_dir: Path, qapp
) -> None:
    """Spec §10：上次异常退出遗留的"下载中"要降级为"已暂停"，且**不自动下载**。"""
    service = TaskService(TaskStore(tasks_file))
    task = service.create_task(
        name="北京地图",
        bounds=GeoBounds(116.0, 39.0, 116.1, 39.1),
        min_zoom=12,
        max_zoom=12,
        map_layer=MapLayerType.VECTOR,
        output_path=work_dir / "beijing.mbtiles",
        total_tiles=6,
    )
    service.start_downloading(task)  # 模拟"进程被强杀时状态还是 downloading"

    window = MainWindow(task_service=TaskService(TaskStore(tasks_file)))
    try:
        assert window._download_worker is None, "不允许自动开始下载"
        reloaded = window.task_service.get_task(task.id)
        assert reloaded is not None
        assert reloaded.status is DownloadTaskStatus.PAUSED
        assert window.task_panel.row_text(0)[1] == "已暂停"
        log = window.log_panel.to_text()
        assert settings.UI_TEXT_TASK_RECOVERED.splitlines()[0] in log
        assert "标记为「已暂停」" in log
    finally:
        window._exit_confirmed = True
        window.close()
        window.deleteLater()


def test_existing_file_asks_before_touching_anything(
    window: MainWindow, qapp, error_dialogs, work_dir: Path
) -> None:
    """Spec §15：目标 .mbtiles 已存在时先问，不静默覆盖也不静默删除。"""
    _fill_small_region(window, work_dir)
    path = _seed_existing_map(window, tiles=3)
    before = path.read_bytes()

    window.download_panel.action_buttons[ACTION_START].click()
    qapp.processEvents()

    assert window._download_worker is None
    dialog = _existing_dialog(window)
    assert dialog.isVisible() is True
    text = dialog.text()
    assert "已有地图文件" in text
    assert "已有瓦片：3 张" in text
    assert "继续下载" in text and "重新下载" in text
    # 三个标准按钮被换成了业务话术（顺序与角色保持不变）
    labels = {
        dialog.button(QMessageBox.StandardButton.Yes).text(),
        dialog.button(QMessageBox.StandardButton.No).text(),
        dialog.button(QMessageBox.StandardButton.Cancel).text(),
    }
    assert labels == {
        settings.UI_TEXT_MBTILES_EXISTS_CONTINUE,
        settings.UI_TEXT_MBTILES_EXISTS_RESTART,
        settings.UI_TEXT_MBTILES_EXISTS_CANCEL,
    }
    assert path.read_bytes() == before  # 还没动过用户的文件
    assert "检测到已有地图文件" in window.log_panel.to_text()

    dialog.button(QMessageBox.StandardButton.Cancel).click()
    qapp.processEvents()


def test_existing_file_cancel_changes_nothing(
    window: MainWindow, qapp, error_dialogs, work_dir: Path
) -> None:
    """选「取消」→ 文件原样、没有备份、也不留下半截任务记录。"""
    _fill_small_region(window, work_dir)
    path = _seed_existing_map(window, tiles=2)
    before = path.read_bytes()

    window.download_panel.action_buttons[ACTION_START].click()
    qapp.processEvents()
    _existing_dialog(window).button(QMessageBox.StandardButton.Cancel).click()
    qapp.processEvents()

    assert path.read_bytes() == before
    assert list(work_dir.glob("*.bak-*")) == []
    assert window._download_worker is None
    assert window.task_panel.task_count == 0, "取消后不该留下 CREATED 的半截记录"
    assert "已取消" in window.log_panel.to_text()


def test_existing_file_continue_resumes_and_skips(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """Spec §15/§16：选「继续下载」→ 复用同一个文件，已有瓦片被跳过。"""
    _stub_download_service(monkeypatch, _png_handler())
    _fill_small_region(window, work_dir)
    path = _seed_existing_map(window, tiles=3)
    total = _expected_tile_count(window)
    assert total > 3

    window.download_panel.action_buttons[ACTION_START].click()
    qapp.processEvents()
    _existing_dialog(window).button(QMessageBox.StandardButton.Yes).click()
    qapp.processEvents()

    worker = window._download_worker
    assert isinstance(worker, DownloadWorker)
    assert worker.is_resume is True  # 走了续传入口
    _wait_for_download(window, qapp)

    log = window.log_panel.to_text()
    assert "选择继续下载：复用已有地图文件" in log
    assert f"已有：3 / {total:,}" in log
    assert f"继续下载：{total - 3:,}" in log
    assert "续传模式" in log

    with MBTilesReader(path) as reader:
        assert reader.get_tile_count() == total
    assert window._status_values["tile_skipped"].text() == "3"
    assert _task_status(window, _panel_task_id(window)) == "completed"


def test_existing_file_restart_requires_second_confirm(
    window: MainWindow, qapp, error_dialogs, work_dir: Path
) -> None:
    """Spec §15：重新下载要二次确认，并明确告知备份路径。"""
    _fill_small_region(window, work_dir)
    path = _seed_existing_map(window, tiles=2)
    before = path.read_bytes()

    window.download_panel.action_buttons[ACTION_START].click()
    qapp.processEvents()
    _existing_dialog(window).button(QMessageBox.StandardButton.No).click()
    qapp.processEvents()

    dialog = window._restart_dialog
    assert dialog is not None
    assert "确认重新下载" in dialog.windowTitle()
    text = dialog.text()
    assert ".bak-" in text
    assert "重命名备份" in text
    assert path.read_bytes() == before  # 还没备份

    # 二次确认里选「取消」→ 一切照旧
    dialog.button(QMessageBox.StandardButton.Cancel).click()
    qapp.processEvents()

    assert path.read_bytes() == before
    assert list(work_dir.glob("*.bak-*")) == []
    assert window._download_worker is None
    assert window.task_panel.task_count == 0


def test_existing_file_restart_backs_up_then_downloads_from_scratch(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """确认重新下载 → 旧文件被**改名备份**（不是删除），然后从零重下。"""
    _stub_download_service(monkeypatch, _png_handler())
    _fill_small_region(window, work_dir)
    path = _seed_existing_map(window, tiles=2)
    before = path.read_bytes()
    total = _expected_tile_count(window)

    window.download_panel.action_buttons[ACTION_START].click()
    qapp.processEvents()
    _existing_dialog(window).button(QMessageBox.StandardButton.No).click()
    qapp.processEvents()
    window._restart_dialog.button(QMessageBox.StandardButton.Yes).click()
    qapp.processEvents()

    backups = list(work_dir.glob("beijing.mbtiles.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before  # 旧内容一字不差地留着
    assert "已把原文件备份为" in window.log_panel.to_text()

    _wait_for_download(window, qapp)
    with MBTilesReader(path) as reader:
        assert reader.get_tile_count() == total
    # 重下 → 一张都不跳过
    assert window._status_values["tile_skipped"].text() == "0"


def test_task_resume_without_token_is_refused(
    window: MainWindow, qapp, error_dialogs, work_dir: Path
) -> None:
    """Spec §9/§16：任务记录里没有 Token，恢复时必须让用户重新填。"""
    task = window.task_service.create_task(
        name="beijing",
        bounds=GeoBounds(116.0, 39.0, 116.1, 39.1),
        min_zoom=12,
        max_zoom=12,
        map_layer=MapLayerType.VECTOR,
        output_path=work_dir / "beijing.mbtiles",
        total_tiles=6,
    )
    window.task_service.set_status(task, DownloadTaskStatus.PAUSED)
    window._refresh_task_list(select_id=task.id)
    assert window.task_panel.task_count == 1

    window.download_panel._token_edit.setText("")  # 默认就是空的，这里写明意图
    window.task_panel.resume_button.click()
    qapp.processEvents()

    assert window._download_worker is None
    assert error_dialogs, "应当给出一次明确提示"
    assert "Token" in error_dialogs[-1][1]
    # 参数仍然回填给用户，方便他填完 Token 直接点「开始下载」
    assert window.download_panel.map_name_edit.text() == "beijing"


def test_task_resume_starts_resume_download(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """Spec §16：选中 PAUSED 任务点「继续」→ 只下缺失的那些。"""
    _stub_download_service(monkeypatch, _png_handler())
    _fill_small_region(window, work_dir)
    path = _seed_existing_map(window, tiles=3)
    total = _expected_tile_count(window)

    task = window.task_service.create_task(
        name="beijing",
        bounds=GeoBounds(116.0, 39.0, 116.1, 39.1),
        min_zoom=12,
        max_zoom=12,
        map_layer=MapLayerType.VECTOR,
        output_path=path,
        total_tiles=total,
    )
    window.task_service.set_status(task, DownloadTaskStatus.PAUSED)
    window._refresh_task_list(select_id=task.id)

    window.task_panel.resume_button.click()
    qapp.processEvents()

    worker = window._download_worker
    assert isinstance(worker, DownloadWorker)
    assert worker.is_resume is True
    _wait_for_download(window, qapp)

    log = window.log_panel.to_text()
    assert f"已有：3 / {total:,}" in log
    with MBTilesReader(path) as reader:
        assert reader.get_tile_count() == total
    assert _task_status(window, task.id) == "completed"


def test_completed_task_cannot_be_resumed(
    window: MainWindow, qapp, error_dialogs, work_dir: Path
) -> None:
    """已完成的任务不该还能"继续"（Spec §16 只针对 PAUSED / FAILED）。"""
    task = window.task_service.create_task(
        name="beijing",
        bounds=GeoBounds(116.0, 39.0, 116.1, 39.1),
        min_zoom=12,
        max_zoom=12,
        map_layer=MapLayerType.VECTOR,
        output_path=work_dir / "beijing.mbtiles",
        total_tiles=6,
    )
    window.task_service.complete_task(task)
    window._refresh_task_list(select_id=task.id)

    window.task_panel.resume_button.click()
    qapp.processEvents()

    assert window._download_worker is None
    assert "无需继续" in window.log_panel.to_text()


def test_task_delete_removes_record_but_keeps_mbtiles(
    window: MainWindow, qapp, error_dialogs, work_dir: Path
) -> None:
    """Spec §20：删除记录只删 JSON，不碰 .mbtiles。"""
    _fill_small_region(window, work_dir)
    path = _seed_existing_map(window, tiles=2)
    task = window.task_service.create_task(
        name="beijing",
        bounds=GeoBounds(116.0, 39.0, 116.1, 39.1),
        min_zoom=12,
        max_zoom=12,
        map_layer=MapLayerType.VECTOR,
        output_path=path,
        total_tiles=6,
    )
    window._refresh_task_list(select_id=task.id)
    assert window.task_panel.task_count == 1

    window.task_panel.delete_button.click()
    qapp.processEvents()

    assert window.task_panel.task_count == 0
    assert window.task_service.get_task(task.id) is None
    assert path.exists() is True  # 地图文件留着
    log = window.log_panel.to_text()
    assert "已删除任务记录" in log
    assert settings.UI_TEXT_TASK_DELETE_HINT in log


def test_task_double_click_backfills_form(
    window: MainWindow, qapp, error_dialogs, work_dir: Path
) -> None:
    """Spec §14：双击任务 → 参数回填到配置面板（Token 除外）。"""
    task = window.task_service.create_task(
        name="shanghai",
        bounds=GeoBounds(121.0, 31.0, 121.5, 31.5),
        min_zoom=10,
        max_zoom=12,
        map_layer=MapLayerType.IMAGE,
        output_path=work_dir / "shanghai.mbtiles",
        total_tiles=999,
    )
    window._refresh_task_list(select_id=task.id)

    assert window.task_panel.select_task(task.id) is True
    item = window.task_panel.table.item(0, 0)
    window.task_panel.table.itemDoubleClicked.emit(item)
    qapp.processEvents()

    panel = window.download_panel
    assert panel.map_name_edit.text() == "shanghai"
    assert panel._west_spin.value() == pytest.approx(121.0)
    assert panel._north_spin.value() == pytest.approx(31.5)
    assert panel._min_zoom_spin.value() == 10
    assert panel._max_zoom_spin.value() == 12
    assert panel._output_edit.text() == str(work_dir)
    assert panel.get_form_data()["map_type"] is MapLayerType.IMAGE
    assert panel._token_edit.text() == ""  # Token 不保存在任务里
    log = window.log_panel.to_text()
    assert "已把任务「shanghai」的参数回填到配置面板" in log
    assert "Token 需要重新填写" in log


def test_task_list_progress_tracks_download_state(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """下载进行中列表里是「下载中」，结束后变成「已完成」并带上张数（Spec §14）。"""
    _stub_download_service(
        monkeypatch, _png_handler(delay=0.05), rate_limiter=RateLimiter.disabled()
    )
    _fill_small_region(window, work_dir)
    total = _expected_tile_count(window)

    window.download_panel.action_buttons[ACTION_START].click()
    qapp.processEvents()

    assert isinstance(window._download_worker, DownloadWorker)
    assert window.task_panel.task_count == 1
    assert window.task_panel.row_text(0)[1] == "下载中"

    _wait_for_download(window, qapp)

    assert window.task_panel.row_text(0)[1] == "已完成"
    assert f"{total:,}" in window.task_panel.row_text(0)[2]
    assert _task_status(window, _panel_task_id(window)) == "completed"
    assert "任务状态：已完成" in window.log_panel.to_text()


def test_failed_task_keeps_failed_status_in_list(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """Spec §18：有瓦片下不到时任务保持 FAILED，并把原因留在记录里。"""
    _stub_download_service(monkeypatch, _png_handler(status_code=500))
    _fill_small_region(window, work_dir)

    window.download_panel.action_buttons[ACTION_START].click()
    _wait_for_download(window, qapp)

    assert window.task_panel.task_count == 1
    assert window.task_panel.row_text(0)[1] == "失败"
    fresh = window.task_service.get_task(_panel_task_id(window))
    assert fresh is not None
    assert fresh.status is DownloadTaskStatus.FAILED
    assert fresh.error
    log = window.log_panel.to_text()
    assert "任务未完成" in log
    assert "点「继续」补齐缺失瓦片" in log


def test_exit_confirmation_does_not_block_event_loop(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """退出确认必须是**非模态**的（``exec()`` 会在无头环境里把用例挂死）。"""
    source = inspect.getsource(
        main_window_module.MainWindow._ask_exit_with_running_task
    )
    assert ".exec()" not in source
    assert ".show()" in source

    _stub_download_service(
        monkeypatch, _png_handler(delay=0.2), rate_limiter=RateLimiter.disabled()
    )
    _fill_small_region(window, work_dir)
    assert _expected_tile_count(window) > 5
    window.show()
    window.download_panel.action_buttons[ACTION_START].click()
    qapp.processEvents()

    assert _wait_until(
        qapp, lambda: window._status_values["tile_succeeded"].text() not in ("0", "—")
    ), "下载在超时前没有写出任何瓦片"

    window.close()
    qapp.processEvents()
    dialog = window._exit_dialog
    assert dialog is not None
    assert dialog.isVisible() is True

    # 事件循环仍在流转
    window.log_panel.append_log("event-loop-alive")
    qapp.processEvents()
    assert "event-loop-alive" in window.log_panel.to_text()

    dialog.button(QMessageBox.StandardButton.Yes).click()
    qapp.processEvents()

    # Spec §17：退出后任务状态是「已暂停」，不是「已取消」
    assert window.task_panel.task_count == 1
    assert window.task_panel.row_text(0)[1] == "已暂停"
    assert "任务将暂停" in window.log_panel.to_text()
    assert window._download_worker is None
    fresh = window.task_service.get_task(_panel_task_id(window))
    assert fresh is not None
    assert fresh.status is DownloadTaskStatus.PAUSED


def test_exit_writes_paused_status_to_disk(
    window: MainWindow, tasks_file: Path, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """退出收尾必须把 PAUSED 落盘 —— 下次启动才看得到"可以继续"。"""
    _stub_download_service(
        monkeypatch, _png_handler(delay=0.2), rate_limiter=RateLimiter.disabled()
    )
    _fill_small_region(window, work_dir)
    window.show()
    window.download_panel.action_buttons[ACTION_START].click()
    qapp.processEvents()

    assert _wait_until(
        qapp, lambda: window._status_values["tile_succeeded"].text() not in ("0", "—")
    ), "下载在超时前没有写出任何瓦片"

    window.close()
    qapp.processEvents()
    window._exit_dialog.button(QMessageBox.StandardButton.Yes).click()
    qapp.processEvents()

    reloaded = TaskStore(tasks_file).list_all()
    assert len(reloaded) == 1
    assert reloaded[0].status is DownloadTaskStatus.PAUSED
    assert reloaded[0].completed_tiles > 0  # 已下完的部分记下来了
    # 已写入的瓦片保留在磁盘上
    assert (work_dir / "beijing.mbtiles").exists() is True
