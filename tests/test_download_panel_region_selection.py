"""DownloadPanel 的行政区级联与 Zoom 模式（Slice 19，Spec §1 ~ §7 / §13 / §14）。

``tests/test_region_selection_controller.py`` 守的是"规则对不对"，
这一组守的是"规则有没有真的落到控件上"：哪一行在什么时候出现、Min/Max 是不是
只读、Radio 互斥有没有生效、切模式的顺序会不会把用户的值顶掉。

为什么必须有一条真的装配控件（哪怕 offscreen）的用例：这些行为全在
``setVisible`` / ``setReadOnly`` / ``QRadioButton`` 上，纯逻辑用例一条都看不见
—— Slice 14 的教训是"同一份布局里两个同 id，编译过、单测绿、真机必崩"。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from app.geo import RegionType
from app.planning import RegionLevel
from app.tasks import TaskService, TaskStore
from app.ui.download_panel import DownloadPanel
from app.ui.main_window import MainWindow
from app.ui.preferences import RegionSelection
from app.ui.region_selection import ZoomMode


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture
def region_resources(monkeypatch, region_root: Path) -> Path:
    """把内置资源目录重定向到合成树（6 条记录、三层）。

    只改 ``app.geo.region.REGION_RESOURCE_DIR`` 这一个全局：目录服务每次取根
    目录时都重新读它，所以**在构造控件之前**改好就够了 —— 这模拟的正是
    "用户照着提示把数据放好了"。
    """
    from app.geo import region as region_module

    monkeypatch.setattr(region_module, "REGION_RESOURCE_DIR", region_root)
    return region_root


@pytest.fixture
def panel(qapp: QApplication, region_resources: Path) -> DownloadPanel:
    """挂在合成目录上的下载面板（不装配主窗口）。"""
    return DownloadPanel()


@pytest.fixture
def window_factory(qapp, tasks_file: Path, preferences, region_resources: Path):
    """造主窗口的工厂（偏好与任务记录都注入临时路径）。"""
    created: list[MainWindow] = []

    def factory() -> MainWindow:
        win = MainWindow(
            task_service=TaskService(TaskStore(tasks_file)), preferences=preferences
        )
        created.append(win)
        return win

    yield factory
    for win in created:
        win._exit_confirmed = True
        win.close()


def _select(panel: DownloadPanel, country=None, province=None, city=None) -> None:
    """按 id 依次点三段下拉（``None`` 表示不点那一段）。"""
    combos = {
        "country": panel.country_combo,
        "province": panel.province_combo,
        "city": panel.city_combo,
    }
    for name, region_id in (("country", country), ("province", province), ("city", city)):
        if region_id is None:
            continue
        combo = combos[name]
        for index in range(combo.count()):
            entry = combo.itemData(index)
            if entry is not None and entry.id == region_id:
                combo.setCurrentIndex(index)
                break
        else:  # pragma: no cover - 用例写错了 id 时立刻炸，而不是静默不选
            raise AssertionError(f"{name} 下拉里没有 {region_id}")


# --------------------------------------------------------------------------- #
# 初值：矩形模式与 Slice ≤18 一模一样（Spec §7）
# --------------------------------------------------------------------------- #
def test_rectangle_mode_is_unchanged(panel: DownloadPanel) -> None:
    assert panel.current_region_type() is RegionType.RECTANGLE
    assert panel.current_region_level() is RegionLevel.CUSTOM
    assert panel.current_region_id() is None
    assert panel.zoom_mode is ZoomMode.RECOMMENDED, "模式本身有默认值……"
    assert panel._min_zoom_spin.isReadOnly() is False, "……但矩形模式下不生效"
    assert panel._max_zoom_spin.value() > panel._min_zoom_spin.value()

    for widget in panel._polygon_only_widgets():
        assert widget.isHidden() is True, f"{widget} 在矩形模式下必须收起"


def test_rectangle_mode_does_not_apply_any_level_policy(panel: DownloadPanel) -> None:
    """Spec §7：不强制使用国家 / 省 / 市的推荐策略。"""
    panel.set_zoom_range(8, 17)
    panel.set_region_type(RegionType.RECTANGLE)

    assert (panel._min_zoom_spin.value(), panel._max_zoom_spin.value()) == (8, 17)
    assert panel.zoom_policy_text == ""


# --------------------------------------------------------------------------- #
# 切到行政区域：级联出现 + 推荐值自动填入（Spec §1 / §4）
# --------------------------------------------------------------------------- #
def test_polygon_mode_shows_the_cascade(panel: DownloadPanel) -> None:
    panel.set_region_type(RegionType.POLYGON)

    assert panel.country_combo.itemData(0).id == "demoland"  # type: ignore[union-attr]
    for widget in panel._polygon_only_widgets():
        assert widget.isHidden() is False

    assert panel.current_region_level() is RegionLevel.COUNTRY
    # 「下载级别」是**推导**出来的（Spec §2）：等级中文名 + 面包屑，都来自目录
    assert panel.level_text == f"{RegionLevel.COUNTRY.display_name} · 示例国"


def test_recommended_zoom_is_applied_and_readonly(panel: DownloadPanel) -> None:
    """Spec §4：推荐模式下 Min/Max 自动更新为推荐值。"""
    panel.set_region_type(RegionType.POLYGON)
    expected = panel.region_controller.recommended_range
    assert expected is not None

    assert (panel._min_zoom_spin.value(), panel._max_zoom_spin.value()) == expected
    assert panel._min_zoom_spin.isReadOnly() is True
    assert panel._max_zoom_spin.isReadOnly() is True
    assert panel.zoom_mode_buttons[0].isChecked() is True


@pytest.mark.parametrize(
    ("region_id", "level"),
    [
        ("demoland", RegionLevel.COUNTRY),
        ("northland", RegionLevel.PROVINCE),
        ("rivertown", RegionLevel.CITY),
    ],
)
def test_zoom_follows_the_level(
    panel: DownloadPanel, region_id: str, level: RegionLevel
) -> None:
    """选不同的级别 → 推荐值与界面文案都跟着换（值来自策略表）。"""
    panel.set_region_type(RegionType.POLYGON)
    assert panel.set_region_id(region_id) is True

    recommendation = panel.region_controller.recommendation(
        panel._min_zoom_spin.value(), panel._max_zoom_spin.value()
    )
    assert recommendation is not None
    assert panel.current_region_level() is level
    assert (panel._min_zoom_spin.value(), panel._max_zoom_spin.value()) == (
        recommendation.min_zoom,
        recommendation.max_zoom,
    )
    assert panel.zoom_policy_text.splitlines()[0] == f"推荐 Zoom：{recommendation.range_text}"
    assert panel.zoom_explain_text == recommendation.reason


def test_switching_region_reapplies_the_recommendation(panel: DownloadPanel) -> None:
    """Spec §4：区域切换后重新应用对应推荐。"""
    panel.set_region_type(RegionType.POLYGON)
    country_range = panel.region_controller.recommended_range

    assert panel.set_region_id("rivertown") is True
    city_range = panel.region_controller.recommended_range
    assert city_range != country_range, "前提：两级的推荐值确实不同"
    assert (panel._min_zoom_spin.value(), panel._max_zoom_spin.value()) == city_range


# --------------------------------------------------------------------------- #
# 级联刷新（Spec §2）
# --------------------------------------------------------------------------- #
def test_cascade_refreshes_provinces_and_cities(panel: DownloadPanel) -> None:
    panel.set_region_type(RegionType.POLYGON)

    _select(panel, province="northland")
    north = [
        panel.city_combo.itemData(i).id
        for i in range(1, panel.city_combo.count())
    ]
    assert north == ["rivertown", "hilltown"]

    _select(panel, province="southland")
    south = [
        panel.city_combo.itemData(i).id
        for i in range(1, panel.city_combo.count())
    ]
    assert south == ["lakecity"]
    assert panel.current_region_id() == "southland", "换省后城市必须被清空"


def test_unlimited_city_falls_back_to_the_province(panel: DownloadPanel) -> None:
    """Spec §2：选了省但没选市 → PROVINCE。"""
    panel.set_region_type(RegionType.POLYGON)
    _select(panel, province="northland")

    panel.city_combo.setCurrentIndex(0)  # 「全部（不限定城市）」
    assert panel.current_region_id() == "northland"
    assert panel.current_region_level() is RegionLevel.PROVINCE


def test_all_three_levels_reach_the_city(panel: DownloadPanel) -> None:
    panel.set_region_type(RegionType.POLYGON)
    _select(panel, province="northland", city="rivertown")

    assert panel.current_region_id() == "rivertown"
    assert panel.current_region_level() is RegionLevel.CITY
    assert "示例国 / 示例北省 / 示例河市" in panel.level_text


def test_set_region_id_fills_the_whole_cascade(panel: DownloadPanel) -> None:
    """按 id 恢复（任务记录 / 偏好）时，三段下拉要一起对上。"""
    assert panel.set_region_id("hilltown") is True

    assert panel.country_combo.currentData().id == "demoland"
    assert panel.province_combo.currentData().id == "northland"
    assert panel.city_combo.currentData().id == "hilltown"

    assert panel.set_region_id("hangzhou") is False
    assert panel.region_error


# --------------------------------------------------------------------------- #
# 模式切换（Spec §5 / §6）
# --------------------------------------------------------------------------- #
def test_switching_to_custom_keeps_the_current_values(panel: DownloadPanel) -> None:
    """Spec §6：推荐 → 自定义，保留当前 Min/Max 作为初始值。"""
    panel.set_region_type(RegionType.POLYGON)
    before = (panel._min_zoom_spin.value(), panel._max_zoom_spin.value())

    panel.zoom_mode_buttons[1].click()

    assert panel.zoom_mode is ZoomMode.CUSTOM
    assert (panel._min_zoom_spin.value(), panel._max_zoom_spin.value()) == before
    assert panel._min_zoom_spin.isReadOnly() is False
    assert panel._max_zoom_spin.isReadOnly() is False


def test_switching_back_to_recommended_reapplies(panel: DownloadPanel) -> None:
    """Spec §6：自定义 → 推荐，重新应用当前 Region 的推荐 Zoom。"""
    panel.set_region_type(RegionType.POLYGON)
    panel.set_zoom_range(8, 18)
    assert (panel._min_zoom_spin.value(), panel._max_zoom_spin.value()) == (8, 18)

    panel.zoom_mode_buttons[0].click()

    assert panel.zoom_mode is ZoomMode.RECOMMENDED
    assert panel._min_zoom_spin.isReadOnly() is True
    expected = panel.region_controller.recommended_range
    assert (panel._min_zoom_spin.value(), panel._max_zoom_spin.value()) == expected


def test_custom_warning_never_touches_the_user_values(panel: DownloadPanel) -> None:
    """中国 + Z8~18：只出警告，值一个都不改（Spec §5）。"""
    panel.set_region_type(RegionType.POLYGON)
    panel.set_zoom_range(8, 18)

    assert "警告：" in panel.zoom_policy_text
    assert "超出全国级推荐范围" in panel.zoom_policy_text
    assert (panel._min_zoom_spin.value(), panel._max_zoom_spin.value()) == (8, 18)

    form = panel.get_form_data()
    assert (form["min_zoom"], form["max_zoom"]) == (8, 18)
    assert form["zoom_mode"] is ZoomMode.CUSTOM


def test_set_zoom_range_switches_to_custom(panel: DownloadPanel) -> None:
    """``set_zoom_range`` 的语义是"这就是我要用的两个数" → 必须切到自定义，
    否则下一次区域变化会把它们**悄悄**换成推荐值。"""
    panel.set_region_type(RegionType.POLYGON)
    panel.set_zoom_range(10, 12)

    assert panel.zoom_mode is ZoomMode.CUSTOM
    assert panel.set_region_id("rivertown") is True
    assert (panel._min_zoom_spin.value(), panel._max_zoom_spin.value()) == (10, 12)


def test_zoom_mode_radios_are_mutually_exclusive(panel: DownloadPanel) -> None:
    panel.set_region_type(RegionType.POLYGON)

    panel.zoom_mode_buttons[0].click()
    assert panel.zoom_mode_buttons[0].isChecked() is True
    assert panel.zoom_mode_buttons[1].isChecked() is False

    panel.zoom_mode_buttons[1].click()
    assert panel.zoom_mode_buttons[1].isChecked() is True
    assert panel.zoom_mode_buttons[0].isChecked() is False
    assert panel.zoom_mode is ZoomMode.CUSTOM


def test_inverted_custom_range_shows_a_message_instead_of_raising(
    panel: DownloadPanel,
) -> None:
    """Min > Max：显示一句人话，不抛异常（抛了会被 Qt 槽静默吞掉）。"""
    panel.set_region_type(RegionType.POLYGON)
    panel.set_zoom_range(18, 4)

    assert "Zoom 策略不可用" in panel.zoom_policy_text


# --------------------------------------------------------------------------- #
# 边界与计算（Spec §8 / §9）
# --------------------------------------------------------------------------- #
def test_polygon_estimate_uses_the_selected_tiles(window_factory) -> None:
    """行政区域点「计算」走的是 Slice 16 的多边形链路，且结果区写明级别。

    "点一下按钮"必须装配主窗口：面板自己只发信号，真正算瓦片的是 MainWindow。
    在裸面板上点它只会静默什么都不发生 —— 这条用例正是为了不把那种"看着通过"的
    假绿写进仓库。
    """
    from app.ui.download_panel import ACTION_CALCULATE

    panel = window_factory().download_panel
    panel.set_region_type(RegionType.POLYGON)
    panel.set_region_id("rivertown")
    panel.set_zoom_range(10, 10)
    panel.action_buttons[ACTION_CALCULATE].click()

    text = panel.result_label.text()
    assert text.splitlines()[0] == "区域：示例河市"
    assert "级别：城市级" in text
    assert "推荐 Zoom：Z12~16" in text
    assert "区域内瓦片" in text and "候选瓦片" in text
    assert "Zoom 明细（候选 / 选中 / 过滤 / 预计容量）：" in text
    assert panel.current_region() is not None


def test_bounds_come_from_the_geojson(panel: DownloadPanel) -> None:
    """Spec §8：只读框里是 GeoJSON 的真实外包矩形。"""
    panel.set_region_type(RegionType.POLYGON)
    panel.set_region_id("rivertown")

    assert panel._west_spin.isReadOnly() is True
    assert (
        panel._west_spin.value(),
        panel._south_spin.value(),
        panel._east_spin.value(),
        panel._north_spin.value(),
    ) == (110.0, 33.0, 111.0, 34.0)
    assert "110.0000" in panel.bounds_hint_label.text()


def test_download_region_bounds_are_the_ones_on_screen(panel: DownloadPanel) -> None:
    """交给下载器的 ``bounds`` 必须是**界面上那四个数**，不是几何的原始外包矩形。

    这条是实机验收脚本抓出来的：夹具的边界恰好**瓦片对齐**（bbox 的四条边正好压在
    瓦片边上），于是

    * 面板把几何的 9 位小数（``115.927734375``）交给下载器 → 候选范围 7×8；
    * 同一份几何被写进 MBTiles metadata 时只保留 6 位小数（``115.927734``）；
    * Inspector 从 metadata 反推候选范围 → 变成 8×9（多出来的那一列 / 一行只是
      **碰到**了多边形边沿，按 Slice 16 的规则"碰边也算相交"就该算进期望值）。

    结果是**完整的包被判成"缺失 10 张"**，打包器以"MBTiles 存在警告"为由拒绝出包
    —— 一处纯粹由精度不一致造出来的假警报。把界面显示的（= 写进 metadata 的）
    那一份交给下载器，往返就自洽了。
    """
    import json

    panel.set_region_type(RegionType.POLYGON)
    panel.set_region_id("rivertown")

    # 夹具里那条边是 ``110.0``（正好 6 位小数以内）—— 不改成带更多位小数的坐标，
    # "按界面那份传"与"按几何传"会**恰好相等**，这条用例就成了空转。
    path = panel.region_controller.service.root / "city" / "rivertown.geojson"
    payload = json.loads(path.read_text(encoding="utf-8"))
    ring = payload["geometry"]["coordinates"][0]
    for index in (0, 3, 4):  # 西边那条边上的三个点
        ring[index][0] = 110.123456789
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    panel.reload_region_boundary()

    region = panel.current_region()
    assert region is not None, "边界文件仍在，区域应当拿得到"
    on_screen = panel.current_bounds()
    assert region.bounds == on_screen, "下载依据必须与界面上显示的一致"
    # 输入框是 6 位小数（QDoubleSpinBox），几何是 9 位 —— 这条只有在"按界面那份传"
    # 时才成立，也就是上一条断言真的在守上面那段推理。
    assert on_screen.west == 110.123457
    assert on_screen.west != 110.123456789
    assert region.geojson_path, "几何仍然来自边界文件（不是退回矩形）"


def test_missing_boundary_keeps_the_polygon_mode(window_factory, monkeypatch) -> None:
    """边界文件缺失 → 明确失败，但**不许退化成矩形**（Slice 16 的红线）。

    这里连"点计算"一起验：错误必须是**这一次**算出来的，而不是结果区里
    还挂着别的东西。
    """
    from app.ui.download_panel import ACTION_CALCULATE

    window = window_factory()
    dropped: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window, "_show_error_dialog", lambda title, message: dropped.append((title, message))
    )
    panel = window.download_panel
    panel.set_region_type(RegionType.POLYGON)
    panel.set_region_id("rivertown")
    panel.region_controller.service.root.joinpath("city", "rivertown.geojson").unlink()
    panel.reload_region_boundary()

    assert panel.current_region() is None
    assert "rivertown.geojson" in panel.current_region_error()
    assert panel.current_region_type() is RegionType.POLYGON, "不许悄悄切回矩形"

    panel.action_buttons[ACTION_CALCULATE].click()
    assert "计算失败" in panel.result_label.text()
    assert dropped and "rivertown.geojson" in dropped[0][1]


# --------------------------------------------------------------------------- #
# 偏好恢复（Spec §14）
# --------------------------------------------------------------------------- #
def test_region_selection_is_restored(window_factory, preferences) -> None:
    preferences.set_region_selection(
        RegionSelection(
            region_type="polygon",
            country_id="demoland",
            province_id="northland",
            city_id="rivertown",
        )
    )
    preferences.set_zoom_mode("custom")
    preferences.set_zoom_range(13, 15)

    panel = window_factory().download_panel

    assert panel.current_region_type() is RegionType.POLYGON
    assert panel.current_region_id() == "rivertown"
    assert panel.zoom_mode is ZoomMode.CUSTOM
    assert (panel._min_zoom_spin.value(), panel._max_zoom_spin.value()) == (13, 15)


def test_unknown_region_falls_back_to_the_surviving_ancestor(
    window_factory, preferences
) -> None:
    """老 id 不存在时逐级回退：市没了用省，而不是整条链一起扔掉。"""
    preferences.set_region_selection(
        RegionSelection(
            region_type="polygon",
            country_id="demoland",
            province_id="northland",
            city_id="atlantis",
        )
    )

    panel = window_factory().download_panel

    assert panel.current_region_type() is RegionType.POLYGON
    assert panel.current_region_id() == "northland"


def test_completely_unknown_region_falls_back_to_rectangle(
    window_factory, preferences
) -> None:
    preferences.set_region_selection(
        RegionSelection(
            region_type="polygon",
            country_id="atlantis",
            province_id="atlantis",
            city_id="atlantis",
        )
    )

    panel = window_factory().download_panel

    assert panel.current_region_type() is RegionType.RECTANGLE
    assert panel.current_region_id() is None


def test_no_stored_selection_keeps_the_default(window_factory, preferences) -> None:
    assert window_factory().download_panel.current_region_type() is RegionType.RECTANGLE


def test_preferences_round_trip(window_factory, preferences) -> None:
    """关窗时存下来的三段 id，下次打开要一模一样（Spec §14）。"""
    window = window_factory()
    window.download_panel.set_region_id("rivertown")
    window.download_panel.set_zoom_mode(ZoomMode.RECOMMENDED)
    window._save_preferences()

    stored = preferences.region_selection
    assert stored.region_type == "polygon"
    assert stored.country_id == "demoland"
    assert stored.province_id == "northland"
    assert stored.city_id == "rivertown"
    assert preferences.zoom_mode == "recommended"
