"""风险相关的三个对话框（Slice 23，Spec §12 / §14 / §16）。

- :class:`RiskDetailsDialog` —— 「查看详情」：逐 Zoom 明细 + 原因 + 建议；
- :class:`DangerousTaskConfirmDialog` —— **一块地**的强确认（手输确认词）；
- :class:`PlanRiskConfirmDialog` —— **整份计划**的确认（四选一；危险档同时
  要求手输确认词）。

为什么放在同一个模块
--------------------

三个窗口回答的是同一个问题（"这次下载到底有多大风险"），只是一个"给我看"、
两个"要我表态"；而确认框的正文就是风险报告自己产出的那些行
（:meth:`~app.planning.DownloadRiskReport.as_lines` /
:meth:`~app.planning.PlanRiskReport.as_lines`）。拆成两个文件的结果是
"文案生成"与"文案展示"隔着一次 import，改一处忘一处。

为什么"一块地"与"整份计划"是两个窗口
------------------------------------

它们拦下来的是同一件事，但**问的问题不一样**：一块地只能"下"或"不下"，
一份计划却有三个真实去处（继续 / 改小 / 停掉危险项），而且这三条路的代价
不同 —— 合成一个窗口就得让"一块地"也看见三个它执行不了的选项。反过来，
把强确认叠加在四选一之上（先问怎么处理、再问确认词）会让用户连着回答两个
问题，而第二个问题的答案恰恰能被第一个回答问题排除掉（"停用危险项"之后
哪里还有危险档要确认）。因此计划侧是一个窗口一次问清：确认词的输入框
**只在危险档出现**。

都不走 ``exec()``
-----------------

项目里所有对话窗口都是**非模态**的（``show()`` + 信号回调，见
``MainWindow._ask_confirmation`` 的说明）：``exec()`` 会把 Qt 事件循环卡在
那个框上，无头测试直接挂死，有头环境下"下载进行中"的框也会把界面冻住。
因此：

* :class:`RiskDetailsDialog` 用 ``show()``，父窗口持有引用；
* :class:`DangerousTaskConfirmDialog` / :class:`PlanRiskConfirmDialog` 用
  ``QDialog.done()`` 收结果 —— ``done()`` 会发 ``finished(int)``，与非模态
  ``QMessageBox`` 的用法完全一致，调用方（``MainWindow`` / ``DownloadPlanDialog``）
  拿到的是**同一个** ``finished`` 契约。

后者还多占一条：计划的四选一不能靠 ``QMessageBox`` 的标准按钮拼。那一屏里要塞
一个**输入框**（危险档的确认词），而 ``QMessageBox`` 只能通过 ``setCheckBox``
加一个复选框，装不下"输入正确才解锁主按钮"这件事。

强确认为什么是"手输确认词"
--------------------------

"确认"这个动作在 LARGE 档已经用过一次了（``QMessageBox`` 的「确认」按钮）。
危险档再用一次同款按钮，用户的学习结果就是"闭眼点右边那个"。
手输 8 个大写字母是**物理上无法无脑完成**的动作，它买到的是"用户确实看过
这一屏"。确认词放在配置里（:data:`~app.config.DANGEROUS_CONFIRM_WORD`），
界面、测试、文档读到的是同一个字面量。
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.config.settings import (
    DANGEROUS_CONFIRM_WORD,
    UI_TEXT_BUTTON_APPLY_RECOMMENDED_ALL,
    UI_TEXT_BUTTON_BACK_TO_EDIT,
    UI_TEXT_BUTTON_CANCEL,
    UI_TEXT_BUTTON_CLOSE,
    UI_TEXT_BUTTON_CONTINUE_ANYWAY,
    UI_TEXT_BUTTON_DISABLE_DANGEROUS,
    UI_TEXT_DANGEROUS_CONFIRM_PLACEHOLDER,
    UI_TEXT_DANGEROUS_CONFIRM_QUESTION,
    UI_TEXT_DANGEROUS_CONFIRM_TITLE,
    UI_TEXT_RISK_CELL_NO_RULE,
    UI_TEXT_RISK_CELL_OVER,
    UI_TEXT_RISK_CELL_RECOMMENDED,
    UI_TEXT_RISK_COLUMN_RECOMMENDED,
    UI_TEXT_RISK_COLUMN_SIZE,
    UI_TEXT_RISK_COLUMN_TILES,
    UI_TEXT_RISK_COLUMN_ZOOM,
    UI_TEXT_RISK_DETAILS_TITLE,
    UI_TEXT_RISK_DETAILS_TOTAL_ROW,
    UI_TEXT_RISK_PLAN_CONFIRM_QUESTION,
    UI_TEXT_RISK_PLAN_CONFIRM_TITLE,
)
from app.geo.tile_math import estimate_size_bytes, format_size, format_tile_count
from app.planning.risk_models import DownloadRiskReport, PlanRiskReport
from app.ui.widgets import WrappingLabel, apply_secondary_text

__all__ = [
    "DangerousTaskConfirmDialog",
    "PlanRiskConfirmDialog",
    "RiskDetailsDialog",
]


class RiskDetailsDialog(QDialog):
    """逐 Zoom 的风险明细（Spec §14）。

    表格只有四列，出自 Spec：``Zoom | 选中瓦片 | 预计容量 | 推荐``。
    「推荐」列的三态值得说明 —— 它不是"好不好"的评价，而是**该等级的策略判据**：

    * ``推荐``：该 Zoom 不超过推荐上界；
    * ``高于推荐``：超过了（这正是"超推荐张数"的来源，用户能一眼看出是哪一级撑起来的）；
    * ``—``：自定义区域没有推荐区间（Spec §7），此时**不评价**
      —— 画一个"高于推荐"会让用户以为系统知道什么，其实系统只是不套用等级规则。

    表格本身不可编辑（``NoEditTriggers``）：这是一个**结论**视图，
    能改的话用户会以为改这里能改变下载范围。
    """

    def __init__(
        self, report: DownloadRiskReport, parent=None
    ) -> None:  # noqa: ANN001 - Qt 的 parent 类型是 QWidget | None
        """
        Args:
            report: 风险报告（详情区与表格都从它读，不另算一份）。
            parent: 父窗口。

        Raises:
            ValueError: ``report`` 不是 :class:`DownloadRiskReport`。
        """
        super().__init__(parent)
        if not isinstance(report, DownloadRiskReport):
            raise ValueError(
                f"report 必须是 DownloadRiskReport，实际为 {type(report).__name__}"
            )
        self._report = report

        self.setWindowTitle(UI_TEXT_RISK_DETAILS_TITLE)
        # 非模态：``show()`` 由调用方执行（见模块文档）
        self.setModal(False)

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_summary())
        layout.addWidget(self._build_table(), 1)
        layout.addWidget(self._build_advice())
        layout.addLayout(self._build_button_row())

    # ------------------------------------------------------------------ #
    # 只读访问（用例与验收脚本按这些入口操作界面）
    # ------------------------------------------------------------------ #
    @property
    def report(self) -> DownloadRiskReport:
        """本窗口展示的报告。"""
        return self._report

    def table(self) -> QTableWidget:
        """逐 Zoom 明细表。"""
        return self._table

    def summary_label(self) -> QLabel:
        """顶部概览标签。"""
        return self._summary_label

    def advice_label(self) -> QLabel:
        """原因 / 建议标签。"""
        return self._advice_label

    def close_button(self) -> QPushButton:
        """「关闭」按钮。"""
        return self._close_button

    def table_text(self) -> list[list[str]]:
        """表格里每个单元格的文本（含表头行）。"""
        table = self._table
        rows = [
            [table.horizontalHeaderItem(column).text() for column in range(table.columnCount())]
        ]
        rows.extend(
            [
                [
                    table.item(row, column).text()
                    for column in range(table.columnCount())
                ]
                for row in range(table.rowCount())
            ]
        )
        return rows

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #
    def _build_summary(self) -> QLabel:
        """概览：等级 / 张数 / 容量 / 区间（报告自己的行，不自造格式）。"""
        report = self._report
        lines = list(report.headline_lines())
        lines.append(f"实际 Zoom：{report.actual_range_text}")
        recommended = report.recommended_range_text
        lines.append(
            f"推荐 Zoom：{recommended}" if recommended else "推荐 Zoom：（自定义区域不套用）"
        )
        self._summary_label = WrappingLabel("\n".join(lines), self)
        return self._summary_label

    def _build_table(self) -> QTableWidget:
        """逐 Zoom 明细表（数据来自报告的 ``per_zoom``，**不重新遍历瓦片**）。"""
        report = self._report
        self._table = QTableWidget(self)
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(
            [
                UI_TEXT_RISK_COLUMN_ZOOM,
                UI_TEXT_RISK_COLUMN_TILES,
                UI_TEXT_RISK_COLUMN_SIZE,
                UI_TEXT_RISK_COLUMN_RECOMMENDED,
            ]
        )
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setMinimumHeight(220)

        rows = [
            (
                f"z{int(item.zoom)}",
                format_tile_count(int(item.selected_tiles)),
                format_size(
                    estimate_size_bytes(
                        int(item.selected_tiles), report.average_tile_size
                    )
                ),
                self._recommended_text(int(item.zoom)),
            )
            for item in report.per_zoom
        ]
        rows.append(
            (
                UI_TEXT_RISK_DETAILS_TOTAL_ROW,
                format_tile_count(report.total_tiles),
                report.size_text,
                "",  # 合计行不评价"推荐"
            )
        )
        self._table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, text in enumerate(values):
                cell = QTableWidgetItem(text)
                if column != 0:
                    # 数字右对齐：一排"1.03 M / 12.00 MB"左对齐时很难上下比较
                    cell.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self._table.setItem(row, column, cell)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        return self._table

    def _recommended_text(self, zoom: int) -> str:
        """该 Zoom 的「推荐」列文案（三态，见类文档）。"""
        if not self._report.has_recommendation:
            return UI_TEXT_RISK_CELL_NO_RULE
        if self._report.is_above_recommendation(zoom):
            return UI_TEXT_RISK_CELL_OVER
        return UI_TEXT_RISK_CELL_RECOMMENDED

    def _build_advice(self) -> QLabel:
        """原因与建议（逐条，带小标题）。"""
        lines: list[str] = []
        if self._report.reasons:
            lines.append("原因：")
            lines.extend(f"- {item}" for item in self._report.reasons)
        if self._report.recommendations:
            lines.append("建议：")
            lines.extend(f"- {item}" for item in self._report.recommendations)
        self._advice_label = WrappingLabel("\n".join(lines), self)
        return self._advice_label

    def _build_button_row(self) -> QHBoxLayout:
        """只有「关闭」：详情窗口是只读的，没有任何提交动作。

        「采用推荐 Zoom」刻意**不**放在这里 —— 它会改用户的输入，
        必须留在主界面（用户点完就知道改的是哪两个框）。
        """
        self._close_button = QPushButton(UI_TEXT_BUTTON_CLOSE, self)
        self._close_button.clicked.connect(self.close)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self._close_button)
        return row


class DangerousTaskConfirmDialog(QDialog):
    """**一块地**的强确认（Spec §12）：输入确认词后才允许继续。

    整份计划的确认在 :class:`PlanRiskConfirmDialog` —— 那一屏要摆四个去处，
    与这里"下 / 不下"两个按钮不是同一张表。本窗口对报告类型是**鸭子类型**的
    （单任务报告与计划报告都收），因为约束它的是"正文的行怎么来的"，
    不是"这份报告是谁生成的"。

    3 个可观察的行为约定：

    1. **确认按钮初始禁用**，输入框内容 == :data:`~app.config.DANGEROUS_CONFIRM_WORD`
       时才启用（大小写敏感、忽略首尾空白）；清空或改错立刻重新禁用；
    2. ``done(Accepted)`` 才等于"用户确认"，``done(Rejected)``（含关窗、Esc、
       点取消）一律当作"不继续" —— 默认安全；
    3. 正文由报告产出（``as_lines()``），对话框只负责把它显示出来。
    """

    def __init__(
        self, report: DownloadRiskReport | PlanRiskReport, parent=None
    ) -> None:  # noqa: ANN001 - Qt 的 parent 类型是 QWidget | None
        """
        Args:
            report: 风险报告（正文来源）。
            parent: 父窗口。

        Raises:
            ValueError: ``report`` 缺少 :func:`_require_report` 要求的那几个成员。
        """
        super().__init__(parent)
        self._report = _require_report(report)
        #: 续传补充行（"已有 / 还要下"）。由调用方用 :meth:`set_resume_note` 给，
        #: 因为"已有多少张"是**任务状态**（属于下载计划），不是风险报告的一部分
        #: —— 报告只说"这块地多大"。
        self._resume_note: list[str] = []

        self.setWindowTitle(UI_TEXT_DANGEROUS_CONFIRM_TITLE)
        self.setModal(False)

        layout = QVBoxLayout(self)
        self._detail_label = WrappingLabel("", self)
        layout.addWidget(self._detail_label)
        self._refresh_detail()

        self._word_edit = _build_word_prompt(layout, self)

        self._cancel_button = QPushButton(UI_TEXT_BUTTON_CANCEL, self)
        self._cancel_button.clicked.connect(self.reject)
        self._continue_button = QPushButton(UI_TEXT_BUTTON_CONTINUE_ANYWAY, self)
        self._continue_button.setEnabled(False)
        self._continue_button.clicked.connect(self.accept)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self._cancel_button)
        row.addWidget(self._continue_button)
        layout.addLayout(row)

        self._word_edit.textChanged.connect(self._on_word_changed)
        # 回车只在确认词正确时才等于"继续"：否则回车会变成一个绕过闸门的动作
        self._word_edit.returnPressed.connect(self._on_return_pressed)

    # ------------------------------------------------------------------ #
    # 只读访问（用例与验收脚本按这些入口操作界面）
    # ------------------------------------------------------------------ #
    @property
    def report(self) -> DownloadRiskReport | PlanRiskReport:
        """本窗口展示的报告（单任务或整份计划）。"""
        return self._report

    @property
    def confirm_word(self) -> str:
        """要求的确认词（来自配置，不在界面里写死）。"""
        return DANGEROUS_CONFIRM_WORD

    def word_edit(self) -> QLineEdit:
        """确认词输入框。"""
        return self._word_edit

    def continue_button(self) -> QPushButton:
        """「仍然继续」按钮（确认词正确前保持禁用）。"""
        return self._continue_button

    def cancel_button(self) -> QPushButton:
        """「取消」按钮。"""
        return self._cancel_button

    def set_resume_note(self, lines: Sequence[str]) -> None:
        """补上"续传"那几行（没有就传空）。

        Args:
            lines: 追加在报告正文之后的说明行。
        """
        self._resume_note = [str(line) for line in lines]
        self._refresh_detail()

    def resume_note(self) -> list[str]:
        """当前的续传补充行。"""
        return list(self._resume_note)

    def detail_text(self) -> str:
        """正文（报告 + 续传补充行）的原始文本。"""
        return str(self._detail_label.text())

    def _refresh_detail(self) -> None:
        """重绘正文：报告自己的行 + 续传补充行。

        正文**始终**由报告产出（``as_lines()``），对话框不自己拼数字 ——
        "确认框里的 500 TB"与"面板上的 512.12 GB"必须是同一个字符串。
        """
        lines = list(self._report.as_lines())
        if self._resume_note:
            lines.append("")
            lines.extend(self._resume_note)
        self._detail_label.set_text("\n".join(lines))

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _on_word_changed(self, text: str) -> None:
        """输入变化 → 决定「仍然继续」能不能点。"""
        self._continue_button.setEnabled(_word_matches(text))

    def _on_return_pressed(self) -> None:
        """回车：只在确认词正确时继续（否则什么也不做，光标留在框里）。"""
        if self._continue_button.isEnabled():
            self.accept()


def _build_word_prompt(layout: QVBoxLayout, parent) -> QLineEdit:
    """确认词那一块（要求说明 + 输入框 + 大小写提示），返回输入框。

    单任务的强确认与整份计划的确认共用这一段。确认词只有一份配置、说明与
    提示也只有一份文案 —— 各抄一遍的结果是改文案时漏掉其中一个，而漏掉的
    那个恰好是"用户要照着敲的那一行"。

    "区分大小写"必须写在界面上（而不是只写在文档里）：用户在框里敲了
    ``continue`` 却点不动按钮时，唯一能让他不骂程序的就是这一行提示。
    """
    layout.addWidget(
        WrappingLabel(
            UI_TEXT_DANGEROUS_CONFIRM_QUESTION.format(
                word=DANGEROUS_CONFIRM_WORD, button=UI_TEXT_BUTTON_CONTINUE_ANYWAY
            ),
            parent,
        )
    )
    edit = QLineEdit(parent)
    edit.setPlaceholderText(
        UI_TEXT_DANGEROUS_CONFIRM_PLACEHOLDER.format(word=DANGEROUS_CONFIRM_WORD)
    )
    layout.addWidget(edit)
    hint = QLabel(f"确认词：{DANGEROUS_CONFIRM_WORD}（区分大小写）", parent)
    apply_secondary_text(hint)
    layout.addWidget(hint)
    return edit


def _normalized(text: str) -> str:
    """去掉首尾空白（用户从别处粘贴时常带一个换行 / 空格）。

    刻意**不**做 ``upper()``：确认词的"大写"是它的一部分，写死成大小写敏感，
    用户才不会形成"随便敲几个字母就行"的习惯。
    """
    return str(text).strip()


def _word_matches(text: str) -> bool:
    """输入内容是否等于确认词（大小写敏感，忽略首尾空白）。"""
    return _normalized(text) == DANGEROUS_CONFIRM_WORD


class PlanRiskConfirmDialog(QDialog):
    """整份计划的风险确认（Slice 23，Spec §16）。

    四个去处，用 ``done(int)`` 的**自定义退出码**区分（``finished`` 信号会把
    这个整数原样带给调用方）：

    =========================  ===============  ==================================
    退出码                     按钮             后果（由调用方执行）
    =========================  ===============  ==================================
    ``RESULT_CONTINUE``        仍然继续         按现在的规模开跑
    ``RESULT_APPLY_ZOOM``      采用推荐 Zoom    把所有超推荐区间的项改回推荐值
    ``RESULT_DISABLE``         停用危险项       停用所有危险项
    ``RESULT_BACK``（默认）     返回修改         什么都不动
    =========================  ===============  ==================================

    两个设计取舍：

    1. **危险档在同一个窗口里要求手输确认词**（而不是先弹四选一、再弹强确认）：
       计划级的两道闸门叠在一起会让用户连着回答两个问题，而第二个问题
       （"你要不要停用危险项"）恰恰是第一个问题的答案能自动回答的。
       这里一次问清：确认词的输入框只在危险档出现，「仍然继续」在输入正确前点不动。
    2. **默认按钮是「返回修改」**：误按回车不该把一份危险计划跑起来。

    刻意不用 ``QMessageBox`` 的四个标准按钮：那一屏里要塞一个**输入框**，
    而 ``QMessageBox`` 只能通过 ``setCheckBox`` 加一个复选，装不下"输入正确
    才解锁主按钮"这件事。
    """

    #: 与 ``QDialog.DialogCode.Rejected`` 一致（关窗 / Esc / 返回修改）
    RESULT_BACK = 0
    #: 与 ``QDialog.DialogCode.Accepted`` 一致（仍然继续）
    RESULT_CONTINUE = 1
    #: 采用推荐 Zoom
    RESULT_APPLY_ZOOM = 2
    #: 停用危险项
    RESULT_DISABLE_DANGEROUS = 3

    def __init__(
        self, report: PlanRiskReport, parent=None
    ) -> None:  # noqa: ANN001 - Qt 的 parent 类型是 QWidget | None
        """
        Args:
            report: 计划风险报告（正文来源）。
            parent: 父窗口。

        Raises:
            ValueError: ``report`` 不是 :class:`PlanRiskReport`。
        """
        super().__init__(parent)
        if not isinstance(report, PlanRiskReport):
            raise ValueError(
                f"report 必须是 PlanRiskReport，实际为 {type(report).__name__}"
            )
        self._report = report
        self._needs_word = bool(report.requires_strong_confirmation)

        self.setWindowTitle(UI_TEXT_RISK_PLAN_CONFIRM_TITLE)
        self.setModal(False)

        layout = QVBoxLayout(self)
        layout.addWidget(WrappingLabel("\n".join(report.as_lines()), self))
        layout.addWidget(
            WrappingLabel(UI_TEXT_RISK_PLAN_CONFIRM_QUESTION.format(), self)
        )

        #: 确认词那一行只在危险档出现（LARGE / VERY_LARGE 不该多问一遍）
        self._word_edit: QLineEdit | None = None
        if self._needs_word:
            self._word_edit = _build_word_prompt(layout, self)
            self._word_edit.textChanged.connect(self._on_word_changed)

        self._back_button = QPushButton(UI_TEXT_BUTTON_BACK_TO_EDIT, self)
        self._back_button.setToolTip("回到计划页，什么都不改")
        self._disable_button = QPushButton(UI_TEXT_BUTTON_DISABLE_DANGEROUS, self)
        self._disable_button.setToolTip("停用所有危险项（合计与风险立刻重算，本轮不启动）")
        self._apply_button = QPushButton(UI_TEXT_BUTTON_APPLY_RECOMMENDED_ALL, self)
        self._apply_button.setToolTip(
            "把所有超出推荐区间的项改回推荐值，然后重新估算（本轮不启动）"
        )
        self._continue_button = QPushButton(UI_TEXT_BUTTON_CONTINUE_ANYWAY, self)
        self._continue_button.setToolTip("按现在的规模开始执行")
        self._continue_button.setEnabled(not self._needs_word)

        self._back_button.clicked.connect(self.reject)
        self._disable_button.clicked.connect(
            lambda: self.done(self.RESULT_DISABLE_DANGEROUS)
        )
        self._apply_button.clicked.connect(lambda: self.done(self.RESULT_APPLY_ZOOM))
        self._continue_button.clicked.connect(self.accept)

        row = QHBoxLayout()
        row.addWidget(self._back_button)
        row.addStretch(1)
        row.addWidget(self._disable_button)
        row.addWidget(self._apply_button)
        row.addWidget(self._continue_button)
        layout.addLayout(row)

        # "返回修改"在最左、"仍然继续"在最右：越靠右的按钮越"往前走"，
        # 而默认焦点落在最左那一个（误按回车不会开跑）。
        self._back_button.setDefault(True)
        self._back_button.setFocus()

    # ------------------------------------------------------------------ #
    # 只读访问（用例与验收脚本按这些入口操作界面）
    # ------------------------------------------------------------------ #
    @property
    def report(self) -> PlanRiskReport:
        """本窗口展示的报告。"""
        return self._report

    @property
    def requires_word(self) -> bool:
        """本窗口是否要求输入确认词（危险档为真）。"""
        return self._needs_word

    def word_edit(self) -> QLineEdit | None:
        """确认词输入框（不需要时是 ``None``）。"""
        return self._word_edit

    def continue_button(self) -> QPushButton:
        """「仍然继续」按钮。"""
        return self._continue_button

    def apply_zoom_button(self) -> QPushButton:
        """「采用推荐 Zoom」按钮。"""
        return self._apply_button

    def disable_button(self) -> QPushButton:
        """「停用危险项」按钮。"""
        return self._disable_button

    def back_button(self) -> QPushButton:
        """「返回修改」按钮。"""
        return self._back_button

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _on_word_changed(self, text: str) -> None:
        """输入变化 → 决定「仍然继续」能不能点。"""
        self._continue_button.setEnabled(_word_matches(text))


def _require_report(report: object) -> DownloadRiskReport | PlanRiskReport:
    """把参数收敛成"一份画得出来的报告"。

    用**鸭子类型**而不是 ``isinstance``：单任务报告与计划报告是两个类，
    而这个窗口只用到它们共有的那几个成员（``as_lines`` / ``level``）。
    按 isinstance 写就得在这里长出一个 ``if``，将来第三种报告又要改一处。

    Raises:
        ValueError: 缺少必需成员（带上缺了哪几个，便于定位）。
    """
    missing = [
        name
        for name in ("as_lines", "headline_lines", "level")
        if not hasattr(report, name)
    ]
    if missing:
        raise ValueError(
            f"report 必须提供 as_lines / headline_lines / level，"
            f"实际为 {type(report).__name__}，缺少：{'、'.join(missing)}"
        )
    return report  # type: ignore[return-value]
