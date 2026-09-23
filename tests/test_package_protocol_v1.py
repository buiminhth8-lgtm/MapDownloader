"""Package V1 协议固化测试（Slice 15 §1 / §2 / §4）。

这个文件回答一个**只能靠机械比对**才能诚实回答的问题：

    Windows 的 Builder 与 Android 的 Parser，对 ``manifest.json``
    的字段定义是不是**逐字一致**？

为什么不能靠"看代码"或"看注释"
--------------------------------

两端各有一份字段清单（Python 的 ``MANIFEST_FIELD_ORDER`` 与 Java 的
``requireXxx(json, "...")`` 调用）。注释里都写着"与另一端逐字对齐" ——
但注释不会在字段改名时变红，只有测试会。

所以这里的做法是：**把 Java 源码当文本读进来**，抽出里面所有
``requireXxx(json, "键名")`` 的键名，再与 Python 侧的字段清单做集合比对。
两端任何一边改了字段名、加了必填字段、漏了嵌套路径，这个文件立刻红。

真正的端到端（Android 侧真的解析这份 fixture）由
``OfflineMapManifestProtocolV1Test`` 承担 —— 那个用例直接读**同一个**
``tests/fixtures/map_package_v1/manifest.json``，所以"两端读的是同一份样例"
这件事有实证，而不是靠约定。

为什么键名比对要连**嵌套路径**一起比
-------------------------------------

``bounds`` 与 ``mbtiles`` 是嵌套对象。如果只比顶层键名，
"``mbtiles.sha256`` 被改成 ``mbtiles.hash``"这种改动是**看不见的** ——
而它恰好会让 Android 侧解析失败。所以 Java 侧的键名保留 ``点号路径`` 原样比对。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.package.models import (
    MANIFEST_BOUNDS_FIELDS,
    MANIFEST_FIELD_ORDER,
    MANIFEST_MBTILES_FIELDS,
    MANIFEST_REGION_FIELDS,
    MapPackageManifest,
    PackageRegion,
    UnsupportedFormatVersion,
    parse_manifest_dict,
)
# Slice 25：``_region_from_metadata`` 是 metadata → manifest 的**唯一**翻译点。
# 从私有位置取它是有意的 —— 这条翻译规则（哪几项透传、``geometryVersion``
# 归谁）就是本文件要钉的协议内容，绕到 ``build()`` 之外再断言一次只会更松。
from app.package.package_builder import _region_from_metadata
from app.planning.download_plan import PLAN_LEVEL_ORDER
from app.planning.region_level import RegionLevel

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
#: 本仓库根目录（tests/ 的上一级）。
BACKEND_ROOT = Path(__file__).resolve().parent.parent

#: **两端共用**的那一份 V1 协议样例。
#:
#: 这个路径是 Slice 15 §2 的硬性约定：Windows 与 Android 的协议测试读**同一份文件**。
#: 一旦谁把它复制一份到自己仓库里"方便测试"，两端就又开始各测各的了。
FIXTURE_PATH = BACKEND_ROOT / "tests" / "fixtures" / "map_package_v1" / "manifest.json"

#: Android 侧解析器的源码路径（**跨仓库**）。
#:
#: 这是本文件唯一一处跨仓库依赖，且只在**测试**里出现（生产代码不依赖它）。
#: 找不到时整个模块 skip，而不是 fail —— 单仓库检出（比如别人只拿了 Windows 侧）
#: 不应该因为缺兄弟仓库而红。但一旦在能读到它，比对就是强制的。
ANDROID_ROOT = BACKEND_ROOT.parent / "osmdroid"
ANDROID_MANIFEST_JAVA = (
    ANDROID_ROOT
    / "OpenStreetMapViewer"
    / "src"
    / "main"
    / "java"
    / "org"
    / "osmdroid"
    / "map"
    / "offline"
    / "model"
    / "OfflineMapManifest.java"
)

#: Java 里读取字段的辅助方法名 → 它们后面那个字符串是 manifest 的键（可带点号）。
_JAVA_REQUIRE_CALL = re.compile(r"require(?:String|Int|Long|Double|Number|Boolean)\s*\(\s*"
                                r"json\s*,\s*\"([^\"]+)\"")


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def v1_manifest_dict() -> dict:
    """读取那份共用的 V1 样例。"""
    assert FIXTURE_PATH.is_file(), f"协议样例不存在：{FIXTURE_PATH}"
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _java_required_keys() -> set[str]:
    """从 Android 解析器源码里抽出所有**必填**键名（保留点号路径）。"""
    source = ANDROID_MANIFEST_JAVA.read_text(encoding="utf-8")
    # 去掉行注释，免得把注释里举的例子也当成字段。
    # 块注释（/** ... */）里有很多形如 `mbtiles.file` 的说明，一定要删干净。
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"//[^\n]*", "", source)
    return set(_JAVA_REQUIRE_CALL.findall(source))


def _python_required_keys() -> set[str]:
    """Windows 侧的必填键名，嵌套字段展开成点号路径。"""
    keys: set[str] = set(MANIFEST_FIELD_ORDER)
    # description 是**可选**的（Slice 9 在 None 时压根不写这个键），
    # Android 也把它当可选处理 —— 所以两边都不算进"必填"。
    keys.discard("description")
    # region 同样是**可选**的（Slice 16）。它进了 MANIFEST_FIELD_ORDER 只是为了
    # 固定书写位置（在 bounds 之后），不代表必填：Slice ≤15 生成的包没有这个键，
    # 必须继续能读（Spec §21/§29）。Android 侧也不用读它 —— 运行时加载只依赖
    # bounds / minZoom / maxZoom / MBTiles（Spec §22）。
    keys.discard("region")
    keys.discard("bounds")
    keys.discard("mbtiles")
    keys.update(f"bounds.{field}" for field in MANIFEST_BOUNDS_FIELDS)
    keys.update(f"mbtiles.{field}" for field in MANIFEST_MBTILES_FIELDS)
    return keys


# --------------------------------------------------------------------------- #
# §1 两端字段一致性
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not ANDROID_MANIFEST_JAVA.is_file(),
    reason=f"读不到 Android 解析器（{ANDROID_MANIFEST_JAVA}），跳过跨仓库比对",
)
def test_windows_and_android_agree_on_every_required_field() -> None:
    """两端对"哪些字段是必填的"定义必须完全一致（Slice 15 §1）。

    这是整个 Slice 15 里最重要的一条断言：它守的是"一端改字段、
    另一端无法导入"这个**只能靠联调发现**的失败模式。
    """
    python_keys = _python_required_keys()
    java_keys = _java_required_keys()

    missing_in_java = sorted(python_keys - java_keys)
    missing_in_python = sorted(java_keys - python_keys)

    assert not missing_in_java, (
        f"Windows 写了这些字段，但 Android 解析器不读它们：{missing_in_java}\n"
        f"（Android 端要么补上，要么显式忽略；不能让它悄悄丢掉）"
    )
    assert not missing_in_python, (
        f"Android 要求这些字段，但 Windows 的字段清单里没有：{missing_in_python}\n"
        f"（这样产出的包 Android 一定导不进去）"
    )


@pytest.mark.skipif(
    not ANDROID_MANIFEST_JAVA.is_file(),
    reason="读不到 Android 解析器，跳过跨仓库比对",
)
def test_android_accepts_unknown_extra_fields() -> None:
    """未来加字段时，Android 必须**忽略**未知键而不是拒收（Slice 15 §1）。

    Android 侧靠 ``optXxx`` 取值、不遍历键，所以天然忽略未知字段。
    这里用源码做个廉价的守卫：解析器里不应出现"枚举所有 key"的调用
    （``keys()`` / ``names()``），那才是会因未知字段而失败的写法。
    """
    source = ANDROID_MANIFEST_JAVA.read_text(encoding="utf-8")
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"//[^\n]*", "", source)
    for risky in ("json.keys()", "json.names()", "keys().length"):
        assert risky not in source, (
            f"Android 解析器里出现了 {risky} —— 这会因为未知字段而拒收新版本的包"
        )


# --------------------------------------------------------------------------- #
# §2 协议样例本身
# --------------------------------------------------------------------------- #
def test_fixture_is_a_valid_v1_manifest(v1_manifest_dict: dict) -> None:
    """那份共用样例必须能被 Windows 侧的解析器读成合法 V1 manifest。"""
    manifest = parse_manifest_dict(v1_manifest_dict)
    assert isinstance(manifest, MapPackageManifest)
    assert manifest.format_version == 1


def test_fixture_carries_every_required_field(v1_manifest_dict: dict) -> None:
    """样例里的每个必填字段都得在，且嵌套路径要在正确的父对象下。"""
    missing: list[str] = []
    for key in sorted(_python_required_keys()):
        # 只有带点号的才往下钻一层；顶层字段必须直接落在根对象上。
        # （曾经的写法用 partition(".") + `if leaf:`，顶层字段的 leaf 是 ""，
        #   会被误判成"要到父对象里找"，于是 13 个顶层字段全报缺。）
        if "." in key:
            parent, _, leaf = key.partition(".")
            holder = v1_manifest_dict.get(parent)
            if not isinstance(holder, dict) or leaf not in holder:
                missing.append(key)
        elif key not in v1_manifest_dict:
            missing.append(key)
    assert not missing, f"协议样例缺少必填字段：{missing}"


# --------------------------------------------------------------------------- #
# §2b region 是可选字段（Slice 16）
# --------------------------------------------------------------------------- #
def test_region_is_optional_for_backward_compatibility(v1_manifest_dict: dict) -> None:
    """``region`` 是**可选**字段：旧包没有它必须照样读得出来（Spec §21/§29）。

    这条断言守的是"Slice 16 没把 Slice 15 固化的旧包兼容性破坏掉"。样例
    （代表 Slice 15 之前产出的包）不含 region，解析器必须不报错，且结果里
    ``region is None`` —— **不能**被哪一层默默填成 rectangle：在 Inspector 眼里
    "没有记录区域"（老包）与"记录了矩形"（新包）是两种含义，合并就没法区分了。
    """
    assert "region" not in v1_manifest_dict, "协议样例不该有 region（它代表旧包）"

    manifest = parse_manifest_dict(v1_manifest_dict)
    assert manifest.region is None


def test_region_round_trips_and_keeps_field_order(v1_manifest_dict: dict) -> None:
    """带上 region 后仍能往返，其余字段逐字节不变，且位置固定。"""
    with_region = dict(v1_manifest_dict)
    with_region["region"] = {
        "type": "polygon",
        "id": "china",
        "boundaryVersion": "2026-09",
    }

    manifest = parse_manifest_dict(with_region)
    assert manifest.region == PackageRegion(
        region_type="polygon", region_id="china", boundary_version="2026-09"
    )

    data = manifest.to_dict()
    keys = list(data)
    # 顺序由 MANIFEST_FIELD_ORDER 决定：region 紧跟在 bounds 之后
    assert keys.index("region") == keys.index("bounds") + 1
    assert list(data["region"]) == [
        key for key in MANIFEST_REGION_FIELDS if key in data["region"]
    ]

    # 去掉 region 应当**逐字节**还原成样例 —— 证明新增字段没有波及任何既有字段
    data.pop("region")
    assert data == v1_manifest_dict


def test_rectangle_region_omits_empty_subfields() -> None:
    """矩形区域只写 ``type``：空字段写成 ``null`` 会在 Android 侧变成字符串 "null"。"""
    assert PackageRegion.for_rectangle().to_dict() == {"type": "rectangle"}
    # 残缺写法（``{"region": {}}``）落回最保守解释
    assert PackageRegion.from_dict({}) == PackageRegion(region_type="rectangle")


def test_fixture_field_order_is_canonical(v1_manifest_dict: dict) -> None:
    """样例的字段顺序要与 ``MANIFEST_FIELD_ORDER`` 一致。

    顺序不是"审美问题"：Slice 9 刻意固定它，为的是同一份数据每次生成出来的
    字节序稳定（打包脚本做增量比对要靠它）。样例是文档也是测试输入，
    顺序变了说明 Builder 的字段清单被改过。
    """
    expected = [key for key in MANIFEST_FIELD_ORDER if key in v1_manifest_dict]
    assert list(v1_manifest_dict) == expected, (
        f"字段顺序与 MANIFEST_FIELD_ORDER 不一致\n"
        f"  期望：{expected}\n  实际：{list(v1_manifest_dict)}"
    )


def test_fixture_covers_the_variants_the_spec_names(v1_manifest_dict: dict) -> None:
    """§4 点名要覆盖的变体：vector / labels / merged / bounds / zoom / tileCount。

    样例只有一个，但必须把"有区分度"的那几个取值都取到，
    否则两端的解析差异测不出来：
    ``mapType=vector``（对比 image）、``labels=true``、``composition=merged``。
    """
    manifest = parse_manifest_dict(v1_manifest_dict)
    assert manifest.map_type == "vector"
    assert manifest.labels is True
    assert manifest.composition == "merged"
    assert manifest.is_merged is True
    assert manifest.min_zoom == 11 and manifest.max_zoom == 13
    assert manifest.tile_count == 12
    assert manifest.mbtiles_file == "map.mbtiles"


def test_fixture_sha256_field_has_the_right_shape(v1_manifest_dict: dict) -> None:
    """``mbtiles.sha256`` 必须是 64 位 hex —— 两端都按这个长度校验。"""
    sha = v1_manifest_dict["mbtiles"]["sha256"]
    assert isinstance(sha, str)
    assert len(sha) == 64
    assert all(ch in "0123456789abcdef" for ch in sha)


def test_fixture_created_at_carries_a_timezone(v1_manifest_dict: dict) -> None:
    """``createdAt`` 必须带时区偏移 —— Windows 侧强制，Android 侧原样保留。"""
    created = v1_manifest_dict["createdAt"]
    assert re.search(r"(Z|[+-]\d{2}:\d{2})$", created), (
        f"createdAt 必须带时区偏移，实际为 {created!r}"
    )


# --------------------------------------------------------------------------- #
# §1 版本兼容
# --------------------------------------------------------------------------- #
def test_windows_rejects_format_version_above_supported(v1_manifest_dict: dict) -> None:
    """``formatVersion > 1`` 时 Windows 侧明确拒绝（Slice 15 §1）。"""
    bumped = dict(v1_manifest_dict, formatVersion=2)
    with pytest.raises(UnsupportedFormatVersion):
        parse_manifest_dict(bumped)


def test_windows_rejects_format_version_below_one(v1_manifest_dict: dict) -> None:
    """``formatVersion < 1`` 同样拒绝（0 和负数都不是合法版本）。"""
    for bad in (0, -1):
        with pytest.raises(Exception):
            parse_manifest_dict(dict(v1_manifest_dict, formatVersion=bad))


def test_bumped_version_error_mentions_both_versions(v1_manifest_dict: dict) -> None:
    """拒绝时的报错要说清"包是几、我支持几" —— 否则用户不知道该怎么办。"""
    with pytest.raises(UnsupportedFormatVersion) as caught:
        parse_manifest_dict(dict(v1_manifest_dict, formatVersion=7))
    text = str(caught.value)
    assert "7" in text and "1" in text, f"报错没把两个版本都写清楚：{text!r}"


def test_fixture_round_trips_through_the_windows_model(v1_manifest_dict: dict) -> None:
    """样例 → 模型 → dict 必须回到同一份结构（字段不增不减、值不变）。

    这条守的是"模型与协议没有暗中的默认值/派生值"：如果 ``to_dict()`` 多写了
    一个字段，说明 Windows 会产出样例里没有的东西，Android 那边就没覆盖到。
    """
    manifest = parse_manifest_dict(v1_manifest_dict)
    assert manifest.to_dict() == v1_manifest_dict


# --------------------------------------------------------------------------- #
# Slice 25：区域等级（``region.level``）的跨端契约
# --------------------------------------------------------------------------- #
#: Android 侧等级枚举的源码（**跨仓库**，与 :data:`ANDROID_MANIFEST_JAVA` 同一条理由）。
#:
#: "哪些等级存在"必须两端一致 —— 不一致的后果不是报错，而是**静默降级**：
#: Android 的 ``RegionLevel.parse`` 认不出来就返回 ``UNKNOWN``，包照常能用，
#: 只是永远排在同级候选的最后。这类问题只能在真机上慢慢看出来。
ANDROID_REGION_LEVEL_JAVA = (
    ANDROID_ROOT
    / "OpenStreetMapViewer"
    / "src"
    / "main"
    / "java"
    / "org"
    / "osmdroid"
    / "map"
    / "offline"
    / "model"
    / "RegionLevel.java"
)

#: Java 枚举常量：``NAME（"展示名", 具体度）,`` —— 最后一项以 ``;`` 结尾而不是 ``,``，
#: 早先只认逗号的版本会静默漏掉 ``UNKNOWN``（那正是本用例要盯住的那一个）。
_JAVA_ENUM_CONSTANT = re.compile(
    r"^\s*([A-Z][A-Z0-9_]*)\s*\(\s*\"([^\"]*)\"\s*,\s*(-?\d+)\s*\)\s*[,;]\s*$",
    re.MULTILINE,
)


def _java_region_levels() -> dict[str, int]:
    """``{等级名（小写）: 具体度}`` —— 从 Android 源码里**抽**出来，不是抄一份。"""
    source = ANDROID_REGION_LEVEL_JAVA.read_text(encoding="utf-8")
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"//[^\n]*", "", source)
    found = _JAVA_ENUM_CONSTANT.findall(source)
    assert found, f"没能从 {ANDROID_REGION_LEVEL_JAVA.name} 里解析出任何枚举常量"
    return {name.lower(): int(specificity) for name, _display, specificity in found}


@pytest.mark.skipif(
    not ANDROID_REGION_LEVEL_JAVA.is_file(),
    reason=f"读不到 Android 等级枚举（{ANDROID_REGION_LEVEL_JAVA}），跳过跨仓库比对",
)
def test_both_ends_share_one_region_level_vocabulary() -> None:
    """``region.level`` 的取值集合两端一致（Slice 25 §4）。

    ``app/planning/region_level.py`` 的 docstring 写着等级值"便于将来 Android 侧
    用同一个字符串沟通"，Android 的 ``RegionLevel.parse`` 也确实在认同一套字符串。
    两边清单一旦分叉，行政区包就会静默降级成"未知等级" —— 这正是 Slice 25
    修掉的那个 Bug（Windows 侧压根没给行政区写这个键）。
    """
    java = _java_region_levels()
    python_levels = {level.value for level in RegionLevel}

    unknown_on_python_side = sorted(python_levels - set(java))
    assert not unknown_on_python_side, (
        f"Windows 会写出这些等级 {unknown_on_python_side}，"
        f"但 Android 的 RegionLevel 认不出来（会静默降级成 UNKNOWN）"
    )
    assert set(java) - python_levels == {"unknown"}, (
        "Android 只应比 Windows 多一个 unknown（老包 / 认不出的等级），"
        f"实际多出：{sorted(set(java) - python_levels - {'unknown'})}"
    )
    assert not hasattr(RegionLevel, "UNKNOWN"), (
        "Windows 侧不该有 UNKNOWN 这个成员：写不出具体等级时写 custom"
        "（明确的自定义），把'不知道'写进产物会让两端对同一个包的理解产生分歧"
    )


@pytest.mark.skipif(
    not ANDROID_REGION_LEVEL_JAVA.is_file(),
    reason="读不到 Android 等级枚举，跳过跨仓库比对",
)
def test_both_ends_agree_on_which_level_is_more_specific() -> None:
    """层次越细越"具体"这条排序两端一致（Slice 24 §5-3）。

    Windows 的 ``PLAN_LEVEL_ORDER`` 是**下载顺序**（由粗到细）；
    Android 的 ``RegionLevel.getSpecificity()`` 是**选包优先级**（越大越具体）。
    两者在"全国 < 省 < 市 < 任务区"这一段必须互为逆序 —— 否则 AUTO 选包在
    Zoom 区间**重叠**的那几档（9~10 / 12~13 / 15~16）会优先留下更粗的那张图。
    """
    java = _java_region_levels()
    ladder = [
        RegionLevel.COUNTRY,
        RegionLevel.PROVINCE,
        RegionLevel.CITY,
        RegionLevel.MISSION,
    ]
    assert tuple(PLAN_LEVEL_ORDER[: len(ladder)]) == tuple(ladder), (
        "下载顺序不再是「全国→省→市→任务区」，这条断言的前提变了，"
        "要么改这里要么改 PLAN_LEVEL_ORDER"
    )

    specificities = [java[level.value] for level in ladder]
    assert specificities == sorted(specificities), (
        f"Android 的具体度应随层级递增，实际为 {dict(zip([l.value for l in ladder], specificities))}"
    )
    assert len(set(specificities)) == len(specificities), (
        "具体度必须两两不同：一旦相等，§5-3 在这一对等级之间就退化成"
        f"「谁也不比谁优先」，选包会依赖输入顺序。实际为 {specificities}"
    )


def test_an_admin_region_keeps_its_level_and_gains_no_mission_keys() -> None:
    """MBTiles metadata → manifest 这一步不许把等级弄丢（Slice 25 §4）。

    这个用例直接打 ``_region_from_metadata``（metadata → manifest 的**唯一**
    翻译点）：它是本次 Bug 的现场 —— 原来 ``geometryVersion`` 跟着
    "有没有 level"走，而 level 只给任务区写；现在 level 人人都写，
    那条推导必须改成认等级，否则行政区包会凭空多出一个 ``geometryVersion``，
    与 ``boundaryVersion`` 说同一件事（同一个事实存两处的经典起点）。
    """
    admin = _region_from_metadata(
        {
            "region_type": "polygon",
            "region_id": "hangzhou",
            "boundary_version": "2026-09",
            "region_level": "city",
        }
    )
    assert admin is not None
    assert admin.to_dict() == {
        "type": "polygon",
        "id": "hangzhou",
        "boundaryVersion": "2026-09",
        "level": "city",
    }, "行政区包只该多一个 level；geometryVersion 是任务区专属的"

    mission = _region_from_metadata(
        {
            "region_type": "polygon",
            "region_id": "mission-001",
            "boundary_version": "1:3000.0:abcdef",
            "region_level": "mission",
            "buffer_meters": "3000",
        }
    )
    assert mission is not None
    assert mission.to_dict() == {
        "type": "polygon",
        "id": "mission-001",
        "boundaryVersion": "1:3000.0:abcdef",
        "level": "mission",
        "bufferMeters": 3000.0,
        "geometryVersion": "1:3000.0:abcdef",
    }, "任务区那一份与 Slice 22 逐字段一致，一个都不许动"

    # 老包（Slice ≤21 的 metadata 没有 region_level）依旧读得出来，
    # 只是没有 level —— "老包仍可导入"这条不能因为加了等级而破。
    legacy = _region_from_metadata(
        {"region_type": "polygon", "region_id": "china", "boundary_version": "2019"}
    )
    assert legacy is not None
    assert legacy.level is None
    assert "level" not in legacy.to_dict()
