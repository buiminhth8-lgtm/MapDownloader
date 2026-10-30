"""Slice 38: read-only source, validated raster export, content dedup and regional splits."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import zlib
from collections import OrderedDict
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from app.geo import (
    GeoBounds,
    RegionTileEstimator,
    build_region_filter,
    region_from_metadata,
    xyz_y_to_tms_y,
)
from app.geo.zoom_coverage import (
    CoverageRule,
    build_coverage,
    coverage_from_metadata,
    coverage_region,
    coverage_zoom_range,
    decode_coverage,
)
from app.mbtiles import MBTilesReader
from app.models.naming import sanitize_map_name
from app.services.map_package_service import MapPackageService
from app.services.mbtiles_inspection_service import MBTilesInspectionService

LAYOUT_NORMALIZED = 'normalized-v1'
LAYOUT_FLAT = 'flat-export-v1'
OSMDROID_SQL = 'SELECT tile_data FROM tiles WHERE tile_column=? AND tile_row=? AND zoom_level=?'
CACHE_BYTES = 32 * 1024 * 1024
MAX_TARGETS = 128


class ExportCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class ExportOptions:
    optimize_png: bool = True
    deduplicate: bool = True
    jpeg_quality: int | None = None
    split_rules: bool = False
    build_packages: bool = True
    reserve_bytes: int = 256 * 1024 * 1024

    def __post_init__(self):
        if self.jpeg_quality is not None and (type(self.jpeg_quality) is not int or not 50 <= self.jpeg_quality <= 95):
            raise ValueError('JPEG 质量必须为 50～95 的整数')
        if type(self.reserve_bytes) is not int or self.reserve_bytes < 0:
            raise ValueError('磁盘保留空间必须为非负整数')


@dataclass(frozen=True)
class ExportTarget:
    name: str
    region: object = None
    min_zoom: int | None = None
    max_zoom: int | None = None


@dataclass(frozen=True)
class PreparedTarget:
    name: str
    region: object
    count: int
    full: bool = False


@dataclass(frozen=True)
class ExportPlan:
    source: Path
    destination: Path
    options: ExportOptions
    metadata: dict
    source_bytes: int
    source_tiles: int
    identity: tuple
    targets: tuple[PreparedTarget, ...]
    required_bytes: int
    free_bytes: int
    warnings: tuple[str, ...]

    def as_lines(self):
        from app.geo import format_size
        return [f'源地图：{self.source}', f'新输出目录：{self.destination}',
                f'源文件：{self.source_tiles:,} 张 / {format_size(self.source_bytes)}',
                f'预计输出 {len(self.targets)} 个文件，合计 {sum(t.count for t in self.targets):,} 个坐标（跨包重叠会分别保存）',
                '内容去重：' + ('map/images + tiles 视图' if self.options.deduplicate else '普通 tiles 表'),
                '图片处理：' + (f'卫星纯底图 JPEG，有损，质量 {self.options.jpeg_quality}' if self.options.jpeg_quality else 'PNG 像素无损优化；JPEG 保持原字节'),
                f'磁盘可用 {format_size(self.free_bytes)}；估计需预留 {format_size(self.required_bytes)}（含 {format_size(self.options.reserve_bytes)} 保留空间与可选打包副本）',
                *[f'分包：{t.name}，{t.count:,} 张' for t in self.targets],
                *[f'提示：{w}' for w in self.warnings]]


def _check(cancel):
    if cancel is not None and cancel():
        raise ExportCancelled('导出已取消，源地图保留')


def _identity(path):
    stat = Path(path).stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def _free(path):
    path = Path(path)
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free


def _encoded(payload):
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return coverage_region(base64.b64encode(zlib.compress(raw, 6)).decode('ascii'))


def _source_region(metadata):
    if metadata.get('region_type') == 'coverage':
        return coverage_from_metadata(metadata)
    bounds = GeoBounds(*(float(v) for v in metadata['bounds'].split(',')))
    region, note = region_from_metadata(metadata.get('region_type'), metadata.get('region_id'),
                                        metadata.get('boundary_version'), bounds)
    if region is None:
        raise ValueError(note or '源地图区域边界不可用，不能安全分包')
    return build_coverage(metadata.get('name', 'source'), [CoverageRule(region, int(metadata['minzoom']), int(metadata['maxzoom']))])


def _clip_target(source, target):
    source_payload = decode_coverage(source.coverage_data)
    if target.region.coverage_data:
        requested = decode_coverage(target.region.coverage_data)['rules']
    else:
        requested = decode_coverage(build_coverage(target.name, [CoverageRule(target.region, target.min_zoom, target.max_zoom)]).coverage_data)['rules']
    source_rules = [(r['minZoom'], r['maxZoom'], shape(r['geometry'])) for r in source_payload['rules']]
    request_rules = [(r['minZoom'], r['maxZoom'], shape(r['geometry']), r) for r in requested]
    low, high = coverage_zoom_range(source)
    rules = []
    for zoom in range(low, high + 1):
        a = [g for low_zoom, high_zoom, g in source_rules if low_zoom <= zoom <= high_zoom]
        active = [(g,rule) for low_zoom,high_zoom,g,rule in request_rules if low_zoom<=zoom<=high_zoom]
        b = [g for g,_ in active]
        if not a or not b:
            continue
        geometry = unary_union(a).intersection(unary_union(b))
        if geometry.geom_type == 'GeometryCollection':
            geometry = unary_union([g for g in geometry.geoms if g.geom_type in ('Polygon', 'MultiPolygon')])
        if geometry.is_empty or geometry.area <= 0:
            continue
        levels = {rule.get('level','custom') for _,rule in active}
        radii = {rule.get('bufferMeters') for _,rule in active}
        rules.append({'id': target.name, 'name': target.name, 'level': next(iter(levels)) if len(levels)==1 else 'custom', 'boundaryVersion': source.boundary_version,
                      'bufferMeters': next(iter(radii)) if len(radii)==1 else None, 'minZoom': zoom, 'maxZoom': zoom, 'geometry': mapping(geometry)})
    if not rules:
        raise ValueError(f'分包「{target.name}」在源地图实际覆盖区域/Zoom 内没有面积，请调整区域')
    return _encoded({'version': 1, 'name': target.name, 'rules': rules})


def prepare_export(source, destination, options=None, *, targets=(), should_cancel=None, progress=None, disk_probe=None):
    options = options or ExportOptions()
    source, destination = Path(source).resolve(), Path(destination).resolve()
    _check(should_cancel)
    if not source.is_file():
        raise ValueError('请选择已下载的 MBTiles 文件')
    if destination.exists() or destination == source or source.is_relative_to(destination):
        raise ValueError('输出必须是尚不存在的新目录，不能覆盖源文件或已有产物')
    wal = source.with_name(source.name + '-wal')
    if wal.is_file() and wal.stat().st_size:
        raise ValueError('源地图仍有 WAL 数据，请停止下载并关闭写入后再导出')
    identity = _identity(source)
    if progress:
        progress('正在检查源地图与覆盖策略…')
    inspected = MBTilesInspectionService().inspect(source, check_coverage=True, should_stop=should_cancel)
    _check(should_cancel)
    if not inspected.valid or inspected.missing_tiles:
        raise ValueError('请先补齐或修复源地图再导出：' + '；'.join(inspected.errors or inspected.warnings))
    metadata = dict(inspected.metadata)
    if metadata.get('format') not in {'png', 'jpg', 'jpeg'}:
        raise ValueError('当前仅支持 PNG/JPEG 栅格 MBTiles')
    if options.jpeg_quality is not None and (metadata.get('map_type') != 'image' or metadata.get('labels') not in {'false','0'}
                                             or metadata.get('composition') != 'single'):
        raise ValueError('JPEG 有损压缩只允许卫星纯底图；矢量、注记和未知来源保持 PNG/原格式')
    source_region = None
    warnings = list(inspected.warnings)
    try:
        source_region = _source_region(metadata)
    except ValueError as exc:
        if options.split_rules or targets or metadata.get('region_type') == 'coverage':
            raise
        warnings.append(f'保留原区域元数据；无法冻结旧包几何：{exc}')
    requested = tuple(targets)
    if options.split_rules:
        if requested:
            raise ValueError('覆盖规则分包和自定义分包不能同时使用')
        if metadata.get('region_type') != 'coverage':
            raise ValueError('按覆盖规则分包需要 Slice 36 单包；普通地图可选择按下载计划区域分包')
        payload = decode_coverage(source_region.coverage_data)
        requested = tuple(ExportTarget(rule['name'], _encoded({'version':1, 'name':rule['name'], 'rules':[rule]}))
                          for rule in payload['rules'])
    if len(requested) > MAX_TARGETS:
        raise ValueError(f'一次最多导出 {MAX_TARGETS} 个区域')
    prepared = []
    for target in requested:
        _check(should_cancel)
        region = _clip_target(source_region, target)
        low, high = coverage_zoom_range(region)
        count = RegionTileEstimator(region).estimate(low, high, should_cancel=should_cancel).selected_tiles
        if not count:
            raise ValueError(f'分包「{target.name}」没有瓦片')
        prepared.append(PreparedTarget(target.name, region, count))
    if not prepared:
        prepared = [PreparedTarget(metadata.get('name') or source.stem, source_region, inspected.tile_count, True)]
    # Includes a temporary export, indices/metadata, optional full package copies and user reserve.
    required = (source.stat().st_size * len(prepared) * (3 if options.build_packages else 2)
                + sum(t.count * 256 + len(t.region.coverage_data if t.region else '') * 2 for t in prepared)
                + 1024 * 1024 + options.reserve_bytes)
    free = (disk_probe or _free)(destination.parent)
    if free < required:
        raise ValueError(f'磁盘空间不足：可用 {free:,} 字节，本轮保守预留 {required:,} 字节')
    if _identity(source) != identity:
        raise ValueError('源文件在预览期间发生变化，请停止写入后重新预览')
    warnings.append('实际节省取决于内容；重复瓦片少或区域重叠较多时，文件合计可能变大，以完成报告为准')
    return ExportPlan(source, destination, options, metadata, identity[0], inspected.tile_count, identity,
                      tuple(prepared), required, free, tuple(warnings))


def _pixels(image):
    if image.mode in {'I', 'I;16', 'F'}:
        return image.mode, image.tobytes()
    return 'RGBA', image.convert('RGBA').tobytes()


def _encode(data, options, *, expected_format=None):
    with Image.open(io.BytesIO(data)) as im:
        if expected_format and im.format != ('PNG' if expected_format == 'png' else 'JPEG'):
            raise ValueError('实际图片格式与 metadata.format 声明不一致，请先检查源地图')
        if im.format not in {'PNG', 'JPEG'} or im.width * im.height > 4 * 1024 * 1024:
            raise ValueError('瓦片不是受支持的 PNG/JPEG，或像素过大')
        im.verify()
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        output = io.BytesIO()
        if options.jpeg_quality is not None:
            if image.convert('RGBA').getchannel('A').getextrema() != (255,255):
                raise ValueError('透明瓦片不能转换为 JPEG，请改用 PNG 无损优化')
            image.convert('RGB').save(output, format='JPEG', quality=options.jpeg_quality, optimize=True, subsampling=0)
            result = output.getvalue()
        elif options.optimize_png and image.format == 'PNG':
            image.save(output, format='PNG', optimize=True, compress_level=9)
            candidate = output.getvalue()
            with Image.open(io.BytesIO(candidate)) as check:
                check.load()
                if _pixels(check) != _pixels(image):
                    raise ValueError('PNG 优化改变像素，拒绝发布')
            result = candidate if len(candidate) < len(data) else data
        else:
            result = data
    with Image.open(io.BytesIO(result)) as check:
        check.load()
    return result


class _TargetWriter:
    def __init__(self, path, metadata, deduplicate):
        self.path, self.deduplicate = path, deduplicate
        self.count = self.unique = self.input_bytes = self.output_bytes = 0
        self.db = sqlite3.connect(path)
        try:
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA cache_size=-1024')
            self.db.execute('CREATE TABLE metadata(name TEXT PRIMARY KEY, value TEXT)')
            self.db.executemany('INSERT INTO metadata VALUES(?,?)',metadata.items())
            if deduplicate:
                self.db.execute('CREATE TABLE images(tile_id INTEGER PRIMARY KEY, hash TEXT NOT NULL UNIQUE, tile_data BLOB NOT NULL)')
                self.db.execute('CREATE TABLE map(zoom_level INTEGER NOT NULL,tile_column INTEGER NOT NULL,tile_row INTEGER NOT NULL, tile_id INTEGER NOT NULL REFERENCES images(tile_id), PRIMARY KEY(zoom_level,tile_column,tile_row))')
                self.db.execute('CREATE VIEW tiles AS SELECT map.zoom_level,map.tile_column,map.tile_row,images.tile_data FROM map JOIN images ON map.tile_id=images.tile_id')
            else:
                self.db.execute('CREATE TABLE tiles(zoom_level INTEGER NOT NULL,tile_column INTEGER NOT NULL,tile_row INTEGER NOT NULL,tile_data BLOB NOT NULL,PRIMARY KEY(zoom_level,tile_column,tile_row))')
            self.db.commit()
        except BaseException:
            self.db.close()
            raise

    def add(self,z,x,y,data,input_bytes):
        row = xyz_y_to_tms_y(z,y)
        if self.deduplicate:
            digest=hashlib.sha256(data).hexdigest()
            existing=self.db.execute('SELECT tile_id,tile_data FROM images WHERE hash=?',(digest,)).fetchone()
            if existing is None:
                tile_id=self.db.execute('INSERT INTO images(hash,tile_data) VALUES(?,?)',(digest,data)).lastrowid
                self.unique += 1
                self.output_bytes += len(data)
            else:
                tile_id,stored=existing
                if bytes(stored)!=data:
                    raise ValueError('检测到内容哈希碰撞，已停止导出')
            self.db.execute('INSERT INTO map VALUES(?,?,?,?)',(z,x,row,tile_id))
        else:
            self.db.execute('INSERT INTO tiles VALUES(?,?,?,?)',(z,x,row,data))
            self.unique += 1
            self.output_bytes += len(data)
        self.count += 1
        self.input_bytes += input_bytes
        if self.count % 256 == 0:
            self.db.commit()

    def close(self):
        self.db.commit()
        self.db.close()


def verify_osmdroid_reads(path, *, should_cancel=None):
    """Replay MBTilesFileArchive's exact TMS query, including its floating row string."""
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite 自检失败')
        count=0
        declared=db.execute("SELECT value FROM metadata WHERE name='format'").fetchone()[0]
        # Check each distinct BLOB once; every coordinate is still read with the Android query.
        seen=set()
        for z,x,row in db.execute('SELECT zoom_level,tile_column,tile_row FROM tiles'):
            if count%256==0:
                _check(should_cancel)
            result=db.execute(OSMDROID_SQL,(str(x),str(float(row)),str(z))).fetchone()
            if not result or not result[0]:
                raise ValueError('osmdroid 查询未能读出瓦片')
            data=bytes(result[0])
            digest=hashlib.sha256(data).digest()
            if digest not in seen:
                _encode(data,ExportOptions(optimize_png=False),expected_format=declared)
                # Keep validation memory bounded even on very diverse satellite maps.
                if len(seen)<8192:
                    seen.add(digest)
            count += 1
        return count


