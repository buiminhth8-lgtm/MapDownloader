"""单瓦片下载器单元测试（Slice 3）。

**所有 HTTP 都走 ``httpx.MockTransport``，测试不触网。** 唯一的"慢"来源是
重试退避，因此用例统一使用 :data:`FAST_POLICY`（退避 0 秒）——这样能在
毫秒级验证"重试了几次"，而不用真的睡 1.5 秒。
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from collections.abc import Callable

import httpx
import pytest

from app.config.settings import DEFAULT_REQUEST_TIMEOUT
from app.downloader import (
    RateLimiter,
    RetryPolicy,
    TileCoordinate,
    TileDownloader,
    TileFailureKind,
)
from app.models import MapLayerType
from app.providers import (
    JPEG_SIGNATURE,
    PNG_SIGNATURE,
    TiandituProvider,
)

#: 假 Token —— 严禁替换成真实密钥
FAKE_TOKEN = "abcdefghijklmn"

TILE = TileCoordinate(z=12, x=3372, y=1552)
LAYER = MapLayerType.VECTOR

#: 只需文件头正确：合法性判定依据是 magic bytes
FAKE_PNG = PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + b"tile-payload" * 16
FAKE_JPEG = JPEG_SIGNATURE + b"\xff\xe0\x00\x10JFIF" + b"tile-payload" * 16

#: 退避为 0 的重试策略：行为与生产一致，但跑得飞快
FAST_POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=0.0, max_delay_seconds=0.0)

#: 不做限速（限速本身另有专门用例）
NO_RATE_LIMIT = RateLimiter.disabled()


# --------------------------------------------------------------------------- #
# 测试脚手架
# --------------------------------------------------------------------------- #
def _image(content: bytes, content_type: str = "image/png") -> httpx.Response:
    return httpx.Response(200, content=content, headers={"content-type": content_type})


def _status(status_code: int, content: bytes = b"", content_type: str = "text/html"):
    return httpx.Response(
        status_code, content=content, headers={"content-type": content_type}
    )


class Scripted:
    """按调用顺序依次返回预设动作的传输层替身。

    ``actions`` 里的元素可以是 :class:`httpx.Response`、异常实例，或
    ``Callable[[httpx.Request], httpx.Response]``。用完之后重复最后一个动作。

    这样"前两次 503、第三次 200"这种剧本可以直接写出来，
    既能断言重试次数，也能断言最终结果。
    """

    def __init__(self, actions: list[object]) -> None:
        if not actions:
            raise ValueError("actions 不能为空")
        self._actions = list(actions)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        action = (
            self._actions.pop(0) if len(self._actions) > 1 else self._actions[0]
        )
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return action(request)
        return action  # type: ignore[return-value]

    @property
    def call_count(self) -> int:
        return len(self.requests)


def _make_downloader(
    scripted: Scripted | Callable[[httpx.Request], httpx.Response],
    *,
    policy: RetryPolicy = FAST_POLICY,
    rate_limiter: object | None = None,
    timeout: float = DEFAULT_REQUEST_TIMEOUT,
) -> TileDownloader:
    provider = TiandituProvider(FAKE_TOKEN)
    return TileDownloader(
        provider,
        transport=httpx.MockTransport(scripted),
        retry_policy=policy,
        rate_limiter=rate_limiter or NO_RATE_LIMIT,  # type: ignore[arg-type]
        timeout=timeout,
    )


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #
async def test_png_response_succeeds() -> None:
    scripted = Scripted([_image(FAKE_PNG)])
    async with _make_downloader(scripted) as downloader:
        result = await downloader.download_tile(TILE, LAYER)

    assert result.success is True
    assert result.tile == TILE
    assert result.status_code == 200
    assert result.content == FAKE_PNG  # 字节留在内存里，不落盘
    assert result.content_length == len(FAKE_PNG)
    assert result.attempts == 1
    assert result.error is None
    assert result.failure_kind is None
    assert result.elapsed_ms >= 0.0
    assert scripted.call_count == 1


async def test_jpeg_response_succeeds() -> None:
    scripted = Scripted([_image(FAKE_JPEG, "image/jpeg")])
    async with _make_downloader(scripted) as downloader:
        result = await downloader.download_tile(TILE, LAYER)

    assert result.success is True
    assert result.content == FAKE_JPEG


async def test_download_four_layers() -> None:
    """四种图层都能下载（URL 由 Slice 2 的 Provider 负责，这里只验证通路）。"""
    scripted = Scripted([_image(FAKE_PNG)])
    async with _make_downloader(scripted) as downloader:
        for layer in MapLayerType:
            result = await downloader.download_tile(TILE, layer)
            assert result.success is True

    assert scripted.call_count == len(MapLayerType)
    queried_layers = [r.url.params["LAYER"] for r in scripted.requests]
    assert queried_layers == ["vec", "cva", "img", "cia"]


# --------------------------------------------------------------------------- #
# 响应内容判定
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"<html><body>Invalid Token</body></html>", "text/html"),
        (b"<?xml version='1.0'?><ServiceException>Bad</ServiceException>", "text/xml"),
        (b'{"error": "Token is invalid"}', "application/json"),
        (b"Token\xe9\x94\x99\xe8\xaf\xaf", "text/plain"),
        (b"\x00\x01\x02\x03binary-garbage", "application/octet-stream"),
    ],
)
async def test_non_image_payload_fails_and_is_retried(
    content: bytes, content_type: str
) -> None:
    """HTTP 200 的错误页不能当成瓦片，且属于"值得重试"的失败。"""
    scripted = Scripted([_status(200, content, content_type)])
    async with _make_downloader(scripted) as downloader:
        result = await downloader.download_tile(TILE, LAYER)

    assert result.success is False
    assert result.failure_kind is TileFailureKind.INVALID_CONTENT
    assert "不是有效的图片瓦片" in (result.error or "")
    assert result.attempts == FAST_POLICY.max_attempts  # 无效响应会重试
    assert scripted.call_count == FAST_POLICY.max_attempts


async def test_empty_response_fails() -> None:
    scripted = Scripted([_status(200, b"", "image/png")])
    async with _make_downloader(scripted) as downloader:
        result = await downloader.download_tile(TILE, LAYER)

    assert result.success is False
    assert result.failure_kind is TileFailureKind.EMPTY_RESPONSE
    assert result.attempts == FAST_POLICY.max_attempts


async def test_oversized_response_fails() -> None:
    from app.config.settings import MAX_TILE_RESPONSE_BYTES

    huge = PNG_SIGNATURE + b"\x00" * MAX_TILE_RESPONSE_BYTES
    scripted = Scripted([_image(huge)])
    async with _make_downloader(scripted) as downloader:
        result = await downloader.download_tile(TILE, LAYER)

    assert result.success is False
    assert result.failure_kind is TileFailureKind.RESPONSE_TOO_LARGE
    assert "体积异常" in (result.error or "")
    # 真实体积超限的响应不应被留在内存里
    assert result.content is None


# --------------------------------------------------------------------------- #
# HTTP 状态码
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status_code", [401, 403, 404, 400])
async def test_unauthorized_and_missing_tiles_are_not_retried(status_code: int) -> None:
    """Spec：400/401/403/404 不做无意义重试，一次就定论。"""
    scripted = Scripted([_status(status_code, b"<html>denied</html>")])
    async with _make_downloader(scripted) as downloader:
        result = await downloader.download_tile(TILE, LAYER)

    assert result.success is False
    assert result.status_code == status_code
    assert result.failure_kind is TileFailureKind.HTTP_STATUS
    assert result.attempts == 1
    assert scripted.call_count == 1
    assert f"HTTP {status_code}" in (result.error or "")


async def test_forbidden_error_message_mentions_token() -> None:
    scripted = Scripted([_status(403)])
    async with _make_downloader(scripted) as downloader:
        result = await downloader.download_tile(TILE, LAYER)

    assert "Token" in (result.error or "")


async def test_too_many_requests_is_retried_then_succeeds() -> None:
    """429 是"稍后再来"，重试一次就成功。"""
    scripted = Scripted([_status(429, b"slow down"), _image(FAKE_PNG)])
    async with _make_downloader(scripted) as downloader:
        result = await downloader.download_tile(TILE, LAYER)

    assert result.success is True
    assert result.attempts == 2
    assert result.was_retried is True
    assert scripted.call_count == 2


async def test_server_error_is_retried_up_to_max_attempts() -> None:
    scripted = Scripted([_status(503, b"unavailable")])
    async with _make_downloader(scripted) as downloader:
        result = await downloader.download_tile(TILE, LAYER)

    assert result.success is False
    assert result.status_code == 503
    assert result.attempts == FAST_POLICY.max_attempts
    assert scripted.call_count == FAST_POLICY.max_attempts


async def test_server_error_recovers_on_third_attempt() -> None:
    scripted = Scripted(
        [_status(500, b"boom"), _status(502, b"bad gateway"), _image(FAKE_PNG)]
    )
    async with _make_downloader(scripted) as downloader:
        result = await downloader.download_tile(TILE, LAYER)

    assert result.success is True
    assert result.attempts == 3
    assert result.status_code == 200


async def test_retry_count_follows_policy() -> None:
    """把 max_attempts 调到 5，失败响应就该被请求 5 次。"""
    policy = RetryPolicy(max_attempts=5, base_delay_seconds=0.0, max_delay_seconds=0.0)
    scripted = Scripted([_status(500)])
    async with _make_downloader(scripted, policy=policy) as downloader:
        result = await downloader.download_tile(TILE, LAYER)

    assert result.attempts == 5
    assert scripted.call_count == 5


# --------------------------------------------------------------------------- #
# 网络异常
# --------------------------------------------------------------------------- #
def _raise(exc: BaseException):
    def action(request: httpx.Request) -> httpx.Response:
        raise exc

    return action


async def test_timeout_is_retried_and_never_raises() -> None:
    scripted = Scripted([_raise(httpx.ReadTimeout("read timed out"))])
    async with _make_downloader(scripted) as downloader:
        result = await downloader.download_tile(TILE, LAYER)

    assert result.success is False
    assert result.failure_kind is TileFailureKind.TIMEOUT
    assert result.status_code is None
    assert "请求超时" in (result.error or "")
    assert result.attempts == FAST_POLICY.max_attempts


async def test_connect_error_is_retried() -> None:
    scripted = Scripted([_raise(httpx.ConnectError("connection refused"))])
    async with _make_downloader(scripted) as downloader:
        result = await downloader.download_tile(TILE, LAYER)

    assert result.success is False
    assert result.failure_kind is TileFailureKind.NETWORK
    assert "网络错误" in (result.error or "")
    assert result.attempts == FAST_POLICY.max_attempts


async def test_network_error_then_success() -> None:
    scripted = Scripted(
        [_raise(httpx.ConnectError("dns failure")), _image(FAKE_PNG)]
    )
    async with _make_downloader(scripted) as downloader:
        result = await downloader.download_tile(TILE, LAYER)

    assert result.success is True
    assert result.attempts == 2


async def test_unexpected_exception_is_folded_into_result() -> None:
    """非 httpx 的意外异常同样不能泄漏给调用方。"""
    scripted = Scripted([_raise(RuntimeError("boom"))])
    async with _make_downloader(scripted) as downloader:
        result = await downloader.download_tile(TILE, LAYER)

    assert result.success is False
    assert result.failure_kind is TileFailureKind.UNEXPECTED
    assert "boom" in (result.error or "")
    assert result.attempts == FAST_POLICY.max_attempts


async def test_invalid_arguments_raise_without_any_request() -> None:
    """参数非法属于调用方错误：显式抛 ValueError，且一次请求都不发。"""
    scripted = Scripted([_image(FAKE_PNG)])
    async with _make_downloader(scripted) as downloader:
        with pytest.raises(ValueError):
            await downloader.download_tile("12/1/2", LAYER)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            await downloader.download_tile(TileCoordinate(23, 0, 0), LAYER)
        with pytest.raises(ValueError):
            await downloader.download_tile(TileCoordinate(1, 2, 0), LAYER)
        with pytest.raises(ValueError):
            await downloader.download_tile(TILE, "vec_w")  # type: ignore[arg-type]

    assert scripted.call_count == 0


# --------------------------------------------------------------------------- #
# HTTP 客户端生命周期
# --------------------------------------------------------------------------- #
async def test_client_is_created_once_and_reused() -> None:
    """Spec 红线：不允许每张瓦片建一个 client。"""
    scripted = Scripted([_image(FAKE_PNG)])
    async with _make_downloader(scripted) as downloader:
        first = downloader.client
        for index in range(5):
            await downloader.download_tile(TileCoordinate(12, 3370 + index, 1552), LAYER)
        assert downloader.client is first
        assert scripted.call_count == 5


async def test_client_is_closed_on_context_exit() -> None:
    scripted = Scripted([_image(FAKE_PNG)])
    downloader = _make_downloader(scripted)
    async with downloader:
        client = downloader.client
        assert client.is_closed is False
    assert client.is_closed is True


async def test_close_is_idempotent() -> None:
    scripted = Scripted([_image(FAKE_PNG)])
    downloader = _make_downloader(scripted)
    await downloader.client.get("https://example.invalid/x")
    await downloader.close()
    await downloader.close()  # 再关一次不能炸


async def test_injected_client_is_not_closed_by_downloader() -> None:
    """注入的 client 生命周期归调用方，下载器不得擅自关闭。"""
    scripted = Scripted([_image(FAKE_PNG)])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(scripted), timeout=DEFAULT_REQUEST_TIMEOUT
    ) as client:
        downloader = TileDownloader(
            TiandituProvider(FAKE_TOKEN), client=client, rate_limiter=NO_RATE_LIMIT
        )
        result = await downloader.download_tile(TILE, LAYER)
        await downloader.close()
        assert client.is_closed is False
    assert result.success is True


async def test_client_and_transport_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="二选一"):
        TileDownloader(
            TiandituProvider(FAKE_TOKEN),
            client=httpx.AsyncClient(),
            transport=httpx.MockTransport(Scripted([_image(FAKE_PNG)])),
        )


async def test_timeout_is_wired_into_the_client() -> None:
    """超时必须真的挂在 client / 请求上，而不是只存在于参数列表里。"""
    scripted = Scripted([_image(FAKE_PNG)])
    downloader = _make_downloader(scripted, timeout=3.5)
    assert downloader.client.timeout.read == 3.5
    assert downloader.client.timeout.connect == 3.5
    await downloader.close()


def test_non_positive_timeout_is_rejected() -> None:
    with pytest.raises(ValueError, match="timeout"):
        TileDownloader(TiandituProvider(FAKE_TOKEN), timeout=0)


# --------------------------------------------------------------------------- #
# 限速
# --------------------------------------------------------------------------- #
class _CountingLimiter:
    """只统计 acquire 次数的限速器替身（不做真实等待）。"""

    def __init__(self) -> None:
        self.acquires = 0

    async def acquire(self) -> None:
        self.acquires += 1


async def test_rate_limiter_is_consulted_for_every_attempt() -> None:
    """重试同样消耗配额，所以每一次尝试前都要过限速器。"""
    limiter = _CountingLimiter()
    scripted = Scripted([_status(500)])
    async with _make_downloader(scripted, rate_limiter=limiter) as downloader:
        await downloader.download_tile(TILE, LAYER)

    assert limiter.acquires == FAST_POLICY.max_attempts
    assert scripted.call_count == FAST_POLICY.max_attempts


async def test_disabled_rate_limiter_adds_no_delay() -> None:
    limiter = RateLimiter.disabled()
    assert limiter.is_enabled is False
    assert limiter.interval_seconds == 0.0

    scripted = Scripted([_image(FAKE_PNG)])
    async with _make_downloader(scripted, rate_limiter=limiter) as downloader:
        started = asyncio.get_running_loop().time()
        await downloader.download_tile(TILE, LAYER)
        elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.05
    assert limiter.acquired_count == 1


async def test_rate_limiter_paces_concurrent_requests() -> None:
    """20 req/s 意味着相邻放行至少间隔 50ms —— 足以证明限速真的在起作用。"""
    limiter = RateLimiter(requests_per_second=20)
    scripted = Scripted([_image(FAKE_PNG)])
    async with _make_downloader(scripted, rate_limiter=limiter) as downloader:
        loop = asyncio.get_running_loop()
        started = loop.time()
        for index in range(4):
            await downloader.download_tile(TileCoordinate(12, 100 + index, 200), LAYER)
        elapsed = loop.time() - started

    # 4 次请求 = 3 个间隔 × 50ms = 150ms（留足余量，只验证下界）
    assert elapsed >= 0.13
    assert limiter.acquired_count == 4


# --------------------------------------------------------------------------- #
# 日志与副作用
# --------------------------------------------------------------------------- #
async def test_logs_never_leak_full_token(caplog) -> None:
    scripted = Scripted([_image(FAKE_PNG)])
    with caplog.at_level(
        logging.DEBUG, logger="app.downloader.tile_downloader"
    ):
        async with _make_downloader(scripted) as downloader:
            await downloader.download_tile(TILE, LAYER)

    text = caplog.text
    assert FAKE_TOKEN not in text
    assert "abcd****lmn" in text  # 脱敏后的 URL 仍然可读，便于排查


async def test_success_logs_are_debug_only(caplog) -> None:
    """每张成功瓦片只能打 DEBUG：几百张瓦片的 INFO 会把日志淹掉。"""
    scripted = Scripted([_image(FAKE_PNG)])
    with caplog.at_level(logging.INFO, logger="app.downloader.tile_downloader"):
        async with _make_downloader(scripted) as downloader:
            await downloader.download_tile(TILE, LAYER)

    assert caplog.records == []


async def test_no_files_are_written(monkeypatch) -> None:
    """本 Slice 只做引擎验证：不允许产生任何落盘副作用。"""

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("下载引擎不得写文件")

    monkeypatch.setattr(pathlib.Path, "open", _forbidden)
    monkeypatch.setattr(pathlib.Path, "write_bytes", _forbidden)
    monkeypatch.setattr(pathlib.Path, "write_text", _forbidden)
    monkeypatch.setattr(pathlib.Path, "mkdir", _forbidden)

    scripted = Scripted([_image(FAKE_PNG)])
    async with _make_downloader(scripted) as downloader:
        result = await downloader.download_tile(TILE, LAYER)

    assert result.success is True
    assert result.content == FAKE_PNG


async def test_elapsed_ms_covers_backoff_wait() -> None:
    """elapsed_ms 是用户感知的"这一张花了多久"，必须含退避等待。"""
    policy = RetryPolicy(
        max_attempts=2, base_delay_seconds=0.05, max_delay_seconds=0.05
    )
    scripted = Scripted([_status(503), _image(FAKE_PNG)])
    async with _make_downloader(scripted, policy=policy) as downloader:
        result = await downloader.download_tile(TILE, LAYER)

    assert result.success is True
    assert result.attempts == 2
    assert result.elapsed_ms >= 50.0
    assert result.summary().startswith("12/3372/1552")
