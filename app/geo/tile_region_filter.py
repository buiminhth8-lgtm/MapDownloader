"""多边形瓦片过滤器（Slice 16）。

回答唯一一个问题：**这张瓦片与行政区域相交吗？**

为什么必须是 ``intersects`` 而不是"瓦片中心点是否在区域内"
----------------------------------------------------------

中心点判据会在边界上漏块。设想区域边界正好切过一张瓦片：中心点落在外侧，
于是这张瓦片被丢弃 —— 而它其实**有一半**是用户要的地。地图上就会出现沿着
行政区边界的一整圈缺口，且缺口位置随边界形状而非规律出现，极难排查。

按 Spec §6/§32，本模块只用 ``prepared.intersects(tile_polygon)``：只要瓦片矩形
与区域有任何一点重叠（哪怕只是擦边），就算命中。代价是外包矩形边缘会多下
若干张瓦片，这是**正确的一侧**——多下的那几张用户看不出问题，少下的会。

性能
----

Shapely 的 ``prepared`` 几何内部建了索引，``intersects`` 比每次现算快一到两个
数量级。**每个任务只 prepare 一次**（在构造函数里），瓦片循环里只做查询。
国家尺度的边界有几十万个顶点，不 prepare 的话逐张瓦片判断能慢到不可接受。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from shapely.geometry import box
from shapely.geometry.base import BaseGeometry
from shapely.prepared import PreparedGeometry, prep

from app.geo.geojson_loader import load_geojson_geometry
from app.geo.region import DownloadRegion
from app.geo.tile_math import calculate_tile_range, tile_bounds

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注，避免 geo 反向依赖 downloader
    from app.downloader.models import TileCoordinate

__all__ = [
    "PolygonTileRegionFilter",
    "ZoomTileFilterStats",
    "build_region_filter",
]


@dataclass(frozen=True)
class ZoomTileFilterStats:
    """单个缩放级别的过滤结果（候选 → 选中）。

    Attributes:
        zoom: 缩放级别。
        candidate_tiles: 外包矩形的瓦片数（未过滤）。
        selected_tiles: 与区域相交的瓦片数。
    """

    zoom: int
    candidate_tiles: int
    selected_tiles: int

    @property
    def filtered_tiles(self) -> int:
        """被过滤掉的张数（``candidate - selected``）。"""
        return max(0, self.candidate_tiles - self.selected_tiles)

    @property
    def filtered_percent(self) -> float:
        """被过滤掉的比例（0~100）；候选为 0 时返回 0.0。"""
        if self.candidate_tiles <= 0:
            return 0.0
        return self.filtered_tiles * 100.0 / self.candidate_tiles


class PolygonTileRegionFilter:
    """基于 Shapely 几何的瓦片过滤器（每个任务构造一次，可反复复用）。

    Attributes:
        name: 区域名（只用于日志与错误信息）。
    """

    def __init__(self, geometry: BaseGeometry, *, name: str = "") -> None:
        """
        Args:
            geometry: 行政区几何（Polygon / MultiPolygon）。
            name: 区域展示名。

        Raises:
            ValueError: 几何为空。prepare 一个空几何不会报错，但之后每张瓦片
                都返回 ``False``，表现为"一片都没下"——这种静默失败必须在
                入口拦掉。
        """
        if geometry is None or geometry.is_empty:
            raise ValueError("PolygonTileRegionFilter 的几何不能为空")
        self._geometry = geometry
        #: **只在这里 prepare 一次**：后续每次 intersects_tile 都复用这份索引。
        self._prepared: PreparedGeometry = prep(geometry)
        self._name = str(name)
        self._tile_count = 0

    # ------------------------------------------------------------------ #
    # 只读属性
    # ------------------------------------------------------------------ #
    @property
    def geometry(self) -> BaseGeometry:
        """原始几何。"""
        return self._geometry

    @property
    def name(self) -> str:
        """区域名。"""
        return self._name

    @property
    def prepared(self) -> PreparedGeometry:
        """预处理的几何（暴露给测试与性能分析）。"""
        return self._prepared

    @property
    def tile_count(self) -> int:
        """本实例已判定的瓦片数（只用于观测，不影响结果）。"""
        return self._tile_count

    # ------------------------------------------------------------------ #
    # 判定
    # ------------------------------------------------------------------ #
    def intersects_tile(self, tile: "TileCoordinate") -> bool:
        """这张瓦片是否与区域相交。

        走鸭子类型（只读 ``tile.z / tile.x / tile.y``）而不是强制
        ``isinstance``：``app.geo`` 是纯计算层，不该 import
        ``app.downloader``（那会把下载调度拉进 geo 的依赖图）。
        """
        return self.intersects_tile_xyz(tile.z, tile.x, tile.y)

    def intersects_tile_xyz(self, zoom: int, x: int, y: int) -> bool:
        """``intersects_tile`` 的坐标版本（供只拿到三个整数的调用方使用）。

        Raises:
            ValueError: ``zoom`` / ``x`` / ``y`` 不构成合法瓦片
                （由 :func:`app.geo.tile_bounds` 抛出）。
        """
        self._tile_count += 1
        bounds = tile_bounds(zoom, x, y)
        tile_polygon = box(bounds.west, bounds.south, bounds.east, bounds.north)
        return bool(self._prepared.intersects(tile_polygon))

    def intersects_bounds(self, bounds) -> bool:
        """整个矩形范围是否与区域有交集（用于"这个范围根本没碰到区域"的快速判断）。"""
        candidate = box(bounds.west, bounds.south, bounds.east, bounds.north)
        return bool(self._prepared.intersects(candidate))

    # ------------------------------------------------------------------ #
    # 批量
    # ------------------------------------------------------------------ #
    def iter_selected_tiles(self, bounds, zoom: int) -> Iterator[tuple[int, int]]:
        """流式产出该级别下**与区域相交**的 ``(x, y)``（XYZ）。

        刻意用生成器而不是返回 list（Spec §9）：全国 z14 的候选瓦片是千万级，
        一次 materialize 就是几百 MB 的常驻内存。调用方（预估 / 下载生产者）
        本来就是逐张消费的。

        Yields:
            ``(x, y)``，顺序为 ``y`` 升序、``x`` 升序（与
            :func:`app.downloader.iter_tile_coordinates` 一致）。
        """
        tile_range = calculate_tile_range(bounds, zoom)
        for y in range(tile_range.min_y, tile_range.max_y + 1):
            for x in range(tile_range.min_x, tile_range.max_x + 1):
                if self.intersects_tile_xyz(zoom, x, y):
                    yield (x, y)

    def count_selected_tiles(self, bounds, zoom: int) -> ZoomTileFilterStats:
        """统计该级别下候选与选中的张数（流式，不落任何列表）。"""
        tile_range = calculate_tile_range(bounds, zoom)
        selected = sum(1 for _ in self.iter_selected_tiles(bounds, zoom))
        return ZoomTileFilterStats(
            zoom=zoom,
            candidate_tiles=tile_range.tile_count,
            selected_tiles=selected,
        )


def build_region_filter(
    region: DownloadRegion | None,
    *,
    geometry: BaseGeometry | None = None,
) -> PolygonTileRegionFilter | None:
    """为一个下载区域构造过滤器。

    Returns:
        - 矩形区域 / ``region`` 为 ``None`` → ``None``（表示"不过滤"，
          调用方按原矩形链路走，行为与 Slice 0~15 完全一致）；
        - 多边形区域 → :class:`PolygonTileRegionFilter`。

    Args:
        region: 下载区域。
        geometry: 已经加载好的几何（避免重复读盘）。

    Raises:
        GeoJSONError: 边界文件不存在（``GeoJSONNotFoundError``）或内容不合法
            （``GeoJSONParseError`` / ``GeoJSONGeometryError``）。
    """
    if region is None or not region.is_polygon:
        return None
    if geometry is None:
        if not region.geojson_path:
            raise ValueError(
                f"多边形区域 {region.name!r} 缺少 geojson_path，无法构造过滤器"
            )
        geometry = load_geojson_geometry(region.geojson_path)
    return PolygonTileRegionFilter(geometry, name=region.name)
