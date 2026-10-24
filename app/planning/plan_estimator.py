"""计划估算（Slice 20，Spec §7 / §8 / §20 / §21）。

回答一个问题："这个计划一共要下多少张、多大？"——**逐个条目**去算，然后相加。

复用，而不是重写（Spec §7）
--------------------------

::

    item.region_id
        ↓ RegionService.get_download_region      目录条目 → DownloadRegion
        ↓ RegionTileEstimator.estimate           按**真实几何**统计（Slice 16）
        ↓ selected_tiles / estimated_bytes       写回条目

本模块里**没有任何瓦片公式**：候选范围、多边形相交判定、容量换算全部来自
:mod:`app.geo`。这不是"省点代码"的问题——两套实现迟早会在某个边界情况上
给出不同的数，而用户看到的是"界面说 28,341 张，下完却是 27,900 张"。

多边形看**选中**张数，不是候选
------------------------------

``selected_tiles`` 是裁剪之后的张数（Spec §7 的硬性要求）。用候选数当总量，
在渤海湾、广州湾这类形状上能差出一个数量级，而用户会以为"程序漏下了"。

禁用条目不参与总量，但仍然会被算（Spec §12）
--------------------------------------------

汇总口径是"启用 **且** 可用"（:attr:`~app.planning.download_plan.DownloadPlanItem.counts_toward_total`），
但估算本身对**所有可用**条目都做一遍：这样"禁用一项"能立刻算出新的总量
（几个加法，不必重新遍历几何），而那一行仍然显示自己的容量——用户正是靠
这个数字决定"要不要把它重新打开"。禁用一次就要重算一遍全计划，是很糟的交互。

可取消（Spec §21）
------------------

取消在条目之间及单条目的候选瓦片遍历期间检查。取消后：

- 已经算完的条目保留结果；
- 没算到的条目保持"尚未估算"；
- **计划配置一个字都不动**（取消估算不是取消计划）；
- 不产生任何半成品。取消估算根本不会触及"生成任务"那条路径
  （那一步由 :class:`~app.planning.plan_service.PlanService` 负责，且要先过校验）。

取消必须**立刻**生效：检查点是回调，界面传进来的那个读的是一个
``threading.Event``（跨线程安全），而不是 Qt 的信号槽往返。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from app.config.settings import DEFAULT_AVERAGE_TILE_SIZE
from app.geo import GeoJSONError, RegionBoundaryMissing
from app.geo.region_estimator import (
    RegionEstimateCancelled,
    RegionTileEstimate,
    RegionTileEstimator,
)
from app.geo.tile_math import format_size
from app.planning.download_plan import DownloadPlan, DownloadPlanItem
from app.regions.models import RegionCatalogError
from app.regions.service import RegionService

__all__ = [
    "DownloadPlanEstimator",
    "PlanEstimateCancelled",
    "PlanEstimateProgress",
]


_logger = logging.getLogger(__name__)

#: 估算一个条目时"这一项自己算不出来"的全部原因。
#:
#: 刻意逐个列出而不是 ``except Exception``：估算跑在**后台线程**里，一个真
#: 正的 bug（例如 TypeError）必须冒泡出去让上层报出来，而不是被"这一项不可用"
#: 的温柔说法盖住 —— 那种掩盖会让用户永远查不出为什么他的区域算不出张数。
_ITEM_ERRORS: tuple[type[BaseException], ...] = (
    RegionCatalogError,      # 区域不在目录里（被删了）
    RegionBoundaryMissing,   # 边界文件没放 / 被删了
    GeoJSONError,            # 边界文件读不出几何
    ValueError,              # Zoom 非法 / 上下界反了（来自 Tile Math 与估算器）
)


class PlanEstimateCancelled(RuntimeError):
    """用户取消了本次估算（Spec §21）。

    刻意**不是** :class:`Exception` 之外的东西：它是一次正常操作的结果，
    调用方（界面）要把它与"估算失败"分开处理 —— 前者什么都不用说，
    后者必须把原因摆出来。
    """


@dataclass(frozen=True)
class PlanEstimateProgress:
    """估算进度快照（Spec §20 的 progress signal 载荷）。

    Attributes:
        done: 已经算完的条目数（含本次这一条）。
        total: 本次要算的条目数。
        region_name: 刚算完的那块地（界面用来显示"正在估算：浙江"）。
        selected_tiles: 这一条算出来的张数；这一条失败时为 0。
        failed: 这一条是否失败了（区域没了 / 边界缺了）。
    """

    done: int
    total: int
    region_name: str = ""
    selected_tiles: int = 0
    failed: bool = False

    @property
    def percent(self) -> int:
        """进度百分比（``0~100`` 的整数；界面进度条用）。"""
        if self.total <= 0:
            return 100
        return min(100, int(self.done * 100 / self.total))

    def describe(self) -> str:
        """一行文案，如 ``正在估算 2/3：示例北省``。"""
        if self.done >= self.total:
            return f"估算完成（{self.total} 项）"
        return f"正在估算 {self.done}/{self.total}：{self.region_name}"


class DownloadPlanEstimator:
    """逐个条目估算并汇总（Spec §7）。

    Attributes:
        service: 区域业务入口（拿 ``DownloadRegion`` 与目录条目）。
    """

    def __init__(
        self,
        service: RegionService | None = None,
        *,
        average_tile_size: int = DEFAULT_AVERAGE_TILE_SIZE,
    ) -> None:
        """
        Args:
            service: 区域服务；``None`` 时自建（跟随资源目录全局）。
            average_tile_size: 单张瓦片平均字节数（容量预估的输入）。
        """
        self._service = service if service is not None else RegionService()
        self._average_tile_size = int(average_tile_size)

    @property
    def service(self) -> RegionService:
        """区域服务。"""
        return self._service

    @property
    def average_tile_size(self) -> int:
        """单张瓦片平均字节数。"""
        return self._average_tile_size

    # ------------------------------------------------------------------ #
    # 单项
    # ------------------------------------------------------------------ #
    def estimate_item(
        self, item: DownloadPlanItem, *, geometry=None, should_cancel=None
    ) -> RegionTileEstimate | None:
        """估算一个条目并**就地写回**它。

        Args:
            item: 要估算的条目（会被改写：张数、容量、逐级明细、状态）。
            geometry: 已经加载好的几何（避免重复读边界文件）。

        Returns:
            估算结果；这一项算不出来时返回 ``None``，并把原因写进
            ``item.invalid_reason``（**不抛异常**：一个坏条目不该让整个计划
            算不出总量，Spec §18 的同一条原则）。
        """
        try:
            entry = self._service.require_region(item.region_id)
            region = self._service.get_download_region(item.region_id, geometry=geometry)
            estimate = RegionTileEstimator(
                region,
                average_tile_size=self._average_tile_size,
                geometry=geometry,
            ).estimate(
                item.min_zoom,
                item.max_zoom,
                **({"should_cancel": should_cancel} if should_cancel is not None else {}),
            )
        except _ITEM_ERRORS as exc:
            item.mark_invalid(str(exc))
            item.error = str(exc)
            _logger.warning("计划条目 %s 估算失败，已标记为不可用：%s", item.region_id, exc)
            return None

        # 名称 / 等级以**目录**为准：计划文件里的那份只是缓存，可能已经过期
        # （数据更新后同一个 id 换了名字）。这里顺手对齐，界面上就不会出现
        # "计划里叫杭州、目录里叫杭州市"这种两个来源各说各话。
        item.region_name = entry.name
        item.region_level = entry.level
        item.candidate_tiles = estimate.candidate_tiles
        item.selected_tiles = estimate.selected_tiles
        item.filtered_tiles = estimate.filtered_tiles
        item.estimated_bytes = estimate.estimated_size_bytes
        item.per_zoom = estimate.per_zoom
        item.mark_valid()
        item.error = ""
        return estimate

    # ------------------------------------------------------------------ #
    # 整个计划
    # ------------------------------------------------------------------ #
    def estimate(
        self,
        plan: DownloadPlan,
        *,
        should_cancel: Callable[[], bool] | None = None,
        progress: Callable[[PlanEstimateProgress], None] | None = None,
    ) -> DownloadPlan:
        """估算整个计划并刷新总量（Spec §7）。

        Args:
            plan: 计划（条目会被就地改写）。
            should_cancel: 在条目开始前和候选瓦片遍历期间检查是否取消。
            progress: 每算完一项回调一次（后台线程里调用，因此实现必须是
                跨线程安全的：通常就是往主线程发一个 Qt 信号）。

        Returns:
            同一个计划对象（``total_tiles`` / ``estimated_bytes`` 已刷新）。

        Raises:
            PlanEstimateCancelled: ``should_cancel()`` 返回 ``True``。
        """
        targets = list(plan.ordered_items())
        total = len(targets)
        _logger.info("开始估算计划 %s（%d 项）", plan.id, total)

        for index, item in enumerate(targets, start=1):
            if should_cancel is not None and should_cancel():
                _logger.info("计划 %s 的估算已被用户取消（已算 %d/%d）", plan.id, index - 1, total)
                raise PlanEstimateCancelled(
                    f"已取消估算（完成 {index - 1}/{total} 项，计划配置未改动）"
                )

            try:
                estimate = self.estimate_item(
                    item, **({"should_cancel": should_cancel} if should_cancel is not None else {})
                )
            except RegionEstimateCancelled as exc:
                raise PlanEstimateCancelled(str(exc)) from exc
            if progress is not None:
                progress(
                    PlanEstimateProgress(
                        done=index,
                        total=total,
                        region_name=item.region_name or item.region_id,
                        selected_tiles=item.selected_tiles,
                        failed=estimate is None,
                    )
                )

        plan.recalculate_totals()
        _logger.info(
            "计划 %s 估算完成：%d 张 / %s",
            plan.id,
            plan.total_tiles,
            format_size(plan.estimated_bytes),
        )
        return plan
