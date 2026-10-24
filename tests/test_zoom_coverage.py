from __future__ import annotations

import base64
import json
import sqlite3
import zlib
from dataclasses import replace
from pathlib import Path

import pytest
from shapely.geometry import Polygon, mapping

from app.downloader import ResumePolicy
from app.geo import DownloadRegion, GeoBounds, RegionTileEstimator, RegionType, build_region_filter, region_from_task
from app.geo.region_estimator import RegionEstimateCancelled
from app.geo.zoom_coverage import (
    CoverageRule,
    build_coverage,
    coverage_region,
    coverage_summary,
    coverage_zoom_range,
    decode_coverage,
    save_coverage_snapshot,
)
from app.models import MapType
from app.planning.storage_budget import BudgetInput, BudgetSettings, assess_budget
from app.services.map_download_service import MapResumeError
from app.tasks import TaskService, TaskStore
from scripts.verify_repair import TileServer, service_for
from scripts.verify_zoom_coverage import make_strategy, run_checks, seed


def rectangle(name='a', bounds=(116.2, 39.7, 116.3, 39.8)):
    return DownloadRegion.rectangle(name, GeoBounds(*bounds))


def tiles(region, low, high):
    f = build_region_filter(region)
    return {(z, x, y) for z in range(low, high + 1) for x, y in f.iter_selected_tiles(region.bounds, z)}


def test_overlapping_rules_are_union_not_sum():
    a, b = rectangle(), rectangle('b', (116.25, 39.75, 116.35, 39.85))
    one = build_coverage('one', (CoverageRule(a, 11, 12),))
    two = build_coverage('two', (CoverageRule(b, 12, 12),))
    combined = build_coverage('combined', (CoverageRule(a, 11, 12), CoverageRule(b, 12, 12)))
    expected = tiles(one, 11, 12) | tiles(two, 11, 12)
    assert tiles(combined, 11, 12) == expected
    estimate = RegionTileEstimator(combined).estimate(11, 12)
    assert estimate.selected_tiles == len(expected) < len(tiles(one, 11, 12)) + len(tiles(two, 11, 12))


def test_identical_rules_do_not_duplicate_tiles():
    r = CoverageRule(rectangle(), 12, 12)
    single, duplicate = build_coverage('same', (r,)), build_coverage('same', (r, r))
    assert RegionTileEstimator(single).estimate(12, 12).selected_tiles == RegionTileEstimator(duplicate).estimate(12, 12).selected_tiles


def test_high_zoom_never_scans_overview_bbox():
    overview = rectangle('overview', (73, 3, 135, 54))
    local = rectangle('local', (116.2, 39.7, 116.2001, 39.7001))
    region = build_coverage('sparse', (CoverageRule(overview, 5, 5), CoverageRule(local, 18, 18)))
    f = build_region_filter(region)
    assert f.candidate_count(18) <= 4
    assert f.candidate_count(6) == 0
    estimate = RegionTileEstimator(region).estimate(5, 18)
    assert all(s.selected_tiles == 0 for s in estimate.per_zoom if 6 <= s.zoom <= 17)
    assert estimate.per_zoom[-1].candidate_tiles <= 4


def test_separated_missions_only_enumerate_their_candidate_union():
    regions = [rectangle('west', (75, 40, 75.0001, 40.0001)), rectangle('east', (130, 40, 130.0001, 40.0001))]
    combined = build_coverage('far', [CoverageRule(r, 18, 18) for r in regions])
    f = build_region_filter(combined)
    assert f.candidate_count(18) <= 8
    assert len(list(f.iter_candidate_tiles(combined.bounds, 18))) == f.candidate_count(18)


