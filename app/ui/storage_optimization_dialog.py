"""Windows UI for background storage optimization and region export."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from app.geo import format_size
from app.services.mbtiles_optimizer import (
    ExportCancelled,
    ExportOptions,
    execute_export,
    prepare_export,
)
from app.ui.desktop import open_directory


class OptimizationWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    progressed = Signal(str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.stop = threading.Event()

    def cancel(self):
        self.stop.set()

    def run(self):
        try:
            result = self.operation(self.stop.is_set, self.progressed.emit)
            self.succeeded.emit(result)
        except ExportCancelled:
            pass
        except Exception as exc:
            if not self.stop.is_set():
                self.failed.emit(str(exc))


class StorageOptimizationDialog(QDialog):
    activity_changed = Signal()
    log_message = Signal(str)
    map_selected = Signal(str)

    def __init__(self, parent=None, *, busy_checker=None, target_factory=None, signature_provider=None, reserve_bytes=256*1024*1024):
        super().__init__(parent)
        self.setWindowTitle('MBTiles 压缩、去重与区域分包')
        self.resize(850,710)
        self.busy_checker = busy_checker or (lambda:False)
        self.target_factory = target_factory
        self.signature_provider = signature_provider
        self.reserve_bytes = reserve_bytes
        self.worker = None
        self.plan = None
        self.report = None
        self._pending = None
        self._error = ''
        self._phase = ''
        self._closed = False
        self._destination = None
        self.source_edit = QLineEdit(self)
        self.output_edit = QLineEdit(self)
        self.name_edit = QLineEdit('optimized_maps',self)
        browse_source = QPushButton('选择地图…',self)
        browse_output = QPushButton('选择目录…',self)
        browse_source.clicked.connect(self._browse_source)
        browse_output.clicked.connect(self._browse_output)
        self.split_combo = QComboBox(self)
        self.split_combo.addItems(['完整地图优化为一份文件','按单包覆盖规则分包（Slice 36）','按当前下载计划启用区域分包'])
        self.png_check = QCheckBox('PNG 像素无损优化，保留透明度',self)
        self.png_check.setChecked(True)
        self.dedup_check = QCheckBox('相同图片内容去重',self)
        self.dedup_check.setChecked(True)
        self.dedup_check.setToolTip("启用后提供标准 tiles 视图读取；若设备的导入器额外要求实体表，可关闭去重使用普通表")
        self.jpeg_check = QCheckBox('卫星纯底图转 JPEG（有损；禁止矢量和注记）',self)
        self.quality_spin = QSpinBox(self)
        self.quality_spin.setRange(50,95)
        self.quality_spin.setValue(85)
        self.quality_spin.setEnabled(False)
        self.jpeg_check.toggled.connect(self.quality_spin.setEnabled)
        self.package_check = QCheckBox('同时生成并验证地图包（额外保存一份完整副本）',self)
        self.package_check.setChecked(True)
        form = QFormLayout()
        row = QHBoxLayout()
        row.addWidget(self.source_edit,1)
        row.addWidget(browse_source)
        form.addRow('源 MBTiles',row)
        row = QHBoxLayout()
        row.addWidget(self.output_edit,1)
        row.addWidget(browse_output)
        form.addRow('输出父目录',row)
        form.addRow('新目录名称',self.name_edit)
        form.addRow('导出范围',self.split_combo)
        self.status_label = QLabel('先预览再导出；源地图只读，已有目录不会被覆盖。',self)
        self.status_label.setWordWrap(True)
        self.text = QPlainTextEdit(self)
        self.text.setReadOnly(True)
        self.progress = QProgressBar(self)
        self.progress.setRange(0,0)
        self.progress.hide()
        self.outputs_combo = QComboBox(self)
        self.outputs_combo.setEnabled(False)
        self.inspect_button = QPushButton('检查/打包所选文件',self)
        self.inspect_button.setEnabled(False)
        self.inspect_button.clicked.connect(self._inspect_selected)
        self.preview_button = QPushButton('预览优化与分包',self)
        self.export_button = QPushButton('导出并验证',self)
        self.export_button.setEnabled(False)
        self.cancel_button = QPushButton('取消 / 返回',self)
        self.open_button = QPushButton('打开结果目录',self)
        self.open_button.setEnabled(False)
        self.preview_button.clicked.connect(self.preview)
        self.export_button.clicked.connect(self.export)
        self.cancel_button.clicked.connect(self.reject)
        self.open_button.clicked.connect(self._open_output)
        buttons = QHBoxLayout()
        for button in (self.preview_button,self.export_button,self.open_button,self.cancel_button):
            buttons.addWidget(button)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.png_check)
        layout.addWidget(self.dedup_check)
        row = QHBoxLayout()
        row.addWidget(self.jpeg_check,1)
        row.addWidget(self.quality_spin)
        layout.addLayout(row)
        layout.addWidget(self.package_check)
        layout.addWidget(self.status_label)
        layout.addWidget(self.text,1)
        layout.addWidget(self.progress)
        row = QHBoxLayout()
        row.addWidget(self.outputs_combo,1)
        row.addWidget(self.inspect_button)
        layout.addLayout(row)
        layout.addLayout(buttons)
        self._inputs = [self.source_edit,self.output_edit,self.name_edit,self.split_combo,self.png_check,
                        self.dedup_check,self.jpeg_check,self.quality_spin,self.package_check,browse_source,browse_output]
        for widget in (self.source_edit,self.output_edit,self.name_edit):
            widget.textChanged.connect(self._invalidate)
        self.split_combo.currentIndexChanged.connect(self._invalidate)
        self.quality_spin.valueChanged.connect(self._invalidate)
        for widget in (self.png_check,self.dedup_check,self.jpeg_check,self.package_check):
            widget.toggled.connect(self._invalidate)

    @property
    def holds_channel(self):
        return self.worker is not None or self.plan is not None

    def set_source(self,path):
        if self.worker is not None:
            return
        path = Path(path)
        self.source_edit.setText(str(path))
        self.output_edit.setText(str(path.parent))
        self.name_edit.setText(path.stem+'_optimized')

    def _invalidate(self,*_):
        if self.plan is not None:
            self.status_label.setText("设置已变化，请重新预览")
        self.plan = None
        self.export_button.setEnabled(False)
        self.activity_changed.emit()

    def _browse_source(self):
        path,_ = QFileDialog.getOpenFileName(self,'选择源地图',self.source_edit.text(),'MBTiles (*.mbtiles)')
        if path:
            self.set_source(path)

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(self,'选择输出父目录',self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def preview(self):
        if self.worker is not None or self.busy_checker():
            self.status_label.setText('请先结束当前下载或准备任务')
            return
        self._closed = False
        self.plan = None
        self.export_button.setEnabled(False)
        self.activity_changed.emit()
        try:
            source = self.source_edit.text().strip()
            parent = self.output_edit.text().strip()
            name = self.name_edit.text().strip()
            from app.models.naming import sanitize_map_name
            if not source or not parent or not name or sanitize_map_name(name)!=name:
                raise ValueError('请填写源文件、输出父目录，以及有效的新目录名称')
            destination = Path(parent)/name
            options = ExportOptions(self.png_check.isChecked(),self.dedup_check.isChecked(),
                                    self.quality_spin.value() if self.jpeg_check.isChecked() else None,
                                    self.split_combo.currentIndex()==1,self.package_check.isChecked(),self.reserve_bytes)
            factory = self.target_factory() if self.split_combo.currentIndex()==2 and self.target_factory else None
            if self.split_combo.currentIndex()==2 and factory is None:
                raise ValueError('请先在下载计划中配置并启用分包区域')
        except ValueError as exc:
            self.status_label.setText(str(exc))
            return
        self._external_signature = self.signature_provider() if self.signature_provider and self.split_combo.currentIndex()==2 else None
        def operation(cancel,progress):
            targets = factory() if factory else ()
            return prepare_export(source,destination,options,targets=targets,should_cancel=cancel,progress=progress)
        self._start('preview',operation)

    def export(self):
        if self.worker is not None or self.plan is None or self.busy_checker():
            return
        if self.split_combo.currentIndex()==2 and self.signature_provider and self.signature_provider()!=self._external_signature:
            self._invalidate()
            self.status_label.setText("下载计划已变化，请重新预览分包区域")
            return
        plan = self.plan
        self._active_destination = plan.destination
        self._start('export',lambda cancel,progress:execute_export(plan,should_cancel=cancel,progress=progress))

    def _start(self,phase,operation):
        self._phase = phase
        self._pending = None
        self._error = ''
        self.export_button.setEnabled(False)
        self.preview_button.setEnabled(False)
        for widget in self._inputs:
            widget.setEnabled(False)
        self.progress.show()
        self.status_label.setText('正在后台处理…')
        worker = OptimizationWorker(operation,self)
        worker.succeeded.connect(self._remember)
        worker.failed.connect(self._remember_error)
        worker.progressed.connect(self.status_label.setText)
        worker.finished.connect(self._finished)
        self.worker = worker
        worker.start()
        self.activity_changed.emit()

    def _remember(self,result):
        self._pending = result

    def _remember_error(self,message):
        self._error = message

    def _finished(self):
        worker,self.worker = self.worker,None
        self.progress.hide()
        self.preview_button.setEnabled(True)
        for widget in self._inputs:
            widget.setEnabled(True)
        self.quality_spin.setEnabled(self.jpeg_check.isChecked())
        completed_export = self._phase == "export" and self._pending is not None
        if (self._closed or worker.stop.is_set()) and not completed_export:
            self.plan = None
            self.status_label.setText('已取消，源地图与已有目录保留')
        elif self._pending is None:
            self.plan = None
            self.status_label.setText('处理失败：'+self._error)
            self.text.setPlainText(self._error)
        elif self._phase=='preview':
            self.plan = self._pending
            self.text.setPlainText('\n'.join(self.plan.as_lines()))
            self.status_label.setText('预览完成；点击「导出并验证」开始。有损 JPEG 会改变像素。')
            self.export_button.setEnabled(True)
        else:
            self.report = self._pending
            self._destination = self._active_destination
            self.plan = None
            self.outputs_combo.clear()
            for item in self.report['outputs']:
                self.outputs_combo.addItem(item['file'],str(self._destination/item['file']))
            self.outputs_combo.setEnabled(True)
            self.inspect_button.setEnabled(True)
            self.open_button.setEnabled(True)
            delta = self.report['savedFileBytes']
            self.text.setPlainText('\n'.join([
                f"输出目录：{self._destination}",f"MBTiles 原文件：{format_size(self.report['sourceFileBytes'])}",
                f"MBTiles 输出合计：{format_size(self.report['outputFileBytes'])}",
                f"全部交付文件（含打包副本与报告）：{format_size(self.report['deliveredBytes'])}",
                f"{'减少' if delta>=0 else '增加'}：{format_size(abs(delta))}（不包含打包副本）",
                *[f"{i['file']}：{i['tiles']:,} 张坐标 / {i['uniqueImages']:,} 张独立图片" for i in self.report['outputs']],
                '已按 osmdroid 读取方式逐坐标读取并校验图片；未执行 Android 真机显示验收。',
                '优化产物只读；续传或损坏修复请操作原地图，再重新导出。',
                *self.report['warnings'],
            ]))
            self.status_label.setText('导出和验证完成；实际统计见下方与 export_report.json')
            self.log_message.emit(f'地图存储优化完成：{self._destination}')
        worker.deleteLater()
        self.activity_changed.emit()

    def _inspect_selected(self):
        path = self.outputs_combo.currentData()
        if path:
            self.map_selected.emit(path)

    def _open_output(self):
        if self._destination:
            ok,error = open_directory(self._destination)
            if not ok:
                self.status_label.setText(error)

    def shutdown(self):
        self.export_button.setEnabled(False)
        self.status_label.setText("正在取消处理…" if self.worker is not None else "已取消预览，源地图保留")
        self._closed = True
        self.plan = None
        worker = self.worker
        if worker is not None:
            worker.cancel()
        self.activity_changed.emit()
        return worker is None or worker.wait(3000)

    def reject(self):
        self.shutdown()
        self.hide()

    def closeEvent(self,event):  # noqa: N802
        if self.shutdown():
            event.accept()
        else:
            event.ignore()
