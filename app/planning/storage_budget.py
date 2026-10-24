"""Slice 37：离线抽样、逐级新增容量预测与预算建议，不发网络请求。"""
from __future__ import annotations

import math
import shutil
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from app.geo import DownloadRegion, RegionTileEstimator, calculate_tile_range, format_size
from app.geo.tile_region_filter import build_region_filter
from app.models import MapType

GIB = 1024 ** 3
DEFAULT_BUDGET_GIB = 64.0
DEFAULT_RESERVE_GIB = 2.0
SAMPLES_PER_ZOOM = 24
MAX_SAMPLE_FILES = 12
DATABASE_OVERHEAD = 1.08
DATABASE_BASE_BYTES = 24 * 1024


class BudgetCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class BudgetSettings:
    enabled: bool = True
    budget_gib: float = DEFAULT_BUDGET_GIB
    reserve_gib: float = DEFAULT_RESERVE_GIB
    packaging_copy: bool = False

    def __post_init__(self):
        if not isinstance(self.enabled, bool) or not isinstance(self.packaging_copy, bool):
            raise ValueError("预算开关必须是布尔值")
        for value in (self.budget_gib, self.reserve_gib):
            if not math.isfinite(value) or value < 0:
                raise ValueError("预算与保留空间必须为有限非负数")
        if self.budget_gib <= 0:
            raise ValueError("本次地图预算必须大于零")


@dataclass(frozen=True)
class BudgetInput:
    id: str
    name: str
    region: DownloadRegion
    min_zoom: int
    max_zoom: int
    output_path: Path
    map_type: MapType
    with_labels: bool
    counts: tuple = ()
    resume: bool = True


@dataclass(frozen=True)
class ZoomPrediction:
    zoom: int
    total: int
    existing: int
    mean_bytes: float
    sample_count: int
    source: str
    uncertainty: float

    @property
    def missing(self):
        return max(0, self.total - self.existing)

    @property
    def new_bytes(self):
        return math.ceil(self.missing * self.mean_bytes * DATABASE_OVERHEAD)

    @property
    def upper_bytes(self):
        return math.ceil(self.new_bytes * self.uncertainty)


@dataclass
class ItemPrediction:
    request: BudgetInput
    zooms: tuple[ZoomPrediction, ...]
    existing_file_bytes: int = 0

    @property
    def new_bytes(self):
        total = sum(z.new_bytes for z in self.zooms)
        metadata_bytes = len(self.request.region.coverage_data.encode("ascii"))
        return total + (DATABASE_BASE_BYTES + metadata_bytes if total and not self.existing_file_bytes else 0)

    @property
    def upper_bytes(self):
        total = sum(z.upper_bytes for z in self.zooms)
        metadata_bytes = len(self.request.region.coverage_data.encode("ascii"))
        return total + (DATABASE_BASE_BYTES + metadata_bytes if total and not self.existing_file_bytes else 0)

    @property
    def final_bytes(self):
        return self.existing_file_bytes + self.new_bytes

    @property
    def final_upper_bytes(self):
        return self.existing_file_bytes + self.upper_bytes

    @property
    def missing_tiles(self):
        return sum(z.missing for z in self.zooms)


@dataclass(frozen=True)
class BudgetSuggestion:
    item_id: str
    name: str
    max_zoom: int
    saved_bytes: int

    def describe(self):
        return f"{self.name}：最高级别降到 Z{self.max_zoom}，保守估计可少预留 {format_size(self.saved_bytes)} 新增空间"


