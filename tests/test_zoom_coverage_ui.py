from __future__ import annotations

from app.geo import region_from_task
from app.planning.storage_budget import BudgetSettings
from tests.test_storage_budget_ui import deterministic_disk, until, window  # noqa: F401


def prepare_plan(window, work_dir):
    plan = window.plan_service.plan
    plan.output_dir = str(work_dir)
    plan.name = 'coverage-ui'
    window.plan_service.add_region('rivertown')
    window.plan_service.set_zoom('rivertown', 10, 10)
    window._on_plan_open_requested()
    assert window._plan_dialog.wait_for_estimate()
    return plan


def test_plan_has_real_single_package_button(window, work_dir):
    prepare_plan(window, work_dir)
    button = window._plan_dialog.coverage_download_button
    assert button.isEnabled() and '单包' in button.text()


def test_coverage_preview_before_task_creation_or_network(window, qapp, work_dir, monkeypatch):
    prepare_plan(window, work_dir)
    reached = []
    monkeypatch.setattr(window, '_continue_plan_after_budget', reached.append)
    window._plan_dialog.coverage_download_button.click()
    dialog = window._storage_budget_dialog
    assert dialog is not None, window.log_panel.to_text()
    until(qapp, lambda current=dialog: current.worker is None)
    assert dialog.guarding and dialog.report is not None and dialog.continue_button.isEnabled()
    assert reached == [] and window.task_service.list_tasks() == []
    assert not list(work_dir.rglob('*.mbtiles')) and not list(work_dir.rglob('.coverage'))
    assert not dialog.apply_button.isEnabled()  # 单包整体裁剪需返回计划逐区域调整。
    dialog.continue_button.click()
    assert len(reached) == 1
    prepared = reached[0]
    assert prepared.budget_checked and prepared.region.region_type.value == 'coverage'
    assert region_from_task(prepared.task).boundary_version == prepared.region.boundary_version
    assert prepared.total == sum(z.total for z in dialog.report.items[0].zooms)
    assert 'coverage_' in prepared.output_path.name and '_vec' in prepared.output_path.name


def test_cancel_preview_creates_no_task_or_files(window, qapp, work_dir):
    prepare_plan(window, work_dir)
    window.start_plan_coverage()
    dialog = window._storage_budget_dialog
    assert dialog is not None, window.log_panel.to_text()
    until(qapp, lambda current=dialog: current.worker is None)
    dialog.reject()
    assert window.task_service.list_tasks() == []
    assert not list(work_dir.rglob('*.mbtiles')) and not list(work_dir.rglob('.coverage'))
    assert not window.download_channel_busy()


def test_plan_change_invalidates_preview(window, qapp, work_dir, monkeypatch):
    prepare_plan(window, work_dir)
    reached = []
    monkeypatch.setattr(window, '_continue_plan_after_budget', reached.append)
    window.start_plan_coverage()
    dialog = window._storage_budget_dialog
    assert dialog is not None, window.log_panel.to_text()
    until(qapp, lambda current=dialog: current.worker is None)
    window.plan_service.set_zoom('rivertown', 10, 11)
    dialog.continue_button.click()
    assert reached == [] and not window.task_service.list_tasks()
    assert '配置已变化' in window.log_panel.to_text()


def test_single_download_is_mutex_with_coverage_preview(window, qapp, work_dir):
    prepare_plan(window, work_dir)
    window.start_plan_coverage()
    dialog = window._storage_budget_dialog
    assert dialog is not None, window.log_panel.to_text()
    until(qapp, lambda current=dialog: current.worker is None)
    window._start_download()
    assert window._download_worker is None and not window.task_service.list_tasks()
    window._plan_dialog.start_plan()
    assert not window._plan_dialog.is_running_plan()


def test_plan_mode_changes_output_filename(window, qapp, work_dir, monkeypatch):
    plan = prepare_plan(window, work_dir)
    reached = []
    monkeypatch.setattr(window, '_continue_plan_after_budget', reached.append)
    paths = []
    for layer in ('vec_w', 'img_w'):
        plan.map_layer = layer
        window.start_plan_coverage()
        dialog = window._storage_budget_dialog
        assert dialog is not None, window.log_panel.to_text()
        until(qapp, lambda current=dialog: current.worker is None)
        paths.append(dialog.report.items[0].request.output_path)
        dialog.reject()
    assert paths[0] != paths[1] and '_vec' in paths[0].name and '_img' in paths[1].name


def test_preview_works_when_default_auto_budget_is_disabled(window, qapp, work_dir):
    prepare_plan(window, work_dir)
    window.preferences.set_storage_budget(BudgetSettings(enabled=False))
    window.start_plan_coverage()
    assert window._storage_budget_dialog is not None, window.log_panel.to_text()
    until(qapp, lambda: window._storage_budget_dialog.worker is None)
    assert window._storage_budget_dialog.guarding


def test_empty_plan_does_not_create_budget_or_task(window):
    window.start_plan_coverage()
    assert window._storage_budget_dialog is None
    assert not window.task_service.list_tasks()


def test_coverage_download_uses_actual_worker_and_can_resume(window, qapp, work_dir, monkeypatch):
    import httpx

    from app.models import DownloadTaskStatus
    from tests.test_ui_smoke import FAKE_PNG, _stub_download_service
    prepare_plan(window, work_dir)
    requests = []
    def server(request):
        requests.append(request)
        return httpx.Response(200, content=FAKE_PNG)
    _stub_download_service(monkeypatch, server)
    window.start_plan_coverage()
    dialog = window._storage_budget_dialog
    assert dialog is not None, window.log_panel.to_text()
    until(qapp, lambda current=dialog: current.worker is None)
    dialog.continue_button.click()
    task = window._current_task
    until(qapp, lambda: window._download_worker is None, timeout=10)
    assert window.task_service.get_task(task.id).status is DownloadTaskStatus.COMPLETED
    assert len(list(work_dir.rglob('*.mbtiles'))) == 1
    count = len(requests)
    window._on_task_resume_requested(task.id)
    until(qapp, lambda: window._scan_worker is None and not window.download_channel_busy(), timeout=10)
    assert len(requests) == count
    assert window.task_service.get_task(task.id).status is DownloadTaskStatus.COMPLETED


def test_double_click_sparse_task_does_not_turn_it_into_rectangle(window, qapp, work_dir, monkeypatch):
    prepare_plan(window, work_dir)
    reached = []
    monkeypatch.setattr(window, '_continue_plan_after_budget', reached.append)
    window.start_plan_coverage()
    assert window._storage_budget_dialog is not None, window.log_panel.to_text()
    until(qapp, lambda: window._storage_budget_dialog.worker is None)
    window._storage_budget_dialog.continue_button.click()
    task = reached[0].task
    before = window.download_panel.get_form_data()
    window._on_task_activated(task.id)
    assert window.download_panel.get_form_data() == before
    assert '保存了独立策略' in window.log_panel.to_text()
