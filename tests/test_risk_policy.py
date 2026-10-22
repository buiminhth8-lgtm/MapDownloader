"""风险等级与阈值策略（Slice 23，Spec §2 / §3 / §9 / §12）。

这一层是**纯数值 + 纯文案**，因此验收方式也是数值的：每一条阈值都测
"边界的两侧"，每一档确认规则都测"该弹与不该弹"。只测"危险会返回危险"
是不够的 —— 把 ``>=`` 写成 ``>`` 照样全绿。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.planning.region_level import RegionLevel
from app.planning.risk_level import RISK_LABEL, RISK_LEVEL_ORDER, RiskLevel
from app.planning.risk_policy import (
    DANGEROUS_SIZE_THRESHOLD,
    DANGEROUS_TILE_THRESHOLD,
    DEFAULT_RISK_POLICY,
    LARGE_SIZE_THRESHOLD,
    LARGE_TILE_THRESHOLD,
    LEVEL_ADVICE,
    VERY_LARGE_SIZE_THRESHOLD,
    VERY_LARGE_TILE_THRESHOLD,
    RiskPolicy,
)
from app.planning.zoom_strategy import ZoomStrategyService

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"

#: 定义阈值的那个文件（守卫要放行它）
_POLICY_FILE = APP_DIR / "planning" / "risk_policy.py"

#: ``app/geo/tile_math.py`` 里有一个同名的 100_000，但它是**展示**阈值
#: （"多少张以上改用 K/M/B 写法"），与风险无关 —— 守卫放行它并写明理由，
#: 免得下一个人看到"有例外"就以为守卫松了。
_DISPLAY_THRESHOLD_FILE = APP_DIR / "geo" / "tile_math.py"


# --------------------------------------------------------------------------- #
# RiskLevel
# --------------------------------------------------------------------------- #
def test_risk_level_values_are_the_persisted_contract() -> None:
    """``value`` 会进日志与执行记录，改名等于打断下游。"""
    assert {item.name: item.value for item in RiskLevel} == {
        "NORMAL": "normal",
        "LARGE": "large",
        "VERY_LARGE": "very_large",
        "DANGEROUS": "dangerous",
    }


def test_severity_is_explicit_and_ordered() -> None:
    """严重度按 :data:`RISK_LEVEL_ORDER` 递增，且**不依赖**枚举定义顺序。"""
    assert [item.severity for item in RISK_LEVEL_ORDER] == [0, 1, 2, 3]
    assert RiskLevel.NORMAL.severity < RiskLevel.LARGE.severity
    assert RiskLevel.LARGE.severity < RiskLevel.VERY_LARGE.severity
    assert RiskLevel.VERY_LARGE.severity < RiskLevel.DANGEROUS.severity


def test_worst_picks_the_more_severe_one() -> None:
    assert RiskLevel.NORMAL.worst(RiskLevel.LARGE) is RiskLevel.LARGE
    assert RiskLevel.DANGEROUS.worst(RiskLevel.NORMAL) is RiskLevel.DANGEROUS
    assert RiskLevel.worst_of() is RiskLevel.NORMAL
    assert (
        RiskLevel.worst_of(RiskLevel.LARGE, RiskLevel.VERY_LARGE, RiskLevel.NORMAL)
        is RiskLevel.VERY_LARGE
    )


def test_at_least_compares_severity() -> None:
    assert RiskLevel.DANGEROUS.at_least(RiskLevel.LARGE) is True
    assert RiskLevel.LARGE.at_least(RiskLevel.DANGEROUS) is False
    assert RiskLevel.LARGE.at_least(RiskLevel.LARGE) is True
    with pytest.raises(ValueError):
        RiskLevel.LARGE.at_least("large")  # type: ignore[arg-type]


def test_parse_is_tolerant_because_old_files_exist() -> None:
    """执行记录可能是**旧版本**写的：认不出来退回正常，而不是让记录作废。"""
    assert RiskLevel.parse("dangerous") is RiskLevel.DANGEROUS
    assert RiskLevel.parse("VERY_LARGE") is RiskLevel.VERY_LARGE
    assert RiskLevel.parse(RiskLevel.LARGE) is RiskLevel.LARGE
    assert RiskLevel.parse("不认识") is RiskLevel.NORMAL
    assert RiskLevel.parse(None) is RiskLevel.NORMAL


def test_label_carries_the_machine_name_only_when_asked() -> None:
    """界面上是中文，日志里带大写名（可以 grep）；两者都不许丢。"""
    assert RiskLevel.DANGEROUS.label() == "危险"
    assert RiskLevel.DANGEROUS.label(with_value=True) == "危险（DANGEROUS）"
    assert RISK_LABEL == "风险："
    assert RiskLevel.NORMAL.display_name == "正常"
    assert RiskLevel.VERY_LARGE.display_name == "很大"


def test_every_level_has_a_symbol() -> None:
    """Spec §11：不许只靠颜色表达风险 —— 文字之外还要有个记号。"""
    symbols = [item.symbol for item in RISK_LEVEL_ORDER]
    assert all(item.strip() for item in symbols)
    assert len(set(symbols)) == len(symbols)


# --------------------------------------------------------------------------- #
# 阈值：边界两侧都要测
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "tiles, expected",
    [
        (0, RiskLevel.NORMAL),
        (1, RiskLevel.NORMAL),
        (LARGE_TILE_THRESHOLD - 1, RiskLevel.NORMAL),
        (LARGE_TILE_THRESHOLD, RiskLevel.LARGE),          # 边界是 >=
        (VERY_LARGE_TILE_THRESHOLD - 1, RiskLevel.LARGE),
        (VERY_LARGE_TILE_THRESHOLD, RiskLevel.VERY_LARGE),
        (DANGEROUS_TILE_THRESHOLD - 1, RiskLevel.VERY_LARGE),
        (DANGEROUS_TILE_THRESHOLD, RiskLevel.DANGEROUS),
        (DANGEROUS_TILE_THRESHOLD * 10, RiskLevel.DANGEROUS),
    ],
)
def test_tile_thresholds(tiles: int, expected: RiskLevel) -> None:
    assert DEFAULT_RISK_POLICY.level_for_tiles(tiles) is expected


@pytest.mark.parametrize(
    "size, expected",
    [
        (0, RiskLevel.NORMAL),
        (LARGE_SIZE_THRESHOLD - 1, RiskLevel.NORMAL),
        (LARGE_SIZE_THRESHOLD, RiskLevel.LARGE),
        (VERY_LARGE_SIZE_THRESHOLD - 1, RiskLevel.LARGE),
        (VERY_LARGE_SIZE_THRESHOLD, RiskLevel.VERY_LARGE),
        (DANGEROUS_SIZE_THRESHOLD - 1, RiskLevel.VERY_LARGE),
        (DANGEROUS_SIZE_THRESHOLD, RiskLevel.DANGEROUS),
    ],
)
def test_size_thresholds(size: int, expected: RiskLevel) -> None:
    assert DEFAULT_RISK_POLICY.level_for_bytes(size) is expected


def test_negative_inputs_are_treated_as_zero() -> None:
    """调用方给错数不该让分析炸掉：负数就是"没有东西要下"。"""
    assert DEFAULT_RISK_POLICY.level_for_tiles(-1) is RiskLevel.NORMAL
    assert DEFAULT_RISK_POLICY.level_for_bytes(-1) is RiskLevel.NORMAL


def test_the_final_level_is_the_worst_of_both_dimensions() -> None:
    """Spec §3：最终风险取各维度最高等级（两个方向都要测）。"""
    policy = DEFAULT_RISK_POLICY
    # 张数正常、容量很大 → 取容量那一档
    assert policy.level_for_tiles(1_000) is RiskLevel.NORMAL
    assert policy.level_for_bytes(VERY_LARGE_SIZE_THRESHOLD) is RiskLevel.VERY_LARGE
    assert policy.level_for(1_000, VERY_LARGE_SIZE_THRESHOLD) is RiskLevel.VERY_LARGE
    # 张数危险、容量正常 → 取张数那一档
    assert policy.level_for(DANGEROUS_TILE_THRESHOLD, 1024) is RiskLevel.DANGEROUS
    # 两个都危险 → 还是危险
    assert policy.level_for(DANGEROUS_TILE_THRESHOLD, DANGEROUS_SIZE_THRESHOLD) is (
        RiskLevel.DANGEROUS
    )


def test_default_thresholds_are_the_spec_numbers() -> None:
    """Spec §3 的默认值本身就是契约（改了要有人知道）。"""
    assert LARGE_TILE_THRESHOLD == 100_000
    assert VERY_LARGE_TILE_THRESHOLD == 1_000_000
    assert DANGEROUS_TILE_THRESHOLD == 10_000_000
    assert LARGE_SIZE_THRESHOLD == 2 * 1024 ** 3
    assert VERY_LARGE_SIZE_THRESHOLD == 20 * 1024 ** 3
    assert DANGEROUS_SIZE_THRESHOLD == 100 * 1024 ** 3


# --------------------------------------------------------------------------- #
# 确认规则（Spec §12 / §13）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "level, confirm, strong",
    [
        (RiskLevel.NORMAL, False, False),
        (RiskLevel.LARGE, True, False),
        (RiskLevel.VERY_LARGE, True, False),
        (RiskLevel.DANGEROUS, True, True),
    ],
)
def test_confirmation_rules(level: RiskLevel, confirm: bool, strong: bool) -> None:
    assert DEFAULT_RISK_POLICY.requires_confirmation(level) is confirm
    assert DEFAULT_RISK_POLICY.requires_strong_confirmation(level) is strong


def test_a_custom_policy_can_be_stricter() -> None:
    """阈值可注入：将来"保守模式"不该改全局常量。"""
    strict = RiskPolicy(
        large_tiles=1_000,
        very_large_tiles=10_000,
        dangerous_tiles=100_000,
        large_bytes=1024 ** 2,
        very_large_bytes=10 * 1024 ** 2,
        dangerous_bytes=100 * 1024 ** 2,
    )
    assert strict.level_for_tiles(50_000) is RiskLevel.VERY_LARGE
    assert DEFAULT_RISK_POLICY.level_for_tiles(50_000) is RiskLevel.NORMAL


@pytest.mark.parametrize(
    "kwargs",
    [
        {"large_tiles": 0},
        {"large_tiles": -1},
        {"large_tiles": True},                     # bool 不是"一个阈值"
        {"large_tiles": 1_000_000, "very_large_tiles": 100_000},   # 顺序反了
        {"very_large_bytes": 100 * 1024 ** 3, "dangerous_bytes": 1024},
        {"confirmation_level": "large"},
        # 强确认不能低于普通确认：它是叠加，不是替换
        {
            "confirmation_level": RiskLevel.VERY_LARGE,
            "strong_confirmation_level": RiskLevel.LARGE,
        },
    ],
)
def test_policy_rejects_impossible_configurations(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        RiskPolicy(**kwargs)


# --------------------------------------------------------------------------- #
# 建议文案（Spec §9）
# --------------------------------------------------------------------------- #
def test_advice_uses_the_zoom_range_from_the_strategy_layer() -> None:
    """推荐区间必须来自策略层 —— 本模块一个字都不许写死。"""
    strategy = ZoomStrategyService()
    policy = DEFAULT_RISK_POLICY
    for level in (RegionLevel.COUNTRY, RegionLevel.PROVINCE, RegionLevel.CITY,
                  RegionLevel.MISSION):
        range_text = strategy.get_policy(level).recommended_range_text
        advice = policy.advice_for(level, range_text)
        assert range_text in advice, f"{level.value} 的建议里没有推荐区间"
    # 全国那条要顺手说清"高 Zoom 换别的等级下"，这是本 Slice 存在的理由
    assert "省" in policy.advice_for(RegionLevel.COUNTRY, "Z4~10")
    assert "任务区域" in policy.advice_for(RegionLevel.CITY, "Z12~16")


def test_custom_advice_has_no_range_placeholder() -> None:
    """自定义区域没有"推荐区间"可言，硬塞一个 Z1~18 是误导。"""
    advice = DEFAULT_RISK_POLICY.advice_for(RegionLevel.CUSTOM, "Z1~18")
    assert "Z1~18" not in advice
    assert "自定义" in advice


def test_every_region_level_has_advice() -> None:
    """新加一个等级却忘了写建议时，这条会红 —— 而不是界面上空一行。"""
    assert set(LEVEL_ADVICE) == set(RegionLevel)


def test_advice_rejects_a_non_level() -> None:
    with pytest.raises(ValueError):
        DEFAULT_RISK_POLICY.advice_for("country", "Z4~10")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 单一来源（Spec §3：阈值集中配置，禁止散落）
# --------------------------------------------------------------------------- #
def _integer_constants(path: Path) -> set[int]:
    """文件里**真的**写出来的整数常量（注释与文档字符串天然不参与）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, int)
            and not isinstance(node.value, bool)
        ):
            found.add(node.value)
    return found


def test_thresholds_live_only_in_the_risk_policy() -> None:
    """六个阈值只允许出现在 ``risk_policy.py``（+ 展示用的那个例外）。

    与 Slice 17 守 Zoom 数字同一条规矩：散落的第一形态就是"UI 里写
    ``if tiles > 10_000_000``"。守卫用 AST 而不是字符串匹配 ——
    文档里写一句"别在 UI 里写 10_000_000 张"就会被字符串匹配误判。
    """
    thresholds = {
        DEFAULT_RISK_POLICY.large_tiles,
        DEFAULT_RISK_POLICY.very_large_tiles,
        DEFAULT_RISK_POLICY.dangerous_tiles,
        DEFAULT_RISK_POLICY.large_bytes,
        DEFAULT_RISK_POLICY.very_large_bytes,
        DEFAULT_RISK_POLICY.dangerous_bytes,
    }
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if path in (_POLICY_FILE, _DISPLAY_THRESHOLD_FILE):
            continue
        hit = _integer_constants(path) & thresholds
        if hit:
            offenders.append(
                f"{path.relative_to(REPO_ROOT).as_posix()} :: {sorted(hit)}"
            )
    assert offenders == [], f"这些模块把风险阈值写死了：{offenders}"
