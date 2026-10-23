"""Zoom 策略表（Slice 17，Spec §3 / §12）。

这组用例守两件事：

1. **四个等级的数值就是 Spec 写的那四个区间**，改一个数字必须有人改测试；
2. **策略值与推荐文案只有一个来源**（``app/planning/``）。第 16 节那三条
   静态守卫不是洁癖：一旦有人在 DownloadPanel 里写一句
   ``if region_id == "china": return "Z4~10"``，策略就分家了 —— 而分家的代价
   要等到"新增一个省级预设"时才显形，那时往往已经有三四处抄本。
"""

from __future__ import annotations

import ast
import itertools
import subprocess
import sys
from pathlib import Path

import pytest

from app.config.settings import (
    MAX_SUPPORTED_ZOOM,
    MIN_SUPPORTED_ZOOM,
    TILE_MATH_MAX_ZOOM,
    TILE_MATH_MIN_ZOOM,
)
from app.planning import (
    DEFAULT_ZOOM_POLICIES,
    RegionLevel,
    ZoomPolicy,
    get_zoom_policy,
    list_zoom_policies,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"
PLANNING_DIR = APP_DIR / "planning"

#: 允许出现"等级 → Zoom 区间"耦合的地方。
#:
#: ``app/geo/region.py`` 是**数据**（中国预设有等级）而不是**规则**：
#: 它只说"中国是全国级"，从不说"全国该用到 Z10"。规则只在 app/planning 里。
#:
#: ``app/mission/models.py``（Slice 22）同属**定义**而不是规则：它只说
#: "任务区这一级叫 MISSION"，并把这件事收在 :data:`MISSION_REGION_LEVEL`
#: 一处 —— 任务区层里凡是要问"我是哪一级"的地方都引用它，而不是各写一遍
#: ``RegionLevel.MISSION``（那才会让"任务区属于哪一级"散落成多份）。
#:
#: ``app/regions/importer/models.py``（Slice 26）同属**定义**：导入管线的
#: ``DEFAULT_LEVEL_ALIASES`` 是"外部数据等级字面量 → RegionLevel"的唯一定义
#: 点（"country→COUNTRY、直辖市→PROVINCE"），Spec §19 的统计标签也绑定等级。
#: "谁挂谁"的嵌套规则仍只在 planning 层（hierarchy_builder 用谓词，不写成员名）。
_LEVEL_LITERAL_ALLOWED = {
    APP_DIR / "geo" / "region.py",
    APP_DIR / "mission" / "models.py",
    APP_DIR / "regions" / "importer" / "models.py",
}

#: 有"等级规则"的五个层级（顺序即文档顺序）。``CUSTOM`` 不算：它没有规则。
_HIERARCHY_LEVELS = (
    RegionLevel.COUNTRY,
    RegionLevel.PROVINCE,
    RegionLevel.CITY,
    RegionLevel.COUNTY,
    RegionLevel.MISSION,
)

#: 上面那四个的**成员名**（静态守卫按名字匹配 AST 属性节点）。
#: 从枚举本身推导，不手抄：抄一份的结果是加了等级却没人想起这里。
_LEVEL_NAMES = frozenset(item.name for item in _HIERARCHY_LEVELS)


def _python_sources(*, skip_planning: bool = False) -> list[Path]:
    """``app/`` 下全部 .py（可选跳过规划层自己）。"""
    files = sorted(APP_DIR.rglob("*.py"))
    if skip_planning:
        files = [item for item in files if PLANNING_DIR not in item.parents]
    return files


# --------------------------------------------------------------------------- #
# RegionLevel
# --------------------------------------------------------------------------- #
def test_region_level_values_are_the_persisted_contract() -> None:
    """``value`` 是契约（会出现在日志与将来的 Android 侧），改名等于打断下游。"""
    assert {item.name: item.value for item in RegionLevel} == {
        "COUNTRY": "country",
        "PROVINCE": "province",
        "CITY": "city",
        "COUNTY": "county",
        "MISSION": "mission",
        "CUSTOM": "custom",
    }


def test_region_level_display_names_are_present_and_unique() -> None:
    names = [item.display_name for item in RegionLevel]
    assert all(name.strip() for name in names)
    assert len(set(names)) == len(names)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, RegionLevel.CUSTOM),
        ("", RegionLevel.CUSTOM),
        ("country", RegionLevel.COUNTRY),
        ("COUNTRY", RegionLevel.COUNTRY),
        ("  Province  ", RegionLevel.PROVINCE),
        (RegionLevel.CITY, RegionLevel.CITY),
        # 认不出来 → CUSTOM（读历史数据的宽容路径）
        ("全国", RegionLevel.CUSTOM),
        ("county", RegionLevel.COUNTY),
        ("countyy", RegionLevel.CUSTOM),
        (7, RegionLevel.CUSTOM),
    ],
)
def test_parse_is_lenient_and_falls_back_to_custom(
    value: object, expected: RegionLevel
) -> None:
    assert RegionLevel.parse(value) is expected


