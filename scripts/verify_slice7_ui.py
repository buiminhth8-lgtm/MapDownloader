"""Slice 7 真实平台界面验收脚本。

**为什么要单独写一个（而不是只跑 pytest）**：``pytest`` 跑在 Qt ``offscreen``
平台上——那里没有真实字体、没有 DPI 缩放、没有系统深浅色主题、``QMessageBox``
也不是真的窗口。而这三种东西恰恰能藏住真实的缺陷。本脚本就是那次"实机看一眼"
的可重复版本：

- 真的起 windows 平台插件、真的显示主窗口；
- 真的点按钮（``QPushButton.click()`` / ``QMessageBox`` 的按钮）；
- 每个场景截一张图，落在 ``docs/images/slice7_*.png``，人可以直接看；
- 全程用 ``httpx.MockTransport`` 假服务端，**不触网、不消耗天地图配额**；
- 任务记录与界面偏好都写到临时目录，**不碰真实用户数据与注册表**。

覆盖 Spec §28 的五个场景：

::

    A  空闲 → 计算 → 预估结果六行（含 Zoom 与地图类型）
    B  预计 > 10,000 张 → 大任务确认框（只提示、不限制）
    C  目标 .mbtiles 已存在 → 三选一（继续 / 重新下载 / 取消）
    D  下载中 → 暂停 / 继续 → 完成小结（含「打开目录」）
    E  部分瓦片失败 → 失败小结（含「继续补齐」）

用法::

    python scripts/verify_slice7_ui.py
    python scripts/verify_slice7_ui.py --offscreen    # 无显示环境（CI）
    python scripts/verify_slice7_ui.py --no-shots     # 只跑检查，不截图
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DOCS = PROJECT_ROOT / "docs" / "images"
FAKE_TOKEN = "abcdefghijklmn"
FAKE_PNG = b""  # 运行时填充（必须是一张真 PNG：合成模式要解码）


def _real_png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# 假服务端
# --------------------------------------------------------------------------- #
def _handler(status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code, content=FAKE_PNG, headers={"content-type": "image/png"}
        )

    return handler


# --------------------------------------------------------------------------- #
# 检查器
# --------------------------------------------------------------------------- #
class Checker:
    """收集检查结果，最后统一汇报并以退出码表达结论。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  [OK]   {label}")
        else:
            self.failures.append(f"{label}{f' — {detail}' if detail else ''}")
            print(f"  [FAIL] {label}{f' — {detail}' if detail else ''}")
        return bool(ok)

    def section(self, title: str) -> None:
        print(f"\n=== {title} ===")


def _pump(app, times: int = 8) -> None:
    for _ in range(times):
        app.processEvents()


