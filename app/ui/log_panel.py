"""日志面板。

职责单一：接收文本并按行展示，不做业务判断、不直接读日志文件。

线程约定：``append_log`` 被声明为 Qt Slot，后续下载线程通过
``signal.connect(log_panel.append_log)`` 跨线程投递即可，
无需在本类内部加锁或使用 QTimer。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import APP_VERSION

#: 日志区最多保留的行数，防止长时间运行内存膨胀
MAX_LOG_BLOCKS = 5000

#: 每行时间戳格式
TIMESTAMP_FORMAT = "%H:%M:%S"


class LogPanel(QGroupBox):
    """运行日志展示区。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("运行日志", parent)

        self._text = QPlainTextEdit(self)
        self._text.setReadOnly(True)
        self._text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._text.setMaximumBlockCount(MAX_LOG_BLOCKS)
        self._text.setPlaceholderText("暂无日志输出")

        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)
        self._text.setFont(font)

        self._clear_button = QPushButton("清空", self)
        self._clear_button.setToolTip("清空当前日志显示（不影响控制台输出）")

        header = QHBoxLayout()
        header.addWidget(QLabel(f"v{APP_VERSION}", self))
        header.addStretch(1)
        header.addWidget(self._clear_button)

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self._text, 1)

        self._clear_button.clicked.connect(self._on_clear_clicked)

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    @Slot(str, str)
    def append_log(self, message: str, level: str = "INFO") -> None:
        """追加一行日志。

        Args:
            message: 日志正文。
            level: 级别标记，仅用于展示（如 ``INFO`` / ``WARNING``）。
        """
        timestamp = datetime.now().strftime(TIMESTAMP_FORMAT)
        line = f"{timestamp} [{level.upper()}] {message}"
        self._text.appendPlainText(line)
        # 自动滚动到底部，查看最新日志无需手动下拉
        self._text.verticalScrollBar().setValue(
            self._text.verticalScrollBar().maximum()
        )

    @Slot()
    def clear_log(self) -> None:
        """清空日志区。"""
        self._text.clear()

    def to_text(self) -> str:
        """返回当前日志全文（主要供测试与复制使用）。"""
        return self._text.toPlainText()

    @property
    def text_edit(self) -> QPlainTextEdit:
        """暴露底层控件，便于测试与后续扩展。"""
        return self._text

    # ------------------------------------------------------------------ #
    # 内部槽
    # ------------------------------------------------------------------ #
    @Slot()
    def _on_clear_clicked(self) -> None:
        self.clear_log()
