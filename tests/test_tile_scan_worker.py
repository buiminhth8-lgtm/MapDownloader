"""后台扫描已有瓦片（Slice 7，Spec §7 / §11）。

这个类存在的唯一理由是 Spec §7 的那条禁令：**SQLite 不许在 Qt 主线程里跑**。
因此这里要验证的不只是"数出来的数字对不对"，还有"它真的在另一个线程里"——
后者才是这个类存在的意义；前者 :mod:`tests.test_resume_download` 已经覆盖过
``count_existing_tiles`` 本身了。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QThread

from app.geo import GeoBounds
from app.mbtiles import MBTilesMetadata, MBTilesWriter
from app.services import count_existing_tiles
from app.ui import tile_scan_worker as tile_scan_worker_module
from app.ui.tile_scan_worker import ExistingTileScan, ExistingTileScanWorker

#: 一个足够小的范围：z12 下单块区域只有几张瓦片，用例跑得快
_WEST, _SOUTH, _EAST, _NORTH = 116.0, 39.0, 116.1, 39.1
_BOUNDS = GeoBounds(_WEST, _SOUTH, _EAST, _NORTH)


def _seed_mbtiles(path: Path, tiles: int, zoom: int = 12) -> Path:
    """预置一个含 ``tiles`` 张真瓦片的 ``.mbtiles``（走生产 Writer）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = MBTilesWriter(path, batch_size=7)
    writer.open()
    writer.write_metadata(
        MBTilesMetadata.for_bounds(_BOUNDS, zoom, zoom, name="seed", description="")
    )
    written = 0
    for x in range(3367, 3400):
        for y in range(1560, 1600):
            if written >= tiles:
                break
            writer.write_tile(zoom, x, y, b"\x89PNG\r\n\x1a\n" + b"0" * 16)
            written += 1
        if written >= tiles:
            break
    writer.close()
    return path


def _run(worker: ExistingTileScanWorker, qapp, timeout: int = 10_000) -> ExistingTileScan:
    """跑一次扫描并返回结果（信号在 worker 线程发出，这里同步收）。"""
    received: list[ExistingTileScan] = []
    worker.scanned.connect(received.append)
    worker.start()
    assert worker.wait(timeout) is True, "扫描线程没有在超时前结束"
    for _ in range(4):
        qapp.processEvents()
    assert len(received) == 1, "扫描应当恰好发一次结果"
    return received[0]


# --------------------------------------------------------------------------- #
# 计数
# --------------------------------------------------------------------------- #
def test_scan_counts_existing_tiles(qapp, work_dir: Path) -> None:
    path = _seed_mbtiles(work_dir / "beijing.mbtiles", tiles=5)

    result = _run(ExistingTileScanWorker(path, [12]), qapp)

    assert result.exists is True
    assert result.tiles == 5
    assert result.zooms == (12,)
    assert result.error is None


def test_scan_matches_the_service_layer(qapp, work_dir: Path) -> None:
    """Worker 的数字必须与直接调 service 完全一致（它只是搬到了另一个线程）。"""
    path = _seed_mbtiles(work_dir / "beijing.mbtiles", tiles=7)

    result = _run(ExistingTileScanWorker(path, [12]), qapp)

    assert result.tiles == count_existing_tiles(path, [12])


def test_scan_of_missing_file_is_not_an_error(qapp, work_dir: Path) -> None:
    """目标文件不存在是**正常情况**（全新下载），不是错误。

    把它报成错误会让每条全新下载的日志里都多一行"扫描失败"的噪音，
    用户会以为哪里坏了。
    """
    path = work_dir / "not-downloaded-yet.mbtiles"

    result = _run(ExistingTileScanWorker(path, [12]), qapp)

    assert result.exists is False
    assert result.tiles == 0
    assert result.error is None


def test_scan_only_counts_requested_zooms(qapp, work_dir: Path) -> None:
    """只统计任务关心的 zoom 区间——多算出来的张数会让"还要下几张"变成错的。"""
    path = _seed_mbtiles(work_dir / "beijing.mbtiles", tiles=3, zoom=12)

    inside = _run(ExistingTileScanWorker(path, [12]), qapp)
    outside = _run(ExistingTileScanWorker(path, [11, 13]), qapp)

    assert inside.tiles == 3
    assert outside.tiles == 0


def test_zooms_are_deduplicated_and_sorted() -> None:
    worker = ExistingTileScanWorker("x.mbtiles", [12, 10, 12, 11])
    assert worker.zooms == (10, 11, 12)


