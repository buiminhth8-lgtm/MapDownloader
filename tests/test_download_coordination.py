"""后台估算与下载入口互斥的交互回归。"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer

from app.geo.region_estimator import RegionEstimateCancelled, RegionTileEstimator
from app.models import DownloadTaskStatus
from app.tasks import TaskService, TaskStore
from app.ui.download_panel import ACTION_CANCEL, ACTION_START
from app.ui.download_plan_dialog import DownloadPlanDialog
from app.ui.main_window import MainWindow


@pytest.fixture
def window(qapp, tasks_file, preferences, region_resources):
    win = MainWindow(task_service=TaskService(TaskStore(tasks_file)), preferences=preferences)
    yield win
    if win._plan_dialog is not None:
        worker = win._plan_dialog._run_worker
        if worker is not None and hasattr(worker, "request_close"):
            worker.request_close()
            assert worker.wait(5000)
        win._plan_dialog._run_worker = None
    win._pending_plan = None
    win._exit_confirmed = True
    win.close()
    qapp.processEvents()


def _until(qapp, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(0.001)
    pytest.fail("后台任务未在期限内完成")


def _polygon(win):
    win.download_panel.set_region_id("rivertown")
    win.download_panel.set_zoom_range(10, 10)
    win.download_panel._token_edit.setText("fake-test-token")


def _slow_estimator(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    threads = []
    original = RegionTileEstimator.estimate

    def slow(self, low, high, *, should_cancel=None, progress=None):
        threads.append(threading.get_ident())
        started.set()
        while not release.wait(0.002):
            if should_cancel and should_cancel():
                raise RegionEstimateCancelled("cancelled")
        return original(self, low, high, should_cancel=should_cancel, progress=progress)

    monkeypatch.setattr(RegionTileEstimator, "estimate", slow)
    return started, release, threads


def _plan_dialog(win):
    dialog = DownloadPlanDialog(win.plan_service, win, auto_estimate=False)
    dialog.download_activity_changed.connect(win._refresh_action_state)
    win._plan_dialog = dialog
    return dialog


def test_running_plan_blocks_both_single_download_entries(window, monkeypatch):
    dialog = _plan_dialog(window)
    dialog._run_worker = SimpleNamespace(isRunning=lambda: True, deleteLater=lambda: None)
    calls = []
    monkeypatch.setattr(window, "_prepare_download", lambda **kw: calls.append(kw))
    window._start_download()
    window._on_task_resume_requested("any-task")
    assert window.download_channel_busy()
    assert not calls
    window._refresh_action_state()
    assert not window.download_panel.action_buttons[ACTION_START].isEnabled()
    dialog._on_run_worker_finished()
    assert not window.download_channel_busy()
    assert window.download_panel.action_buttons[ACTION_START].isEnabled()


def test_single_confirmation_blocks_plan_even_after_its_risk_confirmation(window, monkeypatch):
    dialog = _plan_dialog(window)
    calls = []
    monkeypatch.setattr(dialog, "_ensure_scheduler", lambda: calls.append(True))
    window._pending_plan = object()
    dialog._do_start_plan()
    assert not calls
    assert dialog._run_worker is None
    window._pending_plan = None
    assert not window.download_channel_busy()


def test_single_estimate_runs_off_ui_thread_and_can_be_cancelled(window, monkeypatch, qapp):
    _polygon(window)
    started, release, threads = _slow_estimator(monkeypatch)
    window._run_tile_math()
    _until(qapp, started.is_set)
    assert len(threads) == 1 and threads[0] != threading.get_ident()
    alive = []
    QTimer.singleShot(0, lambda: alive.append(True))
    _until(qapp, lambda: bool(alive))
    assert window.download_panel.action_buttons[ACTION_CANCEL].isEnabled()
    window._start_download()
    assert not window.task_service.list_tasks()
    window._cancel_download()
    _until(qapp, lambda: window._estimate_worker is None)
    release.set()
    assert window._last_calculation is None
    assert not window.download_channel_busy()


def test_estimate_result_is_discarded_if_form_changed(window, monkeypatch, qapp):
    _polygon(window)
    started, release, _ = _slow_estimator(monkeypatch)
    starts = []
    monkeypatch.setattr(window, "_start_prepared_download", lambda *args: starts.append(args))
    window._start_download()
    _until(qapp, started.is_set)
    window.download_panel.set_map_name("changed while estimating")
    release.set()
    _until(qapp, lambda: window._estimate_worker is None)
    assert not starts
    assert not window.task_service.list_tasks()


def test_background_estimate_continues_download_only_once(window, monkeypatch, qapp):
    _polygon(window)
    started, release, _ = _slow_estimator(monkeypatch)
    starts = []
    monkeypatch.setattr(window, "_start_prepared_download", lambda *args: starts.append(args))
    window._start_download()
    _until(qapp, started.is_set)
    window._start_download()
    assert not starts
    release.set()
    _until(qapp, lambda: bool(starts))
    assert len(starts) == 1
    data, estimate = starts[0]
    assert data["region_id"] == "rivertown"
    assert estimate.selected_tiles > 0
    assert window._estimate_worker is None


def test_legacy_polygon_resume_without_total_is_estimated_in_background(
    window, monkeypatch, qapp, work_dir
):
    _polygon(window)
    region = window.download_panel.current_region()
    task = window.task_service.create_task(
        name="old task",
        bounds=region.bounds,
        min_zoom=10,
        max_zoom=10,
        map_layer="vec_w",
        output_path=work_dir / "old.mbtiles",
        region=region,
    )
    assert task.total_tiles == 0
    started, release, _ = _slow_estimator(monkeypatch)
    plans = []
    monkeypatch.setattr(window, "_continue_plan", plans.append)
    window._on_task_resume_requested(task.id)
    _until(qapp, started.is_set)
    assert not plans
    release.set()
    _until(qapp, lambda: bool(plans))
    assert plans[0].total > 0
    assert window.task_service.get_task(task.id).status is DownloadTaskStatus.CREATED


def test_closing_window_cancels_estimate_and_joins_thread(window, monkeypatch, qapp):
    _polygon(window)
    started, release, _ = _slow_estimator(monkeypatch)
    window._run_tile_math()
    _until(qapp, started.is_set)
    worker = window._estimate_worker
    window._exit_confirmed = True
    window.close()
    assert not worker.isRunning()
    release.set()


def test_polygon_cancellation_checks_rejected_candidates_too():
    from shapely.geometry import box

    from tests.test_region_estimator import polygon_region

    estimator = RegionTileEstimator(polygon_region(), geometry=box(0, 0, 1, 1))
    region_filter = estimator.region_filter
    with pytest.raises(RegionEstimateCancelled):
        estimator.estimate(18, 18, should_cancel=lambda: region_filter.tile_count >= 256)
    assert region_filter.tile_count <= 512


def test_real_plan_keeps_channel_reserved_while_paused(window, work_dir, qapp):
    from app.planning.plan_execution import PlanExecutionStatus
    from app.planning.plan_scheduler import PlanScheduler
    from tests.plan_fakes import FakeRunner, ready_plan

    ready_plan(window.plan_service, work_dir, "rivertown")
    runner = FakeRunner(hang=True)
    dialog = _plan_dialog(window)
    dialog._scheduler_factory = lambda: PlanScheduler(
        window.plan_service,
        runner,
        task_service=window.task_service,
        store=window.execution_store,
    )
    dialog._do_start_plan()
    _until(qapp, lambda: bool(runner.calls))
    assert window.download_channel_busy()
    assert not window.download_panel.action_buttons[ACTION_START].isEnabled()
    dialog.pause_plan()
    _until(qapp, lambda: dialog.execution().status is PlanExecutionStatus.PAUSED)
    assert window.download_channel_busy()
    count = len(window.task_service.list_tasks())
    window._start_download()
    assert len(window.task_service.list_tasks()) == count
    dialog._do_cancel_plan()
    _until(qapp, lambda: dialog._run_worker is None)
    assert not window.download_channel_busy()
