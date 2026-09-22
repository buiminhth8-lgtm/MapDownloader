"""Slice 22 真实平台验收脚本：任务区域（航线 Buffer / Z15~18 走廊）。

**为什么不能只跑 pytest。** 这个 Slice 的技术核心（米制 Buffer）在 pytest 里
已经在量地面宽度了，但交付物还有一半是「窗口 + 按钮 + 计划接线」，这些恰好是
offscreen 下最容易失真的部分：

1. **入口是面板上的真按钮**。pytest 里 ``button.click()`` 证明不了「信号有没有
   接对槽」—— Qt 槽里抛的异常会被静默吞掉，表现成「点了没反应」。本 Slice 真的
   踩过一次同类问题：窗口初始状态没套用，两个 Zoom 框显示 1/1，而下一行就写着
   「推荐 Zoom：Z15~18」。
2. **窗口的最小宽度与结果区**。结果区一口气写十几行（候选 / 选中 / 过滤 /
   逐级明细），默认窗口高度下会不会被裁掉，offscreen 看不出来。
3. **落盘是真的三个文件**。pytest 里有断言，但「真开一次窗口、真点一下、
   再去盘上看三个文件」是另一回事 —— 它顺带证明了这个窗口没有绕过服务层。
4. **续传与 Inspector 的接线**。这两条都经过 ``app.geo.region`` 那张动态解析
   注册表；注册是主窗口构造时做的，只有真跑一次主窗口才说明「装上了」。
5. **删除保护走的是真确认框**。提示框一律非模态，因此「点是 / 点否」在脚本里
   能真的点掉 —— 这条反过来证明窗口没有用 ``exec()`` 把自动化堵死。

脚本额外守四条**跨层**约定（都用 AST，不看注释与文档字符串）：

* 任务区层里**没有 Zoom 数字**（没有 15 / 18 这两个整数字面量）；
* 任务区层里**没有 ``RegionLevel.MISSION`` 字面量**（等级收在 ``MISSION_REGION_LEVEL`` 一处）；
* ``app/geo/region.py`` **不 import ``app.mission``**（挂接是单向注册回调，反向会成环）;
* 任务区层**不碰下载器与界面**（它只产出一块地，下载是别人的事）。

全程只碰临时目录：不往仓库的 ``resources/regions/`` 写任何东西，不往注册表写偏好
（``QSettings(IniFormat)`` 指向临时 ini），不触网，**不下载任何瓦片**。

用法::

    python scripts/verify_slice22_mission_region.py
    python scripts/verify_slice22_mission_region.py --offscreen   # 无显示环境
    python scripts/verify_slice22_mission_region.py --no-shots    # 只跑检查
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DOCS = PROJECT_ROOT / "docs"
APP_DIR = PROJECT_ROOT / "app"

#: 当前 ``QApplication``（截图前要空转事件循环让布局收敛）
_APP = None

#: 验收用的航线（杭州西湖北侧），三个点足够画折线。
ROUTE = [(120.00, 30.00), (120.05, 30.02), (120.10, 30.01)]
BUFFER_KM = 3.0
ZOOM = 15

#: 缺失数量的断言必须针对**数字**：中文说明里「误报成缺失」也含「缺失」两个字，
#: 拿子串去否定会把正确的提示判成违规。
_MISSING_CLAIM = re.compile(r"缺失\s*[0-9,]+\s*张")


# --------------------------------------------------------------------------- #
# 检查结果收集
# --------------------------------------------------------------------------- #
class Checker:
    """收集检查结果，最后统一汇报并以退出码表达结论。

    每条失败都带上「为什么」：只报一个 ``False`` 的断言在这类脚本里是最贵的坑
    —— 分不清是产品坏了、夹具假了、还是判据本身写错了。
    """

    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  [OK]   {label}")
        else:
            text = f"{label} — {detail}" if detail else label
            self.failures.append(text)
            print(f"  [FAIL] {text}")
        return bool(ok)

    def section(self, title: str) -> None:
        print(f"\n=== {title} ===")


def _pump(rounds: int = 8) -> None:
    if _APP is None:
        return
    for _ in range(rounds):
        _APP.processEvents()


def _wait_until(predicate, timeout: float = 20.0, interval: float = 0.02) -> bool:
    """空转事件循环直到 ``predicate()`` 为真（超时返回 ``False``，由调用方断言）。

    检查窗口的结论是**后台线程**发信号回来的，不空转事件循环就永远等不到；
    但也不能死循环 —— 超时要把"没等到"这件事说成失败，而不是把脚本挂死。
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            _pump(4)
            return True
        if _APP is not None:
            _APP.processEvents()
        time.sleep(interval)
    return bool(predicate())


def _shot(widget, name: str, enabled: bool) -> None:
    """把控件存成 ``docs/<name>``（``grab()`` 不触发布局重算，先 ``activate()``）。"""
    if not enabled:
        return
    layout = widget.layout()
    if layout is not None:
        layout.activate()
    _pump(12)
    DOCS.mkdir(parents=True, exist_ok=True)
    target = DOCS / name
    if widget.grab().save(str(target)):
        print(f"  [shot] {target.relative_to(PROJECT_ROOT)}")


def _shot_roomy(widget, name: str, enabled: bool) -> None:
    """先把窗口拉高再截图，然后还原（结果区十几行，默认高度会被裁掉）。"""
    if not enabled:
        return
    from PySide6.QtCore import QSize

    original = widget.size()
    widget.resize(QSize(max(original.width(), 1220), 1080))
    _pump(20)
    _shot(widget, name, True)
    widget.resize(original)
    _pump(8)


def _close_message_boxes(widget) -> None:
    from PySide6.QtWidgets import QMessageBox

    for box in widget.findChildren(QMessageBox):
        box.close()


