r"""下载任务列表面板（Slice 5；Slice 7 扩到九列 + 选中信息区）。

::

    下载任务
    ──────────────────────────────────────────────────────────────────────
    名称      地图类型  Zoom  状态    进度   成功    失败  更新时间   输出文件
    北京地图  矢量+注记  8-17  已暂停  63%    5,331   12    09-15 10:32 D:\...\beijing.mbtiles
    ...
    任务信息：北京地图
    状态：已暂停（Zoom 8 ~ 17，矢量地图 + 中文注记）
    MBTiles：D:\maps\beijing.mbtiles
    文件已存在（1.42 GB），继续时已有瓦片会自动跳过
    [继续] [删除记录]

面板只做三件事：展示任务、把"继续/删除/双击/选中"变成信号抛出去。
**它不碰 tasks.json、不拼路径、不去数瓦片**——那些是 ``app.tasks`` 与
``MainWindow`` 的职责。信息区里的"文件已存在"只做一次 ``is_file()``：
数瓦片要查 SQLite，那是禁止在 Qt 主线程做的事（Spec §7）。

为什么用 ``QTableWidget`` 而不是自绘列表：几行一行的表格天然就对，
而且用户能直接选中一行复制文本（路径、错误原因都能复制走，
比"点一个自绘项然后什么也拿不到"有用得多）。

列宽是**实测**出来的
--------------------

九列挤在 420~740px 宽的右列里，写死宽度很容易变成"看起来能放下、
实际全是省略号"。因此这里只给固定列配宽，且刻意收敛：
「进度」只放百分比（``63%``）而不是 ``63%（31,240 / 52,381）``——
后者要 228px，而"成功/失败"已经有独立两列了。详细数字在信息区和
tooltip 里，窄列重复一遍只会把「输出文件」挤没。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
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
    UI_TEXT_TASK_INFO,
    UI_TEXT_TASK_INFO_EMPTY,
    UI_TEXT_TASK_INFO_FILE_EXISTS,
    UI_TEXT_TASK_INFO_FILE_MISSING,
    UI_TEXT_TASK_PANEL_EMPTY,
    UI_TEXT_TASK_RESUME,
)
from app.geo import format_size
from app.models.download_task import DownloadTask, DownloadTaskStatus
from app.models.map_layer import describe_map_type
from app.ui.widgets import WrappingLabel, apply_secondary_text

#: 表格列定义：``(列名, 宽度)``。宽度为 ``None`` 的列自动拉伸。
#:
#: 固定列合计约 560px：右列在默认窗口（1200px 宽）下约 740px，
#: 「输出文件」还能剩下 ~180px；窗口缩到最小时它会变成横向滚动，
#: 但那时路径本来就看不全，靠 tooltip 兜底更现实。
_COLUMNS: tuple[tuple[str, int | None], ...] = (
    ("名称", 104),
    ("地图类型", 96),
    ("Zoom", 52),
    ("状态", 56),
    ("进度", 56),
    ("成功", 64),
    ("失败", 56),
    ("更新时间", 88),
    ("输出文件", None),
)

#: 列名 → 下标。给测试与调用方用：**按名字取列**，列顺序再变也不会让断言
#: 悄悄测到另一列（这类错最阴——它一直是绿的，只是测的东西换了）。
COLUMN_INDEX: dict[str, int] = {
    title: index for index, (title, _width) in enumerate(_COLUMNS)
}

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
    """任务列表 + 选中信息 + [继续] [删除记录]。"""

    #: 用户点了「继续」（参数：task_id）
    resume_requested = Signal(str)
    #: 用户点了「删除记录」（参数：task_id）
    delete_requested = Signal(str)
    #: 用户双击了一行（参数：task_id）——用于把任务参数回填到配置面板
    task_activated = Signal(str)
    #: 用户选中了一行（参数：task_id）；未选中任何行时参数为空串。
    #: MainWindow 用它把任务标记成"当前任务"，从而驱动按钮状态矩阵。
    selection_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("下载任务", parent)

        self._tasks: dict[str, DownloadTask] = {}

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

        self._empty_label = WrappingLabel(UI_TEXT_TASK_PANEL_EMPTY, self)
        # 深色主题下 palette(mid) ≈ 背景色，字会整片消失（见 app/ui/widgets.py）
        apply_secondary_text(self._empty_label)

        #: 选中任务的信息区（Spec §10）。内容比一行表格长得多，
        #: 因此用会自己折行的标签，窗口窄的时候也不会被裁掉半句话。
        self._info_label = WrappingLabel(UI_TEXT_TASK_INFO_EMPTY, self)
        apply_secondary_text(self._info_label)

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
        layout.addWidget(self._info_label)
        layout.addLayout(button_row)

        self._resume_button.clicked.connect(self._on_resume_clicked)
        self._delete_button.clicked.connect(self._on_delete_clicked)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        self._table.itemDoubleClicked.connect(self._on_item_double_clicked)

        self.set_tasks([])

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def set_tasks(self, tasks: Sequence[DownloadTask]) -> None:
        """整体刷新列表（按传入顺序展示，通常是 ``updated_at`` 倒序）。"""
        rows = list(tasks)[:TASK_PANEL_MAX_ROWS]
        self._tasks = {task.id: task for task in rows}

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
        self._refresh_info()

    def update_task(self, task: DownloadTask) -> bool:
        """只更新已有的一行（找不到就返回 ``False``，不插入新行）。

        用途：下载进行中把进度刷到列表上。刻意**不做**整体 ``set_tasks``——
        那会重读一次 tasks.json，而在进度回调里每秒读盘 6 次是没有道理的。
        """
        row = self._row_of(task.id)
        if row is None:
            return False
        self._tasks[task.id] = task
        self._fill_row(row, task)
        if self.selected_task_id() == task.id:
            self._refresh_info()
        return True

    def selected_task_id(self) -> str | None:
        """当前选中的任务 id；没选中返回 ``None``。"""
        items = self._table.selectedItems()
        if not items:
            return None
        return self._table.item(items[0].row(), 0).data(_TASK_ID_ROLE)

    def task(self, task_id: str) -> DownloadTask | None:
        """按 id 取列表里那条任务对象；不在列表里返回 ``None``。

        取的是**内存中**的那一份（``set_tasks`` 时存下来的），不重读
        ``tasks.json``——选中一行、按一次按钮都不该产生磁盘 IO。
        """
        return self._tasks.get(task_id)

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
    def empty_label(self):
        """空列表占位文案。"""
        return self._empty_label

    @property
    def info_label(self):
        """选中任务信息区。"""
        return self._info_label

    @property
    def info_text(self) -> str:
        """信息区的**原始**文本（不含自动折行）。"""
        return self._info_label.text()

    def row_text(self, row: int) -> list[str]:
        """某一行的全部单元格文本（测试断言用）。"""
        return [
            self._table.item(row, column).text()
            for column in range(self._table.columnCount())
        ]

    def row_cells(self, row: int) -> dict[str, str]:
        """某一行的 ``{列名: 文本}``。

        比 :meth:`row_text` 更值得用：断言 ``cells["状态"] == "已暂停"`` 不会
        因为将来插了一列而变成在断言另一列的内容。
        """
        return {
            title: self._table.item(row, index).text()
            for index, (title, _width) in enumerate(_COLUMNS)
        }

    def row_for(self, task_id: str) -> dict[str, str] | None:
        """按任务 id 取那一行的 ``{列名: 文本}``；不在列表里返回 ``None``。"""
        row = self._row_of(task_id)
        return None if row is None else self.row_cells(row)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _row_values(task: DownloadTask) -> tuple[str, ...]:
        """把一条任务摊成一行九列的文本。

        地图类型走 :func:`app.models.describe_map_type` 的短名（``矢量+注记``）：
        窄列放不下 ``矢量地图 + 中文注记``，而"矢量"「影像"这两个字足够区分。
        """
        return (
            task.name,
            describe_map_type(
                task.source_map_type, bool(getattr(task, "with_labels", False)), short=True
            ),
            f"{task.min_zoom}-{task.max_zoom}",
            task.status.display_name,
            f"{task.percent:.0f}%",
            f"{task.succeeded_tiles:,}",
            f"{task.failed_tiles:,}",
            task.updated_at.strftime("%m-%d %H:%M"),
            str(task.output_path),
        )

    def _append_row(self, task: DownloadTask) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._fill_row(row, task)

    def _fill_row(self, row: int, task: DownloadTask) -> None:
        """把任务数据填进第 ``row`` 行。"""
        for column, text in enumerate(self._row_values(task)):
            item = self._table.item(row, column)
            if item is None:
                item = QTableWidgetItem()
                self._table.setItem(row, column, item)
            item.setText(text)
            if column == 0:
                item.setData(_TASK_ID_ROLE, task.id)
            # 长路径 / 长错误不换行，靠 tooltip 兜底
            item.setToolTip(text)

        # 进度列的 tooltip 用完整形态（含"已有 N"），把窄列省下的信息补回去
        progress_item = self._table.item(row, COLUMN_INDEX["进度"])
        progress_item.setToolTip(task.progress_text())

        status_item = self._table.item(row, COLUMN_INDEX["状态"])
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

    def _refresh_info(self) -> None:
        """重画选中任务的信息区（Spec §10）。

        信息来自**内存里的任务对象**，不重读 tasks.json：选中一行不该产生
        磁盘 IO，而且列表显示的就是刚读出来的那份。
        """
        task_id = self.selected_task_id()
        task = self._tasks.get(task_id) if task_id else None
        if task is None:
            self._info_label.set_text(UI_TEXT_TASK_INFO_EMPTY)
            return
        self._info_label.set_text(UI_TEXT_TASK_INFO.format(
            name=task.name,
            status=task.status.display_name,
            min_zoom=task.min_zoom,
            max_zoom=task.max_zoom,
            map_type=describe_map_type(
                task.source_map_type, bool(getattr(task, "with_labels", False))
            ),
            path=task.output_path,
            file_state=self._file_state(Path(str(task.output_path))),
        ))

    @staticmethod
    def _file_state(path: Path) -> str:
        """一句话说明目标文件在不在。

        只做 ``is_file()`` + ``stat()``：这两次都是单次系统调用，
        与"数瓦片要查一遍 SQLite"完全不是一个量级。
        """
        try:
            if not path.is_file():
                return UI_TEXT_TASK_INFO_FILE_MISSING
            return UI_TEXT_TASK_INFO_FILE_EXISTS.format(size=format_size(path.stat().st_size))
        except OSError:  # pragma: no cover - 网络盘 / 权限异常
            return UI_TEXT_TASK_INFO_FILE_MISSING

    # ------------------------------------------------------------------ #
    # 槽
    # ------------------------------------------------------------------ #
    @Slot()
    def _on_selection_changed(self) -> None:
        self._refresh_buttons()
        self._refresh_info()
        self.selection_changed.emit(self.selected_task_id() or "")

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