def test_custom_is_the_only_level_without_hierarchy_rules() -> None:
    assert RegionLevel.CUSTOM.is_custom is True
    assert all(item.is_custom is False for item in _HIERARCHY_LEVELS)


# --------------------------------------------------------------------------- #
# 策略表本身
# --------------------------------------------------------------------------- #
def test_every_region_level_has_a_policy() -> None:
    assert set(DEFAULT_ZOOM_POLICIES) == set(RegionLevel)


@pytest.mark.parametrize(
    ("level", "low", "high", "warning"),
    [
        # Slice 29 按"数据饱和点 + 体积可接受点"重新标定（见 zoom_policy 文档）：
        # 全国下界 4→5（Z4 一屏 8000 公里，没有参考价值）、
        # 城市上界 16→15（市级全域 Z16 = 1.4 GB）、
        # 县级上界 18→17（Z18 多为 2 米影像的插值放大）。
        # 任务区保持 15~18 —— 它是无人机主力档，范围小、体积撑得住。
        (RegionLevel.COUNTRY, 5, 10, 10),
        (RegionLevel.PROVINCE, 9, 13, 14),
        (RegionLevel.CITY, 12, 15, 16),
        (RegionLevel.COUNTY, 14, 17, 18),
        (RegionLevel.MISSION, 15, 18, 18),
    ],
)
def test_default_policy_numbers_match_the_spec(
    level: RegionLevel, low: int, high: int, warning: int
) -> None:
    """Spec §3 的区间，逐个数钉死。"""
    policy = get_zoom_policy(level)
    assert policy.level is level
    assert policy.recommended_min_zoom == low
    assert policy.recommended_max_zoom == high
    assert policy.warning_max_zoom == warning
    assert policy.recommended_range_text == f"Z{low}~{high}"
    assert policy.recommended_text == f"推荐 Zoom：Z{low}~{high}"


def test_custom_policy_spans_the_supported_zoom_range() -> None:
    """自定义等级：建议区间 = 界面允许的全范围，且**不对合法 Zoom 报警**。"""
    policy = get_zoom_policy(RegionLevel.CUSTOM)
    assert policy.recommended_min_zoom == MIN_SUPPORTED_ZOOM
    assert policy.recommended_max_zoom == MAX_SUPPORTED_ZOOM
    assert policy.is_custom is True
    for zoom in range(TILE_MATH_MIN_ZOOM, TILE_MATH_MAX_ZOOM + 1):
        assert policy.should_warn(zoom) is False, f"自定义等级不该对 Z{zoom} 报警"


@pytest.mark.parametrize("policy", list_zoom_policies(), ids=lambda p: p.level.value)
def test_policy_invariants(policy: ZoomPolicy) -> None:
    """区间顺序自洽，且全部落在 Tile Math 支持范围内。"""
    assert policy.recommended_min_zoom >= TILE_MATH_MIN_ZOOM
    assert policy.recommended_min_zoom <= policy.recommended_max_zoom
    assert policy.recommended_max_zoom <= policy.warning_max_zoom
    assert policy.warning_max_zoom <= TILE_MATH_MAX_ZOOM
    assert policy.description.strip()