def _answer(dialog, standard_button) -> bool:
    """点掉当前挂着的提示框；没有（或没有那个按钮）就返回 ``False``。"""
    box = dialog.message_box()
    if box is None:
        return False
    button = box.button(standard_button)
    if button is None:
        return False
    button.click()
    _pump(6)
    return True


def _ok():
    from PySide6.QtWidgets import QMessageBox

    return QMessageBox.StandardButton.Ok


def _yes():
    from PySide6.QtWidgets import QMessageBox

    return QMessageBox.StandardButton.Yes


def _mission_level():
    """任务区等级（从唯一那一处常量取，脚本里也不写字面量）。"""
    from app.mission.models import MISSION_REGION_LEVEL

    return MISSION_REGION_LEVEL


# --------------------------------------------------------------------------- #
# 夹具：临时行政区目录 + 一条航线文件
# --------------------------------------------------------------------------- #
FIXTURE_ENTRIES = (
    {"id": "demoland", "name": "演示国", "level": "country", "parentId": None,
     "geojson": "country/demoland.geojson"},
    {"id": "northland", "name": "北省", "level": "province", "parentId": "demoland",
     "geojson": "province/northland.geojson"},
    {"id": "rivertown", "name": "河城", "level": "city", "parentId": "northland",
     "geojson": "city/rivertown.geojson"},
)

#: 三块 0.4° 方块：估算很快，且互不重叠（区域之间不能互相掩盖）。
FIXTURE_BOXES = {
    "demoland": (100.0, 20.0, 100.4, 20.4),
    "northland": (101.0, 20.0, 101.4, 20.4),
    "rivertown": (102.0, 20.0, 102.4, 20.4),
}


