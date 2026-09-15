"""底图 + 中文注记合成的服务层集成测试（Slice 6，Spec §4 / §6 / §10 / §12 / §18）。

链路（**全程 mock，不触网**）::

    表单（MapType + with_labels）→ MapDownloadService._execute
        → CompositeTileDownloader（底图 + 注记各一次请求 → Pillow alpha 合成）
        → MBTilesTileSink → MBTilesWriter（metadata 写入合成信息）
        → MBTilesReader / validate_mbtiles

要锁住的五件事：

1. **一张最终瓦片 = 两次请求**，且只打该地图类型的两个图层；
2. 写进 ``metadata`` 的 **provider / map_type / labels / composition** 四个键
   与产物真实形态一致 —— 它们是续传时判断"还能不能接着写"的唯一依据；
3. 合成产物一律 **PNG**（JPEG 存不下 alpha，Spec §4）；
4. 单图层 + ``with_labels=True`` 这类自相矛盾的组合**当场拒绝**；
5. **合成方式不一致就拒跑**，且一张瓦片都不写（Spec §12）。
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.composer import TileComposer
from app.downloader import RateLimiter, RetryPolicy
from app.geo import GeoBounds, calculate_tile_ranges
from app.mbtiles import (
    MBTilesMetadata,
    MBTilesReader,
    MBTilesWriter,
    validate_mbtiles,
)
from app.models import (
    COMPOSITION_MERGED,
    COMPOSITION_SINGLE,
    MapLayerType,
    MapType,
    composition_for,
)
from app.providers import PNG_SIGNATURE, TiandituProvider
from app.services import MapDownloadService, MapResumeError, read_existing_metadata
from app.tasks import TaskService, TaskStore

FAKE_TOKEN = "abcdefghijklmn"
NO_RETRY = RetryPolicy(max_attempts=1, base_delay_seconds=0.0, max_delay_seconds=0.0)
NO_RATE_LIMIT = RateLimiter.disabled()

#: 0.1° × 0.1° → z12 有 6 张（小、快）
SMALL_BOUNDS = GeoBounds(116.0, 39.0, 116.1, 39.1)


def _png(size: int = 256, color: tuple[int, int, int, int] = (255, 0, 0, 255)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (size, size), color).save(buffer, format="PNG")
    return buffer.getvalue()


class LayerServer:
    """按图层名分别作答的假天地图服务，并记录每一次请求打的是哪个图层。"""

    def __init__(self) -> None:
        #: 底图用红、注记用蓝 —— 合成结果里能看出"两层都真的参与了"
        self._colors = {"vec": (255, 0, 0, 255), "img": (255, 0, 0, 255)}
        self._label_colors = {"cva": (0, 0, 255, 200), "cia": (0, 0, 255, 200)}
        self.requests: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        layer = str(request.url.params.get("LAYER", ""))
        self.requests.append(layer)
        if layer in self._label_colors:
            return httpx.Response(
                200,
                content=_png(color=self._label_colors[layer]),
                headers={"content-type": "image/png"},
            )
        return httpx.Response(
            200,
            content=_png(color=self._colors.get(layer, (0, 255, 0, 255))),
            headers={"content-type": "image/png"},
        )


def _service(server: LayerServer, **overrides) -> MapDownloadService:
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


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 1. 合成下载：请求次数与图层
# --------------------------------------------------------------------------- #
def test_merged_download_requests_base_and_label_for_every_tile(work_dir: Path) -> None:
    """6 张瓦片 × 2 层 = 12 次请求，且只打 vec/cva。"""
    server = LayerServer()
    path = work_dir / "merged.mbtiles"
    expected = _expected_tiles(SMALL_BOUNDS, 12, 12)

    outcome = _run(
        _service(server).download_to_mbtiles(
            SMALL_BOUNDS, 12, 12, MapType.VECTOR, path, with_labels=True
        )
    )

    assert outcome.is_success is True
    assert outcome.tile_count == expected
    assert sorted(server.requests) == sorted(["vec", "cva"] * expected)
    assert set(server.requests) == {"vec", "cva"}


def test_single_layer_download_requests_only_the_base(work_dir: Path) -> None:
    """不要注记时请求量必须**减半** —— 这正是 UI 里那行预估请求量的依据。"""
    server = LayerServer()
    path = work_dir / "single.mbtiles"
    expected = _expected_tiles(SMALL_BOUNDS, 12, 12)

    outcome = _run(
        _service(server).download_to_mbtiles(
            SMALL_BOUNDS, 12, 12, MapType.VECTOR, path, with_labels=False
        )
    )

    assert outcome.tile_count == expected
    assert server.requests == ["vec"] * expected


def test_image_map_type_requests_its_own_layers(work_dir: Path) -> None:
    """影像地图要 ``img`` + ``cia``，不能串成矢量的图层。"""
    server = LayerServer()
    path = work_dir / "image.mbtiles"

    _run(
        _service(server).download_to_mbtiles(
            SMALL_BOUNDS, 12, 12, MapType.IMAGE, path, with_labels=True
        )
    )

    assert set(server.requests) == {"img", "cia"}


# --------------------------------------------------------------------------- #
# 2. metadata：写进文件的那几个键
# --------------------------------------------------------------------------- #
def test_merged_download_writes_composition_metadata(work_dir: Path) -> None:
    """合成模式必须把"我是怎么来的"写进 metadata（Spec §10）。"""
    server = LayerServer()
    path = work_dir / "merged.mbtiles"

    _run(
        _service(server).download_to_mbtiles(
            SMALL_BOUNDS, 12, 12, MapType.VECTOR, path, with_labels=True
        )
    )

    with MBTilesReader(path) as reader:
        raw = reader.get_metadata()

    assert raw["provider"] == "tianditu"
    assert raw["map_type"] == "vector"
    assert raw["labels"] == "true"
    assert raw["composition"] == COMPOSITION_MERGED
    assert raw["format"] == "png"


def test_single_download_writes_single_composition(work_dir: Path) -> None:
    """不合成时也要写，而且要写成 ``single`` —— "没写"和"写了 single"必须能区分。"""
    server = LayerServer()
    path = work_dir / "single.mbtiles"

    _run(
        _service(server).download_to_mbtiles(
            SMALL_BOUNDS, 12, 12, MapType.VECTOR, path, with_labels=False
        )
    )

    with MBTilesReader(path) as reader:
        raw = reader.get_metadata()

    assert raw["composition"] == COMPOSITION_SINGLE
    assert raw["labels"] == "false"


def test_read_existing_metadata_round_trips_slice6_fields(work_dir: Path) -> None:
    """从磁盘读回来的元数据必须带上合成字段。

    漏掉这一步的后果很隐蔽：空串会被当成"Slice ≤5 的老文件（single）"，
    于是**每个合成文件的续传都会被误判成"合成方式不一致"**。
    """
    path = work_dir / "merged.mbtiles"
    writer = MBTilesWriter(path, batch_size=7)
    writer.open()
    writer.write_metadata(
        MBTilesMetadata.for_bounds(
            SMALL_BOUNDS,
            12,
            12,
            name="merged",
            description="",
            provider="tianditu",
            source_map_type=MapType.IMAGE.value,
            with_labels=True,
            composition=COMPOSITION_MERGED,
        )
    )
    writer.close()

    metadata = read_existing_metadata(path)

    assert metadata is not None
    assert metadata.provider == "tianditu"
    assert metadata.source_map_type == "image"
    assert metadata.with_labels is True
    assert metadata.composition == COMPOSITION_MERGED


def _legacy_metadata() -> MBTilesMetadata:
    """**Slice ≤5 形态**的元数据：只有 MBTiles 标准字段，没有本工具自有字段。

    必须直接构造而不是走 :meth:`MBTilesMetadata.for_bounds` —— 那个工厂的默认值
    就是"本工具的默认配置"（provider / map_type / labels / composition 全都带上），
    用它造出来的"老文件"根本不含老文件该有的缺失。
    """
    return MBTilesMetadata(
        name="legacy",
        description="",
        min_zoom=12,
        max_zoom=12,
        west=SMALL_BOUNDS.west,
        south=SMALL_BOUNDS.south,
        east=SMALL_BOUNDS.east,
        north=SMALL_BOUNDS.north,
    )


def test_read_existing_metadata_keeps_legacy_labels_unknown(work_dir: Path) -> None:
    """老文件没有 ``labels`` 键时要读成 ``None``（"不知道"），而不是 ``False``。"""
    path = work_dir / "legacy.mbtiles"
    writer = MBTilesWriter(path, batch_size=7)
    writer.open()
    writer.write_metadata(_legacy_metadata())
    writer.close()

    with MBTilesReader(path) as reader:
        raw = reader.get_metadata()
    assert "composition" not in raw  # 确认这确实是一个"老形态"的文件

    metadata = read_existing_metadata(path)

    assert metadata is not None
    assert metadata.with_labels is None
    assert metadata.composition == ""
    assert metadata.source_map_type == ""


# --------------------------------------------------------------------------- #
# 3. 产物必须是 PNG
# --------------------------------------------------------------------------- #
def test_merged_tiles_are_real_png_and_contain_both_layers(work_dir: Path) -> None:
    """库里的瓦片要真的能解码，而且两层都参与了合成（不是简单复制底图）。"""
    server = LayerServer()
    path = work_dir / "merged.mbtiles"

    _run(
        _service(server).download_to_mbtiles(
            SMALL_BOUNDS, 12, 12, MapType.VECTOR, path, with_labels=True
        )
    )

    first = next(iter(calculate_tile_ranges(SMALL_BOUNDS, 12, 12)))
    with MBTilesReader(path) as reader:
        data = reader.read_tile(first.zoom, first.min_x, first.min_y)

    assert data is not None
    assert data.startswith(PNG_SIGNATURE)
    with Image.open(io.BytesIO(data)) as image:
        assert image.mode == "RGBA"
        # 注记是半透明蓝压在纯红上 → 结果里必然出现"红蓝混合"的像素
        red, _green, blue, _alpha = image.convert("RGBA").getpixel((0, 0))
        assert blue > 0
        assert red > 0


def test_jpeg_output_is_rejected_in_merged_mode(work_dir: Path) -> None:
    """合成模式只能出 PNG：与其落一个"说 jpg 其实是 png"的库，不如当场拒绝。"""
    server = LayerServer()

    with pytest.raises(ValueError) as excinfo:
        _run(
            _service(server).download_to_mbtiles(
                SMALL_BOUNDS,
                12,
                12,
                MapType.VECTOR,
                work_dir / "bad.mbtiles",
                with_labels=True,
                image_format="jpg",
            )
        )

    assert "PNG" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 4. 自相矛盾的参数组合
# --------------------------------------------------------------------------- #
def test_with_labels_requires_a_map_type(work_dir: Path) -> None:
    """``with_labels=True`` 配单个图层必须报错。

    一块注记瓦片没法回答"它属于矢量还是影像"；猜错会把影像的注记贴到矢量底图上，
    成品"看着有字"但整体是错的。
    """
    server = LayerServer()

    with pytest.raises(ValueError) as excinfo:
        _run(
            _service(server).download_to_mbtiles(
                SMALL_BOUNDS,
                12,
                12,
                MapLayerType.VECTOR,
                work_dir / "bad.mbtiles",
                with_labels=True,
            )
        )

    assert "MapType" in str(excinfo.value)


def test_single_layer_type_still_works_without_labels(work_dir: Path) -> None:
    """不合成时仍然接受单个图层（Slice ≤5 的调用方式不能被破坏）。"""
    server = LayerServer()
    path = work_dir / "compat.mbtiles"

    outcome = _run(
        _service(server).download_to_mbtiles(
            SMALL_BOUNDS, 12, 12, MapLayerType.VECTOR, path, with_labels=False
        )
    )

    assert outcome.tile_count == _expected_tiles(SMALL_BOUNDS, 12, 12)
    assert outcome.with_labels is False


# --------------------------------------------------------------------------- #
# 5. 校验器读懂合成信息（Spec §18）
# --------------------------------------------------------------------------- #
def test_validator_accepts_a_consistent_merged_file(work_dir: Path) -> None:
    server = LayerServer()
    path = work_dir / "merged.mbtiles"

    _run(
        _service(server).download_to_mbtiles(
            SMALL_BOUNDS, 12, 12, MapType.VECTOR, path, with_labels=True
        )
    )

    result = validate_mbtiles(path)

    assert result.valid is True, result.errors


def test_validator_rejects_labels_without_merged_composition(work_dir: Path) -> None:
    """``labels=true`` 配 ``composition=single`` 是自相矛盾的文件。"""
    path = work_dir / "inconsistent.mbtiles"
    writer = MBTilesWriter(path, batch_size=7)
    writer.open()
    writer.write_metadata(
        MBTilesMetadata.for_bounds(
            SMALL_BOUNDS,
            12,
            12,
            name="inconsistent",
            description="",
            source_map_type="vector",
            with_labels=True,
            composition=COMPOSITION_SINGLE,  # 与 labels=true 冲突
        )
    )
    writer.write_tile(12, 3367, 1564, _png())
    writer.close()

    result = validate_mbtiles(path)

    assert result.valid is False
    assert any("composition" in error for error in result.errors)


def test_validator_rejects_non_png_merged_file(work_dir: Path) -> None:
    """合成产物必须是 PNG —— 声明了 merged 却是 jpg，说明文件来路不明。"""
    path = work_dir / "jpg-merged.mbtiles"
    writer = MBTilesWriter(path, batch_size=7)
    writer.open()
    writer.write_metadata(
        MBTilesMetadata.for_bounds(
            SMALL_BOUNDS,
            12,
            12,
            name="jpg-merged",
            description="",
            image_format="jpg",
            source_map_type="vector",
            with_labels=True,
            composition=COMPOSITION_MERGED,
        )
    )
    buffer = io.BytesIO()
    Image.new("RGB", (256, 256), (255, 0, 0)).save(buffer, format="JPEG")
    writer.write_tile(12, 3367, 1564, buffer.getvalue())
    writer.close()

    result = validate_mbtiles(path)

    assert result.valid is False
    assert any("PNG" in error for error in result.errors)


# --------------------------------------------------------------------------- #
# 6. 续传：合成方式不一致就拒跑（Spec §12）
# --------------------------------------------------------------------------- #
def _seed(
    path: Path,
    *,
    with_labels: bool,
    map_type: MapType = MapType.VECTOR,
    tiles: int = 3,
    min_zoom: int = 12,
    max_zoom: int = 12,
) -> None:
    """预置一个"下了一半"的地图包，其合成配置由调用方指定。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = MBTilesWriter(path, batch_size=7)
    writer.open()
    writer.write_metadata(
        MBTilesMetadata.for_bounds(
            SMALL_BOUNDS,
            min_zoom,
            max_zoom,
            name=path.stem,
            description="",
            provider="tianditu",
            source_map_type=map_type.value,
            with_labels=with_labels,
            composition=composition_for(with_labels),
        )
    )
    coords = [
        (item.zoom, x, y)
        for item in calculate_tile_ranges(SMALL_BOUNDS, min_zoom, max_zoom)
        for x in range(item.min_x, item.max_x + 1)
        for y in range(item.min_y, item.max_y + 1)
    ][:tiles]
    for z, x, y in coords:
        writer.write_tile(z, x, y, _png())
    writer.close()


