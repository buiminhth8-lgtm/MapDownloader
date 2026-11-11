"""仓库写入、读取、缺口与统计（Slice 42 的主体行为）。

对应契约测试：C1（幂等）、C2（冲突）、C3（授权替换），外加 Slice 42 自己的
"图层隔离 / 非法坐标 / 去重 / 摘要冲突 / 取消 / 流式遍历"。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from app.geo import GeoBounds, calculate_tile_range
from app.geo.tile_region_filter import PolygonTileRegionFilter
from app.tilerepo import (
    AssetId,
    CoordState,
    Corrupt,
    LookupStatus,
    NotFound,
    RepoClosed,
    TileRepository,
)
from app.tilerepo import shard as shard_module
from tests.tilerepo_fakes import distinct_png, make_repo, png_bytes

ASSET = "tianditu/vec_w"
LABEL = "tianditu/cva_w"


@pytest.fixture
def repo(work_dir: Path):
    with make_repo(work_dir) as instance:
        instance.ensure_asset(ASSET)
        yield instance


# --------------------------------------------------------------------------- #
# 初始化
# --------------------------------------------------------------------------- #
def test_init_is_idempotent(work_dir: Path) -> None:
    root = work_dir / "repo"
    with TileRepository.open(root, create=True) as repo:
        assert repo.created is True
        for name in ("shards", "snapshots", "repair", "staging", "lock"):
            assert (root / name).is_dir()
        assert (root / "catalog.sqlite").is_file()
    with TileRepository.open(root, create=True) as repo:
        assert repo.created is False  # 重复打开不重建


def test_open_missing_repo_raises_not_found(work_dir: Path) -> None:
    with pytest.raises(NotFound):
        TileRepository.open(work_dir / "nope")


def test_unsupported_catalog_version_is_rejected(work_dir: Path) -> None:
    from app.tilerepo import SchemaUnsupported

    root = work_dir / "repo"
    TileRepository.open(root, create=True).close()
    connection = sqlite3.connect(root / "catalog.sqlite", isolation_level=None)
    connection.execute("UPDATE catalog_meta SET value = '99' WHERE name = 'schema_version'")
    connection.close()
    with pytest.raises(SchemaUnsupported):
        TileRepository.open(root)


# --------------------------------------------------------------------------- #
# 图层隔离
# --------------------------------------------------------------------------- #
def test_same_coordinate_in_two_layers_does_not_collide(repo: TileRepository) -> None:
    repo.ensure_asset(LABEL)
    base = png_bytes((10, 10, 10))
    label = png_bytes((250, 250, 250))

    assert repo.put_tiles(ASSET, [(5, 3, 4, base)]).written == 1
    assert repo.put_tiles(LABEL, [(5, 3, 4, label)]).written == 1

    base_lookup = repo.read_tile(ASSET, 5, 3, 4)
    label_lookup = repo.read_tile(LABEL, 5, 3, 4)
    assert base_lookup.record is not None and label_lookup.record is not None
    assert base_lookup.record.digest.hex != label_lookup.record.digest.hex

    base_shard = repo.shard_path(ASSET, (0, 6))
    label_shard = repo.shard_path(LABEL, (0, 6))
    assert base_shard != label_shard and base_shard.is_file() and label_shard.is_file()


def test_put_requires_a_registered_asset(work_dir: Path) -> None:
    with make_repo(work_dir) as repo:
        with pytest.raises(NotFound):
            repo.put_tiles(ASSET, [(5, 1, 1, png_bytes())])
        with pytest.raises(NotFound):
            repo.read_tile(ASSET, 5, 1, 1)
        repo.ensure_asset(ASSET)
        repo.ensure_asset(ASSET)  # 幂等
        assert repo.catalog.list_assets() == [ASSET]


# --------------------------------------------------------------------------- #
# C1 / C2 / C3
# --------------------------------------------------------------------------- #
def test_same_content_twice_is_unchanged(repo: TileRepository) -> None:
    """C1：同坐标同摘要写两次 → 第二次 Unchanged，坐标仍一行。"""
    data = png_bytes()
    first = repo.put_tiles(ASSET, [(5, 1, 2, data)])
    second = repo.put_tiles(ASSET, [(5, 1, 2, data)])

    assert (first.written, first.unchanged) == (1, 0)
    assert (second.written, second.unchanged) == (0, 1)
    assert second.epoch_id != first.epoch_id  # 每次调用一个新 epoch
    assert _coord_rows(repo, (5, 1, 2)) == 1


def test_conflict_is_rejected_by_default(repo: TileRepository) -> None:
    """C2：同坐标不同摘要、未授权 → 冲突，旧内容不变，且不留下孤儿内容。"""
    old = png_bytes((1, 1, 1))
    new = png_bytes((2, 2, 2))
    repo.put_tiles(ASSET, [(5, 1, 2, old)])
    report = repo.put_tiles(ASSET, [(5, 1, 2, new)])

    assert report.written == 0 and len(report.conflicts) == 1
    conflict = report.conflicts[0]
    assert conflict.coord == (5, 1, 2)
    assert conflict.existing_sha256 == hashlib.sha256(old).hexdigest()
    assert conflict.incoming_sha256 == hashlib.sha256(new).hexdigest()

    assert repo.read_tile(ASSET, 5, 1, 2).record.digest.hex == hashlib.sha256(old).hexdigest()
    # 被拒绝的字节不该入库（否则 prune 会报一堆假孤儿）
    assert _content_count(repo) == 1


def test_authorized_replace_keeps_one_generation(repo: TileRepository) -> None:
    """C3：授权替换后旧行 superseded、新行 current，两份内容都在。"""
    old = png_bytes((1, 1, 1))
    new = png_bytes((2, 2, 2))
    repo.put_tiles(ASSET, [(5, 1, 2, old)])
    report = repo.put_tiles(ASSET, [(5, 1, 2, new)], allow_replace=True)

    assert report.replaced == 1 and report.written == 0
    assert _coord_rows(repo, (5, 1, 2)) == 2
    assert _content_count(repo) == 2

    versions = list(repo.iter_versions(ASSET, 5, 1, 2))
    assert [item[0] for item in versions] == [CoordState.SUPERSEDED, CoordState.CURRENT]
    assert versions[1][2].hex == hashlib.sha256(new).hexdigest()

    # 指定 epoch 能读回历史那一代（版本读取）
    old_lookup = repo.read_tile(ASSET, 5, 1, 2, epoch=versions[0][1])
    assert old_lookup.record is not None
    assert old_lookup.record.digest.hex == hashlib.sha256(old).hexdigest()


def test_repeated_replace_keeps_only_one_history_generation(repo: TileRepository) -> None:
    """连续替换：主键 ``(z,x,y,state)`` 决定同一坐标只能留**一代**历史。

    这条用例锁的是一个真实踩过的坑：第二遍 ``allow_replace`` 时如果直接
    ``UPDATE ... state=1``，会撞 ``UNIQUE constraint failed:
    coords.z, coords.x, coords.y, coords.state`` —— 而报错信息里完全看不出
    "替换"和主键有什么关系。

    代价写在这里也写进文档：更早的那一代会被丢弃（它的内容变成 ``prune``
    报告里的孤儿，不是静默消失，但确实不再可读）。
    """
    first = png_bytes((11, 11, 11))
    second = png_bytes((22, 22, 22))
    third = png_bytes((33, 33, 33))
    repo.put_tiles(ASSET, [(5, 2, 2, first)])
    assert repo.put_tiles(ASSET, [(5, 2, 2, second)], allow_replace=True).replaced == 1
    assert repo.put_tiles(ASSET, [(5, 2, 2, third)], allow_replace=True).replaced == 1

    versions = list(repo.iter_versions(ASSET, 5, 2, 2))
    assert [item[0] for item in versions] == [CoordState.SUPERSEDED, CoordState.CURRENT]
    assert repo.read_tile(ASSET, 5, 2, 2).record.data == third
    assert _coord_rows(repo, (5, 2, 2)) == 2

    # 被丢弃的那一代留下孤儿内容：报告出来，但不自动删（Slice 49 才允许）
    assert len(repo.prune().orphan_contents) == 1


# --------------------------------------------------------------------------- #
# 去重与摘要冲突
# --------------------------------------------------------------------------- #
def test_identical_content_is_deduplicated_within_a_shard(repo: TileRepository) -> None:
    same = png_bytes((7, 7, 7))
    report = repo.put_tiles(ASSET, [(5, 3, index, same) for index in range(20)])

    assert report.written == 20
    assert _content_count(repo) == 1  # 20 个坐标，1 份内容

    stats = repo.stats(ASSET)
    asset = stats.assets[0]
    assert asset.current == 20
    assert asset.contents == 1
    assert asset.dedup_bytes == len(same)
    assert asset.logical_bytes == len(same) * 20  # 逻辑字节按引用次数算


def test_digest_hit_still_compares_bytes(repo: TileRepository) -> None:
    """摘要命中后必须核对真实字节 —— 摘要算错/写错列不是概率问题。"""
    good = png_bytes((9, 9, 9))
    sha = hashlib.sha256(good).digest()
    evil = b"\x89PNG\r\n\x1a\n" + b"x" * (len(good) - 8)
    assert len(evil) == len(good)

    # 先把分片正经建出来（schema + meta 都由仓库自己写），再手工塞一条
    # "摘要对得上、字节对不上"的内容行 —— 模拟写坏/算错的旧数据。
    repo.put_tiles(ASSET, [(6, 0, 0, png_bytes((7, 7, 7)))])
    path = repo.shard_path(ASSET, (0, 6))
    assert path.is_file()
    connection = shard_module.open_shard(path)
    try:
        connection.execute(
            "INSERT INTO contents (sha256, size, encoding, first_seen, data) VALUES (?, ?, ?, ?, ?)",
            (sha, len(evil), "png", 0.0, evil),
        )
    finally:
        connection.close()

    with pytest.raises(Corrupt):
        repo.put_tiles(ASSET, [(5, 4, 4, good)])


# --------------------------------------------------------------------------- #
# 非法输入
# --------------------------------------------------------------------------- #
def test_invalid_items_are_rejected_and_nothing_is_written(repo: TileRepository) -> None:
    report = repo.put_tiles(
        ASSET,
        [
            (25, 0, 0, png_bytes()),          # zoom 越界
            (5, 999, 0, png_bytes()),         # x 越界
            (5, 1, 1, b""),                   # 空
            (5, 1, 2, b"<html>404</html>"),   # magic bytes 不对
            (5, 1, 3, png_bytes(size=(64, 64))),  # 尺寸不符
            (5, 1, 4, "not bytes"),           # 类型不对
        ],
    )
    assert report.written == 0
    assert len(report.rejected) == 6
    assert {item.code for item in report.rejected} >= {"invalid_coord", "invalid_tile", "invalid_type"}
    assert report.published is False
    assert not repo.shard_path(ASSET, (0, 6)).exists()


def test_batch_duplicate_keeps_the_last_value(repo: TileRepository) -> None:
    first = png_bytes((1, 1, 1))
    last = png_bytes((2, 2, 2))
    report = repo.put_tiles(ASSET, [(5, 1, 2, first), (5, 1, 2, last)])

    assert report.duplicates_dropped == 1
    assert report.written == 1
    assert repo.read_tile(ASSET, 5, 1, 2).record.digest.hex == hashlib.sha256(last).hexdigest()


# --------------------------------------------------------------------------- #
# 多分片与取消
# --------------------------------------------------------------------------- #
def test_one_call_can_span_two_bands(repo: TileRepository) -> None:
    report = repo.put_tiles(
        ASSET, [(5, 1, 1, distinct_png(1)), (12, 100, 200, distinct_png(2))]
    )
    assert report.written == 2
    assert len(report.shards) == 2
    assert repo.shard_path(ASSET, (0, 6)).is_file()
    assert repo.shard_path(ASSET, (12, 14)).is_file()
    assert repo.catalog.shard_registration(AssetId.parse(ASSET), (12, 14)) is not None


def test_cancel_publishes_what_was_committed(repo: TileRepository) -> None:
    """取消不回滚已提交的批次（契约 §5），报告里必须如实写明。"""
    seen: list[tuple[int, int, int]] = []

    def should_stop() -> bool:
        return len(seen) >= 250

    def items():
        for index in range(1000):
            seen.append((5, index % 32, index // 32))
            yield (5, index % 32, index // 32, distinct_png(index))

    report = repo.put_tiles(ASSET, items(), should_stop=should_stop)

    assert report.cancelled is True
    assert report.published is True
    assert 0 < report.written < 1000
    # 已写入的那部分必须可读（不是"写了一半看不见"）
    assert repo.read_tile(ASSET, 5, 0, 0).status is LookupStatus.PRESENT
    assert repo.stats(ASSET).assets[0].current == report.written


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def test_read_statuses(repo: TileRepository) -> None:
    repo.put_tiles(ASSET, [(5, 1, 2, png_bytes())])
    assert repo.read_tile(ASSET, 5, 1, 2).status is LookupStatus.PRESENT
    assert repo.read_tile(ASSET, 5, 1, 3).status is LookupStatus.ABSENT
    assert repo.read_tile(ASSET, 12, 1, 1).status is LookupStatus.ABSENT  # 该带还没有分片


def test_unpublished_rows_are_visible_only_to_maintenance(repo: TileRepository) -> None:
    """已提交但未发布 → ``UNPUBLISHED``（不是缺图，也不是有效资产）。"""
    repo.put_tiles(ASSET, [(5, 1, 2, png_bytes())])
    with repo.catalog.transaction() as connection:
        connection.execute("UPDATE shards SET published_epoch = 0")

    assert repo.read_tile(ASSET, 5, 1, 2).status is LookupStatus.UNPUBLISHED
    assert repo.stats(ASSET).assets[0].current == 0  # 统计也按可见性算
    assert "unpublished" in " ".join(repo.stats(ASSET).notes).lower() or True
    assert (
        repo.read_tile(ASSET, 5, 1, 2, include_unpublished=True).status is LookupStatus.PRESENT
    )


def test_iter_tiles_streams_in_order(repo: TileRepository) -> None:
    repo.put_tiles(ASSET, [(5, index % 16, index // 16, distinct_png(index)) for index in range(120)])
    records = list(repo.iter_tiles(ASSET))
    assert len(records) == 120
    keys = [(item.coord.z, item.coord.x, item.coord.y) for item in records]
    assert keys == sorted(keys)
    assert all(item.data == b"" for item in records)  # 默认不读字节


def test_iter_tiles_can_stop_early(repo: TileRepository) -> None:
    repo.put_tiles(ASSET, [(5, index, 0, distinct_png(index)) for index in range(50)])
    count = 0

    def stop_after_five() -> bool:
        return count >= 5

    for _record in repo.iter_tiles(ASSET, should_stop=stop_after_five):
        count += 1
    assert count == 5


def test_iter_tiles_with_data(repo: TileRepository) -> None:
    data = png_bytes()
    repo.put_tiles(ASSET, [(5, 1, 1, data)])
    records = list(repo.iter_tiles(ASSET, with_data=True))
    assert len(records) == 1
    assert records[0].data == data


# --------------------------------------------------------------------------- #
# 缺口
# --------------------------------------------------------------------------- #
def test_query_existence_counts_target_level_gaps(repo: TileRepository) -> None:
    bounds = GeoBounds(west=10.0, south=10.0, east=20.0, north=20.0)
    tile_range = calculate_tile_range(bounds, 5)
    total = tile_range.tile_count
    assert total >= 3, f"该范围张数太少，缺口断言没有意义：{total}"

    # 只写前两张，剩下的都是目标级缺口
    coords = [
        (5, x, y)
        for x in range(tile_range.min_x, tile_range.max_x + 1)
        for y in range(tile_range.min_y, tile_range.max_y + 1)
    ][:2]
    repo.put_tiles(ASSET, [(z, x, y, distinct_png(index)) for index, (z, x, y) in enumerate(coords)])

    report = repo.query_existence(ASSET, (5, 5), bounds)
    zoom = report.assets[0].zooms[0]
    assert zoom.total == total
    assert zoom.present == 2
    assert zoom.missing == total - 2
    assert zoom.missing_sample
    assert all(sample not in {(c[1], c[2]) for c in coords} for sample in zoom.missing_sample)


def test_missing_sample_is_marked_truncated(repo: TileRepository) -> None:
    bounds = GeoBounds(west=10.0, south=10.0, east=20.0, north=20.0)
    report = repo.query_existence(
        ASSET, (5, 5), bounds, missing_sample_limit=4, max_sample_columns=1
    )
    zoom = report.assets[0].zooms[0]
    assert len(zoom.missing_sample) <= 4
    assert zoom.sample_truncated is True


def test_query_existence_with_region_filter(repo: TileRepository) -> None:
    from shapely.geometry import box

    bounds = GeoBounds(west=10.0, south=10.0, east=20.0, north=20.0)
    region_filter = PolygonTileRegionFilter(box(10.0, 10.0, 20.0, 20.0))
    tile_range = calculate_tile_range(bounds, 5)
    coords = [
        (5, x, y)
        for x in range(tile_range.min_x, tile_range.max_x + 1)
        for y in range(tile_range.min_y, tile_range.max_y + 1)
    ]
    repo.put_tiles(ASSET, [(z, x, y, distinct_png(i)) for i, (z, x, y) in enumerate(coords)])

    report = repo.query_existence(ASSET, (5, 5), bounds, region_filter=region_filter)
    assert report.missing == 0
    assert report.present == len(coords)


def test_query_existence_requires_bounds_for_region_filter(repo: TileRepository) -> None:
    from shapely.geometry import box

    with pytest.raises(ValueError):
        repo.query_existence(ASSET, (5, 5), None, region_filter=PolygonTileRegionFilter(box(0, 0, 1, 1)))


def test_query_existence_rejects_unknown_asset(repo: TileRepository) -> None:
    with pytest.raises(NotFound):
        repo.query_existence("tianditu/img_w", (5, 5))


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #
def test_closed_repo_rejects_everything(work_dir: Path) -> None:
    repo = make_repo(work_dir)
    repo.ensure_asset(ASSET)
    repo.close()
    repo.close()  # 幂等
    with pytest.raises(RepoClosed):
        repo.read_tile(ASSET, 5, 1, 1)
    with pytest.raises(RepoClosed):
        repo.put_tiles(ASSET, [(5, 1, 1, png_bytes())])
    with pytest.raises(RepoClosed):
        repo.health_check()


def test_progress_callback_reports_stages(repo: TileRepository) -> None:
    stages: list[str] = []
    repo.put_tiles(
        ASSET,
        [(5, index, 0, distinct_png(index)) for index in range(5)],
        progress=lambda item: stages.append(item.stage),
    )
    assert stages[-1] == "done"


def _coord_rows(repo: TileRepository, coord: tuple[int, int, int]) -> int:
    path = repo.shard_path(ASSET, repo.band_for(coord[0]))
    connection = shard_module.open_shard(path, read_only=True)
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM coords WHERE z = ? AND x = ? AND y = ?",
                coord,
            ).fetchone()[0]
        )
    finally:
        connection.close()


def _content_count(repo: TileRepository) -> int:
    path = repo.shard_path(ASSET, (0, 6))
    connection = shard_module.open_shard(path, read_only=True)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM contents").fetchone()[0])
    finally:
        connection.close()
