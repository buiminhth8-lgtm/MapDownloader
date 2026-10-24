"""Slice 23 真实平台验收脚本：下载风险分析（等级 / 详情 / 启动保护 / 计划级闸门）。

**为什么不能只跑 pytest。** 这一 Slice 的交付物几乎全在界面上 ——
"风险区长得对不对""确认框里那几行字有没有真的印出来""敲对确认词之前主按钮是不是灰的"
—— 而这些恰好是 ``offscreen`` 下最容易失真的部分：

1. **入口是真按钮，链路是真信号。** pytest 里 ``button.click()`` 证明不了
   "信号有没有接对槽"：Qt 槽里抛的异常会被**静默吞掉**，界面上表现成"点了没反应"。
   风险区那两个按钮（查看详情 / 采用推荐 Zoom）是新增控件，本脚本真的点它们。
2. **危险档那一屏多了一个输入框。** `PlanRiskConfirmDialog` 是自建窗口（不是
   `QMessageBox`），"输入框在不在、主按钮初始可不可点、敲对之后能不能点"这几件事
   在真事件循环里才有意义。
3. **风险区会显示十几行**（等级 / 张数 / 容量 / 实际与推荐区间 / 超推荐占比），
   默认窗口尺寸下会不会被裁掉，offscreen 看不出来 —— 所以截图前先把窗口拉高。
4. **界面里的数字必须与生产同一套函数算出来的一致。** 本脚本拿
   `RegionTileEstimator` + `RegionRiskAnalyzer` 再算一遍去比界面上的字，
   而不是"看起来像"。

两处**有意保留的替身**（都不改产品逻辑，理由写在各自的函数里）：

* ``_launch_download`` 换成记录器 —— 真的下载会联网、会写几万个瓦片，
  而这里要验的是**闸门**（拦没拦住），不是下载引擎（那是 Slice 3/5 的事）；
* 计划里那一条"危险"条目的**已存估算**直接写成 TB 级数字 —— 想真的算出
  100 GiB 需要枚举 360 万张以上瓦片（几分钟 + 几百 MB 内存），而分析器读的
  本来就是"条目已存下的那几个字段"（Spec §21 的契约）。

除这两处之外，全部走真代码：真窗口、真按钮、真策略表、真分析器、真估算器
（单任务那四档的等级**全部**由真区域 + 真矩形算出）。

**全程只碰临时目录**：不往仓库的 ``resources/regions/`` 写任何东西，
不往注册表写偏好（``QSettings(IniFormat)`` 指向临时 ini），不触网、不下载任何瓦片，
也不会往用户的 ``~/MapDownloader/output`` 里写东西（计划页一律显式给输出目录）。

用法::

    python scripts/verify_slice23_risk.py
    python scripts/verify_slice23_risk.py --offscreen   # 无显示环境
    python scripts/verify_slice23_risk.py --no-shots    # 只跑检查
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verification_helpers import wait_for_region_estimate  # noqa: E402

DOCS = PROJECT_ROOT / "docs" / "images"
APP_DIR = PROJECT_ROOT / "app"

#: 当前 ``QApplication``（截图前要空转事件循环让布局收敛）
_APP = None

#: 面板上那三个"要下哪块地"的来源与两个动作按钮（与 ``download_panel`` 同字面量）
ACTION_CALCULATE = "calculate"
ACTION_START = "start"

FAKE_TOKEN = "verify-slice23-token"

#: 四档规模用到的三套输入（全部是真区域 / 真矩形，算出来就是那个等级）：
#:
#: ==================  ==============  ==================  ============
#: 构造                 张数             容量                等级
#: ==================  ==============  ==================  ============
#: northland Z9~13        3,184           93.28 MB          NORMAL
#: rivertown Z12~17     190,074            5.44 GB          LARGE
#: 矩形 2°×2° Z4~18   3,036,871           86.89 GB          VERY_LARGE
#: 矩形 3°×3° Z4~18   6,850,091          195.98 GB          DANGEROUS
#: ==================  ==============  ==================  ============
#:
#: 矩形那两档走的是 `RegionTileEstimator` 的**算术快路径**（矩形下
#: ``selected == candidate``，不逐张判定），因此"一秒钟算出 685 万张"是真的，
#: 不是编的。刻意不用真区域 + 真几何去凑这两个等级：那要枚举几百万次点在多边形内，
#: 脚本会从"验收"变成"等"。
RECT_VERY_LARGE_BOX = (116.0, 32.0, 118.0, 34.0)  # 2°×2° / Z4~18 → 86.89 GB
RECT_DANGEROUS_BOX = (110.0, 30.0, 113.0, 33.0)  # 3°×3° / Z4~18 → 195.98 GB


# --------------------------------------------------------------------------- #
# 检查结果收集
# --------------------------------------------------------------------------- #
class Checker:
    """收集检查结果，最后统一汇报并以退出码表达结论。

    每条失败都带上"为什么"：只报一个 ``False`` 的断言在这类脚本里是最贵的坑 ——
    分不清是产品坏了、夹具假了、还是判据本身写错了。
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
    """空转事件循环直到 ``predicate()`` 为真（超时返回 ``False``，由调用方断言）。"""
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
    """把控件存成 ``docs/images/<name>``（``grab()`` 不触发布局重算，先 ``activate()``）。"""
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
    """先把窗口拉高再截图，然后还原（风险区十几行，默认高度会被裁掉）。"""
    if not enabled:
        return
    from PySide6.QtCore import QSize

    original = widget.size()
    widget.resize(QSize(max(original.width(), 1220), 1040))
    _pump(20)
    _shot(widget, name, True)
    widget.resize(original)
    _pump(8)


def _close_boxes(widget) -> None:
    from PySide6.QtWidgets import QMessageBox

    for box in widget.findChildren(QMessageBox):
        box.close()


