"""任务区业务入口（Slice 22，Spec §13 ~ §21）。

本模块做两件事
--------------

1. :class:`MissionRegionService` —— 任务区的对外入口：创建 / 读取 / 列出 /
   删除 / 变成 :class:`~app.geo.DownloadRegion` / Zoom 建议 / 瓦片预估。
2. :class:`CompositeRegionService` —— 让"行政区目录 + 任务区"在规划层看起来是
   **同一个入口**，于是 Slice 20 的计划构建、估算、校验一行都不用改。

为什么需要 :class:`CompositeRegionService`
------------------------------------------

规划层（``plan_builder`` / ``plan_estimator`` / ``plan_validator``）只认
:class:`~app.regions.RegionService` 那一套接口（``require_region`` /
``get_download_region`` / ``boundary_available`` / ``missing_message``）。
一个计划里可以同时有"中国 Z4~10"和"某条航线 Z15~18"，所以计划持有的那个服务
必须**两种区域都认识**。

做法是包一层：按 id 前缀分流（``mission-`` 开头 → 任务区，
否则 → 行政区目录），其余方法一律原样转给行政区服务。这样：

- 计划层**零改动**（它不认识"任务区"这个概念，也不需要认识）；
- 行政区那条路一个字都没动，回归风险被压到最低；
- "任务区不是行政区域"这件事仍然是显式的：任务区永远不会出现在
  "国家 / 省 / 市"级联下拉框里，因为它本来就不是行政区。

删除保护（Spec §22）
--------------------

任务区被下载任务 / 下载计划 / 计划执行记录引用时**拒绝删除**，并说清是谁在引用。
理由很实在：删掉几何文件，那些任务与计划里记着的 ``region_id`` 就再也解析不出
形状，续传会当场失败 —— 一个"删除成功"的提示换来一堆打不开的任务，
是最糟的交互。

引用检查是**保守**的：文件读不出来（JSON 坏了）时**拒绝删除**并说明原因，
而不是"当作没有引用"。宁可让用户先修好文件，也不要让他事后发现任务没了。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from app.config.settings import (
    DEFAULT_AVERAGE_TILE_SIZE,
    DEFAULT_MISSION_BUFFER_METERS,
    DEFAULT_TASKS_DIR,
    MISSION_ID_PREFIX,
    PLAN_EXECUTIONS_FILE_NAME,
    PLANS_FILE_NAME,
    TASKS_FILE_NAME,
    UI_TEXT_MISSION_DELETE_REFERENCED,
    UI_TEXT_MISSION_NOT_FOUND,
)
from app.geo.bounds import GeoBounds
from app.geo.region import (
    DownloadRegion,
    RegionType,
    register_region_resolver,
    unregister_region_resolver,
)
from app.mission.mission_region_store import MissionRegionStore
from app.mission.models import (
    MISSION_REGION_LEVEL,
    MissionRegion,
    MissionRegionError,
    MissionRegionMetadata,
    MissionRegionNotFound,
    MissionRegionReferenced,
    MissionWaypoint,
    is_mission_id,
    now_iso,
    require_mission_id,
)
from app.mission.region_builder import (
    MissionRegionBuilder,
    region_geojson_document,
    route_geojson_document,
)
from app.mission.route_loader import load_route_waypoints
from app.planning.zoom_strategy import ZoomPlanEstimate, ZoomStrategyService
from app.regions.models import RegionCatalogError, RegionPreset
from app.regions.service import RegionService, install_region_catalog_resolver

__all__ = [
    "MISSION_REFERENCE_FILES",
    "CompositeRegionService",
    "MissionRegionService",
    "install_mission_region_resolver",
    "mission_reference_files",
    "reset_mission_region_resolver",
    "uninstall_mission_region_resolver",
]

_logger = logging.getLogger(__name__)

def mission_reference_files(data_dir: Path | str) -> tuple[tuple[str, Path], ...]:
    """按一个 ``data/`` 目录算出"可能引用任务区"的三份文件（Spec §22）。

    与 :func:`MISSION_REFERENCE_FILES` 的关系，和 ``plans.json`` 与
    ``tasks.json`` 的关系一样：默认值只是"单跑服务"时的合理猜测，
    界面必须把**它自己那一份**（与任务 / 计划服务同目录）传进来 ——
    否则测试注入临时 ``tasks.json`` 时，引用检查去看的却是用户主目录里的文件。
    """
    directory = Path(data_dir)
    return (
        ("下载任务记录", directory / TASKS_FILE_NAME),
        ("下载计划", directory / PLANS_FILE_NAME),
        ("计划执行记录", directory / PLAN_EXECUTIONS_FILE_NAME),
    )


#: 默认的引用来源（Spec §22）。见 :func:`mission_reference_files`。
MISSION_REFERENCE_FILES: tuple[tuple[str, Path], ...] = mission_reference_files(
    DEFAULT_TASKS_DIR
)


class MissionRegionService:
    """任务区的唯一业务入口（Spec §13）。

    Attributes:
        store: 任务区目录读写。
        strategy: Zoom 策略服务（**唯一**的 Zoom 数字来源，Spec §16）。
    """

    def __init__(
        self,
        store: MissionRegionStore | None = None,
        *,
        builder: MissionRegionBuilder | None = None,
        strategy: ZoomStrategyService | None = None,
        reference_files: Sequence[tuple[str, Path]] | None = None,
        default_buffer_meters: float = DEFAULT_MISSION_BUFFER_METERS,
    ) -> None:
        """
        Args:
            store: 任务区存储；``None`` 时用默认的 ``data/mission_regions``。
            builder: 米制 Buffer 构建器；``None`` 时自建。
            strategy: Zoom 策略服务；``None`` 时自建（默认策略表）。
            reference_files: 引用检查要看的文件；``None`` 时用
                :data:`MISSION_REFERENCE_FILES`。
            default_buffer_meters: 缺省 Buffer 半径（米）。
        """
        self._store = store if store is not None else MissionRegionStore()
        self._builder = builder if builder is not None else MissionRegionBuilder()
        self._strategy = strategy if strategy is not None else ZoomStrategyService()
        self._reference_files = (
            tuple(reference_files) if reference_files is not None else MISSION_REFERENCE_FILES
        )
        self._default_buffer_meters = float(default_buffer_meters)
        # 解析回调在构造时**造一次就存起来**：注册表按对象身份比对，
        # 每次现造一个新闭包的话，注销时根本拿不到当初注册进去的那一个。
        self._resolver = _resolver_for(self)

    # ------------------------------------------------------------------ #
    # 只读属性
    # ------------------------------------------------------------------ #
    @property
    def store(self) -> MissionRegionStore:
        """任务区存储。"""
        return self._store

    @property
    def strategy(self) -> ZoomStrategyService:
        """Zoom 策略服务。

        规划层会从这个属性取策略（``PlanService`` 拿 ``service.strategy``），
        因此把它暴露出来是**接口的一部分**，不是"顺手"。
        """
        return self._strategy

    @property
    def builder(self) -> MissionRegionBuilder:
        """米制 Buffer 构建器。"""
        return self._builder

    @property
    def default_buffer_meters(self) -> float:
        """缺省 Buffer 半径（米）。"""
        return self._default_buffer_meters

    # ------------------------------------------------------------------ #
    # 创建
    # ------------------------------------------------------------------ #
    def next_mission_id(self) -> str:
        """下一个可用的任务区 id（``mission-001``、``mission-002``…）。

        按现有目录里最大的编号 +1：编号是给人看的（"Mission001"），
        用随机串或时间戳只会让对话里说不清"是哪一个"。
        """
        largest = 0
        for region in self._store.list_all():
            body = region.id[len(MISSION_ID_PREFIX) :]
            if body.isdigit():
                largest = max(largest, int(body))
        return f"{MISSION_ID_PREFIX}{largest + 1:03d}"

    def create(
        self,
        name: str,
        *,
        waypoints: Sequence[Any] | None = None,
        route_path: str | Path | None = None,
        route: Mapping[str, Any] | None = None,
        buffer_meters: float | None = None,
        mission_id: str | None = None,
    ) -> MissionRegion:
        """创建一个任务区并落盘（Spec §13）。

        航点来源**三选一**：``waypoints`` / ``route_path``（文件）/ ``route``（已解析对象）。

        Args:
            name: 展示名。
            waypoints: 航点序列。
            route_path: 航线文件（GeoJSON）。
            route: 已经解析好的 GeoJSON 对象。
            buffer_meters: 走廊半径（米）；``None`` 用缺省值。
            mission_id: 指定 id；``None`` 时自动取下一个编号。

        Returns:
            落盘后的任务区（``geojson_path`` 已回填为绝对路径）。

        Raises:
            MissionRouteError: 航线不合法 / 点数不够 / 跨度超限。
            MissionRegionError: 名称 / 半径 / id 不合法，或该 id 已经被占用。
        """
        title = str(name or "").strip()
        if not title:
            raise MissionRegionError("任务区名称不能为空")

        points = self._resolve_waypoints(waypoints, route_path, route)
        radius = (
            self._default_buffer_meters if buffer_meters is None else buffer_meters
        )
        mid = (
            require_mission_id(mission_id)
            if mission_id is not None
            else self.next_mission_id()
        )
        if self._store.exists(mid):
            raise MissionRegionError(
                f"任务区 {mid} 已经存在。"
                "要改航线或半径，请先删除它（或另建一个）—— "
                "悄悄改掉一个正在下载 / 已下载任务区的几何，会让那批瓦片"
                "再也说不清是按哪一版几何裁的（Spec §21）"
            )

        # 几何**只算一次**（Spec §23）：算完直接交给 build 与落盘，
        # 不在"预估"和"保存"两处各算一遍 —— 那是两套真值的经典来源。
        geometry = self._builder.build_geometry(points, radius)
        region = self._builder.build(
            mid,
            title,
            points,
            radius,
            geometry=geometry,
            created_at=now_iso(),
        )
        metadata = MissionRegionMetadata.from_region(region)
        saved = self._store.save(
            region,
            route_geojson=route_geojson_document(
                region.waypoints,
                mission_id=mid,
                name=region.name,
                buffer_meters=region.buffer_meters,
            ),
            region_geojson=region_geojson_document(
                geometry.geometry,
                mission_id=mid,
                name=region.name,
                buffer_meters=region.buffer_meters,
                geometry_version=region.geometry_version,
            ),
            metadata=metadata,
        )
        _logger.info(
            "已创建任务区 %s（%s，%d 个航点 / %.1f 米）→ %s",
            saved.id,
            saved.name,
            saved.waypoint_count,
            saved.buffer_meters,
            saved.geojson_path,
        )
        return saved

    def load_route(
        self, source: str | Path | Mapping[str, Any]
    ) -> tuple[MissionWaypoint, ...]:
        """读一条航线的航点（转给 :func:`~app.mission.route_loader.load_route_waypoints`）。

        服务层转出这个方法，是为了让界面只依赖一个对象 —— 界面上"预览航线"
        与"生成任务区"走的必须**是同一份解析逻辑**。
        """
        return load_route_waypoints(source)

    # ------------------------------------------------------------------ #
    # 读取 / 列表 / 删除
    # ------------------------------------------------------------------ #
    def get(self, mission_id: str) -> MissionRegion:
        """读一个任务区。

        Raises:
            MissionRegionNotFound: 不存在（或几何文件丢了）。
            MissionRegionError: 目录内容坏了。
        """
        return self._store.load(mission_id)

    def list_all(self) -> tuple[MissionRegion, ...]:
        """列出全部任务区（按 id 排序）。"""
        return self._store.list_all()

    def exists(self, mission_id: str) -> bool:
        """这个任务区在不在。"""
        return self._store.exists(mission_id)

    def geometry(self, mission_id: str):
        """加载任务区几何（Shapely 对象）。"""
        return self._store.geometry(mission_id)

    def references(self, mission_id: str) -> tuple[str, ...]:
        """谁在引用这个任务区（返回来源标签，如 ``("下载计划",)``）。

        Raises:
            MissionRegionError: 引用文件读不出来（**不当作"没有引用"**）。
        """
        mid = require_mission_id(mission_id)
        hits: list[str] = []
        for label, path in self._reference_files:
            if _file_mentions(path, mid):
                hits.append(label)
        return tuple(hits)

    def delete(self, mission_id: str) -> bool:
        """删除一个任务区（**带引用保护**，Spec §22）。

        Returns:
            ``True`` 删掉了；``False`` 本来就不存在。

        Raises:
            MissionRegionReferenced: 仍被任务 / 计划 / 执行记录引用。
            MissionRegionError: id 不合法，或引用检查做不了 / 删不掉。
        """
        mid = require_mission_id(mission_id)
        if not self._store.exists(mid) and not self._store.mission_dir(mid).is_dir():
            return False
        referenced = self.references(mid)
        if referenced:
            raise MissionRegionReferenced(
                f"{UI_TEXT_MISSION_DELETE_REFERENCED}（{mid}）："
                f"引用来源 —— {'、'.join(referenced)}。"
                "先把这些任务 / 计划删掉，或等它们执行完成，再来删这个任务区"
            )
        removed = self._store.delete(mid)
        if removed:
            _logger.info("已删除任务区 %s", mid)
        return removed

    # ------------------------------------------------------------------ #
    # 变成下载区域（Spec §14）
    # ------------------------------------------------------------------ #
    def to_download_region(
        self, mission_id: str, *, bounds: GeoBounds | None = None
    ) -> DownloadRegion:
        """任务区 → :class:`~app.geo.DownloadRegion`（Spec §14）。

        产物与行政区多边形**完全同构**：``region_type=polygon``、
        ``region_id=<mission-id>``、``geojson_path=region.geojson 的绝对路径``、
        ``geojson`` 过滤走 :class:`~app.geo.PolygonTileRegionFilter`。
        因此 Slice 16 的多边形下载链路一行都不用改就能下任务区。

        Args:
            mission_id: 任务区 id。
            bounds: 候选矩形。**默认不传**（用任务区自己的范围）。
                续传时调用方会传"任务记录里当初那份" —— 那是当初下单用的
                同一个矩形，原样用才不会改变候选瓦片集。

        Raises:
            MissionRegionNotFound: 任务区不存在 / 几何文件丢了。
        """
        region = self.get(mission_id)
        target = bounds if bounds is not None else region.bounds
        return DownloadRegion(
            region_type=RegionType.POLYGON,
            name=region.name,
            bounds=target,
            region_id=region.id,
            # 几何版本号当"边界版本"用：下载链路上这个字段的语义就是
            # "这批瓦片是按哪一版边界裁的"，对任务区来说就是几何版本。
            boundary_version=region.geometry_version or None,
            geojson_path=region.geojson_path,
            region_level=MISSION_REGION_LEVEL,
            buffer_meters=region.buffer_meters,
        )

    # ------------------------------------------------------------------ #
    # Zoom / 预估（Spec §16 / §17）
    # ------------------------------------------------------------------ #
    def recommendation(self, mission_id: str):
        """任务区的推荐 Zoom（来自策略表；MISSION 是 Z15~18）。

        本方法**不含任何 Zoom 数字** —— 区间永远来自
        :data:`~app.planning.DEFAULT_ZOOM_POLICIES`。
        """
        self.get(mission_id)  # 先确认任务区在（不在就抛，而不是给一个空建议）
        return self._strategy.recommend(MISSION_REGION_LEVEL)

    def evaluate(self, mission_id: str, min_zoom: int, max_zoom: int):
        """用用户设置的 Zoom 与推荐区间比一比（只提示，**不修改**用户的值）。"""
        self.get(mission_id)
        return self._strategy.evaluate(MISSION_REGION_LEVEL, min_zoom, max_zoom)

    def estimate(
        self,
        mission_id: str,
        *,
        min_zoom: int | None = None,
        max_zoom: int | None = None,
        average_tile_size: int = DEFAULT_AVERAGE_TILE_SIZE,
        geometry: Any = None,
    ) -> ZoomPlanEstimate:
        """任务区的真实瓦片数 / 容量（Spec §17）。

        ``min_zoom`` / ``max_zoom`` 都不传时按推荐区间（Z15~18）预估；
        都传时按用户区间预估并附上 warning。**统计口径是 selected**
        （逐张判相交），不是候选矩形 —— 后者会把走廊外的瓦片全算进来。

        Raises:
            MissionRegionNotFound: 任务区不存在。
            ValueError: 只给了一个 zoom（既算不出区间，也判断不了是否超额）。
        """
        region = self.to_download_region(mission_id)
        return self._strategy.recommend_with_estimate(
            region,
            MISSION_REGION_LEVEL,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            average_tile_size=average_tile_size,
            geometry=geometry,
        )
    # ------------------------------------------------------------------ #
    # 规划层接口（与 RegionService 同形，Spec §18）
    # ------------------------------------------------------------------ #
    def require_region(self, mission_id: str) -> RegionPreset:
        """任务区 → 目录条目形状的对象（规划层用）。

        返回的是 :class:`~app.regions.RegionPreset`（行政区目录里那条记录的类型），
        因为规划层只认它：``id`` / ``name`` / ``level`` 三个属性就够它用了
        （``plan_builder`` 拿这三样建条目，``plan_validator`` 拿 ``level`` 比对）。

        ``geojson_path`` 刻意留空：行政区目录条目的那个字段是**资源目录下的相对
        路径**，而任务区几何存在 ``data/mission_regions/`` 下、必须是绝对路径 ——
        硬塞进去只会得到一个既不是相对路径也不是资源的东西。任务区几何的真实
        位置在 :meth:`to_download_region` 的产物里（``geojson_path`` 绝对路径）。

        Raises:
            RegionCatalogError: 任务区不存在。**刻意转成这个异常**：
                规划层本来就用它表达"这个区域已经不在目录里了"，
                一种语义一个异常，比对两种异常更不容易漏接。
        """
        mid = require_mission_id(mission_id)
        try:
            region = self.get(mid)
        except MissionRegionNotFound as exc:
            raise RegionCatalogError(
                UI_TEXT_MISSION_NOT_FOUND.format(mission_id=mid)
            ) from exc
        return RegionPreset(
            id=region.id,
            name=region.name,
            level=MISSION_REGION_LEVEL,
            parent_id=None,
            geojson_path="",
            boundary_version=region.geometry_version or None,
            bounds=region.bounds,
        )

    def boundary_available(self, mission_id: str) -> bool:
        """几何文件就位了没有（与行政区服务的同名方法语义一致）。"""
        if not is_mission_id(mission_id):
            return False
        return self._store.exists(mission_id)

    def missing_message(self, mission_id: str) -> str:
        """几何文件缺失时给用户看的那段话。"""
        mid = require_mission_id(mission_id)
        return (
            f"{UI_TEXT_MISSION_NOT_FOUND.format(mission_id=mid)}"
            f"（应位于 {self._store.region_path(mid)}）"
        )

    # ------------------------------------------------------------------ #
    # 与 app.geo 的挂接
    # ------------------------------------------------------------------ #
    def install(self) -> None:
        """把本服务的解析回调注册到 :mod:`app.geo.region`（见模块级同名函数）。"""
        install_mission_region_resolver(self)

    def uninstall(self) -> None:
        """撤掉本服务注册的解析回调。"""
        uninstall_mission_region_resolver(self)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _resolve_waypoints(
        self,
        waypoints: Sequence[Any] | None,
        route_path: str | Path | None,
        route: Mapping[str, Any] | None,
    ) -> tuple[MissionWaypoint, ...]:
        """航点三选一。

        Raises:
            MissionRegionError: 给的来源不是恰好一个。
        """
        given = [
            name
            for name, value in (
                ("waypoints", waypoints),
                ("route_path", route_path),
                ("route", route),
            )
            if value is not None
        ]
        if len(given) != 1:
            raise MissionRegionError(
                "航点来源必须恰好给一个（waypoints / route_path / route），"
                f"实际给了 {len(given)} 个：{given or '一个都没给'}"
            )
        if waypoints is not None:
            return self._builder.normalize_waypoints(waypoints)
        if route_path is not None:
            return load_route_waypoints(route_path)
        assert route is not None
        return load_route_waypoints(route)


class CompositeRegionService:
    """行政区服务 + 任务区服务，对规划层合成一个入口（Spec §18）。

    分流规则只有一条：**id 前缀**。``mission-`` 开头交给任务区服务，
    其余一律转给行政区服务。没有前缀就不知道 id 属于谁 ——
    与其"两个都试一遍"，不如定一个显式契约。

    Attributes:
        base: 行政区服务。
        mission: 任务区服务。
    """

    def __init__(
        self,
        base: RegionService | None = None,
        mission: MissionRegionService | None = None,
        *,
        mission_prefix: str = MISSION_ID_PREFIX,
    ) -> None:
        self._base = base if base is not None else RegionService()
        self._mission = mission if mission is not None else MissionRegionService()
        self._mission_prefix = str(mission_prefix)
        # 两个几何解析器**在这里一起装上**（Slice 25 修）。
        #
        # 续传与 Inspector 手上只有一个 `region_id`，必须能把 id 变回几何。
        # 任务区那一路在 Slice 22 就写好了，但没人调用 `MissionRegionService.install()`
        # —— 行政区目录那一路更是从 Slice 18 换成目录之后就断了线（`app.geo` 只认
        # 内置的 `china` 预设）。后果是：**目录里的多边形任务一进续传路径就报
        # "找不到对应的行政区边界"**，而计划调度器走的正是续传路径。
        #
        # 装在这个"界面唯一入口"的构造函数里，是为了让"忘了在启动时调用"变成
        # 一件不可能的事 —— 这类缺陷的特点就是运行很久都不报错，只在续传时发作。
        install_region_catalog_resolver(self._base)
        self._mission.install()

    # ------------------------------------------------------------------ #
    # 分流
    # ------------------------------------------------------------------ #
    @property
    def base(self) -> RegionService:
        """行政区服务。"""
        return self._base

    @property
    def mission(self) -> MissionRegionService:
        """任务区服务。"""
        return self._mission

    @property
    def strategy(self) -> ZoomStrategyService:
        """Zoom 策略服务（**与任务区服务同一个对象**，Spec §16 的唯一来源）。"""
        return self._mission.strategy

    def is_mission(self, region_id: object) -> bool:
        """这个 id 是不是任务区。"""
        return is_mission_id(region_id) or str(region_id or "").startswith(
            self._mission_prefix
        )

    # `__getattr__` 只在正常属性查找失败时被调用，因此它不会盖住上面那些
    # 显式的分流属性；其余方法（``list_countries`` / ``loader`` / ``validate``…）
    # 一律原样透传 —— 界面上的级联下拉框因此完全不受影响。
    def __getattr__(self, item: str) -> Any:
        return getattr(self._base, item)

    # ------------------------------------------------------------------ #
    # 规划层接口
    # ------------------------------------------------------------------ #
    def require_region(self, region_id: str) -> RegionPreset:
        """按 id 取区域条目。

        Raises:
            RegionCatalogError: 行政区目录里没有，且不是任务区（或任务区不存在）。
        """
        if self.is_mission(region_id):
            return self._mission.require_region(region_id)
        return self._base.require_region(region_id)

    def get_download_region(self, region_id: str, **kwargs: Any) -> DownloadRegion:
        """按 id 取可下载区域。

        Keyword Args:
            bounds / document / geometry: 与行政区服务同名参数语义一致。

        Raises:
            RegionCatalogError: 行政区目录里没有这个 id。
            MissionRegionNotFound: 任务区不存在。
        """
        if self.is_mission(region_id):
            bounds = kwargs.get("bounds")
            return self._mission.to_download_region(region_id, bounds=bounds)
        return self._base.get_download_region(region_id, **kwargs)

    def boundary_available(self, region_id: str) -> bool:
        """边界 / 几何文件就位了没有。"""
        if self.is_mission(region_id):
            return self._mission.boundary_available(region_id)
        return self._base.boundary_available(region_id)

    def missing_message(self, region_id: str) -> str:
        """文件缺失时给用户看的那段话。"""
        if self.is_mission(region_id):
            return self._mission.missing_message(region_id)
        return self._base.missing_message(region_id)

    def get_zoom_recommendation(
        self, region_id: str, *, min_zoom: int | None = None, max_zoom: int | None = None
    ):
        """按 id 取 Zoom 建议（只推荐 / 只提示，绝不改用户的值）。"""
        if self.is_mission(region_id):
            return self._mission.evaluate(region_id, min_zoom, max_zoom)  # type: ignore[arg-type]
        return self._base.get_zoom_recommendation(
            region_id, min_zoom=min_zoom, max_zoom=max_zoom
        )

    def recommend_with_estimate(
        self,
        region_id: str,
        *,
        min_zoom: int | None = None,
        max_zoom: int | None = None,
        average_tile_size: int = DEFAULT_AVERAGE_TILE_SIZE,
        document: Any = None,
        geometry: Any = None,
    ) -> ZoomPlanEstimate:
        """推荐 Zoom + 真实瓦片数（规划层估算用）。"""
        if self.is_mission(region_id):
            return self._mission.estimate(
                region_id,
                min_zoom=min_zoom,
                max_zoom=max_zoom,
                average_tile_size=average_tile_size,
                geometry=geometry,
            )
        return self._base.recommend_with_estimate(
            region_id,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            average_tile_size=average_tile_size,
            document=document,
            geometry=geometry,
        )

    # ------------------------------------------------------------------ #
    # 任务区专属（方便界面只持有一个对象）
    # ------------------------------------------------------------------ #
    def list_all_missions(self) -> tuple[MissionRegion, ...]:
        """全部任务区。"""
        return self._mission.list_all()

    def create_mission(self, *args: Any, **kwargs: Any) -> MissionRegion:
        """创建任务区（转给任务区服务）。"""
        return self._mission.create(*args, **kwargs)

    def delete_mission(self, mission_id: str) -> bool:
        """删除任务区（带引用保护）。"""
        return self._mission.delete(mission_id)


# --------------------------------------------------------------------------- #
# 与 app.geo 的挂接（动态解析注册表）
# --------------------------------------------------------------------------- #
def _resolver_for(service: MissionRegionService) -> Callable[
    [str, GeoBounds], tuple[DownloadRegion | None, str] | None
]:
    """为某个任务区服务造一个解析回调（供 ``app.geo.region`` 调用）。"""

    def resolve(
        region_id: str, bounds: GeoBounds
    ) -> tuple[DownloadRegion | None, str] | None:
        if not is_mission_id(region_id):
            return None  # 不归我管
        try:
            return service.to_download_region(region_id, bounds=bounds), ""
        except MissionRegionNotFound as exc:
            return None, str(exc)
        except MissionRegionError as exc:
            return None, str(exc)

    return resolve


#: 当前生效的那个解析器。**同一时刻只允许有一个**。
#:
#: 桌面程序只有一个任务区根目录，所以"谁后装上谁生效"就是正确语义；
#: 允许并存反而会出事：两个实例（例如上一个用例留下的、指向另一个临时目录的）
#: 都认识 ``mission-001`` 时，先注册的那个会赢 —— 于是 Inspector 会拿到
#: **另一个目录里的几何**，得出一个看起来很像真的结论。宁可让新的把旧的顶掉。
_ACTIVE_RESOLVER: Callable[..., Any] | None = None


def install_mission_region_resolver(service: MissionRegionService) -> None:
    """把"按 id 找任务区几何"的能力挂到 :mod:`app.geo.region` 上（Spec §20）。

    为什么需要它：续传与 Inspector 都从**任务记录 / MBTiles metadata** 出发
    （那里只有一个 ``region_id``），它们必须能自己把 id 变回几何。而
    ``app.geo`` 是纯计算层，不认识"任务区" —— 所以这里注册一个回调，
    ``app.geo`` 只负责在"行政区预设里找不到这个 id"时问一句。

    必须在程序启动时（``MainWindow`` 组合各服务的时刻）调用一次；
    后装上的会**顶掉**先前那一个（见 :data:`_ACTIVE_RESOLVER`）。
    测试里用完请调用 :func:`uninstall_mission_region_resolver` 清理。
    """
    global _ACTIVE_RESOLVER
    if _ACTIVE_RESOLVER is not None:
        unregister_region_resolver(_ACTIVE_RESOLVER)
    _ACTIVE_RESOLVER = service._resolver  # noqa: SLF001 - 同模块内的约定
    register_region_resolver(_ACTIVE_RESOLVER)
    _logger.debug("已注册任务区几何解析器：%s", service.store.root)


def uninstall_mission_region_resolver(service: MissionRegionService) -> None:
    """撤掉 :func:`install_mission_region_resolver` 注册的那个回调。"""
    global _ACTIVE_RESOLVER
    unregister_region_resolver(service._resolver)  # noqa: SLF001 - 同模块内的约定
    if _ACTIVE_RESOLVER is service._resolver:
        _ACTIVE_RESOLVER = None


def reset_mission_region_resolver() -> None:
    """清掉当前生效的解析器（**测试用**）。

    与 :func:`uninstall_mission_region_resolver` 的差别：那个要你手上还有服务实例，
    这个不需要 —— 用例跑完只剩"全局表里还挂着一个指向临时目录的解析器"这一种情况，
    它只需要被清掉。
    """
    global _ACTIVE_RESOLVER
    if _ACTIVE_RESOLVER is not None:
        unregister_region_resolver(_ACTIVE_RESOLVER)
        _ACTIVE_RESOLVER = None


def _file_mentions(path: Path, needle: str) -> bool:
    """文件里有没有出现这个 id（用于删除前的引用检查）。

    递归比对**所有字符串值**，而不是只看 ``region_id`` 字段：
    宁可多认出几条引用（用户被挡一下、然后去想清楚），
    也不要漏掉一条（用户以为删干净了，实际留下一个打不开的任务）。

    Raises:
        MissionRegionError: 文件存在但读不出来（**不当作"没有引用"**）。
    """
    target = Path(path)
    if not target.is_file():
        return False
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MissionRegionError(
            f"引用检查失败：{target} 读不出来（{exc}）—— "
            "无法确认这个任务区有没有被引用，因此不删除它"
        ) from exc
    if not text.strip():
        return False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MissionRegionError(
            f"引用检查失败：{target} 不是合法的 JSON（第 {exc.lineno} 行 {exc.msg}）—— "
            "无法确认这个任务区有没有被引用，因此不删除它"
        ) from exc
    return _payload_mentions(payload, needle)


def _payload_mentions(payload: Any, needle: str) -> bool:
    """递归找字符串值里有没有 ``needle``。"""
    if isinstance(payload, str):
        return payload == needle
    if isinstance(payload, Mapping):
        return any(_payload_mentions(value, needle) for value in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(_payload_mentions(item, needle) for item in payload)
    return False
