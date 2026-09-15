"""断点续传端到端集成测试（Slice 5）。

链路（**全程 mock，不触网**）::

    已有 MBTiles → 读取元数据 → 一致性校验 → 枚举目标瓦片
        → 已有瓦片索引（只查坐标，不读 BLOB）→ HTTP worker（只下缺失的）
        → MBTilesTileSink → MBTilesWriter（INSERT OR REPLACE）→ 校验 / 统计

这里要锁住的四件事，任何一件错了都会让用户拿到"看起来下完了、其实是残图"的文件：

1. **只下缺失的**：已有瓦片一张都不重复请求（Spec §4 / §5 / §21）；
2. **默认不读 BLOB**：存在性判断只认坐标三列（Spec §22）；
3. **参数不一致就拒跑**：绝不往不属于这个任务的库里写（Spec §12）；
4. **完成判定看库里的实际张数**：不是"没报错就算完成"（Spec §19）。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.downloader import RateLimiter, ResumePolicy, RetryPolicy, SkipMode
from app.geo import GeoBounds, calculate_tile_ranges, xyz_y_to_tms_y
from app.mbtiles import MBTilesMetadata, MBTilesReader, MBTilesWriter
from app.models import DownloadTaskStatus, MapLayerType
from app.providers import PNG_SIGNATURE, TiandituProvider
from app.services import MapDownloadService, MapResumeError
from app.tasks import TaskService, TaskStore

FAKE_TOKEN = "abcdefghijklmn"
LAYER = MapLayerType.VECTOR

FAKE_PNG = PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + b"tile-payload" * 8

NO_RETRY = RetryPolicy(max_attempts=1, base_delay_seconds=0.0, max_delay_seconds=0.0)
NO_RATE_LIMIT = RateLimiter.disabled()

#: 0.1° × 0.1° → z12 有 6 张、z12~13 共 18 张（小、快，足以覆盖分支）
SMALL_BOUNDS = GeoBounds(116.0, 39.0, 116.1, 39.1)

#: 0.45° × 0.45° → z12~13 约 100 张，用来复现 Spec §21 的"目标 100 张"
BIG_BOUNDS = GeoBounds(116.0, 39.0, 116.45, 39.45)


# --------------------------------------------------------------------------- #
# 假瓦片服务器
# --------------------------------------------------------------------------- #
class TileServer:
    """可控的假天地图服务：返回 PNG，或对指定瓦片返回 500。

    坐标语义（**这一条容易搞反，搞反了地图就上下颠倒**）：

    天地图 WMTS 的 ``_w`` 矩阵集是**左上原点**，``TILEROW`` 就是 **XYZ 的 y**
    ——provider 直接把 ``TileCoordinate.y`` 填进 ``TILEROW``，全程不翻转。
    翻转只发生在**落盘那一刻**（``MBTilesWriter`` 写 ``tile_row = (2**z-1) - y``）。
    """

    def __init__(
        self,
        *,
        delay: float = 0.0,
        fail_xy: set[tuple[int, int]] | None = None,
    ) -> None:
        self._delay = delay
        self._fail_xy = set(fail_xy or ())
        self.requests: list[httpx.Request] = []

    @staticmethod
    def tile_key(request: httpx.Request) -> tuple[int, int, int]:
        """从 WMTS 请求里取出 ``(zoom, TILECOL, TILEROW)`` —— 后两者即 XYZ。"""
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
        _zoom, col, row = self.tile_key(request)
        if (col, row) in self._fail_xy:
            return httpx.Response(
                500,
                content=b"<html>upstream failure</html>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(
            200, content=FAKE_PNG, headers={"content-type": "image/png"}
        )

    @property
    def request_count(self) -> int:
        """发出去的 HTTP 请求总数。"""
        return len(self.requests)

    def requested_coords(self) -> set[tuple[int, int, int]]:
        """请求过的 ``(zoom, TILECOL, TILEROW)`` —— 即 **XYZ** 语义。"""
        return {self.tile_key(request) for request in self.requests}


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


# --------------------------------------------------------------------------- #
# 目标瓦片枚举（XYZ）与"半成品库"预置
# --------------------------------------------------------------------------- #
def _target_tiles(
    bounds: GeoBounds, min_zoom: int, max_zoom: int
) -> list[tuple[int, int, int]]:
    """按 ``(z, x, y_xyz)`` 列出目标瓦片 —— 与引擎的枚举口径一致。"""
    tiles: list[tuple[int, int, int]] = []
    for item in calculate_tile_ranges(bounds, min_zoom, max_zoom):
        for x in range(item.min_x, item.max_x + 1):
            for y in range(item.min_y, item.max_y + 1):
                tiles.append((item.zoom, x, y))
    return tiles


def _seed_mbtiles(
    path: Path,
    coords: list[tuple[int, int, int]],
    *,
    bounds: GeoBounds,
    min_zoom: int,
    max_zoom: int,
    layer: MapLayerType = LAYER,
) -> None:
    """用 Slice 4 的 Writer 预置一个"前几轮下了一半"的地图包。

    刻意走真正的写入路径（而不是自己拼 SQLite）：预置出来的文件与生产环境的
    文件同构，测试才有意义——手拼 SQL 很容易造出一个"能过测试但真实文件不是
    这样"的假象。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = MBTilesWriter(path, batch_size=7)
    writer.open()
    writer.write_metadata(
        MBTilesMetadata.for_bounds(
            bounds,
            min_zoom,
            max_zoom,
            name=path.stem,
            description=f"天地图 {layer.value}",
        )
    )
    for z, x, y in coords:
        writer.write_tile(z, x, y, FAKE_PNG)
    writer.close()