def _task_for(
    work_dir: Path,
    *,
    with_labels: bool,
    map_type: MapType = MapType.VECTOR,
    output_name: str = "resume.mbtiles",
):
    task_service = TaskService(TaskStore(work_dir / "tasks.json"))
    total = _expected_tiles(SMALL_BOUNDS, 12, 12)
    task = task_service.create_task(
        name="resume",
        bounds=SMALL_BOUNDS,
        min_zoom=12,
        max_zoom=12,
        # 任务记录里存的是**底图图层**（历史字段），合成与否由 with_labels 承载
        map_layer=map_type.base_layer,
        output_path=work_dir / output_name,
        total_tiles=total,
        with_labels=with_labels,
    )
    return task_service, task, total


def test_resume_matching_composition_succeeds(work_dir: Path) -> None:
    """合成配置一致 → 正常续传，已有瓦片跳过、缺的补上。"""
    server = LayerServer()
    path = work_dir / "resume.mbtiles"
    _seed(path, with_labels=True, tiles=3)
    task_service, task, total = _task_for(work_dir, with_labels=True)

    outcome = _run(
        _service(server).resume_task(task, FAKE_TOKEN, task_service=task_service)
    )

    assert outcome.is_success is True
    with MBTilesReader(path) as reader:
        assert reader.get_tile_count() == total
    # 只请求缺失的那 3 张 × 2 层 = 6 次
    assert len(server.requests) == (total - 3) * 2


