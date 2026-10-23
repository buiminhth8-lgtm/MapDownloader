"""计划校验（Slice 20，Spec §9 / §6 / §15）。

守四件事：

1. **错误与提醒的分界**（Spec §9）：不修就没法生成任务的是 error；只是"你该
   知道"的是 warning（超推荐 Zoom、规模偏大、目标文件已存在）。
2. **校验器只标记不可用，从不标记可用**："能不能读出几何"只有真读一遍才知道，
   那是估算器的活。校验器凭"文件在"就宣布可用，会让空文件/坏坐标混过去。
3. **不改用户的值**（Spec §6）：超推荐只提醒，Zoom 一个字都不许被改。
4. **界面里不出现机器码**：错误码只允许出现在 ``as_line()`` / ``as_lines()``
   的统计行里（给日志 grep），给人看的那一段必须全是人话。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.planning.download_plan import (
    DownloadPlan,
    DownloadPlanItem,
    PlanItemStatus,
    planned_output_paths,
)
from app.planning.plan_builder import DownloadPlanBuilder
from app.planning.plan_estimator import DownloadPlanEstimator
from app.planning.plan_validator import (
    PlanIssue,
    PlanIssueCode,
    PlanValidationResult,
    PlanValidator,
)
from app.planning.region_level import RegionLevel
from app.regions.models import IssueSeverity
from app.regions.service import RegionService


@pytest.fixture
def service(region_resources: Path) -> RegionService:
    return RegionService()


@pytest.fixture
def validator(service: RegionService) -> PlanValidator:
    return PlanValidator(service)


@pytest.fixture
def builder(service: RegionService) -> DownloadPlanBuilder:
    return DownloadPlanBuilder(service)


@pytest.fixture
def estimator(service: RegionService) -> DownloadPlanEstimator:
    return DownloadPlanEstimator(service)


def _ready_plan(
    builder: DownloadPlanBuilder,
    estimator: DownloadPlanEstimator,
    *region_ids: str,
    output_dir: Path | str | None = None,
) -> DownloadPlan:
    """一份"算过、可用"的计划（校验通过的最小输入）。"""
    for region_id in region_ids:
        builder.add_region(region_id)
    plan = builder.plan
    if output_dir is not None:
        plan.output_dir = str(output_dir)
    estimator.estimate(plan)
    return plan


# --------------------------------------------------------------------------- #
# 计划级错误
# --------------------------------------------------------------------------- #
def test_empty_plan_is_an_error(validator: PlanValidator) -> None:
    result = validator.validate(DownloadPlan(name="空计划"))

    assert result.ok is False
    assert result.has(PlanIssueCode.EMPTY_PLAN)
    assert result.checked == 0
    assert "加入下载计划" in result.message()


def test_all_items_disabled_is_an_error(
    validator: PlanValidator, builder: DownloadPlanBuilder, estimator: DownloadPlanEstimator
) -> None:
    """一个启用项都没有 → 拦下（否则"生成任务"会建出 0 个任务还说成功）。"""
    plan = _ready_plan(builder, estimator, "demoland")
    builder.set_enabled("demoland", False)

    result = validator.validate(plan)

    assert result.has(PlanIssueCode.NO_ENABLED_ITEM)
    assert result.ok is False
    assert result.has(PlanIssueCode.EMPTY_PLAN) is False


def test_duplicate_region_is_an_error(validator: PlanValidator) -> None:
    """``plans.json`` 是人可编辑的文件 → 重复项必须被查出来（Spec §5 的下限）。

    刻意**绕过** :meth:`DownloadPlan.add_item`（它对同一个 ``region_id`` 是
    更新而不是追加，Spec §5），直接往列表里塞两条 —— 这正是"文件被手工改过"
    的样子，也是这条检查存在的唯一理由。
    """
    plan = DownloadPlan(name="手工改过的计划")
    plan.items.append(DownloadPlanItem(region_id="demoland", region_name="示例国"))
    plan.items.append(DownloadPlanItem(region_id="demoland", region_name="示例国"))
    assert plan.count == 2, "夹具失效：add_item 本该把它们合并成一条"

    result = validator.validate(plan, check_output=False)

    assert result.has(PlanIssueCode.DUPLICATE_REGION)
    assert result.ok is False
    assert result.count(PlanIssueCode.DUPLICATE_REGION) == 1


def test_duplicate_item_id_is_an_error(validator: PlanValidator) -> None:
    plan = DownloadPlan(name="坏计划")
    plan.items.append(DownloadPlanItem(id="same", region_id="demoland", region_name="示例国"))
    plan.items.append(
        DownloadPlanItem(id="same", region_id="northland", region_name="示例北省")
    )

    result = validator.validate(plan, check_output=False)

    assert result.has(PlanIssueCode.DUPLICATE_ITEM_ID)


def test_output_directory_unavailable_is_one_plan_level_error(
    validator: PlanValidator,
    builder: DownloadPlanBuilder,
    estimator: DownloadPlanEstimator,
    work_dir: Path,
) -> None:
    """输出位置被一个同名**文件**占住 → 报一次（不是每一项报一次）。"""
    plan = _ready_plan(builder, estimator, "demoland", "northland")
    blocker = work_dir / "blocker"
    blocker.write_text("我是一个文件，不是目录", encoding="utf-8")

    result = validator.validate(plan, output_dir=blocker / "sub")

    assert result.has(PlanIssueCode.OUTPUT_DIR_UNAVAILABLE)
    assert result.count(PlanIssueCode.OUTPUT_DIR_UNAVAILABLE) == 1
    assert result.ok is False
    assert str(blocker) in result.message()


def test_validator_never_creates_the_output_directory(
    validator: PlanValidator,
    builder: DownloadPlanBuilder,
    estimator: DownloadPlanEstimator,
    work_dir: Path,
) -> None:
    """Spec §16：本 Slice 只创建任务配置，连目录都不许提前建。"""
    target = work_dir / "还不存在的输出目录"
    plan = _ready_plan(builder, estimator, "demoland", output_dir=target)

    validator.validate(plan)

    assert target.exists() is False


# --------------------------------------------------------------------------- #
# 条目级错误
# --------------------------------------------------------------------------- #
def test_missing_region_is_an_error_and_marks_the_item_invalid(
    validator: PlanValidator,
    builder: DownloadPlanBuilder,
    estimator: DownloadPlanEstimator,
) -> None:
    """区域被删（Spec §18）：这一项不可用，但计划本身照常打开与校验。"""
    plan = _ready_plan(builder, estimator, "demoland")
    plan.add_item(DownloadPlanItem(region_id="nowhere", region_name="不存在的地区"))

    result = validator.validate(plan)

    assert result.has(PlanIssueCode.REGION_MISSING)
    assert result.ok is False
    item = plan.item("nowhere")
    assert item.status is PlanItemStatus.INVALID
    assert item.invalid_reason
    # 剩下的那一项没问题（错误只来自坏项）
    assert result.count(PlanIssueCode.REGION_MISSING) == 1


def test_missing_boundary_file_is_an_error(
    validator: PlanValidator,
    builder: DownloadPlanBuilder,
    estimator: DownloadPlanEstimator,
    region_resources: Path,
) -> None:
    (region_resources / "province" / "northland.geojson").unlink()
    plan = _ready_plan(builder, estimator, "northland")

    result = validator.validate(plan)

    assert result.has(PlanIssueCode.BOUNDARY_MISSING)
    assert result.ok is False
    assert plan.item("northland").status is PlanItemStatus.INVALID
    # 文案直接用区域服务那句（"缺边界"是目录层的 WARNING），校验器不另写一套说法
    assert validator.service.missing_message("northland") in result.message()
    assert all(code.value not in result.message() for code in PlanIssueCode)


def test_illegal_zoom_is_an_error(
    validator: PlanValidator,
    builder: DownloadPlanBuilder,
    estimator: DownloadPlanEstimator,
) -> None:
    plan = _ready_plan(builder, estimator, "northland")
    plan.item("northland").max_zoom = 99

    result = validator.validate(plan)

    assert result.has(PlanIssueCode.ZOOM_INVALID)
    assert result.ok is False
    # 非法 Zoom 时不再叠一层"超出推荐"的噪音（推荐区间对非法输入没有意义）
    assert result.has(PlanIssueCode.ZOOM_OUT_OF_RECOMMENDATION) is False


def test_inverted_zoom_is_an_error(
    validator: PlanValidator,
    builder: DownloadPlanBuilder,
    estimator: DownloadPlanEstimator,
) -> None:
    plan = _ready_plan(builder, estimator, "northland")
    item = plan.item("northland")
    item.min_zoom, item.max_zoom = 13, 9

    result = validator.validate(plan)

    assert result.has(PlanIssueCode.ZOOM_ORDER_INVALID)
    assert "13" in result.message() and "9" in result.message()


def test_zero_selected_tiles_is_an_error(
    validator: PlanValidator, builder: DownloadPlanBuilder
) -> None:
    """Spec §9：``selected_tiles > 0``。已估算但一张都选不中 → 拦下。"""
    builder.add_region("rivertown")
    plan = builder.plan
    item = plan.item("rivertown")
    item.candidate_tiles = 12  # ← 算过了
    item.selected_tiles = 0  # ← 但选不中任何瓦片（合法结论，不是"没算"）
    plan.recalculate_totals()

    result = validator.validate(plan, check_output=False)

    assert result.has(PlanIssueCode.NO_TILES)
    assert result.ok is False
    assert "还没有估算过" not in result.message()


def test_not_estimated_yet_is_an_error_with_a_different_message(
    validator: PlanValidator, builder: DownloadPlanBuilder
) -> None:
    """没算过也是 NO_TILES，但文案必须说"还没估算"（否则用户去调 Zoom 白费劲）。"""
    builder.add_region("rivertown")

    result = validator.validate(builder.plan, check_output=False)

    assert result.has(PlanIssueCode.NO_TILES)
    assert "还没有估算过" in result.message()


def test_level_mismatch_is_an_error(
    validator: PlanValidator,
    builder: DownloadPlanBuilder,
    estimator: DownloadPlanEstimator,
) -> None:
    """等级只认目录（Spec §9）：计划里记的等级与目录不一致 → 报出来。"""
    plan = _ready_plan(builder, estimator, "rivertown")
    plan.item("rivertown").region_level = RegionLevel.COUNTRY

    result = validator.validate(plan, check_output=False)

    assert result.has(PlanIssueCode.LEVEL_MISMATCH)
    assert result.ok is False


def test_validator_reports_an_item_marked_invalid_by_the_estimator(
    validator: PlanValidator,
    builder: DownloadPlanBuilder,
    estimator: DownloadPlanEstimator,
) -> None:
    """状态是估算阶段标的（例如边界在、但读不出几何）→ 校验器只转述，不重判。"""
    plan = _ready_plan(builder, estimator, "demoland")
    plan.item("demoland").mark_invalid("瓦片库读不出几何")

    result = validator.validate(plan)

    assert result.has(PlanIssueCode.ITEM_INVALID)
    assert result.ok is False
    assert "瓦片库读不出几何" in result.message()


def test_validator_never_flips_an_invalid_item_back_to_valid(
    validator: PlanValidator,
    builder: DownloadPlanBuilder,
    estimator: DownloadPlanEstimator,
) -> None:
    """**只标记不可用，从不标记可用**：证明"可用"要靠真的读一遍几何。"""
    plan = _ready_plan(builder, estimator, "demoland")
    item = plan.item("demoland")
    item.mark_invalid("模拟：几何读不出来")

    validator.validate(plan)

    assert item.status is PlanItemStatus.INVALID


# --------------------------------------------------------------------------- #
# 只看启用项的那些检查（Spec §9）
# --------------------------------------------------------------------------- #
def test_disabled_item_does_not_produce_effect_warnings(
    validator: PlanValidator, builder: DownloadPlanBuilder, estimator: DownloadPlanEstimator
) -> None:
    """把一个条目关掉，就是在说"这次不下它" → 别再提醒它"没算过""会占 5GB"。"""
    plan = _ready_plan(builder, estimator, "demoland", "northland")
    builder.set_enabled("northland", False)
    plan.item("northland").clear_estimate()  # 关掉之后连估算结果都清了
    plan.recalculate_totals()

    result = validator.validate(plan, check_output=False)

    assert result.ok is True, result.message()
    assert result.count(PlanIssueCode.NO_TILES) == 0


def test_disabled_item_still_gets_structural_checks(
    validator: PlanValidator, builder: DownloadPlanBuilder, estimator: DownloadPlanEstimator
) -> None:
    """但**结构性**问题照查：关掉它不代表"配置本身"就自洽了。"""
    plan = _ready_plan(builder, estimator, "demoland")
    plan.add_item(
        DownloadPlanItem(region_id="nowhere", region_name="不存在的地区", enabled=False)
    )

    result = validator.validate(plan, check_output=False)

    assert result.has(PlanIssueCode.REGION_MISSING)
    assert result.ok is False


def test_plan_without_enabled_items_of_any_kind_is_still_not_ok(
    validator: PlanValidator, builder: DownloadPlanBuilder, estimator: DownloadPlanEstimator
) -> None:
    """启用项全坏 → 没有可生成的东西（错误来自 REGION_MISSING，不是 NO_ENABLED_ITEM）。"""
    plan = _ready_plan(builder, estimator, "demoland")
    plan.item("demoland").mark_invalid("模拟坏包")

    result = validator.validate(plan)

    assert result.ok is False
    assert result.has(PlanIssueCode.ITEM_INVALID)


# --------------------------------------------------------------------------- #
# 提醒（Spec §6 / §9 / §15）
# --------------------------------------------------------------------------- #
def test_a_small_ready_plan_has_no_warnings_at_all(
    validator: PlanValidator, builder: DownloadPlanBuilder, estimator: DownloadPlanEstimator
) -> None:
    """推荐 Zoom + 算过的小计划：既没有错误也没有提醒。"""
    plan = _ready_plan(builder, estimator, "demoland", "northland")

    result = validator.validate(plan)

    assert result.ok is True, result.message()
    assert result.warnings == ()


def test_a_full_three_level_plan_no_longer_trips_the_size_reminder(
    validator: PlanValidator, builder: DownloadPlanBuilder, estimator: DownloadPlanEstimator
) -> None:
    """全国 + 省 + 市 三级的**默认**（推荐）配置，规模已经不到提醒线。

    Slice 29 之前这条断言的是相反的结论：当时推荐区间是 Z4~10 / Z12~16，
    三级加起来约 5.7 万张，撞上"照推荐配一遍就会被提醒一句"。收紧到
    Z5~10 / Z12~15 之后只有约 1.7 万张 —— 推荐值本身已经把规模管住了，
    提醒留给"用户自己非要往高 Zoom 走"的场景（见
    :func:`test_large_plan_is_only_a_warning`，那条用注入阈值验机制本身）。

    留着这条是为了记住那个取舍：**推荐区间不只是"建议"，它同时是规模闸门**。
    把它放回去（比如把城市级改回 Z16），这条会重新变红。
    """
    plan = _ready_plan(builder, estimator, "demoland", "northland", "rivertown")

    result = validator.validate(plan)

    assert result.ok is True, result.message()
    codes = {row.code for row in result.warnings}
    assert PlanIssueCode.LARGE_TILE_COUNT not in codes, (
        f"推荐配置不该撞上规模提醒，实际 {sorted(c.value for c in codes)}"
    )


def test_zoom_beyond_the_recommendation_is_only_a_warning(
    validator: PlanValidator, builder: DownloadPlanBuilder, estimator: DownloadPlanEstimator
) -> None:
    """Spec §6：省域 Z9~15 **可以存在**，只提醒，不改值。

    夹具刻意选 2°×2° 的省而不是 4°×4° 的国：Z18 那种配置要算上千万张瓦片，
    一条用例会把整个测试套件拖成"疑似挂死"（Slice 18 的教训：
    真点一下的代价要按**最坏输入**估）。
    """
    builder.add_region("northland", use_recommended_zoom=False, min_zoom=9, max_zoom=15)
    plan = builder.plan
    estimator.estimate(plan)

    result = validator.validate(plan)

    assert result.has(PlanIssueCode.ZOOM_OUT_OF_RECOMMENDATION)
    assert result.ok is True, result.message()  # 只是提醒，不拦
    item = plan.item("northland")
    assert (item.min_zoom, item.max_zoom) == (9, 15), "校验器不许改用户设的范围"
    assert item.region_name in result.warnings[0].message


def test_recommendation_text_comes_from_the_strategy(
    validator: PlanValidator, builder: DownloadPlanBuilder, estimator: DownloadPlanEstimator
) -> None:
    """提醒文案取自策略层（Slice 17 的静态守卫盯着"推荐文案不许散落"）。"""
    builder.add_region("northland", use_recommended_zoom=False, min_zoom=9, max_zoom=15)
    plan = builder.plan
    estimator.estimate(plan)

    result = validator.validate(plan)

    item = plan.item("northland")
    expected = validator.strategy.evaluate(item.region_level, 9, 15)
    assert expected.warning is True
    assert expected.warning_text in result.warnings[0].message


def test_slightly_over_the_recommendation_is_also_just_a_warning(
    validator: PlanValidator, builder: DownloadPlanBuilder, estimator: DownloadPlanEstimator
) -> None:
    """省级 Z14 在"推荐 Z13 ~ 警告 Z14"之间的过渡带上：超出但还没到警告阈值。

    仍要说一声（用户可能是故意的，那就更该看见），且用**另一种**文案
    （不是策略层的 warning_text —— 那条只在真警告时用）。
    """
    builder.add_region("northland", use_recommended_zoom=False, min_zoom=9, max_zoom=14)
    plan = builder.plan
    estimator.estimate(plan)

    result = validator.validate(plan)

    assert result.ok is True
    assert result.has(PlanIssueCode.ZOOM_OUT_OF_RECOMMENDATION)
    recommendation = validator.strategy.evaluate(RegionLevel.PROVINCE, 9, 14)
    assert recommendation.warning is False
    assert recommendation.exceeds_recommendation is True
    # 策略层只在真警告时给 warning_text；这次它该是空的（校验器当然也就不该用）
    assert recommendation.warning_text == ""
    assert recommendation.range_text in result.warnings[0].message
    assert "会按你设的范围下载" in result.warnings[0].message


def test_large_plan_is_only_a_warning(
    service: RegionService, builder: DownloadPlanBuilder, estimator: DownloadPlanEstimator
) -> None:
    """规模偏大只提醒（阈值可注入，不写死在用例里）。"""
    plan = _ready_plan(builder, estimator, "demoland")
    validator = PlanValidator(service, large_tiles=1, large_size_bytes=1)

    result = validator.validate(plan)

    assert result.has(PlanIssueCode.LARGE_TILE_COUNT)
    assert result.has(PlanIssueCode.LARGE_SIZE)
    assert result.ok is True
    assert all(row.severity is IssueSeverity.WARNING for row in result.warnings)


def test_existing_output_file_is_only_a_warning(
    validator: PlanValidator,
    builder: DownloadPlanBuilder,
    estimator: DownloadPlanEstimator,
    work_dir: Path,
) -> None:
    """Spec §15 的"不静默覆盖"：既然会撞上上次的成果，就让用户提前知道。"""
    plan = _ready_plan(builder, estimator, "demoland", output_dir=work_dir)
    paths = planned_output_paths(plan, output_dir=work_dir)
    paths["demoland"].parent.mkdir(parents=True, exist_ok=True)
    paths["demoland"].write_bytes("上一次的成果".encode())

    result = validator.validate(plan)

    assert result.has(PlanIssueCode.OUTPUT_FILE_EXISTS)
    assert result.ok is True
    assert str(paths["demoland"]) in result.warnings[-1].message


def test_check_output_false_skips_every_disk_probe(
    validator: PlanValidator, builder: DownloadPlanBuilder, estimator: DownloadPlanEstimator
) -> None:
    """只想知道"配置自不自洽"时可以完全不碰磁盘。"""
    plan = _ready_plan(builder, estimator, "demoland")

    result = validator.validate(plan, check_output=False)

    assert result.directory is None
    assert result.has(PlanIssueCode.OUTPUT_FILE_EXISTS) is False
    assert result.has(PlanIssueCode.OUTPUT_DIR_UNAVAILABLE) is False
    assert result.ok is True


# --------------------------------------------------------------------------- #
# 结果对象与文案（interface position 的守卫）
# --------------------------------------------------------------------------- #
def test_result_helpers_report_the_shape(
    validator: PlanValidator, builder: DownloadPlanBuilder
) -> None:
    builder.add_region("rivertown")
    plan = builder.plan
    plan.item("rivertown").max_zoom = 99

    result = validator.validate(plan, check_output=False)

    assert result.checked == 1
    assert result.plan_id == plan.id
    assert result.ok is False
    assert len(result.errors) >= 1
    assert result.message() == "\n".join(row.describe() for row in result.errors)
    lines = result.as_lines()
    assert lines[0].startswith("下载计划校验：不通过")
    assert lines[0].count("1 项") == 1


def test_interface_text_never_shows_a_machine_code(
    validator: PlanValidator, builder: DownloadPlanBuilder, region_resources: Path
) -> None:
    """给人看的那一段必须是纯人话：错误码只允许出现在 ``as_lines()`` 的统计行里。

    站在**界面**位置检查：把 ``PlanIssueCode`` 的每个 ``value`` 拿去扫用户可见的
    文本（``message()`` 与 ``describe()``）。模型层有机器码是合理的，
    只有"进了界面"才算违规（Slice 14 的教训）。
    """
    plan = DownloadPlan(name="什么都缺的计划")
    plan.add_item(DownloadPlanItem(region_id="nowhere", region_name="不存在的地区"))
    plan.add_item(DownloadPlanItem(id="x", region_id="rivertown", region_name="示例河市"))
    (region_resources / "city" / "rivertown.geojson").unlink()
    plan.add_item(DownloadPlanItem(id="x", region_id="demoland", region_name="示例国"))

    result = validator.validate(plan, check_output=False)

    assert len(result.errors) >= 3
    visible = result.message() + "\n".join(row.describe() for row in result.issues)
    for code in PlanIssueCode:
        assert code.value not in visible, f"界面文本里出现了机器码 {code.value}"

    # 而日志用的那一行**必须**带机器码（否则 grep 不动）
    assert any(row.code.value in row.as_line() for row in result.issues)


def test_issue_rejects_wrong_types() -> None:
    with pytest.raises(ValueError):
        PlanIssue(code="no_tiles", message="x")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PlanIssue(code=PlanIssueCode.NO_TILES, message="x", severity="error")  # type: ignore[arg-type]


def test_issue_strips_and_reports_its_subject() -> None:
    issue = PlanIssue(
        code=PlanIssueCode.NO_TILES,
        message="  有空格  ",
        region_id="  demoland  ",
        severity=IssueSeverity.WARNING,
    )
    assert issue.message == "有空格"
    assert issue.region_id == "demoland"
    assert issue.subject() == "demoland"
    assert PlanIssue(code=PlanIssueCode.EMPTY_PLAN, message="空").subject() == "计划"
    assert issue.is_error is False
    assert issue.as_line().startswith("warning no_tiles [demoland] ")


def test_validation_result_defaults_are_ok() -> None:
    result = PlanValidationResult()
    assert result.ok is True
    assert result.message() == ""
    assert result.count(PlanIssueCode.NO_TILES) == 0
    assert result.as_lines() == ["下载计划校验：通过（0 项，0 个错误 / 0 个提醒）"]
