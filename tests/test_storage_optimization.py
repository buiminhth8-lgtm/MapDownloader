from __future__ import annotations

import io
import json
import sqlite3
from contextlib import closing

import pytest
from PIL import Image
from scripts.verify_storage_optimization import run_checks, seed_source

from app.geo import DownloadRegion, GeoBounds
from app.mbtiles import MBTilesReader, MBTilesWriter
from app.models import DownloadTask, MapType
from app.services import mbtiles_optimizer as module
from app.services.map_download_service import MapDownloadService, MapResumeError
from app.services.mbtiles_inspection_service import MBTilesInspectionService
from app.services.mbtiles_optimizer import (
    ExportCancelled,
    ExportOptions,
    ExportTarget,
    execute_export,
    prepare_export,
    verify_osmdroid_reads,
)


@pytest.fixture
def source(work_dir):
    return seed_source(work_dir,alpha=True)


def plan_for(source, *, options=None, targets=(), destination=None, **kwargs):
    return prepare_export(source,destination or source.parent/'result',options or ExportOptions(build_packages=False,reserve_bytes=0),
                          targets=targets,**kwargs)


def optimized(source, **kwargs):
    plan=plan_for(source,**kwargs)
    report=execute_export(plan)
    return plan,report,plan.destination/report['outputs'][0]['file']


def test_offline_acceptance_real_sqlite_pixel_alpha_package_and_splits(work_dir):
    run_checks(work_dir)


def test_preview_writes_nothing_and_reports_concrete_targets(source):
    before=source.read_bytes()
    plan=plan_for(source)
    assert source.read_bytes()==before and not plan.destination.exists()
    assert str(plan.destination) in '\n'.join(plan.as_lines())
    assert sum(t.count for t in plan.targets)==plan.source_tiles


def test_normalized_schema_deduplicates_content_and_keeps_all_coordinates(source):
    _,report,path=optimized(source)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT type FROM sqlite_master WHERE name='tiles'").fetchone()[0]=='view'
        assert db.execute('SELECT COUNT(*) FROM images').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM map').fetchone()[0]==report['sourceTiles']
        plan=db.execute('EXPLAIN QUERY PLAN '+module.OSMDROID_SQL,('1','1.0','12')).fetchall()
        assert any('SEARCH map' in row[3] for row in plan)
    assert verify_osmdroid_reads(path)==report['sourceTiles']


def test_distinct_image_content_is_not_deduplicated_together(source):
    stream=io.BytesIO()
    Image.new('RGBA',(256,256),(255,0,0,255)).save(stream,format='PNG')
    with closing(sqlite3.connect(source)) as db,db:
        db.execute('UPDATE tiles SET tile_data=? WHERE rowid=(SELECT MIN(rowid) FROM tiles)',(stream.getvalue(),))
    _,report,_=optimized(source)
    assert report['outputs'][0]['uniqueImages']==2


def test_flat_export_preserves_blob_bytes_when_compression_disabled(source):
    _,report,path=optimized(source,options=ExportOptions(optimize_png=False,deduplicate=False,build_packages=False,reserve_bytes=0))
    with MBTilesReader(source) as a,MBTilesReader(path) as b:
        assert list(a.iter_tiles())==list(b.iter_tiles())
    assert report['outputs'][0]['uniqueImages']==report['sourceTiles']


@pytest.mark.parametrize('mode,labels',[(MapType.VECTOR,False),(MapType.VECTOR,True),(MapType.IMAGE,True)])
def test_jpeg_cannot_destroy_vector_text_or_annotations(work_dir,mode,labels):
    source=seed_source(work_dir,mode=mode,labels=labels)
    with pytest.raises(ValueError,match='卫星纯底图'):
        plan_for(source,options=ExportOptions(jpeg_quality=85))


