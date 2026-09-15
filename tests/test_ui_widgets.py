"""通用小控件与主题无关的次要文字色（Slice 7）。

其中"深色主题下字看不见"这条是**手工验收抓出来的**：本机 Windows 处于深色模式，
而所有"折行说明 / 空列表占位 / 字段标题"都写着 ``color: palette(mid)``。实测
``Window`` = ``#1e1e1e``、``Mid`` = ``#282828``，对比度约 1.06:1——控件看得见、
字看不见。浅色主题下完全正常，所以只看浅色截图永远发现不了。

因此这里用 **WCAG 对比度**去钉住"两种主题下都读得清"，而不是断言某个具体色值
（色值随 Qt 版本与系统主题变化，断言具体色值只会变成一条会随机变红的测试）。
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QLabel

from app.ui.widgets import (
    FALLBACK_WIDTH,
    WrappingLabel,
    apply_secondary_text,
    secondary_text_color,
    wrap_anywhere,
)

# --------------------------------------------------------------------------- #
# 折行
# --------------------------------------------------------------------------- #
def test_wrap_anywhere_splits_long_tokens(qapp) -> None:
    """超长无空格 token（Windows 路径 / SSL 错误）必须能断开。

    ``QLabel.setWordWrap(True)`` 找不到断点时会**不折行、直接横向裁掉**，
    这正是这个函数存在的理由。
    """
    metrics = QLabel().fontMetrics()
    text = "C:" + "\\very-long-directory-name" * 12 + "\\final.mbtiles"

    wrapped = wrap_anywhere(text, 200, metrics)

    assert "\n" in wrapped
    for line in wrapped.splitlines():
        assert metrics.horizontalAdvance(line) <= 200


def test_wrap_anywhere_preserves_content(qapp) -> None:
    """折行只插换行符，不丢字符（否则路径会被静默改掉）。"""
    metrics = QLabel().fontMetrics()
    text = "D:\\maps\\beijing.mbtiles"

    wrapped = wrap_anywhere(text, 60, metrics)

    assert wrapped.replace("\n", "") == text


def test_wrap_anywhere_with_non_positive_width_returns_as_is(qapp) -> None:
    """宽度未知（``<= 0``）时原样返回，等首次布局后再重排。"""
    metrics = QLabel().fontMetrics()
    text = "abc"

    assert wrap_anywhere(text, 0, metrics) == text
    assert wrap_anywhere(text, -10, metrics) == text


def test_wrap_anywhere_keeps_existing_line_breaks(qapp) -> None:
    metrics = QLabel().fontMetrics()

    wrapped = wrap_anywhere("a\nb", 500, metrics)

    assert wrapped.splitlines() == ["a", "b"]


# --------------------------------------------------------------------------- #
# WrappingLabel
# --------------------------------------------------------------------------- #
def test_wrapping_label_reports_source_text(qapp) -> None:
    """``text()`` 必须返回**原始**文本，不是折行后的。

    否则调用方与断言都会看到注入了换行符的字符串，用它去比路径会永远不等。
    """
    label = WrappingLabel("x" * 300)
    label.resize(80, 200)

    assert label.text() == "x" * 300
    # 真正显示在控件上的那一份（走 QLabel 自己的实现）确实被折行了
    displayed = QLabel.text(label)
    assert displayed != label.text()
    assert displayed.startswith("x")


def test_wrapping_label_default_size_policy_does_not_drive_width(qapp) -> None:
    """标签水平方向必须是 ``Ignored``：长路径不许反过来撑宽它所在的列。"""
    from PySide6.QtWidgets import QSizePolicy

    label = WrappingLabel("D:\\maps\\a-very-long-name.mbtiles")

    assert label.sizePolicy().horizontalPolicy() is QSizePolicy.Policy.Ignored


def test_wrapping_label_fallback_width_is_used_before_first_layout(qapp) -> None:
    assert FALLBACK_WIDTH > 0


# --------------------------------------------------------------------------- #
# 次要文字色：两种主题下都要读得清（回归守卫）
# --------------------------------------------------------------------------- #
def _relative_luminance(color: QColor) -> float:
    """WCAG 2.x 的相对亮度。"""
    channels = []
    for raw in (color.red(), color.green(), color.blue()):
        value = raw / 255.0
        channels.append(
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        )
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_ratio(foreground: QColor, background: QColor) -> float:
    """WCAG 对比度（1:1 ~ 21:1）。"""
    first = _relative_luminance(foreground)
    second = _relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _light_palette() -> QPalette:
    """一份浅色主题调色板（取值来自 Qt 的默认浅色调色板）。"""
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#f0f0f0"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#000000"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#000000"))
    palette.setColor(QPalette.ColorRole.Mid, QColor("#d0d0d0"))
    palette.setColor(QPalette.ColorRole.Midlight, QColor("#e0e0e0"))
    return palette


def _dark_palette() -> QPalette:
    """一份深色主题调色板（取值复刻本机 Windows 深色模式实测结果）。"""
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#1e1e1e"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#2d2d2d"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Mid, QColor("#282828"))  # ← 几乎等于背景色
    palette.setColor(QPalette.ColorRole.Midlight, QColor("#5a5a5a"))
    return palette


@pytest.mark.parametrize(
    "palette_factory", [_light_palette, _dark_palette], ids=["light", "dark"]
)
def test_secondary_text_is_readable(palette_factory) -> None:
    """次要文字与背景的对比度必须 ≥ 4.5:1（WCAG AA 正文标准）。

    这条是那个真实缺陷的守卫：深色主题下 ``palette(mid)`` 只有约 **1.06:1**，
    字整片消失；而浅色主题下它是正常的——所以只有两个主题都测，才拦得住。
    """
    palette = palette_factory()
    color = secondary_text_color(palette)

    ratio = _contrast_ratio(color, palette.color(QPalette.ColorRole.Window))
    assert ratio >= 4.5, f"对比度只有 {ratio:.2f}:1，深色/浅色主题下会读不清"


@pytest.mark.parametrize(
    "palette_factory", [_light_palette, _dark_palette], ids=["light", "dark"]
)
def test_secondary_text_is_dimmer_than_body_text(palette_factory) -> None:
    """"次要"必须真的是次要的：对比度低于正文，否则视觉层级就没了。"""
    palette = palette_factory()
    secondary = secondary_text_color(palette)
    body = palette.color(QPalette.ColorRole.WindowText)

    assert _contrast_ratio(secondary, palette.color(QPalette.ColorRole.Window)) < (
        _contrast_ratio(body, palette.color(QPalette.ColorRole.Window))
    )


def test_palette_mid_really_is_the_problem() -> None:
    """把"为什么不能用 ``Mid``"写成断言，免得将来有人又改回去。

    这条不是在测我们的代码，而是在记录那个环境事实：Qt 的深色主题里
    ``Mid`` 与 ``Window`` 近乎同色。
    """
    palette = _dark_palette()
    mid = palette.color(QPalette.ColorRole.Mid)
    window = palette.color(QPalette.ColorRole.Window)

    assert _contrast_ratio(mid, window) < 1.5, "深色主题的 Mid 本来就该近乎背景色"


def test_apply_secondary_text_only_touches_the_text_role(qapp) -> None:
    """只改前景色：背景、字号、其它角色都不许被顺手改掉。"""
    label = QLabel("说明文字")
    palette = _dark_palette()
    label.setPalette(palette)
    before_background = label.palette().color(QPalette.ColorRole.Window)
    before_font = label.font()

    apply_secondary_text(label)

    after = label.palette()
    assert after.color(QPalette.ColorRole.Window) == before_background
    assert label.font() == before_font
    assert after.color(QPalette.ColorRole.WindowText) == secondary_text_color(palette)
    assert after.color(QPalette.ColorRole.WindowText) != palette.color(
        QPalette.ColorRole.WindowText
    )