def _wait_until(app, predicate, timeout_s: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _wait_scan(app, window, timeout_ms: int = 20_000) -> None:
    scan = window._scan_worker
    if scan is None:
        return
    scan.wait(timeout_ms)
    _pump(app)


#: 连续两个及以上 ASCII 字母 → 判为"漏改的英文按钮"（"OK" 这种也算）。
#: 只跑 ``offscreen`` 的 pytest 完全查不出这个：那里不渲染按钮，
#: ``StandardButton.Yes`` 的默认文案永远是 "Yes"，没人会去断言它。
_ENGLISH_BUTTON = re.compile(r"[A-Za-z]{2,}")


def _english_buttons(labels: list[str]) -> list[str]:
    """按钮文案里还带着英文的那些（空列表 = 全是中文）。"""
    return [text for text in labels if _ENGLISH_BUTTON.search(text)]


def _shot(widget, name: str, enabled: bool) -> None:
    """把任意控件（主窗口 / 弹窗）存成 ``docs/images/<name>``。

    弹窗刻意**单独截**：``QMessageBox`` 是独立的顶层窗口，跟着主窗口一起截
    会被窗口右边界切掉一半（截出来正好是最需要看的那行字被裁掉）。
    """
    if not enabled:
        return
    DOCS.mkdir(parents=True, exist_ok=True)
    target = DOCS / name
    if widget.grab().save(str(target)):
        print(f"  [shot] {target.relative_to(PROJECT_ROOT)}")


# --------------------------------------------------------------------------- #
# 环境搭建
# --------------------------------------------------------------------------- #
def _fill_form(window, output_dir: Path, *, west=116.0, east=116.1, south=39.0,
               north=39.1, zoom=12, name="beijing", labels=True) -> None:
    panel = window.download_panel
    panel._token_edit.setText(FAKE_TOKEN)
    panel._west_spin.setValue(west)
    panel._south_spin.setValue(south)
    panel._east_spin.setValue(east)
    panel._north_spin.setValue(north)
    panel._min_zoom_spin.setValue(zoom)
    panel._max_zoom_spin.setValue(zoom)
    panel._output_edit.setText(str(output_dir))
    panel.map_name_edit.setText(name)
    panel._labels_check.setChecked(labels)


def _seed_existing(window, tiles: int = 3) -> Path:
    """按当前表单预置一个含真瓦片的 ``.mbtiles``（走生产 Writer）。"""
    from app.geo import GeoBounds, calculate_tile_ranges
    from app.mbtiles import MBTilesMetadata, MBTilesWriter
    from app.models import composition_for
    from app.services import build_output_path

    data = window.download_panel.get_form_data()
    bounds = GeoBounds(data["west"], data["south"], data["east"], data["north"])
    path = build_output_path(data["output_dir"], data["map_name"])
    path.parent.mkdir(parents=True, exist_ok=True)
    labels = bool(data["with_labels"])
    writer = MBTilesWriter(path, batch_size=7)
    writer.open()
    writer.write_metadata(
        MBTilesMetadata.for_bounds(
            bounds,
            data["min_zoom"],
            data["max_zoom"],
            name=data["map_name"],
            description="",
            source_map_type=data["map_type"].value,
            with_labels=labels,
            composition=composition_for(labels),
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


def _build_window(qapp, work_dir: Path):
    """装配一个走临时目录 / 临时偏好、且网络被打桩的主窗口。"""
    from PySide6.QtCore import QSettings

    import app.ui.main_window as main_window_module
    from app.providers import TiandituProvider
    from app.tasks import TaskService, TaskStore
    from app.ui.main_window import MainWindow
    from app.ui.preferences import UiPreferences

    original = TiandituProvider

    def factory(token: str, *args: object, **kwargs: object) -> TiandituProvider:
        kwargs.setdefault("transport", httpx.MockTransport(_handler()))
        return original(token, *args, **kwargs)

    main_window_module.TiandituProvider = factory  # type: ignore[assignment]

    prefs_settings = QSettings(
        str(work_dir / "ui.ini"), QSettings.Format.IniFormat
    )
    return MainWindow(
        task_service=TaskService(TaskStore(work_dir / "tasks.json")),
        preferences=UiPreferences(prefs_settings),
    )


def _stub_service(
    work_dir: Path, *, status_code: int = 200, requests_per_second: float | None = None
) -> None:
    """把 DownloadWorker 内部的 MapDownloadService 换成走 MockTransport 的版本。

    生产代码里没有任何测试钩子，因此这里替换的是 worker 模块**引用的那个名字**——
    顺带把限速 / 退避 / 批次调成"测试档位"。替换 service 而不是 TileDownloader：
    下载器由 service 自己创建，在 service 这一层注入才是唯一拦得住的入口。

    Args:
        work_dir: 临时目录（保留参数以便将来放夹具文件）。
        status_code: 假服务端返回的 HTTP 状态码。
        requests_per_second: 限速；``None`` 表示不限速。要观察"下载进行中"的中间态
            （暂停 / 取消）就得限速，否则几张小瓦片会在一瞬间跑完，
            用例会退化成"暂停一个已经结束的任务"。
    """
    import app.ui.download_worker as download_worker_module
    from app.downloader import RateLimiter, RetryPolicy

    # **必须从定义它的模块取真身**，不能取 ``download_worker_module`` 上那个
    # 名字：同一个进程里连着换桩（场景 D 换 200、场景 E 换 500）时，
    # 第二次拿到的会是上一次的桩，桩套桩 → 内层会把外层的 transport 覆盖掉，
    # 500 悄悄变回 200，用例就会"绿得很奇怪"。
    from app.services.map_download_service import MapDownloadService as _RealService

    def factory(provider, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler(status_code))
        kwargs["retry_policy"] = RetryPolicy(
            max_attempts=1 if status_code >= 400 else 3,
            base_delay_seconds=0.0,
            max_delay_seconds=0.0,
        )
        kwargs["rate_limiter"] = (
            RateLimiter.disabled()
            if requests_per_second is None
            else RateLimiter(requests_per_second=requests_per_second)
        )
        kwargs["batch_size"] = 7
        return _RealService(provider, **kwargs)

    download_worker_module.MapDownloadService = factory  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# 场景
# --------------------------------------------------------------------------- #
def scene_a_estimate(app, window, checker: Checker, shots: bool) -> None:
    checker.section("场景 A：空闲 → 计算 → 预估结果")
    panel = window.download_panel
    panel._min_zoom_spin.setValue(8)
    panel._max_zoom_spin.setValue(17)
    panel.action_buttons["calculate"].click()
    _pump(app)

    lines = panel.result_label.text().splitlines()
    for line in lines:
        print(f"    {line}")
    checker.check(len(lines) == 6, "结果区是六行", f"实际 {len(lines)} 行")
    checker.check(any("预计瓦片：" in x for x in lines), "含瓦片数")
    checker.check(any("预计 WMTS 请求：" in x for x in lines), "含请求量")
    checker.check(any("预计容量：" in x for x in lines), "含容量")
    checker.check(any(x.startswith("Zoom：8 ~ 17") for x in lines), "含 Zoom 区间")
    checker.check(any(x.startswith("地图类型：") for x in lines), "含地图类型")
    checker.check(any(x.startswith("输出文件：") for x in lines), "含输出文件")
    checker.check(
        panel.action_buttons["start"].isEnabled() and
        not panel.action_buttons["pause"].isEnabled() and
        not panel.action_buttons["cancel"].isEnabled(),
        "空闲态按钮：可点「开始下载」，不可点「暂停 / 取消」",
    )
    _shot(window, "slice7_scene_a_estimate.png", shots)


def scene_b_large_task(
    app, window, checker: Checker, work_dir: Path, shots: bool
) -> None:
    checker.section("场景 B：大任务确认（> 10,000 张，只提示不限制）")
    from PySide6.QtWidgets import QMessageBox

    _fill_form(window, work_dir, west=116.0, east=117.0, south=39.0, north=40.0,
               zoom=12, name="big")
    window.download_panel._min_zoom_spin.setValue(8)
    window.download_panel._max_zoom_spin.setValue(17)
    window.download_panel.action_buttons["start"].click()
    _pump(app)

    dialog = window._confirm_dialog
    checker.check(dialog is not None, "弹出了大任务确认框")
    if dialog is None:
        return
    plan = window._pending_plan
    checker.check(plan is not None and plan.total > 10_000,
                  "计划规模确实超过 10,000 张",
                  f"total={plan.total if plan else None}")
    print(f"    确认框标题：{dialog.windowTitle()}")
    # Slice 23 起这个框的正文由风险报告拼出来（``report.as_lines()`` + 一句提问），
    # 措辞从"请求"变成了"预计下载 N 张瓦片"。要守的性质没变 ——
    # **框里必须给出这次的瓦片量**，而不是只说一句"任务很大"。
    # 所以按"含张数"断言而不是锁死措辞：锁措辞的结果是每次文案优化都要改验收脚本。
    _body = dialog.text()
    checker.check(
        "张瓦片" in _body and any(char.isdigit() for char in _body),
        "确认框里给了这次要下的瓦片量",
        _body.replace("\n", " / ")[:160],
    )
    # 大任务只提示、不限制：这里点「否」（= 放弃本次）收尾，免得为了一条检查
    # 真的去下十几万张——"确认后照常开跑"那条路径由场景 D 用小区域覆盖。
    labels = [button.text() for button in dialog.buttons()]
    checker.check(dialog.button(QMessageBox.StandardButton.Yes) is not None,
                  "确认框有「是」按钮（= 继续下载）")
    checker.check(not _english_buttons(labels), "确认框按钮全是中文", str(labels))
    _shot(dialog, "slice7_scene_b_large_task.png", shots)
    dialog.button(QMessageBox.StandardButton.No).click()
    _pump(app)
    checker.check(window.task_panel.task_count == 0, "放弃后没有留下半截任务记录",
                  f"实际 {window.task_panel.task_count} 条")


def scene_c_existing_file(
    app, window, checker: Checker, work_dir: Path, shots: bool
) -> None:
    checker.section("场景 C：目标文件已存在 → 三选一")
    from PySide6.QtWidgets import QMessageBox

    _fill_form(window, work_dir, name="scene_c")
    path = _seed_existing(window, tiles=3)
    before = path.read_bytes()

    window.download_panel.action_buttons["start"].click()
    _pump(app)
    _wait_scan(app, window)

    dialog = window._exists_dialog
    checker.check(dialog is not None, "弹出了「已有地图文件」选择框")
    if dialog is None:
        return
    text = dialog.text()
    checker.check("已有瓦片：3 张" in text, "数出了已有的 3 张瓦片", text.splitlines()[:4])
    checker.check(path.read_bytes() == before, "还没动过用户的文件")
    labels = [button.text() for button in dialog.buttons()]
    checker.check(not _english_buttons(labels), "三选一按钮全是中文", str(labels))
    _shot(dialog, "slice7_scene_c_existing.png", shots)

    dialog.button(QMessageBox.StandardButton.Cancel).click()
    _pump(app)
    checker.check(path.read_bytes() == before, "取消后文件一字未改")
    checker.check(list(work_dir.glob("*.bak-*")) == [], "没有产生备份文件")
    checker.check(window.task_panel.task_count == 0, "取消后任务列表为空")

    # 再走一遍"重新下载"这条支路：它还有一次二次确认（Spec §14 要求明说旧文件
    # 会被备份到哪里，而不是静默删掉）。这里只验证到二次确认框为止并取消掉——
    # 真跑一遍下载是场景 D 的活。
    window.download_panel.action_buttons["start"].click()
    _pump(app)
    _wait_scan(app, window)
    dialog = window._exists_dialog
    checker.check(dialog is not None, "第二次点「开始下载」仍然弹出三选一")
    if dialog is not None:
        dialog.button(QMessageBox.StandardButton.No).click()  # 「重新下载」
        _pump(app)
        restart = window._restart_dialog
        checker.check(restart is not None, "「重新下载」有二次确认")
        if restart is not None:
            labels = [button.text() for button in restart.buttons()]
            checker.check(not _english_buttons(labels), "二次确认按钮全是中文", str(labels))
            _shot(restart, "slice7_scene_c2_restart.png", shots)
            restart.button(QMessageBox.StandardButton.Cancel).click()
            _pump(app)
    checker.check(path.read_bytes() == before, "二次确认里取消后文件仍一字未改")
    checker.check(list(work_dir.glob("*.bak-*")) == [], "取消后没有留下备份文件")

    # 把这个预置文件清掉：后面的场次要用"目标不存在"这条路径
    path.unlink(missing_ok=True)


def scene_d_download(app, window, checker: Checker, work_dir: Path, shots: bool) -> None:
    checker.section("场景 D：下载中 → 暂停 / 继续 → 完成小结")
    from PySide6.QtWidgets import QMessageBox

    from app.geo import GeoBounds, estimate_tile_count

    # 区域刻意放大到 z12 下约 15 张、并把限速调到 10 请求/秒：
    # 这样"下载进行中"是一个能稳定观察到的中间态，而不是靠"跑得快"侥幸赢下的时序。
    _fill_form(
        window, work_dir,
        west=116.0, south=39.0, east=116.3, north=39.2,
        zoom=12, name="scene_d",
    )
    data = window.download_panel.get_form_data()
    total = estimate_tile_count(
        GeoBounds(data["west"], data["south"], data["east"], data["north"]),
        data["min_zoom"],
        data["max_zoom"],
    )
    print(f"    本次区域共 {total} 张瓦片（勾了中文注记 → 每张 2 次请求）")
    checker.check(total > 10, "区域足够大，能观察到下载中的中间态", f"total={total}")

    panel = window.download_panel
    panel.action_buttons["start"].click()
    _pump(app)

    worker = window._download_worker
    checker.check(worker is not None, "下载线程已启动")
    checker.check(
        panel.action_buttons["pause"].isEnabled()
        and panel.action_buttons["cancel"].isEnabled()
        and not panel.action_buttons["start"].isEnabled(),
        "下载中：可点「暂停 / 取消」，不可点「开始下载」",
    )
    checker.check(window._status_values["task_status"].text() == "下载中",
                  "进度区状态是「下载中」",
                  window._status_values["task_status"].text())
    _shot(window, "slice7_scene_d1_downloading.png", shots)

    # 暂停 → 正在暂停 → 已暂停
    panel.action_buttons["pause"].click()
    _pump(app)
    if worker is not None and worker.isRunning():
        paused = _wait_until(
            app, lambda: window._status_values["task_status"].text() == "已暂停"
        )
        checker.check(paused, "暂停落到「已暂停」",
                      window._status_values["task_status"].text())
        checker.check(panel.action_buttons["resume"].isEnabled(),
                      "已暂停：可点「继续」")
        panel.action_buttons["resume"].click()
        _pump(app)
    else:
        # 小区域可能在暂停生效前就跑完了——不算失败，但要如实说出来
        print("    [note] 任务在暂停生效前已结束（区域太小），跳过暂停/继续检查")

    finished = _wait_until(app, lambda: window._download_worker is None)
    checker.check(finished, "任务在超时内结束")

    result = panel.download_result_label.text()
    print("    结果区：")
    for line in result.splitlines():
        print(f"      {line}")
    checker.check("验证：PASS" in result, "产物通过校验")
    checker.check(window._complete_dialog is not None, "弹出了完成小结框")
    if window._complete_dialog is not None:
        labels = [
            b.text() for b in window._complete_dialog.buttons()
        ]
        checker.check(
            any("打开目录" in text for text in labels),
            "完成小结里有「打开目录」", str(labels),
        )
        checker.check(not _english_buttons(labels), "完成小结按钮全是中文", str(labels))
        _shot(window._complete_dialog, "slice7_scene_d2_complete.png", shots)
        # 完成小结是**非模态**的（Spec §16：不能把用户卡死在弹窗里），带的是
        # 自定义「打开目录」（ActionRole）+ 标准 Close，没有 Ok——这里按 Close 关。
        close_button = window._complete_dialog.button(QMessageBox.StandardButton.Close)
        checker.check(close_button is not None, "完成小结有关闭出口")
        if close_button is not None:
            close_button.click()
        else:
            window._complete_dialog.close()
        _pump(app)

    output = work_dir / "scene_d.mbtiles"
    checker.check(output.exists(), "磁盘上真的有产物", str(output))
    if window.task_panel.task_count:
        cells = window.task_panel.row_cells(0)
        checker.check(cells["状态"] == "已完成", "任务列表那一行是「已完成」",
                      cells["状态"])
    else:
        checker.check(False, "任务列表里有这一条")


def scene_e_failure(app, window, checker: Checker, work_dir: Path, shots: bool) -> None:
    checker.section("场景 E：部分瓦片失败 → 失败小结 + 「继续补齐」")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox

    _stub_service(work_dir, status_code=500)

    _fill_form(window, work_dir, name="failing")
    window.download_panel.action_buttons["start"].click()
    _pump(app)
    checker.check(_wait_until(app, lambda: window._download_worker is None),
                  "失败任务也在超时内干净收尾（GUI 没有崩）")

    result = window.download_panel.download_result_label.text()
    checker.check("验证：FAIL" in result, "结果区说明校验 FAIL")
    checker.check("失败原因：" in result, "结果区给出失败原因")

    dialog = window._failed_dialog
    checker.check(dialog is not None, "弹出了失败小结框")
    if dialog is not None:
        labels = [b.text() for b in dialog.buttons()]
        checker.check(any("继续补齐" in text for text in labels),
                      "失败小结里有「继续补齐」", str(labels))
        checker.check(not _english_buttons(labels), "失败小结按钮全是中文", str(labels))
        _shot(dialog, "slice7_scene_e_failed.png", shots)
        dialog.button(QMessageBox.StandardButton.Close).click()
        _pump(app)

    # 任务记录要落盘成 FAILED（下次打开才能接着补齐）。
    # 场景 D 已经留下一条"已完成"，所以这里按**名字**找本次那条，
    # 而不是赌它一定排在第 0 行。
    rows = [
        (row, window.task_panel.row_cells(row))
        for row in range(window.task_panel.task_count)
    ]
    target = next((row for row, cells in rows if cells["名称"] == "failing"), None)
    checker.check(target is not None, "任务列表里有本次这条（failing）",
                  str([cells["名称"] for _row, cells in rows]))
    if target is not None:
        item = window.task_panel.table.item(target, 0)
        task_id = item.data(Qt.ItemDataRole.UserRole)
        fresh = window.task_service.get_task(task_id)
        checker.check(fresh is not None and fresh.status.value == "failed",
                      "任务记录落盘为 FAILED",
                      fresh.status.value if fresh else "None")
        checker.check(window.task_panel.row_cells(target)["状态"] == "失败",
                      "列表那一行显示「失败」",
                      window.task_panel.row_cells(target)["状态"])


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slice 7 真实平台界面验收")
    parser.add_argument("--offscreen", action="store_true",
                        help="使用 offscreen 平台（无显示环境 / CI）")
    parser.add_argument("--no-shots", action="store_true", help="不生成截图")
    parser.add_argument("--scenes", default="abcde",
                        help="要跑的场次字母，例如 acd")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global FAKE_PNG
    args = parse_args(argv)

    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    from PySide6.QtWidgets import QApplication

    from app.main import silence_noisy_http_loggers

    silence_noisy_http_loggers()
    FAKE_PNG = _real_png()

    app = QApplication(sys.argv[:1])
    work_dir = Path(tempfile.mkdtemp(prefix="verify-slice7-"))
    checker = Checker()
    shots = not args.no_shots

    print(f"platform={app.platformName()}  work_dir={work_dir}")
    try:
        window = _build_window(app, work_dir)
        window.show()
        _pump(app)

        if "a" in args.scenes:
            scene_a_estimate(app, window, checker, shots)
        if "b" in args.scenes:
            scene_b_large_task(app, window, checker, work_dir, shots)
        if "c" in args.scenes:
            scene_c_existing_file(app, window, checker, work_dir, shots)
        if "d" in args.scenes:
            _stub_service(work_dir, requests_per_second=10)
            scene_d_download(app, window, checker, work_dir, shots)
        if "e" in args.scenes:
            scene_e_failure(app, window, checker, work_dir, shots)

        window._exit_confirmed = True
        if window._download_worker is not None and window._download_worker.isRunning():
            window._download_worker.cancel()
            window._download_worker.wait(15_000)
        window.close()
        _pump(app, 4)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    print("\n" + "=" * 62)
    if checker.failures:
        print(f"FAILED：{len(checker.failures)} 项检查未通过（通过 {checker.passed} 项）")
        for item in checker.failures:
            print(f"  - {item}")
        return 1
    print(f"PASSED：{checker.passed} 项检查全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
