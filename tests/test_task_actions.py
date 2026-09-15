"""UI 状态 → 按钮可用性（Slice 7，Spec §6）。

按钮的可用性是用户唯一能看到的"状态机"。表里少一个格子，用户就会遇到
"能点但没意义"或者"该点却点不动"的按钮，而这类缺陷在手工点测里几乎不可能
穷举——表可以被逐格断言，所以这里就把每一格都钉住。

本模块不 import Qt（``app.ui.task_actions`` 本身是纯 Python），因此这里
不需要 ``QApplication``，跑得也比装配窗口快几个数量级。
"""

from __future__ import annotations

import pytest

from app.models import DownloadTaskStatus
from app.ui.task_actions import (
    AVAILABLE_ACTIONS,
    TaskAction,
    UiTaskState,
    actions_for,
    is_available,
)

#: 全部四种动作
ALL_ACTIONS = frozenset(TaskAction)


# --------------------------------------------------------------------------- #
# 逐格断言
# --------------------------------------------------------------------------- #
def test_matrix_covers_every_state() -> None:
    """每个状态都必须有明确的定义。

    漏一个状态的后果不是报错，而是 ``actions_for`` 回落到空集合——四个按钮
    全灰，界面看起来"卡死了"。因此这里比的是**集合相等**，新增状态时这条会红。
    """
    assert set(AVAILABLE_ACTIONS) == set(UiTaskState)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (UiTaskState.IDLE, {TaskAction.START}),
        (UiTaskState.SCANNING, {TaskAction.CANCEL}),
        (UiTaskState.CONFIRMING, {TaskAction.CANCEL}),
        (UiTaskState.DOWNLOADING, {TaskAction.PAUSE, TaskAction.CANCEL}),
        (UiTaskState.PAUSING, {TaskAction.CANCEL}),
        (UiTaskState.PAUSED, {TaskAction.RESUME, TaskAction.CANCEL}),
        (UiTaskState.COMPLETED, {TaskAction.START}),
        (UiTaskState.FAILED, {TaskAction.START, TaskAction.RESUME}),
        (UiTaskState.CANCELLED, {TaskAction.START, TaskAction.RESUME}),
    ],
)
def test_available_actions_per_state(state: UiTaskState, expected: set) -> None:
    assert actions_for(state) == frozenset(expected)


def test_every_state_allows_something() -> None:
    """任何状态下都至少有一个按钮可用（否则界面看起来像卡死了）。"""
    for state in UiTaskState:
        assert actions_for(state), f"{state} 下四个按钮全灰"


def test_busy_states_never_allow_start() -> None:
    """有任务占用下载通道时绝不允许「开始下载」（Spec §6 的核心一条）。

    放行它的后果是并发跑两个任务写两个文件、互相抢限速与并发额度，
    而用户以为自己只是"又点了一下"。
    """
    for state in UiTaskState:
        if state.is_busy:
            assert TaskAction.START not in actions_for(state), state


def test_idle_states_never_allow_pause_or_resume() -> None:
    """没有任务在跑时，「暂停」/「继续」没有任何意义。"""
    for state in (UiTaskState.IDLE, UiTaskState.COMPLETED):
        assert TaskAction.PAUSE not in actions_for(state)
        assert TaskAction.RESUME not in actions_for(state)


def test_confirming_state_only_allows_cancel() -> None:
    """弹着确认框时只放开「取消」。

    如果这时放开「开始下载」，用户就等于绕过了一次确认——而那次确认正是
    Spec §4 用来拦住"静默启动几十万张瓦片"的东西。
    """
    assert actions_for(UiTaskState.CONFIRMING) == frozenset({TaskAction.CANCEL})


def test_scanning_state_allows_cancel_so_the_ui_is_not_frozen() -> None:
    """扫描一个几十万张的库要几秒到几十秒，期间必须留一个出口。"""
    assert TaskAction.CANCEL in actions_for(UiTaskState.SCANNING)


def test_pausing_state_still_allows_cancel() -> None:
    """暂停收尾过程中用户改主意想直接取消，没有理由拦住他。"""
    assert actions_for(UiTaskState.PAUSING) == frozenset({TaskAction.CANCEL})


def test_completed_state_does_not_allow_resume() -> None:
    """已完成的任务没有缺失瓦片，「继续」是空动作。"""
    assert TaskAction.RESUME not in actions_for(UiTaskState.COMPLETED)


def test_failed_state_allows_both_start_and_resume() -> None:
    """失败：继续 = 补缺失的瓦片（常见）；开始 = 换个文件名重来（也可能）。"""
    assert actions_for(UiTaskState.FAILED) == frozenset(
        {TaskAction.START, TaskAction.RESUME}
    )


def test_paused_state_does_not_allow_start() -> None:
    """PAUSED 有明确的「继续」语义，再放开一个「开始」入口会让"我点了开始，
    它为什么又建了一条记录"变成一个需要解释的问题。"""
    assert TaskAction.START not in actions_for(UiTaskState.PAUSED)


