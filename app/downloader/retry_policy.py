"""重试策略（Slice 3）。

**纯逻辑模块**：只做"第几次尝试该等多久"与"这个失败值不值得重试"的判断，
不 ``import asyncio``、不 sleep、不发请求 —— 真正的等待由
:mod:`app.downloader.tile_downloader` 执行。这样整条退避曲线都可以用普通
单元测试断言，不需要真实等待。

退避曲线（``base = 0.5s``、``max = 5s``）::

    第 1 次失败后等待  0.5s
    第 2 次失败后等待  1.0s
    第 3 次失败后等待  2.0s
    第 4 次失败后等待  4.0s
    第 5 次失败后等待  5.0s   ← 被 max_delay_seconds 封顶
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.config.settings import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BASE_DELAY_SECONDS,
    DEFAULT_RETRY_MAX_DELAY_SECONDS,
)
from app.downloader.models import TileDownloadResult, TileFailureKind

__all__ = [
    "RETRYABLE_FAILURE_KINDS",
    "RETRYABLE_STATUS_CODES",
    "RetryPolicy",
    "is_retryable_status_code",
    "should_retry",
]


#: 明确值得重试的状态码。
#:
#: - ``408`` 请求超时 / ``425`` Too Early / ``429`` 被限流：都是"稍后再来"的语义。
#: - ``5xx`` 由 :func:`is_retryable_status_code` 的区间判断覆盖，不必逐个列出。
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 425, 429})

#: 值得重试的失败分类。
#:
#: 唯一**不重试**的是 :attr:`TileFailureKind.HTTP_STATUS`——它的答案取决于
#: 状态码（401/403/404 重试多少次都是同样的结果）。
RETRYABLE_FAILURE_KINDS: frozenset[TileFailureKind] = frozenset(
    {
        TileFailureKind.NETWORK,
        TileFailureKind.TIMEOUT,
        TileFailureKind.EMPTY_RESPONSE,
        TileFailureKind.INVALID_CONTENT,
        TileFailureKind.RESPONSE_TOO_LARGE,
        TileFailureKind.UNEXPECTED,
    }
)


def is_retryable_status_code(status_code: int | None) -> bool:
    """HTTP 状态码是否值得重试。

    规则（覆盖全部取值，不留"凭感觉"的灰区）::

        >= 500   → 重试（服务端抖动）
        408/425/429 → 重试（稍后再来的语义）
        其他 4xx → 不重试（400 参数错、401/403 Token 问题、404 瓦片不存在）
        2xx/3xx  → 不重试（能走到"失败"分支说明内容本身有问题，
                           重试同一份内容没有意义）
        None     → 不重试（没拿到响应，走 failure_kind 判定）

    >>> is_retryable_status_code(503)
    True
    >>> is_retryable_status_code(403)
    False
    >>> is_retryable_status_code(429)
    True
    """
    if status_code is None:
        return False
    if status_code >= 500:
        return True
    return status_code in RETRYABLE_STATUS_CODES


@dataclass
class RetryPolicy:
    """指数退避重试策略。

    Attributes:
        max_attempts: 最大尝试次数（**含首次请求**）。``3`` 表示最多重试 2 次。
        base_delay_seconds: 首次退避时长，之后逐次翻倍。
        max_delay_seconds: 单次退避上限，防止指数增长把任务拖死。
    """

    max_attempts: int = DEFAULT_MAX_RETRIES
    base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS
    max_delay_seconds: float = DEFAULT_RETRY_MAX_DELAY_SECONDS

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts 至少为 1，实际为 {self.max_attempts}")
        if self.base_delay_seconds < 0:
            raise ValueError(
                f"base_delay_seconds 不能为负，实际为 {self.base_delay_seconds}"
            )
        if self.max_delay_seconds < 0:
            raise ValueError(
                f"max_delay_seconds 不能为负，实际为 {self.max_delay_seconds}"
            )
        if self.base_delay_seconds > self.max_delay_seconds:
            raise ValueError(
                "base_delay_seconds 不能大于 max_delay_seconds，"
                f"实际 {self.base_delay_seconds} > {self.max_delay_seconds}"
            )

    # ------------------------------------------------------------------ #
    # 退避曲线
    # ------------------------------------------------------------------ #
    def delay_after_attempt(self, attempt: int) -> float:
        """第 ``attempt`` 次尝试失败后，下一次尝试前应等待的秒数。

        Args:
            attempt: 已完成的尝试序号，从 **1** 开始。

        Returns:
            退避秒数，范围 ``[base_delay_seconds, max_delay_seconds]``。

        Raises:
            ValueError: ``attempt < 1``。

        >>> RetryPolicy().delay_after_attempt(1)
        0.5
        >>> RetryPolicy().delay_after_attempt(2)
        1.0
        >>> RetryPolicy().delay_after_attempt(3)
        2.0
        >>> RetryPolicy().delay_after_attempt(5)  # 封顶
        5.0
        """
        if attempt < 1:
            raise ValueError(f"attempt 从 1 开始，实际为 {attempt}")
        # 2**(attempt-1) 在 attempt 很大时会变成天文数字，先按指数上限截断再算，
        # 避免构造出无意义的巨大整数（虽然 min() 最终也会封顶）。
        exponent = min(attempt - 1, 64)
        raw = self.base_delay_seconds * math.pow(2.0, exponent)
        return min(raw, self.max_delay_seconds)

    def delay_sequence(self) -> list[float]:
        """完整的退避序列，长度 ``max_attempts - 1``（最后一次失败后不再等待）。"""
        return [self.delay_after_attempt(n) for n in range(1, self.max_attempts)]


# --------------------------------------------------------------------------- #
# 结果判定
# --------------------------------------------------------------------------- #
def _is_retryable(result: TileDownloadResult) -> bool:
    """给定一次失败的尝试结果，判断是否值得重试。"""
    if result.success:
        return False

    kind = result.failure_kind
    if kind is TileFailureKind.HTTP_STATUS:
        return is_retryable_status_code(result.status_code)
    if kind is None:
        # 理论上不会发生（失败必然带分类），保守起见按状态码兜底。
        return is_retryable_status_code(result.status_code)
    return kind in RETRYABLE_FAILURE_KINDS


def should_retry(
    result: TileDownloadResult,
    attempt: int,
    policy: RetryPolicy,
) -> bool:
    """综合"失败原因"与"已用次数"判断是否继续重试。

    Args:
        result: 本次尝试的结果。
        attempt: 本次尝试的序号（从 1 开始）。
        policy: 重试策略。

    Returns:
        还能再试一次返回 ``True``，否则 ``False``。

    >>> from app.downloader.models import TileCoordinate, TileFailureKind
    >>> ok = TileDownloadResult(
    ...     TileCoordinate(1, 0, 0), True, b"\x89PNG\r\n\x1a\n", 200, 1.0, 1, None
    ... )
    >>> should_retry(ok, 1, RetryPolicy())
    False
    >>> denied = TileDownloadResult(
    ...     TileCoordinate(1, 0, 0), False, None, 403, 1.0, 1, "HTTP 403",
    ...     TileFailureKind.HTTP_STATUS,
    ... )
    >>> should_retry(denied, 1, RetryPolicy())
    False
    >>> flaky = TileDownloadResult(
    ...     TileCoordinate(1, 0, 0), False, None, 503, 1.0, 1, "HTTP 503",
    ...     TileFailureKind.HTTP_STATUS,
    ... )
    >>> should_retry(flaky, 1, RetryPolicy())
    True
    >>> should_retry(flaky, 3, RetryPolicy())
    False
    """
    if attempt >= policy.max_attempts:
        # 硬上限：无论失败原因是什么都不再重试，杜绝无限重试。
        return False
    return _is_retryable(result)
