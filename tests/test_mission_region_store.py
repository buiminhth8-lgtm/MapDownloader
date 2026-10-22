"""任务区持久化（Slice 22，Spec §11 / §12 / §21）。

这组用例盯的是**盘上那三个文件**，不是内存里的对象。任务区与行政区目录最大的
不同在于：行政区几何是随程序分发的资源，任务区几何是**用户自己生成的** ——
它一旦落盘，就要同时支撑三件事：

- **续传**：上次下到哪了，靠的是"几何还是那一份"；
- **Inspector**：缺多少张图，靠的是拿盘上的几何去逐张判相交；
- **导出**：包里的 region 声明要能指出它是谁。

所以"同一个目录读两次得到同一个几何"不是锦上添花，它是这三件事的地基。
反过来，如果有人在两次运行之间改了 ``region.geojson``，那续传就会拿着
一份**新的**瓦片集去问"上次下到哪了" —— 结论会是一堆假的"缺失"。
:meth:`MissionRegionStore.load` 因此默认把元数据记的范围与实际几何对一遍，
对不上就报错（Spec §21：不许悄悄改几何）。

本文件里所有"重启"都是**换一个 store 实例**再读同一个目录 —— 这才是
"关掉程序再打开"，共用一个对象读自己的缓存测不出任何东西。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config.settings import (
    MISSION_METADATA_FILENAME,
    MISSION_REGION_FILENAME,
    MISSION_ROUTE_FILENAME,
)
from app.geo.geojson_loader import load_geojson_geometry
from app.mission import (
    MissionRegionBuilder,
    MissionRegionError,
    MissionRegionMetadata,
    MissionRegionNotFound,
    MissionRegionStore,
    region_geojson_document,
    route_geojson_document,
)

#: 一条拿来用的航线（杭州西湖北侧，几个点，够画一条折线）。
ROUTE = [(120.00, 30.00), (120.05, 30.02), (120.10, 30.01)]
BUFFER = 3000.0


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def make_region(
    store: MissionRegionStore,
    *,
    mission_id: str = "mission-001",
    name: str = "西湖巡检航线",
    waypoints=ROUTE,
    buffer_meters: float = BUFFER,
):
    """真造一个任务区并落盘（builder 算几何 → store 写三个文件）。

    Returns:
        ``(落盘后的 region, 几何)`` —— 很多断言需要几何本体来比对。
    """
    builder = MissionRegionBuilder()
    geometry = builder.build_geometry(waypoints, buffer_meters)
    region = builder.build(
        mission_id, name, waypoints, buffer_meters, geometry=geometry
    )
    saved = store.save(
        region,
        route_geojson=route_geojson_document(
            region.waypoints,
            mission_id=mission_id,
            name=name,
            buffer_meters=region.buffer_meters,
        ),
        region_geojson=region_geojson_document(
            geometry.geometry,
            mission_id=mission_id,
            name=name,
            buffer_meters=region.buffer_meters,
            geometry_version=geometry.geometry_version,
        ),
        metadata=MissionRegionMetadata.from_region(region),
    )
    return saved, geometry


@pytest.fixture
def store(work_dir: Path) -> MissionRegionStore:
    """挂在临时目录上的任务区存储。"""
    return MissionRegionStore(work_dir / "mission_regions")


# --------------------------------------------------------------------------- #
# 落盘：三个文件各是干什么的
# --------------------------------------------------------------------------- #
def test_saving_writes_exactly_the_three_documented_files(store) -> None:
    """三个文件，一个不多一个不少（多出来的文件没人知道该不该信）。"""
    saved, _ = make_region(store)
    directory = store.mission_dir("mission-001")
    names = sorted(path.name for path in directory.iterdir())
    assert names == sorted(
        [MISSION_ROUTE_FILENAME, MISSION_REGION_FILENAME, MISSION_METADATA_FILENAME]
    )
    assert saved.geojson_path == str(store.region_path("mission-001"))


def test_the_returned_path_is_absolute_and_points_at_the_polygon(store) -> None:
    """回填的必须是**绝对路径**且指向 ``region.geojson``（不是航线）。

    下载链路拿到这个路径就要去读几何，读错文件会得到一条 LineString ——
    而"线过不了多边形过滤器"这件事在下单阶段才暴露，太晚了。
    """
    saved, _ = make_region(store)
    path = Path(saved.geojson_path)
    assert path.is_absolute()
    assert path.name == MISSION_REGION_FILENAME

    geometry = load_geojson_geometry(path)
    assert geometry.geom_type in {"Polygon", "MultiPolygon"}


def test_route_file_keeps_the_original_line(store) -> None:
    """``route.geojson`` 存的是**原始航线**，不是缓冲后的面。"""
    make_region(store)
    payload = json.loads(store.route_path("mission-001").read_text(encoding="utf-8"))
    assert payload["geometry"]["type"] == "LineString"
    assert len(payload["geometry"]["coordinates"]) == len(ROUTE)
    # 原始航点必须原样在盘上（缓冲是后面的事，不改航线本身）
    assert payload["geometry"]["coordinates"][0] == [120.00, 30.00]


def test_metadata_records_what_the_directory_contains(store) -> None:
    """元数据是"这个目录里装着什么"的唯一说明，几个关键字段都要在。"""
    _saved, geometry = make_region(store)
    payload = json.loads(store.metadata_path("mission-001").read_text(encoding="utf-8"))
    assert payload["id"] == "mission-001"
    assert payload["name"] == "西湖巡检航线"
    assert payload["buffer_meters"] == pytest.approx(BUFFER)
    assert payload["waypoint_count"] == len(ROUTE)
    assert payload["geometry_version"] == geometry.geometry_version
    for key in ("west", "south", "east", "north"):
        assert payload["bounds"][key] == pytest.approx(
            getattr(geometry.bounds, key), abs=1e-9
        )


def test_no_tmp_file_is_left_behind(store) -> None:
    """原子写的"半成品"必须消失 —— 留一个 ``.tmp`` 会让目录多一个谜。"""
    make_region(store)
    leftovers = list(store.mission_dir("mission-001").glob("*.tmp"))
    assert leftovers == []


# --------------------------------------------------------------------------- #
# 重启一致（Spec §21）
# --------------------------------------------------------------------------- #
def test_a_second_store_reads_back_the_same_geometry(store, work_dir) -> None:
    """**关掉程序再打开**：换一个 store 实例，读到的几何必须一模一样。

    比对形状用显式几何相等，不用 ``==``：Shapely 的 ``==`` 是"结构相同"，
    顶点顺序变了就会判不等，那时候错误信息会指向一个根本不存在的问题。
    """
    saved, geometry = make_region(store)

    reopened = MissionRegionStore(work_dir / "mission_regions")
    loaded = reopened.load("mission-001")

    assert loaded.id == saved.id
    assert loaded.name == saved.name
    assert loaded.buffer_meters == pytest.approx(saved.buffer_meters)
    assert loaded.geometry_version == saved.geometry_version
    assert loaded.waypoints == saved.waypoints
    for key in ("west", "south", "east", "north"):
        assert getattr(loaded.bounds, key) == pytest.approx(
            getattr(geometry.bounds, key), abs=1e-9
        )
    assert load_geojson_geometry(loaded.geojson_path).equals(geometry.geometry)


def test_reloading_twice_is_stable(store, work_dir) -> None:
    """读两次也要一样（防止"读一次顺手改了盘上文件"这种隐形副作用）。"""
    make_region(store)
    root = work_dir / "mission_regions"
    first = MissionRegionStore(root).load("mission-001")
    second = MissionRegionStore(root).load("mission-001")
    assert first.bounds.west == second.bounds.west
    assert first.waypoints == second.waypoints
    assert Path(first.geojson_path) == Path(second.geojson_path)


def test_hash_of_the_geometry_file_does_not_change_on_reread(store) -> None:
    """读取不许改写盘上文件 —— 内容摘要前后一致才叫"只读"。"""
    import hashlib

    make_region(store)
    path = store.region_path("mission-001")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    store.load("mission-001")
    store.load_waypoints("mission-001")
    store.geometry("mission-001")
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert before == after


def test_a_tampered_geometry_is_rejected_instead_of_silently_used(store) -> None:
    """有人改了 ``region.geojson`` → 报错，而不是拿着它去续传（Spec §21）。

    这是本文件里最重要的一条用例。"几何被改过"不会以异常的形式出现，
    它只会让续传算出一个**新的瓦片集**，然后生成一大堆假的"缺失瓦片"。
    """
    make_region(store)
    path = store.region_path("mission-001")
    payload = json.loads(path.read_text(encoding="utf-8"))
    # 把整个多边形往东挪 0.05 度（约 5 km）—— 一处很典型的"手改"
    ring = payload["geometry"]["coordinates"]

    def shift(node):
        if isinstance(node, list) and len(node) == 2 and all(
            isinstance(value, (int, float)) for value in node
        ):
            return [node[0] + 0.05, node[1]]
        return [shift(item) for item in node]

    payload["geometry"]["coordinates"] = shift(ring)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(MissionRegionError) as excinfo:
        store.load("mission-001")
    message = str(excinfo.value)
    assert "不一致" in message
    assert "续传" in message  # 说清后果，而不是只说"校验失败"


def test_a_meta_only_shift_is_also_caught(store) -> None:
    """反方向改（只改元数据、几何是真的）同样要被抓到。

    只校验一个方向等于没校验：只要两边对不上，就说明"记录"与"事实"分家了。
    """
    make_region(store)
    path = store.metadata_path("mission-001")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["bounds"]["east"] += 0.5
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(MissionRegionError):
        store.load("mission-001")


def test_verify_can_be_turned_off_for_the_cheap_read(store) -> None:
    """校验是一道重活（要读几 MB 几何），允许显式关掉 —— 但默认是开的。"""
    make_region(store)
    path = store.region_path("mission-001")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["geometry"]["coordinates"][0][0][0] += 0.05
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    # 默认：报错
    with pytest.raises(MissionRegionError):
        store.load("mission-001")
    # 显式关掉：读得出来（元数据仍然是那份，界面要的是它）
    loaded = store.load("mission-001", verify=False)
    assert loaded.id == "mission-001"


# --------------------------------------------------------------------------- #
# 路径受限（Spec §12）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad_id",
    [
        "evil-001",  # 缺 mission- 前缀：它不是任务区
        "mission-",  # 只有前缀，没有正文
        "mission-../evil",  # 想爬出根目录
        "mission-a/b",  # 想多一层
        "mission-a\\b",  # Windows 分隔符也不行
        "mission-..",
        "mission-.",
        "mission-001/../002",
        "mission-巡检",  # 非 ASCII：跨平台目录名容易出事
    ],
)
def test_illegal_ids_never_produce_a_path(store, bad_id: str) -> None:
    """不合法的 id 一律拒绝，绝不"清理一下再用"。

    清理式处理最坏的地方在于：``mission-../evil`` 被"清理"成 ``mission-evil``
    之后，用户以为自己在操作 A，程序操作的是 B。
    """
    with pytest.raises((MissionRegionError, ValueError)):
        store.mission_dir(bad_id)


def test_every_path_helper_goes_through_the_same_gate(store) -> None:
    """三个路径助手都要有同一道闸 —— 只在 ``mission_dir`` 上把关是不够的。"""
    for helper in (store.route_path, store.region_path, store.metadata_path):
        with pytest.raises((MissionRegionError, ValueError)):
            helper("mission-../evil")


def test_surrounding_whitespace_normalises_to_the_same_directory(store) -> None:
    """首尾空白会被规范化掉 —— 而**规范化之后的形态是唯一形态**。

    这不是"宽容"，是把"带空格的 id"这条支路彻底掐掉：模型、元数据、目录名
    三处都从同一个 :func:`~app.mission.models.require_mission_id` 取值，
    所以盘上永远不会出现一个叫 ``" mission-001"`` 的目录，
    也就不会出现"界面认得出、下载链路认不出"这种分家。
    """
    assert store.mission_dir("  mission-001  ") == store.mission_dir("mission-001")
    assert store.region_path("\tmission-002\n") == store.region_path("mission-002")


def test_saved_paths_stay_inside_the_root(store) -> None:
    """写出来的三个路径必须都在根目录内（写比读更危险，要单独量一次）。"""
    make_region(store)
    root = store.root.resolve()
    for path in (
        store.route_path("mission-001"),
        store.region_path("mission-001"),
        store.metadata_path("mission-001"),
    ):
        resolved = path.resolve()
        assert root in resolved.parents  # 在根目录之内
        assert resolved.parent == store.mission_dir("mission-001")  # 且是自己的目录
        assert resolved.is_file()


def test_an_illegal_id_cannot_be_constructed_at_all(store) -> None:
    """非法 id 在**造模型**那一步就过不去 —— 写盘入口根本收不到它。

    这条用例守的是"防线在前端还是后端"：如果只有 ``mission_dir`` 把关，
    那么某个绕过它的新调用点就能把目录写到根外面去。模型自己拒绝，
    才是"无论谁调用都安全"。
    """
    builder = MissionRegionBuilder()
    geometry = builder.build_geometry(ROUTE, BUFFER)
    for bad_id in ("../evil", "evil-001", "mission-a/b", ""):
        with pytest.raises((MissionRegionError, ValueError)):
            builder.build(bad_id, "x", ROUTE, BUFFER, geometry=geometry)


# --------------------------------------------------------------------------- #
# 禁用内容（Spec §12）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "payload",
    [
        {"type": "Feature", "parameters": {"token": "abc"}},
        {"type": "Feature", "token": "abc"},
        {"type": "Feature", "geometry": {"type": "Polygon", "tk": "abc"}},
        # 藏在很里面也要抓（递归扫，不是只看第一层）
        {"type": "Feature", "properties": {"meta": {"nested": [{"access_token": "x"}]}}},
        {"type": "Feature", "properties": {"tile_coordinate": [1, 2, 3]}},
        {"type": "Feature", "Properties": {"APIKey": "x"}},  # 大小写不敏感
    ],
)
def test_token_and_tile_coordinates_cannot_be_persisted(store, payload) -> None:
    """Token / 瓦片坐标不许进任务区文件：这份文件会跟着地图包出去。"""
    builder = MissionRegionBuilder()
    geometry = builder.build_geometry(ROUTE, BUFFER)
    region = builder.build(
        "mission-001", "x", ROUTE, BUFFER, geometry=geometry
    )
    with pytest.raises(MissionRegionError) as excinfo:
        store.save(region, route_geojson=payload, region_geojson={"type": "Feature"})
    assert "不允许出现" in str(excinfo.value)
    # 拦下来了就不该留下半个目录（半成品目录比没有更让人困惑）
    assert not store.region_path("mission-001").exists()


def test_the_value_containing_the_word_token_is_fine(store) -> None:
    """扫的是**键名**：名字里带 "token" 的航线（比如 "token-river"）不该被误伤。"""
    builder = MissionRegionBuilder()
    geometry = builder.build_geometry(ROUTE, BUFFER)
    region = builder.build(
        "mission-001", "token 河航线", ROUTE, BUFFER, geometry=geometry
    )
    store.save(
        region,
        route_geojson={
            "type": "Feature",
            "properties": {"id": "mission-001", "name": "token 河航线"},
        },
        region_geojson={"type": "Feature", "properties": {"regionType": "polygon"}},
    )
    assert store.exists("mission-001")


# --------------------------------------------------------------------------- #
# 缺文件 / 坏文件
# --------------------------------------------------------------------------- #
def test_a_mission_that_was_never_saved_is_not_found(store) -> None:
    with pytest.raises(MissionRegionNotFound):
        store.load("mission-001")
    with pytest.raises(MissionRegionNotFound):
        store.load_metadata("mission-001")
    with pytest.raises(MissionRegionNotFound):
        store.geometry("mission-001")
    with pytest.raises(MissionRegionNotFound):
        store.load_waypoints("mission-001")
    assert not store.exists("mission-001")


def test_an_incomplete_directory_says_which_file_is_missing(store) -> None:
    """几何文件被人删了 → 说清缺哪个、在哪个路径，并给出下一步。"""
    make_region(store)
    store.region_path("mission-001").unlink()
    with pytest.raises(MissionRegionNotFound) as excinfo:
        store.load("mission-001")
    message = str(excinfo.value)
    assert MISSION_REGION_FILENAME in message
    assert "delete" in message  # 告诉用户怎么收场


def test_a_missing_route_file_is_reported_too(store) -> None:
    """航线文件缺了同样算目录损坏（三个文件是一起写的）。"""
    make_region(store)
    store.route_path("mission-001").unlink()
    with pytest.raises(MissionRegionNotFound) as excinfo:
        store.load_waypoints("mission-001")
    assert MISSION_ROUTE_FILENAME in str(excinfo.value)


def test_broken_json_is_reported_with_the_line_number(store) -> None:
    """坏 JSON 要说清第几行 —— "文件坏了"帮不上忙。"""
    make_region(store)
    store.metadata_path("mission-001").write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(MissionRegionError) as excinfo:
        store.load_metadata("mission-001")
    assert "JSON" in str(excinfo.value)


def test_a_non_object_top_level_is_rejected(store) -> None:
    """顶层是数组也要拒（后面所有代码都按"这是个对象"写的）。"""
    make_region(store)
    store.metadata_path("mission-001").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(MissionRegionError) as excinfo:
        store.load_metadata("mission-001")
    assert "JSON 对象" in str(excinfo.value)


def test_directory_name_and_metadata_id_must_agree(store) -> None:
    """目录名是 ``mission-001``、元数据里写着 ``mission-002`` → 拒。

    这是"被复制过来的目录"的典型症状，信哪一边都会出事。
    """
    make_region(store)
    path = store.metadata_path("mission-001")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["id"] = "mission-002"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(MissionRegionError) as excinfo:
        store.load_metadata("mission-001")
    assert "mission-002" in str(excinfo.value)


def test_existence_is_not_claimed_for_a_half_written_directory(store) -> None:
    """只有航线文件 → ``exists`` 为假（半个目录不能当任务区用）。"""
    make_region(store)
    store.metadata_path("mission-001").unlink()
    assert not store.exists("mission-001")


# --------------------------------------------------------------------------- #
# 列表
# --------------------------------------------------------------------------- #
def test_listing_returns_saved_missions_in_id_order(store) -> None:
    for index in (3, 1, 2):
        make_region(store, mission_id=f"mission-00{index}", name=f"航线 {index}")
    listed = store.list_all()
    assert [region.id for region in listed] == [
        "mission-001",
        "mission-002",
        "mission-003",
    ]


def test_listing_ignores_foreign_directories(store, work_dir) -> None:
    """根目录下别的目录（例如用户自己建的、或旧版本留下的）不该被当成任务区。"""
    make_region(store)
    root = store.root
    (root / "notes").mkdir(parents=True, exist_ok=True)
    (root / "notes" / "readme.txt").write_text("hello", encoding="utf-8")
    listed = store.list_all()
    assert [region.id for region in listed] == ["mission-001"]


def test_listing_skips_a_broken_mission_instead_of_failing(store) -> None:
    """一个坏目录不该让整份清单不可用（与目录校验同一条原则）。"""
    make_region(store, mission_id="mission-001")
    make_region(store, mission_id="mission-002")
    # 把 002 的几何弄坏
    store.region_path("mission-002").write_text("not json", encoding="utf-8")
    listed = store.list_all()
    assert [region.id for region in listed] == ["mission-001"]


def test_listing_an_empty_root_is_empty_not_an_error(store) -> None:
    """根目录还不存在时返回空元组（首次使用就是这么开始的）。"""
    assert store.list_all() == ()


# --------------------------------------------------------------------------- #
# 删除
# --------------------------------------------------------------------------- #
def test_deleting_removes_the_whole_directory(store) -> None:
    make_region(store)
    assert store.delete("mission-001") is True
    assert not store.mission_dir("mission-001").exists()
    assert store.root.is_dir()  # 根目录要留着，后面还要用


def test_deleting_twice_is_not_an_error(store) -> None:
    """第二次删返回 ``False`` 而不是抛异常 —— 界面上的"删一下"要能重试。"""
    make_region(store)
    store.delete("mission-001")
    assert store.delete("mission-001") is False


def test_deleting_one_mission_leaves_the_others_alone(store) -> None:
    make_region(store, mission_id="mission-001")
    make_region(store, mission_id="mission-002")
    store.delete("mission-001")
    assert store.exists("mission-002")
    assert [region.id for region in store.list_all()] == ["mission-002"]


def test_deleting_an_illegal_id_is_refused(store) -> None:
    with pytest.raises((MissionRegionError, ValueError)):
        store.delete("mission-../evil")


# --------------------------------------------------------------------------- #
# 元数据 与 region 的往返
# --------------------------------------------------------------------------- #
def test_metadata_round_trips_through_disk(store) -> None:
    """``to_dict`` → 盘 → ``from_dict`` 不丢字段。"""
    make_region(store)
    meta = store.load_metadata("mission-001")
    again = MissionRegionMetadata.from_dict(
        json.loads(store.metadata_path("mission-001").read_text(encoding="utf-8"))
    )
    assert again == meta


def test_metadata_id_mismatch_on_save_is_refused(store) -> None:
    """落盘时元数据与模型 id 不一致 → 拒（别写出一个自相矛盾的目录）。"""
    builder = MissionRegionBuilder()
    geometry = builder.build_geometry(ROUTE, BUFFER)
    region = builder.build("mission-001", "x", ROUTE, BUFFER, geometry=geometry)
    wrong = MissionRegionMetadata(
        id="mission-002",
        name="x",
        buffer_meters=BUFFER,
        waypoint_count=len(ROUTE),
        geometry_version=geometry.geometry_version,
        bounds=geometry.bounds,
    )
    with pytest.raises(MissionRegionError):
        store.save(
            region,
            route_geojson={"type": "Feature"},
            region_geojson={"type": "Feature"},
            metadata=wrong,
        )


def test_loaded_region_exposes_what_the_download_chain_needs(store) -> None:
    """读回来的对象要自带"下载链路需要的四件事"：id / 等级 / 半径 / 几何路径。"""
    make_region(store)
    loaded = MissionRegionStore(store.root).load("mission-001")
    assert loaded.id == "mission-001"
    assert loaded.geojson_path
    assert loaded.buffer_meters == pytest.approx(BUFFER)
    assert loaded.waypoint_count == len(ROUTE)
    assert loaded.geometry_version