def _new_task(
    work_dir: Path,
    *,
    bounds: GeoBounds = SMALL_BOUNDS,
    min_zoom: int = 12,
    max_zoom: int = 13,
    layer: MapLayerType = LAYER,
    name: str = "beijing",
    output_name: str = "beijing.mbtiles",
):
    """建一条任务记录 + 一个 TaskService（任务文件与地图文件同在临时目录）。"""
    task_service = TaskService(TaskStore(work_dir / "tasks.json"))
    expected = len(_target_tiles(bounds, min_zoom, max_zoom))
    task = task_service.create_task(
        name=name,
        bounds=bounds,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        map_layer=layer,
        output_path=work_dir / output_name,
        total_tiles=expected,
    )
    return task_service, task, expected


def _db_tiles(path: Path) -> int:
    with MBTilesReader(path) as reader:
        return reader.get_tile_count()


def _tms_rows(path: Path) -> set[tuple[int, int, int]]:
    """直接读库里的 ``(zoom_level, tile_column, tile_row)`` —— 全是 TMS 语义。"""
    conn = sqlite3.connect(path)
    try:
        return {
            (int(z), int(x), int(y))
            for z, x, y in conn.execute(
                "SELECT zoom_level, tile_column, tile_row FROM tiles"
            )
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 1. 核心：跳过已有、只下缺失、最终张数达标（Spec §21）
# --------------------------------------------------------------------------- #
def test_resume_skips_existing_and_downloads_the_rest(work_dir: Path) -> None:
    """目标约 100 张、已有 60% → 只发剩下的请求，最终库里张数达标。"""
    server = TileServer()
    service = _make_service(server)

    bounds = BIG_BOUNDS
    tiles = _target_tiles(bounds, 12, 13)
    total = len(tiles)
    assert total >= 80, f"这个区域只有 {total} 张瓦片，测试意图（约 100 张）不成立"

    existing = tiles[: int(total * 0.6)]
    target_path = work_dir / "beijing.mbtiles"
    _seed_mbtiles(target_path, existing, bounds=bounds, min_zoom=12, max_zoom=13)
    assert _db_tiles(target_path) == len(existing)
    assert server.request_count == 0  # 预置过程不该触网

    task_service, task, expected = _new_task(work_dir, bounds=bounds)
    assert expected == total

    outcome = asyncio.run(
        service.resume_task(task, FAKE_TOKEN, task_service=task_service)
    )
    summary = outcome.summary

    assert summary.skipped == len(existing)
    assert summary.succeeded == total - len(existing)
    assert summary.failed == 0
    assert summary.completed == total
    assert server.request_count == total - len(existing)

    assert outcome.resumed is True
    assert outcome.existing_tiles == len(existing)
    assert outcome.target_tiles == total
    assert outcome.tile_count == total  # 库里确实有目标张数
    assert outcome.is_success is True
    assert outcome.verdict is DownloadTaskStatus.COMPLETED
    assert outcome.skip_mode is SkipMode.CACHED

    reloaded = TaskStore(work_dir / "tasks.json").get(task.id)
    assert reloaded is not None
    assert reloaded.status is DownloadTaskStatus.COMPLETED
    assert reloaded.total_tiles == total
    assert reloaded.skipped_tiles == len(existing)
    assert reloaded.succeeded_tiles == total - len(existing)


def test_resume_never_requests_a_tile_that_is_already_present(work_dir: Path) -> None:
    """已有瓦片的 XYZ 坐标与"请求过的坐标"必须完全没有交集。"""
    server = TileServer()
    service = _make_service(server)

    bounds = SMALL_BOUNDS
    tiles = _target_tiles(bounds, 12, 13)
    existing = tiles[: len(tiles) // 2]
    target_path = work_dir / "beijing.mbtiles"
    _seed_mbtiles(target_path, existing, bounds=bounds, min_zoom=12, max_zoom=13)

    task_service, task, total = _new_task(work_dir, bounds=bounds)
    outcome = asyncio.run(service.resume_task(task, FAKE_TOKEN, task_service=task_service))

    # 请求侧与库里的坐标体系一致（都是 XYZ），所以可以直接比集合
    assert set(existing).isdisjoint(server.requested_coords()), "已有瓦片被重复请求了"
    assert len(server.requested_coords()) == total - len(existing)
    assert outcome.summary.skipped == len(existing)
    assert outcome.summary.succeeded == total - len(existing)


def test_second_resume_downloads_nothing(work_dir: Path) -> None:
    """跑完之后再"继续"一次：0 个请求、全部跳过、库里张数不变。"""
    server = TileServer()
    service = _make_service(server)

    bounds = SMALL_BOUNDS
    task_service, task, total = _new_task(work_dir, bounds=bounds)
    first = asyncio.run(service.resume_task(task, FAKE_TOKEN, task_service=task_service))
    assert first.is_success is True
    requests_after_first = server.request_count
    assert requests_after_first == total

    second = asyncio.run(
        service.resume_task(task, FAKE_TOKEN, task_service=task_service)
    )
    assert second.summary.skipped == total
    assert second.summary.succeeded == 0
    assert second.summary.failed == 0
    assert server.request_count == requests_after_first  # 一个请求都没多发
    assert second.tile_count == total
    assert second.is_success is True


def test_resume_after_partial_rounds_only_fetches_the_remainder(work_dir: Path) -> None:
    """前几轮已落了绝大部分（50 + 20 + 25 的模式）→ 这一轮只补剩下的那几张。"""
    bounds = SMALL_BOUNDS
    tiles = _target_tiles(bounds, 12, 12)
    total = len(tiles)
    assert total >= 6, "区域太小，构不出『大部分已有、只缺一点』的场景"

    # 构造出"只缺 5 张（不足 5 张时至少缺 1 张）"的局面
    missing_count = min(5, total - 1)
    missing = tiles[:missing_count]
    existing = tiles[missing_count:]

    server = TileServer()
    service = _make_service(server)
    target_path = work_dir / "beijing.mbtiles"
    _seed_mbtiles(target_path, existing, bounds=bounds, min_zoom=12, max_zoom=12)

    task_service, task, expected = _new_task(
        work_dir, bounds=bounds, min_zoom=12, max_zoom=12
    )
    outcome = asyncio.run(service.resume_task(task, FAKE_TOKEN, task_service=task_service))

    assert expected == total
    assert server.request_count == len(missing)
    assert outcome.summary.skipped == len(existing)
    assert outcome.summary.succeeded == len(missing)
    assert _db_tiles(target_path) == total
    assert outcome.is_success is True


# --------------------------------------------------------------------------- #
# 2. 存在性判断的开销（Spec §22）
# --------------------------------------------------------------------------- #
def test_default_policy_reads_no_tile_blob(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认策略下一张 BLOB 都不能读：几十万张的库读一遍就是分钟级磁盘 IO。"""
    calls: list[tuple[int, int, int]] = []
    original = MBTilesWriter.read_tile

    def spy(self, z: int, x: int, y_xyz: int):
        calls.append((z, x, y_xyz))
        return original(self, z, x, y_xyz)

    monkeypatch.setattr(MBTilesWriter, "read_tile", spy)

    bounds = SMALL_BOUNDS
    tiles = _target_tiles(bounds, 12, 12)
    server = TileServer()
    service = _make_service(server)
    target_path = work_dir / "beijing.mbtiles"
    _seed_mbtiles(target_path, tiles, bounds=bounds, min_zoom=12, max_zoom=12)

    task_service, task, total = _new_task(
        work_dir, bounds=bounds, min_zoom=12, max_zoom=12
    )
    outcome = asyncio.run(service.resume_task(task, FAKE_TOKEN, task_service=task_service))

    assert outcome.summary.skipped == total
    assert calls == [], f"默认策略下不该读 BLOB，实际读了 {len(calls)} 次"


def test_cached_mode_preloads_once_per_zoom_and_never_asks_per_tile(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """小任务走批量缓存：每个 zoom 只查一次，不是每张瓦片一次。"""
    preload: list[int | None] = []
    per_tile: list[tuple[int, int, int]] = []
    original_preload = MBTilesWriter.get_existing_tiles
    original_has = MBTilesWriter.has_tile

    def spy_preload(self, zoom: int | None = None):
        preload.append(zoom)
        return original_preload(self, zoom)

    def spy_has(self, z: int, x: int, y_xyz: int) -> bool:
        per_tile.append((z, x, y_xyz))
        return original_has(self, z, x, y_xyz)

    monkeypatch.setattr(MBTilesWriter, "get_existing_tiles", spy_preload)
    monkeypatch.setattr(MBTilesWriter, "has_tile", spy_has)

    bounds = SMALL_BOUNDS
    tiles = _target_tiles(bounds, 12, 13)
    server = TileServer()
    service = _make_service(server)
    target_path = work_dir / "beijing.mbtiles"
    _seed_mbtiles(target_path, tiles[: len(tiles) // 2], bounds=bounds, min_zoom=12, max_zoom=13)

    task_service, task, _ = _new_task(work_dir, bounds=bounds)
    outcome = asyncio.run(service.resume_task(task, FAKE_TOKEN, task_service=task_service))

    assert outcome.skip_mode is SkipMode.CACHED
    assert sorted(item for item in preload if item is not None) == [12, 13]
    assert per_tile == [], "CACHED 模式不该出现逐张 SQL 查询"


def test_verify_policy_reads_blob_when_explicitly_enabled(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式打开 ``verify_existing_tile`` 后才会读 BLOB —— 这个开关是真有效的。"""
    calls: list[tuple[int, int, int]] = []
    original = MBTilesWriter.read_tile

    def spy(self, z: int, x: int, y_xyz: int):
        calls.append((z, x, y_xyz))
        return original(self, z, x, y_xyz)

    monkeypatch.setattr(MBTilesWriter, "read_tile", spy)

    bounds = SMALL_BOUNDS
    tiles = _target_tiles(bounds, 12, 12)
    server = TileServer()
    service = _make_service(server)
    target_path = work_dir / "beijing.mbtiles"
    _seed_mbtiles(target_path, tiles, bounds=bounds, min_zoom=12, max_zoom=12)

    task_service, task, total = _new_task(
        work_dir, bounds=bounds, min_zoom=12, max_zoom=12
    )
    outcome = asyncio.run(
        service.resume_task(
            task,
            FAKE_TOKEN,
            task_service=task_service,
            resume_policy=ResumePolicy(verify_existing_tile=True),
        )
    )

    assert sorted(calls) == sorted(tiles), "开启校验后每张已有瓦片都应被读一次"
    assert outcome.summary.skipped == total  # 内容都是合法 PNG → 依然全部沿用
    assert outcome.summary.succeeded == 0


# --------------------------------------------------------------------------- #
# 3. XYZ / TMS 端到端（Spec §21）
# --------------------------------------------------------------------------- #
def test_stored_rows_are_tms_while_requests_are_xyz(work_dir: Path) -> None:
    """请求用 XYZ，落盘翻成 TMS —— 两者必须**恰好差一次翻转**。

    这条线决定"将来 osmdroid 里地图会不会上下颠倒"，是本 Slice 最值得钉死的地方。
    """
    bounds = SMALL_BOUNDS
    tiles = _target_tiles(bounds, 12, 12)
    server = TileServer()
    service = _make_service(server)
    task_service, task, total = _new_task(
        work_dir, bounds=bounds, min_zoom=12, max_zoom=12
    )

    outcome = asyncio.run(service.resume_task(task, FAKE_TOKEN, task_service=task_service))
    assert outcome.is_success is True

    rows = _tms_rows(outcome.output_path)
    assert len(rows) == total

    # 至少有若干张瓦片的 tile_row ≠ 请求时的 XYZ y，否则"翻转"这条线根本没被测到
    flipped = [(z, x, y) for (z, x, y) in tiles if xyz_y_to_tms_y(z, y) != y]
    assert flipped, "选择的区域没有跨过赤道，测不到 XYZ↔TMS 翻转"

    requested = server.requested_coords()
    assert requested == set(tiles), "WMTS 请求里的 TILEROW 就是 XYZ y"

    with MBTilesReader(outcome.output_path) as reader:
        for z, x, y_xyz in tiles:
            assert reader.has_tile(z, x, y_xyz) is True
            # 库里存的是翻转后的 TMS 行号，而且确实与请求时的 y 不同
            assert (z, x, xyz_y_to_tms_y(z, y_xyz)) in rows
        # 不存在的坐标必须答 False
        assert reader.has_tile(12, 0, 0) is False


def test_reader_existing_tiles_returns_xyz(work_dir: Path) -> None:
    """``get_existing_tiles`` 对外一律用 XYZ 语义。"""
    bounds = SMALL_BOUNDS
    tiles = _target_tiles(bounds, 12, 12)
    target_path = work_dir / "seed.mbtiles"
    _seed_mbtiles(target_path, tiles, bounds=bounds, min_zoom=12, max_zoom=12)

    with MBTilesReader(target_path) as reader:
        assert reader.get_existing_tiles(12) == set(tiles)
        assert reader.get_existing_tiles(13) == set()
        assert reader.count_tiles_by_zoom() == {12: len(tiles)}


# --------------------------------------------------------------------------- #
# 4. 参数一致性校验（Spec §12）
# --------------------------------------------------------------------------- #
def _assert_incompatible(work_dir: Path, task, target_path: Path) -> None:
    """跑一次续传，断言"一张都不写"并抛出规定文案。"""
    before = _db_tiles(target_path)
    server = TileServer()
    service = _make_service(server)
    task_service = TaskService(TaskStore(work_dir / "tasks.json"))

    with pytest.raises(MapResumeError) as info:
        asyncio.run(service.resume_task(task, FAKE_TOKEN, task_service=task_service))

    assert settings.UI_TEXT_RESUME_INCOMPATIBLE in str(info.value)
    assert server.request_count == 0, "校验失败时不该发出任何请求"
    assert _db_tiles(target_path) == before, "校验失败时不该写入任何瓦片"


def test_resume_refuses_when_bounds_mismatch(work_dir: Path) -> None:
    target_path = work_dir / "beijing.mbtiles"
    _seed_mbtiles(target_path, [], bounds=BIG_BOUNDS, min_zoom=12, max_zoom=13)
    _task_service, task, _ = _new_task(work_dir, bounds=SMALL_BOUNDS)
    _assert_incompatible(work_dir, task, target_path)


def test_resume_refuses_when_zoom_mismatch(work_dir: Path) -> None:
    target_path = work_dir / "beijing.mbtiles"
    _seed_mbtiles(target_path, [], bounds=SMALL_BOUNDS, min_zoom=12, max_zoom=12)
    _task_service, task, _ = _new_task(work_dir, bounds=SMALL_BOUNDS, min_zoom=12, max_zoom=13)
    _assert_incompatible(work_dir, task, target_path)


def test_resume_refuses_when_layer_mismatch(work_dir: Path) -> None:
    target_path = work_dir / "beijing.mbtiles"
    _seed_mbtiles(
        target_path,
        [],
        bounds=SMALL_BOUNDS,
        min_zoom=12,
        max_zoom=13,
        layer=MapLayerType.IMAGE,
    )
    _task_service, task, _ = _new_task(work_dir, bounds=SMALL_BOUNDS, layer=LAYER)
    _assert_incompatible(work_dir, task, target_path)


def test_compatibility_problems_describe_each_mismatch(work_dir: Path) -> None:
    """排查提示要能指出"哪儿不一致"，而不是只丢一句"不兼容"。"""
    from app.services import read_existing_metadata

    target_path = work_dir / "beijing.mbtiles"
    _seed_mbtiles(target_path, [], bounds=BIG_BOUNDS, min_zoom=12, max_zoom=12)
    _task_service, task, _ = _new_task(
        work_dir, bounds=SMALL_BOUNDS, min_zoom=12, max_zoom=13
    )

    metadata = read_existing_metadata(target_path)
    assert metadata is not None

    from app.services import check_resume_compatibility

    result = check_resume_compatibility(task, metadata)
    assert result.compatible is False
    joined = "\n".join(result.problems)
    # 西/南边界相同，所以只有东/北边界与最大缩放三项不一致
    assert "东边界不一致" in joined
    assert "北边界不一致" in joined
    assert "最大缩放不一致" in joined
    assert "西边界不一致" not in joined
    assert settings.UI_TEXT_RESUME_INCOMPATIBLE in result.message


def test_resume_treats_missing_file_as_fresh_download(work_dir: Path) -> None:
    """目标文件不在了 → 按全新下载处理（而不是报错）。"""
    server = TileServer()
    service = _make_service(server)
    task_service, task, total = _new_task(
        work_dir, bounds=SMALL_BOUNDS, min_zoom=12, max_zoom=12
    )

    outcome = asyncio.run(service.resume_task(task, FAKE_TOKEN, task_service=task_service))
    assert outcome.existing_tiles == 0
    assert outcome.summary.skipped == 0
    assert outcome.summary.succeeded == total
    assert server.request_count == total
    assert outcome.is_success is True


# --------------------------------------------------------------------------- #
# 5. 缺 Token / 非法任务（Spec §9）
# --------------------------------------------------------------------------- #
def test_resume_without_token_raises_with_clear_message(work_dir: Path) -> None:
    """Token 不落盘，所以恢复时不给 Token 必须明确报错（不能偷偷去打真网）。"""
    task_service, task, _ = _new_task(
        work_dir, bounds=SMALL_BOUNDS, min_zoom=12, max_zoom=12
    )
    service = MapDownloadService(
        provider=None, transport=httpx.MockTransport(TileServer())
    )

    with pytest.raises(MapResumeError) as info:
        asyncio.run(service.resume_task(task, "", task_service=task_service))
    message = str(info.value)
    assert "Token" in message
    assert "不保存" in message or "不会" in message


def test_resume_rejects_bad_layer_value(work_dir: Path) -> None:
    _task_service, task, _ = _new_task(
        work_dir, bounds=SMALL_BOUNDS, min_zoom=12, max_zoom=12
    )
    task.map_layer = "no-such-layer"
    service = _make_service(TileServer())
    with pytest.raises(MapResumeError, match="图层"):
        asyncio.run(service.resume_task(task, FAKE_TOKEN))


def test_resume_rejects_non_task_object(work_dir: Path) -> None:
    service = _make_service(TileServer())
    with pytest.raises(ValueError, match="DownloadTask"):
        asyncio.run(service.resume_task("not-a-task", FAKE_TOKEN))  # type: ignore[arg-type]


def test_token_never_lands_in_the_task_file(work_dir: Path) -> None:
    """跑完整条续传链路后，tasks.json 里不能出现 Token（Spec §9）。"""
    server = TileServer()
    service = _make_service(server)
    task_service, task, _total = _new_task(
        work_dir, bounds=SMALL_BOUNDS, min_zoom=12, max_zoom=12
    )
    asyncio.run(service.resume_task(task, FAKE_TOKEN, task_service=task_service))

    text = (work_dir / "tasks.json").read_text(encoding="utf-8")
    assert FAKE_TOKEN not in text
    assert "token" not in text.lower()
    assert task.id in text


# --------------------------------------------------------------------------- #
# 6. 部分失败 / 取消后的续传（Spec §18 / §19）
# --------------------------------------------------------------------------- #
def test_partial_failure_keeps_failed_status_and_resume_fills_the_gap(
    work_dir: Path,
) -> None:
    """有瓦片下不到时不能标成已完成；再续传一次只补缺失的那几张。"""
    bounds = SMALL_BOUNDS
    tiles = _target_tiles(bounds, 12, 12)
    total = len(tiles)
    assert total >= 6

    # 让其中两张稳定失败（服务端按 TILECOL / TILEROW 匹配，即 XYZ）
    failing = {(x, y) for _z, x, y in tiles[:2]}
    server = TileServer(fail_xy=failing)
    service = _make_service(server)
    task_service, task, expected = _new_task(
        work_dir, bounds=bounds, min_zoom=12, max_zoom=12
    )
    assert expected == total

    first = asyncio.run(service.resume_task(task, FAKE_TOKEN, task_service=task_service))
    assert first.summary.failed == 2
    assert first.summary.succeeded == total - 2
    assert first.tile_count == total - 2  # 库里确实少了 2 张
    assert first.is_success is False
    assert first.verdict is DownloadTaskStatus.FAILED
    assert "2 张瓦片下载失败" in first.failure_text

    reloaded = TaskStore(work_dir / "tasks.json").get(task.id)
    assert reloaded is not None
    assert reloaded.status is DownloadTaskStatus.FAILED
    assert reloaded.failed_tiles == 2
    assert reloaded.error  # 原因留在记录里，用户才知道再点一次继续有没有用

    # 第二次：网络恢复，只补那 2 张
    server2 = TileServer()
    service2 = _make_service(server2)
    second = asyncio.run(service2.resume_task(task, FAKE_TOKEN, task_service=task_service))
    assert server2.request_count == 2
    assert second.summary.skipped == total - 2
    assert second.summary.succeeded == 2
    assert second.tile_count == total
    assert second.is_success is True

    final = TaskStore(work_dir / "tasks.json").get(task.id)
    assert final is not None
    assert final.status is DownloadTaskStatus.COMPLETED
    assert final.error is None


def test_cancelled_resume_keeps_tiles_and_can_continue(work_dir: Path) -> None:
    """取消后续传：已写入的瓦片全在，再点「继续」只下剩下的。"""
    bounds = GeoBounds(116.0, 39.0, 116.5, 39.5)
    total = len(_target_tiles(bounds, 12, 12))
    assert total >= 15, "需要足够多的瓦片才能在中途取消"

    server = TileServer(delay=0.08)
    service = _make_service(server, concurrency=2)
    task_service, task, expected = _new_task(
        work_dir, bounds=bounds, min_zoom=12, max_zoom=12
    )
    assert expected == total

    state = {"succeeded": 0}

    def on_progress(progress) -> None:
        state["succeeded"] = progress.succeeded

    async def run_and_cancel():
        handle = asyncio.create_task(
            service.resume_task(
                task, FAKE_TOKEN, on_progress=on_progress, task_service=task_service
            )
        )
        for _ in range(500):
            await asyncio.sleep(0.01)
            if state["succeeded"] >= 4:
                break
        service.cancel()
        return await handle

    outcome = asyncio.run(run_and_cancel())

    assert outcome.cancelled is True
    assert outcome.verdict is DownloadTaskStatus.CANCELLED
    written = _db_tiles(outcome.output_path)
    assert 0 < written < total, f"取消时已写入 {written}/{total} 张，期望部分完成"

    reloaded = TaskStore(work_dir / "tasks.json").get(task.id)
    assert reloaded is not None
    assert reloaded.status is DownloadTaskStatus.CANCELLED
    assert reloaded.completed_tiles >= written

    # 再续传：任务记录里已有的那些会被跳过
    server2 = TileServer()
    service2 = _make_service(server2)
    second = asyncio.run(service2.resume_task(task, FAKE_TOKEN, task_service=task_service))
    assert second.summary.skipped >= written
    assert second.tile_count == total
    assert second.is_success is True
    assert server2.request_count == total - second.summary.skipped


# --------------------------------------------------------------------------- #
# 7. 写回同一个文件、不产生重复行、不破坏元数据
# --------------------------------------------------------------------------- #
def test_resume_writes_into_the_same_file_without_duplicates(work_dir: Path) -> None:
    """Spec §6：写回同一个 MBTiles，且不会因为重复写入而多出行。"""
    bounds = SMALL_BOUNDS
    tiles = _target_tiles(bounds, 12, 12)
    total = len(tiles)
    half = total // 2

    target_path = work_dir / "beijing.mbtiles"
    _seed_mbtiles(target_path, tiles[:half], bounds=bounds, min_zoom=12, max_zoom=12)

    server = TileServer()
    service = _make_service(server)
    task_service, task, expected = _new_task(
        work_dir, bounds=bounds, min_zoom=12, max_zoom=12
    )
    outcome = asyncio.run(service.resume_task(task, FAKE_TOKEN, task_service=task_service))

    assert expected == total
    assert outcome.output_path == target_path  # 同一个文件，不是新建的
    assert _db_tiles(target_path) == total  # 不多不少
    assert len(_tms_rows(target_path)) == total

    names = sorted(item.name for item in work_dir.iterdir())
    assert names.count("beijing.mbtiles") == 1
    assert not [name for name in names if name.endswith(("-wal", "-shm"))]


def test_resume_keeps_existing_metadata(work_dir: Path) -> None:
    """续传不重写元数据：用户手工改过的 description 必须留着。"""
    bounds = SMALL_BOUNDS
    target_path = work_dir / "beijing.mbtiles"
    _seed_mbtiles(target_path, [], bounds=bounds, min_zoom=12, max_zoom=12)

    conn = sqlite3.connect(target_path)
    try:
        conn.execute(
            "UPDATE metadata SET value = ? WHERE name = 'description'",
            ("用户自己写的说明",),
        )
        conn.commit()
    finally:
        conn.close()

    server = TileServer()
    service = _make_service(server)
    task_service, task, total = _new_task(
        work_dir, bounds=bounds, min_zoom=12, max_zoom=12
    )
    outcome = asyncio.run(service.resume_task(task, FAKE_TOKEN, task_service=task_service))

    with MBTilesReader(outcome.output_path) as reader:
        assert reader.get_metadata()["description"] == "用户自己写的说明"
    assert outcome.summary.succeeded == total  # 空库 → 全都要下


def test_resume_counts_reported_in_outcome_lines(work_dir: Path) -> None:
    """结果文案要能一眼看出"跳过多少 / 新下多少"（Spec §16 的收尾形态）。"""
    bounds = SMALL_BOUNDS
    tiles = _target_tiles(bounds, 12, 12)
    half = len(tiles) // 2
    target_path = work_dir / "beijing.mbtiles"
    _seed_mbtiles(target_path, tiles[:half], bounds=bounds, min_zoom=12, max_zoom=12)

    server = TileServer()
    service = _make_service(server)
    task_service, task, total = _new_task(
        work_dir, bounds=bounds, min_zoom=12, max_zoom=12
    )
    outcome = asyncio.run(service.resume_task(task, FAKE_TOKEN, task_service=task_service))

    joined = "\n".join(outcome.as_lines())
    assert "续传：" in joined
    assert f"已有 {half:,} 张" in joined
    assert f"本次跳过 {half:,} 张" in joined
    assert f"新下载 {total - half:,} 张" in joined


# --------------------------------------------------------------------------- #
# 8. Spec §23 要求的日志
# --------------------------------------------------------------------------- #
def test_resume_emits_the_required_log_lines(
    work_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Spec §23 点名的几行日志必须真的被记录下来，且**不含 Token**。

    这些日志是排查"用户说没下完"时唯一的线索：单看界面上的数字无法判断
    到底是"已有被跳过"还是"请求根本没发出去"。
    """
    caplog.set_level(logging.INFO)

    bounds = SMALL_BOUNDS
    tiles = _target_tiles(bounds, 12, 12)
    half = len(tiles) // 2
    target_path = work_dir / "beijing.mbtiles"
    _seed_mbtiles(target_path, tiles[:half], bounds=bounds, min_zoom=12, max_zoom=12)

    server = TileServer()
    service = _make_service(server)
    task_service, task, total = _new_task(
        work_dir, bounds=bounds, min_zoom=12, max_zoom=12
    )

    asyncio.run(service.resume_task(task, FAKE_TOKEN, task_service=task_service))

    text = caplog.text
    assert f"Resume task: {task.id}" in text
    assert f"Existing tiles: {half}" in text
    assert f"Remaining tiles: {total - half}" in text
    assert "Resume download started" in text
    assert "Task created id=" in text
    assert "Task persisted id=" in text
    assert "Task completed id=" in text
    assert "已有瓦片索引：" in text
    # 日志里绝不能出现 Token（Spec §9）。httpx 的 INFO 日志会打出
    # ``tk=<Token>`` 的完整 URL，所以 tests/conftest.py 里和生产一样把它压掉了。
    assert FAKE_TOKEN not in text
    assert "tk=" not in text
