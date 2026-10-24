"""429 压力回归全部走 MockTransport，不对真实服务发送测试请求。"""
from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from itertools import pairwise

import httpx
import pytest
from PIL import Image

from app.config.settings import DEFAULT_DOWNLOAD_CONCURRENCY, DEFAULT_REQUESTS_PER_SECOND
from app.downloader import (
    CompositeTileDownloader,
    DownloadManager,
    RateLimiter,
    RetryPolicy,
    TileCoordinate,
    TileDownloader,
)
from app.downloader.rate_limiter import RateLimitExceeded, parse_retry_after
from app.geo import GeoBounds
from app.models import MapLayerType, MapType
from app.providers import TiandituProvider
from app.services.map_download_service import MapDownloadService

BOUNDS = GeoBounds(116.2, 39.7, 116.6, 40.1)
TILE = TileCoordinate(12, 3372, 1552)
LAYER = MapLayerType.VECTOR
POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=0, max_delay_seconds=0)


def png():
    stream = io.BytesIO()
    Image.new("RGBA", (256, 256), (80, 120, 160, 255)).save(stream, format="PNG")
    return stream.getvalue()


PNG = png()


def image():
    return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})


def limiter(**kwargs):
    options = {"requests_per_second": 1000, "cooldown_base_seconds": 0.04,
               "cooldown_max_seconds": 0.08, "jitter_ratio": 0, "recovery_successes": 100}
    options.update(kwargs)
    return RateLimiter(**options)


def downloader(server, pacing):
    return TileDownloader(TiandituProvider("fake-token-for-offline-tests"),
                          transport=httpx.MockTransport(server), rate_limiter=pacing,
                          retry_policy=POLICY)


@pytest.mark.parametrize("value,expected", [
    (None, None), ("", None), (" 12 ", 12), ("0", 0), ("-5", None),
    ("1.5", None), ("nan", None), ("inf", None), ("nonsense", None),
    ("9" * 400, None), ("１２", None),
])
def test_retry_after_seconds_and_invalid_headers(value, expected):
    assert parse_retry_after(value) == expected


def test_retry_after_http_date_and_expired_date():
    now = datetime(2026, 10, 24, 10, 2, tzinfo=UTC)
    assert parse_retry_after(format_datetime(now + timedelta(seconds=60), usegmt=True), now=now) == 60
    assert parse_retry_after(format_datetime(now - timedelta(seconds=1), usegmt=True), now=now) == 0


def test_production_defaults_are_conservative():
    assert DEFAULT_DOWNLOAD_CONCURRENCY == 3
    assert DEFAULT_REQUESTS_PER_SECOND == 4
    pacing = RateLimiter()
    assert pacing.cooldown_base_seconds == 5
    assert pacing.max_throttle_episodes == 5


@pytest.mark.parametrize("rate", [float("nan"), float("inf"), -1])
def test_rejects_invalid_rate(rate):
    with pytest.raises(ValueError):
        RateLimiter(rate)


async def test_already_waiting_requests_obey_new_global_cooldown():
    pacing = limiter()
    starts = []
    first_throttle = []

    async def server(request):
        starts.append(asyncio.get_running_loop().time())
        if len(starts) == 1:
            first_throttle.append(asyncio.get_running_loop().time())
            return httpx.Response(429)
        return image()

    async with downloader(server, pacing) as inner:
        results = await asyncio.wait_for(asyncio.gather(*(
            inner.download_tile(TileCoordinate(12, 3370 + i, 1552), LAYER)
            for i in range(8)
        )), 3)
    assert all(r.success for r in results)
    assert len(starts) == 9
    assert min(starts[1:]) - first_throttle[0] >= 0.035
    assert min(b - a for a, b in pairwise(starts[1:])) >= 0.0015
    assert pacing.effective_requests_per_second == 500


