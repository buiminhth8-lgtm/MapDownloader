"""当前任务的进度面板（Slice 7，Spec §5）。

只做一件事：**把一份进度快照显示出来**。它不下载、不计数、不判断状态，
甚至不决定按钮能不能点（那是 :mod:`app.ui.download_panel` 的职责，
按钮和「开始下载」放在一起才不会出现"按钮在两处、规则有两套"）。

为什么单独成类
--------------

Slice 4 起这块是 ``MainWindow._build_status_group()`` 里现搭的。抽出来有实际
收益，不只是整洁：

- ``MainWindow`` 已经有 1600 行，"状态区有哪几行"这种细节不该再占它的篇幅；
- 行数从 9 涨到 11（Spec §5 要求「任务名称」「剩余」），布局必须重新安排，
  单独一个类里改比在大文件中间改安全得多；
- 它可以被单独构造和断言，不必拉起整个主窗口。

布局：**两列网格**而不是一列 ``QFormLayout``
-------------------------------------------

左侧配置列只有 460px 宽，而它下面还挂着「预估结果」「下载结果」
（下载结束时两者都可见）。一列排 11 行会多出约 50px 高——刚好是让
「总进度」掉出可视区的量级。两列网格把 11 个字段压成 6 行，
**比 Slice 6 时的 9 行还矮**，于是"进度条永远看得见"这条既有约束
不但没被破坏，余量还变大了。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QLabel,
    QProgressBar,
    QWidget,
)

from app.config.settings import UI_TEXT_SCANNING_EXISTING
from app.geo import format_size
from app.ui.widgets import apply_secondary_text

__all__ = ["TaskProgressPanel"]


#: 字段键 → (行标题, 初始值)。
#:
#: 顺序即网格的阅读顺序：**两个一组**，因此元组的相邻两项会并排显示。
#: 分组是刻意的——"任务名称 / 状态"是"这是谁"，"当前 Zoom / 总瓦片"是规模，
#: "已完成 / 剩余"是进度，剩下的都是明细。
_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("task_name", "任务名称", "—"),
    ("task_status", "状态", "空闲"),
    ("current_zoom", "当前 Zoom", "—"),
    ("tile_total", "总瓦片", "0"),
    ("tile_done", "已完成", "0"),
    ("tile_remaining", "剩余", "0"),
    ("tile_skipped", "已有（跳过）", "0"),
    ("tile_succeeded", "本次成功", "0"),
    ("tile_failed", "失败", "0"),
    ("speed", "下载速度", "0 瓦片/秒"),
    ("downloaded_bytes", "已下载", "0 B"),
)

#: 每行放几个字段
_COLUMNS = 2


class TaskProgressPanel(QGroupBox):
    """当前任务的实时状态：11 个字段 + 总进度条。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("下载状态", parent)

        grid = QGridLayout(self)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(4)

        self._values: dict[str, QLabel] = {}
        for index, (key, title, default) in enumerate(_FIELDS):
            row, pair = divmod(index, _COLUMNS)
            column = pair * 2
            caption = QLabel(title, self)
            # 深色主题下 palette(mid) ≈ 背景色，字会整片消失；
            # 见 app/ui/widgets.py 里 secondary_text_color 的说明。
            apply_secondary_text(caption)
            value = QLabel(default, self)
            value.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            self._values[key] = value
            grid.addWidget(caption, row, column)
            grid.addWidget(value, row, column + 1)
        # 值列可以拉伸，标题列不参与拉伸（否则两列标题宽度不一致，看着很乱）
        for column in range(0, _COLUMNS * 2, 2):
            grid.setColumnStretch(column + 1, 1)

        self._progress_bar = QProgressBar(self)
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setFormat("总进度 %p%")
        # 进度条独占最后一行、横跨两列：它是最重要的一个控件，
        # 挤在半栏里既看不清百分比也点不准。
        bar_row = (len(_FIELDS) + _COLUMNS - 1) // _COLUMNS
        grid.addWidget(self._progress_bar, bar_row, 0, 1, _COLUMNS * 2)

    # ------------------------------------------------------------------ #
    # 只读状态
    # ------------------------------------------------------------------ #
    @property
    def values(self) -> dict[str, QLabel]:
        """字段键 → 值标签。返回的是内部字典本身（调用方只读）。"""
        return self._values

    @property
    def progress_bar(self) -> QProgressBar:
        """总进度条。"""
        return self._progress_bar

    @property
    def field_keys(self) -> tuple[str, ...]:
        """全部字段键（顺序即展示顺序）。"""
        return tuple(key for key, _title, _default in _FIELDS)

    def value_text(self, key: str) -> str:
        """取某个字段的当前文本（键不存在时返回空串，不抛异常）。"""
        label = self._values.get(key)
        return label.text() if label is not None else ""

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def set_value(self, key: str, text: str) -> None:
        """写一个字段（键不存在时静默忽略——见下）。"""
        label = self._values.get(key)
        if label is not None:
            label.setText(text)

    def set_status(self, text: str) -> None:
        """:meth:`set_value` 的语义化版本（状态是写得最频繁的一个字段）。"""
        self.set_value("task_status", text)

    def set_task_name(self, name: str | None) -> None:
        """显示当前任务名（``None`` 显示占位符）。"""
        self.set_value("task_name", str(name) if name else "—")

    def reset(
        self,
        total: int = 0,
        *,
        name: str | None = None,
        status: str = "空闲",
        skipped: int | None = None,
    ) -> None:
        """把面板清成"从零开始"。

        开工前必须清一次：上一轮的失败数、速度、体积留在界面上，
        会让用户以为新任务一上来就失败了几十张。

        Args:
            total: 本次目标瓦片数。
            name: 任务名；``None`` 表示未知（全新下载时任务记录已建好，通常是有的）。
            status: 起始状态文案。
            skipped: 已知的"已有瓦片"数；``None`` 表示尚未扫描到。
        """
        self.set_task_name(name)
        self.set_status(status)
        self.set_value("current_zoom", "—")
        self.set_value("tile_total", f"{max(0, int(total)):,}")
        self.set_value("tile_done", "0")
        self.set_value("tile_remaining", f"{max(0, int(total)):,}")
        self.set_value("tile_succeeded", "0")
        self.set_value("tile_failed", "0")
        self.set_value("tile_skipped", "0" if skipped is None else f"{skipped:,}")
        self.set_value("speed", "0 瓦片/秒")
        self.set_value("downloaded_bytes", "0 B")
        self._progress_bar.setValue(0)

    def apply_progress(self, progress) -> None:
        """把一份下载引擎的进度快照渲染出来。

        参数用**鸭子类型**：本模块只读那几个属性，不需要 import 引擎类型
        （UI 层对引擎的依赖越少越好，测试也更容易造一个假快照）。
        """
        total = int(getattr(progress, "total", 0) or 0)
        completed = int(getattr(progress, "completed", 0) or 0)
        self.set_value("tile_total", f"{total:,}")
        self.set_value("tile_done", f"{completed:,}")
        self.set_value("tile_remaining", f"{max(0, total - completed):,}")
        self.set_value("tile_succeeded", f"{int(getattr(progress, 'succeeded', 0) or 0):,}")
        self.set_value("tile_failed", f"{int(getattr(progress, 'failed', 0) or 0):,}")
        self.set_value("tile_skipped", f"{int(getattr(progress, 'skipped', 0) or 0):,}")
        self.set_value(
            "speed", f"{float(getattr(progress, 'tiles_per_second', 0.0) or 0.0):.1f} 瓦片/秒"
        )
        self.set_value(
            "downloaded_bytes",
            format_size(int(getattr(progress, "bytes_downloaded", 0) or 0)),
        )
        current_zoom = getattr(progress, "current_zoom", None)
        if current_zoom is not None:
            self.set_value("current_zoom", str(current_zoom))
        self._progress_bar.setValue(int(float(getattr(progress, "percent", 0.0) or 0.0)))

    def set_percent(self, percent: float) -> None:
        """直接设置总进度（收尾时用 100，不依赖最后一帧进度回调）。"""
        self._progress_bar.setValue(int(percent))

    # ------------------------------------------------------------------ #
    # 扫描已有瓦片（Spec §11）
    # ------------------------------------------------------------------ #
    def show_scanning(self) -> None:
        """进入"正在扫描已有瓦片…"。

        扫描发生在**用户点了开始/继续之后、下载真正开始之前**。这段时间里
        后台在查 SQLite，界面必须动起来——否则用户看到的是一个点了没反应的
        按钮，最常见的反应是再点一次。
        """
        self.set_status(UI_TEXT_SCANNING_EXISTING)
        self.set_value("speed", "—")

    def show_scan_result(self, existing: int, remaining: int) -> None:
        """扫描结束：把"已有多少 / 还要下多少"摆出来（Spec §11）。

        只写这两个字段，**不碰状态行**：扫描之后紧接着就是 ``reset()`` +
        进入下载态，状态文案由那一侧统一给，两处都写必然出现一瞬间的错值。
        那两行日志（"已有：N / 待下载：M"）另行打印，因为日志要留痕，
        而面板只是当下这一眼。
        """
        self.set_value("tile_skipped", f"{max(0, int(existing)):,}")
        self.set_value("tile_remaining", f"{max(0, int(remaining)):,}")
