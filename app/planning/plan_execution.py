"""下载计划**执行**的运行时模型（Slice 21，Spec §2 / §3 / §12 / §13）。

Slice 20 回答"我要下哪几块地"，本模块回答"这几块地跑到哪一步了"：

::

    PlanExecution
    ├── PlanItemExecution  item=中国    task=t-1  COMPLETED
    ├── PlanItemExecution  item=浙江省  task=t-2  RUNNING
    └── PlanItemExecution  item=杭州市  task=t-3  WAITING

为什么是**第三份**数据，而不是往计划里加状态
--------------------------------------------

================================  ==========================================
``data/plans.json``               用户想要什么（可随时改，改完仍是同一份计划）
``data/tasks.json``               每一块地的单任务记录（下载器反复改写进度）
``data/plan_executions.json``     这份计划跑到哪一步了（本模块）
================================  ==========================================

把"跑到哪一步"写进计划文件会有两个直接后果：一是执行进度按秒写盘，会把用户的
计划配置一起反复重写；二是"计划"与"这次执行"变成同一个生命周期 —— 而它们是
两回事：同一份计划可以被执行很多次（跑完 → 改了再跑），用户改计划时也不该
影响"上一次跑到哪"这条历史。

为什么这里有两个看起来像"重名"的东西（Spec §2 的命名冲突）
----------------------------------------------------------

Spec 把执行态枚举命名为 ``PlanItemStatus``，但这个名字在 Slice 20 已经被用了
—— 它指的是"这一项**能不能用**"（``VALID`` / ``INVALID``，区域在不在目录里、
边界文件在不在）。两者是**完全不同的问题**：

====================================  ======================================
:class:`~app.planning.download_plan.PlanItemStatus`  可用性（配置问题）
:class:`PlanItemRunStatus`                           执行态（跑到哪了）
====================================  ======================================

同名同层共存会让"导入哪个 PlanItemStatus"变成一个靠记忆的问题，而这两者的
取值集合又完全不重叠（前者两个、后者七个），一旦有人 import 错了不会报错，
只会在某条分支上静默走错。因此这里**刻意换名**为 :class:`PlanItemRunStatus`，
并在 :mod:`app.planning` 的文档里写明两个名字的分工。

刻意不存的东西（Spec §3 / §19）
-------------------------------

不存 Token、不存几何、不存瓦片列表。执行记录里关于"一块地"的全部信息就是
``plan_item_id`` 与 ``task_id`` 两个**引用**：区域名、Zoom、张数由
:class:`~app.planning.download_plan.DownloadPlan` 提供，进度由
:class:`~app.models.download_task.DownloadTask` 提供。引用而不是复制，
是"界面上的数字"与"真正在下的东西"不会分家的唯一办法。

本模块是**纯数据 + 纯函数**：不 import Qt、不碰磁盘、不起线程、不 import
``app.services``。于是 :func:`build_progress` 这类聚合逻辑可以脱离窗口与
网络单独断言。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from app.geo.tile_math import format_size
from app.planning.download_plan import DownloadPlan, DownloadPlanItem

__all__ = [
    "PlanExecution",
    "PlanExecutionStatus",
    "PlanItemExecution",
    "PlanItemRunStatus",
    "PlanProgress",
    "build_progress",
    "compute_plan_totals",
    "item_tile_contribution",
    "new_execution_for",
    "plan_region_lookup",
    "recover_running_execution",
]


class PlanExecutionStatus(Enum):
    """整份计划的执行状态（Spec §2）。

    与 :class:`~app.models.download_task.DownloadTaskStatus` **不是一回事**，
    也不复用它的取值：任务状态描述"一个下载器在干什么"，这里描述"一份计划
    走到哪了"，两者的合法迁移完全不同（例如计划可以被"暂停"很多次，而
    "部分失败"是计划独有的概念）。

    刻意**没有**"正在暂停"这个中间态：暂停是"请求 → 当前任务安全停下"两步，
    但中间那一瞬用户不需要知道（他知道的是"点了暂停"，而进度条还动一下是
    正常的），多一个落盘状态只会让恢复逻辑多一种情况要处理。
    """

    #: 刚建好、还没跑（或者上一次跑完了又被清掉）。
    CREATED = "created"
    #: 正在跑（有且仅有一个条目在 RUNNING）。
    RUNNING = "running"
    #: 用户暂停，或程序退出时被安全停下。**可以继续**。
    PAUSED = "paused"
    #: 所有条目都完成了。
    COMPLETED = "completed"
    #: 停在某个失败/跳过的条目上，其余的还没跑。**可以重试或跳过**。
    PARTIAL_FAILED = "partial_failed"
    #: 整份计划没法进行（例如没有任何可执行的条目）。
    FAILED = "failed"
    #: 用户取消：当前任务安全停下，后续条目一律标成已取消。
    CANCELLED = "cancelled"

    @property
    def display_name(self) -> str:
        """中文展示名（界面直接用，不显示枚举名）。"""
        return _EXECUTION_STATUS_NAMES[self]

    @property
    def is_active(self) -> bool:
        """是否是"这次执行还活着"的状态（Spec §18：期间禁止改计划）。

        ``RUNNING`` 与 ``PAUSED`` 都算：暂停期间改计划同样会让"执行记录"与
        "计划"对不上（条目被删了，记录里那条就再也对不上了）。
        """
        return self in (PlanExecutionStatus.RUNNING, PlanExecutionStatus.PAUSED)

    @property
    def is_terminal(self) -> bool:
        """是否已经结束（不会自己再往前走）。"""
        return self in (
            PlanExecutionStatus.COMPLETED,
            PlanExecutionStatus.FAILED,
            PlanExecutionStatus.CANCELLED,
        )

    @property
    def can_resume(self) -> bool:
        """界面上该不该给「继续」（Spec §11 / §20）。"""
        return self in (
            PlanExecutionStatus.PAUSED,
            PlanExecutionStatus.PARTIAL_FAILED,
            PlanExecutionStatus.CREATED,
        )


_EXECUTION_STATUS_NAMES: dict[PlanExecutionStatus, str] = {
    PlanExecutionStatus.CREATED: "未开始",
    PlanExecutionStatus.RUNNING: "执行中",
    PlanExecutionStatus.PAUSED: "已暂停",
    PlanExecutionStatus.COMPLETED: "已完成",
    PlanExecutionStatus.PARTIAL_FAILED: "部分失败",
    PlanExecutionStatus.FAILED: "失败",
    PlanExecutionStatus.CANCELLED: "已取消",
}


class PlanItemRunStatus(Enum):
    """计划里**一个条目**的执行状态（Spec §2 / §17）。

    名字带 ``Run`` 是为了与 :class:`~app.planning.download_plan.PlanItemStatus`
    （可用性）分开，理由见模块文档。

    底层下载状态（``DOWNLOADING`` / ``PACKAGING`` / ``VALIDATING`` …）**不**在
    这里重复实现：一个条目只有"还没跑 / 正在跑 / 停了 / 跑完了"，中间那些
    细粒度状态属于单个任务，界面要看就去看任务列表。
    """

    WAITING = "waiting"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"

    @property
    def display_name(self) -> str:
        """中文展示名。"""
        return _ITEM_RUN_STATUS_NAMES[self]

    @property
    def symbol(self) -> str:
        """列表行首的符号（Spec §17：「保持 UI 简洁，不引入复杂自定义控件」）。

        用**字符**而不是图标资源：这一列只是"一眼看出哪一行在跑"，
        为它引入 ``QIcon`` / 图片资源会让打包与高 DPI 缩放都多一件要管的事。
        这几个字符在 Windows / macOS / Linux 的常见字体里都有字形。
        """
        return _ITEM_RUN_SYMBOLS[self]

    @property
    def is_pending(self) -> bool:
        """还没结束、调度器仍该管它。

        ``PAUSED`` 也算：条目是"停下但没跑完"，继续时要从它接着跑（Spec §9）。
        """
        return self in (PlanItemRunStatus.WAITING, PlanItemRunStatus.PAUSED)

    @property
    def is_finished(self) -> bool:
        """已经有结论了（不会再被调度器自动捡起来）。

        ``PAUSED`` **不算**：它只是"暂时停下"。反过来 ``SKIPPED`` 算：
        那是用户在"跳过并继续"里明确表过的态（Spec §11）。
        """
        return self in (
            PlanItemRunStatus.COMPLETED,
            PlanItemRunStatus.FAILED,
            PlanItemRunStatus.CANCELLED,
            PlanItemRunStatus.SKIPPED,
        )

    @property
    def is_success(self) -> bool:
        """是否是"这块地拿到了"（只有 ``COMPLETED`` 算）。"""
        return self is PlanItemRunStatus.COMPLETED


_ITEM_RUN_STATUS_NAMES: dict[PlanItemRunStatus, str] = {
    PlanItemRunStatus.WAITING: "等待中",
    PlanItemRunStatus.RUNNING: "执行中",
    PlanItemRunStatus.PAUSED: "已暂停",
    PlanItemRunStatus.COMPLETED: "已完成",
    PlanItemRunStatus.FAILED: "失败",
    PlanItemRunStatus.CANCELLED: "已取消",
    PlanItemRunStatus.SKIPPED: "已跳过",
}

_ITEM_RUN_SYMBOLS: dict[PlanItemRunStatus, str] = {
    PlanItemRunStatus.WAITING: "○",
    PlanItemRunStatus.RUNNING: "▶",
    PlanItemRunStatus.PAUSED: "Ⅱ",
    PlanItemRunStatus.COMPLETED: "✓",
    PlanItemRunStatus.FAILED: "✕",
    PlanItemRunStatus.CANCELLED: "–",
    PlanItemRunStatus.SKIPPED: "»",
}


@dataclass
class PlanItemExecution:
    """一条计划条目的执行记录（Spec §3）。

    只有四个字段，而且只有**引用**没有内容：``plan_item_id`` 指向计划里的那一行，
    ``task_id`` 指向 ``tasks.json`` 里的那条任务。区域名 / Zoom / 张数全部现查
    —— 复制一份进来就多一个会过期的真相。

    Attributes:
        plan_item_id: :attr:`~app.planning.download_plan.DownloadPlanItem.id`。
            **不是** ``region_id``：用户删掉一行再加回来是**另一行**，
            执行记录不该跟着跑到新行上去。
        task_id: 这条条目对应的 :class:`~app.models.DownloadTask` id；
            还没建任务时为 ``None``。
        status: 执行态。
        error: 失败原因（人话）。只有 ``FAILED`` 会有值。
    """

    plan_item_id: str = ""
    task_id: str | None = None
    status: PlanItemRunStatus = PlanItemRunStatus.WAITING
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_item_id", str(self.plan_item_id or "").strip())
        object.__setattr__(self, "task_id", self.task_id or None)
        object.__setattr__(self, "status", _parse_run_status(self.status))
        if self.error is not None:
            object.__setattr__(self, "error", str(self.error))

    def mark(self, status: PlanItemRunStatus, error: str | None = None) -> None:
        """改状态（并顺手清掉不属于新状态的失败原因）。"""
        self.status = status
        self.error = str(error) if error else None

    def describe(self, plan_item: DownloadPlanItem | None = None) -> str:
        """一行日志文案，如 ``示例国（全国级）Z4~10 ✓ 已完成``。"""
        head = plan_item.describe() if plan_item is not None else self.plan_item_id
        text = f"{head} {self.status.symbol} {self.status.display_name}"
        if self.error:
            text += f"：{self.error}"
        return text


@dataclass
class PlanExecution:
    """一份计划的执行记录（Spec §3）。

    Attributes:
        plan_id: 对应的 :attr:`~app.planning.download_plan.DownloadPlan.id`。
        status: 整份计划的状态。
        current_item_index: 当前（或最后）在跑的那一条在 :attr:`items` 里的下标；
            -1 表示还没开始。
        items: 逐条的执行记录，顺序与 :meth:`DownloadPlan.ordered_enabled_items`
            一致（也就是 Spec §13 的 全国 → 省 → 市 → 任务区）。
            **只包含"生成过任务"的条目**：未启用或不可用的条目压根不进来，
            否则进度分母里会混进永远不会跑的东西。
        total_tiles: 进度分母（Spec §12：所有启用项 ``selected_tiles`` 之和）。
        completed_tiles: 分子（逐条按实际下载量累计，见 :func:`item_tile_contribution`）。
        downloaded_bytes: 已落盘字节数（已完成的写实测，在跑的写估算，见
            :func:`build_progress`）。
    """

    plan_id: str = ""
    status: PlanExecutionStatus = PlanExecutionStatus.CREATED
    current_item_index: int = -1
    items: list[PlanItemExecution] = field(default_factory=list)

    total_tiles: int = 0
    completed_tiles: int = 0
    downloaded_bytes: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", str(self.plan_id or "").strip())
        object.__setattr__(self, "status", _parse_execution_status(self.status))
        object.__setattr__(self, "items", list(self.items))
        object.__setattr__(self, "current_item_index", int(self.current_item_index))

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    @property
    def count(self) -> int:
        """条目总数。"""
        return len(self.items)

    @property
    def current(self) -> PlanItemExecution | None:
        """当前那一条；没有（还没开始 / 下标越界）返回 ``None``。"""
        if 0 <= self.current_item_index < len(self.items):
            return self.items[self.current_item_index]
        return None

    def index_of(self, plan_item_id: str) -> int:
        """按 ``plan_item_id`` 找下标；没有返回 ``-1``。"""
        wanted = str(plan_item_id or "").strip()
        for index, item in enumerate(self.items):
            if item.plan_item_id == wanted:
                return index
        return -1

    def find(self, plan_item_id: str) -> PlanItemExecution | None:
        """按 ``plan_item_id`` 取条目。"""
        index = self.index_of(plan_item_id)
        return self.items[index] if index >= 0 else None

    def pending_index(self) -> int:
        """下一条**该跑**的条目下标；没有返回 ``-1``。

        判据是 :attr:`PlanItemRunStatus.is_pending`（``WAITING`` 或 ``PAUSED``）：
        暂停过的条目也在这里被捡回来，正是 Spec §9 要的"从当前 PAUSED Item 继续"。
        """
        for index, item in enumerate(self.items):
            if item.status.is_pending:
                return index
        return -1

    @property
    def completed_count(self) -> int:
        """已完成条目数。"""
        return sum(1 for item in self.items if item.status.is_success)

    @property
    def failed_count(self) -> int:
        """失败条目数（不含用户主动跳过的）。"""
        return sum(1 for item in self.items if item.status is PlanItemRunStatus.FAILED)

    @property
    def unresolved_count(self) -> int:
        """没能拿到成果、也不是"还没跑"的条目数（失败 + 跳过 + 已取消）。

        用来定 :attr:`PlanExecutionStatus.PARTIAL_FAILED`：全绿才算
        ``COMPLETED``，留下任何一块没下成就是"部分失败"。
        """
        return sum(
            1
            for item in self.items
            if item.status
            in (
                PlanItemRunStatus.FAILED,
                PlanItemRunStatus.SKIPPED,
                PlanItemRunStatus.CANCELLED,
            )
        )

    @property
    def is_finished(self) -> bool:
        """所有条目都结束了吗（没有任何 ``WAITING`` / ``PAUSED``）。"""
        return all(item.status.is_finished for item in self.items)

    def item_ids(self) -> list[str]:
        """全部 ``plan_item_id``（顺序即执行顺序）。"""
        return [item.plan_item_id for item in self.items]

    @property
    def items_text(self) -> str:
        """``2 / 3`` 形式的进度文本（Spec §16 的「任务」一行）。"""
        total = len(self.items)
        if total <= 0:
            return "0 / 0"
        shown = self.current_item_index + 1
        return f"{min(max(shown, 1), total)} / {total}"

    def mark(self, index: int, status: PlanItemRunStatus, error: str | None = None) -> None:
        """给某一条改状态（越界时什么都不做）。"""
        if 0 <= index < len(self.items):
            self.items[index].mark(status, error)

    def reset_for_retry(self, index: int) -> bool:
        """把某一条退回"该跑"（Spec §11 的「重试当前」）。

        Returns:
            ``True`` 表示改动了。
        """
        if not 0 <= index < len(self.items):
            return False
        self.items[index].mark(PlanItemRunStatus.PAUSED)
        self.current_item_index = index
        return True

    def set_status(self, status: PlanExecutionStatus) -> None:
        """改整份计划的状态。"""
        self.status = status

    def describe(self) -> str:
        """一行日志文案，如 ``计划 p1：执行中 2/3（✓1 ✕0 →1）``。"""
        return (
            f"计划 {self.plan_id}：{self.status.display_name} {self.items_text}"
            f"（✓{self.completed_count} ✕{self.failed_count} ○{len(self.items)})"
        )


def _parse_execution_status(value: object) -> PlanExecutionStatus:
    """宽容解析执行状态；认不出来按 :attr:`PlanExecutionStatus.CREATED`。

    读历史文件的那一侧要宽容（与 :meth:`RegionLevel.parse` 同一套路）：
    认不出来只可能是有人手工改了文件或版本不同，落到"还没开始"比抛异常好
    —— 它至少还能被继续。
    """
    if isinstance(value, PlanExecutionStatus):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        for item in PlanExecutionStatus:
            if item.value == normalized:
                return item
    return PlanExecutionStatus.CREATED


def _parse_run_status(value: object) -> PlanItemRunStatus:
    """宽容解析条目执行态；认不出来按 :attr:`PlanItemRunStatus.WAITING`。"""
    if isinstance(value, PlanItemRunStatus):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        for item in PlanItemRunStatus:
            if item.value == normalized:
                return item
    return PlanItemRunStatus.WAITING


# --------------------------------------------------------------------------- #
# 构造 / 恢复（纯函数）
# --------------------------------------------------------------------------- #
def new_execution_for(
    plan: DownloadPlan, tasks: Sequence[Any] = ()
) -> PlanExecution:
    """按计划与任务记录造一份全新的执行记录（Spec §6）。

    Args:
        plan: 计划。
        tasks: 这次为计划建出来的任务记录（任何有 ``id`` / ``region_id`` 的对象）。
            **按 ``region_id`` 对上条目，不按下标** —— 下标对上是"调用方恰好按
            计划顺序传"这种默契，而默契会在某次重构里断掉，断掉的表现是
            "任务被安到了另一块地上"，且不会报错。用 region_id 对上之后，
            传进来的顺序随便。

    Returns:
        状态为 :attr:`PlanExecutionStatus.CREATED`、全部条目 ``WAITING``、
        ``current_item_index`` 为 -1 的执行记录。对不上任务的那些条目
        ``task_id`` 保持 ``None``（"还没建任务"）。
    """
    items = plan.ordered_enabled_items()
    by_region: dict[str, str] = {}
    for task in tasks:
        region_id = str(getattr(task, "region_id", "") or "")
        task_id = str(getattr(task, "id", "") or "")
        if region_id and task_id:
            by_region[region_id] = task_id

    executions = [
        PlanItemExecution(
            plan_item_id=item.id,
            task_id=by_region.get(item.region_id),
            status=PlanItemRunStatus.WAITING,
        )
        for item in items
    ]
    return PlanExecution(
        plan_id=plan.id,
        status=PlanExecutionStatus.CREATED,
        current_item_index=-1,
        items=executions,
        total_tiles=sum(item.selected_tiles for item in items),
    )


def recover_running_execution(
    execution: PlanExecution,
) -> tuple[PlanExecution, bool]:
    """把"上次异常退出时还在跑"的执行降级为可继续的状态（Spec §20 / §25）。

    **绝不自动联网继续**：``RUNNING`` 一律变成 ``PAUSED``（整份计划与那一条
    在跑的条目都是），用户按下「继续」之前一个请求都不发。理由与
    :meth:`~app.tasks.task_service.TaskService.recover_interrupted_tasks`
    完全相同：自动下载会消耗用户的配额，那是必须由人拍板的动作。

    顺带把遗留的 ``RUNNING`` **条目**也降成 ``PAUSED``：条目与整份计划的状态
    必须一致，只改一头会让界面显示"计划已暂停"而某一行还在转圈。

    Returns:
        ``(execution, changed)``：``changed`` 为 ``True`` 表示真的动过（调用方
        据此决定要不要落盘与提示用户"上次计划未完成"）。
    """
    changed = False
    if execution.status is PlanExecutionStatus.RUNNING:
        execution.status = PlanExecutionStatus.PAUSED
        changed = True
    for item in execution.items:
        if item.status is PlanItemRunStatus.RUNNING:
            item.status = PlanItemRunStatus.PAUSED
            changed = True
    return execution, changed


# --------------------------------------------------------------------------- #
# 聚合进度（Spec §12 / §13 / §14）
# --------------------------------------------------------------------------- #
def item_tile_contribution(
    plan_item: DownloadPlanItem | None, task: Any | None
) -> int:
    """一条条目对"整份计划已完成张数"的贡献（Spec §12）。

    规则：取**任务实际处理的张数**，并以该项的 ``selected_tiles`` 收口。

    - 跑完的条目：``task.completed_tiles == selected_tiles`` → 贡献等于分母，
      于是计划能走到 100%；
    - 在跑 / 暂停 / 失败的条目：按它真实下到的张数记（半截的成果也是成果，
      记 0 会让"失败在 90%"看起来像"什么都没干"）；
    - 还没跑的条目：没有任务或进度为 0 → 贡献 0。

    收口（``min``）是防御性的：分母来自估算，分子来自任务记录，两者在
    "计划被改过 / 估算被重跑过"之后可能不一致；不收口会算出 >100% 的进度。

    条目没估过（``selected_tiles == 0``）时**不做收口**，直接把任务的张数算上
    —— 否则那种条目永远贡献 0，计划进度会莫名其妙卡住。
    """
    if task is None:
        return 0
    done = max(0, int(getattr(task, "completed_tiles", 0) or 0))
    target = max(0, int(getattr(plan_item, "selected_tiles", 0) or 0))
    if target <= 0:
        return done
    return min(done, target)


def compute_plan_totals(
    plan: DownloadPlan,
    execution: PlanExecution,
    tasks: dict[str, Any],
) -> tuple[int, int]:
    """算出 ``(total_tiles, completed_tiles)``（Spec §12）。

    ``total_tiles`` 取**计划的**启用项之和，而不是执行记录里那份缓存：计划是
    "要下多少"的唯一来源，执行记录里的那份可能来自上一次估算。差别在于
    "用户改过计划但还没重跑"——那种情况下进度条的分母跟着计划走才对。

    ``tasks`` 是 ``{task_id: DownloadTask}``；查不到的条目不贡献分子（它已经
    被标成失败/无法恢复，不该再让进度看起来偏高）。
    """
    items = plan.ordered_enabled_items()
    by_plan_item = {item.id: item for item in items}
    total = sum(item.selected_tiles for item in items)
    completed = 0
    for entry in execution.items:
        task = tasks.get(entry.task_id) if entry.task_id else None
        plan_item = by_plan_item.get(entry.plan_item_id)
        if plan_item is None:
            # 计划里已经没有这一项（用户改过计划）：它既不在分母里，
            # 也不该在分子里 —— 两个数必须来自同一批条目。
            continue
        if entry.status is PlanItemRunStatus.COMPLETED and task is not None:
            # 已完成：直接按分母记。任务的计数器可能因为"服务端 404 少几张"
            # 而略小于目标，但结论是"这块地拿到了"，进度条不该因此缺一角。
            completed += max(0, int(plan_item.selected_tiles))
            continue
        completed += item_tile_contribution(plan_item, task)
    return total, min(completed, total) if total > 0 else completed


@dataclass
class PlanProgress:
    """聚合进度快照（Spec §13）。

    界面每收到一帧就照着画，**不做任何计算** —— 数字全部来自这里，
    于是"进度条"与"状态行"不会各算一遍。

    Attributes:
        current_item_index: 当前条目下标（-1 = 还没开始）。
        total_items: 条目总数。
        current_region_name: 当前区域的展示名。
        current_task_percent: 当前任务的百分比。
        plan_percent: 整份计划的百分比（Spec §12：按张数聚合，不是按条目数）。
        total_tiles: 分母（所有启用项 ``selected_tiles`` 之和）。
        completed_tiles: 分子。
        downloaded_bytes: 已落盘字节（已完成的写实测大小，在跑的按比例估算）。
        speed_bytes_per_second: 速度（Spec §14：**只有当前任务在跑时**才有值，
            其余时刻是 0 —— 任务是串行的，不存在"整份计划的速度"这种独立量）。
        message: 一句话状态（写日志与状态行用）。

    比 Spec §13 多的三个字段（都是界面要显示的东西，缺了就得在界面里再算一遍）：

    - ``status``：整份计划的状态（状态行要写"执行中 / 已暂停"）；
    - ``speed_tiles_per_second``：引擎给的原生速度单位（瓦片/秒）；
    - ``current_zoom_text``：``Z4~10`` 这种文本，Spec §16 的「当前」一行要用。
    """

    current_item_index: int = -1
    total_items: int = 0
    current_region_name: str = ""
    current_task_percent: float = 0.0
    plan_percent: float = 0.0
    total_tiles: int = 0
    completed_tiles: int = 0
    downloaded_bytes: int = 0
    speed_bytes_per_second: float = 0.0
    message: str = ""

    status: PlanExecutionStatus = PlanExecutionStatus.CREATED
    speed_tiles_per_second: float = 0.0
    current_zoom_text: str = ""

    # ------------------------------------------------------------------ #
    # 展示
    # ------------------------------------------------------------------ #
    @property
    def items_text(self) -> str:
        """``2 / 3``（Spec §16 的「任务」一行）。"""
        if self.total_items <= 0:
            return "0 / 0"
        shown = min(max(self.current_item_index + 1, 1), self.total_items)
        return f"{shown} / {self.total_items}"

    @property
    def current_text(self) -> str:
        """当前区域那行文本，如 ``示例国 Z4~10``。"""
        if not self.current_region_name:
            return "—"
        if self.current_zoom_text:
            return f"{self.current_region_name} {self.current_zoom_text}"
        return self.current_region_name

    @property
    def speed_text(self) -> str:
        """速度文本，如 ``5.2 MB/s``（没有速度时是 ``—``）。"""
        if self.speed_bytes_per_second <= 0:
            return "—"
        return f"{format_size(int(self.speed_bytes_per_second))}/s"

    @property
    def tiles_text(self) -> str:
        """``12,345 / 57,302`` 形式的张数进度。"""
        return f"{self.completed_tiles:,} / {self.total_tiles:,}"

    def as_lines(self) -> list[str]:
        """多行文案（Spec §16 的区块照它画；日志与验收脚本也用它）。"""
        return [
            f"状态：{self.status.display_name}",
            f"当前：{self.current_text}",
            f"任务：{self.items_text}",
            f"当前任务：{self.current_task_percent:.0f}%",
            f"总体：{self.plan_percent:.0f}%（{self.tiles_text} 张）",
            f"速度：{self.speed_text}",
        ]


def build_progress(
    plan: DownloadPlan,
    execution: PlanExecution,
    *,
    tasks: dict[str, Any],
    current_task_percent: float = 0.0,
    current_speed_tiles_per_second: float = 0.0,
    downloaded_bytes_by_task: dict[str, int] | None = None,
    message: str = "",
) -> PlanProgress:
    """把"计划 + 执行记录 + 任务记录"聚合成一帧进度（Spec §12 / §13 / §14）。

    Args:
        plan: 计划（分母与展示名的来源）。
        execution: 执行记录。
        tasks: ``{task_id: DownloadTask}``。
        current_task_percent: 当前任务的百分比（来自引擎进度回调）。
        current_speed_tiles_per_second: 当前任务的速度（瓦片/秒）。
            Spec §14：任务切换时**归零**，由调用方负责（本函数不记忆上一帧）。
        downloaded_bytes_by_task: 已知的落盘字节数（一般是已经跑完的那些任务的
            实测 ``file_size``）。没在表里的按"预计容量 × 完成比例"估算。
        message: 一句话状态。

    Returns:
        :class:`PlanProgress`。**纯函数**：不改任何入参，可脱离窗口断言。
    """
    total, completed = compute_plan_totals(plan, execution, tasks)
    items = plan.ordered_enabled_items()
    by_plan_item = {item.id: item for item in items}

    index = execution.current_item_index
    current_entry = execution.current
    current_plan_item = (
        by_plan_item.get(current_entry.plan_item_id) if current_entry is not None else None
    )

    known_bytes = dict(downloaded_bytes_by_task or {})
    downloaded = 0
    for entry in execution.items:
        task = tasks.get(entry.task_id) if entry.task_id else None
        measured = known_bytes.get(entry.task_id or "")
        if measured is not None:
            downloaded += max(0, int(measured))
            continue
        plan_item = by_plan_item.get(entry.plan_item_id)
        if plan_item is None:
            continue
        target = max(0, int(plan_item.selected_tiles))
        if target <= 0:
            continue
        shares = item_tile_contribution(plan_item, task)
        # 按比例折算容量：估算的产物，界面与日志都只把它当"大概多大"。
        downloaded += int(plan_item.estimated_bytes * (shares / target))

    # Spec §14：计划速度 = 当前任务速度。没有当前任务（暂停 / 刚切换 / 结束）
    # 时必须是 0 —— 否则界面上会挂着上一个任务的残留速度，看着像还在下。
    running = current_entry is not None and current_entry.status is PlanItemRunStatus.RUNNING
    speed_tiles = float(current_speed_tiles_per_second) if running else 0.0
    if not running:
        current_task_percent = float(current_task_percent) if current_entry is not None else 0.0

    average_tile_bytes = 0
    if current_plan_item is not None and current_plan_item.selected_tiles > 0:
        average_tile_bytes = int(
            current_plan_item.estimated_bytes / current_plan_item.selected_tiles
        )
    speed_bytes = max(0.0, speed_tiles * average_tile_bytes)

    return PlanProgress(
        current_item_index=index,
        total_items=len(execution.items),
        current_region_name=(
            (current_plan_item.region_name or current_plan_item.region_id)
            if current_plan_item is not None
            else ""
        ),
        current_task_percent=max(0.0, min(100.0, float(current_task_percent))),
        plan_percent=(completed / total * 100.0) if total > 0 else 0.0,
        total_tiles=total,
        completed_tiles=completed,
        downloaded_bytes=downloaded,
        speed_bytes_per_second=speed_bytes,
        message=message,
        status=execution.status,
        speed_tiles_per_second=speed_tiles,
        current_zoom_text=(
            current_plan_item.zoom_text if current_plan_item is not None else ""
        ),
    )


def plan_region_lookup(plan: DownloadPlan) -> dict[str, DownloadPlanItem]:
    """``{plan_item_id: DownloadPlanItem}``（便利函数，供调度器与界面共用）。"""
    return {item.id: item for item in plan.ordered_enabled_items()}