@dataclass
class BudgetReport:
    settings: BudgetSettings
    items: tuple[ItemPrediction, ...]
    # 每个磁盘/挂载点独立计算，防止把 D: 空间套用到 C:。
    disks: dict[str, tuple[int | None, int]] = field(default_factory=dict)
    suggestions: tuple[BudgetSuggestion, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def new_bytes(self):
        return sum(i.new_bytes for i in self.items)

    @property
    def final_bytes(self):
        return sum(i.final_bytes for i in self.items)

    @property
    def final_upper_bytes(self):
        return sum(i.final_upper_bytes for i in self.items)

    @property
    def requests(self):
        return sum(i.missing_tiles * i.request.map_type.request_count_per_tile(i.request.with_labels)
                   for i in self.items)

    @property
    def over_budget(self):
        return self.final_upper_bytes > self.settings.budget_gib * GIB

    @property
    def disk_ok(self):
        return all(free is not None and free >= required for free, required in self.disks.values())

    @property
    def needs_confirmation(self):
        return self.over_budget or not self.disk_ok

    def as_lines(self):
        lines = [
            f"本次下载/计划预算：{self.settings.budget_gib:g} GiB（不含其他任务的全局配额）",
            f"新增瓦片：{sum(i.missing_tiles for i in self.items):,} 张；预计 HTTP 请求：{self.requests:,} 次（不含重试）",
            f"预计新增文件容量：{format_size(self.new_bytes)}",
            f"目标文件合计：约 {format_size(self.final_bytes)}；保守估计 {format_size(self.final_upper_bytes)}",
            "预算状态：" + ("超过保守估计预算，请调整或明确继续" if self.over_budget else "预算内"),
        ]
        for disk, (free, required) in self.disks.items():
            lines.append(f"磁盘 {disk}：可用 {format_size(free) if free is not None else '无法检测'}；"
                         f"本轮需预留 {format_size(required)}（含 {self.settings.reserve_gib:g} GiB 保留空间）")
        if self.settings.packaging_copy:
            lines.append("空间预留包含同盘生成一份完整打包副本；异盘打包须另查目标盘空间")
        else:
            lines.append("空间预留仅包含下载；如需同盘打包，请勾选预留打包副本")
        for item in self.items:
            mode = "卫星" if item.request.map_type is MapType.IMAGE else "矢量"
            lines.append(f"\n{item.request.name} · {mode} · {'合成注记' if item.request.with_labels else '纯底图'}")
            for z in item.zooms:
                lines.append(f"  Z{z.zoom}：目标 {z.total:,}，已有 {z.existing:,}，新增 {z.missing:,}；"
                             f"约 {z.mean_bytes / 1024:.1f} KiB/张，{z.source}（样本 {z.sample_count}）")
        lines.extend("建议 · " + s.describe() for s in self.suggestions)
        if self.over_budget and self.items:
            largest = max(self.items, key=lambda i: i.upper_bytes)
            lines.append(f"建议 · 优先缩小「{largest.request.name}」的高清区域或航线缓冲半径，调整后重新预测")
        lines.extend(self.notes)
        return lines


def _check(cancel):
    if cancel is not None and cancel():
        raise BudgetCancelled("容量预测已取消")


def _open_readonly(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _matches(metadata, request):
    labels = str(metadata.get("labels", "false")).lower() in {"true", "1"}
    composition = metadata.get("composition", "single")
    return (metadata.get("map_type") == request.map_type.value and labels == request.with_labels
            and composition == ("merged" if request.with_labels else "single")
            and metadata.get("format") in ({"png"} if request.with_labels else {"png", "jpg", "jpeg"}))


def _samples(paths, request, cancel):
    by_zoom = defaultdict(list)
    seen = set()
    for path in paths:
        _check(cancel)
        path = Path(path).resolve()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        if len(seen) > MAX_SAMPLE_FILES:
            break
        connection = None
        try:
            connection = _open_readonly(path)
            if not _matches(dict(connection.execute("SELECT name,value FROM metadata")), request):
                continue
            for zoom in range(request.min_zoom, request.max_zoom + 1):
                _check(cancel)
                first = connection.execute(
                    "SELECT tile_column FROM tiles WHERE zoom_level=? ORDER BY tile_column LIMIT 1", (zoom,)
                ).fetchone()
                if first is None:
                    continue
                last = connection.execute(
                    "SELECT tile_column FROM tiles WHERE zoom_level=? ORDER BY tile_column DESC LIMIT 1", (zoom,)
                ).fetchone()
                extent = first[0], last[0]
                keys = set()
                # 分列取样，避免只取文件开头；仅查询长度，不读取整块图片 BLOB。
                for j in range(SAMPLES_PER_ZOOM):
                    _check(cancel)
                    column = round(extent[0] + (extent[1] - extent[0]) * j / (SAMPLES_PER_ZOOM - 1))
                    row = connection.execute(
                        "SELECT tile_column,tile_row,length(tile_data) FROM tiles "
                        "WHERE zoom_level=? AND tile_column>=? ORDER BY tile_column,tile_row LIMIT 1",
                        (zoom, column),
                    ).fetchone()
                    if row and row[2] > 32 and row[:2] not in keys:
                        keys.add(row[:2])
                        by_zoom[zoom].append(row[2])
        except (sqlite3.DatabaseError, OSError):
            continue
        finally:
            if connection is not None:
                connection.close()
    return by_zoom


def _existing(request, counts, cancel):
    path = Path(request.output_path)
    existing = defaultdict(int)
    if not request.resume or not path.is_file():
        return existing, 0
    connection = _open_readonly(path)
    try:
        metadata = dict(connection.execute("SELECT name,value FROM metadata"))
        if metadata.get("map_type") and not _matches(metadata, request):
            raise ValueError(f"已有文件地图类型或注记方式不匹配：{path}")
        region_filter = build_region_filter(request.region)
        for zoom in counts:
            _check(cancel)
            bounds = calculate_tile_range(request.region.bounds, zoom)
            rows = connection.execute(
                "SELECT tile_column,tile_row FROM tiles WHERE zoom_level=? "
                "AND tile_column BETWEEN ? AND ? AND tile_row BETWEEN ? AND ?",
                (zoom, bounds.min_x, bounds.max_x, (1 << zoom) - 1 - bounds.max_y,
                 (1 << zoom) - 1 - bounds.min_y),
            )
            for index, (x, tms_y) in enumerate(rows):
                if index % 256 == 0:
                    _check(cancel)
                if region_filter is None or region_filter.intersects_tile_xyz(zoom, x, (1 << zoom) - 1 - tms_y):
                    existing[zoom] += 1
    finally:
        connection.close()
    wal = path.with_name(path.name + "-wal")
    size = path.stat().st_size + (wal.stat().st_size if wal.is_file() else 0)
    return existing, size


def _disk(path):
    path = Path(path).resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    # Windows 各盘分别汇总；POSIX 按文件系统设备号汇总。
    key = path.anchor if path.drive else f"filesystem:{path.stat().st_dev}"
    try:
        return key, shutil.disk_usage(path).free
    except OSError:
        return key, None


def assess_budget(requests, settings=None, *, should_cancel=None, progress=None, disk_probe=None):
    settings = settings or BudgetSettings()
    requests = tuple(requests)
    if not requests:
        raise ValueError("没有可预测的下载任务")
    outputs = [Path(r.output_path).resolve() for r in requests]
    if len(set(outputs)) != len(outputs):
        raise ValueError("多个预算条目不能指向同一个输出文件")
    paths = list(outputs)
    for directory in sorted({p.parent for p in paths}, key=str):
        if directory.is_dir():
            paths.extend(sorted(directory.glob("*.mbtiles"))[:MAX_SAMPLE_FILES])
    items = []
    disk_totals = {}
    disk_probe = disk_probe or _disk
    for index, request in enumerate(requests):
        _check(should_cancel)
        if progress is not None:
            progress(f"正在预测 {index + 1}/{len(requests)}：{request.name}")
        counts = {s.zoom: s.selected_tiles for s in request.counts}
        if not counts:
            estimate = RegionTileEstimator(request.region).estimate(
                request.min_zoom, request.max_zoom, should_cancel=should_cancel
            )
            counts = {s.zoom: s.selected_tiles for s in estimate.per_zoom}
        if any(z < request.min_zoom or z > request.max_zoom or n < 0 for z, n in counts.items()):
            raise ValueError("逐级瓦片统计与请求的 Zoom 范围不一致")
        existing, size = _existing(request, counts, should_cancel)
        samples = _samples(paths, request, should_cancel)
        zooms = []
        for zoom, total in sorted(counts.items()):
            values = samples.get(zoom, [])
            if values:
                mean = sum(values) / len(values)
                # 样本少或离散度高时加大保守范围，不能把抽样当成容量保证。
                spread = max(values) / mean
                uncertainty = max(1.3 if len(values) >= 8 else 1.8, min(3, spread))
                source = "本地同模式同级别抽样"
            else:
                mean = (128 if request.with_labels else 96) * 1024 if request.map_type is MapType.IMAGE else 30 * 1024
                uncertainty, source = 2.0, "默认模型，缺少同级别样本"
            zooms.append(ZoomPrediction(zoom, total, min(total, existing[zoom]), mean,
                                        len(values), source, uncertainty))
        item = ItemPrediction(request, tuple(zooms), size)
        items.append(item)
        disk, free = disk_probe(request.output_path.parent)
        required = item.upper_bytes + (item.final_upper_bytes if settings.packaging_copy else 0)
        old_free, old_required = disk_totals.get(disk, (free, 0))
        disk_totals[disk] = (min(old_free, free) if old_free is not None and free is not None else None,
                             old_required + required)
    disks = {key: (free, math.ceil(required + settings.reserve_gib * GIB))
             for key, (free, required) in disk_totals.items()}
    suggestions = []
    for item in items:
        saved = 0
        last_saved = 0
        for zoom in sorted(item.zooms, key=lambda z: z.zoom, reverse=True):
            if zoom.zoom <= item.request.min_zoom:
                break
            saved += zoom.upper_bytes
            if saved > last_saved:
                last_saved = saved
                suggestions.append(BudgetSuggestion(item.request.id, item.request.name, zoom.zoom - 1, saved))
    _check(should_cancel)
    return BudgetReport(settings, tuple(items), disks, tuple(sorted(suggestions, key=lambda s: s.saved_bytes, reverse=True)), (
        "预测不是容量保证：区域内容、源影像、编码与数据库增长均会影响实际大小。",
        "已有文件保留不删除；降低 Zoom 只减少待下载部分，不回收已经写入的瓦片。",
        "损坏瓦片重下载、源站更新与 HTTP 重试未计入新增坐标请求数。",
    ))
