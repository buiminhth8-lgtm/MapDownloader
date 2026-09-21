"""计划执行记录的持久化（Slice 21，Spec §19 / §20 / §21）。

``data/plan_executions.json`` —— 第三个文件，也只装一件事：**这份计划跑到哪了**。

::

    {
      "version": 1,
      "executions": [
        {
          "plan_id": "8f3c…",
          "status": "paused",
          "current_item_index": 1,
          "total_tiles": 400,
          "completed_tiles": 250,
          "downloaded_bytes": 1048576,
          "items": [
            {"plan_item_id": "a1", "task_id": "t1", "status": "completed", "error": null},
            {"plan_item_id": "b2", "task_id": "t2", "status": "paused",    "error": null}
          ]
        }
      ]
    }

三条与其它两个文件一致的红线
----------------------------

1. **白名单**：只有 :data:`_EXECUTION_FIELDS` / :data:`_ITEM_FIELDS` 里的键会写盘。
   将来有人往模型里加字段却忘了同步白名单时，``_assert_no_extra_fields`` 会
   在写盘前抛错（而不是悄悄把字段存下来）。
2. **禁词**：字段名里出现 ``token`` / ``tk`` / ``secret`` 之类片段直接拒绝写盘。
   这张表与 :mod:`app.tasks.task_store` / :mod:`app.planning.plan_store` 的
   **逐字相同**，有一条用例断言三张表相等 —— 它们必须一起改。
3. **损坏隔离**：JSON 解析不了 / 顶层结构不对时，把文件改名成
   ``plan_executions.json.corrupt.<时间戳>``（**不删除**），以空表继续启动。

原子写是 ``tmp → flush → fsync → os.replace``，与另外两个 store 一字不差：
执行记录在半截写入时被读到，会让界面显示"计划停在某个中间状态"，
而那个状态从来没存在过。

为什么状态变化与进度分两种写法（Spec §19）
-------------------------------------------

:meth:`PlanExecutionStore.save` 一律立即写盘（状态变化走这条）。
节流放在**调用方**（:class:`~app.planning.plan_scheduler.PlanScheduler`）：
只有它知道"这一帧是进度还是状态变化"。把节流塞进 store 会让 store 必须猜
调用方的意图 —— 而"猜错"的表现是"计划已经停了，文件里还写着正在跑"。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config.settings import (
    DEFAULT_PLAN_EXECUTIONS_FILE,
    PLAN_EXECUTIONS_CORRUPT_SUFFIX,
    PLAN_EXECUTIONS_FILE_VERSION,
)
from app.planning.plan_execution import (
    PlanExecution,
    PlanExecutionStatus,
    PlanItemExecution,
    PlanItemRunStatus,
    recover_running_execution,
)

__all__ = [
    "PlanExecutionStore",
    "PlanExecutionStoreError",
    "execution_to_payload",
    "payload_to_execution",
]


_logger = logging.getLogger(__name__)


class PlanExecutionStoreError(RuntimeError):
    """执行记录无法读写（路径不可写、JSON 损坏到无法解析、字段非法等）。"""


#: 允许出现在执行记录里的键。**白名单**。
#:
#: 注意这里**没有**任何 Token / 几何 / 瓦片坐标字段，也没有区域名与 Zoom：
#: 那些从 :class:`~app.planning.download_plan.DownloadPlan` 现查（Spec §3）。
_EXECUTION_FIELDS: tuple[str, ...] = (
    "plan_id",
    "status",
    "current_item_index",
    "items",
    "total_tiles",
    "completed_tiles",
    "downloaded_bytes",
)

#: 允许出现在**条目**里的键（Spec §3 的四个字段，一个不多）。
_ITEM_FIELDS: tuple[str, ...] = (
    "plan_item_id",
    "task_id",
    "status",
    "error",
)

#: 字段名里出现这些片段即视为"疑似密钥"，直接拒绝写盘（第二道防线）。
#:
#: 与 :data:`app.tasks.task_store._FORBIDDEN_KEY_FRAGMENTS`、
#: :data:`app.planning.plan_store._FORBIDDEN_KEY_FRAGMENTS` **逐字相同**
#: —— ``tests/test_plan_execution_store.py`` 里有一条用例断言三者相等，
#: 所以它们必须一起改。
_FORBIDDEN_KEY_FRAGMENTS: tuple[str, ...] = (
    "token",
    "tk",
    "secret",
    "password",
    "passwd",
    "apikey",
    "api_key",
    "credential",
)


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
def execution_to_payload(execution: PlanExecution) -> dict[str, Any]:
    """把执行记录转成可 JSON 序列化的 dict（只含白名单字段）。

    状态一律存枚举的 ``value``（小写字符串）：与其它两个文件同一约定 ——
    "打开 JSON 用记事本就能看懂"比"序列化最快"重要。
    """
    return {
        "plan_id": execution.plan_id,
        "status": execution.status.value,
        "current_item_index": int(execution.current_item_index),
        "total_tiles": int(execution.total_tiles),
        "completed_tiles": int(execution.completed_tiles),
        "downloaded_bytes": int(execution.downloaded_bytes),
        "items": [
            {
                "plan_item_id": item.plan_item_id,
                "task_id": item.task_id,
                "status": item.status.value,
                "error": item.error,
            }
            for item in execution.items
        ],
    }


def payload_to_execution(payload: object) -> PlanExecution:
    """把 JSON 里的一个对象解析成 :class:`PlanExecution`。

    Raises:
        PlanExecutionStoreError: 不是对象、缺 ``plan_id``、``items`` 不是数组。
            单条**条目**坏了只跳过那一条（与任务记录同一规矩）：一条坏记录
            不该让整份执行丢失，那样用户会看到"计划从来没跑过"。
    """
    if not isinstance(payload, dict):
        raise PlanExecutionStoreError(f"执行记录不是 JSON 对象：{type(payload).__name__}")
    plan_id = str(payload.get("plan_id") or "").strip()
    if not plan_id:
        raise PlanExecutionStoreError("执行记录缺少 plan_id")

    raw_items = payload.get("items", [])
    if not isinstance(raw_items, list):
        raise PlanExecutionStoreError("执行记录的 items 字段不是数组")

    items: list[PlanItemExecution] = []
    for entry in raw_items:
        parsed = _item_from_payload(entry)
        if parsed is not None:
            items.append(parsed)

    return PlanExecution(
        plan_id=plan_id,
        status=payload.get("status", PlanExecutionStatus.CREATED.value),
        current_item_index=_as_int(payload.get("current_item_index"), -1),
        items=items,
        total_tiles=_as_int(payload.get("total_tiles"), 0),
        completed_tiles=_as_int(payload.get("completed_tiles"), 0),
        downloaded_bytes=_as_int(payload.get("downloaded_bytes"), 0),
    )


def _item_from_payload(payload: object) -> PlanItemExecution | None:
    """解析一条条目；坏的那一条返回 ``None``（跳过而不是整份失败）。"""
    if not isinstance(payload, dict):
        _logger.warning("执行记录里有一条条目不是 JSON 对象，已跳过")
        return None
    plan_item_id = str(payload.get("plan_item_id") or "").strip()
    if not plan_item_id:
        _logger.warning("执行记录里有一条条目缺少 plan_item_id，已跳过")
        return None
    task_id = payload.get("task_id")
    error = payload.get("error")
    return PlanItemExecution(
        plan_item_id=plan_item_id,
        task_id=str(task_id) if task_id else None,
        status=payload.get("status", PlanItemRunStatus.WAITING.value),
        error=str(error) if error else None,
    )


def _as_int(value: object, default: int) -> int:
    """宽容转 int：读历史文件的那一侧不该因为一个字符串就整份失败。"""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _assert_no_extra_fields(execution: PlanExecution) -> None:
    """写盘前自检：payload 里的键必须都在白名单里。

    Raises:
        PlanExecutionStoreError: 出现了白名单外的字段 —— 说明有人加了模型字段
            却没同步白名单。抛错而不是静默丢弃：静默丢弃会让"我存了那个字段"
            变成一个只在重启后才发现的假象。
    """
    payload = execution_to_payload(execution)
    extra = set(payload) - set(_EXECUTION_FIELDS)
    if extra:
        raise PlanExecutionStoreError(f"执行记录出现白名单外的字段：{sorted(extra)}")
    for index, item in enumerate(payload["items"]):
        extra_item = set(item) - set(_ITEM_FIELDS)
        if extra_item:
            raise PlanExecutionStoreError(
                f"执行记录第 {index} 项出现白名单外的字段：{sorted(extra_item)}"
            )


def _assert_no_secret_keys(node: object, path: str = "") -> None:
    """递归扫字段名，命中禁词片段就抛错（Spec §19 的"不保存 Token"）。

    **按字段名扫，不按值扫**：值里出现 "token" 这种词可能只是文案
    （"Token 无效"），按值扫会把正常数据判成密钥。
    """
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            for fragment in _FORBIDDEN_KEY_FRAGMENTS:
                if fragment in lowered:
                    raise PlanExecutionStoreError(
                        f"执行记录里出现疑似密钥字段 {path}{key}（片段 {fragment}），拒绝写盘"
                    )
            _assert_no_secret_keys(value, f"{path}{key}.")
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            _assert_no_secret_keys(value, f"{path}[{index}].")


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class PlanExecutionStore:
    """``data/plan_executions.json`` 的读写。

    Attributes:
        path: 记录文件路径。
    """

    def __init__(
        self,
        path: str | Path = DEFAULT_PLAN_EXECUTIONS_FILE,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            path: 执行记录文件路径。测试务必传临时目录 —— 默认路径在用户目录下。
            logger: 可选日志器。
        """
        self._path = Path(path)
        self._logger = logger or _logger

    @property
    def path(self) -> Path:
        """记录文件路径。"""
        return self._path

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def load_all(self) -> list[PlanExecution]:
        """读出全部执行记录（文件不存在 / 损坏时返回空列表）。

        单条记录坏掉只跳过那一条：一份执行记录里可能有多个计划的记录，
        一条坏了不该让其它计划也变成"从来没跑过"。
        """
        payload = self._read_payload()
        if payload is None:
            return []
        raw = payload.get("executions", [])
        if not isinstance(raw, list):
            self._logger.error("执行记录文件的 executions 字段不是数组，本次按空处理")
            return []
        executions: list[PlanExecution] = []
        for entry in raw:
            try:
                executions.append(payload_to_execution(entry))
            except PlanExecutionStoreError as exc:
                self._logger.warning("跳过一条无效的执行记录：%s", exc)
        return executions

    def find(self, plan_id: str) -> PlanExecution | None:
        """按计划 id 取执行记录；没有返回 ``None``。"""
        wanted = str(plan_id or "").strip()
        for execution in self.load_all():
            if execution.plan_id == wanted:
                return execution
        return None

    def unfinished(self) -> list[PlanExecution]:
        """还"活着"或"可以继续"的执行记录（Spec §20 的启动检查）。

        包含 ``RUNNING``（上次异常退出留下的）、``PAUSED`` 与
        ``PARTIAL_FAILED``（停在一个失败项上，可以重试或跳过）。
        """
        return [
            execution
            for execution in self.load_all()
            if execution.status.is_active
            or execution.status is PlanExecutionStatus.PARTIAL_FAILED
        ]

    def load_for_recovery(self, plan_id: str) -> tuple[PlanExecution | None, bool]:
        """读出某计划的执行记录，并把遗留的 ``RUNNING`` 降级成 ``PAUSED``（Spec §20）。

        **只改状态、绝不联网**：调用方拿到的是"可以继续"的计划，而不是
        "已经开始跑了"的计划。改动会立即落盘 —— 否则下次启动还要再降级一次，
        而中间这段时间文件里写的是"正在跑"（假的）。

        Returns:
            ``(execution, changed)``；没有记录时是 ``(None, False)``。
        """
        execution = self.find(plan_id)
        if execution is None:
            return None, False
        execution, changed = recover_running_execution(execution)
        if changed:
            self.save(execution)
            self._logger.warning(
                "计划 %s 上次未正常收尾（记录里仍是运行中），已降级为「已暂停」，"
                "不会自动开始下载",
                plan_id,
            )
        return execution, changed

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    def save(self, execution: PlanExecution) -> PlanExecution:
        """按 ``plan_id`` 新增或覆盖一条执行记录（其余记录保持不动）。"""
        self.save_all([execution], merge=True)
        return execution

    def save_all(
        self, executions: list[PlanExecution], *, merge: bool = True
    ) -> list[PlanExecution]:
        """写入一批执行记录。

        Args:
            executions: 要写入的记录。
            merge: ``True`` 时与文件里现有的合并（同 ``plan_id`` 覆盖）；
                ``False`` 时整体替换（删除用）。

        Returns:
            写入后的完整列表。

        Raises:
            PlanExecutionStoreError: 路径不可写、payload 出现白名单外字段、
                或出现疑似密钥字段。
        """
        existing: dict[str, PlanExecution] = {}
        if merge:
            for item in self.load_all():
                existing[item.plan_id] = item
        for execution in executions:
            existing[execution.plan_id] = execution

        for execution in existing.values():
            _assert_no_extra_fields(execution)

        payload = {
            "version": PLAN_EXECUTIONS_FILE_VERSION,
            "executions": [
                execution_to_payload(item) for item in existing.values()
            ],
        }
        _assert_no_secret_keys(payload)
        self._write_payload(payload)
        return self.load_all()

    def delete(self, plan_id: str) -> bool:
        """删掉某计划的执行记录。

        **只删执行记录**：不碰 ``plans.json``（那是用户的配置）、
        不碰 ``tasks.json``、更不碰任何 ``.mbtiles``（那是下载成果）。

        Returns:
            ``True`` 表示确实删掉了一条。
        """
        target = str(plan_id or "").strip()
        current = self.load_all()
        remaining = [item for item in current if item.plan_id != target]
        if len(remaining) == len(current):
            return False
        payload = {
            "version": PLAN_EXECUTIONS_FILE_VERSION,
            "executions": [execution_to_payload(item) for item in remaining],
        }
        _assert_no_secret_keys(payload)
        self._write_payload(payload)
        self._logger.info("已删除计划 %s 的执行记录（下载成果与任务记录保持不动）", target)
        return True

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _read_payload(self) -> dict[str, Any] | None:
        """读文件并解析 JSON；不存在 / 损坏时返回 ``None``。"""
        if not self._path.is_file():
            return None
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            self._logger.error("读取执行记录文件失败：%s", exc)
            return None
        if not text.strip():
            self._logger.warning("执行记录文件为空：%s", self._path)
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            self._quarantine(f"JSON 解析失败：{exc}")
            return None
        if not isinstance(payload, dict):
            self._quarantine("顶层不是 JSON 对象")
            return None
        return payload

    def _write_payload(self, payload: dict[str, Any]) -> None:
        """原子写入：tmp → flush → fsync → ``os.replace``。

        Raises:
            PlanExecutionStoreError: 目录创建失败 / 写入失败。
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PlanExecutionStoreError(
                f"无法创建执行记录目录 {self._path.parent}：{exc}"
            ) from exc

        tmp_path = self._tmp_path()
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
        try:
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._path)
        except OSError as exc:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:  # pragma: no cover - 清理失败不影响主流程
                pass
            raise PlanExecutionStoreError(
                f"写入执行记录失败 {self._path}：{exc}"
            ) from exc

    def _tmp_path(self) -> Path:
        """临时文件路径：同目录下的 ``plan_executions.json.tmp``。"""
        return self._path.with_name(self._path.name + ".tmp")

    def _quarantine(self, reason: str) -> Path | None:
        """把损坏的记录文件改名隔离（**不删除**），返回新路径。"""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self._path.with_name(
            f"{self._path.name}{PLAN_EXECUTIONS_CORRUPT_SUFFIX}.{stamp}"
        )
        try:
            os.replace(self._path, backup)
        except OSError as exc:  # pragma: no cover - 隔离失败也要让主流程继续
            self._logger.error("执行记录损坏（%s）且隔离失败：%s", reason, exc)
            return None
        self._logger.warning(
            "执行记录文件损坏（%s），已隔离为 %s（不删除用户文件）", reason, backup.name
        )
        return backup