def test_resume_merged_task_on_single_file_is_rejected(work_dir: Path) -> None:
    """任务是"合成"、文件是"单层" → 拒跑，且**一张瓦片都不写**（Spec §12）。

    混写出来的既不是 A 也不是 B，只是一份"有的地块有字、有的没有"的坏数据。
    """
    server = LayerServer()
    path = work_dir / "resume.mbtiles"
    _seed(path, with_labels=False, tiles=3)  # 文件只有底图
    task_service, task, _total = _task_for(work_dir, with_labels=True)  # 任务要合成

    with pytest.raises(MapResumeError) as excinfo:
        _run(_service(server).resume_task(task, FAKE_TOKEN, task_service=task_service))

    assert "合成方式不一致" in str(excinfo.value)
    assert server.requests == []  # 校验发生在发请求之前
    with MBTilesReader(path) as reader:
        assert reader.get_tile_count() == 3  # 一张都没多


def test_resume_single_task_on_merged_file_is_rejected(work_dir: Path) -> None:
    """反方向同样要拒：文件是合成包、任务却只想要底图。"""
    server = LayerServer()
    path = work_dir / "resume.mbtiles"
    _seed(path, with_labels=True, tiles=3)
    task_service, task, _total = _task_for(work_dir, with_labels=False)

    with pytest.raises(MapResumeError):
        _run(_service(server).resume_task(task, FAKE_TOKEN, task_service=task_service))

    assert server.requests == []


