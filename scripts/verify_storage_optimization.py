"""Slice 38 offline acceptance with real raster SQLite files and osmdroid SQL."""
from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

from scripts.verify_zoom_coverage import make_strategy  # noqa: E402

from app.geo import build_region_filter  # noqa: E402
from app.mbtiles import MBTilesMetadata, MBTilesReader, MBTilesWriter  # noqa: E402
from app.models import MapType  # noqa: E402
from app.models.map_layer import composition_for  # noqa: E402
from app.services.mbtiles_optimizer import (  # noqa: E402
    ExportOptions,
    execute_export,
    prepare_export,
    verify_osmdroid_reads,
)


def seed_source(work, *, mode=MapType.VECTOR, labels=True, alpha=False):
    work=Path(work)
    work.mkdir(parents=True,exist_ok=True)
    source=work/'source.mbtiles'
    region=make_strategy()
    f=build_region_filter(region)
    image=Image.new('RGBA' if alpha else 'RGB',(256,256),(30,90,120,140) if alpha else (30,90,120))
    stream=io.BytesIO()
    image.save(stream,format='PNG',compress_level=0)
    metadata=MBTilesMetadata.for_bounds(region.bounds,8,12,name='storage-source',source_map_type=mode.value,
                                        with_labels=labels,composition=composition_for(labels),provider='tianditu',
                                        region_type='coverage',region_id=region.region_id,boundary_version=region.boundary_version)
    metadata.coverage_data=region.coverage_data
    with MBTilesWriter(source) as writer:
        writer.write_metadata(metadata)
        for zoom in range(8,13):
            for x,y in f.iter_selected_tiles(region.bounds,zoom):
                writer.write_tile(zoom,x,y,stream.getvalue())
    return source


def run_checks(work):
    source=seed_source(work,alpha=True)
    before=source.read_bytes()
    destination=Path(work)/'optimized'
    plan=prepare_export(source,destination,ExportOptions(reserve_bytes=0))
    assert not destination.exists()
    report=execute_export(plan)
    assert source.read_bytes()==before and report['savedFileBytes']>0
    record=report['outputs'][0]
    output=destination/record['file']
    assert record['uniqueImages']==1 and record['tiles']>1
    assert record['fileBytes']<len(before)//2
    print('[PASS] PNG 优化与内容去重降低实际文件大小，源文件逐字节保留')
    with MBTilesReader(source) as a, MBTilesReader(output) as b:
        original=list(a.iter_tile_coordinates())
        assert original==list(b.iter_tile_coordinates())
        for z,x,y in original:
            with Image.open(io.BytesIO(a.read_tile(z,x,y))) as left,Image.open(io.BytesIO(b.read_tile(z,x,y))) as right:
                assert left.convert('RGBA').tobytes()==right.convert('RGBA').tobytes()
    assert verify_osmdroid_reads(output)==record['tiles']
    print('[PASS] 每个坐标重放 osmdroid TMS 查询，PNG 像素和透明度一致')
    from app.services.map_package_service import MapPackageService
    assert MapPackageService().validate(destination/record['package']).valid
    print('[PASS] 去重 tiles 视图可被现有读取器、覆盖检查、打包和地图包验证读取')
    split_destination=Path(work)/'split'
    split=execute_export(prepare_export(source,split_destination,ExportOptions(split_rules=True,reserve_bytes=0)))
    assert len(split['outputs'])==3
    with MBTilesReader(source) as reader:
        source_coords=set(reader.iter_tile_coordinates())
    union=set()
    for item in split['outputs']:
        with MBTilesReader(split_destination/item['file']) as reader:
            coords=set(reader.iter_tile_coordinates())
            assert coords<=source_coords
            union.update(coords)
    assert union==source_coords
    print('[PASS] 按 Slice 36 冻结规则分包，逐包只保留对应区域/Zoom，合计无漏图')
    flat_dir=Path(work)/'flat'
    flat=execute_export(prepare_export(source,flat_dir,ExportOptions(deduplicate=False,build_packages=False,reserve_bytes=0)))
    assert verify_osmdroid_reads(flat_dir/flat['outputs'][0]['file'])==len(source_coords)
    print('[PASS] 普通 tiles 表兼容模式也能完成压缩导出与逐坐标读取')
    assert json.loads((destination/'export_report.json').read_text(encoding='utf-8'))==report
    print('[PASS] 报告记录实际前后大小、唯一图片数、校验范围与有损标记')
    print('Slice 38 存储优化离线验收全部通过；Android 真机显示仍需设备验收')


if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='storage-optimization-') as temp:
        run_checks(Path(temp))
