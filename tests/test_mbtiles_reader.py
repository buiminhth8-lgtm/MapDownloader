"""MBTiles 读取器单元测试（Slice 4）。

全部用真实 SQLite 文件（临时目录），不触网。
重点验证**对外 XYZ / 内部 TMS** 的透明转换，以及只读连接不会产生副作用。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.geo import xyz_y_to_tms_y
from app.mbtiles import MBTilesMetadata, MBTilesReader, MBTilesWriter
from app.providers import PNG_SIGNATURE

FAKE_PNG = PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + b"tile-payload" * 8


def _build(db: Path, tiles: list[tuple[int, int, int]]) -> Path:
    """用 Writer 造一个库；``tiles`` 为 ``(z, x, y_xyz)`` 列表。"""
    with MBTilesWriter(db) as writer:
        writer.write_metadata(
            MBTilesMetadata(
                name="beijing",
                description="读取器测试",
                min_zoom=3,
                max_zoom=12,
                west=116.0,
                south=39.0,
                east=116.5,
                north=39.5,
            )
        )
        for z, x, y in tiles:
            writer.write_tile(z, x, y, FAKE_PNG)
    return db


# --------------------------------------------------------------------------- #
# 元数据
# --------------------------------------------------------------------------- #
def test_get_metadata(work_dir: Path) -> None:
    db = _build(work_dir / "map.mbtiles", [(3, 4, 2)])

    with MBTilesReader(db) as reader:
        metadata = reader.get_metadata()

    assert metadata["name"] == "beijing"
    assert metadata["format"] == "png"
    assert metadata["bounds"] == "116.000000,39.000000,116.500000,39.500000"
    assert metadata["minzoom"] == "3"
    assert metadata["maxzoom"] == "12"
    assert set(metadata) == {
        "name",
        "type",
        "version",
        "description",
        "format",
        "bounds",
        "minzoom",
        "maxzoom",
    }


def test_get_metadata_returns_empty_dict_when_table_empty(work_dir: Path) -> None:
    db = work_dir / "empty-meta.mbtiles"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    conn.commit()
    conn.close()

    with MBTilesReader(db) as reader:
        assert reader.get_metadata() == {}
        assert reader.get_tile_count() == 0
        assert reader.get_zoom_range() is None


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
def test_tile_count(work_dir: Path) -> None:
    db = _build(work_dir / "map.mbtiles", [(3, i, 0) for i in range(5)])

    with MBTilesReader(db) as reader:
        assert reader.get_tile_count() == 5


def test_zoom_range(work_dir: Path) -> None:
    db = _build(work_dir / "map.mbtiles", [(3, 0, 0), (12, 3367, 1563), (8, 1, 1)])

    with MBTilesReader(db) as reader:
        assert reader.get_zoom_range() == (3, 12)


def test_zoom_range_is_none_without_tiles(work_dir: Path) -> None:
    db = work_dir / "no-tiles.mbtiles"
    with MBTilesWriter(db) as writer:
        writer.write_metadata(
            MBTilesMetadata(
                name="empty",
                description="",
                min_zoom=8,
                max_zoom=12,
                west=116.0,
                south=39.0,
                east=116.5,
                north=39.5,
            )
        )

    with MBTilesReader(db) as reader:
        assert reader.get_tile_count() == 0
        assert reader.get_zoom_range() is None


# --------------------------------------------------------------------------- #
# 单瓦片读取
# --------------------------------------------------------------------------- #
def test_read_tile_roundtrip(work_dir: Path) -> None:
    db = _build(work_dir / "map.mbtiles", [(12, 3367, 1563)])

    with MBTilesReader(db) as reader:
        assert reader.has_tile(12, 3367, 1563) is True
        assert reader.read_tile(12, 3367, 1563) == FAKE_PNG


def test_read_tile_returns_none_for_missing(work_dir: Path) -> None:
    db = _build(work_dir / "map.mbtiles", [(12, 3367, 1563)])

    with MBTilesReader(db) as reader:
        assert reader.has_tile(12, 3367, 1564) is False
        assert reader.read_tile(12, 3367, 1564) is None


def test_xyz_tms_transparency(work_dir: Path) -> None:
    """对外 XYZ、内部 TMS：写 (3,4,2) 后必须能用 (3,4,2) 读回来。

    同时确认库里存的确实是 TMS 的 5，而"把 TMS 值当 XYZ 用"读不到东西 ——
    这两条一起才说明转换真的发生了，而不是"恰好两边都写对了"。
    """
    db = _build(work_dir / "map.mbtiles", [(3, 4, 2)])

    conn = sqlite3.connect(str(db))
    raw = conn.execute(
        "SELECT zoom_level, tile_column, tile_row FROM tiles"
    ).fetchall()
    conn.close()
    assert raw == [(3, 4, 5)]
    assert raw[0][2] == xyz_y_to_tms_y(3, 2)

    with MBTilesReader(db) as reader:
        assert reader.has_tile(3, 4, 2) is True            # XYZ 命中
        assert reader.read_tile(3, 4, 2) == FAKE_PNG
        assert reader.has_tile(3, 4, 5) is False           # TMS 值当 XYZ 用 -> 未命中
        assert reader.read_tile(3, 4, 5) is None


def test_read_tile_by_manually_inserted_tms_row(work_dir: Path) -> None:
    """手工按 TMS 语义插入一行，Reader 依然要用 XYZ 语义读出来。"""
    db = work_dir / "manual.mbtiles"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
        "tile_row INTEGER, tile_data BLOB)"
    )
    # z=3 一共 8 行；tile_row=5 对应 XYZ 的 y = 8-1-5 = 2
    conn.execute("INSERT INTO tiles VALUES (3, 4, 5, ?)", (FAKE_PNG,))
    conn.commit()
    conn.close()

    with MBTilesReader(db) as reader:
        assert reader.read_tile(3, 4, 2) == FAKE_PNG
        assert reader.read_tile(3, 4, 5) is None


# --------------------------------------------------------------------------- #
# 校验辅助
# --------------------------------------------------------------------------- #
def test_list_tables(work_dir: Path) -> None:
    db = _build(work_dir / "map.mbtiles", [(3, 4, 2)])

    with MBTilesReader(db) as reader:
        assert {"metadata", "tiles"} <= reader.list_tables()


def test_sample_tiles_returns_xyz_and_is_deterministic(work_dir: Path) -> None:
    coordinates = [(3, 0, 0), (3, 1, 2), (3, 4, 2), (12, 3367, 1563)]
    db = _build(work_dir / "map.mbtiles", coordinates)

    with MBTilesReader(db) as reader:
        first = reader.sample_tiles(10)
        second = reader.sample_tiles(10)

    assert first == second                                   # 顺序可复现
    assert [item[:3] for item in first] == sorted(coordinates)
    assert all(item[3] == FAKE_PNG for item in first)


def test_sample_tiles_respects_limit(work_dir: Path) -> None:
    db = _build(work_dir / "map.mbtiles", [(3, i, 0) for i in range(6)])

    with MBTilesReader(db) as reader:
        assert len(reader.sample_tiles(3)) == 3


def test_sample_tiles_rejects_bad_limit(work_dir: Path) -> None:
    db = _build(work_dir / "map.mbtiles", [(3, 4, 2)])

    with MBTilesReader(db) as reader:
        with pytest.raises(ValueError):
            reader.sample_tiles(0)
        with pytest.raises(ValueError):
            reader.sample_tiles("x")  # type: ignore[arg-type]


def test_sample_tiles_rejects_corrupt_row(work_dir: Path) -> None:
    """库里出现越界的 tile_row 时必须报错，而不是悄悄返回错坐标。"""
    db = work_dir / "corrupt.mbtiles"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
        "tile_row INTEGER, tile_data BLOB)"
    )
    conn.execute("INSERT INTO tiles VALUES (3, 0, 999, ?)", (FAKE_PNG,))
    conn.commit()
    conn.close()

    with MBTilesReader(db) as reader:
        with pytest.raises(ValueError):
            reader.sample_tiles(5)


# --------------------------------------------------------------------------- #
# 参数与生命周期
# --------------------------------------------------------------------------- #
def test_missing_file_raises_and_is_not_created(work_dir: Path) -> None:
    """只读打开不会"顺手建一个空库" —— 这正是用 mode=ro 的目的。"""
    missing = work_dir / "nope.mbtiles"

    with pytest.raises(FileNotFoundError):
        MBTilesReader(missing).open()

    assert not missing.exists()


def test_invalid_sqlite_file_raises(work_dir: Path) -> None:
    bogus = work_dir / "bogus.mbtiles"
    bogus.write_bytes(b"this is definitely not a sqlite database" * 4)

    with pytest.raises(sqlite3.DatabaseError):
        MBTilesReader(bogus).open()


def test_operations_before_open_raise(work_dir: Path) -> None:
    db = _build(work_dir / "map.mbtiles", [(3, 4, 2)])
    reader = MBTilesReader(db)

    with pytest.raises(RuntimeError):
        reader.get_metadata()
    with pytest.raises(RuntimeError):
        reader.get_tile_count()
    with pytest.raises(RuntimeError):
        reader.read_tile(3, 4, 2)


@pytest.mark.parametrize(
    "z, x, y",
    [(3, 8, 0), (3, 0, 8), (-1, 0, 0), (23, 0, 0), (3, -1, 0)],
)
def test_illegal_coordinates_are_rejected(work_dir: Path, z, x, y) -> None:
    db = _build(work_dir / "map.mbtiles", [(3, 4, 2)])

    with MBTilesReader(db) as reader:
        with pytest.raises(ValueError):
            reader.read_tile(z, x, y)
        with pytest.raises(ValueError):
            reader.has_tile(z, x, y)


def test_reader_does_not_create_wal_sidecars(work_dir: Path) -> None:
    """读一个正常交付的库不该在旁边留下 -wal / -shm（依赖 Writer 收尾时退出 WAL）。"""
    db = _build(work_dir / "map.mbtiles", [(3, 4, 2)])
    before = {p.name for p in work_dir.iterdir()}

    with MBTilesReader(db) as reader:
        reader.get_metadata()
        reader.get_tile_count()
        reader.read_tile(3, 4, 2)

    assert {p.name for p in work_dir.iterdir()} == before


def test_close_is_idempotent(work_dir: Path) -> None:
    db = _build(work_dir / "map.mbtiles", [(3, 4, 2)])
    reader = MBTilesReader(db)
    reader.open()
    reader.close()
    reader.close()
    assert not reader.is_open
