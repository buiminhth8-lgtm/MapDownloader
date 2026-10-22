"""默认 Zoom 策略表 —— **唯一权威来源**（Slice 17，Spec §3 / §12）。

改一个数字只需要改这里。UI、Downloader、估算器、打包、将来的 Android 侧都
只允许问 :func:`get_zoom_policy` / :class:`~app.planning.ZoomStrategyService`，
禁止自己写 ``if level == "country": 10`` 或把 4/10/13 这类数字抄一份。

默认策略（Spec §3）
------------------

=========  ==========  =============  ============================
等级        建议区间    警告阈值       说明
=========  ==========  =============  ============================
COUNTRY    Z4 ~ Z10    > Z10          大范围概览
PROVINCE   Z9 ~ Z13    > Z14          省域浏览 / 城市定位
CITY       Z12 ~ Z16   > Z16          城市范围任务规划
COUNTY     Z14 ~ Z18   > Z18          区县级高精度浏览
MISSION    Z15 ~ Z18   > Z18          高精度，仅限有限作业区
CUSTOM     全支持范围  不警告         自定义区域不套用等级规则
=========  ==========  =============  ============================

三个刻意的设计
--------------

1. **区间之间有重叠**（全国上界 Z10 > 省级下界 Z9）。真实边界不会恰好落在
   瓦片级别上：一个省如果只取 Z9~Z10，省内城市就糊了；一个全国图如果只取
   Z4~Z8，省界就看不清。重叠带让"全国概览 + 几个重点省"能用同一套级别拼起来，
   详情见 ``docs/zoom_strategy.md``。

2. **推荐上界与警告阈值可以不同**（省级建议到 Z13、Z14 不警告）。中间那条
   过渡带是给"我就是想看清楚这一个省的县城"留的余地 —— 有代价，但没到需要
   弹提示的程度。

3. **CUSTOM 的警告阈值取 Tile Math 的天花板**，因此在合法 Zoom 范围内
   **永不警告**（Spec §5）。自定义区域唯一的约束是"Zoom 本身合法"
   （:func:`~app.geo.tile_math.validate_zoom`），等级建议在这里不适用。
"""

from __future__ import annotations

from app.config.settings import (
    MAX_SUPPORTED_ZOOM,
    MIN_SUPPORTED_ZOOM,
    TILE_MATH_MAX_ZOOM,
)
from app.planning.models import ZoomPolicy
from app.planning.region_level import RegionLevel

__all__ = [
    "DEFAULT_ZOOM_POLICIES",
    "get_zoom_policy",
    "list_zoom_policies",
]


#: 等级 → 策略。``RegionLevel`` 里**每一个**成员都必须在这里有对应项，
#: 否则新加的等级会静默拿不到建议（``tests/test_zoom_policy.py`` 会拦住它）。
DEFAULT_ZOOM_POLICIES: dict[RegionLevel, ZoomPolicy] = {
    RegionLevel.COUNTRY: ZoomPolicy(
        level=RegionLevel.COUNTRY,
        recommended_min_zoom=4,
        recommended_max_zoom=10,
        warning_max_zoom=10,
        description="全国级地图建议用于大范围概览，高 Zoom 应按省、市或任务区域下载。",
    ),
    RegionLevel.PROVINCE: ZoomPolicy(
        level=RegionLevel.PROVINCE,
        recommended_min_zoom=9,
        recommended_max_zoom=13,
        warning_max_zoom=14,
        description="省级地图适合省域浏览和城市定位。",
    ),
    RegionLevel.CITY: ZoomPolicy(
        level=RegionLevel.CITY,
        recommended_min_zoom=12,
        recommended_max_zoom=16,
        warning_max_zoom=16,
        description="城市级地图适合城市范围任务规划。",
    ),
    RegionLevel.COUNTY: ZoomPolicy(
        level=RegionLevel.COUNTY,
        recommended_min_zoom=14,
        recommended_max_zoom=18,
        warning_max_zoom=18,
        description="县级地图适合街道级高精度浏览，单县范围较小、适合下到高 Zoom。",
    ),
    RegionLevel.MISSION: ZoomPolicy(
        level=RegionLevel.MISSION,
        recommended_min_zoom=15,
        recommended_max_zoom=18,
        warning_max_zoom=18,
        description="任务区适合高精度 Zoom，仅建议用于有限作业区域。",
    ),
    RegionLevel.CUSTOM: ZoomPolicy(
        level=RegionLevel.CUSTOM,
        recommended_min_zoom=MIN_SUPPORTED_ZOOM,
        recommended_max_zoom=MAX_SUPPORTED_ZOOM,
        # 天花板而不是 ``MAX_SUPPORTED_ZOOM``：自定义层级不产生"等级建议"型警告
        warning_max_zoom=TILE_MATH_MAX_ZOOM,
        description="自定义区域不套用行政等级规则，按你设置的 Zoom 下载。",
    ),
}


def get_zoom_policy(level: object) -> ZoomPolicy:
    """取某个等级的 Zoom 策略（Spec §5）。

    与 :meth:`RegionLevel.parse` 的宽容不同，这里是**严格**的：调用方是策略
    服务与界面，它们手上本来就该是一个 :class:`RegionLevel`。把 ``"curnty"``
    这种拼错的字符串悄悄变成 CUSTOM，只会让"为什么不给建议"变成一个查不出来
    的谜。

    Raises:
        ValueError: 不是 :class:`RegionLevel`，或该等级没有登记策略。
    """
    if not isinstance(level, RegionLevel):
        raise ValueError(
            f"level 必须是 RegionLevel，实际为 {type(level).__name__}（{level!r}）"
        )
    try:
        return DEFAULT_ZOOM_POLICIES[level]
    except KeyError as exc:  # pragma: no cover - 新增等级却忘了配策略时才会走到
        raise ValueError(
            f"等级 {level.value} 没有登记 Zoom 策略，请在 DEFAULT_ZOOM_POLICIES 里补一条"
        ) from exc


def list_zoom_policies() -> tuple[ZoomPolicy, ...]:
    """全部策略，顺序与 :class:`RegionLevel` 的声明顺序一致（文档 / 测试用）。"""
    return tuple(
        DEFAULT_ZOOM_POLICIES[item]
        for item in RegionLevel
        if item in DEFAULT_ZOOM_POLICIES
    )
