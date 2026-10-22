"""任务区层（Slice 22）：航线 → 米制 Buffer 多边形 → Z15~18 离线地图。

这一层回答的是"**一条作业航线周围多大范围要下高精度图**"，并把答案变成
Slice 16 已有的 :class:`~app.geo.DownloadRegion` 交给下载链路 —— 它不是
第二套下载系统，而是"多边形区域"的**又一个来源**（另一个来源是行政区目录）。

要解决的问题
------------

"长三角 Z15~18"是 3000 万张瓦片、上百 GB；而作业人员真正需要的往往只是
**一条航线两侧几百米到几公里**。把高 Zoom 限制在走廊里，是效率问题，
也是"离线设备装得下"的问题（Spec §1）。

因此流程是::

    航点（WGS84）
      → LineString
      → 以航线中心为原点的本地投影（AEQD）      ← 关键一步
      → 在**米**制平面上 Buffer（例如 5000 m）
      → 投回 WGS84 的多边形
      → MissionRegion（region_type=polygon / region_level=mission）
      → RegionTileEstimator（逐张判相交，给出真实张数）
      → DownloadPlan → PlanScheduler（Z15~18）

最要紧的一条：**绝不在 WGS84 的"度"上 buffer**。"0.05 度"在广州是 5.5 km、
在漠河是 2.5 km —— 一个随纬度变化的缓冲半径不是半径，是一个 bug（Spec §7）。

模块划分
--------

========================================  ======================================
模块                                       职责
========================================  ======================================
:mod:`~app.mission.models`                :class:`MissionWaypoint` / :class:`MissionRegion` / 元数据
:mod:`~app.mission.route_loader`          GeoJSON 航线 → 航点序列（只认 LineString 语义）
:mod:`~app.mission.region_builder`        航点 + 半径（米） → 多边形几何
:mod:`~app.mission.mission_region_store`  落盘 / 读取 / 删除（原子写、路径受限）
:mod:`~app.mission.mission_region_service`业务入口 + 与行政区服务的合并入口
========================================  ======================================

用法::

    from app.mission import MissionRegionService, MissionRegionStore

    service = MissionRegionService(MissionRegionStore())
    region = service.create(name="西湖巡检航线", route_path=Path("route.geojson"),
                            buffer_meters=5000)
    service.to_download_region(region.id)      # → app.geo.DownloadRegion
    service.recommendation(region.id)          # → Z15~18（来自 Zoom 策略表）

依赖方向
--------

``app.mission`` → ``app.geo`` / ``app.planning`` / ``app.regions``，**单向**。
``app.geo`` 不认识本包：任务区几何不是"随程序分发的资源"，它落在
``data/mission_regions/`` 下，靠 ``app.geo.region`` 里那张**动态解析注册表**
（:func:`~app.geo.region.register_region_resolver`）在续传与 Inspector 时被认出来。
这条边有意识做成"注册回调"而不是 import，否则 ``app.geo`` 会反过来依赖业务层。
"""

from app.mission.mission_region_service import (
    MISSION_REFERENCE_FILES,
    CompositeRegionService,
    MissionRegionService,
    install_mission_region_resolver,
    mission_reference_files,
    reset_mission_region_resolver,
    uninstall_mission_region_resolver,
)
from app.mission.mission_region_store import MissionRegionStore
from app.mission.models import (
    MissionError,
    MissionRegion,
    MissionRegionError,
    MissionRegionMetadata,
    MissionRegionNotFound,
    MissionRegionReferenced,
    MissionRouteError,
    MissionWaypoint,
    km_to_meters,
    meters_to_km,
)
from app.mission.region_builder import (
    MissionGeometry,
    MissionRegionBuilder,
    region_geojson_document,
    route_geojson_document,
)
from app.mission.route_loader import (
    load_route_document,
    load_route_waypoints,
    parse_route_geojson,
)

__all__ = [
    # 模型
    "MissionWaypoint",
    "MissionRegion",
    "MissionRegionMetadata",
    "MissionError",
    "MissionRouteError",
    "MissionRegionError",
    "MissionRegionNotFound",
    "MissionRegionReferenced",
    "km_to_meters",
    "meters_to_km",
    # 航线
    "load_route_waypoints",
    "load_route_document",
    "parse_route_geojson",
    # 构建
    "MissionRegionBuilder",
    "MissionGeometry",
    "route_geojson_document",
    "region_geojson_document",
    # 持久化
    "MissionRegionStore",
    # 服务
    "MissionRegionService",
    "CompositeRegionService",
    "install_mission_region_resolver",
    "uninstall_mission_region_resolver",
    "reset_mission_region_resolver",
    "MISSION_REFERENCE_FILES",
    "mission_reference_files",
]
