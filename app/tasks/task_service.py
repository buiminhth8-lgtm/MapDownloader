"""任务生命周期服务（Slice 5）。

把"任务状态怎么变、什么时候落盘"收敛到一处，UI 只调这里的语义化方法
（``pause_task`` / ``complete_task`` …），不直接碰 :class:`TaskStore`。

状态机（Spec §10）
------------------

::

    CREATED ──► CALCULATING ──► DOWNLOADING ──► COMPLETED
                                    │  ▲
                                    ▼  │
                                  PAUSED
                                    │
                                    ▼
                              FAILED / CANCELLED

程序启动时如果发现上次记录仍是 ``DOWNLOADING``（或其它"运行中"状态），
说明上次是**异常退出**（被任务管理器杀掉、断电、崩溃）。这类记录一律降级为
``PAUSED``，并且**绝不自动开始下载**——用户的网络配额不该被一个隐式行为消耗掉。

进度落盘的节流（Spec §13）
--------------------------

每张瓦片都写一次 JSON 是不可接受的：几十万张的任务会产生几十万次 fsync，
磁盘与下载互相抢时间。这里的规则是"满足任一即写"：

- 距上次写入 >= ``DEFAULT_TASK_SAVE_INTERVAL_SECONDS``（1 秒），或
- 成功张数自上次写入以来增加 >= ``DEFAULT_TASK_SAVE_TILE_INTERVAL``（100 张）。

而**状态变化一律立即写**（开始/暂停/继续/取消/失败/完成）：那才是"如果丢了
会让人判断错误"的信息，而进度数字丢一秒的量完全无所谓。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from pathlib import Path

from app.config.settings import (
    DEFAULT_TASK_SAVE_INTERVAL_SECONDS,
    DEFAULT_TASK_SAVE_TILE_INTERVAL,
    DEFAULT_TASKS_FILE,
)
from app.geo import DownloadRegion, GeoBounds
from app.models.download_task import DownloadTask, DownloadTaskStatus, new_task_id
from app.models.map_layer import MapLayerType
from app.tasks.task_store import TaskStore

__all__ = ["TaskService"]


_logger = logging.getLogger(__name__)


class TaskService:
    """任务的增删改查 + 状态流转 + 节流落盘。

    无状态之外的一点点状态就是节流用的时间戳/计数表，键是任务 id。
    """

    def __init__(
        self,
        store: TaskStore | None = None,
        *,
        save_interval_seconds: float = DEFAULT_TASK_SAVE_INTERVAL_SECONDS,
        save_tile_interval: int = DEFAULT_TASK_SAVE_TILE_INTERVAL,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            store: 任务存储；``None`` 用默认的 ``~/MapDownloader/data/tasks.json``。
            save_interval_seconds: 进度落盘的时间间隔（秒）。
            save_tile_interval: 进度落盘的张数间隔。
            logger: 可选日志器。
        """
        self._store = store or TaskStore(DEFAULT_TASKS_FILE)
        self._save_interval = max(0.0, float(save_interval_seconds))
        self._save_tile_interval = max(1, int(save_tile_interval))
        self._logger = logger or _logger
        #: 上一次落盘的时间与张数，键为任务 id
        self._last_saved_at: dict[str, float] = {}
        self._last_saved_tiles: dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # 只读
    # ------------------------------------------------------------------ #
    @property
    def store(self) -> TaskStore:
        """底层存储（测试与诊断用）。"""
        return self._store

    def list_tasks(self) -> list[DownloadTask]:
        """全部任务（按更新时间倒序）。"""
        return self._store.list_all()

    def get_task(self, task_id: str) -> DownloadTask | None:
        """按 id 取任务。"""
        return self._store.get(task_id)

    # ------------------------------------------------------------------ #
    # 创建 / 删除
    # ------------------------------------------------------------------ #
    def create_task(
        self,
        *,
        name: str,
        bounds: GeoBounds,
        min_zoom: int,
        max_zoom: int,
        map_layer: MapLayerType | str,
        output_path: str | Path,
        total_tiles: int = 0,
        with_labels: bool = False,
        region: DownloadRegion | None = None,
        task_id: str | None = None,
    ) -> DownloadTask:
        """建一条新任务并**立即落盘**。

        Args:
            name: 任务名（一般等于地图名称）。
            bounds: 下载范围。
            min_zoom / max_zoom: 缩放区间。
            map_layer: 图层（枚举或 ``MapLayerType.value`` 字符串）。
            output_path: 目标 ``.mbtiles``。
            total_tiles: 目标瓦片数（可由 Tile Math 预算得到；未知时留 0）。
            with_labels: 本次是否把中文注记合成进瓦片（Slice 6）。
                必须落盘：续传时要靠它比对已有 MBTiles 的 ``composition``，
                否则"合成任务续到一个纯底图文件上"会被静默放行。
            region: 下载区域（Slice 16）。**只落盘标识**（类型 / id / 版本），
                边界 GeoJSON 本身不进 ``tasks.json``；续传时按 id 回预设表找文件。
            task_id: 指定 id（测试用；正常不传）。

        Returns:
            新建的任务（状态为 ``CREATED``）。

        Raises:
            ValueError: bounds 类型不对。
            TaskStoreError: 落盘失败。
        """
        if not isinstance(bounds, GeoBounds):
            raise ValueError(f"bounds 必须是 GeoBounds，实际为 {type(bounds).__name__}")

        layer_value = (
            map_layer.value if isinstance(map_layer, MapLayerType) else str(map_layer)
        )
        task = DownloadTask(
            id=task_id or new_task_id(),
            name=str(name),
            status=DownloadTaskStatus.CREATED,
            map_layer=layer_value,
            west=bounds.west,
            south=bounds.south,
            east=bounds.east,
            north=bounds.north,
            min_zoom=int(min_zoom),
            max_zoom=int(max_zoom),
            output_path=Path(output_path),
            total_tiles=max(0, int(total_tiles)),
            with_labels=bool(with_labels),
            region_type=region.region_type.value if region else "",
            region_id=(region.region_id or "") if region else "",
            boundary_version=(region.boundary_version or "") if region else "",
        )
        self._store.save(task)
        self._mark_saved(task)
        self._logger.info(
            "Task created id=%s name=%s layer=%s labels=%s tiles=%d",
            task.id,
            task.name,
            task.map_layer,
            task.with_labels,
            task.total_tiles,
        )
        return task

    def delete_task(self, task_id: str) -> bool:
        """删除任务**记录**（不动 ``.mbtiles``）。

        Returns:
            ``True`` 表示删掉了一条；``False`` 表示本来就没有。
        """
        removed = self._store.delete(task_id)
        self._last_saved_at.pop(task_id, None)
        self._last_saved_tiles.pop(task_id, None)
        if removed:
            self._logger.info("Task record deleted id=%s", task_id)
        return removed

    # ------------------------------------------------------------------ #
    # 状态流转（全部立即落盘）
    # ------------------------------------------------------------------ #
    def set_status(
        self,
        task: DownloadTask,
        status: DownloadTaskStatus,
        *,
        error: str | None = None,
    ) -> DownloadTask:
        """改变状态并立即写盘。"""
        task.update_status(status)
        if error is not None:
            task.error = error
        self._store.save(task)
        self._mark_saved(task)
        self._logger.info(
            "Task persisted id=%s status=%s completed=%d/%d",
            task.id,
            status.value,
            task.completed_tiles,
            task.total_tiles,
        )
        return task

    def start_downloading(
        self, task: DownloadTask, *, total_tiles: int | None = None
    ) -> DownloadTask:
        """进入下载态（``DOWNLOADING``）。"""
        if total_tiles is not None:
            task.total_tiles = max(0, int(total_tiles))
        task.error = None
        return self.set_status(task, DownloadTaskStatus.DOWNLOADING)

    def pause_task(self, task: DownloadTask) -> DownloadTask:
        """暂停（``PAUSED``）——退出/暂停时调用，任务可以下次继续。"""
        return self.set_status(task, DownloadTaskStatus.PAUSED)

    def resume_task(self, task: DownloadTask) -> DownloadTask:
        """继续（``DOWNLOADING``）——从暂停/失败态恢复下载。"""
        task.error = None
        return self.set_status(task, DownloadTaskStatus.DOWNLOADING)

    def cancel_task(self, task: DownloadTask) -> DownloadTask:
        """取消（``CANCELLED``）。已写入的瓦片保留，可再次继续。"""
        return self.set_status(task, DownloadTaskStatus.CANCELLED)

    def complete_task(self, task: DownloadTask) -> DownloadTask:
        """完成（``COMPLETED``）。**只在 §19 的完成判定成立时调用。**"""
        task.completed_tiles = max(task.completed_tiles, task.total_tiles)
        return self.set_status(task, DownloadTaskStatus.COMPLETED)

    def fail_task(self, task: DownloadTask, error: str) -> DownloadTask:
        """失败（``FAILED``）并保存原因，供用户判断"要不要再继续一次"。"""
        return self.set_status(task, DownloadTaskStatus.FAILED, error=str(error))

    # ------------------------------------------------------------------ #
    # 进度（节流落盘）
    # ------------------------------------------------------------------ #
    def update_progress(self, task: DownloadTask, progress) -> bool:
        """把引擎进度写进任务，并按节流规则决定要不要落盘。

        Args:
            task: 任务对象（会被就地更新）。
            progress: 引擎进度快照（鸭子类型，见 ``DownloadTask.apply_progress``）。

        Returns:
            ``True`` 表示本次真的写了盘。
        """
        task.apply_progress(progress)
        if not self._should_save(task):
            return False
        self.save_now(task)
        return True

    def save_now(self, task: DownloadTask) -> None:
        """立即落盘（不受节流限制）。"""
        task.updated_at = _now()
        self._store.save(task)
        self._mark_saved(task)

    def _should_save(self, task: DownloadTask) -> bool:
        """节流判定：时间到了，或张数涨够了。"""
        last_at = self._last_saved_at.get(task.id)
        if last_at is None:
            return True
        if time.monotonic() - last_at >= self._save_interval:
            return True
        last_tiles = self._last_saved_tiles.get(task.id, 0)
        return task.completed_tiles - last_tiles >= self._save_tile_interval

    def _mark_saved(self, task: DownloadTask) -> None:
        """记录"刚刚写过盘"的时间与张数。"""
        self._last_saved_at[task.id] = time.monotonic()
        self._last_saved_tiles[task.id] = task.completed_tiles

    # ------------------------------------------------------------------ #
    # 启动恢复
    # ------------------------------------------------------------------ #
    def recover_interrupted_tasks(self) -> list[DownloadTask]:
        """把上次异常退出遗留的"运行中"任务降级为 ``PAUSED``。

        Returns:
            被改动的任务列表（空列表表示上次是干净退出）。

        **绝不自动开始下载**：只改状态。用户打开程序看到的是"有 3 个任务可以
        继续"，而不是"程序自己开始跑了几十万张瓦片"。
        """
        recovered: list[DownloadTask] = []
        for task in self._store.list_all():
            if not task.is_interrupted:
                continue
            previous = task.status
            task.status = DownloadTaskStatus.PAUSED
            task.updated_at = _now()
            self._store.save(task)
            self._mark_saved(task)
            recovered.append(task)
            self._logger.info(
                "Task recovered id=%s %s -> paused（程序上次未正常收尾）",
                task.id,
                previous.value,
            )
        if recovered:
            self._logger.warning(
                "检测到 %d 个任务在程序退出时仍处于运行中，已标记为「已暂停」，"
                "不会自动开始下载",
                len(recovered),
            )
        return recovered

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    def count_tasks(self) -> int:
        """任务记录条数。"""
        return len(self._store.list_all())


def _now():
    """当前时间（抽成函数便于测试打桩）。"""
    from datetime import datetime

    return datetime.now()

