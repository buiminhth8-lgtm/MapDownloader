r"""下载任务列表面板（Slice 5）。

一个**刻意做得很简单**的任务区（Spec §14 明确说"不需要做复杂任务中心"）：

::

    下载任务
    ────────────────────────────────────────────────
    名称      状态     进度              输出路径     更新时间
    北京地图  已暂停   64%（32,418/50,000） D:\...\beijing.mbtiles  10:32
    ...
    [继续] [删除记录]

面板只做三件事：展示任务、把"继续/删除/双击"变成信号抛出去。
**它不碰 tasks.json、不拼路径、不知道 .mbtiles 在哪**——那些是
``app.tasks`` 与 ``MainWindow`` 的职责。

为什么用 ``QTableWidget`` 而不是自绘列表：五行一行的表格天然就对，
而且用户能直接选中一行复制文本（路径、错误原因都能复制走，
比"点一个自绘项然后什么也拿不到"有用得多）。
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import (
    TASK_PANEL_MAX_ROWS,
    UI_TEXT_TASK_DELETE,
    UI_TEXT_TASK_DELETE_HINT,
    UI_TEXT_TASK_PANEL_EMPTY,
    UI_TEXT_TASK_RESUME,
)
from app.models.download_task import DownloadTask, DownloadTaskStatus

#: 表格列定义：``(标题, 宽度)``。宽度为 ``None`` 的列自动拉伸。
#:
#: 「进度」列按**实测像素宽**定：`progress_text()` 的常见形态
#: （``50%（9,820 / 19,640）``）实测 228px，130px 会把分母截成 ``…``，
#: 而"还剩多少"恰恰是用户最想看的信息。这里给到 232px 让常见形态完整显示；
#: 带「已有 N」的最长形态仍有截断，但那属于少数情况，tooltip 里是全文。
#:
#: 固定列合计约 504px：右列在默认窗口（1200px 宽）下约 740px，
#: 「输出路径」还能剩下 ~236px；窗口缩到最小时它会变成横向滚动，
#: 但那时路径本来就看不全，靠 tooltip 兜底更现实。
_COLUMNS: tuple[tuple[str, int | None], ...] = (
    ("名称", 116),
    ("状态", 60),
    ("进度", 232),
    ("输出路径", None),
    ("更新时间", 96),
)

#: 任务 id 挂在第 0 列的自定义角色上（比"用行号反查列表"更稳：
#: 排序 / 过滤之后行号就不再等于下标了）
_TASK_ID_ROLE = Qt.ItemDataRole.UserRole

#: 状态 → 文字颜色。用颜色而不是图标：无头环境与高 DPI 下都更可靠。
_STATUS_COLORS: dict[DownloadTaskStatus, str] = {
    DownloadTaskStatus.COMPLETED: "#1a7f37",
    DownloadTaskStatus.FAILED: "#b42318",
    DownloadTaskStatus.CANCELLED: "#8c6d1f",
    DownloadTaskStatus.DOWNLOADING: "#0b5cad",
    DownloadTaskStatus.PAUSED: "#5a5a5a",
}


class TaskPanel(QGroupBox):
    """任务列表 + [继续] [删除记录]。"""

    #: 用户点了「继续」（参数：task_id）
    resume_requested = Signal(str)
    #: 用户点了「删除记录」（参数：task_id）
    delete_requested = Signal(str)
    #: 用户双击了一行（参数：task_id）——用于把任务参数回填到配置面板
    task_activated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("下载任务", parent)

        self._table = QTableWidget(0, len(_COLUMNS), self)
        self._table.setHorizontalHeaderLabels([title for title, _ in _COLUMNS])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        # 表格自己不决定面板宽度（否则长路径会把右侧列撑开）
        self._table.setSizeAdjustPolicy(
            QTableWidget.SizeAdjustPolicy.AdjustIgnored
        )
        self._table.setMinimumHeight(104)

        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        for index, (_title, width) in enumerate(_COLUMNS):
            if width is None:
                header.setSectionResizeMode(
                    index, QHeaderView.ResizeMode.Stretch
                )
            else:
                header.setSectionResizeMode(
                    index, QHeaderView.ResizeMode.Interactive
                )
                self._table.setColumnWidth(index, width)

        self._empty_label = QLabel(UI_TEXT_TASK_PANEL_EMPTY, self)
        self._empty_label.setStyleSheet("color: palette(mid);")

        self._resume_button = QPushButton(UI_TEXT_TASK_RESUME, self)
        self._delete_button = QPushButton(UI_TEXT_TASK_DELETE, self)
        self._delete_button.setToolTip(UI_TEXT_TASK_DELETE_HINT)

        button_row = QHBoxLayout()
        button_row.addWidget(self._resume_button)
        button_row.addWidget(self._delete_button)
        button_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self._table)
        layout.addWidget(self._empty_label)
        layout.addLayout(button_row)

        self._resume_button.clicked.connect(self._on_resume_clicked)
        self._delete_button.clicked.connect(self._on_delete_clicked)
        self._table.itemSelectionChanged.connect(self._refresh_buttons)
        self._table.itemDoubleClicked.connect(self._on_item_double_clicked)

        self.set_tasks([])

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def set_tasks(self, tasks: Sequence[DownloadTask]) -> None:
        """整体刷新列表（按传入顺序展示，通常是 ``updated_at`` 倒序）。"""
        rows = list(tasks)[:TASK_PANEL_MAX_ROWS]
        # 刷新期间不响应选择变化，避免"清空 → 逐行填充"过程中按钮状态抖动
        self._table.blockSignals(True)
        self._table.setRowCount(0)
        for task in rows:
            self._append_row(task)
        self._table.blockSignals(False)

        has_rows = bool(rows)
        self._table.setVisible(has_rows)
        self._empty_label.setVisible(not has_rows)
        self._refresh_buttons()

    def update_task(self, task: DownloadTask) -> bool:
        """只更新已有的一行（找不到就返回 ``False``，不插入新行）。

        用途：下载进行中把进度刷到列表上。刻意**不做**整体 ``set_tasks``——
        那会重读一次 tasks.json，而在进度回调里每秒读盘 6 次是没有道理的。
        """
        row = self._row_of(task.id)
        if row is None:
            return False
        self._fill_row(row, task)
        return True

    def selected_task_id(self) -> str | None:
        """当前选中的任务 id；没选中返回 ``None``。"""
        items = self._table.selectedItems()
        if not items:
            return None
        return self._table.item(items[0].row(), 0).data(_TASK_ID_ROLE)

    def select_task(self, task_id: str) -> bool:
        """选中指定任务；不在列表里返回 ``False``。"""
        row = self._row_of(task_id)
        if row is None:
            return False
        self._table.selectRow(row)
        return True

    @property
    def task_count(self) -> int:
        """当前展示的任务条数。"""
        return self._table.rowCount()

    # ------------------------------------------------------------------ #
    # 只读控件（测试与诊断）
    # ------------------------------------------------------------------ #
    @property
    def table(self) -> QTableWidget:
        """底层表格。"""
        return self._table

    @property
    def resume_button(self) -> QPushButton:
        """「继续」按钮。"""
        return self._resume_button

    @property
    def delete_button(self) -> QPushButton:
        """「删除记录」按钮。"""
        return self._delete_button

    @property
    def empty_label(self) -> QLabel:
        """空列表占位文案。"""
        return self._empty_label

    def row_text(self, row: int) -> list[str]:
        """某一行的全部单元格文本（测试断言用）。"""
        return [
            self._table.item(row, column).text()
            for column in range(self._table.columnCount())
        ]

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _append_row(self, task: DownloadTask) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._fill_row(row, task)

    def _fill_row(self, row: int, task: DownloadTask) -> None:
        """把任务数据填进第 ``row`` 行。"""
        values = (
            task.name,
            task.status.display_name,
            task.progress_text(),
            str(task.output_path),
            task.updated_at.strftime("%m-%d %H:%M"),
        )
        for column, text in enumerate(values):
            item = self._table.item(row, column)
            if item is None:
                item = QTableWidgetItem()
                self._table.setItem(row, column, item)
            item.setText(text)
            if column == 0:
                item.setData(_TASK_ID_ROLE, task.id)
            # 长路径 / 长错误不换行，靠 tooltip 兜底
            item.setToolTip(text)

        status_item = self._table.item(row, 1)
        color = _STATUS_COLORS.get(task.status)
        if color:
            status_item.setForeground(QColor(color))
        if task.error:
            status_item.setToolTip(task.error)

    def _row_of(self, task_id: str) -> int | None:
        """按 task_id 找行号（找不到返回 ``None``）。"""
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is not None and item.data(_TASK_ID_ROLE) == task_id:
                return row
        return None

    def _refresh_buttons(self) -> None:
        """按钮可用性跟着选中状态走。"""
        has_selection = self.selected_task_id() is not None
        self._resume_button.setEnabled(has_selection)
        self._delete_button.setEnabled(has_selection)

    # ------------------------------------------------------------------ #
    # 槽
    # ------------------------------------------------------------------ #
    @Slot()
    def _on_resume_clicked(self) -> None:
        task_id = self.selected_task_id()
        if task_id:
            self.resume_requested.emit(task_id)

    @Slot()
    def _on_delete_clicked(self) -> None:
        task_id = self.selected_task_id()
        if task_id:
            self.delete_requested.emit(task_id)

    @Slot(QTableWidgetItem)
    def _on_item_double_clicked(self, item: QTableWidgetItem) -> None:
        task_id = self._table.item(item.row(), 0).data(_TASK_ID_ROLE)
        if task_id:
            self.task_activated.emit(task_id)
