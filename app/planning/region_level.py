"""行政等级（Slice 17，Spec §2）。

``COUNTRY / PROVINCE / CITY / MISSION`` 是**规划视角**上的层级，回答的是
"这块地有多大、打算拿它干什么"，而不是"几何长什么样"（那是
:class:`~app.geo.RegionType` 的职责）。两者正交：

- 一个省级区域既可以是多边形（拿了省界数据），也可以是矩形（用户手填范围）；
- 一个多边形区域既可能是全国，也可能是一个作业区。

为什么要单独一层等级，而不是让 UI 各自判断"是不是中国"
------------------------------------------------------

Zoom 建议、容量预警、将来的分层下载计划都要问同一个问题："这是哪一级区域？"
如果每个使用方自己判断（"region_id == 'china' 就算全国"、"面积大于多少算省"），
规则就会散落，且**新增一个省级 Preset 时必然有人漏改**。有了等级枚举，
新增数据只是给预设多写一个字段。

这个模块**刻意不 import 任何东西**（纯 stdlib）：它是 ``app.geo`` 与
``app.planning`` 之间唯一的依赖点（``app.geo.region`` 要往区域模型上挂等级），
保持零依赖可以让这条边永不构成循环导入。
"""

from __future__ import annotations

from enum import Enum

__all__ = ["RegionLevel"]


class RegionLevel(Enum):
    """下载区域的规划层级。

    ``value`` 是**会出现在日志与文档里的稳定标识**：日志里写
    ``level=country`` 比 ``level=RegionLevel.COUNTRY`` 好 grep，也便于将来
    Android 侧用同一个字符串沟通。因此它是契约，改名等于打断下游。
    """

    #: 全国 —— 大范围概览，高 Zoom 没有意义（Spec §3）。
    COUNTRY = "country"

    #: 省 / 自治区 / 直辖市 —— 省域浏览与城市定位（Spec §3）。
    PROVINCE = "province"

    #: 市 / 区县 —— 城市范围任务规划（Spec §3）。
    CITY = "city"

    #: 作业区 / 航线缓冲 —— 高精度，但只建议用于有限区域（Spec §3）。
    MISSION = "mission"

    #: 自定义区域。**不套用任何行政等级规则**（Spec §5）：
    #: 老 Region / 老任务记录没有等级字段时也落到这里。
    CUSTOM = "custom"

    # ------------------------------------------------------------------ #
    # 解析与展示
    # ------------------------------------------------------------------ #
    @classmethod
    def parse(cls, value: object) -> "RegionLevel":
        """宽容解析：认不出来的一律按 :attr:`CUSTOM` 处理。

        "认不出 → CUSTOM" 而不是抛异常，是因为这个方法的调用方是**读历史数据**
        的那一侧（老 tasks.json、老 manifest、用户手写的配置）。在那里，
        "不知道等级"的正确后果是"不套用等级规则"，而 CUSTOM 恰好就是
        "不套用规则"。

        严格校验的场景（策略服务收到一个非法等级）请直接用枚举本身 ——
        :meth:`ZoomStrategyService.evaluate` 会抛 :class:`ValueError`，
        不在这里悄悄兜底。

        >>> RegionLevel.parse(None) is RegionLevel.CUSTOM
        True
        >>> RegionLevel.parse("COUNTRY") is RegionLevel.COUNTRY
        True
        >>> RegionLevel.parse("全国") is RegionLevel.CUSTOM
        True
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            for item in cls:
                if item.value == normalized:
                    return item
        return cls.CUSTOM

    @property
    def display_name(self) -> str:
        """中文展示名（界面与提示文案使用；日志请用 ``value``）。"""
        return _DISPLAY_NAMES[self]

    @property
    def is_custom(self) -> bool:
        """是否属于"不套用行政等级规则"的自定义层级。"""
        return self is RegionLevel.CUSTOM


#: 等级 → 中文展示名。集中在这里，避免 UI 里出现 ``if level is COUNTRY: "全国"``
#: 这种散落的映射（Spec §12 的同类要求：策略值只能有一个权威来源）。
_DISPLAY_NAMES: dict[RegionLevel, str] = {
    # "全国级"而不是"全国"：它后面要接"推荐范围"组成一句提醒
    # （「超出全国级推荐范围」），直接拼"全国"会读成"超出全国推荐范围"。
    RegionLevel.COUNTRY: "全国级",
    RegionLevel.PROVINCE: "省级",
    RegionLevel.CITY: "城市级",
    RegionLevel.MISSION: "任务区",
    RegionLevel.CUSTOM: "自定义",
}
