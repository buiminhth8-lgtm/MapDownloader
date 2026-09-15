"""异步限速器（Slice 3）。

按"**预约式**"节流：每个 :meth:`RateLimiter.acquire` 先在锁内领到一个未来的
时间片，再只睡自己那一份。好处是等待时间被**分摊**掉——不会出现
"所有 worker 同时睡一秒、然后同时打出一发请求"的脉冲式流量。

反面做法（本项目明确禁止）::

    for tile in tiles:
        await download(tile)
        await asyncio.sleep(1 / rate)     # 每个任务都背一个固定 sleep，
                                          # 任务一多就堆成几十秒的延迟

本模块不 ``import httpx``，可被纯单元测试覆盖。``acquire()`` 需要运行中的事件
循环，但**构造**不需要——因此可以在协程外先建好限速器再传进去。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.config.settings import DEFAULT_REQUESTS_PER_SECOND

__all__ = ["RateLimiter"]


@dataclass
class RateLimiter:
    """按固定速率放行的异步限速器。

    Args:
        requests_per_second: 每秒允许的请求数。``<= 0`` 表示**关闭限速**
            （``acquire()`` 立即返回，不产生任何 sleep）。
        max_burst: 允许的瞬时突发量。``0`` 表示严格匀速（默认）；
            大于 0 时最多累积 ``max_burst`` 个时间片供突发消费。

    Example:
        >>> limiter = RateLimiter(requests_per_second=8)   # 125ms 一发
        >>> limiter.interval_seconds
        0.125
        >>> RateLimiter(requests_per_second=0).is_enabled
        False
    """

    requests_per_second: float = DEFAULT_REQUESTS_PER_SECOND
    max_burst: int = 0

    #: 已放行的请求数，便于测试与诊断（不参与限速计算）
    acquired_count: int = field(default=0, init=False)

    #: 下一次允许发请求的时间点（事件循环时钟）；``None`` 表示尚未开始
    _next_slot: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            rate = float(self.requests_per_second)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"requests_per_second 必须是数值，实际为 {self.requests_per_second!r}"
            ) from exc
        if rate < 0:
            raise ValueError(f"requests_per_second 不能为负，实际为 {rate}")
        self.requests_per_second = rate
        if self.max_burst < 0:
            raise ValueError(f"max_burst 不能为负，实际为 {self.max_burst}")

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    @property
    def is_enabled(self) -> bool:
        """是否启用限速（``requests_per_second <= 0`` 即关闭）。"""
        return self.requests_per_second > 0

    @property
    def interval_seconds(self) -> float:
        """两次请求之间的最小间隔（秒）；关闭限速时为 ``0.0``。"""
        if not self.is_enabled:
            return 0.0
        return 1.0 / self.requests_per_second

    @classmethod
    def disabled(cls) -> RateLimiter:
        """构造一个不做任何限速的实例。"""
        return cls(requests_per_second=0.0)

    # ------------------------------------------------------------------ #
    # 放行
    # ------------------------------------------------------------------ #
    async def acquire(self) -> None:
        """领取一个放行名额；必要时等待到该名额的时间点。

        关闭限速时直接返回，连事件循环都不惊动。
        """
        if not self.is_enabled:
            self.acquired_count += 1
            return

        interval = self.interval_seconds
        loop = asyncio.get_running_loop()
        now = loop.time()

        if self._next_slot is None:
            # 首个请求立即放行，之后按间隔依次排。
            self._next_slot = now
        if self.max_burst > 0:
            # 允许积攒：把游标往前推也不会超过"现在 + 突发额度"
            earliest = now - self.max_burst * interval
            if self._next_slot < earliest:
                self._next_slot = earliest

        slot = self._next_slot
        self._next_slot = max(slot, now) + interval
        self.acquired_count += 1

        delay = slot - now
        if delay > 0:
            await asyncio.sleep(delay)

    def reset(self) -> None:
        """清空节流状态（关闭限速或复用实例时调用）。"""
        self._next_slot = None