def test_recommended_ranges_overlap_between_neighbouring_levels() -> None:
    """相邻层级**刻意**留重叠带（Spec §15 要在文档里解释的那件事）。

    这条断言是给将来"顺手把区间改整齐"的人看的：全国 Z4~10 与省级 Z9~13
    重叠 Z9~10 是有意的，省级与城市级重叠 Z12~13、城市级与任务区重叠 Z15~16
    同理。把它们接成不重叠的阶梯会让"全国概览 + 重点省"无法用同一套级别拼。
    """
    ordered = [get_zoom_policy(item) for item in _HIERARCHY_LEVELS]
    for lower, upper in itertools.pairwise(ordered):
        assert lower.recommended_max_zoom >= upper.recommended_min_zoom, (
            f"{lower.level.value} 与 {upper.level.value} 之间没有重叠："
            f"{lower.recommended_range_text} / {upper.recommended_range_text}"
        )


def test_every_description_states_its_own_range() -> None:
    """每条推荐说明都必须写出**它自己那个区间**。

    原先这条是逐字钉死四句话。Slice 29 给说明补了"约多少米/像素、多大体积"
    之后，逐字比对变成了负担：改一个数字要同步改文案，而文案措辞本来就该
    随场景优化。这里改成检查**自洽性** —— 说明里必须出现
    ``Z5~10`` 这样的区间原文。

    它守的是真缺陷：区间从 Z4~10 改成 Z5~10 却忘了改说明时，用户在界面上
    看到的是"Z5~10"，而推荐说明还在讲 Z4 —— 两句话互相矛盾，比没有说明更糟。
    """
    for policy in list_zoom_policies():
        if policy.is_custom:
            continue
        assert policy.recommended_range_text in policy.description, (
            f"{policy.level.value} 的说明没写出自己的区间 "
            f"{policy.recommended_range_text}：{policy.description!r}"
        )


# --------------------------------------------------------------------------- #
# get_zoom_policy：严格
# --------------------------------------------------------------------------- #
def test_get_zoom_policy_returns_the_registry_object() -> None:
    """取到的是表里那**同一个**对象（不是副本）：策略是共享的不可变值。"""
    assert get_zoom_policy(RegionLevel.COUNTRY) is DEFAULT_ZOOM_POLICIES[
        RegionLevel.COUNTRY
    ]


@pytest.mark.parametrize("bad", ["country", "", None, 4, object()])
def test_get_zoom_policy_rejects_anything_but_region_level(bad: object) -> None:
    """策略服务这一侧**不宽容**：拼错的等级必须立刻炸，而不是悄悄变 CUSTOM。"""
    with pytest.raises(ValueError, match="RegionLevel"):
        get_zoom_policy(bad)


def test_list_zoom_policies_follows_the_enum_declaration_order() -> None:
    assert [item.level for item in list_zoom_policies()] == list(RegionLevel)