# --------------------------------------------------------------------------- #
# 线程边界（这才是本类存在的理由）
# --------------------------------------------------------------------------- #
def test_scan_runs_off_the_calling_thread(
    qapp, work_dir: Path, monkeypatch
) -> None:
    """扫描必须真的发生在**另一个**线程里（Spec §7 的落点）。

    这条是本模块的核心断言。做法是给 ``count_existing_tiles`` 套一层记录：
    记下它被调用时所在的线程，再和主线程对比。如果哪天有人图省事把它改回
    "在 ``_start_download`` 里直接调用"，数字还是对的、其他用例还是绿的，
    只有这条会红——这正是想让它抓住的回归。
    """
    path = _seed_mbtiles(work_dir / "beijing.mbtiles", tiles=2)
    observed: list[QThread] = []
    original = count_existing_tiles

    def spy(target: object, zooms: object) -> int:
        observed.append(QThread.currentThread())
        return original(target, zooms)  # type: ignore[arg-type]

    monkeypatch.setattr(tile_scan_worker_module, "count_existing_tiles", spy)

    result = _run(ExistingTileScanWorker(path, [12]), qapp)

    assert result.tiles == 2
    assert len(observed) == 1
    assert observed[0] is not qapp.thread(), "计数查询跑在了主线程上（Spec §7 禁止）"


def test_signal_arrives_on_the_main_thread(qapp, work_dir: Path) -> None:
    """信号必须回到**主线程**（队列连接），否则 UI 会在工作线程里被改。"""
    path = _seed_mbtiles(work_dir / "beijing.mbtiles", tiles=2)
    threads: list[QThread] = []

    worker = ExistingTileScanWorker(path, [12])
    worker.scanned.connect(lambda _scan: threads.append(QThread.currentThread()))
    worker.start()
    assert worker.wait(10_000) is True
    for _ in range(4):
        qapp.processEvents()

    assert threads == [qapp.thread()]


def test_worker_reports_the_path_it_scanned(qapp, work_dir: Path) -> None:
    path = work_dir / "named-beijing.mbtiles"
    worker = ExistingTileScanWorker(path, [12])

    assert worker.output_path == path

    result = _run(worker, qapp)
    assert result.output_path == path


# --------------------------------------------------------------------------- #
# 放弃（用户点了「取消」）
# --------------------------------------------------------------------------- #
def test_abandon_marks_the_worker(qapp, work_dir: Path) -> None:
    """``abandon()`` 只做一个标记：结果照发，由主线程决定丢弃。

    刻意不去中断 SQL：``sqlite3`` 的查询默认不可中断，为此引入第二个连接与
    ``interrupt()`` 的代价远大于收益（计数查询最多几百毫秒）。用户感知到的
    仍然是"立刻取消"。
    """
    path = _seed_mbtiles(work_dir / "beijing.mbtiles", tiles=2)
    worker = ExistingTileScanWorker(path, [12])

    assert worker.abandoned is False
    worker.abandon()
    assert worker.abandoned is True

    # 放弃之后依然会正常结束并发出结果（只是调用方不再使用它）
    result = _run(worker, qapp)
    assert result.tiles == 2
    assert worker.abandoned is True


# --------------------------------------------------------------------------- #
# 失败路径
# --------------------------------------------------------------------------- #
def test_scan_error_is_reported_not_raised(
    qapp, work_dir: Path, monkeypatch
) -> None:
    """扫描本身失败时：``error`` 有话说、``tiles`` 为 0，**异常绝不冒泡进 Qt**。

    异常冒泡进 Qt 事件循环的后果比"多数几张瓦片"严重得多——它会让整个进程
    在没有任何 Python traceback 的情况下消失。
    """
    path = _seed_mbtiles(work_dir / "beijing.mbtiles", tiles=2)

    def boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(tile_scan_worker_module, "count_existing_tiles", boom)

    result = _run(ExistingTileScanWorker(path, [12]), qapp)

    assert result.error is not None
    assert "扫描已有瓦片失败" in result.error
    assert "database is locked" in result.error
    assert result.tiles == 0
    # 文件是存在的——"数不出来"与"文件不在"是两件事，调用方要能分辨
    assert result.exists is True


# --------------------------------------------------------------------------- #
# 结果对象的形状
# --------------------------------------------------------------------------- #
def test_scan_result_defaults() -> None:
    scan = ExistingTileScan(Path("x.mbtiles"))
    assert scan.exists is False
    assert scan.tiles == 0
    assert scan.zooms == ()
    assert scan.error is None


def test_scan_result_is_frozen() -> None:
    """结果是一份不可变快照：它在两个线程之间传递，不该被中途改写。"""
    scan = ExistingTileScan(Path("x.mbtiles"), tiles=3)
    with pytest.raises(Exception):
        scan.tiles = 99  # type: ignore[misc]


def test_worker_is_a_qthread_with_a_scanned_signal() -> None:
    """接口契约：它必须是一个 ``QThread``，并且结果只通过 ``scanned`` 送出。"""
    worker = ExistingTileScanWorker("x.mbtiles", [12])

    assert isinstance(worker, QThread)
    assert hasattr(worker, "scanned")
    # 参数化之前的原始状态
    assert worker.isRunning() is False
    assert worker.abandoned is False
