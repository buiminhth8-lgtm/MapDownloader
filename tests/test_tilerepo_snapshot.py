"""稳定读取视图（快照）：可复现、跨分片、冻结后不被替换影响（契约 C4/C5）。

快照的核心承诺只有一句：**清单里记的是 sha256，不是"当时的当前版本"**。
于是"冻结 → 别人替换掉这个坐标 → 再读"必须仍然读到旧字节；而"内容被回收 →
再读"必须抛 ``SnapshotExpired``，不能静默少几张。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.geo import GeoBounds
from app.tilerepo import CoordState, Corrupt, SnapshotExpired, TileRepository
from app.tilerepo import snapshot as snapshot_module
from tests.tilerepo_fakes import distinct_png, make_repo, png_bytes

ASSET = "tianditu/vec_w"
LABEL = "tianditu/cva_w"


@pytest.fixture
def repo(work_dir: Path):
    with make_repo(work_dir) as instance:
        instance.ensure_asset(ASSET)
        instance.ensure_asset(LABEL)
        yield instance


def _entries(view) -> list[tuple[str, int, int, int]]:
    return [(r.asset.text, r.coord.z, r.coord.x, r.coord.y) for r in view]


# --------------------------------------------------------------------------- #
# 可复现
# --------------------------------------------------------------------------- #
def test_same_data_yields_same_fingerprint(repo: TileRepository) -> None:
    """同一份数据冻结两次 → 同一个指纹，且第二次不重写文件。

    指纹里**不能**有 ``created_at``：有它的话每次冻结都得到新指纹，"这份导出的
    数据和上次那批是不是同一批"这个问题就再也答不了。
    """
    repo.put_tiles(ASSET, [(5, 1, 1, distinct_png(1)), (5, 1, 2, distinct_png(2))])

    first = repo.freeze_snapshot()
    path = repo.snapshots_dir / f"{first.fingerprint}.json"
    stamp = path.stat().st_mtime_ns

    second = repo.freeze_snapshot()
    assert second.fingerprint == first.fingerprint
    assert second.entries == first.entries == 2
    assert path.stat().st_mtime_ns == stamp, "同数据不该重写快照文件"
    assert len(snapshot_module.snapshot_files(repo)) == 1


def test_new_write_changes_the_fingerprint(repo: TileRepository) -> None:
    repo.put_tiles(ASSET, [(5, 1, 1, distinct_png(1))])
    first = repo.freeze_snapshot()

    repo.put_tiles(ASSET, [(5, 1, 2, distinct_png(2))])
    second = repo.freeze_snapshot()

    assert second.fingerprint != first.fingerprint
    assert (first.entries, second.entries) == (1, 2)
    # 旧清单还在，且内容不变
    with repo.open_snapshot(first.fingerprint) as view:
        assert _entries(view) == [(ASSET, 5, 1, 1)]


# --------------------------------------------------------------------------- #
# 冻结后不被替换影响
# --------------------------------------------------------------------------- #
def test_snapshot_keeps_bytes_across_replacement(repo: TileRepository) -> None:
    old = png_bytes((10, 10, 10))
    new = png_bytes((250, 250, 250))
    repo.put_tiles(ASSET, [(5, 2, 3, old)])
    meta = repo.freeze_snapshot()

    report = repo.put_tiles(ASSET, [(5, 2, 3, new)], allow_replace=True)
    assert report.replaced == 1
    assert repo.read_tile(ASSET, 5, 2, 3).record.data == new  # 当前 = 新

    with repo.open_snapshot(meta.fingerprint) as view:
        records = list(view)
    assert [r.data for r in records] == [old]

    # 指定 epoch 也能读回旧版本（历史版本不是"被覆盖掉了"，是分状态保留）
    versions = list(repo.iter_versions(ASSET, 5, 2, 3))
    assert [item[0] for item in versions] == [CoordState.SUPERSEDED, CoordState.CURRENT]
    assert [item[1] for item in versions] == [1, 2]
    assert versions[0][2].sha256 != versions[1][2].sha256


def test_snapshot_expires_when_content_is_gone(repo: TileRepository) -> None:
    """内容被回收 → 抛 ``SnapshotExpired``，而不是"跳过这一张继续"。

    跳过会产出"少几张但报告说齐全"的包 —— 这种错误在交付验收里最贵。
    """
    repo.put_tiles(ASSET, [(5, 4, 4, png_bytes())])
    meta = repo.freeze_snapshot()

    from app.tilerepo import shard as shard_module

    path = repo.shard_path(ASSET, (0, 6))
    connection = shard_module.open_shard(path)
    try:
        connection.execute("DELETE FROM coords")
        connection.execute("DELETE FROM contents")
    finally:
        connection.close()

    with repo.open_snapshot(meta.fingerprint) as view, pytest.raises(SnapshotExpired):
        list(view)


def test_snapshot_expires_when_shard_file_is_gone(repo: TileRepository) -> None:
    repo.put_tiles(ASSET, [(5, 4, 4, png_bytes())])
    meta = repo.freeze_snapshot()
    path = repo.shard_path(ASSET, (0, 6))
    root = repo.root
    # 仓库开着时句柄还在，Windows 不允许删；先关（等价于"上次运行结束后被清理"）
    repo.close()
    path.unlink()

    with (
        TileRepository.open(root, create=False) as reopened,
        reopened.open_snapshot(meta.fingerprint) as view,
        pytest.raises(SnapshotExpired),
    ):
        list(view)


# --------------------------------------------------------------------------- #
# 跨分片
# --------------------------------------------------------------------------- #
def test_snapshot_spans_shards_in_order(repo: TileRepository) -> None:
    repo.put_tiles(ASSET, [(13, 5, 6, distinct_png(1)), (5, 2, 2, distinct_png(2))])
    repo.put_tiles(LABEL, [(5, 2, 2, distinct_png(3))])

    meta = repo.freeze_snapshot(assets=[ASSET, LABEL])
    with repo.open_snapshot(meta.fingerprint) as view:
        seen = _entries(view)
        assert [r.data for r in view]  # 数据确实读得出来（跨两个分片文件）

    assert seen == [
        (ASSET, 5, 2, 2),
        (ASSET, 13, 5, 6),
        (LABEL, 5, 2, 2),
    ], "清单必须按 asset, z, x, y 有序 —— 导出端要靠这个顺序流式写包"


def test_snapshot_honours_zoom_range_and_bounds(repo: TileRepository) -> None:
    repo.put_tiles(ASSET, [(5, 1, 1, distinct_png(1)), (13, 5000, 3000, distinct_png(2))])

    meta = repo.freeze_snapshot(assets=[ASSET], zoom_range=(5, 5))
    assert meta.entries == 1
    assert meta.selector["zoomRange"] == [5, 5]

    bounds = GeoBounds(west=0.0, south=0.0, east=1.0, north=1.0)
    narrowed = repo.freeze_snapshot(assets=[ASSET], zoom_range=(5, 5), bounds=bounds)
    with repo.open_snapshot(narrowed.fingerprint) as view:
        for record in view:
            assert (record.coord.x, record.coord.y) == (1, 1)


# --------------------------------------------------------------------------- #
# 取消与文件格式
# --------------------------------------------------------------------------- #
def test_cancel_leaves_no_partial_snapshot(repo: TileRepository) -> None:
    repo.put_tiles(ASSET, [(5, x, 1, distinct_png(x)) for x in range(1, 6)])

    state = {"count": 0}

    def stop() -> bool:
        state["count"] += 1
        return state["count"] > 2

    with pytest.raises(Exception):
        repo.freeze_snapshot(should_stop=stop)

    assert snapshot_module.snapshot_files(repo) == []
    leftovers = [item.name for item in repo.snapshots_dir.iterdir() if item.name != ".keep"]
    assert leftovers == [], f"取消后不该留下任何中间文件：{leftovers}"
    assert repo.catalog.list_snapshots() == []


def test_snapshot_file_is_stream_readable(repo: TileRepository) -> None:
    """文件结构：头一行是 JSON 头，entries 一行一条（导出端按行流式读）。"""
    repo.put_tiles(ASSET, [(5, x, 1, distinct_png(x)) for x in range(1, 4)])
    meta = repo.freeze_snapshot()

    path = repo.snapshots_dir / f"{meta.fingerprint}.json"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("{") and lines[0].endswith('"entries":[')
    assert len([line for line in lines[1:-1] if line.strip()]) == 3
    assert lines[-1].strip() == "]}"

    header = json.loads(lines[0][: -len(',"entries":[')] + "}")
    assert header["version"] == snapshot_module.SNAPSHOT_VERSION
    assert header["entryCount"] == 3
    assert header["entriesDigest"]


def test_truncated_snapshot_file_is_reported_as_corrupt(repo: TileRepository) -> None:
    repo.put_tiles(ASSET, [(5, 1, 1, png_bytes())])
    meta = repo.freeze_snapshot()
    path = repo.snapshots_dir / f"{meta.fingerprint}.json"
    path.write_text(path.read_text(encoding="utf-8")[:40], encoding="utf-8")

    with pytest.raises(Corrupt):
        snapshot_module.load_meta(repo, meta.fingerprint)


def test_view_after_close_refuses_to_read(repo: TileRepository) -> None:
    repo.put_tiles(ASSET, [(5, 1, 1, png_bytes())])
    meta = repo.freeze_snapshot()
    view = repo.open_snapshot(meta.fingerprint)
    view.close()
    with pytest.raises(Exception):
        list(view)
    view.close()  # 幂等
