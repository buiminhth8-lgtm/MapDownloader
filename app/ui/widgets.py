"""通用小控件（Slice 7）。

从 :mod:`app.ui.download_panel` 里搬出来的，因为 Slice 7 起"长文本不裁切"这条
需求出现在三个面板上（配置区的说明、任务列表的选中信息、日志）。

**为什么不用 ``QLabel.setWordWrap(True)``**：它按单词边界断行，遇到 Windows
绝对路径、SSL 错误文本这类超长无空格 token 时找不到断点，于是**不折行、直接
横向裁掉**（实测在 460px 宽的配置列里，"文件：C:\\...\\x.mbtiles" 只显示到一半，
且强制重排也修不好）。:class:`WrappingLabel` 把"折行"这一步拿回自己手里，
文本里全是显式换行符，布局结果完全确定。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFontMetrics, QPalette
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget

__all__ = [
    "WrappingLabel",
    "apply_secondary_text",
    "relative_luminance",
    "secondary_text_color",
    "status_text_color",
    "wrap_anywhere",
]

#: 标签尚未完成首次布局时使用的折行宽度（配置列的典型内宽）。
#: 首次 ``set_text`` 时控件宽度还是 0，只能用这个估值；随后的 ``resizeEvent``
#: 会按真实宽度重排，因此它只影响一帧，不会留下错误换行。
FALLBACK_WIDTH = 420

#: 次要文字朝背景色插值的比例（0 = 正文色，1 = 完全与背景同色）。
#: 0.45 在深浅两个主题下都落在"明显比正文淡、但对比度仍在 4.5:1 以上"的位置。
_SECONDARY_BLEND = 0.45


def secondary_text_color(palette: QPalette) -> QColor:
    """由调色板算出"次要文字"颜色：把正文色朝背景色插值。

    **为什么不用 ``color: palette(mid)``**：``Mid`` 这个角色的含义随主题而变，
    而且在深色主题下它恰好≈窗口背景。实测本机深色主题：``Window`` = ``#1e1e1e``、
    ``Mid`` = ``#282828``——两者对比度约 1.06:1，也就是"控件看得见、字看不见"。
    界面上所有"折行说明 / 空列表占位 / 字段标题"用的都是它，于是一整片文字
    在深色主题下集体消失，而浅色主题下完全正常（因此只看浅色截图发现不了）。

    Args:
        palette: 取值来源（通常直接传控件的 ``palette()``）。

    Returns:
        正文色与背景色之间的插值颜色。
    """
    text = palette.color(QPalette.ColorRole.WindowText)
    background = palette.color(QPalette.ColorRole.Window)
    weight = _SECONDARY_BLEND
    return QColor(
        round(text.red() * (1 - weight) + background.red() * weight),
        round(text.green() * (1 - weight) + background.green() * weight),
        round(text.blue() * (1 - weight) + background.blue() * weight),
    )


def apply_secondary_text(widget: QWidget) -> None:
    """把某个控件的正文色换成"次要文字"色（浅色 / 深色主题都读得清）。

    用 ``setPalette`` 而不是 ``setStyleSheet``：样式表里的颜色只在设置时解析
    一次，而调色板是本控件自己的，取的是装配时已经生效的那一份——不必去猜
    当前是不是深色主题。
    """
    palette = widget.palette()
    palette.setColor(QPalette.ColorRole.WindowText, secondary_text_color(palette))
    widget.setPalette(palette)


#: 语义色（PASS 绿 / WARNING 琥珀 / FAIL 红）朝黑压的比例（浅色主题用）。
_TEXT_TINT_ON_LIGHT = 0.35
#: 语义色朝白提的比例（深色主题用）。
_TEXT_TINT_ON_DARK = 0.45


def relative_luminance(color: QColor) -> float:
    """WCAG 相对亮度（0 = 黑，1 = 白）。

    单独公开出来是为了让"对比度够不够"这件事可被**断言**：光看颜色值
    说明不了任何问题，只有算出来才知道（Slice 7 的次要文字隐形就是这么发现的）。
    """
    channels = []
    for value in (color.red(), color.green(), color.blue()):
        ratio = value / 255
        channels.append(ratio / 12.92 if ratio <= 0.03928 else ((ratio + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def status_text_color(palette: QPalette, base: QColor | str) -> QColor:
    """把语义色（PASS 绿 / WARNING 琥珀 / FAIL 红）调成**当前主题下读得清**的颜色。

    为什么不直接用那三个固定的十六进制值：任何颜色都不可能同时对纯白与
    深色窗口背景都达到 4.5:1 —— 两个背景的亮度差了近 80 倍，这是数学上的
    不可能，不是调参问题。因此按背景明暗把语义色**压暗或提亮**：
    浅色主题往黑压、深色主题往白提，保住色相、拉开亮度。

    Args:
        palette: 取值来源（通常传控件的 ``palette()``）。
        base: 语义色（``QColor`` 或 ``"#rrggbb"``）。

    Returns:
        适合当前背景的前景色。
    """
    color = QColor(base)
    background = palette.color(QPalette.ColorRole.Window)
    if relative_luminance(background) > 0.5:
        return _mix(color, QColor(0, 0, 0), _TEXT_TINT_ON_LIGHT)
    return _mix(color, QColor(255, 255, 255), _TEXT_TINT_ON_DARK)


def _mix(color: QColor, other: QColor, weight: float) -> QColor:
    """把 ``color`` 朝 ``other`` 混合 ``weight`` 比例（0 = 原样，1 = 全变）。"""
    return QColor(
        round(color.red() * (1 - weight) + other.red() * weight),
        round(color.green() * (1 - weight) + other.green() * weight),
        round(color.blue() * (1 - weight) + other.blue() * weight),
    )


def wrap_anywhere(text: str, width: int, metrics: QFontMetrics) -> str:
    """按**字符**宽度折行，返回插入显式换行符后的文本。

    Args:
        text: 原始文本（可含换行）。
        width: 可用宽度（像素）。``<= 0`` 时原样返回。
        metrics: 用于测量宽度的字体度量。

    Returns:
        折行后的文本。
    """
    if width <= 0:
        return text

    lines: list[str] = []
    for raw_line in text.splitlines() or [""]:
        current = ""
        for char in raw_line:
            if current and metrics.horizontalAdvance(current + char) > width:
                lines.append(current)
                current = char
            else:
                current += char
        lines.append(current)
    return "\n".join(lines)


class WrappingLabel(QLabel):
    """多行说明标签：**自己按字符折行**，不依赖 ``QLabel.setWordWrap``。

    用法上仍兼容 ``QLabel``：``set_text()`` 传原始文本，``text()`` 取回原始
    文本（不是折行后的），因此调用方与断言不必关心折行细节。
    """

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # 水平方向 Ignored：本标签**不得**反过来决定所在列的宽度
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._source_text = ""
        self._wrapped_width = -1
        self.set_text(text)

    def set_text(self, text: str) -> None:
        """设置原始文本（显示时会按当前宽度折行）。"""
        self._source_text = text
        self._rewrap(force=True)

    def setText(self, text: str) -> None:  # noqa: N802 - 与 QLabel 同名，便于替换
        """``QLabel.setText`` 的等价物：转发到 :meth:`set_text`。

        **这条转发不是可有可无的糖。** 本类把原始文本存在 ``_source_text`` 里、
        ``text()`` 读的也是它；如果调用方走的是 ``QLabel.setText``，那么
        C++ 那边的显示文本被改了，``_source_text`` 却没动 —— 于是
        "眼睛看到的是新值、``text()`` 读出来的是旧值"（Slice 9 的打包对话框
        就是这么写的：界面显示正常，断言却全读到占位符 ``—``）。
        重写之后本类才是文档承诺的那种 ``QLabel`` 替代品：
        两种写法都指向同一份状态，不存在"哪个是真值"的问题。
        """
        self.set_text(text)

    def text(self) -> str:  # noqa: A003 - 与 QLabel 同名，便于替换
        """返回**原始**文本（不含自动插入的换行符）。"""
        return self._source_text

    def _rewrap(self, *, force: bool = False) -> None:
        width = self.width()
        if width <= 0:
            width = FALLBACK_WIDTH
        if not force and width == self._wrapped_width:
            return
        self._wrapped_width = width

        wrapped = wrap_anywhere(self._source_text, width, self.fontMetrics())
        if wrapped != super().text():
            super().setText(wrapped)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名约定
        super().resizeEvent(event)
        self._rewrap()
