"""行政等级（Slice 17，Spec §2；Slice 27 扩到县级）。

``COUNTRY / PROVINCE / CITY / COUNTY / MISSION`` 是**规划视角**上的层级，回答的是
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

层级规则（Slice 18）
--------------------

:data:`PARENT_LEVEL_RULES` 也放在这里：等级枚举与"等级怎么嵌套"是同一个概念的
两半，分散在两个包里的结果必然是"加了等级忘了改规则"。这条规则被
``app.regions.validator`` 用来校验目录，但**规则本身只有这一份**。
"""

from __future__ import annotations

from enum import Enum

__all__ = ["ADMINISTRATIVE_LEVELS", "PARENT_LEVEL_RULES", "RegionLevel"]


class RegionLevel(Enum):
    """下载区域的规划层级。

    ``value`` 是**会出现在日志与文档里的稳定标识**：日志里写
    ``level=country`` 比 ``level=RegionLevel.COUNTRY`` 好 grep，也便于 Android 侧
    用同一个字符串沟通。因此它是契约，改名等于打断下游。

    **它同时是跨端契约，不只是日志格式**：这些值会写进每个包的
    ``manifest.json`` 的 ``region.level``，Android 的 ``RegionLevel`` 按同一套字符串
    解析。认不出来时它不报错，而是静默降级成"未知等级"（选包里排最后）——
    所以新增一个成员时必须**同时**在 Android 侧加上，由
    ``tests/test_package_protocol_v1.py`` 读 Android 源码逐项比对兜住。
    """

    #: 全国 —— 大范围概览，高 Zoom 没有意义（Spec §3）。
    COUNTRY = "country"

    #: 省 / 自治区 / 直辖市 —— 省域浏览与城市定位（Spec §3）。
    PROVINCE = "province"

    #: 市 / 自治州 / 盟 —— 城市范围任务规划（Spec §3）。
    CITY = "city"

    #: 县 / 区 / 旗 / 县级市 —— 最小行政区，高精度街道级（Slice 27）。
    #: 直辖市的下辖区、省直辖县级市**没有市级上级**，它们的父级直接是省
    #: （见 :data:`PARENT_LEVEL_RULES`）。
    COUNTY = "county"

    #: 作业区 / 航线缓冲 —— 高精度，但只建议用于有限区域（Spec §3）。
    MISSION = "mission"

    #: 自定义区域。**不套用任何行政等级规则**（Spec §5）：
    #: 老 Region / 老任务记录没有等级字段时也落到这里。
    CUSTOM = "custom"

    # ------------------------------------------------------------------ #
    # 解析与展示
    # ------------------------------------------------------------------ #
    @classmethod
    def parse(cls, value: object) -> RegionLevel:
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
    def is_country(self) -> bool:
        """是不是全国级（行政层级的根）。"""
        return self is RegionLevel.COUNTRY

    @property
    def is_province(self) -> bool:
        """是不是省级。"""
        return self is RegionLevel.PROVINCE

    @property
    def is_city(self) -> bool:
        """是不是城市级。"""
        return self is RegionLevel.CITY

    @property
    def is_county(self) -> bool:
        """是不是县级（县 / 区 / 旗 / 县级市）。"""
        return self is RegionLevel.COUNTY

    @property
    def is_mission(self) -> bool:
        """是不是任务区 / 作业区（航线走廊）。

        这个谓词放在枚举上而不是让使用方写 ``level is RegionLevel.MISSION``：
        "哪些等级的几何不在资源目录里""哪些等级带走廊半径"这类判断散出去以后，
        加一级区域时总有人漏改一处。``tests/test_zoom_policy.py`` 的静态守卫
        正是拦"在 planning 层之外写等级成员名"的那种写法，这个属性就是它的正解。
        """
        return self is RegionLevel.MISSION

    @property
    def is_administrative(self) -> bool:
        """是否纳入行政层级规则（全国 / 省 / 市）。

        ``MISSION`` / ``CUSTOM`` 不在其中：它们可以有上级（"某条航线的作业区挂在
        杭州市下"），但不受"省必须挂在国下"这类约束（Slice 18 Spec §6）。
        """
        return self in ADMINISTRATIVE_LEVELS

    @property
    def is_custom(self) -> bool:
        """是否属于"不套用行政等级规则"的自定义层级。"""
        return self is RegionLevel.CUSTOM


#: 等级 → **允许的父等级集合**。空集合表示"必须是层级的根"。
#:
#: 这张表放在**定义等级的这个模块**里（而不是校验器里）：它和枚举是同一个
#: 概念的两半 —— "有哪些等级"和"这些等级怎么嵌套"。想加一级区域时，
#: 改枚举 + 补一行表就够，校验器、目录查询、界面都不用动。
#:
#: 用**集合**而不是单值，是为了表达"县 / 区"这一类的一对多：绝大多数县挂在
#: 市下面（``COUNTY → CITY``），但直辖市的下辖区、省直辖县级市**没有市级上级**，
#: 直接挂在省下面（``COUNTY → PROVINCE``，Slice 27 的中国行政区划就是这样）。
#: 一个值对不上就拒，会让整个直辖市 / 海南、湖北、新疆兵团市的县全被判成
#: "层级错误"。
#:
#: ``MISSION`` / ``CUSTOM`` **刻意不在表内**：它们不参与行政层级（Spec §6），
#: 查不到就表示"没有等级约束"，与"必须是根"是两回事。
PARENT_LEVEL_RULES: dict[RegionLevel, frozenset[RegionLevel]] = {
    RegionLevel.COUNTRY: frozenset(),
    RegionLevel.PROVINCE: frozenset({RegionLevel.COUNTRY}),
    RegionLevel.CITY: frozenset({RegionLevel.PROVINCE}),
    RegionLevel.COUNTY: frozenset({RegionLevel.CITY, RegionLevel.PROVINCE}),
}

#: 纳入行政层级的等级，**从** :data:`PARENT_LEVEL_RULES` 推导。
#: 不手写第二份列表：多写一份就一定会有人在加等级时漏掉。
ADMINISTRATIVE_LEVELS: tuple[RegionLevel, ...] = tuple(PARENT_LEVEL_RULES)


#: 等级 → 中文展示名。集中在这里，避免 UI 里出现 ``if level is COUNTRY: "全国"``
#: 这种散落的映射（Spec §12 的同类要求：策略值只能有一个权威来源）。
_DISPLAY_NAMES: dict[RegionLevel, str] = {
    # "全国级"而不是"全国"：它后面要接"推荐范围"组成一句提醒
    # （「超出全国级推荐范围」），直接拼"全国"会读成"超出全国推荐范围"。
    RegionLevel.COUNTRY: "全国级",
    RegionLevel.PROVINCE: "省级",
    RegionLevel.CITY: "城市级",
    RegionLevel.COUNTY: "县级",
    RegionLevel.MISSION: "任务区",
    RegionLevel.CUSTOM: "自定义",
}
