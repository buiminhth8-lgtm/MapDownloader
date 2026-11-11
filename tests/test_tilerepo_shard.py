"""分片路由、DDL、schema 校验与写租约（Slice 42）。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from app.tilerepo import AssetId, CoordKey, Corrupt, SchemaUnsupported
from app.tilerepo import shard as shard_module
from app.tilerepo.shard import (
    acquire_lease,
    band_for_zoom,
    band_name,
    check_shard_schema,
    open_shard,
    raise_if_escapes,
    read_lease,
    read_shard_meta,
    release_all_leases,
    shard_path,
    shard_relative_path,
    table_names,
)


# --------------------------------------------------------------------------- #
# zoom 带与路径
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("zoom", "expected"),
    [(0, (0, 6)), (6, (0, 6)), (7, (7, 11)), (11, (7, 11)), (12, (12, 14)),
     (14, (12, 14)), (15, (15, 18)), (18, (15, 18))],
)
def test_band_for_zoom_boundaries(zoom: int, expected: tuple[int, int]) -> None:
    """带边界是"闭区间"，边界值归入哪一个带必须写死一条用例钉住。"""
    assert band_for_zoom(zoom) == expected


@pytest.mark.parametrize("zoom", [-1, 19, 22])
def test_band_for_zoom_rejects_uncovered_zoom(zoom: int) -> None:
    with pytest.raises(ValueError):
        band_for_zoom(zoom)


def test_band_name_is_zero_padded() -> None:
    assert band_name((0, 6)) == "z00-06"
    assert band_name((15, 18)) == "z15-18"


def test_shard_relative_path_layout() -> None:
    relative = shard_relative_path(AssetId("tianditu", "vec_w"), (7, 11))
    assert relative.as_posix() == "shards/tianditu/EPSG_3857/256/vec_w/z07-11.sqlite"


def test_shard_relative_path_rejects_unsafe_segments() -> None:
    with pytest.raises(ValueError):
        shard_relative_path(AssetId("tianditu", "vec_w"), (0, 6), projection="EPSG 3857/../x")


def test_shard_path_stays_inside_root(work_dir: Path) -> None:
    root = work_dir / "repo"
    path = shard_path(root, AssetId("tianditu", "cva_w"), (12, 14))
    assert root.resolve() in path.resolve().parents


def test_path_escape_is_a_corruption_not_a_value_error(work_dir: Path) -> None:
    """catalog 里的路径是可以被手工改的 —— 越界要按"数据坏了"处理。"""
    with pytest.raises(Corrupt):
        raise_if_escapes(work_dir / "repo", work_dir / "repo" / ".." / "evil.sqlite")


# --------------------------------------------------------------------------- #
# 连接与 schema
# --------------------------------------------------------------------------- #
def test_open_shard_creates_schema_and_meta(work_dir: Path) -> None:
    path = work_dir / "shards" / "tianditu" / "vec_w" / "z00-06.sqlite"
    connection = open_shard(path, create=True)
    try:
        connection.executescript(shard_module.DDL)
        shard_module.write_shard_meta(
            connection,
            {
                "schema_version": shard_module.TILE_REPO_SCHEMA_VERSION,
                "asset_id": "tianditu/vec_w",
                "source_id": "tianditu",
                "layer": "vec_w",
                "projection": "EPSG:3857",
                "tile_size": 256,
                "z_min": 0,
                "z_max": 6,
            },
        )
    finally:
        connection.close()

    connection = open_shard(path, read_only=True)
    try:
        assert {"coords", "contents", "shard_meta"} <= set(table_names(connection))
        meta = read_shard_meta(connection)
        assert meta["asset_id"] == "tianditu/vec_w"
        assert check_shard_schema(
            connection, asset=AssetId("tianditu", "vec_w"), band=(0, 6)
        )["schema_version"] == str(shard_module.TILE_REPO_SCHEMA_VERSION)
    finally:
        connection.close()


def test_missing_meta_key_is_corrupt(work_dir: Path) -> None:
    path = work_dir / "a.sqlite"
    connection = open_shard(path, create=True)
    try:
        connection.executescript(shard_module.DDL)
        shard_module.write_shard_meta(connection, {"schema_version": "1", "asset_id": "tianditu/vec_w"})
        with pytest.raises(Corrupt):
            check_shard_schema(connection)
    finally:
        connection.close()


def test_unknown_schema_version_is_rejected_not_migrated(work_dir: Path) -> None:
    path = work_dir / "a.sqlite"
    connection = open_shard(path, create=True)
    try:
        connection.executescript(shard_module.DDL)
        shard_module.write_shard_meta(
            connection,
            {
                "schema_version": "99",
                "asset_id": "tianditu/vec_w",
                "source_id": "tianditu",
                "layer": "vec_w",
                "projection": "EPSG:3857",
                "tile_size": 256,
                "z_min": 0,
                "z_max": 6,
            },
        )
        with pytest.raises(SchemaUnsupported):
            check_shard_schema(connection)
    finally:
        connection.close()


def test_shard_self_description_must_match_its_path(work_dir: Path) -> None:
    path = work_dir / "a.sqlite"
    connection = open_shard(path, create=True)
    try:
        connection.executescript(shard_module.DDL)
        shard_module.write_shard_meta(
            connection,
            {
                "schema_version": "1",
                "asset_id": "tianditu/img_w",
                "source_id": "tianditu",
                "layer": "img_w",
                "projection": "EPSG:3857",
                "tile_size": 256,
                "z_min": 0,
                "z_max": 6,
            },
        )
        with pytest.raises(Corrupt):
            check_shard_schema(connection, asset=AssetId("tianditu", "vec_w"), band=(0, 6))
    finally:
        connection.close()


def test_read_only_connection_rejects_writes(work_dir: Path) -> None:
    """``query_only`` 的连接必须真的写不进去（健康检查靠这条保证"只读"）。"""
    path = work_dir / "a.sqlite"
    connection = open_shard(path, create=True)
    connection.executescript(shard_module.DDL)
    connection.close()

    connection = open_shard(path, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "INSERT INTO contents (sha256, size, encoding, first_seen, data) "
                "VALUES (?, 1, 'png', 0, ?)",
                (b"0" * 32, b"x"),
            )
    finally:
        connection.close()


def test_foreign_key_blocks_dangling_coords(work_dir: Path) -> None:
    path = work_dir / "a.sqlite"
    connection = open_shard(path, create=True)
    try:
        connection.executescript(shard_module.DDL)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO coords (z, x, y, sha256, state, fetched_at, epoch_id) "
                "VALUES (5, 1, 2, ?, 0, 0, 1)",
                (b"1" * 32,),
            )
    finally:
        connection.close()


def test_point_query_uses_the_primary_key(work_dir: Path) -> None:
    """主要查询必须吃主键索引，不能退化成全表扫描（"用查询计划当证据"）。"""
    path = work_dir / "a.sqlite"
    connection = open_shard(path, create=True)
    try:
        connection.executescript(shard_module.DDL)
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT sha256 FROM coords "
            "WHERE z = 5 AND x = 1 AND y = 2 AND state = 0"
        ).fetchall()
        detail = " ".join(str(row["detail"]) for row in plan)
        assert "PRIMARY KEY" in detail.upper()
        assert "SCAN" not in detail.upper()
    finally:
        connection.close()


# --------------------------------------------------------------------------- #
# 写租约
# --------------------------------------------------------------------------- #
def test_lease_acquire_release(work_dir: Path) -> None:
    lock_dir = work_dir / "lock"
    lease = acquire_lease(lock_dir, "tianditu-vec_w-z00-06", label="测试")
    try:
        assert lease.path.is_file()
        info = read_lease(lease.path)
        assert info is not None and info.pid == os.getpid()
    finally:
        lease.release()
    assert not lease.path.exists()


def test_lease_is_reentrant_within_a_process(work_dir: Path) -> None:
    """同进程二次申请复用同一把锁（外层导入 + 内层逐批写入）。"""
    lock_dir = work_dir / "lock"
    first = acquire_lease(lock_dir, "same", label="外层")
    second = acquire_lease(lock_dir, "same", label="内层")
    assert first is second
    second.release()
    assert first.path.is_file()  # 还有一层没释放
    first.release()
    assert not first.path.exists()


def test_lease_release_is_idempotent(work_dir: Path) -> None:
    lease = acquire_lease(work_dir / "lock", "x")
    lease.release()
    lease.release()  # 不抛
    assert not lease.path.exists()


def test_expired_lease_can_be_taken_over(work_dir: Path) -> None:
    lock_dir = work_dir / "lock"
    lock_dir.mkdir(parents=True)
    path = lock_dir / "stale.lock"
    path.write_text(
        json.dumps({"token": "dead", "pid": 1, "host": "somewhere", "label": "旧进程", "created_at": 0}),
        encoding="utf-8",
    )
    old = time.time() - 3600
    os.utime(path, (old, old))

    lease = acquire_lease(lock_dir, "stale", timeout=90.0, label="接管者")
    try:
        info = read_lease(lease.path)
        assert info is not None and info.token != "dead"
        assert info.label == "接管者"
    finally:
        lease.release()


def test_live_lease_is_not_taken_over(work_dir: Path) -> None:
    """心跳新鲜的锁不许被抢 —— 抢了就等于两个写者同时写一个分片。"""
    from app.tilerepo import Busy

    lock_dir = work_dir / "lock"
    held = acquire_lease(lock_dir, "live", label="持有者")
    try:
        # 同进程会重入，所以用"另一个进程"的视角模拟：手工把 token 换掉
        release_all_leases()
        path = lock_dir / "live.lock"
        path.write_text(
            json.dumps(
                {"token": "other", "pid": 999999, "host": "other-host", "label": "别人", "created_at": time.time()}
            ),
            encoding="utf-8",
        )
        with pytest.raises(Busy) as excinfo:
            acquire_lease(lock_dir, "live", timeout=90.0)
        holder = excinfo.value.detail.get("holder")
        assert isinstance(holder, dict) and holder.get("pid") == 999999
    finally:
        held.release()


def test_release_all_leases_clears_held_locks(work_dir: Path) -> None:
    lock_dir = work_dir / "lock"
    acquire_lease(lock_dir, "a")
    acquire_lease(lock_dir, "b")
    assert release_all_leases() == 2
    assert list(lock_dir.glob("*.lock")) == []


def test_coord_key_is_hashable_and_ordered() -> None:
    coords = {CoordKey(5, 1, 2), CoordKey(5, 1, 2), CoordKey(6, 0, 0)}
    assert len(coords) == 2
    assert sorted([CoordKey(6, 0, 0), CoordKey(5, 1, 2)])[0] == CoordKey(5, 1, 2)
