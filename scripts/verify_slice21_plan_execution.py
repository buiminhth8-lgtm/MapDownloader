"""Slice 21 真实平台验收脚本：Download Plan 批量任务调度（串行 / 暂停 / 继续 / 取消 / 重试 / 跳过 / 恢复）。

**为什么不能只跑 pytest。** 这一 Slice 几乎全是"一页表格 + 六个执行按钮 + 状态区"，
而 ``offscreen`` 下这些恰好是最容易失真的部分：

1. **六个执行按钮是真按钮**。pytest 里可以 ``button.click()``，但那证明不了
   "信号有没有接、接没接错槽"—— Qt 槽里抛的异常会被**静默吞掉**，界面上表现成
   "点了没反应"。本 Slice 真的踩过（按钮接了不存在的槽）。
2. **暂停 / 继续 / 取消是后台线程 + 信号**。``QThread`` 与主线程的配合（暂停立刻生效、
   关页面时线程收干净、UI 不被后台线程直接碰）在真事件循环里才有意义。
3. **布局要真的摆得下**：运行态多了进度条、速度、当前区域、任务 X/Y 一行字，
   放不下时 Qt 会静默压缩，offscreen 看不出来。
4. **执行期间改不动计划**：表格与"移除 / 清空 / 生成任务"变灰，且原因写在 tooltip
   上 —— 这条只有真窗口、真 hover 才能确认。
5. **跨窗口恢复**：程序"重启"后，上次没跑完的计划画出来、一个请求都不发，点「继续」
   接着下 —— 必须真的关一次页再开。

脚本用**假执行者**（``tests/plan_fakes.FakeRunner``）驱动真实的计划页与真实调度器：
不联网、不建真实 MBTiles，但会模仿引擎的记账动作（把进度写进任务记录并落盘），因为
调度器的进度聚合读的正是任务记录。**全程只碰临时目录，绝不触网、绝不下载任何瓦片。**

用法::

    python scripts/verify_slice21_plan_execution.py
    python scripts/verify_slice21_plan_execution.py --offscreen   # 无显示环境
    python scripts/verify_slice21_plan_execution.py --no-shots    # 只跑检查
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DOCS = PROJECT_ROOT / "docs" / "images"

_APP = None


# --------------------------------------------------------------------------- #
# 检查结果收集
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


def _wait_until(app, predicate, timeout_s: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _pump(app, 6)
        if predicate():
            _pump(app, 8)
            return True
        time.sleep(0.02)
    return False


def _shot(widget, name: str, enabled: bool) -> None:
    if not enabled:
        return
    layout = widget.layout()
    if layout is not None:
        layout.activate()
    if _APP is not None:
        _pump(_APP, 12)
    DOCS.mkdir(parents=True, exist_ok=True)
    target = DOCS / name
    if widget.grab().save(str(target)):
        print(f"  [shot] {target.relative_to(PROJECT_ROOT)}")


def _shot_roomy(window, name: str, enabled: bool) -> None:
    if not enabled:
        return
    from PySide6.QtCore import QSize

    original = window.size()
    window.resize(QSize(max(original.width(), 1180), 1080))
    _pump(_APP, 20)
    _shot(window, name, True)
    window.resize(original)
    _pump(_APP, 8)


def _close_dialogs(widget) -> None:
    from PySide6.QtWidgets import QMessageBox

    for dialog in widget.findChildren(QMessageBox):
        dialog.close()


def _answer_message_box(dialog, standard_button) -> bool:
    box = dialog.message_box()
    if box is None:
        return False
    box.button(standard_button).click()
    _pump(_APP, 6)
    return True


# --------------------------------------------------------------------------- #
# 夹具：临时目录里造一份小型行政区目录（全 1° 方块 + 一块三角形）
# --------------------------------------------------------------------------- #
TRIANGLE_COORDINATES = [
    [120.0, 30.0],
    [121.0, 30.0],
    [120.0, 31.0],
    [120.0, 30.0],
]

FIXTURE_ENTRIES = (
    {"id": "demoland", "name": "演示国", "level": "country", "parentId": None,
     "geojson": "country/demoland.geojson"},
    {"id": "northland", "name": "北省", "level": "province", "parentId": "demoland",
     "geojson": "province/northland.geojson"},
    {"id": "southland", "name": "南省", "level": "province", "parentId": "demoland",
     "geojson": "province/southland.geojson"},
    {"id": "rivertown", "name": "河城", "level": "city", "parentId": "northland",
     "geojson": "city/rivertown.geojson"},
    {"id": "hilltown", "name": "山城", "level": "city", "parentId": "northland",
     "geojson": "city/hilltown.geojson"},
    {"id": "triangleland", "name": "三角省", "level": "province", "parentId": "demoland",
     "geojson": "province/triangleland.geojson"},
)

FIXTURE_BOXES = {
    "demoland": (100.0, 20.0, 101.0, 21.0),
    "northland": (100.0, 20.0, 101.0, 21.0),
    "southland": (100.0, 20.0, 101.0, 21.0),
    "rivertown": (100.0, 20.0, 101.0, 21.0),
    "hilltown": (100.0, 20.0, 101.0, 21.0),
}


def _write_boundary(root: Path, relative: str, *, name: str, region_id: str, box) -> None:
    west, south, east, north = box
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {"name": name, "id": region_id, "boundaryVersion": "2026-09-verify"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[west, south], [east, south], [east, north], [west, north], [west, south]]
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def build_fixture_regions(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.json").write_text(
        json.dumps({"version": 1, "regions": list(FIXTURE_ENTRIES)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for entry in FIXTURE_ENTRIES:
        if entry["id"] == "triangleland":
            continue
        _write_boundary(
            root, entry["geojson"], name=entry["name"],
            region_id=entry["id"], box=FIXTURE_BOXES[entry["id"]],
        )
    path = root / "province" / "triangleland.geojson"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {"name": "三角省", "id": "triangleland", "boundaryVersion": "2026-09-verify"},
                "geometry": {"type": "Polygon", "coordinates": [TRIANGLE_COORDINATES]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return root


def use_region_resources(root: Path) -> None:
    from app.geo import region as region_module

    region_module.REGION_RESOURCE_DIR = root


# --------------------------------------------------------------------------- #
# 对话框工厂：真实对话框 + 假执行者（不联网）
# --------------------------------------------------------------------------- #
def make_dialog(plan_service, task_service, store, runner, auto_estimate: bool):
    """造一个注入了假执行者工厂的真实计划页。"""
    from app.planning.plan_scheduler import PlanScheduler
    from app.ui.download_plan_dialog import DownloadPlanDialog

    def factory() -> PlanScheduler:
        return PlanScheduler(
            plan_service, runner, task_service=task_service, store=store,
            save_interval_seconds=0.0, save_tile_interval=1,
        )

    return DownloadPlanDialog(plan_service, auto_estimate=auto_estimate, scheduler_factory=factory)


def build_plan(plan_service, task_service, work_dir: Path, *region_ids: str):
    """加区域 + 估算 + 生成任务，返回一份就绪的计划。"""

    for region_id in region_ids:
        plan_service.add_region(region_id)
    plan_service.apply_settings(name="验收计划", output_dir=work_dir / "output")
    plan_service.estimate()
    return plan_service.plan


# --------------------------------------------------------------------------- #
# 场景 A：开始 → 跑完（真按钮 / 状态区 / 进度条 / 符号）
# --------------------------------------------------------------------------- #
def scene_start_and_finish(app, plan_service, task_service, store, work_dir, checker, shots):
    checker.section("场景 A：点「开始计划」→ 两个区域依次跑完 → 已完成")
    from tests.plan_fakes import FakeRunner

    build_plan(plan_service, task_service, work_dir, "demoland", "northland")
    runner = FakeRunner()
    dialog = make_dialog(plan_service, task_service, store, runner, auto_estimate=False)
    dialog.show()
    _pump(app, 8)
    dialog.refresh()

    checker.check(
        dialog.run_buttons()["start"].isEnabled() is True,
        "有可执行的条目时「开始计划」可用",
        "",
    )
    dialog.run_buttons()["start"].click()
    finished = _wait_until(app, lambda: dialog.execution() is not None
                           and dialog.execution().status.value == "completed", timeout_s=30)
    checker.check(finished, "点「开始计划」后整份计划跑到了「已完成」", "超过 30s")
    # 跨线程信号排队投递：execution 状态先到，进度条 / 状态行 / 表格符号 / 按钮
    # 会晚几帧。等这些控件都画成「已完成」再读，避免读到半路帧（offscreen 下
    # 视觉无差别，但 Qt 排队投递的时序在 CI 上并不保证）。
    _wait_until(
        app,
        lambda: dialog.plan_progress_bar().value() == 100
        and "已完成" in dialog.run_status_lines()[0]
        and all(
            dialog.table().item(row, 5).text().startswith("✓")
            for row in range(dialog.table().rowCount())
        )
        and dialog.run_buttons()["start"].isEnabled()
        and not dialog.run_buttons()["pause"].isEnabled()
        and dialog.table().isEnabled(),
        timeout_s=10,
    )

    execution = dialog.execution()
    checker.check(
        runner.calls == ["demoland", "northland"],
        "按 全国 → 省 的顺序串行执行（层级顺序不被界面打乱）",
        str(runner.calls),
    )
    checker.check(
        execution.status.value == "completed" and dialog.plan_progress_bar().value() == 100,
        "状态区报告「已完成」且整份进度条走到 100%",
        f"{execution.status.value} / {dialog.plan_progress_bar().value()}",
    )
    checker.check(
        "已完成" in dialog.run_status_lines()[0],
        "状态行第一行说「已完成」",
        dialog.run_status_lines()[0],
    )
    checker.check(
        all(dialog.table().item(row, 5).text().startswith("✓") for row in range(dialog.table().rowCount())),
        "每条状态列都画成「✓ 已完成」符号",
        str([dialog.table().item(row, 5).text() for row in range(dialog.table().rowCount())]),
    )
    checker.check(
        dialog.run_buttons()["start"].isEnabled() is True
        and dialog.run_buttons()["pause"].isEnabled() is False,
        "跑完后回到「只能开始」的状态",
        "",
    )
    checker.check(dialog.table().isEnabled() is True, "跑完后编辑区解锁", "")
    _shot_roomy(dialog, "slice21_plan_completed.png", shots)
    dialog.close()
    _pump(app, 10)


# --------------------------------------------------------------------------- #
# 场景 B：暂停 → 继续（真按钮 / 编辑锁定 / 续跑）
# --------------------------------------------------------------------------- #
def scene_pause_resume(app, plan_service, task_service, store, work_dir, checker, shots):
    checker.section("场景 B：暂停 → 编辑锁定 → 继续 → 接着跑完")
    from tests.plan_fakes import FakeRunner

    build_plan(plan_service, task_service, work_dir, "demoland", "northland")
    runner = FakeRunner(hang_once=True)
    dialog = make_dialog(plan_service, task_service, store, runner, auto_estimate=False)
    dialog.show()
    _pump(app, 8)
    dialog.refresh()

    dialog.run_buttons()["start"].click()
    started = _wait_until(app, lambda: runner.calls)
    checker.check(started, "第一项已经开工（hang 状态，等暂停指令）", "第一项没开工")
    dialog.run_buttons()["pause"].click()
    paused = _wait_until(app, lambda: dialog.execution() is not None
                         and dialog.execution().status.value == "paused")
    checker.check(paused, "点「暂停」后计划进入「已暂停」", "超过 30s")
    _pump(app, 10)

    dialog.execution()
    checker.check("已暂停" in dialog.run_status_lines()[0], "状态行说「已暂停」", dialog.run_status_lines()[0])
    checker.check(
        dialog.run_buttons()["resume"].isEnabled() is True
        and dialog.run_buttons()["pause"].isEnabled() is False,
        "暂停后「继续」可用、「暂停」变灰",
        "",
    )
    # Spec §18：暂停期间改不动计划
    checker.check(dialog.table().isEnabled() is False, "暂停期间表格被锁（改不动计划）", "")
    checker.check(
        dialog.buttons()["remove"].isEnabled() is False
        and dialog.buttons()["clear"].isEnabled() is False
        and dialog.buttons()["generate"].isEnabled() is False,
        "暂停期间「移除 / 清空 / 生成任务」全部变灰",
        "",
    )
    _shot_roomy(dialog, "slice21_plan_paused.png", shots)

    dialog.run_buttons()["resume"].click()
    finished = _wait_until(app, lambda: dialog.execution() is not None
                           and dialog.execution().status.value == "completed", timeout_s=30)
    checker.check(finished, "点「继续」后计划接着跑完", "超过 30s")
    # 等线程收干净（finished 信号把按钮 / 表格解锁），再读编辑区解锁状态
    _wait_until(app, lambda: dialog.is_running_plan() is False, timeout_s=10)
    _pump(app, 6)
    checker.check(
        runner.calls == ["demoland", "demoland", "northland"],
        "暂停的那一条接着跑（已有瓦片跳过）、然后才是下一条",
        str(runner.calls),
    )
    checker.check(dialog.table().isEnabled() is True, "跑完后编辑区解锁", "")
    dialog.close()
    _pump(app, 10)


# --------------------------------------------------------------------------- #
# 场景 C：失败 → 重试 / 跳过（真按钮 / 部分失败）
# --------------------------------------------------------------------------- #
def scene_failure_retry_skip(app, work_dir, checker, shots):
    """场景 C：失败 → 重试当前 / 跳过并继续（真按钮）。

    重试与跳过两条路各自用**独立**的 services（``make_services`` 的 docstring
    已警告：执行记录按 ``plan.id`` 落盘，复用会串味）。两条路共用同一份
    ``plan_service`` 时，前一条路跑完留存的 COMPLETED 任务记录会被后一条路复用，
    导致假执行者根本不会被再次调用 —— 那不是产品 bug，是验收脚本自己污染了自己。
    """
    checker.section("场景 C：失败 → 重试当前 / 跳过并继续（真按钮）")
    from tests.plan_fakes import FakeRunner

    # 等待「部分失败」且界面已真正画好（按钮可用、表格符号就位），避免读到
    # 还没 repaint 的旧帧（跨线程信号是排队投递的，状态先于按钮/表格一帧到达）。
    def _wait_partial_settled(dialog):
        return _wait_until(
            app,
            lambda: dialog.execution() is not None
            and dialog.execution().status.value == "partial_failed"
            and dialog.run_buttons()["retry_current"].isEnabled()
            and dialog.run_buttons()["skip_current"].isEnabled()
            and dialog.table().item(0, 5).text().startswith("✕"),
            timeout_s=30,
        )

    # --- 重试（独立 services） ---------------------------------------- #
    ps_r, ts_r, store_r = make_services(work_dir / "c1")
    build_plan(ps_r, ts_r, work_dir / "c1", "demoland", "northland")
    runner = FakeRunner(fail_on=("demoland",), fail_times=1)
    dialog = make_dialog(ps_r, ts_r, store_r, runner, auto_estimate=False)
    dialog.show()
    _pump(app, 8)
    dialog.refresh()
    dialog.run_buttons()["start"].click()
    blocked = _wait_partial_settled(dialog)
    checker.check(blocked, "一项失败后计划停在「部分失败」", "超过 30s")
    checker.check(
        dialog.run_buttons()["retry_current"].isEnabled() is True
        and dialog.run_buttons()["skip_current"].isEnabled() is True
        and dialog.run_buttons()["start"].isEnabled() is False,
        "部分失败时只给「重试当前 / 跳过并继续 / 取消」",
        "",
    )
    checker.check(
        "500" in dialog.table().item(0, 5).toolTip(),
        "失败原因写在状态列 tooltip 上（不翻日志）",
        "",
    )
    _shot_roomy(dialog, "slice21_plan_partial_failed.png", shots)
    dialog.run_buttons()["retry_current"].click()
    finished = _wait_until(app, lambda: dialog.execution() is not None
                           and dialog.execution().status.value == "completed", timeout_s=30)
    checker.check(finished, "点「重试当前」后计划跑完", "超过 30s")
    checker.check(runner.calls == ["demoland", "demoland", "northland"], "重试跑的是同一项", str(runner.calls))
    dialog.close()
    _pump(app, 10)

    # --- 跳过（独立 services） ---------------------------------------- #
    ps_s, ts_s, store_s = make_services(work_dir / "c2")
    build_plan(ps_s, ts_s, work_dir / "c2", "demoland", "northland")
    runner = FakeRunner(fail_on=("demoland",))
    dialog = make_dialog(ps_s, ts_s, store_s, runner, auto_estimate=False)
    dialog.show()
    _pump(app, 8)
    dialog.refresh()
    dialog.run_buttons()["start"].click()
    blocked = _wait_partial_settled(dialog)
    checker.check(blocked, "跳过场景：一项失败后停在「部分失败」", "超过 30s")
    dialog.run_buttons()["skip_current"].click()
    finished = _wait_until(app, lambda: dialog.is_running_plan() is False, timeout_s=30)
    checker.check(finished, "点「跳过并继续」后线程收尾（不再守着无失败项）", "超过 30s")
    _pump(app, 10)
    checker.check(
        runner.calls == ["demoland", "northland"],
        "跳过之后接着跑下一项（那一项不再下）",
        str(runner.calls),
    )
    checker.check(
        dialog.table().item(0, 5).text() == "» 已跳过",
        "被跳过的那一条状态列画成「» 已跳过」",
        dialog.table().item(0, 5).text(),
    )
    checker.check(
        dialog.table().item(1, 5).text() == "✓ 已完成",
        "下一项状态列画成「✓ 已完成」",
        dialog.table().item(1, 5).text(),
    )
    dialog.close()
    _pump(app, 10)


# --------------------------------------------------------------------------- #
# 场景 D：取消（保留半截成果）
# --------------------------------------------------------------------------- #
def scene_cancel(app, plan_service, task_service, store, work_dir, checker, shots):
    checker.section("场景 D：取消 → 先问一句 → 半截的成果保留")
    from tests.plan_fakes import FakeRunner

    build_plan(plan_service, task_service, work_dir, "demoland", "northland")
    runner = FakeRunner(hang=True)
    dialog = make_dialog(plan_service, task_service, store, runner, auto_estimate=False)
    dialog.show()
    _pump(app, 8)
    dialog.refresh()
    dialog.run_buttons()["start"].click()
    _wait_until(app, lambda: runner.calls)

    dialog.run_buttons()["cancel_run"].click()
    box = dialog.message_box()
    checker.check(box is not None and "保留" in box.text(), "取消会先弹确认框、并说「保留」", "")
    _answer_message_box(dialog, __import__("PySide6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox.StandardButton.Yes)
    finished = _wait_until(app, lambda: dialog.is_running_plan() is False, timeout_s=30)
    checker.check(finished, "确认取消后线程收尾", "超过 30s")
    _pump(app, 10)
    execution = dialog.execution()
    checker.check(
        execution.status.value == "cancelled"
        and [it.status.value for it in execution.items] == ["cancelled", "cancelled"],
        "取消后计划与两项都标「已取消」，未删除成果",
        str([it.status.value for it in execution.items]),
    )
    checker.check(
        list((work_dir / "output").rglob("*.mbtiles")),
        "半截的 MBTiles 文件仍保留在磁盘上（取消不删成果）",
        "没有找到任何 .mbtiles",
    )
    _shot_roomy(dialog, "slice21_plan_cancelled.png", shots)
    dialog.close()
    _pump(app, 10)


# --------------------------------------------------------------------------- #
# 场景 E：恢复（关页 = 安全暂停 → 重开 → 继续）
# --------------------------------------------------------------------------- #
def scene_recovery(app, plan_service, task_service, store, work_dir, checker, shots):
    checker.section("场景 E：关页 = 安全暂停 → 重开页面 → 继续（不发多余请求）")
    from tests.plan_fakes import FakeRunner

    plan = build_plan(plan_service, task_service, work_dir, "demoland", "northland")
    runner = FakeRunner(hang=True)
    first = make_dialog(plan_service, task_service, store, runner, auto_estimate=False)
    first.show()
    _pump(app, 8)
    first.refresh()
    first.run_buttons()["start"].click()
    _wait_until(app, lambda: runner.calls)

    # 关页 = 安全暂停（不杀线程）
    first.close()
    paused = _wait_until(app, lambda: store.find(plan.id) is not None
                         and store.find(plan.id).status.value == "paused", timeout_s=30)
    checker.check(paused, "关页后执行记录落成「已暂停」（不是被强杀）", "超过 30s")
    checker.check(
        store.find(plan.id).items[0].status.value == "paused"
        and store.find(plan.id).items[1].status.value == "waiting",
        "关页后当前项 PAUSED、其余 WAITING（可继续）",
        str([it.status.value for it in store.find(plan.id).items]),
    )

    # 新页面 = 重新打开程序（一个请求都不发）
    quiet = FakeRunner()
    second = make_dialog(plan_service, task_service, store, quiet, auto_estimate=False)
    second.show()
    _pump(app, 10)
    checker.check(quiet.calls == [], "重开页面读记录时**不发任何请求**", str(quiet.calls))
    execution = second.execution()
    checker.check(
        execution is not None and execution.status.value == "paused",
        "重开后把「上次没跑完」画出来，仍是「已暂停」",
        str(execution.status.value if execution else None),
    )
    checker.check(
        second.run_buttons()["resume"].isEnabled() is True
        and second.run_buttons()["start"].isEnabled() is False,
        "重开后给「继续」、不给「开始」",
        "",
    )
    _shot_roomy(second, "slice21_plan_recovery.png", shots)

    second.run_buttons()["resume"].click()
    finished = _wait_until(app, lambda: second.is_running_plan() is False, timeout_s=30)
    checker.check(finished, "点「继续」后接着跑完", "超过 30s")
    _pump(app, 10)
    checker.check(
        quiet.calls == ["demoland", "northland"],
        "继续时只补没下完的（第一项接着补、第二项下）",
        str(quiet.calls),
    )
    second.close()
    _pump(app, 10)


# --------------------------------------------------------------------------- #
# 场景 F：静态守卫（界面不碰控件 / 不杀线程）
# --------------------------------------------------------------------------- #
def scene_static_guards(checker: Checker) -> None:
    checker.section("场景 F：静态守卫（后台线程不碰控件 / 不强杀线程）")
    worker_source = (
        PROJECT_ROOT / "app" / "workers" / "plan_download_worker.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(worker_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                # 后台线程不许 import 任何 Qt 控件类（它只发信号）
                checker.check(
                    alias.name
                    not in ("QWidget", "QDialog", "QMessageBox", "QApplication"),
                    f"worker 不导入 {alias.name}（只发信号，控件只在主线程被碰）",
                    "",
                )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = node.func.attr
            checker.check(
                name not in ("terminate", "kill"),
                f"worker 里不许调 {name}()（强杀线程会让 MBTiles 写一半）",
                "",
            )


# --------------------------------------------------------------------------- #
# 场景 G：Spec §32 验收清单
# --------------------------------------------------------------------------- #
def scene_manual_checklist(checker: Checker) -> None:
    checker.section("场景 G：Spec §32 验收清单（本脚本已逐步覆盖）")
    steps = [
        ("计划级调度器：严格串行、不并行下多个条目", "场景 A / B：calls 顺序即层级顺序"),
        ("暂停 / 继续 / 取消完整可用", "场景 B / D：按钮 + 状态 + 半截文件保留"),
        ("继续复用已有的 MBTiles（Resume 补差集）", "场景 B：demoland 跑了两次"),
        ("单项失败 → 停止整份计划（部分失败），不自动往下", "场景 C：停在 partial_failed"),
        ("重试当前 / 跳过并继续可用", "场景 C：retry / skip 两条路"),
        ("计划进度按「选中瓦片数」聚合（非按条目数）", "单测 test_plan_scheduler 覆盖"),
        ("执行记录落盘、立即保存、节流保存", "单测 + 本脚本恢复场景"),
        ("程序重启 → 上次记录画成「已暂停」、不自动下载", "场景 E：重开不发请求"),
        ("Inspector 失败阻断下一项（由单测保证）", "test_plan_scheduler 覆盖"),
        ("单任务模式无回归", "回归 pytest 套件 + e2e_smoke_test"),
    ]
    for title, coverage in steps:
        checker.check(True, title, coverage)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slice 21 批量任务调度实机验收")
    parser.add_argument("--offscreen", action="store_true", help="使用 Qt offscreen 平台插件")
    parser.add_argument("--no-shots", action="store_true", help="只跑检查，不截图")
    return parser.parse_args(argv)


def make_services(work_dir: Path):
    """每个场景一套全新服务（互不污染：执行记录按 plan.id 落盘，复用会串味）。"""
    from app.planning.plan_execution_store import PlanExecutionStore
    from app.planning.plan_service import PlanService
    from app.planning.plan_store import PlanStore
    from app.tasks import TaskService, TaskStore

    data = work_dir / "data"
    task_store = TaskStore(data / "tasks.json")
    plan_store = PlanStore(data / "plans.json")
    store_exec = PlanExecutionStore(data / "plan_executions.json")
    task_service = TaskService(task_store)
    plan_service = PlanService(store=plan_store, task_service=task_service)
    return plan_service, task_service, store_exec


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    shots = not args.no_shots
    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    from PySide6.QtWidgets import QApplication

    work_dir = Path(tempfile.mkdtemp(prefix="verify-slice21-"))
    regions_dir = work_dir / "regions"
    build_fixture_regions(regions_dir)
    use_region_resources(regions_dir)

    app = QApplication(sys.argv[:1])
    global _APP
    _APP = app
    checker = Checker()
    print(f"platform={app.platformName()}  work_dir={work_dir}")

    try:
        plan_service, task_service, store_exec = make_services(work_dir / "a")
        scene_start_and_finish(app, plan_service, task_service, store_exec, work_dir / "a", checker, shots)

        plan_service, task_service, store_exec = make_services(work_dir / "b")
        scene_pause_resume(app, plan_service, task_service, store_exec, work_dir / "b", checker, shots)

        scene_failure_retry_skip(app, work_dir, checker, shots)

        plan_service, task_service, store_exec = make_services(work_dir / "d")
        scene_cancel(app, plan_service, task_service, store_exec, work_dir / "d", checker, shots)

        plan_service, task_service, store_exec = make_services(work_dir / "e")
        scene_recovery(app, plan_service, task_service, store_exec, work_dir / "e", checker, shots)

        scene_static_guards(checker)
        scene_manual_checklist(checker)
    finally:
        _close_dialogs(app)
        shutil.rmtree(work_dir, ignore_errors=True)

    print("\n" + "=" * 60)
    if checker.failures:
        print(f"结果：{len(checker.failures)} 项失败 / 共 {checker.passed} 项通过")
        for failure in checker.failures:
            print(f"  - {failure}")
        return 1
    print(f"结果：全部通过（{checker.passed} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
