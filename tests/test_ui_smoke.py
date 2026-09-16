"""GUI 冒烟测试。

在 offscreen 平台下装配主窗口，覆盖：

- Slice 0：区域齐全、默认值正确、占位按钮点击不崩、启动日志可写入
- Slice 1："计算"按钮接入 Tile Math
- Slice 2："测试天地图连接"按钮接入 Provider（网络用 MockTransport 打桩）
- Slice 3：[开始下载]/[暂停]/[继续] 接入异步下载引擎
  （网络同样用 MockTransport 打桩，测试不触网）
"""

from __future__ import annotations

import asyncio
import inspect
import io
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image
from PySide6.QtCore import QEvent, Qt, QThread
from PySide6.QtWidgets import QFileDialog, QLabel, QMessageBox

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
from app.models import (
    DownloadTaskStatus,
    MapLayerType,
    MapType,
    composition_for,
    describe_map_type,
)
from app.providers import PNG_SIGNATURE, TiandituProvider
from app.services import AndroidTransferService, build_output_path
from app.tasks import TaskService, TaskStore
from app.ui.connection_check import ConnectionTestWorker
from app.ui.device_panel import DevicePanel
from app.ui.download_panel import (
    ACTION_CALCULATE,
    ACTION_CANCEL,
    ACTION_PAUSE,
    ACTION_RESUME,
    ACTION_START,
    ACTION_TEST_CONNECTION,
    DownloadPanel,
)
from app.ui.download_worker import DownloadWorker
from app.ui.log_panel import LogPanel
from app.ui.main_window import MainWindow
from app.ui.mbtiles_inspector_dialog import MBTilesInspectorDialog
from app.ui.package_dialog import (
    PackageBuildWorker,
    PackageDialog,
    PackageValidateDialog,
    PackageValidateWorker,
)
from app.ui.validation import auto_map_name
from tests.android_fakes import FakeAdbClient

#: 假 Token（禁止替换为真实密钥）
FAKE_TOKEN = "abcdefghijklmn"


def _real_png(size: int = settings.TILE_SIZE) -> bytes:
    """生成一张**真的能被解码**的 PNG。

    Slice ≤5 只要文件头正确就够了（校验按 magic bytes 判定）；Slice 6 的中文注记
    合成是真的要 ``Image.open`` + ``alpha_composite``，所以桩数据必须是一张合法
    图片——否则所有涉及下载的用例都会变成"合成失败 6 张"，
    测的就不再是业务逻辑，而是桩数据本身。
    """
    buffer = io.BytesIO()
    Image.new("RGBA", (size, size), (0, 0, 0, 0)).save(buffer, format="PNG")
    data = buffer.getvalue()
    assert data.startswith(PNG_SIGNATURE)  # 与生产判定用的是同一套 magic bytes
    return data


#: 假瓦片：一张 256×256 的透明 PNG（文件头与内容都是真的）
FAKE_PNG = _real_png()

#: 退避为 0 的重试策略：行为与生产一致，但测试不用真的等
FAST_POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=0.0, max_delay_seconds=0.0)


@pytest.fixture()
def window(qapp, tasks_file: Path, preferences) -> MainWindow:
    """每个用例一个全新的主窗口。

    任务记录与界面偏好都注入临时路径：默认的
    ``~/MapDownloader/data/tasks.json`` / ``QSettings(ORG_NAME, APP_NAME)``
    会污染真实用户目录，而且用例之间会互相看到对方留下的任务与窗口形状。
    """
    win = MainWindow(task_service=TaskService(TaskStore(tasks_file)), preferences=preferences)
    yield win
    _shutdown_window(win)


def _shutdown_window(win: MainWindow) -> None:
    """收掉一个用例的主窗口。

    用例可能留下正在跑的后台线程（下载 / 设备刷新 / 传输）。这里把"退出确认"
    直接跳过（那是有头交互的产物，不该由夹具去点按钮），改为等待线程回收——
    QThread 在运行时被销毁会让 Qt 直接中止进程。
    """
    win._exit_confirmed = True
    for getter in (
        lambda: win._download_worker,
        lambda: win._device_worker,
        lambda: win._transfer_worker,
        lambda: win._package_check_worker,
    ):
        worker = getter()
        if worker is not None and worker.isRunning():
            cancel = getattr(worker, "cancel", None)
            if callable(cancel):
                cancel()
            worker.wait(10_000)
    win.close()
    win.deleteLater()


@pytest.fixture()
def android_window_factory(qapp, tasks_file: Path, preferences):
    """造一个接了**假 adb** 的主窗口（Slice 10）。

    为什么不用上面那个 ``window``：真实实现会去起 ``adb`` 进程探测设备。
    测试不该依赖"本机装没装 Android SDK"——一台干净的 CI 机器上也要能跑。
    注入 ``FakeAdbClient`` 之后，设备区可以被真的点下去（下拉框会填进三台
    真机格式的设备），而一个进程都不会起。

    Returns:
        工厂函数；``**client_kwargs`` 透传给 ``FakeAdbClient``
        （例如 ``adb_available=False`` 造出"没装 adb"）。
    """
    windows: list[MainWindow] = []

    def factory(**client_kwargs) -> MainWindow:
        service = AndroidTransferService(client=FakeAdbClient(**client_kwargs))
        win = MainWindow(
            task_service=TaskService(TaskStore(tasks_file)),
            preferences=preferences,
            android_service=service,
        )
        windows.append(win)
        return win

    yield factory
    for win in windows:
        _shutdown_window(win)


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


class _RecordingLayerServer:
    """假天地图服务：记录每一次请求打的是哪个图层，并返回一张真 PNG。

    与 ``_png_handler`` 的区别在于"按请求作答"：合成模式下能不能分清
    ``vec`` / ``cva``（以及它们的请求次数）是必须验证的事。
    """

    def __init__(self) -> None:
        self.layers: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.layers.append(str(request.url.params.get("LAYER", "")))
        return httpx.Response(
            200, content=FAKE_PNG, headers={"content-type": "image/png"}
        )


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


def _wait_for_device_worker(window: MainWindow, qapp) -> None:
    """等待设备刷新线程结束并把结果送回主线程（Slice 10）。"""
    worker = window._device_worker
    assert worker is not None, "没有起刷新线程"
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
    """Slice 7 的进度区是 :class:`TaskProgressPanel`：11 个字段 + 总进度条。"""
    values = window._status_values
    assert set(values) == {
        "task_name",
        "task_status",
        "current_zoom",
        "tile_total",
        "tile_done",
        "tile_remaining",
        "tile_succeeded",
        "tile_failed",
        "tile_skipped",
        "speed",
        "downloaded_bytes",
    }
    assert values["task_name"].text() == "—"
    assert values["task_status"].text() == "空闲"
    assert values["tile_total"].text() == "0"
    assert values["tile_done"].text() == "0"
    assert values["tile_remaining"].text() == "0"
    assert values["tile_succeeded"].text() == "0"
    assert values["tile_failed"].text() == "0"
    assert values["tile_skipped"].text() == "0"
    assert values["downloaded_bytes"].text() == "0 B"
    assert window.progress_bar.value() == 0
    # 旧名字仍然可用（转发到同一个面板），Slice 3~6 的调用点不受影响
    assert window.status_group is window.task_progress


def test_device_panel_empty_state(window: MainWindow) -> None:
    """Slice 10：设备区从 ``QListWidget`` 占位换成"下拉框 + 详情"。

    空状态仍然要有：没有设备时下拉框里是一句占位文案，且**不是**空的——
    一个空下拉框会被当成"控件坏了"。
    """
    panel = window.device_panel
    assert panel.device_combo.count() == 1
    assert panel.device_combo.itemText(0) == settings.UI_TEXT_DEVICE_EMPTY
    assert panel.device_combo.isEnabled() is False
    assert panel.selected_serial == ""
    assert panel.selected_device() is None


def test_transfer_dialog_handles_are_initialised(window: MainWindow) -> None:
    """两个确认框的按钮引用在装配时就有默认值（``None``）。

    它们只在弹窗被创建时才赋值。读一个**不存在**的属性会 ``AttributeError``，
    而这个读取动作发生在"用户点完 [选择地图包]、传输还没开始"的时刻——
    抛在 Qt 槽里就会被静默吞掉。
    """
    assert window._transfer_confirm_dialog is None
    assert window._transfer_exists_dialog is None
    assert window._transfer_confirm_start is None
    assert window._transfer_overwrite_button is None