def test_jpeg_satellite_is_opt_in_and_reported_as_lossy(work_dir):
    source=seed_source(work_dir,mode=MapType.IMAGE,labels=False)
    _,report,path=optimized(source,options=ExportOptions(jpeg_quality=85,build_packages=False,reserve_bytes=0))
    assert report['lossy'] and report['savedFileBytes']>0
    with MBTilesReader(path) as reader:
        assert reader.get_metadata()['format']=='jpg'
        for _,_,_,data in reader.iter_tiles():
            with Image.open(io.BytesIO(data)) as im:
                assert im.format=='JPEG' and im.size==(256,256)


def test_transparent_satellite_cannot_silently_flatten_into_jpeg(work_dir):
    source=seed_source(work_dir,mode=MapType.IMAGE,labels=False,alpha=True)
    plan=plan_for(source,options=ExportOptions(jpeg_quality=85,build_packages=False,reserve_bytes=0))
    before=source.read_bytes()
    with pytest.raises(ValueError,match='透明'):
        execute_export(plan)
    assert source.read_bytes()==before and not plan.destination.exists()
    assert not list(source.parent.glob('.*.export-*'))


@pytest.mark.parametrize('mutate',["DELETE FROM tiles WHERE rowid=(SELECT MIN(rowid) FROM tiles)",
                                  "UPDATE tiles SET tile_data=x'010203'"])
def test_missing_or_broken_source_requires_repair(source,mutate):
    with closing(sqlite3.connect(source)) as db,db:
        db.execute(mutate)
    with pytest.raises(ValueError,match='修复'):
        plan_for(source)


def test_existing_destination_is_never_overwritten(source):
    destination=source.parent/'existing'
    destination.mkdir()
    marker=destination/'keep.txt'
    marker.write_text('keep',encoding='utf-8')
    with pytest.raises(ValueError,match='新目录'):
        plan_for(source,destination=destination)
    assert marker.read_text(encoding='utf-8')=='keep'


def test_raced_destination_after_preview_is_preserved(source):
    plan=plan_for(source)
    plan.destination.mkdir()
    marker=plan.destination/'keep.txt'
    marker.write_text('keep',encoding='utf-8')
    with pytest.raises(ValueError,match='已经存在'):
        execute_export(plan)
    assert marker.is_file()


def test_source_change_after_preview_invalidates_plan(source):
    plan=plan_for(source)
    with closing(sqlite3.connect(source)) as db,db:
        db.execute("INSERT INTO metadata VALUES('changed','yes')")
    with pytest.raises(ValueError,match='已变化'):
        execute_export(plan)
    assert not plan.destination.exists()


def test_cancel_during_preview_does_not_write(source):
    with pytest.raises(ExportCancelled):
        plan_for(source,should_cancel=lambda:True)
    assert not (source.parent/'result').exists()


def test_cancel_during_export_discards_all_staging_and_preserves_source(source):
    plan=plan_for(source)
    before=source.read_bytes()
    calls=0
    def cancel():
        nonlocal calls
        calls+=1
        return calls>=5
    with pytest.raises(ExportCancelled):
        execute_export(plan,should_cancel=cancel)
    assert not plan.destination.exists() and source.read_bytes()==before
    assert not list(source.parent.glob('.*.export-*'))


def test_tile_write_failure_does_not_publish_a_partial_result(source,monkeypatch):
    plan=plan_for(source)
    before=source.read_bytes()
    def fail(*args):
        raise OSError('simulated disk full')
    monkeypatch.setattr(module._TargetWriter,'add',fail)
    with pytest.raises(OSError,match='disk full'):
        execute_export(plan)
    assert not plan.destination.exists() and source.read_bytes()==before
    assert not list(source.parent.glob('.*.export-*'))


def test_disk_space_is_checked_before_preview_and_export(source):
    with pytest.raises(ValueError,match='空间不足'):
        plan_for(source,disk_probe=lambda p:0)
    plan=plan_for(source)
    with pytest.raises(ValueError,match='空间已变化'):
        execute_export(plan,disk_probe=lambda p:0)