def test_resume_map_type_mismatch_is_rejected(work_dir: Path) -> None:
    """影像是影像、矢量是矢量：地图类型不同也不能接着写。"""
    server = LayerServer()
    path = work_dir / "resume.mbtiles"
    _seed(path, with_labels=True, map_type=MapType.IMAGE, tiles=3)
    task_service, task, _total = _task_for(work_dir, with_labels=True, map_type=MapType.VECTOR)

    with pytest.raises(MapResumeError) as excinfo:
        _run(_service(server).resume_task(task, FAKE_TOKEN, task_service=task_service))

    assert "地图类型不一致" in str(excinfo.value)
    assert server.requests == []


def test_resume_legacy_file_is_accepted(work_dir: Path) -> None:
    """Slice ≤5 的老文件（没有 composition 键）视作 single，纯底图续传照跑。

    这不是"放行错误"，而是尊重历史事实：那个版本只可能下过单层底图。
    """
    server = LayerServer()
    path = work_dir / "legacy.mbtiles"
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = MBTilesWriter(path, batch_size=7)
    writer.open()
    writer.write_metadata(_legacy_metadata())
    for item in calculate_tile_ranges(SMALL_BOUNDS, 12, 12):
        for x in range(item.min_x, item.min_x + 1):
            for y in range(item.min_y, item.min_y + 1):
                writer.write_tile(item.zoom, x, y, _png())
    writer.close()

    task_service, task, total = _task_for(work_dir, with_labels=False)
    task.output_path = path
    task_service.store.save(task)

    outcome = _run(
        _service(server).resume_task(task, FAKE_TOKEN, task_service=task_service)
    )

    assert outcome.is_success is True
    with MBTilesReader(path) as reader:
        assert reader.get_tile_count() == total


