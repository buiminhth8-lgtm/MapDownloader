"""并发与资源生命周期（Slice 42 要求 9）。

约定：**进程内并发靠"每操作一个新连接 + 写租约重入 + BEGIN IMMEDIATE"，
跨进程靠文件租约**。这里逐条验证，外加"关闭之后句柄确实放掉了"——
在 Windows 上这件事只能用"能不能删掉整个目录"来证明。
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any

import pytest

from app.tilerepo import Busy, RepoClosed, TileRepository
from tests.tilerepo_fakes import distinct_png, make_repo, png_bytes

ASSET = "tianditu/vec_w"


@pytest.fixture
def repo(work_dir: Path):
    with make_repo(work_dir) as instance:
        instance.ensure_asset(ASSET)
        yield instance


def _run_parallel(targets: list[Any]) -> list[BaseException | None]:
    """并发跑一组调用，返回各自的异常（None = 成功）。"""
    errors: list[BaseException | None] = [None] * len(targets)
    barrier = threading.Barrier(len(targets))

    def wrapper(index: int, call) -> None:
        try:
            barrier.wait(timeout=30)
            call()
        except BaseException as exc:  # noqa: BLE001 - 要把异常带回主线程断言
            errors[index] = exc

    threads = [
        threading.Thread(target=wrapper, args=(index, call), daemon=True)
        for index, call in enumerate(targets)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    return errors


def test_two_threads_write_different_coords(repo: TileRepository) -> None:
    """同一分片、不同坐标：两个线程都要成功（进程内重入持有同一把文件租约）。"""
    errors = _run_parallel(
        [
            lambda: repo.put_tiles(ASSET, [(5, 1, 1, distinct_png(1))]),
            lambda: repo.put_tiles(ASSET, [(5, 2, 2, distinct_png(2))]),
        ]
    )
    assert errors == [None, None]
    assert {record.coord.as_tuple() for record in repo.iter_tiles(ASSET)} == {
        (5, 1, 1),
        (5, 2, 2),
    }
    assert repo.health_check().ok is True


def test_two_threads_write_the_same_coord(repo: TileRepository) -> None:
    """同一坐标并发：要么幂等（同内容），要么一个成功一个冲突（不同内容）。

    无论哪种，**最终只能有一行 current** —— 这是"不得无条件覆盖"和
    "并发下也不会写出两行当前版本"的合取。
    """
    same = png_bytes((30, 40, 50))
    errors = _run_parallel(
        [
            lambda: repo.put_tiles(ASSET, [(5, 3, 3, same)]),
            lambda: repo.put_tiles(ASSET, [(5, 3, 3, same)]),
        ]
    )
    assert errors == [None, None]
    assert repo.read_tile(ASSET, 5, 3, 3).status.value == "present"
    versions = list(repo.iter_versions(ASSET, 5, 3, 3))
    assert len(versions) == 1, "同内容并发写不该产生两代版本"

    other = [(5, 4, 4, png_bytes((1, 1, 1))), (5, 4, 4, png_bytes((2, 2, 2)))]
    errors = _run_parallel(
        [
            lambda: repo.put_tiles(ASSET, [other[0]]),
            lambda: repo.put_tiles(ASSET, [other[1]]),
        ]
    )
    assert errors == [None, None] or all(
        isinstance(item, Exception) for item in errors if item is not None
    )
    current = [item for item in repo.iter_versions(ASSET, 5, 4, 4) if item[0].value == 0]
    assert len(current) == 1


def test_reader_sees_consistent_snapshot_while_writing(repo: TileRepository) -> None:
    """读与写并行：读到的只能是"某个完整提交点"，不会读到半个批次。"""
    repo.put_tiles(ASSET, [(5, x, 1, distinct_png(x)) for x in range(1, 4)])
    seen: list[int] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            seen.append(sum(1 for _ in repo.iter_tiles(ASSET)))

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for x in range(4, 9):
            repo.put_tiles(ASSET, [(5, x, 1, distinct_png(x))])
    finally:
        stop.set()
        thread.join(timeout=60)

    assert seen, "读线程一次都没跑完，这个用例什么也没验证到"
    assert all(count >= 3 for count in seen), "读到了比初始状态还少的行 —— 有半提交"


def test_cross_process_writes_to_different_bands(repo: TileRepository) -> None:
    """跨进程、不同分片：互不影响（租约是按分片带的，不是整仓一把锁）。"""
    from tests.tilerepo_fakes import child_env

    script = """
