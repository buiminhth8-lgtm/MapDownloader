"""计划的编排入口（Slice 20，Spec §14 / §15 / §16）。

把四件事收在一个类后面，界面只认识 :class:`PlanService`：

::

    PlanBuilder   编辑（加 / 删 / 启停 / 定 Zoom）
    PlanEstimator 估算（逐项 + 汇总）
    PlanValidator 校验（errors / warnings）
    PlanStore     持久化（data/plans.json）

为什么要有这么一层
------------------

因为"生成下载任务"这件事同时用到了上面四者，而它是**唯一**会碰
:class:`~app.tasks.TaskService` 的地方：

::

    PlanService.create_download_tasks()
        1. 校验（不通过就一个任务都不建，Spec §9 / §21）
        2. 逐项把 region_id → DownloadRegion（RegionService，Spec §14）
        3. 按 §15 / §16 的命名规则定出输出文件
        4. 调**现有的** TaskService.create_task 建记录，形状与单任务下载一模一样

单区域下载那条路（``MainWindow._prepare_download``）一个字都没改
（Spec §23）：计划是**在它旁边**长出来的一条支线，不是它的替代品。

绝不自动开始下载（Spec §14 / §25）
---------------------------------

:meth:`PlanService.create_download_tasks` 只写 ``tasks.json`` 里的记录，
**不碰** :class:`~app.services.MapDownloadService`、不起线程、不发一个请求。
"生成任务"与"开始下载"是两件事，混在一起的后果是用户点一下按钮就消耗掉
几十万次配额 —— 而按钮上写的是"生成任务"。

回滚
----

建任务时如果第 3 项失败，前 2 项会被删掉（**只删记录**）：
半套任务比没有任务更糟 —— 用户无法判断"到底生成了几个"，
而重新点一次只会再生成一批。计划本身不受影响（那是他的配置）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.config.settings import DEFAULT_OUTPUT_DIR, DEFAULT_PLAN_NAME
from app.geo import GeoJSONError, RegionBoundaryMissing
from app.models.download_task import DownloadTask
from app.models.map_layer import MapType
from app.planning.download_plan import (
    DownloadPlan,
    plan_directory,
    planned_output_paths,
)
from app.planning.plan_builder import DownloadPlanBuilder, PlanAddResult
from app.planning.plan_estimator import DownloadPlanEstimator
from app.planning.plan_store import PlanStore
from app.planning.plan_validator import PlanValidationResult, PlanValidator
from app.planning.zoom_strategy import ZoomStrategyService
from app.regions.models import RegionCatalogError
from app.regions.service import RegionService
from app.tasks.task_service import TaskService
from app.tasks.task_store import TaskStoreError

__all__ = ["PlanService", "PlanTaskCreationError"]


_logger = logging.getLogger(__name__)


class PlanTaskCreationError(RuntimeError):
    """无法把计划变成下载任务（校验没过、区域读不出来、任务记录写不了）。

    单独一个类型是为了让界面能把它与"计划文件写失败"分开说话：
    前者要用户去改计划，后者是磁盘问题。
    """


class PlanService:
    """计划编排的对外入口（Spec §14）。

    Attributes:
        store: 计划文件读写。
        task_service: 任务记录服务（生成任务的落点）。
    """

    def __init__(
        self,
        *,
        store: PlanStore | None = None,
        service: RegionService | None = None,
        strategy: ZoomStrategyService | None = None,
        task_service: TaskService | None = None,
        builder: DownloadPlanBuilder | None = None,
        estimator: DownloadPlanEstimator | None = None,
        validator: PlanValidator | None = None,
    ) -> None:
        """
        Args:
            store: 计划存储；``None`` 时用默认的 ``~/MapDownloader/data/plans.json``。
            service: 区域服务；``None`` 时自建（跟随资源目录全局）。
            strategy: Zoom 策略服务；``None`` 时取 ``service.strategy``。
            task_service: 任务服务；``None`` 时自建（默认 ``tasks.json``）。
                界面必须把**它自己那个**传进来，否则计划生成的任务与界面上
                "下载任务"列表读的不是同一个文件。
            builder / estimator / validator: 可选的现成实例（测试注入用）。
                不给就自建，且三者共用同一个 ``service`` / ``strategy``。
        """
        self._region_service = service if service is not None else RegionService()
        self._strategy = (
            strategy if strategy is not None else self._region_service.strategy
        )
        self._store = store if store is not None else PlanStore()
        self._task_service = task_service if task_service is not None else TaskService()
        self._builder = (
            builder
            if builder is not None
            else DownloadPlanBuilder(self._region_service, strategy=self._strategy)
        )
        self._estimator = (
            estimator
            if estimator is not None
            else DownloadPlanEstimator(self._region_service)
        )
        self._validator = (
            validator
            if validator is not None
            else PlanValidator(self._region_service, strategy=self._strategy)
        )
        #: 输出目录的兜底值：计划里没记输出目录时用它（与单任务下载同一个默认）。
        self._default_output_dir = str(DEFAULT_OUTPUT_DIR)

    # ------------------------------------------------------------------ #
    # 只读
    # ------------------------------------------------------------------ #
    @property
    def plan(self) -> DownloadPlan:
        """当前计划（就地可变）。"""
        return self._builder.plan

    @property
    def store(self) -> PlanStore:
        """计划存储。"""
        return self._store

    @property
    def builder(self) -> DownloadPlanBuilder:
        """编辑器。"""
        return self._builder

    @property
    def estimator(self) -> DownloadPlanEstimator:
        """估算器。"""
        return self._estimator

    @property
    def validator(self) -> PlanValidator:
        """校验器。"""
        return self._validator

    @property
    def region_service(self) -> RegionService:
        """区域服务。"""
        return self._region_service

    @property
    def task_service(self) -> TaskService:
        """任务服务。"""
        return self._task_service

    def summary_text(self) -> str:
        """面板上那行摘要（Spec §10 的入口反馈）。"""
        plan = self.plan
        if plan.is_empty:
            return "下载计划：暂无区域"
        if plan.total_tiles <= 0:
            return f"下载计划：{plan.enabled_count} 个区域（尚未估算）"
        return (
            f"下载计划：{plan.enabled_count} 个区域 / "
            f"{plan.total_tiles_text} 张 / {plan.size_text}"
        )

    # ------------------------------------------------------------------ #
    # 载入 / 保存（Spec §17 / §18）
    # ------------------------------------------------------------------ #
    def load(
        self, *, name: str | None = None, output_dir: object = None
    ) -> DownloadPlan:
        """读磁盘上的计划作为"当前计划"。

        - 文件不存在（旧版本、首次运行）→ 新建一份空计划，**正常启动**（Spec §18）；
        - 文件损坏 → :meth:`PlanStore.load_all` 会隔离坏文件并返回空列表，
          这里同样落到"新建一份空计划"；
        - 有多个计划时取**最后一份**（最近保存的那个）。

        区域是否还在目录里**不在这里判断**：那是
        :meth:`PlanValidator.validate` 与估算器的事，它们会把条目标成
        "不可用"而不是让整份计划打不开（Spec §18）。

        Returns:
            当前计划。
        """
        plans = self._store.load_all()
        if plans:
            plan = plans[-1]
            _logger.info("已载入下载计划 %s（%s）", plan.id, plan.describe())
        else:
            plan = DownloadPlan(
                name=name or DEFAULT_PLAN_NAME,
                output_dir=str(output_dir or "").strip() or self._default_output_dir,
            )
            _logger.info("没有可用的下载计划文件，已新建一份空计划 %s", plan.id)
        self._builder.load(plan)
        return plan

    def save(self) -> DownloadPlan:
        """把当前计划写盘（按 id 覆盖）。

        Raises:
            PlanStoreError: 路径不可写、payload 里出现疑似密钥字段。
        """
        plan = self._builder.build()
        self._store.save(plan)
        _logger.info("下载计划已保存：%s", plan.describe())
        return plan

    def reset(self) -> DownloadPlan:
        """清空当前计划的条目（保留 id / 名字 / 输出设置）。"""
        plan = self._builder.reset()
        _logger.info("下载计划 %s 已清空条目", plan.id)
        return plan

    def apply_settings(
        self,
        *,
        name: object = None,
        output_dir: object = None,
        map_layer: object = None,
        with_labels: bool | None = None,
    ) -> DownloadPlan:
        """把界面上的设置同步进计划（``None`` 表示"这一项不动"）。

        名字与输出目录决定**文件落在哪**（Spec §16），图层与注记决定
        **下的是什么**（Spec §14），因此都必须进计划：只存在界面上，
        重启后就恢复不出来了。
        """
        if name is not None:
            self._builder.set_name(str(name))
        if output_dir is not None:
            self._builder.set_output_dir(output_dir)
        if map_layer is not None:
            self._builder.set_map_layer(map_layer)
        if with_labels is not None:
            self._builder.set_with_labels(with_labels)
        return self.plan

    # ------------------------------------------------------------------ #
    # 编辑（Spec §10 / §12）
    # ------------------------------------------------------------------ #
    def add_region(
        self,
        region_id: str,
        *,
        use_recommended_zoom: bool = True,
        min_zoom: int | None = None,
        max_zoom: int | None = None,
        save: bool = True,
    ) -> PlanAddResult:
        """把一块区域加进计划并（默认）立即落盘。

        Raises:
            RegionCatalogError: id 不在目录里。
            ValueError: 自定义 Zoom 没给全 / 非法 / 上下界反了。
            PlanStoreError: 落盘失败。
        """
        result = self._builder.add_region(
            region_id,
            use_recommended_zoom=use_recommended_zoom,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
        )
        if save:
            self.save()
        return result

    def remove_region(self, region_id: str, *, save: bool = True) -> bool:
        """从计划里移除一块区域。"""
        removed = self._builder.remove_region(region_id)
        if removed and save:
            self.save()
        return removed

    def set_enabled(
        self, region_id: str, enabled: bool = True, *, save: bool = True
    ) -> bool:
        """启用 / 禁用一个条目（Spec §12）。"""
        changed = self._builder.set_enabled(region_id, enabled)
        if changed and save:
            self.save()
        return changed

    def clear(self, *, save: bool = True) -> DownloadPlan:
        """清空计划的条目（Spec §11 的「清空」）。"""
        plan = self.reset()
        if save:
            self.save()
        return plan

    # ------------------------------------------------------------------ #
    # 估算 / 校验
    # ------------------------------------------------------------------ #
    def estimate(self, *, should_cancel=None, progress=None) -> DownloadPlan:
        """估算当前计划（**后台线程里调**，Spec §20）。

        Raises:
            PlanEstimateCancelled: 用户取消了。
        """
        return self._estimator.estimate(
            self.plan, should_cancel=should_cancel, progress=progress
        )

    def validate(self, *, check_output: bool = True) -> PlanValidationResult:
        """校验当前计划（Spec §9）。"""
        return self._validator.validate(
            self.plan,
            output_dir=self.plan.output_dir or self._default_output_dir,
            check_output=check_output,
        )

    def output_paths(self, *, output_dir: object = None) -> dict[str, Path]:
        """当前计划会产出的文件：``{region_id: 路径}``（Spec §15 / §16）。"""
        return planned_output_paths(
            self.plan,
            output_dir=self._effective_output_dir(output_dir),
        )

    def plan_directory(self, *, output_dir: object = None) -> Path:
        """当前计划的输出目录（``<输出目录>/<计划名>/``，Spec §16）。"""
        return plan_directory(
            self.plan, output_dir=self._effective_output_dir(output_dir)
        )

    # ------------------------------------------------------------------ #
    # 生成下载任务（Spec §14 / §15 / §16）
    # ------------------------------------------------------------------ #
    def create_download_tasks(
        self, *, output_dir: object = None
    ) -> list[DownloadTask]:
        """把当前计划里**启用且可用**的条目转成现有的
        :class:`~app.models.DownloadTask` 记录（Spec §14）。

        顺序即 :meth:`~app.planning.download_plan.DownloadPlan.ordered_enabled_items`
        （全国 → 省 → 市 → 任务区），为 Slice 21 的串行执行做准备（Spec §13）。

        **不会开始下载**：只写任务记录，状态停在"已创建"。

        Args:
            output_dir: 覆盖计划的输出目录（界面上可能刚改过）。

        Returns:
            新建的任务，顺序与计划一致。

        Raises:
            PlanTaskCreationError: 校验不通过（消息里就是给人看的那些原因）、
                区域读不出来、或任务记录写失败（此时**已建的那些会被删掉**）。
        """
        plan = self.plan
        # 先校验再动手：任何一个错误都意味着"这次生成出来的任务里有坏的"，
        # 而半套任务比没有任务更难收拾（Spec §21 的同一原则）。
        result = self.validate(check_output=True)
        if not result.ok:
            raise PlanTaskCreationError(
                "计划校验没通过，没有生成任何下载任务：\n" + result.message()
            )

        items = plan.ordered_enabled_items()
        if not items:
            raise PlanTaskCreationError(
                "计划里没有一个可用的启用项：先加入区域并完成一次估算"
            )

        directory = self._effective_output_dir(output_dir)
        paths = planned_output_paths(plan, output_dir=directory)
        layer = plan.map_layer or MapType.VECTOR.base_layer.value
        created: list[DownloadTask] = []

        try:
            for item in items:
                region = self._region_service.get_download_region(item.region_id)
                task = self._task_service.create_task(
                    name=f"{item.region_name or item.region_id} {item.zoom_text}",
                    bounds=region.bounds,
                    min_zoom=item.min_zoom,
                    max_zoom=item.max_zoom,
                    map_layer=layer,
                    output_path=paths[item.region_id],
                    # Spec §7：任务的目标张数就是这一项的**选中**张数
                    total_tiles=item.selected_tiles,
                    with_labels=plan.with_labels,
                    region=region,
                )
                created.append(task)
                _logger.info(
                    "计划 %s 生成任务 id=%s region=%s %s -> %s",
                    plan.id,
                    task.id,
                    item.region_id,
                    item.zoom_text,
                    task.output_path,
                )
        except (
            RegionCatalogError,
            RegionBoundaryMissing,
            GeoJSONError,
            TaskStoreError,
            OSError,
            ValueError,
        ) as exc:
            self._rollback(created)
            raise PlanTaskCreationError(
                f"生成下载任务失败（已回滚这次新建的 {len(created)} 条记录）：{exc}"
            ) from exc

        return created

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _rollback(self, created: list[DownloadTask]) -> None:
        """删掉本次已经建出来的任务记录（**只删记录**，不碰任何文件）。"""
        for task in created:
            try:
                self._task_service.delete_task(task.id)
            except (TaskStoreError, OSError) as exc:  # pragma: no cover - 磁盘异常
                _logger.error("回滚任务记录 %s 失败：%s", task.id, exc)
        if created:
            _logger.warning("已回滚本次新建的 %d 条任务记录", len(created))

    def _effective_output_dir(self, output_dir: object = None) -> str:
        """定出这次用哪个输出目录：显式给的 > 计划里的 > 默认的。"""
        if output_dir is not None and str(output_dir).strip():
            return str(output_dir).strip()
        return self.plan.output_dir or self._default_output_dir
