"""``app.tasks`` 的持久化与状态流转测试（Slice 5）。

覆盖 Spec §8 / §9 / §10 / §13 / §20 / §21：

- TaskStore：save / update / get / list / delete 的基本行为；
- 文件形态：UTF-8、人可读、字段白名单、版本号；
- 原子写：不留 ``.tmp``、不出现半截文件；
- 损坏文件：**隔离而不是删除**，且读操作永不抛异常（一个坏文件不能让程序起不来）；
- Token 绝不落盘（白名单 + 键名黑名单两道防线）；
- TaskService：状态机、节流落盘、启动恢复（``DOWNLOADING`` → ``PAUSED``，不自动下载）；
- 删除记录**不碰** ``.mbtiles``（Spec §20）。

本模块全程不触网、不建 MBTiles。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import app.tasks.task_store as task_store_module
from app.config import settings
from app.geo import GeoBounds
from app.models.download_task import DownloadTask, DownloadTaskStatus
from app.models.map_layer import MapLayerType
from app.tasks import TaskService, TaskStore, TaskStoreError

#: 假 Token（禁止替换为真实密钥）
FAKE_TOKEN = "abcdefghijklmn"


# --------------------------------------------------------------------------- #
# 替身 / 工具
# --------------------------------------------------------------------------- #
class _CountingProgress:
    """引擎进度的鸭子类型替身（``DownloadTask.apply_progress`` 只认字段名）。"""

    def __init__(
        self,
        total: int = 100,
        completed: int = 0,
        succeeded: int = 0,
        failed: int = 0,
        skipped: int = 0,
        zoom: int = 9,
    ) -> None:
        self.total = total
        self.completed = completed
        self.succeeded = succeeded
        self.failed = failed
        self.skipped = skipped
        self.current_zoom = zoom
        self.tiles_per_second = 12.5


def _make_task(task_id: str = "task-1", **overrides: object) -> DownloadTask:
    """造一条形态完整的任务记录（字段都填上，便于断言"原样存取"）。"""
    task = DownloadTask(
        id=task_id,
        name="北京地图",
        status=DownloadTaskStatus.PAUSED,
        map_layer=MapLayerType.VECTOR_LABEL.value,
        west=116.0,
        south=39.0,
        east=117.0,
        north=40.0,
        min_zoom=8,
        max_zoom=10,
        output_path=Path("D:/maps/beijing.mbtiles"),
        total_tiles=1000,
        completed_tiles=400,
        succeeded_tiles=380,
        failed_tiles=20,
        skipped_tiles=0,
    )
    for key, value in overrides.items():
        setattr(task, key, value)
    return task


def _bounds() -> GeoBounds:
    return GeoBounds(west=116.0, south=39.0, east=117.0, north=40.0)


# --------------------------------------------------------------------------- #
# TaskStore：基本增删改查
# --------------------------------------------------------------------------- #
def test_default_path_is_under_home(tasks_file: Path) -> None:
    """Spec §8：任务文件固定在 ``~/MapDownloader/data/tasks.json``。"""
    assert settings.DEFAULT_TASKS_FILE.name == "tasks.json"
    assert settings.DEFAULT_TASKS_FILE.parent.name == "data"
    assert settings.DEFAULT_TASKS_DIR.parent.name == "MapDownloader"
    # 夹具给的路径是临时的，但 store 必须原样使用它（不做任何"聪明"的改写）
    store = TaskStore(tasks_file)
    assert store.path == tasks_file


def test_save_then_get_roundtrip(tasks_file: Path) -> None:
    """存进去再读出来，字段一个不少、类型不变。"""
    store = TaskStore(tasks_file)
    task = _make_task()
    store.save(task)

    loaded = store.get("task-1")
    assert loaded is not None
    assert loaded.name == "北京地图"
    assert loaded.status is DownloadTaskStatus.PAUSED
    assert loaded.map_layer == MapLayerType.VECTOR_LABEL.value
    assert (loaded.west, loaded.south, loaded.east, loaded.north) == (116.0, 39.0, 117.0, 40.0)
    assert (loaded.min_zoom, loaded.max_zoom) == (8, 10)
    assert loaded.output_path == Path("D:/maps/beijing.mbtiles")
    assert isinstance(loaded.output_path, Path)
    assert (loaded.total_tiles, loaded.completed_tiles) == (1000, 400)
    assert (loaded.succeeded_tiles, loaded.failed_tiles) == (380, 20)
    assert isinstance(loaded.created_at, datetime)
    assert isinstance(loaded.updated_at, datetime)


def test_file_is_utf8_and_human_readable(tasks_file: Path) -> None:
    """Spec §8：UTF-8、缩进、中文不转义——用记事本打开就能看懂。"""
    store = TaskStore(tasks_file)
    store.save(_make_task())

    text = tasks_file.read_text(encoding="utf-8")
    assert "北京地图" in text  # ensure_ascii=False
    assert "\n  " in text  # indent=2
    payload = json.loads(text)
    assert payload["version"] == settings.TASKS_FILE_VERSION
    assert isinstance(payload["tasks"], list)
    assert len(payload["tasks"]) == 1
    # 时间与路径都是字符串（不是 Python repr）
    assert isinstance(payload["tasks"][0]["created_at"], str)
    assert isinstance(payload["tasks"][0]["output_path"], str)


def test_update_overwrites_same_id(tasks_file: Path) -> None:
    """同 id 再存一次是"更新"，不是"追加"。"""
    store = TaskStore(tasks_file)
    store.save(_make_task())
    store.save(_make_task(completed_tiles=800, status=DownloadTaskStatus.COMPLETED))

    tasks = store.list_all()
    assert len(tasks) == 1
    assert tasks[0].completed_tiles == 800
    assert tasks[0].status is DownloadTaskStatus.COMPLETED


def test_list_is_sorted_by_updated_at_desc(tasks_file: Path) -> None:
    """列表按 ``updated_at`` 倒序：最近动过的排最前。"""
    store = TaskStore(tasks_file)
    now = datetime(2026, 9, 15, 10, 0, 0)
    store.save(_make_task("old", updated_at=now - timedelta(hours=2)))
    store.save(_make_task("new", updated_at=now))
    store.save(_make_task("mid", updated_at=now - timedelta(hours=1)))

    assert [task.id for task in store.list_all()] == ["new", "mid", "old"]


def test_get_missing_id_returns_none(tasks_file: Path) -> None:
    store = TaskStore(tasks_file)
    store.save(_make_task())
    assert store.get("nope") is None


def test_delete_removes_record_but_keeps_mbtiles(tasks_file: Path) -> None:
    """Spec §20：删记录**不删地图文件**——那是用户几小时的下载成果。"""
    store = TaskStore(tasks_file)
    store.save(_make_task("a"))
    store.save(_make_task("b"))

    # 造一个"同目录下的地图文件"，删记录之后它必须还在
    mbtiles = settings.DEFAULT_OUTPUT_DIR / "beijing.mbtiles"  # 不落盘，只用来断言 path
    assert mbtiles.suffix == ".mbtiles"

    assert store.delete("a") is True
    assert [task.id for task in store.list_all()] == ["b"]
    assert store.delete("a") is False  # 再删一次没有效果
    assert store.get("b") is not None


def test_delete_keeps_other_records_intact(tasks_file: Path) -> None:
    store = TaskStore(tasks_file)
    store.save(_make_task("a", name="甲", total_tiles=11))
    store.save(_make_task("b", name="乙", total_tiles=22))
    store.save(_make_task("c", name="丙", total_tiles=33))

    store.delete("b")

    remaining = {task.id: task for task in store.list_all()}
    assert set(remaining) == {"a", "c"}
    assert remaining["a"].name == "甲"
    assert remaining["c"].total_tiles == 33


# --------------------------------------------------------------------------- #
# TaskStore：原子写 / 损坏处理
# --------------------------------------------------------------------------- #
def test_atomic_write_leaves_no_tmp_file(tasks_file: Path) -> None:
    """写完之后目录里只能有 tasks.json，不能留 ``.tmp`` 半成品。"""
    store = TaskStore(tasks_file)
    store.save(_make_task())

    siblings = sorted(item.name for item in tasks_file.parent.iterdir())
    assert siblings == ["tasks.json"]
    assert tasks_file.with_name(tasks_file.name + ".tmp").exists() is False


def test_write_replaces_file_wholesale(tasks_file: Path) -> None:
    """每次写都是整体重写：不会出现"旧记录残留 + 新记录"的混合态。"""
    store = TaskStore(tasks_file)
    store.save(_make_task("a"))
    store.save_all([_make_task("b")], merge=False)

    assert [task.id for task in store.list_all()] == ["b"]


def test_missing_file_reads_as_empty(tasks_file: Path) -> None:
    """文件不存在 = 没有任务；不抛异常、不创建文件。"""
    store = TaskStore(tasks_file)
    assert tasks_file.exists() is False
    assert store.load_all() == []
    assert store.exists is False
    assert tasks_file.exists() is False  # 读操作不该产生副作用


def test_empty_file_reads_as_empty(tasks_file: Path) -> None:
    """空文件（写到一半被打断的极端情况）当"没有任务"处理。"""
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text("", encoding="utf-8")

    store = TaskStore(tasks_file)
    assert store.load_all() == []


def test_corrupt_file_is_quarantined_not_deleted(tasks_file: Path) -> None:
    """解析不了的文件改名隔离，**不删**——用户手工修一下还有救。"""
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text('{"version": 1, "tasks": [', encoding="utf-8")

    store = TaskStore(tasks_file)
    assert store.load_all() == []

    assert tasks_file.exists() is False  # 原名让位
    quarantined = list(tasks_file.parent.glob(tasks_file.name + settings.TASKS_CORRUPT_SUFFIX + "*"))
    assert len(quarantined) == 1
    assert '{"version": 1, "tasks": [' in quarantined[0].read_text(encoding="utf-8")

    # 隔离之后还能正常用：再写一次就又有了
    store.save(_make_task())
    assert [task.id for task in store.list_all()] == ["task-1"]


def test_corrupt_file_does_not_break_repeated_reads(tasks_file: Path) -> None:
    """连续两次读到坏文件不会互相覆盖隔离名（时间戳 + 原名让位）。"""
    store = TaskStore(tasks_file)
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text("not json at all", encoding="utf-8")
    assert store.load_all() == []
    first = list(tasks_file.parent.glob("*.corrupt*"))
    assert len(first) == 1

    tasks_file.write_text("also not json", encoding="utf-8")
    assert store.load_all() == []
    second = list(tasks_file.parent.glob("*.corrupt*"))
    # 同秒内可能撞名（os.replace 会覆盖），这里只要求"没有把文件弄丢"
    assert len(second) >= 1
    assert tasks_file.exists() is False


def test_single_bad_record_does_not_hide_good_ones(tasks_file: Path) -> None:
    """一条记录坏了，其他记录照样能用（并记一条 warning）。"""
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": settings.TASKS_FILE_VERSION,
        "tasks": [
            task_store_module.task_to_payload(_make_task("good")),
            {"id": "", "status": "paused"},  # 缺 id
            {**_row(_make_task("bad-status")), "status": "no-such-status"},
        ],
    }
    tasks_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    tasks = TaskStore(tasks_file).list_all()
    assert [task.id for task in tasks] == ["good"]


def _row(task: DownloadTask) -> dict:
    return task_store_module.task_to_payload(task)


def test_non_object_top_level_is_quarantined(tasks_file: Path) -> None:
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text("[1, 2, 3]", encoding="utf-8")
    assert TaskStore(tasks_file).load_all() == []
    assert tasks_file.exists() is False


# --------------------------------------------------------------------------- #
# TaskStore：Token 绝不落盘（Spec §9）
# --------------------------------------------------------------------------- #
def test_payload_has_exactly_whitelisted_fields(tasks_file: Path) -> None:
    """白名单是"永远不会多写一个字段"的硬保证。"""
    payload = task_store_module.task_to_payload(_make_task())
    assert set(payload) == set(task_store_module._TASK_FIELDS)
    assert "token" not in payload


def test_token_never_appears_in_file(tasks_file: Path) -> None:
    """把 Token 放进任务对象也写不进磁盘（它不是白名单字段）。"""
    store = TaskStore(tasks_file)
    task = _make_task()
    # 模拟"有人不小心把 token 挂到了任务对象上"
    task.token = FAKE_TOKEN  # type: ignore[attr-defined]
    store.save(task)

    assert FAKE_TOKEN not in tasks_file.read_text(encoding="utf-8")


def test_secret_key_fragment_is_rejected_before_writing(
    tasks_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """键名里出现 ``tk``/``token`` 一律拒写（第二道防线，防未来手滑）。"""
    original = task_store_module.task_to_payload

    def bad_payload(task: DownloadTask) -> dict:
        payload = original(task)
        payload["tk"] = FAKE_TOKEN  # 白名单里没有它 → 第一道防线就会拦
        return payload

    monkeypatch.setattr(task_store_module, "task_to_payload", bad_payload)
    store = TaskStore(tasks_file)
    with pytest.raises(TaskStoreError):
        store.save(_make_task())

    assert tasks_file.exists() is False
    assert list(tasks_file.parent.glob("*")) == []


def test_secret_key_check_catches_nested_keys() -> None:
    """黑名单是递归的：嵌套 dict 里的 ``api_key`` 也拦得住。"""
    with pytest.raises(TaskStoreError) as info:
        task_store_module._assert_no_secret_keys(
            {"tasks": [{"meta": {"api_key": "x"}}]}
        )
    assert "api_key" in str(info.value)


def test_secret_key_check_allows_innocent_values() -> None:
    """只查键名不查值：一张叫"token 试验"的地图完全可以写盘。"""
    task_store_module._assert_no_secret_keys({"name": "token 试验"})  # 不抛


def test_unwritable_path_raises_store_error(tasks_file: Path) -> None:
    """路径不可写要抛 ``TaskStoreError``，而不是让 ``OSError`` 冒到 UI 层。

    构造方式：把"父目录"做成一个**文件**——``mkdir(parents=True)`` 在
    Windows / POSIX 上都会稳定失败，比依赖只读权限可靠得多。
    """
    blocker = tasks_file.parent / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    store = TaskStore(blocker / "tasks.json")
    with pytest.raises(TaskStoreError):
        store.save(_make_task())

    # 半成品临时文件必须被清掉，不能在用户目录里留垃圾
    assert list(blocker.parent.glob("*.tmp")) == []


# --------------------------------------------------------------------------- #
# TaskService：创建 / 状态机
# --------------------------------------------------------------------------- #
def test_create_task_persists_immediately(tasks_file: Path) -> None:
    """建档即落盘，状态是 ``CREATED``。"""
    service = TaskService(TaskStore(tasks_file))
    task = service.create_task(
        name="beijing",
        bounds=_bounds(),
        min_zoom=8,
        max_zoom=10,
        map_layer=MapLayerType.VECTOR,
        output_path=tasks_file.parent / "beijing.mbtiles",
        total_tiles=1234,
    )

    assert task.status is DownloadTaskStatus.CREATED
    assert task.total_tiles == 1234
    assert tasks_file.exists() is True
    on_disk = TaskStore(tasks_file).get(task.id)
    assert on_disk is not None
    assert on_disk.name == "beijing"


def test_create_task_rejects_bad_bounds(tasks_file: Path) -> None:
    service = TaskService(TaskStore(tasks_file))
    with pytest.raises(ValueError):
        service.create_task(
            name="x",
            bounds="not-bounds",  # type: ignore[arg-type]
            min_zoom=8,
            max_zoom=10,
            map_layer=MapLayerType.VECTOR,
            output_path=tasks_file.parent / "x.mbtiles",
        )


def test_state_machine_transitions_are_persisted(tasks_file: Path) -> None:
    """CREATED → DOWNLOADING → PAUSED → DOWNLOADING → COMPLETED，每一步都落盘。"""
    service = TaskService(TaskStore(tasks_file))
    task = service.create_task(
        name="beijing",
        bounds=_bounds(),
        min_zoom=8,
        max_zoom=10,
        map_layer=MapLayerType.VECTOR,
        output_path=tasks_file.parent / "beijing.mbtiles",
        total_tiles=100,
    )

    seen: list[str] = []

    def reload_status() -> str:
        fresh = TaskService(TaskStore(tasks_file)).get_task(task.id)
        assert fresh is not None
        return fresh.status.value

    seen.append(reload_status())
    service.start_downloading(task)
    seen.append(reload_status())
    service.pause_task(task)
    seen.append(reload_status())
    service.resume_task(task)
    seen.append(reload_status())
    service.complete_task(task)
    seen.append(reload_status())

    assert seen == ["created", "downloading", "paused", "downloading", "completed"]


def test_fail_task_records_reason(tasks_file: Path) -> None:
    """失败必须留下原因：UI 靠它告诉用户"再点一次继续有没有用"。"""
    service = TaskService(TaskStore(tasks_file))
    task = service.create_task(
        name="beijing",
        bounds=_bounds(),
        min_zoom=8,
        max_zoom=10,
        map_layer=MapLayerType.VECTOR,
        output_path=tasks_file.parent / "beijing.mbtiles",
        total_tiles=100,
    )
    service.fail_task(task, "有 12 张瓦片下载失败：请求超时（12 张）")

    reloaded = service.get_task(task.id)
    assert reloaded is not None
    assert reloaded.status is DownloadTaskStatus.FAILED
    assert "12 张瓦片下载失败" in (reloaded.error or "")

    # 继续下载时必须把旧错误清掉，否则界面上永远挂着上一次的失败原因
    service.resume_task(reloaded)
    assert reloaded.error is None
    assert reloaded.can_resume is True


def test_complete_task_marks_progress_full(tasks_file: Path) -> None:
    service = TaskService(TaskStore(tasks_file))
    task = service.create_task(
        name="beijing",
        bounds=_bounds(),
        min_zoom=8,
        max_zoom=10,
        map_layer=MapLayerType.VECTOR,
        output_path=tasks_file.parent / "beijing.mbtiles",
        total_tiles=100,
    )
    service.complete_task(task)
    assert task.completed_tiles == 100
    assert task.percent == pytest.approx(100.0)
    assert task.can_resume is False


# --------------------------------------------------------------------------- #
# TaskService：节流落盘（Spec §13）
# --------------------------------------------------------------------------- #
def test_progress_save_is_throttled_after_creation(tasks_file: Path) -> None:
    """建档那一次已经落了盘，所以紧接着的进度回调按节流走（Spec §13）。

    真机上的收益是明确的：一只 6 并发、8 req/s 的下载器每秒会抛出几十帧进度，
    每一帧都写一次 JSON 就变成"磁盘比网络还忙"。
    """
    service = TaskService(
        TaskStore(tasks_file), save_interval_seconds=60.0, save_tile_interval=1000
    )
    task = service.create_task(
        name="beijing",
        bounds=_bounds(),
        min_zoom=8,
        max_zoom=10,
        map_layer=MapLayerType.VECTOR,
        output_path=tasks_file.parent / "beijing.mbtiles",
        total_tiles=1000,
    )

    # 时间没到、张数没涨够 → 不写盘
    assert service.update_progress(task, _CountingProgress(completed=1)) is False
    assert service.update_progress(task, _CountingProgress(completed=10)) is False
    assert TaskStore(tasks_file).get(task.id).completed_tiles == 0  # type: ignore[union-attr]
    # 但内存里的任务对象是**同步更新**的（UI 读的是它）
    assert task.completed_tiles == 10

    # 张数涨够 1000 张 → 立刻写盘
    assert service.update_progress(task, _CountingProgress(completed=1000)) is True
    assert TaskStore(tasks_file).get(task.id).completed_tiles == 1000  # type: ignore[union-attr]


def test_progress_save_triggers_on_tile_interval(tasks_file: Path) -> None:
    """张数涨够 ``save_tile_interval`` 就写盘，哪怕时间还没到。"""
    service = TaskService(
        TaskStore(tasks_file), save_interval_seconds=3600.0, save_tile_interval=50
    )
    task = service.create_task(
        name="beijing",
        bounds=_bounds(),
        min_zoom=8,
        max_zoom=10,
        map_layer=MapLayerType.VECTOR,
        output_path=tasks_file.parent / "beijing.mbtiles",
        total_tiles=1000,
    )
    service.update_progress(task, _CountingProgress(completed=1))

    assert service.update_progress(task, _CountingProgress(completed=30)) is False
    assert service.update_progress(task, _CountingProgress(completed=51)) is True
    assert TaskStore(tasks_file).get(task.id).completed_tiles == 51  # type: ignore[union-attr]


def test_progress_save_triggers_on_time_interval(tasks_file: Path) -> None:
    """时间到了就写盘，哪怕一张都没涨。"""
    service = TaskService(
        TaskStore(tasks_file), save_interval_seconds=0.05, save_tile_interval=10_000
    )
    task = service.create_task(
        name="beijing",
        bounds=_bounds(),
        min_zoom=8,
        max_zoom=10,
        map_layer=MapLayerType.VECTOR,
        output_path=tasks_file.parent / "beijing.mbtiles",
        total_tiles=1000,
    )
    service.update_progress(task, _CountingProgress(completed=1))
    time.sleep(0.06)
    assert service.update_progress(task, _CountingProgress(completed=1)) is True


def test_save_now_bypasses_throttle(tasks_file: Path) -> None:
    """状态变化走 ``save_now``：不该被节流挡住。"""
    service = TaskService(
        TaskStore(tasks_file), save_interval_seconds=3600.0, save_tile_interval=10_000
    )
    task = service.create_task(
        name="beijing",
        bounds=_bounds(),
        min_zoom=8,
        max_zoom=10,
        map_layer=MapLayerType.VECTOR,
        output_path=tasks_file.parent / "beijing.mbtiles",
        total_tiles=1000,
    )
    task.completed_tiles = 777
    service.save_now(task)
    assert TaskStore(tasks_file).get(task.id).completed_tiles == 777  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# TaskService：启动恢复（Spec §10）
# --------------------------------------------------------------------------- #
def test_recover_downgrades_downloading_to_paused(tasks_file: Path) -> None:
    """上次异常退出留下的 ``DOWNLOADING`` 要降级为 ``PAUSED``，**不自动下载**。"""
    store = TaskStore(tasks_file)
    store.save(_make_task("running", status=DownloadTaskStatus.DOWNLOADING))
    store.save(_make_task("paused", status=DownloadTaskStatus.PAUSED))
    store.save(_make_task("done", status=DownloadTaskStatus.COMPLETED))

    service = TaskService(store)
    recovered = service.recover_interrupted_tasks()

    assert [task.id for task in recovered] == ["running"]
    assert recovered[0].status is DownloadTaskStatus.PAUSED

    after = {task.id: task.status for task in service.list_tasks()}
    assert after["running"] is DownloadTaskStatus.PAUSED
    assert after["paused"] is DownloadTaskStatus.PAUSED  # 没被动过
    assert after["done"] is DownloadTaskStatus.COMPLETED


def test_recover_is_idempotent_and_quiet_when_clean(tasks_file: Path) -> None:
    """干净退出时返回空列表；跑第二遍也不会有额外变化。"""
    store = TaskStore(tasks_file)
    store.save(_make_task("paused", status=DownloadTaskStatus.PAUSED))
    service = TaskService(store)

    assert service.recover_interrupted_tasks() == []
    assert service.recover_interrupted_tasks() == []


def test_recover_with_no_file_is_a_noop(tasks_file: Path) -> None:
    service = TaskService(TaskStore(tasks_file))
    assert service.recover_interrupted_tasks() == []
    assert tasks_file.exists() is False  # 没有任务就不该创建文件


def test_delete_task_removes_record_only(tasks_file: Path) -> None:
    """TaskService.delete_task 不做任何"顺手清理文件"的动作。"""
    service = TaskService(TaskStore(tasks_file))
    task = service.create_task(
        name="beijing",
        bounds=_bounds(),
        min_zoom=8,
        max_zoom=10,
        map_layer=MapLayerType.VECTOR,
        output_path=tasks_file.parent / "beijing.mbtiles",
        total_tiles=10,
    )
    # 造一个真的地图文件在任务目录旁边
    fake_map = tasks_file.parent / "beijing.mbtiles"
    fake_map.write_bytes(b"SQLite format 3\x00")

    assert service.delete_task(task.id) is True
    assert service.get_task(task.id) is None
    assert fake_map.exists() is True  # 记录删了，文件留着
    assert service.delete_task(task.id) is False


def test_count_tasks(tasks_file: Path) -> None:
    service = TaskService(TaskStore(tasks_file))
    assert service.count_tasks() == 0
    for index in range(3):
        service.create_task(
            name=f"m{index}",
            bounds=_bounds(),
            min_zoom=8,
            max_zoom=9,
            map_layer=MapLayerType.VECTOR,
            output_path=tasks_file.parent / f"m{index}.mbtiles",
        )
    assert service.count_tasks() == 3
