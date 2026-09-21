"""计划的增量编辑（Slice 20，Spec §3 / §4 / §5 / §6）。

:class:`DownloadPlanBuilder` 是"用户一次次点『加入下载计划』"这件事的落点：
它持有**一份正在编辑的计划**，每加一块地就往前推一步，最后
:meth:`~DownloadPlanBuilder.build` 交出一份完整的 :class:`~app.planning.download_plan.DownloadPlan`。

界面因此不需要自己维护"已加了哪些区域"这套状态 —— 面板只知道"用户点了按钮"，
计划里有什么由本类说了算（与 Slice 19 的 :class:`~app.ui.region_selection.RegionSelectionController`
同一个思路：规则收在一处，面板只剩转发）。

Zoom 只有一个来源（Spec §3 最后一句）
-------------------------------------

本模块里**没有一个 Zoom 数字**。默认值一律走
:meth:`~app.planning.ZoomStrategyService.recommend`（它读的是
:data:`~app.planning.DEFAULT_ZOOM_POLICIES` 那张唯一权威表），自定义值只做
"是不是合法整数区间"的检查，**不做**任何"是否超出推荐范围"的判断
——后者属于 :class:`~app.planning.plan_validator.PlanValidator` 的 warning
（Spec §6：超范围只警告、不修改）。

因此本模块**没有** `clamp()` / `enforce()` 这类方法，也不会有：一旦存在，
早晚会有人调用它，而用户会以为"我设的 Z18 生效了"。

防重复（Spec §5）
----------------

同一个计划里，同一个 ``region_id`` **最多一项**。再次添加时是**更新**原条目
（而不是新增一行、也不是静默忽略）：用户点"加入"时的期望是"按我现在这套参数
算进去"，更新既满足了这个期望，又不会造出两份指向同一块地的任务。

调用方靠 :class:`PlanAddResult` 的 ``created`` 区分"新加了"和"更新了" ——
界面上这两句话必须不一样，否则用户会怀疑自己点重了。

为什么本模块的 import 写在模块级，而 ``app/planning/__init__.py`` 不转出本模块
----------------------------------------------------------------------------

:mod:`app.regions` 在导入期会拉 ``app.geo.region``，而 ``app.geo.region`` 又会
回头拉 ``app.planning``（要 :class:`RegionLevel`）。这条环只要在
``app/planning/__init__.py`` 执行期间被走通，就会撞上"半初始化的
``app.geo.region``"（``import app.geo`` 恰好是从 region 那一行触发
``app.planning`` 的时候）。**本模块不在 ``app.planning.__init__`` 的转出清单里**，
于是它只会被"``app.geo`` 已经装好之后"的调用方导入（界面、测试、验收脚本），
那条环就永远走不到。详见 :mod:`app.planning` 的模块文档。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config.settings import DEFAULT_PLAN_NAME
from app.geo.tile_math import validate_zoom
from app.planning.download_plan import DownloadPlan, DownloadPlanItem
from app.planning.zoom_strategy import ZoomStrategyService
from app.regions.models import RegionCatalogError, RegionPreset
from app.regions.service import RegionService

__all__ = ["PlanAddResult", "DownloadPlanBuilder"]


_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanAddResult:
    """一次"加入下载计划"的结果（Spec §5）。

    Attributes:
        item: 落进计划里的那条条目（新增或更新后的）。
        created: ``True`` = 这次新加了一行；``False`` = 更新了已有的一行。
            界面必须据此说不同的话（"已加入" / "已更新"）——两句话都用
            "已加入"会让用户以为自己点重了，从而去找一个并不存在的重复项。
    """

    item: DownloadPlanItem
    created: bool

    @property
    def message(self) -> str:
        """给用户看的一句话（**不含错误码**）。"""
        verb = "已加入下载计划" if self.created else "已更新下载计划中的"
        return f"{verb}：{self.item.describe()}"


class DownloadPlanBuilder:
    """增量地编辑一份计划（Spec §3）。

    Attributes:
        service: 区域业务入口（层级、边界、``DownloadRegion``）。
        strategy: Zoom 策略服务。默认复用 ``service`` 持有的那一个：
            两者读的是同一张策略表，多一个实例不会算出不同结果，
            但会多一处必须同步的注入点。
    """

    def __init__(
        self,
        service: RegionService | None = None,
        *,
        strategy: ZoomStrategyService | None = None,
        plan: DownloadPlan | None = None,
    ) -> None:
        """
        Args:
            service: 区域服务。``None`` 时自建一个默认实例（它跟随
                ``app.geo.region.REGION_RESOURCE_DIR`` 这个全局，验收脚本把资源
                重定向到临时目录时会自动跟着走）。
            strategy: Zoom 策略服务；``None`` 时取 ``service.strategy``。
            plan: 一开始就编辑这份计划（从 ``plans.json`` 恢复时用）；
                ``None`` 时新建一份空计划。
        """
        self._service = service if service is not None else RegionService()
        self._strategy = (
            strategy if strategy is not None else self._service.strategy
        )
        self._plan = plan if plan is not None else DownloadPlan(
            name=DEFAULT_PLAN_NAME
        )

    # ------------------------------------------------------------------ #
    # 只读
    # ------------------------------------------------------------------ #
    @property
    def service(self) -> RegionService:
        """区域服务。"""
        return self._service

    @property
    def strategy(self) -> ZoomStrategyService:
        """Zoom 策略服务。"""
        return self._strategy

    @property
    def plan(self) -> DownloadPlan:
        """当前正在编辑的计划（**就地可变**：编辑方法都作用在它上面）。"""
        return self._plan

    def contains(self, region_id: str) -> bool:
        """计划里有没有这个区域。"""
        return self._plan.has_region(region_id)

    # ------------------------------------------------------------------ #
    # 计划级设置
    # ------------------------------------------------------------------ #
    def load(self, plan: DownloadPlan) -> DownloadPlan:
        """换一份计划继续编辑（从磁盘恢复时用），返回它。"""
        if not isinstance(plan, DownloadPlan):
            raise ValueError(f"plan 必须是 DownloadPlan，实际为 {type(plan).__name__}")
        self._plan = plan
        return self._plan

    def set_name(self, name: str) -> str:
        """设置计划名（它同时是输出子目录名，Spec §16）。"""
        self._plan.name = str(name or "").strip()
        return self._plan.name

    def set_output_dir(self, path: object) -> str:
        """设置计划共享的基础输出目录（Spec §16）。"""
        self._plan.output_dir = str(path or "").strip()
        return self._plan.output_dir

    def set_map_layer(self, layer: object) -> str:
        """设置图层标识（``MapLayerType.value``）。"""
        value = getattr(layer, "value", layer)
        self._plan.map_layer = str(value or "").strip()
        return self._plan.map_layer

    def set_with_labels(self, value: bool) -> bool:
        """设置"是否把中文注记合成进瓦片"（计划级共享）。"""
        self._plan.with_labels = bool(value)
        return self._plan.with_labels

    def reset(self) -> DownloadPlan:
        """清空条目（保留计划 id / 名字 / 输出设置），返回计划。

        与 :meth:`DownloadPlan.clear_items` 是同一件事，多一个方法只是为了让
        "用户点了『清空』"这条路径在读代码时一眼可见。
        """
        self._plan.clear_items()
        return self._plan

    # ------------------------------------------------------------------ #
    # 条目编辑（Spec §3 / §5）
    # ------------------------------------------------------------------ #
    def recommended_zoom(self, region_id: str) -> tuple[int, int]:
        """某个区域的推荐 Zoom 区间（来自策略层，本模块不存任何数字）。

        Raises:
            RegionCatalogError: id 不在目录里。
        """
        entry = self._service.require_region(region_id)
        recommendation = self._strategy.recommend(entry.level)
        return recommendation.min_zoom, recommendation.max_zoom

    def add_region(
        self,
        region_id: str,
        use_recommended_zoom: bool = True,
        min_zoom: int | None = None,
        max_zoom: int | None = None,
    ) -> PlanAddResult:
        """把一块区域加进计划（Spec §3）。

        Args:
            region_id: 目录里的区域 id。
            use_recommended_zoom: ``True`` 时 Zoom 取该等级的推荐区间。
            min_zoom: 自定义下界（``use_recommended_zoom=False`` 时必填）。
            max_zoom: 自定义上界（同上）。

        Returns:
            :class:`PlanAddResult`（含 ``created``，界面据此换措辞）。

        Raises:
            RegionCatalogError: id 不在目录里（消息里列出可用 id）。
            ValueError: 自定义 Zoom 没给全 / 不是合法缩放级别 / 上下界反了；
                或者 ``use_recommended_zoom=True`` 却又给了 Zoom
                （那种调用说明调用方自己也没想清楚要哪一个）。
        """
        entry = self._service.require_region(region_id)
        low, high = self._resolve_zoom(
            entry, use_recommended_zoom, min_zoom, max_zoom
        )

        existing = self._plan.item(entry.id)
        if existing is None:
            item = DownloadPlanItem(
                region_id=entry.id,
                region_name=entry.name,
                region_level=entry.level,
                min_zoom=low,
                max_zoom=high,
            )
            self._plan.add_item(item)
            _logger.info("计划 %s 加入区域 %s（%s）", self._plan.id, entry.id, item.zoom_text)
            return PlanAddResult(item=item, created=True)

        # --- 已存在：**更新**而不是新增一行（Spec §5）---
        # Zoom 变了就必须作废上一次的估算（那个数字是"按当时那对 Zoom 算的"）；
        # 没变则保留，用户重复点一次"加入"不该让界面上的一行数字突然变成"—"。
        if (existing.min_zoom, existing.max_zoom) != (low, high):
            existing.clear_estimate()
        existing.region_name = entry.name
        existing.region_level = entry.level
        existing.min_zoom = low
        existing.max_zoom = high
        # 用户点"加入"就是在说"我要它"，因此顺带把它重新打开。
        existing.enabled = True
        existing.mark_valid()
        _logger.info(
            "计划 %s 更新区域 %s（%s）", self._plan.id, entry.id, existing.zoom_text
        )
        return PlanAddResult(item=existing, created=False)

    def remove_region(self, region_id: str) -> bool:
        """从计划里删掉一块区域。

        Returns:
            ``True`` 表示删掉了；``False`` 表示本来就没有。
        """
        removed = self._plan.remove_region(region_id)
        if removed:
            self._plan.recalculate_totals()
            _logger.info("计划 %s 移除区域 %s", self._plan.id, region_id)
        return removed

    def set_enabled(self, region_id: str, enabled: bool = True) -> bool:
        """启用 / 禁用一个条目（Spec §12）。

        **不改 Zoom、不清估算**：禁用只是"这次不下它"，那个条目仍然是一个
        完整的配置，重新启用时应当原样回来。

        Returns:
            ``True`` 表示改动了；``False`` 表示计划里没有这个区域。
        """
        changed = self._plan.set_enabled(region_id, enabled)
        if changed:
            self._plan.recalculate_totals()
        return changed

    # ------------------------------------------------------------------ #
    # 交付
    # ------------------------------------------------------------------ #
    def build(self, name: str | None = None) -> DownloadPlan:
        """交出当前计划（可选地顺手改个名字）。

        会重算一次总量：调用方可能拿它去做校验或生成任务，而"总张数"必须与
        "启用且可用的那些条目"一致（Spec §7 / §12）。
        """
        if name is not None:
            self.set_name(name)
        return self._plan.recalculate_totals()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _resolve_zoom(
        self,
        entry: RegionPreset,
        use_recommended_zoom: bool,
        min_zoom: int | None,
        max_zoom: int | None,
    ) -> tuple[int, int]:
        """定出这一项用哪对 Zoom（推荐值 / 用户值）。

        Raises:
            ValueError: 见 :meth:`add_region` 的说明。
        """
        if use_recommended_zoom:
            if min_zoom is not None or max_zoom is not None:
                raise ValueError(
                    "use_recommended_zoom=True 时不应再给 min_zoom / max_zoom："
                    "两者同时给出会让「按推荐」与「按用户输入」变得无法区分"
                )
            recommendation = self._strategy.recommend(entry.level)
            return recommendation.min_zoom, recommendation.max_zoom

        if min_zoom is None or max_zoom is None:
            raise ValueError(
                "自定义 Zoom 必须同时给出 min_zoom 与 max_zoom："
                "只给一个既无法确定区间，也无法判断是否超出推荐范围"
            )
        # 走 Tile Math 的同一道校验（`validate_zoom`），不在这里另写一套范围检查
        low = validate_zoom(min_zoom)
        high = validate_zoom(max_zoom)
        if low > high:
            # 顺序反了是**非法输入**，不顺手帮用户调过来：那会让界面上的
            # 18~8 与实际下载的 8~18 悄悄不一致（与 Slice 17 同一条规则）。
            raise ValueError(
                f"min_zoom 不能大于 max_zoom，实际 min_zoom={low}, max_zoom={high}"
            )
        return low, high

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<DownloadPlanBuilder {self._plan.describe()}>"
