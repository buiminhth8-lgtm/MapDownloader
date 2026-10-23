"""下载风险分析器（Slice 23，Spec §5 / §6 / §7 / §8 / §18 / §19 / §21 / §23）。

这一层的验收要点有三个，每个都配了断言方向相反的一对用例：

1. **用的是 ``selected`` 而不是外包矩形候选数** —— 造一份"候选远大于选中"的
   估算，断言结论跟着选中数走（用候选数会得到另一个等级）；
2. **Zoom 高不等于危险** —— 任务区 Z15~18 只覆盖一条走廊（两万张）必须是正常；
   而同一个等级把 Buffer 放大到一千万张时仍然要判危险（Spec §18）；
3. **不做任何枚举** —— 用 AST 守着分析器源码里不出现
   ``calculate_tile_range`` / ``iter_selected_tiles`` / ``RegionTileEstimator``
   （Spec §21 / §26），并且它压根不 import 那一层。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.geo.region import RegionType
from app.geo.region_estimator import RegionTileEstimate, ZoomRegionStats
from app.geo.tile_math import estimate_size_bytes, format_size, format_tile_count
from app.planning.download_plan import DownloadPlanItem
from app.planning.download_risk_analyzer import (
    RISK_ESTIMATE_ATTRS,
    DownloadRiskAnalyzer,
)
from app.planning.region_level import RegionLevel
from app.planning.risk_level import RiskLevel
from app.planning.risk_policy import (
    DANGEROUS_TILE_THRESHOLD,
    DEFAULT_RISK_POLICY,
    LARGE_TILE_THRESHOLD,
    VERY_LARGE_TILE_THRESHOLD,
    RiskPolicy,
)
from app.planning.zoom_policy import get_zoom_policy
from app.planning.zoom_strategy import ZoomStrategyService

REPO_ROOT = Path(__file__).resolve().parents[1]
ANALYZER_SOURCE = (
    REPO_ROOT / "app" / "planning" / "download_risk_analyzer.py"
)

#: 统计口径的夹具：每个 zoom 的选中张数
_COUNTRY_SMALL = {4: 50, 5: 200, 6: 800, 7: 3_200, 8: 12_800, 9: 51_200, 10: 204_800}


def _per_zoom(counts: dict[int, int]) -> tuple[ZoomRegionStats, ...]:
    """把 ``{zoom: 选中张数}`` 变成真实的 :class:`ZoomRegionStats` 明细。

    候选数与选中数**故意不同**（候选多 40%）：这正是"用候选还是用选中"的
    分水岭，用同样的数会让两类实现在测试里无法区分。
    """
    stats = []
    for zoom, selected in sorted(counts.items()):
        candidate = int(selected * 1.4) + 7
        stats.append(
            ZoomRegionStats(
                zoom=zoom,
                candidate_tiles=candidate,
                selected_tiles=selected,
                filtered_tiles=max(0, candidate - selected),
            )
        )
    return tuple(stats)


def _estimate(
    counts: dict[int, int],
    *,
    region_type: RegionType = RegionType.POLYGON,
    name: str = "示例区域",
    candidate_extra: int = 0,
) -> RegionTileEstimate:
    """一份真实的 :class:`RegionTileEstimate`（张数 / 容量都由生产同一套函数算）。

    ``candidate_extra`` 只加大**候选**数（把外包矩形做大），用来验证
    "风险不跟着候选走"。
    """
    per_zoom = _per_zoom(counts)
    selected = sum(item.selected_tiles for item in per_zoom)
    candidate = sum(item.candidate_tiles for item in per_zoom) + candidate_extra
    return RegionTileEstimate(
        region_name=name,
        region_type=region_type,
        candidate_tiles=candidate,
        selected_tiles=selected,
        filtered_tiles=max(0, candidate - selected),
        filtered_percent=(
            max(0, candidate - selected) * 100.0 / candidate if candidate else 0.0
        ),
        estimated_size_bytes=estimate_size_bytes(selected),
        per_zoom=per_zoom,
    )


@pytest.fixture
def analyzer() -> DownloadRiskAnalyzer:
    return DownloadRiskAnalyzer(strategy=ZoomStrategyService())


# --------------------------------------------------------------------------- #
# 只用 selected（Spec §5 / §26）
# --------------------------------------------------------------------------- #
def test_the_risk_follows_selected_tiles_not_the_bounding_box(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """候选数虚高一个数量级时，风险**不许**跟着涨（Spec §5 的硬要求）。

    夹具做成"选中 2 千张、候选 200 万张"：用候选数判定会得到 DANGEROUS，
    用选中数才是 NORMAL —— 两个方向都能区分。
    """
    estimate = _estimate({10: 500, 11: 1_500}, candidate_extra=20_000_000)
    assert estimate.candidate_tiles > DANGEROUS_TILE_THRESHOLD
    assert estimate.selected_tiles == 2_000

    report = analyzer.analyze(RegionLevel.CITY, 10, 11, estimate)
    assert report.total_tiles == estimate.selected_tiles == 2_000
    assert report.level is RiskLevel.NORMAL
    # 容量也跟着选中数走（同一份 selected 换算）
    assert report.estimated_bytes == estimate.estimated_size_bytes


def test_a_candidate_based_report_would_be_a_different_level(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """反向对照：如果实现"顺手"用了候选数，等级会**明显不同**。"""
    estimate = _estimate({10: 500, 11: 1_500}, candidate_extra=20_000_000)
    report = analyzer.analyze(RegionLevel.CITY, 10, 11, estimate)
    candidate_level = DEFAULT_RISK_POLICY.level_for_tiles(estimate.candidate_tiles)
    assert candidate_level is RiskLevel.DANGEROUS
    assert report.level is not candidate_level


# --------------------------------------------------------------------------- #
# 四个等级（张数维度）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "tiles, expected",
    [
        (1_000, RiskLevel.NORMAL),
        (LARGE_TILE_THRESHOLD, RiskLevel.LARGE),
        (VERY_LARGE_TILE_THRESHOLD, RiskLevel.VERY_LARGE),
        (DANGEROUS_TILE_THRESHOLD, RiskLevel.DANGEROUS),
    ],
)
def test_tile_dimension_levels(
    analyzer: DownloadRiskAnalyzer, tiles: int, expected: RiskLevel
) -> None:
    report = analyzer.analyze(RegionLevel.PROVINCE, 9, 13, _estimate({9: tiles}))
    assert report.level is expected


def test_the_size_dimension_can_raise_the_level(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """张数正常 + 容量很大 → 取容量那一档（Spec §3 / §22 的组合用例）。"""
    big = _estimate({9: 50_000})
    assert DEFAULT_RISK_POLICY.level_for_tiles(big.selected_tiles) is RiskLevel.NORMAL
    # 平均瓦片体积放大到 2MB/张 时容量进入"非常大"
    heavy = DownloadRiskAnalyzer(average_tile_size=2 * 1024 * 1024)
    report = heavy.analyze(
        RegionLevel.PROVINCE,
        9,
        9,
        _estimate({9: 50_000}),
    )
    assert report.level is RiskLevel.NORMAL  # 容量仍按传入的 estimate 走

    huge = RegionTileEstimate(
        region_name="容量怪物",
        region_type=RegionType.RECTANGLE,
        candidate_tiles=50_000,
        selected_tiles=50_000,
        filtered_tiles=0,
        filtered_percent=0.0,
        estimated_size_bytes=50 * 1024 ** 3,
        per_zoom=_per_zoom({9: 50_000}),
    )
    assert analyzer.analyze(RegionLevel.PROVINCE, 9, 9, huge).level is (
        RiskLevel.VERY_LARGE
    )


# --------------------------------------------------------------------------- #
# Zoom 超推荐（Spec §6）
# --------------------------------------------------------------------------- #
def test_over_recommended_tiles_and_percent(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """只统计**推荐上界以上**的那些 zoom（全国推荐到 Z10，所以 Z11 起才算）。"""
    counts = {8: 100, 9: 200, 10: 700, 11: 1_000, 12: 4_000}
    report = analyzer.analyze(RegionLevel.COUNTRY, 8, 12, _estimate(counts))

    # 数值问策略表，不在这里抄第二份（唯一的真值在 test_zoom_policy.py）
    policy = get_zoom_policy(RegionLevel.COUNTRY)
    assert report.recommended_min_zoom == policy.recommended_min_zoom
    assert report.recommended_max_zoom == policy.recommended_max_zoom
    assert policy.recommended_max_zoom == 10, "下面按 Z10 分界统计，策略改了要同步改这里"
    assert report.over_recommended_tiles == 1_000 + 4_000
    expected_percent = (5_000 / 6_000) * 100.0
    assert report.over_recommended_percent == pytest.approx(expected_percent)
    assert report.over_percent_text == f"{expected_percent:.1f}%"
    assert report.is_above_recommendation(11) is True
    assert report.is_above_recommendation(10) is False


def test_no_over_recommendation_when_inside_the_range(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """推荐区间内不许有"高于推荐"的数字（否则界面会莫名其妙多一行）。"""
    report = analyzer.analyze(RegionLevel.COUNTRY, 4, 10, _estimate(_COUNTRY_SMALL))
    assert report.over_recommended_tiles == 0
    assert report.over_recommended_percent == 0.0
    assert all("高于推荐" not in line for line in report.headline_lines())


# --------------------------------------------------------------------------- #
# 重点回归：全国 Z8~18（Spec §23）
# --------------------------------------------------------------------------- #
def _country_zoom_8_to_18() -> RegionTileEstimate:
    """一个模拟的"中国 Z8~18"：每升一级翻四倍，最终 179 亿张。

    刻意**不**跑真实几何：本用例要守的是"这个组合必须被判危险"，
    而不是"中国的边界文件在不在"。张数按 Web 墨卡托的真实增长规律构造，
    量级与真实全国 Z18 一致（百亿级）。
    """
    counts = {}
    value = 12_800
    for zoom in range(8, 19):
        counts[zoom] = value
        value *= 4
    return _estimate(counts, name="示例国")


def test_country_zoom_8_to_18_is_dangerous(analyzer: DownloadRiskAnalyzer) -> None:
    """Spec §23 的回归：**绝不允许"全国 Z18"无提示启动**。"""
    estimate = _country_zoom_8_to_18()
    assert estimate.selected_tiles > 10_000_000_000

    report = analyzer.analyze(RegionLevel.COUNTRY, 8, 18, estimate)

    assert report.level is RiskLevel.DANGEROUS
    assert report.requires_confirmation is True
    assert report.requires_strong_confirmation is True
    # 推荐区间必须说出来（否则用户只知道"危险"，不知道"那该怎么办"）
    assert report.recommended_range_text == get_zoom_policy(
        RegionLevel.COUNTRY
    ).recommended_range_text
    assert "Z10" in "".join(report.reasons)
    # 超大张数与容量都要有具体数字
    joined = "\n".join(report.reasons)
    assert f"{report.total_tiles:,}" in joined
    assert report.size_text in joined
    # 超推荐占比要有
    assert report.over_recommended_percent > 90.0
    assert report.over_percent_text in joined
    # 建议里要给出"改用省 / 市 / 任务区域"这条路
    assert any("省" in line for line in report.recommendations)


def test_the_dangerous_report_text_is_human_readable(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """Spec §8：不许只显示一个 ``DANGEROUS`` —— 必须有可以读的理由。"""
    report = analyzer.analyze(RegionLevel.COUNTRY, 8, 18, _country_zoom_8_to_18())
    lines = report.as_lines()
    assert lines[0] == "风险：危险（DANGEROUS）"
    assert f"预计瓦片：{format_tile_count(report.total_tiles)}" in lines
    assert f"预计容量：{format_size(report.estimated_bytes)}" in lines
    assert "实际 Zoom：Z8~18" in lines
    assert (
        f"推荐 Zoom：{get_zoom_policy(RegionLevel.COUNTRY).recommended_range_text}"
        in lines
    )
    assert "原因：" in lines and "建议：" in lines
    assert all(len(line.strip()) > 1 for line in lines)


# --------------------------------------------------------------------------- #
# Zoom 高 ≠ 危险（Spec §7 / §18）
# --------------------------------------------------------------------------- #
def test_a_small_mission_zone_is_not_dangerous_just_because_of_zoom(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """任务区 Z15~18 = 一条走廊：两万张就是正常，不许因为 Zoom 数字高就报警。"""
    report = analyzer.analyze(
        RegionLevel.MISSION,
        15,
        18,
        _estimate({15: 1_250, 16: 5_000, 17: 5_000, 18: 8_750}, name="巡检走廊"),
    )
    assert report.total_tiles == 20_000
    assert report.level is RiskLevel.NORMAL
    assert report.requires_confirmation is False
    # Z18 正好等于任务区的推荐上界 → 不算"超推荐"
    assert report.over_recommended_tiles == 0


def test_a_huge_mission_zone_is_still_dangerous(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """Spec §18：Buffer 放大到千万张时，等级再"高精度"也必须判危险。"""
    report = analyzer.analyze(
        RegionLevel.MISSION,
        15,
        18,
        _estimate({15: 10_000, 16: 40_000, 17: 160_000, 18: 10_000_000_000}),
    )
    assert report.level is RiskLevel.DANGEROUS
    assert report.requires_strong_confirmation is True


def test_custom_level_has_no_recommendation_but_still_gets_graded(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """自定义区域不套用等级建议，但规模照样要算（否则矩形模式就是保护盲区）。"""
    report = analyzer.analyze(
        RegionLevel.CUSTOM, 8, 18, _country_zoom_8_to_18()
    )
    assert report.recommended_min_zoom is None
    assert report.recommended_max_zoom is None
    assert report.recommended_range_text is None
    assert report.has_recommendation is False
    assert report.over_recommended_tiles == 0
    assert report.level is RiskLevel.DANGEROUS
    assert "不套用等级建议" in "\n".join(report.reasons)
    assert all("Z1~18" not in line for line in report.as_lines())


def test_the_analyzer_is_geometry_agnostic(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """矩形与多边形走同一条分析路径（Spec §19）。"""
    counts = {9: 400_000}
    rectangle = analyzer.analyze(
        RegionLevel.PROVINCE, 9, 9, _estimate(counts, region_type=RegionType.RECTANGLE)
    )
    polygon = analyzer.analyze(
        RegionLevel.PROVINCE, 9, 9, _estimate(counts, region_type=RegionType.POLYGON)
    )
    assert rectangle.level is polygon.level
    assert rectangle.total_tiles == polygon.total_tiles


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #
def test_analyzer_rejects_a_non_level(analyzer: DownloadRiskAnalyzer) -> None:
    with pytest.raises(ValueError):
        analyzer.analyze("country", 4, 10, _estimate(_COUNTRY_SMALL))


@pytest.mark.parametrize("low, high", [(18, 8), (23, 24), (0, 25)])
def test_analyzer_rejects_impossible_zoom_ranges(
    analyzer: DownloadRiskAnalyzer, low: int, high: int
) -> None:
    """判据与估算器**同一套**（``validate_zoom`` 的 Tile Math 范围）。

    刻意不在这里再收一次成 1~18：那样会出现"估算器接受了 19、风险分析拒绝"
    这种两个层各说各话。界面上的上界由输入控件负责（那才是它的职责）。
    """
    with pytest.raises(ValueError):
        analyzer.analyze(RegionLevel.COUNTRY, low, high, _estimate(_COUNTRY_SMALL))


def test_analyzer_rejects_an_object_without_estimate_fields(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """鸭子类型不是"什么都能塞"：缺字段要说清缺哪一个。"""

    class Half:
        selected_tiles = 10

    with pytest.raises(ValueError) as excinfo:
        analyzer.analyze(RegionLevel.COUNTRY, 4, 10, Half())
    message = str(excinfo.value)
    assert "estimated_size_bytes" in message
    assert "per_zoom" in message
    assert all(name in message for name in RISK_ESTIMATE_ATTRS)


def test_average_tile_size_must_be_positive() -> None:
    with pytest.raises(ValueError):
        DownloadRiskAnalyzer(average_tile_size=0)


# --------------------------------------------------------------------------- #
# 不重新枚举（Spec §21 / §26）
# --------------------------------------------------------------------------- #
_FORBIDDEN_NAMES = (
    "calculate_tile_range",
    "calculate_tile_ranges",
    "iter_selected_tiles",
    "build_region_filter",
    "RegionTileEstimator",
    "estimate_region_tiles",
)


def test_the_analyzer_source_contains_no_enumeration_calls() -> None:
    """风险分析只吃现成估算 —— 这条守卫盯着"别顺手重算一遍"。

    只用 AST 看**真的调用了什么 / import 了什么**：模块文档里合法地提到了
    ``iter_selected_tiles``（正是在说"这里没有它"），字符串匹配会误判。
    """
    tree = ast.parse(ANALYZER_SOURCE.read_text(encoding="utf-8"))
    called: set[str] = set()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert called & set(_FORBIDDEN_NAMES) == set()
    assert not any("region_estimator" in name for name in imported), imported


def test_the_analyzer_only_reports_on_what_it_was_given(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """它拿到的就是结论：不碰几何、不碰磁盘（这里是"只读输入"的行为版）。"""
    estimate = _estimate(_COUNTRY_SMALL)
    before = (estimate.selected_tiles, estimate.estimated_size_bytes)
    analyzer.analyze(RegionLevel.COUNTRY, 4, 10, estimate)
    assert (estimate.selected_tiles, estimate.estimated_size_bytes) == before


# --------------------------------------------------------------------------- #
# 计划条目：直接用它存下来的估算
# --------------------------------------------------------------------------- #
def test_analyze_item_uses_the_stored_estimate(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """条目已经算过张数 / 容量 / 逐级明细，风险分析读的就是那几个数。"""
    item = DownloadPlanItem(
        region_id="demoland",
        region_name="示例国",
        region_level=RegionLevel.COUNTRY,
        min_zoom=8,
        max_zoom=18,
        selected_tiles=20_000_000,
        estimated_bytes=estimate_size_bytes(20_000_000),
        candidate_tiles=25_000_000,
        filtered_tiles=5_000_000,
        per_zoom=_per_zoom({8: 1_000, 9: 4_000, 18: 19_995_000}),
    )
    report = analyzer.analyze_item(item)
    assert report.total_tiles == 20_000_000
    assert report.level is RiskLevel.DANGEROUS
    assert report.region_name == "示例国"
    assert report.over_recommended_tiles == 19_995_000


def test_analyze_item_rejects_a_non_item(analyzer: DownloadRiskAnalyzer) -> None:
    with pytest.raises(ValueError):
        analyzer.analyze_item({"region_id": "demoland"})  # type: ignore[arg-type]


def test_analyze_item_rejects_an_impossible_zoom_range(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    """计划文件里的 Zoom 是外部输入，不能假设它一定成立。"""
    item = DownloadPlanItem(region_id="demoland", min_zoom=18, max_zoom=8)
    with pytest.raises(ValueError):
        analyzer.analyze_item(item)


# --------------------------------------------------------------------------- #
# 格式化只有一份（Spec §20）
# --------------------------------------------------------------------------- #
def test_report_text_comes_from_the_shared_formatters(
    analyzer: DownloadRiskAnalyzer,
) -> None:
    report = analyzer.analyze(
        RegionLevel.COUNTRY, 8, 18, _country_zoom_8_to_18()
    )
    assert report.tiles_text == format_tile_count(report.total_tiles)
    assert report.size_text == format_size(report.estimated_bytes)


def test_a_stricter_policy_changes_the_verdict(analyzer: DownloadRiskAnalyzer) -> None:
    """阈值可注入：分析器把策略表当参数用，不是读全局常量。

    两个维度都要落在**同一档**（张数 LARGE、容量 LARGE），否则测出来的是
    "取最高"而不是"策略被用上了"。
    """
    tiny = RiskPolicy(
        large_tiles=100,
        very_large_tiles=10_000,
        dangerous_tiles=1_000_000,
        large_bytes=100 * 1024 ** 2,
        very_large_bytes=1024 ** 3,
        dangerous_bytes=10 * 1024 ** 3,
    )
    strict = DownloadRiskAnalyzer(policy=tiny)
    estimate = _estimate({10: 5_000})
    assert analyzer.analyze(RegionLevel.CITY, 10, 10, estimate).level is RiskLevel.NORMAL
    assert strict.analyze(RegionLevel.CITY, 10, 10, estimate).level is RiskLevel.LARGE
