"""「关于」对话框的文案守卫。

这里曾经把进度写死成"当前进度 Slice 10"以及"Android 端的地图包安装（Slice 11）
尚未实现"，而代码早已走到更后面 —— 用户在"关于"里读到的是**错的**。

根因不是"忘了改"，而是**用序号描述进度**：Slice 是个只增不减的编号，每加一版
都得回来同步一次，迟早会漏。现在正文只描述"现在能做什么"，本文件负责让它
保持这个形状：出现 Slice 编号、或者出现"尚未实现"这类否定式进度句，就是回归。
"""

from __future__ import annotations

from app.config.settings import APP_NAME, APP_VERSION, PACKAGE_FORMAT_VERSION
from app.ui.main_window import _about_html


def test_about_text_has_no_slice_number() -> None:
    """正文不许出现 Slice 编号 —— 它是必然会滞后的那种信息。"""
    text = _about_html()
    assert "Slice" not in text
    assert "slice" not in text


def test_about_text_has_no_negative_progress_claim() -> None:
    """正文不许出现"尚未实现"这类断言：它一旦过期就在误导用户。"""
    text = _about_html()
    for phrase in ("尚未实现", "暂未实现", "待实现", "敬请期待"):
        assert phrase not in text, f"「关于」里不该出现 {phrase!r}"


def test_about_text_carries_the_single_source_versions() -> None:
    """版本号只在 settings 里定义一次，正文照抄，不许自己再写一份。"""
    text = _about_html()
    assert APP_NAME in text
    assert APP_VERSION in text
    assert f"formatVersion {PACKAGE_FORMAT_VERSION}" in text
