"""损坏瓦片修复的真 SQLite / Mock HTTP 回归。"""
from __future__ import annotations

import io
import sqlite3
import struct
import zlib
from pathlib import Path

import pytest
from PIL import Image
from scripts.verify_repair import BROKEN_PNG, overwrite_tile, run_checks, seed, service_for

from app.downloader import ResumePolicy
from app.geo import xyz_y_to_tms_y
from app.mbtiles import MBTilesReader
from app.mbtiles.diagnostics import probe_image
from app.models import DownloadTaskStatus
from app.providers import PNG_SIGNATURE
from app.providers.image_validation import is_decodable_tile
from app.services.map_download_service import MapResumeError


async def test_offline_acceptance_runs_the_whole_repair_loop(work_dir):
    outcome = await run_checks(work_dir)
    assert outcome.is_success and outcome.diagnostics.deep_scan_complete


async def test_repair_only_requests_corrupt_and_missing_tiles(work_dir):
    tasks, task, server, service, coordinates = await seed(work_dir)
    damaged = set(coordinates[:3])
    for key, payload in zip(coordinates[:3], (BROKEN_PNG, b"", b"<html>error</html>")):
        overwrite_tile(task.output_path, key, payload)
    missing = coordinates[-1]
    z, x, y = missing
    with sqlite3.connect(task.output_path) as connection:
        connection.execute("DELETE FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                           (z, x, xyz_y_to_tms_y(z, y)))
    outcome = await service.resume_task(task, task_service=tasks, resume_policy=ResumePolicy.repair())
    assert outcome.is_success
    assert set(server.requests) == damaged | {missing}
    assert len(server.requests) == 4
    assert outcome.invalid_existing_tiles == outcome.repaired_tiles == 3
    assert outcome.summary.skipped == len(coordinates) - 4
    assert outcome.diagnostics.deep_scanned_tiles == len(coordinates)


async def test_partial_repair_does_not_claim_failed_tiles_were_repaired(work_dir):
    tasks, task, server, service, coordinates = await seed(work_dir)
    for key in coordinates[:2]:
        overwrite_tile(task.output_path, key, BROKEN_PNG)
    server.fail_keys.add(coordinates[1])
    outcome = await service.resume_task(task, task_service=tasks, resume_policy=ResumePolicy.repair())
    assert not outcome.is_success
    assert outcome.invalid_existing_tiles == 2 and outcome.repaired_tiles == 1
    assert outcome.summary.failed == 1 and outcome.diagnostics.corrupt_tiles == 1
    assert "发现但未修复 1 张" in "\n".join(outcome.extra_lines)
    server.fail_keys.clear()
    server.requests.clear()
    retry = await service.resume_task(task, task_service=tasks, resume_policy=ResumePolicy.repair())
    assert retry.is_success and server.requests == [coordinates[1]]


async def test_cancel_during_existing_validation_and_retry(work_dir):
    tasks, task, server, service, coordinates = await seed(work_dir)
    service = service_for(server, progress_interval_seconds=0.0)
    damaged = coordinates[-1]
    overwrite_tile(task.output_path, damaged, BROKEN_PNG)
    def cancel(progress):
        if progress.skipped >= 1:
            service.cancel()
    outcome = await service.resume_task(task, task_service=tasks, resume_policy=ResumePolicy.repair(), on_progress=cancel)
    assert outcome.cancelled and not outcome.is_success
    assert tasks.get_task(task.id).status is DownloadTaskStatus.CANCELLED
    with MBTilesReader(task.output_path) as reader:
        assert reader.get_tile_count() == len(coordinates)
    server.requests.clear()
    retry = await service.resume_task(task, task_service=tasks, resume_policy=ResumePolicy.repair())
    assert retry.is_success and server.requests == [damaged]


async def test_cancel_during_final_inspection_cannot_be_completed(work_dir):
    tasks, task, server, _, _ = await seed(work_dir)
    service = None
    def cancel_on_inspection(_message):
        service.cancel()
    service = service_for(server, validation_progress=cancel_on_inspection)
    outcome = await service.resume_task(task, task_service=tasks, resume_policy=ResumePolicy.repair())
    assert outcome.cancelled and not outcome.is_success
    assert not outcome.diagnostics.deep_scan_complete
    assert not outcome.diagnostics.valid
    assert tasks.get_task(task.id).status is DownloadTaskStatus.CANCELLED


async def test_incomplete_final_inspection_cannot_report_success(work_dir, monkeypatch):
    tasks, task, _, service, _ = await seed(work_dir)
    original = service._inspection.inspect
    def incomplete(*args, **kwargs):
        result = original(*args, **kwargs)
        result.deep_scan_complete = False
        return result
    monkeypatch.setattr(service._inspection, "inspect", incomplete)
    outcome = await service.resume_task(task, task_service=tasks, resume_policy=ResumePolicy.repair())
    assert not outcome.is_success and not outcome.validation.valid
    assert "全量复检尚未完成" in outcome.failure_text
    assert tasks.get_task(task.id).status is DownloadTaskStatus.FAILED


async def test_missing_decoder_stops_before_writes_or_http(work_dir, monkeypatch):
    import app.providers.image_validation as validation
    tasks, task, server, service, _ = await seed(work_dir)
    before = Path(task.output_path).read_bytes()
    def missing():
        raise ImportError("Pillow missing")
    monkeypatch.setattr(validation, "require_image_decoder", missing)
    with pytest.raises(MapResumeError, match="Pillow"):
        await service.resume_task(task, task_service=tasks, resume_policy=ResumePolicy.repair())
    assert not server.requests
    assert Path(task.output_path).read_bytes() == before
    assert tasks.get_task(task.id).status is DownloadTaskStatus.FAILED


def test_full_decode_catches_truncated_jpeg_that_verify_accepts():
    stream = io.BytesIO()
    Image.new("RGB", (32, 32), (20, 50, 90)).save(stream, format="JPEG")
    damaged = stream.getvalue()[:-2]
    with Image.open(io.BytesIO(damaged)) as image:
        image.verify()
    assert not is_decodable_tile(damaged)
    assert not probe_image(damaged).ok


def test_full_decode_catches_png_with_valid_crc_but_bad_compressed_pixels():
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    damaged = PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0))
    damaged += chunk(b"IDAT", b"not-a-zlib-stream") + chunk(b"IEND", b"")
    with Image.open(io.BytesIO(damaged)) as image:
        image.verify()
    assert not is_decodable_tile(damaged)
    assert not probe_image(damaged).ok


async def test_extra_corrupt_tile_outside_task_scope_is_not_downloaded(work_dir):
    from contextlib import closing
    tasks, task, server, service, _ = await seed(work_dir)
    with closing(sqlite3.connect(task.output_path)) as connection, connection:
        connection.execute(
            "INSERT INTO tiles (zoom_level,tile_column,tile_row,tile_data) VALUES (12,0,0,?)",
            (BROKEN_PNG,),
        )
    outcome = await service.resume_task(task, task_service=tasks, resume_policy=ResumePolicy.repair())
    assert not server.requests
    assert not outcome.is_success and outcome.diagnostics.corrupt_tiles == 1
    assert outcome.diagnostics.deep_scan_complete
