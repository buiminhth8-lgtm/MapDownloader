"""流式文件工具：分块复制 + 流式 SHA256（Slice 9，Spec §4 / §9 / §32）。

为什么整份文件读进内存是**禁止**的
----------------------------------

Spec §32 点名了三种规模：500 MB / 2 GB / 5 GB。``path.read_bytes()`` 在 5 GB 的
MBTiles 上会立刻抛 ``MemoryError`` —— 而且是在用户已经点了"生成地图包"、等了
两分钟之后才抛。所以本模块只提供**分块**接口：

- :func:`calculate_sha256` 每次读 ``chunk_size``（默认 4 MiB）喂给
  ``hashlib``，内存峰值恒等于一个块大小，与文件大小无关；
- :func:`copy_file` 同样按块读写，顺带把"已复制多少字节"报给调用方，
  于是界面上的进度条是真的在走，而不是从 0 直接跳到 100。

两者的结果都必须与"一次性读完再算 / ``shutil.copy2``"**逐字节一致** ——
测试里就是拿 ``hashlib.sha256(path.read_bytes())`` 对照流式结果（Spec §27）。

进度回调用 ``(done, total)`` 两个字节数，而不是直接发界面用的进度对象：
本模块属于"文件工具"层，不该知道界面文案长什么样（Slice 9 的界面文案全在
``app.config.settings``）。谁调谁负责翻译。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path
from typing import Callable

from app.config.settings import (
    PACKAGE_COPY_CHUNK_SIZE,
    PACKAGE_SHA256_CHUNK_SIZE,
)
from app.package.models import PackageAborted, PackageError

__all__ = [
    "ByteProgressCallback",
    "calculate_sha256",
    "copy_file",
    "verify_sha256",
    "file_size",
]

_logger = logging.getLogger(__name__)

#: 字节级进度回调：``(已处理字节, 总字节)``。总字节在开始前就已经确定，
#: 因为两者都作用于一个已经存在于磁盘上的文件。
ByteProgressCallback = Callable[[int, int], None]

#: 每处理多少字节回报一次进度。4 MiB 的块 + 每块回报一次 ≈ 每秒几次，
#: 足够让进度条顺滑，又不会把信号队列刷爆（跨线程 emit 是有成本的）。
_PROGRESS_EVERY_BYTES = 4 * 1024 * 1024


def _require_file(path: str | Path) -> Path:
    """确认路径是一个**普通文件**，返回 ``Path``。

    目录 / 不存在的路径必须在动手之前就报错：``open()`` 对目录抛的是
    ``IsADirectoryError``，对不存在的文件抛 ``FileNotFoundError`` —— 两者都
    不带"我们要拿它当 MBTiles 用"这层语义，直接往上抛会让用户看到一句
    ``[Errno 21] Is a directory``。
    """
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"文件不存在：{target}")
    if not target.is_file():
        raise PackageError(f"不是文件：{target}")
    return target


def file_size(path: str | Path) -> int:
    """返回文件字节数（不存在 / 不是文件时抛错，语义同 :func:`_require_file`）。"""
    return _require_file(path).stat().st_size


def _report(
    callback: ByteProgressCallback | None,
    done: int,
    total: int,
    last_reported: int,
    *,
    force: bool = False,
) -> int:
    """按块节流地回报一次进度，返回"上次回报到的字节数"。

    节流放在这里而不是界面侧：4 MiB 的块读一次就 emit 一次已经够用，
    再往上叠一层定时器只会让"进度条为什么不动"更难解释。
    """
    if callback is None:
        return last_reported
    if not force and done - last_reported < _PROGRESS_EVERY_BYTES and done < total:
        return last_reported
    callback(done, total)
    return done


def calculate_sha256(
    path: str | Path,
    chunk_size: int = PACKAGE_SHA256_CHUNK_SIZE,
    progress_callback: ByteProgressCallback | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> str:
    """流式计算文件的 SHA256，返回 64 位**小写** hex（Spec §4）。

    Args:
        path: 目标文件（GB 级也没问题）。
        chunk_size: 每次读取的字节数，默认 4 MiB。
        progress_callback: 可选进度回调 ``(已读字节, 总字节)``。
        should_stop: 可选中止条件（每块查一次）。为 ``True`` 时抛
            :class:`~app.package.PackageAborted` —— 用户关掉对话框之后，
            不该还有一个线程在把 5 GB 的文件从头读到尾。

    Returns:
        ``hashlib.sha256(path.read_bytes()).hexdigest()`` 的等价结果。

    Raises:
        FileNotFoundError: 文件不存在。
        PackageError: 路径不是普通文件，或 ``chunk_size`` 非法。
        PackageAborted: 被 ``should_stop`` 中止。

    >>> import hashlib, tempfile, pathlib
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     target = pathlib.Path(tmp) / "a.bin"
    ...     _ = target.write_bytes(b"hello")
    ...     calculate_sha256(target) == hashlib.sha256(b"hello").hexdigest()
    True
    """
    size = int(chunk_size)
    if size <= 0:
        raise PackageError(f"chunk_size 必须为正整数，实际为 {chunk_size}")

    source = _require_file(path)
    total = source.stat().st_size

    digest = hashlib.sha256()
    done = 0
    reported = 0
    with open(source, "rb") as handle:
        while True:
            if should_stop is not None and should_stop():
                raise PackageAborted(f"计算 SHA256 已被中止：{source}")
            block = handle.read(size)
            if not block:
                break
            digest.update(block)
            done += len(block)
            reported = _report(progress_callback, done, total, reported)

    _report(progress_callback, done, total, reported, force=True)
    return digest.hexdigest()


def verify_sha256(
    path: str | Path,
    expected: str,
    chunk_size: int = PACKAGE_SHA256_CHUNK_SIZE,
    progress_callback: ByteProgressCallback | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """实际 SHA256 是否与 ``expected`` 相同（大小写不敏感）。

    比较前统一 ``strip().lower()``：manifest 可能是手写的 / 从别处复制的，
    末尾多一个换行或大写字母不该被判成"文件损坏" —— 真正的损坏是
    哈希值不同，不是"写法不同"。
    """
    actual = calculate_sha256(
        path,
        chunk_size=chunk_size,
        progress_callback=progress_callback,
        should_stop=should_stop,
    )
    return actual == str(expected).strip().lower()


def copy_file(
    source: str | Path,
    target: str | Path,
    chunk_size: int = PACKAGE_COPY_CHUNK_SIZE,
    progress_callback: ByteProgressCallback | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """把 ``source`` 分块复制到 ``target``，返回复制的字节数（Spec §9）。

    与 ``shutil.copy2`` 的差别只有两点，都是为了本 Slice 的需求：

    1. **分块**，于是能报进度（Spec §9 明确要求 ``bytes_copied`` / ``total_bytes``）；
    2. 可中止。

    文件权限与修改时间用 ``shutil.copystat`` 补上（等价于 ``copy2`` 的那部分
    行为）。``copystat`` 失败**不算错误**：FAT32 的 U 盘、某些网络盘上它本来
    就会失败，而"复制内容"才是用户的诉求 —— 只记一条日志。

    Raises:
        FileNotFoundError: 源文件不存在。
        PackageError: 源不是普通文件、目标与源是同一个文件、``chunk_size`` 非法。
        PackageAborted: 被 ``should_stop`` 中止（此时目标文件可能只写了一半，
            调用方负责删掉它 —— 构建器会连整个 ``.building`` 目录一起清掉）。
    """
    size = int(chunk_size)
    if size <= 0:
        raise PackageError(f"chunk_size 必须为正整数，实际为 {chunk_size}")

    src = _require_file(source)
    dst = Path(target)
    if src.resolve() == dst.resolve():
        # 复制到自身会把文件截断成 0 字节 —— 必须是显式错误而不是"看起来成功"。
        raise PackageError(f"源文件与目标文件是同一个文件，拒绝原地复制：{src}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    total = src.stat().st_size
    done = 0
    reported = 0
    with open(src, "rb") as reader, open(dst, "wb") as writer:
        while True:
            if should_stop is not None and should_stop():
                raise PackageAborted(f"复制已被中止：{src} → {dst}")
            block = reader.read(size)
            if not block:
                break
            writer.write(block)
            done += len(block)
            reported = _report(progress_callback, done, total, reported)

    _report(progress_callback, done, total, reported, force=True)

    try:
        shutil.copystat(src, dst)
    except OSError as exc:  # pragma: no cover - 取决于文件系统
        _logger.warning("保留源文件时间戳失败（不影响内容）：%s", exc)

    # 把数据刷到磁盘再返回：后面紧接着要算 SHA256 并把它写进 manifest，
    # 让"元数据先落地、内容还在缓存里"这种顺序不再可能。
    _flush_to_disk(dst)
    return done


def _flush_to_disk(path: Path) -> None:
    """``fsync`` 一次（失败只记日志）。

    Windows 上 ``os.fsync`` 对普通文件是有效的，但某些网络盘会抛 ``OSError``。
    那种情况下"能不能立刻落盘"由操作系统决定，我们不该让打包失败在这里。
    """
    try:
        with open(path, "rb+") as handle:
            os.fsync(handle.fileno())
    except OSError as exc:  # pragma: no cover - 取决于文件系统
        _logger.debug("fsync 失败（不影响正确性）：%s", exc)
