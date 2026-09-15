"""重试策略单元测试（Slice 3）。

全部是纯逻辑：不联网、不 sleep（退避等待由 ``tile_downloader`` 执行，
本模块只回答"该等多久"）、不依赖事件循环。
"""

from __future__ import annotations

import pytest

from app.config.settings import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BASE_DELAY_SECONDS,
    DEFAULT_RETRY_MAX_DELAY_SECONDS,
)
from app.downloader.models import TileCoordinate, TileDownloadResult, TileFailureKind
from app.downloader.retry_policy import (
    RETRYABLE_FAILURE_KINDS,
    RETRYABLE_STATUS_CODES,
    RetryPolicy,
    is_retryable_status_code,
    should_retry,
)

TILE = TileCoordinate(z=12, x=3372, y=1552)


def _result(
    *,
    success: bool = False,
    status_code: int | None = None,
    kind: TileFailureKind | None = None,
    error: str | None = None,
) -> TileDownloadResult:
    """构造一个最小可用的下载结果。"""
    return TileDownloadResult(
        tile=TILE,
        success=success,
        content=b"\x89PNG\r\n\x1a\n" if success else None,
        status_code=status_code,
        elapsed_ms=1.0,
        attempts=1,
        error=error,
        failure_kind=kind,
    )


# --------------------------------------------------------------------------- #
# 默认值与构造校验
# --------------------------------------------------------------------------- #
def test_default_policy_values() -> None:
    policy = RetryPolicy()
    assert policy.max_attempts == DEFAULT_MAX_RETRIES == 3
    assert policy.base_delay_seconds == DEFAULT_RETRY_BASE_DELAY_SECONDS == 0.5
    assert policy.max_delay_seconds == DEFAULT_RETRY_MAX_DELAY_SECONDS == 5.0


def test_max_attempts_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=0)


def test_negative_delays_are_rejected() -> None:
    with pytest.raises(ValueError, match="base_delay_seconds"):
        RetryPolicy(base_delay_seconds=-1.0)
    with pytest.raises(ValueError, match="max_delay_seconds"):
        RetryPolicy(max_delay_seconds=-1.0)


def test_base_delay_cannot_exceed_max_delay() -> None:
    with pytest.raises(ValueError, match="不能大于"):
        RetryPolicy(base_delay_seconds=10.0, max_delay_seconds=1.0)


# --------------------------------------------------------------------------- #
# 指数退避曲线
# --------------------------------------------------------------------------- #
def test_exponential_backoff_sequence() -> None:
    """Spec 要求的 0.5s -> 1s -> 2s。"""
    policy = RetryPolicy()
    assert policy.delay_after_attempt(1) == 0.5
    assert policy.delay_after_attempt(2) == 1.0
    assert policy.delay_after_attempt(3) == 2.0


def test_backoff_is_capped_by_max_delay() -> None:
    policy = RetryPolicy(base_delay_seconds=0.5, max_delay_seconds=5.0)
    assert policy.delay_after_attempt(4) == 4.0
    assert policy.delay_after_attempt(5) == 5.0  # 8.0 被封顶
    assert policy.delay_after_attempt(50) == 5.0


def test_backoff_never_overflows() -> None:
    """极大的 attempt 不能炸出天文数字或抛异常。"""
    policy = RetryPolicy()
    assert policy.delay_after_attempt(10_000) == policy.max_delay_seconds


def test_custom_base_delay() -> None:
    policy = RetryPolicy(base_delay_seconds=0.25, max_delay_seconds=1.0)
    assert policy.delay_after_attempt(1) == 0.25
    assert policy.delay_after_attempt(2) == 0.5
    assert policy.delay_after_attempt(3) == 1.0
    assert policy.delay_after_attempt(4) == 1.0


def test_zero_base_delay_is_allowed() -> None:
    """测试里把退避设成 0 可以跑得飞快，但语义必须仍然正确。"""
    policy = RetryPolicy(base_delay_seconds=0.0)
    assert policy.delay_after_attempt(3) == 0.0


