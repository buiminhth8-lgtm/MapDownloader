"""Zoom 策略服务（Slice 17，Spec §5 / §9）。

对外只有四件事：**推荐**、**评估**、**取策略**、**推荐 + 容量预估**。
它们都不修改用户设置的 Zoom —— 超出推荐范围只给 ``warning``（Spec §10）。

与 :mod:`app.geo` 的依赖方向
----------------------------

本模块**不在导入期** import :mod:`app.geo`（除了 ``tile_math``，它不依赖区域）：

``app.geo.region`` 要往区域模型上挂 :class:`~app.planning.RegionLevel`（Spec §7），
于是存在 ``app.geo.region → app.planning.region_level → app.planning.__init__``
这条边。如果本模块在导入期又去 import ``app.geo.region_estimator``，而这次导入
恰好是从 ``app.geo`` 那一侧发起的，就会撞上"半初始化的 ``app.geo.region`` 里
还没有 ``DownloadRegion``"这种 ImportError。把估算器的 import 推迟到
:meth:`ZoomStrategyService.recommend_with_estimate` 里，两边谁先导入都成立
（``tests/test_zoom_policy.py`` 里有两条子进程用例守着这件事）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.config.settings import DEFAULT_AVERAGE_TILE_SIZE
from app.geo.tile_math import validate_zoom
from app.planning.models import ZoomPolicy, ZoomRecommendation
from app.planning.region_level import RegionLevel
from app.planning.zoom_policy import DEFAULT_ZOOM_POLICIES

if TYPE_CHECKING:  # pragma: no cover - 只给类型检查器看
    from app.geo.region import DownloadRegion
    from app.geo.region_estimator import RegionTileEstimate

__all__ = [
    "PLAN_SIZE_LABEL",
    "PLAN_TILES_LABEL",
    "ZoomPlanEstimate",
    "ZoomStrategyService",
]

#: 「推荐 + 预估」文案的固定前缀（与 :class:`RegionTileEstimate` 的用语保持一致，
#: 但序号含义更明确：这里的数字是**区域内**的，不是候选矩形的）。
PLAN_TILES_LABEL: str = "预计瓦片（区域内）："
PLAN_SIZE_LABEL: str = "预计容量："


def _region_tile_estimator() -> Any:
    """惰性取 :class:`~app.geo.RegionTileEstimator`（见模块文档的循环说明）。

    返回类型写成 ``Any``：为了不在导入期引入 :mod:`app.geo.region_estimator`，
    连注解都不能在运行期求值（``from __future__ import annotations`` 只解决了
    函数签名，这里的返回值注解在 ``typing.get_type_hints`` 下仍会求值，
    没必要为此埋一个坑）。
    """
    from app.geo.region_estimator import RegionTileEstimator

    return RegionTileEstimator


@dataclass(frozen=True)
class ZoomPlanEstimate:
    """推荐 Zoom + 实测预估的组合结论（Spec §9）。

    "实测"两个字是重点：瓦片张数来自 :class:`~app.geo.RegionTileEstimator`
    对**真实区域几何**的统计（多边形会逐张判相交），不是按面积比例估算的
    经验值 —— 后者在广州湾、渤海湾这种形状上能差出一个数量级。

    Attributes:
        recommendation: 推荐 / 评估结论（含用户原始 Zoom，若有）。
        estimate: 实际预估结果。
        estimated_min_zoom: 预估用的下界。
        estimated_max_zoom: 预估用的上界。
        average_tile_size: 单张瓦片平均字节数（容量预估的输入，写下来便于复核）。
        used_recommended_zoom: 预估用的是推荐区间（``True``）还是用户给的区间。
    """

    recommendation: ZoomRecommendation
    estimate: RegionTileEstimate
    estimated_min_zoom: int
    estimated_max_zoom: int
    average_tile_size: int
    used_recommended_zoom: bool

    def __post_init__(self) -> None:
        if self.estimated_min_zoom > self.estimated_max_zoom:
            raise ValueError(
                "estimated_min_zoom 不能大于 estimated_max_zoom，"
                f"实际为 {self.estimated_min_zoom} > {self.estimated_max_zoom}"
            )
        zooms = tuple(item.zoom for item in self.estimate.per_zoom)
        if zooms and zooms != tuple(
            range(self.estimated_min_zoom, self.estimated_max_zoom + 1)
        ):
            raise ValueError(
                "预估明细与实际区间不一致："
                f"明细为 {zooms}，区间为 "
                f"{self.estimated_min_zoom}~{self.estimated_max_zoom}"
            )

    # ------------------------------------------------------------------ #
    # 派生
    # ------------------------------------------------------------------ #
    @property
    def level(self) -> RegionLevel:
        """区域等级。"""
        return self.recommendation.level

    @property
    def recommended_range_text(self) -> str:
        """推荐区间文本（如 ``Z4~10``）。"""
        return self.recommendation.range_text

    @property
    def selected_tiles(self) -> int:
        """区域内瓦片数（下游所有计算的**唯一**分母来源）。"""
        return self.estimate.selected_tiles

    @property
    def candidate_tiles(self) -> int:
        """候选矩形瓦片数。"""
        return self.estimate.candidate_tiles

    @property
    def size_text(self) -> str:
        """格式化容量（如 ``361.93 MB``）。"""
        return self.estimate.size_text

    def as_lines(self) -> list[str]:
        """多行文案：推荐 / 当前 / 警告 / 预计张数 / 预计容量（Spec §9）。"""
        lines = self.recommendation.as_lines()
        lines.append(f"{PLAN_TILES_LABEL}{self.selected_tiles:,}")
        lines.append(f"{PLAN_SIZE_LABEL}{self.size_text}")
        return lines


class ZoomStrategyService:
    """Zoom 策略入口（Spec §5）。

    策略表可注入（``policies``）：将来若出现"用户自定义等级喜好"或"省级数据
    落地后修正区间"，不必改动本类。默认就是
    :data:`~app.planning.zoom_policy.DEFAULT_ZOOM_POLICIES` —— 全局唯一的
    策略来源（Spec §12）。

    Attributes:
        policies: 本实例使用的策略表（只读副本）。
    """

    def __init__(
        self, policies: Mapping[RegionLevel, ZoomPolicy] | None = None
    ) -> None:
        """
        Raises:
            ValueError: 策略表缺项（有等级没有策略）。
        """
        table = dict(DEFAULT_ZOOM_POLICIES if policies is None else policies)
        missing = [item.value for item in RegionLevel if item not in table]
        if missing:
            raise ValueError(
                f"Zoom 策略表缺少等级：{'、'.join(missing)} —— "
                "每个 RegionLevel 都必须有对应策略"
            )
        self._policies: dict[RegionLevel, ZoomPolicy] = table

    # ------------------------------------------------------------------ #
    # 只读属性
    # ------------------------------------------------------------------ #
    @property
    def policies(self) -> Mapping[RegionLevel, ZoomPolicy]:
        """策略表副本（返回副本是为了让调用方改不动实例内部状态）。"""
        return dict(self._policies)

    # ------------------------------------------------------------------ #
    # 策略 / 推荐 / 评估
    # ------------------------------------------------------------------ #
    def get_policy(self, level: object) -> ZoomPolicy:
        """取某个等级的策略。

        Raises:
            ValueError: 不是 :class:`RegionLevel`，或该等级没有登记策略。
        """
        if not isinstance(level, RegionLevel):
            raise ValueError(
                f"level 必须是 RegionLevel，实际为 {type(level).__name__}（{level!r}）"
            )
        policy = self._policies.get(level)
        if policy is None:  # pragma: no cover - 构造期已保证完整
            raise ValueError(f"等级 {level.value} 没有登记 Zoom 策略")
        return policy

    def recommend(self, level: object) -> ZoomRecommendation:
        """纯推荐：只回推荐区间，不算代价（用户还没给 Zoom 时用）。

        ``warning`` 恒为 ``False`` —— 还没有"当前 Zoom"可比，谈不上超出。
        """
        policy = self.get_policy(level)
        return ZoomRecommendation(
            level=policy.level,
            min_zoom=policy.recommended_min_zoom,
            max_zoom=policy.recommended_max_zoom,
            warning=False,
            reason=policy.description,
        )

    def evaluate(
        self, level: object, min_zoom: int, max_zoom: int
    ) -> ZoomRecommendation:
        """拿用户设置的 Zoom 与推荐区间比一比（Spec §4 / §10）。

        **只读入、不改写**：返回值里的 ``requested_*`` 就是传进来的两个数，
        该不该提醒由 :attr:`ZoomRecommendation.warning` 表达。想强行把用户
        Zoom 改成推荐值？本层不提供这种能力，界面上也不该有。

        Raises:
            ValueError: level 非法；zoom 非法（超出 Tile Math 支持范围）；
                ``min_zoom > max_zoom``。
        """
        policy = self.get_policy(level)
        low = validate_zoom(min_zoom)
        high = validate_zoom(max_zoom)
        if low > high:
            raise ValueError(
                f"min_zoom 不能大于 max_zoom，实际 min_zoom={low}, max_zoom={high}"
            )
        return ZoomRecommendation(
            level=policy.level,
            min_zoom=policy.recommended_min_zoom,
            max_zoom=policy.recommended_max_zoom,
            warning=policy.should_warn(high),
            reason=policy.description,
            requested_min_zoom=low,
            requested_max_zoom=high,
        )

    def recommend_with_estimate(
        self,
        region: DownloadRegion,
        level: object,
        *,
        min_zoom: int | None = None,
        max_zoom: int | None = None,
        average_tile_size: int = DEFAULT_AVERAGE_TILE_SIZE,
        geometry: Any = None,
    ) -> ZoomPlanEstimate:
        """推荐 Zoom 并给出**真实**的瓦片数与容量（Spec §9）。

        复用 :class:`~app.geo.RegionTileEstimator`，不重新实现多边形统计。

        Args:
            region: 下载区域（矩形 / 多边形都可以）。
            level: 区域等级。
            min_zoom: 用户当前下界。与 ``max_zoom`` **同时给或同时不给**：
                不给就按推荐区间预估；给了就按用户区间预估，并附上 warning。
            max_zoom: 用户当前上界。
            average_tile_size: 单张瓦片平均字节数。
            geometry: 已加载的几何（避免重复读边界文件）。

        Raises:
            ValueError: 只给了一个 zoom；level / zoom 非法。
            GeoJSONError: 多边形边界文件不可读（由估算器抛出）。
        """
        if (min_zoom is None) != (max_zoom is None):
            raise ValueError(
                "min_zoom 与 max_zoom 必须同时给出：只给一个既无法确定预估区间，"
                "也无法判断是否超出推荐范围"
            )

        if min_zoom is None and max_zoom is None:
            recommendation = self.recommend(level)
            low = recommendation.min_zoom
            high = recommendation.max_zoom
            used_recommended = True
        else:
            assert min_zoom is not None and max_zoom is not None
            low = validate_zoom(min_zoom)
            high = validate_zoom(max_zoom)
            if low > high:
                # 顺序反了不是"用户的小怪癖"，是**非法输入**：直接炸，不要顺手
                # 帮他调过来（那会让界面上的 18~8 与实际预估的 8~18 悄悄不一致）。
                raise ValueError(
                    f"min_zoom 不能大于 max_zoom，实际 min_zoom={low}, max_zoom={high}"
                )
            recommendation = self.evaluate(level, low, high)
            used_recommended = False

        estimator_cls = _region_tile_estimator()
        estimator = estimator_cls(
            region, average_tile_size=average_tile_size, geometry=geometry
        )
        estimate = estimator.estimate(low, high)
        return ZoomPlanEstimate(
            recommendation=recommendation,
            estimate=estimate,
            estimated_min_zoom=low,
            estimated_max_zoom=high,
            average_tile_size=int(average_tile_size),
            used_recommended_zoom=used_recommended,
        )
