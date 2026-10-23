"""界面偏好持久化（Slice 7，Spec §22）。

这一层要同时回答两个问题，而且第二个比第一个重要得多：

1. 输出目录 / 地图类型 / 缩放区间 / 窗口位置能不能"下次打开还在那儿"；
2. **有没有任何一条路径把 Token 写进去**。

第 2 条不是"顺便检查一下"：注册表是一个任何程序都能读、还会被各种"清理工具"
打包上传的地方，而 Token 是用户唯一真正的凭据（Spec §9）。因此这里把
"不存密钥"从一个约定变成一条会用 ``ValueError`` 说话的不变量。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QWidget

from app.config.settings import APP_NAME, ORG_NAME
from app.ui.preferences import SECRET_KEY_FRAGMENTS, RegionSelection, UiPreferences


@pytest.fixture()
def isolated_prefs_factory():
    """造若干个**互不可见**的 :class:`UiPreferences`，各自一个临时 ini。

    需要它是因为 ``QSettings(QSettings.Format.IniFormat)`` 这个重载并不存在
    ——要换落点必须给出**文件路径**。用例结束后统一清空并删除临时目录，
    保证注册表与本机的临时目录都不会被留下垃圾。

    Yields:
        一个无参可调用对象，每次调用返回一个全新的 :class:`UiPreferences`。
    """
    opened: list[QSettings] = []
    directories: list[Path] = []

    def make() -> UiPreferences:
        directory = Path(tempfile.mkdtemp(prefix="prefs-test-"))
        directories.append(directory)
        settings = QSettings(str(directory / "ui.ini"), QSettings.Format.IniFormat)
        opened.append(settings)
        return UiPreferences(settings)

    yield make

    for settings in opened:
        settings.clear()
    for directory in directories:
        shutil.rmtree(directory, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 下载配置的读写
# --------------------------------------------------------------------------- #
def test_defaults_are_none(preferences: UiPreferences) -> None:
    """一片空白的 preferences 读出来全是 ``None``（由调用方用默认值兜底）。"""
    assert preferences.output_dir is None
    assert preferences.map_type is None
    assert preferences.zoom_range is None
    assert preferences.stored_keys() == ()


def test_output_dir_round_trip(preferences: UiPreferences) -> None:
    preferences.set_output_dir("D:/maps")
    assert preferences.output_dir == "D:/maps"


def test_map_type_round_trip(preferences: UiPreferences) -> None:
    preferences.set_map_type("image")
    assert preferences.map_type == "image"


def test_zoom_range_round_trip(preferences: UiPreferences) -> None:
    preferences.set_zoom_range(10, 14)
    assert preferences.zoom_range == (10, 14)


def test_zoom_range_needs_both_ends(preferences: UiPreferences) -> None:
    """只记住一半的缩放区间没有意义——不完整的记录应当当作"没有记录"。

    否则会出现"上次的 min_zoom 被继承、max_zoom 却被重置"这种半新半旧的
    状态，而用户完全无法理解为什么缩放区间变成了 10~17。
    """
    preferences.settings.setValue("download/min_zoom", 10)
    assert preferences.zoom_range is None


def test_empty_string_clears_the_value(preferences: UiPreferences) -> None:
    preferences.set_output_dir("D:/maps")
    preferences.set_output_dir("")
    assert preferences.output_dir is None


def test_none_clears_the_value(preferences: UiPreferences) -> None:
    preferences.set_map_type("vector")
    preferences.set_map_type(None)
    assert preferences.map_type is None


def test_whitespace_is_trimmed(preferences: UiPreferences) -> None:
    preferences.set_output_dir("  D:/maps  ")
    assert preferences.output_dir == "D:/maps"


def test_garbage_values_do_not_raise(preferences: UiPreferences) -> None:
    """注册表被手工改过 / 旧版本写的是别的类型 → 当作"没有记录"，不能崩。

    偏好是锦上添花；一个读到脏值的注册表不该让程序起不来。
    """
    preferences.settings.setValue("download/min_zoom", "十二")
    preferences.settings.setValue("download/max_zoom", object())

    assert preferences.zoom_range is None


# --------------------------------------------------------------------------- #
# "不存 Token" 的不变量（Spec §9 / §22）
# --------------------------------------------------------------------------- #
def test_managed_keys_contain_no_secret_fragments(
    qapp, preferences: UiPreferences
) -> None:
    """把所有能写的键都写一遍，实际落盘的键名里不能沾上任何密钥片段。

    依赖 ``qapp`` 是因为要 ``save_geometry`` —— 造一个 ``QWidget`` 而没有
    ``QApplication`` 会让 Qt 直接中止进程（没有 Python 异常可捕），
    整个测试会话会静默消失。
    """
    preferences.set_output_dir("D:/maps")
    preferences.set_map_type("vector")
    preferences.set_zoom_range(8, 17)
    # Slice 19 新增的键也要写一遍：漏掉它们的话，这条守卫对新增的那几行
    # 等于没跑 —— 而"行政区 id"这一类键名恰好是最容易顺手带上敏感词的地方。
    preferences.set_region_selection(
        RegionSelection(
            region_type="polygon",
            country_id="china",
            province_id="zhejiang",
            city_id="hangzhou",
        )
    )
    preferences.set_zoom_mode("recommended")
    preferences.save_geometry(QWidget())

    keys = preferences.stored_keys()
    assert keys, "至少要真的写出几个键，否则这条断言是空转的"
    assert "download/city_id" in keys, "前提：新增的键确实写进去了"

    for key in keys:
        lowered = key.lower()
        for fragment in SECRET_KEY_FRAGMENTS:
            assert fragment not in lowered, f"键名 {key!r} 命中了密钥片段 {fragment!r}"


def test_writing_a_token_like_key_is_refused(preferences: UiPreferences) -> None:
    """写一个名字像密钥的键必须直接抛 ``ValueError``。

    这不是"防御性编程"式的空转：万一将来有人顺手加了个 ``set_token``，
    第一个测试就会红，而不是等到某天有人翻注册表才发现。
    """
    for key in ("download/token", "download/TK", "app/password", "x/secret", "a/passwd"):
        with pytest.raises(ValueError, match="不允许保存疑似密钥的键"):
            preferences._assert_not_secret(key)


def test_secret_fragments_cover_the_obvious_cases() -> None:
    assert "token" in SECRET_KEY_FRAGMENTS
    assert "tk" in SECRET_KEY_FRAGMENTS
    assert "password" in SECRET_KEY_FRAGMENTS


def test_no_key_in_the_ini_looks_like_a_credential(preferences: UiPreferences) -> None:
    """从**底层 QSettings** 的角度再确认一次（不依赖 ``stored_keys`` 的实现）。"""
    preferences.set_output_dir("D:/maps")
    preferences.set_map_type("vector")
    preferences.set_zoom_range(8, 17)

    for key in preferences.settings.allKeys():
        assert "token" not in key.lower()


# --------------------------------------------------------------------------- #
# clear()
# --------------------------------------------------------------------------- #
def test_clear_removes_managed_values(preferences: UiPreferences) -> None:
    preferences.set_output_dir("D:/maps")
    preferences.set_map_type("image")
    preferences.set_zoom_range(10, 12)

    preferences.clear()

    assert preferences.output_dir is None
    assert preferences.map_type is None
    assert preferences.zoom_range is None
    assert preferences.stored_keys() == ()


def test_clear_leaves_foreign_keys_alone(preferences: UiPreferences) -> None:
    """只清自己管理的键，不动键空间里的其它内容。

    真实场景：同一个应用将来可能还有别的模块往 QSettings 里写东西，
    "清空偏好"不该变成"清空所有设置"。
    """
    preferences.settings.setValue("other/module/setting", "keep me")
    preferences.set_output_dir("D:/maps")

    preferences.clear()

    assert preferences.settings.value("other/module/setting") == "keep me"


# --------------------------------------------------------------------------- #
# 窗口几何
# --------------------------------------------------------------------------- #
def test_geometry_round_trip(qapp, isolated_prefs_factory) -> None:
    """保存 → 恢复：窗口尺寸必须真的回来了，而不是"resize 了但它没理我"。

    尺寸刻意取 640×480：``offscreen`` 平台的虚拟屏幕是 **800×800**，而
    ``restoreGeometry`` 会把窗口夹到屏幕范围内（存 1024 宽会恢复成 798）。
    用超出屏幕的尺寸断言"恢复了"就变成了在测 Qt 的夹取逻辑，而不是本项目
    的偏好读写。
    """
    prefs = isolated_prefs_factory()

    first = QWidget()
    first.resize(640, 480)
    prefs.save_geometry(first)

    second = QWidget()
    second.resize(200, 150)  # 先做成一个明显不同的尺寸
    assert prefs.restore_geometry(second) is True
    assert (second.width(), second.height()) == (640, 480)

    first.deleteLater()
    second.deleteLater()


def test_restore_without_archive_returns_false(qapp, isolated_prefs_factory) -> None:
    """没有存档时返回 ``False``，窗口保持默认尺寸（调用方据此决定要不要记日志）。"""
    prefs = isolated_prefs_factory()
    widget = QWidget()
    widget.resize(320, 200)

    assert prefs.restore_geometry(widget) is False
    assert (widget.width(), widget.height()) == (320, 200)

    widget.deleteLater()


def test_corrupted_geometry_is_ignored(qapp, preferences: UiPreferences) -> None:
    """被改坏的存档不能把程序拦在启动路径上。"""
    preferences.settings.setValue("window/geometry", "not a byte array")
    widget = QWidget()
    widget.resize(320, 200)

    assert preferences.restore_geometry(widget) is False
    assert (widget.width(), widget.height()) == (320, 200)

    widget.deleteLater()


# --------------------------------------------------------------------------- #
# 隔离（这正是测试夹具存在的理由）
# --------------------------------------------------------------------------- #
def test_two_instances_do_not_share_state(isolated_prefs_factory) -> None:
    """两个独立落点的实例互不可见。

    这条断言的是"测试为什么能用临时 ini 代替注册表"的前提：只要落点不同，
    用例之间就是一张白纸。生产里落点是注册表，测试里是各自的临时文件。
    """
    first = isolated_prefs_factory()
    second = isolated_prefs_factory()

    first.set_output_dir("D:/first")

    assert first.output_dir == "D:/first"
    assert second.output_dir is None


def test_production_default_points_at_the_registry(qapp) -> None:
    """默认构造（不注入）时的落点是"组织 + 应用名"。

    这条**只读**：不写入任何值，只确认落点与 ``QSettings(ORG_NAME, APP_NAME)``
    一致。测试如果拿它去写值，就等于污染真实用户的注册表——而那正是本项目
    测试夹具（``preferences`` fixture）存在的理由。
    """
    prefs = UiPreferences()

    assert prefs.settings.fileName() == QSettings(ORG_NAME, APP_NAME).fileName()
    # Windows 上是注册表路径（以 \\ 或 HKEY 开头），不是某个 ini 文件
    name = prefs.settings.fileName()
    assert name.startswith("\\") or name.upper().startswith("HKEY"), name


def test_ini_backend_file_is_a_real_file(preferences: UiPreferences) -> None:
    """``IniFormat`` 的落点确实是一个真实文件，而不是注册表。

    这条把"测试没有在碰注册表"变成可断言的：只要 fixture 被换回默认构造，
    断言立刻会红，而不是等到某天有人在注册表里看到下载目录才被发现。
    """
    preferences.set_output_dir("D:/maps")

    name = preferences.settings.fileName()
    assert name.endswith(".ini")
    assert Path(name).exists(), f"IniFormat 的落点应当是一个真实文件：{name}"


# --------------------------------------------------------------------------- #
# 区域选择与 Zoom 模式（Slice 19，Spec §14）
# --------------------------------------------------------------------------- #
def test_region_selection_round_trip(preferences: UiPreferences) -> None:
    preferences.set_region_selection(
        RegionSelection(
            region_type="polygon",
            country_id="china",
            province_id="zhejiang",
            city_id="hangzhou",
        )
    )

    stored = preferences.region_selection
    assert stored == RegionSelection(
        region_type="polygon",
        country_id="china",
        province_id="zhejiang",
        city_id="hangzhou",
    )
    assert stored.is_empty is False


def test_empty_region_selection_clears_the_previous_values(
    preferences: UiPreferences,
) -> None:
    """"不限定省"要能把上次那个省**清掉**，而不是留着残值。

    留着的话，下次打开会变成"国还是那个国、省却变成了上次选的"，而用户
    记得自己明明选了"全部"。
    """
    preferences.set_region_selection(
        RegionSelection(region_type="polygon", country_id="china", province_id="zhejiang")
    )
    preferences.set_region_selection(RegionSelection(region_type="polygon", country_id="china"))

    stored = preferences.region_selection
    assert stored.province_id is None
    assert stored.city_id is None


def test_region_selection_defaults_to_empty(preferences: UiPreferences) -> None:
    assert preferences.region_selection == RegionSelection()
    assert preferences.region_selection.is_empty is True


def test_county_id_round_trip(preferences: UiPreferences) -> None:
    """县级 id 要能单独往返（Slice 28）。

    它是"最具体那一级"，决定下载范围。这条看似只是多一个字段，
    但漏掉它的后果是静默放大范围（县 → 市/省），不报错。
    """
    preferences.set_region_selection(
        RegionSelection(
            region_type="polygon",
            country_id="china",
            province_id="jiangsu",
            city_id="nanjing",
            county_id="gulou",
        )
    )

    stored = preferences.region_selection
    assert stored.county_id == "gulou"
    assert stored.city_id == "nanjing"
    assert stored.is_empty is False


def test_clearing_the_county_leaves_nothing_behind(preferences: UiPreferences) -> None:
    """"不限定县区"要能把上次的县**清掉**。

    留着的话，恢复流程会优先用它 → 用户明明选了"整个市"，下载的还是那个县
    （范围反而变小，张数对不上，且同样不报错）。
    """
    preferences.set_region_selection(
        RegionSelection(
            region_type="polygon",
            country_id="china",
            province_id="jiangsu",
            city_id="nanjing",
            county_id="gulou",
        )
    )
    preferences.set_region_selection(
        RegionSelection(
            region_type="polygon",
            country_id="china",
            province_id="jiangsu",
            city_id="nanjing",
        )
    )

    assert preferences.region_selection.county_id is None


def test_county_key_is_managed(preferences: UiPreferences) -> None:
    """县级键必须进 `stored_keys()`：漏了它，"不存 Token"那条断言就看不到它，
    而它本身也确实该被纳入统一管理（清理配置时要一起清）。"""
    preferences.set_region_selection(
        RegionSelection(region_type="polygon", country_id="china", county_id="gulou")
    )

    assert "download/county_id" in preferences.stored_keys()


def test_zoom_mode_round_trip(preferences: UiPreferences) -> None:
    assert preferences.zoom_mode is None
    preferences.set_zoom_mode("recommended")
    assert preferences.zoom_mode == "recommended"
    preferences.set_zoom_mode(None)
    assert preferences.zoom_mode is None, "存 None 表示清除（不做成一个空字符串键）"


def test_custom_zoom_keys_are_the_managed_ones(preferences: UiPreferences) -> None:
    """Slice 19 起缩放区间落在 ``custom_*`` 上（Spec §14 的命名）。"""
    preferences.set_zoom_range(8, 17)

    assert preferences.settings.contains("download/custom_min_zoom")
    assert preferences.settings.contains("download/custom_max_zoom")


def test_legacy_zoom_keys_are_still_read(preferences: UiPreferences) -> None:
    """老配置（Slice ≤18 写的 ``download/min_zoom``）要能被读出来。

    升级不该让一个已经调到顺手位置的用户"回到出厂设置"。
    """
    preferences.settings.setValue("download/min_zoom", 11)
    preferences.settings.setValue("download/max_zoom", 13)

    assert preferences.zoom_range == (11, 13)


def test_new_zoom_keys_win_over_the_legacy_ones(preferences: UiPreferences) -> None:
    """新键一旦存在，老键就不该再有机会覆盖它 —— 哪怕新值更小。"""
    preferences.settings.setValue("download/min_zoom", 11)
    preferences.settings.setValue("download/max_zoom", 13)
    preferences.set_zoom_range(5, 7)

    assert preferences.zoom_range == (5, 7)


def test_clear_also_removes_the_legacy_keys(preferences: UiPreferences) -> None:
    """``clear()`` 要连只读的老键一起清：否则"清空偏好"之后旧值又冒出来了。"""
    preferences.settings.setValue("download/min_zoom", 11)
    preferences.settings.setValue("download/max_zoom", 13)
    preferences.set_zoom_range(5, 7)

    preferences.clear()

    assert preferences.zoom_range is None
    assert not preferences.settings.contains("download/min_zoom")
