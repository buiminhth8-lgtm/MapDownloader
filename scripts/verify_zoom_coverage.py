"""Slice 36 offline acceptance: one sparse MBTiles, resume, repair and portable package."""
from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.verify_repair import BROKEN_PNG, TileServer, overwrite_tile, service_for  # noqa: E402

from app.downloader import ResumePolicy  # noqa: E402
from app.geo import (  # noqa: E402
    DownloadRegion,
    GeoBounds,
    RegionTileEstimator,
    RegionType,
    build_region_filter,
)
from app.geo.zoom_coverage import (  # noqa: E402
    CoverageRule,
    build_coverage,
    coverage_zoom_range,
    save_coverage_snapshot,
)
from app.mbtiles import MBTilesReader  # noqa: E402
from app.models import MapType  # noqa: E402
from app.services.map_package_service import MapPackageService  # noqa: E402
from app.services.mbtiles_inspection_service import MBTilesInspectionService  # noqa: E402
from app.tasks import TaskService, TaskStore  # noqa: E402

#: 验收脚本自己的边界文件目录（每次跑都重写，跨进程不共享状态）
_REGION_DIR = Path(tempfile.mkdtemp(prefix='zoom-coverage-verify-'))


def _rect_region(name: str, bounds: GeoBounds, region_id: str) -> DownloadRegion:
    """按外包矩形造一块行政区域（现场写一份矩形 GeoJSON）。

    自定义矩形能力（``DownloadRegion.rectangle``）已随界面入口一起删除，而本
    脚本要的只是"一块不裁掉任何瓦片的范围"——写成多边形是等价做法，而且走的
    正是生产那条 ``PolygonTileRegionFilter`` 链路，验收更有意义。
    """
    _REGION_DIR.mkdir(parents=True, exist_ok=True)
    path = _REGION_DIR / f'{region_id}.geojson'
    path.write_text(json.dumps({
        'type': 'Feature',
        'properties': {'name': name, 'id': region_id, 'boundaryVersion': '2026.09-verify'},
        'geometry': {
            'type': 'Polygon',
            'coordinates': [[
                [bounds.west, bounds.south], [bounds.east, bounds.south],
                [bounds.east, bounds.north], [bounds.west, bounds.north],
                [bounds.west, bounds.south],
            ]],
        },
    }, ensure_ascii=False), encoding='utf-8')
    return DownloadRegion(
        region_type=RegionType.POLYGON,
        name=name,
        bounds=bounds,
        region_id=region_id,
        boundary_version='2026.09-verify',
        geojson_path=str(path),
    )


def make_strategy():
    overview = _rect_region('overview', GeoBounds(116.0, 39.5, 116.8, 40.1), 'overview')
    mission = _rect_region('mission', GeoBounds(116.2, 39.7, 116.3, 39.8), 'mission')
    overlap = _rect_region('mission-overlap', GeoBounds(116.25, 39.75, 116.35, 39.85), 'overlap')
    return build_coverage('多级覆盖验收', (CoverageRule(overview, 8, 9), CoverageRule(mission, 11, 12),
                                          CoverageRule(overlap, 12, 12)))


async def seed(work_dir, *, mode=MapType.VECTOR, labels=True, server=None):
    path = work_dir / f'coverage-{mode.value}-{labels}.mbtiles'
    region = save_coverage_snapshot(make_strategy(), path)
    low, high = coverage_zoom_range(region)
    estimate = RegionTileEstimator(region).estimate(low, high)
    tasks = TaskService(TaskStore(work_dir / 'tasks.json'))
    task = tasks.create_task(name=region.name, bounds=region.bounds, min_zoom=low, max_zoom=high,
                             map_layer=mode.base_layer, with_labels=labels, output_path=path,
                             total_tiles=estimate.selected_tiles, region=region)
    server = server or TileServer()
    stream = io.BytesIO()
    Image.new('RGB', (256, 256), (30, 90, 120)).save(stream, format='PNG')
    server.good = stream.getvalue()
    service = service_for(server)
    outcome = await service.resume_task(task, task_service=tasks)
    assert outcome.is_success, outcome.failure_text
    return tasks, task, server, service, outcome


async def run_checks(work_dir):
    region = make_strategy()
    selected = build_region_filter(region)
    low, high = coverage_zoom_range(region)
    expected = {(z, x, y) for z in range(low, high + 1)
                for x, y in selected.iter_selected_tiles(region.bounds, z)}
    tasks, task, server, service, outcome = await seed(work_dir)
    assert outcome.summary.total == len(expected)
    assert set(server.requests) == expected and len(server.requests) == 2 * len(expected)
    with MBTilesReader(task.output_path) as reader:
        assert set(reader.iter_tile_coordinates()) == expected
    print('[PASS] 低 Zoom 概览 + 高 Zoom 任务区写入同一个 MBTiles，重叠坐标只合成下载一次')
    assert not any(z == 10 for z, _, _ in expected)
    assert outcome.diagnostics.coverage_checked and outcome.diagnostics.missing_tiles == 0
    print('[PASS] 未配置的中间 Zoom 与高清区域外空白不误报为缺失')

    server.requests.clear()
    again = await service.resume_task(tasks.get_task(task.id), task_service=tasks)
    assert again.is_success and not server.requests
    print('[PASS] 任务重载后续传只跳过已有策略内瓦片，零额外请求')
    damaged = next(iter(expected))
    overwrite_tile(task.output_path, damaged, BROKEN_PNG)
    repaired = await service.resume_task(task, task_service=tasks, resume_policy=ResumePolicy.repair())
    assert repaired.is_success and repaired.repaired_tiles == 1
    assert server.requests == [damaged, damaged]
    print('[PASS] 损坏修复仅请求损坏坐标对应的底图和注记，策略保持一致')

    task_snapshot = Path(task.coverage_path)
    task_snapshot.unlink()
    server.requests.clear()
    assert (await service.resume_task(task, task_service=tasks)).is_success and not server.requests
    print('[PASS] 本地策略侧文件缺失时从 MBTiles 内置快照恢复，无需原始边界路径')

    packages = MapPackageService()
    built = packages.build(task.output_path, work_dir / 'packages', package_name='coverage_acceptance')
    assert built.success, built
    checked = packages.validate(built.package_dir)
    assert checked.valid, checked.errors
    manifest_path = Path(built.package_dir) / 'manifest.json'
    payload = json.loads(manifest_path.read_text(encoding='utf-8'))
    assert len(payload['region']['coverage']['rules']) == 3
    assert payload['region']['type'] == 'coverage'
    assert not any('geometry' in rule for rule in payload['region']['coverage']['rules'])
    print('[PASS] 地图包 manifest 携带分区与 Zoom 摘要，MBTiles 自带完整快照，打包校验通过')
    payload['region']['coverage']['rules'][0]['maxZoom'] = 18
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    assert not packages.validate(built.package_dir).valid
    print('[PASS] manifest 覆盖规则被篡改时拒绝通过校验')
    with closing(sqlite3.connect(task.output_path)) as db, db:
        db.execute("UPDATE metadata SET value='corrupt' WHERE name='coverage_data'")
    result = MBTilesInspectionService().inspect(task.output_path)
    assert not result.valid and not result.coverage_checked
    print('[PASS] 策略损坏时不退化为矩形下载或误报覆盖完整')
    print('Slice 36 单包多级覆盖离线验收全部通过')


def main():
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    with tempfile.TemporaryDirectory(prefix='coverage-acceptance-') as directory:
        asyncio.run(run_checks(Path(directory)))


if __name__ == '__main__':
    main()
