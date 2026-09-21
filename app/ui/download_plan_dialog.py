"""下载计划页（Slice 20 / Slice 21，Spec §11 / §12 / §19 / §20 / §21）。

一页看完"我要下这几块地，一共多大"，并且能改、能**串行下载**：

::

    ┌ 离线计划 ───────────────────────────── 输出：D:/out/离线计划 ─┐
    │ 已形成三级覆盖：全国级 → 省级 → 市级                          │
    │ ☑ 示例国    全国级  Z4~10    268 张   7.85 MB  ✓ 可用        │
    │ ☑ 示例北省  省级    Z9~13  3,449 张  100.98 MB  ▶ 执行中      │
    │ ☑ 示例河市  市级    Z12~16 53,585 张  1.53 GB   ○ 等待中      │
    │ 合计：3 个区域 / 57,302 张 / 1.63 GB                          │
    │ ── 执行 ───────────────────────────────────────────────────  │
    │ 状态：执行中   当前：示例北省 Z9~13   任务：2 / 3              │
    │ 当前任务 ████████░░░░ 62%                                    │
    │ 整份计划 ████░░░░░░░░ 31%（17,860 / 57,302 张）  5.2 MB/s     │
    │ [开始计划][暂停][继续][取消计划][重试当前][跳过并继续]         │
    │ [重新估算] [移除] [清空] [生成任务]                    [关闭]  │
    └──────────────────────────────────────────────────────────────┘

这个页面**不做三件事**（Spec §25）：

- 不在本页实现下载：开始计划只是起一个
  :class:`~app.workers.plan_download_worker.PlanDownloadWorker`，顺序与
  暂停语义全在 :class:`~app.planning.plan_scheduler.PlanScheduler`。
- 不重新实现 Zoom 推荐、不重新实现瓦片计数：所有数字都来自
  :class:`~app.planning.plan_service.PlanService`，本模块里**没有任何公式**；
  执行进度也只照 :meth:`~app.planning.plan_execution.PlanProgress.as_lines`
  画（界面不自己算百分比）。
- 不替用户改配置（Spec §6）：Zoom 只显示、不编辑；越界的提醒照实说，
  值保持原样。

估算放在后台线程（Spec §20）
----------------------------

"重新估算"按钮起一个 :class:`~app.ui.plan_estimate_worker.PlanEstimateWorker`，
期间按钮换班（重新估算 → 取消估算），表格禁用。**首次打开会自动估算一次**
（计划里还有没算过的条目时）：只显示一堆 ``—`` 的页面几乎等于没有信息，
而用户点开它正是为了看数字。取消后配置一个字都不动（Spec §21）。

执行期间禁止改计划（Slice 21 / Spec §18）
-----------------------------------------

一旦有 ``RUNNING`` / ``PAUSED`` 的执行记录，勾选框与"移除 / 清空 / 生成任务 /
重新估算"**全部禁用**：执行记录按 ``plan_item_id`` 认条目，此时删掉一项会让那条
记录永远对不上（下次打开就是"无法恢复"）。界面把这条规则做成**看得见**的
（而不是点了没反应）：执行区写着"执行中：暂停后才能修改计划"。

界面文案里不出现机器码（Spec §9 / Slice 14 的教训）
--------------------------------------------------

:class:`~app.planning.plan_validator.PlanIssue` 的机器码只用于日志；
这里一律显示 :meth:`PlanIssue.describe` 那句人话。
条目状态列显示的是 :class:`~app.planning.plan_execution.PlanItemRunStatus`
的符号与中文名（``✓ 已完成``），不是 ``completed`` 这种枚举值。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QGroupBox,
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
from app.geo.tile_math import format_size
from app.planning.download_plan import DownloadPlan, DownloadPlanItem
from app.planning.plan_estimator import PlanEstimateProgress
from app.planning.plan_execution import (
    PlanExecution,
    PlanExecutionStatus,
    PlanItemExecution,
    PlanProgress,
)
from app.planning.plan_scheduler import PlanScheduler
from app.planning.plan_service import PlanService, PlanTaskCreationError
from app.planning.plan_validator import PlanValidationResult
from app.ui.plan_estimate_worker import PlanEstimateWorker
from app.ui.widgets import WrappingLabel, apply_secondary_text
from app.workers.plan_download_worker import PlanDownloadWorker

__all__ = [
    "PLAN_ACTION_CANCEL_ESTIMATE",
    "PLAN_ACTION_CANCEL_RUN",
    "PLAN_ACTION_CLEAR",
    "PLAN_ACTION_CLOSE",
    "PLAN_ACTION_ESTIMATE",
    "PLAN_ACTION_GENERATE",
    "PLAN_ACTION_PAUSE",
    "PLAN_ACTION_REMOVE",
    "PLAN_ACTION_RESUME",
    "PLAN_ACTION_RETRY",
    "PLAN_ACTION_SKIP",
    "PLAN_ACTION_START",
    "DownloadPlanDialog",
]


#: 按钮标识（信号载荷与 ``buttons()`` 的键共用的稳定名字）。
PLAN_ACTION_ESTIMATE = "estimate"
PLAN_ACTION_CANCEL_ESTIMATE = "cancel_estimate"
PLAN_ACTION_REMOVE = "remove"
PLAN_ACTION_CLEAR = "clear"
PLAN_ACTION_GENERATE = "generate"
PLAN_ACTION_CLOSE = "close"

#: 执行相关按钮（Slice 21 / Spec §15）
PLAN_ACTION_START = "start"
PLAN_ACTION_PAUSE = "pause"
PLAN_ACTION_RESUME = "resume"
PLAN_ACTION_CANCEL_RUN = "cancel_run"
PLAN_ACTION_RETRY = "retry_current"
PLAN_ACTION_SKIP = "skip_current"

#: 表格列（顺序即界面顺序）。勾选框与区域名同占第一列：一行只有一个
#: "这块地"，把它拆成两列会让宽度本来就紧张的表格更难读。
_COLUMNS: tuple[str, ...] = ("区域", "等级", "缩放", "预计瓦片", "预计容量", "状态")

#: 表格里"这一项能不能用"的两种人话（不显示 ``PlanItemStatus`` 的枚举名）
_STATUS_OK = "可参与下载"
_STATUS_DISABLED = "已停用（不参与本次生成）"

#: 估算还没跑完时状态列的占位
_STATUS_PENDING = "尚未估算"

#: 执行期间不许改计划时的提示（Spec §18）
_EDIT_LOCKED_TEXT = "执行中：暂停或结束后才能修改这份计划"


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
        scheduler_factory: "Callable[[], PlanScheduler] | None" = None,
    ) -> None:
        """
        Args:
            plan_service: 计划编排入口。
            parent: 父窗口。
            auto_estimate: 首次显示时若还有没算过的条目，是否自动估算一次。
                关掉它只是为了在用例里把"估算"这一步握在自己手里。
            scheduler_factory: 造一个 :class:`PlanScheduler` 的工厂（Slice 21）。
                由主窗口注入 —— 只有它知道 Token、任务服务与执行记录文件在哪。
                ``None`` 时执行区的按钮保持禁用（本页仍然可以编辑与生成任务）。

                传**工厂**而不是实例：本页只在用户真的点「开始计划」时才需要
                调度器，提前建它会白白拉起重的一串依赖；而一旦建好就**留着自己
                用**（暂停后"继续"必须落在同一个实例上，Spec §9）。
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

        # --- 计划执行（Slice 21） ---------------------------------------- #
        self._scheduler_factory = scheduler_factory
        #: 调度器：**本页持有**，跨"暂停 → 继续"复用（Spec §9）
        self._scheduler: PlanScheduler | None = None
        #: 正在跑的那一次执行的线程桥
        self._run_worker: PlanDownloadWorker | None = None
        #: 最近一帧执行记录（表格的符号与按钮可用性都读它）
        self._execution: PlanExecution | None = None
        #: 最近一帧聚合进度（状态区照它的 ``as_lines()`` 画）
        self._progress: PlanProgress | None = None
        #: 执行区里最近几行日志（Spec §24：关键事件，不刷屏）
        self._run_logs: list[str] = []
        #: 编辑类按钮的原始 tooltip（锁定期间要被替换成"为什么不能改"，解锁时还原）
        self._edit_tooltips: dict[str, str] = {}

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
        layout.addWidget(self._run_box)
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
        self._build_run_area()

    def _build_run_area(self) -> None:
        """执行区（Slice 21 / Spec §15 ~ §18）。

        与"估算进度条"分开画：一个是"算多大"，一个是"下到哪了"，混在一根条上
        用户没法判断到底是哪一步在动。
        """
        self._run_box = QGroupBox("计划执行", self)

        self._run_state_label = WrappingLabel("", self._run_box)
        self._run_detail_label = WrappingLabel("", self._run_box)
        self._run_tiles_label = WrappingLabel("", self._run_box)
        self._run_log_label = WrappingLabel("", self._run_box)
        apply_secondary_text(self._run_log_label)

        self._task_progress_bar = QProgressBar(self._run_box)
        self._task_progress_bar.setRange(0, 100)
        self._task_progress_bar.setFormat("当前任务 %p%")
        self._plan_progress_bar = QProgressBar(self._run_box)
        self._plan_progress_bar.setRange(0, 100)
        self._plan_progress_bar.setFormat("整份计划 %p%")

        self._start_button = QPushButton("开始计划", self._run_box)
        self._start_button.setToolTip(
            "按「全国 → 省 → 市」的顺序**串行**下载：一个跑完才开下一个"
        )
        self._pause_button = QPushButton("暂停", self._run_box)
        self._pause_button.setToolTip("让当前任务安全停下（已写入的瓦片保留），后续任务不启动")
        self._resume_button = QPushButton("继续", self._run_box)
        self._resume_button.setToolTip("从暂停的那一项接着下，已有的瓦片会被跳过")
        self._cancel_run_button = QPushButton("取消计划", self._run_box)
        self._cancel_run_button.setToolTip(
            "停下当前任务并取消后续。**已下载的地图与半截的瓦片都会保留**"
        )
        self._retry_button = QPushButton("重试当前", self._run_box)
        self._retry_button.setToolTip("重新跑失败的那一项（已有的瓦片会被跳过）")
        self._skip_button = QPushButton("跳过并继续", self._run_box)
        self._skip_button.setToolTip("把失败的那一项标记为跳过，然后接着跑下一项")

        self._run_buttons: dict[str, QPushButton] = {
            PLAN_ACTION_START: self._start_button,
            PLAN_ACTION_PAUSE: self._pause_button,
            PLAN_ACTION_RESUME: self._resume_button,
            PLAN_ACTION_CANCEL_RUN: self._cancel_run_button,
            PLAN_ACTION_RETRY: self._retry_button,
            PLAN_ACTION_SKIP: self._skip_button,
        }
        self._start_button.clicked.connect(self.start_plan)
        self._pause_button.clicked.connect(self.pause_plan)
        self._resume_button.clicked.connect(self.resume_plan)
        self._cancel_run_button.clicked.connect(self.cancel_plan)
        self._retry_button.clicked.connect(self.retry_current_item)
        self._skip_button.clicked.connect(self.skip_current_item)

        run_buttons_row = QHBoxLayout()
        for key in (
            PLAN_ACTION_START,
            PLAN_ACTION_PAUSE,
            PLAN_ACTION_RESUME,
            PLAN_ACTION_CANCEL_RUN,
            PLAN_ACTION_RETRY,
            PLAN_ACTION_SKIP,
        ):
            run_buttons_row.addWidget(self._run_buttons[key])
        run_buttons_row.addStretch(1)

        box_layout = QVBoxLayout(self._run_box)
        box_layout.addWidget(self._run_state_label)
        box_layout.addWidget(self._run_detail_label)
        box_layout.addWidget(self._task_progress_bar)
        box_layout.addWidget(self._plan_progress_bar)
        box_layout.addWidget(self._run_tiles_label)
        box_layout.addWidget(self._run_log_label)
        box_layout.addLayout(run_buttons_row)
        self._apply_run_state(None)

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

    # --- 计划执行（Slice 21）：只读访问器（测试与验收脚本用） ---------- #
    def run_buttons(self) -> dict[str, QPushButton]:
        """执行区按钮字典（``PLAN_ACTION_START`` / ``PAUSE`` / … → 按钮）。"""
        return dict(self._run_buttons)

    def run_box(self) -> QGroupBox:
        """执行区那一块。"""
        return self._run_box

    def execution(self) -> PlanExecution | None:
        """最近一帧执行记录（还没开始过时为 ``None``）。"""
        return self._execution

    def plan_progress_bar(self) -> QProgressBar:
        """整份计划的进度条（Spec §12：按张数聚合）。"""
        return self._plan_progress_bar

    def task_progress_bar(self) -> QProgressBar:
        """当前任务的进度条（Spec §14）。"""
        return self._task_progress_bar

    def run_status_lines(self) -> list[str]:
        """执行区那几行状态文案（Spec §16 的区块照它画）。"""
        return [
            self._run_state_label.text(),
            self._run_detail_label.text(),
            self._run_tiles_label.text(),
        ]

    def is_running_plan(self) -> bool:
        """是否有一次计划执行正在跑。"""
        worker = self._run_worker
        return worker is not None and worker.isRunning()

    def wait_for_plan(self, timeout: int = 60_000) -> bool:
        """等计划执行线程结束（验收脚本与用例用）。"""
        worker = self._run_worker
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
        self._sync_editable()
        self._refresh_run_text()

    def _refresh_run_text(self) -> None:
        """执行区那几行文字（没有执行记录时写一句"还没开始"）。"""
        execution = self._execution
        if execution is None:
            self._run_state_label.setText("状态：还没开始执行这份计划")
            self._run_detail_label.setText("")
            self._run_tiles_label.setText("")
            self._task_progress_bar.setValue(0)
            self._plan_progress_bar.setValue(0)
            return
        self._run_state_label.setText(
            f"状态：{execution.status.display_name}　任务：{execution.items_text}"
        )
        if execution.status.is_terminal:
            self._run_detail_label.setText("这一轮已经结束：可以再点「开始计划」重跑")
        self._run_tiles_label.setText(
            f"{execution.completed_tiles:,} / {execution.total_tiles:,} 张"
            f"　已落盘 {_size_text(execution.downloaded_bytes)}"
        )

    def _fill_table(self) -> None:
        """重建表格内容（状态列带上执行符号，Spec §18）。"""
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
            self._decorate_status_cell(row, item)

    def _decorate_status_cell(self, row: int, item: DownloadPlanItem) -> None:
        """状态列升级成"执行态"：``✓ 已完成`` / ``▶ 执行中`` / ``✕ 失败`` …

        只对**这次执行里真的有记录**的条目做 —— 没进执行的（停用的、不可用的）
        继续显示可用性，否则用户看不出那一行为什么没参与。
        """
        entry = self._run_entry(item.id)
        if entry is None:
            return
        cell = self._table.item(row, len(_COLUMNS) - 1)
        if cell is None:  # pragma: no cover - 防御性分支
            return
        cell.setText(f"{entry.status.symbol} {entry.status.display_name}")
        if entry.error:
            cell.setToolTip(entry.error)

    def _run_entry(self, plan_item_id: str) -> PlanItemExecution | None:
        """这次执行里某一条目的记录（没有则为 ``None``）。"""
        execution = self._execution
        if execution is None:
            return None
        return execution.find(plan_item_id)

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
        self._sync_editable()

    def _apply_busy(self, busy: bool) -> None:
        """估算期间换班：重新估算 → 取消估算，其余动作全部禁用。

        只负责"估算进度条 + 取消估算按钮"，其余按钮（含「生成任务」「开始计划」）
        的可用性统一由 :meth:`_sync_editable` 决定 —— 一个按钮的可用性只能有
        一个写入点，否则"谁最后写的算谁的"，回归就会以"某个按钮偶尔是亮的"
        这种形式出现（本 Slice 就踩到过一次：估算结束后生成按钮被解锁了）。
        """
        self._estimate_button.setEnabled(not busy)
        self._cancel_estimate_button.setEnabled(busy)
        self._progress_bar.setVisible(busy)

    # ------------------------------------------------------------------ #
    # 计划执行（Slice 21 / Spec §15 ~ §18 / §20 / §25）
    # ------------------------------------------------------------------ #
    @Slot()
    def start_plan(self) -> None:
        """开始（或接管）执行这份计划（Spec §7 / §15）。"""
        if self.is_running_plan() or self.is_estimating():
            return
        scheduler = self._ensure_scheduler()
        if scheduler is None:  # pragma: no cover - 按钮此时是禁用的
            self._status_label.setText("当前无法执行计划（没拿到调度入口）")
            return
        if self.plan.is_empty:
            self._status_label.setText("计划里还没有区域，先「加入下载计划」")
            return

        self._run_logs.clear()
        worker = PlanDownloadWorker(scheduler, self.plan, token=self._token())
        worker.progress.connect(self._on_run_progress)
        worker.item_changed.connect(self._on_run_frame)
        worker.status_changed.connect(self._on_run_frame)
        worker.log.connect(self._on_run_log)
        worker.completed.connect(self._on_run_completed)
        worker.paused.connect(self._on_run_paused)
        worker.cancelled.connect(self._on_run_cancelled)
        worker.failed.connect(self._on_run_failed)
        worker.finished.connect(self._on_run_worker_finished)
        self._run_worker = worker
        self._status_label.setText("正在开始计划…")
        self._sync_editable()
        worker.start()

    @Slot()
    def pause_plan(self) -> None:
        """暂停（Spec §8）：当前任务安全停下，后续不启动。"""
        worker = self._run_worker
        if worker is None or not worker.isRunning():
            return
        self._status_label.setText("正在安全暂停当前任务…")
        worker.pause()

    @Slot()
    def resume_plan(self) -> None:
        """继续（Spec §9）：从暂停的那一项接着下（跳过已有瓦片）。"""
        worker = self._run_worker
        if worker is None or not worker.isRunning():
            # 线程已结束（例如程序重启后重新打开本页）：用同一个调度器再起一轮
            self.start_plan()
            return
        self._status_label.setText("正在继续执行计划…")
        worker.resume()

    @Slot()
    def cancel_plan(self) -> None:
        """取消计划（Spec §10）。先问一句：这是一次"放弃剩下的"，但**不删成果**。"""
        worker = self._run_worker
        if worker is None:
            return
        self._ask(
            QMessageBox.Icon.Question,
            "取消下载计划",
            "确定要取消这次计划吗？\n"
            "正在下的那一个会安全停下；已经下好的地图与半截的瓦片都会保留。",
            on_yes=self._do_cancel_plan,
        )

    def _do_cancel_plan(self) -> None:
        worker = self._run_worker
        if worker is None:  # pragma: no cover - 防御性分支
            return
        self._status_label.setText("正在取消计划…")
        worker.cancel()

    @Slot()
    def retry_current_item(self) -> None:
        """重试当前失败的那一条（Spec §11）。

        执行线程已经收尾（例如"跳过之后没有可重试的失败项"）时，按
        :meth:`resume_plan` 的同一套做法：用同一个调度器再起一轮，把遗留的
        执行记录接管过来重跑（Spec §9 / §11）。
        """
        worker = self._run_worker
        if worker is None:
            self.start_plan()
            return
        self._status_label.setText("正在重试当前这一项…")
        worker.retry_current()

    @Slot()
    def skip_current_item(self) -> None:
        """跳过当前失败的那一条并继续（Spec §11）。"""
        worker = self._run_worker
        if worker is None:
            self.start_plan()
            return
        self._status_label.setText("已跳过，正在继续…")
        worker.skip_current()

    # ------------------------------------------------------------------ #
    # 执行：调度器与恢复
    # ------------------------------------------------------------------ #
    def _ensure_scheduler(self) -> PlanScheduler | None:
        """按需造出调度器并**留住**（暂停后"继续"必须在同一个实例上，Spec §9）。"""
        if self._scheduler is not None:
            return self._scheduler
        factory = self._scheduler_factory
        if factory is None:
            return None
        self._scheduler = factory()
        return self._scheduler

    def load_stored_execution(self) -> PlanExecution | None:
        """读回磁盘上的执行记录（启动时用，Spec §20）。

        读回来只把遗留的 ``RUNNING`` 降级成 ``PAUSED``，**绝不自动开始下载**：
        界面把状态画出来，是否继续由用户点。
        """
        scheduler = self._ensure_scheduler()
        if scheduler is None:
            return None
        execution = scheduler.load_stored_execution(self.plan)
        if execution is None:
            return None
        self._execution = execution
        if not execution.status.is_terminal:
            self._run_logs = [
                f"上次的计划没跑完（{execution.status.display_name}）："
                "点「继续」可以从当前这一项接着下（已有的瓦片会跳过）"
            ]
            self._log(f"计划「{self.plan.name or self.plan.id}」上次执行未完成，已就绪待继续")
        self.refresh()
        return execution

    def _token(self) -> str:
        """天地图 Token（主窗口拿着；拿不到就是空串，服务层会报"缺 Token"）。"""
        parent = self.parent()
        getter = getattr(parent, "map_token", None)
        return str(getter() if callable(getter) else getter or "")

    # ------------------------------------------------------------------ #
    # 执行：worker 信号（都在主线程执行）
    # ------------------------------------------------------------------ #
    @Slot(object)
    def _on_run_frame(self, execution: PlanExecution) -> None:
        """一帧执行记录：表格符号、按钮可用性、状态区都跟着它走。"""
        self._execution = execution
        self.refresh()

    @Slot(object)
    def _on_run_progress(self, progress: PlanProgress) -> None:
        """一帧聚合进度（Spec §13 / §14：界面**不做任何计算**）。

        文案直接照 :meth:`PlanProgress.as_lines` 排：状态 / 当前 / 任务 /
        当前任务% / 总体% / 速度。界面自己再算一遍百分比的话，"进度条"与
        "状态行"就有两个真值了。
        """
        self._progress = progress
        self._task_progress_bar.setValue(int(round(progress.current_task_percent)))
        self._plan_progress_bar.setValue(int(round(progress.plan_percent)))
        lines = progress.as_lines()
        self._run_state_label.setText("　".join(lines[0:3]))
        self._run_detail_label.setText("　".join(lines[3:5]))
        tiles = f"{lines[5]}　已落盘 {_size_text(progress.downloaded_bytes)}"
        self._run_tiles_label.setText(
            f"{tiles}　{progress.message}" if progress.message else tiles
        )

    @Slot(str)
    def _on_run_log(self, message: str) -> None:
        self._run_logs.append(message)
        del self._run_logs[:-6]  # 只留最近六行，别把这一块撑成一屏日志
        self._run_log_label.setText("\n".join(self._run_logs))

    @Slot(object)
    def _on_run_completed(self, execution: PlanExecution) -> None:
        self._execution = execution
        self._status_label.setText("计划已完成：所有区域都下好了")
        self._log(f"下载计划「{self.plan.name or self.plan.id}」已完成")
        self.refresh()

    @Slot(object)
    def _on_run_paused(self, execution: PlanExecution) -> None:
        self._execution = execution
        self._status_label.setText(
            "计划已暂停：点「继续」会从这一项接着下（已写入的瓦片保留）"
        )
        self._log(f"下载计划「{self.plan.name or self.plan.id}」已暂停")
        self.refresh()

    @Slot(object)
    def _on_run_cancelled(self, execution: PlanExecution) -> None:
        self._execution = execution
        self._status_label.setText(
            "计划已取消：已下载的地图与半截的瓦片都保留在磁盘上"
        )
        self._log(f"下载计划「{self.plan.name or self.plan.id}」已取消")
        self.refresh()

    @Slot(str)
    def _on_run_failed(self, message: str) -> None:
        self._status_label.setText(message)
        self._log(f"下载计划执行失败：{message}")
        self._ask(QMessageBox.Icon.Warning, "计划无法执行", message)
        self.refresh()

    @Slot()
    def _on_run_worker_finished(self) -> None:
        """执行线程结束（终态或收尾完成）。

        ``finished`` 是 ``QThread`` 自带的信号，此时线程真的已经退出 —— 可以
        安全地 ``deleteLater()``。**调度器不销毁**：同一个实例还要给"继续"用。
        """
        worker = self._run_worker
        self._run_worker = None
        if worker is not None:
            worker.deleteLater()
        self._sync_editable()

    # ------------------------------------------------------------------ #
    # 执行：界面状态
    # ------------------------------------------------------------------ #
    def _apply_run_state(self, execution: PlanExecution | None) -> None:
        """按执行状态换班执行区按钮（Spec §15 / §18）。

        没有执行记录、或者记录已到终态 → 只给「开始计划」；
        ``RUNNING`` → 暂停 / 取消；``PAUSED`` → 继续 / 取消；
        ``PARTIAL_FAILED`` → 重试当前 / 跳过并继续 / 取消（Spec §11）。
        """
        if not hasattr(self, "_run_buttons"):  # pragma: no cover - 构建期
            return
        status = execution.status if execution is not None else None
        can_start = self._scheduler_factory is not None and (
            status is None or status.is_terminal
        )
        self._start_button.setEnabled(can_start and not self.is_estimating())
        self._pause_button.setEnabled(status is PlanExecutionStatus.RUNNING)
        self._resume_button.setEnabled(status is PlanExecutionStatus.PAUSED)
        self._cancel_run_button.setEnabled(
            status is not None
            and status
            in (
                PlanExecutionStatus.RUNNING,
                PlanExecutionStatus.PAUSED,
                PlanExecutionStatus.PARTIAL_FAILED,
            )
        )
        self._retry_button.setEnabled(status is PlanExecutionStatus.PARTIAL_FAILED)
        self._skip_button.setEnabled(status is PlanExecutionStatus.PARTIAL_FAILED)
        if self._scheduler_factory is None:
            self._start_button.setToolTip("本窗口没有配置下载入口，暂时不能执行计划")

    def _sync_editable(self) -> None:
        """按"执行是否活着"开关编辑类按钮与表格（Spec §18）。

        ``RUNNING`` / ``PAUSED`` 都算"活着"（暂停期间改计划同样会让执行记录与
        计划对不上）。执行区按钮不受此限 —— 它们正是用来改变这个状态的。

        用"按钮变灰"而不是"点了没反应"来表达这条规则，并把原因写进按钮的
        tooltip：用户鼠标一停就能看到"执行中：暂停或结束后才能修改这份计划"。
        """
        busy_estimate = self.is_estimating()
        execution = self._execution
        locked = (
            execution is not None
            and execution.status.is_active
            and self.is_running_plan()
        )
        reason = (
            _EDIT_LOCKED_TEXT
            if locked
            else "估算进行中，等一下再改"
        )
        for key in (
            PLAN_ACTION_ESTIMATE,
            PLAN_ACTION_REMOVE,
            PLAN_ACTION_CLEAR,
        ):
            button = self._buttons[key]
            if key not in self._edit_tooltips:
                # 第一次进来先把原始 tooltip 存下来 —— 锁上时要能还原回去
                self._edit_tooltips[key] = button.toolTip()
            button.setEnabled(not busy_estimate and not locked)
            button.setToolTip(
                reason if (busy_estimate or locked) else self._edit_tooltips[key]
            )
        # 「生成任务」多一道闸：计划没通过校验时它也得是灰的（Spec §14）。
        # 这个按钮的可用性只在这里被写 —— 别处再写一次就会互相覆盖。
        generate = self._generate_button
        if PLAN_ACTION_GENERATE not in self._edit_tooltips:
            self._edit_tooltips[PLAN_ACTION_GENERATE] = generate.toolTip()
        validate_ok = bool(
            self._last_validation is not None and self._last_validation.ok
        )
        generate.setEnabled(validate_ok and not busy_estimate and not locked)
        generate.setToolTip(
            reason
            if (busy_estimate or locked)
            else self._edit_tooltips[PLAN_ACTION_GENERATE]
        )
        self._table.setEnabled(not busy_estimate and not locked)
        self._estimate_button.setEnabled(not busy_estimate and not locked)
        self._cancel_estimate_button.setEnabled(busy_estimate)
        if locked and _EDIT_LOCKED_TEXT not in self._run_logs:
            # 锁上时写一行日志：用户点开这一页正是为了看"为什么不能改了"
            self._on_run_log(_EDIT_LOCKED_TEXT)
        self._apply_run_state(execution)

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
            # 先看有没有"上次没跑完"的执行记录（Spec §20）：有的话把状态画出来，
            # 让用户看见「继续 / 取消」，但**绝不自动开始下载**。
            self.load_stored_execution()
            if self._auto_estimate and self.plan.needs_estimate():
                self.estimate()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名约定
        """关页面前收干净线程：销毁一个还在跑的 ``QThread`` 会直接崩进程。

        计划执行线程特殊（Spec §25）：**先安全暂停再等**（不 ``terminate()``）——
        那一步会把 MBTiles flush 掉、把执行记录落成"可继续"，下次打开就能接着下。
        """
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            worker.wait(5_000)
        run_worker = self._run_worker
        if run_worker is not None and run_worker.isRunning():
            run_worker.request_close()
            if not run_worker.wait(30_000):  # pragma: no cover - 只在磁盘卡死时
                self._log("关闭下载计划页时，执行线程没有在 30 秒内收干净")
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


def _size_text(size_bytes: int) -> str:
    """字节数的人话（``7.85 MB``）—— 复用 geo 层那一份格式化，界面不自己写。"""
    return format_size(max(0, int(size_bytes)))
