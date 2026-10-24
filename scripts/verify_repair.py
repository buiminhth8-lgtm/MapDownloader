"""离线验收：诊断损坏、普通续传、定向修复、坏源失败与再次修复。"""
from __future__ import annotations

import argparse
import asyncio
import io
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from PIL import Image  # noqa: E402

from app.downloader import RateLimiter, ResumePolicy, RetryPolicy, iter_tile_coordinates  # noqa: E402
from app.geo import GeoBounds, xyz_y_to_tms_y  # noqa: E402
from app.mbtiles import MBTilesReader  # noqa: E402
from app.models import DownloadTaskStatus, MapLayerType  # noqa: E402
from app.providers import PNG_SIGNATURE, TiandituProvider  # noqa: E402
from app.services import MapDownloadService, MBTilesInspectionService  # noqa: E402
from app.tasks import TaskService, TaskStore  # noqa: E402

BOUNDS = GeoBounds(116.0, 39.0, 116.1, 39.1)
ZOOM = 12
BROKEN_PNG = PNG_SIGNATURE + b"broken-payload"


def png():
    stream = io.BytesIO()
    Image.new("RGB", (32, 32), (30, 90, 120)).save(stream, format="PNG")
    return stream.getvalue()


class TileServer:
    def __init__(self):
        self.good = png()
        self.requests = []
        self.bad_keys = set()
        self.fail_keys = set()

    def __call__(self, request):
        params = request.url.params
        key = tuple(int(params[name]) for name in ("TILEMATRIX", "TILECOL", "TILEROW"))
        self.requests.append(key)
        if key in self.fail_keys:
            return httpx.Response(503)
        content = BROKEN_PNG if key in self.bad_keys else self.good
        return httpx.Response(200, content=content, headers={"content-type": "image/png"})


def service_for(server, **kwargs):
    return MapDownloadService(
        TiandituProvider("fake-repair-acceptance-token"),
        concurrency=2,
        transport=httpx.MockTransport(server),
        retry_policy=RetryPolicy(max_attempts=1),
        rate_limiter=RateLimiter.disabled(),
        **kwargs,
    )


def overwrite_tile(path, key, content):
    z, x, y = key
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE tiles SET tile_data=? WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            (content, z, x, xyz_y_to_tms_y(z, y)),
        )


async def seed(work_dir, server=None):
    server = server or TileServer()
    tasks = TaskService(TaskStore(work_dir / "tasks.json"))
    task = tasks.create_task(
        name="repair-acceptance", bounds=BOUNDS, min_zoom=ZOOM, max_zoom=ZOOM,
        map_layer=MapLayerType.VECTOR, output_path=work_dir / "map.mbtiles",
    )
    service = service_for(server)
    outcome = await service.resume_task(task, task_service=tasks)
    assert outcome.is_success, outcome.failure_text
    server.requests.clear()
    coordinates = [tile.as_tuple for tile in iter_tile_coordinates(BOUNDS, ZOOM, ZOOM)]
    return tasks, task, server, service, coordinates


async def run_checks(work_dir):
    tasks, task, server, service, coordinates = await seed(work_dir)
    damaged = set(coordinates[:2])
    for key in damaged:
        overwrite_tile(task.output_path, key, BROKEN_PNG)
    before = MBTilesInspectionService().inspect(task.output_path, deep_scan=True)
    assert before.corrupt_tiles == 2 and not before.valid
    print("[PASS] 全量检查发现 2 张文件头正确但正文损坏的瓦片")

    fast = await service.resume_task(task, task_service=tasks)
    assert not server.requests and not fast.is_success
    print("[PASS] 普通续传保留快速跳过行为；无法解码时不误报完成")

    repaired = await service.resume_task(task, task_service=tasks, resume_policy=ResumePolicy.repair())
    assert repaired.is_success, repaired.failure_text
    assert set(server.requests) == damaged and len(server.requests) == 2
    assert repaired.invalid_existing_tiles == repaired.repaired_tiles == 2
    assert repaired.diagnostics.deep_scan_complete and repaired.diagnostics.corrupt_tiles == 0
    assert tasks.get_task(task.id).status is DownloadTaskStatus.COMPLETED
    print("[PASS] 只请求 2 张损坏瓦片，修复后全量复检通过")

    server.requests.clear()
    repeat = await service.resume_task(task, task_service=tasks, resume_policy=ResumePolicy.repair())
    assert repeat.is_success and not server.requests and repeat.repaired_tiles == 0
    print("[PASS] 再次修复 0 请求，不重下健康瓦片")

    bad_key = coordinates[0]
    overwrite_tile(task.output_path, bad_key, BROKEN_PNG)
    server.bad_keys.add(bad_key)
    server.requests.clear()
    failed = await service.resume_task(task, task_service=tasks, resume_policy=ResumePolicy.repair())
    assert not failed.is_success and failed.repaired_tiles == 0 and failed.summary.failed == 1
    with MBTilesReader(task.output_path) as reader:
        assert reader.read_tile(*bad_key) == BROKEN_PNG
    assert tasks.get_task(task.id).status is DownloadTaskStatus.FAILED
    print("[PASS] 源站继续返回坏图片时修复失败，不伪报覆盖成功")

    server.bad_keys.clear()
    server.requests.clear()
    retry = await service.resume_task(task, task_service=tasks, resume_policy=ResumePolicy.repair())
    assert retry.is_success and server.requests == [bad_key] and retry.repaired_tiles == 1
    print("[PASS] 再次修复仅请求遗留坏瓦片，任务恢复为已完成")
    return retry


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="保留独立临时目录中的验收产物")
    args = parser.parse_args(argv)
    work_dir = Path(tempfile.mkdtemp(prefix="repair-acceptance-"))
    try:
        asyncio.run(run_checks(work_dir))
    except Exception as exc:
        print(f"[FAIL] 修复验收失败：{exc}", file=sys.stderr)
        return 1
    finally:
        if args.keep:
            print(f"验收产物：{work_dir}")
        else:
            shutil.rmtree(work_dir)
    print("损坏瓦片闭环验收全部通过（离线 Mock 服务端）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
