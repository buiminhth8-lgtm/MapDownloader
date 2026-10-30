from __future__ import annotations

import threading
import time

import pytest
from PySide6.QtCore import QThread
from scripts.verify_storage_optimization import seed_source

from app.geo import DownloadRegion, GeoBounds
from app.services.mbtiles_optimizer import ExportCancelled, ExportTarget
from app.ui import storage_optimization_dialog as ui
from tests.test_storage_budget_ui import until, window  # noqa: F401


@pytest.fixture
def dialog(window,work_dir,qapp):
    source=seed_source(work_dir/'input',alpha=True)
    window.open_storage_optimization(str(source))
    result=window._optimization_dialog
    result.package_check.setChecked(False)
    yield result
    result.shutdown()
    qapp.processEvents()


def preview(dialog,qapp):
    dialog.preview_button.click()
    until(qapp,lambda:dialog.worker is None,timeout=10)
    assert dialog.plan is not None,dialog.status_label.text()


def test_menu_and_package_buttons_open_optimization_window(window,qapp,work_dir):
    window.storage_optimization_action.trigger()
    assert window._optimization_dialog is not None
    source=seed_source(work_dir/'input')
    window.open_package_dialog(str(source))
    until(qapp,lambda:window._package_dialog.diagnostics is not None)
    window._package_dialog.optimize_button.click()
    assert window._optimization_dialog.source_edit.text()==str(source)


def test_preview_needs_no_token_runs_in_worker_and_creates_no_output(dialog,window,qapp,monkeypatch):
    window.download_panel._token_edit.clear()
    real=ui.prepare_export
    threads=[]
    def prepare(*args,**kwargs):
        threads.append(QThread.currentThread())
        return real(*args,**kwargs)
    monkeypatch.setattr(ui,'prepare_export',prepare)
    preview(dialog,qapp)
    assert threads and threads[0] is not window.thread()
    assert not dialog.plan.destination.exists() and dialog.export_button.isEnabled()
    assert window.download_channel_busy()


def test_option_edits_invalidate_previous_preview(dialog,qapp,window):
    preview(dialog,qapp)
    dialog.dedup_check.setChecked(False)
    assert dialog.plan is None and not dialog.export_button.isEnabled()
    assert not window.download_channel_busy()


def test_preview_holds_shared_download_mutex(dialog,qapp,window):
    preview(dialog,qapp)
    window._start_download()
    assert window._download_worker is None and not window.task_service.list_tasks()
    window._cancel_download()
    assert dialog.plan is None and not window.download_channel_busy()


def test_actual_export_is_visible_and_selected_file_can_be_packaged(dialog,qapp,window):
    preview(dialog,qapp)
    destination=dialog.plan.destination
    original=dialog.plan.source.read_bytes()
    dialog.export_button.click()
    until(qapp,lambda:dialog.worker is None,timeout=15)
    assert dialog.report is not None,dialog.status_label.text()
    assert dialog.report['savedFileBytes']>0 and destination.is_dir()
    assert dialog.source_edit.text() and original==__import__('pathlib').Path(dialog.source_edit.text()).read_bytes()
    assert dialog.outputs_combo.count()==1 and dialog.inspect_button.isEnabled()
    assert not window.download_channel_busy()
    dialog.inspect_button.click()
    until(qapp,lambda:window._package_dialog.diagnostics is not None)
    assert window._package_dialog.diagnostics.valid


def test_coverage_rule_split_creates_three_valid_outputs(dialog,qapp):
    dialog.split_combo.setCurrentIndex(1)
    preview(dialog,qapp)
    assert len(dialog.plan.targets)==3
    dialog.export_button.click()
    until(qapp,lambda:dialog.worker is None,timeout=15)
    assert len(dialog.report['outputs'])==3
    assert dialog.outputs_combo.count()==3


def test_plan_region_split_requires_current_enabled_regions(dialog,qapp):
    dialog.split_combo.setCurrentIndex(2)
    dialog.preview()
    assert dialog.worker is None and dialog.plan is None
    assert '没有启用' in dialog.status_label.text()


def test_plan_change_invalidates_selected_region_preview(dialog,qapp,window):
    dialog.split_combo.setCurrentIndex(2)
    region=DownloadRegion.rectangle('requested',GeoBounds(115,38,118,42))
    dialog.target_factory=lambda:lambda:(ExportTarget('requested',region,12,12),)
    preview(dialog,qapp)
    window.plan_service.plan.name='changed-after-preview'
    dialog.export()
    assert dialog.worker is None and dialog.plan is None
    assert '计划已变化' in dialog.status_label.text()


def test_cancel_background_export_releases_mutex_and_preserves_source(dialog,qapp,window,monkeypatch):
    preview(dialog,qapp)
    source=dialog.plan.source
    original=source.read_bytes()
    destination=dialog.plan.destination
    started=threading.Event()
    def slow(plan,*,should_cancel,progress):
        started.set()
        while not should_cancel():
            time.sleep(.002)
        raise ExportCancelled()
    monkeypatch.setattr(ui,'execute_export',slow)
    dialog.export()
    until(qapp,started.is_set)
    assert window.download_channel_busy()
    dialog.reject()
    until(qapp,lambda:dialog.worker is None)
    assert not window.download_channel_busy() and not destination.exists()
    assert source.read_bytes()==original


def test_completed_publication_is_not_misreported_as_cancelled(dialog,qapp,monkeypatch):
    preview(dialog,qapp)
    destination=dialog.plan.destination
    real=ui.execute_export
    def late_cancel(plan,**kwargs):
        report=real(plan,**kwargs)
        dialog.worker.stop.set()  # Commit already happened; a late click cannot undo publication.
        return report
    monkeypatch.setattr(ui,'execute_export',late_cancel)
    dialog.export()
    until(qapp,lambda:dialog.worker is None,timeout=15)
    assert dialog.report is not None and destination.is_dir()
    assert '导出和验证完成' in dialog.status_label.text()


def test_existing_output_error_is_visible_and_does_not_overwrite(dialog,qapp):
    preview(dialog,qapp)
    destination=dialog.plan.destination
    destination.mkdir()
    marker=destination/'keep'
    marker.write_text('keep',encoding='utf-8')
    dialog.export()
    until(qapp,lambda:dialog.worker is None)
    assert marker.read_text(encoding='utf-8')=='keep'
    assert dialog.report is None and '已经存在' in dialog.status_label.text()


def test_shutdown_waits_for_worker_before_main_window_closes(dialog,qapp,window,monkeypatch):
    preview(dialog,qapp)
    started=threading.Event()
    def slow(plan,*,should_cancel,progress):
        started.set()
        while not should_cancel():
            time.sleep(.002)
        raise ExportCancelled()
    monkeypatch.setattr(ui,'execute_export',slow)
    dialog.export()
    until(qapp,started.is_set)
    window._exit_confirmed=True
    window.close()
    until(qapp,lambda:dialog.worker is None)
    assert not window.download_channel_busy()