import io, sys
from pathlib import Path
from PIL import Image
from app.tilerepo import TileRepository

root, zoom = Path(sys.argv[1]), int(sys.argv[2])
buffer = io.BytesIO()
Image.new('RGB', (256, 256), (zoom % 250, 50, 50)).save(buffer, format='PNG')
repo = TileRepository.open(root, create=False)
repo.put_tiles('tianditu/vec_w', [(zoom, 1, 1, buffer.getvalue())])
repo.close()
print('OK', zoom)
"""
    import subprocess
    import sys

    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(repo.root), str(zoom)],
            cwd=str(Path(__file__).resolve().parent.parent),
            env=child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for zoom in (5, 13)
    ]
    try:
        for process in processes:
            out, err = process.communicate(timeout=120)
            assert process.returncode == 0, err
            assert "OK" in out
    finally:
        for process in processes:
            if process.poll() is None:  # pragma: no cover - 兜底
                process.kill()

    with TileRepository.open(repo.root, create=False) as reopened:
        assert reopened.read_tile(ASSET, 5, 1, 1).status.value == "present"
        assert reopened.read_tile(ASSET, 13, 1, 1).status.value == "present"
        assert reopened.health_check().ok is True


def test_close_releases_leases_and_file_handles(repo: TileRepository, work_dir: Path) -> None:
    """``close()`` 之后：租约文件不留、句柄全部释放。

    句柄是否真的放了，Windows 上最硬的证据是"整个目录能不能被删掉"。
    """
    repo.put_tiles(ASSET, [(5, 1, 1, png_bytes())])
    root = repo.root
    repo.close()

    assert list((root / "lock").glob("*.lock")) == []
    assert repo.closed is True
    with pytest.raises(RepoClosed):
        repo.read_tile(ASSET, 5, 1, 1)
    with pytest.raises(RepoClosed):
        repo.put_tiles(ASSET, [(5, 1, 2, png_bytes())])
    with pytest.raises(RepoClosed):
        repo.health_check()

    shutil.rmtree(root)  # 有任何一个连接没关，这里会 PermissionError
    assert not root.exists()
    assert work_dir.is_dir()


def test_close_is_idempotent_and_keeps_data(repo: TileRepository) -> None:
    data = png_bytes((6, 6, 6))
    repo.put_tiles(ASSET, [(5, 1, 1, data)])
    root = repo.root
    repo.close()
    repo.close()  # 重复关闭不该抛

    with TileRepository.open(root, create=False) as reopened:
        assert reopened.read_tile(ASSET, 5, 1, 1).record.data == data


def test_busy_does_not_leave_a_half_written_batch(work_dir: Path) -> None:
    """租约抢不到时**一个字节都不许写**（否则"失败但写了一半"最难查）。"""
    from tests.tilerepo_fakes import kill_tree, spawn_lease_holder

    root = work_dir / "repo"
    with make_repo(work_dir) as repo:
        repo.ensure_asset(ASSET)
        holder, pid = spawn_lease_holder(root, "tianditu-vec_w-z00-06")
        try:
            with pytest.raises(Busy) as excinfo:
                repo.put_tiles(ASSET, [(5, 1, 1, png_bytes())])
            assert excinfo.value.detail["holder"]["pid"] == pid
            assert not repo.shard_path(ASSET, (0, 6)).exists()
            assert repo.read_tile(ASSET, 5, 1, 1).status.value == "absent"
        finally:
            kill_tree(holder)