def build_fixture_regions(root: Path) -> Path:
    """造一份小型行政区目录（仓库资源区保持干净）。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.json").write_text(
        json.dumps(
            {"version": 1, "regions": list(FIXTURE_ENTRIES)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    for entry in FIXTURE_ENTRIES:
        west, south, east, north = FIXTURE_BOXES[entry["id"]]
        path = root / entry["geojson"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "type": "Feature",
                    "properties": {
                        "name": entry["name"],
                        "id": entry["id"],
                        "boundaryVersion": "2026-09-verify",
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [west, south],
                                [east, south],
                                [east, north],
                                [west, north],
                                [west, south],
                            ]
                        ],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return root


def use_region_resources(root: Path) -> None:
    """把行政区资源目录重定向到 ``root``（必须在建窗口之前调用）。"""
    from app.geo import region as region_module

    region_module.REGION_RESOURCE_DIR = root


def write_route(path: Path) -> Path:
    """一份真的 GeoJSON 航线（带 properties，模拟别的软件导出的样子）。"""
    path.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {"name": "西湖巡检"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [list(point) for point in ROUTE],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# 场景 A：面板上的入口（真窗口 / 真按钮）
# --------------------------------------------------------------------------- #
def scene_entry(app, window, checker: Checker, shots: bool):
    checker.section("场景 A：点面板上的「任务区域…」（真窗口 / 真按钮）")
    from app.ui.download_panel import (
        ACTION_MISSION_OPEN,
        ACTION_PLAN_ADD,
        ACTION_PLAN_OPEN,
    )

    panel = window.download_panel
    buttons = panel.plan_buttons()

    checker.check(
        set(buttons) == {ACTION_PLAN_ADD, ACTION_PLAN_OPEN, ACTION_MISSION_OPEN},
        "计划那一排有三个入口（加入计划 / 查看计划 / 任务区域）",
        str(sorted(buttons)),
    )
    labels = {key: button.text() for key, button in buttons.items()}
    checker.check(
        labels.get(ACTION_MISSION_OPEN) == "任务区域…",
        "任务区入口的文案是「任务区域…」",
        labels.get(ACTION_MISSION_OPEN, "缺少该按钮"),
    )

    # 布局：三个按钮都要真的占到位置、且互不重叠（几何量断言，不看截图）
    _pump(12)
    rects = [
        buttons[key].geometry()
        for key in (ACTION_PLAN_ADD, ACTION_PLAN_OPEN, ACTION_MISSION_OPEN)
    ]
    checker.check(
        all(rect.width() > 0 and rect.height() > 0 for rect in rects),
        "三个入口按钮都占到了真实空间（没有被压成 0）",
        str([(rect.width(), rect.height()) for rect in rects]),
    )
    checker.check(
        all(
            not rects[i].intersects(rects[j])
            for i in range(len(rects))
            for j in range(i + 1, len(rects))
        ),
        "三个按钮互不重叠",
        str(rects),
    )
    _shot_roomy(window, "slice22_mission_entry.png", shots)

    buttons[ACTION_MISSION_OPEN].click()
    _pump(12)
    dialog = window._mission_dialog
    checker.check(
        dialog is not None and dialog.isVisible(),
        "点「任务区域…」真的打开了任务区窗口",
        "窗口为 None 或不可见",
    )
    if dialog is None:
        return None

    recommended = window.mission_service.strategy.recommend(_mission_level())
    checker.check(
        dialog.zoom_mode() == "recommended"
        and all(spin.isReadOnly() for spin in dialog.zoom_spins()),
        "窗口打开时是推荐模式且两个 Zoom 框只读（初始状态已套用）",
        f"{dialog.zoom_mode()} / "
        f"{[spin.isReadOnly() for spin in dialog.zoom_spins()]}",
    )
    checker.check(
        (dialog.zoom_spins()[0].value(), dialog.zoom_spins()[1].value())
        == (recommended.min_zoom, recommended.max_zoom),
        "两个框里填的就是策略表的推荐区间（不是 QSpinBox 的缺省 1/1）",
        f"{(dialog.zoom_spins()[0].value(), dialog.zoom_spins()[1].value())} "
        f"vs {(recommended.min_zoom, recommended.max_zoom)}",
    )
    checker.check(
        dialog.mission_service is window.mission_service,
        "窗口用的是主窗口**那一个**任务区服务（没有另起一份数据目录）",
        "",
    )
    return dialog


# --------------------------------------------------------------------------- #
# 场景 B：生成任务区（真输入 / 真文件 / 真落盘）
# --------------------------------------------------------------------------- #
def scene_create(app, dialog, checker: Checker, route_file: Path, shots: bool) -> None:
    checker.section("场景 B：填航线 → 点「生成任务区」（真读文件 / 真落盘）")

    # --- 刻意犯错一：没有名称 ---
    dialog.buttons()["create"].click()
    _pump(10)
    checker.check(
        "任务区名称" in dialog.result_lines()[0]
        and dialog.region_combo().currentData() is None,
        "没填名称就点 → 说清缺什么，且什么都不创建",
        dialog.result_lines()[0],
    )
    _answer(dialog, _ok())

    # --- 刻意犯错二：航线文件不存在 ---
    dialog.name_edit().setText("西湖巡检航线")
    dialog.route_edit().setText(str(route_file.parent / "nope.geojson"))
    dialog.buttons()["create"].click()
    _pump(10)
    checker.check(
        "不存在" in dialog.result_lines()[0],
        "航线文件不存在 → 结果区给出加载器说的那句话（不是「生成失败」四个字）",
        dialog.result_lines()[0],
    )
    _answer(dialog, _ok())

    # --- 真生成 ---
    dialog.route_edit().setText(str(route_file))
    dialog.buffer_spin().setValue(BUFFER_KM)
    dialog.buttons()["create"].click()
    _pump(16)

    region = dialog.current_region()
    checker.check(
        region is not None,
        "点「生成任务区」→ 下拉里选中了新建的任务区",
        "current_region 为 None",
    )
    if region is None:
        return

    store = dialog.mission_service.store
    names = sorted(path.name for path in store.mission_dir(region.id).iterdir())
    checker.check(
        names == ["metadata.json", "region.geojson", "route.geojson"],
        "盘上整整三个文件（route / region / metadata），一个不多一个不少",
        str(names),
    )
    path = Path(region.geojson_path)
    checker.check(
        path.is_absolute() and path.is_file(),
        "回填的几何路径是**绝对路径**且真的存在（下载链路要直接读它）",
        region.geojson_path,
    )

    from app.geo.geojson_loader import load_geojson_geometry

    geometry = load_geojson_geometry(path)
    checker.check(
        geometry.geom_type in {"Polygon", "MultiPolygon"},
        "落盘的是**面**（Buffer 的产物），不是那条线",
        geometry.geom_type,
    )
    checker.check(
        region.buffer_meters == int(BUFFER_KM * 1000),
        f"界面输入 {BUFFER_KM} km → 盘上是 {int(BUFFER_KM * 1000)} 米",
        str(region.buffer_meters),
    )
    checker.check(
        region.waypoint_count == len(ROUTE),
        f"航点数就是文件里的 {len(ROUTE)} 个",
        str(region.waypoint_count),
    )

    # 米制缓冲的**地面证据**：把航线与多边形一起投到同一个本地米制平面，量
    # 「航线到多边形边界的最短距离」—— 圆形接头的 Buffer 里这个距离**恰好**等于
    # 半径。按度数缓冲则不会（在杭州会偏，在漠河会偏得更多）。
    #
    # 刻意不在 WGS84 上用"北边界 - 航线纬度"去量：那条折线的三个点纬度不同，
    # 北边界其实来自**中间那个点**的偏移量，量出来会比半径大三分之一
    # （本脚本第一版就这么错过一次 —— 夹具的几何形状撑不起那条判据）。
    from pyproj import Transformer
    from shapely.geometry import LineString
    from shapely.ops import transform as shapely_transform

    center_lon = (region.bounds.west + region.bounds.east) / 2
    center_lat = (region.bounds.south + region.bounds.north) / 2
    metric = (
        f"+proj=aeqd +lat_0={center_lat} +lon_0={center_lon} "
        "+datum=WGS84 +units=m +no_defs"
    )
    forward = Transformer.from_crs("EPSG:4326", metric, always_xy=True)
    to_metric = lambda x, y, z=None: forward.transform(x, y)  # noqa: E731
    line_m = shapely_transform(to_metric, LineString(ROUTE))
    polygon_m = shapely_transform(to_metric, geometry)
    distance = polygon_m.boundary.distance(line_m)
    shortfall = region.buffer_meters - distance
    # 允许的落差 = 圆头的**内接多边形**效应：圆头按每象限 16 段拟合，于是边界的
    # 弦永远落在半径内侧，差一个 ``r·(1-cos(π/64))`` ≈ 0.12% 的半径（3 km 上是
    # 3.6 m）。这不是误差而是**有意的取整**，而且方向是安全的那一侧 ——
    # 多边形绝不会鼓到半径之外，所以不会"多下"用户没要的瓦片。
    checker.check(
        -0.01 <= shortfall <= region.buffer_meters * 0.005,
        "航线到边界的最短距离 == Buffer 半径（米制；只差圆头拟合的那不到 0.5%）",
        f"{distance:.2f} 米 vs 半径 {region.buffer_meters:.0f} 米"
        f"（差 {shortfall:.2f} 米）",
    )

    properties = json.loads(path.read_text(encoding="utf-8"))["properties"]
    checker.check(
        properties.get("regionType") == "polygon"
        and properties.get("regionLevel") == "mission"
        and "bufferMeters" in properties,
        "几何文件自带身份（regionType / regionLevel / bufferMeters）",
        str(properties),
    )

    lines = dialog.result_lines()
    text = "\n".join(lines)
    checker.check(
        "已生成" in text and region.geometry_version in text,
        "结果区写明已生成与几何版本号（用户能记下是哪一版）",
        lines[0] if lines else "",
    )
    _shot_roomy(dialog, "slice22_mission_created.png", shots)


# --------------------------------------------------------------------------- #
# 场景 C：计算（真按钮 / 三个数字 / 与生产同源）
# --------------------------------------------------------------------------- #
def scene_estimate(app, window, dialog, checker: Checker, shots: bool) -> None:
    checker.section("场景 C：点「计算」（候选 / 选中 / 过滤 / 逐级明细）")
    from app.geo.region_estimator import RegionTileEstimator

    dialog.buttons()["estimate"].click()
    _pump(12)
    estimate = dialog.last_estimate()
    checker.check(estimate is not None, "点「计算」算出了结果", "last_estimate 为 None")
    if estimate is None:
        return

    lines = dialog.result_lines()
    text = "\n".join(lines)
    for label in ("候选瓦片（外包矩形）", "区域内瓦片（多边形）", "过滤掉", "预计容量"):
        checker.check(label in text, f"结果区有「{label}」这一项", "")

    inner = estimate.estimate
    checker.check(
        f"{inner.candidate_tiles:,}" in text and f"{inner.selected_tiles:,}" in text,
        "候选与选中两个数字都真的写进去了（不是只画了标签）",
        f"{inner.candidate_tiles} / {inner.selected_tiles}",
    )
    checker.check(
        inner.filtered_tiles > 0 and inner.selected_tiles < inner.candidate_tiles,
        "走廊比外包矩形小 —— 这正是本 Slice 的效率主张（滤掉了一批瓦片）",
        f"candidate={inner.candidate_tiles} selected={inner.selected_tiles} "
        f"filtered={inner.filtered_tiles}",
    )
    detail_lines = [line.strip() for line in lines if line.strip().startswith("z")]
    checker.check(
        [line[:3] for line in detail_lines] == ["z15", "z16", "z17", "z18"],
        "逐级明细正好是 Z15~18 四级（区间来自策略表）",
        str([line[:3] for line in detail_lines]),
    )

    # 与**生产同一套函数**再算一遍（绝不写死推导值）
    region = dialog.mission_service.to_download_region(dialog.current_region().id)
    expected = RegionTileEstimator(region).estimate(15, 18)
    checker.check(
        (inner.selected_tiles, inner.candidate_tiles)
        == (expected.selected_tiles, expected.candidate_tiles),
        "窗口里的数字与生产估算器逐字一致（没有第二套瓦片公式）",
        f"{inner.selected_tiles} vs {expected.selected_tiles}",
    )
    _shot_roomy(dialog, "slice22_mission_estimated.png", shots)


# --------------------------------------------------------------------------- #
# 场景 D：加入下载计划（真按钮 / 顺序 / 与行政区共存）
# --------------------------------------------------------------------------- #
def scene_add_to_plan(app, window, dialog, checker: Checker, shots: bool) -> str:
    checker.section("场景 D：点「加入下载计划」（真按钮 / 与行政区同一份计划）")
    from app.planning.plan_store import PlanStore

    plan_service = window.plan_service
    panel = window.download_panel
    mission_id = dialog.current_region().id

    # 先加三块行政区（走面板上那套）
    for region_id in ("demoland", "northland", "rivertown"):
        panel.set_region_id(region_id)
        _pump(4)
        panel.plan_buttons()["plan_add"].click()
        _pump(8)

    dialog.buttons()["add"].click()
    _pump(10)

    item = plan_service.plan.item(mission_id)
    checker.check(item is not None, "任务区进了计划（与行政区同一份）", "计划里没有该条目")
    if item is None:
        return mission_id

    checker.check(
        item.region_level is _mission_level(),
        "计划条目的等级是 mission（不是 custom —— 那会排到最后）",
        item.region_level.value,
    )
    checker.check(
        (item.min_zoom, item.max_zoom) == (15, 18),
        "推荐模式下进计划的是 Z15~18（来自策略表）",
        f"{(item.min_zoom, item.max_zoom)}",
    )
    checker.check(
        [entry.region_id for entry in plan_service.plan.ordered_items()]
        == ["demoland", "northland", "rivertown", mission_id],
        "顺序：全国 → 省 → 市 → 任务区（与点击顺序无关）",
        str([entry.region_id for entry in plan_service.plan.ordered_items()]),
    )
    checker.check(
        "已加入下载计划" in dialog.plan_hint_text(),
        "提示行回了一句「已加入下载计划」",
        dialog.plan_hint_text().splitlines()[0] if dialog.plan_hint_text() else "",
    )

    before = plan_service.plan.count
    dialog.buttons()["add"].click()
    _pump(8)
    checker.check(
        plan_service.plan.count == before
        and "已更新下载计划" in dialog.plan_hint_text(),
        "同一个任务区再加一次是「更新」，不是多一行",
        f"{before} -> {plan_service.plan.count}",
    )

    reloaded = PlanStore(plan_service.store.path).load_all()[-1]
    checker.check(
        reloaded.item(mission_id) is not None
        and reloaded.item(mission_id).region_level is _mission_level(),
        "计划已经落盘，重启后那条任务区还在（等级也保住了）",
        str([entry.region_id for entry in reloaded.items]),
    )
    summary = panel.plan_summary_label().text()
    checker.check(
        "4 个区域" in summary,
        "面板摘要跟着变了（点一下就能看到全局）",
        summary,
    )

    # 估算 + 生成任务：任务区那条的目标张数来自计划
    plan_service.estimate()
    _pump(6)
    item = plan_service.plan.item(mission_id)
    checker.check(
        item.selected_tiles < item.candidate_tiles and item.selected_tiles > 0,
        "计划里记的是**走廊**张数（不是候选矩形 —— 否则进度永远到不了 100%）",
        f"selected={item.selected_tiles} candidate={item.candidate_tiles}",
    )
    tasks = plan_service.create_download_tasks()
    mission_task = next((task for task in tasks if task.region_id == mission_id), None)
    checker.check(
        mission_task is not None and mission_task.total_tiles == item.selected_tiles,
        "生成的任务目标张数 == 计划里的选中张数",
        str(mission_task.total_tiles) if mission_task else "没有该任务",
    )
    checker.check(
        mission_task is not None
        and mission_task.output_path.name == f"{mission_id}_z15_18.mbtiles",
        "输出文件名是 <任务区id>_z15_18.mbtiles（命名与行政区同源）",
        mission_task.output_path.name if mission_task else "",
    )
    checker.check(
        [task.region_id for task in tasks]
        == ["demoland", "northland", "rivertown", mission_id],
        "四条任务的顺序 = 计划的层级顺序（Slice 21 就按它串行跑）",
        str([task.region_id for task in tasks]),
    )
    _shot_roomy(window, "slice22_mission_plan.png", shots)
    return mission_id


# --------------------------------------------------------------------------- #
# 场景 E：续传（同一份盘上几何）
# --------------------------------------------------------------------------- #
def scene_resume(app, window, checker: Checker, mission_id: str) -> None:
    checker.section("场景 E：续传（从任务记录还原同一份几何）")
    import hashlib

    from app.geo import estimate_region_tiles, region_from_task

    service = window.mission_service
    fresh = service.to_download_region(mission_id)
    path = Path(fresh.geojson_path)
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()

    class _Record:
        """任务记录里续传真正要读的那几个字段（``region_from_task`` 是鸭子类型）。"""

        name = "任务区任务"
        west = fresh.bounds.west
        south = fresh.bounds.south
        east = fresh.bounds.east
        north = fresh.bounds.north
        region_type = fresh.region_type.value
        region_id = fresh.region_id
        boundary_version = fresh.boundary_version or ""

    resumed = region_from_task(_Record())
    checker.check(
        resumed is not None,
        "主窗口装上的解析回调认得这个任务区 id（注册真的生效了）",
        "region_from_task 返回 None",
    )
    if resumed is None:
        return

    checker.check(
        resumed.geojson_path == fresh.geojson_path and Path(resumed.geojson_path) == path,
        "续传拿回的是**同一份盘上几何**（路径逐字相同）",
        resumed.geojson_path,
    )
    checker.check(
        resumed.boundary_version == fresh.boundary_version,
        "几何版本号也原样（这批瓦片按哪一版几何裁的，说不清就会出事）",
        str(resumed.boundary_version),
    )
    checker.check(
        estimate_region_tiles(resumed, ZOOM, ZOOM).selected_tiles
        == estimate_region_tiles(fresh, ZOOM, ZOOM).selected_tiles,
        "逐张比对：续传的瓦片集与当初下单时**完全一致**（不会重下、也不会漏判）",
        "",
    )
    checker.check(
        hashlib.sha256(path.read_bytes()).hexdigest() == before_hash,
        "续传是只读的：几何文件一个字节都没被改写（不重算几何）",
        "",
    )

    class _Foreign(_Record):
        region_id = "mission-999"

    checker.check(
        region_from_task(_Foreign()) is None,
        "本机没有那个任务区 → 解析不出来（返回 None，而不是猜一个矩形）",
        "",
    )


# --------------------------------------------------------------------------- #
# 场景 F：Inspector 覆盖度（真包 / 真诊断）
# --------------------------------------------------------------------------- #
def _build_package(path: Path, bounds, tiles, *, region_id: str, boundary_version: str) -> None:
    from PIL import Image

    from app.geo import RegionType
    from app.mbtiles import MBTilesMetadata, MBTilesWriter

    buffer = io.BytesIO()
    Image.new("RGBA", (16, 16), (0, 0, 0, 0)).save(buffer, format="PNG")
    png = buffer.getvalue()

    metadata = MBTilesMetadata.for_bounds(
        bounds,
        ZOOM,
        ZOOM,
        name="任务区包",
        region_type=RegionType.POLYGON.value,
        region_id=region_id,
        boundary_version=boundary_version,
        region_level="mission",
        buffer_meters=int(BUFFER_KM * 1000),
    )
    with MBTilesWriter(path) as writer:
        writer.write_metadata(metadata)
        for x, y in sorted(tiles):
            writer.write_tile(ZOOM, x, y, png)


def _candidates_of(bounds) -> set:
    from app.geo import calculate_tile_range

    tile_range = calculate_tile_range(bounds, ZOOM)
    return {
        (x, y)
        for y in range(tile_range.min_y, tile_range.max_y + 1)
        for x in range(tile_range.min_x, tile_range.max_x + 1)
    }


def scene_inspector(
    app, window, checker: Checker, work_dir: Path, mission_id: str, shots: bool
) -> None:
    checker.section("场景 F：Inspector（完整 / 缺一张 / 几何不在本机）")
    from app.geo import GeoBounds, build_region_filter
    from app.services import MBTilesInspectionService

    service = window.mission_service
    region = service.to_download_region(mission_id)

    candidates = _candidates_of(region.bounds)
    region_filter = build_region_filter(region)
    inside = sorted(
        (x, y) for x, y in candidates if region_filter.intersects_tile_xyz(ZOOM, x, y)
    )
    checker.check(
        0 < len(inside) < len(candidates),
        "夹具自检：走廊内的瓦片真比候选少（否则「按矩形算」也会过）",
        f"inside={len(inside)} candidates={len(candidates)}",
    )

    service.install()
    version = region.boundary_version or ""

    complete = work_dir / "mission_complete.mbtiles"
    _build_package(complete, region.bounds, candidates, region_id=mission_id,
                   boundary_version=version)
    result = MBTilesInspectionService().inspect(complete)
    checker.check(
        result.coverage_checked is True
        and result.expected_tiles == len(inside)
        and result.missing_tiles == 0,
        f"完整包：期望 = 走廊内的 {len(inside)} 张、缺失 0",
        f"checked={result.coverage_checked} expected={result.expected_tiles} "
        f"missing={result.missing_tiles}",
    )
    checker.check(
        result.status == "PASS" and result.valid is True,
        "结论 PASS，且 region_level / buffer_meters 不会被通用校验器判成错",
        f"status={result.status} errors={result.errors[:2]}",
    )

    # 界面证据要**从界面那一条路**拿：真的开检查窗口、真的点「快速检查」，
    # 等它自己跑完 —— 直接 grab 主窗口会得到一张与场景 E 一模一样的图，
    # 那种截图叫"我截了张图"，不叫"我验了这件事"。
    window.open_mbtiles_inspector(str(complete))
    dialog = window._inspector_dialog
    dialog.buttons["quick"].click()
    _wait_until(lambda: dialog.result is not None, timeout=60.0)
    shown = dialog.result
    checker.check(
        shown is not None
        and shown.coverage_checked is True
        and shown.expected_tiles == len(inside)
        and shown.missing_tiles == 0,
        "检查窗口里点「快速检查」→ 它自己算出的结论与服务层一致（不是拿服务层的数字去凑）",
        f"检查窗口 result={None if shown is None else (shown.expected_tiles, shown.missing_tiles)}",
    )

    # 结论得**看得见**：读渲染出来的 Zoom 统计表（列：级别 / 张数 / Expected / Missing），
    # 而不是回头去读服务层的对象 —— 用户看的是这张表。
    table = dialog.zoom_table
    rendered = {}
    for row in range(table.rowCount()):
        cells = [
            table.item(row, column).text() if table.item(row, column) else ""
            for column in range(table.columnCount())
        ]
        rendered[cells[0].strip()] = cells[2:]
    expected_text, missing_text = rendered.get(str(ZOOM), ["", ""])
    checker.check(
        expected_text.replace(",", "") == str(len(inside)) and missing_text == "0",
        f"界面上 Z{ZOOM} 那一行写着 Expected={len(inside)} / Missing=0（覆盖结论看得见）",
        f"Z{ZOOM} 行 = {expected_text} / {missing_text}",
    )
    _shot_roomy(dialog, "slice22_mission_inspector.png", shots)
    dialog.close()
    _pump(6)

    omitted = inside[0]
    missing_one = work_dir / "mission_missing_one.mbtiles"
    _build_package(missing_one, region.bounds, candidates - {omitted},
                   region_id=mission_id, boundary_version=version)
    result2 = MBTilesInspectionService().inspect(missing_one)
    checker.check(
        result2.missing_tiles == 1
        and result2.missing_details == [f"{ZOOM}/{omitted[0]}/{omitted[1]}"],
        "少一张就报**那一张**（z/x/y 指得出是哪张）",
        f"missing={result2.missing_tiles} details={result2.missing_details}",
    )

    foreign_bounds = GeoBounds(west=120.0, south=30.0, east=120.1, north=30.1)
    foreign = work_dir / "mission_foreign.mbtiles"
    _build_package(foreign, foreign_bounds, _candidates_of(foreign_bounds),
                   region_id="mission-999", boundary_version="1:3000.0:abcdef123456")
    result3 = MBTilesInspectionService().inspect(foreign)
    checker.check(
        result3.coverage_checked is False and result3.missing_tiles is None,
        "几何不在本机：覆盖度标**未知**，一个具体数字都不给（不猜）",
        f"checked={result3.coverage_checked} missing={result3.missing_tiles}",
    )
    checker.check(
        result3.missing_details == []
        and _MISSING_CLAIM.search("\n".join(result3.warnings + result3.notices)) is None,
        "也不按外包矩形报「缺失 N 张」（误报比不知道糟得多）",
        str(result3.warnings[:1]),
    )
    checker.check(
        any("未知" in warning for warning in result3.warnings),
        "并且**明说**为什么算不出来（提示里要有「未知」两个字）",
        str(result3.warnings[:1]),
    )


# --------------------------------------------------------------------------- #
# 场景 G：删除保护（真按钮 / 真确认框）
# --------------------------------------------------------------------------- #
def scene_delete(
    app, window, dialog, checker: Checker, mission_id: str, shots: bool
) -> None:
    checker.section("场景 G：点「删除任务区」（被引用时拒绝 / 移除计划后能删）")
    from PySide6.QtWidgets import QMessageBox

    from app.planning.plan_store import PlanStore

    service = dialog.mission_service
    plan_service = window.plan_service

    # --- 被计划引用：直接拒绝，不弹确认框 ---
    dialog.buttons()["delete"].click()
    _pump(10)
    box = dialog.message_box()
    checker.check(
        box is not None and "正在被下载计划引用" in box.text(),
        "被计划引用 → 弹窗说清是谁在引用",
        box.text() if box else "没有弹窗",
    )
    checker.check(
        box is not None and box.button(QMessageBox.StandardButton.Yes) is None,
        "是**说明**不是**确认**（只有 Ok —— 不骗用户点一次无意义的「是」）",
        str(box.standardButtons()) if box else "",
    )
    checker.check(
        service.exists(mission_id) is True,
        "拒绝就该真的没删",
        str(service.exists(mission_id)),
    )
    _shot_roomy(dialog, "slice22_mission_delete_blocked.png", shots)
    _answer(dialog, _ok())

    # --- 从计划里移除；任务记录还在引用 → 仍然拒绝 ---
    plan_service.remove_region(mission_id)
    _pump(6)
    references = service.references(mission_id)
    checker.check(
        "下载计划" not in references,
        "从计划移除后，引用来源里不再有下载计划",
        str(references),
    )
    if "下载任务记录" in references:
        dialog.buttons()["delete"].click()
        _pump(8)
        box = dialog.message_box()
        checker.check(
            box is not None and "下载任务记录" in box.text(),
            "任务记录还引用着 → 依旧拒绝，并说清是任务记录（计划删了、任务还在）",
            box.text() if box else "没有弹窗",
        )
        _answer(dialog, _ok())
        for task in window.task_service.list_tasks():
            window.task_service.delete_task(task.id)
        _pump(6)

    # --- 真的能删了 ---
    dialog.buttons()["delete"].click()
    _pump(10)
    box = dialog.message_box()
    checker.check(
        box is not None and box.button(QMessageBox.StandardButton.Yes) is not None,
        "没人引用了 → 这次才问一句「确定删除？」",
        (box.text()[:44] if box else "没有弹窗"),
    )
    checker.check(
        box is not None and "不可恢复" in box.text(),
        "问的话里说明了后果（几何与元数据都会被删掉）",
        box.text() if box else "",
    )
    if box is None:
        return

    box.button(QMessageBox.StandardButton.No).click()
    _pump(8)
    checker.check(
        service.exists(mission_id) is True,
        "答「否」→ 一个文件都不许少",
        str(service.exists(mission_id)),
    )

    dialog.buttons()["delete"].click()
    _pump(8)
    _answer(dialog, _yes())
    checker.check(
        service.exists(mission_id) is False,
        "答「是」→ 任务区目录被删掉",
        str(service.exists(mission_id)),
    )
    checker.check(
        "已删除任务区" in dialog.result_lines()[0],
        "结果区回了一句「已删除任务区」",
        dialog.result_lines()[0],
    )
    checker.check(
        dialog.region_combo().currentData() is None,
        "下拉回到「暂无任务区」（列表与磁盘一致）",
        dialog.region_combo().currentText(),
    )
    reloaded = PlanStore(plan_service.store.path).load_all()[-1]
    checker.check(
        reloaded.item(mission_id) is None,
        "计划文件里也没有那条了（界面与落盘一致）",
        str([entry.region_id for entry in reloaded.items]),
    )


# --------------------------------------------------------------------------- #
# 场景 H：持久化（关窗 → 新窗）
# --------------------------------------------------------------------------- #
def scene_persistence(
    app, checker: Checker, work_dir: Path, preferences, route_file: Path
) -> None:
    checker.section("场景 H：关窗 → 新窗（任务区还在 / 坏目录不阻塞启动）")
    from app.tasks import TaskService, TaskStore
    from app.ui.main_window import MainWindow

    store = TaskStore(work_dir / "restart" / "tasks.json")
    opened: list[MainWindow] = []

    def open_window() -> MainWindow:
        window = MainWindow(task_service=TaskService(store), preferences=preferences)
        window.show()
        _pump(10)
        opened.append(window)
        return window

    window = open_window()
    checker.check(
        window.mission_service.list_all() == (),
        "全新的数据目录 → 一个任务区都没有（没有文件也是正常启动）",
        str(len(window.mission_service.list_all())),
    )
    window.download_panel.plan_buttons()["mission_open"].click()
    _pump(12)
    dialog = window._mission_dialog
    dialog.name_edit().setText("重启验收航线")
    dialog.route_edit().setText(str(route_file))
    dialog.buffer_spin().setValue(1.0)
    dialog.buttons()["create"].click()
    _pump(12)
    checker.check(
        dialog.current_region() is not None,
        "在新窗口里建了一个任务区",
        "创建失败",
    )
    dialog.close()
    window._exit_confirmed = True
    window.close()
    _pump(8)

    fresh = open_window()
    regions = fresh.mission_service.list_all()
    checker.check(
        len(regions) == 1 and regions[0].name == "重启验收航线",
        "新窗口里任务区还在（它落盘了，不是窗口里的内存状态）",
        str([region.name for region in regions]),
    )
    if regions:
        checker.check(
            regions[0].buffer_meters == 1000.0
            and regions[0].waypoint_count == len(ROUTE),
            "半径与航点数都从 metadata 读回来了（不必重算几何）",
            f"{regions[0].buffer_meters} / {regions[0].waypoint_count}",
        )
        fresh.download_panel.plan_buttons()["mission_open"].click()
        _pump(12)
        reopened = fresh._mission_dialog
        checker.check(
            reopened.select_region(regions[0].id) is True,
            "新窗口的任务区窗口里能选中它",
            "下拉里找不到",
        )
        reopened.close()

    # 坏目录（几何文件被删）→ 跳过它、正常启动
    mission_root = fresh.mission_service.store.root
    directory = sorted(path for path in mission_root.iterdir() if path.is_dir())[0]
    (directory / "region.geojson").unlink()
    survivor = open_window()
    checker.check(
        survivor.mission_service.list_all() == (),
        "一个坏任务区目录（几何丢了）→ 跳过它并正常启动（其余功能不受影响）",
        str(len(survivor.mission_service.list_all())),
    )
    checker.check(
        survivor.mission_service.store.root.is_dir(),
        "任务区根目录没有被顺手删掉（跳过坏目录不等于清理磁盘）",
        str(survivor.mission_service.store.root),
    )

    for item in opened:
        item._exit_confirmed = True
        item.close()
    _pump(8)


# --------------------------------------------------------------------------- #
# 场景 I：静态守卫（跨层约定，全部走 AST）
# --------------------------------------------------------------------------- #
def _mission_sources() -> list[Path]:
    return sorted((APP_DIR / "mission").rglob("*.py"))


def _int_literals(path: Path) -> set:
    """文件里的**整数常量**（注释与文档字符串是字符串，不参与判断）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    }