# --------------------------------------------------------------------------- #
# ZoomPolicy 自身的校验
# --------------------------------------------------------------------------- #
def _policy(**overrides: object) -> ZoomPolicy:
    base: dict[str, object] = {
        "level": RegionLevel.CITY,
        "recommended_min_zoom": 12,
        "recommended_max_zoom": 16,
        "warning_max_zoom": 16,
        "description": "城市级地图适合城市范围任务规划。",
    }
    base.update(overrides)
    return ZoomPolicy(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"recommended_min_zoom": 17, "warning_max_zoom": 18}, "recommended_min_zoom"),
        ({"warning_max_zoom": 15}, "warning_max_zoom"),
        ({"description": "   "}, "description"),
        ({"recommended_min_zoom": TILE_MATH_MAX_ZOOM + 1}, "zoom"),
        ({"warning_max_zoom": TILE_MATH_MAX_ZOOM + 1}, "zoom"),
        ({"level": "city"}, "RegionLevel"),
    ],
)
def test_invalid_policy_is_rejected(overrides: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _policy(**overrides)


def test_covers_only_accepts_ranges_inside_the_recommendation() -> None:
    policy = get_zoom_policy(RegionLevel.PROVINCE)  # Z9~13，警告阈值 14
    assert policy.covers(9, 13) is True
    assert policy.covers(10, 12) is True
    assert policy.covers(8, 13) is False  # 下界更低 → 超出
    assert policy.covers(9, 14) is False  # 上界更高 → 超出
    # Z14 是"超出建议但不警告"的过渡带：两件事必须分开
    assert policy.covers(9, 14) is False
    assert policy.should_warn(14) is False
    assert policy.should_warn(15) is True


def test_should_warn_ignores_the_low_end() -> None:
    """低 Zoom 只是少下一点，没有要提醒的代价。"""
    policy = get_zoom_policy(RegionLevel.CITY)  # Z12~16
    assert policy.should_warn(1) is False
    assert policy.should_warn(16) is False
    assert policy.should_warn(17) is True


# --------------------------------------------------------------------------- #
# 单一权威来源（Spec §12）
# --------------------------------------------------------------------------- #
def test_zoom_policy_is_constructed_only_in_the_planning_layer() -> None:
    """``ZoomPolicy(...)`` 只允许出现在规划层：策略表就是那一张。"""
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in _python_sources(skip_planning=True)
        if "ZoomPolicy(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"这些模块自己造了 ZoomPolicy：{offenders}"


def test_hierarchy_levels_are_not_hardcoded_outside_the_planning_layer() -> None:
    """``RegionLevel.COUNTRY`` 这类耦合只允许出现在规划层（+ 中国预设的数据定义）。

    只查 COUNTRY / PROVINCE / CITY / MISSION：它们是**等级规则**的锚点。
    ``RegionLevel.CUSTOM`` 是"没有规则"，界面拿它做分支是合理的。

    判据走 **AST** 而不是字符串匹配：注释或文档字符串里写一句"别在这里写
    ``RegionLevel.COUNTRY``"就会被字符串匹配误判成违规（Slice 14 在
    ``checksum`` 的守卫上踩过同一个坑）。AST 只看**真的访问了属性**的地方。
    """
    offenders = []
    for path in _python_sources(skip_planning=True):
        if path in _LEVEL_LITERAL_ALLOWED:
            continue
        found = _hardcoded_hierarchy_levels(path)
        if found:
            offenders.append(
                f"{path.relative_to(REPO_ROOT).as_posix()} :: {sorted(found)}"
            )
    assert offenders == [], f"这些模块把等级写死了：{offenders}"


def _hardcoded_hierarchy_levels(path: Path) -> set[str]:
    """文件里**真的**访问了哪些 ``RegionLevel.<等级>`` 属性。

    认两种写法：``RegionLevel.COUNTRY``、``planning.RegionLevel.COUNTRY``。
    字符串与注释不参与判断（这正是与字符串匹配的关键差别）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in _LEVEL_NAMES:
            continue
        base = node.value
        owner = ""
        if isinstance(base, ast.Name):
            owner = base.id
        elif isinstance(base, ast.Attribute):
            owner = base.attr
        if owner == "RegionLevel":
            found.add(node.attr)
    return found


def test_policy_descriptions_live_only_in_the_planning_layer() -> None:
    """推荐文案不许抄进 UI / Downloader（Spec §6：文案不许散落）。"""
    sentences = [item.description for item in list_zoom_policies()]
    offenders: list[str] = []
    for path in _python_sources(skip_planning=True):
        text = path.read_text(encoding="utf-8")
        for sentence in sentences:
            if sentence in text:
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()} :: {sentence}")
    assert offenders == [], f"这些模块抄了推荐文案：{offenders}"


# --------------------------------------------------------------------------- #
# 导入顺序（子进程：in-process 已经被别的 import 污染，测不出循环）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "statement",
    [
        "import app.geo",
        "import app.planning",
        # 最容易踩的那条：先 geo 后 planning（geo.region 要在导入期拿 RegionLevel）
        "import app.geo; import app.planning",
        "import app.services",
        "import app.package",
    ],
)
def test_import_order_never_hits_a_partial_module(statement: str) -> None:
    """``app.geo.region`` ↔ ``app.planning`` 之间只有一条依赖边，不能成环。

    真跑子进程而不是 ``importlib.reload``：只有全新解释器才能保证"第一次导入
    是从哪一侧发起的"这个前提，而循环导入恰恰只在特定顺序下才会暴露。
    """
    completed = subprocess.run(
        [sys.executable, "-c", statement],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, (
        f"`{statement}` 失败：\n{completed.stdout}\n{completed.stderr}"
    )
