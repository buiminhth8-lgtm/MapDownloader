"""容量预测的后台线程与非模态预算窗口。"""
from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from app.planning.storage_budget import BudgetCancelled, BudgetSettings, assess_budget


class StorageBudgetWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)
    progressed = Signal(str)

    def __init__(self, factory, settings, parent=None, *, assessor=None):
        super().__init__(parent)
        self.factory, self.settings, self.assessor = factory, settings, assessor or assess_budget
        self.stop = threading.Event()

    def cancel(self):
        self.stop.set()

    def run(self):
        try:
            requests = self.factory()
            report = self.assessor(requests, self.settings, should_cancel=self.stop.is_set,
                                   progress=self.progressed.emit)
            if not self.stop.is_set():
                self.completed.emit(report)
        except BudgetCancelled:
            pass
        except Exception as exc:
            if not self.stop.is_set():
                self.failed.emit(f"容量预测失败：{exc}")


class StorageBudgetDialog(QDialog):
    approved = Signal(object)
    cancelled = Signal()
    adjustment_requested = Signal(object)
    activity_changed = Signal()
    report_ready = Signal(object)

    def __init__(self, preferences, parent=None):
        super().__init__(parent)
        self.setWindowTitle("下载容量预算与预测")
        self.resize(780, 620)
        self.preferences = preferences
        self.worker = None
        self.report = None
        self.guarding = False
        self.allow_adjust = True
        self._factory = None
        self._pending_report = None
        self._error = ""
        self._closed = False
        self._auto_continue = False
        self.enabled_check = QCheckBox("下载前自动检查预算与磁盘空间", self)
        self.budget_spin = QDoubleSpinBox(self)
        self.budget_spin.setRange(0.01, 1048576)
        self.budget_spin.setSuffix(" GiB")
        self.reserve_spin = QDoubleSpinBox(self)
        self.reserve_spin.setRange(0, 1048576)
        self.reserve_spin.setSuffix(" GiB")
        self.packaging_check = QCheckBox("同时预留同盘生成一份完整打包副本的空间", self)
        settings = preferences.storage_budget
        self.enabled_check.setChecked(settings.enabled)
        self.budget_spin.setValue(settings.budget_gib)
        self.reserve_spin.setValue(settings.reserve_gib)
        self.packaging_check.setChecked(settings.packaging_copy)
        form = QFormLayout()
        form.addRow(self.enabled_check)
        form.addRow("本次下载/计划的文件预算", self.budget_spin)
        form.addRow("每个磁盘保留空间", self.reserve_spin)
        form.addRow(self.packaging_check)
        self.status_label = QLabel("准备预测", self)
        self.status_label.setWordWrap(True)
        self.report_text = QPlainTextEdit(self)
        self.report_text.setReadOnly(True)
        self.suggestions = QComboBox(self)
        self.apply_button = QPushButton("采用所选 Zoom 建议并返回调整", self)
        self.recalculate_button = QPushButton("重新预测并保存设置", self)
        self.cancel_button = QPushButton("取消预测 / 返回调整", self)
        self.continue_button = QPushButton("确认继续下载", self)
        row = QHBoxLayout()
        for button in (self.recalculate_button, self.cancel_button, self.continue_button):
            row.addWidget(button)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status_label)
        layout.addWidget(self.report_text, 1)
        layout.addWidget(self.suggestions)
        layout.addWidget(self.apply_button)
        layout.addLayout(row)
        self.recalculate_button.clicked.connect(self.recalculate)
        self.cancel_button.clicked.connect(self.reject)
        self.continue_button.clicked.connect(self._approve)
        self.apply_button.clicked.connect(self._apply_suggestion)
        for widget in (self.enabled_check, self.budget_spin, self.reserve_spin, self.packaging_check):
            signal = widget.toggled if isinstance(widget, QCheckBox) else widget.valueChanged
            signal.connect(self._settings_changed)
        self._set_busy(False)

    @property
    def is_running(self):
        return self.worker is not None

    def settings_value(self):
        return BudgetSettings(self.enabled_check.isChecked(), self.budget_spin.value(),
                              self.reserve_spin.value(), self.packaging_check.isChecked())

    def begin(self, factory, *, guard=False, auto_continue=False):
        if self.worker is not None or self.guarding:
            raise ValueError("容量窗口正在处理另一个请求")
        self._factory = factory
        self.guarding = guard
        self._closed = False
        self._auto_continue = auto_continue
        self.continue_button.setVisible(guard)
        self.recalculate(save=False)

    def _settings_changed(self, *_):
        self.report = None
        self.continue_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.status_label.setText("设置已变化，请重新预测后再继续")

    def recalculate(self, *_args, save=True):
        if self.worker is not None or self._factory is None:
            return
        settings = self.settings_value()
        if save:
            self.preferences.set_storage_budget(settings)
        self.report = self._pending_report = None
        self._error = ""
        self._set_busy(True)
        self.status_label.setText("正在后台统计已有瓦片、抽样并预测容量…")
        worker = StorageBudgetWorker(self._factory, settings, self)
        worker.completed.connect(self._remember_report)
        worker.failed.connect(self._remember_error)
        worker.progressed.connect(self.status_label.setText)
        worker.finished.connect(self._finished)
        self.worker = worker
        worker.start()
        self.activity_changed.emit()

    def _remember_report(self, report):
        self._pending_report = report

    def _remember_error(self, error):
        self._error = error

    def _finished(self):
        worker, self.worker = self.worker, None
        self._set_busy(False)
        if self._closed or worker.stop.is_set():
            self.activity_changed.emit()
            return
        self.report = self._pending_report
        if self.report is None:
            self.status_label.setText(self._error or "预测未完成，请重新预测或返回调整")
            self.report_text.setPlainText(self._error)
            self.show()
        else:
            self.report_text.setPlainText("\n".join(self.report.as_lines()))
            self.suggestions.clear()
            for suggestion in self.report.suggestions:
                self.suggestions.addItem(suggestion.describe(), suggestion)
            self.apply_button.setEnabled(self.allow_adjust and bool(self.report.suggestions))
            self.apply_button.setToolTip("请返回下载计划逐条调整区域和 Zoom 后重新生成单包" if not self.allow_adjust else "")
            self.continue_button.setEnabled(self.guarding and self.report.disk_ok)
            self.status_label.setText("磁盘空间不足或无法检测，请调整后重新预测" if not self.report.disk_ok
                                      else "超出预算：可以调整范围/Zoom，或明确继续" if self.report.over_budget
                                      else "预测完成：预算与磁盘检查通过")
            self.report_ready.emit(self.report)
            if self.guarding and self._auto_continue and not self.report.needs_confirmation:
                self._approve()
            else:
                self.show()
                self.raise_()
        self.activity_changed.emit()

    def _set_busy(self, busy):
        for widget in (self.enabled_check, self.budget_spin, self.reserve_spin,
                       self.packaging_check, self.recalculate_button):
            widget.setEnabled(not busy)
        self.continue_button.setEnabled(False)
        self.apply_button.setEnabled(False)

    def _approve(self):
        if self.worker is not None or self.report is None or not self.report.disk_ok:
            return
        report = self.report
        self.guarding = False
        self._closed = True
        self.hide()
        self.approved.emit(report)
        self.activity_changed.emit()

    def _apply_suggestion(self):
        if self.worker is not None or self.report is None:
            return
        suggestion = self.suggestions.currentData()
        if suggestion is None:
            return
        self.guarding = False
        self._closed = True
        self.hide()
        self.adjustment_requested.emit(suggestion)
        self.activity_changed.emit()

    def reject(self):
        self.shutdown()
        self.hide()

    def shutdown(self):
        was_active = self.guarding or not self._closed
        self.guarding = False
        self._closed = True
        worker = self.worker
        if worker is not None:
            worker.cancel()
        if was_active:
            self.cancelled.emit()
        self.activity_changed.emit()
        return worker is None or worker.wait(3000)

    def closeEvent(self, event):  # noqa: N802
        if self.shutdown():
            event.accept()
        else:
            event.ignore()