def test_disk_space_drop_during_stream_discards_staging(source):
    plan=plan_for(source)
    calls=0
    def free(_):
        nonlocal calls
        calls+=1
        return 2**50 if calls==1 else 0
    with pytest.raises(ValueError,match='保留值'):
        execute_export(plan,disk_probe=free)
    assert not plan.destination.exists() and not list(source.parent.glob('.*.export-*'))


@pytest.mark.parametrize('deduplicate',[False,True])
def test_exported_files_are_readable_but_protected_against_writer(source,deduplicate):
    _,_,path=optimized(source,options=ExportOptions(deduplicate=deduplicate,build_packages=False,reserve_bytes=0))
    before=path.read_bytes()
    with pytest.raises(Exception,match='只读'):
        MBTilesWriter(path).open()
    assert path.read_bytes()==before


async def test_resume_does_not_write_to_readonly_optimized_artifact(source):
    _,_,path=optimized(source)
    before=path.read_bytes()
    task=DownloadTask(name='export',output_path=path,west=116,south=39,east=117,north=40,map_layer='vec_w')
    with pytest.raises(MapResumeError,match='只读'):
        await MapDownloadService().resume_task(task,'offline-token')
    assert path.read_bytes()==before


def test_dangling_normalized_images_are_not_hidden_by_tiles_join(source):
    _,_,path=optimized(source)
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('DELETE FROM images')
    result=MBTilesInspectionService().inspect(path)
    assert not result.valid and any('引用' in text for text in result.errors)


def test_region_export_is_clipped_to_source_high_zoom_coverage(source):
    requested=DownloadRegion.rectangle('large-request',GeoBounds(115,38,118,42))
    _,report,path=optimized(source,targets=[ExportTarget('high-only',requested,12,12)])
    with MBTilesReader(source) as a,MBTilesReader(path) as b:
        expected={coord for coord in a.iter_tile_coordinates() if coord[0]==12}
        assert set(b.iter_tile_coordinates())==expected
    assert report['outputs'][0]['tiles']==len(expected)


def test_outside_region_does_not_fabricate_empty_package(source):
    requested=DownloadRegion.rectangle('outside',GeoBounds(100,20,101,21))
    with pytest.raises(ValueError,match='没有面积'):
        plan_for(source,targets=[ExportTarget('outside',requested,8,12)])


def test_normalized_files_can_be_reexported(source):
    _,report,path=optimized(source)
    second,again,_=optimized(path,destination=source.parent/'second')
    assert again['sourceTiles']==report['sourceTiles']
    assert second.destination.is_dir()


def test_report_counts_full_delivery_including_package_copies(source):
    plan,report,_=optimized(source,options=ExportOptions(reserve_bytes=0))
    files=[p for p in plan.destination.rglob('*') if p.is_file()]
    assert report['deliveredBytes']==sum(p.stat().st_size for p in files)
    assert report['deliveredBytes']>report['outputFileBytes']
    assert json.loads((plan.destination/'export_report.json').read_text(encoding='utf-8'))==report
    assert not report['androidDeviceVerified']


@pytest.mark.parametrize('quality',[0,49,96,101,True])
def test_invalid_jpeg_quality_is_rejected(quality):
    with pytest.raises(ValueError):
        ExportOptions(jpeg_quality=quality)


def test_no_savings_is_reported_honestly_when_duplicate_storage_is_disabled(source):
    plan,report,_=optimized(source,options=ExportOptions(optimize_png=False,deduplicate=False,split_rules=True,build_packages=False,reserve_bytes=0))
    assert report['savedFileBytes']==report['sourceFileBytes']-report['outputFileBytes']
    assert report['outputFileBytes']==sum(r['fileBytes'] for r in report['outputs'])
    assert len(report['outputs'])==3 and plan.destination.exists()


