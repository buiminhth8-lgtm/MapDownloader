"""级联选择控制器（Slice 19，Spec §2 / §4 / §5 / §6 / §12 / §13）。

这一组**不装配窗口**：控制器刻意做成不依赖 Qt 的纯对象，所以"国家 → 省 → 市
怎么推导"、"推荐区间从哪来"、"超范围会不会改用户的值"这三件事可以脱离界面
直接断言。界面上那层（控件可见性、只读、Radio 互斥）在
``tests/test_download_panel_region_selection.py`` 里守。

三条最值得守的性质：

1. **层级只来自目录**（Spec §2）：控制器不推 parent、不扫文件；
2. **Zoom 只来自策略表**（Spec §4）：这里断言的是"服务说多少就是多少"，
   而不是把 4/10/13 这些数字再抄一遍；
3. **只提醒不改值**（Spec §5）：每条"看到警告"的用例都配一条"入参原样带着走"。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.geo import RegionBoundaryMissing, geometry_to_bounds
from app.planning import RegionLevel
from app.regions import RegionService
from app.ui.region_selection import RegionSelectionController, ZoomMode

FIXTURE_CATALOG = Path(__file__).resolve().parent / "fixtures" / "regions" / "catalog"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture
def controller(region_root: Path) -> RegionSelectionController:
    """挂在合成目录（6 条记录、三层）上的控制器。"""
    return RegionSelectionController(RegionService(root=region_root))


def _catalog_root(directory: Path, entries: list[dict], *, version: int = 1) -> Path:
    """按给定的记录列表造一份目录（用来造"省下面没城市"这类形状）。"""
    root = directory / "regions"
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.json").write_text(
        json.dumps({"version": version, "regions": entries}, ensure_ascii=False),
        encoding="utf-8",
    )
    return root


def _copy_fixture(root: Path) -> None:
    """把合成树拷到 ``root``（需要"把某个文件删掉/弄坏"的用例用）。"""
    shutil.copytree(FIXTURE_CATALOG, root, dirs_exist_ok=True)


# --------------------------------------------------------------------------- #
# 级联（Spec §2）
# --------------------------------------------------------------------------- #
def test_country_province_city_cascade(controller: RegionSelectionController) -> None:
    assert [item.id for item in controller.list_countries()] == ["demoland"]

    assert controller.select_country("demoland") is True
    assert [item.id for item in controller.list_provinces()] == ["northland", "southland"]

    assert controller.select_province("northland") is True
    assert [item.id for item in controller.list_cities()] == ["rivertown", "hilltown"]

    assert controller.select_city("rivertown") is True
    assert controller.selected_region_id == "rivertown"
    assert controller.path_text == "示例国 / 示例北省 / 示例河市"


def test_level_follows_the_deepest_selection(controller: RegionSelectionController) -> None:
    """Spec §2 的三种组合：只选国 → 国家级；选省不选市 → 省级；选市 → 城市级。"""
    assert controller.level is RegionLevel.CUSTOM, "什么都没选时没有等级"

    controller.select_country("demoland")
    assert controller.level is RegionLevel.COUNTRY

    controller.select_province("northland")
    assert controller.level is RegionLevel.PROVINCE
    assert controller.selected_region_id == "northland"

    controller.select_city("rivertown")
    assert controller.level is RegionLevel.CITY

    # 市这一级"放回全部" → 退回省级
    assert controller.select_city(None) is True
    assert controller.level is RegionLevel.PROVINCE
    assert controller.selected_region_id == "northland"


def test_switching_country_clears_province_and_city(
    controller: RegionSelectionController,
) -> None:
    """换国家必须把省、市一起清掉（Spec §2）—— 否则会留下跨国家的组合。"""
    controller.select_country("demoland")
    controller.select_province("northland")
    controller.select_city("rivertown")

    assert controller.select_country("demoland") is True
    assert controller.province is None
    assert controller.city is None
    assert controller.level is RegionLevel.COUNTRY


def test_switching_province_clears_city(controller: RegionSelectionController) -> None:
    controller.select_country("demoland")
    controller.select_province("northland")
    controller.select_city("rivertown")

    assert controller.select_province("southland") is True
    assert controller.city is None
    assert controller.level is RegionLevel.PROVINCE
    assert [item.id for item in controller.list_cities()] == ["lakecity"]


def test_selecting_a_city_from_another_province_is_refused(
    controller: RegionSelectionController,
) -> None:
    """跨省选市要被拦下：界面上的三段下拉本身不会造出这种组合，
    但 `set_region_id` / 偏好恢复那条路会 —— 拦在控制器这一层就够了。"""
    controller.select_country("demoland")
    controller.select_province("northland")

    assert controller.select_city("lakecity") is False
    assert controller.city is None
    assert "不属于当前选择的省份" in controller.error


def test_select_region_walks_the_ancestor_chain(
    controller: RegionSelectionController,
) -> None:
    """给一条末端 id，三级自己铺满（回填任务记录 / 偏好走的就是这条路）。"""
    assert controller.select_region("rivertown") is True
    assert (controller.country.id, controller.province.id, controller.city.id) == (
        "demoland",
        "northland",
        "rivertown",
    )

    # 再给一条省级 id：城市必须被清掉，而不是留在上一次那个
    assert controller.select_region("northland") is True
    assert controller.city is None
    assert controller.level is RegionLevel.PROVINCE

    assert controller.select_region("demoland") is True
    assert controller.province is None and controller.city is None
    assert controller.level is RegionLevel.COUNTRY


@pytest.mark.parametrize("bad", ["hangzhou", "", None, "  "])
def test_unknown_region_is_refused_with_a_readable_reason(
    controller: RegionSelectionController, bad: object
) -> None:
    assert controller.select_region(bad) is False  # type: ignore[arg-type]
    assert controller.error


def test_catalog_empty_is_reported_not_crashed(work_dir: Path) -> None:
    """目录里一条记录都没有：返回空 + 一句人话，绝不抛异常（Spec §13）。"""
    root = _catalog_root(work_dir, [])
    controller = RegionSelectionController(RegionService(root=root))

    assert controller.list_countries() == ()
    assert "index.json" in controller.error
    assert controller.level is RegionLevel.CUSTOM
    assert controller.download_region() is None


def test_province_without_cities_is_not_an_error(work_dir: Path) -> None:
    """省下面没有城市是**数据形状**，不是错误：空列表 + 空 error。"""
    root = _catalog_root(
        work_dir,
        [
            {"id": "demoland", "name": "示例国", "level": "country", "parentId": None,
             "geojson": "country/demoland.geojson"},
            {"id": "northland", "name": "示例北省", "level": "province",
             "parentId": "demoland", "geojson": "province/northland.geojson"},
        ],
    )
    controller = RegionSelectionController(RegionService(root=root))
    controller.select_country("demoland")
    controller.select_province("northland")

    assert controller.list_cities() == ()
    assert controller.error == ""
    assert controller.level is RegionLevel.PROVINCE


def test_county_is_the_deepest_selection(work_dir: Path) -> None:
    """国 → 省 → 市 → 县四级：选到县时等级才是县级（Slice 27）。"""
    root = _catalog_root(
        work_dir,
        [
            {"id": "demoland", "name": "示例国", "level": "country", "parentId": None,
             "geojson": "country/demoland.geojson"},
            {"id": "northland", "name": "示例北省", "level": "province",
             "parentId": "demoland", "geojson": "province/northland.geojson"},
            {"id": "rivertown", "name": "示例河市", "level": "city",
             "parentId": "northland", "geojson": "city/rivertown.geojson"},
            {"id": "hilltown", "name": "示例山县", "level": "county",
             "parentId": "rivertown", "geojson": "county/hilltown.geojson"},
        ],
    )
    controller = RegionSelectionController(RegionService(root=root))
    controller.select_country("demoland")
    controller.select_province("northland")
    controller.select_city("rivertown")
    controller.select_county("hilltown")

    assert controller.selected_region_id == "hilltown"
    assert controller.level is RegionLevel.COUNTY
    assert controller.county is not None and controller.county.id == "hilltown"
    # 县下面没有更细的行政级（目录里没有挂到 hilltown 下的条目）
    assert controller.service.children("hilltown") == ()


def test_county_may_hang_directly_under_a_province(work_dir: Path) -> None:
    """县挂省（直辖市 / 省直辖县）时，跳过市这一级也能选中（Slice 27）。"""
    root = _catalog_root(
        work_dir,
        [
            {"id": "demoland", "name": "示例国", "level": "country", "parentId": None,
             "geojson": "country/demoland.geojson"},
            {"id": "northland", "name": "示例直辖市", "level": "province",
             "parentId": "demoland", "geojson": "province/northland.geojson"},
            {"id": "hilltown", "name": "示例区", "level": "county",
             "parentId": "northland", "geojson": "county/hilltown.geojson"},
        ],
    )
    controller = RegionSelectionController(RegionService(root=root))
    controller.select_country("demoland")
    controller.select_province("northland")

    # 没有市级：list_cities 空，但 list_counties 直接按省查得到
    assert controller.list_cities() == ()
    counties = controller.list_counties("northland")
    assert [item.id for item in counties] == ["hilltown"]

    assert controller.select_county("hilltown") is True
    assert controller.level is RegionLevel.COUNTY
    assert controller.selected_region_id == "hilltown"


def test_non_administrative_entry_cannot_be_selected(
    work_dir: Path,
) -> None:
    """任务区 / 自定义条目不进四段级联：界面上没有这一段下拉（Spec §18 不实现 Mission）。"""
    root = _catalog_root(
        work_dir,
        [
            {"id": "demoland", "name": "示例国", "level": "country", "parentId": None,
             "geojson": "country/demoland.geojson"},
            {"id": "site-a", "name": "作业区 A", "level": "mission",
             "parentId": "demoland", "geojson": "mission/site_a.geojson"},
        ],
    )
    controller = RegionSelectionController(RegionService(root=root))

    assert controller.select_region("site-a") is False
    assert "只支持国家 / 省 / 市 / 县" in controller.error


# --------------------------------------------------------------------------- #
# 推荐 Zoom（Spec §4）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("region_id", "level"),
    [
        ("demoland", RegionLevel.COUNTRY),
        ("northland", RegionLevel.PROVINCE),
        ("rivertown", RegionLevel.CITY),
    ],
)
def test_recommended_range_comes_from_the_strategy_table(
    controller: RegionSelectionController, region_id: str, level: RegionLevel
) -> None:
    """推荐区间**就是**策略服务说的那个 —— 控制器不自己算、界面更不抄。"""
    controller.select_region(region_id)
    expected = controller.strategy.recommend(level)

    assert controller.level is level
    assert controller.recommended_range == (expected.min_zoom, expected.max_zoom)


def test_recommended_range_is_none_without_an_administrative_level(
    controller: RegionSelectionController,
) -> None:
    """没选任何行政区域（或矩形模式）不给推荐 —— Spec §7 不套用等级策略。"""
    assert controller.recommended_range is None
    assert controller.has_level_policy is False


def test_recommended_mode_overrides_the_given_range(
    controller: RegionSelectionController,
) -> None:
    """推荐模式下"实际会下载的区间"是推荐值（Spec §4）。"""
    controller.select_region("rivertown")
    assert controller.zoom_mode is ZoomMode.RECOMMENDED
    assert controller.effective_range(8, 17) == controller.recommended_range


def test_custom_mode_keeps_the_user_range_untouched(
    controller: RegionSelectionController,
) -> None:
    """自定义模式下入参原样返回：一个字符都不改（Spec §5）。"""
    controller.select_region("rivertown")
    controller.set_zoom_mode(ZoomMode.CUSTOM)

    assert controller.effective_range(8, 17) == (8, 17)


def test_custom_mode_warns_without_touching_the_input(
    controller: RegionSelectionController,
) -> None:
    """中国 + Z8~18（Spec §5 的例子）：warning 为真，且**入参原样带着走**。"""
    controller.select_region("demoland")
    controller.set_zoom_mode(ZoomMode.CUSTOM)

    recommendation = controller.recommendation(8, 18)
    assert recommendation is not None
    assert recommendation.warning is True
    assert "超出全国级推荐范围" in recommendation.warning_text
    # 关键：值没被改，也没被 clamp
    assert (recommendation.requested_min_zoom, recommendation.requested_max_zoom) == (8, 18)
    assert controller.effective_range(8, 18) == (8, 18)


def test_inside_the_recommendation_there_is_no_warning(
    controller: RegionSelectionController,
) -> None:
    controller.select_region("demoland")
    controller.set_zoom_mode(ZoomMode.CUSTOM)

    recommendation = controller.recommendation(4, 10)
    assert recommendation is not None
    assert recommendation.warning is False


def test_zoom_mode_parse_is_forgiving() -> None:
    """偏好里存着本版本不认识的模式 → 回落推荐（不猜成"自定义"）。"""
    assert ZoomMode.parse("custom") is ZoomMode.CUSTOM
    assert ZoomMode.parse("RECOMMENDED") is ZoomMode.RECOMMENDED
    assert ZoomMode.parse(None) is ZoomMode.RECOMMENDED
    assert ZoomMode.parse(123) is ZoomMode.RECOMMENDED


def test_inverted_zoom_range_is_reported_not_raised(
    controller: RegionSelectionController,
) -> None:
    """自定义模式下把 Min 拉到比 Max 大：策略层会拒绝 —— 界面这一层必须拿到
    ``None`` + 一句人话，而不是让异常从 Qt 槽里漏出去被静默吞掉（Spec §13）。"""
    controller.select_region("rivertown")
    controller.set_zoom_mode(ZoomMode.CUSTOM)

    assert controller.recommendation(18, 4) is None
    assert "Zoom 策略不可用" in controller.error


# --------------------------------------------------------------------------- #
# DownloadRegion（Spec §3 / §13）
# --------------------------------------------------------------------------- #
def test_download_region_uses_the_actual_boundary(
    controller: RegionSelectionController,
) -> None:
    """区域一律由 RegionService 构造，范围来自 GeoJSON 的真实外包矩形。"""
    controller.select_region("rivertown")
    region = controller.download_region()

    assert region is not None
    assert region.region_id == "rivertown"
    assert region.region_level is RegionLevel.CITY
    assert region.is_polygon
    assert (region.bounds.west, region.bounds.north) == (110.0, 34.0)


def test_download_region_without_a_selection_is_none(
    controller: RegionSelectionController,
) -> None:
    assert controller.download_region() is None
    assert "还没有选择行政区域" in controller.error


def test_missing_boundary_file_fails_safely(
    region_root: Path,
) -> None:
    """边界文件不在 → 明确失败 + 告诉他放哪，**绝不退化成矩形**（Spec §13）。"""
    (region_root / "city" / "rivertown.geojson").unlink()
    controller = RegionSelectionController(RegionService(root=region_root))
    controller.select_region("rivertown")

    assert controller.boundary_available is False
    assert controller.download_region() is None
    assert "rivertown.geojson" in controller.error
    assert "不许退化成矩形" not in controller.error  # 错误文案里不该出现内部用语


def test_corrupted_boundary_file_fails_safely(region_root: Path) -> None:
    (region_root / "city" / "rivertown.geojson").write_text("{ 坏了", encoding="utf-8")
    controller = RegionSelectionController(RegionService(root=region_root))
    controller.select_region("rivertown")

    assert controller.download_region() is None
    assert controller.error


def test_load_document_raises_the_documented_errors(region_root: Path) -> None:
    controller = RegionSelectionController(RegionService(root=region_root))
    controller.select_region("rivertown")
    document = controller.load_document()
    bounds = geometry_to_bounds(document.geometry)
    assert (bounds.west, bounds.south, bounds.east, bounds.north) == (110.0, 33.0, 111.0, 34.0)

    (region_root / "city" / "rivertown.geojson").unlink()
    with pytest.raises(RegionBoundaryMissing):
        controller.load_document()


def test_missing_message_points_at_the_file(controller: RegionSelectionController) -> None:
    """没选区域时也要给一句话，而不是空串。"""
    assert controller.missing_message() == "还没有选择行政区域"


# --------------------------------------------------------------------------- #
# 按区域自动生成的地图名（Slice 43）
# --------------------------------------------------------------------------- #
#: 与真实中国目录同构的一小份：省 → 市 → 县，外加一个直辖市（下辖区直接挂省）。
_CHINA_ENTRIES: list[dict] = [
    {"id": "china", "name": "中国", "level": "country", "parentId": None,
     "geojson": "country/china.geojson"},
    {"id": "jiangsu", "name": "江苏省", "level": "province", "parentId": "china",
     "geojson": "province/jiangsu.geojson"},
    {"id": "suzhou", "name": "苏州市", "level": "city", "parentId": "jiangsu",
     "geojson": "city/suzhou.geojson"},
    {"id": "kunshan", "name": "昆山市", "level": "county", "parentId": "suzhou",
     "geojson": "county/kunshan.geojson"},
    {"id": "beijing", "name": "北京市", "level": "province", "parentId": "china",
     "geojson": "province/beijing.geojson"},
    {"id": "dongcheng", "name": "东城区", "level": "county", "parentId": "beijing",
     "geojson": "county/dongcheng.geojson"},
]


def _china_controller(tmp_path: Path) -> RegionSelectionController:
    return RegionSelectionController(RegionService(root=_catalog_root(tmp_path, _CHINA_ENTRIES)))


def test_map_name_suggestion_adds_one_level_at_a_time(tmp_path: Path) -> None:
    """用户点下来的每一级，名字就跟着长一级（这正是他要的文件名）。

    全国 → ``全国基础包``（不写"中国"）；省 → ``江苏省``；市 → ``江苏省_苏州市``；
    县 → 再加一段。国家节点永远不出现 —— 它是所有选择共同的上下文。
    """
    controller = _china_controller(tmp_path)

    assert controller.select_country("china") is True
    assert controller.map_name_suggestion == "全国基础包"

    assert controller.select_province("jiangsu") is True
    assert controller.map_name_suggestion == "江苏省"

    assert controller.select_city("suzhou") is True
    assert controller.map_name_suggestion == "江苏省_苏州市"

    assert controller.select_county("kunshan") is True
    assert controller.map_name_suggestion == "江苏省_苏州市_昆山市"


def test_map_name_suggestion_handles_municipalities_without_a_city_level(tmp_path: Path) -> None:
    """直辖市下辖区只有"省 → 区"两段 —— 不能按固定段数硬拼。

    这正是"按真实父子链拼"而不是"省 + 市 + 县三段"的理由：北京没有市级上级，
    硬拼会拼出 ``北京市__东城区``（中间空一段）。
    """
    controller = _china_controller(tmp_path)
    assert controller.select_country("china") is True
    assert controller.select_province("beijing") is True
    assert controller.select_county("dongcheng") is True

    assert controller.map_name_suggestion == "北京市_东城区"


def test_map_name_suggestion_names_other_countries_by_their_own_name(
    controller: RegionSelectionController,
) -> None:
    """"全国基础包"是给中国的词；别的国家叫什么就写什么，不套用。"""
    assert controller.select_country("demoland") is True
    assert controller.map_name_suggestion == "示例国基础包"

    assert controller.select_province("northland") is True
    assert controller.map_name_suggestion == "示例北省"

    assert controller.select_city("rivertown") is True
    assert controller.map_name_suggestion == "示例北省_示例河市"


def test_map_name_suggestion_is_empty_without_a_country(
    controller: RegionSelectionController,
) -> None:
    """什么都没选时返回空串 —— 调用方据此保留用户已填的名字，不拿空串覆盖。"""
    controller.reset()
    assert controller.map_name_suggestion == ""


def test_map_name_suggestion_survives_a_broken_parent_link(tmp_path: Path) -> None:
    """父 id 指向不存在的记录：退化成"只有自己这一段"，绝不死循环/抛异常。

    目录坏了该由校验器报错；界面不该因此卡住或崩掉（Spec §13 的同一原则）。
    """
    entries = [
        {"id": "china", "name": "中国", "level": "country", "parentId": None,
         "geojson": "country/china.geojson"},
        {"id": "orphan", "name": "孤儿市", "level": "city", "parentId": "missing",
         "geojson": "city/orphan.geojson"},
    ]
    controller = RegionSelectionController(RegionService(root=_catalog_root(tmp_path, entries)))
    assert controller.select_region("orphan") is True

    assert controller.map_name_suggestion == "孤儿市"


# --------------------------------------------------------------------------- #
# 按区域自动生成的地图名（Slice 43）
# --------------------------------------------------------------------------- #
#: 与真实中国目录同构的一小份：省 → 市 → 县，外加一个直辖市（下辖区直接挂省）。
_CHINA_ENTRIES: list[dict] = [
    {"id": "china", "name": "中国", "level": "country", "parentId": None,
     "geojson": "country/china.geojson"},
    {"id": "jiangsu", "name": "江苏省", "level": "province", "parentId": "china",
     "geojson": "province/jiangsu.geojson"},
    {"id": "suzhou", "name": "苏州市", "level": "city", "parentId": "jiangsu",
     "geojson": "city/suzhou.geojson"},
    {"id": "kunshan", "name": "昆山市", "level": "county", "parentId": "suzhou",
     "geojson": "county/kunshan.geojson"},
    {"id": "beijing", "name": "北京市", "level": "province", "parentId": "china",
     "geojson": "province/beijing.geojson"},
    {"id": "dongcheng", "name": "东城区", "level": "county", "parentId": "beijing",
     "geojson": "county/dongcheng.geojson"},
]


def _china_controller(tmp_path: Path) -> RegionSelectionController:
    return RegionSelectionController(RegionService(root=_catalog_root(tmp_path, _CHINA_ENTRIES)))


def test_map_name_suggestion_adds_one_level_at_a_time(tmp_path: Path) -> None:
    """用户点下来的每一级，名字就跟着长一级（这正是他要的文件名）。

    全国 → ``全国基础包``（不写"中国"）；省 → ``江苏省``；市 → ``江苏省_苏州市``；
    县 → 再加一段。国家节点永远不出现 —— 它是所有选择共同的上下文。
    """
    controller = _china_controller(tmp_path)

    assert controller.select_country("china") is True
    assert controller.map_name_suggestion == "全国基础包"

    assert controller.select_province("jiangsu") is True
    assert controller.map_name_suggestion == "江苏省"

    assert controller.select_city("suzhou") is True
    assert controller.map_name_suggestion == "江苏省_苏州市"

    assert controller.select_county("kunshan") is True
    assert controller.map_name_suggestion == "江苏省_苏州市_昆山市"


def test_map_name_suggestion_handles_municipalities_without_a_city_level(tmp_path: Path) -> None:
    """直辖市下辖区只有"省 → 区"两段 —— 不能按固定段数硬拼。

    这正是"按真实父子链拼"而不是"省 + 市 + 县三段"的理由：北京没有市级上级，
    硬拼会拼出 ``北京市__东城区``（中间空一段）。
    """
    controller = _china_controller(tmp_path)
    assert controller.select_country("china") is True
    assert controller.select_province("beijing") is True
    assert controller.select_county("dongcheng") is True

    assert controller.map_name_suggestion == "北京市_东城区"


def test_map_name_suggestion_names_other_countries_by_their_own_name(
    controller: RegionSelectionController,
) -> None:
    """"全国基础包"是给中国的词；别的国家叫什么就写什么，不套用。"""
    assert controller.select_country("demoland") is True
    assert controller.map_name_suggestion == "示例国基础包"

    assert controller.select_province("northland") is True
    assert controller.map_name_suggestion == "示例北省"

    assert controller.select_city("rivertown") is True
    assert controller.map_name_suggestion == "示例北省_示例河市"


def test_map_name_suggestion_is_empty_without_a_country(
    controller: RegionSelectionController,
) -> None:
    """什么都没选时返回空串 —— 调用方据此保留用户已填的名字，不拿空串覆盖。"""
    controller.reset()
    assert controller.map_name_suggestion == ""


def test_map_name_suggestion_survives_a_broken_parent_link(tmp_path: Path) -> None:
    """父 id 指向不存在的记录：退化成"只有自己这一段"，绝不死循环/抛异常。

    目录坏了该由校验器报错；界面不该因此卡住或崩掉（Spec §13 的同一原则）。
    """
    entries = [
        {"id": "china", "name": "中国", "level": "country", "parentId": None,
         "geojson": "country/china.geojson"},
        {"id": "orphan", "name": "孤儿市", "level": "city", "parentId": "missing",
         "geojson": "city/orphan.geojson"},
    ]
    controller = RegionSelectionController(RegionService(root=_catalog_root(tmp_path, entries)))
    assert controller.select_region("orphan") is True

    assert controller.map_name_suggestion == "孤儿市"
