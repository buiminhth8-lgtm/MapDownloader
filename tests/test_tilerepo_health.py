"""健康检查、恢复、索引重建与清理报告（契约 C14/C15 与 Slice 42 的维护承诺）。

三条边界，每条都有对应用例：

1. ``health_check`` **只读**：跑完前后分片字节数必须一样；
2. ``error`` 只给"数据真的坏了"（悬空引用、分片丢失、schema 不认），计数漂移这类
   "跑一次 rebuild 就好"的问题只算 warning —— 否则健康检查天天红色，用户就不看了；
3. ``prune`` 本 Slice **只报告不删除**，``dry_run=False`` 直接说"不支持"。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.tilerepo import (
    Corrupt,
    LookupStatus,
    NotFound,
    SchemaUnsupported,
    TileRepository,
    UnsupportedOperation,
)
from app.tilerepo import shard as shard_module
from app.tilerepo.health import health_check, prune, rebuild_index, recover
from tests.tilerepo_fakes import distinct_png, make_repo, png_bytes

ASSET = "tianditu/vec_w"
BAND = (0, 6)


@pytest.fixture
def repo(work_dir: Path):
    with make_repo(work_dir) as instance:
        instance.ensure_asset(ASSET)
        yield instance


def _shard_size(repo: TileRepository) -> int:
    path = repo.shard_path(ASSET, BAND)
    return sum(
        path.with_name(path.name + suffix).stat().st_size
        for suffix in ("", "-wal", "-shm")
        if path.with_name(path.name + suffix).exists()
    )


def _tamper(connection, sql: str, params: tuple = ()) -> None:
    connection.execute(sql, params)


# --------------------------------------------------------------------------- #
# 只读
# --------------------------------------------------------------------------- #
def test_health_check_never_writes(repo: TileRepository) -> None:
    repo.put_tiles(ASSET, [(5, x, 1, distinct_png(x)) for x in range(1, 4)])
    before = {path.name: path.stat().st_size for path in repo.root.rglob("*") if path.is_file()}

    report = repo.health_check("full")
    assert report.ok is True
    assert report.shards[0].tile_count == 3
    assert report.orphan_contents == 0

    after = {path.name: path.stat().st_size for path in repo.root.rglob("*") if path.is_file()}
    assert after == before, "健康检查写盘了 —— 它是只读诊断，不是维护动作"


def test_empty_repo_is_healthy(repo: TileRepository) -> None:
    report = repo.health_check()
    assert report.ok is True
    assert report.issues == ()
    assert report.shards == ()


# --------------------------------------------------------------------------- #
# 真的坏了 → error
# --------------------------------------------------------------------------- #
def test_dangling_reference_is_an_error(repo: TileRepository) -> None:
    repo.put_tiles(ASSET, [(5, 1, 1, png_bytes())])

    path = repo.shard_path(ASSET, BAND)
    connection = shard_module.open_shard(path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")  # 模拟"外部工具改坏了分片"
        _tamper(connection, "DELETE FROM contents")
    finally:
        connection.close()

    report = repo.health_check()
    assert report.ok is False
    assert report.dangling_refs == 1
    assert [item.code for item in report.errors] == ["dangling_ref"]
    # 悬空引用在读取路径上也不能装作没事
    assert repo.read_tile(ASSET, 5, 1, 1).status is LookupStatus.CORRUPT


def test_digest_mismatch_is_detected_by_full_check(repo: TileRepository) -> None:
    repo.put_tiles(ASSET, [(5, 1, 1, png_bytes((1, 1, 1)))])

    path = repo.shard_path(ASSET, BAND)
    connection = shard_module.open_shard(path)
    try:
        _tamper(connection, "UPDATE contents SET data = ?", (b"\x89PNG\r\n\x1a\n" + b"0" * 40,))
    finally:
        connection.close()

    assert repo.health_check("quick").ok is True, "quick 不复算摘要，这是刻意的性能取舍"
    full = repo.health_check("full")
    assert full.ok is False
    assert [item.code for item in full.errors] == ["digest_mismatch"]


def test_missing_shard_file_is_an_error(repo: TileRepository) -> None:
    repo.put_tiles(ASSET, [(5, 1, 1, png_bytes())])
    path = repo.shard_path(ASSET, BAND)
    root = repo.root
    # 外部删分片必须先关仓库：仓库开着时常驻连接持着文件句柄，Windows 会直接
    # 拒绝删除（WinError 32）。这既是测试约束，也是产品行为：运行期间分片不会被
    # 别的进程悄悄拽走。
    repo.close()
    path.unlink()

    with TileRepository.open(root, create=False) as reopened:
        report = reopened.health_check()
        assert report.ok is False
        assert [item.code for item in report.errors] == ["missing_shard"]
        # 登记指向不存在的文件时读取不能崩，只能如实报"没有"
        assert reopened.read_tile(ASSET, 5, 1, 1).status is LookupStatus.ABSENT


def test_open_repository_prevents_external_shard_deletion(repo: TileRepository) -> None:
    """仓库开着时，外部删除分片会被操作系统拒绝（句柄还在）。

    这条不是"顺手测一下"：它保证了 `health_check` 看到的"文件不见了"只可能是
    **上一次运行**留下的状态，而不是"另一个进程刚刚在我脚下删了文件"。
    """
    repo.put_tiles(ASSET, [(5, 1, 1, png_bytes())])
    path = repo.shard_path(ASSET, BAND)
    with pytest.raises(OSError):
        path.unlink()
    assert path.is_file()
    assert repo.health_check().ok is True


def test_unknown_shard_schema_is_reported_but_not_rebuilt(repo: TileRepository) -> None:
    """不认识的 schema 版本必须**报错并停下**，绝不能"顺手重建"。

    重建会把一个未来版本（或别人写的）的库当垃圾清掉 —— 那是不可恢复的数据损失。
    """
    repo.put_tiles(ASSET, [(5, 1, 1, png_bytes())])
    path = repo.shard_path(ASSET, BAND)
    connection = shard_module.open_shard(path)
    try:
        _tamper(connection, "UPDATE shard_meta SET value = '99' WHERE name = 'schema_version'")
    finally:
        connection.close()

    report = repo.health_check()
    assert report.ok is False
    assert "schema_unsupported" in {item.code for item in report.errors}

    with pytest.raises(SchemaUnsupported):
        repo.put_tiles(ASSET, [(5, 1, 2, png_bytes((2, 2, 2)))])

    assert path.is_file(), "报错不等于删文件"
    connection = shard_module.open_shard(path)
    try:
        assert int(connection.execute("SELECT COUNT(*) FROM coords").fetchone()[0]) == 1
    finally:
        connection.close()


# --------------------------------------------------------------------------- #
# 可以修 → warning + rebuild
# --------------------------------------------------------------------------- #
def test_count_drift_is_a_warning_and_rebuild_fixes_it(repo: TileRepository) -> None:
    repo.put_tiles(ASSET, [(5, 1, 1, png_bytes()), (5, 1, 2, png_bytes((3, 3, 3)))])
    with repo.catalog.transaction() as connection:
        connection.execute(
            "UPDATE shards SET tile_count = 999 WHERE asset_id = ?", (ASSET,)
        )

    report = repo.health_check()
    assert report.ok is True, "计数漂移不该让健康检查变成失败"
    assert {item.code for item in report.warnings} == {"count_mismatch"}

    fixed = repo.rebuild_index()
    assert fixed.fixed_shards == 1
    assert repo.health_check().ok is True
    assert not [item for item in repo.health_check().issues if item.code == "count_mismatch"]

    again = repo.rebuild_index()
    assert again.fixed_shards == 0, "重建必须幂等：第二遍不该再'修'一次"


def test_rebuild_is_idempotent_and_keeps_data(repo: TileRepository) -> None:
    data = png_bytes((9, 8, 7))
    repo.put_tiles(ASSET, [(5, 3, 3, data)])
    before = list(repo.iter_tiles(ASSET))

    first = repo.rebuild_index()
    second = repo.rebuild_index()
    assert first.registered_shards == 0  # 登记本来就在，只是被重算了一遍
    assert second.fixed_shards == 0
    assert [record.digest for record in repo.iter_tiles(ASSET)] == [r.digest for r in before]
    assert repo.read_tile(ASSET, 5, 3, 3).record.data == data


def test_catalog_can_be_rebuilt_from_shards(repo: TileRepository) -> None:
    """catalog 整个丢了也能救：``shard_meta`` 里有资产身份、带号、尺寸。"""
    data = png_bytes((4, 5, 6))
    repo.put_tiles(ASSET, [(5, 2, 2, data)])
    meta = repo.freeze_snapshot(assets=[ASSET])
    root = repo.root
    repo.close()

    for item in root.glob("catalog.sqlite*"):
        item.unlink()

    with TileRepository.open(root, create=True) as rebuilt:
        assert rebuilt.created is True
        assert rebuilt.catalog.list_assets() == []
        with pytest.raises(NotFound):
            rebuilt.read_tile(ASSET, 5, 2, 2)

        report = rebuilt.rebuild_index()
        assert report.registered_shards == 1
        assert report.registered_snapshots == 1
        assert any("epoch" in note for note in report.notes), report.notes

        lookup = rebuilt.read_tile(ASSET, 5, 2, 2)
        assert lookup.status is LookupStatus.PRESENT
        assert lookup.record.data == data
        with rebuilt.open_snapshot(meta.fingerprint) as view:
            assert [r.data for r in view] == [data]
        assert rebuilt.health_check().ok is True


# --------------------------------------------------------------------------- #
# 清理：只报告
# --------------------------------------------------------------------------- #
def test_prune_reports_orphans_without_touching_bytes(repo: TileRepository) -> None:
    repo.put_tiles(ASSET, [(5, 1, 1, png_bytes())])
    orphan = png_bytes((123, 45, 67))
    path = repo.shard_path(ASSET, BAND)
    connection = shard_module.open_shard(path)
    try:
        _tamper(
            connection,
            "INSERT INTO contents (sha256, size, encoding, first_seen, data) VALUES (?, ?, ?, ?, ?)",
            (
                __import__("hashlib").sha256(orphan).digest(),
                len(orphan),
                "png",
                0.0,
                orphan,
            ),
        )
    finally:
        connection.close()

    before = _shard_size(repo)
    report = repo.prune()
    assert report.dry_run is True
    assert len(report.orphan_contents) == 1
    assert report.reclaimable_bytes == len(orphan)
    assert report.notes, "只报告不删除这件事必须写在报告里"
    assert _shard_size(repo) == before, "prune 不许删任何东西"

    with pytest.raises(UnsupportedOperation):
        repo.prune(dry_run=False)


# --------------------------------------------------------------------------- #
# 恢复
# --------------------------------------------------------------------------- #
def test_recover_on_clean_repo_is_a_noop(repo: TileRepository) -> None:
    repo.put_tiles(ASSET, [(5, 1, 1, png_bytes())])
    report = repo.recover()
    assert report.changed == 0
    assert report.as_lines()


def test_recover_ignores_operations_without_rows(repo: TileRepository) -> None:
    """分片里没有那一代的行 = 那批数据根本没落盘 → 标 aborted，不许补出假数据。"""
    repo.put_tiles(ASSET, [(5, 1, 1, png_bytes())])
    ghost = repo.catalog.create_epoch("put", ASSET)
    op_id = repo.catalog.begin_operation("put", ASSET, ghost, {})

    report = repo.recover()
    assert report.aborted == (op_id,)
    assert repo.catalog.get_operation(op_id)["state"] == "aborted"
    assert repo.read_tile(ASSET, 5, 1, 1).status is LookupStatus.PRESENT
    assert repo.recover().changed == 0


def test_recover_keeps_unpublished_rows(repo: TileRepository) -> None:
    """有行、没发布 → 补发布，而不是把行删掉。

    删行等于"静默丢掉已经写进磁盘的写入"，正是契约禁止的那件事。
    """
    repo.put_tiles(ASSET, [(5, 1, 1, png_bytes())])

    path = repo.shard_path(ASSET, BAND)
    connection = shard_module.open_shard(path)
    try:
        _tamper(connection, "UPDATE coords SET epoch_id = 77")
    finally:
        connection.close()
    with repo.catalog.transaction() as catalog_connection:
        catalog_connection.execute(
            "UPDATE shards SET published_epoch = 1 WHERE asset_id = ?", (ASSET,)
        )
    op_id = repo.catalog.begin_operation("put", ASSET, 77, {})

    assert repo.read_tile(ASSET, 5, 1, 1).status is LookupStatus.UNPUBLISHED
    report = repo.recover()
    assert report.republished == (op_id,)
    assert repo.read_tile(ASSET, 5, 1, 1).status is LookupStatus.PRESENT
    assert repo.read_tile(ASSET, 5, 1, 1).record.epoch == 77
    assert repo.recover().changed == 0


def test_recover_when_shard_is_actually_gone(repo: TileRepository) -> None:
    """登记在、文件没了：恢复不能崩，健康检查要报出来。"""
    repo.put_tiles(ASSET, [(5, 1, 1, png_bytes())])
    path = repo.shard_path(ASSET, BAND)
    root = repo.root
    repo.close()
    path.unlink()
    with TileRepository.open(root, create=False) as reopened:
        assert reopened.recover().changed == 0
        assert shard_module.shard_paths_under(root) == []
        assert reopened.health_check().ok is False


def test_help_functions_are_reachable_from_module(repo: TileRepository) -> None:
    """模块级函数与仓库方法必须是同一套语义（别维护两份实现）。"""
    repo.put_tiles(ASSET, [(5, 1, 1, png_bytes())])
    assert health_check(repo).ok is True
    assert rebuild_index(repo).fixed_shards == 0
    assert prune(repo).dry_run is True
    assert recover(repo).changed == 0


def test_missing_catalog_directory_is_not_silently_recreated(work_dir: Path) -> None:
    """``create=False`` 打开一个空目录必须报错，不许"顺手建一个新的"。"""
    empty = work_dir / "nothing-here"
    empty.mkdir()
    with pytest.raises(NotFound):
        TileRepository.open(empty, create=False)


def test_copy_of_repo_still_opens(repo: TileRepository, work_dir: Path) -> None:
    """整仓拷走（换路径）后仍然可用：分片里没有任何绝对路径。"""
    repo.put_tiles(ASSET, [(5, 1, 1, png_bytes())])
    repo.close()
    clone = work_dir / "clone"
    shutil.copytree(repo.root, clone)

    with TileRepository.open(clone, create=False) as copied:
        assert copied.read_tile(ASSET, 5, 1, 1).status is LookupStatus.PRESENT
        assert copied.health_check().ok is True
        assert copied.rebuild_index().fixed_shards == 0


def test_corrupt_shard_file_is_reported_not_hidden(repo: TileRepository) -> None:
    repo.put_tiles(ASSET, [(5, 1, 1, png_bytes())])
    path = repo.shard_path(ASSET, BAND)
    root = repo.root
    repo.close()
    path.write_bytes(b"not a sqlite file")

    with TileRepository.open(root, create=False) as reopened:
        report = reopened.health_check()
        assert report.ok is False
        assert {item.code for item in report.errors} == {"shard_corrupt"}
        with pytest.raises(Corrupt):
            reopened.read_tile(ASSET, 5, 1, 1)
