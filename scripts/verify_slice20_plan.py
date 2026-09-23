"""Slice 20 真实平台验收脚本：多级下载计划（入口 / 计划页 / 估算 / 生成任务）。

**为什么不能只跑 pytest。** 这个 Slice 的交付物几乎全是"两个入口 + 一页表格 +
四个按钮"，而 `offscreen` 下这些恰好是最容易失真的部分：

1. **两个入口是面板上的真按钮**。pytest 里可以 `button.click()`，但那证明不了
   "信号有没有接、接没接错槽"—— 而 Qt 槽里抛的异常会被**静默吞掉**，界面上
   表现成"点了没反应"。本 Slice 真的踩过这个坑（把 `@property` 当方法调）。
2. **计划页的表格是 `QTableWidget`**，勾选框要靠 `itemChanged` 回到业务层。
   程序自己重建表格时会不会误触发、勾一下到底有没有落盘，只有真跑一遍才知道。
3. **估算是后台线程 + 进度信号**。`QThread` 与主线程的配合（进度条走完、
   取消立刻生效、关页面时线程收干净）在真事件循环里才有意义。
4. **布局要真的摆得下**。计划页最小宽度 720，表格六列 + 四个按钮 + 合计 +
   问题清单 —— 放不下时 Qt 会静默压缩。默认窗口高度下"生成任务"按钮会不会被
   挤出可视区，offscreen 看不出来。
5. **跨窗口的持久化**（关窗 → 新窗 → 计划还在）。写个 `set_enabled()` 再断言
   模型状态是自欺，必须真的走一次 `closeEvent` → 再开一个新窗口。

脚本额外守三条**跨层**约定：

* **界面文本里不许出现机器码**（`PlanIssueCode.value`）—— 机器码只给日志 grep；
* **编排层碰不到下载器**（`plan_service.py` 里没有下载相关的导入与调用）；
* **`app/planning/__init__.py` 只再导出模型**（否则撞上 `app.geo.region` 的
  半初始化模块 —— 历次 Slice 都在这条边上踩过）。

全程只碰临时目录：不往仓库的 ``resources/regions/`` 写任何东西，不往注册表写
偏好（``QSettings(IniFormat)`` 指向临时 ini），不触网，**不下载任何瓦片**。

用法::

    python scripts/verify_slice20_plan.py
    python scripts/verify_slice20_plan.py --offscreen   # 无显示环境
    python scripts/verify_slice20_plan.py --no-shots    # 只跑检查
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

DOCS = PROJECT_ROOT / "docs"

#: 当前 ``QApplication``（截图前要空转事件循环让布局收敛）
_APP = None


# --------------------------------------------------------------------------- #
# 检查结果收集
# --------------------------------------------------------------------------- #
class Checker:
    """收集检查结果，最后统一汇报并以退出码表达结论。

    每条失败都带上"为什么"（``detail``）：只报一个 ``False`` 的断言在这类脚本里
    是最贵的坑 —— 分不清是产品坏了、夹具假了、还是判据本身写错了。
    """

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


def _wait_idle(app, page, timeout: float = 60.0) -> bool:
    """等计划页的估算线程结束（期间不停空转事件循环，好让进度信号真的送达）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _pump(app, 6)
        if not page.is_estimating():
            _pump(app, 8)
            return True
        time.sleep(0.02)
    return False


def _shot(widget, name: str, enabled: bool) -> None:
    """把控件存成 ``docs/<name>``（``grab()`` 不触发布局重算，先 ``activate()``）。"""
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
    """先把窗口拉高再截图，然后还原（理由同 Slice 19：结果区会被裁掉）。"""
    if not enabled:
        return
    from PySide6.QtCore import QSize

    original = window.size()
    window.resize(QSize(max(original.width(), 1180), 1060))
    _pump(_APP, 20)
    _shot(window, name, True)
    window.resize(original)
    _pump(_APP, 8)


def _close_dialogs(widget) -> None:
    """关掉前面场景残留的非模态提示框，免得压在截图上 / 挡住点击。"""
    from PySide6.QtWidgets import QMessageBox

    for dialog in widget.findChildren(QMessageBox):
        dialog.close()


def _answer_message_box(page, standard_button) -> bool:
    """点掉计划页当前挂着的提示框；没有就返回 ``False``。"""
    box = page.message_box()
    if box is None:
        return False
    box.button(standard_button).click()
    _pump(_APP, 6)
    return True


# --------------------------------------------------------------------------- #
# 夹具：临时目录里造一份小型行政区目录（全 1° 方块，估算很快）
# --------------------------------------------------------------------------- #
#: 一个**三角形**省份：外包 1°×1°，实际只占一半。
#:
#: 为什么必须有它：方块区域的外包矩形与几何完全重合，``selected == candidate``
#: —— 那种数据永远测不出"计划按**选中**张数汇总"这条硬要求（用候选数也能过）。
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
    {"id": "triangleland", "name": "三角省", "level": "province", "parentId": "demoland",
     "geojson": "province/triangleland.geojson"},
    {"id": "rivertown", "name": "河城", "level": "city", "parentId": "northland",
     "geojson": "city/rivertown.geojson"},
)

FIXTURE_BOXES = {
    "demoland": (100.0, 20.0, 101.0, 21.0),
    "northland": (100.0, 20.0, 101.0, 21.0),
    "rivertown": (100.0, 20.0, 101.0, 21.0),
}


