"""下载 → MBTiles 端到端集成测试（Slice 4）。

链路（**全程 mock，不触网**）::

    DownloadManager → MBTilesTileSink → MBTilesWriter → MBTilesReader/Validator

最关键的两条断言：

1. **成功下载数量 == 数据库里的瓦片数量**（不多不少）；
2. **XYZ↔TMS 在全链路上一致**：请求 URL 里的 ``TILEROW``（TMS）与库里
   ``tile_row`` 必须对得上，且能用 XYZ 语义读回同一份字节。
   这是决定"将来 osmdroid 显示是否上下颠倒"的那条线。
"""

from __future__ import annotations

import asyncio
import inspect
import io
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.config.settings import (
    DEFAULT_MBTILES_QUEUE_CAPACITY,
    MAX_MBTILES_QUEUE_CAPACITY,
    MIN_MBTILES_QUEUE_CAPACITY,
)
from app.downloader import RateLimiter, RetryPolicy
from app.geo import GeoBounds, calculate_tile_ranges, xyz_y_to_tms_y
from app.mbtiles import (
    MBTilesMetadata,
    MBTilesReader,
    MBTilesTileSink,
    MBTilesWriteError,
    MBTilesWriter,
    validate_mbtiles,
)
from app.models import MapLayerType
from app.providers import PNG_SIGNATURE, TiandituProvider
from app.services.map_download_service import (
    MapDownloadService,
    build_output_path,
    sanitize_map_name,
)

FAKE_TOKEN = "abcdefghijklmn"
LAYER = MapLayerType.VECTOR

def _real_png() -> bytes:
    """一张**真的**能被 Pillow 解码的 PNG，而不是"PNG 文件头 + 垃圾正文"。

    Slice 4 时用合成字节就够了（当时的校验只看 magic bytes）。Slice 8 起
    下载结束后的校验升级成了完整诊断，会真的走 ``Image.open + verify()``，
    于是"头对、正文是垃圾"会被正确地判成损坏瓦片 —— 那不是代码坏了，
    是夹具不再真实。用真图之后 ``validation.valid is True`` 这条断言
    才真正说明"链路写出来的是一份可用地图"。
    """
    buffer = io.BytesIO()
    Image.new("RGBA", (16, 16), (0, 0, 0, 0)).save(buffer, format="PNG")
    data = buffer.getvalue()
    assert data.startswith(PNG_SIGNATURE)
    return data


FAKE_PNG = _real_png()

NO_RETRY = RetryPolicy(max_attempts=1, base_delay_seconds=0.0, max_delay_seconds=0.0)
NO_RATE_LIMIT = RateLimiter.disabled()

#: 0.1° × 0.1° → z12 有 6 张、z12~13 共 18 张（落在 Spec 建议的 10~50 张区间）
SMALL_BOUNDS = GeoBounds(116.0, 39.0, 116.1, 39.1)

#: 0.5° × 0.5° → z12 有 56 张，用来制造"还在下载"的窗口
MID_BOUNDS = GeoBounds(116.0, 39.0, 116.5, 39.5)


