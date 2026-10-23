"""默认 Zoom 策略表 —— **唯一权威来源**（Slice 17，Spec §3 / §12）。

改一个数字只需要改这里。UI、Downloader、估算器、打包、将来的 Android 侧都
只允许问 :func:`get_zoom_policy` / :class:`~app.planning.ZoomStrategyService`，
禁止自己写 ``if level == "country": 10`` 或把 4/10/13 这类数字抄一份。

默认策略（Spec §3，Slice 29 按无人机图基底重新标定）
--------------------------------------------------

=========  ==========  =============  ==================  ============================
等级        建议区间    警告阈值       米/像素(@32°N)       说明
=========  ==========  =============  ==================  ============================
COUNTRY    Z5 ~ Z10    > Z10          4148 ~ 124          大范围概览 / 转场索引
PROVINCE   Z9 ~ Z13    > Z14          247 ~ 16            省域浏览 / 城市定位
CITY       Z12 ~ Z15   > Z16          32 ~ 4.1            城市范围任务规划
COUNTY     Z14 ~ Z17   > Z18          8.1 ~ 1.0           区县级高精度浏览
MISSION    Z15 ~ Z18   > Z18          4.1 ~ 0.5           高精度，仅限有限作业区
CUSTOM     全支持范围  不警告           全范围               自定义区域不套用等级规则
=========  ==========  =============  ==================  ============================

四个刻意的设计
--------------

1. **区间之间有重叠**（全国上界 Z10 > 省级下界 Z9）。真实边界不会恰好落在
   瓦片级别上：一个省如果只取 Z9~Z10，省内城市就糊了；一个全国图如果只取
   Z4~Z8，省界就看不清。重叠带让"全国概览 + 几个重点省"能用同一套级别拼起来，
   详情见 ``docs/zoom_strategy.md``。

2. **推荐上界与警告阈值可以不同**（省级建议到 Z13、Z14 不警告）。中间那条
   过渡带是给"我就是想看清楚这一个省的县城"留的余地 —— 有代价，但没到需要
   弹提示的程度。Slice 29 起这条设计承担了新职责：**把"数据有真实增益"与
   "只是插值放大"分开**。天地图农村影像的原生分辨率约 2 米（高分一号 / 六号），
   对应 Z16；所以 Z17、Z18 这类"只买体积不买信息"的层级一律**降为允许、
   不再推荐**（CITY 上界 Z16 → Z15，COUNTY 上界 Z18 → Z17）。

3. **等级区间的上界 = 该范围内体积可接受的点，不是数据天花板**。市级全域下
   到 Z16 是 1.4 GB（南京实测），那是作业区该干的事，不是"市级包"该干的事。
   要更高精度就用任务区（:data:`~app.planning.ground_resolution.UAV_ZOOM_PRESETS`
   的 ``survey`` / ``inspect`` 档）—— 范围小，体积才撑得住。

4. **CUSTOM 的警告阈值取 Tile Math 的天花板**，因此在合法 Zoom 范围内
   **永不警告**（Spec §5）。自定义区域唯一的约束是"Zoom 本身合法"
   （:func:`~app.geo.tile_math.validate_zoom`），等级建议在这里不适用。
   给无人机定向调参时走这一档：知道自己要什么的人不需要等级规则。
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
        # 下界 Z5 而不是 Z4：Z4 一个像素 8.3 公里，全国只要 12 张瓦片 ——
        # 那已经是一张色块图，看不出省界也找不到城市，留着只会让"全国包"
        # 里塞一堆没人会看的层级。Z5（4.1 km/px）才能看出省界轮廓。
        recommended_min_zoom=5,
        recommended_max_zoom=10,
        warning_max_zoom=10,
        description=(
            "全国级建议 Z5~10（约 4.1 公里~124 米/像素），用于大范围概览与转场索引。"
            "成片陆地到 Z10 约 350 MB；再往下应拆成省、市或作业区分别下载。"
        ),
    ),
    RegionLevel.PROVINCE: ZoomPolicy(
        level=RegionLevel.PROVINCE,
        recommended_min_zoom=9,
        recommended_max_zoom=13,
        warning_max_zoom=14,
        description=(
            "省级建议 Z9~13（约 247 米~16 米/像素），适合省域浏览和城市定位。"
            "一个中等省份到 Z13 约 450 MB。"
        ),
    ),
    RegionLevel.CITY: ZoomPolicy(
        level=RegionLevel.CITY,
        # 上界 16 → 15：南京全域 Z16 要 1.4 GB、Z15 只要 353 MB，而 Z15
        # （4 米/像素）已能看清乡镇路网与村庄轮廓。"看清单栋房子"是作业区
        # 的活儿，不该让市级包默认背这个体积。
        recommended_min_zoom=12,
        recommended_max_zoom=15,
        warning_max_zoom=16,
        description=(
            "城市级建议 Z12~15（约 32 米~4.1 米/像素），适合城市范围任务规划。"
            "要看清单栋建筑请改用任务区，市级全域下到 Z16 会到 GB 级。"
        ),
    ),
    RegionLevel.COUNTY: ZoomPolicy(
        level=RegionLevel.COUNTY,
        # 上界 18 → 17：Z18 在多数农村地区只是 2 米影像的插值放大，
        # 体积却是 Z17 的 4 倍。Z17（1 米/像素）是"有真实增益"的最后一档。
        recommended_min_zoom=14,
        recommended_max_zoom=17,
        warning_max_zoom=18,
        description=(
            "县级建议 Z14~17（约 8.1 米~1.0 米/像素）。Z17 是天地图农村影像"
            "有真实增益的最后一档；Z18 多为插值放大，仍可手填但不推荐。"
        ),
    ),
    RegionLevel.MISSION: ZoomPolicy(
        level=RegionLevel.MISSION,
        # 任务区**保持 15~18**：它是无人机的主力档，且范围天然小。
        # 10 km² 作业区到 Z18 约 178 MB，体积完全撑得住 —— 这是唯一
        # 该推荐 Z18 的地方。
        recommended_min_zoom=15,
        recommended_max_zoom=18,
        warning_max_zoom=18,
        description=(
            "任务区建议 Z15~18（约 4.1 米~0.5 米/像素），是无人机图基底的主力档。"
            "10 平方公里作业区到 Z18 约 178 MB；范围小，高 Zoom 才撑得住。"
        ),
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