def test_polygon_holes_use_existing_intersection_semantics(work_dir):
    poly = Polygon([(116,39),(118,39),(118,41),(116,41)], holes=[[(116.4,39.4),(117.6,39.4),(117.6,40.6),(116.4,40.6)]])
    path = work_dir/'hole.geojson'
    path.write_text(json.dumps(mapping(poly)), encoding='utf-8')
    region = DownloadRegion(RegionType.POLYGON, 'hole', GeoBounds(*poly.bounds), geojson_path=str(path))
    covered = build_coverage('hole-coverage', (CoverageRule(region, 12, 12),))
    original_filter = build_region_filter(region)
    expected = {(12,x,y) for x,y in original_filter.iter_selected_tiles(region.bounds, 12)}
    assert tiles(covered, 12, 12) == expected


def test_snapshot_freezes_boundary_geometry(work_dir):
    p = work_dir/'polygon.geojson'
    p.write_text(json.dumps(mapping(Polygon([(116,39),(117,39),(117,40),(116,40)]))), encoding='utf-8')
    original = DownloadRegion(RegionType.POLYGON, 'frozen', GeoBounds(116,39,117,40), geojson_path=str(p))
    region = build_coverage('frozen', [CoverageRule(original, 10, 10)])
    expected = tiles(region,10,10)
    p.unlink()
    restored = coverage_region(region.coverage_data, expected_fingerprint=region.boundary_version)
    assert tiles(restored,10,10) == expected
    assert str(work_dir) not in json.dumps(decode_coverage(region.coverage_data))


def test_snapshot_roundtrip_identity_summary_and_range():
    region = make_strategy()
    assert make_strategy() == region
    assert coverage_region(region.coverage_data) == region
    assert coverage_zoom_range(region) == (8,12)
    summary = coverage_summary(region)
    assert len(summary['rules']) == 3 and summary['fingerprint'] == region.boundary_version
    assert all('geometry' not in rule and 'bounds' in rule for rule in summary['rules'])


@pytest.mark.parametrize('data', ['', 'broken', base64.b64encode(b'not-zlib').decode(), base64.b64encode(zlib.compress(b'{}')).decode()])
def test_invalid_snapshot_fails_closed(data):
    with pytest.raises(ValueError):
        coverage_region(data)


def test_wrong_fingerprint_fails_closed():
    with pytest.raises(ValueError, match='指纹'):
        coverage_region(make_strategy().coverage_data, expected_fingerprint='0'*64)


def test_invalid_rule_range_empty_and_nested_are_rejected():
    with pytest.raises(ValueError):
        CoverageRule(rectangle(),12,11)
    with pytest.raises(ValueError):
        build_coverage('empty', [])
    with pytest.raises(ValueError):
        CoverageRule(make_strategy(),8,12)


def test_estimate_is_cancellable():
    with pytest.raises(RegionEstimateCancelled):
        RegionTileEstimator(make_strategy()).estimate(8,12,should_cancel=lambda:True)


def test_tasks_store_only_snapshot_reference(work_dir):
    region=save_coverage_snapshot(make_strategy(), work_dir/'map.mbtiles')
    tasks=TaskService(TaskStore(work_dir/'tasks.json'))
    task=tasks.create_task(name='coverage', bounds=region.bounds,min_zoom=8,max_zoom=12,
                          map_layer=MapType.VECTOR.base_layer,output_path=work_dir/'map.mbtiles',region=region)
    restored=tasks.get_task(task.id)
    assert region_from_task(restored).boundary_version == region.boundary_version
    stored=(work_dir/'tasks.json').read_text(encoding='utf-8')
    assert 'coverage_data' not in stored and len(stored) < 5000
    assert restored.coverage_path == region.coverage_path


def test_missing_or_damaged_sidecar_with_no_map_cannot_become_rectangle(work_dir):
    region=save_coverage_snapshot(make_strategy(), work_dir/'map.mbtiles')
    from app.models import DownloadTask
    task=DownloadTask(name='coverage',region_type='coverage',boundary_version=region.boundary_version,
                      coverage_path=region.coverage_path,output_path=work_dir/'map.mbtiles',
                      west=region.bounds.west,south=region.bounds.south,east=region.bounds.east,north=region.bounds.north)
    Path(region.coverage_path).write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError):
        region_from_task(task)
    Path(region.coverage_path).unlink()
    with pytest.raises(ValueError):
        region_from_task(task)


