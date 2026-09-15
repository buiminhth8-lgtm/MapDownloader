"""下载任务持久化（Slice 5）。

**一个 JSON 文件，一张表**：``~/MapDownloader/data/tasks.json``。

为什么是 JSON 而不是 SQLite
---------------------------

Spec §8 明确要求本 Slice **不引入任务数据库**。算一下量级就知道这个约束是
对的：一个用户手里通常只有个位数到几十个下载任务，一个任务几十个字段——
总量是"几 KB"，用不到任何索引、事务或并发能力。为了几十行数据引一个
SQLite 依赖，换来的是"多一个需要迁移的 schema"和"多一个可能被写坏的库"。

JSON 的代价是"没有事务"，所以原子性必须自己保证（见 :meth:`TaskStore.save_all`）。

原子写
------

::

    tasks.json.tmp   ←  先写完整内容 + flush + fsync
         ↓  os.replace()      （同目录改名，POSIX/NTFS 上都是原子的）
    tasks.json

直接覆盖写 ``tasks.json`` 的问题是：进程在写到一半时被强杀（用户点关机、
任务管理器结束进程），磁盘上就留下一个截断的 JSON——**所有任务记录一起丢**。
先写临时文件再改名则保证任意时刻的 ``tasks.json`` 要么是旧的完整版本、
要么是新的完整版本，不存在中间态。

损坏文件的处理
--------------

读取到不合法的 JSON 时**不删用户文件**，而是改名成 ``tasks.json.corrupt<时间戳>``
后返回空列表：用户的任务记录还有救（手工修一下就能恢复），
而程序也能继续正常使用，不会反复在一个坏文件上炸。

Token 安全（Spec §9）
---------------------

本模块**只序列化白名单字段**，并且额外做一次"字段名黑名单"自检：
只要 payload 里出现了形如 token/tk/secret/password 的键，直接抛
:class:`TaskStoreError` 而不是写盘。白名单是第一道防线（正常写不出问题），
黑名单是第二道——防止将来有人"顺手"往 payload 里加了个 ``token`` 字段，
而这种错误一旦上线就是密钥泄漏。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config.settings import (
    DEFAULT_TASKS_FILE,
    TASKS_CORRUPT_SUFFIX,
    TASKS_FILE_VERSION,
)
from app.models.download_task import DownloadTask, DownloadTaskStatus

__all__ = ["TaskStore", "TaskStoreError", "task_from_payload", "task_to_payload"]


_logger = logging.getLogger(__name__)


class TaskStoreError(RuntimeError):
    """任务记录无法读写（路径不可写、JSON 损坏到无法解析、字段非法等）。"""


#: 允许出现在 payload 里的键。**白名单**：只有这里列出的字段会被写盘。
_TASK_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "status",
    "map_layer",
    "map_type",
    "with_labels",
    "west",
    "south",
    "east",
    "north",
    "min_zoom",
    "max_zoom",
    "output_path",
    "total_tiles",
    "completed_tiles",
    "succeeded_tiles",
    "failed_tiles",
    "skipped_tiles",
    "created_at",
    "updated_at",
    "error",
)

#: 字段名里出现这些片段即视为"疑似密钥"，直接拒绝写盘（第二道防线）。
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


def task_to_payload(task: DownloadTask) -> dict[str, Any]:
    """把任务转成可 JSON 序列化的 dict（只含白名单字段）。

    时间用 ISO-8601 字符串、路径用 ``str``、状态用枚举的 ``value``：
    目标是"打开 tasks.json 用记事本就能看懂"，而不是"序列化最快"。
    """
    payload: dict[str, Any] = {
        "id": task.id,
        "name": task.name,
        "status": task.status.value,
        "map_layer": task.map_layer,
        "map_type": task.map_type,
        "with_labels": bool(task.with_labels),
        "west": float(task.west),
        "south": float(task.south),
        "east": float(task.east),
        "north": float(task.north),
        "min_zoom": int(task.min_zoom),
        "max_zoom": int(task.max_zoom),
        "output_path": str(task.output_path),
        "total_tiles": int(task.total_tiles),
        "completed_tiles": int(task.completed_tiles),
        "succeeded_tiles": int(task.succeeded_tiles),
        "failed_tiles": int(task.failed_tiles),
        "skipped_tiles": int(task.skipped_tiles),
        "created_at": _format_time(task.created_at),
        "updated_at": _format_time(task.updated_at),
        "error": task.error,
    }
    extra = set(payload) - set(_TASK_FIELDS)
    if extra:
        # 白名单自检：将来有人往 payload 里加字段却忘了同步 _TASK_FIELDS 时，
        # 会在第一次写盘就炸，而不是把一个未审计的字段悄悄写进磁盘。
        raise TaskStoreError(f"任务 payload 出现白名单外的字段：{sorted(extra)}")
    return payload

def task_from_payload(payload: dict[str, Any]) -> DownloadTask:
    """从 dict 还原任务。

    Raises:
        TaskStoreError: 缺关键字段、状态字符串不认识、类型不对。
    """
    if not isinstance(payload, dict):
        raise TaskStoreError(f"任务记录必须是对象，实际为 {type(payload).__name__}")

    task_id = str(payload.get("id") or "").strip()
    if not task_id:
        raise TaskStoreError("任务记录缺少 id")

    raw_status = payload.get("status")
    try:
        status = DownloadTaskStatus(str(raw_status))
    except ValueError as exc:
        raise TaskStoreError(
            f"任务 {task_id} 的状态无法识别：{raw_status!r}"
        ) from exc

    task = DownloadTask(id=task_id, status=status)
    task.name = str(payload.get("name") or task.name)
    task.map_layer = str(payload.get("map_layer") or "")
    task.map_type = str(payload.get("map_type") or "")
    # 缺这个键的老记录 = Slice 5 时代的单层任务，False 正是当时的真实情况
    task.with_labels = _as_bool(payload.get("with_labels", False), "with_labels", task_id)

    task.west = _as_float(payload.get("west"), "west", task_id)
    task.south = _as_float(payload.get("south"), "south", task_id)
    task.east = _as_float(payload.get("east"), "east", task_id)
    task.north = _as_float(payload.get("north"), "north", task_id)
    task.min_zoom = _as_int(payload.get("min_zoom"), "min_zoom", task_id)
    task.max_zoom = _as_int(payload.get("max_zoom"), "max_zoom", task_id)

    output_path = payload.get("output_path")
    if output_path:
        task.output_path = Path(str(output_path))

    task.total_tiles = _as_int(payload.get("total_tiles", 0), "total_tiles", task_id)
    task.completed_tiles = _as_int(
        payload.get("completed_tiles", 0), "completed_tiles", task_id
    )
    task.succeeded_tiles = _as_int(
        payload.get("succeeded_tiles", 0), "succeeded_tiles", task_id
    )
    task.failed_tiles = _as_int(payload.get("failed_tiles", 0), "failed_tiles", task_id)
    task.skipped_tiles = _as_int(
        payload.get("skipped_tiles", 0), "skipped_tiles", task_id
    )

    created = payload.get("created_at")
    if created:
        task.created_at = _parse_time(created, task_id, "created_at")
    updated = payload.get("updated_at")
    if updated:
        task.updated_at = _parse_time(updated, task_id, "updated_at")

    error = payload.get("error")
    task.error = str(error) if error else None
    return task


def _format_time(value: datetime) -> str:
    """``datetime`` → ISO-8601（秒精度，够用且短）。"""
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    return str(value)


def _parse_time(value: Any, task_id: str, field: str) -> datetime:
    """ISO-8601 → ``datetime``；解析不了就报"记录损坏"而不是静默用当前时间。"""
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise TaskStoreError(
            f"任务 {task_id} 的 {field} 不是合法时间：{value!r}"
        ) from exc


def _as_float(value: Any, field: str, task_id: str) -> float:
    """宽松转 float（JSON 里可能是字符串）。"""
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise TaskStoreError(
            f"任务 {task_id} 的 {field} 不是数字：{value!r}"
        ) from exc


def _as_int(value: Any, field: str, task_id: str) -> int:
    """宽松转 int（JSON 里可能是 ``8.0`` 或 ``"8"``）。"""
    try:
        return int(float(value))
    except (TypeError, ValueError) as exc:
        raise TaskStoreError(
            f"任务 {task_id} 的 {field} 不是整数：{value!r}"
        ) from exc


def _as_bool(value: Any, field: str, task_id: str) -> bool:
    """转 bool，并**拒绝**字符串 ``"false"`` 被当成真。

    ``bool("false")`` 是 ``True``——直接拿它读 JSON 会在有人手工把
    ``"false"``（带引号）写进 tasks.json 时把单层任务当成合成任务，
    进而让续传的一致性校验放行一份"一半有字一半没字"的地图。
    因此这里只认真正的布尔与 ``true`` / ``false`` 文本。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text == "true":
        return True
    if text == "false" or text == "":
        return False
    raise TaskStoreError(f"任务 {task_id} 的 {field} 不是布尔值：{value!r}")


