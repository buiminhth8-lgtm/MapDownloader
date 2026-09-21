"""计划调度相关的测试替身（Slice 21）。

为什么单独一个模块
------------------

计划级调度的两层 —— :class:`~app.planning.plan_scheduler.PlanScheduler` 与
:class:`~app.workers.plan_download_worker.PlanDownloadWorker` —— 都需要同一个
"假执行者"：它**不联网、不建 MBTiles**，但会**模仿引擎的记账动作**（把进度
写进任务记录并落盘），因为调度器的进度聚合读的正是任务记录。两份测试各写
一个假执行者的话，这条"记账"约定就有了两个版本，改一处忘一处就会让某个文件
的断言退化成在测空气（本项目的 ``tests/android_fakes.py`` 是同一套理由）。

所以这里放**唯一一份**替身，两个测试文件共用。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.planning.plan_execution import PlanItemRunStatus
from app.planning.plan_scheduler import PlanTaskOutcome

__all__ = [
    "ExplodingRunner",
    "FakeRunner",
    "ProgressLike",
    "ready_plan",
]


@dataclass
class ProgressLike:
    """引擎进度快照的替身（调度器只读 ``percent`` 与 ``tiles_per_second``）。"""

    total: int = 0
    completed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    current_zoom: int = 0
    tiles_per_second: float = 0.0

    @property
    def percent(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(100.0, self.completed / self.total * 100.0)


class FakeRunner:
    """可控的假执行者。

    Args:
        fail_on: 这些 ``region_id`` 的任务会失败。
        fail_times: 每个失败区域**失败几次**后转为成功（``0`` = 一直失败）。
            用来测「重试当前」：第一次失败，重试的一次成功。
        hang: ``True`` 时任务一直挂着，直到收到停止请求被放行（测暂停 / 取消）。
        hang_once: 只有**第一次**调用会挂（测"暂停后继续，同一条接着跑完"）。
        stop_once: 第一次调用**自己**返回"被停下"（不经过停止请求）。
            用来造出"没有任何人点暂停，条目却停在 PAUSED"的情形 ——
            调度器必须把它收成"部分失败"而不是原地重跑（死循环）。
    """

    def __init__(
        self,
        *,
        fail_on: tuple[str, ...] = (),
        fail_times: int = 0,
        hang: bool = False,
        hang_once: bool = False,
        stop_once: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self.max_concurrent = 0
        self.stopped_calls = 0
        self.completed_calls = 0
        self._active = 0
        self._stop_requested = False
        self._gate: asyncio.Event | None = None
        self._fail_on = set(fail_on)
        self._fail_times = int(fail_times)
        self._failures: dict[str, int] = {}
        self._hang = bool(hang)
        self._hang_once = bool(hang_once)
        self._stop_once = bool(stop_once)
        self._runs = 0

    # --- 被调用 ------------------------------------------------------- #
    async def run(
        self,
        task: Any,
        *,
        token: str = "",
        on_progress=None,
        task_service: Any = None,
    ) -> PlanTaskOutcome:
        """把一条任务"跑"完（见类文档：会记账，但不联网）。"""
        self._stop_requested = False
        self._gate = asyncio.Event()
        self._active += 1
        self._runs += 1
        self.max_concurrent = max(self.max_concurrent, self._active)
        region = str(getattr(task, "region_id", "") or "")
        self.calls.append(region)
        total = max(1, int(getattr(task, "total_tiles", 0) or 0))
        try:
            if task_service is not None:
                task_service.start_downloading(task)
            if on_progress is not None:
                on_progress(
                    ProgressLike(
                        total=total, completed=total // 2, tiles_per_second=7.5
                    )
                )

            if self._stop_once and self._runs == 1:
                # 引擎"自己"停了下来（没人点暂停 / 取消）——半截的成果留在磁盘上
                self.stopped_calls += 1
                self._touch_output(task)
                return PlanTaskOutcome(
                    status=PlanItemRunStatus.CANCELLED,
                    completed_tiles=total // 2,
                    total_tiles=total,
                    downloaded_bytes=0,
                    stopped=True,
                )

            if self._hang or (self._hang_once and self._runs == 1):
                assert self._gate is not None
                await self._gate.wait()
                if self._stop_requested:
                    # 被"停下来"：半截的 MBTiles 留在磁盘上（取消时"不删除"那条要看它）
                    self.stopped_calls += 1
                    self._touch_output(task)
                    return PlanTaskOutcome(
                        status=PlanItemRunStatus.CANCELLED,
                        completed_tiles=total // 2,
                        total_tiles=total,
                        downloaded_bytes=0,
                        stopped=True,
                    )
                # 被 :meth:`release` 放行 → 当成一次正常跑完（用来模拟"进程被强行重启"）

            half = ProgressLike(total=total, completed=total // 2)
            if self._should_fail(region):
                if task_service is not None:
                    task.apply_progress(half)
                    task_service.fail_task(task, self._error_text(region))
                return PlanTaskOutcome(
                    status=PlanItemRunStatus.FAILED,
                    completed_tiles=total // 2,
                    total_tiles=total,
                    error=self._error_text(region),
                )

            if on_progress is not None:
                on_progress(
                    ProgressLike(total=total, completed=total, tiles_per_second=7.5)
                )
            if task_service is not None:
                task.apply_progress(ProgressLike(total=total, completed=total))
                task_service.complete_task(task)
            self._touch_output(task)
            self.completed_calls += 1
            return PlanTaskOutcome(
                status=PlanItemRunStatus.COMPLETED,
                completed_tiles=total,
                total_tiles=total,
                downloaded_bytes=total * 10,
            )
        finally:
            self._active -= 1

    # --- 控制 --------------------------------------------------------- #
    def request_stop(self, *, pause: bool = True) -> None:
        """请求安全停下当前任务（调度器侧会调它）。"""
        self._stop_requested = True
        self._open_gate()

    def release(self) -> None:
        """放行挂着的任务，让它**正常跑完**（不标记为"被停下"）。

        用来模拟"进程被强行重启"：正在跑的那一条其实是跑完了的，只是
        调度器来不及把 ``COMPLETED`` 写进记录 —— 记录里留下的是 ``RUNNING``。
        """
        self._open_gate()

    def _open_gate(self) -> None:
        gate = self._gate
        if gate is not None and not gate.is_set():
            gate.set()

    # --- 辅助 --------------------------------------------------------- #
    def _should_fail(self, region: str) -> bool:
        if region not in self._fail_on:
            return False
        if self._fail_times <= 0:
            return True
        seen = self._failures.get(region, 0)
        if seen >= self._fail_times:
            return False
        self._failures[region] = seen + 1
        return True

    @staticmethod
    def _error_text(region: str) -> str:
        return f"{region} 下载失败：服务端返回 500"

    @staticmethod
    def _touch_output(task: Any) -> None:
        """在磁盘上留下一个"半截的文件"（取消时"不删除"那条断言要看它）。"""
        path = Path(str(getattr(task, "output_path", "")))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-mbtiles")


class ExplodingRunner(FakeRunner):
    """直接抛异常的假执行者（守"单条任务异常不带走整份计划"）。"""

    async def run(self, task: Any, **kwargs: Any) -> PlanTaskOutcome:  # noqa: D102
        raise RuntimeError("假执行者炸了")


def ready_plan(plan_service: Any, work_dir: Path, *region_ids: str, name: str = "离线计划"):
    """把若干区域加进计划、算一遍，返回可执行的计划。"""
    for region_id in region_ids:
        plan_service.add_region(region_id)
    plan_service.apply_settings(name=name, output_dir=work_dir / "output")
    plan_service.estimate()
    return plan_service.plan