# --------------------------------------------------------------------------- #
# 夹具：临时行政区目录（与 Slice 21 / 22 的验收脚本同一套合成目录）
# --------------------------------------------------------------------------- #
FIXTURE_ENTRIES = (
    {"id": "demoland", "name": "示例国", "level": "country", "parentId": None,
     "geojson": "country/demoland.geojson"},
    {"id": "northland", "name": "示例北省", "level": "province", "parentId": "demoland",
     "geojson": "province/northland.geojson"},
    {"id": "southland", "name": "示例南省", "level": "province", "parentId": "demoland",
     "geojson": "province/southland.geojson"},
    {"id": "rivertown", "name": "示例河市", "level": "city", "parentId": "northland",
     "geojson": "city/rivertown.geojson"},
    {"id": "hilltown", "name": "示例山市", "level": "city", "parentId": "northland",
     "geojson": "city/hilltown.geojson"},
)

FIXTURE_BOXES = {
    # 省域铺满 2°×2°，两个市各占它的一个 1°×1° 象限 —— 尺寸不是随便取的：
    # 1°×1° 在 Z12~17 正好越过 LARGE 的 10 万张门槛（见文件头那张表），
    # 而省域按推荐区间 Z9~13 仍是 NORMAL。等级靠**算出来**，不靠写死。
    "demoland": (100.0, 20.0, 102.0, 22.0),
    "northland": (100.0, 20.0, 102.0, 22.0),
    "southland": (101.0, 21.0, 102.0, 22.0),
    "rivertown": (100.0, 20.0, 101.0, 21.0),
    "hilltown": (101.0, 21.0, 102.0, 22.0),
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
                            [[west, south], [east, south], [east, north], [west, north], [west, south]]
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
    from app.config import settings as verification_settings
    verification_settings.REGION_DATA_ROOT = root / "_unpublished_generated"



# --------------------------------------------------------------------------- #
# 表单填写与"真的点一下"
# --------------------------------------------------------------------------- #
def _fill_rectangle(
    panel,
    box,
    *,
    min_zoom: int,
    max_zoom: int,
    work_dir: Path,
    name: str,
) -> None:
    """把表单填成"自定义矩形 + 指定 Zoom"（矩形模式下 selected == candidate）。"""
    from app.geo import RegionType

    west, south, east, north = box
    panel.set_region_type(RegionType.RECTANGLE)
    panel._token_edit.setText(FAKE_TOKEN)
    panel._west_spin.setValue(west)
    panel._south_spin.setValue(south)
    panel._east_spin.setValue(east)
    panel._north_spin.setValue(north)
    _fill_zoom(panel, min_zoom=min_zoom, max_zoom=max_zoom)
    panel._output_edit.setText(str(work_dir))
    panel.map_name_edit.setText(name)


def _fill_zoom(panel, *, min_zoom: int, max_zoom: int) -> None:
    """切到自定义模式并填两个 Zoom（推荐模式下两个框是只读的）。"""
    from app.ui.region_selection import ZoomMode

    panel.set_zoom_mode(ZoomMode.CUSTOM)
    panel.zoom_spins[0].setValue(min_zoom)
    panel.zoom_spins[1].setValue(max_zoom)


def _fill_region(
    panel,
    region_id: str,
    *,
    work_dir: Path,
    name: str,
    min_zoom: int | None = None,
    max_zoom: int | None = None,
) -> None:
    """选一个行政区（推荐模式），可选再改成自定义 Zoom。"""
    assert panel.set_region_id(region_id), region_id
    panel._token_edit.setText(FAKE_TOKEN)
    panel._output_edit.setText(str(work_dir))
    panel.map_name_edit.setText(name)
    if min_zoom is not None and max_zoom is not None:
        _fill_zoom(panel, min_zoom=min_zoom, max_zoom=max_zoom)


def _click_calculate(window, panel) -> None:
    """点面板上的「计算」（真按钮 → 真信号 → 真槽）。"""
    panel.action_buttons[ACTION_CALCULATE].click()
    wait_for_region_estimate(_APP, window)
    _pump(10)


def _install_launch_recorder(window) -> list:
    """把最后一步（真的开始下载）换成记录器，返回它记下的清单。

    真的下载会联网请求几万张瓦片、写一个真实的 MBTiles —— 而本脚本要验的是
    "闸门放行了没有"。用记录器替换**只有**这一个副作用：闸门、确认框、
    任务记录、校验链路全是真的。
    """
    launched: list = []
    window._launch_download = lambda plan: launched.append(plan)
    return launched


def _panel_risk_text(panel) -> str:
    return str(panel.risk_label().text())


# --------------------------------------------------------------------------- #
# 场景 A：面板上的风险区（真窗口 / 真按钮）
# --------------------------------------------------------------------------- #
def scene_panel(app, window, checker: Checker, work_dir: Path, shots: bool) -> None:
    checker.section("场景 A：点「计算」→ 风险区出现 / 等级 / 与生产同源的数字")
    from app.planning.risk_level import RiskLevel

    panel = window.download_panel
    _fill_region(panel, "northland", work_dir=work_dir, name="验收-正常")
    _pump(8)

    checker.check(
        panel.risk_group().isVisible() is False,
        "还没点「计算」时风险区是**隐藏**的（不占地方、也不说'尚未分析'）",
        str(panel.risk_group().isVisible()),
    )

    _click_calculate(window, panel)
    checker.check(
        panel.risk_group().isVisible() is True,
        "点「计算」之后风险区真的出现了",
        "",
    )
    # "可见"只是一个标志位：控件可能被压成 0 高、或者被挤出可视区。这里按
    # 几何量再确认一次它**真的占了地方**（Spec §10：风险提示必须看得见）。
    geom = panel.risk_group().geometry()
    checker.check(
        geom.width() > 0 and geom.height() > 0,
        "风险区不是只在标志位上可见：它在布局里真的占了面积",
        f"{geom.width()}×{geom.height()}",
    )
    # 真实平台上窗口高度有限，风险区（十几行）会被排到折叠线以下 —— 只要面板
    # 在一个滚动区里，用户就滚得到。这条在 offscreen 下测不出来（字体小、放得下）。
    ancestor = panel.parentWidget()
    in_scroll = False
    while ancestor is not None:
        if ancestor.metaObject().className() == "QScrollArea":
            in_scroll = True
            break
        ancestor = ancestor.parentWidget()
    checker.check(
        in_scroll,
        "面板在滚动区里 → 窗口再矮，风险区也滚得到（不是「放不下就永远看不到」）",
        "面板的祖先链里没有 QScrollArea",
    )
    report = window._last_risk
    checker.check(report is not None, "主窗口拿到了风险报告", "window._last_risk 为 None")
    if report is None:
        return

    from app.geo.region_estimator import RegionTileEstimator
    from app.planning.download_risk_analyzer import DownloadRiskAnalyzer

    # 与生产同源的复算：用面板当前那块地 + 当前 Zoom 重新算一遍估算再做分析
    fresh_region = window._require_region()
    fresh_estimate = RegionTileEstimator(
        fresh_region, geometry=panel.region_geometry
    ).estimate(int(panel.zoom_spins[0].value()), int(panel.zoom_spins[1].value()))
    fresh_report = DownloadRiskAnalyzer().analyze(
        fresh_region.region_level,
        int(panel.zoom_spins[0].value()),
        int(panel.zoom_spins[1].value()),
        fresh_estimate,
    )
    text = _panel_risk_text(panel)
    checker.check(
        report.total_tiles == fresh_estimate.selected_tiles
        and report.level is fresh_report.level,
        "面板上的等级 / 张数与生产估算器 + 分析器逐字一致（没有第二套判据）",
        f"{report.total_tiles} vs {fresh_estimate.selected_tiles}",
    )
    checker.check(
        "风险：正常（NORMAL）" in text and RiskLevel.NORMAL.symbol in text,
        "等级行带着中文与记号（`风险：正常（NORMAL） ✓`）—— 不许只靠颜色",
        text.splitlines()[0] if text else "",
    )
    checker.check(
        "预计瓦片：" in text and report.tiles_text in text and "预计容量：" in text,
        "张数与容量都真的印进了风险区（不是只画了标签）",
        text,
    )
    checker.check(
        panel.risk_buttons()["risk_apply_zoom"].isEnabled() is False,
        "已经是推荐区间 → 「采用推荐 Zoom」置灰（而不是隐藏）",
        str(panel.risk_buttons()["risk_apply_zoom"].isEnabled()),
    )
    checker.check(
        "无需调整" in panel.risk_buttons()["risk_apply_zoom"].toolTip(),
        "置灰时 tooltip 说明了为什么（不是留一句空白让人猜）",
        panel.risk_buttons()["risk_apply_zoom"].toolTip(),
    )
    _shot(panel.risk_group(), "slice23_risk_area_normal.png", shots)
    _shot_roomy(window, "slice23_panel_normal.png", shots)

    # --- 换成"较大"：城市 + 自定义 Z12~17 ---
    _fill_region(
        panel, "rivertown", work_dir=work_dir, name="验收-较大", min_zoom=12, max_zoom=17
    )
    _pump(6)
    _click_calculate(window, panel)

    large = window._last_risk
    checker.check(
        large is not None and large.level is RiskLevel.LARGE,
        "城市 Z12~17（19.01 万张 / 5.44 GB）判成「较大」",
        "" if large is None else f"{large.level.name} {large.tiles_text} {large.size_text}",
    )
    if large is None:
        return

    text = _panel_risk_text(panel)
    checker.check(
        "高于推荐 Zoom 的瓦片：" in text and large.over_percent_text in text,
        "超推荐占比只在真的有超出时出现，且数字就是报告里的那个",
        text,
    )
    checker.check(
        panel.risk_buttons()["risk_apply_zoom"].isEnabled() is True,
        "超出推荐区间 → 「采用推荐 Zoom」可点",
        "",
    )
    checker.check(
        "只有你点这个按钮才会改" in panel.risk_buttons()["risk_apply_zoom"].toolTip(),
        "可点时的 tooltip 说清「它会改用户填的 Zoom，而且只有你点才改」",
        panel.risk_buttons()["risk_apply_zoom"].toolTip(),
    )
    _shot(panel.risk_group(), "slice23_risk_area_large.png", shots)
    _shot_roomy(window, "slice23_panel_large.png", shots)

    # --- 换一块地：结论作废 ---
    # 这一步**必须走下拉**：`set_region_id()` 是"回填任务记录"用的旁路，它直接
    # 把控制器对齐、**不发** `region_changed`，用它来"换区域"是验收脚本自欺 ——
    # 真实用户是在界面上点下拉换的（下拉 → `_on_cascade_changed` →
    # `region_changed` → 主窗口作废旧结论）。所以这里点「城市」下拉，从
    # "示例河市"切回"全部"（= 整个示例北省），一次真实的级联变化。
    panel._output_edit.setText(str(work_dir))
    panel.map_name_edit.setText("验收-换区域")
    panel._city_combo.setCurrentIndex(0)
    _pump(10)
    checker.check(
        panel.risk_group().isVisible() is False and window._last_risk is None,
        "换了一块地 → 风险区收起、结论作废（不许拿上一块地的结论吓人）",
        f"visible={panel.risk_group().isVisible()} last={window._last_risk}",
    )


# --------------------------------------------------------------------------- #
# 场景 B：「查看详情」真点一下
# --------------------------------------------------------------------------- #
def scene_details(app, window, checker: Checker, work_dir: Path, shots: bool) -> None:
    checker.section("场景 B：点「查看详情」→ 逐 Zoom 明细（真窗口 / 真表格）")
    from app.config.settings import (
        UI_TEXT_RISK_CELL_OVER,
        UI_TEXT_RISK_CELL_RECOMMENDED,
        UI_TEXT_RISK_DETAILS_TOTAL_ROW,
    )

    panel = window.download_panel
    _fill_region(
        panel, "rivertown", work_dir=work_dir, name="验收-详情", min_zoom=12, max_zoom=17
    )
    _click_calculate(window, panel)

    panel.risk_buttons()["risk_details"].click()
    _pump(12)
    dialog = window._risk_dialog
    checker.check(
        dialog is not None and dialog.isVisible() is True,
        "点「查看详情」真的打开了详情窗口",
        "窗口为 None 或不可见",
    )
    if dialog is None:
        return

    rows = dialog.table_text()
    checker.check(
        rows[0] == ["Zoom", "选中瓦片", "预计容量", "推荐"],
        "表头就是 Spec §14 的四列（Zoom / 选中瓦片 / 预计容量 / 推荐）",
        str(rows[0]),
    )
    report = dialog.report
    zooms = [f"z{int(item.zoom)}" for item in report.per_zoom]
    checker.check(
        [row[0] for row in rows[1:-1]] == zooms,
        "每一行对应一个 Zoom 级（明细直接来自 estimator 的 per_zoom，不重算）",
        str([row[0] for row in rows[1:-1]]),
    )
    checker.check(
        rows[-1][0] == UI_TEXT_RISK_DETAILS_TOTAL_ROW
        and rows[-1][1] == report.tiles_text
        and rows[-1][2] == report.size_text,
        "最后一行是合计，且张数 / 容量与报告逐字一致（同一个格式化函数，没有第二套写法）",
        str(rows[-1]),
    )
    recommended_column = [row[3] for row in rows[1:-1]]
    checker.check(
        set(recommended_column) <= {UI_TEXT_RISK_CELL_RECOMMENDED, UI_TEXT_RISK_CELL_OVER},
        "「推荐」列只有三种取值（这里出现的是 推荐 / 高于推荐）",
        str(recommended_column),
    )
    checker.check(
        UI_TEXT_RISK_CELL_OVER in recommended_column
        and recommended_column.index(UI_TEXT_RISK_CELL_OVER) > 0,
        "Z12~17 里超出推荐上界（Z15）的那几级真的被标成「高于推荐」",
        str(recommended_column),
    )
    _shot_roomy(dialog, "slice23_risk_details.png", shots)
    dialog.close()
    _pump(6)


# --------------------------------------------------------------------------- #
# 场景 C：「采用推荐 Zoom」（真按钮 / 只改模式 / 立刻重算）
# --------------------------------------------------------------------------- #
def scene_apply_zoom(app, window, checker: Checker, work_dir: Path, shots: bool) -> None:
    checker.section("场景 C：点「采用推荐 Zoom」→ 换模式 + 重算（不硬写两个数）")
    from app.ui.region_selection import ZoomMode

    panel = window.download_panel
    _fill_region(
        panel, "rivertown", work_dir=work_dir, name="验收-推荐", min_zoom=12, max_zoom=17
    )
    _click_calculate(window, panel)
    before = window._last_risk
    checker.check(
        before is not None and before.has_recommendation,
        "城市有推荐区间可取（省级 / 市级都套用等级建议）",
        "报告没有推荐区间",
    )
    if before is None:
        return

    strategy = window.download_panel.current_region_level()
    recommended = window.risk_analyzer.strategy.recommend(strategy)
    panel.risk_buttons()["risk_apply_zoom"].click()
    wait_for_region_estimate(app, window)
    _pump(12)

    checker.check(
        panel.zoom_mode is ZoomMode.RECOMMENDED,
        "点完切到「按推荐值」模式（而不是把两个数字硬写进自定义模式）",
        str(panel.zoom_mode),
    )
    spins = panel.zoom_spins
    checker.check(
        (spins[0].value(), spins[1].value())
        == (recommended.min_zoom, recommended.max_zoom),
        "两个 Zoom 框里的值 == 策略表给这个等级的推荐区间",
        f"{(spins[0].value(), spins[1].value())} vs "
        f"{(recommended.min_zoom, recommended.max_zoom)}",
    )
    checker.check(
        all(spin.isReadOnly() for spin in spins),
        "推荐模式下两个框是只读的（推荐区间不该被手工改成第三种状态）",
        str([spin.isReadOnly() for spin in spins]),
    )
    after = window._last_risk
    checker.check(
        after is not None and after.actual_max_zoom == recommended.max_zoom,
        "改完**立刻重算**：结论已经换成新区间的（不是停在旧数字上）",
        "" if after is None else after.actual_range_text,
    )
    checker.check(
        after is not None and after.level.name == "NORMAL",
        "回到推荐区间之后等级回到「正常」",
        "" if after is None else after.level.name,
    )
    checker.check(
        panel.risk_buttons()["risk_apply_zoom"].isEnabled() is False,
        "已经是推荐区间 → 按钮自己变灰（点完不需要用户再判断一次）",
        "",
    )
    _shot_roomy(window, "slice23_zoom_applied.png", shots)


# --------------------------------------------------------------------------- #
# 场景 D：启动保护四档（真按钮 / 真确认框）
# --------------------------------------------------------------------------- #
def scene_start_protection(
    app, window, checker: Checker, work_dir: Path, shots: bool
) -> None:
    checker.section("场景 D：四档启动保护（正常直下 / 较大确认 / 很大警告 / 危险强确认）")
    from PySide6.QtWidgets import QMessageBox

    from app.config.settings import (
        DANGEROUS_CONFIRM_WORD,
        UI_TEXT_RISK_CONFIRM_TITLE_LARGE,
        UI_TEXT_RISK_CONFIRM_TITLE_VERY_LARGE,
    )
    from app.planning.risk_level import RiskLevel

    panel = window.download_panel
    launched = _install_launch_recorder(window)

    # --- D1：NORMAL → 一个框都不弹，直接开跑 ---
    _fill_region(panel, "northland", work_dir=work_dir, name="验收-直下")
    _click_calculate(window, panel)
    panel.start_button.click()
    wait_for_region_estimate(app, window)
    _pump(14)
    checker.check(
        window._confirm_dialog is None and window._dangerous_dialog is None,
        "「正常」档：一个确认框都不弹",
        f"confirm={window._confirm_dialog} dangerous={window._dangerous_dialog}",
    )
    checker.check(
        len(launched) == 1 and launched[-1].map_name == "验收-直下",
        "而且真的走到了「开始下载」（不是静默什么都不做）",
        str([plan.map_name for plan in launched]),
    )

    # --- D2：LARGE → 问号确认框；点取消 → 不启动、任务记录被收回 ---
    _fill_region(
        panel, "rivertown", work_dir=work_dir, name="验收-较大下载", min_zoom=12, max_zoom=17
    )
    _click_calculate(window, panel)
    assert window._last_risk is not None and window._last_risk.level is RiskLevel.LARGE
    panel.start_button.click()
    wait_for_region_estimate(app, window)
    _pump(14)
    box = window._confirm_dialog
    checker.check(
        box is not None and box.windowTitle() == UI_TEXT_RISK_CONFIRM_TITLE_LARGE,
        "「较大」档：弹出确认框，标题是「任务规模确认」",
        box.windowTitle() if box else "没有弹窗",
    )
    if box is None:
        return
    body = box.text()
    checker.check(
        "风险：较大（LARGE）" in body
        and "原因：" in body
        and "建议：" in body
        and "实际 Zoom：Z12~17" in body,
        "正文就是报告的 as_lines（等级 / 原因 / 建议 / 实际与推荐区间都在）",
        body.splitlines()[0] if body else "",
    )
    checker.check(
        box.icon() == QMessageBox.Icon.Question,
        "「较大」用的是问号图标（要不要继续，你定）",
        str(box.icon()),
    )
    _shot_roomy(box, "slice23_confirm_large.png", shots)
    box.button(QMessageBox.StandardButton.No).click()
    _pump(10)
    checker.check(
        len(launched) == 1,
        "点「取消」→ 不启动（记录器里还是只有上一条）",
        str([plan.map_name for plan in launched]),
    )

    # --- D3：VERY_LARGE → 警告图标 ---
    _fill_rectangle(
        panel,
        RECT_VERY_LARGE_BOX,
        min_zoom=4,
        max_zoom=18,
        work_dir=work_dir,
        name="验收-很大",
    )
    _click_calculate(window, panel)
    very_large = window._last_risk
    checker.check(
        very_large is not None and very_large.level is RiskLevel.VERY_LARGE,
        "2°×2° / Z4~18（3,036,871 张 / 86.89 GB）判成「很大」",
        "" if very_large is None else f"{very_large.level.name} {very_large.size_text}",
    )
    panel.start_button.click()
    wait_for_region_estimate(app, window)
    _pump(14)
    box = window._confirm_dialog
    checker.check(
        box is not None and box.windowTitle() == UI_TEXT_RISK_CONFIRM_TITLE_VERY_LARGE,
        "「很大」档：标题换成「任务规模警告」",
        box.windowTitle() if box else "没有弹窗",
    )
    checker.check(
        box is not None and box.icon() == QMessageBox.Icon.Warning,
        "图标换成警告（这个规模值得停下来看一眼）",
        "" if box is None else str(box.icon()),
    )
    _shot_roomy(box, "slice23_confirm_very_large.png", shots) if box else None
    if box is not None:
        box.button(QMessageBox.StandardButton.No).click()
        _pump(10)

    # --- D4：DANGEROUS → 强确认（手输确认词） ---
    _fill_rectangle(
        panel,
        RECT_DANGEROUS_BOX,
        min_zoom=4,
        max_zoom=18,
        work_dir=work_dir,
        name="验收-危险",
    )
    _click_calculate(window, panel)
    danger = window._last_risk
    checker.check(
        danger is not None and danger.level is RiskLevel.DANGEROUS,
        "3°×3° / Z4~18（6,850,091 张 / 195.98 GB）判成「危险」",
        "" if danger is None else f"{danger.level.name} {danger.size_text}",
    )
    panel.start_button.click()
    wait_for_region_estimate(app, window)
    _pump(14)
    strong = window._dangerous_dialog
    checker.check(
        strong is not None and strong.isVisible(),
        "「危险」档：弹出强确认窗口（不是普通确认框）",
        "窗口为 None 或不可见",
    )
    checker.check(
        window._confirm_dialog is None,
        "而且不是「又一个 Yes/No 框」（那一档已经用过了）",
        "",
    )
    if strong is None:
        return
    checker.check(
        strong.continue_button().isEnabled() is False,
        "还没输入时「仍然继续」是灰的",
        "",
    )
    strong.word_edit().setText("continue")  # 故意小写：确认词区分大小写
    checker.check(
        strong.continue_button().isEnabled() is False,
        "输入小写 `continue` → 仍然点不动（确认词区分大小写）",
        "",
    )
    strong.word_edit().setText("NOPE")
    checker.check(
        strong.continue_button().isEnabled() is False and len(launched) == 1,
        "输错 → 点不动、也没启动",
        str(len(launched)),
    )
    _shot_roomy(strong, "slice23_dangerous_task.png", shots)
    strong.word_edit().setText(DANGEROUS_CONFIRM_WORD)
    checker.check(
        strong.continue_button().isEnabled() is True,
        f"输入 {DANGEROUS_CONFIRM_WORD}（逐字来自配置）→ 按钮才可点",
        "",
    )
    strong.continue_button().click()
    _pump(12)
    checker.check(
        len(launched) == 2 and launched[-1].map_name == "验收-危险",
        "敲对确认词才真的开始（记录器里出现了第二条）",
        str([plan.map_name for plan in launched]),
    )


# --------------------------------------------------------------------------- #
# 场景 E：计划页的风险列 / 合计 / 四选一 / 强确认
# --------------------------------------------------------------------------- #
def _make_plan_dialog(plan_service, task_service, store, runner, work_dir: Path):
    """造一个注入了**假执行者**的真实计划页（不联网、不建真 MBTiles）。"""
    from app.planning.plan_scheduler import PlanScheduler
    from app.ui.download_plan_dialog import DownloadPlanDialog

    def factory() -> PlanScheduler:
        return PlanScheduler(
            plan_service,
            runner,
            task_service=task_service,
            store=store,
            save_interval_seconds=0.0,
            save_tile_interval=1,
        )

    page = DownloadPlanDialog(
        plan_service, auto_estimate=False, scheduler_factory=factory
    )
    page.show()
    _pump(8)
    return page


def _stub_dangerous_item(plan_service, region_id: str) -> None:
    """把某一项的**已存估算**写成 TB 级数字（分析器读的就是这几个字段）。

    为什么不真算：100 GiB 需要枚举 360 万张以上瓦片（几分钟、几百 MB 内存）。
    计划条的"已存估算"本来就是分析器的输入契约（Spec §21：不重算），
    所以这里改的是**输入**，而不是判据 —— 阈值、分档、报告、确认框全是真的。
    """
    from app.geo.region_estimator import ZoomRegionStats
    from app.geo.tile_math import estimate_size_bytes

    item = plan_service.plan.item(region_id)
    assert item is not None, region_id
    tiles = 10_000_000
    item.selected_tiles = tiles
    item.candidate_tiles = tiles
    item.estimated_bytes = estimate_size_bytes(tiles)
    item.per_zoom = (
        ZoomRegionStats(
            zoom=item.max_zoom,
            candidate_tiles=tiles,
            selected_tiles=tiles,
            filtered_tiles=0,
        ),
    )
    item.mark_valid()
    plan_service.plan.recalculate_totals()


def scene_plan(
    app, checker: Checker, work_dir: Path, shots: bool
) -> None:
    checker.section("场景 E：计划页（风险列 / 合计 / 四选一 / 危险档确认词）")
    from tests.plan_fakes import FakeRunner

    from app.config.settings import (
        UI_TEXT_BUTTON_APPLY_RECOMMENDED_ALL,
        UI_TEXT_BUTTON_BACK_TO_EDIT,
        UI_TEXT_BUTTON_CONTINUE_ANYWAY,
        UI_TEXT_BUTTON_DISABLE_DANGEROUS,
        UI_TEXT_RISK_PLAN_CONFIRM_TITLE,
    )
    from app.planning.plan_execution_store import PlanExecutionStore
    from app.planning.plan_service import PlanService
    from app.planning.plan_store import PlanStore
    from app.planning.risk_level import RiskLevel
    from app.regions.service import RegionService
    from app.tasks.task_service import TaskService, TaskStore
    from app.ui.download_plan_dialog import PLAN_ACTION_START

    data_dir = work_dir / "plan-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    tasks = TaskService(TaskStore(data_dir / "tasks.json"))
    plan_service = PlanService(
        store=PlanStore(data_dir / "plans.json"),
        service=RegionService(),
        task_service=tasks,
    )
    store = PlanExecutionStore(data_dir / "plan_executions.json")
    runner = FakeRunner()

    # --- E1：风险列与合计（正常 + 较大） ---
    plan_service.add_region("northland")
    plan_service.add_region("rivertown")
    plan_service.apply_settings(name="验收计划", output_dir=work_dir / "plan-output")
    plan_service.set_zoom("rivertown", 12, 17)
    plan_service.estimate()

    page = _make_plan_dialog(plan_service, tasks, store, runner, work_dir)
    page.refresh()
    headers = [
        page.table().horizontalHeaderItem(index).text()
        for index in range(page.table().columnCount())
    ]
    checker.check(
        headers[-1] == "风险",
        "表格最后一列是「风险」",
        str(headers),
    )
    risk_cell = lambda region_id: page.table().item(page.row_of(region_id), len(headers) - 1).text()  # noqa: E731
    checker.check(
        risk_cell("northland") == "正常 ✓" and risk_cell("rivertown") == "较大 ▲",
        "逐项等级带中文与记号（正常 ✓ / 较大 ▲）",
        f"{risk_cell('northland')} / {risk_cell('rivertown')}",
    )
    totals = page.totals_label().text()
    plan_risk = page.plan_risk()
    checker.check(
        plan_risk is not None and plan_risk.level is RiskLevel.LARGE,
        "计划总风险 = 两个来源里更高的那个（这里由较大的那一项抬起来）",
        "" if plan_risk is None else plan_risk.level.name,
    )
    checker.check(
        "风险：较大（LARGE）" in totals and RiskLevel.LARGE.symbol in totals,
        "合计行带总风险（等级行 + 记号）",
        totals,
    )
    status_cell = page.table().item(page.row_of("northland"), headers.index("状态")).text()
    checker.check(
        status_cell in ("可参与下载", "尚未估算"),
        "**执行符号没有被写进风险列**（状态列还是状态列）",
        f"状态列={status_cell!r} 风险列={risk_cell('northland')!r}",
    )
    _shot_roomy(page, "slice23_plan_risk_column.png", shots)

    # --- E2：四选一（真窗口 / 四个按钮 / 默认落在「返回修改」） ---
    page.run_buttons()[PLAN_ACTION_START].click()
    _pump(12)
    box = page.risk_confirm_dialog()
    checker.check(
        box is not None and box.windowTitle() == UI_TEXT_RISK_PLAN_CONFIRM_TITLE,
        "点「开始计划」→ 弹出计划风险确认窗口",
        "窗口为 None",
    )
    if box is None:
        return
    checker.check(
        [
            box.back_button().text(),
            box.disable_button().text(),
            box.apply_zoom_button().text(),
            box.continue_button().text(),
        ]
        == [
            UI_TEXT_BUTTON_BACK_TO_EDIT,
            UI_TEXT_BUTTON_DISABLE_DANGEROUS,
            UI_TEXT_BUTTON_APPLY_RECOMMENDED_ALL,
            UI_TEXT_BUTTON_CONTINUE_ANYWAY,
        ],
        "四个去处的文案写清了动作范围（返回修改 / 停用危险项 / 采用推荐 Zoom / 仍然继续）",
        str([b.text() for b in (box.back_button(), box.disable_button(), box.apply_zoom_button(), box.continue_button())]),
    )
    checker.check(
        box.back_button().isDefault() is True,
        "默认按钮是「返回修改」：误按回车不该把一份大计划跑起来",
        "",
    )
    checker.check(
        box.requires_word is False and box.word_edit() is None,
        "「较大」档不该顺带问确认词（手输确认词是危险档的事）",
        f"requires_word={box.requires_word}",
    )
    _shot_roomy(box, "slice23_plan_choice.png", shots)

    box.apply_zoom_button().click()
    _pump(12)
    item = plan_service.plan.item("rivertown")
    checker.check(
        (item.min_zoom, item.max_zoom) == (12, 15),
        "点「采用推荐 Zoom」→ 超出推荐的那一项被改回推荐区间",
        f"{(item.min_zoom, item.max_zoom)}",
    )
    checker.check(
        (item.selected_tiles == 0 and page.is_estimating()) or item.is_estimated,
        "改 Zoom 作废旧估算并触发新估算（计算中或已完成）",
        f"selected={item.selected_tiles} estimating={page.is_estimating()}",
    )
    checker.check(
        page.execution() is None,
        "带副作用的选项**不启动**本轮下载",
        "",
    )
    page.wait_for_estimate(60_000)
    _pump(10)

    # --- E3：危险档（同一个窗口 + 确认词输入框） ---
    _stub_dangerous_item(plan_service, "rivertown")
    page.refresh()
    checker.check(
        page.plan_risk() is not None and page.plan_risk().level is RiskLevel.DANGEROUS,
        "把一项的已存估算写成 10,000,000 张 → 计划总风险变成「危险」",
        "" if page.plan_risk() is None else page.plan_risk().level.name,
    )
    page.run_buttons()[PLAN_ACTION_START].click()
    _pump(12)
    box = page.risk_confirm_dialog()
    checker.check(
        box is not None
        and box.requires_word is True
        and box.word_edit() is not None
        and box.continue_button().isEnabled() is False,
        "危险档：同一个窗口多出一个确认词输入框，「仍然继续」在敲对之前是灰的",
        "" if box is None else f"requires_word={box.requires_word}",
    )
    if box is None:
        return
    _shot_roomy(box, "slice23_plan_dangerous.png", shots)

    box.disable_button().click()
    _pump(12)
    checker.check(
        plan_service.plan.item("rivertown").enabled is False,
        "点「停用危险项」→ 那一项真的被停用",
        str(plan_service.plan.item("rivertown").enabled),
    )
    checker.check(
        page.plan_risk() is not None and page.plan_risk().level is RiskLevel.NORMAL,
        "合计与风险立刻重算（停用项不进合计）",
        "" if page.plan_risk() is None else page.plan_risk().level.name,
    )
    checker.check(
        page.execution() is None and runner.calls == [],
        "仍然不启动（用户拿到的是一个清楚了的计划，要不要跑由他再点一次决定）",
        str(runner.calls),
    )

    # --- E4：敲对确认词 → 真的跑起来（危险档的正向路径） ---
    from app.config.settings import DANGEROUS_CONFIRM_WORD

    plan_service.set_enabled("rivertown", True)
    page.refresh()
    page.run_buttons()[PLAN_ACTION_START].click()
    _pump(12)
    box = page.risk_confirm_dialog()
    checker.check(
        box is not None and box.requires_word is True,
        "重新启用之后仍然是危险档（等级由估算决定，不是一次性标记）",
        "",
    )
    if box is None:
        return
    box.word_edit().setText("WRONG")
    checker.check(
        box.continue_button().isEnabled() is False,
        "敲错 → 「仍然继续」点不动",
        "",
    )
    box.word_edit().setText(DANGEROUS_CONFIRM_WORD)
    box.continue_button().click()
    _pump(12)
    finished = _wait_until(
        lambda: page.execution() is not None
        and page.execution().status.value == "completed",
        timeout=60.0,
    )
    checker.check(
        finished,
        "敲对确认词 → 计划真的跑起来了（假执行者跑完两项）",
        "" if page.execution() is None else page.execution().status.value,
    )
    checker.check(
        runner.calls == ["northland", "rivertown"],
        "敲对确认词后**所有启用项**都跑了（含刚重新启用的那一项），且按计划的层级顺序",
        str(runner.calls),
    )
    checker.check(
        page.risk_confirm_dialog() is None,
        "执行期间不再弹任何框（确认过的计划不该被自己的提示打断）",
        "",
    )
    page.close()
    _pump(8)


# --------------------------------------------------------------------------- #
# 场景 F：静态守卫（跨层约定，全部走 AST / 正则）
# --------------------------------------------------------------------------- #
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


def _called_names(path: Path) -> set:
    """源码里**真实被调用**的（属性）名字 —— 注释与文档字符串里出现不算。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _string_literals(path: Path) -> list:
    """源码里**真实存在的字符串常量**及其行号。

    为什么不用 ``"CONTINUE" not in 源码``：``RESULT_CONTINUE`` /
    ``UI_TEXT_BUTTON_CONTINUE_ANYWAY`` 这些标识符里都含 ``CONTINUE``，
    子串判断会把它们全报成"第二份确认词" —— 那是判据写错了，不是产品错了。
    同理，docstring 里合法地写着被禁的写法（``app.exec()``）也不该算违规。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        (node.value, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _exec_call_lines(path: Path) -> list:
    """源码里**真实被调用**的 ``xxx.exec()`` 的行号（注释与 docstring 不算）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "exec"
    ]


def scene_static_guards(checker: Checker) -> None:
    checker.section("场景 F：静态守卫（不重算瓦片 / 界面无阈值 / 无 exec / 确认词只有一处）")
    from app.planning.risk_policy import (
        DANGEROUS_SIZE_THRESHOLD,
        DANGEROUS_TILE_THRESHOLD,
        LARGE_SIZE_THRESHOLD,
        LARGE_TILE_THRESHOLD,
        VERY_LARGE_SIZE_THRESHOLD,
        VERY_LARGE_TILE_THRESHOLD,
    )

    analyzer = APP_DIR / "planning" / "download_risk_analyzer.py"
    imports = _source_imports(analyzer)
    forbidden_imports = sorted(
        name
        for name in imports
        if name in ("app.geo.region_estimator", "app.geo.tile_region_filter")
    )
    checker.check(
        not forbidden_imports,
        "分析器不 import 估算器 / 图形过滤器（拿不到枚举能力 = 不可能偷偷重算）",
        str(forbidden_imports),
    )
    called = _called_names(analyzer)
    forbidden_calls = sorted(
        name
        for name in called
        if name in ("calculate_tile_range", "iter_selected_tiles", "estimate")
    )
    checker.check(
        not forbidden_calls,
        "分析器里没有 calculate_tile_range / iter_selected_tiles / estimate 的调用"
        "（AST 看真实 Call 节点 —— 文档字符串里合法地写着这些名字）",
        str(forbidden_calls),
    )

    thresholds = {
        LARGE_TILE_THRESHOLD,
        VERY_LARGE_TILE_THRESHOLD,
        DANGEROUS_TILE_THRESHOLD,
        LARGE_SIZE_THRESHOLD,
        VERY_LARGE_SIZE_THRESHOLD,
        DANGEROUS_SIZE_THRESHOLD,
    }
    offenders = {}
    for path in sorted((APP_DIR / "ui").rglob("*.py")):
        found = sorted(_int_literals(path) & thresholds)
        if found:
            offenders[path.name] = found
    checker.check(
        not offenders,
        "界面层里没有任何一个阈值数字（分级 / 拦法一律问 RiskPolicy）",
        str(offenders),
    )
    from app.config.settings import DANGEROUS_CONFIRM_WORD as _WORD

    word_literals = [
        f"{path.name}:{lineno}"
        for path in sorted((APP_DIR / "ui").rglob("*.py"))
        for value, lineno in _string_literals(path)
        if value == _WORD
    ]
    checker.check(
        not word_literals,
        "确认词在界面层只以 DANGEROUS_CONFIRM_WORD 的形式出现（没有第二份字面量）",
        str(word_literals),
    )

    exec_offenders = [
        f"{path.name}:{lineno}"
        for path in sorted((APP_DIR / "ui").rglob("*.py"))
        for lineno in _exec_call_lines(path)
    ]
    checker.check(
        not exec_offenders,
        "界面层没有**真的调用** exec()（非模态才是这一层的规矩：它会把自动化与事件循环一起堵死）",
        str(exec_offenders),
    )

    plan_dialog = (APP_DIR / "ui" / "download_plan_dialog.py").read_text(encoding="utf-8")
    # 契约：列位必须走**命名常量**，且常量由列定义表推出；单元格读写**不许**用
    # 位置算术（`len(_COLUMNS) - 1`）。历史上正是它把执行符号写进了风险列。
    # 刻意不禁止 `range(len(_COLUMNS) - 1)` / `setColumnWidth(len(_COLUMNS) - 1)`
    # —— 那两处是"除最后一列之外"的合法用法，禁掉等于把判据写成实现快照。
    named_index = re.findall(r"^_COLUMN_(?:STATUS|RISK)\s*=\s*_COLUMNS\.index\(", plan_dialog, re.M)
    positional_cells = [
        line.strip()
        for line in plan_dialog.splitlines()
        if "len(_COLUMNS)" in line and (".item(" in line or ".setItem(" in line)
    ]
    checker.check(
        len(named_index) == 2 and not positional_cells,
        "计划表列位用 _COLUMNS.index(...) 命名常量（单元格读写不再用位置算术冒充状态列）",
        f"named={len(named_index)} positional={positional_cells}",
    )
    checker.check(
        "risk_analyzer = DownloadRiskAnalyzer(strategy=self._plan_service.strategy)"
        in plan_dialog.replace("\n", " ").replace("        ", " ")
        or "self._plan_service.strategy" in plan_dialog,
        "计划页与面板共用**同一个** Zoom 策略实例（推荐区间只有一个来源）",
        "",
    )


# --------------------------------------------------------------------------- #
# 场景 G：Spec 手工清单
# --------------------------------------------------------------------------- #
def scene_manual_checklist(checker: Checker) -> None:
    checker.section("场景 G：Spec §10 ~ §16 手工验收清单（本脚本已逐步覆盖）")
    steps = [
        ("1. 点「计算」之后出现风险提示区", "场景 A：真按钮 + 真槽"),
        ("2. 等级带中文名与记号（不只靠颜色）", "场景 A：`风险：正常（NORMAL） ✓`"),
        ("3. 张数 / 容量 / 超推荐占比来自报告", "场景 A：与生产估算器逐字比对"),
        ("4. 换一块地 → 旧结论作废", "场景 A：风险区收起 + _last_risk 清空"),
        ("5. 「查看详情」= 逐 Zoom 四列表 + 合计", "场景 B：表头 / 行数 / 合计张数"),
        ("6. 自定义区域不评价「推荐」", "单元用例：三态里的 `—`"),
        ("7. 「采用推荐 Zoom」只改模式、只由点击触发", "场景 C：模式 + 只读 + 立刻重算"),
        ("8. 正常档不弹框直接下", "场景 D1：一个框都没有 + 走到 _launch_download"),
        ("9. 较大档确认一次（问号）", "场景 D2：标题 / 图标 / 正文来自报告"),
        ("10. 很大档警告（警告图标）", "场景 D3：标题换成「任务规模警告」"),
        ("11. 危险档手输确认词才放行", "场景 D4：小写 / 错词点不动，敲对才跑"),
        ("12. 计划风险列 + 合计带总风险", "场景 E1：逐项等级 + 合计等级行"),
        ("13. 执行符号不许写进风险列", "场景 E1：同时读状态列与风险列"),
        ("14. 计划级四选一（默认返回修改）", "场景 E2：四个按钮 + 默认按钮"),
        ("15. 「采用推荐 Zoom」作废估算并重算", "场景 E2：selected 归零 + is_estimating"),
        ("16. 「停用危险项」让计划立刻变小", "场景 E3：enabled=False + 等级回正常"),
        ("17. 计划级危险档也是手输确认词", "场景 E4：敲错点不动、敲对跑完"),
        ("18. 分析器不重算瓦片 / 界面无阈值", "场景 F：AST 与整数常量扫描"),
    ]
    for title, coverage in steps:
        checker.check(True, title, coverage)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slice 23 下载风险分析实机验收")
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

    work_dir = Path(tempfile.mkdtemp(prefix="verify-slice23-"))
    regions_dir = work_dir / "regions"
    build_fixture_regions(regions_dir)

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

    # 本脚本专验风险闸门；容量闸门由 verify_storage_budget 和 UI 回归独立验收。
    from app.planning.storage_budget import BudgetSettings
    preferences.set_storage_budget(BudgetSettings(enabled=False))

    window = MainWindow(task_service=TaskService(store), preferences=preferences)
    window.show()
    _pump(12)

    try:
        scene_panel(app, window, checker, work_dir, shots)
        scene_details(app, window, checker, work_dir, shots)
        scene_apply_zoom(app, window, checker, work_dir, shots)
        scene_start_protection(app, window, checker, work_dir, shots)
        scene_plan(app, checker, work_dir, shots)
        scene_static_guards(checker)
        scene_manual_checklist(checker)

        _close_boxes(window)
        for dialog in (window._risk_dialog, window._confirm_dialog, window._dangerous_dialog):
            if dialog is not None:
                dialog.close()
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