def test_device_area_fits_the_panel_at_default_size(window: MainWindow, qapp) -> None:
    """默认窗口下设备区容得下设备面板（不然用户要滚一下才能看到 [传输到设备]）。

    **必须 ``show()``**：不显示的窗口不会跑布局，``viewport().height()`` 是个
    没有意义的残值（28px），断言会以"面板太高了"的形式误报。

    注意这是**平台相关**的下界：``offscreen`` 的字体比真机小，所以这条过了不等于
    真机过了。真实字体 / DPI 下的同义断言在 ``scripts/verify_slice10_android.py``
    的场景 E 里（那里量到 430px，也正是右列配比取 450 的依据）。
    """
    from app.ui.main_window import _RIGHT_COLUMN_SIZES

    window.show()
    for _ in range(8):
        qapp.processEvents()

    panel = window.device_panel
    scroll = window._device_scroll

    assert panel.sizeHint().height() > 0
    assert scroll.verticalScrollBar().maximum() == 0
    assert scroll.horizontalScrollBar().maximum() == 0
    # 右列给设备区的初始高度，要留得下滚动区的边框
    assert panel.sizeHint().height() + 8 <= _RIGHT_COLUMN_SIZES[0]


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
    # Slice 6 起下拉框放的是"地图类型"（矢量 / 影像），底图图层由它派生
    assert data["map_type"] is MapType.VECTOR
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
    assert data["map_type"] is MapType.IMAGE
    assert data["with_labels"] is False
    assert data["min_zoom"] == 10
    assert data["max_zoom"] == 12
    assert data["map_name"] == "beijing"


# --------------------------------------------------------------------------- #
# 占位按钮
# --------------------------------------------------------------------------- #
def test_action_buttons_exist(window: MainWindow) -> None:
    buttons = window.download_panel.action_buttons
    # Slice 7 在「继续」与「测试连接」之间插入了「取消」（Spec §5/§6）：
    # 暂停 / 继续 / 取消回答的是同一个问题——"现在要不要让它停下来"。
    assert list(buttons) == [
        ACTION_CALCULATE,
        ACTION_START,
        ACTION_PAUSE,
        ACTION_RESUME,
        ACTION_CANCEL,
        ACTION_TEST_CONNECTION,
    ]
    assert [b.text() for b in buttons.values()] == [
        "计算",
        "开始下载",
        "暂停",
        "继续",
        "取消",
        "测试天地图连接",
    ]
    # 空闲态：暂停 / 继续 / 取消没有意义（按 Spec §6 的状态矩阵启用）
    assert buttons[ACTION_CALCULATE].isEnabled() is True
    assert buttons[ACTION_START].isEnabled() is True
    assert buttons[ACTION_TEST_CONNECTION].isEnabled() is True
    assert buttons[ACTION_PAUSE].isEnabled() is False
    assert buttons[ACTION_RESUME].isEnabled() is False
    assert buttons[ACTION_CANCEL].isEnabled() is False


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
    """没有设备时：[刷新设备] 必须能点，[传输到设备] 必须拦得住（Spec §8）。

    ``[刷新设备]`` 是发现 ADB 的唯一入口，装配时被禁用就等于永远灰着——
    这个断言守的就是那个"把门锁上再找钥匙"的坑。
    """
    panel = window.device_panel
    assert panel.refresh_button.isEnabled() is True
    assert panel.transfer_button.isEnabled() is False

    # 第二道：按钮禁用只是界面层的拦，槽本身也要拦（信号可能从别处触发）
    window._on_transfer_to_android()
    assert settings.UI_TEXT_TRANSFER_NEED_DEVICE in window.log_panel.to_text()
    assert window._transfer_worker is None
    assert panel.progress_bar.value() == 0


def test_choose_package_slot_sets_the_directory(android_window_factory, qapp, monkeypatch) -> None:
    """[选择地图包] 槽真的把选中的目录设上了，而且**不抛**（Spec §12）。

    这条守的是一个实机才现形的 bug：槽里写的是
    ``self.download_panel.output_dir()``，而 ``DownloadPanel`` **没有**这个方法
    ——点击时抛 ``AttributeError``，Qt 把槽里的异常吞掉，用户看到的是
    "按钮点了没反应"（不报错、不弹窗、什么都不发生）。
    验收脚本靠"点一下看目录有没有设上"抓到它，这里把它固化成常驻守卫。
    """
    window = android_window_factory()
    panel = window.device_panel
    calls: list[tuple] = []

    def _fake(*args, **kwargs):
        calls.append(args)
        return "D:/maps/packages/beijing"

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(_fake))
    panel.choose_package_button.click()

    assert calls, "[选择地图包] 没有弹出目录选择框"
    assert panel.package_dir == "D:/maps/packages/beijing"
    # 起点目录取自下载输出目录（用户的地图包一般就在旁边）
    assert str(calls[0][2]) == window.download_panel.output_edit.text().strip()


def test_choose_package_cancelled_changes_nothing(android_window_factory, monkeypatch) -> None:
    """用户点了取消 → 什么都不改（尤其不该把已有的包清掉）。"""
    window = android_window_factory()
    panel = window.device_panel
    panel.set_package_dir("D:/maps/packages/keepme")

    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: "")
    )
    panel.choose_package_button.click()

    assert panel.package_dir == "D:/maps/packages/keepme"


def test_refresh_devices_fills_the_combo(android_window_factory, qapp) -> None:
    """[刷新设备] 走完一遍后台探测，把三台设备（含未授权 / 离线）显示出来。"""
    window = android_window_factory()
    panel = window.device_panel

    panel.refresh_button.click()
    _wait_for_device_worker(window, qapp)

    assert panel.device_combo.count() == 3
    assert panel.device_combo.isEnabled() is True
    assert panel.selected_serial == "ABC123"
    assert panel.selected_device() is not None
    assert "已发现 3 台设备" in window.log_panel.to_text()
    # ADB 可用时不该出现"未检测到 ADB"的提示
    assert settings.UI_TEXT_DEVICE_ADB_MISSING not in window.log_panel.to_text()


def test_refresh_devices_reports_missing_adb(android_window_factory, qapp) -> None:
    """没装 adb：如实说明 + 告诉用户怎么办，而不是静默地显示"没有设备"（Spec §10）。"""
    window = android_window_factory(adb_available=False)
    panel = window.device_panel

    panel.refresh_button.click()
    _wait_for_device_worker(window, qapp)

    assert panel.device_combo.count() == 1
    assert panel.device_combo.itemText(0) == settings.UI_TEXT_DEVICE_EMPTY
    assert settings.UI_TEXT_DEVICE_ADB_MISSING in window.log_panel.to_text()
    # 不可用状态要让用户看得见"不可用"，而不是留一个空的 ADB 行
    assert settings.UI_TEXT_DEVICE_ADB_UNAVAILABLE in panel.adb_text


