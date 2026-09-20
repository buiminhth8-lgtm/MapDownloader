"""多边形区域续传（Slice 16，Spec §12 / §13 / §27）。

这组用例守的三条：

1. **"已有多少张"只数区域内的**：文件里混着区域外的瓦片时（它的前身可能是
   一个矩形任务），把那些也算进"已有"会让"还要下"偏小，用户面对的是一个
   永远补不平的缺口 —— 进度条永远走不满，而日志说"已完成"。
2. **续传也要按区域裁剪**：分母是 60 却按矩形区间下 100 张，等于续传时
   偷偷把任务的定义改掉了。
3. **GeoJSON 不进 ``tasks.json``**：一份省级边界几 MB，塞进任务文件会让每次
   进度落盘都变成一次大写入；恢复时按 ``region_id`` 回预设表找路径即可。

行政区域预设是**测试内临时注册**的（monkeypatch 掉预设表与资源目录），
因此这些用例既不依赖 `resources/regions/china.geojson` 存在，也不会往
仓库的资源目录里写任何东西。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.downloader import RateLimiter, RetryPolicy
from app.geo import (
    DownloadRegion,
    GeoBounds,
    RegionPreset,
    calculate_tile_range,
    estimate_tile_count,
    tile_x_to_lon,
    tile_y_to_lat,
)
from app.geo import region as region_module
from app.mbtiles import MBTilesMetadata, MBTilesReader, MBTilesWriter
from app.models import MapLayerType
from app.providers import PNG_SIGNATURE, TiandituProvider
from app.services import (
    MapResumeError,
    check_resume_compatibility,
)
from app.services.map_download_service import (
    MapDownloadService,
    count_existing_region_tiles,
    region_target_tiles,
)
from app.tasks import TaskService, TaskStore

FAKE_TOKEN = "abcdefghijklmn"
LAYER = MapLayerType.VECTOR
ZOOM = 12

BASE_X, BASE_Y = 3372, 1552
BLOCK = 10
HIT_COLS, HIT_ROWS = 6, 10
EDGE_NUDGE = 1e-4
BOUNDARY_VERSION = "2026.09-fixture"


def _real_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (16, 16), (0, 0, 0, 0)).save(buffer, format="PNG")
    data = buffer.getvalue()
    assert data.startswith(PNG_SIGNATURE)
    return data


FAKE_PNG = _real_png()
NO_RETRY = RetryPolicy(max_attempts=1, base_delay_seconds=0.0, max_delay_seconds=0.0)
NO_RATE_LIMIT = RateLimiter.disabled()


# --------------------------------------------------------------------------- #
# 夹具：由瓦片数学算出来的外包矩形与命中环（与 test_polygon_download 同源）
# --------------------------------------------------------------------------- #
def block_bounds() -> GeoBounds:
    return GeoBounds(
        west=tile_x_to_lon(BASE_X, ZOOM),
        south=tile_y_to_lat(BASE_Y + BLOCK, ZOOM) + EDGE_NUDGE,
        east=tile_x_to_lon(BASE_X + BLOCK, ZOOM) - EDGE_NUDGE,
        north=tile_y_to_lat(BASE_Y, ZOOM),
    )


def all_tiles() -> set[tuple[int, int]]:
    return {
        (x, y)
        for y in range(BASE_Y, BASE_Y + BLOCK)
        for x in range(BASE_X, BASE_X + BLOCK)
    }


def in_region_tiles() -> set[tuple[int, int]]:
    return {
        (x, y)
        for y in range(BASE_Y, BASE_Y + HIT_ROWS)
        for x in range(BASE_X, BASE_X + HIT_COLS)
    }


def out_of_region_tiles(columns: int = 3) -> set[tuple[int, int]]:
    """区域**以东**的若干列（这些瓦片绝不该被下）。"""
    return {
        (x, y)
        for y in range(BASE_Y, BASE_Y + HIT_ROWS)
        for x in range(BASE_X + HIT_COLS, BASE_X + HIT_COLS + columns)
    }


def hit_polygon() -> list[list[list[float]]]:
    """命中环：4 条边都缩进到瓦片内部四分之一处（避免"共边也算相交"）。"""
    cw = 360.0 / (2**ZOOM)
    row_h = tile_y_to_lat(BASE_Y, ZOOM) - tile_y_to_lat(BASE_Y + 1, ZOOM)
    row_h_last = tile_y_to_lat(BASE_Y + HIT_ROWS - 1, ZOOM) - tile_y_to_lat(
        BASE_Y + HIT_ROWS, ZOOM
    )
    west = tile_x_to_lon(BASE_X, ZOOM) + cw * 0.25
    east = tile_x_to_lon(BASE_X + HIT_COLS, ZOOM) - cw * 0.25
    north = tile_y_to_lat(BASE_Y, ZOOM) - row_h * 0.25
    south = tile_y_to_lat(BASE_Y + HIT_ROWS, ZOOM) + row_h_last * 0.25
    return [[[west, south], [east, south], [east, north], [west, north], [west, south]]]


@pytest.fixture
def preset(work_dir: Path, monkeypatch) -> RegionPreset:
    """在本用例范围内注册一个临时行政区预设。

    做法是把 ``app.geo.region`` 的**资源目录**指向临时目录、并把预设表换成
    只有这一条的元组。于是 ``RegionPreset.geojson_path`` 会解析到临时文件，
    而仓库的 ``resources/regions/`` 仍然是"一份真实边界都没有"的干净状态。
    """
    polygon_path = work_dir / "test-hit.geojson"
    polygon_path.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {
                    "name": "测试命中区",
                    "id": "test-hit",
                    "boundaryVersion": BOUNDARY_VERSION,
                },
                "geometry": {"type": "Polygon", "coordinates": hit_polygon()},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    item = RegionPreset(
        region_id="test-hit",
        name="测试命中区",
        geojson_filename="test-hit.geojson",
        default_boundary_version=BOUNDARY_VERSION,
    )
    monkeypatch.setattr(region_module, "REGION_RESOURCE_DIR", work_dir)
    monkeypatch.setattr(region_module, "REGION_PRESETS", (item,))
    return item


def region_for(preset: RegionPreset) -> DownloadRegion:
    """由预设构造下载区域，并把候选范围对齐到测试用的 10×10 瓦片块。"""
    return DownloadRegion.from_preset(preset).with_bounds(block_bounds())


# --------------------------------------------------------------------------- #
# 假瓦片服务器 + 服务
# --------------------------------------------------------------------------- #
class TileServer:
    def __init__(self) -> None:
        self.requests: list[tuple[int, int, int]] = []

    @staticmethod
    def tile_key(request: httpx.Request) -> tuple[int, int, int]:
        params = request.url.params
        return (
            int(params["TILEMATRIX"]),
            int(params["TILECOL"]),
            int(params["TILEROW"]),
        )

    def requested_xy(self) -> set[tuple[int, int]]:
        """请求过的 ``(x, y)``。天地图的 ``TILEROW`` 就是 XYZ 的 ``y``。"""
        return {(col, row) for _zoom, col, row in self.requests}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(self.tile_key(request))
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


def write_file_with(
    path: Path,
    tiles: set[tuple[int, int]],
    *,
    region_type: str = "polygon",
    region_id: str = "test-hit",
    boundary_version: str = BOUNDARY_VERSION,
) -> None:
    """直接建一个含指定瓦片的 MBTiles（模拟"下到一半、且混着区域外瓦片"）。"""
    metadata = MBTilesMetadata.for_bounds(
        block_bounds(),
        ZOOM,
        ZOOM,
        name="partial",
        region_type=region_type,
        region_id=region_id,
        boundary_version=boundary_version,
    )
    with MBTilesWriter(path) as writer:
        writer.write_metadata(metadata)
        for x, y in sorted(tiles):
            writer.write_tile(ZOOM, x, y, FAKE_PNG)


def make_task(
    tasks_file: Path,
    path: Path,
    region: DownloadRegion,
    *,
    total_tiles: int,
):
    service = TaskService(TaskStore(tasks_file))
    return service.create_task(
        name="polygon-task",
        bounds=block_bounds(),
        min_zoom=ZOOM,
        max_zoom=ZOOM,
        map_layer=LAYER,
        output_path=path,
        total_tiles=total_tiles,
        region=region,
    )


def tiles_in(path: Path) -> set[tuple[int, int]]:
    with MBTilesReader(path) as reader:
        return {(x, y) for _z, x, y in reader.iter_tile_coordinates(ZOOM)}


# --------------------------------------------------------------------------- #
# 夹具自检
# --------------------------------------------------------------------------- #
def test_fixture_counts_add_up() -> None:
    assert calculate_tile_range(block_bounds(), ZOOM).tile_count == 100
    assert len(in_region_tiles()) == 60
    assert len(out_of_region_tiles()) == 30
    assert in_region_tiles() <= all_tiles()
    assert not (out_of_region_tiles() & in_region_tiles())


def test_region_target_tiles_uses_selected_for_polygon(preset: RegionPreset) -> None:
    """分母：多边形取选中数（60），矩形取候选数（100）。"""
    region = region_for(preset)

    assert region_target_tiles(region, block_bounds(), ZOOM, ZOOM) == 60
    assert region_target_tiles(None, block_bounds(), ZOOM, ZOOM) == 100
    assert (
        region_target_tiles(
            DownloadRegion.rectangle("自定义矩形", block_bounds()),
            block_bounds(),
            ZOOM,
            ZOOM,
        )
        == estimate_tile_count(block_bounds(), ZOOM, ZOOM)
    )


def test_count_existing_region_tiles_ignores_tiles_outside_the_region(
    preset: RegionPreset, work_dir: Path
) -> None:
    """文件里有 70 行（40 内 + 30 外），"已有"必须报 40。"""
    path = work_dir / "mixed.mbtiles"
    inside = set(sorted(in_region_tiles())[:40])
    assert len(inside) == 40
    write_file_with(path, inside | out_of_region_tiles())

    assert len(tiles_in(path)) == 70
    assert count_existing_region_tiles(path, [ZOOM], region_for(preset)) == 40
    # 矩形口径会把 70 全算进去 —— 这正是不能用它的原因
    assert count_existing_region_tiles(path, [ZOOM], None) == 70


def test_count_existing_region_tiles_is_zero_for_a_missing_file(
    preset: RegionPreset, work_dir: Path
) -> None:
    assert count_existing_region_tiles(work_dir / "nope.mbtiles", [ZOOM], region_for(preset)) == 0


def test_count_existing_region_tiles_is_zero_when_all_tiles_are_outside(
    preset: RegionPreset, work_dir: Path
) -> None:
    path = work_dir / "outside_only.mbtiles"
    write_file_with(path, out_of_region_tiles())

    assert count_existing_region_tiles(path, [ZOOM], region_for(preset)) == 0


# --------------------------------------------------------------------------- #
# §27 主用例：60 张目标 / 已有 40 → 跳过 40、下载 20
# --------------------------------------------------------------------------- #
async def test_resume_skips_only_the_in_region_tiles(
    preset: RegionPreset, work_dir: Path, tasks_file: Path
) -> None:
    """已有 40（区域内）+ 30（区域外）→ 跳过 40、下载 20。"""
    region = region_for(preset)
    path = work_dir / "resume.mbtiles"
    inside_first_40 = set(sorted(in_region_tiles())[:40])
    write_file_with(path, inside_first_40 | out_of_region_tiles())
    task = make_task(tasks_file, path, region, total_tiles=60)

    server = TileServer()
    service = make_service(server)
    outcome = await service.resume_task(task, FAKE_TOKEN)

    # --- 计数：只有区域内那 40 张算"已有" ---
    assert outcome.existing_tiles == 40
    assert outcome.summary.skipped == 40
    assert outcome.summary.succeeded == 20
    assert outcome.summary.failed == 0
    assert outcome.summary.total == 60
    assert outcome.target_tiles == 60
    assert outcome.resumed is True

    # --- 请求：只为"区域内且缺失"的那 20 张发请求 ---
    missing = in_region_tiles() - inside_first_40
    assert len(missing) == 20
    assert server.requested_xy() == missing
    # 区域外的瓦片一张都没被请求（续传也走区域裁剪）
    assert not (server.requested_xy() & out_of_region_tiles())

    # --- 落盘：区域内的 60 张齐了；区域外的 30 张原样留着（续传不删别人的数据） ---
    assert in_region_tiles() <= tiles_in(path)
    assert tiles_in(path) == in_region_tiles() | out_of_region_tiles()
    # 任务记录里的目标数也要是 60（用 100 会让进度永远走不满）
    assert task.total_tiles == 60


async def test_second_resume_downloads_nothing(
    preset: RegionPreset, work_dir: Path, tasks_file: Path
) -> None:
    """先完整下一遍（60 张），再续传 → 跳过 60、下载 0。"""
    region = region_for(preset)
    path = work_dir / "done.mbtiles"

    first_server = TileServer()
    first = await make_service(first_server).download_to_mbtiles(
        block_bounds(), ZOOM, ZOOM, LAYER, path, map_name="polygon-task", region=region
    )
    assert first.summary.succeeded == 60
    assert first_server.requested_xy() == in_region_tiles()

    task = make_task(tasks_file, path, region, total_tiles=60)
    second_server = TileServer()
    outcome = await make_service(second_server).resume_task(task, FAKE_TOKEN)

    assert outcome.existing_tiles == 60
    assert outcome.summary.skipped == 60
    assert outcome.summary.succeeded == 0
    assert outcome.summary.total == 60
    assert second_server.requests == []
    # 全部命中已有瓦片同样是"成功"，不是"什么都没干"
    assert outcome.is_success is True


async def test_resume_from_a_full_rectangle_file_counts_only_in_region_existing(
    preset: RegionPreset, work_dir: Path, tasks_file: Path
) -> None:
    """文件是矩形下的（100 张全在）→ 区域内的 60 张全算"已有"，一张都不用下。

    这是最容易写错的一种：文件里的瓦片确实都在，只是多了 40 张区域外的。
    如果"已有"按全量算，结果看起来是对的（跳 100、下 0），但进度分母会是
    100 而不是 60，用户看到的是"60/100"这种永远补不平的进度。
    """
    region = region_for(preset)
    path = work_dir / "rectangle_first.mbtiles"
    write_file_with(path, all_tiles(), region_type="rectangle", region_id="")

    server = TileServer()
    service = make_service(server)
    task = make_task(tasks_file, path, region, total_tiles=60)

    outcome = await service.resume_task(task, FAKE_TOKEN)

    assert outcome.existing_tiles == 60
    assert outcome.summary.skipped == 60
    assert outcome.summary.succeeded == 0
    assert outcome.summary.total == 60
    assert server.requests == []


async def test_resume_denominator_follows_the_recorded_region_type(
    preset: RegionPreset, work_dir: Path, tasks_file: Path
) -> None:
    """分母跟着**记录里的**区域类型走：矩形记录 → 100，多边形记录 → 60。

    两条记录指向内容完全相同的文件（只有 40 张），差别只在 ``region_type``
    这一个字段上。把两种情况放在同一个用例里，是为了让"分母由记录决定、
    而不是由文件内容决定"这件事一眼可见。
    """
    region = region_for(preset)
    inside_first_40 = set(sorted(in_region_tiles())[:40])

    # --- 情况 A：记录里没有区域字段（老记录）→ 按矩形办，分母 100 ---
    rect_path = work_dir / "as_rectangle.mbtiles"
    write_file_with(rect_path, inside_first_40)
    rect_task = make_task(tasks_file, rect_path, region, total_tiles=60)
    rect_task.region_type = ""
    rect_task.region_id = ""
    rect_task.boundary_version = ""

    outcome_rect = await make_service(TileServer()).resume_task(rect_task, FAKE_TOKEN)

    assert outcome_rect.summary.total == 100
    assert outcome_rect.existing_tiles == 40

    # --- 情况 B：同一个文件、记录里写着多边形 → 分母 60 ---
    poly_path = work_dir / "as_polygon.mbtiles"
    write_file_with(poly_path, inside_first_40)
    poly_task = make_task(tasks_file, poly_path, region, total_tiles=60)
    assert poly_task.region_type == "polygon"

    outcome_poly = await make_service(TileServer()).resume_task(poly_task, FAKE_TOKEN)

    assert outcome_poly.summary.total == 60
    assert outcome_poly.existing_tiles == 40
    assert outcome_poly.summary.succeeded == 20


async def test_resume_refuses_when_the_boundary_is_gone(
    work_dir: Path, tasks_file: Path, monkeypatch
) -> None:
    """记录说是行政区域、但本版本找不到边界 → 拒绝，**不回退成矩形**。

    回退的后果是把"继续下 20 张"变成"沿外包矩形再下 60 张"，用户只在账单上
    看得出（Spec §12/§32）。
    """
    region = DownloadRegion(
        region_type=region_module.RegionType.POLYGON,
        name="已消失的行政区",
        bounds=block_bounds(),
        region_id="ghost",
        boundary_version="v1",
        geojson_path=str(work_dir / "ghost.geojson"),
    )
    path = work_dir / "ghost.mbtiles"
    path.write_bytes(b"")  # 文件是否存在不影响这条路径
    task = make_task(tasks_file, path, region, total_tiles=60)

    monkeypatch.setattr(region_module, "REGION_PRESETS", ())

    server = TileServer()
    with pytest.raises(MapResumeError, match="找不到对应的行政区边界"):
        await make_service(server).resume_task(task, FAKE_TOKEN)

    assert server.requests == [], "拒绝时不应发出任何请求"


# --------------------------------------------------------------------------- #
# §13：GeoJSON 不进任务文件
# --------------------------------------------------------------------------- #
def test_tasks_file_stores_only_region_identifiers(
    preset: RegionPreset, work_dir: Path, tasks_file: Path
) -> None:
    """``tasks.json`` 里只能有类型 / id / 版本，**不能有边界坐标**。"""
    region = region_for(preset)
    path = work_dir / "task.mbtiles"
    task = make_task(tasks_file, path, region, total_tiles=60)

    raw = tasks_file.read_text(encoding="utf-8")
    payload = json.loads(raw)["tasks"][0]

    assert payload["region_type"] == "polygon"
    assert payload["region_id"] == "test-hit"
    assert payload["boundary_version"] == BOUNDARY_VERSION

    # 关键：一串一平方米都不该出现。用命中环的第一个坐标做指纹。
    fingerprint = f"{hit_polygon()[0][0][0]:.6f}"
    assert fingerprint in str(hit_polygon()), "指纹应来自夹具本身"
    assert fingerprint not in raw
    assert "coordinates" not in raw
    assert "geojson" not in raw.lower()
    # 文件应当很小：几 MB 的边界塞进来会让每次进度落盘都变成大写入
    assert len(raw) < 4096


def test_old_task_without_region_fields_reads_as_rectangle(
    work_dir: Path, tasks_file: Path
) -> None:
    """老记录（没有 region_* 字段）→ 矩形，行为与 Slice ≤15 一致（Spec §29）。"""
    path = work_dir / "old.mbtiles"
    service = TaskService(TaskStore(tasks_file))
    task = service.create_task(
        name="old-task",
        bounds=block_bounds(),
        min_zoom=ZOOM,
        max_zoom=ZOOM,
        map_layer=LAYER,
        output_path=path,
        total_tiles=100,
    )
    # 模拟 writePackage 之前的老记录：删掉这三个键
    payload = json.loads(tasks_file.read_text(encoding="utf-8"))
    for row in payload["tasks"]:
        for key in ("region_type", "region_id", "boundary_version"):
            row.pop(key, None)
    tasks_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    reloaded = TaskService(TaskStore(tasks_file)).get_task(task.id)

    assert reloaded is not None
    assert reloaded.region_type == ""
    assert region_module.region_from_task(reloaded).region_type is region_module.RegionType.RECTANGLE
    assert region_target_tiles(region_module.region_from_task(reloaded), block_bounds(), ZOOM, ZOOM) == 100


# --------------------------------------------------------------------------- #
# 续传一致性校验里的区域那几条
# --------------------------------------------------------------------------- #
def _metadata(region_type: str, region_id: str, boundary_version: str) -> MBTilesMetadata:
    return MBTilesMetadata.for_bounds(
        block_bounds(),
        ZOOM,
        ZOOM,
        name="pkg",
        region_type=region_type,
        region_id=region_id,
        boundary_version=boundary_version,
    )


def test_region_id_mismatch_is_a_hard_problem(
    preset: RegionPreset, work_dir: Path, tasks_file: Path
) -> None:
    """两边都有 region_id 且不同 → 硬拦（不同区域的瓦片不能混写）。"""
    region = region_for(preset)
    task = make_task(tasks_file, work_dir / "x.mbtiles", region, total_tiles=60)

    result = check_resume_compatibility(task, _metadata("polygon", "other-region", BOUNDARY_VERSION))

    assert result.compatible is False
    assert any("行政区域不一致" in item for item in result.problems)


def test_missing_region_id_on_the_file_only_warns(
    preset: RegionPreset, work_dir: Path, tasks_file: Path
) -> None:
    """文件没记区域（老包）→ **不能**判不兼容，那是误报不是保护。"""
    region = region_for(preset)
    task = make_task(tasks_file, work_dir / "x.mbtiles", region, total_tiles=60)

    result = check_resume_compatibility(task, _metadata("", "", ""))

    assert result.compatible is True
    assert result.warnings == []


def test_rectangle_file_with_a_polygon_task_warns(
    preset: RegionPreset, work_dir: Path, tasks_file: Path
) -> None:
    """文件明确记着 rectangle、任务是行政区域 → 提示"包里混着区域外瓦片"。"""
    region = region_for(preset)
    task = make_task(tasks_file, work_dir / "x.mbtiles", region, total_tiles=60)

    result = check_resume_compatibility(task, _metadata("rectangle", "", ""))

    assert result.compatible is True
    assert any("区域外" in item for item in result.warnings)


def test_boundary_version_change_warns(
    preset: RegionPreset, work_dir: Path, tasks_file: Path
) -> None:
    """边界换了版本 → 提示，但放行（瓦片本身仍然有效）。"""
    region = region_for(preset)
    task = make_task(tasks_file, work_dir / "x.mbtiles", region, total_tiles=60)

    result = check_resume_compatibility(task, _metadata("polygon", "test-hit", "2030.01-new"))

    assert result.compatible is True
    assert any("边界版本不同" in item for item in result.warnings)


def test_rectangle_task_gets_no_region_warnings(work_dir: Path, tasks_file: Path) -> None:
    """矩形任务不该被这些规则波及（Spec §7 的兼容红线）。"""
    task = TaskService(TaskStore(tasks_file)).create_task(
        name="rect",
        bounds=block_bounds(),
        min_zoom=ZOOM,
        max_zoom=ZOOM,
        map_layer=LAYER,
        output_path=work_dir / "r.mbtiles",
        total_tiles=100,
        region=DownloadRegion.rectangle("自定义矩形", block_bounds()),
    )

    for metadata in (
        _metadata("", "", ""),
        _metadata("rectangle", "", ""),
        _metadata("rectangle", "", "v2"),
    ):
        result = check_resume_compatibility(task, metadata)
        assert result.compatible is True
        assert result.warnings == []
