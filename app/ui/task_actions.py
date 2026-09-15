"""任务状态 → 按钮可用性（Slice 7，Spec §6）。

把"什么状态下允许点哪个按钮"从散落的 ``if running: ... else: ...`` 收敛成
**一张表**。这样做的理由不只是整洁：

- 按钮的可用性是用户唯一能看到的"状态机"。表里少一个格子，用户就会看到一个
  能点但没意义的按钮（例如「继续」一个已完成的任务），而这类缺陷在手工点测里
  很难穷举——表可以被逐格断言。
- UI 状态比持久化的 :class:`~app.models.download_task.DownloadTaskStatus` 多两个：
  ``SCANNING``（正在扫描已有瓦片，Spec §11）与 ``PAUSING``（已请求暂停、
  但引擎还在收尾，Spec §13）。这两个都是**瞬时**状态，不该写进任务文件
  ——持久化一个"正在暂停"的任务，重启后无法判断它到底停没停。因此本模块
  单独定义 :class:`UiTaskState`，与磁盘上的状态解耦。

本模块是**纯 Python**：不 import Qt，因此可以在没有 ``QApplication`` 的环境里
直接单测（按钮矩阵的正确性不该依赖于能不能创建窗口）。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum

from app.models.download_task import DownloadTaskStatus

__all__ = [
    "AVAILABLE_ACTIONS",
    "TaskAction",
    "UiTaskState",
    "actions_for",
    "is_available",
]


class TaskAction(Enum):
    """用户可以发起的四个下载动作。"""

    START = "start"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"

    @property
    def display_name(self) -> str:
        """中文展示名（按钮文案与日志共用）。"""
        return _ACTION_DISPLAY_NAMES[self]


_ACTION_DISPLAY_NAMES: dict[TaskAction, str] = {
    TaskAction.START: "开始下载",
    TaskAction.PAUSE: "暂停",
    TaskAction.RESUME: "继续",
    TaskAction.CANCEL: "取消",
}


class UiTaskState(Enum):
    """界面上实际呈现的任务状态（含两个不落盘的瞬时态）。

    ``from_status`` 负责把持久化状态映射过来。``CALCULATING`` / ``PACKAGING`` /
    ``VALIDATING`` 映射到 :attr:`DOWNLOADING`：从用户角度看它们都是
    "任务在跑、可以暂停或取消"，区分它们只会多出三个无人需要的按钮组合。
    """

    #: 没有任务在跑（也是"刚启动"的状态）
    IDLE = "idle"
    #: 正在后台扫描已有瓦片（Spec §11），此时还不能定下要下多少张
    SCANNING = "scanning"
    #: 弹着确认框（大任务 / 已有文件 / 重新下载），等用户拍板
    CONFIRMING = "confirming"
    DOWNLOADING = "downloading"
    #: 已请求暂停，但引擎还在收尾（Spec §13）
    PAUSING = "pausing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def display_name(self) -> str:
        """中文展示名。"""
        return _STATE_DISPLAY_NAMES[self]

    @property
    def is_busy(self) -> bool:
        """是否处于"有任务正在占用下载通道"的状态。"""
        return self in (
            UiTaskState.SCANNING,
            UiTaskState.CONFIRMING,
            UiTaskState.DOWNLOADING,
            UiTaskState.PAUSING,
        )

    @classmethod
    def from_status(cls, status: DownloadTaskStatus) -> "UiTaskState":
        """把持久化状态映射成界面状态。

        Args:
            status: 任务记录里的状态。

        Returns:
            对应的 :class:`UiTaskState`；三个"进行中但无独立交互"的细分状态
            统一归到 :attr:`DOWNLOADING`。
        """
        return _STATUS_TO_UI_STATE[status]


_STATE_DISPLAY_NAMES: dict[UiTaskState, str] = {
    UiTaskState.IDLE: "空闲",
    UiTaskState.SCANNING: "扫描已有瓦片",
    UiTaskState.CONFIRMING: "等待确认",
    UiTaskState.DOWNLOADING: "下载中",
    UiTaskState.PAUSING: "正在暂停",
    UiTaskState.PAUSED: "已暂停",
    UiTaskState.COMPLETED: "已完成",
    UiTaskState.FAILED: "失败",
    UiTaskState.CANCELLED: "已取消",
}

_STATUS_TO_UI_STATE: dict[DownloadTaskStatus, UiTaskState] = {
    DownloadTaskStatus.CREATED: UiTaskState.IDLE,
    DownloadTaskStatus.CALCULATING: UiTaskState.DOWNLOADING,
    DownloadTaskStatus.DOWNLOADING: UiTaskState.DOWNLOADING,
    DownloadTaskStatus.PAUSED: UiTaskState.PAUSED,
    DownloadTaskStatus.PACKAGING: UiTaskState.DOWNLOADING,
    DownloadTaskStatus.VALIDATING: UiTaskState.DOWNLOADING,
    DownloadTaskStatus.COMPLETED: UiTaskState.COMPLETED,
    DownloadTaskStatus.FAILED: UiTaskState.FAILED,
    DownloadTaskStatus.CANCELLED: UiTaskState.CANCELLED,
}


#: 状态 → 允许的动作（Spec §6 的表）。
#:
#: 逐格说明几个不那么显然的地方：
#:
#: - ``SCANNING`` 允许**取消**：扫描一个几十万张的库要几秒到几十秒，
#:   期间把四个按钮全禁掉会让用户以为程序卡死；「取消」是安全的出口
#:   （扫描还没写任何东西，取消即"这次不下了"）。
#: - ``CONFIRMING`` 只允许**取消**：确认框本身就有"是/否"，面板这边再放开
#:   「开始下载」会绕过那次确认（这正是 Spec §4 禁止的"静默启动大型任务"）。
#: - ``PAUSING`` 允许**取消**：暂停与取消都要先让引擎收尾，收尾过程中
#:   用户改主意想直接取消，没有理由拦住他。
#: - ``PAUSED`` / ``FAILED`` / ``CANCELLED`` 都允许**开始**：这三者都没有任务
#:   在占用下载通道，用户完全可以调好参数开始一次新的下载。把它们禁用会
#:   逼用户先删掉那条任务记录才能下别的图。
#: - ``COMPLETED`` 只允许**开始**：没有缺失瓦片，继续是空动作。
#: - ``PAUSED`` / ``CANCELLED`` 不允许**开始**是刻意的：它们有明确的「继续」
#:   语义，同时放开两个入口会让"我点了开始，它为什么又建了一条记录"变成
#:   一个需要解释的问题。
#: - ``FAILED`` 同时允许开始与继续：继续 = 补缺失的瓦片（常见），
#:   开始 = 换个文件名重来（也可能）。
AVAILABLE_ACTIONS: Mapping[UiTaskState, frozenset[TaskAction]] = {
    UiTaskState.IDLE: frozenset({TaskAction.START}),
    UiTaskState.SCANNING: frozenset({TaskAction.CANCEL}),
    UiTaskState.CONFIRMING: frozenset({TaskAction.CANCEL}),
    UiTaskState.DOWNLOADING: frozenset({TaskAction.PAUSE, TaskAction.CANCEL}),
    UiTaskState.PAUSING: frozenset({TaskAction.CANCEL}),
    UiTaskState.PAUSED: frozenset({TaskAction.RESUME, TaskAction.CANCEL}),
    UiTaskState.COMPLETED: frozenset({TaskAction.START}),
    UiTaskState.FAILED: frozenset({TaskAction.START, TaskAction.RESUME}),
    UiTaskState.CANCELLED: frozenset({TaskAction.START, TaskAction.RESUME}),
}


def actions_for(state: UiTaskState) -> frozenset[TaskAction]:
    """某状态下允许的动作集合（未知状态一律视为"什么都不允许"）。

    返回空集合而不是抛异常：按钮可用性出错的后果是"某个按钮点不动"，
    而抛异常的后果是整个界面起不来——两者的代价完全不对称。
    """
    return AVAILABLE_ACTIONS.get(state, frozenset())


def is_available(state: UiTaskState, action: TaskAction) -> bool:
    """某状态下某个动作是否可用。"""
    return action in actions_for(state)
