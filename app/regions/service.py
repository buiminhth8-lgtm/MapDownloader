"""区域业务入口（Slice 18，Spec §10）。

界面与上层只需要认识这一个类：它能回答"中国下面有哪些省"、"杭州市的边界在哪"、
"这个区域建议用哪个 Zoom"，也能直接给出一个可下载的
:class:`~app.geo.DownloadRegion`。把目录、加载器、策略服务三件事收在一个入口后面，
是为了将来做 Slice 19 的级联选择界面时，GUI 不必同时持有三个对象、也不会有机会
绕开层级规则自己去扫目录。

Zoom 建议**不在这一层算**（Spec §9）：这里只把目录里的
:attr:`~app.regions.RegionPreset.level` 交给
:class:`~app.planning.ZoomStrategyService`，区间永远来自
:data:`~app.planning.DEFAULT_ZOOM_POLICIES` 那张唯一权威表。
:meth:`RegionService.recommend_with_estimate` 里的等级**取自区域对象本身**
（``region.region_level``），不是另传一个参数 —— 这样"下载的区域"和"算建议用的等级"
在物理上就是同一个字段，不存在传错的可能。

目录读不出来时怎么办
--------------------

``index.json`` 是本项目**自带的元数据文件**，正常随程序分发。万一它缺失或损坏
（用户手工删了、打包漏了），整个"行政区域"功能不该跟着炸掉：

- :attr:`RegionService.catalog_error` 会记下原因（同时打一条 ERROR 级日志）；
- 目录退化成**由内置扁平预设合成**的那一份（当前就是 ``china`` 这一条），
  于是下拉框仍然有"中国"可选，下载、续传、Inspector 全都不受影响，
  连"中国是全国级 → 推荐 Z4~10"这条建议也照旧；
- 但**层级查询**只会看到那一条：省、市的列表是空的 —— 没有元数据就真的没有
  层级，这里不编造。

这不是"静默降级"：日志里有 ERROR，:attr:`RegionService.catalog_error` 可查，
``tests/test_region_catalog.py`` 也有一条用例专门走这条路。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.config.settings import DEFAULT_AVERAGE_TILE_SIZE
from app.geo import region as geo_region
from app.geo.bounds import GeoBounds
from app.geo.geojson_loader import GeoJSONDocument
from app.planning.zoom_strategy import ZoomPlanEstimate, ZoomStrategyService
from app.regions.catalog import (
    RegionCatalog,
    effective_region_root,
    region_resource_root,
)
from app.regions.loader import RegionLoader
from app.regions.models import CatalogValidationResult, RegionCatalogError, RegionPreset
from app.regions.validator import validate_catalog

if TYPE_CHECKING:  # pragma: no cover - 只给类型检查器看
    from app.planning.models import ZoomRecommendation

__all__ = [
    "RegionService",
    "install_region_catalog_resolver",
    "uninstall_region_catalog_resolver",
]

_logger = logging.getLogger(__name__)


class RegionService:
    """行政区域的唯一业务入口（Spec §10）。

    Attributes:
        strategy: Zoom 策略服务（可注入，测试里用来固定策略表）。
    """

    def __init__(
        self,
        catalog: RegionCatalog | None = None,
        *,
        root: Path | None = None,
        strategy: ZoomStrategyService | None = None,
        prefer_generated: bool = False,
    ) -> None:
        if catalog is not None and root is not None:
            raise ValueError("catalog 与 root 只能给一个：给了目录就不该再指定资源目录")
        self._explicit_catalog = catalog
        self._explicit_root = Path(root) if root is not None else None
        self._strategy = strategy if strategy is not None else ZoomStrategyService()
        self._cache: RegionCatalog | None = None
        self._catalog_error: str = ""
        #: 生成数据集（import pipeline 发布的那种）是否优先（Slice 26，§16）。
        #: **默认关**：默认路径与 Slice 18 逐字节一致，测试与验收脚本继续
        #: monkeypatch ``REGION_RESOURCE_DIR`` 就行。要"导入后不用重启就看
        #: 到新数据"的调用方（主窗口）显式打开它。
        self._prefer_generated = bool(prefer_generated)

    # ------------------------------------------------------------------ #
    # 目录 / 加载器
    # ------------------------------------------------------------------ #
    @property
    def root(self) -> Path:
        """资源根目录。

        显式给了 ``root`` / ``catalog`` 就用那个；``prefer_generated=True``
        且两者都没给时走 :func:`~app.regions.catalog.effective_region_root`
        （生成数据集已发布则优先，否则手工目录）；否则**每次访问都重新读**
        ``app.geo.region.REGION_RESOURCE_DIR`` 这个模块全局 —— 验收脚本与测试
        把内置资源重定向到临时目录时，这个服务会自动跟着走，不必再多同步一处。
        """
        if self._explicit_root is not None:
            return self._explicit_root.resolve()
        if self._explicit_catalog is not None:
            return self._explicit_catalog.root
        if self._prefer_generated:
            return effective_region_root().resolve()
        return region_resource_root().resolve()

    @property
    def catalog(self) -> RegionCatalog:
        """当前目录（带缓存；index.json 被改过会自动重读，见 :meth:`RegionCatalog.stale`）。"""
        if self._explicit_catalog is not None:
            return self._explicit_catalog

        root = self.root
        cached = self._cache
        if cached is not None and cached.root == root and not cached.stale():
            return cached

        try:
            catalog = RegionCatalog.load(root=root)
        except RegionCatalogError as exc:
            catalog = self._fallback_catalog(root)
            self._catalog_error = str(exc)
            _logger.error(
                "行政区目录不可用，已退回内置预设（只剩它们那几条，没有层级）：%s", exc
            )
        else:
            self._catalog_error = ""
        self._cache = catalog
        return catalog

    @property
    def catalog_error(self) -> str:
        """目录读不出来时的原因（正常是空串）。

        读这个属性会**确保目录已经读过一次**：否则"先看错误、再查询"的调用方
        永远只能看到空串 —— 一个只会说"没事"的体检报告没有意义。
        """
        self.catalog
        return self._catalog_error

    @property
    def strategy(self) -> ZoomStrategyService:
        """Zoom 策略服务（Slice 19）。

        界面侧要"取推荐值 / 判断是否超范围"，而本服务已经持有策略实例（见
        :meth:`get_zoom_recommendation`）。把它暴露出来是为了让
        :class:`~app.ui.region_selection.RegionSelectionController` 复用**同一个**
        实例，而不是各造一个 —— 两者读的是同一张
        :data:`~app.planning.DEFAULT_ZOOM_POLICIES`，多一个实例不会算出不同结果，
        但会多一处"注入点"，验收脚本想固定策略表时就得多改一处。
        """
        return self._strategy

    @property
    def loader(self) -> RegionLoader:
        """与当前目录配套的加载器。"""
        return RegionLoader(self.catalog, root=self.root)

    # ------------------------------------------------------------------ #
    # 重载（Slice 26，Spec §17）
    # ------------------------------------------------------------------ #
    def reload(self) -> RegionCatalog:
        """立刻重读目录（导入新数据集后调用，无需重启程序）。

        语义（Spec §17）：

        - 成功 → 返回新目录，:attr:`catalog_error` 清空；
        - **失败 → 继续保留旧 Catalog**，错误原因记进 :attr:`catalog_error`
          并打一条 ERROR 日志。一个刚发布失败 / 被手删的 index.json 不该
          让整个"行政区域"功能当场炸掉 —— 用户此刻还可能正看着旧数据。

        注意与 :attr:`catalog` 的自动失效（``stale()`` 比对 mtime/size）
        的分工：那个是"文件被改过就重读"的兜底，本方法是"我刚发布完，
        现在就重新读"的显式动作。根目录也会重新解析 —— 发布前它是手工
        目录、发布后可能切到 generated（``prefer_generated=True`` 时）。

        注入的目录（``catalog=``）不会自己变，reload 对它直接返回原对象。
        """
        if self._explicit_catalog is not None:
            return self._explicit_catalog
        root = self.root
        try:
            fresh = RegionCatalog.load(root=root)
        except RegionCatalogError as exc:
            previous = self._cache
            self._catalog_error = str(exc)
            _logger.error(
                "行政区目录重读失败，继续使用%s：%s",
                "旧目录" if previous is not None else "内置预设兜底",
                exc,
            )
            if previous is not None and previous.root == root:
                return previous
            catalog = self._fallback_catalog(root)
            self._cache = catalog
            return catalog
        self._cache = fresh
        self._catalog_error = ""
        _logger.debug("行政区目录已重读：%s（%d 条）", root, len(fresh))
        return fresh

    # ------------------------------------------------------------------ #
    # 层级查询（Spec §10）
    # ------------------------------------------------------------------ #
    def get_region(self, region_id: str | None) -> RegionPreset | None:
        """按 id 取一条；找不到返回 ``None``。"""
        return self.catalog.get(region_id)

    def require_region(self, region_id: str | None) -> RegionPreset:
        """按 id 取一条；找不到抛 :class:`RegionCatalogError`（消息里列出可用 id）。"""
        return self.catalog.require(region_id)

    def list_countries(self) -> tuple[RegionPreset, ...]:
        """全部全国级区域（顺序与目录一致）。"""
        return tuple(item for item in self.catalog.list_all() if item.is_country)

    def list_provinces(self, country_id: str) -> tuple[RegionPreset, ...]:
        """某个国家下的省级区域（顺序与目录一致）。

        Raises:
            RegionCatalogError: ``country_id`` 不在目录里。
        """
        self.require_region(country_id)
        return tuple(
            item for item in self.catalog.list_children(country_id) if item.is_province
        )

    def list_cities(self, province_id: str) -> tuple[RegionPreset, ...]:
        """某个省下的城市级区域。

        Raises:
            RegionCatalogError: ``province_id`` 不在目录里。
        """
        self.require_region(province_id)
        return tuple(
            item for item in self.catalog.list_children(province_id) if item.is_city
        )

    def children(self, parent_id: str | None) -> tuple[RegionPreset, ...]:
        """直接下级（``None`` 表示"全部根节点"）。"""
        return self.catalog.list_children(parent_id)

    def ancestors(self, region_id: str | None) -> tuple[RegionPreset, ...]:
        """全部上级，从根到直接父（面包屑顺序）。"""
        return self.catalog.get_ancestors(region_id)

    def path_text(self, region_id: str | None) -> str:
        """面包屑文本，如 ``中国 / 浙江省 / 杭州市``。"""
        return self.catalog.path_text(region_id)

    # ------------------------------------------------------------------ #
    # 下载区域（Spec §4 / §10）
    # ------------------------------------------------------------------ #
    def get_download_region(
        self,
        region_id: str,
        *,
        bounds: GeoBounds | None = None,
        document: GeoJSONDocument | None = None,
        geometry: Any = None,
    ) -> geo_region.DownloadRegion:
        """把区域 id 变成一个可下载的 :class:`~app.geo.DownloadRegion`。

        Raises:
            RegionCatalogError: id 不在目录里。
            RegionBoundaryMissing: 边界文件不存在（消息里写清放哪、叫什么）。
            GeoJSONError: 边界文件不合法。
        """
        return self.loader.to_download_region(
            region_id, bounds=bounds, document=document, geometry=geometry
        )

    def resolve_region_geometry(
        self, region_id: str, bounds: GeoBounds
    ) -> "tuple[geo_region.DownloadRegion | None, str] | None":
        """把 ``region_id`` 还原成几何，供 :mod:`app.geo.region` 的动态解析表使用。

        **为什么需要它**（Slice 25 修）：续传与 Inspector 都是从**任务记录 /
        MBTiles metadata** 出发的，那里只有一个 ``region_id``。``app.geo`` 的
        :func:`~app.geo.region.region_from_task` 会先查内置扁平预设
        （``REGION_PRESETS``，目前只有 ``china``），查不到就问一遍动态解析器。

        在 Slice 18 把"内置预设"换成"目录"之后，**目录里的区域**（真实使用时
        的省市、夹具里的 ``demoland``）恰好两边都不认识 —— 于是"行政区目录里的
        多边形任务"一旦进入续传路径（计划调度器走的就是这条路）就会报
        "找不到对应的行政区边界"。这不是防御性代码，而是把一条断掉的线接上。

        返回契约与 :data:`~app.geo.region.RegionResolver` 一致：

        - ``None``：**我不认识这个 id** —— 让后面的解析器（任务区）接着试；
        - ``(region, "")``：认出来了，几何可用；
        - ``(None, note)``：认出来了但边界文件不可用。此时调用方**不许**退回
          矩形（那会悄悄改变任务的定义），而要如实报"几何不可用"。

        Args:
            region_id: 任务记录 / metadata 里的区域标识。
            bounds: 当初下单用的候选矩形。**必须用它**而不是从几何重推 ——
                换一个 bbox 就等于换了一批瓦片，恢复出来的集合与上次对不上。
        """
        wanted = str(region_id or "").strip()
        if not wanted:
            return None
        try:
            preset = self.get_region(wanted)
        except RegionCatalogError:  # pragma: no cover - get_region 不抛这个
            return None
        if preset is None:
            # 目录里没有 → 不是行政区这一路，交给别的解析器（例如任务区）。
            return None
        if not self.boundary_available(wanted):
            # 认识这个 id，但边界文件不在了。**不要**退回矩形。
            return None, self.missing_message(wanted)
        return self.get_download_region(wanted, bounds=bounds), ""

    def resolve_path(self, region_id: str) -> Path:
        """边界文件的规范路径（**不检查是否存在**）。"""
        return self.loader.peek_path(region_id)

    def boundary_available(self, region_id: str) -> bool:
        """边界文件就位了没有。"""
        return self.loader.available(region_id)

    def missing_message(self, region_id: str) -> str:
        """边界文件缺失时给用户看的那段话（放哪、叫什么、要什么授权）。"""
        return self.loader.missing_message(region_id)

    def list_presets(self) -> tuple[geo_region.RegionPreset, ...]:
        """把目录转成 Slice 16 的扁平预设（下拉框与错误文案用）。

        界面侧只有这一处接口变化（Spec §15）：下拉框的**数据来源**从"内置常量"
        换成了"目录"，而条目类型没变，因此面板其余代码一行都不用动。
        """
        loader = self.loader
        return tuple(loader.to_geo_preset(entry) for entry in self.catalog.list_all())

    # ------------------------------------------------------------------ #
    # Zoom（Spec §9）
    # ------------------------------------------------------------------ #
    def get_zoom_recommendation(
        self,
        region_id: str,
        *,
        min_zoom: int | None = None,
        max_zoom: int | None = None,
    ) -> "ZoomRecommendation":
        """某个区域的 Zoom 建议。

        不传 ``min_zoom`` / ``max_zoom`` 时是**纯推荐**（``warning`` 恒为 False）；
        传了就与推荐区间比一比 —— 超出只置 ``warning``，**绝不改用户的值**。

        Raises:
            RegionCatalogError: id 不在目录里。
            ValueError: 只传了一个 zoom，或 zoom 非法 / 顺序反了。
        """
        entry = self.require_region(region_id)
        if min_zoom is None and max_zoom is None:
            return self._strategy.recommend(entry.level)
        if (min_zoom is None) != (max_zoom is None):
            raise ValueError(
                "min_zoom 与 max_zoom 必须同时给出：只给一个既无法确定区间，"
                "也无法判断是否超出推荐范围"
            )
        assert min_zoom is not None and max_zoom is not None
        return self._strategy.evaluate(entry.level, min_zoom, max_zoom)

    def recommend_with_estimate(
        self,
        region_id: str,
        *,
        min_zoom: int | None = None,
        max_zoom: int | None = None,
        average_tile_size: int = DEFAULT_AVERAGE_TILE_SIZE,
        document: GeoJSONDocument | None = None,
        geometry: Any = None,
    ) -> ZoomPlanEstimate:
        """推荐 Zoom + **真实**的瓦片数与容量（Spec §9）。

        等级取自 :attr:`~app.geo.DownloadRegion.region_level`（它来自目录），
        几何统计复用 :class:`~app.geo.RegionTileEstimator` —— 本方法不自己数瓦片。

        Raises:
            RegionCatalogError: id 不在目录里。
            RegionBoundaryMissing: 边界文件不存在。
            ValueError: 只传了一个 zoom，或 zoom 非法 / 顺序反了。
        """
        region = self.get_download_region(region_id, document=document, geometry=geometry)
        return self._strategy.recommend_with_estimate(
            region,
            region.region_level,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            average_tile_size=average_tile_size,
            geometry=geometry,
        )

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #
    def validate(self, *, deep: bool = False) -> CatalogValidationResult:
        """校验当前目录（Spec §13）。

        ``deep=True`` 会把每个 GeoJSON 都解析一遍，慢很多；验收与排查时才用。
        """
        return validate_catalog(self.catalog, deep=deep, loader=self.loader)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _fallback_catalog(self, root: Path) -> RegionCatalog:
        """目录不可用时，用内置扁平预设合成一份。

        合成的条目 ``parent_id`` 一律为 ``None``：内置预设本来就没有层级信息，
        编一个父亲出来只会让 ``list_provinces`` 给出假答案。
        """
        entries: list[RegionPreset] = []
        for preset in geo_region.list_region_presets():
            try:
                entries.append(
                    RegionPreset(
                        id=preset.region_id,
                        name=preset.name,
                        level=preset.region_level,
                        parent_id=None,
                        geojson_path=preset.geojson_filename,
                        boundary_version=preset.default_boundary_version,
                    )
                )
            except ValueError:  # pragma: no cover - 内置预设的路径一定是安全的
                _logger.error("内置预设 %s 的路径不合法，已跳过", preset.region_id)
        return RegionCatalog(tuple(entries), root=root)

    def __iter__(self) -> Iterator[RegionPreset]:  # pragma: no cover - 便利方法
        return iter(self.catalog)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<RegionService root={self.root} 区域数={len(self.catalog)}>"


# --------------------------------------------------------------------------- #
# 与 app.geo 的挂接（Slice 25）
# --------------------------------------------------------------------------- #
#: 当前生效的目录解析器。
#:
#: **只保留一个**（后装的顶掉先前的）：允许并存时，"上一个用例 / 上一份配置
#: 留下的实例"会赢，于是续传拿到的是**另一个目录**里的几何 —— 一个看起来很像
#: 真的错误结论。任务区那一路（:func:`~app.mission.install_mission_region_resolver`）
#: 用的是同一条规矩。
_ACTIVE_CATALOG_RESOLVER: "geo_region.RegionResolver | None" = None


def install_region_catalog_resolver(service: RegionService) -> None:
    """把「按 id 在行政区目录里找几何」的能力挂到 :mod:`app.geo.region` 上。

    为什么需要它：续传（``DownloadTask``）与 Inspector（MBTiles metadata）都只
    拿得到一个 ``region_id``，它们必须能自己把 id 变回几何。而 ``app.geo`` 是纯
    计算层，不认识"目录"这件事 —— 所以这里注册一个回调，``app.geo`` 只在
    "内置扁平预设里找不到这个 id"时问一句。

    与 :func:`~app.mission.install_mission_region_resolver` **同形**（两者注册的
    是同一个表，按注册顺序询问，不认识就返回 ``None`` 让下一个接着试）。
    :class:`~app.mission.CompositeRegionService` 在构造时把两者一起装上，
    所以"忘了在启动时调用"不会悄悄变成"续传不好使"。
    """
    global _ACTIVE_CATALOG_RESOLVER
    if _ACTIVE_CATALOG_RESOLVER is not None:
        geo_region.unregister_region_resolver(_ACTIVE_CATALOG_RESOLVER)
    _ACTIVE_CATALOG_RESOLVER = service.resolve_region_geometry
    geo_region.register_region_resolver(_ACTIVE_CATALOG_RESOLVER)
    _logger.debug("已注册行政区目录解析器：%s", service.root)


def uninstall_region_catalog_resolver(service: RegionService) -> None:
    """撤掉 :func:`install_region_catalog_resolver` 注册的那个回调。"""
    global _ACTIVE_CATALOG_RESOLVER
    geo_region.unregister_region_resolver(service.resolve_region_geometry)
    if _ACTIVE_CATALOG_RESOLVER is not None and (
        _ACTIVE_CATALOG_RESOLVER.__self__ is service
    ):
        _ACTIVE_CATALOG_RESOLVER = None
