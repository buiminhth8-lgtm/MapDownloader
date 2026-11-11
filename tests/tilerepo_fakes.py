"""Slice 42 测试夹具：合成瓦片字节、临时仓库、子进程运行器。

三条约定（与项目其它 ``*_fakes.py`` 一致）：

1. **不联网、不读真实地图**。瓦片一律用 Pillow 现画，颜色决定内容，够用且确定。
2. **不用 ``tmp_path``**。本机 ``pytest-of-<user>`` 根会被沙箱拦在
   ``PermissionError``，所以临时目录一律走 ``conftest.work_dir``。
3. **子进程用同一份解释器与 ``PYTHONPATH``**：崩溃验收要的正是"真的被强杀"，
   在测试进程内 ``raise`` 代替不了它。
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image

from app.tilerepo import TileRepository

#: 仓库根（子进程的 ``cwd`` 与 ``PYTHONPATH`` 都用它）。
REPO_ROOT = Path(__file__).resolve().parent.parent

#: 合成瓦片的默认边长（与 ``settings.TILE_SIZE`` 一致）。
TILE_SIDE = 256


def png_bytes(color: tuple[int, int, int] = (200, 30, 30), size: tuple[int, int] | None = None) -> bytes:
    """画一张纯色 PNG（默认 256×256）。"""
    side = size or (TILE_SIDE, TILE_SIDE)
    buffer = io.BytesIO()
    Image.new("RGB", side, color).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(color: tuple[int, int, int] = (10, 120, 200), size: tuple[int, int] | None = None) -> bytes:
    """画一张纯色 JPEG（默认 256×256）。"""
    side = size or (TILE_SIDE, TILE_SIDE)
    buffer = io.BytesIO()
    Image.new("RGB", side, color).save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def distinct_png(index: int) -> bytes:
    """由序号生成**内容不同**的瓦片（颜色按序号取模，保证 256 以内不重复）。"""
    return png_bytes(((index * 37) % 256, (index * 91) % 256, (index * 53) % 256))


def make_repo(work_dir: Path, name: str = "repo", **kwargs: object) -> TileRepository:
    """在 ``work_dir`` 下建一个空仓库。"""
    return TileRepository.open(work_dir / name, create=True, **kwargs)  # type: ignore[arg-type]


def child_env(**extra: str) -> dict[str, str]:
    """子进程环境：把仓库根放进 ``PYTHONPATH``，再叠加调用方给的变量。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.pop("TILEREPO_FAULT_INJECT", None)
    env.update(extra)
    return env


def run_child(
    code: str,
    root: Path,
    *,
    fault: str | None = None,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess:
    """在子进程里跑一段代码（``sys.argv[1]`` 是仓库根）。

    Args:
        code: 要执行的 Python 源码。
        root: 传给子进程的仓库根目录。
        fault: 故障注入规格（``'crash:after_shard_commit'`` 之类），None 表示不注入。
        timeout: 子进程超时（秒）。

    Returns:
        ``subprocess.CompletedProcess``（``returncode`` 见 ``_testhooks.CRASH_EXIT_CODE``）。
    """
    env = child_env(**({"TILEREPO_FAULT_INJECT": fault} if fault else {}))
    return subprocess.run(
        [sys.executable, "-c", code, str(root)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


#: 子进程里"写一张瓦片"的最小脚本（崩溃验收与并发验收共用）。
PUT_ONE_CHILD = """
import io, sys
from pathlib import Path
from PIL import Image
from app.tilerepo import TileRepository

root = Path(sys.argv[1])
buffer = io.BytesIO()
Image.new('RGB', (256, 256), (11, 22, 33)).save(buffer, format='PNG')
repo = TileRepository.open(root, create=True)
repo.ensure_asset('tianditu/vec_w')
repo.put_tiles('tianditu/vec_w', [(5, 1, 2, buffer.getvalue())])
repo.close()
print('CHILD_FINISHED')
"""

#: 子进程里"长期持有某个分片租约"的脚本（``argv[2]`` 是锁名）。
HOLD_LEASE_CHILD = """
import os, sys, time
from pathlib import Path
from app.tilerepo import shard

root = Path(sys.argv[1])
lease = shard.acquire_lease(root / 'lock', sys.argv[2], label='测试占位（另一个进程）')
print('LOCKED', os.getpid(), flush=True)
time.sleep(60)
lease.release()
"""


def spawn_lease_holder(root: Path, lock_name: str) -> tuple[subprocess.Popen, int]:
    """起一个子进程持有租约，直到被杀掉。

    Returns:
        ``(进程, 真正持有者的 pid)``。pid 由子进程自己打印：本机
        ``venv/Scripts/python.exe`` 是个转发器，``Popen.pid`` 拿到的是转发器，
        与子进程里的 ``os.getpid()`` 不是同一个值。
    """
    process = subprocess.Popen(
        [sys.executable, "-c", HOLD_LEASE_CHILD, str(root), lock_name],
        cwd=str(REPO_ROOT),
        env=child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    handshake = process.stdout.readline().split()
    if handshake[:1] != ["LOCKED"]:  # pragma: no cover - 子进程起不来时给出线索
        raise AssertionError(f"子进程没能拿到租约：{process.stderr.read() if process.stderr else ''}")
    return process, int(handshake[1])


def kill_tree(process: subprocess.Popen) -> None:
    """连整棵进程树一起结束（见 :func:`spawn_lease_holder` 里关于转发器的说明）。"""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:  # pragma: no cover - 本机是 Windows，这条分支只为可移植性保留
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):  # pragma: no cover
        process.wait(timeout=30)
