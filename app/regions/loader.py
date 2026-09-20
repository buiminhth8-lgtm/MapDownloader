"""区域加载器（Slice 18，Spec §4 / §5 / §11）。

目录（:class:`~app.regions.RegionCatalog`）回答"有哪些区域、谁是谁的上级"，
本模块负责下一步：**把一条目录记录变成一个能真正下载的区域**。

::

    RegionPreset（目录条目）
        ↓ RegionLoader.peek_path / resolve_path    路径安全 + 存在性
        ↓ load_document                            读 GeoJSON（lazy，Spec §5）
        ↓ geometry_to_bounds                       真实外包矩形（Spec §11）
    DownloadRegion（Slice 16 的统一区域模型）        ↓ 交给已有的下载链路

三件事特意分开
--------------

1. **算路径**（:meth:`RegionLoader.peek_path`）与**查存在**
   （:meth:`RegionLoader.resolve_path`）不是一个方法：UI 需要在"文件还没放"
   的时候也能把"该放哪、叫什么"说清楚，那时候路径必须算得出来但不能抛异常。
2. **读几何**（:meth:`RegionLoader.load_geometry`）与**构造下载区域**
   （:meth:`RegionLoader.to_download_region`）分开：调用方常常已经有一份几何了
   （面板为了显示范围读过一次），传 ``geometry=`` / ``document=`` 就能免掉第二次
   读盘 —— 这同时也保证"界面上显示的区域"和"实际裁剪的区域"来自同一次读取。
3. **范围一律从几何算**（Spec §11）：目录里那个可选的 ``bounds`` 只当"声明值"，
   :meth:`RegionLoader.to_download_region` 在没有几何时**一定去读文件**，
   绝不会拿一个手填的矩形（比如 73/3/136/54）顶替多边形真实范围 ——
   那会让候选瓦片数量与真实边界对不上，而用户从界面上看不出来。

本模块不缓存 bounds。"先缓存、靠 boundaryVersion 失效"看起来省事，实际上
估算前本来就必须加载几何（:class:`~app.geo.RegionTileEstimator` 要 prepared
一份几何做 ``intersects``），缓存只在"只想知道范围"的那一条路径上省一次读盘，
却引入一份可能过期的数据（Spec §11 最后一句正是在防这个）。因此这里选择了
**不缓存**这条更简单的路。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.geo import region as geo_region
from app.geo.bounds import GeoBounds
from app.geo.geojson_loader import (
    GeoJSONDocument,
    geometry_to_bounds,
    load_geojson_document,
)
from app.regions.catalog import RegionCatalog
from app.regions.models import RegionCatalogError, RegionPreset

__all__ = ["RegionLoader"]


class RegionLoader:
    """把目录条目变成几何 / 下载区域（Spec §4）。

    Attributes:
        catalog: 数据来源。
        root: 资源根目录；缺省跟随 ``catalog.root``（它自己会跟随全局，
            Spec §5 的 lazy load 与资源重定向都靠这条链）。
    """

    def __init__(self, catalog: RegionCatalog, *, root: Path | None = None) -> None:
        if not isinstance(catalog, RegionCatalog):
            raise ValueError(
                f"catalog 必须是 RegionCatalog，实际为 {type(catalog).__name__}"
            )
        self._catalog = catalog
        self._explicit_root = Path(root) if root is not None else None

    # ------------------------------------------------------------------ #
    # 只读属性
    # ------------------------------------------------------------------ #
    @property
    def catalog(self) -> RegionCatalog:
        """数据来源。"""
        return self._catalog

    @property
    def root(self) -> Path:
        """资源根目录。"""
        if self._explicit_root is not None:
            return self._explicit_root.resolve()
        return self._catalog.root

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def region(self, region_id: str | None) -> RegionPreset:
        """取目录条目。

        Raises:
            RegionCatalogError: 目录里没有这个 id。
        """
        return self._catalog.require(region_id)

    def peek_path(self, region_id: str | None) -> Path:
        """算边界文件的规范路径，**不检查它是否存在**。

        UI 在"文件还没放"时也要说清"该放哪"，所以这条路必须能走通。

        Raises:
            RegionCatalogError: id 不在目录里。
            ValueError: 路径越界（Spec §12）。
        """
        return self.region(region_id).path_under(self.root)

    def resolve_path(self, region_id: str | None) -> Path:
        """算路径并确认文件存在。

        Raises:
            RegionCatalogError: id 不在目录里。
            RegionBoundaryMissing: 文件不存在（消息里直接写清放哪、叫什么）。
        """
        path = self.peek_path(region_id)
        if not path.is_file():
            raise geo_region.RegionBoundaryMissing(
                self.missing_message(region_id), path=str(path)
            )
        return path

    def available(self, region_id: str | None) -> bool:
        """边界文件是否已就位（不抛异常：目录里没这个 id 也只是"不可用"）。"""
        try:
            return self.peek_path(region_id).is_file()
        except (RegionCatalogError, ValueError):
            return False

    def missing_message(self, region_id: str | None) -> str:
        """文件缺失时给用户看的完整说明（位置 + 授权要求）。

        文案由 Slice 16 的 :meth:`app.geo.region.RegionPreset.missing_message`
        生成：同一个区域可能在两条路径上被问到（面板走目录、续传走扁平预设），
        两处必须一字不差，否则用户会看到两种说法。
        """
        return self.to_geo_preset(self.region(region_id)).missing_message()

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #
    def load_document(
        self, region_id: str | None, *, attempt_repair: bool = False
    ) -> GeoJSONDocument:
        """读边界文件（Spec §4 / §5）。

        Raises:
            RegionCatalogError: id 不在目录里。
            RegionBoundaryMissing: 文件不存在。
            GeoJSONError: 文件不合法（由加载器抛出）。
        """
        return load_geojson_document(
            self.resolve_path(region_id), attempt_repair=attempt_repair
        )

    def load_geometry(self, region_id: str | None, *, attempt_repair: bool = False) -> Any:
        """只取几何（丢掉 properties 里的其余内容）。"""
        return self.load_document(region_id, attempt_repair=attempt_repair).geometry

    def load_bounds(self, region_id: str | None, *, attempt_repair: bool = False) -> GeoBounds:
        """真实外包矩形（Spec §11：从几何算，不用手填的值）。"""
        return geometry_to_bounds(
            self.load_geometry(region_id, attempt_repair=attempt_repair)
        )

    def boundary_version(self, region_id: str | None, document: GeoJSONDocument | None = None) -> str:
        """边界版本：目录里的声明优先，其次文件自带的 ``properties``。

        目录里写 ``boundaryVersion`` 表示"这批数据的版本由部署方认定"；
        没写就以数据自带的为准 —— 两条都空就是空串（老行为）。
        """
        entry = self.region(region_id)
        if entry.boundary_version:
            return entry.boundary_version
        if document is not None:
            return document.boundary_version
        return ""

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #
    def to_download_region(
        self,
        region_id: str | None,
        *,
        bounds: GeoBounds | None = None,
        document: GeoJSONDocument | None = None,
        geometry: Any = None,
        name: str | None = None,
    ) -> geo_region.DownloadRegion:
        """目录条目 → :class:`~app.geo.DownloadRegion`（Spec §4）。

        Args:
            region_id: 目录里的 id。
            bounds: 已经算好的外包矩形；给了就**不再读文件**（Spec §11 允许，
                但调用方要自己保证它来自几何，而不是手填的范围）。
            document: 已经加载好的文档；给了就复用它取范围与版本。
            geometry: 已经加载好的几何（与 ``document`` 二选一，``document`` 优先）。
            name: 覆盖展示名（默认用目录里的 ``name``）。

        Raises:
            RegionCatalogError: id 不在目录里。
            RegionBoundaryMissing: 需要读文件但它不存在。
            GeoJSONError: 文件不合法。
            ValueError: 几何为空（``geometry_to_bounds`` 抛）。
        """
        entry = self.region(region_id)
        doc = document
        if geometry is None and doc is not None:
            geometry = doc.geometry
        if geometry is None and bounds is None:
            doc = self.load_document(entry.id)
            geometry = doc.geometry
        if bounds is None:
            bounds = geometry_to_bounds(geometry)
        if doc is None:
            version = entry.boundary_version or ""
        else:
            version = self.boundary_version(entry.id, doc)

        return geo_region.DownloadRegion(
            region_type=geo_region.RegionType.POLYGON,
            name=name if name is not None else entry.name,
            bounds=bounds,
            region_id=entry.id,
            boundary_version=version or None,
            geojson_path=str(self.peek_path(entry.id)),
            # 等级只从目录来（Spec §9）：Zoom 建议由等级驱动，不在别处再存一份
            region_level=entry.level,
        )

    def to_geo_preset(self, entry: RegionPreset) -> geo_region.RegionPreset:
        """目录条目 → Slice 16 的扁平预设（下拉框与错误文案用）。

        这是两个 ``RegionPreset`` 之间**唯一**的转换方向（见
        ``app/regions/models.py`` 的模块文档）。转出来的预设带着本加载器的
        ``root``，因此它的 :attr:`~app.geo.region.RegionPreset.geojson_path`
        与目录算出来的路径**必然**是同一个文件。
        """
        return geo_region.RegionPreset(
            region_id=entry.id,
            name=entry.name,
            geojson_filename=entry.geojson_path,
            default_boundary_version=entry.boundary_version,
            region_type=geo_region.RegionType.POLYGON,
            region_level=entry.level,
            root=self.root,
        )

    def geo_preset(self, region_id: str | None) -> geo_region.RegionPreset:
        """按 id 取扁平预设（等价于 ``to_geo_preset(region(id))``）。"""
        return self.to_geo_preset(self.region(region_id))

    # ------------------------------------------------------------------ #
    # 展示
    # ------------------------------------------------------------------ #
    def describe(self, region_id: str | None) -> str:
        """一行说明，如 ``中国（全国级）· country/china.geojson``。"""
        entry = self.region(region_id)
        return f"{entry.describe()} · {entry.geojson_path}"
