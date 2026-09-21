"""执行记录的持久化（Slice 21，Spec §19 / §20）。

守四件事：

1. **白名单**：写进 ``plan_executions.json`` 的键必须**精确**是那七个（条目四个），
   多一个都会在写盘前抛错 —— 有人加了字段却忘了同步白名单时，宁可当场炸，
   也不要"存了但重启后读不回来"。
2. **不保存 Token**：字段名里出现 ``token`` / ``tk`` 之类直接拒绝写盘；
   三张禁词表（tasks / plans / executions）**逐字相同**，有一条用例把三者
   对齐 —— 它们必须一起改。
3. **损坏隔离，不删除**：解析不了就改名成 ``….corrupt.<时间戳>``，
   用户的文件一个字节都不丢。
4. **启动恢复只改状态、不联网**：``RUNNING`` → ``PAUSED`` 并立即落盘，
   ``recover`` 的返回值告诉调用方"要不要提示用户"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.planning.plan_execution import (
    PlanExecution,
    PlanExecutionStatus,
    PlanItemExecution,
    PlanItemRunStatus,
)
from app.planning.plan_execution_store import (
    PlanExecutionStore,
    PlanExecutionStoreError,
    execution_to_payload,
    payload_to_execution,
)


@pytest.fixture
def store(work_dir: Path) -> PlanExecutionStore:
    """独立的执行记录文件（跟着用例的工作目录走，绝不碰用户目录）。"""
    return PlanExecutionStore(work_dir / "plan_executions.json")


def _execution(plan_id: str = "plan-1", **overrides) -> PlanExecution:
    """一份有两条条目的执行记录。"""
    execution = PlanExecution(
        plan_id=plan_id,
        status=overrides.pop("status", PlanExecutionStatus.RUNNING),
        current_item_index=overrides.pop("current_item_index", 1),
        items=[
            PlanItemExecution("i-1", "t-1", PlanItemRunStatus.COMPLETED),
            PlanItemExecution("i-2", "t-2", PlanItemRunStatus.RUNNING),
        ],
        total_tiles=overrides.pop("total_tiles", 400),
        completed_tiles=overrides.pop("completed_tiles", 100),
        downloaded_bytes=overrides.pop("downloaded_bytes", 1_048_576),
    )
    for key, value in overrides.items():
        setattr(execution, key, value)
    return execution


# --------------------------------------------------------------------------- #
# 读写（Spec §19）
# --------------------------------------------------------------------------- #
def test_a_missing_file_is_an_empty_list_not_an_error(store: PlanExecutionStore) -> None:
    """首次运行 / 旧版本没有这个文件是**正常情况**，不该报错。"""
    assert store.load_all() == []
    assert store.find("plan-1") is None
    assert store.unfinished() == []


def test_save_and_load_round_trip(store: PlanExecutionStore) -> None:
    store.save(_execution())

    loaded = store.find("plan-1")

    assert loaded is not None
    assert loaded.status is PlanExecutionStatus.RUNNING
    assert loaded.current_item_index == 1
    assert loaded.total_tiles == 400
    assert loaded.completed_tiles == 100
    assert loaded.downloaded_bytes == 1_048_576
    assert loaded.item_ids() == ["i-1", "i-2"]
    assert loaded.items[0].status is PlanItemRunStatus.COMPLETED
    assert loaded.items[1].task_id == "t-2"


def test_saving_the_same_plan_overwrites_it(store: PlanExecutionStore) -> None:
    store.save(_execution(completed_tiles=10))
    store.save(_execution(completed_tiles=250))

    assert len(store.load_all()) == 1
    assert store.find("plan-1").completed_tiles == 250


def test_saving_one_plan_keeps_the_others(store: PlanExecutionStore) -> None:
    """一份执行记录覆盖另一份是最容易犯的错（同 id 才覆盖）。"""
    store.save(_execution("plan-1", completed_tiles=10))
    store.save(_execution("plan-2", completed_tiles=20))

    assert {item.plan_id for item in store.load_all()} == {"plan-1", "plan-2"}
    store.save(_execution("plan-1", completed_tiles=99))
    assert store.find("plan-2").completed_tiles == 20
    assert store.find("plan-1").completed_tiles == 99


def test_writing_leaves_no_temporary_file_behind(store: PlanExecutionStore) -> None:
    """``tmp → fsync → os.replace`` 之后不该留下 ``.tmp``。"""
    store.save(_execution())

    assert list(store.path.parent.glob("*.tmp")) == []


def test_a_none_task_id_and_error_survive_the_round_trip(
    store: PlanExecutionStore,
) -> None:
    """还没建任务的条目（``task_id=None``）与失败原因都要能原样读回来。"""
    execution = PlanExecution(
        plan_id="p",
        items=[
            PlanItemExecution("i-1", None, PlanItemRunStatus.WAITING),
            PlanItemExecution("i-2", "t-2", PlanItemRunStatus.FAILED, "网络断了"),
        ],
    )
    store.save(execution)

    loaded = store.find("p")

    assert loaded.items[0].task_id is None
    assert loaded.items[1].error == "网络断了"
    assert loaded.items[1].status is PlanItemRunStatus.FAILED


def test_delete_removes_only_the_record(store: PlanExecutionStore) -> None:
    store.save(_execution("plan-1"))
    store.save(_execution("plan-2"))

    assert store.delete("plan-1") is True
    assert store.delete("plan-1") is False
    assert [item.plan_id for item in store.load_all()] == ["plan-2"]


# --------------------------------------------------------------------------- #
# 白名单与禁词（Spec §19）
# --------------------------------------------------------------------------- #
def test_only_whitelisted_keys_are_written(store: PlanExecutionStore) -> None:
    """写进文件的键必须**精确**等于白名单（多一个就说明有人忘了同步）。"""
    store.save(_execution())

    payload = json.loads(store.path.read_text(encoding="utf-8"))

    assert set(payload) == {"version", "executions"}
    assert set(payload["executions"][0]) == {
        "plan_id",
        "status",
        "current_item_index",
        "items",
        "total_tiles",
        "completed_tiles",
        "downloaded_bytes",
    }
    assert set(payload["executions"][0]["items"][0]) == {
        "plan_item_id",
        "task_id",
        "status",
        "error",
    }


def test_a_new_field_must_be_added_to_the_whitelist(monkeypatch, store) -> None:
    """有人在模型里加字段却忘了同步白名单时，写盘当场失败（而不是静默存下来）。

    这是**自检**：正常路径永远不会触发，所以只能把 payload 生成器换掉来验它。
    """
    import app.planning.plan_execution_store as module

    def leaky(execution: PlanExecution) -> dict:
        payload = execution_to_payload(execution)
        payload["region_name"] = "顺手加的字段"
        return payload

    monkeypatch.setattr(module, "execution_to_payload", leaky)

    with pytest.raises(PlanExecutionStoreError, match="白名单外"):
        store.save(_execution())


def test_a_secret_looking_key_is_refused(monkeypatch, store) -> None:
    """字段名里出现禁词片段 → 拒绝写盘（第二道防线）。"""
    import app.planning.plan_execution_store as module

    def leaky(execution: PlanExecution) -> dict:
        payload = execution_to_payload(execution)
        payload["tk"] = "abcdef0123456789"
        return payload

    monkeypatch.setattr(module, "execution_to_payload", leaky)
    # 先把白名单自检让开，单独验禁词这一关
    monkeypatch.setattr(module, "_assert_no_extra_fields", lambda execution: None)

    with pytest.raises(PlanExecutionStoreError, match="疑似密钥"):
        store.save(_execution())
    assert not store.path.exists(), "拒绝写盘时不该留下半个文件"


def test_the_forbidden_key_tables_are_identical_everywhere() -> None:
    """三张表逐字相同 —— 它们必须一起改，否则某个文件会漏掉一种密钥。"""
    from app.planning import plan_execution_store, plan_store
    from app.tasks import task_store

    assert (
        plan_execution_store._FORBIDDEN_KEY_FRAGMENTS
        == plan_store._FORBIDDEN_KEY_FRAGMENTS
        == task_store._FORBIDDEN_KEY_FRAGMENTS
    )


def test_the_file_never_contains_a_token(store: PlanExecutionStore) -> None:
    store.save(_execution())

    text = store.path.read_text(encoding="utf-8")

    assert "token" not in text.lower()
    assert "tk" not in text.lower()


def test_the_secret_scan_walks_into_lists() -> None:
    """禁词扫描要能钻进数组里的对象（否则 ``items`` 里的字段没人管）。"""
    from app.planning.plan_execution_store import _assert_no_secret_keys

    with pytest.raises(PlanExecutionStoreError):
        _assert_no_secret_keys({"items": [{"api_key": "x"}]})

    with pytest.raises(PlanExecutionStoreError):
        _assert_no_secret_keys({"items": [{"nested": [{"secret": "x"}]}]})

    # 正常字段名不该误报（这是这一段最容易写坏的地方）
    _assert_no_secret_keys(
        {"plan_id": "p", "items": [{"task_id": "t", "error": None}]}
    )


# --------------------------------------------------------------------------- #
# 损坏与半坏（Spec §19）
# --------------------------------------------------------------------------- #
def test_a_corrupt_file_is_quarantined_not_deleted(store: PlanExecutionStore) -> None:
    """损坏 → 改名隔离（可逆），**绝不删除**。"""
    store.path.write_text("{这不是 JSON", encoding="utf-8")

    assert store.load_all() == []

    backups = [
        item.name for item in store.path.parent.iterdir() if ".corrupt" in item.name
    ]
    assert len(backups) == 1
    # 后缀是 ``.corrupt.<时间戳>`` —— 断言要用**前缀匹配**，不能写等号
    assert backups[0].startswith(store.path.name + ".corrupt")
    assert store.path.exists() is False or store.path.stat().st_size == 0


def test_a_non_object_payload_is_quarantined(store: PlanExecutionStore) -> None:
    store.path.write_text("[1, 2, 3]", encoding="utf-8")

    assert store.load_all() == []
    assert any(".corrupt" in item.name for item in store.path.parent.iterdir())


def test_one_broken_record_does_not_take_down_the_others(
    store: PlanExecutionStore,
) -> None:
    """单条记录坏掉只跳过那一条 —— 一条坏了不该让别的计划"从来没跑过"。"""
    store.save(_execution("plan-good"))
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["executions"].append({"status": "running"})  # 缺 plan_id
    payload["executions"].append("这不是对象")
    store.path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    loaded = store.load_all()

    assert [item.plan_id for item in loaded] == ["plan-good"]


def test_a_broken_item_is_skipped_but_the_record_survives(
    store: PlanExecutionStore,
) -> None:
    store.save(_execution())
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["executions"][0]["items"].append({"task_id": "t-x"})  # 缺 plan_item_id
    payload["executions"][0]["items"].append("不是对象")
    store.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded = store.find("plan-1")

    assert loaded is not None
    assert loaded.item_ids() == ["i-1", "i-2"]


def test_items_not_a_list_drops_only_that_record(store: PlanExecutionStore) -> None:
    """``items`` 不是数组 → 那份记录整体作废，但别的记录不受影响。"""
    store.save(_execution("plan-1"))
    store.save(_execution("plan-2"))
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["executions"][0]["items"] = "不是数组"
    store.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded = store.load_all()

    assert [item.plan_id for item in loaded] == ["plan-2"]


def test_executions_not_a_list_means_nothing_to_load(store: PlanExecutionStore) -> None:
    """顶层 ``executions`` 不是数组 → 本次按空处理（不隔离文件：它还能解析）。"""
    store.save(_execution("plan-1"))
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["executions"] = "不是数组"
    store.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert store.load_all() == []
    assert store.path.exists(), "结构不对但仍是合法 JSON，不该被隔离掉"


def test_payload_to_execution_requires_a_plan_id() -> None:
    with pytest.raises(PlanExecutionStoreError, match="plan_id"):
        payload_to_execution({"status": "paused"})


def test_payload_to_execution_rejects_a_non_object() -> None:
    with pytest.raises(PlanExecutionStoreError, match="JSON 对象"):
        payload_to_execution("nope")


def test_a_write_failure_is_reported_clearly(work_dir: Path) -> None:
    """路径不可写时抛我们的异常类型（而不是裸 OSError）。"""
    # 拿一个**目录**当文件路径：打开它必定失败
    blocked = work_dir / "as-a-directory"
    blocked.mkdir()
    store = PlanExecutionStore(blocked)

    with pytest.raises(PlanExecutionStoreError, match="写入执行记录失败"):
        store.save(_execution())


# --------------------------------------------------------------------------- #
# 启动恢复（Spec §20）
# --------------------------------------------------------------------------- #
def test_unfinished_lists_what_the_user_may_continue(
    store: PlanExecutionStore,
) -> None:
    store.save(_execution("running", status=PlanExecutionStatus.RUNNING))
    store.save(_execution("paused", status=PlanExecutionStatus.PAUSED))
    store.save(_execution("partial", status=PlanExecutionStatus.PARTIAL_FAILED))
    store.save(_execution("done", status=PlanExecutionStatus.COMPLETED))
    store.save(_execution("cancelled", status=PlanExecutionStatus.CANCELLED))

    assert {item.plan_id for item in store.unfinished()} == {
        "running",
        "paused",
        "partial",
    }


def test_recovery_downgrades_running_to_paused_and_saves_it(
    store: PlanExecutionStore,
) -> None:
    """规则：遗留 ``RUNNING`` → ``PAUSED``，**立即落盘**，绝不自动联网。"""
    store.save(_execution("plan-1"))  # RUNNING

    execution, changed = store.load_for_recovery("plan-1")

    assert changed is True
    assert execution.status is PlanExecutionStatus.PAUSED
    assert execution.items[1].status is PlanItemRunStatus.PAUSED
    # 磁盘上也必须是 PAUSED：否则下次启动还要再降级一次，中间读到的都是假的
    assert store.find("plan-1").status is PlanExecutionStatus.PAUSED


def test_recovery_of_a_paused_execution_is_a_no_op(store: PlanExecutionStore) -> None:
    """幂等：整份计划与条目**都是** PAUSED 时 ``changed=False``。

    界面靠这个布尔值决定"要不要提示上次未完成" —— 每次启动都提示一遍是骚扰。
    """
    paused = _execution("plan-1", status=PlanExecutionStatus.PAUSED)
    paused.items[1].status = PlanItemRunStatus.PAUSED
    store.save(paused)

    execution, changed = store.load_for_recovery("plan-1")

    assert changed is False
    assert execution.status is PlanExecutionStatus.PAUSED


def test_recovery_also_downgrades_a_running_item(store: PlanExecutionStore) -> None:
    """计划与条目必须**一起**降级（只改一头会让界面显示"已暂停"却有一行在转圈）。"""
    half = _execution("plan-1", status=PlanExecutionStatus.PAUSED)
    # 条目仍是 RUNNING（程序被强杀时可能停在这一态）
    assert half.items[1].status is PlanItemRunStatus.RUNNING
    store.save(half)

    execution, changed = store.load_for_recovery("plan-1")

    assert changed is True
    assert execution.status is PlanExecutionStatus.PAUSED
    assert execution.items[1].status is PlanItemRunStatus.PAUSED
    assert store.find("plan-1").items[1].status is PlanItemRunStatus.PAUSED


def test_recovery_of_an_unknown_plan_is_empty(store: PlanExecutionStore) -> None:
    execution, changed = store.load_for_recovery("nope")

    assert execution is None
    assert changed is False
