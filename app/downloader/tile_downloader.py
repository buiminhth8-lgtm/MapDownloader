"""单瓦片下载器（Slice 3）。

职责：**把一张瓦片从 URL 变成内存里的字节**，并负责超时、限速、重试与响应
合法性判定。它不知道"一共有多少张瓦片"，也不写任何文件——批量调度在
:mod:`app.downloader.download_manager`，落盘在 Slice 4 的 MBTilesWriter。

与 Slice 2 的关系
-----------------

URL 构造与响应合法性判定**全部复用** ``app.providers``：

- ``TiandituProvider.build_tile_url`` —— WMTS URL（不做第二份参数拼接）
- ``is_valid_tile_response`` / ``describe_response`` —— 按 magic bytes 判图
- ``http_error_message`` —— 状态码语义的唯一出处
- ``mask_url`` —— 日志里的 Token 一律脱敏

本模块相对 Slice 2 的 ``test_tile`` 多了三件事：**连接复用**（一个
``AsyncClient`` 服务整个任务）、**限速**、**指数退避重试**。

参数校验约定（与 Slice 2 一致）
------------------------------

Token 为空 / zoom 越界 / x、y 越界 / 图层类型不对 → **抛 ``ValueError``**。
这是调用方的编程错误，不做静默吞并（批量场景下由 worker 兜底成失败结果）。
网络与响应内容问题 → 一律变成 :class:`TileDownloadResult`，不向上泄漏异常。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from types import TracebackType

import httpx

from app.config.settings import (
    DEFAULT_REQUEST_TIMEOUT,
    HTTP_USER_AGENT,
    MAX_TILE_RESPONSE_BYTES,
    TIANDITU_ACCEPT_HEADER,
)
from app.downloader.models import TileCoordinate, TileDownloadResult, TileFailureKind
from app.downloader.rate_limiter import RateLimiter, parse_retry_after
from app.downloader.retry_policy import RetryPolicy, should_retry
from app.models.map_layer import MapLayerType
from app.providers import (
    TiandituProvider,
    describe_exception,
    describe_response,
    http_error_message,
    is_valid_tile_response,
    mask_url,
)

__all__ = ["TileDownloader"]

_logger = logging.getLogger(__name__)


#: 错误文案（与 Slice 2 的措辞保持一致，便于用户在两处看到同样的说法）
_ERROR_TIMEOUT = "请求超时"
_ERROR_NETWORK = "网络错误"
_ERROR_EMPTY = "响应内容为空"


@dataclass
class _AttemptResult:
    """**单次**HTTP 尝试的结果。

    刻意不是 :class:`TileDownloadResult`：后者描述的是"这张瓦片最终怎么样"
    （含重试次数与总耗时），而这个是"这一发请求怎么样"。两者混用会让
    ``attempts`` / ``elapsed_ms`` 的语义变得含糊。
    """

    success: bool
    content: bytes | None
    status_code: int | None
    elapsed_ms: float
    error: str | None = None
    failure_kind: TileFailureKind | None = None

    def as_result(self, tile: TileCoordinate) -> TileDownloadResult:
        """转成公开结果（``attempts`` / 总耗时由调用方填）。"""
        return TileDownloadResult(
            tile=tile,
            success=self.success,
            content=self.content,
            status_code=self.status_code,
            elapsed_ms=self.elapsed_ms,
            attempts=1,
            error=self.error,
            failure_kind=self.failure_kind,
        )


class TileDownloader:
    """带超时 / 限速 / 重试的单瓦片下载器。

    典型的用法是整个批量任务共用一个实例::

        async with TileDownloader(provider) as downloader:
            result = await downloader.download_tile(TileCoordinate(12, 3372, 1552), layer)

    ``AsyncClient`` **创建一次、整个任务复用**：每张瓦片新建一个 client 会带来
    重复的 DNS 解析与 TCP/TLS 握手，在本项目的量级（数万瓦片）下是致命的。
    客户端在第一次真正发请求时才创建，因此本类可以在事件循环之外构造。
    """

    def __init__(
        self,
        provider: TiandituProvider,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_policy: RetryPolicy | None = None,
        rate_limiter: RateLimiter | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            provider: 瓦片源（持有 Token）。
            client: 直接注入的 ``AsyncClient``。注入时**本类不负责关闭它**
                （生命周期归调用方）；与 ``transport`` 二选一。
            transport: httpx 异步传输层，仅供测试注入 ``httpx.MockTransport``。
                生产环境保持 ``None``。
            retry_policy: 重试策略，默认 :class:`RetryPolicy`。
            rate_limiter: 限速器，默认按 ``DEFAULT_REQUESTS_PER_SECOND`` 限速；
                传 :meth:`RateLimiter.disabled` 可关闭。
            timeout: 单次请求超时（秒）。
            logger: 日志器，默认用本模块的 logger。

        Raises:
            ValueError: 超时为非正数，或 ``client`` 与 ``transport`` 同时给出。
        """
        if client is not None and transport is not None:
            raise ValueError("client 与 transport 不能同时提供，请二选一")
        if timeout <= 0:
            raise ValueError(f"timeout 必须为正数，实际为 {timeout}")

        self._provider = provider
        self._transport = transport
        self._client = client
        self._owns_client = client is None
        self._timeout = float(timeout)
        self._policy = retry_policy or RetryPolicy()
        self._rate_limiter = rate_limiter or RateLimiter()
        self._logger = logger or _logger
        self._request_gate: asyncio.Event | None = None
        self._request_cancel: asyncio.Event | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    @property
    def client(self) -> httpx.AsyncClient:
        """惰性创建并复用同一个 ``AsyncClient``。"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=True,
                headers={
                    "Accept": TIANDITU_ACCEPT_HEADER,
                    "User-Agent": HTTP_USER_AGENT,
                },
            )
        return self._client

    async def close(self) -> None:
        """关闭内部创建的客户端。注入的客户端不会被关闭。"""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> TileDownloader:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    # 只读属性
    # ------------------------------------------------------------------ #
    @property
    def provider(self) -> TiandituProvider:
        """当前使用的瓦片源。"""
        return self._provider

    @property
    def retry_policy(self) -> RetryPolicy:
        """当前重试策略。"""
        return self._policy

    @property
    def rate_limiter(self) -> RateLimiter:
        """当前限速器。"""
        return self._rate_limiter

    def set_request_control(
        self, gate: asyncio.Event | None, cancel_event: asyncio.Event | None
    ) -> None:
        """让每个 HTTP 尝试（包含重试和冷却）服从批量任务的暂停与取消。"""
        self._request_gate = gate
        self._request_cancel = cancel_event

    async def _controlled_wait(self, awaitable):
        cancel = self._request_cancel
        if cancel is None:
            return await awaitable
        pending = asyncio.ensure_future(awaitable)
        cancelled = asyncio.create_task(cancel.wait())
        try:
            await asyncio.wait((pending, cancelled), return_when=asyncio.FIRST_COMPLETED)
            if cancel.is_set():
                raise asyncio.CancelledError
            return await pending
        finally:
            for task in (pending, cancelled):
                if not task.done():
                    task.cancel()
            await asyncio.gather(pending, cancelled, return_exceptions=True)

    async def _wait_to_request(self) -> None:
        while True:
            gate = self._request_gate
            if gate is not None:
                await self._controlled_wait(gate.wait())
            await self._controlled_wait(self._rate_limiter.acquire())
            # 控制等待的收尾也可能让出事件循环：HTTP 真正发出前再复查新冷却。
            if isinstance(self._rate_limiter, RateLimiter) and self._rate_limiter.requests_blocked:
                continue
            # 暂停可能发生在节流等待期间；恢复时重新排队，避免积累名额后突发。
            if gate is None or gate.is_set():
                return

    # ------------------------------------------------------------------ #
    # 下载
    # ------------------------------------------------------------------ #
    async def download_tile(
        self,
        tile: TileCoordinate,
        layer: MapLayerType,
    ) -> TileDownloadResult:
        """下载一张瓦片，必要时按策略重试。

        Args:
            tile: 瓦片坐标。
            layer: 图层类型。

        Returns:
            :class:`TileDownloadResult`。网络异常、超时、HTTP 非 200、
            空响应、非法图片内容都会被转换成结果对象（``success=False``），
            **不会**向调用方抛异常。

        Raises:
            ValueError: 瓦片坐标或图层参数非法。这类错误重试多少次都是同样
                结果，因此直接暴露（批量场景由 worker 兜底捕获）。
            RateLimitExceeded: 持续限流触发保护；批量调度器保留成果并停止本轮。
        """
        if not isinstance(tile, TileCoordinate):
            raise ValueError(
                f"tile 必须是 TileCoordinate，实际为 {type(tile).__name__}"
            )
        # URL 只构造一次：参数校验也随之只做一次，非法参数不会进入重试循环。
        url = self._provider.build_tile_url(layer, tile.z, tile.x, tile.y)
        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug("下载瓦片 %s（%s）", tile, mask_url(url))

        started = time.perf_counter()
        attempt = 0
        outcome: _AttemptResult | None = None

        while True:
            attempt += 1
            outcome = await self._attempt(url)

            if outcome.success:
                break
            if not should_retry(
                outcome.as_result(tile), attempt, self._policy
            ):
                if attempt > 1:
                    self._logger.debug(
                        "瓦片 %s 放弃重试（已尝试 %d 次）：%s", tile, attempt, outcome.error
                    )
                break

            if outcome.status_code == 429 and isinstance(self._rate_limiter, RateLimiter) and self._rate_limiter.is_enabled:
                # 429 已更新共享冷却；下一次尝试与所有图层一起等待。
                continue
            delay = self._policy.delay_after_attempt(attempt)
            self._logger.debug(
                "瓦片 %s 第 %d 次尝试失败（%s），%.2fs 后重试",
                tile,
                attempt,
                outcome.error,
                delay,
            )
            if delay > 0:
                await self._controlled_wait(asyncio.sleep(delay))

        result = outcome.as_result(tile)
        # 总耗时包含退避等待——用户感知的"这一张花了多久"就是它。
        result.elapsed_ms = (time.perf_counter() - started) * 1000.0
        result.attempts = attempt
        if result.success:
            self._logger.debug("瓦片 %s 下载完成：%d bytes", tile, result.content_length)
        return result

    # ------------------------------------------------------------------ #
    # 单次尝试
    # ------------------------------------------------------------------ #
    async def _attempt(self, url: str) -> _AttemptResult:
        """发一次请求并判定响应，把传输异常折叠成结果对象，控制指令与限流停止向上传递。"""
        await self._wait_to_request()
        generation = self._rate_limiter.generation if isinstance(self._rate_limiter, RateLimiter) else 0

        request_started = time.perf_counter()
        try:
            response = await self.client.get(url)
        except httpx.TimeoutException:
            return _AttemptResult(
                success=False,
                content=None,
                status_code=None,
                elapsed_ms=(time.perf_counter() - request_started) * 1000.0,
                error=f"{_ERROR_TIMEOUT}（>{self._timeout:g} 秒）",
                failure_kind=TileFailureKind.TIMEOUT,
            )
        except httpx.HTTPError as exc:
            return _AttemptResult(
                success=False,
                content=None,
                status_code=None,
                elapsed_ms=(time.perf_counter() - request_started) * 1000.0,
                # 走 describe_exception 而不是直接 f"{exc}"：连接层异常的
                # str() 可能为空，那样用户只会看到一句"网络错误："，
                # 连是 DNS、TLS 还是超时都分不出来。
                error=f"{_ERROR_NETWORK}：{describe_exception(exc)}",
                failure_kind=TileFailureKind.NETWORK,
            )
        except Exception as exc:  # noqa: BLE001 - 兜底：绝不让异常打断整个任务
            return _AttemptResult(
                success=False,
                content=None,
                status_code=None,
                elapsed_ms=(time.perf_counter() - request_started) * 1000.0,
                error=f"未知错误：{describe_exception(exc)}",
                failure_kind=TileFailureKind.UNEXPECTED,
            )

        elapsed_ms = (time.perf_counter() - request_started) * 1000.0
        result = self._judge(response, elapsed_ms)
        limiter = self._rate_limiter
        if isinstance(limiter, RateLimiter):
            if response.status_code == 429:
                previous_generation = limiter.generation
                delay = limiter.on_throttled(
                    parse_retry_after(response.headers.get("Retry-After")), generation=generation
                )
                if limiter.generation != previous_generation:
                    self._logger.warning(
                        "HTTP 429 限流：全部图层共享冷却 %.1f 秒，HTTP 请求速率降至 %.2f 次/秒",
                        delay, limiter.effective_requests_per_second,
                    )
            elif result.success and limiter.on_success(generation=generation):
                self._logger.info(
                    "限流后下载已稳定，HTTP 请求速率逐步恢复至 %.2f 次/秒",
                    limiter.effective_requests_per_second,
                )
        return result

    @staticmethod
    def _judge(response: httpx.Response, elapsed_ms: float) -> _AttemptResult:
        """把 HTTP 响应判定成成功 / 失败（判定顺序与 Slice 2 一致）。"""
        status_code = response.status_code
        content_type = response.headers.get("content-type")
        content = response.content
        length = len(content)

        def _failure(
            error: str, kind: TileFailureKind
        ) -> _AttemptResult:
            return _AttemptResult(
                success=False,
                content=None,
                status_code=status_code,
                elapsed_ms=elapsed_ms,
                error=error,
                failure_kind=kind,
            )

        if status_code != 200:
            return _failure(http_error_message(status_code), TileFailureKind.HTTP_STATUS)
        if length == 0:
            return _failure(_ERROR_EMPTY, TileFailureKind.EMPTY_RESPONSE)
        if length > MAX_TILE_RESPONSE_BYTES:
            return _failure(
                f"响应体积异常（{length} 字节，超过上限 {MAX_TILE_RESPONSE_BYTES} 字节）",
                TileFailureKind.RESPONSE_TOO_LARGE,
            )
        if not is_valid_tile_response(content, content_type):
            return _failure(
                f"响应不是有效的图片瓦片：{describe_response(content, content_type)}",
                TileFailureKind.INVALID_CONTENT,
            )

        return _AttemptResult(
            success=True,
            content=content,
            status_code=status_code,
            elapsed_ms=elapsed_ms,
            error=None,
            failure_kind=None,
        )
