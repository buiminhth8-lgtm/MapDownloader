"""下载计划页（Slice 20，Spec §11 / §12 / §19 / §20 / §21）。

一页看完"我要下这几块地，一共多大"，并且能改：

::

    ┌ 离线计划 ───────────────────────────── 输出：D:/out/离线计划 ─┐
    │ 已形成三级覆盖：全国级 → 省级 → 市级                          │
    │ ☑ 示例国    全国级  Z4~10    268 张   7.85 MB   ✓ 可用        │
    │ ☑ 示例北省  省级    Z9~13  3,449 张  100.98 MB  ✓ 可用        │
    │ ☑ 示例河市  市级    Z12~16 53,585 张  1.53 GB   ✓ 可用        │
    │ 合计：3 个区域 / 57,302 张 / 1.63 GB                          │
    │ 提醒：计划规模偏大：57,302 张瓦片（超过 50,000 张）…            │
    │ [重新估算] [移除] [清空] [生成任务] [关闭]                     │
    └──────────────────────────────────────────────────────────────┘

这个页面**不做三件事**（Spec §25）：

- 不提供"全部开始下载"。本页只生成 :class:`~app.models.DownloadTask` 记录，
  开始下载仍由用户在下载面板上一个一个发起（批量调度是 Slice 21）。
- 不重新实现 Zoom 推荐、不重新实现瓦片计数：所有数字都来自
  :class:`~app.planning.plan_service.PlanService`，本模块里**没有任何公式**。
- 不替用户改配置（Spec §6）：Zoom 只显示、不编辑；越界的提醒照实说，
  值保持原样。

估算放在后台线程（Spec §20）
----------------------------

"重新估算"按钮起一个 :class:`~app.ui.plan_estimate_worker.PlanEstimateWorker`，
期间按钮换班（重新估算 → 取消估算），表格禁用。**首次打开会自动估算一次**
（计划里还有没算过的条目时）：只显示一堆 ``—`` 的页面几乎等于没有信息，
而用户点开它正是为了看数字。取消后配置一个字都不动（Spec §21）。

界面文案里不出现机器码（Spec §9 / Slice 14 的教训）
--------------------------------------------------

:class:`~app.planning.plan_validator.PlanIssue` 的机器码只用于日志；
这里一律显示 :meth:`PlanIssue.describe` 那句人话。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import UI_TEXT_BUTTON_CLOSE
from app.planning.download_plan import DownloadPlan, DownloadPlanItem
from app.planning.plan_estimator import PlanEstimateProgress
from app.planning.plan_service import PlanService, PlanTaskCreationError
from app.planning.plan_validator import PlanValidationResult
from app.ui.plan_estimate_worker import PlanEstimateWorker
from app.ui.widgets import WrappingLabel, apply_secondary_text

__all__ = [
    "PLAN_ACTION_CANCEL_ESTIMATE",
    "PLAN_ACTION_CLEAR",
    "PLAN_ACTION_CLOSE",
    "PLAN_ACTION_ESTIMATE",
    "PLAN_ACTION_GENERATE",
    "PLAN_ACTION_REMOVE",
    "DownloadPlanDialog",
]


#: 按钮标识（信号载荷与 ``buttons()`` 的键共用的稳定名字）。
PLAN_ACTION_ESTIMATE = "estimate"
PLAN_ACTION_CANCEL_ESTIMATE = "cancel_estimate"
PLAN_ACTION_REMOVE = "remove"
PLAN_ACTION_CLEAR = "clear"
PLAN_ACTION_GENERATE = "generate"
PLAN_ACTION_CLOSE = "close"

#: 表格列（顺序即界面顺序）。勾选框与区域名同占第一列：一行只有一个
#: "这块地"，把它拆成两列会让宽度本来就紧张的表格更难读。
_COLUMNS: tuple[str, ...] = ("区域", "等级", "缩放", "预计瓦片", "预计容量", "状态")

#: 表格里"这一项能不能用"的两种人话（不显示 ``PlanItemStatus`` 的枚举名）
_STATUS_OK = "可参与下载"
_STATUS_DISABLED = "已停用（不参与本次生成）"

#: 估算还没跑完时状态列的占位
_STATUS_PENDING = "尚未估算"


class DownloadPlanDialog(QDialog):
    """下载计划页（Spec §11）。

    Attributes:
        plan_service: 计划编排入口（本页所有操作的唯一落点）。
    """

    #: 生成下载任务成功，载荷是新建的 ``list[DownloadTask]``
    tasks_created = Signal(object)

    #: 计划内容或结论变了（主窗口据此刷新面板摘要与任务列表）
    plan_changed = Signal()

    def __init__(
        self,
        plan_service: PlanService,
        parent: QWidget | None = None,
        *,
        auto_estimate: bool = True,
    ) -> None:
        """
        Args:
            plan_service: 计划编排入口。
            parent: 父窗口。
            auto_estimate: 首次显示时若还有没算过的条目，是否自动估算一次。
                关掉它只是为了在用例里把"估算"这一步握在自己手里。
        """
        super().__init__(parent)
        self.setWindowTitle("下载计划")
        self.setMinimumWidth(720)

        self._plan_service = plan_service
        self._auto_estimate = bool(auto_estimate)
        self._auto_estimate_done = False
        self._worker: PlanEstimateWorker | None = None
        self._last_validation: PlanValidationResult | None = None
        #: 正在重建表格 —— 用来挡住"程序自己改单元格"回调进来的信号（同
        #: ``DownloadPanel._syncing`` 的理由：否则刷新会递归并改坏选中态）。
        self._refreshing = False
        #: 非模态提示框（与 ``MainWindow`` 的确认框同一套做法：不用 ``exec()``，
        #: 免得界面卡在一个必须点掉的框上）
        self._message_box: QMessageBox | None = None

        self._build_header()
        self._build_table()
        self._build_footer()
        layout = QVBoxLayout(self)
        layout.addWidget(self._header_label)
        layout.addWidget(self._coverage_label)
        layout.addWidget(self._table, 1)
        layout.addWidget(self._totals_label)
        layout.addWidget(self._issues_label)
        layout.addLayout(self._progress_row)
        layout.addLayout(self._button_row)

        self.refresh()

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build_header(self) -> None:
        self._header_label = WrappingLabel("", self)
        self._coverage_label = WrappingLabel("", self)
        apply_secondary_text(self._coverage_label)

    def _build_table(self) -> None:
        self._table = QTableWidget(0, len(_COLUMNS), self)
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.itemChanged.connect(self._on_item_changed)
        header = self._table.horizontalHeader()
        header.setStretchLastSection(True)
        for index in range(len(_COLUMNS) - 1):
            self._table.setColumnWidth(index, 108)
        self._table.setColumnWidth(len(_COLUMNS) - 1, 190)

    def _build_footer(self) -> None:
        self._totals_label = WrappingLabel("", self)
        self._issues_label = WrappingLabel("", self)
        apply_secondary_text(self._issues_label)

        self._progress_bar = QProgressBar(self)
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._status_label = QLabel("", self)
        apply_secondary_text(self._status_label)

        self._progress_row = QHBoxLayout()
        self._progress_row.addWidget(self._progress_bar, 1)
        self._progress_row.addWidget(self._status_label)

        self._estimate_button = QPushButton("重新估算", self)
        self._estimate_button.setToolTip(
            "按每个区域当前的范围与缩放级别重算瓦片数与容量（在后台线程里跑）"
        )
        self._cancel_estimate_button = QPushButton("取消估算", self)
        self._cancel_estimate_button.setToolTip(
            "停止本次估算。已算完的区域保留结果，计划配置不会改动"
        )
        self._remove_button = QPushButton("移除", self)
        self._remove_button.setToolTip("把表格里选中的那一行从计划里移除")
        self._clear_button = QPushButton("清空", self)
        self._clear_button.setToolTip("清空计划里的所有区域（保留计划名与输出目录）")
        self._generate_button = QPushButton("生成任务", self)
        self._generate_button.setToolTip(
            "把启用且可用的区域各生成一条下载任务记录。"
            "**不会开始下载** —— 下载仍由「下载任务」列表里逐条发起"
        )
        # 显式给中文文案，**不用** ``QDialogButtonBox`` 的标准按钮：Qt 只在装了
        # 对应翻译文件时才会把 ``Close`` 翻成「关闭」，而本项目没有随程序分发
        # ``qt_zh_CN.qm`` —— 那样这里会显示成一个英文按钮（Slice 9 / 10 的窗口
        # 都是这么处理的：``QPushButton(UI_TEXT_BUTTON_CLOSE)``）。这个缺陷
        # ``pytest`` 抓不到（offscreen 下同样是英文），是靠验收脚本的截图看出来的。
        self._close_button = QPushButton(UI_TEXT_BUTTON_CLOSE, self)
        self._close_button.setToolTip("关闭本页。计划会留在磁盘上，下次打开还在")

        self._buttons: dict[str, QPushButton] = {
            PLAN_ACTION_ESTIMATE: self._estimate_button,
            PLAN_ACTION_CANCEL_ESTIMATE: self._cancel_estimate_button,
            PLAN_ACTION_REMOVE: self._remove_button,
            PLAN_ACTION_CLEAR: self._clear_button,
            PLAN_ACTION_GENERATE: self._generate_button,
            PLAN_ACTION_CLOSE: self._close_button,
        }
        self._estimate_button.clicked.connect(self.estimate)
        self._cancel_estimate_button.clicked.connect(self.cancel_estimate)
        self._remove_button.clicked.connect(self.remove_selected)
        self._clear_button.clicked.connect(self.clear_plan)
        self._generate_button.clicked.connect(self.generate_tasks)
        self._close_button.clicked.connect(self.close)

        # 「关闭」单独放在最右边（``addStretch`` 把它和四个动作按钮隔开）：
        # 它是一个"离开这一页"的动作，混在四个"改这份计划"的按钮里会被误点。
        self._button_row = QHBoxLayout()
        for key in (
            PLAN_ACTION_ESTIMATE,
            PLAN_ACTION_CANCEL_ESTIMATE,
            PLAN_ACTION_REMOVE,
            PLAN_ACTION_CLEAR,
            PLAN_ACTION_GENERATE,
        ):
            self._button_row.addWidget(self._buttons[key])
        self._button_row.addStretch(1)
        self._button_row.addWidget(self._close_button)
        self._apply_busy(False)

    # ------------------------------------------------------------------ #
    # 只读访问器（测试与验收脚本用）
    # ------------------------------------------------------------------ #
    @property
    def plan_service(self) -> PlanService:
        """计划编排入口。"""
        return self._plan_service

    @property
    def plan(self) -> DownloadPlan:
        """当前计划。"""
        return self._plan_service.plan

    def buttons(self) -> dict[str, QPushButton]:
        """按钮字典（``PLAN_ACTION_*`` → 按钮）。"""
        return dict(self._buttons)

    def table(self) -> QTableWidget:
        """计划表格。"""
        return self._table

    def totals_label(self) -> QLabel:
        """合计那一行。"""
        return self._totals_label

    def issues_label(self) -> QLabel:
        """错误 / 提醒那一块。"""
        return self._issues_label

    def status_label(self) -> QLabel:
        """进度右侧那行状态文案。"""
        return self._status_label

    def progress_bar(self) -> QProgressBar:
        """估算进度条。"""
        return self._progress_bar

    def row_region_ids(self) -> list[str]:
        """表格里每一行对应的 ``region_id``（按界面顺序）。"""
        return [str(self._table.item(row, 0).data(Qt.ItemDataRole.UserRole)) for row in range(self._table.rowCount())]

    def row_of(self, region_id: str) -> int:
        """某区域在表格里的行号；不在表里返回 ``-1``。"""
        try:
            return self.row_region_ids().index(region_id)
        except ValueError:
            return -1

    def is_estimating(self) -> bool:
        """是否正在估算。"""
        return self._worker is not None and self._worker.isRunning()

    def wait_for_estimate(self, timeout: int = 30_000) -> bool:
        """等估算线程结束（验收脚本与用例用；界面自己靠信号驱动）。"""
        worker = self._worker
        if worker is None:
            return True
        return bool(worker.wait(timeout))

    # ------------------------------------------------------------------ #
    # 刷新
    # ------------------------------------------------------------------ #
    @Slot()
    def refresh(self) -> None:
        """把计划现状画到界面上（表格 / 合计 / 问题清单 / 按钮可用性）。

        **先校验、后画表**：校验器带副作用 —— 它会把"区域被删了 / 边界文件没了"
        的条目标记成不可用。反过来画的后果是状态列永远慢一拍：用户看到的那一行
        写着"尚未估算"，而下面已经列着"缺边界文件"的错误（自相矛盾的两行）。
        """
        plan = self.plan
        self._last_validation = self._plan_service.validate()
        self._refreshing = True
        try:
            self._fill_table()
        finally:
            self._refreshing = False
        self._header_label.setText(
            f"{plan.name or plan.id}"
            f"　输出目录：{self._plan_service.plan_directory()}"
        )
        self._coverage_label.setText(self._coverage_text())
        self._totals_label.setText(self._totals_text())
        self._issues_label.setText(self._issues_text(self._last_validation))
        self._apply_busy(self.is_estimating())
        self._generate_button.setEnabled(bool(self._last_validation.ok))

    def _fill_table(self) -> None:
        """重建表格内容。"""
        plan = self.plan
        items = list(plan.ordered_items())
        self._table.setRowCount(len(items))
        for row, item in enumerate(items):
            name = QTableWidgetItem(item.region_name or item.region_id)
            name.setData(Qt.ItemDataRole.UserRole, item.region_id)
            name.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            name.setCheckState(
                Qt.CheckState.Checked if item.enabled else Qt.CheckState.Unchecked
            )
            if not item.is_valid:
                name.setToolTip(item.invalid_reason)
            self._table.setItem(row, 0, name)
            for column, text in enumerate(self._row_texts(item), start=1):
                cell = QTableWidgetItem(text)
                cell.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self._table.setItem(row, column, cell)

    @staticmethod
    def _row_texts(item: DownloadPlanItem) -> tuple[str, ...]:
        """一行里除区域名之外的各列文本。"""
        return (
            item.level_text,
            item.zoom_text,
            item.tiles_text,
            item.size_text,
            _status_text(item),
        )

    def _coverage_text(self) -> str:
        """层级覆盖提示（Spec §19：是**提示**，不是硬依赖）。"""
        levels = self.plan.coverage_levels()
        if len(levels) < 2:
            return ""
        names = " → ".join(level.display_name for level in levels)
        if len(levels) >= 3:
            return f"已形成三级离线地图覆盖：{names}"
        return f"当前覆盖：{names}（可以再补上一级）"

    def _totals_text(self) -> str:
        """合计行：张数与容量是"启用且可用"的条目之和（Spec §12）。"""
        plan = self.plan
        if plan.is_empty:
            return "合计：计划里还没有区域"
        disabled = plan.count - plan.enabled_count
        text = (
            f"合计：{plan.enabled_count} 个区域 / {plan.total_tiles_text} 张 / "
            f"{plan.size_text}"
        )
        if disabled:
            text += f"（另有 {disabled} 项未启用，不计入合计）"
        return text

    def _issues_text(self, result: PlanValidationResult | None) -> str:
        """错误与提醒（**人话**，Spec §9；机器码只在日志里）。"""
        if result is None:  # pragma: no cover - 防御性分支
            return ""
        lines = [f"错误：{issue.describe()}" for issue in result.errors]
        lines.extend(f"提醒：{issue.describe()}" for issue in result.warnings)
        if not lines:
            return "检查通过：这个计划可以直接生成下载任务"
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 编辑
    # ------------------------------------------------------------------ #
    @Slot()
    def remove_selected(self) -> None:
        """把选中的那一行移出计划（Spec §11 的「移除」）。"""
        row = self._table.currentRow()
        if row < 0:
            self._status_label.setText("先在上面选中一行，再点「移除」")
            return
        region_id = self._table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        name = self._table.item(row, 0).text()
        if self._plan_service.remove_region(str(region_id)):
            self._status_label.setText(f"已移除「{name}」")
            self._after_change()

    @Slot()
    def clear_plan(self) -> None:
        """清空计划（先问一句：这是不可撤销的，Spec §11）。"""
        if self.plan.is_empty:
            self._status_label.setText("计划本来就是空的")
            return
        self._ask(
            QMessageBox.Icon.Question,
            "清空下载计划",
            f"确定要移除计划里的全部 {self.plan.count} 个区域吗？\n"
            "计划名与输出目录会保留，被移除的区域要重新添加。",
            on_yes=self._do_clear,
        )

    def _do_clear(self) -> None:
        self._plan_service.clear()
        self._status_label.setText("已清空计划")
        self._after_change()

    @Slot(QTableWidgetItem)
    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """勾选框变化 → 启用 / 停用该条目（Spec §12）。"""
        if self._refreshing or item.column() != 0:
            return
        region_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if not region_id:
            return  # pragma: no cover - 防御性分支
        enabled = item.checkState() == Qt.CheckState.Checked
        entry = self.plan.item(region_id)
        if entry is None or entry.enabled is enabled:
            return
        self._plan_service.set_enabled(region_id, enabled)
        self._status_label.setText(
            f"「{item.text()}」已启用" if enabled else f"「{item.text()}」已停用（不参与生成）"
        )
        self._after_change()

    def _after_change(self) -> None:
        """一次编辑之后：重画自己，并通知主窗口刷新摘要。"""
        self.refresh()
        self.plan_changed.emit()

    # ------------------------------------------------------------------ #
    # 估算（Spec §20 / §21）
    # ------------------------------------------------------------------ #
    @Slot()
    def estimate(self) -> None:
        """起一个后台线程重算整份计划（Spec §20）。"""
        if self.is_estimating():
            return
        if self.plan.is_empty:
            self._status_label.setText("计划里还没有区域，先「加入下载计划」")
            return
        worker = PlanEstimateWorker(self._plan_service, self)
        worker.progressed.connect(self._on_progress)
        worker.estimated.connect(self._on_estimated)
        worker.cancelled.connect(self._on_cancelled)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        self._progress_bar.setValue(0)
        self._apply_busy(True)
        self._status_label.setText("正在估算…")
        worker.start()

    @Slot()
    def cancel_estimate(self) -> None:
        """请求取消本次估算（Spec §21）。**不动计划配置**。"""
        worker = self._worker
        if worker is None or not worker.isRunning():
            return
        worker.cancel()
        self._status_label.setText("正在取消估算…")

    @Slot(object)
    def _on_progress(self, progress: PlanEstimateProgress) -> None:
        self._progress_bar.setValue(progress.percent)
        self._status_label.setText(progress.describe())

    @Slot(object)
    def _on_estimated(self, _plan: DownloadPlan) -> None:
        self.refresh()
        self._status_label.setText(
            f"估算完成：{self.plan.total_tiles_text} 张 / {self.plan.size_text}"
        )

    @Slot(str)
    def _on_cancelled(self, message: str) -> None:
        self.refresh()
        self._status_label.setText(f"已取消估算。{message}")
        self._log("用户取消了下载计划估算")

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self.refresh()
        self._status_label.setText(message)
        self._ask(QMessageBox.Icon.Warning, "估算失败", message)

    @Slot()
    def _on_worker_finished(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        self._apply_busy(False)
        self._generate_button.setEnabled(
            bool(self._last_validation is not None and self._last_validation.ok)
        )

    def _apply_busy(self, busy: bool) -> None:
        """估算期间换班：重新估算 → 取消估算，其余动作全部禁用。"""
        self._estimate_button.setEnabled(not busy)
        self._cancel_estimate_button.setEnabled(busy)
        self._remove_button.setEnabled(not busy)
        self._clear_button.setEnabled(not busy)
        # 表格也禁掉：估算线程正就地改写条目，这时勾勾选选会让两边各改一半
        self._table.setEnabled(not busy)
        self._progress_bar.setVisible(busy)
        if busy:
            self._generate_button.setEnabled(False)

    # ------------------------------------------------------------------ #
    # 生成任务（Spec §14 / §15 / §16）
    # ------------------------------------------------------------------ #
    @Slot()
    def generate_tasks(self) -> None:
        """把启用且可用的条目各生成一条下载任务记录（**不开始下载**）。"""
        try:
            tasks = self._plan_service.create_download_tasks()
        except PlanTaskCreationError as exc:
            self._ask(QMessageBox.Icon.Warning, "无法生成下载任务", str(exc))
            self._status_label.setText("生成下载任务失败，一个任务都没有创建")
            return
        self._status_label.setText(f"已生成 {len(tasks)} 条下载任务（尚未开始下载）")
        self._ask(
            QMessageBox.Icon.Information,
            "已生成下载任务",
            "已生成 "
            + f"{len(tasks)} 条下载任务，每一条都停在「已创建」：\n"
            + "\n".join(f"· {task.name} → {task.output_path}" for task in tasks)
            + "\n\n下载请到「下载任务」列表里逐条发起。",
        )
        self.tasks_created.emit(list(tasks))
        self.refresh()

    # ------------------------------------------------------------------ #
    # 杂项
    # ------------------------------------------------------------------ #
    def _ask(
        self,
        icon: QMessageBox.Icon,
        title: str,
        text: str,
        *,
        on_yes=None,
    ) -> QMessageBox:
        """弹一个**非模态**提示框（与 ``MainWindow`` 的确认框同一套做法）。

        不用 ``exec()``：那会让本对话框卡在一个必须点掉的框上，而"点掉它"
        这件事在自动化里要靠另一条线程去点 —— 一个只为了显示信息的框，
        不该有这种代价。
        """
        self._close_message_box()
        box = QMessageBox(icon, title, text, parent=self)
        if on_yes is not None:
            box.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            box.setDefaultButton(QMessageBox.StandardButton.No)
            box.button(QMessageBox.StandardButton.Yes).clicked.connect(
                lambda _checked=False: on_yes()
            )
        else:
            box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.finished.connect(lambda _result: self._forget_message_box(box))
        self._message_box = box
        box.show()
        return box

    def message_box(self) -> QMessageBox | None:
        """当前挂着的提示框（用例与验收脚本据此点按钮）。"""
        return self._message_box

    def _close_message_box(self) -> None:
        box = self._message_box
        if box is not None:
            box.close()

    def _forget_message_box(self, box: QMessageBox) -> None:
        if self._message_box is box:
            self._message_box = None

    def _log(self, message: str) -> None:
        """写主窗口的日志面板（拿不到就只留下界面状态，不报错）。"""
        panel = getattr(self.parent(), "log_panel", None)
        append = getattr(panel, "append_log", None)
        if callable(append):
            append(message, "WARNING")

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名约定
        super().showEvent(event)
        if not self._auto_estimate_done:
            self._auto_estimate_done = True
            if self._auto_estimate and self.plan.needs_estimate():
                self.estimate()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名约定
        """关页面前收干净线程：销毁一个还在跑的 ``QThread`` 会直接崩进程。"""
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            worker.wait(5_000)
        self._close_message_box()
        self._message_box = None
        super().closeEvent(event)


def _status_text(item: DownloadPlanItem) -> str:
    """状态列的人话（不显示 ``PlanItemStatus`` 的枚举名）。"""
    if not item.is_valid:
        return item.invalid_reason or "当前不可用"
    if not item.enabled:
        return _STATUS_DISABLED
    if not item.is_estimated:
        return _STATUS_PENDING
    return _STATUS_OK
