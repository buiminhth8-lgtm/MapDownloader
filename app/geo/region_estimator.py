"""区域瓦片预估（Slice 16，Spec §8）。

回答"这次要下多少张、比矩形少下多少"，并且**处处使用 ``selected`` 而不是
``candidate``**：

- 进度条分母、大任务确认阈值、容量预估、WMTS 请求量 —— 全部以
  :attr:`RegionTileEstimate.selected_tiles` 为准。

用 candidate 的后果不是"数字大一点"那么简单：用户看到"要下 4.2 万张"，
实际下到 2.8 万张就结束了，会以为程序漏下了 1.4 万张。

预估是**流式**的（Spec §9）
---------------------------

逐 zoom、逐行、逐列地判断，不构造任何全量瓦片列表。全国 z10~z14 的候选瓦片
以千万计，先 materialize 再过滤会直接把内存吃光。唯一的例外是**外包矩形本身**
（:class:`~app.geo.TileRange`），它只存四个整数，与瓦片总数无关。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config.settings import DEFAULT_AVERAGE_TILE_SIZE
from app.geo.region import DownloadRegion, RegionType
from app.geo.tile_math import (
    calculate_tile_range,
    estimate_size_bytes,
    format_size,
    validate_zoom,
)
from app.geo.tile_region_filter import (
    PolygonTileRegionFilter,
    build_region_filter,
)

__all__ = [
    "ZoomRegionStats",
    "RegionTileEstimate",
    "RegionTileEstimator",
    "estimate_region_tiles",
]


@dataclass(frozen=True)
class ZoomRegionStats:
    """单个缩放级别的"候选 → 选中"结果（Spec §8）。

    Attributes:
        zoom: 缩放级别。
        candidate_tiles: 外包矩形里的瓦片数。
        selected_tiles: 与区域真正相交的瓦片数。矩形模式下与候选相等。
        filtered_tiles: 被过滤掉的张数。
    """

    zoom: int
    candidate_tiles: int
    selected_tiles: int
    filtered_tiles: int

    @property
    def filtered_percent(self) -> float:
        """过滤比例（0~100）；候选为 0 时返回 0.0。"""
        if self.candidate_tiles <= 0:
            return 0.0
        return self.filtered_tiles * 100.0 / self.candidate_tiles


@dataclass(frozen=True)
class RegionTileEstimate:
    """一次区域预估的完整结论（Spec §8）。

    Attributes:
        region_name: 区域展示名。
        region_type: 区域类型。
        candidate_tiles: 外包矩形瓦片数（"如果不裁剪会有多少"）。
        selected_tiles: 真正需要下载的瓦片数（**所有下游计算都用它**）。
        filtered_tiles: 被多边形裁掉的张数。
        filtered_percent: 过滤比例（0~100）。
        estimated_size_bytes: 按平均瓦片体积粗估的下载量。
        per_zoom: 逐缩放级别明细，顺序为 zoom 升序。
    """

    region_name: str
    region_type: RegionType
    candidate_tiles: int
    selected_tiles: int
    filtered_tiles: int
    filtered_percent: float
    estimated_size_bytes: int
    per_zoom: tuple[ZoomRegionStats, ...] = field(default_factory=tuple)

    # ------------------------------------------------------------------ #
    # 派生
    # ------------------------------------------------------------------ #
    @property
    def is_polygon(self) -> bool:
        """是否经过多边形裁剪。"""
        return self.region_type is RegionType.POLYGON

    @property
    def size_text(self) -> str:
        """格式化后的容量文本（如 ``361.93 MB``）。"""
        return format_size(self.estimated_size_bytes)

    @property
    def filtered_percent_text(self) -> str:
        """过滤比例的展示文本（如 ``33.7%``）。"""
        return f"{self.filtered_percent:.1f}%"

    def requests(self, per_tile: int = 1) -> int:
        """预计 WMTS 请求次数 = 选中张数 × 每张请求数。

        开启中文注记后每张瓦片要请求底图 + 注记两次，因此 ``per_tile=2``。
        """
        return self.selected_tiles * max(0, int(per_tile))

    def as_lines(self, *, per_tile: int = 1, zoom_detail: bool = True) -> list[str]:
        """多行展示文案（GUI 预估区 / 日志共用）。

        行序与 Spec §17 一致：区域 → 候选 → 区域内 → 过滤 → 比例 → 容量 → 请求，
        最后是逐 zoom 明细。矩形模式下**不出现**候选/过滤三行（那三行对矩形
        恒等于"候选 = 选中、过滤 = 0"，纯属噪音）。
        """
        lines = [f"区域：{self.region_name}"]
        if self.is_polygon:
            lines.extend(
                [
                    f"候选瓦片：{self.candidate_tiles:,}",
                    f"区域内瓦片：{self.selected_tiles:,}",
                    f"过滤瓦片：{self.filtered_tiles:,}",
                    f"过滤比例：{self.filtered_percent_text}",
                ]
            )
        else:
            lines.append(f"预计瓦片：{self.selected_tiles:,}")
        lines.extend(
            [
                f"预计容量：{self.size_text}",
                f"WMTS 请求：{self.requests(per_tile):,}",
            ]
        )
        if zoom_detail and self.per_zoom:
            lines.append("Zoom 明细：")
            lines.extend(
                f"  z{item.zoom}  候选 {item.candidate_tiles:,}  "
                f"选中 {item.selected_tiles:,}  过滤 {item.filtered_tiles:,}"
                for item in self.per_zoom
            )
        return lines


class RegionTileEstimator:
    """按 :class:`~app.geo.DownloadRegion` 统计各级别候选 / 选中瓦片数。

    一个实例可反复调用 :meth:`estimate`（过滤器只构造一次，prepared 索引复用）。

    Attributes:
        region: 下载区域。
    """

    def __init__(
        self,
        region: DownloadRegion,
        *,
        average_tile_size: int = DEFAULT_AVERAGE_TILE_SIZE,
        region_filter: PolygonTileRegionFilter | None = None,
        geometry=None,
    ) -> None:
        """
        Args:
            region: 下载区域。
            average_tile_size: 单张瓦片平均字节数（预估容量用）。
            region_filter: 已经构造好的过滤器；``None`` 时会按需构造
                （多边形需要读边界文件）。
            geometry: 已经加载的几何（避免重复读盘）。

        Raises:
            ValueError: ``region`` 不是 :class:`~app.geo.DownloadRegion`。
            GeoJSONError: 多边形边界文件不可读 / 不合法。
        """
        if not isinstance(region, DownloadRegion):
            raise ValueError(
                f"region 必须是 DownloadRegion，实际为 {type(region).__name__}"
            )
        self._region = region
        self._average_tile_size = int(average_tile_size)
        if region_filter is not None:
            self._filter: PolygonTileRegionFilter | None = region_filter
        else:
            self._filter = build_region_filter(region, geometry=geometry)

    # ------------------------------------------------------------------ #
    # 只读属性
    # ------------------------------------------------------------------ #
    @property
    def region(self) -> DownloadRegion:
        """下载区域。"""
        return self._region

    @property
    def region_filter(self) -> PolygonTileRegionFilter | None:
        """过滤器；矩形区域为 ``None``。"""
        return self._filter

    # ------------------------------------------------------------------ #
    # 预估
    # ------------------------------------------------------------------ #
    def estimate(self, min_zoom: int, max_zoom: int) -> RegionTileEstimate:
        """统计 ``min_zoom ~ max_zoom``（含两端）的候选 / 选中瓦片。

        矩形区域：``selected == candidate``（**不调用任何几何运算**，
        与 Slice 0~15 的数字逐位一致）。

        Raises:
            ValueError: zoom 非法，或 ``min_zoom > max_zoom``。
        """
        validate_zoom(min_zoom)
        validate_zoom(max_zoom)
        if min_zoom > max_zoom:
            raise ValueError(
                f"min_zoom 不能大于 max_zoom，实际 min_zoom={min_zoom}, max_zoom={max_zoom}"
            )

        bounds = self._region.bounds
        per_zoom: list[ZoomRegionStats] = []
        candidate_total = 0
        selected_total = 0

        for zoom in range(int(min_zoom), int(max_zoom) + 1):
            tile_range = calculate_tile_range(bounds, zoom)
            candidate = tile_range.tile_count
            if self._filter is None:
                selected = candidate
            else:
                # 流式逐张判定：不落地任何瓦片列表（Spec §9）
                selected = sum(1 for _ in self._filter.iter_selected_tiles(bounds, zoom))
            per_zoom.append(
                ZoomRegionStats(
                    zoom=zoom,
                    candidate_tiles=candidate,
                    selected_tiles=selected,
                    filtered_tiles=max(0, candidate - selected),
                )
            )
            candidate_total += candidate
            selected_total += selected

        filtered_total = max(0, candidate_total - selected_total)
        percent = (filtered_total * 100.0 / candidate_total) if candidate_total else 0.0
        return RegionTileEstimate(
            region_name=self._region.name,
            region_type=self._region.region_type,
            candidate_tiles=candidate_total,
            selected_tiles=selected_total,
            filtered_tiles=filtered_total,
            filtered_percent=percent,
            estimated_size_bytes=estimate_size_bytes(
                selected_total, self._average_tile_size
            ),
            per_zoom=tuple(per_zoom),
        )


def estimate_region_tiles(
    region: DownloadRegion,
    min_zoom: int,
    max_zoom: int,
    *,
    average_tile_size: int = DEFAULT_AVERAGE_TILE_SIZE,
    geometry=None,
) -> RegionTileEstimate:
    """便捷入口：一次性预估，不需要保留 estimator 实例。

    需要反复预估（例如 UI 里用户来回改 zoom）时请直接持有
    :class:`RegionTileEstimator`：边界文件的加载与 prepared 索引都在
    构造函数里完成，重复构造的代价与边界规模成正比。
    """
    return RegionTileEstimator(
        region, average_tile_size=average_tile_size, geometry=geometry
    ).estimate(min_zoom, max_zoom)
