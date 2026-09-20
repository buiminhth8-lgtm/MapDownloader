"""规划层（Slice 17）：Zoom 策略核心。

回答"这种规模的区域，该用哪个 Zoom 区间"——并且**只回答建议**，
不替用户做决定（Spec §10 / §16）。

模块划分
--------

==============================  ==================================================
模块                            职责
==============================  ==================================================
:mod:`~app.planning.region_level`  :class:`RegionLevel` —— 全国 / 省 / 市 / 任务区 / 自定义
:mod:`~app.planning.models`        :class:`ZoomPolicy`、:class:`ZoomRecommendation`
:mod:`~app.planning.zoom_policy`   :data:`DEFAULT_ZOOM_POLICIES` —— 唯一权威策略表
:mod:`~app.planning.zoom_strategy` :class:`ZoomStrategyService`、:class:`ZoomPlanEstimate`
==============================  ==================================================

用法::

    from app.planning import RegionLevel, ZoomStrategyService

    service = ZoomStrategyService()
    service.recommend(RegionLevel.COUNTRY)          # 推荐 Z4~10
    service.evaluate(RegionLevel.COUNTRY, 8, 18)    # warning=True，但不改用户的 8~18
    service.recommend_with_estimate(region, RegionLevel.COUNTRY)   # 推荐 + 真实张数/容量

本层是**纯策略**：不 import UI、不发起请求、不写磁盘。估算部分复用
:mod:`app.geo` 的实现（Spec §9），不另起一套多边形统计。

``import app.planning`` 的导入顺序说明见 :mod:`app.planning.zoom_strategy` 的模块
文档 —— ``app.geo.region`` 需要 :class:`RegionLevel`，因此这里刻意让
:mod:`app.planning.region_level` 先被导入，且不允许任何子模块在导入期
import ``app.geo`` 的区域实现。
"""

from app.planning.region_level import RegionLevel

# 先导入 region_level 再导入其余模块：`app.geo.region` 依赖它，而
# `app.planning` 的 __init__ 有可能正是被那条依赖触发的（见 zoom_strategy 文档）。
from app.planning.models import (
    ZOOM_CURRENT_LABEL,
    ZOOM_RECOMMEND_LABEL,
    ZOOM_WARNING_PREFIX,
    ZoomPolicy,
    ZoomRecommendation,
)
from app.planning.zoom_policy import (
    DEFAULT_ZOOM_POLICIES,
    get_zoom_policy,
    list_zoom_policies,
)
from app.planning.zoom_strategy import (
    PLAN_SIZE_LABEL,
    PLAN_TILES_LABEL,
    ZoomPlanEstimate,
    ZoomStrategyService,
)

__all__ = [
    "RegionLevel",
    "ZoomPolicy",
    "ZoomRecommendation",
    "ZoomPlanEstimate",
    "ZoomStrategyService",
    "DEFAULT_ZOOM_POLICIES",
    "get_zoom_policy",
    "list_zoom_policies",
    "ZOOM_RECOMMEND_LABEL",
    "ZOOM_CURRENT_LABEL",
    "ZOOM_WARNING_PREFIX",
    "PLAN_TILES_LABEL",
    "PLAN_SIZE_LABEL",
]
