"""下载计划模型与持久化（Slice 20，Spec §2 / §8 / §13 / §15 / §16 / §17 / §18）。

这里守三件事：

1. **模型不藏几何**：条目里只有 ``region_id`` 与计数，没有边界、没有瓦片坐标
   （Spec §2 / §8）。
2. **汇总口径**：只有"启用 **且** 可用"的条目进总量（Spec §12）。
3. **写盘的白名单**：计划文件里既不能出现密钥字段，也不能出现估算结果
   （那是算出来的，重算即可；写进磁盘等于把"当时对"当成"永远对"）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.config.settings import DEFAULT_PLAN_NAME
from app.planning.download_plan import (
    PLAN_LEVEL_ORDER,
    DownloadPlan,
    DownloadPlanItem,
    PlanItemStatus,
    item_file_stem,
    level_sort_key,
    new_item_id,
    new_plan_id,
    plan_directory,
    plan_folder_name,
    planned_output_paths,
)
from app.planning.plan_store import (
    PlanStore,
    PlanStoreError,
    item_from_payload,
    item_to_payload,
    plan_from_payload,
    plan_to_payload,
)
from app.planning.region_level import RegionLevel

REPO_ROOT = Path(__file__).resolve().parent.parent


def make_item(
    region_id: str = "china",
    *,
    name: str = "中国",
    level: RegionLevel = RegionLevel.COUNTRY,
    min_zoom: int = 4,
    max_zoom: int = 10,
    **kwargs,
) -> DownloadPlanItem:
    """造一个条目（默认取全国级的推荐区间，不写死任何策略数字）。"""
    return DownloadPlanItem(
        region_id=region_id,
        region_name=name,
        region_level=level,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 等级顺序（Spec §13）
# --------------------------------------------------------------------------- #
def test_level_order_covers_every_region_level() -> None:
    """``PLAN_LEVEL_ORDER`` 必须与枚举一一对应。

    漏一个等级的后果不是"排错位置"，而是它在 :func:`level_sort_key` 里拿到
    兜底名次、永远排在最后 —— 而 Slice 21 会照这个顺序串行执行。
    """
    assert set(PLAN_LEVEL_ORDER) == set(RegionLevel)
    assert len(PLAN_LEVEL_ORDER) == len(RegionLevel)


def test_level_sort_key_follows_the_declared_order() -> None:
    ranks = [level_sort_key(level) for level in PLAN_LEVEL_ORDER]
    assert ranks == sorted(ranks)
    # 认不出来的等级收敛到 CUSTOM（它本来就排在最后），而不是抛异常
    assert level_sort_key("没有这个等级") == level_sort_key(RegionLevel.CUSTOM)
    assert level_sort_key(None) == level_sort_key(RegionLevel.CUSTOM)
    assert level_sort_key(RegionLevel.COUNTRY) == 0
    assert level_sort_key(RegionLevel.COUNTRY) < level_sort_key(RegionLevel.PROVINCE)


# --------------------------------------------------------------------------- #
# 条目
# --------------------------------------------------------------------------- #
def test_item_defaults_are_empty_and_complete() -> None:
    item = DownloadPlanItem()
    assert item.id and item.id != new_item_id()  # 会自动生成一个 id
    assert item.region_id == ""
    assert item.status is PlanItemStatus.VALID
    assert item.enabled is True
    assert item.selected_tiles == 0 and item.estimated_bytes == 0
    assert item.per_zoom == ()


def test_item_id_changes_per_instance() -> None:
    ids = {DownloadPlanItem().id for _ in range(20)}
    assert len(ids) == 20


def test_new_ids_are_short_and_unique() -> None:
    assert len(new_plan_id()) == 12
    assert len({new_plan_id() for _ in range(50)}) == 50


def test_item_texts_before_estimate() -> None:
    """还没估算时显示 ``—``，而不是 ``0 张 / 0.00 B``。

    "0 张"会被读成"这块地没有瓦片"（一个结论），而事实是"还没算"。
    """
    item = make_item()
    assert item.zoom_text == "Z4~10"
    assert item.tiles_text == "—"
    assert item.size_text == "—"
    assert item.is_estimated is False


def test_item_texts_after_estimate() -> None:
    item = make_item(candidate_tiles=28_341, selected_tiles=28_341, estimated_bytes=820 * 1024**2)
    assert item.tiles_text == "28,341"
    assert item.size_text == "820.00 MB"
    assert item.is_estimated is True


def test_item_rejects_no_zoom_values() -> None:
    """模型**不校验** Zoom：非法值要被校验器报出来，而不是炸在构造里。"""
    item = make_item(min_zoom=99, max_zoom=3)
    assert item.min_zoom == 99 and item.max_zoom == 3
    assert item.zoom_text == "Z99~3"


def test_item_level_is_parsed_leniently() -> None:
    assert make_item(level="city").region_level is RegionLevel.CITY
    assert make_item(level="不认识").region_level is RegionLevel.CUSTOM


def test_status_parse_and_texts() -> None:
    assert PlanItemStatus.parse("invalid") is PlanItemStatus.INVALID
    assert PlanItemStatus.parse("没有这个") is PlanItemStatus.VALID
    assert PlanItemStatus.INVALID.display_name == "不可用"
    assert PlanItemStatus.INVALID.is_valid is False


def test_counts_toward_total_needs_both_enabled_and_valid() -> None:
    item = make_item()
    assert item.counts_toward_total is True
    item.enabled = False
    assert item.counts_toward_total is False
    item.enabled = True
    item.mark_invalid("区域没了")
    assert item.counts_toward_total is False
    assert item.status is PlanItemStatus.INVALID
    assert item.invalid_reason == "区域没了"
    item.mark_valid()
    assert item.counts_toward_total is True and item.invalid_reason == ""


def test_mark_invalid_clears_a_stale_estimate() -> None:
    """不可用时必须把上一次的数字清掉。

    留着它，界面上就会出现"不可用 / 28,341 张"这种自相矛盾的一行，
    而总量又没算它 —— 用户没法判断到底哪个数是真的。
    """
    item = make_item(candidate_tiles=10, selected_tiles=8, estimated_bytes=1024, per_zoom=(1, 2))
    item.mark_invalid("边界文件读不出")
    assert (item.candidate_tiles, item.selected_tiles, item.estimated_bytes) == (0, 0, 0)
    assert item.per_zoom == ()
    assert item.tiles_text == "—"


def test_item_describe_mentions_state() -> None:
    item = make_item(candidate_tiles=5, selected_tiles=5, estimated_bytes=100, enabled=False)
    text = item.describe()
    assert "中国" in text
    assert "全国级" in text and "Z4~10" in text
    assert "5 张" in text
    assert "未启用" in text
    item.mark_invalid("区域没了")
    assert "不可用" in item.describe()
    assert "5 张" not in item.describe()


# --------------------------------------------------------------------------- #
# 计划
# --------------------------------------------------------------------------- #
def test_plan_item_lookup_and_removal() -> None:
    plan = DownloadPlan(name="p")
    item = plan.add_item(make_item("china"))
    assert plan.item("china") is item
    assert plan.item("  china  ") is item  # 两侧空白不影响
    assert plan.has_region("china") is True
    assert plan.index_of("china") == 0
    assert plan.remove_region("china") is True
    assert plan.remove_region("china") is False  # 连点两次不该炸
    assert plan.item("china") is None
    assert plan.index_of("china") == -1


def test_plan_add_item_replaces_the_same_region() -> None:
    """同一个区域只允许一项（Spec §5）。"""
    plan = DownloadPlan(name="p")
    plan.add_item(make_item("china", max_zoom=10))
    plan.add_item(make_item("china", max_zoom=12))
    assert plan.count == 1
    assert plan.item("china").max_zoom == 12


def test_plan_is_empty_and_count() -> None:
    plan = DownloadPlan(name="p")
    assert plan.is_empty and plan.count == 0
    plan.add_item(make_item())
    assert not plan.is_empty and plan.count == 1


def test_plan_clear_items_keeps_identity_and_settings() -> None:
    plan = DownloadPlan(name="我的计划", output_dir="D:/out", map_layer="vec_w", with_labels=True)
    plan.add_item(make_item())
    plan.recalculate_totals()
    plan.clear_items()
    assert plan.items == []
    assert plan.total_tiles == 0 and plan.estimated_bytes == 0
    assert (plan.name, plan.output_dir, plan.map_layer, plan.with_labels) == (
        "我的计划",
        "D:/out",
        "vec_w",
        True,
    )


def test_recalculate_totals_skips_disabled_and_invalid() -> None:
    """Spec §12：禁用项不参与总量；不可用项同样不参与。"""
    plan = DownloadPlan(name="p")
    plan.add_item(make_item("a", selected_tiles=100, estimated_bytes=1000, candidate_tiles=100))
    plan.add_item(make_item("b", selected_tiles=200, estimated_bytes=2000, candidate_tiles=200))
    plan.add_item(make_item("c", selected_tiles=400, estimated_bytes=4000, candidate_tiles=400))
    plan.recalculate_totals()
    assert (plan.total_tiles, plan.estimated_bytes) == (700, 7000)

    plan.item("b").enabled = False
    plan.recalculate_totals()
    assert (plan.total_tiles, plan.estimated_bytes) == (500, 5000)

    plan.item("c").mark_invalid("区域没了")
    plan.recalculate_totals()
    assert (plan.total_tiles, plan.estimated_bytes) == (100, 1000)

    plan.item("b").enabled = True
    plan.recalculate_totals()
    assert (plan.total_tiles, plan.estimated_bytes) == (300, 3000)


def test_ordered_items_follows_level_then_insertion_order() -> None:
    """Spec §13：全国 → 省 → 市，同级别保持用户添加顺序。"""
    plan = DownloadPlan(name="p")
    plan.add_item(make_item("city-1", name="市甲", level=RegionLevel.CITY))
    plan.add_item(make_item("prov-2", name="省乙", level=RegionLevel.PROVINCE))
    plan.add_item(make_item("country-3", name="国丙", level=RegionLevel.COUNTRY))
    plan.add_item(make_item("city-4", name="市丁", level=RegionLevel.CITY))
    plan.add_item(make_item("prov-5", name="省戊", level=RegionLevel.PROVINCE))

    assert [item.region_id for item in plan.ordered_items()] == [
        "country-3",
        "prov-2",
        "prov-5",
        "city-1",
        "city-4",
    ]
    # 插入顺序本身没被改动（界面上的行号不该因为排序而跳）
    assert [item.region_id for item in plan.items] == [
        "city-1",
        "prov-2",
        "country-3",
        "city-4",
        "prov-5",
    ]


def test_ordered_enabled_items_skips_disabled_and_invalid() -> None:
    plan = DownloadPlan(name="p")
    plan.add_item(make_item("a", level=RegionLevel.CITY, selected_tiles=1, candidate_tiles=1))
    plan.add_item(make_item("b", level=RegionLevel.COUNTRY, selected_tiles=1, candidate_tiles=1))
    plan.add_item(make_item("c", level=RegionLevel.PROVINCE, selected_tiles=1, candidate_tiles=1))
    plan.item("c").enabled = False
    plan.item("a").mark_invalid("区域没了")
    assert [item.region_id for item in plan.ordered_enabled_items()] == ["b"]


def test_enabled_items_property_and_count() -> None:
    plan = DownloadPlan(name="p")
    plan.add_item(make_item("a", selected_tiles=1, candidate_tiles=1))
    plan.add_item(make_item("b", selected_tiles=1, candidate_tiles=1))
    plan.item("a").enabled = False
    assert [item.region_id for item in plan.enabled_items] == ["b"]
    assert plan.enabled_count == 1
    assert plan.invalid_items == ()


def test_needs_estimate_covers_invalid_items_too() -> None:
    """不可用的条目也算"没算过"：重试估算正是让它们恢复的唯一路径。"""
    plan = DownloadPlan(name="p")
    item = plan.add_item(make_item("ghost", candidate_tiles=10, selected_tiles=10))
    assert plan.needs_estimate() is False
    item.mark_invalid("区域没了")
    assert plan.needs_estimate() is True


def test_coverage_levels_lists_only_administrative_levels() -> None:
    """Spec §19 的三级覆盖提示。"""
    plan = DownloadPlan(name="p")
    assert plan.coverage_levels() == ()
    plan.add_item(make_item("a", level=RegionLevel.CITY, candidate_tiles=1, selected_tiles=1))
    assert plan.coverage_levels() == (RegionLevel.CITY,)
    plan.add_item(make_item("b", level=RegionLevel.COUNTRY, candidate_tiles=1, selected_tiles=1))
    plan.add_item(make_item("c", level=RegionLevel.PROVINCE, candidate_tiles=1, selected_tiles=1))
    assert plan.coverage_levels() == (
        RegionLevel.COUNTRY,
        RegionLevel.PROVINCE,
        RegionLevel.CITY,
    )
    # 作业区 / 自定义不算"行政层级覆盖"
    plan.add_item(make_item("d", level=RegionLevel.MISSION, candidate_tiles=1, selected_tiles=1))
    plan.add_item(make_item("e", level=RegionLevel.CUSTOM, candidate_tiles=1, selected_tiles=1))
    assert plan.coverage_levels() == (
        RegionLevel.COUNTRY,
        RegionLevel.PROVINCE,
        RegionLevel.CITY,
    )
    # 禁用后不再算覆盖
    plan.item("b").enabled = False
    assert plan.coverage_levels() == (RegionLevel.PROVINCE, RegionLevel.CITY)


def test_plan_describe_mentions_totals() -> None:
    plan = DownloadPlan(name="我的计划")
    plan.add_item(make_item(candidate_tiles=10, selected_tiles=10, estimated_bytes=1024))
    plan.recalculate_totals()
    text = plan.describe()
    assert "我的计划" in text and "1 项" in text and "10 张" in text


# --------------------------------------------------------------------------- #
# 输出命名（Spec §15 / §16）
# --------------------------------------------------------------------------- #
def test_folder_name_falls_back_to_the_default() -> None:
    assert plan_folder_name(DownloadPlan(name="china_plan")) == "china_plan"
    assert plan_folder_name(DownloadPlan(name="")) == DEFAULT_PLAN_NAME
    # 非法字符交给 sanitize_map_name（Slice 7 起唯一的文件名清理实现）
    assert plan_folder_name(DownloadPlan(name="a/b:c")) == "a_b_c"


def test_plan_directory_prefers_the_explicit_output_dir() -> None:
    plan = DownloadPlan(name="p", output_dir="D:/from_plan")
    assert plan_directory(plan) == Path("D:/from_plan") / "p"
    assert plan_directory(plan, output_dir="D:/from_ui") == Path("D:/from_ui") / "p"


def test_file_stem_matches_the_spec_examples() -> None:
    """Spec §15 给的例子：``china_z4_10.mbtiles``。"""
    assert item_file_stem(make_item("china", min_zoom=4, max_zoom=10)) == "china_z4_10"
    assert item_file_stem(make_item("zhejiang", min_zoom=9, max_zoom=13)) == "zhejiang_z9_13"
    assert item_file_stem(make_item("hangzhou", min_zoom=12, max_zoom=16)) == "hangzhou_z12_16"


def test_file_stem_is_sanitized_for_windows() -> None:
    stem = item_file_stem(make_item("a/b:c"))
    assert stem == "a_b_c_z4_10"


def test_file_stem_dedupes_against_taken_names() -> None:
    """同名时安全处理（Spec §15）：**不覆盖**前一个。"""
    item = make_item("china", min_zoom=4, max_zoom=10)
    assert item_file_stem(item, taken=["china_z4_10"]) == "china_z4_10-2"
    assert (
        item_file_stem(item, taken=["china_z4_10", "china_z4_10-2"]) == "china_z4_10-3"
    )


def test_planned_output_paths_only_covers_enabled_in_order() -> None:
    plan = DownloadPlan(name="计划", output_dir="D:/out")
    plan.add_item(make_item("hangzhou", level=RegionLevel.CITY, selected_tiles=1, candidate_tiles=1))
    plan.add_item(make_item("china", level=RegionLevel.COUNTRY, selected_tiles=1, candidate_tiles=1))
    plan.add_item(make_item("zhejiang", level=RegionLevel.PROVINCE, selected_tiles=1, candidate_tiles=1))
    plan.item("zhejiang").enabled = False

    paths = planned_output_paths(plan)
    assert list(paths) == ["china", "hangzhou"]
    assert paths["china"] == Path("D:/out/计划/china_z4_10.mbtiles")
    assert paths["hangzhou"] == Path("D:/out/计划/hangzhou_z4_10.mbtiles")
    # 缺省输出目录时落到 DEFAULT_OUTPUT_DIR（不在这里创建任何目录）
    assert plan_directory(DownloadPlan(name="p")).name == "p"


# --------------------------------------------------------------------------- #
# 序列化（Spec §17 / §18）
# --------------------------------------------------------------------------- #
def test_item_payload_only_has_the_whitelisted_keys() -> None:
    payload = item_to_payload(make_item("china"))
    assert set(payload) == {
        "id",
        "region_id",
        "region_name",
        "region_level",
        "min_zoom",
        "max_zoom",
        "enabled",
    }
    # 估算结果与可用性判定**不写盘**（是算出来的结论，重算即可）
    assert "selected_tiles" not in payload
    assert "estimated_bytes" not in payload
    assert "status" not in payload
    assert payload["region_level"] == "country"


def test_plan_payload_round_trip() -> None:
    plan = DownloadPlan(
        name="我的计划",
        output_dir="D:/out",
        map_layer="vec_w",
        with_labels=True,
    )
    plan.add_item(make_item("china"))
    plan.add_item(make_item("zhejiang", name="浙江省", level=RegionLevel.PROVINCE, min_zoom=9, max_zoom=13))
    plan.item("zhejiang").enabled = False

    restored = plan_from_payload(plan_to_payload(plan))
    assert restored.id == plan.id
    assert (restored.name, restored.output_dir) == ("我的计划", "D:/out")
    assert (restored.map_layer, restored.with_labels) == ("vec_w", True)
    assert [item.region_id for item in restored.items] == ["china", "zhejiang"]
    assert restored.item("zhejiang").enabled is False
    assert restored.item("zhejiang").region_level is RegionLevel.PROVINCE
    assert restored.item("zhejiang").zoom_text == "Z9~13"
    # 估算不落盘 ⇒ 读回来是"尚未估算"
    assert restored.total_tiles == 0
    assert restored.needs_estimate() is True


def test_item_from_payload_needs_a_region_id() -> None:
    with pytest.raises(PlanStoreError):
        item_from_payload({"region_id": "  "})
    with pytest.raises(PlanStoreError):
        item_from_payload("不是对象")


def test_item_from_payload_rejects_bad_numbers() -> None:
    with pytest.raises(PlanStoreError):
        item_from_payload({"region_id": "china", "min_zoom": "四", "max_zoom": 10})


def test_item_from_payload_requires_both_zooms() -> None:
    """缺 Zoom 一律拒绝：拿默认区间顶上去会让"文件改坏了"变成"静默下几十万张"。"""
    with pytest.raises(PlanStoreError):
        item_from_payload({"region_id": "china", "min_zoom": 4})
    with pytest.raises(PlanStoreError):
        item_from_payload({"region_id": "china"})


def test_item_from_payload_rejects_bad_booleans() -> None:
    """``"false"`` 必须是假的，认不出的值要报错（而不是默默当"启用"）。"""
    for value, expected in (("false", False), ("true", True), (0, False), (1, True)):
        assert (
            item_from_payload(
                {"region_id": "a", "min_zoom": 4, "max_zoom": 10, "enabled": value}
            ).enabled
            is expected
        )
    with pytest.raises(PlanStoreError):
        item_from_payload(
            {"region_id": "a", "min_zoom": 4, "max_zoom": 10, "enabled": "也许"}
        )


def test_plan_from_payload_skips_a_bad_item_without_losing_the_plan() -> None:
    """Spec §18：一条坏条目不该让整份计划打不开。"""
    payload = {
        "id": "plan-1",
        "name": "计划",
        "items": [
            {"region_id": "china", "region_level": "country", "min_zoom": 4, "max_zoom": 10},
            {"region_name": "缺了 region_id"},
            {"region_id": "zhejiang", "region_level": "province", "min_zoom": 9, "max_zoom": 13},
        ],
    }
    plan = plan_from_payload(payload)
    assert plan.id == "plan-1"
    assert [item.region_id for item in plan.items] == ["china", "zhejiang"]


def test_plan_from_payload_rejects_broken_items_field() -> None:
    with pytest.raises(PlanStoreError):
        plan_from_payload({"items": {"region_id": "china"}})


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #
def test_store_save_and_load(plans_file: Path) -> None:
    store = PlanStore(plans_file)
    plan = DownloadPlan(name="计划", output_dir="D:/out", map_layer="vec_w")
    plan.add_item(make_item("china"))
    store.save(plan)

    assert plans_file.is_file()
    loaded = store.load_all()
    assert len(loaded) == 1
    assert loaded[0].id == plan.id
    assert loaded[0].item("china").region_id == "china"
    # 原子写不留临时文件
    assert list(plans_file.parent.glob("*.tmp")) == []


def test_store_missing_file_means_no_plan(plans_file: Path) -> None:
    """Spec §18：旧版本没有计划文件时正常启动。"""
    store = PlanStore(plans_file / "不存在")
    assert store.exists is False
    assert store.load_all() == []
    assert store.get("whatever") is None


def test_store_upserts_by_id(plans_file: Path) -> None:
    store = PlanStore(plans_file)
    plan = DownloadPlan(id="fixed", name="计划")
    store.save(plan)
    plan.add_item(make_item("china"))
    store.save(plan)
    assert len(store.load_all()) == 1
    assert len(store.get("fixed").items) == 1


def test_store_keeps_other_plans_untouched(plans_file: Path) -> None:
    store = PlanStore(plans_file)
    store.save(DownloadPlan(id="one", name="甲"))
    store.save(DownloadPlan(id="two", name="乙"))
    assert [plan.id for plan in store.load_all()] == ["one", "two"]
    assert store.delete("one") is True
    assert store.delete("one") is False
    assert [plan.id for plan in store.load_all()] == ["two"]


def test_store_quarantines_a_corrupt_file(plans_file: Path) -> None:
    """损坏的计划文件被隔离（**不删除**），程序照常启动。"""
    plans_file.parent.mkdir(parents=True, exist_ok=True)
    plans_file.write_text("{ 这不是 JSON", encoding="utf-8")
    store = PlanStore(plans_file)
    assert store.load_all() == []
    quarantined = list(plans_file.parent.glob(f"{plans_file.name}.corrupt.*"))
    assert len(quarantined) == 1
    assert "这不是 JSON" in quarantined[0].read_text(encoding="utf-8")


def test_store_quarantines_a_non_object_top_level(plans_file: Path) -> None:
    plans_file.parent.mkdir(parents=True, exist_ok=True)
    plans_file.write_text("[1, 2, 3]", encoding="utf-8")
    assert PlanStore(plans_file).load_all() == []
    assert len(list(plans_file.parent.glob(f"{plans_file.name}.corrupt.*"))) == 1


def test_store_quarantines_a_broken_plans_field(plans_file: Path) -> None:
    plans_file.parent.mkdir(parents=True, exist_ok=True)
    plans_file.write_text(json.dumps({"version": 1, "plans": "不是数组"}), encoding="utf-8")
    assert PlanStore(plans_file).load_all() == []
    assert len(list(plans_file.parent.glob(f"{plans_file.name}.corrupt.*"))) == 1


def test_store_treats_an_empty_file_as_no_plans(plans_file: Path) -> None:
    plans_file.parent.mkdir(parents=True, exist_ok=True)
    plans_file.write_text("   \n", encoding="utf-8")
    assert PlanStore(plans_file).load_all() == []
    # 空文件**不隔离**：它更可能是"上次写到一半被打断"，没有抢救价值也没有信息
    assert list(plans_file.parent.glob(f"{plans_file.name}.corrupt.*")) == []


def test_store_refuses_payload_with_secret_keys(plans_file: Path, monkeypatch) -> None:
    """第二道防线：字段名里出现疑似密钥片段就拒绝写盘。"""
    from app.planning import plan_store as store_module

    def leaky(plan):  # noqa: ANN001, ANN202 - 故意造一个泄漏字段
        payload = plan_to_payload(plan)
        payload["token"] = "should-never-be-written"
        return payload

    monkeypatch.setattr(store_module, "plan_to_payload", leaky)
    with pytest.raises(PlanStoreError):
        PlanStore(plans_file).save(DownloadPlan(name="计划"))
    assert plans_file.exists() is False


def test_plan_file_never_contains_a_token(plans_file: Path) -> None:
    """端到端：真的写一次盘，然后在文本里找密钥形状的东西。"""
    store = PlanStore(plans_file)
    plan = DownloadPlan(name="计划", map_layer="vec_w")
    plan.add_item(make_item("china"))
    store.save(plan)
    text = plans_file.read_text(encoding="utf-8").lower()
    for fragment in ("token", "secret", "password", "apikey", "credential"):
        assert fragment not in text
    assert "geojson" not in text and "coordinates" not in text


def test_secret_key_blacklist_matches_the_task_store() -> None:
    """两份黑名单必须逐字相同。

    ``app.planning`` 在导入期不能拉 ``app.tasks``（会成环，见
    :mod:`app.planning` 的模块文档），所以那张表在两边各存了一份。
    这条用例就是"两份不会漂移"的保证 —— 比"记得同步"可靠。
    """
    from app.planning.plan_store import _FORBIDDEN_KEY_FRAGMENTS as plan_list
    from app.tasks.task_store import _FORBIDDEN_KEY_FRAGMENTS as task_list

    assert plan_list == task_list


def test_store_round_trips_through_the_real_file_schema(plans_file: Path) -> None:
    """写出来的 JSON 结构是可读的、带版本号的（Spec §17）。"""
    store = PlanStore(plans_file)
    plan = DownloadPlan(name="计划", output_dir="D:/out", map_layer="vec_w", with_labels=True)
    plan.add_item(make_item("china"))
    store.save(plan)
    payload = json.loads(plans_file.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert isinstance(payload["plans"], list)
    assert set(payload["plans"][0]) == {
        "id",
        "name",
        "output_dir",
        "map_layer",
        "with_labels",
        "items",
    }
    assert payload["plans"][0]["items"][0]["region_id"] == "china"


# --------------------------------------------------------------------------- #
# 导入顺序（子进程：in-process 已经被别的 import 污染，测不出循环）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "statement",
    [
        "import app.planning.download_plan",
        "import app.planning.plan_store",
        "import app.planning.plan_builder",
        "import app.planning.plan_estimator",
        "import app.planning.plan_validator",
        "import app.planning.plan_service",
        # 最容易踩的那条：先 geo 后 planning
        "import app.geo; import app.planning.plan_service",
        "import app.regions; import app.planning.plan_builder",
    ],
)
def test_plan_modules_import_in_any_order(statement: str) -> None:
    """计划的几个模块在任何导入顺序下都必须能装好。

    这不是"顺便测一下"：``app.regions`` 在导入期会拉 ``app.geo.region``，
    而后者又会回头拉 ``app.planning``。如果计划的服务模块被放进
    ``app/planning/__init__.py`` 的转出清单，``import app.geo`` 就会撞上
    "半初始化的 ``app.geo.region``"（实测报
    ``cannot import name 'is_safe_region_relative_path' from partially
    initialized module 'app.geo.region'``）。这条用例把"它们不在那份清单里"
    变成一条会失败的断言。
    """
    completed = subprocess.run(
        [sys.executable, "-c", statement],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr


def test_plan_services_are_not_reexported_from_the_planning_package() -> None:
    """``app.planning`` 只转出模型，不转出服务（理由见上一条用例）。"""
    import app.planning as planning

    for name in (
        "DownloadPlanBuilder",
        "DownloadPlanEstimator",
        "PlanValidator",
        "PlanService",
        "PlanStore",
    ):
        assert not hasattr(planning, name), f"app.planning 不该转出 {name}"
    # 模型是可以转出的：它只依赖叶子模块
    assert hasattr(planning, "DownloadPlan")
    assert hasattr(planning, "DownloadPlanItem")