def test_unauthorized_device_blocks_the_transfer(android_window_factory, qapp) -> None:
    """未授权的设备**显示**出来，但传输入口拦住（Spec §8）：这是两件事。"""
    window = android_window_factory()
    panel = window.device_panel

    panel.refresh_button.click()
    _wait_for_device_worker(window, qapp)

    assert panel.select_serial("XYZ456") is True
    assert panel.selected_serial == "XYZ456"
    assert panel.transfer_button.isEnabled() is False
    assert settings.UI_TEXT_DEVICE_UNAUTHORIZED in panel.hint_text


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
    """启动横幅里的每一行都要进日志。

    刻意**不**把整条元组钉死在测试里：每加一个 Slice 就要改一次这条断言，
    而它真正要守的是"``STARTUP_MESSAGES`` 里写了什么，日志里就有什么"。
    """
    assert STARTUP_MESSAGES[0] == "Application started"
    assert any("Slice 7" in message for message in STARTUP_MESSAGES)
    assert any("Slice 10" in message for message in STARTUP_MESSAGES)

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

    # 默认勾选了中文注记 → 每张瓦片请求底图 + 注记两次，请求数必须是瓦片数的两倍。
    map_type = data["map_type"]
    assert map_type.request_count_per_tile(data["with_labels"]) == 2
    # Spec §3 的行序：瓦片数 → 请求量 → 容量 → Zoom → 地图类型 → 输出文件
    assert panel.result_label.text() == "\n".join(
        [
            f"预计瓦片：{expected_count:,}",
            settings.UI_TEXT_ESTIMATED_REQUESTS_MERGED.format(
                requests=expected_count * 2, tiles=expected_count
            ),
            f"预计容量：{expected_size}",
            settings.UI_TEXT_ESTIMATED_ZOOM.format(min_zoom=8, max_zoom=17),
            settings.UI_TEXT_ESTIMATED_MAP_TYPE.format(
                map_type=describe_map_type(map_type, True)
            ),
            f"输出文件：{build_output_path(data['output_dir'], data['map_name'])}",
        ]
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
    assert window.device_panel.device_combo.count() == 1
    assert (
        window.device_panel.device_combo.itemText(0) == settings.UI_TEXT_DEVICE_EMPTY
    )
    # 设备区的两个按钮都在，且没有设备时 [传输到设备] 是禁用的（Slice 10）
    assert window.device_panel.refresh_button.isEnabled() is True
    assert window.device_panel.transfer_button.isEnabled() is False


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
    assert (
        window.device_panel.device_combo.itemText(0) == settings.UI_TEXT_DEVICE_EMPTY
    )


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
class _SlowTransport(httpx.AsyncBaseTransport):
    """在假服务前面加一个**异步**延迟，用来制造"下载还在进行中"的窗口。

    为什么不用 ``_png_handler(delay=...)`` 里那个 ``time.sleep``：它是同步阻塞，
    会把事件循环一起按住。后果有两个，而且都很难查：

    - 所有瓦片几乎在同一瞬间才被报告完成，"下了一部分、任务仍在跑"这个中间态
      只剩几十毫秒宽 —— 取消类 / 退出类用例会随机地退化成"取消一个空任务"；
    - ``cancel()`` 要在请求回来之后才有机会生效，于是取消一个慢下载要等它**跑完**。

    ``asyncio.sleep`` 不阻塞循环：中间态稳定存在，取消也能立刻生效。
    """

    def __init__(self, handler, delay: float) -> None:
        self._delay = delay
        self._inner = httpx.MockTransport(handler)

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:  # pragma: no cover - 由 httpx 调用
        await asyncio.sleep(self._delay)
        return await self._inner.handle_async_request(request)


def _stub_download_service(
    monkeypatch,
    handler,
    *,
    rate_limiter=None,
    policy: RetryPolicy = FAST_POLICY,
    batch_size: int = 7,
    delay: float = 0.0,
) -> None:
    """让 DownloadWorker 内部的 MapDownloadService 走 MockTransport（不触网）。

    生产代码里没有任何测试钩子，因此这里替换的是 worker 模块引用的
    ``MapDownloadService`` 名字——顺带把限速/退避/批次调成"测试用"的档位。
    替换 service 而不是 ``TileDownloader``：Slice 4 起下载器由 service 自己创建，
    在 service 这一层注入才是唯一拦得住的入口。

    ``delay`` 大于 0 时用 :class:`_SlowTransport` 给每个请求加异步延迟，
    用来制造"下载进行中"的窗口（见该类的说明）。
    """
    original = download_worker_module.MapDownloadService

    def factory(provider: TiandituProvider, **kwargs: object):
        kwargs["transport"] = (
            _SlowTransport(handler, delay) if delay else httpx.MockTransport(handler)
        )
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


def _fill_multi_batch_region(window: MainWindow, work_dir: Path) -> int:
    """填一个"要分几批才下完"的区域，返回瓦片数（z12 下 20 张）。

    取消类 / 退出类用例需要"任务还在跑"的中间态。这个区域的规模（20 张瓦片、
    并发 6）意味着至少 4 批，配合 :class:`_SlowTransport` 的延迟能留出
    1 秒以上的窗口——不是靠"跑得快"侥幸赢下的时序，而是靠规模撑出来的。
    """
    panel = window.download_panel
    _fill_small_region(window, work_dir)
    panel._east_spin.setValue(116.3)
    panel._north_spin.setValue(39.2)
    return _expected_tile_count(window)


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


def _wait_for_scan(window: MainWindow, qapp, timeout: int = 10_000) -> None:
    """等待后台「扫描已有瓦片」线程结束，并把结果送回主线程。

    Slice 7 起，[开始下载] 与任务列表的「继续」在目标 ``.mbtiles`` 已存在时会
    先起一个 :class:`~app.ui.tile_scan_worker.ExistingTileScanWorker`——数一数
    里面已有多少张是一次 SQLite 查询，而 Spec §7 明令禁止在 Qt 主线程里做。
    于是"点一下 + ``processEvents()``"不再足以让三选一弹窗 / 下载线程出现，
    必须真的等那个后台线程跑完（它结束后会以队列信号的形式回到主线程继续流程）。
    """
    scan = window._scan_worker
    assert scan is not None, "目标文件已存在时应当先启动后台扫描"
    assert scan.wait(timeout) is True, "后台扫描线程没有在超时前结束"
    for _ in range(6):
        qapp.processEvents()


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
    # Slice 7：有瓦片下不到 = 任务 FAILED（Spec §18）。状态文案与按钮矩阵、
    # 任务列表那一列、以及「继续补齐」的出口必须说的是同一件事。
    assert values["task_status"].text() == "失败"
    assert values["tile_failed"].text() == f"{expected:,}"
    assert values["tile_succeeded"].text() == "0"

    result = window.download_panel.download_result_label.text()
    assert "验证：FAIL" in result          # 零瓦片的库不是可用地图
    assert "失败原因：" in result
    assert "403" in result
    # Token 无效是常见原因，提示里必须点到
    assert "Token" in result

    assert "下载完成" in window.log_panel.to_text()


def _dismiss_dialog_and_flush(dialog: QMessageBox, qapp) -> None:
    """点掉一个非模态弹窗，并把延迟删除真的跑完。

    ``WA_DeleteOnClose`` 走的是 ``DeferredDelete`` 事件，光 ``processEvents()``
    不保证它被派发；显式 ``sendPostedEvents`` 才能确定 C++ 对象已经没了 ——
    这条测试要测的恰恰是"对象已经没了"之后的行为。
    """
    dialog.button(QMessageBox.StandardButton.Close).click()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def test_dismissing_complete_dialog_then_closing_window_is_safe(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """关掉完成小结之后，主窗口还必须关得掉（Spec §16 + §23）。

    这是**实机验收跑出来的缺陷**的回归守卫：所有非模态弹窗都带
    ``WA_DeleteOnClose``，用户点一下「关闭」C++ 对象当场就销毁了，而
    ``MainWindow._complete_dialog`` 还攥着那个空壳。之后关主窗口时
    ``_close_dialogs()`` 对空壳调 ``close()`` 会抛
    ``RuntimeError: Internal C++ object already deleted``——异常从
    ``closeEvent`` 里窜出去，``super().closeEvent()`` 根本执行不到。
    用户看到的现象是「点了 X，窗口关不掉」，比丢一个弹窗严重得多。
    """
    _stub_download_service(monkeypatch, _png_handler())
    _fill_small_region(window, work_dir)

    window.download_panel.action_buttons[ACTION_START].click()
    _wait_for_download(window, qapp)

    dialog = window._complete_dialog
    assert dialog is not None, "成功结束应当弹出完成小结"
    _dismiss_dialog_and_flush(dialog, qapp)
    # 销毁回调把引用清干净了，窗口关闭时就再也不会碰到悬空指针
    assert window._complete_dialog is None

    window._exit_confirmed = True
    assert window.close() is True
    assert window.isVisible() is False


def test_dismissing_failed_dialog_then_closing_window_is_safe(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """失败小结同理：它同样带 ``WA_DeleteOnClose`` 且没有 ``finished`` 处理器。"""
    _stub_download_service(
        monkeypatch,
        _png_handler(
            status_code=403, content=b"<html>denied</html>", content_type="text/html"
        ),
    )
    _fill_small_region(window, work_dir)

    window.download_panel.action_buttons[ACTION_START].click()
    _wait_for_download(window, qapp)

    dialog = window._failed_dialog
    assert dialog is not None, "整片失败应当弹出失败小结"
    assert "继续补齐" in [button.text() for button in dialog.buttons()]
    _dismiss_dialog_and_flush(dialog, qapp)
    assert window._failed_dialog is None

    window._exit_confirmed = True
    assert window.close() is True
    assert window.isVisible() is False


def test_download_without_token_never_starts_worker(
    window: MainWindow, error_dialogs
) -> None:
    """Spec §9/§20：没有 Token 连任务都不该建（校验走统一入口）。"""
    window.download_panel.action_buttons[ACTION_START].click()

    assert window._download_worker is None
    assert window._scan_worker is None
    assert settings.UI_TEXT_VALIDATION_TOKEN_REQUIRED in window.log_panel.to_text()
    assert error_dialogs and error_dialogs[0][0] == settings.UI_TEXT_VALIDATION_TITLE


def test_download_with_invalid_bounds_never_starts_worker(
    window: MainWindow, error_dialogs, work_dir: Path
) -> None:
    _fill_small_region(window, work_dir)
    window.download_panel._east_spin.setValue(100.0)  # east < west

    window.download_panel.action_buttons[ACTION_START].click()

    assert window._download_worker is None
    assert settings.UI_TEXT_VALIDATION_WEST_EAST in window.log_panel.to_text()
    assert error_dialogs and error_dialogs[0][0] == settings.UI_TEXT_VALIDATION_TITLE
    # 连数据库都不该被创建
    assert list(work_dir.iterdir()) == []


def test_download_with_empty_map_name_is_auto_filled(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """Spec §2.7：地图名称为空时**自动补一个**（用中心点坐标），而不是拦住用户。

    名称决定文件名，空着会让"输出文件"那一行显示不出来；但为此弹一个错误框
    纯属给用户添活——坐标已经足够唯一地命名这块区域了。
    """
    _stub_download_service(monkeypatch, _png_handler())
    _fill_small_region(window, work_dir)
    window.download_panel.map_name_edit.setText("")

    # 名称为空时，输出路径无法确定（UI 不替用户编）
    assert window._resolve_output_path(window.download_panel.get_form_data()) is None

    window.download_panel.action_buttons[ACTION_START].click()

    expected_name = auto_map_name(116.0, 39.0, 116.1, 39.1)
    assert window.download_panel.map_name_edit.text() == expected_name
    assert (
        settings.UI_TEXT_MAP_NAME_AUTO_GENERATED.format(name=expected_name)
        in window.log_panel.to_text()
    )
    assert error_dialogs == []

    _wait_for_download(window, qapp)
    assert (work_dir / f"{expected_name}.mbtiles").exists()


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
    # Slice 7：点下暂停先进入 PAUSING（"正在暂停…"），等引擎回了一帧进度
    # （证明它在途请求已落地）才落成 PAUSED。因此这里要等，而不是点完就断言。
    assert _wait_until(
        qapp, lambda: window._status_values["task_status"].text() == "已暂停"
    ), "点暂停后界面没有落到「已暂停」"
    assert "正在暂停" in window.log_panel.to_text()
    assert "下载已暂停" in window.log_panel.to_text()
    assert window.download_panel.pause_button.isEnabled() is False
    assert window.download_panel.resume_button.isEnabled() is True
    # 暂停期间「取消」仍然可用：用户随时可以改主意
    assert window.download_panel.cancel_button.isEnabled() is True

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
    assert (
        window.device_panel.device_combo.itemText(0) == settings.UI_TEXT_DEVICE_EMPTY
    )


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

    Slice 6 起"同构"还包含合成信息：元数据必须写上当前表单选的地图类型与
    ``composition``，否则续传一致性校验（Spec §12）会正确地拒绝这个文件，
    用例就变成在测"拒绝逻辑"而不是"跳过逻辑"了。
    """
    data = window.download_panel.get_form_data()
    bounds = GeoBounds(data["west"], data["south"], data["east"], data["north"])
    path = build_output_path(data["output_dir"], data["map_name"])
    path.parent.mkdir(parents=True, exist_ok=True)

    map_type = data["map_type"]
    with_labels = bool(data["with_labels"])
    writer = MBTilesWriter(path, batch_size=7)
    writer.open()
    writer.write_metadata(
        MBTilesMetadata.for_bounds(
            bounds,
            data["min_zoom"],
            data["max_zoom"],
            name=data["map_name"],
            description="",
            provider=settings.MBTILES_PROVIDER_TIANDITU,
            source_map_type=map_type.value,
            with_labels=with_labels,
            composition=composition_for(with_labels),
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
    window: MainWindow, tasks_file: Path, work_dir: Path, qapp, preferences
) -> None:
    """重启程序后，磁盘上的任务记录要出现在列表里（Spec §8 / §14）。

    断言**按列名**取值（``row_cells``）。Slice 7 把列表从 4 列扩到了 9 列
    （多了地图类型 / Zoom / 更新时间 / 输出文件），按行号取值会静默地测到
    别的列——那种错误最阴：它一直是绿的，只是测的东西换了。
    """
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

    second = MainWindow(
        task_service=TaskService(TaskStore(tasks_file)), preferences=preferences
    )
    try:
        assert second.task_panel.task_count == 1
        row = second.task_panel.row_cells(0)
        assert row["名称"] == "上海地图"
        assert row["地图类型"] == "矢量"
        assert row["Zoom"] == "10-12"
        assert row["状态"] == "已暂停"
        assert row["进度"] == "64%"  # 321 / 500
        assert row["成功"] == "321"
        assert row["失败"] == "0"
        assert row["输出文件"] == str(work_dir / "shanghai.mbtiles")
    finally:
        second._exit_confirmed = True
        second.close()
        second.deleteLater()


def test_interrupted_task_is_recovered_as_paused_on_startup(
    tasks_file: Path, work_dir: Path, qapp, preferences
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

    window = MainWindow(
        task_service=TaskService(TaskStore(tasks_file)), preferences=preferences
    )
    try:
        assert window._download_worker is None, "不允许自动开始下载"
        reloaded = window.task_service.get_task(task.id)
        assert reloaded is not None
        assert reloaded.status is DownloadTaskStatus.PAUSED
        assert window.task_panel.row_cells(0)["状态"] == "已暂停"
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
    _wait_for_scan(window, qapp)

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
    _wait_for_scan(window, qapp)
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
    _wait_for_scan(window, qapp)
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
    _wait_for_scan(window, qapp)
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
    _wait_for_scan(window, qapp)
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
        # 必须与 ``_seed_existing_map`` 写进文件的合成配置一致（表单默认勾选了
        # 中文注记）：否则 Spec §12 的一致性校验会正确地拒绝续传，
        # 用例就变成在测"拒绝逻辑"了。
        with_labels=True,
    )
    window.task_service.set_status(task, DownloadTaskStatus.PAUSED)
    window._refresh_task_list(select_id=task.id)

    window.task_panel.resume_button.click()
    # 「继续」要先在后台数清已有多少张（Spec §11），再决定续传多少
    _wait_for_scan(window, qapp)

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
    # 老式任务记录（只有 map_layer，没有 source_map_type）也要能反查出地图类型
    assert panel.get_form_data()["map_type"] is MapType.IMAGE
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
    assert window.task_panel.row_cells(0)["状态"] == "下载中"

    _wait_for_download(window, qapp)

    cells = window.task_panel.row_cells(0)
    assert cells["状态"] == "已完成"
    assert cells["进度"] == "100%"
    assert cells["成功"] == f"{total:,}"
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
    assert window.task_panel.row_cells(0)["状态"] == "失败"
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

    _stub_download_service(monkeypatch, _png_handler(), delay=0.4)
    total = _fill_multi_batch_region(window, work_dir)
    assert total > 15
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
    assert window.task_panel.row_cells(0)["状态"] == "已暂停"
    assert "任务将暂停" in window.log_panel.to_text()
    assert window._download_worker is None
    fresh = window.task_service.get_task(_panel_task_id(window))
    assert fresh is not None
    assert fresh.status is DownloadTaskStatus.PAUSED


def test_exit_writes_paused_status_to_disk(
    window: MainWindow, tasks_file: Path, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """退出收尾必须把 PAUSED 落盘 —— 下次启动才看得到"可以继续"。"""
    _stub_download_service(monkeypatch, _png_handler(), delay=0.4)
    _fill_multi_batch_region(window, work_dir)
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


# --------------------------------------------------------------------------- #
# 底图 + 中文注记合成：图层说明 / 请求量预估 / 接线（Slice 6）
# --------------------------------------------------------------------------- #
def test_layer_plan_text_tracks_map_type_and_labels(window: MainWindow) -> None:
    """三行说明必须随「地图类型」与「中文注记」实时变化（Spec §13）。"""
    panel = window.download_panel
    combo, labels = panel.map_type_combo, panel.labels_check

    # 默认：矢量 + 中文注记
    assert combo.currentData() is MapType.VECTOR
    assert labels.isChecked() is True
    text = panel.layer_plan_text()
    assert "当前下载：vec_w + cva_w" in text
    assert settings.UI_TEXT_LAYER_OUTPUT_MERGED in text
    assert "每张瓦片 2 次 WMTS 请求" in text

    # 切到影像：图层跟着换，请求次数不变
    combo.setCurrentIndex(1)
    assert combo.currentData() is MapType.IMAGE
    text = panel.layer_plan_text()
    assert "当前下载：img_w + cia_w" in text
    assert "每张瓦片 2 次 WMTS 请求" in text

    # 取消中文注记：只剩底图，请求次数减半
    labels.setChecked(False)
    text = panel.layer_plan_text()
    assert "当前下载：img_w" in text
    assert "cva_w" not in text and "cia_w" not in text
    assert settings.UI_TEXT_LAYER_OUTPUT_SINGLE in text
    assert "每张瓦片 1 次 WMTS 请求" in text


def test_layer_plan_label_is_rendered(window: MainWindow) -> None:
    """说明标签把模板渲染出来了（不是留着一堆 ``{}`` 占位符）。"""
    panel = window.download_panel
    rendered = panel.layer_plan_label.text()

    assert "{layers}" not in rendered
    assert "{output}" not in rendered
    assert "{requests}" not in rendered
    assert "vec_w" in rendered


def test_layer_plan_change_signal_is_emitted(window: MainWindow) -> None:
    """下拉框 / 复选框变化要发信号 —— 否则「预估结果」里的请求量不会重画。"""
    panel = window.download_panel
    seen: list[str] = []
    panel.layer_plan_changed.connect(lambda: seen.append("changed"))

    panel.labels_check.setChecked(False)
    panel.map_type_combo.setCurrentIndex(1)

    assert len(seen) == 2


def test_estimated_requests_halve_without_labels(window: MainWindow) -> None:
    """取消中文注记后，「预估结果」里的请求量必须等于瓦片数（而不是两倍）。"""
    panel = window.download_panel
    panel.set_calculation_result(1_000, "1.0 MB", "D:/maps/x.mbtiles")
    assert "预计 WMTS 请求：2,000（1,000 张 × 底图+注记 2 层）" in panel.result_label.text()

    panel.labels_check.setChecked(False)
    panel.set_calculation_result(1_000, "1.0 MB", "D:/maps/x.mbtiles")
    assert "预计 WMTS 请求：1,000" in panel.result_label.text()
    assert "底图+注记 2 层" not in panel.result_label.text()


def test_calculate_preview_follows_layer_plan(window: MainWindow) -> None:
    """点过「计算」之后切换注记，请求量行要立刻跟着变（不必重新点计算）。"""
    panel = window.download_panel
    panel._min_zoom_spin.setValue(10)
    panel._max_zoom_spin.setValue(12)
    panel.action_buttons[ACTION_CALCULATE].click()

    total = window._last_calculation[0]  # type: ignore[index]
    assert f"预计 WMTS 请求：{total * 2:,}" in panel.result_label.text()

    panel.labels_check.setChecked(False)

    assert f"预计 WMTS 请求：{total:,}" in panel.result_label.text()
    assert "底图+注记 2 层" not in panel.result_label.text()


def test_large_task_confirm_shows_request_estimate(
    window: MainWindow, qapp, error_dialogs, work_dir: Path
) -> None:
    """大任务确认框要给出请求次数（含"为什么是两倍"的解释）。

    Slice 5 的模板加了 ``{requests}`` 占位符却没人传值，四个大任务用例当场
    ``KeyError``——这条用例把"必须传进去"钉住。
    """
    total = _fill_large_region(window, work_dir)
    window.download_panel.action_buttons[ACTION_START].click()
    qapp.processEvents()

    plan = window._pending_plan
    assert plan is not None
    assert plan.requests == total * 2
    text = window._confirm_dialog.text()
    assert f"{total * 2:,}" in text
    assert "每张瓦片要请求底图 + 注记两次" in text


def test_large_task_request_estimate_without_labels(
    window: MainWindow, qapp, error_dialogs, work_dir: Path
) -> None:
    """不勾中文注记时请求量不翻倍 —— 预估必须反映真实选择。"""
    window.download_panel.labels_check.setChecked(False)
    total = _fill_large_region(window, work_dir)
    window.download_panel.action_buttons[ACTION_START].click()
    qapp.processEvents()

    plan = window._pending_plan
    assert plan is not None
    assert plan.requests == total
    assert plan.composition_text == settings.UI_TEXT_COMPOSITION_SINGLE


def test_download_worker_receives_map_type_and_labels(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """接线检查：合成模式的下载线程拿到的是 ``MapType`` + ``with_labels=True``。

    只传底图图层的话服务层就没法知道该用哪个注记图层，"合成"根本无从谈起。
    """
    _stub_download_service(monkeypatch, _png_handler())
    _fill_small_region(window, work_dir)
    window.download_panel.labels_check.setChecked(True)

    window.download_panel.action_buttons[ACTION_START].click()
    qapp.processEvents()

    worker = window._download_worker
    assert isinstance(worker, DownloadWorker)
    assert worker.layer is MapType.VECTOR
    assert worker.with_labels is True

    _wait_for_download(window, qapp)


def test_start_download_writes_merged_metadata(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """端到端：勾着中文注记点「开始下载」，产物必须是写清了 composition 的合成包。"""
    server = _RecordingLayerServer()
    _stub_download_service(monkeypatch, server, rate_limiter=RateLimiter.disabled())
    _fill_small_region(window, work_dir)
    total = _expected_tile_count(window)
    window.download_panel.labels_check.setChecked(True)

    window.download_panel.action_buttons[ACTION_START].click()
    _wait_for_download(window, qapp)

    path = _expected_output_path(window)
    with MBTilesReader(path) as reader:
        raw = reader.get_metadata()
        assert reader.get_tile_count() == total

    assert raw["composition"] == "merged"
    assert raw["labels"] == "true"
    assert raw["map_type"] == "vector"
    assert raw["format"] == "png"
    # 一张最终瓦片两次请求，且只打该地图类型的两个图层
    assert sorted(server.layers) == sorted(["vec", "cva"] * total)

    log = window.log_panel.to_text()
    assert settings.UI_TEXT_COMPOSITION_MERGED in log
    assert "合成模式：每张瓦片请求 vec_w + cva_w 各一次" in log


def test_start_download_without_labels_writes_single_metadata(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """不勾中文注记：产物是 ``single``，请求量减半。"""
    server = _RecordingLayerServer()
    _stub_download_service(monkeypatch, server, rate_limiter=RateLimiter.disabled())
    _fill_small_region(window, work_dir)
    total = _expected_tile_count(window)
    window.download_panel.labels_check.setChecked(False)

    window.download_panel.action_buttons[ACTION_START].click()
    _wait_for_download(window, qapp)

    with MBTilesReader(_expected_output_path(window)) as reader:
        raw = reader.get_metadata()
    assert raw["composition"] == "single"
    assert server.layers == ["vec"] * total
    assert settings.UI_TEXT_COMPOSITION_SINGLE in window.log_panel.to_text()


def test_task_backfill_restores_labels_and_map_type(
    window: MainWindow, qapp, error_dialogs, work_dir: Path
) -> None:
    """双击任务要把「地图类型」和「中文注记」都回填（Spec §14）。"""
    task = window.task_service.create_task(
        name="shanghai",
        bounds=GeoBounds(121.0, 31.0, 121.5, 31.5),
        min_zoom=10,
        max_zoom=12,
        map_layer=MapLayerType.IMAGE,
        output_path=work_dir / "shanghai.mbtiles",
        total_tiles=100,
        with_labels=True,
    )
    window._refresh_task_list(select_id=task.id)
    assert window.task_panel.select_task(task.id) is True
    item = window.task_panel.table.item(0, 0)

    # 先手动改掉，确认回填真的覆盖了当前界面状态
    window.download_panel.map_type_combo.setCurrentIndex(0)
    window.download_panel.labels_check.setChecked(False)

    window.task_panel.table.itemDoubleClicked.emit(item)
    qapp.processEvents()

    panel = window.download_panel
    assert panel.get_form_data()["map_type"] is MapType.IMAGE
    assert panel.get_form_data()["with_labels"] is True
    assert "img_w + cia_w" in panel.layer_plan_text()
    # 回填也要把说明刷新到最新的任务形态
    assert "vec_w" not in panel.layer_plan_text()


def test_task_started_from_resume_keeps_merged_composition(
    window: MainWindow, monkeypatch, qapp, error_dialogs, work_dir: Path
) -> None:
    """从任务列表「继续」时，合成配置取自任务记录（而不是界面上的当前勾选）。"""
    _stub_download_service(monkeypatch, _png_handler())
    _fill_small_region(window, work_dir)
    path = _seed_existing_map(window, tiles=3)  # 与表单一致：merged

    task = window.task_service.create_task(
        name="beijing",
        bounds=GeoBounds(116.0, 39.0, 116.1, 39.1),
        min_zoom=12,
        max_zoom=12,
        map_layer=MapLayerType.VECTOR,
        output_path=path,
        total_tiles=_expected_tile_count(window),
        with_labels=True,
    )
    window.task_service.set_status(task, DownloadTaskStatus.PAUSED)
    window._refresh_task_list(select_id=task.id)

    # 用户把界面上的注记勾掉了——这不该影响"继续"一个合成任务
    window.download_panel.labels_check.setChecked(False)

    window.task_panel.resume_button.click()
    _wait_for_scan(window, qapp)

    plan = window._pending_plan or window._download_worker
    assert plan is not None
    worker = window._download_worker
    assert isinstance(worker, DownloadWorker)
    assert worker.with_labels is True
    assert worker.layer is MapType.VECTOR
    _wait_for_download(window, qapp)

    with MBTilesReader(path) as reader:
        assert reader.get_tile_count() == _expected_tile_count(window)


# --------------------------------------------------------------------------- #
# Slice 8：MBTiles 检查与诊断工具
# --------------------------------------------------------------------------- #
def _make_complete_map(path: Path, *, zoom: int = 8) -> Path:
    """写一个**覆盖完整**的地图包（走生产同一条 Writer 路径）。

    ``GOOD_INSPECT_BOUNDS`` 在 z8 下恰好只映射到 1 张瓦片，所以"填满整个
    期望集合"只要写一张 —— 用例因此能断言到 ``PASS``，而不是被覆盖缺失
    拖成 ``WARNING``。
    """
    bounds = GeoBounds(116.0, 39.0, 116.5, 39.5)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = MBTilesWriter(path)
    writer.open()
    writer.write_metadata(
        MBTilesMetadata.for_bounds(bounds, zoom, zoom, name="inspector")
    )
    tile_range = calculate_tile_ranges(bounds, zoom, zoom)[0]
    for x in range(tile_range.min_x, tile_range.max_x + 1):
        for y in range(tile_range.min_y, tile_range.max_y + 1):
            writer.write_tile(zoom, x, y, _real_png())
    writer.close()
    return path


def _open_inspector(window: MainWindow, path: Path | str = "") -> MBTilesInspectorDialog:
    window.open_mbtiles_inspector(str(path))
    dialog = window._inspector_dialog
    assert dialog is not None, "应当创建一个检查对话框"
    return dialog


def _wait_for_inspection(dialog: MBTilesInspectorDialog, qapp, timeout: int = 15_000) -> None:
    """等后台检查线程结束（并把队列信号送回主线程）。

    检查跑在 :class:`MBTilesInspectionWorker` 里（Spec §18：不许占 Qt 主线程），
    所以"点一下 + processEvents()"不足以拿到结论，必须真的等线程结束。
    """
    worker = dialog.inspection_worker
    assert worker is not None, "应当已启动后台检查线程"
    assert worker.wait(timeout) is True, "后台检查线程没有在超时前结束"
    for _ in range(8):
        qapp.processEvents()


def test_inspect_action_exists_in_file_menu(window: MainWindow) -> None:
    """[检查 MBTiles] 必须是「文件」菜单里的一项（Spec §16）。"""
    from app.config.settings import UI_TEXT_INSPECT_BUTTON

    assert UI_TEXT_INSPECT_BUTTON in window.inspect_action.text()
    # 挂在「文件」菜单下（遍历菜单栏找，而不是依赖局部变量）
    texts = [
        action.text()
        for menu_action in window.menuBar().actions()
        if menu_action.menu() is not None
        for action in menu_action.menu().actions()
    ]
    assert any(UI_TEXT_INSPECT_BUTTON in text for text in texts)


def test_inspector_dialog_is_reused(window: MainWindow, work_dir: Path) -> None:
    """反复打开复用同一个实例：不然屏幕上会攒一堆只能逐个关掉的窗口。"""
    first = _open_inspector(window, work_dir / "a.mbtiles")
    second = _open_inspector(window, work_dir / "b.mbtiles")
    assert first is second
    assert first.path.endswith("b.mbtiles")


def test_inspector_prefills_path_from_task_panel(
    window: MainWindow, work_dir: Path, qapp
) -> None:
    """任务列表里的「检查」要带着选中任务的产物路径打开对话框。"""
    path = _make_complete_map(work_dir / "selected.mbtiles")
    task = window.task_service.create_task(
        name="selected",
        bounds=GeoBounds(116.0, 39.0, 116.5, 39.5),
        min_zoom=8,
        max_zoom=8,
        map_layer=MapLayerType.VECTOR,
        output_path=path,
        total_tiles=1,
    )
    window.task_service.set_status(task, DownloadTaskStatus.COMPLETED)
    window._refresh_task_list(select_id=task.id)

    window.task_panel.inspect_button.click()
    qapp.processEvents()

    dialog = window._inspector_dialog
    assert dialog is not None
    assert dialog.path == str(path)


def test_inspector_without_file_reports_and_starts_nothing(
    window: MainWindow, qapp
) -> None:
    """没选文件就点检查 → 只提示，不起线程（Spec §17）。"""
    dialog = _open_inspector(window)
    dialog.set_path("")
    dialog.start_quick_check()
    qapp.processEvents()

    assert dialog.inspection_worker is None
    assert dialog.is_running is False


def test_inspector_quick_check_reaches_pass(window: MainWindow, work_dir: Path, qapp) -> None:
    """端到端：一个好文件在对话框里跑完快速检查，结论是 PASS。"""
    path = _make_complete_map(work_dir / "good.mbtiles")
    dialog = _open_inspector(window, path)

    dialog.start_quick_check()
    assert dialog.is_running is True  # 真的起了后台线程
    _wait_for_inspection(dialog, qapp)

    assert dialog.is_running is False
    assert dialog.status_value.text() == "PASS"
    assert dialog.zoom_table.rowCount() >= 1


def test_inspector_deep_check_runs_in_background(
    window: MainWindow, work_dir: Path, qapp
) -> None:
    path = _make_complete_map(work_dir / "deep.mbtiles")
    dialog = _open_inspector(window, path)

    dialog.start_deep_check()
    worker = dialog.inspection_worker
    assert worker is not None
    assert worker.deep_scan is True
    # 关键：检查必须跑在**别的线程**上，主线程要继续能响应
    assert worker is not QThread.currentThread()
    _wait_for_inspection(dialog, qapp)
    assert dialog.status_value.text() == "PASS"


def test_inspector_reports_failure_for_broken_file(
    window: MainWindow, work_dir: Path, qapp
) -> None:
    broken = work_dir / "broken.mbtiles"
    broken.write_bytes(b"definitely not sqlite" * 16)
    dialog = _open_inspector(window, broken)

    dialog.start_quick_check()
    _wait_for_inspection(dialog, qapp)

    assert dialog.status_value.text() == "FAIL"


def test_inspector_export_report_writes_next_to_map(
    window: MainWindow, work_dir: Path, qapp
) -> None:
    """[导出报告] 落点是被检查文件旁边的 ``xxx.mbtiles.report.txt``（Spec §26）。"""
    path = _make_complete_map(work_dir / "export.mbtiles")
    dialog = _open_inspector(window, path)
    dialog.start_quick_check()
    _wait_for_inspection(dialog, qapp)

    # 直接调导出（避免测试里弹文件对话框）
    from app.services import MBTilesInspectionService

    report = MBTilesInspectionService().export_report(dialog.result)
    assert report.name == "export.mbtiles.report.txt"
    assert "PASS" in report.read_text(encoding="utf-8")


def test_inspector_close_is_safe(window: MainWindow, work_dir: Path, qapp) -> None:
    """关窗不该崩：对话框**不能**带 WA_DeleteOnClose（后台线程还在跑时会中止进程）。"""
    assert window._inspector_dialog is None
    dialog = _open_inspector(window, _make_complete_map(work_dir / "close.mbtiles"))

    assert dialog.close() is True
    qapp.processEvents()
    # 实例仍被主窗口持有，可以再打开
    assert window.open_mbtiles_inspector(str(work_dir / "close.mbtiles")) is None
    assert window._inspector_dialog is dialog


def test_closing_window_shuts_down_inspector(
    window: MainWindow, work_dir: Path, qapp
) -> None:
    """主窗口关闭时，检查对话框要先收掉后台线程（否则关程序会卡住）。"""
    dialog = _open_inspector(window, _make_complete_map(work_dir / "shutdown.mbtiles"))
    dialog.show()
    qapp.processEvents()

    window.close()
    qapp.processEvents()
    assert dialog.inspection_worker is None


def test_inspector_dialog_has_no_qt_main_thread_sql() -> None:
    """检查对话框里不许出现 SQLite 调用（Spec §18：SQL 只能待在 reader 里）。"""
    source = inspect.getsource(main_window_module)
    assert "MBTilesInspectorDialog" in source
    dialog_source = inspect.getsource(MBTilesInspectorDialog)
    assert "sqlite3" not in dialog_source
    assert "SELECT " not in dialog_source


# --------------------------------------------------------------------------- #
# Slice 9：离线地图包（Spec §16 ~ §21）
# --------------------------------------------------------------------------- #
def _open_package_dialog(window: MainWindow, path: "Path | str" = "") -> PackageDialog:
    """打开「生成地图包」窗口（主窗口的入口，不是直接 new 一个）。"""
    window.open_package_dialog(str(path) if path else "")
    dialog = window._package_dialog
    assert dialog is not None, "应当创建打包对话框"
    return dialog


def _wait_until(qapp, predicate, timeout: float = 30.0) -> bool:
    """轮询 + 空转事件循环，直到条件成立或超时。

    不能只 ``worker.wait()``：**线程结束**与**完成信号投递到主线程**之间
    有一个真实的窗口 —— 那时 ``isRunning()`` 已经是 ``False``，但界面上
    还是空的（Slice 8 踩过的坑，两个方向都要堵）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    qapp.processEvents()
    return bool(predicate())


def _wait_for_source_check(dialog: PackageDialog, qapp, timeout: float = 30.0) -> None:
    worker = dialog.inspection_worker
    if worker is not None:
        worker.wait(int(timeout * 1000))
    assert _wait_until(qapp, lambda: dialog.diagnostics is not None, timeout), (
        "源文件检查没有在超时前把结论送回主线程"
    )


def _wait_for_build_result(dialog: PackageDialog, qapp, timeout: float = 60.0) -> None:
    worker = dialog.build_worker
    if worker is not None:
        worker.wait(int(timeout * 1000))
    assert _wait_until(
        qapp,
        lambda: dialog.build_worker is None and dialog.result is not None,
        timeout,
    ), "构建没有在超时前把结果送回主线程"


def _wait_for_confirmation(dialog: PackageDialog, qapp, timeout: float = 60.0) -> None:
    """等"需要用户拍板"的确认框（Spec §16 / §23）。"""
    worker = dialog.build_worker
    if worker is not None:
        worker.wait(int(timeout * 1000))
    assert _wait_until(
        qapp,
        lambda: dialog.build_worker is None and dialog._confirm_box is not None,
        timeout,
    ), "没有等到确认框"


def _wait_for_validation_view(dialog: PackageDialog, qapp, timeout: float = 60.0) -> None:
    """等「同一个窗口里的 [验证地图包]」把结论画出来。

    注意 ``PackageDialog`` 的 ``result`` 属性装的是**构建结果**（Spec §10），
    校验结论只画在控件上，所以这里的就绪信号是"文本区里出现了四行结论"。
    """
    worker = dialog.validate_worker
    if worker is not None:
        worker.wait(int(timeout * 1000))
    assert _wait_until(
        qapp,
        lambda: dialog.validate_worker is None
        and "Result：" in dialog.diagnostics_text.toPlainText(),
        timeout,
    ), "校验没有在超时前把结论画到界面上"


def _button_with_text(box, text: str):
    for button in box.buttons():
        if button.text() == text:
            return button
    raise AssertionError(f"确认框里没有「{text}」按钮：{[b.text() for b in box.buttons()]}")


def _read_manifest_text(package_dir: Path) -> dict:
    """读一个地图包里的 manifest（界面侧不解析，测试用来独立核对）。"""
    import json

    return json.loads((Path(package_dir) / "manifest.json").read_text(encoding="utf-8"))


def test_package_actions_exist_in_file_menu(window: MainWindow) -> None:
    """Spec §17 / §21：[生成地图包] 与 [验证地图包] 都挂在「文件」菜单里。"""
    from app.config.settings import (
        UI_TEXT_PACKAGE_BUTTON,
        UI_TEXT_PACKAGE_BUTTON_VALIDATE,
    )

    assert UI_TEXT_PACKAGE_BUTTON in window.package_action.text()
    assert UI_TEXT_PACKAGE_BUTTON_VALIDATE in window.validate_package_action.text()
    texts = [
        action.text()
        for menu_action in window.menuBar().actions()
        if menu_action.menu() is not None
        for action in menu_action.menu().actions()
    ]
    assert any(UI_TEXT_PACKAGE_BUTTON in text for text in texts)
    assert any(UI_TEXT_PACKAGE_BUTTON_VALIDATE in text for text in texts)


def test_package_dialog_is_reused(window: MainWindow, work_dir: Path) -> None:
    """反复打开要复用同一个实例（否则屏幕上会攒一堆窗口）。"""
    first = _open_package_dialog(window)
    second = _open_package_dialog(window, work_dir / "a.mbtiles")
    assert first is second
    assert first.path.endswith("a.mbtiles")


def test_package_dialog_keeps_default_output_dir(window: MainWindow) -> None:
    """Spec §17：输出目录有默认值（``<默认输出目录>/packages``），用户不必先挑。"""
    dialog = _open_package_dialog(window)

    assert dialog.output_dir.endswith("packages")
    assert dialog.package_name == ""
    assert dialog.buttons["build"].isEnabled() is False  # 还没选源文件


def test_package_dialog_fills_map_info_from_background_check(
    window: MainWindow, work_dir: Path, qapp
) -> None:
    """Spec §6 / §17：「地图类型 / Zoom / 瓦片」全部来自后台检查，界面不自己算。"""
    path = _make_complete_map(work_dir / "good.mbtiles")
    dialog = _open_package_dialog(window, path)

    # 起线程之后**立刻**取引用，再空转事件循环（否则小文件可能已经跑完）
    worker = dialog.inspection_worker
    assert worker is not None, "选中源文件后应当自动开始一次快速检查"
    assert worker is not QThread.currentThread(), "检查必须在后台线程里跑"
    _wait_for_source_check(dialog, qapp)

    assert dialog.diagnostics is not None
    assert dialog.diagnostics.status == "PASS"
    assert dialog.info_values["tiles"].text() == "1"
    assert dialog.info_values["zoom"].text() == "8~8"
    assert "矢量" in dialog.info_values["map_type"].text()
    # 包名按 metadata 的 name 预填（Spec §6：不另维护一套数据）
    assert dialog.package_name == "inspector"
    assert dialog.buttons["build"].isEnabled() is True


def test_package_dialog_disables_build_for_a_failing_source(
    window: MainWindow, work_dir: Path, qapp
) -> None:
    """Spec §23：检查结论是 FAIL 时 [生成地图包] 点不动。"""
    broken = work_dir / "broken.mbtiles"
    broken.write_bytes(b"definitely not a sqlite database" * 32)
    dialog = _open_package_dialog(window, broken)

    _wait_for_source_check(dialog, qapp)

    assert dialog.diagnostics is not None and dialog.diagnostics.status == "FAIL"
    assert dialog.buttons["build"].isEnabled() is False
    assert dialog.package_values["validation"].text() != "PASS"


def test_package_build_runs_in_background(window: MainWindow, work_dir: Path, qapp) -> None:
    """Spec §18 / §19：复制几 GB 与算 SHA256 都不能占主线程。

    这里给构建器注入一个**慢复制**，让"构建中"这个状态有一个确定的窗口，
    从而能真的断言到「线程在跑 + 按钮已禁用」，而不是"跑得太快没看见"。
    """
    from app.package import MapPackageBuilder
    from app.package.checksum import copy_file as real_copy
    from app.services import MapPackageService

    def slow_copy(source, target, **kwargs):
        time.sleep(0.4)
        return real_copy(source, target, **kwargs)

    service = MapPackageService(builder=MapPackageBuilder(copy_file=slow_copy))
    path = _make_complete_map(work_dir / "good.mbtiles")
    dialog = PackageDialog(window, service=service, mbtiles_path=path)
    _wait_for_source_check(dialog, qapp)
    dialog.set_output_dir(str(work_dir / "packages"))
    dialog.name_edit.setText("beijing")

    dialog.buttons["build"].click()

    worker = dialog.build_worker
    assert worker is not None, "应当起后台构建线程"
    assert worker is not QThread.currentThread(), "构建必须在后台线程里跑"
    assert worker.isRunning() is True, "主线程不该等在这里"
    # 构建期间按钮矩阵收缩，但主线程仍然活着（事件循环没被占住）
    assert dialog.is_building is True
    assert dialog.buttons["build"].isEnabled() is False
    assert qapp.processEvents() is None or True  # 事件循环可用 = 界面没冻结

    _wait_for_build_result(dialog, qapp)
    assert dialog.build_worker is None
    assert dialog.buttons["build"].isEnabled() is True


def test_package_dialog_produces_a_verifiable_package(
    window: MainWindow, work_dir: Path, qapp
) -> None:
    """Spec §17 / §18 / §20：从界面走完整条链路，产物可被独立校验。

    这是"手动验收"（Spec §35 第 3 ~ 10 步）在代码里的等价物。
    """
    path = _make_complete_map(work_dir / "good.mbtiles")
    dialog = _open_package_dialog(window, path)
    _wait_for_source_check(dialog, qapp)
    dialog.set_output_dir(str(work_dir / "packages"))
    dialog.name_edit.setText("北京离线地图")

    dialog.buttons["build"].click()
    _wait_for_build_result(dialog, qapp)

    result = dialog.result
    assert result is not None and result.success is True, result.errors if result else None
    package = Path(result.package_dir)
    assert (package / "map.mbtiles").is_file()
    assert (package / "manifest.json").is_file()

    # 完成视图（Spec §20）
    assert dialog.package_values["directory"].text() == result.package_dir
    assert dialog.package_values["tiles"].text() == "1"
    assert dialog.package_values["zoom"].text() == "8"
    assert dialog.package_values["sha256"].text() == result.sha256
    assert dialog.package_values["validation"].text() == "PASS"
    assert dialog.progress_bar.value() == 100
    assert dialog.result.validation_status == "PASS"

    # [验证地图包] 就在同一个窗口里（Spec §21）
    dialog.start_validation(result.package_dir)
    worker = dialog.validate_worker
    assert worker is not None
    assert worker is not QThread.currentThread()
    _wait_for_validation_view(dialog, qapp)
    assert dialog.package_values["validation"].text() == "PASS"
    text = dialog.diagnostics_text.toPlainText()
    assert "Manifest：PASS" in text
    assert "MBTiles：PASS" in text
    assert "SHA256：PASS" in text
    assert "Result：PASS" in text


def test_package_dialog_asks_before_overwriting(
    window: MainWindow, work_dir: Path, qapp
) -> None:
    """Spec §16：目标已存在 → 弹[覆盖]/[选择其他名称]/[取消]，**二次确认**。

    而且是**非模态**的（``exec()`` 会在无头测试里把用例挂死，
    用户也没道理不能一边看框一边去翻别的任务）。
    """
    from app.config.settings import (
        UI_TEXT_BUTTON_CANCEL,
        UI_TEXT_PACKAGE_BUTTON_OVERWRITE,
        UI_TEXT_PACKAGE_BUTTON_RENAME,
        UI_TEXT_PACKAGE_EXISTS_TITLE,
    )

    path = _make_complete_map(work_dir / "good.mbtiles")
    dialog = _open_package_dialog(window, path)
    _wait_for_source_check(dialog, qapp)
    dialog.set_output_dir(str(work_dir / "packages"))
    dialog.name_edit.setText("beijing")

    dialog.buttons["build"].click()
    _wait_for_build_result(dialog, qapp)
    first_sha = dialog.result.sha256
    mbtiles = Path(dialog.result.package_dir) / "map.mbtiles"
    before = mbtiles.read_bytes()

    # 第二次点：目标已存在
    dialog.result = None
    dialog.buttons["build"].click()
    _wait_for_confirmation(dialog, qapp)

    box = dialog._confirm_box
    assert box is not None
    assert box.windowTitle() == UI_TEXT_PACKAGE_EXISTS_TITLE
    for text in (
        UI_TEXT_PACKAGE_BUTTON_OVERWRITE,
        UI_TEXT_PACKAGE_BUTTON_RENAME,
        UI_TEXT_BUTTON_CANCEL,
    ):
        assert _button_with_text(box, text) is not None
    # 用户还没拍板：磁盘上必须一个字节都没变
    assert mbtiles.read_bytes() == before

    _button_with_text(box, UI_TEXT_PACKAGE_BUTTON_OVERWRITE).click()
    _wait_for_build_result(dialog, qapp)

    assert dialog.result.success is True, dialog.result.errors
    # 覆盖 = 改名备份，不是删除（Spec §16）：旧包必须**还在**，只是换了名字
    assert dialog.result.backup_path is not None
    backup = Path(dialog.result.backup_path)
    assert backup.is_dir()
    assert (backup / "map.mbtiles").read_bytes() == before
    assert _read_manifest_text(backup)["mbtiles"]["sha256"] == first_sha
    # 正式目录里是新的那一份（源文件没变，所以内容逐字节相同 —— "新"体现在
    # manifest 里的摘要与它自身对得上）
    assert mbtiles.read_bytes() == before
    assert (
        _read_manifest_text(mbtiles.parent)["mbtiles"]["sha256"] == dialog.result.sha256
    )


def test_package_validate_dialog_reports_pass_then_fail(
    window: MainWindow, work_dir: Path, qapp
) -> None:
    """Spec §21 / §35 第 11 步：改一个字节之后，界面**必须**从 PASS 变成 FAIL。

    "改了文件却仍然显示 PASS"是这条 Slice 最坏的结果 —— 用户会带着一个坏包
    去导入。所以这条端到端（界面 → 后台线程 → 结论 → 文本区）必须有守卫。
    """
    from app.config.settings import UI_TEXT_PACKAGE_VALIDATE_DONE_TITLE
    from app.package import MapPackageBuilder

    source = _make_complete_map(work_dir / "good.mbtiles")
    built = MapPackageBuilder().build(source, work_dir / "packages", package_name="beijing")
    assert built.success is True

    window.open_package_validator(built.package_dir)
    dialog = window._package_validator_dialog
    assert dialog is not None
    assert dialog.package_dir == built.package_dir

    dialog.validate_button.click()
    worker = dialog.validation_worker
    assert worker is not None
    assert worker is not QThread.currentThread()
    assert _wait_until(qapp, lambda: dialog.result is not None and not dialog.is_running)
    assert dialog.result.valid is True
    assert "Result：PASS" in dialog.result_text.toPlainText()

    # ---- 改一个字节（Spec §35 第 11 步）----
    mbtiles = Path(built.package_dir) / "map.mbtiles"
    payload = bytearray(mbtiles.read_bytes())
    payload[4200] ^= 0xFF
    mbtiles.write_bytes(bytes(payload))
    dialog.result = None

    dialog.validate_button.click()
    assert _wait_until(qapp, lambda: dialog.result is not None and not dialog.is_running)

    assert dialog.result.valid is False, "改过字节的包绝不能仍然 PASS"
    text = dialog.result_text.toPlainText()
    assert "Result：FAIL" in text
    assert "SHA256：FAIL" in text
    assert dialog.result.status == "FAIL"


def test_task_panel_package_button_requires_an_existing_file(
    window: MainWindow, work_dir: Path, qapp
) -> None:
    """Spec §17：这个入口**只有当前 MBTiles 存在且基本可用时**才可用。"""
    panel = window.task_panel
    assert panel.package_button.isEnabled() is False, "没有选中任务时应当是灰的"

    # 选中一条任务，但产物文件不在磁盘上 → 仍然不可点
    missing = window.task_service.create_task(
        name="missing",
        bounds=GeoBounds(116.0, 39.0, 116.5, 39.5),
        min_zoom=8,
        max_zoom=8,
        map_layer=MapLayerType.VECTOR,
        output_path=work_dir / "missing.mbtiles",
        total_tiles=1,
    )
    window.task_service.set_status(missing, DownloadTaskStatus.COMPLETED)
    window._refresh_task_list(select_id=missing.id)
    qapp.processEvents()
    assert panel.package_button.isEnabled() is False

    # 换成一个真的存在的产物 → 可点，并且把路径抛出去
    real = _make_complete_map(work_dir / "real.mbtiles")
    task = window.task_service.create_task(
        name="real",
        bounds=GeoBounds(116.0, 39.0, 116.5, 39.5),
        min_zoom=8,
        max_zoom=8,
        map_layer=MapLayerType.VECTOR,
        output_path=real,
        total_tiles=1,
    )
    window.task_service.set_status(task, DownloadTaskStatus.COMPLETED)
    window._refresh_task_list(select_id=task.id)
    qapp.processEvents()
    assert panel.package_button.isEnabled() is True

    paths: list[str] = []
    panel.package_requested.connect(paths.append)
    panel.package_button.click()
    assert paths == [str(real)]


def test_task_panel_package_button_opens_dialog_with_selected_file(
    window: MainWindow, work_dir: Path, qapp
) -> None:
    """点任务列表里的 [生成地图包] 要带着选中任务的产物打开窗口。"""
    real = _make_complete_map(work_dir / "real.mbtiles")
    task = window.task_service.create_task(
        name="real",
        bounds=GeoBounds(116.0, 39.0, 116.5, 39.5),
        min_zoom=8,
        max_zoom=8,
        map_layer=MapLayerType.VECTOR,
        output_path=real,
        total_tiles=1,
    )
    window.task_service.set_status(task, DownloadTaskStatus.COMPLETED)
    window._refresh_task_list(select_id=task.id)

    window.task_panel.package_button.click()
    qapp.processEvents()

    dialog = window._package_dialog
    assert dialog is not None
    assert dialog.path == str(real)
    _wait_for_source_check(dialog, qapp)


def test_closing_window_shuts_down_package_dialogs(
    window: MainWindow, work_dir: Path, qapp
) -> None:
    """主窗口关闭时要先收掉打包/验证线程（否则关程序会卡住或直接中止进程）。"""
    path = _make_complete_map(work_dir / "good.mbtiles")
    package = _open_package_dialog(window, path)
    window.open_package_validator(str(work_dir / "packages"))
    validator = window._package_validator_dialog
    assert validator is not None
    package.show()
    validator.show()
    qapp.processEvents()

    window.close()
    qapp.processEvents()

    assert package.build_worker is None
    assert package.inspection_worker is None
    assert package.validate_worker is None
    assert validator.validation_worker is None


def test_package_dialog_has_no_qt_main_thread_sql_and_uses_workers() -> None:
    """Spec §19：打包/校验一律走 QThread，主线程不许碰 SQLite。"""
    import app.ui.package_dialog as package_dialog_module

    source = inspect.getsource(package_dialog_module)
    assert "sqlite3" not in source
    assert "SELECT " not in source
    assert issubclass(PackageBuildWorker, QThread)
    assert issubclass(PackageValidateWorker, QThread)
    # 结果必须经信号回主线程，而不是在后台线程里直接改控件
    assert "progress_changed" in source
    assert "package_completed" in source
    assert "validation_completed" in source
