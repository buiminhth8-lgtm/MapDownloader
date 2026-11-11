"""故障注入钩子（Slice 42 专用，**只在显式设置环境变量时生效**）。

为什么生产代码里要留一个"能把自己弄死"的开关
--------------------------------------------

"崩在事务提交之后、catalog 发布之前"这类状态**没法用 Python 异常模拟** ——
``raise`` 会走 ``except`` 分支，文件会正常关闭、连接会回滚，于是测试看起来通过了，
而真实的进程被 kill 时这些收尾根本不会发生。Slice 42 的验收要求里有一条
"仅抛 Python 异常不能替代进程中断验收"，所以这里留一个真正的
``os._exit()`` 通道。

用法（只应在测试与验收脚本里出现）::

    TILEREPO_FAULT_INJECT=crash:after_shard_commit python ...

取值::

    crash:<phase>   直接 os._exit(97)，模拟被强杀（不跑 finally、不刷日志）
    raise:<phase>   抛 RuntimeError（模拟"代码里有 bug"这一类可捕获失败）
    sleep:<phase>   睡 30 秒（用外部进程配合测"租约占用 / 并发"）

可用阶段见 :data:`PHASES`。默认（不设环境变量）时 :func:`inject` 什么也不做，
连一次字符串比较都只在环境变量非空时才发生 —— 生产路径上它等价于空函数。
"""

from __future__ import annotations

import os
import time

__all__ = ["FAULT_ENV", "PHASES", "inject"]

#: 环境变量名。**不要**把它设进任何用户可见的配置或文档的常规流程里。
FAULT_ENV: str = "TILEREPO_FAULT_INJECT"

#: 可注入的阶段（与 put_tiles / publish 的步骤一一对应）::
#:
#:   after_operation      写前日志落盘之后（分片还一行没写）
#:   during_shard_write   分片事务已开始、尚未提交（回滚后应"什么都没发生"）
#:   after_shard_commit   分片事务已提交、catalog 还没发布（数据在但不可见）
#:   before_publish       所有分片都提交完、发布事务尚未开始
#:   after_publish        发布事务已提交、操作还没打 done
PHASES: tuple[str, ...] = (
    "after_operation",
    "during_shard_write",
    "after_shard_commit",
    "before_publish",
    "after_publish",
)

#: ``os._exit`` 的退出码。刻意不是 0/1，便于在测试里区分"注入生效"与"脚本失败"。
CRASH_EXIT_CODE: int = 97


def inject(phase: str) -> None:
    """按环境变量在指定阶段制造故障（未设置时是空操作）。"""
    spec = os.environ.get(FAULT_ENV, "")
    if not spec:
        return
    kind, _, target = spec.partition(":")
    if target != phase:
        return
    if kind == "crash":
        os._exit(CRASH_EXIT_CODE)
    if kind == "raise":
        raise RuntimeError(f"故障注入（raise）：{phase}")
    if kind == "sleep":
        time.sleep(30.0)