@pytest.mark.parametrize('mode',[MapType.VECTOR,MapType.IMAGE])
@pytest.mark.parametrize('labels',[False,True])
async def test_both_map_modes_and_labels_use_one_exact_sparse_file(work_dir, mode, labels):
    _,task,server,_,outcome=await seed(work_dir,mode=mode,labels=labels)
    region=region_from_task(task)
    expected=tiles(region,8,12)
    assert outcome.is_success and outcome.diagnostics.missing_tiles == 0
    assert set(server.requests)==expected
    assert len(server.requests)==len(expected)*(2 if labels else 1)
    assert len(list(work_dir.glob('*.mbtiles')))==1


async def test_complete_offline_acceptance(work_dir):
    await run_checks(work_dir)


async def test_resume_refuses_different_strategy_before_any_http(work_dir):
    tasks,task,server,service,_=await seed(work_dir)
    server.requests.clear()
    other=build_coverage('other',[CoverageRule(rectangle(),12,12)])
    with sqlite3.connect(task.output_path) as db:
        db.execute("UPDATE metadata SET value=? WHERE name='coverage_data'",(other.coverage_data,))
    with pytest.raises(MapResumeError,match='指纹'):
        await service.resume_task(task,task_service=tasks)
    assert not server.requests


async def test_partial_failure_resume_preserves_healthy_tiles(work_dir):
    server=TileServer()
    region=make_strategy()
    expected=tiles(region,8,12)
    failed=next(iter(expected))
    server.fail_keys.add(failed)
    with pytest.raises(AssertionError):
        await seed(work_dir,server=server,labels=False)
    tasks=TaskService(TaskStore(work_dir/'tasks.json'))
    task=tasks.list_tasks()[0]
    server.fail_keys.clear()
    server.requests.clear()
    outcome=await service_for(server).resume_task(task,task_service=tasks)
    assert outcome.is_success and server.requests==[failed]
    assert outcome.summary.skipped==len(expected)-1


async def test_cancellation_and_repair_use_the_same_coverage(work_dir):
    tasks,task,server,_,_=await seed(work_dir,labels=False)
    from scripts.verify_repair import BROKEN_PNG, overwrite_tile
    expected=tiles(region_from_task(task),8,12)
    for key in expected:
        overwrite_tile(task.output_path,key,BROKEN_PNG)
    server.requests.clear()
    service=service_for(server,progress_interval_seconds=0)
    def cancel(progress):
        if progress.succeeded>=1:
            service.cancel()
    cancelled=await service.resume_task(task,task_service=tasks,resume_policy=ResumePolicy.repair(),on_progress=cancel)
    assert cancelled.cancelled and not cancelled.is_success
    server.requests.clear()
    repaired=await service_for(server).resume_task(task,task_service=tasks,resume_policy=ResumePolicy.repair())
    assert repaired.is_success
    assert set(server.requests)<=expected


async def test_budget_uses_unique_tiles_and_resume_deductions(work_dir):
    _,task,_,_,_=await seed(work_dir)
    region=region_from_task(task)
    request=BudgetInput('union','union',region,8,12,task.output_path,MapType.VECTOR,True)
    report=assess_budget([request],BudgetSettings(reserve_gib=0),disk_probe=lambda p:('disk',2**40))
    assert report.requests==0 and report.new_bytes==0
    assert sum(z.total for z in report.items[0].zooms)==len(tiles(region,8,12))
    fresh=assess_budget([replace(request,output_path=work_dir/'fresh.mbtiles')],disk_probe=lambda p:('disk',2**40))
    assert fresh.requests==2*len(tiles(region,8,12))
    assert fresh.new_bytes>=len(region.coverage_data)
