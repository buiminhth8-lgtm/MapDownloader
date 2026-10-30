"""Slice 36: immutable, portable per-Zoom area coverage with streaming tile union."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import zlib
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path

from shapely.geometry import box, mapping, shape
from shapely.prepared import prep

from app.geo.bounds import GeoBounds
from app.geo.geojson_loader import load_geojson_geometry
from app.geo.region import DownloadRegion, RegionType
from app.geo.tile_math import calculate_tile_range, tile_bounds, validate_zoom

MAX_RULES = 128
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class CoverageRule:
    region: DownloadRegion
    min_zoom: int
    max_zoom: int

    def __post_init__(self):
        validate_zoom(self.min_zoom)
        validate_zoom(self.max_zoom)
        if self.min_zoom > self.max_zoom:
            raise ValueError("覆盖规则最小 Zoom 不能大于最大 Zoom")
        if self.region.region_type is RegionType.COVERAGE:
            raise ValueError("覆盖策略不能嵌套")


def _canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode('utf-8')


def decode_coverage(data):
    """Validate the portable snapshot; malformed strategies never become rectangles."""
    try:
        compressed = base64.b64decode(data, validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, MAX_SNAPSHOT_BYTES + 1)
        if len(raw) > MAX_SNAPSHOT_BYTES or not decoder.eof or decoder.unused_data:
            raise ValueError("覆盖策略快照过大或压缩数据不完整")
        payload = json.loads(raw)
        if payload.get('version') != 1 or not isinstance(payload.get('name'), str) or not payload['name'].strip():
            raise ValueError("覆盖策略版本或名称无效")
        rules = payload['rules']
        if not isinstance(rules, list) or not 1 <= len(rules) <= MAX_RULES:
            raise ValueError("覆盖规则数量无效")
        for rule in rules:
            if not isinstance(rule.get('id'), str) or not isinstance(rule.get('name'), str):
                raise ValueError("覆盖区域标识或名称无效")
            low, high = rule['minZoom'], rule['maxZoom']
            if type(low) is not int or type(high) is not int:
                raise ValueError("覆盖 Zoom 必须为整数")
            validate_zoom(low)
            validate_zoom(high)
            if low > high:
                raise ValueError("覆盖规则 Zoom 范围无效")
            geometry = shape(rule['geometry'])
            if geometry.geom_type not in {'Polygon', 'MultiPolygon'} or geometry.is_empty or not geometry.is_valid:
                raise ValueError("覆盖策略必须包含有效多边形")
            GeoBounds(*geometry.bounds)
        return payload
    except (ValueError, TypeError, KeyError, AttributeError, zlib.error) as exc:
        raise ValueError(f"无法读取多级覆盖策略：{exc}") from exc


def coverage_region(data, *, snapshot_path='', expected_fingerprint=''):
    payload = decode_coverage(data)
    fingerprint = hashlib.sha256(_canonical(payload)).hexdigest()
    if expected_fingerprint and fingerprint != expected_fingerprint:
        raise ValueError("覆盖策略指纹不一致，无法安全继续")
    extents = [shape(r['geometry']).bounds for r in payload['rules']]
    bounds = GeoBounds(min(b[0] for b in extents), min(b[1] for b in extents),
                       max(b[2] for b in extents), max(b[3] for b in extents))
    return DownloadRegion(RegionType.COVERAGE, payload['name'], bounds,
                          region_id='coverage-' + fingerprint[:16], boundary_version=fingerprint,
                          coverage_data=data, coverage_path=str(snapshot_path))


def build_coverage(name, rules):
    rules = tuple(rules)
    if not rules or len(rules) > MAX_RULES:
        raise ValueError(f"请配置 1～{MAX_RULES} 条覆盖规则")
    entries = []
    for rule in rules:
        region = rule.region
        geometry = (load_geojson_geometry(region.geojson_path) if region.is_polygon
                    else box(*region.bounds.as_tuple()))
        entries.append({'id': region.region_id or region.name, 'name': region.name,
                        'level': region.region_level.value, 'boundaryVersion': region.boundary_version or '',
                        'bufferMeters': region.buffer_meters,
                        'minZoom': rule.min_zoom, 'maxZoom': rule.max_zoom,
                        'geometry': mapping(geometry)})
    payload = {'version': 1, 'name': str(name).strip(), 'rules': entries}
    raw = _canonical(payload)
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise ValueError("覆盖策略快照过大，请减少区域数量")
    return coverage_region(base64.b64encode(zlib.compress(raw, 6)).decode('ascii'))



def coverage_from_metadata(metadata):
    """Validate the declared envelope as well as the portable snapshot identity."""
    fingerprint = metadata.get('boundary_version', '')
    if metadata.get('region_type') != 'coverage' or len(fingerprint) != 64:
        raise ValueError('多级覆盖类型或策略指纹缺失')
    region = coverage_region(metadata.get('coverage_data', ''), expected_fingerprint=fingerprint)
    try:
        bounds = GeoBounds(*(float(v) for v in metadata['bounds'].split(',')))
        low, high = coverage_zoom_range(region)
        if (int(metadata['minzoom']), int(metadata['maxzoom'])) != (low, high):
            raise ValueError('声明的 Zoom 范围与覆盖策略不一致')
        if any(abs(a-b) > 0.00001 for a,b in zip(bounds.as_tuple(), region.bounds.as_tuple(), strict=True)):
            raise ValueError('声明的范围与覆盖策略不一致')
        if metadata.get('region_id') != region.region_id:
            raise ValueError('声明的区域标识与覆盖策略不一致')
    except (KeyError, TypeError) as exc:
        raise ValueError('覆盖策略声明缺少关键字段') from exc
    return region

def coverage_zoom_range(region):
    rules = decode_coverage(region.coverage_data)['rules']
    return min(r['minZoom'] for r in rules), max(r['maxZoom'] for r in rules)


def coverage_summary(region):
    payload = decode_coverage(region.coverage_data)
    return {'version': 1, 'fingerprint': region.boundary_version,
            'rules': [{key: value for key, value in r.items() if key != 'geometry'} |
                      {'bounds': list(shape(r['geometry']).bounds)} for r in payload['rules']]}


def save_coverage_snapshot(region, output_path):
    """Save once, outside task progress JSON; metadata carries its own portable copy."""
    path = Path(output_path).parent / '.coverage' / f'{region.boundary_version}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _canonical(decode_coverage(region.coverage_data))
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("已有覆盖策略快照损坏，请恢复快照或换一个输出目录")
    else:
        temporary = path.with_suffix('.tmp')
        try:
            temporary.write_bytes(raw)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return replace(region, coverage_path=str(path))


def coverage_from_task(task):
    if len(task.boundary_version) != 64:
        raise ValueError('任务缺少覆盖策略指纹，无法安全继续')
    path = Path(getattr(task, 'coverage_path', '') or '.')
    try:
        if path.is_file():
            raw = path.read_bytes()
            if len(raw) > MAX_SNAPSHOT_BYTES:
                raise ValueError("覆盖策略快照过大")
            data = base64.b64encode(zlib.compress(raw, 6)).decode('ascii')
        else:
            # A moved map remains resumable without Windows resource paths or its sidecar.
            from app.mbtiles.reader import MBTilesReader
            with MBTilesReader(task.output_path) as reader:
                data = reader.get_metadata().get('coverage_data', '')
        return coverage_region(data, snapshot_path=str(path), expected_fingerprint=task.boundary_version)
    except (OSError, ValueError) as exc:
        raise ValueError(f"覆盖策略快照不可用，无法安全继续：{exc}") from exc


class CoverageTileFilter:
    """Merge candidate row intervals, never enumerate the national bbox at high Zoom."""

    def __init__(self, region):
        payload = decode_coverage(region.coverage_data)
        self.rules = []
        self._active_cache = {}
        self.name = region.name
        for rule in payload['rules']:
            geometry = shape(rule['geometry'])
            self.rules.append((rule['minZoom'], rule['maxZoom'], GeoBounds(*geometry.bounds), prep(geometry)))

    def active(self, zoom):
        if zoom not in self._active_cache:
            self._active_cache[zoom] = tuple((calculate_tile_range(b, zoom), prepared)
                                             for low, high, b, prepared in self.rules if low <= zoom <= high)
        return self._active_cache[zoom]

    def row_intervals(self, zoom):
        ranges = [r for r, _ in self.active(zoom)]
        if not ranges:
            return
        # Sweep disjoint Y spans too: widely separated mission regions do not scan empty rows.
        boundaries = sorted({v for r in ranges for v in (r.min_y, r.max_y + 1)})
        for y0, y1 in pairwise(boundaries):
            spans = sorted((r.min_x, r.max_x) for r in ranges if r.min_y <= y0 <= r.max_y)
            merged = []
            for left, right in spans:
                if merged and left <= merged[-1][1] + 1:
                    merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
                else:
                    merged.append((left, right))
            if not merged:
                continue
            for y in range(y0, y1):
                for left, right in merged:
                    yield y, left, right

    def candidate_count(self, zoom):
        return sum(right - left + 1 for _, left, right in self.row_intervals(zoom))

    def iter_candidate_tiles(self, bounds, zoom):
        for y, left, right in self.row_intervals(zoom):
            for x in range(left, right + 1):
                yield x, y

    def intersects_tile_xyz(self, zoom, x, y):
        candidates = [prepared for r, prepared in self.active(zoom)
                      if r.min_x <= x <= r.max_x and r.min_y <= y <= r.max_y]
        if not candidates:
            return False
        tile = tile_bounds(zoom, x, y)
        polygon = box(tile.west, tile.south, tile.east, tile.north)
        return any(p.intersects(polygon) for p in candidates)

    def intersects_tile(self, tile):
        return self.intersects_tile_xyz(tile.z, tile.x, tile.y)

    def iter_selected_tiles(self, bounds, zoom):
        for x, y in self.iter_candidate_tiles(bounds, zoom):
            if self.intersects_tile_xyz(zoom, x, y):
                yield x, y
