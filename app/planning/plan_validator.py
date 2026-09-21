"""计划校验（Slice 20，Spec §9）。

把"这个计划能不能拿去生成任务"变成一份**可读的问题清单**：哪些是错误
（不修就没法生成），哪些只是提醒（照下不误，但你该知道）。

为什么是"清单"而不是一个布尔值
------------------------------

:class:`~app.planning.plan_estimator.DownloadPlanEstimator` 在估算时会逐个条目
把"区域被删了 / 边界文件没了"标成不可用。如果校验只回一个 ``False``，
界面就只能说"计划有问题"——而用户手里可能是一个 5 条目的计划，他需要知道
**是哪一条、为什么**。因此这里返回 ``errors`` + ``warnings``，
每条都带着"谁"和"人话"（Slice 14 的教训：界面里不出现错误码，
机器码只在 :meth:`PlanIssue.as_line` 里，给日志 grep 用）。

错误与提醒的分界（Spec §9）
---------------------------

============================  ==============================================
**错误**（不修就没法生成）      计划空了 / 一个启用的条目都没有 / 区域不在目录里 /
                                边界文件缺失 / Zoom 非法或上下界反了 /
                                同一条目或同一区域出现两次 / 等级与目录对不上 /
                                这一项算不出瓦片（含"还没估算"）/
                                输出目录不可用
**提醒**（只提示，不拦）        超出该等级的推荐 Zoom / 计划规模偏大 /
                                目标文件已经存在
============================  ==============================================

"超出推荐 Zoom"归到提醒而不是错误，是 Spec §6 的明确要求：全国 Z4~18 这种
配置**可以存在于计划里**，但必须显示策略警告。校验器只负责把警告摆出来，
不负责把它改掉 —— 本模块里没有任何一处会写 ``item.min_zoom`` / ``max_zoom``。

哪些检查只看启用的条目
----------------------

- **结构性**问题（区域在不在、边界在不在、Zoom 合不合法、等级对不对、
  有没有重复）对**所有**条目都查：它们描述的是"这份配置本身是否自洽"，
  与"这次下不下"无关。
- **后果性**问题（算不算得出瓦片、Zoom 是否超出推荐、文件是否已存在、
  规模是否偏大）只看**启用且可用**的条目：用户把一个条目关掉，就是在说
  "这次不下它"，再去提醒它"没有瓦片""会占 5GB"只是噪音。

校验是**带副作用**的
--------------------

:meth:`PlanValidator.validate` 会把每个条目的
:attr:`~app.planning.download_plan.DownloadPlanItem.status` 刷新到"当前事实"
（区域没了就标记不可用），并重算一次计划总量。理由：判断"现在到底行不行"
这件事只能有一个权威 —— 如果校验器说"区域不存在"而条目的状态还写着"可用"，
界面上就会出现自相矛盾的两行。

它**只标记不可用，从不标记可用**：证明"可用"的唯一办法是真的把几何读出来算
一遍，那是估算器的工作（:meth:`~app.planning.plan_estimator.DownloadPlanEstimator.estimate`）。
"文件在"不等于"文件能读出几何"——空文件、纯文本、错误坐标都会被
:class:`~app.geo.GeoJSONError` 拦下。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from app.config.settings import (
    PLAN_LARGE_SIZE_WARNING_BYTES,
    PLAN_LARGE_TILES_WARNING,
)
from app.geo.tile_math import format_size, validate_zoom
from app.planning.download_plan import (
    DownloadPlan,
    DownloadPlanItem,
    planned_output_paths,
    plan_directory,
)
from app.planning.zoom_strategy import ZoomStrategyService
from app.regions.models import IssueSeverity, RegionCatalogError, RegionPreset
from app.regions.service import RegionService

__all__ = [
    "PlanIssueCode",
    "PlanIssue",
    "PlanValidationResult",
    "PlanValidator",
]


_logger = logging.getLogger(__name__)

#: 严重程度沿用区域目录那一套（:class:`~app.regions.IssueSeverity`）：
#: 只有 ERROR / WARNING 两档，而且**同一个概念**在两处各写一个枚举，
#: 只会让界面不得不写 `if severity is A or severity is B` 这种代码。
Severity = IssueSeverity


class PlanIssueCode(Enum):
    """问题的稳定机器码（日志 grep、测试断言、报告分栏都用它）。

    ``value`` 是持久化契约：文档与验收脚本会引用它，改名等于让历史日志对不上号。
    """

    # --- 计划级 ---
    EMPTY_PLAN = "empty_plan"
    NO_ENABLED_ITEM = "no_enabled_item"
    LEVEL_MISMATCH = "level_mismatch"
    DUPLICATE_ITEM_ID = "duplicate_item_id"
    DUPLICATE_REGION = "duplicate_region"
    OUTPUT_DIR_UNAVAILABLE = "output_dir_unavailable"
    LARGE_TILE_COUNT = "large_tile_count"
    LARGE_SIZE = "large_size"

    # --- 条目级 ---
    REGION_MISSING = "region_missing"
    BOUNDARY_MISSING = "boundary_missing"
    ZOOM_INVALID = "zoom_invalid"
    ZOOM_ORDER_INVALID = "zoom_order_invalid"
    ITEM_INVALID = "item_invalid"
    NO_TILES = "no_tiles"
    ZOOM_OUT_OF_RECOMMENDATION = "zoom_out_of_recommendation"
    OUTPUT_FILE_EXISTS = "output_file_exists"


@dataclass(frozen=True)
class PlanIssue:
    """一条计划问题。

    Attributes:
        code: 机器码（见 :class:`PlanIssueCode`）。
        message: 给人看的一句话（**不含错误码**）。
        item_id: 相关条目 id；计划级问题为空串。
        region_id: 相关区域 id；计划级问题为空串。
        severity: 严重程度。
    """

    code: PlanIssueCode
    message: str
    item_id: str = ""
    region_id: str = ""
    severity: Severity = Severity.ERROR

    def __post_init__(self) -> None:
        if not isinstance(self.code, PlanIssueCode):
            raise ValueError(f"code 必须是 PlanIssueCode，实际为 {self.code!r}")
        if not isinstance(self.severity, Severity):
            raise ValueError(f"severity 必须是 IssueSeverity，实际为 {self.severity!r}")
        object.__setattr__(self, "message", str(self.message).strip())
        object.__setattr__(self, "item_id", str(self.item_id or "").strip())
        object.__setattr__(self, "region_id", str(self.region_id or "").strip())

    @property
    def is_error(self) -> bool:
        """是否是"不修就没法生成任务"的问题。"""
        return self.severity is Severity.ERROR

    def subject(self) -> str:
        """这条问题说的是谁（计划级问题说的是"计划"）。"""
        return self.region_id or "计划"

    def describe(self) -> str:
        """面向人的一行（**不带机器码**，给界面与报告用）。"""
        return self.message

    def as_line(self) -> str:
        """面向日志的一行（带机器码，好 grep）。"""
        return f"{self.severity.value} {self.code.value} [{self.subject()}] {self.message}"


@dataclass(frozen=True)
class PlanValidationResult:
    """一次计划校验的结论（Spec §9）。

    Attributes:
        issues: 全部问题，顺序即发现顺序（先是计划级，再按条目顺序）。
        checked: 参与校验的条目数。
        plan_id: 被校验的计划 id。
        directory: 这次校验认定的输出目录（计划级问题定位用）；没查输出时为
            ``None``。
    """

    issues: tuple[PlanIssue, ...] = ()
    checked: int = 0
    plan_id: str = ""
    directory: Path | None = None

    @property
    def errors(self) -> tuple[PlanIssue, ...]:
        """错误。"""
        return tuple(item for item in self.issues if item.is_error)

    @property
    def warnings(self) -> tuple[PlanIssue, ...]:
        """提醒。"""
        return tuple(item for item in self.issues if not item.is_error)

    @property
    def ok(self) -> bool:
        """能不能拿去生成任务（**只看 error**：提醒不拦）。"""
        return not self.errors

    def count(self, code: PlanIssueCode) -> int:
        """按机器码数一数。"""
        return sum(1 for item in self.issues if item.code is code)

    def has(self, code: PlanIssueCode) -> bool:
        """有没有某类问题。"""
        return self.count(code) > 0

    def message(self) -> str:
        """把错误拼成一段可以直接显示的文本（没问题时是空串）。"""
        return "\n".join(item.describe() for item in self.errors)

    def as_lines(self) -> list[str]:
        """报告正文行（人话在前，机器码只在末尾的统计行里）。"""
        lines = [
            f"下载计划校验：{'通过' if self.ok else '不通过'}"
            f"（{self.checked} 项，{len(self.errors)} 个错误 / "
            f"{len(self.warnings)} 个提醒）"
        ]
        lines.extend(f"错误：{item.describe()}" for item in self.errors)
        lines.extend(f"提醒：{item.describe()}" for item in self.warnings)
        if self.issues:
            lines.append(
                "机器码统计："
                + "、".join(
                    f"{code.value}×{self.count(code)}"
                    for code in PlanIssueCode
                    if self.has(code)
                )
            )
        return lines


class PlanValidator:
    """校验一份计划（Spec §9）。

    Attributes:
        service: 区域业务入口（查区域是否存在、边界文件是否就位）。
        strategy: Zoom 策略服务（推荐区间与警告文案的唯一来源）。
    """

    def __init__(
        self,
        service: RegionService | None = None,
        *,
        strategy: ZoomStrategyService | None = None,
        large_tiles: int = PLAN_LARGE_TILES_WARNING,
        large_size_bytes: int = PLAN_LARGE_SIZE_WARNING_BYTES,
    ) -> None:
        """
        Args:
            service: 区域服务；``None`` 时自建。
            strategy: Zoom 策略服务；``None`` 时取 ``service.strategy``。
            large_tiles: "规模偏大"的张数阈值（只警告）。
            large_size_bytes: "规模偏大"的容量阈值（只警告）。
        """
        self._service = service if service is not None else RegionService()
        self._strategy = strategy if strategy is not None else self._service.strategy
        self._large_tiles = int(large_tiles)
        self._large_size_bytes = int(large_size_bytes)

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

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #
    def validate(
        self,
        plan: DownloadPlan,
        *,
        output_dir: object = None,
        check_output: bool = True,
    ) -> PlanValidationResult:
        """校验一份计划（Spec §9）。

        Args:
            plan: 要校验的计划。
            output_dir: 覆盖计划自带的输出目录（界面上可能刚改过）。
            check_output: 是否检查输出目录与"目标文件已存在"。
                关掉它可以在"只想知道配置自不自洽"的场景下少碰一次磁盘。

        Returns:
            :class:`PlanValidationResult`。
        """
        issues: list[PlanIssue] = []
        plan.recalculate_totals()

        if plan.is_empty:
            issues.append(
                PlanIssue(
                    code=PlanIssueCode.EMPTY_PLAN,
                    message=(
                        "计划里还没有任何区域：先在「下载范围」里选好国家 / 省 / 市，"
                        "再点「加入下载计划」"
                    ),
                )
            )
        elif plan.enabled_count == 0:
            issues.append(
                PlanIssue(
                    code=PlanIssueCode.NO_ENABLED_ITEM,
                    message=(
                        "计划里没有一个可用的启用项：至少启用一项"
                        "（且它的边界文件要就位）才能生成下载任务"
                    ),
                )
            )

        self._check_duplicates(plan, issues)

        directory = plan_directory(plan, output_dir=output_dir) if check_output else None
        paths = (
            planned_output_paths(plan, output_dir=output_dir) if check_output else {}
        )
        if (
            directory is not None
            and not plan.is_empty
            and not _directory_usable(directory)
        ):
            # 计划级查一次就够：目录是计划共享的，逐条目报同一句话只会刷屏。
            issues.append(
                PlanIssue(
                    code=PlanIssueCode.OUTPUT_DIR_UNAVAILABLE,
                    message=(
                        f"输出目录不可用：{directory}"
                        "（同名位置被一个文件占住了，或者这个位置没有写权限）"
                    ),
                )
            )

        for item in plan.ordered_items():
            self._check_item(item, issues, paths)

        self._check_totals(plan, issues)
        return PlanValidationResult(
            issues=tuple(issues),
            checked=plan.count,
            plan_id=plan.id,
            directory=directory,
        )

    # ------------------------------------------------------------------ #
    # 内部：计划级
    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_duplicates(
        plan: DownloadPlan, issues: list[PlanIssue]
    ) -> None:
        """条目 id 与区域 id 都不能重复（Spec §5 的下限保证）。

        Spec §5 要求"同一区域只允许一个启用项"，而
        :class:`DownloadPlanBuilder` 已经靠"更新而不是新增"实现了它。这里再查
        一遍，是因为 ``plans.json`` 是**人可以编辑的文件**，也可能来自更早的
        版本 —— 那种情况下重复项会变成两个指向同一块地、写到同一个文件的任务。
        """
        seen_ids: dict[str, int] = {}
        seen_regions: dict[str, int] = {}
        for index, item in enumerate(plan.items, start=1):
            seen_ids[item.id] = seen_ids.get(item.id, 0) + 1
            seen_regions[item.region_id] = seen_regions.get(item.region_id, 0) + 1
            if seen_regions[item.region_id] > 1:
                issues.append(
                    PlanIssue(
                        code=PlanIssueCode.DUPLICATE_REGION,
                        message=(
                            f"同一个区域在计划里出现了多次（第 {index} 项）："
                            f"「{item.region_name or item.region_id}」"
                            "只会生成一个下载任务，请先移除多余的项"
                        ),
                        item_id=item.id,
                        region_id=item.region_id,
                    )
                )
            if seen_ids[item.id] > 1:
                issues.append(
                    PlanIssue(
                        code=PlanIssueCode.DUPLICATE_ITEM_ID,
                        message=(
                            f"两个计划项用了同一个内部 id（第 {index} 项）："
                            "文件可能被手工编辑过，建议删掉这一项重新加入"
                        ),
                        item_id=item.id,
                        region_id=item.region_id,
                    )
                )

    def _check_totals(self, plan: DownloadPlan, issues: list[PlanIssue]) -> None:
        """计划级的规模提醒（只警告，Spec §9）。"""
        if plan.total_tiles > self._large_tiles:
            issues.append(
                PlanIssue(
                    code=PlanIssueCode.LARGE_TILE_COUNT,
                    message=(
                        f"计划规模偏大：{plan.total_tiles:,} 张瓦片"
                        f"（超过 {self._large_tiles:,} 张）。"
                        "可以在计划里关掉暂时不需要的项再生成任务"
                    ),
                    severity=Severity.WARNING,
                )
            )
        if plan.estimated_bytes > self._large_size_bytes:
            issues.append(
                PlanIssue(
                    code=PlanIssueCode.LARGE_SIZE,
                    message=(
                        f"预计容量偏大：{format_size(plan.estimated_bytes)}"
                        f"（超过 {format_size(self._large_size_bytes)}）。"
                        "请确认磁盘剩余空间"
                    ),
                    severity=Severity.WARNING,
                )
            )

    # ------------------------------------------------------------------ #
    # 内部：条目级
    # ------------------------------------------------------------------ #
    def _check_item(
        self,
        item: DownloadPlanItem,
        issues: list[PlanIssue],
        paths: dict[str, Path],
    ) -> None:
        """校验一个条目（结构性问题对全部条目查，后果性只看启用项）。"""
        reported, zoom_ok = self._check_zoom(item, issues)
        if zoom_ok:
            # Zoom 非法时不必再谈"推荐区间"：那会在一件真问题上盖一层噪音，
            # 而且 evaluate() 本身就会因为非法输入抛异常。
            entry = self._check_region(item, issues, reported)
            if entry is not None and item.enabled:
                self._check_effective(item, issues, paths)

        if not item.enabled:
            return
        if not item.is_valid and not reported:
            # 状态是估算阶段标的（例如边界文件存在但读不出几何）。校验器只把
            # 它转述出来——**不在这里重新判定**，因为"能不能读出几何"只有真的
            # 读一遍才知道（Spec §9 的"GeoJSON 可用"）。
            issues.append(
                PlanIssue(
                    code=PlanIssueCode.ITEM_INVALID,
                    message=(
                        f"「{item.region_name or item.region_id}」当前不可用："
                        f"{item.invalid_reason or '无法估算它的瓦片数'}"
                    ),
                    item_id=item.id,
                    region_id=item.region_id,
                )
            )

    def _check_zoom(
        self, item: DownloadPlanItem, issues: list[PlanIssue]
    ) -> tuple[set[PlanIssueCode], bool]:
        """Zoom 合法性（Spec §9）。返回 ``(已报过的码, 是否可继续)``。"""
        reported: set[PlanIssueCode] = set()
        name = item.region_name or item.region_id
        try:
            # 与 Tile Math 用同一道校验，不在这里另写一套范围判断
            low = validate_zoom(item.min_zoom)
            high = validate_zoom(item.max_zoom)
        except ValueError as exc:
            issues.append(
                PlanIssue(
                    code=PlanIssueCode.ZOOM_INVALID,
                    message=f"「{name}」的缩放级别不合法：{exc}",
                    item_id=item.id,
                    region_id=item.region_id,
                )
            )
            return reported | {PlanIssueCode.ZOOM_INVALID}, False

        if low > high:
            issues.append(
                PlanIssue(
                    code=PlanIssueCode.ZOOM_ORDER_INVALID,
                    message=(
                        f"「{name}」的缩放区间反了（{low} ~ {high}）："
                        "小的一头要写在左边"
                    ),
                    item_id=item.id,
                    region_id=item.region_id,
                )
            )
            return reported | {PlanIssueCode.ZOOM_ORDER_INVALID}, False
        return reported, True

    def _check_region(
        self,
        item: DownloadPlanItem,
        issues: list[PlanIssue],
        reported: set[PlanIssueCode],
    ) -> RegionPreset | None:
        """区域是否还在目录里、边界是否就位（Spec §9 / §18）。"""
        name = item.region_name or item.region_id
        try:
            entry = self._service.require_region(item.region_id)
        except RegionCatalogError:
            item.mark_invalid("这个区域已经不在区域目录里了")
            issues.append(
                PlanIssue(
                    code=PlanIssueCode.REGION_MISSING,
                    message=(
                        f"计划里的区域「{name}」已经不在区域目录里了："
                        "它不会生成下载任务（可以在计划里移除它，"
                        "或者把区域数据放回资源目录）"
                    ),
                    item_id=item.id,
                    region_id=item.region_id,
                )
            )
            reported.add(PlanIssueCode.REGION_MISSING)
            return None

        if item.region_level is not entry.level:
            # 等级只认目录（Spec §9）：判定目录里那条记录说了算。
            # 照计划里那份去看策略区间，会给出一个"按目录看并不适用"的建议。
            issues.append(
                PlanIssue(
                    code=PlanIssueCode.LEVEL_MISMATCH,
                    message=(
                        f"「{entry.name}」在计划里记的是{item.level_text}，"
                        f"而目录里现在是{entry.level.display_name}："
                        "建议把它从计划里移除再重新加入"
                    ),
                    item_id=item.id,
                    region_id=item.region_id,
                )
            )
            reported.add(PlanIssueCode.LEVEL_MISMATCH)

        if not self._service.boundary_available(item.region_id):
            item.mark_invalid("边界文件不在")
            issues.append(
                PlanIssue(
                    code=PlanIssueCode.BOUNDARY_MISSING,
                    message=self._service.missing_message(item.region_id),
                    item_id=item.id,
                    region_id=item.region_id,
                )
            )
            reported.add(PlanIssueCode.BOUNDARY_MISSING)
        return entry

    def _check_effective(
        self,
        item: DownloadPlanItem,
        issues: list[PlanIssue],
        paths: dict[str, Path],
    ) -> None:
        """只看启用项的三件事：有没有瓦片、Zoom 是否超推荐、文件是否已存在。"""
        self._check_tiles(item, issues)
        self._check_recommendation(item, issues)
        self._check_output_file(item, issues, paths)

    @staticmethod
    def _check_tiles(
        item: DownloadPlanItem, issues: list[PlanIssue]
    ) -> None:
        """``selected_tiles > 0``（Spec §9）。"""
        name = item.region_name or item.region_id
        if not item.is_estimated:
            issues.append(
                PlanIssue(
                    code=PlanIssueCode.NO_TILES,
                    message=(
                        f"「{name}」还没有估算过：先点一次「重新估算」，"
                        "再生成下载任务"
                    ),
                    item_id=item.id,
                    region_id=item.region_id,
                )
            )
            return
        if item.selected_tiles <= 0:
            issues.append(
                PlanIssue(
                    code=PlanIssueCode.NO_TILES,
                    message=(
                        f"「{name}」在当前 Zoom（{item.zoom_text}）下没有任何瓦片："
                        "把 Zoom 调高一点（或换一块区域）再试"
                    ),
                    item_id=item.id,
                    region_id=item.region_id,
                )
            )

    def _check_recommendation(
        self, item: DownloadPlanItem, issues: list[PlanIssue]
    ) -> None:
        """Zoom 与推荐区间的关系（Spec §6 / §9）。

        文案直接取 :attr:`~app.planning.ZoomRecommendation.warning_text`
        （它由 :class:`~app.planning.ZoomPolicy` 的 ``description`` 拼成），
        本模块不抄那份说明 —— Slice 17 的静态守卫盯着"推荐文案不许散落"。
        """
        recommendation = self._strategy.evaluate(
            item.region_level, item.min_zoom, item.max_zoom
        )
        name = item.region_name or item.region_id
        if recommendation.warning:
            issues.append(
                PlanIssue(
                    code=PlanIssueCode.ZOOM_OUT_OF_RECOMMENDATION,
                    message=f"「{name}」{recommendation.warning_text}",
                    item_id=item.id,
                    region_id=item.region_id,
                    severity=Severity.WARNING,
                )
            )
        elif recommendation.exceeds_recommendation:
            # 超出建议但还没到"该警告"的阈值（例如省级的过渡带）。仍然要说
            # 一声：用户可能正需要它是"故意的"——那就更该看见了。
            issues.append(
                PlanIssue(
                    code=PlanIssueCode.ZOOM_OUT_OF_RECOMMENDATION,
                    message=(
                        f"「{name}」的 {item.zoom_text} 超出了"
                        f"{recommendation.level_name}的推荐范围"
                        f"（{recommendation.range_text}），会按你设的范围下载"
                    ),
                    item_id=item.id,
                    region_id=item.region_id,
                    severity=Severity.WARNING,
                )
            )

    @staticmethod
    def _check_output_file(
        item: DownloadPlanItem,
        issues: list[PlanIssue],
        paths: dict[str, Path],
    ) -> None:
        """目标文件是不是已经存在（Spec §15"不覆盖已有文件而不确认"）。

        这里**只是提醒**：本 Slice 不启动下载，而下载启动时本来就会问
        "继续还是重下"（Slice 5 的既定行为）。放在校验里是为了让用户在
        「生成任务」之前就知道"这次会撞上上次的成果"。
        """
        path = paths.get(item.region_id)
        if path is None or not path.is_file():
            return
        issues.append(
            PlanIssue(
                code=PlanIssueCode.OUTPUT_FILE_EXISTS,
                message=(
                    f"「{item.region_name or item.region_id}」的目标文件已存在："
                    f"{path}。开始下载时会先问你是继续还是重下，不会静默覆盖"
                ),
                item_id=item.id,
                region_id=item.region_id,
                severity=Severity.WARNING,
            )
        )


def _directory_usable(directory: Path) -> bool:
    """输出目录能不能用来写文件（**尽力判断**，不创建任何东西）。

    规则：往上找到第一个**已存在**的祖先，要求它是目录且可写。
    刻意不 ``mkdir``：校验不该有副作用（Spec §16 要求本 Slice 只创建任务配置，
    连目录都由下载阶段去建）。

    局限要说清：Windows 上的 ACL 拒绝经常逃过 ``os.access``，因此这只是一道
    "明显不行"的网（路径上有个同名文件、盘符不存在、只读目录），
    真正的写入失败仍由下载阶段报出来。
    """
    probe = directory
    while True:
        if probe.exists():
            break
        parent = probe.parent
        if parent == probe:  # pragma: no cover - 到了盘根还没有存在的祖先
            return False
        probe = parent
    if not probe.is_dir():
        return False
    return os.access(probe, os.W_OK)