# --------------------------------------------------------------------------- #
# 假瓦片服务器
# --------------------------------------------------------------------------- #
class TileServer:
    """可控的假天地图服务：只负责返回 PNG 或指定的失败状态码。"""

    def __init__(self, *, delay: float = 0.0, fail_predicate=None, status_code: int = 500):
        self._delay = delay
        self._fail_predicate = fail_predicate
        self._status_code = status_code
        self.requests: list[httpx.Request] = []

    @staticmethod
    def tile_key(request: httpx.Request) -> tuple[int, int, int]:
        """从 WMTS 请求里取出 ``(zoom, TILECOL, TILEROW)`` —— 全是 TMS 语义。"""
        params = request.url.params
        return (
            int(params["TILEMATRIX"]),
            int(params["TILECOL"]),
            int(params["TILEROW"]),
        )

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail_predicate is not None and self._fail_predicate(*self.tile_key(request)):
            return httpx.Response(
                self._status_code,
                content=b"<html>upstream failure</html>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(200, content=FAKE_PNG, headers={"content-type": "image/png"})


def _make_service(server: TileServer, **overrides) -> MapDownloadService:
    params = {
        "concurrency": 4,
        "retry_policy": NO_RETRY,
        "rate_limiter": NO_RATE_LIMIT,
        "batch_size": 7,
    }
    params.update(overrides)
    return MapDownloadService(
        TiandituProvider(FAKE_TOKEN),
        transport=httpx.MockTransport(server),
        **params,
    )


def _expected_tiles(bounds: GeoBounds, min_zoom: int, max_zoom: int) -> int:
    return sum(r.tile_count for r in calculate_tile_ranges(bounds, min_zoom, max_zoom))


@asynccontextmanager
async def _running(coro):
    """把下载跑成后台 Task，并保证收尾（断言失败也不会留下孤儿任务）。"""
    task = asyncio.create_task(coro)
    try:
        yield task
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# 全成功
# --------------------------------------------------------------------------- #
async def test_download_writes_every_tile_to_mbtiles(work_dir: Path) -> None:
    server = TileServer()
    service = _make_service(server)
    path = work_dir / "beijing.mbtiles"
    expected = _expected_tiles(SMALL_BOUNDS, 12, 13)

    outcome = await service.download_to_mbtiles(
        SMALL_BOUNDS, 12, 13, LAYER, path, map_name="beijing"
    )

    assert outcome.summary.succeeded == expected
    assert outcome.summary.failed == 0
    # 核心不变式：下载成功数 == 库中瓦片数
    assert outcome.tile_count == expected
    assert outcome.validation.valid is True
    assert outcome.is_success is True
    assert outcome.cancelled is False

    with MBTilesReader(path) as reader:
        assert reader.get_tile_count() == expected
        assert reader.get_zoom_range() == (12, 13)


async def test_metadata_matches_requested_range(work_dir: Path) -> None:
    server = TileServer()
    service = _make_service(server)
    path = work_dir / "meta.mbtiles"

    await service.download_to_mbtiles(
        SMALL_BOUNDS, 12, 12, LAYER, path, map_name="meta", description="集成测试"
    )

    with MBTilesReader(path) as reader:
        metadata = reader.get_metadata()

    assert metadata["name"] == "meta"
    assert metadata["description"] == "集成测试"
    assert metadata["format"] == "png"
    assert metadata["type"] == "baselayer"
    assert metadata["minzoom"] == "12"
    assert metadata["maxzoom"] == "12"
    assert metadata["bounds"] == "116.000000,39.000000,116.100000,39.100000"
    # Spec §19（Slice 16）：矩形下载也要**显式**记下区域类型，
    # Inspector 才能区分"这是个矩形包"与"这是个老包、根本没记录区域"。
    assert metadata["region_type"] == "rectangle"

    # 用 Builder 也能重建出同一份元数据（元数据是纯函数式派生的）
    rebuilt = MBTilesMetadata.for_bounds(
        SMALL_BOUNDS, 12, 12, name="meta", description="集成测试", region_type="rectangle"
    )
    assert rebuilt.as_dict() == metadata


async def test_output_directory_contains_single_file(work_dir: Path) -> None:
    """交付物必须是单个 .mbtiles：不能留下 -wal / -shm 旁挂文件。"""
    server = TileServer()
    service = _make_service(server)
    path = work_dir / "tidy.mbtiles"

    await service.download_to_mbtiles(SMALL_BOUNDS, 12, 12, LAYER, path)

    assert sorted(p.name for p in work_dir.iterdir()) == ["tidy.mbtiles"]


async def test_progress_callback_is_invoked(work_dir: Path) -> None:
    server = TileServer()
    service = _make_service(server)
    seen: list[tuple[int, int]] = []

    await service.download_to_mbtiles(
        SMALL_BOUNDS,
        12,
        13,
        LAYER,
        work_dir / "progress.mbtiles",
        on_progress=lambda progress: seen.append((progress.completed, progress.total)),
    )

    assert seen                                        # 至少被调过一次
    assert seen[0][1] == _expected_tiles(SMALL_BOUNDS, 12, 13)
    assert seen[-1][0] == seen[-1][1]                  # 最后一帧是跑完的


async def test_xyz_tms_consistency_end_to_end(work_dir: Path) -> None:
    """Spec §16 的端到端版本：请求用 XYZ、落库用 TMS，翻转恰好发生一次，
    并且能用 XYZ 语义读回同一份字节。

    两套行号体系必须分清：

    * 天地图 ``_w`` 是 Web Mercator 左上原点矩阵集，**协议里的 ``TILEROW``
      就等于 XYZ 的 y**（Slice 2 已确立，且经真实网络 403/200 验证过路径与参数）；
    * MBTiles 规范的 ``tile_row`` 是 **TMS**，原点在左下角。

    因此请求侧与存储侧**本来就该相差一次翻转**。把两侧直接相等作为断言，
    等于要求"库里的瓦片行号是错的"——本用例修正为验证这条翻转边界。
    """
    server = TileServer()
    service = _make_service(server, concurrency=1)
    path = work_dir / "coords.mbtiles"
    z = 12

    await service.download_to_mbtiles(SMALL_BOUNDS, z, z, LAYER, path)

    # 1) 请求侧：URL 里的 (TILECOL, TILEROW)，TILEROW 是 XYZ 语义
    requested_xyz = {
        (col, row) for _, col, row in map(TileServer.tile_key, server.requests)
    }
    assert requested_xyz, "没有发出任何请求"

    # 2) 存储侧：tile_row 必须是 XYZ 翻转一次之后的 TMS 行号
    expected_stored = {(col, xyz_y_to_tms_y(z, row)) for col, row in requested_xyz}
    conn = sqlite3.connect(str(path))
    stored = {
        (col, row)
        for _, col, row in conn.execute(
            "SELECT zoom_level, tile_column, tile_row FROM tiles WHERE zoom_level = ?",
            (z,),
        )
    }
    conn.close()
    assert stored == expected_stored
    # 翻转确实发生了；否则这条用例等于什么都没测
    assert stored != requested_xyz

    # 3) 坐标侧：库里每一行的 tile_row 都等于 XYZ 翻转后的值
    with MBTilesReader(path) as reader:
        for column, row in stored:
            y_xyz = (1 << z) - 1 - row
            assert reader.has_tile(z, column, y_xyz) is True
            assert reader.read_tile(z, column, y_xyz) == FAKE_PNG
            # 反向：把 TMS 行号当 XYZ 用必须查不到（除非该瓦片恰好对称存在）
            if xyz_y_to_tms_y(z, row) != row:
                assert reader.has_tile(z, column, row) is False


# --------------------------------------------------------------------------- #
# 部分 / 全部失败
# --------------------------------------------------------------------------- #
async def test_partial_failure_counts_match_database(work_dir: Path) -> None:
    """一半瓦片 403：成功数 == 库中数量，失败原因被聚合出来。"""
    server = TileServer(fail_predicate=lambda z, x, y: x % 2 == 0, status_code=403)
    service = _make_service(server)
    path = work_dir / "partial.mbtiles"
    expected = _expected_tiles(SMALL_BOUNDS, 12, 12)

    outcome = await service.download_to_mbtiles(SMALL_BOUNDS, 12, 12, LAYER, path)

    assert outcome.summary.failed > 0
    assert outcome.summary.succeeded > 0
    assert outcome.summary.succeeded + outcome.summary.failed == expected
    assert outcome.tile_count == outcome.summary.succeeded
    assert outcome.validation.valid is True

    reasons = dict(outcome.summary.top_failures())
    assert any("403" in reason for reason in reasons)


async def test_all_failed_still_produces_readable_file(work_dir: Path) -> None:
    """全部失败：文件仍然生成（可打开），但校验必须报 FAIL。"""
    server = TileServer(fail_predicate=lambda z, x, y: True, status_code=500)
    service = _make_service(server)
    path = work_dir / "allfailed.mbtiles"
    expected = _expected_tiles(SMALL_BOUNDS, 12, 12)

    outcome = await service.download_to_mbtiles(SMALL_BOUNDS, 12, 12, LAYER, path)

    assert outcome.summary.failed == expected
    assert outcome.summary.succeeded == 0
    assert outcome.tile_count == 0
    assert outcome.validation.valid is False
    assert any("tiles 表为空" in text for text in outcome.validation.errors)
    assert path.is_file()
    assert outcome.is_success is False
    assert any("失败原因" in line for line in outcome.as_lines())


async def test_failed_tiles_are_not_written(work_dir: Path) -> None:
    """失败瓦片不得写库——库里的坐标集合必须恰好等于成功的那批。"""
    server = TileServer(fail_predicate=lambda z, x, y: y % 3 == 0)
    service = _make_service(server)
    path = work_dir / "nowrite.mbtiles"

    outcome = await service.download_to_mbtiles(SMALL_BOUNDS, 12, 12, LAYER, path)

    succeeded_rows = outcome.summary.succeeded
    with MBTilesReader(path) as reader:
        assert reader.get_tile_count() == succeeded_rows
        # 抽样确认每一张都真的是图片（失败页不会被误写进去）
        for _, _, _, data in reader.sample_tiles(5):
            assert data.startswith(PNG_SIGNATURE)


# --------------------------------------------------------------------------- #
# 取消
# --------------------------------------------------------------------------- #
async def test_cancel_keeps_already_downloaded_tiles(work_dir: Path) -> None:
    """取消后：已下载的瓦片保留、文件可用、并在结果里明确标注。"""
    server = TileServer(delay=0.05)
    service = _make_service(server, concurrency=2)
    path = work_dir / "cancelled.mbtiles"
    expected = _expected_tiles(MID_BOUNDS, 12, 12)

    async with _running(
        service.download_to_mbtiles(MID_BOUNDS, 12, 12, LAYER, path)
    ) as task:
        # 等引擎真正跑起来再取消
        for _ in range(200):
            await asyncio.sleep(0.01)
            progress = service.progress
            if progress is not None and progress.completed > 0:
                break
        service.cancel()
        outcome = await task

    assert outcome.cancelled is True
    # Slice 5 起：skipped 专指"已有瓦片未重下"，取消未轮到的记在 unprocessed
    assert outcome.summary.unprocessed > 0
    assert outcome.summary.skipped == 0
    assert outcome.summary.completed < expected        # 确实被打断了
    assert outcome.tile_count == outcome.summary.succeeded
    assert outcome.tile_count > 0                      # 但已下载的都留下来了
    assert path.is_file()
    assert any("已取消" in line for line in outcome.as_lines())

    # 关键：取消后文件依然是**可用**的（校验通过、可读）
    assert validate_mbtiles(path).valid is True
    with MBTilesReader(path) as reader:
        assert reader.get_tile_count() == outcome.summary.succeeded


async def test_cancel_does_not_delete_output_file(work_dir: Path) -> None:
    server = TileServer(delay=0.05)
    service = _make_service(server, concurrency=2)
    path = work_dir / "keep.mbtiles"

    async with _running(
        service.download_to_mbtiles(MID_BOUNDS, 12, 12, LAYER, path)
    ) as task:
        await asyncio.sleep(0.1)
        service.cancel()
        await task

    assert path.exists()


# --------------------------------------------------------------------------- #
# 写库失败
# --------------------------------------------------------------------------- #
async def test_writer_failure_during_download_raises(work_dir: Path) -> None:
    """写库中途失败：必须抛 MBTilesWriteError，而不是静默丢瓦片。"""

    class BrokenWriter(MBTilesWriter):
        def write_tile(self, z, x, y_xyz, data):  # noqa: D102 - 故意打桩
            raise sqlite3.OperationalError("disk I/O error")

    import app.services.map_download_service as module

    original = module.MBTilesWriter
    module.MBTilesWriter = BrokenWriter
    try:
        server = TileServer()
        service = _make_service(server)
        with pytest.raises(MBTilesWriteError, match="写入失败"):
            await service.download_to_mbtiles(
                SMALL_BOUNDS, 12, 12, LAYER, work_dir / "broken.mbtiles"
            )
    finally:
        module.MBTilesWriter = original


async def test_writer_creation_failure_raises(work_dir: Path) -> None:
    """建库阶段就失败（例如目录不可写）：同样归成 MBTilesWriteError。"""

    class UnopenableWriter(MBTilesWriter):
        def open(self) -> None:  # noqa: D102 - 故意打桩
            raise sqlite3.OperationalError("unable to open database file")

    import app.services.map_download_service as module

    original = module.MBTilesWriter
    module.MBTilesWriter = UnopenableWriter
    try:
        service = _make_service(TileServer())
        with pytest.raises(MBTilesWriteError, match="无法创建 MBTiles"):
            await service.download_to_mbtiles(
                SMALL_BOUNDS, 12, 12, LAYER, work_dir / "nope.mbtiles"
            )
    finally:
        module.MBTilesWriter = original


async def test_sink_failure_cancels_download(work_dir: Path) -> None:
    """写库报错后，进度回调应把任务取消掉（继续下载只是白耗配额）。"""
    writer = MBTilesWriter(work_dir / "probe.mbtiles")
    writer.open()
    writer.write_metadata(
        MBTilesMetadata.for_bounds(SMALL_BOUNDS, 12, 12, name="probe")
    )
    sink = MBTilesTileSink(writer)
    sink._failure = sqlite3.OperationalError("disk I/O error")   # noqa: SLF001

    cancelled: list[bool] = []

    class FakeManager:
        def cancel(self) -> None:
            cancelled.append(True)

    service = _make_service(TileServer())
    service._sink = sink                                          # noqa: SLF001
    forward = service._make_progress_forwarder(FakeManager(), None)  # noqa: SLF001

    forward(_progress_stub())

    assert cancelled == [True]
    writer.abort()


def _progress_stub():
    from app.downloader import DownloadProgress

    return DownloadProgress(total=6, completed=1, succeeded=1)


# --------------------------------------------------------------------------- #
# Sink 行为
# --------------------------------------------------------------------------- #
async def test_sink_skips_failed_results(work_dir: Path) -> None:
    from app.downloader import TileCoordinate, TileDownloadResult

    writer = MBTilesWriter(work_dir / "skip.mbtiles")
    writer.open()
    writer.write_metadata(
        MBTilesMetadata.for_bounds(SMALL_BOUNDS, 12, 12, name="skip")
    )
    sink = MBTilesTileSink(writer)
    try:
        await sink.consume(
            TileDownloadResult(
                tile=TileCoordinate(12, 1, 1),
                success=False,
                content=None,
                status_code=403,
                elapsed_ms=1.0,
                attempts=1,
                error="HTTP 403",
            )
        )
        await sink.consume(
            TileDownloadResult(
                tile=TileCoordinate(12, 2, 2),
                success=True,
                content=None,        # 成功但没有内容 -> 同样跳过
                status_code=200,
                elapsed_ms=1.0,
                attempts=1,
                error=None,
            )
        )
        written = await sink.drain()
    finally:
        writer.close()

    assert written == 0
    assert sink.skipped == 2
    assert sink.accepted == 0
    with MBTilesReader(writer.path) as reader:
        assert reader.get_tile_count() == 0


async def test_sink_writes_serially_to_a_single_writer(work_dir: Path) -> None:
    """单消费者不变式：writer.write_tile 不允许被重入（并发调用）。"""
    from app.downloader import TileCoordinate, TileDownloadResult

    depth = {"current": 0, "max": 0, "calls": 0}

    class ProbeWriter(MBTilesWriter):
        def write_tile(self, z, x, y_xyz, data):  # noqa: D102 - 插桩
            depth["current"] += 1
            depth["max"] = max(depth["max"], depth["current"])
            depth["calls"] += 1
            try:
                super().write_tile(z, x, y_xyz, data)
            finally:
                depth["current"] -= 1

    writer = ProbeWriter(work_dir / "serial.mbtiles", batch_size=3)
    writer.open()
    writer.write_metadata(MBTilesMetadata.for_bounds(SMALL_BOUNDS, 12, 12, name="s"))
    sink = MBTilesTileSink(writer, queue_capacity=MIN_MBTILES_QUEUE_CAPACITY)
    sink.start()
    try:
        for index in range(20):
            await sink.consume(
                TileDownloadResult(
                    tile=TileCoordinate(12, index, 0),
                    success=True,
                    content=FAKE_PNG,
                    status_code=200,
                    elapsed_ms=1.0,
                    attempts=1,
                    error=None,
                )
            )
        written = await sink.drain()
    finally:
        writer.close()

    assert written == 20
    assert depth["calls"] == 20
    assert depth["max"] == 1                                   # 从未并发/重入
    with MBTilesReader(writer.path) as reader:
        assert reader.get_tile_count() == 20


async def test_sink_validates_queue_capacity(work_dir: Path) -> None:
    writer = MBTilesWriter(work_dir / "cap.mbtiles")
    writer.open()
    try:
        for bad in (MIN_MBTILES_QUEUE_CAPACITY - 1, MAX_MBTILES_QUEUE_CAPACITY + 1, "x"):
            with pytest.raises(ValueError):
                MBTilesTileSink(writer, queue_capacity=bad)
        sink = MBTilesTileSink(writer, queue_capacity=MIN_MBTILES_QUEUE_CAPACITY)
        assert sink.queue_capacity == MIN_MBTILES_QUEUE_CAPACITY
    finally:
        writer.close()


def test_sink_requires_open_writer(work_dir: Path) -> None:
    writer = MBTilesWriter(work_dir / "closed.mbtiles")
    with pytest.raises(ValueError, match="open"):
        MBTilesTileSink(writer)

    with MBTilesWriter(work_dir / "closed.mbtiles") as opened:
        with pytest.raises(TypeError):
            MBTilesTileSink("not a writer")  # type: ignore[arg-type]
        del opened


def test_default_queue_capacity_is_bounded() -> None:
    assert MIN_MBTILES_QUEUE_CAPACITY <= DEFAULT_MBTILES_QUEUE_CAPACITY
    assert DEFAULT_MBTILES_QUEUE_CAPACITY <= MAX_MBTILES_QUEUE_CAPACITY


# --------------------------------------------------------------------------- #
# 参数与输出路径
# --------------------------------------------------------------------------- #
async def test_invalid_arguments_are_rejected(work_dir: Path) -> None:
    service = _make_service(TileServer())
    path = work_dir / "x.mbtiles"

    with pytest.raises(ValueError):
        await service.download_to_mbtiles("not bounds", 12, 12, LAYER, path)
    with pytest.raises(ValueError):
        await service.download_to_mbtiles(SMALL_BOUNDS, 12, 12, "vec_w", path)


async def test_download_creates_missing_directories(work_dir: Path) -> None:
    service = _make_service(TileServer())
    path = work_dir / "deep" / "nested" / "map.mbtiles"

    outcome = await service.download_to_mbtiles(SMALL_BOUNDS, 12, 12, LAYER, path)

    assert path.is_file()
    assert outcome.output_path == path


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("beijing", "beijing"),
        ("  北京 地图  ", "北京 地图"),
        ("a/b:c", "a_b_c"),
        ("a?b*c", "a_b_c"),
        ("trailing.", "trailing"),
        ("", "offline_map"),
        ("   ", "offline_map"),
        ("CON", "CON_map"),
        ("lpt1", "lpt1_map"),
    ],
)
def test_sanitize_map_name(raw: str, expected: str) -> None:
    assert sanitize_map_name(raw) == expected


def test_build_output_path(work_dir: Path) -> None:
    assert build_output_path(work_dir, "beijing") == work_dir / "beijing.mbtiles"
    assert build_output_path(work_dir, "a/b") == work_dir / "a_b.mbtiles"
    assert build_output_path(work_dir, "") == work_dir / "offline_map.mbtiles"


def test_service_control_methods_are_safe_without_task(work_dir: Path) -> None:
    """没在跑的时候调 pause/resume/cancel 应当是无害的空操作。"""
    service = _make_service(TileServer())
    service.pause()
    service.resume()
    service.cancel()
    assert service.progress is None
    assert service.manager is None
    assert service.tiles_written == 0


async def test_no_real_network_is_used() -> None:
    """契约：本测试文件里的下载一律经过 MockTransport。

    这里顺手确认 ``httpx.MockTransport`` 对异步 handler 的支持仍在
    （Slice 3 依赖这个能力，编译器不会替我们检查）。
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=FAKE_PNG)

    assert inspect.iscoroutinefunction(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await client.get("https://example.invalid/tile")
    assert response.status_code == 200
    assert response.content == FAKE_PNG