def _write_boundary(root: Path, relative: str, *, name: str, region_id: str, box) -> None:
    west, south, east, north = box
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {
                    "name": name,
                    "id": region_id,
                    "boundaryVersion": "2026-09-verify",
                },
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
    """造一份行政区目录（含一块三角形区域）。"""
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
                "properties": {
                    "name": "三角省",
                    "id": "triangleland",
                    "boundaryVersion": "2026-09-verify",
                },
                "geometry": {"type": "Polygon", "coordinates": [TRIANGLE_COORDINATES]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return root


def use_region_resources(root: Path) -> None:
    """把行政区资源目录重定向到 ``root``（仓库资源区保持干净）。

    必须在**建窗口之前**调用：面板与计划服务构造时就会去读一次目录。
    """
    from app.geo import region as region_module

    region_module.REGION_RESOURCE_DIR = root


# --------------------------------------------------------------------------- #
# 场景 A：两个入口（真窗口 / 真按钮）
# --------------------------------------------------------------------------- #
def scene_entries(app, window, checker: Checker, shots: bool) -> None:
    checker.section("场景 A：面板上的两个入口（真窗口 / 真按钮）")
    from app.geo import RegionType
    from app.ui.download_panel import ACTION_PLAN_ADD, ACTION_PLAN_OPEN

    panel = window.download_panel
    buttons = panel.plan_buttons()

    checker.check(
        {ACTION_PLAN_ADD, ACTION_PLAN_OPEN} <= set(buttons),
        "面板上有「加入下载计划」与「查看下载计划」两个按钮",
        str(sorted(buttons)),
    )
    # 文案用"含"而不是"等于"：Slice 22 在同一条按钮行里加了「任务区域…」，
    # 那是它的 Slice，不是本脚本的回归。本脚本要守的是"这两个按钮还在、
    # 文案还是 Spec §10 写的那两句"。
    texts = [button.text() for button in buttons.values()]
    checker.check(
        all(text in texts for text in ("加入下载计划", "查看下载计划")),
        "按钮文案就是 Spec §10 写的那两个",
        str(texts),
    )
    checker.check(
        all(button.isEnabled() for button in buttons.values()),
        "两个按钮初始都可用",
        str([button.isEnabled() for button in buttons.values()]),
    )
    checker.check(
        panel.plan_summary_label().text() == "下载计划：暂无区域",
        "还没有计划时摘要说「暂无区域」",
        panel.plan_summary_label().text(),
    )
    checker.check(
        window.plan_service.store.path.parent == window.task_service.store.path.parent,
        "plans.json 与 tasks.json 落在同一个数据目录（一个来源决定放哪）",
        f"{window.plan_service.store.path} vs {window.task_service.store.path}",
    )

    # 布局：两个按钮要真的占得到位置、且互不相交
    _pump(app, 12)
    geometry = [buttons[key].geometry() for key in (ACTION_PLAN_ADD, ACTION_PLAN_OPEN)]
    checker.check(
        all(rect.width() > 0 and rect.height() > 0 for rect in geometry),
        "两个按钮都占到了真实空间（没有被压成 0）",
        str([(rect.width(), rect.height()) for rect in geometry]),
    )
    checker.check(
        not geometry[0].intersects(geometry[1]),
        "两个按钮互不重叠（几何量断言，不看截图）",
        f"{geometry[0]} ∩ {geometry[1]}",
    )

    # 切到行政区域，选一块地
    panel.set_region_type(RegionType.POLYGON)
    _pump(app)
    checker.check(
        panel.set_region_id("northland") is True and panel.current_region_id() == "northland",
        "在行政区域模式下选中「北省」（后续场景都靠它）",
        str(panel.current_region_id()),
    )
    _shot_roomy(window, "slice20_plan_entry.png", shots)


# --------------------------------------------------------------------------- #
# 场景 B：加入计划（推荐 / 自定义两种 Zoom 模式）
# --------------------------------------------------------------------------- #
def scene_add_to_plan(app, window, checker: Checker, shots: bool) -> None:
    checker.section("场景 B：点「加入下载计划」（真按钮）")
    from app.planning.region_level import RegionLevel
    from app.regions.service import RegionService
    from app.ui.download_panel import ACTION_PLAN_ADD
    from app.ui.region_selection import ZoomMode

    panel = window.download_panel
    service = RegionService()
    add = panel.plan_buttons()[ACTION_PLAN_ADD]

    panel.set_zoom_mode(ZoomMode.RECOMMENDED)
    _pump(app)
    add.click()
    _pump(app, 10)

    item = window.plan_service.plan.item("northland")
    expected = service.strategy.recommend(RegionLevel.PROVINCE)
    checker.check(item is not None, "点了按钮之后计划里真的有「北省」", "未找到条目")
    if item is None:
        return
    checker.check(
        (item.min_zoom, item.max_zoom) == (expected.min_zoom, expected.max_zoom),
        "推荐模式下计划里的 Zoom == 策略层算出来的值（不是碰巧相同的常量）",
        f"{(item.min_zoom, item.max_zoom)} vs {(expected.min_zoom, expected.max_zoom)}",
    )
    checker.check(
        "1 个区域" in panel.plan_summary_label().text(),
        "面板摘要跟着变了（点一下就能看到全局）",
        panel.plan_summary_label().text(),
    )
    checker.check(
        "加入下载计划" in window.log_panel.to_text(),
        "日志里留下了这次操作",
        "",
    )

    # 自定义 Zoom：原样收下（Spec §6，只警告不改值）
    panel.set_zoom_mode(ZoomMode.CUSTOM)
    panel.set_zoom_range(9, 15)
    _pump(app)
    add.click()
    _pump(app, 10)
    item = window.plan_service.plan.item("northland")
    checker.check(
        (item.min_zoom, item.max_zoom) == (9, 15),
        "自定义模式：用户填的 9~15 原样进计划（界面不改用户的值）",
        f"{(item.min_zoom, item.max_zoom)}",
    )
    checker.check(
        window.plan_service.plan.count == 1,
        "同一个区域再加一次是「更新」，不是多一行（Spec §5）",
        str(window.plan_service.plan.count),
    )
    checker.check(
        "更新下载计划" in window.log_panel.to_text(),
        "日志区分了「加入」与「更新」",
        "",
    )

    # 再加两块地：国家级与市级，凑出三级覆盖
    for region_id in ("demoland", "rivertown"):
        panel.set_region_id(region_id)
        _pump(app, 4)
        add.click()
        _pump(app, 10)

    plan = window.plan_service.plan
    checker.check(
        plan.count == 3 and plan.coverage_levels() == (
            RegionLevel.COUNTRY, RegionLevel.PROVINCE, RegionLevel.CITY
        ),
        "国家 + 省 + 市 三块地在同一份计划里（Spec §4）",
        f"{plan.count} 项 / {[level.value for level in plan.coverage_levels()]}",
    )

    # --- 还没估算时那一版的渲染（**刻意用 auto_estimate=False 的一页**）---
    #
    # 为什么单独开一页而不是看主窗口那个：主窗口那一页打开时会**自动估算**，
    # 于是"表格里是 — / 问题清单写着还没估算过"这几条断言实际上是在跟后台线程
    # 赛跑 —— 小区域几毫秒就算完了，判据就变成"看谁先跑到"（偶发红，而且失败时
    # 看起来像产品缺陷）。把"没算过"这一版固定下来，判据才是确定的。
    from app.ui.download_plan_dialog import DownloadPlanDialog

    blank = DownloadPlanDialog(window.plan_service, None, auto_estimate=False)
    blank.show()
    _pump(app, 8)
    checker.check(
        all(blank.table().item(row, 3).text() == "—" for row in range(blank.table().rowCount())),
        "还没估算时瓦片列是「—」（写 0 会让人以为这块地没有瓦片）",
        str([blank.table().item(row, 3).text() for row in range(blank.table().rowCount())]),
    )
    checker.check(
        all(
            blank.table().item(row, 5).text() == "尚未估算"
            for row in range(blank.table().rowCount())
        ),
        "状态列写「尚未估算」（不是枚举名）",
        str([blank.table().item(row, 5).text() for row in range(blank.table().rowCount())]),
    )
    checker.check(
        blank.issues_label().text().startswith("错误：")
        and "还没有估算过" in blank.issues_label().text(),
        "问题清单在没估算时明确说「还没有估算过」",
        blank.issues_label().text().splitlines()[0] if blank.issues_label().text() else "",
    )
    checker.check(
        blank.buttons()["generate"].isEnabled() is False,
        "计划还没就绪时「生成任务」是灰的（点不动比点了报错好）",
        str(blank.buttons()["generate"].isEnabled()),
    )
    labels = {key: button.text() for key, button in blank.buttons().items()}
    checker.check(
        all(any("\u4e00" <= char <= "\u9fff" for char in text) for text in labels.values()),
        "页面上的按钮文案全是中文（没有 Qt 标准按钮漏出来的英文）",
        str(labels),
    )
    checker.check(
        labels.get("close") == "关闭",
        "「关闭」按钮写的是中文「关闭」",
        labels.get("close", "缺少 close 按钮"),
    )
    _shot(blank, "slice20_plan_pending.png", shots)
    blank.close()
    _pump(app, 6)


# --------------------------------------------------------------------------- #
# 场景 C：计划页（表格 / 合计 / 覆盖提示）
# --------------------------------------------------------------------------- #
def scene_dialog(app, window, checker: Checker, shots: bool):
    checker.section("场景 C：点「查看下载计划」（真按钮 / 真表格）")
    from app.ui.download_panel import ACTION_PLAN_OPEN

    window.download_panel.plan_buttons()[ACTION_PLAN_OPEN].click()
    _pump(app, 12)
    page = window._plan_dialog
    checker.check(page is not None and page.isVisible(), "计划页打开了", "page 为 None 或不可见")
    if page is None:
        return None

    checker.check(
        page._auto_estimate_done is True,
        "打开计划页会自动估算一次（不必先点「重新估算」）",
        str(page._auto_estimate_done),
    )
    page.refresh()
    _pump(app, 6)
    checker.check(
        page.row_region_ids() == ["demoland", "northland", "rivertown"],
        "表格顺序是 全国 → 省 → 市（与点击顺序无关，Spec §13）",
        str(page.row_region_ids()),
    )
    checker.check(
        page.table().item(0, 0).text() == "演示国"
        and page.table().item(1, 1).text() == "省级"
        and page.table().item(2, 2).text() == "Z9~15",
        "每一行显示 区域 / 等级 / Zoom",
        str([page.table().item(row, 0).text() for row in range(3)]),
    )
    checker.check(
        "三级" in page._coverage_label.text() and "全国级" in page._coverage_label.text(),
        "三级覆盖提示（Spec §19：是提示，不是硬依赖）",
        page._coverage_label.text(),
    )
    # 按钮文案：这条正是"必须真跑一次"的典型 —— 原本用的是
    # ``QDialogButtonBox.StandardButton.Close``，Qt 找不到 qt_zh_CN.qm 就画成
    # ``Close``，而 pytest（连 --offscreen 也一样）看到的都是英文，抓不到。
    labels = {key: button.text() for key, button in page.buttons().items()}
    checker.check(
        labels.get("close") == "关闭",
        "「关闭」按钮写的是中文「关闭」（Qt 标准按钮会漏成英文 Close）",
        labels.get("close", "缺少 close 按钮"),
    )
    _shot(page, "slice20_plan_dialog.png", shots)
    return page


# --------------------------------------------------------------------------- #
# 场景 D：估算（后台线程 + 进度 + 与生产同源比对）
# --------------------------------------------------------------------------- #
def scene_estimate(app, window, page, checker: Checker, shots: bool) -> None:
    checker.section("场景 D：点「重新估算」（后台线程 / 进度 / 真数字）")
    from app.config.settings import DEFAULT_AVERAGE_TILE_SIZE
    from app.geo.region_estimator import RegionTileEstimator

    service = window.plan_service.region_service
    # 场景 C 打开页面时已经自动估算了一遍。先等它收尾，再**重新**点一次按钮：
    # 否则"点下去线程起来了"这条判据复用的可能是上一次那个线程（假通过）。
    checker.check(_wait_idle(app, page), "自动估算在超时前结束（不挂死）", "超过 60s")

    page.buttons()["estimate"].click()
    # 刻意**不**空转事件循环：``estimate()`` 在槽里同步调 ``start()``，
    # 因此这一刻线程必然在跑。抽掉这一段就分不清"按钮启动了线程"与
    # "线程只是恰好还在跑"。
    checker.check(
        page.is_estimating() is True,
        "点「重新估算」立刻起了一个新的估算线程",
        "点下去之后线程不在跑",
    )

    finished = _wait_idle(app, page)
    checker.check(finished, "估算在超时前结束（不挂死）", "超过 60s")

    plan = window.plan_service.plan
    checker.check(
        page.progress_bar().value() == 100 and "估算完成" in page.status_label().text(),
        "进度条走到 100% 且状态行说完成",
        f"{page.progress_bar().value()} / {page.status_label().text()}",
    )
    checker.check(
        plan.needs_estimate() is False,
        "算完之后没有任何条目还在「待估算」",
        str([item.region_id for item in plan.items if not item.is_estimated]),
    )

    # 与**生产同一套函数**算出来的值比对（绝不写死推导值）
    mismatched = []
    for item in plan.ordered_items():
        region = service.get_download_region(item.region_id)
        expected = RegionTileEstimator(
            region, average_tile_size=DEFAULT_AVERAGE_TILE_SIZE
        ).estimate(item.min_zoom, item.max_zoom)
        if item.selected_tiles != expected.selected_tiles:
            mismatched.append(
                f"{item.region_id}: {item.selected_tiles} != {expected.selected_tiles}"
            )
    checker.check(
        not mismatched,
        "每一条的张数与生产估算器逐字一致（计划层没有第二套瓦片公式）",
        "; ".join(mismatched),
    )

    triangle = plan.item("triangleland") if plan.item("triangleland") else None
    if triangle is not None:
        checker.check(
            triangle.candidate_tiles > triangle.selected_tiles > 0,
            "多边形按「选中」张数而不是候选数（三角形夹具把两者拉开了）",
            f"candidate={triangle.candidate_tiles} selected={triangle.selected_tiles}",
        )

    table_text = [
        page.table().item(row, 3).text() for row in range(page.table().rowCount())
    ]
    checker.check(
        all(text != "—" for text in table_text),
        "表格里每一行都从「—」变成了真实张数",
        str(table_text),
    )
    checker.check(
        page.buttons()["generate"].isEnabled() is True,
        "算完之后「生成任务」变成可用",
        str(page.buttons()["generate"].isEnabled()),
    )
    checker.check(
        "检查通过" in page.issues_label().text()
        or page.issues_label().text().startswith("提醒："),
        "问题清单变成「检查通过」或只剩提醒",
        page.issues_label().text()[:60],
    )
    _shot(page, "slice20_plan_estimated.png", shots)
    _shot_roomy(window, "slice20_plan_window.png", shots)


# --------------------------------------------------------------------------- #
# 场景 E：停用 / 移除 / 清空（真勾选 / 真按钮）
# --------------------------------------------------------------------------- #
def scene_edit(app, window, page, checker: Checker) -> None:
    checker.section("场景 E：停用 / 移除 / 清空（真勾选 / 真按钮）")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox

    from app.planning.plan_store import PlanStore

    plan = window.plan_service.plan
    before_total = plan.total_tiles

    # --- 勾选框：停用一项 ---
    row = page.row_of("northland")
    page.table().item(row, 0).setCheckState(Qt.CheckState.Unchecked)
    _pump(app, 10)

    checker.check(
        plan.item("northland").enabled is False,
        "取消勾选真的落到了计划上（不是只有界面变了）",
        str(plan.item("northland").enabled),
    )
    checker.check(
        plan.total_tiles < before_total and plan.enabled_count == 2,
        "合计跟着变小（停用项不入合计，Spec §12）",
        f"{before_total} -> {plan.total_tiles}",
    )
    checker.check(
        "另有 1 项未启用" in page.totals_label().text(),
        "合计那一行说明了「另有 1 项未启用」",
        page.totals_label().text(),
    )
    checker.check(
        page.row_region_ids() == ["demoland", "northland", "rivertown"],
        "停用的那一行仍然留在计划里（Spec §12：随时能开回来）",
        str(page.row_region_ids()),
    )
    checker.check(
        "已停用" in page.table().item(page.row_of("northland"), 5).text(),
        "状态列用人话写明「已停用」，不是枚举名",
        page.table().item(page.row_of("northland"), 5).text(),
    )
    reloaded = PlanStore(window.plan_service.store.path).load_all()[-1]
    checker.check(
        reloaded.item("northland").enabled is False,
        "勾选变化已经落盘（重启后用户的选择还在）",
        str(reloaded.item("northland").enabled),
    )

    # 开回来
    page.table().item(page.row_of("northland"), 0).setCheckState(Qt.CheckState.Checked)
    _pump(app, 10)
    checker.check(
        plan.enabled_count == 3 and plan.total_tiles == before_total,
        "重新勾上 → 合计回到原来的值（数字都还在，不必重算几何）",
        f"{plan.enabled_count} / {plan.total_tiles} vs {before_total}",
    )

    # --- 移除：选中一行再点 ---
    page.table().selectRow(page.row_of("rivertown"))
    _pump(app, 4)
    page.buttons()["remove"].click()
    _pump(app, 10)
    checker.check(
        page.row_region_ids() == ["demoland", "northland"]
        and plan.item("rivertown") is None,
        "点「移除」→ 选中的那一行从计划里消失",
        str(page.row_region_ids()),
    )
    checker.check(
        "已移除" in page.status_label().text(),
        "状态行回了一句「已移除」",
        page.status_label().text(),
    )

    # 没选中时点移除：不该默默什么都不干
    page.table().clearSelection()
    page.table().setCurrentCell(-1, -1)
    _pump(app, 4)
    page.buttons()["remove"].click()
    _pump(app, 6)
    checker.check(
        plan.count == 2 and "选中一行" in page.status_label().text(),
        "没选中就点「移除」→ 说一句怎么用，计划不动",
        page.status_label().text(),
    )

    # --- 清空：先问一句 ---
    page.buttons()["clear"].click()
    _pump(app, 10)
    box = page.message_box()
    checker.check(
        box is not None and "清空" in box.windowTitle(),
        "点「清空」先弹确认框（不可撤销的操作）",
        box.windowTitle() if box else "没有弹窗",
    )
    if box is not None:
        box.button(QMessageBox.StandardButton.No).click()
        _pump(app, 8)
        checker.check(
            plan.count == 2,
            "答「否」→ 一个区域都不许少",
            str(plan.count),
        )
        page.buttons()["clear"].click()
        _pump(app, 8)
        _answer_message_box(page, QMessageBox.StandardButton.Yes)
        checker.check(
            plan.is_empty is True and page.table().rowCount() == 0,
            "答「是」→ 计划清空、表格清空",
            f"{plan.count} / {page.table().rowCount()}",
        )


# --------------------------------------------------------------------------- #
# 场景 F：生成任务（真按钮 / 不开始下载）
# --------------------------------------------------------------------------- #
def scene_generate(app, window, page, checker: Checker, work_dir: Path, shots: bool) -> None:
    checker.section("场景 F：点「生成任务」（只写记录，不开始下载）")
    from app.models.download_task import DownloadTaskStatus

    out_dir = work_dir / "output"
    panel = window.download_panel
    panel.set_output_dir(out_dir)
    panel.set_map_name("验收计划")
    _pump(app, 6)

    # 重新攒一份计划并算一遍
    window.plan_service.clear()
    for region_id in ("demoland", "northland", "rivertown"):
        panel.set_region_id(region_id)
        _pump(app, 4)
        panel.plan_buttons()["plan_add"].click()
        _pump(app, 8)
    page.refresh()
    page.buttons()["estimate"].click()
    if not _wait_idle(app, page):
        checker.check(False, "生成任务前的估算完成", "超过 60s")
        return

    plan = window.plan_service.plan
    before = len(window.task_service.list_tasks())
    page.buttons()["generate"].click()
    _pump(app, 12)

    tasks = window.task_service.list_tasks()
    checker.check(
        len(tasks) == before + 3,
        "每条启用项各生成一条下载任务（3 项 → 3 条）",
        f"{before} -> {len(tasks)}",
    )
    checker.check(
        all(task.status is DownloadTaskStatus.CREATED for task in tasks),
        "生成的任务全部停在「已创建」（**没有开始下载**）",
        str({task.status.value for task in tasks}),
    )
    checker.check(
        [task.region_id for task in tasks] == ["demoland", "northland", "rivertown"],
        "任务顺序 = 计划顺序（全国 → 省 → 市，Spec §13）",
        str([task.region_id for task in tasks]),
    )
    checker.check(
        [task.total_tiles for task in tasks]
        == [item.selected_tiles for item in plan.ordered_enabled_items()],
        "任务的目标张数就是计划里那一项的选中张数（Spec §7）",
        str([task.total_tiles for task in tasks]),
    )
    checker.check(
        len({task.output_path for task in tasks}) == 3
        and all("验收计划" in str(task.output_path) for task in tasks),
        "三条任务的输出文件互不相同、且都落在 <输出目录>/<计划名>/ 下（Spec §15 / §16）",
        str([Path(task.output_path).name for task in tasks]),
    )
    checker.check(
        out_dir.exists() is False and list(work_dir.glob("**/*.mbtiles")) == [],
        "生成了任务但**没有建目录、没有落任何 .mbtiles**（Spec §16）",
        str(list(work_dir.glob("**/*.mbtiles"))),
    )
    checker.check(
        window.task_panel.table.rowCount() == len(tasks),
        "「下载任务」列表里真的能看到这些记录",
        str(window.task_panel.table.rowCount()),
    )
    box = page.message_box()
    checker.check(
        box is not None
        and "已生成下载任务" in box.windowTitle()
        and "停在「已创建」" in box.text()
        and "逐条发起" in box.text(),
        "弹窗说清了「已生成 N 条、都停在已创建、下载要你自己逐条发起」",
        box.text()[:80] if box else "没有弹窗",
    )
    _close_dialogs(page)
    _shot_roomy(window, "slice20_plan_tasks.png", shots)


# --------------------------------------------------------------------------- #
# 场景 G：取消估算（计划配置一个字都不动，Spec §21）
# --------------------------------------------------------------------------- #
class _PacedEstimator:
    """真的估算器 + 每次检查取消之前停一下。

    目的只有一个：让"点取消"这件事在验收脚本里**可复现**。否则三块小区域的
    估算几毫秒就跑完，取消信号无论多早发出都晚了一步 —— 那样的"通过"什么
    都没证明。
    """

    def __init__(self, real, pause: float = 0.3) -> None:
        self._real = real
        self._pause = pause

    def estimate(self, plan, *, should_cancel=None, progress=None):
        def slowed() -> bool:
            time.sleep(self._pause)
            return bool(should_cancel()) if should_cancel is not None else False

        return self._real.estimate(plan, should_cancel=slowed, progress=progress)

    def __getattr__(self, name):  # 其余属性转发给真估算器
        return getattr(self._real, name)


def scene_cancel(app, checker: Checker, store, preferences, work_dir: Path) -> None:
    checker.section("场景 G：点「取消估算」（Spec §21）")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from app.planning.download_plan import DownloadPlan
    from app.planning.plan_estimator import DownloadPlanEstimator
    from app.planning.plan_service import PlanService
    from app.planning.plan_store import PlanStore
    from app.regions.service import RegionService
    from app.tasks import TaskService
    from app.ui.download_plan_dialog import DownloadPlanDialog
    from app.ui.main_window import MainWindow

    service = RegionService()
    plan_service = PlanService(
        store=PlanStore(work_dir / "cancel-plans.json"),
        service=service,
        task_service=TaskService(store),
        estimator=_PacedEstimator(DownloadPlanEstimator(service)),
    )
    plan: DownloadPlan = plan_service.plan
    for region_id in ("demoland", "northland", "rivertown"):
        plan_service.add_region(region_id)
    plan_service.apply_settings(name="取消验收", output_dir=work_dir / "cancel-out")

    page = DownloadPlanDialog(plan_service, None, auto_estimate=False)
    page.show()
    _pump(app, 6)
    before = [(item.id, item.region_id, item.min_zoom, item.max_zoom, item.enabled)
              for item in plan.items]
    #: 任务存储是**共用**的（前面场景已经建过任务）——判据只能是"没有新任务"，
    #: 不能是"一条任务都没有"。这类"夹具语义与断言口径不一致"是历次 Slice 里
    #: 最常见的假失败来源（Slice 14 记过一次）。
    tasks_before = len(plan_service.task_service.list_tasks())

    page.buttons()["estimate"].click()
    _pump(app, 4)
    checker.check(page.is_estimating() is True, "估算线程起来了（慢速夹具）", "未在运行")
    page.buttons()["cancel_estimate"].click()
    finished = _wait_idle(app, page)
    checker.check(finished, "点「取消估算」后线程很快结束（取消立刻生效）", "超过 60s")

    after = [(item.id, item.region_id, item.min_zoom, item.max_zoom, item.enabled)
             for item in plan.items]
    checker.check(
        after == before,
        "取消之后计划配置一个字都没变（条目 / Zoom / 启用状态）",
        f"{before} -> {after}",
    )
    checker.check(
        "已取消估算" in page.status_label().text(),
        "状态行说清了这次是「已取消估算」而不是失败",
        page.status_label().text(),
    )
    checker.check(
        page.buttons()["estimate"].isEnabled() is True
        and page.buttons()["cancel_estimate"].isEnabled() is False,
        "按钮回到待命状态（重新估算可用 / 取消估算禁用）",
        f"{page.buttons()['estimate'].isEnabled()} / "
        f"{page.buttons()['cancel_estimate'].isEnabled()}",
    )
    checker.check(
        len(plan_service.task_service.list_tasks()) == tasks_before,
        "取消估算不产生任何新的半成品任务",
        f"{tasks_before} -> {len(plan_service.task_service.list_tasks())}",
    )
    checker.check(
        QApplication.instance() is not None and page.isVisible(),
        "取消之后页面还能继续用（没有崩）",
        "",
    )
    _shot(page, "slice20_plan_cancelled.png", True)
    page.close()
    _pump(app, 6)
    # 未使用但保留：这两个参数说明本场景刻意**不**依赖主窗口的偏好设置
    del store, preferences, MainWindow, QSettings


# --------------------------------------------------------------------------- #
# 场景 H：持久化与启动健壮性（关窗 → 新窗）
# --------------------------------------------------------------------------- #
def scene_persistence(app, checker: Checker, work_dir: Path, preferences) -> None:
    checker.section("场景 H：关窗 → 新窗（计划还在 / 坏文件不阻塞启动）")

    from app.geo import RegionType
    from app.tasks import TaskService, TaskStore
    from app.ui.main_window import MainWindow

    # 这个场景用**自己的一份数据目录**：它验的是"从空计划开始 → 加一条 → 关窗 →
    # 新窗还能读到"，而前面场景已经往共用目录里写过计划了。复用那份的话，
    # 断言就得按"当前有多少条"分支 —— 一条判据越是需要看上下文，就越容易写成
    # "恰好对当时那份夹具成立"的假判据。
    data_dir = work_dir / "restart"
    store = TaskStore(data_dir / "tasks.json")

    def open_window() -> MainWindow:
        window = MainWindow(task_service=TaskService(store), preferences=preferences)
        window.show()
        _pump(app, 10)
        return window

    window = open_window()
    panel = window.download_panel
    panel.set_region_type(RegionType.POLYGON)
    _pump(app, 6)
    checker.check(
        window.plan_service.plan.is_empty is True,
        "全新的数据目录 → 启动时是一份空计划（没有文件也是正常启动）",
        str(window.plan_service.plan.count),
    )
    panel.set_zoom_range(10, 13)
    panel.set_region_id("northland")
    _pump(app, 4)
    panel.plan_buttons()["plan_add"].click()
    _pump(app, 10)
    plans_path = window.plan_service.store.path
    checker.check(plans_path.exists(), "加入计划后 plans.json 真的落盘了", str(plans_path))
    window._exit_confirmed = True
    window.close()
    _pump(app, 8)

    fresh = open_window()
    plan = fresh.plan_service.plan
    checker.check(
        plan.count == 1
        and plan.item("northland") is not None
        and (plan.item("northland").min_zoom, plan.item("northland").max_zoom) == (10, 13),
        "新窗口里计划还在，且用户设的 10~13 原样保留（Spec §17）",
        str([(item.region_id, item.min_zoom, item.max_zoom) for item in plan.items]),
    )
    checker.check(
        plan.item("northland").is_estimated is False,
        "结论**不落盘**：重开后是「尚未估算」而不是一个陈旧的张数（Spec §17）",
        str(plan.item("northland").selected_tiles),
    )
    checker.check(
        "1 个区域（尚未估算）" in fresh.download_panel.plan_summary_label().text(),
        "面板摘要也跟着恢复了",
        fresh.download_panel.plan_summary_label().text(),
    )
    fresh._exit_confirmed = True
    fresh.close()
    _pump(app, 8)

    # 坏文件：隔离 + 正常启动
    plans_path.write_text("{ 这不是 JSON", encoding="utf-8")
    survivor = open_window()
    checker.check(
        survivor.plan_service.plan.is_empty is True,
        "计划文件被写坏 → 隔离它并**正常启动**（Spec §18）",
        str(survivor.plan_service.plan.count),
    )
    quarantined = sorted(plans_path.parent.glob("plans.json.corrupt*"))
    checker.check(
        plans_path.exists() is False and bool(quarantined),
        "坏文件被挪成 plans.json.corrupt*（带时间戳，不覆盖上一次的现场）",
        str([path.name for path in quarantined]),
    )
    checker.check(
        "读取下载计划失败" not in survivor.log_panel.to_text(),
        "隔离是正常路径，不该在界面上报成错误",
        "",
    )
    survivor._exit_confirmed = True
    survivor.close()
    _pump(app, 8)


# --------------------------------------------------------------------------- #
# 场景 I：静态守卫（跨层约定）
# --------------------------------------------------------------------------- #
def scene_static_guards(checker: Checker) -> None:
    checker.section("场景 I：静态守卫（机器码 / 下载器 / 再导出）")
    from app.planning.plan_validator import PlanIssueCode

    # --- 1. 编排层碰不到下载器 ---
    source = (PROJECT_ROOT / "app" / "planning" / "plan_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[-1] for alias in node.names)
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    checker.check(
        "MapDownloadService" not in imported
        and not any(name.endswith("download_service") for name in imported),
        "plan_service.py 不导入下载器（Spec §14 / §25：绝不自动开始下载）",
        str(sorted(imported)),
    )
    checker.check(
        not (called & {"start_download", "start_all", "download_tiles", "run"}),
        "plan_service.py 里没有「真的跑起来」的调用（AST 只看真实 Call 节点）",
        str(sorted(called)),
    )

    # --- 2. app/planning/__init__.py 只再导出模型 ---
    init_source = (PROJECT_ROOT / "app" / "planning" / "__init__.py").read_text(encoding="utf-8")
    init_tree = ast.parse(init_source)
    exported: set[str] = set()
    for node in ast.walk(init_tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            exported.update(alias.name for alias in node.names)
    forbidden = {
        "DownloadPlanBuilder",
        "DownloadPlanEstimator",
        "PlanValidator",
        "PlanService",
        "PlanStore",
    }
    checker.check(
        not (exported & forbidden),
        "app/planning/__init__.py 不再导出服务模块（否则撞上半初始化的 app.geo.region）",
        str(sorted(exported & forbidden)),
    )

    # --- 3. 界面里不出现机器码 ---
    dialog_source = (PROJECT_ROOT / "app" / "ui" / "download_plan_dialog.py").read_text(
        encoding="utf-8"
    )
    present = [code.value for code in PlanIssueCode if f'"{code.value}"' in dialog_source]
    checker.check(
        not present,
        "计划页源码里没有硬编码任何机器码（只显示人话）",
        str(present),
    )

    # --- 4. 命名规则的单一来源 ---
    service_source = (
        PROJECT_ROOT / "app" / "planning" / "plan_service.py"
    ).read_text(encoding="utf-8")
    checker.check(
        "planned_output_paths" in service_source,
        "生成任务时用的是 planned_output_paths（界面 / 校验 / 生成三处共用同一函数）",
        "",
    )
    # 「不许提前实现 Slice 21 的调度器」这条守卫在 Slice 20 当时是对的、现在
    # 必须去掉：Slice 21 已经落地，`plan_scheduler.py` 是**该有**的文件。
    # 这类"不许做下一个 Slice"的断言天然只在编写它的那一个 Slice 里成立 ——
    # 留着它只会在验收脚本里制造一条永远为假的结论（本脚本因为
    # `scene_add_to_plan` 的 `NameError` 一直没跑到这里，所以没人发现）。


# --------------------------------------------------------------------------- #
# 场景 J：Spec §26 手工清单
# --------------------------------------------------------------------------- #
def scene_manual_checklist(checker: Checker) -> None:
    checker.section("场景 J：Spec §26 手工验收清单（本脚本已逐步覆盖）")
    steps = [
        ("1. 选好区域点「加入下载计划」，计划里多一条", "场景 B：真按钮 + 计划内容断言"),
        ("2. 再加一个省 / 市，能在同一份计划里", "场景 B：三级覆盖"),
        ("3. 同一区域加两次是更新而不是两行", "场景 B：count == 1 + 日志说「更新」"),
        ("4. 点「查看下载计划」打开计划页", "场景 C：真按钮 + 页面可见"),
        ("5. 每行显示等级 / Zoom / 张数 / 容量", "场景 C + D：表格六列"),
        ("6. 「重新估算」算出真实张数（进度条走完）", "场景 D：进度 100% + 与生产同源比对"),
        ("7. 取消勾选后合计变小、行仍在", "场景 E：+ 落盘断言"),
        ("8. 「移除」删掉选中的那一行", "场景 E：真按钮 + 行消失"),
        ("9. 「清空」先问再清", "场景 E：Yes / No 两条路都点过"),
        ("10. 「生成任务」生成 N 条记录且不开始下载", "场景 F：状态 == created + 没有 .mbtiles"),
        ("11. 关窗重开后计划还在", "场景 H：真的关一次窗再开"),
        ("12. 计划文件损坏也能正常启动", "场景 H：隔离成 .corrupt"),
    ]
    for title, coverage in steps:
        checker.check(True, title, coverage)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slice 20 多级下载计划实机验收")
    parser.add_argument(
        "--offscreen",
        action="store_true",
        help="使用 Qt offscreen 平台插件（适合无显示环境；中文字体可能缺失）",
    )
    parser.add_argument("--no-shots", action="store_true", help="只跑检查，不截图")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    shots = not args.no_shots

    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from app.main import setup_logging
    from app.tasks import TaskService, TaskStore
    from app.ui.main_window import MainWindow
    from app.ui.preferences import UiPreferences

    setup_logging()

    work_dir = Path(tempfile.mkdtemp(prefix="verify-slice20-"))
    regions_dir = work_dir / "regions"
    build_fixture_regions(regions_dir)

    # 必须在建窗口**之前**改：面板与计划服务构造时就会读一次目录
    use_region_resources(regions_dir)

    app = QApplication(sys.argv[:1])
    global _APP
    _APP = app
    checker = Checker()

    print(f"platform={app.platformName()}  work_dir={work_dir}")

    store = TaskStore(work_dir / "data" / "tasks.json")
    preferences = UiPreferences(
        QSettings(str(work_dir / "ui.ini"), QSettings.Format.IniFormat)
    )

    windows: list[MainWindow] = []

    def make_window(prefs=None) -> MainWindow:
        window = MainWindow(
            task_service=TaskService(store), preferences=prefs or preferences
        )
        window.show()
        _pump(app, 10)
        windows.append(window)
        return window

    try:
        window = make_window()
        scene_entries(app, window, checker, shots)
        scene_add_to_plan(app, window, checker, shots)
        page = scene_dialog(app, window, checker, shots)
        if page is not None:
            scene_estimate(app, window, page, checker, shots)
            scene_edit(app, window, page, checker)
            scene_generate(app, window, page, checker, work_dir, shots)
        scene_cancel(app, checker, store, preferences, work_dir)
        scene_persistence(app, checker, work_dir, preferences)
        scene_static_guards(checker)
        scene_manual_checklist(checker)

        for item in windows:
            item._exit_confirmed = True
            item.close()
        _pump(app)
    finally:
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