def _assert_no_secret_keys(payload: Any, *, where: str = "payload") -> None:
    """递归检查 payload 的**键名**，命中疑似密钥片段即拒绝。

    只查键不查值：值可能是用户给地图起的名字（叫 "token 试验" 也完全合理），
    而键名是我们自己控制的——键名里出现 token 说明有人正在往盘上写密钥。
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).lower()
            for fragment in _FORBIDDEN_KEY_FRAGMENTS:
                if fragment in lowered:
                    raise TaskStoreError(
                        f"拒绝把疑似密钥字段写入任务记录：{where}.{key}"
                        f"（Token 不得落盘，Spec §9）"
                    )
            _assert_no_secret_keys(value, where=f"{where}.{key}")
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            _assert_no_secret_keys(item, where=f"{where}[{index}]")


class TaskStore:
    """任务记录的 JSON 读写。

    用法::

        store = TaskStore()                 # 默认 ~/MapDownloader/data/tasks.json
        store.save(task)
        tasks = store.list_all()
        store.delete(task.id)

    本类**无状态**（除了路径）：每次读都重新解析文件，每次写都整体重写。
    几十行数据的场景下，"内存里缓存一份"带来的只是一致性风险，没有收益。
    """

    def __init__(
        self,
        path: str | Path = DEFAULT_TASKS_FILE,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            path: 任务文件路径。父目录会在首次写入时自动创建。
            logger: 可选日志器。
        """
        self._path = Path(path)
        self._logger = logger or _logger

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    @property
    def path(self) -> Path:
        """任务文件路径。"""
        return self._path

    @property
    def exists(self) -> bool:
        """文件当前是否存在。"""
        return self._path.is_file()

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def load_all(self) -> list[DownloadTask]:
        """读取全部任务。

        Returns:
            任务列表（按 ``updated_at`` 倒序，最新的在最前）。
            文件不存在、或内容损坏到无法解析时返回空列表——
            **本方法不抛异常**：一个坏掉的任务文件不应该让程序起不来。
        """
        payload = self._read_payload()
        if payload is None:
            return []

        records = payload.get("tasks")
        if not isinstance(records, list):
            self._quarantine("tasks 字段不是数组")
            return []

        tasks: list[DownloadTask] = []
        skipped = 0
        for record in records:
            try:
                tasks.append(task_from_payload(record))
            except TaskStoreError as exc:
                # 单条记录坏了不应该拖累其他记录：跳过并计数，最后汇总一条日志。
                skipped += 1
                self._logger.warning("跳过一条无法解析的任务记录：%s", exc)
        if skipped:
            self._logger.warning("任务文件中有 %d 条记录无法解析，已跳过", skipped)

        tasks.sort(key=lambda item: item.updated_at, reverse=True)
        return tasks

    def list_all(self) -> list[DownloadTask]:
        """:meth:`load_all` 的别名（Spec §8 的接口名）。"""
        return self.load_all()

    def get(self, task_id: str) -> DownloadTask | None:
        """按 id 取任务；不存在返回 ``None``。"""
        target = str(task_id)
        for task in self.load_all():
            if task.id == target:
                return task
        return None

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    def save(self, task: DownloadTask) -> None:
        """新增或覆盖一个任务（按 id 匹配），整体重写文件。"""
        self.save_all([task], merge=True)

    def save_all(
        self, tasks: list[DownloadTask], *, merge: bool = True
    ) -> list[DownloadTask]:
        """写入一批任务。

        Args:
            tasks: 要写入的任务。
            merge: ``True`` 时与文件里现有记录合并（同名 id 覆盖）；
                ``False`` 时**整体替换**（用于删除等需要精确控制内容的操作）。

        Returns:
            写入后的完整任务列表（已排序）。

        Raises:
            TaskStoreError: 路径不可写，或 payload 里出现疑似密钥字段。
        """
        existing: dict[str, DownloadTask] = {}
        if merge:
            for item in self.load_all():
                existing[item.id] = item
        for task in tasks:
            existing[task.id] = task

        payload = {
            "version": TASKS_FILE_VERSION,
            "tasks": [task_to_payload(item) for item in existing.values()],
        }
        # 第二道防线：写盘之前再自检一次字段名
        _assert_no_secret_keys(payload)
        self._write_payload(payload)
        return self.load_all()

    def delete(self, task_id: str) -> bool:
        """删除一条任务记录。

        **只删记录，不碰 ``.mbtiles``**（Spec §20）：地图文件是用户的资产，
        删它必须是一次明确的、单独的操作。

        Returns:
            ``True`` 表示确实删掉了一条记录；``False`` 表示本来就没有。
        """
        target = str(task_id)
        current = self.load_all()
        remaining = [task for task in current if task.id != target]
        if len(remaining) == len(current):
            return False
        payload = {
            "version": TASKS_FILE_VERSION,
            "tasks": [task_to_payload(item) for item in remaining],
        }
        _assert_no_secret_keys(payload)
        self._write_payload(payload)
        return True

    # ------------------------------------------------------------------ #
    # 内部：读写
    # ------------------------------------------------------------------ #
    def _read_payload(self) -> dict[str, Any] | None:
        """读文件并解析 JSON；不存在/损坏时返回 ``None``。"""
        if not self._path.is_file():
            return None
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            self._logger.error("读取任务文件失败：%s", exc)
            return None
        if not text.strip():
            # 空文件（上次写到一半被打断的极端情况）：当"没有任务"处理。
            self._logger.warning("任务文件为空：%s", self._path)
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
            TaskStoreError: 目录创建失败 / 写入失败。
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TaskStoreError(f"无法创建任务目录 {self._path.parent}：{exc}") from exc

        tmp_path = self._tmp_path()
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
        try:
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.write("\n")
                handle.flush()
                # fsync 是这里唯一"贵"的一步：它保证内容真的落到磁盘，
                # 而不只是进了操作系统的页缓存——否则断电后 os.replace
                # 可能把一个内容为空的新文件改名到位。
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._path)
        except OSError as exc:
            # 尽力清掉半成品临时文件，避免留一个 .tmp 让用户困惑
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:  # pragma: no cover - 清理失败不影响主流程
                pass
            raise TaskStoreError(f"写入任务文件失败 {self._path}：{exc}") from exc

    def _tmp_path(self) -> Path:
        """临时文件路径：同一目录下的 ``tasks.json.tmp``。

        必须**同目录**：``os.replace`` 只有跨同一文件系统才是原子改名，
        放到 ``%TEMP%`` 再改名有可能变成"复制 + 删除"，原子性就没了。
        """
        return self._path.with_name(self._path.name + ".tmp")

    def _quarantine(self, reason: str) -> Path | None:
        """把损坏的任务文件改名隔离（不删除），返回新路径。

        刻意**不删**：里面可能还有用户手工加过的备注，或者值得抢救的部分。
        加时间戳是为了连续坏两次时不会互相覆盖。
        """
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self._path.with_name(
            f"{self._path.name}{TASKS_CORRUPT_SUFFIX}.{stamp}"
        )
        try:
            os.replace(self._path, backup)
        except OSError as exc:  # pragma: no cover - 隔离失败也要让主流程继续
            self._logger.error("任务文件损坏（%s）且隔离失败：%s", reason, exc)
            return None
        self._logger.error(
            "任务文件损坏（%s），已隔离为 %s，本次按「没有任务」处理", reason, backup
        )
        return backup