def execute_export(plan, *, should_cancel=None, progress=None, disk_probe=None):
    _check(should_cancel)
    if _identity(plan.source)!=plan.identity:
        raise ValueError('源文件已变化，请重新预览')
    if plan.destination.exists():
        raise ValueError('输出目录已经存在，请换一个名称；已有产物未改动')
    free=disk_probe or _free
    if free(plan.destination.parent)<plan.required_bytes:
        raise ValueError('磁盘空间已变化，请释放空间并重新预览')
    plan.destination.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.'+plan.destination.name+'.export-',dir=plan.destination.parent))
    writers=[]
    records=[]
    cache=OrderedDict()
    cache_size=0
    try:
        filters=[]
        for index,target in enumerate(plan.targets):
            metadata=dict(plan.metadata)
            for key in ('region_type','region_id','boundary_version','region_level','buffer_meters','coverage_data'):
                metadata.pop(key,None)
            if target.region is not None:
                metadata.update(target.region.metadata_rows())
                levels={rule.get('level','custom') for rule in decode_coverage(target.region.coverage_data)['rules']}
                if target.full and plan.metadata.get('region_level'):
                    metadata['region_level']=plan.metadata['region_level']
                elif len(levels)==1:
                    metadata['region_level']=next(iter(levels))
                metadata['bounds']=','.join(str(v) for v in target.region.bounds.as_tuple())
                low,high=coverage_zoom_range(target.region)
                metadata.update(minzoom=str(low),maxzoom=str(high))
            else:
                metadata.update({k:v for k,v in plan.metadata.items() if k.startswith('region_') or k in {'boundary_version','buffer_meters'}})
            metadata.update(name=target.name,export_tile_count=str(target.count),storage_layout=LAYOUT_NORMALIZED if plan.options.deduplicate else LAYOUT_FLAT,
                            export_readonly='true',optimization=json.dumps({'version':1,'pngLossless':plan.options.optimize_png,
                                                                          'jpegQuality':plan.options.jpeg_quality}))
            if plan.options.jpeg_quality is not None:
                metadata['format']='jpg'
            path=stage/f'{index+1:03d}_{sanitize_map_name(target.name)}.mbtiles'
            writers.append(_TargetWriter(path,metadata,plan.options.deduplicate))
            filters.append(None if target.full else build_region_filter(target.region))
        with MBTilesReader(plan.source) as reader:
            # Hold one SQLite read snapshot during all source iteration.
            reader._require_open().execute('BEGIN')
            for index,(z,x,y,data) in enumerate(reader.iter_tiles(batch_size=8)):
                _check(should_cancel)
                if index%256==0:
                    if free(stage)<plan.options.reserve_bytes+1024*1024:
                        raise ValueError('磁盘剩余空间低于保留值，已停止导出')
                    if progress:
                        progress(f'压缩与去重：{index:,}/{plan.source_tiles:,} 张')
                selected=[w for w,f in zip(writers,filters,strict=True) if f is None or f.intersects_tile_xyz(z,x,y)]
                if not selected:
                    continue
                digest=hashlib.sha256(data).digest()
                cached=cache.get(digest)
                if cached is not None and cached[0]==data:
                    transformed=cached[1]
                    cache.move_to_end(digest)
                else:
                    transformed=_encode(data,plan.options,expected_format=None if plan.options.jpeg_quality else plan.metadata["format"])
                    size=len(data)+len(transformed)
                    if size<=CACHE_BYTES:
                        while cache and (cache_size+size>CACHE_BYTES or len(cache)>=128):
                            _,old=cache.popitem(last=False)
                            cache_size -= len(old[0])+len(old[1])
                        cache[digest]=(data,transformed)
                        cache_size += size
                for writer in selected:
                    writer.add(z,x,y,transformed,len(data))
        for writer,target in zip(writers,plan.targets,strict=True):
            writer.close()
            if writer.count != target.count:
                raise ValueError(f'分包「{target.name}」不完整：预期 {target.count} 张，导出 {writer.count} 张；请先补齐源地图')
            _check(should_cancel)
            if progress:
                progress(f'校验图片与 osmdroid 查询：{target.name}')
            if verify_osmdroid_reads(writer.path,should_cancel=should_cancel)!=writer.count:
                raise ValueError('osmdroid 读取计数不一致')
            inspected=MBTilesInspectionService().inspect(writer.path,check_coverage=True,should_stop=should_cancel)
            _check(should_cancel)
            if not inspected.valid or inspected.missing_tiles:
                raise ValueError('导出覆盖检查失败：'+'；'.join(inspected.errors or inspected.warnings))
            package=None
            if plan.options.build_packages:
                built=MapPackageService().build(writer.path,stage/'packages',package_name=writer.path.stem,
                                               accept_warnings=True,should_stop=should_cancel)
                _check(should_cancel)
                if not built.success:
                    raise ValueError(f'地图包构建失败：{built}')
                checked=MapPackageService().validate(built.package_dir,should_stop=should_cancel)
                _check(should_cancel)
                if not checked.valid:
                    raise ValueError('地图包验证失败：'+'；'.join(checked.errors))
                package=str(Path(built.package_dir).relative_to(stage))
            records.append({'file':writer.path.name,'tiles':writer.count,'uniqueImages':writer.unique,
                            'inputTileBytes':writer.input_bytes,'storedImageBytes':writer.output_bytes,
                            'fileBytes':writer.path.stat().st_size,'package':package,
                            'osmdroidQueryVerified':True,'warnings':list(inspected.warnings)})
        report={'version':1,'source':str(plan.source),'sourceFileBytes':plan.source_bytes,'sourceTiles':plan.source_tiles,
                'outputFileBytes':sum(r['fileBytes'] for r in records),'outputs':records,'warnings':list(plan.warnings),
                'androidDeviceVerified':False,'lossy':plan.options.jpeg_quality is not None}
        report['savedFileBytes']=report['sourceFileBytes']-report['outputFileBytes']
        artifact_bytes=sum(p.stat().st_size for p in stage.rglob('*') if p.is_file())
        report['deliveredBytes']=artifact_bytes
        # The JSON report itself is part of delivered storage; converge its digit count.
        for _ in range(8):
            report['deliveredSavedBytes']=plan.source_bytes-report['deliveredBytes']
            rendered=json.dumps(report,ensure_ascii=False,indent=2).encode('utf-8')
            total=artifact_bytes+len(rendered)
            if total==report['deliveredBytes']:
                break
            report['deliveredBytes']=total
        (stage/'export_report.json').write_bytes(rendered)
        _check(should_cancel)
        # rename (not replace) rejects a raced destination; all outputs publish together.
        os.rename(stage,plan.destination)
        return report
    finally:
        for writer in writers:
            with suppress(sqlite3.Error):
                writer.db.close()
        if stage.exists():
            shutil.rmtree(stage)