def test_resume_legacy_file_rejects_merged_task(work_dir: Path) -> None:
    """反过来：老文件 + "要合成"的任务 → 必须拒绝（老文件里没有注记）。"""
    server = LayerServer()
    path = work_dir / "legacy.mbtiles"
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = MBTilesWriter(path, batch_size=7)
    writer.open()
    writer.write_metadata(_legacy_metadata())
    writer.write_tile(12, 3367, 1564, _png())
    writer.close()

    task_service, task, _total = _task_for(work_dir, with_labels=True)
    task.output_path = path
    task_service.store.save(task)

    with pytest.raises(MapResumeError):
        _run(_service(server).resume_task(task, FAKE_TOKEN, task_service=task_service))

    assert server.requests == []


# --------------------------------------------------------------------------- #
# 7. 结果对象与任务记录
# --------------------------------------------------------------------------- #
def test_outcome_reports_map_type_and_composition(work_dir: Path) -> None:
    """结果对象要说清"这次到底下的是什么"，界面照它展示。"""
    server = LayerServer()

    merged = _run(
        _service(server).download_to_mbtiles(
            SMALL_BOUNDS, 12, 12, MapType.IMAGE, work_dir / "a.mbtiles", with_labels=True
        )
    )
    single = _run(
        _service(server).download_to_mbtiles(
            SMALL_BOUNDS, 12, 12, MapType.IMAGE, work_dir / "b.mbtiles", with_labels=False
        )
    )

    assert merged.map_type == MapType.IMAGE.value
    assert merged.with_labels is True
    assert single.with_labels is False
    assert merged.composition_text != single.composition_text


