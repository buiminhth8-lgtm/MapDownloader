"""计划编排与任务生成（Slice 20，Spec §13 / §14 / §15 / §16 / §17 / §18 / §25）。

守五件事：

1. **一对一、多对多**（Spec §14）：每个"启用且可用"的条目恰好生成一个现有的
   :class:`~app.models.DownloadTask`，形状与单区域下载**完全一样**（复用
   ``TaskService``，不抄下载逻辑）。
2. **顺序即层级**（Spec §13）：全国 → 省 → 市，与用户加入的顺序无关 ——
   这是给 Slice 21 串行执行准备的地基。
3. **绝不自动开始下载**（Spec §14 / §25）：只写任务记录，不建目录、不碰网络。
   除了行为断言，还有一条 ``ast`` 静态守卫（字符串匹配会被文档字符串误伤）。
4. **半套任务比没有任务更糟**：建到一半失败 → 已建的记录被删掉。
5. **配置活下来、结论不活**（Spec §17 / §18）：重启后区域 / Zoom / 启用状态都还在，
   而瓦片数是**算出来**的，不从文件里读回来。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.config.settings import DEFAULT_PLAN_NAME
from app.models.download_task import DownloadTaskStatus
from app.planning.download_plan import DownloadPlan, DownloadPlanItem, plan_directory
from app.planning.plan_service import PlanService, PlanTaskCreationError
from app.planning.plan_store import PlanStore
from app.planning.plan_validator import PlanIssueCode
from app.regions.service import RegionService
from app.tasks.task_service import TaskService
from app.tasks.task_store import TaskStore, TaskStoreError

#: 仓库根（静态守卫要读源码）。
ROOT = Path(__file__).resolve().parents[1]

#: Windows 文件名非法字符（与 ``app.models.naming`` 同一套）。
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


@pytest.fixture
def service(region_resources: Path) -> RegionService:
    return RegionService()


@pytest.fixture
def task_service(tasks_file: Path) -> TaskService:
    return TaskService(TaskStore(tasks_file))


@pytest.fixture
def plan_service(
    service: RegionService, plans_file: Path, task_service: TaskService
) -> PlanService:
    return PlanService(
        store=PlanStore(plans_file), service=service, task_service=task_service
    )


def _ready(
    plan_service: PlanService, work_dir: Path, *region_ids: str, name: str = "离线计划"
) -> DownloadPlan:
    """把若干区域加进计划、算一遍，返回可生成任务的计划。"""
    for region_id in region_ids:
        plan_service.add_region(region_id)
    plan_service.apply_settings(name=name, output_dir=work_dir / "output")
    plan_service.estimate()
    return plan_service.plan


# --------------------------------------------------------------------------- #
# 任务生成（Spec §14 / §15 / §16）
# --------------------------------------------------------------------------- #
def test_one_item_generates_one_task(
    plan_service: PlanService, task_service: TaskService, work_dir: Path
) -> None:
    plan = _ready(plan_service, work_dir, "northland")
    item = plan.item("northland")

    tasks = plan_service.create_download_tasks()

    assert len(tasks) == 1
    task = tasks[0]
    assert task.min_zoom == item.min_zoom and task.max_zoom == item.max_zoom
    assert task.total_tiles == item.selected_tiles
    assert task.region_id == "northland"
    assert item.region_name in task.name
    assert item.zoom_text in task.name
    assert task.output_path == plan_service.output_paths()["northland"]
    assert (
        task.output_path.name
        == f"northland_z{item.min_zoom}_{item.max_zoom}_vec.mbtiles"
    )
    # 与单区域下载同一条路：记录真的落盘了
    assert [row.id for row in task_service.list_tasks()] == [task.id]


def test_multi_item_generates_one_task_each(
    plan_service: PlanService, work_dir: Path
) -> None:
    plan = _ready(plan_service, work_dir, "demoland", "northland", "rivertown")

    tasks = plan_service.create_download_tasks()

    assert len(tasks) == 3
    assert {task.region_id for task in tasks} == {"demoland", "northland", "rivertown"}
    assert all(task.total_tiles > 0 for task in tasks)
    assert len({task.output_path for task in tasks}) == 3
    assert plan.count == 3


def test_task_order_follows_the_hierarchy_not_the_click_order(
    plan_service: PlanService, work_dir: Path
) -> None:
    """Spec §13：市级先加，生成出来仍在最后（Slice 21 要按这个顺序串行执行）。"""
    _ready(plan_service, work_dir, "rivertown", "demoland", "northland")

    tasks = plan_service.create_download_tasks()

    assert [task.region_id for task in tasks] == ["demoland", "northland", "rivertown"]


def test_disabled_items_do_not_generate_tasks(
    plan_service: PlanService, work_dir: Path
) -> None:
    """Spec §12：禁用项不入总量、不生成任务，但**留在计划里**（随时能开回来）。"""
    _ready(plan_service, work_dir, "demoland", "northland")
    plan_service.set_enabled("northland", False)

    tasks = plan_service.create_download_tasks()

    assert [task.region_id for task in tasks] == ["demoland"]
    assert plan_service.plan.item("northland") is not None


def test_output_paths_live_under_a_per_plan_directory(
    plan_service: PlanService, work_dir: Path
) -> None:
    """Spec §16：一个计划的文件收在 ``<输出目录>/<计划名>/`` 下。"""
    _ready(plan_service, work_dir, "demoland", "northland", name="华东离线包")

    directory = plan_service.plan_directory()
    tasks = plan_service.create_download_tasks()

    assert directory == Path(str(work_dir / "output")) / "华东离线包"
    assert all(task.output_path.parent == directory for task in tasks)


def test_output_directory_is_not_created_yet(
    plan_service: PlanService, work_dir: Path
) -> None:
    """Spec §16 最后一句：本 Slice 只创建任务配置，目录留给下载阶段去建。"""
    _ready(plan_service, work_dir, "demoland")

    plan_service.create_download_tasks()

    assert (work_dir / "output").exists() is False
    assert list(work_dir.glob("**/*.mbtiles")) == []


def test_plan_name_is_sanitized_for_the_directory(
    plan_service: PlanService, work_dir: Path
) -> None:
    """计划名直接进目录名 → 必须先净化（Spec §15 的"Windows 合法"）。"""
    _ready(plan_service, work_dir, "demoland", name='我的计划:2026?<华东>|"全图"')

    directory = plan_service.plan_directory()

    assert _ILLEGAL.search(directory.name) is None
    assert directory.name != ""


def test_an_empty_plan_name_falls_back_to_the_default(
    plan_service: PlanService, work_dir: Path
) -> None:
    """空名字会直接变成"输出目录根"，几个计划的文件就会撞在一起。"""
    _ready(plan_service, work_dir, "demoland", name="   ")

    assert plan_directory(plan_service.plan).name == DEFAULT_PLAN_NAME


def test_the_plan_layer_carries_layer_and_labels_into_tasks(
    plan_service: PlanService, work_dir: Path, task_service: TaskService
) -> None:
    """Spec §14：图层与"是否合成注记"必须跟着计划走，否则重启后就恢复不出来。"""
    from app.models.map_layer import MapLayerType

    _ready(plan_service, work_dir, "demoland")
    plan_service.apply_settings(map_layer=MapLayerType.VECTOR_LABEL, with_labels=True)

    task = plan_service.create_download_tasks()[0]

    assert task.map_layer == MapLayerType.VECTOR_LABEL.value
    assert task.with_labels is True
    assert task_service.get_task(task.id).with_labels is True


# --------------------------------------------------------------------------- #
# 不通过 / 回滚（Spec §9 / §21）
# --------------------------------------------------------------------------- #
def test_a_plan_that_fails_validation_creates_nothing(
    plan_service: PlanService, task_service: TaskService, work_dir: Path
) -> None:
    """校验没过 → **一个任务都不建**，而且要说清是为什么。"""
    _ready(plan_service, work_dir, "demoland")
    plan_service.set_enabled("demoland", False)

    with pytest.raises(PlanTaskCreationError) as excinfo:
        plan_service.create_download_tasks()

    assert task_service.list_tasks() == []
    message = str(excinfo.value)
    assert "没有生成任何下载任务" in message
    assert "一个可用的启用项" in message


def test_a_broken_item_blocks_task_creation_entirely(
    plan_service: PlanService, task_service: TaskService, work_dir: Path
) -> None:
    """一条坏项 → 整个计划不生成（半套任务比没有任务更难收拾）。"""
    plan = _ready(plan_service, work_dir, "demoland")
    plan.add_item(DownloadPlanItem(region_id="nowhere", region_name="不存在的地区"))

    with pytest.raises(PlanTaskCreationError) as excinfo:
        plan_service.create_download_tasks()

    assert task_service.list_tasks() == []
    assert "不存在的地区" in str(excinfo.value)


class _FailingTaskService(TaskService):
    """建到第 N 条时抛错（模拟"写任务记录时磁盘坏了"）—— 用来验证回滚。"""

    def __init__(self, store: TaskStore, *, fail_on: int) -> None:
        super().__init__(store)
        self.fail_on = fail_on
        self.calls = 0

    def create_task(self, **kwargs):  # type: ignore[override]
        self.calls += 1
        if self.calls >= self.fail_on:
            raise TaskStoreError("模拟：任务记录写不进去")
        return super().create_task(**kwargs)


def test_tasks_created_before_a_failure_are_rolled_back(
    service: RegionService, plans_file: Path, tasks_file: Path, work_dir: Path
) -> None:
    """第 3 条失败 → 前 2 条记录被删掉（否则用户无法判断"到底生成了几个"）。"""
    failing = _FailingTaskService(TaskStore(tasks_file), fail_on=3)
    plan_service = PlanService(
        store=PlanStore(plans_file), service=service, task_service=failing
    )
    _ready(plan_service, work_dir, "demoland", "northland", "rivertown")

    with pytest.raises(PlanTaskCreationError) as excinfo:
        plan_service.create_download_tasks()

    assert "已回滚" in str(excinfo.value)
    assert failing.list_tasks() == []
    # 计划本身不受影响（那是用户的配置）
    assert plan_service.plan.count == 3


def test_rollback_does_not_touch_the_plan_file(
    service: RegionService, plans_file: Path, tasks_file: Path, work_dir: Path
) -> None:
    failing = _FailingTaskService(TaskStore(tasks_file), fail_on=1)
    plan_service = PlanService(
        store=PlanStore(plans_file), service=service, task_service=failing
    )
    _ready(plan_service, work_dir, "demoland")
    before = plans_file.read_bytes()

    with pytest.raises(PlanTaskCreationError):
        plan_service.create_download_tasks()

    assert plans_file.read_bytes() == before


# --------------------------------------------------------------------------- #
# 持久化（Spec §17 / §18）
# --------------------------------------------------------------------------- #
def test_missing_plan_file_starts_with_an_empty_plan(
    service: RegionService, plans_file: Path, task_service: TaskService
) -> None:
    """旧版本 / 首次运行：没有计划文件也必须**正常启动**（Spec §18）。"""
    plan_service = PlanService(
        store=PlanStore(plans_file), service=service, task_service=task_service
    )

    plan = plan_service.load()

    assert plan.is_empty is True
    assert plan.name == DEFAULT_PLAN_NAME
    assert plan.output_dir  # 有兜底输出目录，不是空串
    assert plans_file.exists() is False, "载入不该顺手写盘"


def test_plan_survives_a_restart(
    service: RegionService,
    plans_file: Path,
    task_service: TaskService,
    work_dir: Path,
) -> None:
    """重启后区域 / Zoom / 启用状态都在，而且**要重算瓦片数**（Spec §17 / §18）。"""
    first = PlanService(
        store=PlanStore(plans_file), service=service, task_service=task_service
    )
    plan = _ready(first, work_dir, "demoland", "northland")
    first.set_enabled("northland", False)
    plan.add_item(DownloadPlanItem(region_id="rivertown", region_name="示例河市"))
    first.save()
    expected = [
        (item.region_id, item.min_zoom, item.max_zoom, item.enabled) for item in plan.items
    ]

    second = PlanService(
        store=PlanStore(plans_file), service=service, task_service=task_service
    )
    reloaded = second.load()

    assert [
        (item.region_id, item.min_zoom, item.max_zoom, item.enabled)
        for item in reloaded.items
    ] == expected
    assert reloaded.count == 3


def test_estimates_are_recomputed_not_read_back(
    service: RegionService,
    plans_file: Path,
    task_service: TaskService,
    work_dir: Path,
) -> None:
    """Spec §17：结论（状态 / 张数）不落盘 —— 重开后是"还没算"，不是陈旧数字。"""
    first = PlanService(
        store=PlanStore(plans_file), service=service, task_service=task_service
    )
    plan = _ready(first, work_dir, "demoland")
    assert plan.item("demoland").selected_tiles > 0
    first.save()

    second = PlanService(
        store=PlanStore(plans_file), service=service, task_service=task_service
    )
    reloaded = second.load()

    item = reloaded.item("demoland")
    assert item.is_estimated is False
    assert item.selected_tiles == 0
    assert item.tiles_text == "—"
    assert reloaded.needs_estimate() is True
    # 而重新算一次就能拿回同一个数（结论是可重现的，只是不缓存）
    second.estimate()
    assert second.plan.item("demoland").selected_tiles > 0


def test_a_region_that_disappeared_does_not_block_the_plan(
    service: RegionService,
    plans_file: Path,
    task_service: TaskService,
    work_dir: Path,
    region_resources: Path,
) -> None:
    """Spec §18：区域被删 → 该项标成不可用，**整份计划照常打开**。"""
    first = PlanService(
        store=PlanStore(plans_file), service=service, task_service=task_service
    )
    _ready(first, work_dir, "demoland", "northland")
    first.save()

    (region_resources / "province" / "northland.geojson").unlink()
    second = PlanService(
        store=PlanStore(plans_file), service=service, task_service=task_service
    )
    reloaded = second.load()
    assert reloaded.count == 2, "打不开就等于连删掉那个区块都做不了"

    result = second.validate()
    assert result.has(PlanIssueCode.BOUNDARY_MISSING)
    assert reloaded.item("northland").is_valid is False
    # 另一块地没被牵连：错误只来自坏项
    assert result.count(PlanIssueCode.BOUNDARY_MISSING) == 1


def test_adding_without_saving_keeps_the_file_untouched(
    plan_service: PlanService, plans_file: Path
) -> None:
    plan_service.add_region("demoland")
    before = plans_file.read_bytes()

    plan_service.add_region("northland", save=False)

    assert plans_file.read_bytes() == before
    assert plan_service.plan.count == 2


def test_removing_a_region_drops_it_from_the_file(
    plan_service: PlanService, plans_file: Path
) -> None:
    plan_service.add_region("demoland")
    plan_service.add_region("northland")

    assert plan_service.remove_region("northland") is True
    assert plan_service.remove_region("northland") is False

    reloaded = PlanStore(plans_file).load_all()[-1]
    assert [item.region_id for item in reloaded.items] == ["demoland"]


def test_turning_a_plan_back_on_is_persisted(
    plan_service: PlanService, plans_file: Path
) -> None:
    plan_service.add_region("demoland")
    plan_service.add_region("northland")
    plan_service.set_enabled("northland", False)

    plan_service.set_enabled("northland", True)

    reloaded = PlanStore(plans_file).load_all()[-1]
    assert reloaded.item("northland").enabled is True


# --------------------------------------------------------------------------- #
# 摘要与设置（Spec §10 / §11）
# --------------------------------------------------------------------------- #
def test_summary_text_tracks_the_plan_state(
    plan_service: PlanService, work_dir: Path
) -> None:
    assert plan_service.summary_text() == "下载计划：暂无区域"

    plan_service.add_region("demoland")
    assert "尚未估算" in plan_service.summary_text()

    plan_service.estimate()
    summary = plan_service.summary_text()
    assert "1 个区域" in summary
    assert plan_service.plan.total_tiles_text in summary
    assert plan_service.plan.size_text in summary


def test_settings_are_stored_in_the_plan(
    plan_service: PlanService, work_dir: Path
) -> None:
    """名字与输出目录决定"文件落在哪"，图层与注记决定"下的是什么"（Spec §14 / §16）。"""
    from app.models.map_layer import MapLayerType

    plan_service.add_region("demoland")

    plan_service.apply_settings(
        name="  新名字  ",
        output_dir=str(work_dir / "out"),
        map_layer=MapLayerType.IMAGE,
        with_labels=True,
    )

    plan = plan_service.plan
    assert plan.name == "新名字"
    assert plan.output_dir == str(work_dir / "out")
    assert plan.map_layer == MapLayerType.IMAGE.value
    assert plan.with_labels is True

    plan_service.apply_settings(name="又一版")  # None 表示"这一项不动"
    assert plan.output_dir == str(work_dir / "out")
    assert plan.map_layer == MapLayerType.IMAGE.value


def test_clear_empties_the_plan_but_keeps_its_identity(
    plan_service: PlanService, work_dir: Path
) -> None:
    """Spec §11 的「清空」：名字 / 输出目录 / id 都留着（否则"清空"等于换了个计划）。"""
    _ready(plan_service, work_dir, "demoland", name="我的计划")
    before_id = plan_service.plan.id

    plan_service.clear()

    plan = plan_service.plan
    assert plan.is_empty is True
    assert plan.id == before_id
    assert plan.name == "我的计划"
    assert plan.output_dir == str(work_dir / "output")


# --------------------------------------------------------------------------- #
# 静态守卫（Spec §14 / §25）
# --------------------------------------------------------------------------- #
def test_the_orchestrator_cannot_reach_the_downloader() -> None:
    """静态守卫：编排层碰不到下载器（Spec §14 / §25"绝不自动开始下载"）。

    用 ``ast`` 而不是字符串匹配：本模块的文档字符串里正大光明地写着
    "不碰 ``MapDownloadService``"，`"MapDownloadService" in source` 必然误报
    （Slice 14 的教训）。这里只看**真实导入**与**真实调用**。
    """
    source = (ROOT / "app" / "planning" / "plan_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[-1] for alias in node.names)
    assert "MapDownloadService" not in imported
    assert not any(name.endswith("download_service") for name in imported)

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (called & {"start_download", "start_all", "download_tiles", "run"})


def test_generated_tasks_start_in_the_created_state(
    plan_service: PlanService, work_dir: Path
) -> None:
    """行为断言：生成的任务停在"已创建"，没有任何进度（下载由用户自己点）。"""
    _ready(plan_service, work_dir, "demoland", "northland")

    tasks = plan_service.create_download_tasks()

    assert all(task.status is DownloadTaskStatus.CREATED for task in tasks)
    assert all(task.progress.tile_done == 0 for task in tasks)
    assert all(task.completed_tiles == 0 for task in tasks)
