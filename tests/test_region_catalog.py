"""行政区目录：读 index.json、层级查询、坏数据处置（Slice 18，Spec §3 / §5）。

这一组守的核心是**"目录是一份数据，不是一段代码"**：

- 加一个省 = 往 ``index.json`` 补一条 + 放一个文件，**不改任何 Python**；
- 一条坏记录不该让整份目录不可用（用户会因此连"中国"都选不到），
  所以"跳过它 + 记一条 issue"；
- 整份 index.json 读不出来才是硬失败（抛 :class:`RegionCatalogError`）；
- 建目录**不许解析任何 GeoJSON**（Spec §5）——全国省市数据几百 MB，
  启动时全读一遍会让程序几十秒打不开。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.geo import region as geo_region
from app.planning import RegionLevel
from app.regions import (
    INDEX_FILENAME,
    RegionCatalog,
    RegionCatalogError,
    RegionService,
    RegionLoader,
)
from app.regions.catalog import parse_index_payload
from app.regions.models import CatalogIssueCode

# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _payload(*entries: dict) -> dict:
    """拼一个最小可用的 index.json 结构。"""
    return {"version": 1, "regions": list(entries)}


def _entry(**overrides) -> dict:
    base = {
        "id": "x",
        "name": "示例区",
        "level": "country",
        "parentId": None,
        "geojson": "country/x.geojson",
    }
    base.update(overrides)
    return base


def _issues_of(payload: dict) -> list:
    issues: list = []
    parse_index_payload(payload, issues=issues)
    return issues


def _codes(issues: list) -> set[CatalogIssueCode]:
    return {item.code for item in issues}


# --------------------------------------------------------------------------- #
# 读文件
# --------------------------------------------------------------------------- #
def test_catalog_reads_the_layered_index(region_catalog: RegionCatalog, region_root: Path) -> None:
    """三棵树全读进来，顺序与 index.json 一致。"""
    assert region_catalog.root == region_root
    assert region_catalog.ids == (
        "demoland",
        "northland",
        "southland",
        "rivertown",
        "hilltown",
        "lakecity",
    )
    assert region_catalog.index_path == region_root / INDEX_FILENAME
    assert region_catalog.issues == (), "这份夹具是干净的，不该有任何问题"


def test_root_follows_the_module_global(region_root: Path, monkeypatch) -> None:
    """``root`` 缺省时跟随 ``app.geo.region.REGION_RESOURCE_DIR``。

    验收脚本与测试把内置资源重定向到临时目录时靠的就是这条：目录层**自动**
    跟着走，不需要再多同步一个开关（多一个开关就多一处会忘）。
    """
    monkeypatch.setattr(geo_region, "REGION_RESOURCE_DIR", region_root)

    catalog = RegionCatalog.load()

    assert catalog.root == region_root.resolve()
    assert catalog.get("rivertown") is not None


def test_explicit_root_wins_over_the_global(region_root: Path, monkeypatch) -> None:
    """显式传了 ``root`` 就认它，不再跟随全局。"""
    monkeypatch.setattr(geo_region, "REGION_RESOURCE_DIR", region_root.parent / "nowhere")

    catalog = RegionCatalog.load(root=region_root)

    assert catalog.get("demoland") is not None


def test_missing_index_file_is_a_hard_failure(region_root: Path) -> None:
    """整份目录文件不见了 → 明确报错（不是"空目录"）。"""
    (region_root / INDEX_FILENAME).unlink()

    with pytest.raises(RegionCatalogError, match=INDEX_FILENAME):
        RegionCatalog.load(root=region_root)


def test_index_file_with_broken_json_is_a_hard_failure(region_root: Path) -> None:
    (region_root / INDEX_FILENAME).write_text("{ 这不是 JSON", encoding="utf-8")

    with pytest.raises(RegionCatalogError, match="合法 JSON"):
        RegionCatalog.load(root=region_root)


@pytest.mark.parametrize(
    "payload",
    [
        [1, 2, 3],  # 顶层是数组
        {"version": 1},  # 没有 regions
        {"version": 1, "regions": "demoland"},  # regions 不是数组
    ],
)
def test_structurally_wrong_index_is_a_hard_failure(payload: object) -> None:
    with pytest.raises(RegionCatalogError):
        RegionCatalog.from_payload(payload)


def test_unsupported_index_version_is_reported_but_still_readable() -> None:
    """版本号比本版本高：记一条问题，但**继续读**（向前兼容）。

    多出来的字段我们忽略即可；直接拒读会让"新版本写的数据在旧版本上完全打不开"，
    而这两者之间通常只差一个无关紧要的新字段。
    """
    catalog = RegionCatalog.from_payload(_payload(_entry(id="x")) | {"version": 99})

    assert catalog.get("x") is not None
    assert CatalogIssueCode.INDEX_VERSION_UNSUPPORTED in _codes(list(catalog.issues))


# --------------------------------------------------------------------------- #
# 查询（Spec §3）
# --------------------------------------------------------------------------- #
def test_get_and_require(region_catalog: RegionCatalog) -> None:
    assert region_catalog.get("china") is None
    assert region_catalog.get("RIVERTOWN") is not None, "id 大小写不敏感"
    assert region_catalog.get(None) is None

    item = region_catalog.require("rivertown")
    assert item.name == "示例河市"
    assert item.level is RegionLevel.CITY

    with pytest.raises(RegionCatalogError, match="rivertown2"):
        region_catalog.require("rivertown2")


def test_require_lists_available_ids_when_missing(region_catalog: RegionCatalog) -> None:
    """报错要说清"目录里有什么" —— 否则用户只能靠猜。"""
    with pytest.raises(RegionCatalogError) as excinfo:
        region_catalog.require("hangzhou")

    message = str(excinfo.value)
    assert "demoland" in message and "northland" in message


def test_list_by_level(region_catalog: RegionCatalog) -> None:
    assert [item.id for item in region_catalog.list_by_level(RegionLevel.COUNTRY)] == [
        "demoland"
    ]
    assert [item.id for item in region_catalog.list_by_level("province")] == [
        "northland",
        "southland",
    ]
    assert [item.id for item in region_catalog.list_by_level(RegionLevel.CITY)] == [
        "rivertown",
        "hilltown",
        "lakecity",
    ]
    assert region_catalog.list_by_level(RegionLevel.MISSION) == ()


@pytest.mark.parametrize("level", ["provinces", "COUNTRIES", 1, None, RegionLevel])
def test_list_by_level_rejects_unknown_level(region_catalog: RegionCatalog, level) -> None:
    """等级要**严格**解析：``"provinces"`` 悄悄变成 CUSTOM 只会给出一个空列表。"""
    with pytest.raises(ValueError, match="RegionLevel"):
        region_catalog.list_by_level(level)


def test_list_children_walks_one_level(region_catalog: RegionCatalog) -> None:
    assert [item.id for item in region_catalog.list_children(None)] == ["demoland"], (
        "None 表示「根节点」，调用方不必先问「根是谁」"
    )
    assert [item.id for item in region_catalog.list_children("demoland")] == [
        "northland",
        "southland",
    ]
    assert [item.id for item in region_catalog.list_children("northland")] == [
        "rivertown",
        "hilltown",
    ]
    assert region_catalog.list_children("rivertown") == (), "市下面没有下级"


def test_get_parent_and_ancestors(region_catalog: RegionCatalog) -> None:
    assert region_catalog.get_parent("demoland") is None, "根节点没有上级"
    assert region_catalog.get_parent("northland").id == "demoland"
    assert region_catalog.get_parent("rivertown").id == "northland"
    assert region_catalog.get_parent("不存在") is None

    ancestors = region_catalog.get_ancestors("rivertown")
    assert [item.id for item in ancestors] == ["demoland", "northland"], (
        "从最远的根到最近的父 —— 拼起来正好是一条面包屑"
    )
    assert region_catalog.get_ancestors("demoland") == ()
    assert region_catalog.get_ancestors("不存在") == ()


def test_get_descendants_is_recursive(region_catalog: RegionCatalog) -> None:
    assert [item.id for item in region_catalog.get_descendants("demoland")] == [
        "northland",
        "southland",
        "rivertown",
        "hilltown",
        "lakecity",
    ]
    assert region_catalog.get_descendants("rivertown") == ()


def test_path_text_is_a_breadcrumb(region_catalog: RegionCatalog) -> None:
    assert region_catalog.path_text("rivertown") == "示例国 / 示例北省 / 示例河市"
    assert region_catalog.path_text("demoland") == "示例国"
    assert region_catalog.path_text("不存在") == ""


def test_container_protocol(region_catalog: RegionCatalog) -> None:
    assert len(region_catalog) == 6
    assert "rivertown" in region_catalog
    assert "china" not in region_catalog
    assert [item.id for item in region_catalog] == list(region_catalog.ids)


# --------------------------------------------------------------------------- #
# 坏数据：跳过它，但说出来
# --------------------------------------------------------------------------- #
def test_duplicate_id_keeps_the_first_and_reports_it() -> None:
    catalog = RegionCatalog.from_payload(
        _payload(
            _entry(id="dup", name="先出现的"),
            _entry(id="dup", name="后出现的"),
        )
    )

    assert [item.name for item in catalog.list_all()] == ["先出现的"]
    assert CatalogIssueCode.DUPLICATE_ID in _codes(list(catalog.issues))


def test_unknown_level_entry_is_skipped() -> None:
    catalog = RegionCatalog.from_payload(
        _payload(_entry(id="typo", level="provincee"), _entry(id="ok", name="正常"))
    )

    assert catalog.ids == ("ok",), "等级认不出的那条会被跳过（无法判断它属于哪一层）"
    assert CatalogIssueCode.UNKNOWN_LEVEL in _codes(list(catalog.issues))


def test_missing_level_entry_is_skipped() -> None:
    payload = _payload(_entry(id="nolevel") | {"level": None})
    catalog = RegionCatalog.from_payload(payload)

    assert catalog.ids == ()
    assert CatalogIssueCode.MISSING_LEVEL in _codes(list(catalog.issues))


@pytest.mark.parametrize(
    "bad, code",
    [
        ({"name": "没有 id"}, CatalogIssueCode.MISSING_ID),
        ({"id": "n", "level": "country", "geojson": "a.geojson"}, CatalogIssueCode.MISSING_NAME),
        (
            {"id": "n", "name": "无路径", "level": "country"},
            CatalogIssueCode.MISSING_GEOJSON_PATH,
        ),
        (
            {"id": "n", "name": "越界", "level": "country", "geojson": "../escape.geojson"},
            CatalogIssueCode.UNSAFE_PATH,
        ),
        (
            {"id": "n", "name": "绝对", "level": "country", "geojson": "/etc/passwd"},
            CatalogIssueCode.UNSAFE_PATH,
        ),
        (
            {"id": "n", "name": "盘符", "level": "country", "geojson": "D:/x.geojson"},
            CatalogIssueCode.UNSAFE_PATH,
        ),
    ],
)
def test_bad_entries_are_skipped_with_the_right_code(bad: dict, code) -> None:
    catalog = RegionCatalog.from_payload(_payload(bad))

    assert catalog.ids == ()
    assert code in _codes(list(catalog.issues))


def test_non_object_entry_is_skipped() -> None:
    catalog = RegionCatalog.from_payload(
        _payload("我不是对象", _entry(id="ok", name="正常"))  # type: ignore[arg-type]
    )

    assert catalog.ids == ("ok",)
    assert CatalogIssueCode.ENTRY_NOT_OBJECT in _codes(list(catalog.issues))


def test_malformed_bounds_is_reported_without_dropping_the_entry() -> None:
    """``bounds`` 是可选的：写坏了就丢掉它，条目本身照常可用（Spec §11）。"""
    catalog = RegionCatalog.from_payload(
        _payload(_entry(id="x", bounds={"west": 1, "south": 2}))
    )

    item = catalog.require("x")
    assert item.bounds is None
    assert CatalogIssueCode.BOUNDS_MALFORMED in _codes(list(catalog.issues))


def test_good_bounds_is_read() -> None:
    catalog = RegionCatalog.from_payload(
        _payload(
            _entry(
                id="x",
                bounds={"west": 110.0, "south": 30.0, "east": 114.0, "north": 34.0},
            )
        )
    )

    bounds = catalog.require("x").bounds
    assert bounds is not None
    assert (bounds.west, bounds.south, bounds.east, bounds.north) == (110.0, 30.0, 114.0, 34.0)


def test_unsafe_path_cannot_even_be_constructed() -> None:
    """路径安全在**模型层**就拦住了：坏的值根本活不到目录里。"""
    from app.regions import RegionPreset

    for bad in ("../x.geojson", "/abs/x.geojson", "C:/x.geojson", "a/../../x.geojson"):
        with pytest.raises(ValueError):
            RegionPreset(id="a", name="A", level=RegionLevel.COUNTRY, geojson_path=bad)


def test_flat_filename_still_works() -> None:
    """老写法（**扁平文件名**，没有子目录）继续可用：Slice 16 的第三方预设长这样。"""
    catalog = RegionCatalog.from_payload(
        _payload(_entry(id="flat", geojson="flat.geojson"))
    )

    assert catalog.require("flat").geojson_path == "flat.geojson"


# --------------------------------------------------------------------------- #
# Lazy load（Spec §5）
# --------------------------------------------------------------------------- #
def test_catalog_construction_does_not_read_any_geometry(
    region_root: Path, monkeypatch
) -> None:
    """建目录只能读 index.json，一个 GeoJSON 都不许解析（Spec §5）。

    这条用例是整条 lazy load 承诺的**唯一**证据：把加载器入口换成计数器，
    只要目录层在构造时读了任何一个文件，计数就不再是 0。
    """
    from app.geo import geojson_loader
    from app.regions import loader as loader_module

    calls: list[str] = []

    def spy(path, *args, **kwargs):  # noqa: ANN001, ANN202 - 只为计数
        calls.append(str(path))
        return original(path, *args, **kwargs)

    original = geojson_loader.load_geojson_document
    monkeypatch.setattr(loader_module, "load_geojson_document", spy)
    monkeypatch.setattr(geojson_loader, "load_geojson_document", spy)

    catalog = RegionCatalog.load(root=region_root)
    # 查询也不该触发解析
    catalog.list_by_level(RegionLevel.CITY)
    catalog.get_ancestors("rivertown")
    catalog.list_children("northland")
    assert calls == [], f"建目录/查询不该读几何，实际读了：{calls}"

    # 真的要用几何时才会读 —— 证明这条链是"按需"而不是"从来没接上"
    RegionLoader(catalog).load_geometry("rivertown")
    assert len(calls) == 1
    assert calls[0].endswith("rivertown.geojson")


def test_stale_detects_index_changes(region_catalog: RegionCatalog, region_root: Path) -> None:
    """``stale()`` 认得出目录文件被改过（mtime 或大小任一变）。"""
    assert region_catalog.stale() is False

    path = region_root / INDEX_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["regions"].append(_entry(id="newcomer", name="新来的"))
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert region_catalog.stale() is True


# --------------------------------------------------------------------------- #
# 兜底：目录读不出来时界面还能用
# --------------------------------------------------------------------------- #
def test_service_falls_back_to_builtin_presets_when_the_index_is_gone(
    region_root: Path, monkeypatch
) -> None:
    """index.json 没了 → 退回内置预设 + 记下原因（不是静默，也不是整页罢工）。

    这是"用户手工删了文件 / 打包漏了"时的兜底：下拉框里仍然有"中国"，
    下载 / 续传 / Inspector 全部照常，连"中国"的 Zoom 建议都还是对的；
    但层级查询只会看到那一条 —— 没有元数据就真的没有层级，这里不编造。
    """
    monkeypatch.setattr(geo_region, "REGION_RESOURCE_DIR", region_root)
    (region_root / INDEX_FILENAME).unlink()

    service = RegionService()

    assert [preset.region_id for preset in service.list_presets()] == ["china"]
    assert INDEX_FILENAME in service.catalog_error
    # 合成的那一条**带着等级**（"中国是全国级"是内置预设本身就带着的事实），
    # 所以界面上的 Zoom 建议仍然是对的；但层级查询只能是它一个，不能编出省来。
    assert [item.id for item in service.list_countries()] == ["china"]
    assert service.children("china") == ()
    assert service.get_region("china").level is RegionLevel.COUNTRY


def test_service_uses_the_catalog_when_it_is_there(region_root: Path, monkeypatch) -> None:
    monkeypatch.setattr(geo_region, "REGION_RESOURCE_DIR", region_root)

    service = RegionService()

    assert service.catalog_error == ""
    assert [preset.region_id for preset in service.list_presets()] == [
        "demoland",
        "northland",
        "southland",
        "rivertown",
        "hilltown",
        "lakecity",
    ]


def test_service_follows_a_rewritten_index(region_root: Path, monkeypatch) -> None:
    """index.json 被改过之后，下一次查询读到的是**新**那份。

    不这么做的话，用户在界面上点「刷新」看不到任何变化 —— 而目录正是为了
    "不改代码就能加区域"才存在的。
    """
    monkeypatch.setattr(geo_region, "REGION_RESOURCE_DIR", region_root)
    service = RegionService()
    assert "newcomer" not in [item.id for item in service.catalog.list_all()]

    path = region_root / INDEX_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["regions"].append(
        {
            "id": "newcomer",
            "name": "示例新市",
            "level": "city",
            "parentId": "northland",
            "geojson": "city/rivertown.geojson",
        }
    )
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert "newcomer" in [item.id for item in service.catalog.list_all()]


# --------------------------------------------------------------------------- #
# 两个同名模型的边界
# --------------------------------------------------------------------------- #
def test_the_two_region_presets_are_different_things() -> None:
    """``app.regions.RegionPreset`` 与 ``app.geo.RegionPreset`` **不是**同一个类。

    这条用例存在的意义：两者的名字一样，很容易有人"顺手统一"。
    统一之后要么丢掉 ``parent_id``（层级查询就没了），要么逼着 ``app.geo``
    依赖 ``app.regions``（= 循环导入）。所以这里把差别钉死：
    一个是目录条目，一个是下拉框里的扁平预设。
    """
    from app.geo import region as geo_region_module
    from app.regions import RegionPreset as CatalogEntry

    assert CatalogEntry is not geo_region_module.RegionPreset

    entry = CatalogEntry(id="a", name="A", level=RegionLevel.PROVINCE, parent_id="b")
    assert hasattr(entry, "parent_id")
    assert not hasattr(geo_region_module.RegionPreset("a", "A"), "parent_id")
