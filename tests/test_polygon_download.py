"""多边形区域下载集成（Slice 16，Spec §10 / §26）。

链路（**全程 mock，不触网**）::

    MapDownloadService → build_region_filter → DownloadManager(region_filter)
        → 区域外的候选瓦片在这里被丢掉（不入队、不计数）
        → MBTilesTileSink → MBTilesWriter → MBTilesReader

这组用例回答三个问题：

1. **下的是不是"区域内那批"**：区域外的瓦片**一次请求都不该发出**；
2. **落盘的张数等于 selected**：候选 100 / 命中 60 → 库里就是 60；
3. **筛选不会污染计数**：``failed`` / ``skipped`` 里不能出现"被裁掉"的那 40 张
   —— 否则用户会在收尾报告里看到"失败 1.4 万张"这种纯粹由裁剪造成的假故障
   （Spec §10/§32）。
"""

from __future__ import annotations

import asyncio
import io
import json
import math
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.downloader import RateLimiter, RetryPolicy
from app.geo import (
    DownloadRegion,
    GeoBounds,
    RegionTileEstimator,
    RegionType,
    calculate_tile_range,
    tile_bounds,
    tile_x_to_lon,
    tile_y_to_lat,
)
from app.mbtiles import MBTilesReader
from app.models import MapLayerType
from app.providers import PNG_SIGNATURE, TiandituProvider
from app.services.map_download_service import MapDownloadService

FAKE_TOKEN = "abcdefghijklmn"
LAYER = MapLayerType.VECTOR
ZOOM = 12

#: 基准瓦片（北京一带）与块大小。选 10×10 是为了让候选数正好 **100** ——
#: Spec §26 用的就是这组数字，于是"候选 100 / 命中 60 / 落盘 60"这篇用例
#: 的每一个数都能在断言里写出来，而不是"大概差多少"。
BASE_X, BASE_Y = 3372, 1552
BLOCK = 10

#: 命中区域：列 6 个、行 10 个 → **60 张**。
HIT_COLS, HIT_ROWS = 6, 10

#: 让矩形停在"最后一张瓦片内部"而不是正好落在它的东/南边界上。
#:
#: ``calculate_tile_range`` 取的是"包含边界点的那张瓦片"：东边界若正好等于
#: 第 11 列的西边线，那张瓦片也会被算进候选区间（11 列而不是 10 列）。
#: 1e-4 度在 z12 下约等于 1.1e-3 列 —— 远大于正变换 1e-6 列的吸附窗口，
#: 又远小于一列的宽度（0.0879 度），因此取值是安全的。
EDGE_NUDGE = 1e-4


def _real_png() -> bytes:
    """真能被 Pillow 解码的 PNG（下载收尾会真的走一遍完整诊断）。"""
    buffer = io.BytesIO()
    Image.new("RGBA", (16, 16), (0, 0, 0, 0)).save(buffer, format="PNG")
    data = buffer.getvalue()
    assert data.startswith(PNG_SIGNATURE)
    return data


FAKE_PNG = _real_png()

NO_RETRY = RetryPolicy(max_attempts=1, base_delay_seconds=0.0, max_delay_seconds=0.0)
NO_RATE_LIMIT = RateLimiter.disabled()


# --------------------------------------------------------------------------- #
# 由瓦片数学**算出来**的夹具（不写死坐标）
# --------------------------------------------------------------------------- #
def block_bounds() -> GeoBounds:
    """10×10 瓦片块的外包矩形。"""
    west = tile_x_to_lon(BASE_X, ZOOM)
    east = tile_x_to_lon(BASE_X + BLOCK, ZOOM) - EDGE_NUDGE
    north = tile_y_to_lat(BASE_Y, ZOOM)
    south = tile_y_to_lat(BASE_Y + BLOCK, ZOOM) + EDGE_NUDGE
    return GeoBounds(west=west, south=south, east=east, north=north)


def column_width() -> float:
    """z12 的列宽（度）。经度方向每列等宽。"""
    return 360.0 / (2**ZOOM)


def row_height(row: int) -> float:
    """某一行的高度（度）。纬度方向墨卡托投影下逐行不等高。"""
    return tile_y_to_lat(row, ZOOM) - tile_y_to_lat(row + 1, ZOOM)


