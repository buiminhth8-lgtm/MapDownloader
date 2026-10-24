from __future__ import annotations

import threading
import time

import pytest
from PySide6.QtCore import QThread

import app.ui.storage_budget_dialog as budget_ui
from app.planning.storage_budget import GIB, BudgetCancelled, BudgetSettings, assess_budget
from app.tasks import TaskService, TaskStore
from app.ui.main_window import MainWindow
from app.ui.storage_budget_dialog import StorageBudgetDialog
from tests.test_storage_budget import request


def until(qapp, condition, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qapp.processEvents()
        if condition():
            return
        time.sleep(0.002)
    pytest.fail("容量预测界面没有在时限内完成")


@pytest.fixture
def window(qapp, tasks_file, preferences, region_resources, work_dir):
    win = MainWindow(task_service=TaskService(TaskStore(tasks_file)), preferences=preferences)
    panel = win.download_panel
    panel.set_output_dir(work_dir)
    panel.set_zoom_range(12, 12)
    panel.map_name_edit.setText("budget-map")
    panel._token_edit.setText("offline-test-token")
    for spin, value in zip(panel.bounds_spins, (116.2, 39.7, 116.3, 39.8), strict=True):
        spin.setValue(value)
    yield win
    win._exit_confirmed = True
    win.close()
    qapp.processEvents()


@pytest.fixture(autouse=True)
def deterministic_disk(monkeypatch):
    def assessor(requests, settings, **kwargs):
        return assess_budget(requests, settings, disk_probe=lambda p: ("test", 1000 * GIB), **kwargs)
    monkeypatch.setattr(budget_ui, "assess_budget", assessor)


def test_manual_prediction_needs_no_token_and_runs_in_worker(window, qapp):
    window.download_panel._token_edit.clear()
    window.download_panel.storage_budget_button.click()
    dialog = window._storage_budget_dialog
    assert dialog is not None and dialog.worker is not QThread.currentThread()
    until(qapp, lambda: dialog.worker is None)
    assert dialog.report is not None
    assert "HTTP" in dialog.report_text.toPlainText()
    assert window._download_worker is None


def test_single_start_checks_budget_before_network_and_risk(window, qapp, monkeypatch):
    reached = []
    monkeypatch.setattr(window, "_continue_plan_after_budget", reached.append)
    window._start_download()
    assert window._download_worker is None
    until(qapp, lambda: bool(reached))
    assert reached[0].budget_checked
    assert reached[0].estimate.estimated_size_bytes > 0
    assert window._storage_budget_dialog.report is not None
    assert not window._storage_budget_dialog.guarding


def test_over_budget_waits_for_explicit_continue(window, qapp, monkeypatch):
    window.preferences.set_storage_budget(BudgetSettings(budget_gib=0.01, reserve_gib=0))
    window.download_panel.set_zoom_range(16, 16)
    reached = []
    monkeypatch.setattr(window, "_continue_plan_after_budget", reached.append)
    window._start_download()
    dialog = window._storage_budget_dialog
    until(qapp, lambda: dialog.worker is None)
    assert dialog.report.over_budget and dialog.guarding
    assert reached == [] and window._download_worker is None
    dialog.continue_button.click()
    assert len(reached) == 1
    assert "超预算继续" in window.log_panel.to_text()


def test_insufficient_disk_disables_continue_and_slot_rechecks(window, qapp, monkeypatch):
    monkeypatch.setattr(budget_ui, "assess_budget", lambda requests, settings, **kwargs:
                        assess_budget(requests, settings, disk_probe=lambda p: ("disk", 0), **kwargs))
    reached = []
    monkeypatch.setattr(window, "_continue_plan_after_budget", reached.append)
    window._start_download()
    dialog = window._storage_budget_dialog
    until(qapp, lambda: dialog.worker is None)
    assert not dialog.continue_button.isEnabled()
    dialog._approve()
    assert reached == [] and window._download_worker is None


def test_form_changes_invalidate_pending_prediction(window, qapp, monkeypatch):
    window.preferences.set_storage_budget(BudgetSettings(budget_gib=0.01, reserve_gib=0))
    window.download_panel.set_zoom_range(16, 16)
    reached = []
    monkeypatch.setattr(window, "_continue_plan_after_budget", reached.append)
    window._start_download()
    dialog = window._storage_budget_dialog
    until(qapp, lambda: dialog.worker is None)
    window.download_panel.set_map_name("changed-after-prediction")
    dialog.continue_button.click()
    assert not reached
    assert "预测已失效" in window.log_panel.to_text()


def test_user_can_apply_zoom_advice_without_downloading_or_removing_files(window, qapp, monkeypatch, work_dir):
    window.preferences.set_storage_budget(BudgetSettings(budget_gib=0.01, reserve_gib=0))
    window.download_panel.set_zoom_range(15, 16)
    reached = []
    monkeypatch.setattr(window, "_continue_plan_after_budget", reached.append)
    window._start_download()
    dialog = window._storage_budget_dialog
    until(qapp, lambda: dialog.worker is None)
    assert dialog.report.over_budget and dialog.apply_button.isEnabled()
    dialog.apply_button.click()
    data = window.download_panel.get_form_data()
    assert data["max_zoom"] == 15
    assert "z15_15" in data["map_name"]
    assert not reached and window._download_worker is None
    assert list(work_dir.glob("*.mbtiles")) == []


def test_cancel_stops_background_prediction_and_releases_shared_download_channel(window, qapp, monkeypatch):
    started = threading.Event()

    def slow(requests, settings, *, should_cancel, progress):
        started.set()
        while not should_cancel():
            time.sleep(0.002)
        raise BudgetCancelled()

    monkeypatch.setattr(budget_ui, "assess_budget", slow)
    window._start_download()
    dialog = window._storage_budget_dialog
    until(qapp, started.is_set)
    assert window.download_channel_busy()
    window._cancel_download()
    until(qapp, lambda: dialog.worker is None)
    assert window._storage_budget_context is None
    assert not window.download_channel_busy()
    assert window._download_worker is None


def test_changed_budget_requires_reprediction(preferences, qapp, work_dir):
    dialog = StorageBudgetDialog(preferences)
    try:
        dialog.begin(lambda: (request(work_dir),), guard=True)
        until(qapp, lambda: dialog.worker is None)
        # 默认预算内自动继续关闭；这里模拟新的人工确认，不复用旧的数值。
        dialog.guarding = True
        dialog.budget_spin.setValue(32)
        assert dialog.report is None
        assert not dialog.continue_button.isEnabled()
        dialog.recalculate()
        until(qapp, lambda: dialog.worker is None)
        assert dialog.report.settings.budget_gib == 32
        assert preferences.storage_budget.budget_gib == 32
    finally:
        dialog.shutdown()


def test_disabled_automatic_check_still_allows_manual_prediction(window, qapp, monkeypatch):
    window.preferences.set_storage_budget(BudgetSettings(enabled=False))
    reached = []
    monkeypatch.setattr(window, "_continue_plan_after_budget", reached.append)
    window._start_download()
    assert reached and window._storage_budget_dialog is None
    window.open_storage_budget()
    until(qapp, lambda: window._storage_budget_dialog.worker is None)
    assert window._storage_budget_dialog.report is not None


def test_plan_prediction_honors_enabled_items_and_precedes_start(window, qapp):
    window.download_panel.set_region_id("rivertown")
    window.download_panel.set_zoom_range(10, 10)
    window._on_plan_add_requested()
    assert window.plan_service.plan.enabled_items
    reached = []
    window.check_plan_storage_budget(lambda: reached.append(True))
    until(qapp, lambda: bool(reached))
    report = window._storage_budget_dialog.report
    assert len(report.items) == 1
    assert report.items[0].request.id == "rivertown"
    assert window._download_worker is None


def test_main_window_close_waits_for_budget_thread(window, qapp, monkeypatch):
    def slow(requests, settings, *, should_cancel, progress):
        while not should_cancel():
            time.sleep(0.002)
        raise BudgetCancelled()

    monkeypatch.setattr(budget_ui, "assess_budget", slow)
    window.open_storage_budget()
    worker = window._storage_budget_dialog.worker
    window._exit_confirmed = True
    window.close()
    assert not worker.isRunning()


def test_manual_report_is_non_mutating_and_no_network(window, qapp, work_dir):
    before = window.download_panel.get_form_data()
    window.open_storage_budget()
    until(qapp, lambda: window._storage_budget_dialog.worker is None)
    assert window.download_panel.get_form_data() == before
    assert list(work_dir.glob("*.mbtiles")) == []
    assert window._download_worker is None


def test_default_preflight_download_failure_and_resume_preserve_existing_tiles(window, qapp, monkeypatch):
    import httpx

    from app.models import DownloadTaskStatus
    from tests.test_ui_smoke import FAKE_PNG, _stub_download_service

    state = {"first": None, "healthy": False}
    requests = []

    def server(request):
        key = (request.url.params["TILECOL"], request.url.params["TILEROW"])
        requests.append(key)
        if state["first"] is None:
            state["first"] = key
        if state["healthy"] or key == state["first"]:
            return httpx.Response(200, content=FAKE_PNG, headers={"content-type": "image/png"})
        return httpx.Response(403)

    _stub_download_service(monkeypatch, server)
    window._start_download()
    until(qapp, lambda: window._download_worker is None
          and bool(window.task_service.list_tasks())
          and window.task_service.list_tasks()[0].status is DownloadTaskStatus.FAILED)
    task = window.task_service.list_tasks()[0]
    first_report = window._storage_budget_dialog.report
    assert first_report is not None and task.output_path.is_file()
    before = len(requests)
    state["healthy"] = True
    window._on_task_resume_requested(task.id)
    until(qapp, lambda: window._download_worker is None
          and (current := window.task_service.get_task(task.id)) is not None
          and current.status is DownloadTaskStatus.COMPLETED)
    resumed_report = window._storage_budget_dialog.report
    assert resumed_report is not first_report
    assert sum(z.existing for z in resumed_report.items[0].zooms) == 1
    assert len(requests) - before == resumed_report.requests
    assert all(key != state["first"] for key in requests[before:])
    assert task.output_path.is_file()