def test_attempt_index_starts_at_one() -> None:
    with pytest.raises(ValueError, match="attempt"):
        RetryPolicy().delay_after_attempt(0)
    with pytest.raises(ValueError, match="attempt"):
        RetryPolicy().delay_after_attempt(-1)


def test_delay_sequence_length_matches_attempts() -> None:
    """最后一次失败后不再等待，因此序列长度是 max_attempts - 1。"""
    policy = RetryPolicy(max_attempts=3)
    assert policy.delay_sequence() == [0.5, 1.0]
    assert RetryPolicy(max_attempts=1).delay_sequence() == []


# --------------------------------------------------------------------------- #
# 状态码判定
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status_code", [500, 502, 503, 504, 599])
def test_server_errors_are_retryable(status_code: int) -> None:
    assert is_retryable_status_code(status_code) is True


@pytest.mark.parametrize("status_code", [408, 425, 429])
def test_throttling_statuses_are_retryable(status_code: int) -> None:
    assert is_retryable_status_code(status_code) is True
    assert status_code in RETRYABLE_STATUS_CODES


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 405, 410, 418, 451])
def test_client_errors_are_not_retryable(status_code: int) -> None:
    """400/401/403/404 等重试多少次都是同样的结果，不该浪费配额。"""
    assert is_retryable_status_code(status_code) is False


def test_unknown_status_and_missing_response_are_not_retryable() -> None:
    assert is_retryable_status_code(None) is False
    assert is_retryable_status_code(200) is False
    assert is_retryable_status_code(301) is False


# --------------------------------------------------------------------------- #
# should_retry：失败原因 + 次数上限
# --------------------------------------------------------------------------- #
def test_success_is_never_retried() -> None:
    assert should_retry(_result(success=True, status_code=200), 1, RetryPolicy()) is False


@pytest.mark.parametrize(
    "kind",
    [
        TileFailureKind.NETWORK,
        TileFailureKind.TIMEOUT,
        TileFailureKind.EMPTY_RESPONSE,
        TileFailureKind.INVALID_CONTENT,
        TileFailureKind.RESPONSE_TOO_LARGE,
    ],
)
def test_content_and_network_failures_are_retried(kind: TileFailureKind) -> None:
    """Spec：网络异常 / timeout / 无效或空瓦片响应都要重试。"""
    assert kind in RETRYABLE_FAILURE_KINDS
    assert should_retry(_result(kind=kind, error="x"), 1, RetryPolicy()) is True


def test_http_status_retry_decision_follows_status_code() -> None:
    policy = RetryPolicy()
    assert (
        should_retry(
            _result(status_code=503, kind=TileFailureKind.HTTP_STATUS), 1, policy
        )
        is True
    )
    assert (
        should_retry(
            _result(status_code=403, kind=TileFailureKind.HTTP_STATUS), 1, policy
        )
        is False
    )
    assert (
        should_retry(
            _result(status_code=404, kind=TileFailureKind.HTTP_STATUS), 1, policy
        )
        is False
    )
    assert (
        should_retry(
            _result(status_code=429, kind=TileFailureKind.HTTP_STATUS), 1, policy
        )
        is True
    )


def test_should_retry_stops_at_max_attempts() -> None:
    """达到最大次数后，无论失败原因多"值得重试"都必须停。"""
    policy = RetryPolicy(max_attempts=3)
    flaky = _result(kind=TileFailureKind.TIMEOUT, error="超时")
    assert should_retry(flaky, 1, policy) is True
    assert should_retry(flaky, 2, policy) is True
    assert should_retry(flaky, 3, policy) is False
    assert should_retry(flaky, 99, policy) is False


def test_should_retry_with_max_attempts_one_never_retries() -> None:
    policy = RetryPolicy(max_attempts=1)
    assert should_retry(_result(kind=TileFailureKind.NETWORK), 1, policy) is False


def test_failure_without_kind_falls_back_to_status_code() -> None:
    """防御性分支：失败必然带分类，但万一缺失也不能一律重试。"""
    policy = RetryPolicy()
    assert should_retry(_result(status_code=503), 1, policy) is True
    assert should_retry(_result(status_code=403), 1, policy) is False