def hit_polygon() -> list[list[list[float]]]:
    """命中区域的坐标环：列 ``BASE_X .. BASE_X+5`` × 行 ``BASE_Y .. BASE_Y+9``。

    四条边都**缩进到瓦片内部四分之一处**，不与任何瓦片边界重合。这不是洁癖：
    ``intersects`` 把"只共一条边"也算相交，边贴着瓦片画会让相邻那一整列/行
    也被判命中（60 张变成 70 张）。缩进之后命中集合是确定的 6×10。
    """
    cw = column_width()
    west = tile_x_to_lon(BASE_X, ZOOM) + cw * 0.25
    east = tile_x_to_lon(BASE_X + HIT_COLS, ZOOM) - cw * 0.25
    north = tile_y_to_lat(BASE_Y, ZOOM) - row_height(BASE_Y) * 0.25
    south = tile_y_to_lat(BASE_Y + HIT_ROWS, ZOOM) + row_height(BASE_Y + HIT_ROWS - 1) * 0.25
    return [[[west, south], [east, south], [east, north], [west, north], [west, south]]]


def expected_selected() -> set[tuple[int, int]]:
    """应当被下载的 ``(x, y)`` 集合 —— 用**瓦片下标**直接写出来。

    刻意不复用过滤器来生成这个集合：那样就变成"用被测代码断言被测代码"。
    这里只用到"HIT_COLS 列 × HIT_ROWS 行"这个几何事实。
    """
    return {
        (x, y)
        for y in range(BASE_Y, BASE_Y + HIT_ROWS)
        for x in range(BASE_X, BASE_X + HIT_COLS)
    }


def write_polygon(work_dir: Path, name: str = "hit.geojson") -> tuple[Path, DownloadRegion]:
    """把命中区域写成 GeoJSON，并据此构造多边形 :class:`DownloadRegion`。"""
    path = work_dir / name
    path.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {
                    "name": "测试命中区",
                    "id": "test-hit",
                    "boundaryVersion": "2026.09-fixture",
                },
                "geometry": {"type": "Polygon", "coordinates": hit_polygon()},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    region = DownloadRegion(
        region_type=RegionType.POLYGON,
        name="测试命中区",
        bounds=block_bounds(),
        region_id="test-hit",
        boundary_version="2026.09-fixture",
        geojson_path=str(path),
    )
    return path, region


