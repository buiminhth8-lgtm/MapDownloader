"""任务区与下载链路的接线（Slice 22，Spec §16 ~ §23）。

前三个文件各自守住了任务区**内部**的一件事：把航线读进来（route_loader）、
按米做缓冲（region_builder）、把几何可靠地存住（store）。这个文件守的是
**它与既有下载系统接得对不对** —— 而这是本 Slice 唯一真正有风险的地方。

风险在于：任务区不是"第二套下载系统"，它必须是"多边形区域的又一个来源"。
行政区多边形在 Slice 16 已经跑通了整条链路（几何 → 逐张判相交 → 预估 →
计划 → 调度 → 续传 → Inspector → 包）。任务区若在这条链路上多出一个分叉，
就会出现"行政区包好使、任务区包报错"这种最费时间的问题。

所以要量的是这些**跨层**的东西：

=======================================  ====================================
量什么                                   为什么
=======================================  ====================================
等级 = MISSION，Zoom 来自策略表          任务区层里不许有 Zoom 数字（Spec §16）
预估口径是 selected 而不是候选矩形       走廊外的瓦片不能进分母（Spec §9 / §17）
能变成标准 ``DownloadRegion``            Slice 16 的链路一行都不用改（Spec §14）
能加进 Slice 20 的计划、排在市级之后    顺序给 Slice 21 的串行执行用（§20 / §23）
调度器不认识"任务区"这个概念             有 Mission 专用分支就说明接歪了（§8）
续传走同一份盘上几何，不重算             重算 = 换一个瓦片集 = 假缺失（Spec §21）
Inspector 找得到几何就算、找不到就认错   退回矩形算会误报"缺 60%"（Spec §20）
被计划引用时不许删（Spec §22）           删了会留下一个打不开的计划条目
=======================================  ====================================

最后一条是本文件里唯一"用真调度器跑一遍"的地方 —— 其余跨层断言都用
真实对象 + 假执行者，因为这里要证的从来不是"下得下来"，而是"接得上"。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from app.config.settings import (
    DEFAULT_MISSION_BUFFER_METERS,
    UI_TEXT_MISSION_DELETE_REFERENCED,
)
from app.geo import (
    GeoBounds,
    RegionType,
    build_region_filter,
    calculate_tile_range,
    estimate_region_tiles,
    region_from_task,
)
from app.geo import region as region_module
from app.mbtiles import MBTilesMetadata, MBTilesWriter
from app.planning.download_plan import PLAN_LEVEL_ORDER, DownloadPlan
from app.planning.plan_execution import PlanExecutionStatus, PlanItemRunStatus
from app.planning.plan_execution_store import PlanExecutionStore
from app.planning.plan_scheduler import PlanScheduler
from app.planning.plan_service import PlanService
from app.planning.plan_store import PlanStore
from app.planning.region_level import RegionLevel
from app.planning.zoom_policy import DEFAULT_ZOOM_POLICIES
from app.planning.zoom_strategy import ZoomStrategyService
from app.providers import PNG_SIGNATURE
from app.regions.service import RegionService
from app.services import MBTilesInspectionService
from app.tasks.task_service import TaskService
from app.tasks.task_store import TaskStore
from tests.plan_fakes import FakeRunner

from app.mission import (
    CompositeRegionService,
    MissionRegionNotFound,
    MissionRegionReferenced,
    MissionRegionService,
    MissionRegionStore,
    mission_reference_files,
)
from app.mission.models import MISSION_REGION_LEVEL

#: 一条作业航线（杭州西湖北侧），带三个点就够画折线。
ROUTE = [(120.00, 30.00), (120.05, 30.02), (120.10, 30.01)]
BUFFER = 3000.0

#: 覆盖度/瓦片集比对用的单一 Zoom（Z15 是走廊推荐区间的下界）。
ZOOM = 15

#: 缺失数量的断言必须针对**数字**。中文说明里"误报成缺失"也含"缺失"两个字，
#: 用子串去否定会把正确的提示判成违规；真正要禁的是"按外包矩形算出一个具体
#: 数量"这种行为。与 ``test_polygon_inspection`` 同一个正则。
_MISSING_CLAIM = re.compile(r"缺失\s*[0-9,]+\s*张")


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture
def strategy() -> ZoomStrategyService:
    return ZoomStrategyService()


@pytest.fixture
def mission_service(
    work_dir: Path,
    strategy: ZoomStrategyService,
    plans_file: Path,
    tasks_file: Path,
) -> MissionRegionService:
    """挂在临时目录上的任务区服务（引用检查看临时 plans/tasks 文件）。"""
    return MissionRegionService(
        MissionRegionStore(work_dir / "mission_regions"),
        strategy=strategy,
        reference_files=(
            ("下载任务记录", tasks_file),
            ("下载计划", plans_file),
        ),
    )


@pytest.fixture
def region_service(
    region_resources: Path,
    mission_service: MissionRegionService,
    strategy: ZoomStrategyService,
) -> CompositeRegionService:
    """行政区 + 任务区的合并入口（界面用的就是这一个对象）。"""
    return CompositeRegionService(RegionService(strategy=strategy), mission_service)


@pytest.fixture
def plan_service(
    region_service: CompositeRegionService,
    strategy: ZoomStrategyService,
    plans_file: Path,
    tasks_file: Path,
) -> PlanService:
    return PlanService(
        store=PlanStore(plans_file),
        service=region_service,
        strategy=strategy,
        task_service=TaskService(TaskStore(tasks_file)),
    )


@pytest.fixture
def task_service(tasks_file: Path) -> TaskService:
    return TaskService(TaskStore(tasks_file))


@pytest.fixture
def make_mission(mission_service: MissionRegionService):
    """造一个真任务区（默认 mission-001）。"""

    def _make(
        name: str = "西湖巡检航线",
        *,
        waypoints=ROUTE,
        buffer_meters: float = BUFFER,
        mission_id: str | None = None,
    ):
        return mission_service.create(
            name, waypoints=waypoints, buffer_meters=buffer_meters, mission_id=mission_id
        )

    return _make


def _real_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (16, 16), (0, 0, 0, 0)).save(buffer, format="PNG")
    data = buffer.getvalue()
    assert data.startswith(PNG_SIGNATURE)
    return data


FAKE_PNG = _real_png()


def _candidate_tiles(bounds: GeoBounds, zoom: int) -> set[tuple[int, int]]:
    """候选矩形里的全部瓦片（XYZ）。"""
    tile_range = calculate_tile_range(bounds, zoom)
    return {
        (x, y)
        for y in range(tile_range.min_y, tile_range.max_y + 1)
        for x in range(tile_range.min_x, tile_range.max_x + 1)
    }


def _region_tiles(region, zoom: int) -> set[tuple[int, int]]:
    """真正与区域几何相交的瓦片（XYZ）——**按生产同一套过滤器算**。"""
    region_filter = build_region_filter(region)
    assert region_filter is not None
    return {
        (x, y)
        for x, y in _candidate_tiles(region.bounds, zoom)
        if region_filter.intersects_tile_xyz(zoom, x, y)
    }


def build_package(
    path: Path,
    region,
    tiles: set[tuple[int, int]],
    *,
    zoom: int = ZOOM,
    region_id: str | None = None,
) -> None:
    """建一个"任务区地图包"（瓦片是真能解码的 PNG）。

    metadata 里的区域字段与生产代码写出来的保持一致
    （``region_type`` / ``region_id`` / ``boundary_version`` /
    ``region_level`` / ``buffer_meters``）—— Inspector 的判据多严，
    夹具就得有多真。
    """
    metadata = MBTilesMetadata.for_bounds(
        region.bounds,
        zoom,
        zoom,
        name="任务区包",
        region_type=RegionType.POLYGON.value,
        region_id=region_id if region_id is not None else region.region_id,
        boundary_version=region.boundary_version or "",
        # Slice 25 起生产代码**所有区域都写等级**（矩形 custom / 行政区 country…
        # / 任务区 mission），夹具跟着一起写 —— 判据多严，夹具就得多真。
        region_level=region.region_level.value,
        buffer_meters=(region.buffer_meters if region.is_mission else ""),
    )
    with MBTilesWriter(path) as writer:
        writer.write_metadata(metadata)
        for x, y in sorted(tiles):
            writer.write_tile(zoom, x, y, FAKE_PNG)


# --------------------------------------------------------------------------- #
# 等级与 Zoom（Spec §4 / §16）
# --------------------------------------------------------------------------- #
def test_the_mission_level_is_a_real_hierarchy_level() -> None:
    """``MISSION`` 是规划等级里的一员，取值 ``mission``（会进 MBTiles 与 manifest）。"""
    assert MISSION_REGION_LEVEL is RegionLevel.MISSION
    assert MISSION_REGION_LEVEL.value == "mission"


def test_the_hierarchy_order_puts_mission_after_city_and_before_custom() -> None:
    """顺序是**契约**（Spec §20）：全国 → 省 → 市 → 任务区 → 自定义。

    任务区排在市级之后，因为它是"在已经选好的底图上补高精度走廊"；
    自定义排在最后，因为它最不可预期。
    """
    assert PLAN_LEVEL_ORDER == (
        RegionLevel.COUNTRY,
        RegionLevel.PROVINCE,
        RegionLevel.CITY,
        RegionLevel.MISSION,
        RegionLevel.CUSTOM,
    )


def test_the_recommended_zoom_is_z15_to_z18(
    mission_service: MissionRegionService, make_mission
) -> None:
    """推荐区间来自策略表的 Z15~18（Spec §16 的原文）。"""
    make_mission()
    recommendation = mission_service.recommendation("mission-001")
    assert (recommendation.min_zoom, recommendation.max_zoom) == (15, 18)


def test_the_recommendation_follows_the_policy_table_not_a_hardcoded_number(
    work_dir: Path, make_mission
) -> None:
    """**改策略表，推荐跟着变** —— 这才是"没有写死 Zoom"的证明。

    只断言 ``== (15, 18)`` 是抓不住写死的：写死的实现同样返回 15/18。
    把 MISSION 的策略换成 Z16~17，一个"从策略表读"的实现必然跟着变。
    """
    custom = dict(DEFAULT_ZOOM_POLICIES)
    custom[RegionLevel.MISSION] = replace(
        custom[RegionLevel.MISSION],
        recommended_min_zoom=16,
        recommended_max_zoom=17,
        warning_max_zoom=17,
    )
    service = MissionRegionService(
        MissionRegionStore(work_dir / "mission_regions"),
        strategy=ZoomStrategyService(custom),
    )
    service.create("西湖巡检航线", waypoints=ROUTE, buffer_meters=BUFFER)

    recommendation = service.recommendation("mission-001")

    assert (recommendation.min_zoom, recommendation.max_zoom) == (16, 17)


def test_the_estimate_defaults_to_the_recommended_range_and_says_so(
    mission_service: MissionRegionService, make_mission
) -> None:
    """不传 Zoom 时按推荐区间预估，并如实记下"用的是推荐值"。"""
    make_mission()
    result = mission_service.estimate("mission-001")
    assert result.used_recommended_zoom is True
    assert (result.estimated_min_zoom, result.estimated_max_zoom) == (15, 18)
    assert [item.zoom for item in result.estimate.per_zoom] == [15, 16, 17, 18]


def test_a_custom_zoom_only_warns_and_is_never_rewritten(
    mission_service: MissionRegionService, make_mission
) -> None:
    """用户自己填的 Zoom 只提示、**绝不改**（与 Slice 20 同一条规矩）。"""
    make_mission()
    recommendation = mission_service.evaluate("mission-001", 12, 20)
    assert recommendation.warning is True
    # 原样返回用户给的两个数，没有被 clamp
    assert (recommendation.requested_min_zoom, recommendation.requested_max_zoom) == (
        12,
        20,
    )
    inside = mission_service.evaluate("mission-001", 15, 18)
    assert inside.warning is False


# --------------------------------------------------------------------------- #
# 预估口径：selected，不是候选矩形（Spec §9 / §17）
# --------------------------------------------------------------------------- #
def test_the_estimate_counts_tiles_inside_the_corridor_not_the_box(
    mission_service: MissionRegionService, make_mission
) -> None:
    """**本 Slice 的效率主张**：47000 平方公里 vs 一条 6 km 宽的走廊。

    走廊是斜的、两头是圆的，所以外包矩形里必然有一批瓦片与它不相交。
    若预估用的是候选矩形，那批瓦片会被算进"要下载的张数" ——
    任务区就退化成了"给整块矩形下 Z15~18"，正是本 Slice 要避免的事。
    """
    make_mission()
    result = mission_service.estimate("mission-001", min_zoom=ZOOM, max_zoom=ZOOM)
    stats = result.estimate.per_zoom[0]

    assert stats.zoom == ZOOM
    assert stats.selected_tiles < stats.candidate_tiles
    assert stats.filtered_tiles == stats.candidate_tiles - stats.selected_tiles
    assert stats.selected_tiles > 0
    # 候选矩形就是任务区自己的外包范围（先用它缩小搜索面）
    assert stats.candidate_tiles == calculate_tile_range(
        mission_service.get("mission-001").bounds, ZOOM
    ).tile_count


def test_the_estimate_matches_the_geometric_filter_exactly(
    mission_service: MissionRegionService, make_mission
) -> None:
    """预估的数字必须与"拿几何逐张判"的结果**完全一致**（不能差一张）。

    这是防"两套真值"：预估一套算法、下载另一套过滤器，两边差几张，
    用户在界面上看到的数字就永远对不上进度。
    """
    make_mission()
    region = mission_service.to_download_region("mission-001")
    expected = len(_region_tiles(region, ZOOM))

    stats = mission_service.estimate(
        "mission-001", min_zoom=ZOOM, max_zoom=ZOOM
    ).estimate.per_zoom[0]

    assert stats.selected_tiles == expected


# --------------------------------------------------------------------------- #
# 任务区 → DownloadRegion（Spec §14）
# --------------------------------------------------------------------------- #
def test_the_download_region_is_an_ordinary_polygon_region(
    mission_service: MissionRegionService, make_mission
) -> None:
    """产物与行政区多边形**完全同构** —— 这是"复用 Slice 16"的全部秘密。"""
    created = make_mission()
    region = mission_service.to_download_region("mission-001")

    assert region.region_type is RegionType.POLYGON
    assert region.region_id == "mission-001"
    assert region.region_level is MISSION_REGION_LEVEL
    assert region.buffer_meters == pytest.approx(BUFFER)
    assert Path(region.geojson_path).is_absolute()
    assert Path(region.geojson_path).is_file()
    assert Path(region.geojson_path) == Path(created.geojson_path)
    # 候选矩形 = 盘上那份几何的实际范围（不是另算一份）
    for key in ("west", "south", "east", "north"):
        assert getattr(region.bounds, key) == pytest.approx(
            getattr(created.bounds, key), abs=1e-9
        )


def test_the_slice16_filter_works_on_a_mission_region_unchanged(
    mission_service: MissionRegionService, make_mission
) -> None:
    """Slice 16 的 ``build_region_filter`` 拿来就能用（一行都没改）。"""
    make_mission()
    region = mission_service.to_download_region("mission-001")

    region_filter = build_region_filter(region)

    assert region_filter is not None
    assert region_filter.geometry.geom_type in {"Polygon", "MultiPolygon"}
    inside = sorted(_region_tiles(region, ZOOM))[0]
    assert region_filter.intersects_tile_xyz(ZOOM, *inside) is True


def test_the_metadata_rows_carry_what_the_inspector_needs(
    mission_service: MissionRegionService, make_mission
) -> None:
    """MBTiles metadata 要写明"这是任务区、半径多少"（Spec §20）。

    任务区的等级**反查不出来**（几何不在资源目录里），所以必须落在包里；
    行政区包多写一个等级反而是噪声，那边依然不写。
    """
    make_mission()
    rows = mission_service.to_download_region("mission-001").metadata_rows()

    assert rows["region_type"] == "polygon"
    assert rows["region_id"] == "mission-001"
    assert rows["region_level"] == "mission"
    assert rows["buffer_meters"] == "3000"
    assert rows["boundary_version"]


def test_the_manifest_region_declares_level_and_corridor_width(
    mission_service: MissionRegionService, make_mission
) -> None:
    """manifest 的 ``region`` 子对象：``type`` 仍是几何类型，任务是 ``level``。

    ``type`` 在 Slice 16 就定成"矩形还是多边形"，读它的地方（含 Android）
    对认不出的取值一律按矩形兜底 —— 把 ``mission`` 塞进去等于让老读取方
    把一个多边形包当矩形看。
    """
    make_mission()
    payload = mission_service.to_download_region("mission-001").manifest_region()

    assert payload["type"] == "polygon"
    assert payload["id"] == "mission-001"
    assert payload["level"] == "mission"
    assert payload["bufferMeters"] == pytest.approx(BUFFER)
    assert payload["geometryVersion"] == payload["boundaryVersion"]


def test_an_admin_polygon_carries_its_level_but_not_the_mission_keys(
    region_service: CompositeRegionService,
) -> None:
    """行政区包要写等级、不写任务区字段（Slice 25 修正）。

    ``region_level`` 从 Slice 25 起**所有区域都写**：Android 没有区域目录，
    反查不出等级，不写的话行政区包在那边一律落到 ``UNKNOWN``，
    Slice 24 的等级优先级整条失效。它因此不再算"任务区字段"。

    真正的任务区字段（``buffer_meters`` / ``bufferMeters`` / ``geometryVersion``）
    照旧只有任务区写 —— 那些东西换个区域类型就没有意义。
    """
    region = region_service.get_download_region("demoland")
    assert region.is_mission is False

    rows = region.metadata_rows()
    assert rows["region_level"] == "country"
    assert "buffer_meters" not in rows

    payload = region.manifest_region()
    assert payload["level"] == "country"
    assert "bufferMeters" not in payload
    assert "geometryVersion" not in payload


def test_a_plain_rectangle_declares_itself_custom() -> None:
    """纯矩形（没有区域目录条目）写 ``custom``，而不是留空。

    留空在 Android 侧与"老包没有这条信息"收敛到同一个 ``UNKNOWN``，
    而这两件事不一样：一个明确是"用户画的一块矩形"，一个是"不知道"。
    """
    region = region_module.DownloadRegion.rectangle(
        "自定义矩形", GeoBounds(west=0.0, south=0.0, east=1.0, north=1.0)
    )
    assert region.is_mission is False
    assert region.metadata_rows()["region_level"] == "custom"
    assert region.manifest_region()["level"] == "custom"


def test_a_missing_mission_is_reported_not_silently_substituted(
    mission_service: MissionRegionService,
) -> None:
    """引用一个不存在的任务区 → 报"不存在"，而不是退回矩形或猜一块地。"""
    with pytest.raises(MissionRegionNotFound):
        mission_service.to_download_region("mission-404")


# --------------------------------------------------------------------------- #
# 加进计划（Spec §20 / §23）
# --------------------------------------------------------------------------- #
def _planned(
    plan_service: PlanService, work_dir: Path, *region_ids: str
) -> DownloadPlan:
    for region_id in region_ids:
        plan_service.add_region(region_id)
    plan_service.apply_settings(name="离线计划", output_dir=work_dir / "output")
    plan_service.estimate()
    return plan_service.plan


def test_a_mission_can_join_the_same_plan_as_admin_regions(
    plan_service: PlanService, mission_service: MissionRegionService, make_mission, work_dir: Path
) -> None:
    """全国 + 省 + 市 + 任务区，四条并存在**同一份**计划里（Spec §20）。"""
    make_mission()
    plan = _planned(
        plan_service, work_dir, "demoland", "northland", "rivertown", "mission-001"
    )

    assert {item.region_id for item in plan.items} == {
        "demoland",
        "northland",
        "rivertown",
        "mission-001",
    }
    mission_item = plan.item("mission-001")
    assert mission_item is not None
    assert mission_item.region_level is MISSION_REGION_LEVEL
    assert mission_item.region_name == "西湖巡检航线"
    assert (mission_item.min_zoom, mission_item.max_zoom) == (15, 18)


def test_the_mission_item_is_ordered_after_the_city(
    plan_service: PlanService, make_mission, work_dir: Path
) -> None:
    """顺序：全国 → 省 → 市 → 任务区（与用户点选顺序无关）。"""
    make_mission()
    plan = _planned(
        plan_service, work_dir, "mission-001", "rivertown", "demoland", "northland"
    )

    assert [item.region_id for item in plan.ordered_items()] == [
        "demoland",
        "northland",
        "rivertown",
        "mission-001",
    ]


def test_the_plan_total_uses_the_corridor_count_not_the_box(
    plan_service: PlanService, make_mission, work_dir: Path
) -> None:
    """计划里的任务区贡献的是**走廊**张数（Spec §9 的分母口径）。

    如果这里用的是候选矩形，那么"计划总量"会悄悄多出一大截，
    而那部分瓦片下载器根本不会去下 —— 于是进度永远到不了 100%。
    """
    make_mission()
    plan = _planned(plan_service, work_dir, "mission-001")
    item = plan.item("mission-001")

    assert item.selected_tiles < item.candidate_tiles
    assert item.filtered_tiles > 0
    assert plan.total_tiles == item.selected_tiles


def test_a_mission_item_generates_one_ordinary_download_task(
    plan_service: PlanService, task_service: TaskService, make_mission, work_dir: Path
) -> None:
    """一对一：产物就是既有的 ``DownloadTask``，形状与单区域下载一样。"""
    make_mission()
    plan = _planned(plan_service, work_dir, "mission-001")
    item = plan.item("mission-001")

    tasks = plan_service.create_download_tasks()

    assert len(tasks) == 1
    task = tasks[0]
    assert task.region_id == "mission-001"
    assert task.total_tiles == item.selected_tiles
    assert (task.min_zoom, task.max_zoom) == (15, 18)
    assert task.output_path.name == "mission-001_z15_18.mbtiles"
    assert [row.id for row in task_service.list_tasks()] == [task.id]


def test_the_multi_region_plan_makes_one_task_each(
    plan_service: PlanService, make_mission, work_dir: Path
) -> None:
    """四块地 → 四个任务（不合并、不互相顶替）。"""
    make_mission()
    plan = _planned(
        plan_service, work_dir, "demoland", "northland", "rivertown", "mission-001"
    )

    tasks = plan_service.create_download_tasks()

    assert [task.region_id for task in tasks] == [
        "demoland",
        "northland",
        "rivertown",
        "mission-001",
    ]
    assert all(task.total_tiles > 0 for task in tasks)
    assert len({task.output_path for task in tasks}) == 4
    assert plan.count == 4


# --------------------------------------------------------------------------- #
# 调度器（Spec §8）：不认识"任务区"
# --------------------------------------------------------------------------- #
async def test_the_scheduler_runs_a_mission_task_like_any_other(
    plan_service: PlanService, task_service: TaskService, make_mission, work_dir: Path
) -> None:
    """串行执行时任务区排在市级之后 —— 调度器里**没有一行** Mission 专用逻辑。

    调度器只认 ``execution.items`` 与等级顺序，因此这条断言同时是
    "顺序对"与"没有分叉"两件事的证据。
    """
    make_mission()
    plan = _planned(
        plan_service, work_dir, "rivertown", "demoland", "northland", "mission-001"
    )
    runner = FakeRunner()
    scheduler = PlanScheduler(
        plan_service,
        runner,
        task_service=task_service,
        store=PlanExecutionStore(work_dir / "plan_executions.json"),
        save_interval_seconds=0.0,
        save_tile_interval=1,
    )

    execution = await scheduler.start(plan)

    assert runner.calls == ["demoland", "northland", "rivertown", "mission-001"]
    assert execution.status is PlanExecutionStatus.COMPLETED
    assert execution.completed_count == 4
    mission_entry = next(
        entry for entry in execution.items if entry.plan_item_id == plan.item("mission-001").id
    )
    assert mission_entry.status is PlanItemRunStatus.COMPLETED


# --------------------------------------------------------------------------- #
# 续传（Spec §21）
# --------------------------------------------------------------------------- #
class _TaskRecord:
    """任务记录里续传真正要读的那几个字段（走 ``region_from_task`` 的鸭子类型）。"""

    def __init__(self, **fields) -> None:
        self.name = fields.get("name", "任务区任务")
        for key in ("west", "south", "east", "north"):
            setattr(self, key, fields[key])
        self.region_type = fields.get("region_type", "polygon")
        self.region_id = fields.get("region_id", "mission-001")
        self.boundary_version = fields.get("boundary_version", "")


def _task_record_from(region) -> _TaskRecord:
    return _TaskRecord(
        west=region.bounds.west,
        south=region.bounds.south,
        east=region.bounds.east,
        north=region.bounds.north,
        region_type=region.region_type.value,
        region_id=region.region_id,
        boundary_version=region.boundary_version or "",
    )


def test_resume_finds_the_same_geometry_file(
    mission_service: MissionRegionService, make_mission
) -> None:
    """续传从任务记录出发，拿回的必须是**同一份盘上几何**（Spec §21）。

    重算几何（哪怕航线与半径都在）会产生一个"几乎一样"的瓦片集 ——
    差几行几列不会让任何断言失败，但会让 Inspector 报出一堆假缺失。
    所以判据是"路径与版本号都原样"，不是"范围差不多"。
    """
    mission_service.install()
    created = make_mission()
    fresh = mission_service.to_download_region("mission-001")

    resumed = region_from_task(_task_record_from(fresh))

    assert resumed is not None
    assert resumed.geojson_path == fresh.geojson_path
    assert Path(resumed.geojson_path) == Path(created.geojson_path)
    assert resumed.boundary_version == fresh.boundary_version
    assert resumed.region_id == "mission-001"
    assert resumed.region_level is MISSION_REGION_LEVEL
    assert resumed.buffer_meters == pytest.approx(BUFFER)


def test_resume_yields_the_identical_tile_set(
    mission_service: MissionRegionService, make_mission
) -> None:
    """瓦片集逐张相同 —— 这是"续传不会反复重下同一张"的依据。"""
    mission_service.install()
    make_mission()
    fresh = mission_service.to_download_region("mission-001")

    resumed = region_from_task(_task_record_from(fresh))
    assert resumed is not None

    assert _region_tiles(resumed, ZOOM) == _region_tiles(fresh, ZOOM)
    assert (
        estimate_region_tiles(resumed, ZOOM, ZOOM).selected_tiles
        == estimate_region_tiles(fresh, ZOOM, ZOOM).selected_tiles
    )


def test_resume_uses_the_bounds_recorded_in_the_task(
    mission_service: MissionRegionService, make_mission
) -> None:
    """续传要用**任务记录里当初那两个数**，而不是今天的几何范围。

    这条守的是 Slice 19 踩过的那个坑：两端各拿一份不同精度的同一个数 =
    两套真值。记录里写的是什么，续传就得用什么。
    """
    mission_service.install()
    make_mission()
    fresh = mission_service.to_download_region("mission-001")
    record = _task_record_from(fresh)
    # 故意把记录里的东边界挪一点点（模拟"记录与几何在小数位上不完全一致"）
    record.east = fresh.bounds.east - 1e-7

    resumed = region_from_task(record)

    assert resumed is not None
    assert resumed.bounds.east == pytest.approx(fresh.bounds.east - 1e-7)
    assert resumed.geojson_path == fresh.geojson_path  # 几何仍然来自同一个文件


def test_resume_never_rewrites_the_geometry_on_disk(
    mission_service: MissionRegionService, make_mission
) -> None:
    """续传是**只读**的：几何文件的内容一个字节都不该变。

    如果续传顺手把几何重算一遍再写回去，那么"这批瓦片按哪一版几何裁的"
    就再也说不清了（Spec §21 明令禁止）。
    """
    mission_service.install()
    make_mission()
    path = Path(mission_service.to_download_region("mission-001").geojson_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    fresh = mission_service.to_download_region("mission-001")
    for _ in range(3):
        assert region_from_task(_task_record_from(fresh)) is not None

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_resume_is_honest_when_the_mission_is_gone(
    region_service: CompositeRegionService, mission_service: MissionRegionService, make_mission
) -> None:
    """几何不在本机（另一个目录里建的包）→ 解析不出来，返回 ``None``。

    返回 ``None`` 而不是"猜一个矩形"，下游才好把它标成"边界不可用"
    （Spec §20：误报比不知道糟得多）。
    """
    mission_service.install()
    make_mission()
    record = _task_record_from(mission_service.to_download_region("mission-001"))
    mission_service.delete("mission-001")

    assert region_from_task(record) is None


# --------------------------------------------------------------------------- #
# Inspector 覆盖度（Spec §20）
# --------------------------------------------------------------------------- #
def test_a_complete_mission_package_reports_no_missing(
    mission_service: MissionRegionService, make_mission, work_dir: Path
) -> None:
    """矩形候选写满 → 走廊内的瓦片一张不缺 → 缺失 0、结论 PASS。"""
    mission_service.install()
    make_mission()
    region = mission_service.to_download_region("mission-001")
    path = work_dir / "mission_complete.mbtiles"
    build_package(path, region, _candidate_tiles(region.bounds, ZOOM))

    result = MBTilesInspectionService().inspect(path)

    assert result.coverage_checked is True
    assert result.expected_tiles == len(_region_tiles(region, ZOOM))
    assert result.actual_tiles_in_coverage == result.expected_tiles
    assert result.missing_tiles == 0
    assert result.status == "PASS"


def test_a_mission_package_missing_one_tile_reports_exactly_one(
    mission_service: MissionRegionService, make_mission, work_dir: Path
) -> None:
    """少一张就报一张 —— 且报的是**走廊内**那一张（不是矩形的边角）。"""
    mission_service.install()
    make_mission()
    region = mission_service.to_download_region("mission-001")
    inside = sorted(_region_tiles(region, ZOOM))
    assert len(inside) > 3
    omitted = inside[0]
    path = work_dir / "mission_missing_one.mbtiles"
    build_package(path, region, _candidate_tiles(region.bounds, ZOOM) - {omitted})

    result = MBTilesInspectionService().inspect(path)

    assert result.expected_tiles == len(inside)
    assert result.actual_tiles_in_coverage == len(inside) - 1
    assert result.missing_tiles == 1
    assert result.missing_details == [f"{ZOOM}/{omitted[0]}/{omitted[1]}"]
    assert any(_MISSING_CLAIM.search(warning) for warning in result.warnings)


def test_an_unknown_mission_marks_coverage_unknown_instead_of_guessing(
    mission_service: MissionRegionService, work_dir: Path
) -> None:
    """**找得到几何就算、找不到就认错**（Spec §20 的硬要求）。

    这个包是在别的机器上生成的（``mission-001`` 本机没有）。若退回外包矩形
    去算期望值，就会得出"缺了几十张"的结论 —— 而那个包很可能是**完整**的。
    用户会照着这个数字去重下，花掉配额与时间，最后发现还是"缺"。

    所以正确处置是：覆盖度标成未知，并且**一个具体数字都不给**。
    """
    mission_service.install()
    # 包里声明的 ``mission-999`` 在本机不存在 → 解析不出几何
    path = work_dir / "mission_unknown.mbtiles"
    _build_package_without_local_geometry(path, mission_id="mission-999")

    result = MBTilesInspectionService().inspect(path)

    assert result.coverage_checked is False
    # 没有数字 —— 这正是"不猜"的证据
    assert result.missing_tiles is None
    assert result.expected_tiles is None
    assert result.missing_details == []
    assert any("未知" in warning for warning in result.warnings)
    assert any("不按外包矩形" in warning for warning in result.warnings)
    # 全库范围内不含"缺失 N 张"这种按矩形算出来的结论
    text = "\n".join(result.warnings + result.errors + result.notices)
    assert _MISSING_CLAIM.search(text) is None


def _build_package_without_local_geometry(path: Path, *, mission_id: str) -> None:
    """建一个"本机没有对应任务区"的包（用一块普通范围当几何载体）。"""
    bounds = GeoBounds(west=120.0, south=30.0, east=120.1, north=30.1)
    metadata = MBTilesMetadata.for_bounds(
        bounds,
        ZOOM,
        ZOOM,
        name="外部任务区包",
        region_type=RegionType.POLYGON.value,
        region_id=mission_id,
        boundary_version="1:3000.0:abcdef123456",
        region_level="mission",
        buffer_meters=BUFFER,
    )
    with MBTilesWriter(path) as writer:
        writer.write_metadata(metadata)
        for x, y in sorted(_candidate_tiles(bounds, ZOOM)):
            writer.write_tile(ZOOM, x, y, FAKE_PNG)


def test_the_mission_keys_do_not_break_a_generic_validator(
    mission_service: MissionRegionService, make_mission, work_dir: Path
) -> None:
    """``region_level`` / ``buffer_meters`` 是本工具的自有扩展，不该被判错。"""
    mission_service.install()
    make_mission()
    region = mission_service.to_download_region("mission-001")
    path = work_dir / "mission_valid.mbtiles"
    build_package(path, region, _candidate_tiles(region.bounds, ZOOM))

    report = MBTilesInspectionService().inspect(path)

    assert report.valid is True
    assert report.status == "PASS"
    assert not any("无法识别" in warning for warning in report.warnings)


# --------------------------------------------------------------------------- #
# 删除保护（Spec §22）
# --------------------------------------------------------------------------- #
def test_a_mission_referenced_by_the_plan_cannot_be_deleted(
    plan_service: PlanService,
    mission_service: MissionRegionService,
    make_mission,
    work_dir: Path,
) -> None:
    """被计划引用 → 拒绝删除，并说清是谁在引用（Spec §22）。"""
    make_mission()
    _planned(plan_service, work_dir, "mission-001")

    assert mission_service.references("mission-001") == ("下载计划",)
    with pytest.raises(MissionRegionReferenced) as excinfo:
        mission_service.delete("mission-001")
    message = str(excinfo.value)
    assert UI_TEXT_MISSION_DELETE_REFERENCED in message
    assert "下载计划" in message
    # 拒绝就该真的没删
    assert mission_service.exists("mission-001") is True


def test_a_mission_referenced_by_a_task_cannot_be_deleted(
    plan_service: PlanService,
    mission_service: MissionRegionService,
    make_mission,
    work_dir: Path,
) -> None:
    """任务记录也算引用（计划删了、任务还在，同样不能删）。"""
    make_mission()
    _planned(plan_service, work_dir, "mission-001")
    plan_service.create_download_tasks()

    assert "下载任务记录" in mission_service.references("mission-001")
    with pytest.raises(MissionRegionReferenced):
        mission_service.delete("mission-001")


def test_an_unreferenced_mission_can_be_deleted(
    plan_service: PlanService,
    mission_service: MissionRegionService,
    make_mission,
    work_dir: Path,
) -> None:
    """从计划里移除之后就能删（否则用户会陷在"删不掉也去不掉"里）。"""
    make_mission()
    _planned(plan_service, work_dir, "mission-001")
    plan_service.remove_region("mission-001")

    assert mission_service.references("mission-001") == ()
    assert mission_service.delete("mission-001") is True
    assert mission_service.exists("mission-001") is False


# --------------------------------------------------------------------------- #
# 与行政区互不干扰（回归）
# --------------------------------------------------------------------------- #
def test_the_composite_service_still_serves_admin_regions(
    region_service: CompositeRegionService,
) -> None:
    """合并入口对行政区 id 走原来的路（``require_region`` / 下载区域 / 边界可用）。"""
    preset = region_service.require_region("rivertown")
    assert preset.id == "rivertown"
    assert preset.level is RegionLevel.CITY
    assert region_service.boundary_available("rivertown") is True

    region = region_service.get_download_region("rivertown")
    assert region.region_type is RegionType.POLYGON
    assert region.is_mission is False


def test_the_composite_service_routes_mission_ids_to_the_mission_layer(
    region_service: CompositeRegionService, make_mission
) -> None:
    """任务区 id 走任务区那一侧（同形接口，界面只认一个对象）。"""
    make_mission()
    preset = region_service.require_region("mission-001")
    assert preset.id == "mission-001"
    assert preset.level is MISSION_REGION_LEVEL
    # 目录条目的 geojson_path 是"资源目录下的相对路径"，任务区不是资源，
    # 因此这里刻意留空 —— 几何的真实位置在下载区域那一侧（绝对路径）。
    assert preset.geojson_path == ""
    assert region_service.boundary_available("mission-001") is True
    assert region_service.get_download_region("mission-001").is_mission is True


def test_a_mission_does_not_enter_the_static_region_catalog(
    region_service: CompositeRegionService, make_mission
) -> None:
    """任务区**不进**静态目录 —— 它是用户生成的，不是随程序分发的资源。"""
    make_mission()
    region_module.clear_region_resolvers()
    assert region_module.get_region_preset("mission-001") is None
    region_service.mission.install()
    assert region_module.get_region_preset("mission-001") is None  # 仍然是 None
