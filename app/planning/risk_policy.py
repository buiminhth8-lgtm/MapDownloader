"""下载风险策略 —— **唯一权威的阈值表**（Slice 23，Spec §3 / §9 / §12）。

改一个阈值只需要改这里。UI、计划对话框、验收脚本一律只允许问
:data:`DEFAULT_RISK_POLICY` / :class:`RiskPolicy`，禁止自己写
``if tiles > 10_000_000`` 或把 2 / 20 / 100 GiB 抄一份 —— 抄一份的下场是
"界面说危险、弹窗说不大"这种两个来源各说各话。

两个维度，取更高的一档（Spec §3）
---------------------------------

=========  ==============  ==================  =========================
等级        瓦片数          容量                典型场景
=========  ==============  ==================  =========================
NORMAL     < 100 K         < 2 GiB             一个市、一条任务走廊
LARGE      100 K ~ 1 M     2 ~ 20 GiB          几个省、一个大地级市
VERY_LARGE 1 M ~ 10 M      20 ~ 100 GiB        全国概览 + 若干高档位
DANGEROUS  ≥ 10 M          ≥ 100 GiB           全国 Z8~18（数亿张 / 数十 TB）
=========  ==============  ==================  =========================

**为什么两个维度都要**：张数决定"要发多少次请求、跑多久"，容量决定"磁盘装不装
得下"。只按张数判，"全国 Z4~10"（几十万张但每张很小）与"一条走廊 Z18"
（同样几十万张但更密集）会被合并成一档；只按容量判，则高并发的小瓦片任务会被
低估。两者都算、取高者，是唯一在两种情况都不误判的做法。

**为什么不用"Zoom 高就是危险"**：任务区 Z15~18 的 Zoom 数字比全国 Z4~10 高得多，
但它只覆盖一条走廊（几万张）。等级只参与**策略判断**（推荐区间 / 建议文案），
最终风险一律看实际张数与容量（Spec §7 / §18）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.planning.region_level import RegionLevel
from app.planning.risk_level import RiskLevel

__all__ = [
    "DANGEROUS_SIZE_THRESHOLD",
    "DANGEROUS_TILE_THRESHOLD",
    "DEFAULT_RISK_POLICY",
    "LARGE_SIZE_THRESHOLD",
    "LARGE_TILE_THRESHOLD",
    "LEVEL_ADVICE",
    "VERY_LARGE_SIZE_THRESHOLD",
    "VERY_LARGE_TILE_THRESHOLD",
    "RiskPolicy",
]

_GIB: int = 1024 ** 3

#: 瓦片数阈值（Spec §3 的默认值）。``>=`` 该值即进入对应等级。
LARGE_TILE_THRESHOLD: int = 100_000
VERY_LARGE_TILE_THRESHOLD: int = 1_000_000
DANGEROUS_TILE_THRESHOLD: int = 10_000_000

#: 容量阈值（Spec §3 的默认值）。算术平均值 30 KB/张，因此与上面的张数阈值
#: 并不同构 —— 这正是要两个维度而不是换算一次的原因。
LARGE_SIZE_THRESHOLD: int = 2 * _GIB
VERY_LARGE_SIZE_THRESHOLD: int = 20 * _GIB
DANGEROUS_SIZE_THRESHOLD: int = 100 * _GIB

#: 等级 → 建议文案模板（Spec §9）。``{range_text}`` 由
#: :class:`~app.planning.ZoomStrategyService` 填进来 —— **本模块不写任何 Zoom
#: 数字**，否则推荐区间就有了第二个来源。
#:
#: ``CUSTOM`` 的模板刻意不含占位符：自定义区域没有"推荐区间"可言，
#: 写一句"建议按 Z1~18"纯属噪音（同时也解释了为什么它排在这里）。
LEVEL_ADVICE: dict[RegionLevel, str] = {
    RegionLevel.COUNTRY: (
        "建议先按 {range_text} 下载全国概览，需要更精细时改用省 / 市 / 任务区域。"
    ),
    RegionLevel.PROVINCE: (
        "建议按 {range_text} 下载省域，需要更精细时改用市 / 任务区域。"
    ),
    RegionLevel.CITY: (
        "建议按 {range_text} 下载城市范围，高精度作业区请改用任务区域（航线走廊）。"
    ),
    RegionLevel.COUNTY: (
        "建议按 {range_text} 下载县区范围；县级下到高 Zoom，注意单县也会产生大量瓦片。"
    ),
    RegionLevel.MISSION: (
        "任务区建议按 {range_text}；总量仍然偏大时请缩小航线范围或减小 Buffer 半径。"
    ),
    RegionLevel.CUSTOM: (
        "自定义区域不套用等级建议，请自行缩小范围或降低 Max Zoom。"
    ),
}


@dataclass(frozen=True)
class RiskPolicy:
    """风险阈值与确认规则（Spec §3 / §12）。

    不可变，且所有阈值都有默认值 —— 测试与将来的"用户自定义保守 / 激进策略"
    只需要构造另一个实例，不必改全局常量。

    Attributes:
        large_tiles / very_large_tiles / dangerous_tiles: 瓦片数阈值（含）。
        large_bytes / very_large_bytes / dangerous_bytes: 容量阈值（含）。
        confirmation_level: 从这一档起弹确认框（Spec §12：``LARGE``）。
        strong_confirmation_level: 从这一档起要求**强确认**（Spec §12 / §13：
            ``DANGEROUS``，必须手输确认词）。

    Raises:
        ValueError: 阈值非正，或没有严格递增（递增不成立时会出现"更高的等级
            覆盖更低的阈值"这种永远取不到某档的配置）。
    """

    large_tiles: int = LARGE_TILE_THRESHOLD
    very_large_tiles: int = VERY_LARGE_TILE_THRESHOLD
    dangerous_tiles: int = DANGEROUS_TILE_THRESHOLD
    large_bytes: int = LARGE_SIZE_THRESHOLD
    very_large_bytes: int = VERY_LARGE_SIZE_THRESHOLD
    dangerous_bytes: int = DANGEROUS_SIZE_THRESHOLD
    confirmation_level: RiskLevel = RiskLevel.LARGE
    strong_confirmation_level: RiskLevel = RiskLevel.DANGEROUS

    def __post_init__(self) -> None:
        for name in (
            "large_tiles",
            "very_large_tiles",
            "dangerous_tiles",
            "large_bytes",
            "very_large_bytes",
            "dangerous_bytes",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} 必须是整数，实际为 {type(value).__name__}")
            if value <= 0:
                raise ValueError(f"{name} 必须为正，实际为 {value}")
        for low, high, label in (
            (self.large_tiles, self.very_large_tiles, "瓦片数阈值"),
            (self.very_large_tiles, self.dangerous_tiles, "瓦片数阈值"),
            (self.large_bytes, self.very_large_bytes, "容量阈值"),
            (self.very_large_bytes, self.dangerous_bytes, "容量阈值"),
        ):
            if low >= high:
                raise ValueError(
                    f"{label}必须严格递增，实际为 {low} >= {high} —— "
                    "否则某个等级永远取不到"
                )
        for name in ("confirmation_level", "strong_confirmation_level"):
            value = getattr(self, name)
            if not isinstance(value, RiskLevel):
                raise ValueError(
                    f"{name} 必须是 RiskLevel，实际为 {type(value).__name__}"
                )
        if self.strong_confirmation_level.severity < self.confirmation_level.severity:
            raise ValueError(
                "strong_confirmation_level 不能低于 confirmation_level："
                "强确认是叠加在普通确认之上的，不是替换它"
            )

    # ------------------------------------------------------------------ #
    # 分档
    # ------------------------------------------------------------------ #
    def level_for_tiles(self, tiles: int) -> RiskLevel:
        """按**选中**瓦片数分档。

        Args:
            tiles: 瓦片数，非负。负数按 0 处理（调用方给错数不该让分析炸掉，
                "0 张"本身就是最小的风险）。
        """
        count = max(0, int(tiles))
        if count >= self.dangerous_tiles:
            return RiskLevel.DANGEROUS
        if count >= self.very_large_tiles:
            return RiskLevel.VERY_LARGE
        if count >= self.large_tiles:
            return RiskLevel.LARGE
        return RiskLevel.NORMAL

    def level_for_bytes(self, size_bytes: int) -> RiskLevel:
        """按预估容量分档（负值按 0 处理，理由同上）。"""
        size = max(0, int(size_bytes))
        if size >= self.dangerous_bytes:
            return RiskLevel.DANGEROUS
        if size >= self.very_large_bytes:
            return RiskLevel.VERY_LARGE
        if size >= self.large_bytes:
            return RiskLevel.LARGE
        return RiskLevel.NORMAL

    def level_for(self, tiles: int, size_bytes: int) -> RiskLevel:
        """两个维度里更高的一档（Spec §3："最终风险取各维度最高等级"）。"""
        return self.level_for_tiles(tiles).worst(self.level_for_bytes(size_bytes))

    # ------------------------------------------------------------------ #
    # 确认规则（Spec §12 / §13）
    # ------------------------------------------------------------------ #
    def requires_confirmation(self, level: RiskLevel) -> bool:
        """该等级是否需要弹确认框（``LARGE`` 起）。"""
        return level.at_least(self.confirmation_level)

    def requires_strong_confirmation(self, level: RiskLevel) -> bool:
        """该等级是否需要**强确认**（``DANGEROUS`` 起：必须手输确认词）。"""
        return level.at_least(self.strong_confirmation_level)

    # ------------------------------------------------------------------ #
    # 建议文案（Spec §9）
    # ------------------------------------------------------------------ #
    def advice_for(self, level: RegionLevel, range_text: str) -> str:
        """某一等级的建议文案。

        Args:
            level: 规划等级。
            range_text: 推荐区间文本（如 ``Z4~10``），由策略层给。

        Raises:
            ValueError: ``level`` 不是 :class:`RegionLevel`。
        """
        if not isinstance(level, RegionLevel):
            raise ValueError(
                f"level 必须是 RegionLevel，实际为 {type(level).__name__}"
            )
        return LEVEL_ADVICE[level].format(range_text=range_text)


#: 默认策略（Spec §3 的默认阈值 + Spec §12 的确认规则）。
#: **全项目唯一一份** —— 界面与测试都不许自己写阈值。
DEFAULT_RISK_POLICY: RiskPolicy = RiskPolicy()