# --------------------------------------------------------------------------- #
# 假瓦片服务器
# --------------------------------------------------------------------------- #
class TileServer:
    """只负责返回 PNG 或指定的失败状态码；把请求过的 TMS 坐标记下来。"""

    def __init__(self, *, fail_all: bool = False, status_code: int = 500) -> None:
        self._fail_all = fail_all
        self._status_code = status_code
        self.requests: list[tuple[int, int, int]] = []

    @staticmethod
    def tile_key(request: httpx.Request) -> tuple[int, int, int]:
        """从 WMTS 请求里取出 ``(zoom, TILECOL, TILEROW)``（全 TMS 语义）。"""
        params = request.url.params
        return (
            int(params["TILEMATRIX"]),
            int(params["TILECOL"]),
            int(params["TILEROW"]),
        )

    def requested_xyz(self) -> set[tuple[int, int, int]]:
        """请求过的坐标，``(zoom, x, y)`` —— **已经是 XYZ 语义，不需要翻转**。

        天地图的 ``_w`` 矩阵集原点在左上角，协议里的 ``TILEROW`` 就等于 XYZ 的
        ``y``（Slice 2 已确立并经真实网络验证，见
        ``test_download_to_mbtiles.py::test_request_uses_xyz_row_and_storage_uses_tms``）。
        与它相区分的是 **MBTiles 的 ``tile_row``**，那个才是 TMS、相差一次翻转。
        这里多写一句是因为"看到 TILEROW 就翻转"是个很自然的错误，翻完得到的
        是一个永远对不上的集合，而失败信息只会说"两个集合不相等"。
        """
        return {(zoom, col, row) for zoom, col, row in self.requests}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(self.tile_key(request))
        if self._fail_all:
            return httpx.Response(
                self._status_code,
                content=b"<html>upstream failure</html>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(200, content=FAKE_PNG, headers={"content-type": "image/png"})


def make_service(server: TileServer) -> MapDownloadService:
    return MapDownloadService(
        TiandituProvider(FAKE_TOKEN),
        transport=httpx.MockTransport(server),
        concurrency=4,
        retry_policy=NO_RETRY,
        rate_limiter=NO_RATE_LIMIT,
        batch_size=7,
    )


def tiles_in(path: Path, zoom: int = ZOOM) -> set[tuple[int, int]]:
    with MBTilesReader(path) as reader:
        return {(x, y) for _z, x, y in reader.iter_tile_coordinates(zoom)}


# --------------------------------------------------------------------------- #
# 夹具自检：先把"候选 100 / 命中 60"这两件事钉住
# --------------------------------------------------------------------------- #
def test_fixture_block_has_exactly_one_hundred_candidates() -> None:
    """外包矩形在 z12 的候选数必须正好 100（10×10）。"""
    assert calculate_tile_range(block_bounds(), ZOOM).tile_count == 100


def test_fixture_polygon_selects_exactly_sixty_tiles(work_dir: Path) -> None:
    """命中区域必须正好选中 60 张（6 列 × 10 行）。"""
    _path, region = write_polygon(work_dir)

    estimate = RegionTileEstimator(region).estimate(ZOOM, ZOOM)

    assert estimate.candidate_tiles == 100
    assert estimate.selected_tiles == 60
    assert estimate.filtered_tiles == 40


# --------------------------------------------------------------------------- #
# §26 主用例：候选 100 / 命中 60 → 库里 60
# --------------------------------------------------------------------------- #
async def test_polygon_download_writes_only_the_selected_tiles(work_dir: Path) -> None:
    server = TileServer()
    service = make_service(server)
    path = work_dir / "polygon.mbtiles"
    _polygon_path, region = write_polygon(work_dir)
    expected = expected_selected()

    progress_frames: list[int] = []
    outcome = await service.download_to_mbtiles(
        block_bounds(),
        ZOOM,
        ZOOM,
        LAYER,
        path,
        map_name="polygon",
        region=region,
        on_progress=lambda progress: progress_frames.append(progress.total),
    )

    # --- 请求侧：只请求了区域内那 60 张，一张区域外都没有 ---
    requested = {(x, y) for _z, x, y in server.requested_xyz()}
    assert requested == expected
    assert len(server.requests) == 60

    # --- 落盘侧：库里就是 60 张，且全部落在区域内 ---
    assert tiles_in(path) == expected
    with MBTilesReader(path) as reader:
        assert reader.get_tile_count() == 60
        # Spec §19：多边形包必须留下区域标识，Inspector 才不必靠猜
        metadata = reader.get_metadata()
    assert metadata["region_type"] == "polygon"
    assert metadata["region_id"] == "test-hit"
    assert metadata["boundary_version"] == "2026.09-fixture"

    # --- 计数侧：裁剪出来的 40 张不得出现在任何计数里 ---
    assert outcome.summary.total == 60
    assert outcome.summary.succeeded == 60
    assert outcome.summary.failed == 0
    assert outcome.summary.skipped == 0
    assert outcome.summary.completed == 60
    assert outcome.tile_count == 60
    assert outcome.validation.valid is True
    assert outcome.is_success is True

    # --- 进度侧：分母从一开始就是 60（不是候选的 100）---
    assert progress_frames, "至少要有一帧进度"
    assert set(progress_frames) == {60}


async def test_polygon_download_never_requests_outside_tiles(work_dir: Path) -> None:
    """再钉一遍"区域外一次都不请求"（Spec §10：不能先下了再筛）。

    这条单独成例是因为它的失败形态很特殊：如果把过滤放进 worker（下载后再丢弃），
    ``tile_count`` 仍然会是 60，但配额已经被花掉了 —— 只有断言"请求集合"才看得出。
    """
    server = TileServer()
    service = make_service(server)
    path = work_dir / "never.mbtiles"
    _polygon_path, region = write_polygon(work_dir)

    await service.download_to_mbtiles(block_bounds(), ZOOM, ZOOM, LAYER, path, region=region)

    outside = {
        (x, y)
        for y in range(BASE_Y, BASE_Y + BLOCK)
        for x in range(BASE_X, BASE_X + BLOCK)
    } - expected_selected()
    assert outside, "夹具本身应该有一批区域外瓦片"
    for _zoom, x, y in server.requested_xyz():
        assert (x, y) not in outside


async def test_filtered_tiles_do_not_show_up_as_failures(work_dir: Path) -> None:
    """**全部请求失败**时，失败数必须是 60 而不是 100。

    这是"筛选不污染计数"最容易出错也最容易被忽略的一面：如果区域外的瓦片也
    走一遍失败路径，用户看到的是"失败 100 张"，然后开始排查一个根本不存在的
    网络问题 —— 而真相是其中 40 张压根不该被下（Spec §10/§32）。
    """
    server = TileServer(fail_all=True)
    service = make_service(server)
    path = work_dir / "allfail.mbtiles"
    _polygon_path, region = write_polygon(work_dir)

    outcome = await service.download_to_mbtiles(
        block_bounds(), ZOOM, ZOOM, LAYER, path, region=region
    )

    assert outcome.summary.total == 60
    assert outcome.summary.failed == 60
    assert outcome.summary.succeeded == 0
    assert outcome.summary.skipped == 0
    # 失败原因聚合里也只能有 60 张的量
    assert sum(outcome.summary.failures_by_reason.values()) == 60


async def test_rectangle_download_on_the_same_block_still_gets_one_hundred(
    work_dir: Path,
) -> None:
    """同一块矩形、不带区域 → 100 张全下（Spec §7 的回归锚点）。

    与上面几条共用同一套夹具与同一个假服务器，所以"60 vs 100"的差别只可能
    来自 region，不可能来自夹具。
    """
    server = TileServer()
    service = make_service(server)
    path = work_dir / "rectangle.mbtiles"
    bounds = block_bounds()

    outcome = await service.download_to_mbtiles(bounds, ZOOM, ZOOM, LAYER, path)

    assert outcome.summary.total == 100
    assert outcome.summary.succeeded == 100
    assert len(tiles_in(path)) == 100
    assert len(server.requests) == 100
    with MBTilesReader(path) as reader:
        # 矩形也要显式写 region_type（Spec §19）
        assert reader.get_metadata()["region_type"] == "rectangle"


async def test_explicit_rectangle_region_behaves_like_no_region(work_dir: Path) -> None:
    """显式传一个**矩形** ``DownloadRegion`` → 与不传逐位一致。"""
    server = TileServer()
    service = make_service(server)
    path = work_dir / "rect_explicit.mbtiles"
    bounds = block_bounds()
    region = DownloadRegion.rectangle("自定义矩形", bounds)

    outcome = await service.download_to_mbtiles(
        bounds, ZOOM, ZOOM, LAYER, path, region=region
    )

    assert outcome.summary.total == 100
    assert outcome.summary.succeeded == 100
    assert len(tiles_in(path)) == 100


async def test_missing_boundary_file_fails_before_creating_the_database(
    work_dir: Path,
) -> None:
    """边界文件缺失 → 当场失败，**不留下一个空的 .mbtiles**。

    先建库再抛异常的后果是输出目录里攒下一堆 0 字节的地图文件，
    用户既不知道它们是什么，也不敢删。
    """
    server = TileServer()
    service = make_service(server)
    path = work_dir / "should_not_exist.mbtiles"
    region = DownloadRegion(
        region_type=RegionType.POLYGON,
        name="缺失边界",
        bounds=block_bounds(),
        region_id="missing",
        geojson_path=str(work_dir / "definitely-not-here.geojson"),
    )

    with pytest.raises(Exception) as excinfo:
        await service.download_to_mbtiles(block_bounds(), ZOOM, ZOOM, LAYER, path, region=region)

    assert "definitely-not-here.geojson" in str(excinfo.value)
    assert not path.exists()
    assert server.requests == []


async def test_polygon_download_survives_cancel_with_only_selected_tiles(
    work_dir: Path,
) -> None:
    """取消时留下的库同样是"只有区域内那些"。

    取消发生在下载途中，最容易暴露"过滤挂在 worker 上"的实现 —— 那种实现下
    取消前可能已经写进几张区域外瓦片了。这里让延迟足够长、取消足够早，
    然后只断言一件事：库里每一张都在期望集合内。
    """
    server = TileServer()
    service = make_service(server)
    path = work_dir / "cancelled.mbtiles"
    _polygon_path, region = write_polygon(work_dir)

    async def cancel_soon() -> None:
        await asyncio.sleep(0.05)
        task.cancel()

    task = asyncio.create_task(
        service.download_to_mbtiles(block_bounds(), ZOOM, ZOOM, LAYER, path, region=region)
    )
    canceller = asyncio.create_task(cancel_soon())
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        canceller.cancel()

    if path.exists():
        # 库可能只写了 0 张（取消得足够早），但凡写了就只能在期望集合内
        assert tiles_in(path) <= expected_selected()


def test_row_height_helper_is_positive_and_grows_toward_the_equator() -> None:
    """夹具辅助函数自检：同一级别下**各行不等高**（墨卡托的必然结果）。

    写这条是因为 ``hit_polygon`` 的四条缩进量依赖 ``row_height``：它一旦返回
    0 或者负数，缩进就变成"贴边"或"反向越界"，而症状是命中数从 60 变成 70/50
    这种**看起来像过滤写错了**的假失败。

    方向容易记反，所以断言写全：墨卡托把高纬拉伸得更开，于是**同一张瓦片在
    极区覆盖的纬度更少** —— 行高（度）从极地往赤道**递增**（z12 实测：
    y=0 处 0.0076°，赤道处 0.0879°）。
    """
    heights = [row_height(BASE_Y + i) for i in range(3)]
    assert all(h > 0 for h in heights)
    # 往赤道方向（行号增大）行高变大
    assert row_height(0) < row_height(BASE_Y) < row_height(2048)
    # 相邻两行差距很小但不是 0：差一个数量级就会把"缩进"变成"跨行"
    assert 1e-4 < row_height(BASE_Y + 1) / row_height(BASE_Y) - 1.0 < 1e-2
    assert tile_bounds(ZOOM, BASE_X, BASE_Y).north > tile_bounds(ZOOM, BASE_X, BASE_Y).south
