"""MBTiles 写入器单元测试（Slice 4）。

**全部在临时目录里建真实 SQLite 文件，不触网、不需要 mock。**

覆盖点（对齐 Spec §15）：
建库 / metadata 写入 / 单瓦片 / XYZ→TMS / 批量事务 / flush /
重复瓦片 / 非法瓦片拒绝 / 空数据拒绝 / close 后数据库有效。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.config.settings import DEFAULT_MBTILES_BATCH_SIZE
from app.geo import xyz_y_to_tms_y
from app.mbtiles import MBTilesMetadata, MBTilesWriter
from app.providers import PNG_SIGNATURE

#: 够用的假 PNG：只要 magic bytes 对，validator / detect_image_format 就认
FAKE_PNG = PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + b"tile-payload" * 8


def _metadata(**overrides) -> MBTilesMetadata:
    """构造一份合法的元数据，只在需要时覆盖个别字段。"""
    values = {
        "name": "beijing",
        "description": "测试用",
        "min_zoom": 8,
        "max_zoom": 12,
        "west": 116.0,
        "south": 39.0,
        "east": 116.5,
        "north": 39.5,
    }
    values.update(overrides)
    return MBTilesMetadata(**values)


def _rows(db_path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    """用一条**独立的临时连接**读库（用于验证 writer 是否真的落盘）。"""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _tile_row_of(db_path: Path, z: int, x: int) -> int:
    rows = _rows(
        db_path,
        "SELECT tile_row FROM tiles WHERE zoom_level = ? AND tile_column = ?",
        (z, x),
    )
    assert len(rows) == 1
    return int(rows[0][0])


# --------------------------------------------------------------------------- #
# 建库与表结构
# --------------------------------------------------------------------------- #
def test_open_creates_file_and_standard_tables(work_dir: Path) -> None:
    db = work_dir / "sub" / "map.mbtiles"   # 父目录不存在，应自动创建

    with MBTilesWriter(db) as writer:
        assert writer.is_open

    assert db.is_file()
    assert db.parent.is_dir()
    tables = {
        row[0]
        for row in _rows(db, "SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"metadata", "tiles"} <= tables


def test_tiles_table_matches_mbtiles_spec(work_dir: Path) -> None:
    db = work_dir / "map.mbtiles"
    with MBTilesWriter(db):
        pass

    columns = _rows(db, "PRAGMA table_info(tiles)")
    by_name = {row[1]: row for row in columns}
    assert set(by_name) == {"zoom_level", "tile_column", "tile_row", "tile_data"}
    # NOT NULL 标记在 PRAGMA 的第 3 列（notnull），主键在第 5 列（pk）
    assert all(by_name[name][3] == 1 for name in by_name)
    assert [name for name, row in by_name.items() if row[5]] == [
        "zoom_level",
        "tile_column",
        "tile_row",
    ]

    meta_columns = _rows(db, "PRAGMA table_info(metadata)")
    assert {row[1] for row in meta_columns} == {"name", "value"}


def test_no_redundant_index_is_created(work_dir: Path) -> None:
    """主键索引已经能服务 zoom_level 前缀查询，不该再多建一个。"""
    db = work_dir / "map.mbtiles"
    with MBTilesWriter(db):
        pass

    indexes = _rows(db, "PRAGMA index_list(tiles)")
    # 只应有主键带来的那个自动索引，且必须是 UNIQUE
    assert len(indexes) == 1
    assert indexes[0][2] == 1  # unique


def test_open_is_idempotent(work_dir: Path) -> None:
    writer = MBTilesWriter(work_dir / "map.mbtiles")
    writer.open()
    writer.open()
    assert writer.is_open
    writer.close()


def test_reuse_after_close_is_rejected(work_dir: Path) -> None:
    """close() 之后不能复用同一个实例（否则很容易写出"连接已死的"诡异 bug）。"""
    writer = MBTilesWriter(work_dir / "map.mbtiles")
    writer.open()
    writer.close()
    assert not writer.is_open

    with pytest.raises(RuntimeError):
        writer.open()


def test_batch_size_must_be_positive(work_dir: Path) -> None:
    with pytest.raises(ValueError):
        MBTilesWriter(work_dir / "map.mbtiles", batch_size=0)
    with pytest.raises(ValueError):
        MBTilesWriter(work_dir / "map.mbtiles", batch_size="x")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# metadata
# --------------------------------------------------------------------------- #
def test_write_metadata_writes_eight_standard_keys(work_dir: Path) -> None:
    db = work_dir / "map.mbtiles"
    with MBTilesWriter(db) as writer:
        writer.write_metadata(_metadata())

    metadata = dict(_rows(db, "SELECT name, value FROM metadata"))
    assert metadata == {
        "name": "beijing",
        "type": "baselayer",
        "version": "1.0",
        "description": "测试用",
        "format": "png",
        "bounds": "116.000000,39.000000,116.500000,39.500000",
        "minzoom": "8",
        "maxzoom": "12",
    }


def test_write_metadata_is_idempotent(work_dir: Path) -> None:
    db = work_dir / "map.mbtiles"
    with MBTilesWriter(db) as writer:
        writer.write_metadata(_metadata())
        writer.write_metadata(_metadata(description="改过了"))

    rows = _rows(db, "SELECT name, value FROM metadata")
    assert len(rows) == 8
    assert dict(rows)["description"] == "改过了"


def test_metadata_values_are_parameterized(work_dir: Path) -> None:
    """带引号的名字不能破坏 SQL（参数化写入的回归测试）。"""
    db = work_dir / "map.mbtiles"
    tricky = "a'; DROP TABLE metadata; --"
    with MBTilesWriter(db) as writer:
        writer.write_metadata(_metadata(name=tricky, description="it's fine"))

    metadata = dict(_rows(db, "SELECT name, value FROM metadata"))
    assert metadata["name"] == tricky
    assert metadata["description"] == "it's fine"
    assert "metadata" in {
        row[0] for row in _rows(db, "SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_write_metadata_requires_metadata_instance(work_dir: Path) -> None:
    with MBTilesWriter(work_dir / "map.mbtiles") as writer:
        with pytest.raises(TypeError):
            writer.write_metadata({"name": "x"})  # type: ignore[arg-type]


def test_write_tile_before_metadata_is_rejected(work_dir: Path) -> None:
    """没有 metadata 的 MBTiles 是无效文件，宁可在写入端就拒绝。"""
    with MBTilesWriter(work_dir / "map.mbtiles") as writer:
        with pytest.raises(RuntimeError, match="write_metadata"):
            writer.write_tile(3, 4, 2, FAKE_PNG)


# --------------------------------------------------------------------------- #
# 瓦片写入与坐标换算
# --------------------------------------------------------------------------- #
def test_write_single_tile(work_dir: Path) -> None:
    db = work_dir / "map.mbtiles"
    with MBTilesWriter(db) as writer:
        writer.write_metadata(_metadata())
        writer.write_tile(12, 3367, 1563, FAKE_PNG)

    rows = _rows(db, "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles")
    assert len(rows) == 1
    zoom, column, row, data = rows[0]
    assert (zoom, column) == (12, 3367)
    assert row == xyz_y_to_tms_y(12, 1563)
    assert bytes(data) == FAKE_PNG


def test_xyz_to_tms_known_value(work_dir: Path) -> None:
    """Spec §16 的**关键测试**：z=3, x=4, y_xyz=2 → tile_row = 2**3-1-2 = 5。

    这是"未来 osmdroid 能否正常显示"的分水岭：写错方向的话，地图会上下颠倒，
    而且只有在真机上才看得出来。
    """
    db = work_dir / "map.mbtiles"
    with MBTilesWriter(db) as writer:
        writer.write_metadata(_metadata(min_zoom=3, max_zoom=3))
        writer.write_tile(3, 4, 2, FAKE_PNG)

    assert _tile_row_of(db, 3, 4) == 5
    rows = _rows(db, "SELECT zoom_level, tile_column, tile_row FROM tiles")
    assert rows == [(3, 4, 5)]


@pytest.mark.parametrize(
    "z, y_xyz, expected_row",
    [
        (0, 0, 0),
        (1, 0, 1),
        (1, 1, 0),
        (3, 0, 7),
        (3, 7, 0),
        (12, 1563, 4095 - 1563),
    ],
)
def test_xyz_to_tms_across_zooms(
    work_dir: Path, z: int, y_xyz: int, expected_row: int
) -> None:
    db = work_dir / f"map-{z}-{y_xyz}.mbtiles"
    with MBTilesWriter(db) as writer:
        writer.write_metadata(_metadata(min_zoom=z, max_zoom=z))
        writer.write_tile(z, 0, y_xyz, FAKE_PNG)

    assert _tile_row_of(db, z, 0) == expected_row


@pytest.mark.parametrize(
    "z, x, y",
    [
        (-1, 0, 0),            # zoom 下溢
        (23, 0, 0),            # zoom 上溢
        (3, 8, 0),             # x 超出 2**3
        (3, -1, 0),            # x 为负
        (3, 0, 8),             # y 超出 2**3
        (3, 0, -1),            # y 为负
        (3, 1.5, 0),           # 非整数
        ("3", 0, 0),           # 字符串
    ],
)
def test_illegal_tile_coordinates_are_rejected(
    work_dir: Path, z, x, y
) -> None:
    db = work_dir / "map.mbtiles"
    with MBTilesWriter(db) as writer:
        writer.write_metadata(_metadata())
        with pytest.raises(ValueError):
            writer.write_tile(z, x, y, FAKE_PNG)


def test_empty_tile_data_is_rejected(work_dir: Path) -> None:
    with MBTilesWriter(work_dir / "map.mbtiles") as writer:
        writer.write_metadata(_metadata())
        with pytest.raises(ValueError, match="空瓦片"):
            writer.write_tile(3, 4, 2, b"")


def test_non_bytes_tile_data_is_rejected(work_dir: Path) -> None:
    with MBTilesWriter(work_dir / "map.mbtiles") as writer:
        writer.write_metadata(_metadata())
        with pytest.raises(TypeError):
            writer.write_tile(3, 4, 2, "not bytes")  # type: ignore[arg-type]


def test_bytearray_and_memoryview_are_accepted(work_dir: Path) -> None:
    db = work_dir / "map.mbtiles"
    with MBTilesWriter(db) as writer:
        writer.write_metadata(_metadata())
        writer.write_tile(3, 4, 2, bytearray(FAKE_PNG))
        writer.write_tile(3, 5, 2, memoryview(FAKE_PNG))

    rows = _rows(db, "SELECT tile_column, tile_data FROM tiles ORDER BY tile_column")
    assert [bytes(row[1]) for row in rows] == [FAKE_PNG, FAKE_PNG]


# --------------------------------------------------------------------------- #
# 批量事务
# --------------------------------------------------------------------------- #
def test_batch_commits_only_at_batch_size(work_dir: Path) -> None:
    db = work_dir / "map.mbtiles"
    batch = 4
    with MBTilesWriter(db, batch_size=batch) as writer:
        writer.write_metadata(_metadata())

        for index in range(batch - 1):
            writer.write_tile(3, index, 0, FAKE_PNG)
        # 还没到批量阈值 -> 库里应该还是空的（没提交过）
        assert writer.pending_tiles == batch - 1
        assert writer.batches_committed == 0
        assert _rows(db, "SELECT count(*) FROM tiles")[0][0] == 0

        writer.write_tile(3, batch - 1, 0, FAKE_PNG)   # 触发提交
        assert writer.pending_tiles == 0
        assert writer.batches_committed == 1
        assert _rows(db, "SELECT count(*) FROM tiles")[0][0] == batch

    assert writer.tiles_written == batch


def test_flush_writes_partial_batch(work_dir: Path) -> None:
    db = work_dir / "map.mbtiles"
    with MBTilesWriter(db, batch_size=100) as writer:
        writer.write_metadata(_metadata())
        writer.write_tile(3, 0, 0, FAKE_PNG)
        writer.write_tile(3, 1, 0, FAKE_PNG)
        assert _rows(db, "SELECT count(*) FROM tiles")[0][0] == 0

        writer.flush()
        assert writer.pending_tiles == 0
        assert _rows(db, "SELECT count(*) FROM tiles")[0][0] == 2

        writer.flush()   # 空 flush 是空操作
        assert writer.batches_committed == 1


def test_close_flushes_pending_tiles(work_dir: Path) -> None:
    """close() 必须把剩余瓦片写完，否则丢的是用户已下载的数据。"""
    db = work_dir / "map.mbtiles"
    writer = MBTilesWriter(db, batch_size=100)
    writer.open()
    writer.write_metadata(_metadata())
    for index in range(7):
        writer.write_tile(3, index, 0, FAKE_PNG)
    writer.close()

    assert _rows(db, "SELECT count(*) FROM tiles")[0][0] == 7


def test_default_batch_size_comes_from_settings() -> None:
    assert DEFAULT_MBTILES_BATCH_SIZE == 200


# --------------------------------------------------------------------------- #
# 重复瓦片
# --------------------------------------------------------------------------- #
def test_duplicate_tile_overwrites(work_dir: Path) -> None:
    """同一 z/x/y 写两次：库里只有一行，内容是**后写**的那份。"""
    db = work_dir / "map.mbtiles"
    second = PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + b"NEWER" * 4

    with MBTilesWriter(db) as writer:
        writer.write_metadata(_metadata())
        writer.write_tile(3, 4, 2, FAKE_PNG)
        writer.write_tile(3, 4, 2, second)

    rows = _rows(db, "SELECT tile_data FROM tiles")
    assert len(rows) == 1
    assert bytes(rows[0][0]) == second


def test_duplicate_tile_across_batches(work_dir: Path) -> None:
    """跨批次重复也要覆盖（不能因为分批就产生两行）。"""
    db = work_dir / "map.mbtiles"
    with MBTilesWriter(db, batch_size=2) as writer:
        writer.write_metadata(_metadata())
        writer.write_tile(3, 4, 2, FAKE_PNG)
        writer.write_tile(3, 5, 2, FAKE_PNG)   # 触发提交
        writer.write_tile(3, 4, 2, FAKE_PNG)   # 与第一批重复

    assert _rows(db, "SELECT count(*) FROM tiles")[0][0] == 2


# --------------------------------------------------------------------------- #
# 生命周期与异常
# --------------------------------------------------------------------------- #
def test_closed_database_is_readable_and_self_contained(work_dir: Path) -> None:
    """close 之后：数据可读、连接已断、且**不留下 WAL 旁挂文件**。"""
    db = work_dir / "map.mbtiles"
    with MBTilesWriter(db) as writer:
        writer.write_metadata(_metadata())
        writer.write_tile(3, 4, 2, FAKE_PNG)

    # 单独一个连接能读到全部内容
    assert _rows(db, "SELECT count(*) FROM metadata")[0][0] == 8
    assert _rows(db, "SELECT count(*) FROM tiles")[0][0] == 1

    # 交付物必须是**单文件**：WAL 模式会让后续只读打开重新生成 -wal/-shm
    leftovers = sorted(p.name for p in work_dir.iterdir() if p.name != db.name)
    assert leftovers == []
    assert _rows(db, "PRAGMA journal_mode")[0][0] == "delete"


def test_no_connection_leak_when_flush_fails(work_dir: Path) -> None:
    """提交失败也必须把连接关掉（否则文件被锁住，用户删都删不掉）。"""
    db = work_dir / "map.mbtiles"
    writer = MBTilesWriter(db)
    writer.open()
    writer.write_metadata(_metadata())
    writer.write_tile(3, 4, 2, FAKE_PNG)

    # 把表删掉，制造一个必然失败的提交
    writer._conn.execute("DROP TABLE tiles")  # noqa: SLF001 - 故意破坏内部状态
    with pytest.raises(sqlite3.Error):
        writer.flush()

    assert writer.pending_tiles == 0      # 失败后缓冲已清空，不会被重复写
    writer.close()                        # 应该仍然能正常关闭
    assert not writer.is_open


def test_context_manager_aborts_on_exception(work_dir: Path) -> None:
    """with 块里抛异常 -> 回滚未提交瓦片 + 关闭连接。"""
    db = work_dir / "map.mbtiles"
    with pytest.raises(RuntimeError, match="boom"):
        with MBTilesWriter(db) as writer:
            writer.write_metadata(_metadata())
            for index in range(5):
                writer.write_tile(3, index, 0, FAKE_PNG)
            raise RuntimeError("boom")

    # 已提交的 metadata 保留（它单独成事务），未提交的瓦片被丢弃
    assert _rows(db, "SELECT count(*) FROM metadata")[0][0] == 8
    assert _rows(db, "SELECT count(*) FROM tiles")[0][0] == 0


def test_abort_is_idempotent_and_safe_before_open(work_dir: Path) -> None:
    writer = MBTilesWriter(work_dir / "map.mbtiles")
    writer.abort()            # 还没 open 也不该炸
    writer.abort()
    with pytest.raises(RuntimeError):
        writer.open()


def test_operations_after_close_raise(work_dir: Path) -> None:
    writer = MBTilesWriter(work_dir / "map.mbtiles")
    writer.open()
    writer.write_metadata(_metadata())
    writer.close()

    with pytest.raises(RuntimeError):
        writer.write_tile(3, 4, 2, FAKE_PNG)
    with pytest.raises(RuntimeError):
        writer.flush()
    with pytest.raises(RuntimeError):
        writer.write_metadata(_metadata())


def test_writing_to_existing_file_keeps_metadata(work_dir: Path) -> None:
    """已存在且已有 metadata 的库：可以直接续写瓦片（为 Slice 5 留的路）。"""
    db = work_dir / "map.mbtiles"
    with MBTilesWriter(db) as writer:
        writer.write_metadata(_metadata())
        writer.write_tile(3, 4, 2, FAKE_PNG)

    with MBTilesWriter(db) as writer:
        # 没有重新写 metadata，也应该允许写瓦片
        writer.write_tile(3, 5, 2, FAKE_PNG)

    assert _rows(db, "SELECT count(*) FROM tiles")[0][0] == 2
    assert dict(_rows(db, "SELECT name, value FROM metadata"))["name"] == "beijing"
