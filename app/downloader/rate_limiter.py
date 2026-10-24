"""所有图层和重试共用的匀速调度、限流冷却与渐进恢复。"""
from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from app.config.settings import (
    DEFAULT_REQUESTS_PER_SECOND,
    RATE_LIMIT_COOLDOWN_BASE_SECONDS,
    RATE_LIMIT_COOLDOWN_MAX_SECONDS,
    RATE_LIMIT_JITTER_RATIO,
    RATE_LIMIT_MAX_THROTTLE_EPISODES,
    RATE_LIMIT_MIN_REQUESTS_PER_SECOND,
    RATE_LIMIT_RECOVERY_SECONDS,
    RATE_LIMIT_RECOVERY_SUCCESSES,
)

__all__ = ["RateLimitExceeded", "RateLimiter", "parse_retry_after"]


class RateLimitExceeded(RuntimeError):
    """持续限流，安全停止当前批次，等待用户稍后续传。"""


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """RFC 9110：支持非负整数秒和 HTTP 日期，非法值交由本地退避兜底。"""
    if not value:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        try:
            seconds = float(value)
        except ValueError:
            return None
        return seconds if math.isfinite(seconds) else None
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return None
        return max(0.0, (date - (now or datetime.now(UTC))).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


@dataclass
class RateLimiter:
    """发请求前复查共享时间点，避免已预约的请求穿过新设置的冷却期。

    ``requests_per_second`` 是 HTTP 请求总速率，包含底图、注记和重试。
    ``disabled()`` 显式关闭节流和自适应保护，供离线测试使用。
    ``max_burst`` 保留兼容参数；下载请求始终匀速，不积累空闲额度。
    """

    requests_per_second: float = DEFAULT_REQUESTS_PER_SECOND
    max_burst: int = 0
    min_requests_per_second: float = RATE_LIMIT_MIN_REQUESTS_PER_SECOND
    cooldown_base_seconds: float = RATE_LIMIT_COOLDOWN_BASE_SECONDS
    cooldown_max_seconds: float = RATE_LIMIT_COOLDOWN_MAX_SECONDS
    jitter_ratio: float = RATE_LIMIT_JITTER_RATIO
    recovery_successes: int = RATE_LIMIT_RECOVERY_SUCCESSES
    recovery_seconds: float = RATE_LIMIT_RECOVERY_SECONDS
    max_throttle_episodes: int = RATE_LIMIT_MAX_THROTTLE_EPISODES
    acquired_count: int = field(default=0, init=False)
    _next_slot: float | None = field(default=None, init=False, repr=False)
    _cooldown_until: float = field(default=0.0, init=False, repr=False)
    _effective_rate: float = field(default=0.0, init=False, repr=False)
    _generation: int = field(default=0, init=False, repr=False)
    _episodes: int = field(default=0, init=False, repr=False)
    _successes: int = field(default=0, init=False, repr=False)
    _last_throttle: float = field(default=0.0, init=False, repr=False)
    _exhausted: bool = field(default=False, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.requests_per_second = float(self.requests_per_second)
        for name in ("requests_per_second", "min_requests_per_second", "cooldown_base_seconds",
                     "cooldown_max_seconds", "jitter_ratio", "recovery_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} 必须是有限非负数")
        if self.min_requests_per_second <= 0 or self.cooldown_base_seconds <= 0:
            raise ValueError("最低速率和冷却时长必须为正数")
        if self.cooldown_max_seconds < self.cooldown_base_seconds:
            raise ValueError("最大冷却时间不能小于基础冷却时间")
        if self.max_burst < 0 or self.recovery_successes < 1 or self.max_throttle_episodes < 1:
            raise ValueError("突发量不能为负，恢复计数和限流上限必须为正数")
        self._effective_rate = self.requests_per_second

    @property
    def is_enabled(self) -> bool:
        return self.requests_per_second > 0

    @property
    def effective_requests_per_second(self) -> float:
        return self._effective_rate

    @property
    def interval_seconds(self) -> float:
        return 1.0 / self._effective_rate if self.is_enabled else 0.0

    @property
    def requests_blocked(self) -> bool:
        """复查已领取名额是否被稍后到达的 429 作废。"""
        return self.is_enabled and (
            self._exhausted or asyncio.get_running_loop().time() < self._cooldown_until
        )

    @property
    def generation(self) -> int:
        """请求发出时的冷却代次，用于忽略过时的成功和合并同时返回的 429。"""
        return self._generation

    @classmethod
    def disabled(cls) -> RateLimiter:
        return cls(requests_per_second=0.0)

    async def acquire(self) -> None:
        if not self.is_enabled:
            self.acquired_count += 1
            return
        async with self._lock:
            await self._acquire_slot()

    async def _acquire_slot(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            if self._exhausted:
                raise RateLimitExceeded(
                    "服务端持续返回 HTTP 429，已停止本轮下载并保留已完成瓦片；"
                    "请稍后继续下载，并检查 Token 配额或同账号的其他下载程序"
                )
            now = loop.time()
            target = max(self._next_slot or now, self._cooldown_until)
            if target > now:
                await asyncio.sleep(target - now)
                continue  # 等待期间可能收到新的 429，必须重新检查。
            self._next_slot = now + self.interval_seconds
            self.acquired_count += 1
            return

    def on_throttled(self, retry_after: float | None, *, generation: int) -> float:
        """设置全局冷却并减半速率；同一代已在途请求只算一次限流事件。"""
        if not self.is_enabled:
            return 0.0
        now = asyncio.get_running_loop().time()
        server_delay = retry_after or 0.0
        if generation != self._generation:
            self._cooldown_until = max(self._cooldown_until, now + server_delay)
            return max(0.0, self._cooldown_until - now)
        self._generation += 1
        self._episodes += 1
        self._successes = 0
        self._last_throttle = now
        floor = min(self.requests_per_second, self.min_requests_per_second)
        self._effective_rate = max(floor, self._effective_rate / 2)
        local_delay = min(
            self.cooldown_max_seconds,
            self.cooldown_base_seconds * 2 ** min(self._episodes - 1, 16),
        )
        # 只增加等待，绝不能用抖动缩短服务端要求的 Retry-After。
        delay = max(server_delay, local_delay) + random.uniform(0, local_delay * self.jitter_ratio)
        self._cooldown_until = max(self._cooldown_until, now + delay)
        self._exhausted = self._episodes >= self.max_throttle_episodes
        return self._cooldown_until - now

    def on_success(self, *, generation: int) -> bool:
        """稳定一段时间且积累足够成功响应后，逐步恢复，不超过配置上限。"""
        if not self.is_enabled or generation != self._generation or not self._episodes:
            return False
        now = asyncio.get_running_loop().time()
        if now < self._cooldown_until or self._exhausted:
            return False
        self._successes += 1
        if self._successes < self.recovery_successes or now - self._last_throttle < self.recovery_seconds:
            return False
        self._successes = 0
        self._last_throttle = now
        self._episodes = max(0, self._episodes - 1)
        self._effective_rate = min(self.requests_per_second, self._effective_rate * 1.25)
        # 仍未回到原速率时继续要求稳定窗口。
        if self._effective_rate < self.requests_per_second:
            self._episodes = max(1, self._episodes)
        return True

    def reset(self) -> None:
        self._next_slot = None
        self._cooldown_until = 0.0
        self._effective_rate = self.requests_per_second
        self._generation = 0
        self._episodes = self._successes = 0
        self._last_throttle = 0.0
        self._exhausted = False
