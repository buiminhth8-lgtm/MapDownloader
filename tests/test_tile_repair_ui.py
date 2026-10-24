"""修复入口、完成任务再校验、运行互斥和检查结果刷新。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.geo import GeoBounds
from app.mbtiles.diagnostics import MBTilesDiagnosticsResult
from app.models import DownloadTaskStatus, MapLayerType
from tests.test_download_coordination import window  # noqa: F401


def _task(window, work_dir):
    path = work_dir / "repair.mbtiles"
    path.write_bytes(b"placeholder")
    task = window.task_service.create_task(
        name="repair", bounds=GeoBounds(116, 39, 116.1, 39.1), min_zoom=12, max_zoom=12,
        map_layer=MapLayerType.VECTOR, output_path=path, total_tiles=6,
    )
    window.task_service.set_status(task, DownloadTaskStatus.COMPLETED)
    window.download_panel._token_edit.setText("fake-repair-token")
    return task, path


def test_inspector_button_routes_to_repair_even_for_completed_task(window, work_dir, monkeypatch):
    task, path = _task(window, work_dir)
    plans = []
    monkeypatch.setattr(window, "_begin_scan", lambda plan, *args, **kwargs: plans.append(plan))
    window.open_mbtiles_inspector(str(path))
    window._inspector_dialog.buttons["repair"].click()
    assert len(plans) == 1
    assert plans[0].resume_task.id == task.id
    assert plans[0].resume_policy.verify_existing_tile
    assert plans[0].resume_policy.redownload_invalid_tile


def test_repair_requires_token_and_does_not_start_worker(window, work_dir):
    _, path = _task(window, work_dir)
    window.download_panel._token_edit.clear()
    window._on_repair_requested(str(path))
    assert window._download_worker is None and window._scan_worker is None


def test_unknown_file_is_rejected_without_creating_task(window, work_dir):
    window._on_repair_requested(str(work_dir / "unknown.mbtiles"))
    assert not window.task_service.list_tasks()
    assert "未找到" in window.log_panel.to_text()


def test_plan_execution_blocks_repair_entry(window, work_dir, monkeypatch):
    task, path = _task(window, work_dir)
    monkeypatch.setattr(window, "download_channel_busy", lambda **kwargs: True)
    calls = []
    monkeypatch.setattr(window, "_on_task_resume_requested", lambda *args, **kwargs: calls.append(args))
    window._on_repair_requested(str(path))
    assert not calls
    assert window.task_service.get_task(task.id).status is DownloadTaskStatus.COMPLETED


def test_corruption_failure_dialog_binds_repair_to_failed_file(window, work_dir, monkeypatch):
    path = work_dir / "failed.mbtiles"
    calls = []
    monkeypatch.setattr(window, "_on_repair_requested", calls.append)
    outcome = SimpleNamespace(
        summary=SimpleNamespace(succeeded=5, failed=0),
        diagnostics=SimpleNamespace(corrupt_tiles=1, empty_tiles=0), output_path=path,
    )
    window._show_failed_dialog(outcome, "瓦片无法解码")
    button = next(button for button in window._failed_dialog.buttons() if button.text() == "校验并修复")
    button.click()
    assert calls == [str(path)]


def test_repair_clears_old_inspector_result_and_refreshes_after_completion(window, work_dir):
    _, path = _task(window, work_dir)
    window.open_mbtiles_inspector(str(path))
    dialog = window._inspector_dialog
    dialog.result = MBTilesDiagnosticsResult(path=str(path), valid=False, corrupt_tiles=1)
    dialog.set_repair_running()
    assert dialog.result is None and dialog.is_running
    assert not dialog.buttons["quick"].isEnabled() and not dialog.buttons["repair"].isEnabled()
    window._repair_view_path = Path(path)
    result = MBTilesDiagnosticsResult(path=str(path), valid=True, tile_count=6, deep_scan=True,
                                     deep_scan_complete=True, deep_scanned_tiles=6)
    window._finish_repair_view(SimpleNamespace(diagnostics=result))
    assert dialog.result is result and not dialog.is_running
    assert dialog.buttons["repair"].isEnabled()


def test_repair_exception_cannot_leave_old_pass_in_inspector(window, work_dir):
    _, path = _task(window, work_dir)
    window.open_mbtiles_inspector(str(path))
    dialog = window._inspector_dialog
    dialog.result = MBTilesDiagnosticsResult(path=str(path), valid=True)
    dialog.set_repair_running()
    window._repair_view_path = path
    window._finish_repair_view()
    assert dialog.result is None and not dialog.is_running
    assert "修复未完成" in dialog.progress_label.text()


async def test_inspector_repair_runs_download_and_refreshes_real_diagnostics(window, work_dir, monkeypatch, qapp):
    from scripts.verify_repair import BROKEN_PNG, overwrite_tile, seed

    from app.downloader import RateLimiter, RetryPolicy
    from app.ui import download_worker
    from tests.test_download_coordination import _until

    tasks, task, server, _, coordinates = await seed(work_dir)
    window.task_service = tasks
    window.download_panel._token_edit.setText("fake-ui-repair-token")
    overwrite_tile(task.output_path, coordinates[0], BROKEN_PNG)
    original = download_worker.MapDownloadService
    def make_service(provider, **kwargs):
        import httpx
        kwargs.update(transport=httpx.MockTransport(server), rate_limiter=RateLimiter.disabled(),
                      retry_policy=RetryPolicy(max_attempts=1))
        return original(provider, **kwargs)
    monkeypatch.setattr(download_worker, "MapDownloadService", make_service)
    monkeypatch.setattr(window, "_show_complete_dialog", lambda outcome: None)
    window.open_mbtiles_inspector(str(task.output_path))
    dialog = window._inspector_dialog
    dialog.buttons["repair"].click()
    _until(qapp, lambda: dialog.result is not None and dialog.result.deep_scan_complete)
    assert dialog.result.valid and dialog.result.corrupt_tiles == 0
    assert server.requests == [coordinates[0]]
    assert not window.download_channel_busy()
    assert not dialog.is_running and dialog.buttons["repair"].isEnabled()
    assert window.task_service.get_task(task.id).status is DownloadTaskStatus.COMPLETED


def test_failed_missing_tile_repair_keeps_strict_repair_action(window, work_dir):
    outcome = SimpleNamespace(
        summary=SimpleNamespace(succeeded=0, failed=1), repair_mode=True,
        diagnostics=SimpleNamespace(corrupt_tiles=0, empty_tiles=0),
        output_path=work_dir / "missing.mbtiles",
    )
    window._show_failed_dialog(outcome, "下载的图片无法解码，缺失瓦片未补齐")
    assert "校验并修复" in [button.text() for button in window._failed_dialog.buttons()]
    assert "继续补齐" not in [button.text() for button in window._failed_dialog.buttons()]
