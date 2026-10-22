"""Zoom 策略数据模型（Slice 17，Spec §3 / §4）。

三个不可变值对象，**只描述"建议什么"，不描述"必须做什么"**：

- :class:`ZoomPolicy` —— 一个等级的建议区间与警告阈值（权威定义见
  :data:`~app.planning.zoom_policy.DEFAULT_ZOOM_POLICIES`）；
- :class:`ZoomRecommendation` —— 一次"推荐"或"评估"的结论。用户给的 Zoom
  原样带在 ``requested_*`` 里，**永不被改写**（Spec §10）；
- :class:`~app.planning.zoom_strategy.ZoomPlanEstimate` —— 推荐 + 容量预估的
  组合（它要引用 :class:`~app.geo.RegionTileEstimate`，因此放在策略服务那一侧，
  本模块保持对 :mod:`app.geo` 的最小依赖）。

为什么模型里没有 ``enforce()`` / ``clamp()`` 这类方法
----------------------------------------------------

因为一旦存在，早晚会有人调用它。策略层的职责是"把代价讲清楚"：全国下到 Z18
是几十 TB 和几周的请求量，用户要么知道，要么被明确告知 —— 而不是被悄悄改成
Z4~10 之后以为"下载失败了"。判断权留给用户（Spec §10 / §16）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.geo.tile_math import validate_zoom
from app.planning.region_level import RegionLevel

__all__ = [
    "ZOOM_CURRENT_LABEL",
    "ZOOM_RECOMMEND_LABEL",
    "ZOOM_WARNING_PREFIX",
    "ZoomPolicy",
    "ZoomRecommendation",
]

#: 界面 / 日志里的固定前缀。放这里而不是 UI 里，是因为
#: :meth:`ZoomRecommendation.hint_lines` 与状态栏文案必须逐字一致 ——
#: 两处各写一份，改文案时必然漏一处。
ZOOM_RECOMMEND_LABEL: str = "推荐 Zoom："
ZOOM_CURRENT_LABEL: str = "当前 Zoom："
ZOOM_WARNING_PREFIX: str = "警告："


@dataclass(frozen=True)
class ZoomPolicy:
    """一个行政等级的 Zoom 建议（Spec §3）。

    Attributes:
        level: 适用等级。
        recommended_min_zoom: 建议区间下界。
        recommended_max_zoom: 建议区间上界。
        warning_max_zoom: **警告**阈值：``max_zoom`` 大于它就给 warning。
            它可以等于 :attr:`recommended_max_zoom`（全国、城市级），也可以
            更高（省级留出 Z14 的过渡带）—— 这正是"推荐"与"硬限制"的区别。
        description: 给用户看的推荐说明（Spec §6 的原文）。它会出现在界面提示
            与 :class:`ZoomRecommendation` 的 ``reason`` 里，因此必须是完整句子。

    Raises:
        ValueError: 区间顺序不成立，或 Zoom 超出 Tile Math 支持范围。
    """

    level: RegionLevel
    recommended_min_zoom: int
    recommended_max_zoom: int
    warning_max_zoom: int
    description: str

    def __post_init__(self) -> None:
        if not isinstance(self.level, RegionLevel):
            raise ValueError(
                f"level 必须是 RegionLevel，实际为 {type(self.level).__name__}"
            )
        for name in ("recommended_min_zoom", "recommended_max_zoom", "warning_max_zoom"):
            # 走 Tile Math 的同一道校验：策略区间不可能合法而缩放级别本身非法
            validate_zoom(getattr(self, name))
        if self.recommended_min_zoom > self.recommended_max_zoom:
            raise ValueError(
                "recommended_min_zoom 不能大于 recommended_max_zoom，"
                f"实际为 {self.recommended_min_zoom} > {self.recommended_max_zoom}"
            )
        if self.warning_max_zoom < self.recommended_max_zoom:
            raise ValueError(
                "warning_max_zoom 不能低于 recommended_max_zoom："
                "那样会把「推荐区间内」的取值判成警告，"
                f"实际为 warning_max_zoom={self.warning_max_zoom}, "
                f"recommended_max_zoom={self.recommended_max_zoom}"
            )
        if not str(self.description).strip():
            raise ValueError("description 不能为空：界面提示要靠它说明原因")

    # ------------------------------------------------------------------ #
    # 派生
    # ------------------------------------------------------------------ #
    @property
    def is_custom(self) -> bool:
        """是否属于"不套用行政等级规则"的自定义层级（Spec §5）。"""
        return self.level.is_custom

    @property
    def recommended_range_text(self) -> str:
        """建议区间的紧凑文本，如 ``Z4~10``。"""
        return f"Z{self.recommended_min_zoom}~{self.recommended_max_zoom}"

    @property
    def recommended_text(self) -> str:
        """界面上那一行，如 ``推荐 Zoom：Z4~10``。"""
        return f"{ZOOM_RECOMMEND_LABEL}{self.recommended_range_text}"

    @property
    def level_name(self) -> str:
        """等级的中文展示名（``全国级`` / ``省级`` …）。"""
        return self.level.display_name

    def covers(self, min_zoom: int, max_zoom: int) -> bool:
        """给定区间是否**完整落在**建议区间内。

        只看建议区间，不看警告阈值 —— 两者不是一回事：省级 Z14 落在
        "超出建议但不警告"的过渡带里，:meth:`covers` 为 ``False`` 而
        ``warning`` 为 ``False``。
        """
        return self.recommended_min_zoom <= min_zoom and max_zoom <= self.recommended_max_zoom

    def should_warn(self, max_zoom: int) -> bool:
        """``max_zoom`` 是否触发警告。

        只看上界，不看下界：低 Zoom 意味着**更少**的瓦片和更快的下载，
        没有需要提醒的代价。反过来，全国 Z18 的量级是用户必须提前知道的。
        """
        return max_zoom > self.warning_max_zoom


@dataclass(frozen=True)
class ZoomRecommendation:
    """一次推荐 / 评估的结论（Spec §4）。

    ``requested_*`` 为 ``None`` 表示"这是一次纯推荐"（用户还没给 Zoom）；
    给了则是"评估"：原样记录用户的选择，配套 :attr:`exceeds_recommendation`
    与 :attr:`warning` 分别回答"是否超出建议"和"是否该提醒代价"。

    Attributes:
        level: 适用等级。
        min_zoom: 推荐区间下界（``requested_*`` 给了也不改）。
        max_zoom: 推荐区间上界。
        warning: 是否应提醒用户代价（不禁止下载）。
        reason: 人话解释（来自 :attr:`ZoomPolicy.description`）。
        requested_min_zoom: 用户设置的 Min Zoom；没给就是 ``None``。
        requested_max_zoom: 用户设置的 Max Zoom；没给就是 ``None``。
    """

    level: RegionLevel
    min_zoom: int
    max_zoom: int
    warning: bool
    reason: str
    requested_min_zoom: int | None = None
    requested_max_zoom: int | None = None

    # ------------------------------------------------------------------ #
    # 派生
    # ------------------------------------------------------------------ #
    @property
    def range_text(self) -> str:
        """推荐区间文本，如 ``Z4~10``。"""
        return f"Z{self.min_zoom}~{self.max_zoom}"

    @property
    def level_name(self) -> str:
        """等级的中文展示名。"""
        return self.level.display_name

    @property
    def has_request(self) -> bool:
        """用户是否给出了 Zoom。"""
        return (
            self.requested_min_zoom is not None
            and self.requested_max_zoom is not None
        )

    @property
    def requested_range_text(self) -> str | None:
        """用户设置的区间文本（如 ``Z8~18``）；没给就是 ``None``。"""
        if not self.has_request:
            return None
        return f"Z{self.requested_min_zoom}~{self.requested_max_zoom}"

    @property
    def exceeds_recommendation(self) -> bool:
        """用户的区间是否超出建议（Spec §4）。

        "超出"包含两种：下界比建议更低（范围铺得太大），或上界比建议更高
        （精度要到不需要的地步）。没有用户输入时恒为 ``False``。
        """
        if not self.has_request:
            return False
        assert self.requested_min_zoom is not None  # has_request 已保证
        assert self.requested_max_zoom is not None
        return (
            self.requested_min_zoom < self.min_zoom
            or self.requested_max_zoom > self.max_zoom
        )

    @property
    def warning_text(self) -> str:
        """单行警告文案（不警告时为空串）。"""
        if not self.warning:
            return ""
        current = self.requested_range_text
        head = f"{ZOOM_WARNING_PREFIX}超出{self.level_name}推荐范围"
        if current:
            head = f"{head}（当前 {current}）"
        sentence = f"{head}，建议使用 {self.range_text}。"
        return f"{sentence}{self.reason}" if self.reason else sentence

    # ------------------------------------------------------------------ #
    # 展示
    # ------------------------------------------------------------------ #
    def as_lines(self) -> list[str]:
        """界面提示区的多行文案（Spec §11）。

        顺序固定：推荐 → 当前（若有）→ 警告（若有）。界面上"推荐"永远在最上面，
        因为用户来这一看就是想问"该用多少"。
        """
        lines = [f"{ZOOM_RECOMMEND_LABEL}{self.range_text}"]
        current = self.requested_range_text
        if current:
            lines.append(f"{ZOOM_CURRENT_LABEL}{current}")
        text = self.warning_text
        if text:
            lines.append(text)
        return lines