def test_paused_keeps_cancel_but_cancelled_keeps_start() -> None:
    """PAUSED 与 CANCELLED 的差异是刻意的，不是遗漏。

    - ``PAUSED``：后台线程**还活着**（暂停只是关掉了引擎的闸门），所以
      「取消」仍然有对象可取消；而「开始」被禁掉，是因为它已有明确的
      「继续」语义，两个入口并存会让"我点了开始，它为什么又建了一条记录"
      变成一个需要解释的问题。
    - ``CANCELLED``：线程已经收尾退出，没有东西可取消了，「取消」因此没有意义；
      而「开始」是放开的——用户取消了一份参数不合适的任务后，最自然的下一步
      就是调好参数重新下。

    两者都允许「继续」（都还有缺失的瓦片要补）。
    """
    paused = actions_for(UiTaskState.PAUSED)
    cancelled = actions_for(UiTaskState.CANCELLED)

    assert TaskAction.RESUME in paused and TaskAction.RESUME in cancelled
    assert TaskAction.CANCEL in paused
    assert TaskAction.CANCEL not in cancelled
    assert TaskAction.START in cancelled
    assert TaskAction.START not in paused


# --------------------------------------------------------------------------- #
# is_available / actions_for 的兜底
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("action", list(TaskAction))
def test_is_available_matches_the_matrix(action: TaskAction) -> None:
    for state in UiTaskState:
        assert is_available(state, action) == (
            action in AVAILABLE_ACTIONS[state]
        )


def test_unknown_state_yields_nothing_instead_of_raising() -> None:
    """未知状态返回空集合而不是抛异常。

    按钮可用性出错的后果是"某个按钮点不动"，抛异常的后果是整个界面起不来
    ——两者的代价完全不对称。``None`` 就是一个足够有代表性的未知状态。
    """
    assert actions_for(None) == frozenset()  # type: ignore[arg-type]
    assert is_available(None, TaskAction.START) is False  # type: ignore[arg-type]


def test_actions_for_returns_a_frozenset() -> None:
    """返回值必须是不可变的：调用方不该能顺手改掉全局表。"""
    result = actions_for(UiTaskState.IDLE)
    assert isinstance(result, frozenset)


# --------------------------------------------------------------------------- #
# 持久化状态 → 界面状态
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", list(DownloadTaskStatus))
def test_every_persisted_status_maps_to_a_ui_state(status: DownloadTaskStatus) -> None:
    """``tasks.json`` 里的每一种状态都必须能映射，否则列表选中该项时按钮全灰。"""
    assert isinstance(UiTaskState.from_status(status), UiTaskState)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (DownloadTaskStatus.CREATED, UiTaskState.IDLE),
        (DownloadTaskStatus.DOWNLOADING, UiTaskState.DOWNLOADING),
        (DownloadTaskStatus.CALCULATING, UiTaskState.DOWNLOADING),
        (DownloadTaskStatus.PACKAGING, UiTaskState.DOWNLOADING),
        (DownloadTaskStatus.VALIDATING, UiTaskState.DOWNLOADING),
        (DownloadTaskStatus.PAUSED, UiTaskState.PAUSED),
        (DownloadTaskStatus.COMPLETED, UiTaskState.COMPLETED),
        (DownloadTaskStatus.FAILED, UiTaskState.FAILED),
        (DownloadTaskStatus.CANCELLED, UiTaskState.CANCELLED),
    ],
)
def test_status_mapping(status: DownloadTaskStatus, expected: UiTaskState) -> None:
    """三个"进行中但交互相同"的细分状态统一归到 DOWNLOADING。

    从用户角度看 CALCULATING / PACKAGING / VALIDATING 都是"任务在跑、可以暂停
    或取消"；替它们各造一个界面状态只会多出三组无人需要的按钮组合。
    """
    assert UiTaskState.from_status(status) is expected


def test_transient_states_are_not_persisted() -> None:
    """``SCANNING`` / ``PAUSING`` 永远不该出现在 ``tasks.json`` 里。

    持久化一个"正在暂停"的任务，重启后就无法判断它到底停没停——因为它其实
    取决于那个已经不存在的线程。因此这两个状态只活在 :class:`UiTaskState`。
    """
    persisted_values = {status.value for status in DownloadTaskStatus}
    for transient in (UiTaskState.SCANNING, UiTaskState.PAUSING):
        assert transient.value not in persisted_values


# --------------------------------------------------------------------------- #
# 展示名
# --------------------------------------------------------------------------- #
def test_display_names_are_chinese_and_unique() -> None:
    names = [state.display_name for state in UiTaskState]
    assert all(names), "展示名不能为空"
    assert len(set(names)) == len(names), "展示名重复会让状态无法区分"


def test_action_display_names() -> None:
    assert [action.display_name for action in TaskAction] == [
        "开始下载",
        "暂停",
        "继续",
        "取消",
    ]


def test_busy_flag() -> None:
    assert UiTaskState.DOWNLOADING.is_busy is True
    assert UiTaskState.SCANNING.is_busy is True
    assert UiTaskState.CONFIRMING.is_busy is True
    assert UiTaskState.PAUSING.is_busy is True
    # 暂停 / 失败 / 取消 / 完成都不占通道：用户完全可以开始或继续下一轮
    for state in (
        UiTaskState.IDLE,
        UiTaskState.PAUSED,
        UiTaskState.COMPLETED,
        UiTaskState.FAILED,
        UiTaskState.CANCELLED,
    ):
        assert state.is_busy is False