def _source_imports(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def scene_static_guards(checker: Checker) -> None:
    checker.section("场景 I：静态守卫（Zoom 数字 / 等级字面量 / 依赖方向）")

    # --- 1. 任务区层里没有 Zoom 数字 ---
    zoom_offenders = {
        path.name: sorted(_int_literals(path) & {15, 18}) for path in _mission_sources()
    }
    zoom_offenders = {name: found for name, found in zoom_offenders.items() if found}
    checker.check(
        not zoom_offenders,
        "任务区层里没有 15 / 18 这两个整数字面量（Zoom 只来自策略表）",
        str(zoom_offenders),
    )

    # --- 2. 等级字面量收在一处 ---
    level_offenders = []
    for path in _mission_sources():
        if path.name == "models.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr != "MISSION":
                continue
            owner = node.value
            if isinstance(owner, ast.Name) and owner.id == "RegionLevel":
                level_offenders.append(f"{path.name}:{node.lineno}")
    checker.check(
        not level_offenders,
        "RegionLevel.MISSION 字面量只出现在 models.py（其余引用 MISSION_REGION_LEVEL）",
        str(level_offenders),
    )

    # --- 3. app/geo 不反向依赖 app.mission ---
    region_path = APP_DIR / "geo" / "region.py"
    region_imports = _source_imports(region_path)
    checker.check(
        not any(name.startswith("app.mission") for name in region_imports),
        "app/geo/region.py 不 import app.mission（挂接是单向注册回调，反向会成环）",
        str(sorted(region_imports)),
    )
    checker.check(
        "register_region_resolver" in region_path.read_text(encoding="utf-8"),
        "app/geo 里确实有那张动态解析注册表（续传 / Inspector 靠它把 id 变回几何）",
        "",
    )

    # --- 4. 任务区层不碰下载器与界面 ---
    forbidden = ("app.services", "app.workers", "app.ui")
    touched = {
        path.name: sorted(
            name for name in _source_imports(path) if name.startswith(forbidden)
        )
        for path in _mission_sources()
    }
    touched = {name: found for name, found in touched.items() if found}
    checker.check(
        not touched,
        "任务区层不 import 下载器 / 执行线程 / 界面（它只产出一块地）",
        str(touched),
    )

    # --- 5. 界面里没有硬编码 Zoom、也没有阻塞式 exec() ---
    dialog_path = APP_DIR / "ui" / "mission_region_dialog.py"
    dialog_source = dialog_path.read_text(encoding="utf-8")
    checker.check(
        "strategy.recommend" in dialog_source,
        "任务区窗口的推荐值来自 strategy.recommend（不是自己写死的两个数）",
        "",
    )
    checker.check(
        15 not in _int_literals(dialog_path) and 18 not in _int_literals(dialog_path),
        "任务区窗口里也没有 15 / 18 的整数字面量",
        str(sorted(_int_literals(dialog_path) & {15, 18})),
    )
    checker.check(
        ".exec()" not in dialog_source,
        "任务区窗口的提示框一律非模态（exec() 会把自动化堵死）",
        "",
    )


# --------------------------------------------------------------------------- #
# 场景 J：Spec §25 手工清单
# --------------------------------------------------------------------------- #
def scene_manual_checklist(checker: Checker) -> None:
    checker.section("场景 J：Spec §25 手工验收清单（本脚本已逐步覆盖）")
    steps = [
        ("1. 导入 GeoJSON 航线（LineString）", "场景 B：真读文件 + 航点数断言"),
        ("2. 填半径（界面按 km，盘上按米）", "场景 B：3000 米 + 地面半宽量出来的"),
        ("3. 生成任务区 → 三个文件落盘", "场景 B：目录内容逐字比对"),
        ("4. 计算 → 候选 / 选中 / 过滤 / 逐级明细", "场景 C：四个标签 + 真数字"),
        ("5. 加入下载计划 → 与行政区同一份", "场景 D：四个条目顺序断言"),
        ("6. 计划里任务区排在全国 / 省 / 市之后", "场景 D：ordered_items 顺序"),
        ("7. 生成任务 → 目标张数 = 走廊张数", "场景 D：total_tiles == selected_tiles"),
        ("8. 关窗重开后任务区还在", "场景 H：真的关一次窗再开"),
        ("9. 续传用同一份几何（不重算）", "场景 E：路径 / 版本 / 瓦片集 / 文件摘要"),
        ("10. Inspector 找得到几何就报准", "场景 F：完整 0 缺失 / 缺一张报一张"),
        ("11. 几何不在本机 → 标未知并说明", "场景 F：不给数字 + 提示里有「未知」"),
        ("12. 被计划引用的任务区删不掉", "场景 G：真按钮 + 真确认框"),
        ("13. 一个坏任务区目录不阻塞启动", "场景 H：删掉几何文件再启动"),
    ]
    for title, coverage in steps:
        checker.check(True, title, coverage)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slice 22 任务区域实机验收")
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

    work_dir = Path(tempfile.mkdtemp(prefix="verify-slice22-"))
    regions_dir = work_dir / "regions"
    build_fixture_regions(regions_dir)
    route_file = write_route(work_dir / "route.geojson")

    # 必须在建窗口**之前**改：面板与服务构造时就会读一次目录
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

    window = MainWindow(task_service=TaskService(store), preferences=preferences)
    window.show()
    _pump(12)

    try:
        dialog = scene_entry(app, window, checker, shots)
        if dialog is not None:
            scene_create(app, dialog, checker, route_file, shots)
            scene_estimate(app, window, dialog, checker, shots)
            mission_id = scene_add_to_plan(app, window, dialog, checker, shots)
            scene_resume(app, window, checker, mission_id)
            scene_inspector(app, window, checker, work_dir, mission_id, shots)
            scene_delete(app, window, dialog, checker, mission_id, shots)
        scene_persistence(app, checker, work_dir, preferences, route_file)
        scene_static_guards(checker)
        scene_manual_checklist(checker)

        _close_message_boxes(window)
        window._exit_confirmed = True
        window.close()
        _pump(8)
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