def test_metadata_of_outcome_matches_written_file(work_dir: Path) -> None:
    """结果对象里的 metadata 必须与真实写进文件的那些键一致。"""
    server = LayerServer()
    path = work_dir / "merged.mbtiles"

    outcome = _run(
        _service(server).download_to_mbtiles(
            SMALL_BOUNDS, 12, 12, MapType.VECTOR, path, with_labels=True
        )
    )

    with MBTilesReader(path) as reader:
        raw = reader.get_metadata()

    assert dict(outcome.metadata.as_rows())["composition"] == raw["composition"]
    assert outcome.metadata.source_map_type == raw["map_type"]
    assert str(outcome.metadata.with_labels).lower() == raw["labels"]


def test_task_records_composition_for_future_resume(work_dir: Path) -> None:
    """任务记录必须带着 ``with_labels`` —— 下次续传才有比对依据。"""
    server = LayerServer()
    task_service, task, _total = _task_for(work_dir, with_labels=True)

    _run(
        _service(server).download_to_mbtiles(
            SMALL_BOUNDS,
            12,
            12,
            MapType.VECTOR,
            work_dir / "resume.mbtiles",
            with_labels=True,
            task=task,
            task_service=task_service,
        )
    )

    reloaded = TaskService(TaskStore(work_dir / "tasks.json")).get_task(task.id)

    assert reloaded is not None
    assert reloaded.with_labels is True
    assert reloaded.composition == COMPOSITION_MERGED
    assert reloaded.source_map_type is MapType.VECTOR


def test_composer_can_be_swapped_for_a_custom_one(work_dir: Path) -> None:
    """服务层允许注入自定义合成器（便于将来接别的图像后端）。"""
    calls: list[int] = []
    real = TileComposer()

    class CountingComposer(TileComposer):
        def compose(self, base_data: bytes, label_data: bytes) -> bytes:
            calls.append(1)
            return real.compose(base_data, label_data)

    server = LayerServer()
    service = MapDownloadService(
        TiandituProvider(FAKE_TOKEN),
        transport=httpx.MockTransport(server),
        retry_policy=NO_RETRY,
        rate_limiter=NO_RATE_LIMIT,
        composer=CountingComposer(),
    )
    expected = _expected_tiles(SMALL_BOUNDS, 12, 12)

    _run(
        service.download_to_mbtiles(
            SMALL_BOUNDS, 12, 12, MapType.VECTOR, work_dir / "c.mbtiles", with_labels=True
        )
    )

    assert len(calls) == expected