def test_unavailable_legacy_boundary_can_only_be_copied_whole(source):
    with closing(sqlite3.connect(source)) as db,db:
        db.execute("UPDATE metadata SET value='polygon' WHERE name='region_type'")
        db.execute("UPDATE metadata SET value='unknown-region' WHERE name='region_id'")
        db.execute("DELETE FROM metadata WHERE name='coverage_data'")
    _,report,path=optimized(source)
    assert any('无法冻结' in w for w in report['warnings'])
    assert verify_osmdroid_reads(path)==report['sourceTiles']
    requested=DownloadRegion.rectangle('clip',GeoBounds(116,39,117,40))
    with pytest.raises(ValueError):
        plan_for(source,destination=source.parent/'split',targets=[ExportTarget('clip',requested,8,12)])


@pytest.mark.parametrize('deduplicate',[False,True])
def test_removed_export_coordinate_is_detected_even_without_legacy_geometry(source,deduplicate):
    _,_,path=optimized(source,options=ExportOptions(deduplicate=deduplicate,build_packages=False,reserve_bytes=0))
    with closing(sqlite3.connect(path)) as db,db:
        relation='map' if deduplicate else 'tiles'
        db.execute(f'DELETE FROM {relation} WHERE rowid=(SELECT MIN(rowid) FROM {relation})')
        db.execute("UPDATE metadata SET value='polygon' WHERE name='region_type'")
        db.execute("UPDATE metadata SET value='unavailable' WHERE name='region_id'")
    assert not MBTilesInspectionService().inspect(path).valid


@pytest.mark.parametrize('mode',[MapType.VECTOR,MapType.IMAGE])
@pytest.mark.parametrize('labels',[False,True])
def test_lossless_export_preserves_both_modes_and_label_contract(work_dir,mode,labels):
    source=seed_source(work_dir,mode=mode,labels=labels,alpha=labels)
    _,report,path=optimized(source)
    with MBTilesReader(path) as reader:
        metadata=reader.get_metadata()
        assert metadata['map_type']==mode.value
        assert metadata['labels']==str(labels).lower() and metadata['format']=='png'
    assert not report['lossy'] and report['savedFileBytes']>0


def test_palette_transparency_survives_lossless_optimization(source):
    image=Image.new('P',(256,256),0)
    image.putpalette([0,0,0,255,0,0]+[0]*762)
    image.info['transparency']=0
    stream=io.BytesIO()
    image.save(stream,format='PNG',compress_level=0)
    raw=stream.getvalue()
    with closing(sqlite3.connect(source)) as db,db:
        db.execute('UPDATE tiles SET tile_data=?',(raw,))
    _,_,path=optimized(source)
    with MBTilesReader(path) as reader:
        _,_,_,data=next(reader.iter_tiles())
        with Image.open(io.BytesIO(data)) as decoded:
            assert decoded.convert('RGBA').tobytes()==image.convert('RGBA').tobytes()


def test_osmdroid_validation_rejects_payload_format_metadata_mismatch(source):
    _,_,path=optimized(source)
    with closing(sqlite3.connect(path)) as db,db:
        db.execute("UPDATE metadata SET value='jpg' WHERE name='format'")
    with pytest.raises(ValueError,match='格式'):
        verify_osmdroid_reads(path)


def test_regional_export_keeps_requested_level_for_android_selection(source):
    from dataclasses import replace

    from app.geo.zoom_coverage import decode_coverage
    from app.planning.region_level import RegionLevel
    requested=replace(DownloadRegion.rectangle('province',GeoBounds(115,38,118,42)),region_level=RegionLevel.parse('province'))
    _,_,path=optimized(source,targets=[ExportTarget('province',requested,12,12)])
    with MBTilesReader(path) as reader:
        metadata=reader.get_metadata()
        assert metadata['region_level']=='province'
        assert all(rule['level']=='province' for rule in decode_coverage(metadata['coverage_data'])['rules'])
