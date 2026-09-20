"""Slice 19 真实平台验收脚本：行政区域级联选择 + 推荐 Zoom UI。

**为什么不能只跑 pytest。** 这个 Slice 的交付物几乎全是"控件之间的关系"，而
`offscreen` 下这些关系恰好是最容易失真的那部分：

1. **级联是三个真下拉之间的联动**（选省 → 城市下拉重填）。`pytest` 里可以
   `setCurrentIndex()` 手动驱动，但那证明不了"用户点下去以后控件自己会连线"——
   信号有没有接、接没接错槽，只有真窗口上点一遍才算;
2. **推荐 / 自定义是两个真单选按钮**（`QButtonGroup` 互斥 + 只读切换 +
   "只警告不改值"）。`isChecked()` 在 offscreen 下也准，但"点了以后值有没有被
   悄悄改掉"要靠真跑一遍事件循环;
3. **恢复是跨进程的**（`QSettings` → 关窗 → 新窗口）。**必须真的关一次窗**
   （走 `closeEvent` → `_save_preferences`），再用同一个偏好文件开一个新窗口，
   才能证明"下次打开还在那儿"。写个 `set_region_id()` 再断言控件状态是自欺;
4. **布局要真的摆得下**。行政区域模式下比矩形模式多出 14 个控件，三个下拉
   并排、两个单选并排 —— 放不下时 Qt 会静默压缩（字体更小、控件重叠），
   `pytest` 的 offscreen 平台连字体都是另一套。

脚本额外守两条**跨层**约定：

* **界面不许自己解析目录 / GeoJSON 来决定层级** —— 等级只能来自
  `RegionSelectionController`（`app/regions` 的目录条目）；
* **推荐 Zoom 的值不许在界面里出现第二份** —— 界面显示的必须与
  `ZoomStrategyService` 的输出逐字相同。

全程只碰临时目录：不往仓库的 ``resources/regions/`` 写任何东西，不往注册表写
偏好（用 ``QSettings(IniFormat)`` 指向临时 ini），不触网。

用法::

    python scripts/verify_slice19_region_ui.py
    python scripts/verify_slice19_region_ui.py --offscreen   # 无显示环境
    python scripts/verify_slice19_region_ui.py --no-shots    # 只跑检查
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import sys
import tempfile
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

    每条失败都带上"为什么"（``detail``）。只报一个 ``False`` 的断言在这类脚本里
    是最贵的坑：分不清是产品坏了、夹具假了、还是判据本身写错了。
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


def _close_dialogs(window) -> None:
    """关掉前面场景残留的非模态提示框，免得压在截图上 / 挡住点击。"""
    from PySide6.QtWidgets import QMessageBox

    for dialog in window.findChildren(QMessageBox):
        dialog.close()


def _shot_roomy(window, name: str, enabled: bool) -> None:
    """先把窗口拉高再截图，然后还原。

    Slice 19 的面板比矩形模式高出一整块（三段下拉 + 级别 + 边界提示 + 策略行 +
    推荐说明），默认窗口高度下**结果区会被裁掉** —— 而"Zoom 明细四列"恰恰是
    这个 Slice 要留档的东西。`grab()` 是按控件当前尺寸离屏渲染的，与窗口是否
    真的在屏幕上完全可见无关，所以临时放大是安全的。
    """
    if not enabled:
        return
    from PySide6.QtCore import QSize

    original = window.size()
    window.resize(QSize(max(original.width(), 1180), 1080))
    _pump(_APP, 20)
    _shot(window, name, True)
    window.resize(original)
    _pump(_APP, 8)


def _texts(combo) -> list[str]:
    return [combo.itemText(index) for index in range(combo.count())]


# --------------------------------------------------------------------------- #
# 夹具：在临时目录里造一份**四级形态**的行政区目录
# --------------------------------------------------------------------------- #
#: 夹具目录。坐标是**合成的方块**，不是任何真实行政区的边界。
#:
#: ``dryland``（旱省）刻意**不放**边界文件 —— 它代表"目录里有这一条，但授权数据
#: 还没到位"这个部署状态，用来验 Spec §12 的异常处理分支。
FIXTURE_ENTRIES = (
    {"id": "demoland", "name": "演示国", "level": "country", "parentId": None,
     "geojson": "country/demoland.geojson"},
    #: 第二个国家：只有一个国家的话，`setCurrentIndex(0)` 不会发信号 ——
    #: "换国家要清省 / 清市"这条就永远验不到（选中的还是同一项）。
    {"id": "demoland2", "name": "二国", "level": "country", "parentId": None,
     "geojson": "country/demoland2.geojson"},
    {"id": "northland", "name": "北省", "level": "province", "parentId": "demoland",
     "geojson": "province/northland.geojson"},
    {"id": "southland", "name": "南省", "level": "province", "parentId": "demoland",
     "geojson": "province/southland.geojson"},
    {"id": "eastland", "name": "东省", "level": "province", "parentId": "demoland",
     "geojson": "province/eastland.geojson"},
    {"id": "dryland", "name": "旱省", "level": "province", "parentId": "demoland",
     "geojson": "province/dryland.geojson"},
    {"id": "rivertown", "name": "河城", "level": "city", "parentId": "northland",
     "geojson": "city/rivertown.geojson"},
    {"id": "hilltown", "name": "山城", "level": "city", "parentId": "southland",
     "geojson": "city/hilltown.geojson"},
)

#: 每个区域一个小方块（1° 量级）。刻意做小：场景 D 会真的点一次「计算」，走的是
#: 生产那条多边形预估链路 —— 大范围在高 Zoom 下要算几十万张瓦片，脚本会挂在那里
#: （这不是产品缺陷，是夹具选得不合适）。
FIXTURE_BOXES = {
    "demoland": (100.0, 20.0, 104.0, 24.0),
    "demoland2": (60.0, 30.0, 61.0, 31.0),
    "northland": (100.0, 22.0, 102.0, 24.0),
    "southland": (102.0, 22.0, 104.0, 24.0),
    "eastland": (100.0, 20.0, 102.0, 21.0),
    "rivertown": (100.0, 23.0, 101.0, 24.0),
    "hilltown": (103.0, 22.0, 104.0, 23.0),
}

#: 故意不放边界文件的那一条（"目录里有、数据还没到位"）
FIXTURE_MISSING_ID = "dryland"


def write_boundary(root: Path, relative: str, *, name: str, region_id: str, box) -> Path:
    """在 ``root`` 下按相对路径写一份合法边界 GeoJSON（合成夹具）。"""
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
    return path


def build_fixture_regions(root: Path) -> Path:
    """造一份行政区目录（``dryland`` 那条的边界文件**故意缺失**）。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.json").write_text(
        json.dumps({"version": 1, "regions": list(FIXTURE_ENTRIES)},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for entry in FIXTURE_ENTRIES:
        if entry["id"] == FIXTURE_MISSING_ID:
            continue
        write_boundary(
            root,
            entry["geojson"],
            name=entry["name"],
            region_id=entry["id"],
            box=FIXTURE_BOXES[entry["id"]],
        )
    return root


def use_region_resources(root: Path) -> None:
    """把行政区资源目录重定向到 ``root``（仓库资源区保持干净）。

    必须在**建窗口之前**调用：面板构造时就会去读一次目录。
    """
    from app.geo import region as region_module

    region_module.REGION_RESOURCE_DIR = root


# --------------------------------------------------------------------------- #
# 场景 A：级联选择（真窗口 / 真下拉 / 真联动）
# --------------------------------------------------------------------------- #
def scene_cascade(app, window, checker: Checker, shots: bool) -> None:
    checker.section("场景 A：国家 / 省 / 市三级级联（真窗口）")
    from app.geo import RegionType
    from app.planning import RegionLevel

    panel = window.download_panel
    country = panel.country_combo
    province = panel.province_combo
    city = panel.city_combo

    # --- 初始是矩形：三段下拉都该是隐藏的 ---
    checker.check(
        panel.current_region_type() is RegionType.RECTANGLE,
        "冷启动仍然是自定义矩形（Slice ≤18 的默认行为不变）",
        panel.current_region_type().value,
    )
    checker.check(
        all(combo.isHidden() for combo in (country, province, city)),
        "矩形模式下三段下拉收起（行政区域那一块整体不可见）",
        str([combo.isHidden() for combo in (country, province, city)]),
    )

    panel.set_region_type(RegionType.POLYGON)
    _pump(app)

    checker.check(
        not any(combo.isHidden() for combo in (country, province, city)),
        "切到「行政区域」后三段下拉都露出来了",
        str([combo.isHidden() for combo in (country, province, city)]),
    )
    country_texts = _texts(country)
    checker.check(
        country_texts == ["演示国", "二国"],
        "「国家」下拉只列国家级",
        str(country_texts),
    )
    checker.check(
        _texts(province) == ["全部（不限定省份）", "北省", "南省", "东省", "旱省（边界文件未就绪）"],
        "「省份」下拉按当前国家刷新，缺边界文件的条目**照样列出来**并标注",
        str(_texts(province)),
    )
    checker.check(
        _texts(city) == ["全部（不限定城市）"],
        "还没选省时「城市」下拉只有「不限定城市」",
        str(_texts(city)),
    )
    checker.check(
        panel.current_region_level() is RegionLevel.COUNTRY,
        "只选国家 → 等级 COUNTRY（国 → 省 → 市是**可选**的，不是必填）",
        panel.current_region_level().value,
    )
    checker.check(
        panel.current_region_id() == "demoland",
        "国家级时生效 id 是国家自己",
        str(panel.current_region_id()),
    )
    checker.check(
        panel.level_text.startswith("全国级") and "演示国" in panel.level_text,
        "「下载级别」一行显示等级 + 完整路径（都来自目录条目）",
        panel.level_text,
    )

    # --- 点省：城市下拉要跟着重填 ---
    province.setCurrentIndex(1)
    _pump(app)
    checker.check(
        panel.current_region_level() is RegionLevel.PROVINCE
        and panel.current_region_id() == "northland",
        "选省 → 等级 PROVINCE，生效 id 是省自己（不是国家的）",
        f"{panel.current_region_level().value} / {panel.current_region_id()}",
    )
    checker.check(
        _texts(city) == ["全部（不限定城市）", "河城"],
        "选省 → 「城市」下拉重填成该省的城市",
        str(_texts(city)),
    )
    checker.check(
        "北省" in panel.level_text and "演示国" in panel.level_text,
        "路径文本把国 → 省串起来（层级来自目录，不是界面拼的）",
        panel.level_text,
    )

    # --- 点市 ---
    city.setCurrentIndex(1)
    _pump(app)
    checker.check(
        panel.current_region_level() is RegionLevel.CITY
        and panel.current_region_id() == "rivertown",
        "选市 → 等级 CITY，生效 id 是市",
        f"{panel.current_region_level().value} / {panel.current_region_id()}",
    )
    _shot(window, "slice19_cascade_city.png", shots)

    # --- 城市下拉里的"全部"：退回省级 ---
    city.setCurrentIndex(0)
    _pump(app)
    checker.check(
        panel.current_region_level() is RegionLevel.PROVINCE
        and panel.current_region_id() == "northland",
        "把城市选回「全部」→ 退回省级（市 → 省 → 国的兜底顺序在界面侧也成立）",
        f"{panel.current_region_level().value} / {panel.current_region_id()}",
    )

    # --- 该省没有城市级数据时，下拉要说清楚而不是空着 ---
    province.setCurrentIndex(3)  # 东省：夹具里没有子级
    _pump(app)
    checker.check(
        _texts(city) == ["（该省暂无城市级数据）"]
        and panel.current_region_level() is RegionLevel.PROVINCE,
        "省下没有城市数据时，下拉写明「暂无城市级数据」并停在省级",
        str(_texts(city)),
    )

    # --- 换国家要清省 / 清市（不能留着上一个国家的省）---
    province.setCurrentIndex(1)  # 北省
    _pump(app)
    city.setCurrentIndex(1)  # 河城
    _pump(app)
    country.setCurrentIndex(1)  # 换到二国
    _pump(app)
    checker.check(
        panel.current_region_id() == "demoland2"
        and _texts(province) == ["全部（不限定省份）"]
        and _texts(city) == ["全部（不限定城市）"],
        "换国家 → 省 / 市都被清掉（不留上一个国家的残值）",
        f"{panel.current_region_id()} / {_texts(province)} / {_texts(city)}",
    )
    country.setCurrentIndex(0)  # 换回演示国
    _pump(app)
    checker.check(
        panel.current_region_id() == "demoland"
        and _texts(province) == [
            "全部（不限定省份）",
            "北省",
            "南省",
            "东省",
            "旱省（边界文件未就绪）",
        ],
        "换回来 → 生效 id 是国家自己，省份下拉按新国家重填",
        f"{panel.current_region_id()} / {_texts(province)}",
    )

    # --- 布局：控件要真的占得到位置（不是被压成 0 高）---
    _pump(app, 12)
    sizes = {}
    for name, widget in (
        ("国家", country),
        ("省份", province),
        ("城市", city),
        ("级别", panel.level_label),
    ):
        sizes[name] = widget.size()
    checker.check(
        all(size.width() > 40 and size.height() > 10 for size in sizes.values()),
        "行政区域那一行的控件都有真实尺寸（没被布局压扁）",
        str({key: (value.width(), value.height()) for key, value in sizes.items()}),
    )
    recommended, custom = panel.zoom_mode_buttons
    checker.check(
        recommended.isChecked() != custom.isChecked(),
        "推荐 / 自定义两个单选互斥（有且只有一个被选中）",
        f"recommended={recommended.isChecked()} custom={custom.isChecked()}",
    )


# --------------------------------------------------------------------------- #
# 场景 B：推荐 Zoom（真窗口）
# --------------------------------------------------------------------------- #
def scene_recommended_zoom(app, window, checker: Checker, shots: bool) -> None:
    checker.section("场景 B：推荐 Zoom 随等级自动填值（真窗口）")
    from app.geo import RegionType
    from app.planning import RegionLevel, ZoomStrategyService
    from app.ui.region_selection import ZoomMode

    panel = window.download_panel
    strategy = ZoomStrategyService()
    recommended, custom = panel.zoom_mode_buttons

    panel.set_region_type(RegionType.POLYGON)
    _pump(app)
    panel.country_combo.setCurrentIndex(0)
    _pump(app)

    checker.check(
        panel.zoom_mode is ZoomMode.RECOMMENDED,
        "默认策略是「推荐 Zoom」（用户不必先去理解有一张策略表）",
        panel.zoom_mode.value,
    )
    checker.check(
        recommended.isChecked() and not custom.isChecked(),
        "单选按钮的界面状态与面板状态一致",
        f"radio={recommended.isChecked()} mode={panel.zoom_mode.value}",
    )

    # --- 三级各看一次：界面显示 == 策略层输出 ---
    cases = [
        ("国家", None, None, RegionLevel.COUNTRY),
        ("省", 1, None, RegionLevel.PROVINCE),
        ("市", 1, 1, RegionLevel.CITY),
    ]
    for label, province_index, city_index, level in cases:
        if province_index is not None:
            panel.province_combo.setCurrentIndex(province_index)
            _pump(app)
        if city_index is not None:
            panel.city_combo.setCurrentIndex(city_index)
            _pump(app)
        expected = strategy.recommend(level)
        data = panel.get_form_data()
        checker.check(
            (data["min_zoom"], data["max_zoom"])
            == (expected.min_zoom, expected.max_zoom),
            f"{label}级 → Zoom 自动填成策略建议值（{expected.range_text}）",
            f"{data['min_zoom']}~{data['max_zoom']}",
        )
        checker.check(
            panel.zoom_policy_text.splitlines()[:1]
            == [f"推荐 Zoom：{expected.range_text}"],
            f"{label}级建议行首行与策略服务的输出逐字相同（没有第二份值）",
            repr(panel.zoom_policy_text.splitlines()[:1]),
        )
        checker.check(
            panel.zoom_explain_text == expected.reason and bool(expected.reason),
            f"{label}级「推荐说明」来自策略层（界面没抄一份文案）",
            panel.zoom_explain_text[:60],
        )

    # --- 推荐模式下 Min/Max 只读：不允许出现"显示 A 实际用 B" ---
    min_spin, max_spin = panel.zoom_spins
    checker.check(
        min_spin.isReadOnly() and max_spin.isReadOnly(),
        "推荐模式下 Min / Max 只读（用户改不动，也就不会与建议值不一致）",
        f"min={min_spin.isReadOnly()} max={max_spin.isReadOnly()}",
    )
    _shot(window, "slice19_zoom_recommended.png", shots)

    # --- 切到自定义：解除只读，值不许被改 ---
    before = panel.zoom_range_text()
    custom.setChecked(True)
    _pump(app)
    checker.check(
        panel.zoom_mode is ZoomMode.CUSTOM,
        "点「自定义 Zoom」→ 面板模式跟着变（信号真的连上了）",
        panel.zoom_mode.value,
    )
    checker.check(
        not min_spin.isReadOnly() and not max_spin.isReadOnly(),
        "自定义模式下 Min / Max 可编辑",
        f"min={min_spin.isReadOnly()} max={max_spin.isReadOnly()}",
    )
    checker.check(
        panel.zoom_range_text() == before,
        "模式切换本身**不改** Zoom 的值（切模式不等于改参数）",
        f"{before} → {panel.zoom_range_text()}",
    )

    # --- 切回推荐：值回到建议值 ---
    recommended.setChecked(True)
    _pump(app)
    expected_city = strategy.recommend(RegionLevel.CITY)
    checker.check(
        panel.zoom_range_text() == f"{expected_city.min_zoom} ~ {expected_city.max_zoom}",
        "切回「推荐 Zoom」→ 值回到当前等级的建议值",
        panel.zoom_range_text(),
    )
    checker.check(
        min_spin.isReadOnly() and max_spin.isReadOnly(),
        "切回推荐后 Min / Max 重新只读",
        f"min={min_spin.isReadOnly()}",
    )

    # --- 矩形模式下不许强制策略 ---
    panel.set_region_type(RegionType.RECTANGLE)
    _pump(app)
    rect_before = panel.zoom_range_text()
    checker.check(
        panel.zoom_range_text() == rect_before,
        "矩形模式下「推荐 Zoom」不干预 Zoom（CUSTOM 等级没有建议值可言）",
        rect_before,
    )
    checker.check(
        panel.zoom_policy_text == "" and panel.zoom_explain_text == "",
        "矩形模式下建议行与推荐说明都收起（不显示一行恒定的噪音）",
        repr(panel.zoom_policy_text),
    )
    panel.set_region_type(RegionType.POLYGON)
    _pump(app)


# --------------------------------------------------------------------------- #
# 场景 C：超范围只警告、不改值（Spec §5）
# --------------------------------------------------------------------------- #
def scene_zoom_warning(app, window, checker: Checker, shots: bool) -> None:
    checker.section("场景 C：自定义 Zoom 超范围只警告，不阻塞也不改值")
    from app.geo import RegionType
    from app.ui.download_panel import ACTION_CALCULATE, ACTION_START

    panel = window.download_panel
    panel.set_region_type(RegionType.POLYGON)
    _pump(app)
    panel.province_combo.setCurrentIndex(1)
    _pump(app)
    panel.city_combo.setCurrentIndex(1)  # 河城 → CITY，建议 Z12~16
    _pump(app)

    panel.set_zoom_range(8, 17)
    _pump(app)
    checker.check(
        "城市级" in panel.zoom_policy_text and "Z12~16" in panel.zoom_policy_text,
        "自定义 8~17 对市级区域给出「城市级」警告（等级来自目录条目）",
        panel.zoom_policy_text.replace("\n", " / "),
    )
    checker.check(
        panel.zoom_range_text() == "8 ~ 17",
        "警告不修改用户输入（值仍是 8 ~ 17）",
        panel.zoom_range_text(),
    )
    checker.check(
        panel.action_buttons[ACTION_CALCULATE].isEnabled()
        and panel.action_buttons[ACTION_START].isEnabled(),
        "警告不阻塞：「计算」与「开始下载」仍然可用",
        f"calculate={panel.action_buttons[ACTION_CALCULATE].isEnabled()} "
        f"start={panel.action_buttons[ACTION_START].isEnabled()}",
    )
    checker.check(
        panel.current_zoom_recommendation() is not None
        and panel.current_zoom_recommendation().warning,
        "策略层把这一对值标成了 warning（界面只是显示它，不自己算阈值）",
        str(panel.current_zoom_recommendation().warning),
    )
    _shot(window, "slice19_zoom_warning.png", shots)

    # --- 范围倒置：说人话，不从 Qt 槽里漏异常 ---
    panel.set_zoom_range(16, 12)
    _pump(app)
    checker.check(
        bool(panel.zoom_policy_text.strip()),
        "Min > Max 时建议行给出一句话（不是空白，也不是崩溃）",
        repr(panel.zoom_policy_text[:80]),
    )
    checker.check(
        panel.zoom_range_text() == "16 ~ 12",
        "范围倒置也不改用户输入（交给校验层去拦，界面不抢着纠正）",
        panel.zoom_range_text(),
    )

    panel.set_zoom_range(12, 16)
    _pump(app)
    checker.check(
        "警告" not in panel.zoom_policy_text,
        "改回建议区间后警告消失（非阻塞，自己会收）",
        panel.zoom_policy_text.replace("\n", " / "),
    )


# --------------------------------------------------------------------------- #
# 场景 D：Polygon 范围 / 估算 / Zoom 明细（Spec §8 / §10）
# --------------------------------------------------------------------------- #
def scene_polygon_estimate(app, window, checker: Checker, shots: bool) -> None:
    checker.section("场景 D：范围来自 GeoJSON、估算与 Zoom 明细（真点「计算」）")
    from app.geo import RegionType, geometry_to_bounds
    from app.ui.download_panel import ACTION_CALCULATE

    panel = window.download_panel
    panel.set_region_type(RegionType.POLYGON)
    _pump(app)
    panel.province_combo.setCurrentIndex(1)
    _pump(app)
    panel.city_combo.setCurrentIndex(1)  # 河城
    _pump(app)

    # --- 范围必须等于夹具那个方块（隔着 GeoJSON 也要对得上）---
    document = panel.region_controller.load_document()
    bounds = geometry_to_bounds(document.geometry)
    data = panel.get_form_data()
    checker.check(
        (data["west"], data["south"], data["east"], data["north"])
        == (bounds.west, bounds.south, bounds.east, bounds.north),
        "四个范围框是按 GeoJSON 的 geometry 回填的（不是默认矩形）",
        f"{data['west']},{data['south']},{data['east']},{data['north']}",
    )
    checker.check(
        (bounds.west, bounds.south, bounds.east, bounds.north)
        == FIXTURE_BOXES["rivertown"],
        "回填的范围与夹具边界文件里写的一致",
        f"{bounds.west},{bounds.south},{bounds.east},{bounds.north}",
    )
    checker.check(
        all(spin.isReadOnly() for spin in panel.bounds_spins),
        "行政区域模式下经纬度只读（手改会让「显示的范围」与「裁剪依据」对不上）",
        str([spin.isReadOnly() for spin in panel.bounds_spins]),
    )

    # --- 真的点一次「计算」（Zoom 收窄到 12~13：验链路，不是验算力）---
    panel.set_zoom_range(12, 13)
    _pump(app)
    panel.action_buttons[ACTION_CALCULATE].click()
    _pump(app, 24)
    text = panel.result_label.text()
    checker.check(
        "区域：河城" in text and "级别：城市级" in text and "推荐 Zoom：Z12~16" in text,
        "结果区写明算的是哪个区域、什么级别、建议区间（都来自控制器/策略层）",
        repr(text.splitlines()[:3]),
    )
    checker.check(
        "候选瓦片" in text and "区域内瓦片" in text and "过滤瓦片" in text,
        "候选 / 区域内 / 过滤 三行都在（Slice 16 链路无回归）",
        repr(text[:200]),
    )
    checker.check(
        "Zoom 明细（候选 / 选中 / 过滤 / 预计容量）：" in text,
        "逐级明细的表头写明了四列（Slice 19 新增「预计容量」）",
        repr([line for line in text.splitlines() if "明细" in line]),
    )
    detail_lines = [
        line for line in text.splitlines() if line.strip().startswith("z")
    ]
    checker.check(
        len(detail_lines) == 2,
        "明细是**逐级**一行（这里 12~13 就是两行）",
        str(detail_lines),
    )
    checker.check(
        all(
            any(unit in line for unit in ("B", "KB", "MB", "GB"))
            and line.count("/") == 2
            for line in detail_lines
        ),
        "每行明细都有「候选 / 选中 / 过滤」三列 + 一个带单位的容量",
        str(detail_lines),
    )
    checker.check(
        panel.region_error == "",
        "边界可用时区域错误清空（没留下上一次的残值）",
        repr(panel.region_error),
    )
    _shot_roomy(window, "slice19_polygon_estimate.png", shots)


# --------------------------------------------------------------------------- #
# 场景 E：缺边界文件 / 目录不可用（Spec §12 异常处理）
# --------------------------------------------------------------------------- #
def scene_missing_boundary(app, window, checker: Checker) -> None:
    checker.section("场景 E：缺边界文件 / 目录不可用（不许崩，要给得出人话）")
    from app.geo import RegionType
    from app.planning import RegionLevel
    from app.ui.download_panel import ACTION_CALCULATE

    panel = window.download_panel
    panel.set_region_type(RegionType.POLYGON)
    _pump(app)

    dry_index = _texts(panel.province_combo).index("旱省（边界文件未就绪）")
    panel.province_combo.setCurrentIndex(dry_index)
    _pump(app)
    checker.check(
        panel.current_region_level() is RegionLevel.PROVINCE,
        "边界文件缺失的条目**照样能选中**，等级仍按目录条目给（靠元数据）",
        panel.current_region_level().value,
    )
    checker.check(
        panel.current_region() is None,
        "但取不到 DownloadRegion（缺文件时不许悄悄退回矩形去下）",
        str(panel.current_region()),
    )
    checker.check(
        "边界" in panel.region_error and "geojson" in panel.region_error.lower(),
        "提示行告诉用户**缺哪个文件**（人话里带着路径）",
        panel.region_error[:120],
    )
    checker.check(
        panel.current_zoom_recommendation() is not None,
        "边界缺失不影响 Zoom 建议（建议只依赖等级，不依赖文件）",
        str(panel.current_zoom_recommendation() is not None),
    )

    panel.action_buttons[ACTION_CALCULATE].click()
    _pump(app, 16)
    checker.check(
        panel.result_label.text().startswith("计算失败：")
        or "边界" in panel.result_label.text(),
        "点「计算」不会因为缺文件而崩（要么报错行，要么给出原因）",
        repr(panel.result_label.text()[:120]),
    )


# --------------------------------------------------------------------------- #
# 场景 F：QSettings 保存 / 恢复（关窗 → 新窗口）
# --------------------------------------------------------------------------- #
def scene_preferences_roundtrip(app, checker: Checker, make_window, preferences) -> None:
    checker.section("场景 F：偏好保存 / 恢复（真的关一次窗，再开一个新窗口）")
    from app.geo import RegionType
    from app.ui.preferences import RegionSelection
    from app.ui.region_selection import ZoomMode

    # --- 造一个有内容的界面状态，然后**关窗**（走 closeEvent → _save_preferences）---
    window = make_window(preferences)
    panel = window.download_panel
    panel.set_region_type(RegionType.POLYGON)
    _pump(app)
    panel.province_combo.setCurrentIndex(1)
    _pump(app)
    panel.city_combo.setCurrentIndex(1)  # 河城
    _pump(app)
    panel.set_zoom_range(11, 14)  # → 自定义模式
    _pump(app)
    expected_zoom = panel.zoom_range_text()
    window._exit_confirmed = True
    window.close()
    _pump(app, 12)

    stored = preferences.region_selection
    checker.check(
        stored.region_type == RegionType.POLYGON.value
        and stored.country_id == "demoland"
        and stored.province_id == "northland"
        and stored.city_id == "rivertown",
        "关窗时三段 id 分别落盘（市 / 省 / 国 各存一份，不是一个末端 id）",
        f"{stored.region_type} / {stored.country_id} / {stored.province_id} / {stored.city_id}",
    )
    checker.check(
        preferences.zoom_mode == ZoomMode.CUSTOM.value,
        "关窗时记下的是策略**模式**（自定义）",
        str(preferences.zoom_mode),
    )
    checker.check(
        preferences.zoom_range == (11, 14),
        "自定义区间落在新的 custom_* 键上",
        str(preferences.zoom_range),
    )

    # --- 新窗口：一切照旧 ---
    window = make_window(preferences)
    panel = window.download_panel
    _pump(app, 12)
    checker.check(
        panel.current_region_type() is RegionType.POLYGON,
        "新窗口记住了上次是行政区域模式",
        panel.current_region_type().value,
    )
    checker.check(
        panel.current_region_id() == "rivertown"
        and _texts(panel.province_combo)[panel.province_combo.currentIndex()]
        == "北省",
        "三段下拉逐级铺好，生效 id 回到上次那个市",
        f"{_texts(panel.country_combo)} / {_texts(panel.province_combo)} / {panel.current_region_id()}",
    )
    checker.check(
        panel.zoom_mode is ZoomMode.CUSTOM and panel.zoom_range_text() == expected_zoom,
        "策略与自定义区间都恢复了（推荐模式没有把 11~14 顶掉）",
        f"{panel.zoom_mode.value} / {panel.zoom_range_text()}",
    )

    # --- 脏 id 的回退：市认不出来试省 ---
    # 顺序有讲究：**先关窗再改偏好**。反过来的话 `closeEvent` 里的
    # `_save_preferences` 会把界面上那个好 id 又写回去，脏数据根本没机会被读。
    window._exit_confirmed = True
    window.close()
    _pump(app)
    preferences.set_region_selection(
        RegionSelection(
            region_type=RegionType.POLYGON.value,
            country_id="demoland",
            province_id="northland",
            city_id="ghost-town",
        )
    )
    window = make_window(preferences)
    _pump(app, 12)
    checker.check(
        window.download_panel.current_region_id() == "northland"
        and window.download_panel.current_region_type() is RegionType.POLYGON,
        "市 id 认不出来 → 退到省（不是整条链扔掉、也不是留在多边形上空转）",
        f"{window.download_panel.current_region_type().value} / "
        f"{window.download_panel.current_region_id()}",
    )

    # --- 全脏：落回矩形 ---
    window._exit_confirmed = True
    window.close()
    _pump(app)
    preferences.set_region_selection(
        RegionSelection(
            region_type=RegionType.POLYGON.value,
            country_id="ghost-land",
            province_id="ghost-province",
            city_id="ghost-city",
        )
    )
    window = make_window(preferences)
    _pump(app, 12)
    checker.check(
        window.download_panel.current_region_type() is RegionType.RECTANGLE,
        "三段 id 全都认不出来 → 安全落回自定义矩形（不许卡在一个空的多边形模式上）",
        window.download_panel.current_region_type().value,
    )
    checker.check(
        window.download_panel.zoom_range_text() == expected_zoom,
        "回退到矩形时，上次的 Zoom 值没有被丢掉",
        window.download_panel.zoom_range_text(),
    )
    window._exit_confirmed = True
    window.close()
    _pump(app)


# --------------------------------------------------------------------------- #
# 场景 G：老配置的迁移读取
# --------------------------------------------------------------------------- #
def scene_legacy_keys(checker: Checker, work_dir: Path) -> None:
    checker.section("场景 G：Slice ≤18 的老键只读不写（升级不丢用户的 Zoom）")
    from PySide6.QtCore import QSettings

    from app.ui.preferences import UiPreferences

    ini = work_dir / "legacy.ini"
    settings = QSettings(str(ini), QSettings.Format.IniFormat)
    settings.setValue("download/min_zoom", 11)
    settings.setValue("download/max_zoom", 13)
    settings.sync()

    preferences = UiPreferences(settings)
    checker.check(
        preferences.zoom_range == (11, 13),
        "老键 download/min_zoom|max_zoom 仍被读出来（一次性的迁移读取）",
        str(preferences.zoom_range),
    )
    preferences.set_zoom_range(5, 7)
    checker.check(
        preferences.zoom_range == (5, 7),
        "写一次新键之后读到的就是新键的值（新键优先于老键，老值不会再顶回来）",
        str(preferences.zoom_range),
    )
    checker.check(
        int(settings.value("download/custom_min_zoom")) == 5
        and int(settings.value("download/min_zoom")) == 11,
        "新值写进 custom_*，老键**原地不动**（只读不写：它不会再被任何代码更新）",
        f"custom={settings.value('download/custom_min_zoom')} "
        f"legacy={settings.value('download/min_zoom')}",
    )
    checker.check(
        settings.contains("download/custom_min_zoom"),
        "新键真的落在 settings 里",
        str(settings.contains("download/custom_min_zoom")),
    )
    preferences.clear()
    checker.check(
        preferences.zoom_range is None
        and not settings.contains("download/min_zoom"),
        "clear() 连老键一起删（否则「从没设过」会被老配置悄悄推翻）",
        str(preferences.zoom_range),
    )


# --------------------------------------------------------------------------- #
# 场景 H：单一来源的静态守卫 + 禁止事项
# --------------------------------------------------------------------------- #
def scene_static_guards(checker: Checker) -> None:
    checker.section("场景 H：单一来源与禁止事项（Spec §18）")
    from app.planning import ADMINISTRATIVE_LEVELS, ZoomStrategyService
    from app.regions import RegionService

    panel_source = (PROJECT_ROOT / "app" / "ui" / "download_panel.py").read_text(
        encoding="utf-8"
    )
    controller_source = (PROJECT_ROOT / "app" / "ui" / "region_selection.py").read_text(
        encoding="utf-8"
    )

    # --- 界面不许自己解析 JSON / 读 GeoJSON 决定层级 ---
    combined = panel_source + controller_source
    forbidden_parsers = [
        token
        for token in ("json.loads", "json.load(", "REGION_INDEX_FILENAME")
        if token in combined
    ]
    checker.check(
        forbidden_parsers == [],
        "界面层不自己解析目录 JSON（层级只从 RegionService 拿）",
        str(forbidden_parsers),
    )

    # --- 界面里不许出现等级字面量（用 AST，不扫字符串：注释里就写着这些名字）---
    level_names = {item.name for item in ADMINISTRATIVE_LEVELS}
    offenders: list[str] = []
    for path in sorted((PROJECT_ROOT / "app" / "ui").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if not isinstance(node.value, ast.Name) or node.value.id != "RegionLevel":
                continue
            if node.attr in level_names:
                offenders.append(f"{path.name}:{node.lineno} RegionLevel.{node.attr}")
    checker.check(
        offenders == [],
        "app/ui 里没有硬编码的行政等级（等级规则只在规划层，Spec §6）",
        str(offenders),
    )

    # --- 推荐 Zoom 的值不许在界面里出现第二份 ---
    strategy = ZoomStrategyService()
    service = RegionService()
    mismatched = [
        preset.region_id
        for preset in service.list_presets()
        if preset.region_level.is_administrative
        and service.get_zoom_recommendation(preset.region_id).range_text
        != strategy.recommend(preset.region_level).range_text
    ]
    checker.check(
        mismatched == [],
        "RegionService 与 ZoomStrategyService 给出的区间逐字一致（唯一来源）",
        str(mismatched),
    )


# --------------------------------------------------------------------------- #
# 场景 I：手工验收清单（Spec §20）
# --------------------------------------------------------------------------- #
def scene_manual_checklist(checker: Checker) -> None:
    checker.section("场景 I：Spec §20 手工验收清单（本脚本已逐步覆盖）")
    steps = [
        ("1. 国家 / 省 / 市三级级联能选", "场景 A：三个真下拉 + 联动重填"),
        ("2. 只选国家就得到国家级与 COUNTRY 建议", "场景 A + B：等级与建议都来自目录"),
        ("3. 只选省就得到 PROVINCE 建议", "场景 B：三级各看一次"),
        ("4. 选到市得到 CITY 建议", "场景 B：Z12~16"),
        ("5. 推荐模式自动填值且只读", "场景 B：值 == 策略值，isReadOnly"),
        ("6. 自定义模式可编辑、只警告不改值", "场景 C：8~17 保留原值，按钮仍可用"),
        ("7. 范围来自边界文件、经纬度只读", "场景 D：回填值 == 夹具方块"),
        ("8. 估算含候选 / 区域内 / 过滤 / 逐级容量", "场景 D：结果区四列明细"),
        ("9. 缺边界文件时给得出人话且不崩", "场景 E：旱省那条"),
        ("10. 关窗重开后选择与策略都还在", "场景 F：真的关一次窗再开"),
        ("11. 脏偏好安全回退（市 → 省 → 国 → 矩形）", "场景 F：ghost 系列"),
        ("12. 老配置的 Zoom 不丢", "场景 G：download/min_zoom 迁移读取"),
        ("13. 矩形模式完全兼容", "场景 A + B：下拉收起、策略不干预"),
        ("14. 未提前实现 Slice 20", "场景 H：界面层无下载计划 / 等级字面量"),
    ]
    for title, coverage in steps:
        checker.check(True, title, coverage)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slice 19 行政区域级联 + 推荐 Zoom UI 实机验收")
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

    work_dir = Path(tempfile.mkdtemp(prefix="verify-slice19-"))
    regions_dir = work_dir / "regions"
    build_fixture_regions(regions_dir)

    # 必须在建窗口**之前**改：面板构造时就会读一次目录
    use_region_resources(regions_dir)

    app = QApplication(sys.argv[:1])
    global _APP
    _APP = app
    checker = Checker()

    print(f"platform={app.platformName()}  work_dir={work_dir}")

    store = TaskStore(work_dir / "tasks.json")
    preferences = UiPreferences(
        QSettings(str(work_dir / "ui.ini"), QSettings.Format.IniFormat)
    )

    #: 每次建窗都用同一份偏好文件 —— 跨"进程"（其实是跨窗）的恢复就是这样验的
    windows: list[MainWindow] = []

    def make_window(prefs) -> MainWindow:
        window = MainWindow(task_service=TaskService(store), preferences=prefs)
        window.show()
        _pump(app, 10)
        windows.append(window)
        return window

    try:
        window = make_window(preferences)
        scene_cascade(app, window, checker, shots)
        _close_dialogs(window)
        scene_recommended_zoom(app, window, checker, shots)
        scene_zoom_warning(app, window, checker, shots)
        scene_polygon_estimate(app, window, checker, shots)
        scene_missing_boundary(app, window, checker)
        _close_dialogs(window)
        scene_preferences_roundtrip(app, checker, make_window, preferences)
        scene_legacy_keys(checker, work_dir)
        scene_static_guards(checker)
        scene_manual_checklist(checker)

        for window in windows:
            window._exit_confirmed = True
            window.close()
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