async def test_retry_after_is_not_capped_by_local_maximum():
    pacing = limiter()
    calls = []

    def server(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with downloader(server, pacing) as inner:
        task = asyncio.create_task(inner.download_tile(TILE, LAYER))
        try:
            await asyncio.sleep(0.04)
            assert len(calls) == 1
            assert pacing._cooldown_until - asyncio.get_running_loop().time() > 119
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_inflight_429s_coalesce_and_stale_success_does_not_recover():
    pacing = limiter(recovery_successes=1, recovery_seconds=0)
    delay = pacing.on_throttled(None, generation=0)
    assert delay >= 0.039
    pacing.on_throttled(None, generation=0)
    assert pacing.generation == 1
    assert pacing.effective_requests_per_second == 500
    await asyncio.sleep(0.05)
    assert not pacing.on_success(generation=0)
    assert pacing.effective_requests_per_second == 500
    assert pacing.on_success(generation=1)
    assert pacing.effective_requests_per_second == 625


async def test_repeated_throttles_back_off_and_stop():
    pacing = limiter(requests_per_second=4, max_throttle_episodes=3)
    assert pacing.on_throttled(None, generation=0) >= 0.039
    assert pacing.effective_requests_per_second == 2
    assert pacing.on_throttled(None, generation=1) >= 0.079
    assert pacing.effective_requests_per_second == 1
    pacing.on_throttled(None, generation=2)
    assert pacing.effective_requests_per_second == 0.5
    with pytest.raises(RateLimitExceeded, match="保留已完成瓦片"):
        await pacing.acquire()


@pytest.mark.parametrize("merged", [False, True])
async def test_persistent_429_stops_batch_without_user_cancel_or_request_storm(merged):
    pacing = limiter(max_throttle_episodes=2)
    calls = []

    def server(request):
        calls.append(request)
        return httpx.Response(429)

    async with downloader(server, pacing) as inner:
        manager = DownloadManager(CompositeTileDownloader(inner) if merged else inner, concurrency=3)
        summary = await asyncio.wait_for(manager.download(
            BOUNDS, 12, 12, MapType.VECTOR if merged else LAYER
        ), 3)
    assert not summary.cancelled
    assert not summary.is_success
    assert summary.stop_reason and "429" in summary.stop_reason
    assert summary.unprocessed > 0
    assert summary.total == summary.completed + summary.unprocessed
    assert len(calls) == 2  # 两次冷却探测后停止，不遍历整个下载队列。


@pytest.mark.parametrize("merged", [False, True])
async def test_cancel_interrupts_long_retry_after_for_both_layers(merged):
    calls = []
    throttled = asyncio.Event()

    def server(request):
        calls.append(request)
        throttled.set()
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with downloader(server, limiter()) as inner:
        manager = DownloadManager(CompositeTileDownloader(inner) if merged else inner, concurrency=3)
        task = asyncio.create_task(manager.download(
            BOUNDS, 12, 12, MapType.VECTOR if merged else LAYER
        ))
        try:
            await asyncio.wait_for(throttled.wait(), 1)
            await asyncio.sleep(0.02)
            manager.cancel()
            summary = await asyncio.wait_for(task, 0.5)
            assert summary.cancelled and not summary.stop_reason
            assert summary.failed == 0
            assert summary.unprocessed == summary.total
            assert len(calls) == 1
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_pause_holds_retries_and_resume_does_not_release_burst():
    pacing = limiter(requests_per_second=100)
    calls = []
    throttled = asyncio.Event()

    def server(request):
        calls.append(asyncio.get_running_loop().time())
        if len(calls) == 1:
            throttled.set()
            return httpx.Response(429)
        return image()

    async with downloader(server, pacing) as inner:
        manager = DownloadManager(inner, concurrency=3)
        task = asyncio.create_task(manager.download(BOUNDS, 12, 12, LAYER))
        try:
            await asyncio.wait_for(throttled.wait(), 1)
            manager.pause()
            await asyncio.sleep(0.08)
            assert len(calls) == 1
            manager.resume()
            await asyncio.sleep(0.09)
            manager.cancel()
            summary = await asyncio.wait_for(task, 1)
            assert summary.cancelled
            assert len(calls) >= 3
            assert min(b - a for a, b in pairwise(calls[1:])) >= 0.016
        finally:
            manager.cancel()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_composite_layers_and_retries_share_one_limiter():
    pacing = limiter()
    calls = []

    def server(request):
        calls.append(request)
        return httpx.Response(429) if len(calls) == 1 else image()

    async with downloader(server, pacing) as inner:
        result = await CompositeTileDownloader(inner).download_tile(TILE, MapType.VECTOR)
    assert result.success
    assert len(calls) == 3 and pacing.acquired_count == 3
    assert {r.url.params['LAYER'] for r in calls} == {"vec", "cva"}


async def test_stopped_service_preserves_map_and_can_resume(work_dir):
    calls = []
    path = work_dir / "rate-limited.mbtiles"

    def server(request):
        calls.append(request)
        return image() if len(calls) == 1 else httpx.Response(429)

    service = MapDownloadService(
        TiandituProvider("fake-token-for-offline-tests"), transport=httpx.MockTransport(server),
        rate_limiter=limiter(max_throttle_episodes=2), retry_policy=POLICY, concurrency=1,
    )
    first = await asyncio.wait_for(service.download_to_mbtiles(BOUNDS, 11, 11, LAYER, path), 3)
    assert first.summary.stop_reason and not first.cancelled and not first.is_success
    assert first.tile_count == 1
    assert "429" in first.failure_text
    assert first.summary.completed + first.summary.unprocessed == first.summary.total

    from app.downloader import ResumePolicy
    resumed_calls = []

    def healthy(request):
        resumed_calls.append(request)
        return image()

    second_service = MapDownloadService(
        TiandituProvider("fake-token-for-offline-tests"), transport=httpx.MockTransport(healthy),
        rate_limiter=RateLimiter.disabled(), retry_policy=POLICY,
    )
    second = await second_service.download_to_mbtiles(
        BOUNDS, 11, 11, LAYER, path, resume_policy=ResumePolicy()
    )
    assert second.is_success
    assert second.summary.skipped == 1
    assert len(resumed_calls) == second.summary.total - 1


async def test_idle_does_not_accumulate_burst_credits():
    pacing = limiter(requests_per_second=100, max_burst=10)
    await pacing.acquire()
    await asyncio.sleep(0.03)
    started = asyncio.get_running_loop().time()
    await asyncio.gather(*(pacing.acquire() for _ in range(4)))
    assert asyncio.get_running_loop().time() - started >= 0.025



@pytest.mark.parametrize("exhausted", [False, True])
async def test_throttle_after_permit_before_http_invalidates_old_permit(exhausted):
    class LateThrottle(RateLimiter):
        async def acquire(self):
            await super().acquire()
            if self.acquired_count == 1:
                self.on_throttled(None, generation=0)

    pacing = LateThrottle(requests_per_second=1000, cooldown_base_seconds=0.04,
                         cooldown_max_seconds=0.08, jitter_ratio=0,
                         max_throttle_episodes=1 if exhausted else 5)
    calls = []

    def server(request):
        calls.append(asyncio.get_running_loop().time())
        return image()

    started = asyncio.get_running_loop().time()
    async with downloader(server, pacing) as inner:
        if exhausted:
            with pytest.raises(RateLimitExceeded):
                await inner.download_tile(TILE, LAYER)
            assert calls == []
        else:
            result = await inner.download_tile(TILE, LAYER)
            assert result.success
            assert calls[0] - started >= 0.035
