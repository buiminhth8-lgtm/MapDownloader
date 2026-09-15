"""批量下载调度单元测试（Slice 3）。

**全部走 ``httpx.MockTransport``，测试不触网。** 假服务器支持注入延迟与
"按瓦片编号失败"，因此并发、暂停、取消都能在几十毫秒内被确定性地断言。

关于 Spec 里的"空任务"：``GeoBounds`` 强制 ``west < east``、``south < north``，
而任意 zoom 的 ``calculate_tile_range`` 至少返回 1 张瓦片，所以"0 张瓦片的任务"
在本项目的模型下**结构上不可能出现**。因此这里覆盖的是"非法任务参数"，
并在 ``download()`` 里保留了零瓦片的防御性分支。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import httpx
import pytest

from app.config.settings import (
    MAX_DOWNLOAD_CONCURRENCY,
    PROGRESS_EMIT_INTERVAL_SECONDS,
)
from app.downloader import (
    DownloadManager,
    DownloadProgress,
    DownloadSummary,
    RateLimiter,
    RetryPolicy,
    TileCoordinate,
    TileDownloader,
    TileDownloadResult,
    iter_tile_coordinates,
)
from app.geo import GeoBounds, calculate_tile_ranges, estimate_tile_count
from app.models import MapLayerType
from app.providers import PNG_SIGNATURE, TiandituProvider

FAKE_TOKEN = "abcdefghijklmn"
LAYER = MapLayerType.VECTOR

FAKE_PNG = PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + b"tile-payload" * 16

FAST_POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=0.0, max_delay_seconds=0.0)
NO_RATE_LIMIT = RateLimiter.disabled()

#: 北京附近 0.1° × 0.1° —— z12 只有 6 张瓦片，适合做精确断言
SMALL_BOUNDS = GeoBounds(116.0, 39.0, 116.1, 39.1)

#: 0.5° × 0.5° —— z12 有 56 张，用来制造"请求还在途"的窗口
MID_BOUNDS = GeoBounds(116.0, 39.0, 116.5, 39.5)

#: 各范围在各缩放区间下的瓦片数（由 Slice 1 统计得出，写在断言里当锚点）
SMALL_Z12 = 6
SMALL_Z12_13 = 18
SMALL_Z12_14 = 53
MID_Z12 = 56


# --------------------------------------------------------------------------- #
# 假瓦片服务器
# --------------------------------------------------------------------------- #
class TileServer:
    """可控的异步瓦片服务器替身。

    Args:
        delay: 每个请求的处理耗时（秒）。用于制造"请求在途"的窗口，
            以便观察并发度与暂停行为。
        fail_predicate: ``(z, x, y) -> bool``，返回 ``True`` 的瓦片一律返回
            500。因为按瓦片编号判定，重试时会稳定地再次失败。
        status_code: 失败时返回的状态码。
    """

    def __init__(
        self,
        *,
        delay: float = 0.0,
        fail_predicate=None,
        status_code: int = 500,
    ) -> None:
        self._delay = delay
        self._fail_predicate = fail_predicate
        self._status_code = status_code
        self.requests: list[httpx.Request] = []
        self.inflight = 0
        self.max_inflight = 0

    @staticmethod
    def tile_key(request: httpx.Request) -> tuple[int, int, int]:
        params = request.url.params
        return (
            int(params["TILEMATRIX"]),
            int(params["TILECOL"]),
            int(params["TILEROW"]),
        )

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            if self._fail_predicate is not None and self._fail_predicate(
                *self.tile_key(request)
            ):
                return httpx.Response(
                    self._status_code,
                    content=b"<html>upstream failure</html>",
                    headers={"content-type": "text/html"},
                )
            return httpx.Response(
                200, content=FAKE_PNG, headers={"content-type": "image/png"}
            )
        finally:
            self.inflight -= 1

    @property
    def call_count(self) -> int:
        return len(self.requests)


def _make_downloader(server: TileServer) -> TileDownloader:
    return TileDownloader(
        TiandituProvider(FAKE_TOKEN),
        transport=httpx.MockTransport(server),
        retry_policy=FAST_POLICY,
        rate_limiter=NO_RATE_LIMIT,
    )


def _tile_count(bounds: GeoBounds, min_zoom: int, max_zoom: int) -> int:
    return sum(r.tile_count for r in calculate_tile_ranges(bounds, min_zoom, max_zoom))


@asynccontextmanager
async def _running_download(
    manager: DownloadManager,
    bounds: GeoBounds,
    min_zoom: int,
    max_zoom: int,
) -> AsyncIterator[asyncio.Task]:
    """把下载跑成一个可中途操控的后台 Task，并保证收尾。

    为什么需要这个包装：暂停 / 取消类用例会在任务运行途中做断言。一旦断言失败，
    那只后台 Task 就成了"孤儿"——事件循环关闭时会挂在它身上，整个 pytest 进程
    卡死（这不是产品缺陷，而是测试脚手架的问题）。这里用 ``finally`` 兜底取消
    并回收。

    Yields:
        正在运行的下载任务，可直接 ``await`` 取 :class:`DownloadSummary`。
    """
    task = asyncio.create_task(manager.download(bounds, min_zoom, max_zoom, LAYER))
    try:
        yield task
    finally:
        manager.cancel()
        try:
            await asyncio.wait_for(task, timeout=5)
        except BaseException:  # noqa: BLE001 - 收尾异常不该掩盖用例本身的失败
            task.cancel()


# --------------------------------------------------------------------------- #
# 瓦片枚举
# --------------------------------------------------------------------------- #
def test_iter_tile_coordinates_count_matches_geo_module() -> None:
    """枚举出来的瓦片数必须与 Slice 1 的统计一致（不自己算另一套）。"""
    tiles = list(iter_tile_coordinates(SMALL_BOUNDS, 12, 13))

    assert len(tiles) == estimate_tile_count(SMALL_BOUNDS, 12, 13)
    assert len(tiles) == _tile_count(SMALL_BOUNDS, 12, 13)
    assert len(tiles) == len(set(tiles))  # 不重不漏


def test_iter_tile_coordinates_matches_ranges_exactly() -> None:
    expected = {
        TileCoordinate(z=r.zoom, x=x, y=y)
        for r in calculate_tile_ranges(SMALL_BOUNDS, 10, 12)
        for y in range(r.min_y, r.max_y + 1)
        for x in range(r.min_x, r.max_x + 1)
    }
    assert set(iter_tile_coordinates(SMALL_BOUNDS, 10, 12)) == expected


def test_iter_tile_coordinates_is_lazy() -> None:
    """必须是生成器：千万级瓦片不能先 materialize 成 list（Spec 明令禁止）。"""
    tiles = iter_tile_coordinates(SMALL_BOUNDS, 10, 12)
    assert inspect.isgenerator(tiles)


def test_iter_tile_coordinates_is_deterministic_and_zoom_major() -> None:
    first = list(iter_tile_coordinates(SMALL_BOUNDS, 10, 11))
    again = list(iter_tile_coordinates(SMALL_BOUNDS, 10, 11))

    assert first == again
    zooms = [tile.z for tile in first]
    assert zooms == sorted(zooms)  # 低缩放先行
    assert zooms[0] == 10 and zooms[-1] == 11


def test_iter_tile_coordinates_propagates_invalid_zoom() -> None:
    with pytest.raises(ValueError, match="min_zoom 不能大于 max_zoom"):
        list(iter_tile_coordinates(SMALL_BOUNDS, 13, 12))
    with pytest.raises(ValueError, match="zoom"):
        list(iter_tile_coordinates(SMALL_BOUNDS, 0, 23))


# --------------------------------------------------------------------------- #
# 基本下载
# --------------------------------------------------------------------------- #
async def test_download_all_tiles_succeed() -> None:
    server = TileServer()
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=4)
    expected = _tile_count(SMALL_BOUNDS, 12, 12)

    async with downloader:
        summary = await manager.download(SMALL_BOUNDS, 12, 12, LAYER)

    assert isinstance(summary, DownloadSummary)
    assert summary.total == expected
    assert summary.succeeded == expected
    assert summary.failed == 0
    assert summary.skipped == 0
    assert summary.cancelled is False
    assert summary.completed == expected
    assert summary.is_success is True
    assert summary.bytes_downloaded == expected * len(FAKE_PNG)
    assert summary.duration_seconds >= 0.0
    assert summary.average_tiles_per_second > 0.0
    assert server.call_count == expected


async def test_download_multi_zoom() -> None:
    server = TileServer()
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=3)
    expected = _tile_count(SMALL_BOUNDS, 12, 14)

    async with downloader:
        summary = await manager.download(SMALL_BOUNDS, 12, 14, LAYER)

    assert summary.total == expected
    assert summary.succeeded == expected
    assert {TileServer.tile_key(r)[0] for r in server.requests} == {12, 13, 14}


async def test_download_counts_failures() -> None:
    """按瓦片编号固定失败的瓦片，重试后仍是失败，计数必须准确。"""
    server = TileServer(fail_predicate=lambda z, x, y: y % 2 == 0)
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=4)

    tiles = list(iter_tile_coordinates(SMALL_BOUNDS, 12, 12))
    expected_failures = sum(1 for t in tiles if t.y % 2 == 0)
    expected_successes = len(tiles) - expected_failures

    async with downloader:
        summary = await manager.download(SMALL_BOUNDS, 12, 12, LAYER)

    assert summary.failed == expected_failures
    assert summary.succeeded == expected_successes
    assert summary.completed == len(tiles)
    assert summary.is_success is False
    # 失败的瓦片被重试了 max_attempts 次
    assert server.call_count == expected_successes + expected_failures * 3


# --------------------------------------------------------------------------- #
# on_tile 回调（Slice 4 的消费口）
# --------------------------------------------------------------------------- #
async def test_on_tile_receives_every_successful_tile() -> None:
    server = TileServer()
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=4)
    expected = _tile_count(SMALL_BOUNDS, 12, 12)

    delivered: list[TileDownloadResult] = []

    async with downloader:
        summary = await manager.download(
            SMALL_BOUNDS, 12, 12, LAYER, on_tile=delivered.append
        )

    assert len(delivered) == expected
    assert summary.succeeded == expected
    assert all(item.success for item in delivered)
    assert all(item.content == FAKE_PNG for item in delivered)
    assert len({item.tile for item in delivered}) == expected  # 无重复


async def test_on_tile_is_only_called_for_successes() -> None:
    server = TileServer(fail_predicate=lambda z, x, y: y % 2 == 0)
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=4)

    delivered: list[TileDownloadResult] = []

    async with downloader:
        summary = await manager.download(
            SMALL_BOUNDS, 12, 12, LAYER, on_tile=delivered.append
        )

    assert len(delivered) == summary.succeeded
    assert summary.failed > 0
    assert all(item.success for item in delivered)


async def test_on_tile_supports_async_callbacks() -> None:
    """回调可以是协程函数（Slice 4 的 writer 更可能是这种形态）。"""
    server = TileServer()
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=3)

    delivered: list[TileCoordinate] = []

    async def sink(result: TileDownloadResult) -> None:
        await asyncio.sleep(0)
        delivered.append(result.tile)

    async with downloader:
        summary = await manager.download(SMALL_BOUNDS, 12, 12, LAYER, on_tile=sink)

    assert len(delivered) == summary.succeeded == summary.total


async def test_on_tile_failure_is_counted_as_failed() -> None:
    """消费方写失败等同于这张瓦片没落定，不能算成功。"""
    server = TileServer()
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=4)
    expected = _tile_count(SMALL_BOUNDS, 12, 12)

    def broken_sink(result: TileDownloadResult) -> None:
        raise RuntimeError("sink exploded")

    async with downloader:
        summary = await manager.download(
            SMALL_BOUNDS, 12, 12, LAYER, on_tile=broken_sink
        )

    assert summary.succeeded == 0
    assert summary.failed == expected
    assert summary.bytes_downloaded == 0


async def test_download_without_callbacks_works() -> None:
    """回调是可选的：纯统计模式（GUI 测试模式就是一种用法）。"""
    server = TileServer()
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=2)

    async with downloader:
        summary = await manager.download(SMALL_BOUNDS, 12, 12, LAYER)

    assert summary.succeeded == summary.total


async def test_manager_does_not_retain_tile_bytes() -> None:
    """边下载边消费：引擎自己不留瓦片字节。"""
    server = TileServer()
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=4)
    expected = _tile_count(SMALL_BOUNDS, 12, 12)

    async with downloader:
        summary = await manager.download(SMALL_BOUNDS, 12, 12, LAYER)

    assert summary.succeeded == expected
    for name in ("tiles", "results", "_tiles", "_results", "buffer", "_buffer"):
        assert not hasattr(manager, name), f"管理器不应持有 {name}"


# --------------------------------------------------------------------------- #
# 进度
# --------------------------------------------------------------------------- #
async def test_progress_total_is_known_up_front() -> None:
    server = TileServer(delay=0.01)
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=2)
    expected = _tile_count(SMALL_BOUNDS, 12, 12)

    snapshots: list[DownloadProgress] = []

    async with downloader:
        await manager.download(
            SMALL_BOUNDS, 12, 12, LAYER, on_progress=snapshots.append
        )

    first = snapshots[0]
    assert first.total == expected  # 分母一开始就有，不是边发现边增长
    assert first.completed == 0
    assert first.percent == 0.0


async def test_progress_final_snapshot_is_complete() -> None:
    server = TileServer()
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=4)
    expected = _tile_count(SMALL_BOUNDS, 12, 12)

    snapshots: list[DownloadProgress] = []

    async with downloader:
        await manager.download(
            SMALL_BOUNDS, 12, 12, LAYER, on_progress=snapshots.append
        )

    last = snapshots[-1]
    assert last.completed == expected
    assert last.succeeded == expected
    assert last.failed == 0
    assert last.percent == 100.0
    assert last.remaining == 0
    assert last.bytes_downloaded == expected * len(FAKE_PNG)
    assert last.current_zoom == 12


async def test_progress_is_throttled() -> None:
    """Spec：进度回调不能每张瓦片都发，否则会把 PySide6 压垮。"""
    server = TileServer()
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=6)
    expected = _tile_count(SMALL_BOUNDS, 12, 14)
    assert expected == SMALL_Z12_14  # 样本量足够看出节流效果

    snapshots: list[DownloadProgress] = []

    async with downloader:
        summary = await manager.download(
            SMALL_BOUNDS, 12, 14, LAYER, on_progress=snapshots.append
        )

    assert summary.succeeded == expected
    # MockTransport 是"瞬时"的，因此除首尾两次强制回调外不该有更多
    assert len(snapshots) <= 4
    assert len(snapshots) >= 2


async def test_progress_is_emitted_while_slow_download_runs() -> None:
    """慢速下载时进度要真的在刷新，而不只是首尾两次。"""
    server = TileServer(delay=0.04)
    downloader = _make_downloader(server)
    manager = DownloadManager(
        downloader, concurrency=4, progress_interval_seconds=0.05
    )
    expected = _tile_count(SMALL_BOUNDS, 12, 13)
    assert expected == SMALL_Z12_13

    snapshots: list[DownloadProgress] = []

    async with downloader:
        summary = await manager.download(
            SMALL_BOUNDS, 12, 13, LAYER, on_progress=snapshots.append
        )

    assert summary.succeeded == expected
    assert len(snapshots) > 2
    assert [s.completed for s in snapshots] == sorted(s.completed for s in snapshots)


async def test_progress_speed_is_reported() -> None:
    server = TileServer(delay=0.02)
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=2)

    async with downloader:
        await manager.download(
            SMALL_BOUNDS, 12, 12, LAYER, on_progress=lambda p: None
        )

    assert manager.progress.tiles_per_second > 0.0


async def test_progress_snapshot_is_available_without_callback() -> None:
    """没有进度回调时 ``manager.progress`` 也必须是最新的。"""
    server = TileServer()
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=2)
    expected = _tile_count(SMALL_BOUNDS, 12, 12)

    async with downloader:
        summary = await manager.download(SMALL_BOUNDS, 12, 12, LAYER)

    assert manager.progress.total == expected
    assert manager.progress.completed == expected
    assert manager.progress.succeeded == summary.succeeded
    assert manager.progress.percent == 100.0


async def test_progress_callback_may_be_async() -> None:
    server = TileServer()
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=2)

    snapshots: list[DownloadProgress] = []

    async def on_progress(progress: DownloadProgress) -> None:
        await asyncio.sleep(0)
        snapshots.append(progress)

    async with downloader:
        await manager.download(SMALL_BOUNDS, 12, 12, LAYER, on_progress=on_progress)

    assert snapshots and snapshots[-1].percent == 100.0


# --------------------------------------------------------------------------- #
# 并发
# --------------------------------------------------------------------------- #
async def test_concurrency_is_capped_by_worker_count() -> None:
    """Queue + 固定 worker：在途请求数永远不超过 concurrency。"""
    server = TileServer(delay=0.02)
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=4)
    expected = _tile_count(SMALL_BOUNDS, 12, 13)
    assert expected == SMALL_Z12_13

    async with downloader:
        summary = await manager.download(SMALL_BOUNDS, 12, 13, LAYER)

    assert summary.succeeded == expected
    assert server.max_inflight <= 4
    # 也证明了"真的并发"而不是串行
    assert server.max_inflight == 4
    # 顺序执行需要 expected × delay，并发后应显著更短
    assert summary.duration_seconds < expected * 0.02


async def test_concurrency_one_runs_sequentially() -> None:
    server = TileServer(delay=0.005)
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=1)

    async with downloader:
        summary = await manager.download(SMALL_BOUNDS, 12, 12, LAYER)

    assert summary.succeeded == summary.total
    assert server.max_inflight == 1


async def test_concurrency_does_not_change_results() -> None:
    """并发度只是性能参数，不能改变结果集。"""
    results: list[set[TileCoordinate]] = []
    for concurrency in (1, 3, 8):
        server = TileServer()
        downloader = _make_downloader(server)
        manager = DownloadManager(downloader, concurrency=concurrency)
        seen: list[TileCoordinate] = []
        async with downloader:
            await manager.download(
                SMALL_BOUNDS, 12, 12, LAYER, on_tile=lambda r: seen.append(r.tile)
            )
        results.append(set(seen))

    assert results[0] == results[1] == results[2]
    assert len(results[0]) == _tile_count(SMALL_BOUNDS, 12, 12)


# --------------------------------------------------------------------------- #
# 暂停 / 恢复 / 取消
# --------------------------------------------------------------------------- #
async def test_pause_freezes_progress_and_resume_finishes() -> None:
    server = TileServer(delay=0.02)
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=2)
    expected = _tile_count(MID_BOUNDS, 12, 12)
    assert expected == MID_Z12

    async with downloader, _running_download(manager, MID_BOUNDS, 12, 12) as task:
        await asyncio.sleep(0.1)  # 让它真正跑起来

        assert manager.is_paused is False
        manager.pause()
        assert manager.is_paused is True

        await asyncio.sleep(0.1)  # 等在途的两张请求跑完
        frozen = manager.progress.completed
        assert 0 < frozen < expected

        await asyncio.sleep(0.15)  # 暂停期间不该有任何新进度
        assert manager.progress.completed == frozen

        manager.resume()
        assert manager.is_paused is False
        summary = await asyncio.wait_for(task, timeout=5)

    assert summary.succeeded == expected
    assert summary.failed == 0
    assert summary.cancelled is False


async def test_pause_and_resume_are_logged(caplog) -> None:
    server = TileServer(delay=0.02)
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=2)

    with caplog.at_level(logging.INFO, logger="app.downloader.download_manager"):
        async with downloader, _running_download(
            manager, SMALL_BOUNDS, 12, 14
        ) as task:
            await asyncio.sleep(0.05)
            manager.pause()
            manager.resume()
            await asyncio.wait_for(task, timeout=5)

    assert "Download paused" in caplog.text
    assert "Download resumed" in caplog.text


async def test_cancel_returns_cancelled_summary() -> None:
    server = TileServer(delay=0.02)
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=2)
    expected = _tile_count(SMALL_BOUNDS, 12, 14)
    assert expected == SMALL_Z12_14

    async with downloader, _running_download(manager, SMALL_BOUNDS, 12, 14) as task:
        await asyncio.sleep(0.1)
        manager.cancel()
        assert manager.is_cancelled is True

        summary = await asyncio.wait_for(task, timeout=5)

    assert summary.cancelled is True
    assert summary.total == expected
    assert 0 <= summary.succeeded < expected  # 取消时通常还剩没下完的
    # 三种归宿必须覆盖全部瓦片，不许凭空消失。
    # Slice 5 起"没轮到"的瓦片记在 unprocessed（skipped 改成"已有瓦片未重下"）。
    assert summary.succeeded + summary.failed + summary.unprocessed == expected
    assert summary.unprocessed > 0
    assert summary.skipped == 0  # 本次没有任何"已有瓦片"可跳


async def test_cancel_while_paused_does_not_hang() -> None:
    """取消与暂停叠加时必须能退出——这正是"抬起闸门"要解决的死锁。"""
    server = TileServer(delay=0.02)
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=2)

    async with downloader, _running_download(manager, SMALL_BOUNDS, 12, 14) as task:
        await asyncio.sleep(0.08)
        manager.pause()
        await asyncio.sleep(0.05)
        manager.cancel()

        summary = await asyncio.wait_for(task, timeout=5)

    assert summary.cancelled is True
    assert manager.is_paused is False  # 取消会顺手抬起闸门


async def test_cancel_is_logged(caplog) -> None:
    server = TileServer(delay=0.02)
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=2)

    with caplog.at_level(logging.INFO, logger="app.downloader.download_manager"):
        async with downloader, _running_download(
            manager, SMALL_BOUNDS, 12, 14
        ) as task:
            await asyncio.sleep(0.05)
            manager.cancel()
            await asyncio.wait_for(task, timeout=5)

    assert "Download cancelled" in caplog.text


async def test_cancel_stops_further_requests() -> None:
    server = TileServer(delay=0.02)
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=2)
    expected = _tile_count(SMALL_BOUNDS, 12, 14)

    async with downloader, _running_download(manager, SMALL_BOUNDS, 12, 14) as task:
        await asyncio.sleep(0.1)
        manager.cancel()
        summary = await asyncio.wait_for(task, timeout=5)

    assert summary.cancelled is True
    assert server.call_count < expected
    assert summary.total == expected  # 分母仍是完整任务量


async def test_control_methods_are_safe_when_idle() -> None:
    """没有任务时调用 pause/resume/cancel 不能炸，也不能污染下一次任务。"""
    server = TileServer()
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=2)

    manager.pause()
    manager.resume()
    manager.cancel()
    assert manager.is_paused is False
    assert manager.is_cancelled is False
    assert manager.is_running is False

    async with downloader:
        summary = await manager.download(SMALL_BOUNDS, 12, 12, LAYER)

    assert summary.cancelled is False
    assert summary.succeeded == summary.total


async def test_manager_can_be_reused_after_a_run() -> None:
    """状态必须在任务结束后清干净，实例可以反复使用。"""
    server = TileServer()
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=3)

    async with downloader:
        first = await manager.download(SMALL_BOUNDS, 12, 12, LAYER)
        assert manager.is_running is False
        second = await manager.download(SMALL_BOUNDS, 12, 12, LAYER)

    assert first.succeeded == second.succeeded == first.total


# --------------------------------------------------------------------------- #
# 参数校验
# --------------------------------------------------------------------------- #
async def test_invalid_task_arguments_are_rejected() -> None:
    server = TileServer()
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=2)

    async with downloader:
        with pytest.raises(ValueError, match="GeoBounds"):
            await manager.download((116.0, 39.0, 117.0, 40.0), 12, 12, LAYER)
        with pytest.raises(ValueError, match="MapLayerType"):
            await manager.download(SMALL_BOUNDS, 12, 12, "vec_w")
        with pytest.raises(ValueError, match="min_zoom 不能大于 max_zoom"):
            await manager.download(SMALL_BOUNDS, 13, 12, LAYER)
        with pytest.raises(ValueError, match="zoom"):
            await manager.download(SMALL_BOUNDS, 12, 23, LAYER)

    assert server.call_count == 0  # 参数校验失败时一个请求都不发


@pytest.mark.parametrize("concurrency", [0, -1, MAX_DOWNLOAD_CONCURRENCY + 1, 100])
def test_concurrency_must_be_within_bounds(concurrency: int) -> None:
    downloader = _make_downloader(TileServer())
    with pytest.raises(ValueError, match="concurrency"):
        DownloadManager(downloader, concurrency=concurrency)


def test_negative_progress_interval_is_rejected() -> None:
    downloader = _make_downloader(TileServer())
    with pytest.raises(ValueError, match="progress_interval_seconds"):
        DownloadManager(downloader, progress_interval_seconds=-1.0)


async def test_second_download_is_rejected_while_running() -> None:
    server = TileServer(delay=0.02)
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=2)

    async with downloader, _running_download(manager, SMALL_BOUNDS, 12, 14) as task:
        await asyncio.sleep(0.05)
        assert manager.is_running is True
        with pytest.raises(ValueError, match="正在运行"):
            await manager.download(SMALL_BOUNDS, 12, 12, LAYER)
        manager.cancel()
        await asyncio.wait_for(task, timeout=5)


# --------------------------------------------------------------------------- #
# 日志与副作用
# --------------------------------------------------------------------------- #
async def test_start_and_finish_are_logged(caplog) -> None:
    server = TileServer()
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=6)

    with caplog.at_level(logging.INFO, logger="app.downloader.download_manager"):
        async with downloader:
            await manager.download(SMALL_BOUNDS, 12, 12, LAYER)

    text = caplog.text
    assert "Download started" in text
    assert "layer=vec_w" in text
    assert "zoom=12-12" in text
    assert "concurrency=6" in text
    assert "Download completed" in text
    assert "success=" in text and "duration=" in text


async def test_successful_tiles_are_not_logged_at_info(caplog) -> None:
    """Spec：每一张成功的瓦片都打 INFO 是禁止的。"""
    server = TileServer()
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=4)
    expected = _tile_count(SMALL_BOUNDS, 12, 12)

    with caplog.at_level(logging.INFO, logger="app.downloader"):
        async with downloader:
            summary = await manager.download(SMALL_BOUNDS, 12, 12, LAYER)

    assert summary.succeeded == expected
    per_tile = [r for r in caplog.records if "瓦片" in r.getMessage()]
    assert per_tile == []


async def test_failure_logging_is_capped(caplog) -> None:
    """大面积失败时逐条打印会淹掉日志，必须限流。"""
    from app.config.settings import MAX_LOGGED_TILE_FAILURES

    server = TileServer(fail_predicate=lambda z, x, y: True)
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=4)
    expected = _tile_count(SMALL_BOUNDS, 12, 12)
    assert expected > MAX_LOGGED_TILE_FAILURES

    with caplog.at_level(logging.WARNING, logger="app.downloader.download_manager"):
        async with downloader:
            summary = await manager.download(SMALL_BOUNDS, 12, 12, LAYER)

    assert summary.failed == expected
    per_tile = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "瓦片下载失败" in r.getMessage()
    ]
    assert len(per_tile) == MAX_LOGGED_TILE_FAILURES


# --------------------------------------------------------------------------- #
# 失败原因聚合（DownloadSummary.failures_by_reason / top_failures / as_lines）
# --------------------------------------------------------------------------- #
async def test_summary_aggregates_failure_reasons() -> None:
    """失败原因按"人话文本"聚合，而不是只丢一个 failed 数字。"""
    server = TileServer(fail_predicate=lambda z, x, y: True)
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=4)
    expected = _tile_count(SMALL_BOUNDS, 12, 12)

    async with downloader:
        summary = await manager.download(SMALL_BOUNDS, 12, 12, LAYER)

    assert summary.failed == expected
    assert list(summary.failures_by_reason.values()) == [expected]
    reason = next(iter(summary.failures_by_reason))
    assert "HTTP 500" in reason
    assert summary.top_failures() == [(reason, expected)]


async def test_summary_aggregates_distinct_reasons_by_count() -> None:
    """多种失败原因要分别计数，且按张数倒序展示。"""

    def handler(request: httpx.Request) -> httpx.Response:
        _, x, _ = TileServer.tile_key(request)
        code = 403 if x % 2 == 0 else 500
        return httpx.Response(
            code, content=b"<html>nope</html>", headers={"content-type": "text/html"}
        )

    downloader = TileDownloader(
        TiandituProvider(FAKE_TOKEN),
        transport=httpx.MockTransport(handler),
        retry_policy=FAST_POLICY,
        rate_limiter=NO_RATE_LIMIT,
    )
    manager = DownloadManager(downloader, concurrency=4)
    expected = _tile_count(SMALL_BOUNDS, 12, 12)

    async with downloader:
        summary = await manager.download(SMALL_BOUNDS, 12, 12, LAYER)

    assert summary.failed == expected
    assert len(summary.failures_by_reason) == 2
    assert sum(summary.failures_by_reason.values()) == expected

    top = summary.top_failures()
    assert [count for _, count in top] == sorted(
        (count for _, count in top), reverse=True
    )
    # 张数相同时按原因文本排序 —— 聚合顺序必须是确定的
    assert summary.top_failures(limit=1) == top[:1]


async def test_summary_has_no_failure_reasons_when_all_succeed() -> None:
    server = TileServer()
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=4)

    async with downloader:
        summary = await manager.download(SMALL_BOUNDS, 12, 12, LAYER)

    assert summary.failed == 0
    assert summary.failures_by_reason == {}
    assert summary.top_failures() == []


async def test_failure_reasons_are_reset_between_runs() -> None:
    """同一个 manager 连跑两次，第二次不能继承第一轮的失败原因。"""
    server = TileServer(fail_predicate=lambda z, x, y: True)
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=4)

    async with downloader:
        first = await manager.download(SMALL_BOUNDS, 12, 12, LAYER)

    assert first.failures_by_reason

    ok_server = TileServer()
    ok_downloader = _make_downloader(ok_server)
    ok_manager = DownloadManager(ok_downloader, concurrency=4)
    async with ok_downloader:
        second = await ok_manager.download(SMALL_BOUNDS, 12, 12, LAYER)

    assert second.failures_by_reason == {}


async def test_engine_logs_failure_reason_summary(caplog) -> None:
    """收尾日志里要有一行聚合后的失败原因，供 stderr / 日志文件排查。"""
    server = TileServer(fail_predicate=lambda z, x, y: True)
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=4)

    with caplog.at_level(logging.WARNING, logger="app.downloader.download_manager"):
        async with downloader:
            summary = await manager.download(SMALL_BOUNDS, 12, 12, LAYER)

    aggregated = [
        r.getMessage() for r in caplog.records if "Download failure reasons" in r.getMessage()
    ]
    assert len(aggregated) == 1
    assert f"（{summary.failed} 张）" in aggregated[0]
    assert FAKE_TOKEN not in caplog.text


def test_as_lines_reports_top_failure_reasons() -> None:
    """``as_lines``（GUI 日志逐行展示）必须带上失败原因。"""
    summary = DownloadSummary(
        total=10,
        succeeded=4,
        failed=6,
        failures_by_reason={"HTTP 403 — Token 可能无效": 5, "请求超时": 1},
    )

    lines = summary.as_lines()

    assert "总瓦片：10" in lines
    assert "失败：6" in lines
    assert "失败原因：HTTP 403 — Token 可能无效（5 张）" in lines
    assert "失败原因：请求超时（1 张）" in lines
    assert lines.index("失败原因：HTTP 403 — Token 可能无效（5 张）") < lines.index(
        "失败原因：请求超时（1 张）"
    )


def test_as_lines_omits_failure_reasons_when_nothing_failed() -> None:
    summary = DownloadSummary(total=6, succeeded=6)

    lines = summary.as_lines()

    assert not any(line.startswith("失败原因：") for line in lines)


def test_top_failures_limit_is_bounded() -> None:
    from app.config.settings import MAX_REPORTED_FAILURE_REASONS

    summary = DownloadSummary(
        failures_by_reason={f"原因{i}": 1 for i in range(10)}
    )

    assert len(summary.top_failures()) == MAX_REPORTED_FAILURE_REASONS
    assert len(summary.failures_by_reason) == 10


async def test_logs_never_leak_full_token(caplog) -> None:
    server = TileServer()
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=2)

    with caplog.at_level(logging.DEBUG, logger="app.downloader"):
        async with downloader:
            await manager.download(SMALL_BOUNDS, 12, 12, LAYER)

    assert FAKE_TOKEN not in caplog.text
    assert "abcd****lmn" in caplog.text


async def test_bounded_queue_keeps_memory_flat() -> None:
    """队列必须有界：否则生产端会抢在消费端前面把整个任务塞进内存。"""
    server = TileServer(delay=0.03)
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=2)

    async with downloader, _running_download(manager, SMALL_BOUNDS, 12, 13) as task:
        await asyncio.sleep(0.06)
        # 生产者的进度被队列容量反压住，不可能"全部生产完再开始下载"
        assert manager.progress.completed < manager.progress.total
        await asyncio.wait_for(task, timeout=5)


async def test_download_duration_matches_wall_clock() -> None:
    delay = 0.01
    server = TileServer(delay=delay)
    downloader = _make_downloader(server)
    manager = DownloadManager(downloader, concurrency=4)
    expected = _tile_count(SMALL_BOUNDS, 12, 12)

    started = time.perf_counter()
    async with downloader:
        summary = await manager.download(SMALL_BOUNDS, 12, 12, LAYER)
    wall = time.perf_counter() - started

    assert summary.succeeded == expected
    assert summary.duration_seconds <= wall + 0.05
    assert summary.duration_seconds >= delay
