"""进程被强杀之后的恢复（**真子进程**，不是"抛个异常"）。

为什么必须开子进程
------------------
``os._exit`` 与 ``kill -9`` 的区别在于"谁来清理"：进程内 ``raise`` 之后
``finally`` / ``atexit`` 都还会跑，SQLite 也会被优雅关闭，于是"写到一半崩了"
这个场景根本没被复现。本文件用 ``TILEREPO_FAULT_INJECT=crash:<phase>`` 让子进程在
指定阶段 ``os._exit(97)`` —— 没有清理、没有 flush、连接不关、锁文件不删。

五个注入点与"崩完之后应该是什么样"的对应关系（契约 C6/C7）::

    after_operation      操作记录写了，分片一个字节没写     → aborted，读 ABSENT
    during_shard_write   分片事务进行中                     → aborted，读 ABSENT
    after_shard_commit   分片事务已提交，catalog 没发布      → republish，读 UNPUBLISHED
    before_publish       同上（catalog 事务还没开）          → republish，读 UNPUBLISHED
    after_publish        已发布，操作没打 done               → 只补 done，读 PRESENT

``recover()`` 之后一律 ``PRESENT``（前两种除外 —— 那两批数据确实没落盘，不许假装
有）。第二次 ``recover()`` 必须 ``changed == 0``：恢复本身要幂等。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.tilerepo import Busy, LookupStatus, TileRepository
from app.tilerepo import shard as shard_module
from app.tilerepo._testhooks import CRASH_EXIT_CODE
from tests.tilerepo_fakes import (
    PUT_ONE_CHILD,
    kill_tree,
    make_repo,
    png_bytes,
    run_child,
    spawn_lease_holder,
)

ASSET = "tianditu/vec_w"
CHILD_COLOR = (11, 22, 33)

#: ``phase -> (恢复分类, 崩溃后立刻读到的状态, 恢复后的状态)``。
PHASES: dict[str, tuple[str, LookupStatus, LookupStatus]] = {
    "after_operation": ("aborted", LookupStatus.ABSENT, LookupStatus.ABSENT),
    "during_shard_write": ("aborted", LookupStatus.ABSENT, LookupStatus.ABSENT),
    "after_shard_commit": ("republished", LookupStatus.UNPUBLISHED, LookupStatus.PRESENT),
    "before_publish": ("republished", LookupStatus.UNPUBLISHED, LookupStatus.PRESENT),
    "after_publish": ("completed", LookupStatus.PRESENT, LookupStatus.PRESENT),
}


def _open(root: Path) -> TileRepository:
    """打开一个已经存在的仓库（``create=False``：崩溃后不许"顺手重建"）。"""
    return TileRepository.open(root, create=False)


def test_child_without_fault_finishes_normally(work_dir: Path) -> None:
    """对照组：不注入故障时子进程应当正常结束 —— 否则后面的"崩溃"说明不了问题。"""
    root = work_dir / "repo"
    result = run_child(PUT_ONE_CHILD, root)
    assert result.returncode == 0, result.stderr
    assert "CHILD_FINISHED" in result.stdout

    with _open(root) as repo:
        lookup = repo.read_tile(ASSET, 5, 1, 2)
        assert lookup.status is LookupStatus.PRESENT
        assert lookup.record is not None
        assert lookup.record.data == png_bytes(CHILD_COLOR)


@pytest.mark.parametrize("phase", list(PHASES))
def test_crash_then_recover(work_dir: Path, phase: str) -> None:
    category, before, after = PHASES[phase]
    root = work_dir / f"repo-{phase}"
    result = run_child(PUT_ONE_CHILD, root, fault=f"crash:{phase}")

    # 1. 真的是进程级中断：退出码是注入点约定的 97，且没走到最后一行 print
    assert result.returncode == CRASH_EXIT_CODE, result.stderr
    assert "CHILD_FINISHED" not in result.stdout

    with _open(root) as repo:
        # 2. 崩溃之后、恢复之前：可见性必须如实反映"提交到哪里了"
        assert repo.read_tile(ASSET, 5, 1, 2).status is before
        # 3. 结构没坏：未完成的操作只是 warning，不该把健康检查打成红色
        report = repo.health_check()
        assert report.ok is True, report.as_lines()
        pending = [item for item in report.issues if item.code == "pending_operation"]
        assert len(pending) == 1

        # 4. 恢复：按"分片里到底有没有这一代的行"分类
        recovery = repo.recover()
        assert getattr(recovery, category), recovery.as_dict()
        assert recovery.changed == 1
        assert recovery.errors == ()

        assert repo.read_tile(ASSET, 5, 1, 2).status is after
        # 5. 幂等：第二次恢复什么都不用改
        assert repo.recover().changed == 0
        assert repo.health_check().ok is True


def test_recover_keeps_the_bytes_that_reached_disk(work_dir: Path) -> None:
    """崩在"分片已提交、catalog 未发布"时，恢复后必须能读到**原来那份字节**。

    这是契约里"恢复不得静默丢失已经成功返回给调用方的写入"的直接验收：
    数据已经在磁盘上了，只是还没发布 —— 恢复的动作是"发布"，不是"删掉重来"。
    """
    root = work_dir / "repo"
    assert run_child(PUT_ONE_CHILD, root, fault="crash:before_publish").returncode == CRASH_EXIT_CODE

    with _open(root) as repo:
        # 恢复前：用 include_unpublished 能看到那一行（维护/验收专用开关）
        hidden = repo.read_tile(ASSET, 5, 1, 2, include_unpublished=True)
        assert hidden.status is LookupStatus.PRESENT
        assert hidden.record is not None
        assert hidden.record.data == png_bytes(CHILD_COLOR)

        repo.recover()
        visible = repo.read_tile(ASSET, 5, 1, 2)
        assert visible.record is not None
        assert visible.record.data == png_bytes(CHILD_COLOR)
        assert [record.coord.as_tuple() for record in repo.iter_tiles(ASSET)] == [(5, 1, 2)]


def test_new_write_takes_over_the_dead_lease(work_dir: Path) -> None:
    """被强杀的写者不会删锁。下一个写者应当**立刻**接管，而不是白等 90 秒。

    判定依据是"持有者 pid 还在不在"（``LeaseInfo.holder_alive``），心跳超时只
    是兜底：崩溃场景下心跳永远停止，等超时等于让用户重启后先干等一整个
    ``TILE_REPO_LEASE_TIMEOUT_SECONDS``。
    """
    root = work_dir / "repo"
    assert run_child(PUT_ONE_CHILD, root, fault="crash:during_shard_write").returncode == CRASH_EXIT_CODE

    with _open(root) as repo:
        locks = list(repo.lock_dir.glob("*.lock"))
        assert locks, "崩在写分片途中必须留下锁文件（否则这条验收什么也没测到）"
        info = shard_module.read_lease(locks[0])
        assert info is not None
        assert info.holder_alive() is False, "子进程已经退出，pid 不该还活着"

        started = time.monotonic()
        report = repo.put_tiles(ASSET, [(5, 1, 3, png_bytes((7, 8, 9)))])
        elapsed = time.monotonic() - started
        assert report.accepted == 1
        assert elapsed < 10, f"接管死锁花了 {elapsed:.1f}s，说明退化成了等心跳超时"

        # 死锁被接管后，新写入的瓦片照常可读
        assert repo.read_tile(ASSET, 5, 1, 3).status is LookupStatus.PRESENT

        # 之前那个崩掉的操作仍然按"没落盘"处理
        assert repo.recover().aborted
        assert repo.read_tile(ASSET, 5, 1, 2).status is LookupStatus.ABSENT


def test_live_holder_blocks_with_busy(work_dir: Path) -> None:
    """持有者**还活着**时必须拒绝（``Busy``），不能"看着像死锁就抢过来"。

    两条路都要测：抢活锁会写坏数据，而不抢死锁会让崩溃恢复卡住 —— 判活是这两者
    之间唯一的分界线。
    """
    root = work_dir / "repo"
    with make_repo(work_dir) as setup:
        setup.ensure_asset(ASSET)
        setup.put_tiles(ASSET, [(5, 1, 1, png_bytes())])
    # 必须先关掉本进程的仓库：租约是进程级的，自己还拿着的话子进程也抢不到
    holder, holder_pid = spawn_lease_holder(root, "tianditu-vec_w-z00-06")
    try:
        with _open(root) as repo:
            health = repo.health_check()
            assert any(item.code == "dead_lease" for item in health.issues) is False
            with pytest.raises(Busy) as excinfo:
                repo.put_tiles(ASSET, [(5, 1, 2, png_bytes((1, 2, 3)))])
            detail = excinfo.value.detail
            assert str(detail.get("lock", "")).endswith(".lock")
            # 租约里记的是**真正持有者**的 pid（转发器 pid 会随时间消失，没意义）
            assert detail.get("holder", {}).get("pid") == holder_pid
            # 抢不到就不许动数据：既没写进去，也没把那个活着的写者的锁删掉
            assert (root / "lock" / "tianditu-vec_w-z00-06.lock").is_file()
            assert repo.read_tile(ASSET, 5, 1, 2).status is LookupStatus.ABSENT
    finally:
        kill_tree(holder)


def test_crash_does_not_break_other_shards(work_dir: Path) -> None:
    """一个分片崩了不影响别的分片：别把"局部未完成"升级成"整仓不可用"。"""
    root = work_dir / "repo"
    with make_repo(root.parent, name=root.name) as repo:
        repo.ensure_asset(ASSET)
        repo.put_tiles(ASSET, [(5, 0, 0, png_bytes((5, 5, 5)))])

    script = """
import io, sys
from pathlib import Path
from PIL import Image
from app.tilerepo import TileRepository

root = Path(sys.argv[1])
buffer = io.BytesIO()
Image.new('RGB', (256, 256), (11, 22, 33)).save(buffer, format='PNG')
repo = TileRepository.open(root, create=False)
repo.put_tiles('tianditu/vec_w', [(13, 1, 1, buffer.getvalue())])
"""
    result = run_child(script, root, fault="crash:during_shard_write")
    assert result.returncode == CRASH_EXIT_CODE

    with _open(root) as repo:
        # z5 的分片完全没被这次崩溃碰到
        assert repo.read_tile(ASSET, 5, 0, 0).status is LookupStatus.PRESENT
        assert repo.read_tile(ASSET, 13, 1, 1).status is LookupStatus.ABSENT
        assert repo.recover().aborted
        assert repo.health_check().ok is True
