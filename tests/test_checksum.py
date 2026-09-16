"""流式 SHA256 与分块复制的单元测试（Slice 9，Spec §27 / §32）。

这个文件只钉两件事：

1. **流式结果必须与"一次性读完整算"完全一致**（Spec §27 明写要跟
   ``hashlib`` 对照）。分块本身很容易写出"最后一块漏了""多读了一次空块"
   这种看起来能跑、结果偶发不对的实现，而它的症状是"包明明没坏却报 SHA256
   mismatch"——最难查的一类。
2. **不把整个文件读进内存**（Spec §32）。500 MB / 2 GB / 5 GB 的库上，
   ``path.read_bytes()`` 会直接 ``MemoryError``，而且是在用户已经点了
   "生成地图包"、等了两分钟之后才炸。

因此这里刻意用**很小的 chunk_size**（16 / 64 字节）跑多块场景，而不是真造一个
GB 级文件 —— 要验证的是"多块路径正确"，不是"磁盘够大"。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.config.settings import PACKAGE_COPY_CHUNK_SIZE, PACKAGE_SHA256_CHUNK_SIZE
from app.package import (
    PackageAborted,
    PackageError,
    calculate_sha256,
    copy_file,
    file_size,
    verify_sha256,
)

# --------------------------------------------------------------------------- #
# SHA256
# --------------------------------------------------------------------------- #
def test_sha256_matches_hashlib_for_empty_file(work_dir: Path) -> None:
    """空文件：``sha256(b"")``，不能是空串或报错。"""
    target = work_dir / "empty.bin"
    target.write_bytes(b"")
    assert calculate_sha256(target) == hashlib.sha256(b"").hexdigest()


def test_sha256_matches_hashlib_for_small_file(work_dir: Path) -> None:
    target = work_dir / "small.bin"
    payload = b"mbtiles"
    target.write_bytes(payload)
    assert calculate_sha256(target) == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("size", [1, 15, 16, 17, 4096, 65_537])
def test_sha256_matches_hashlib_across_chunk_boundaries(work_dir: Path, size: int) -> None:
    """块边界前后都要一致：**比块小 / 正好一块 / 多一块的零头** 三种都覆盖。

    ``chunk_size=16`` 时这五个尺寸分别落在"不足一块""正好一块""差一字节满一块"
    "跨多块""多块 + 零头"上 —— 分块实现最容易漏的正是最后那个零头。
    """
    target = work_dir / f"size-{size}.bin"
    payload = bytes((index * 7 + 3) % 256 for index in range(size))
    target.write_bytes(payload)

    streamed = calculate_sha256(target, chunk_size=16)
    assert streamed == hashlib.sha256(payload).hexdigest()


def test_sha256_matches_hashlib_with_default_chunk_size(work_dir: Path) -> None:
    """默认块大小下也要一致（5 MB > 4 MiB，会真的走多块路径）。"""
    payload = b"tile-payload" * (5 * 1024 * 1024 // 12)
    target = work_dir / "big.bin"
    target.write_bytes(payload)
    assert PACKAGE_SHA256_CHUNK_SIZE < len(payload)

    assert calculate_sha256(target) == hashlib.sha256(payload).hexdigest()


def test_sha256_is_lowercase_hex(work_dir: Path) -> None:
    """Spec §4：返回 64 位**小写** hex。"""
    target = work_dir / "case.bin"
    target.write_bytes(b"x")
    digest = calculate_sha256(target)
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(char in "0123456789abcdef" for char in digest)


def test_sha256_reports_progress_up_to_total(work_dir: Path) -> None:
    """进度回调必须单调、最后一次等于总字节数（否则进度条会停在 80%）。"""
    target = work_dir / "progress.bin"
    target.write_bytes(b"a" * 100)
    seen: list[tuple[int, int]] = []
    calculate_sha256(target, chunk_size=16, progress_callback=lambda d, t: seen.append((d, t)))

    assert seen, "应当至少回报一次进度"
    assert [done for done, _ in seen] == sorted(done for done, _ in seen)
    assert seen[-1] == (100, 100)
    assert {total for _, total in seen} == {100}


def test_sha256_on_missing_file_raises_filenotfound(work_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        calculate_sha256(work_dir / "nope.mbtiles")


def test_sha256_on_directory_raises_package_error(work_dir: Path) -> None:
    """目录不是文件：报的是我们自己的错，而不是 ``IsADirectoryError``。"""
    folder = work_dir / "folder"
    folder.mkdir()
    with pytest.raises(PackageError):
        calculate_sha256(folder)


def test_sha256_rejects_bad_chunk_size(work_dir: Path) -> None:
    target = work_dir / "a.bin"
    target.write_bytes(b"a")
    with pytest.raises(PackageError):
        calculate_sha256(target, chunk_size=0)


def test_sha256_can_be_aborted(work_dir: Path) -> None:
    """中止要抛 :class:`PackageAborted`，而不是安静地返回一个错的摘要。

    安静返回是最坏的结果：manifest 里会写下一个**算了一半**的哈希，
    而且校验时才发现对不上。
    """
    target = work_dir / "abort.bin"
    target.write_bytes(b"a" * 4096)
    with pytest.raises(PackageAborted):
        calculate_sha256(target, chunk_size=16, should_stop=lambda: True)


# --------------------------------------------------------------------------- #
# verify_sha256
# --------------------------------------------------------------------------- #
def test_verify_sha256_accepts_matching_digest(work_dir: Path) -> None:
    target = work_dir / "v.bin"
    target.write_bytes(b"hello")
    digest = hashlib.sha256(b"hello").hexdigest()
    assert verify_sha256(target, digest) is True
    # 大小写与首尾空白不该影响结论（manifest 可能是手写的 / 复制来的）
    assert verify_sha256(target, f"  {digest.upper()}  ") is True


def test_verify_sha256_rejects_other_digest(work_dir: Path) -> None:
    target = work_dir / "v.bin"
    target.write_bytes(b"hello")
    assert verify_sha256(target, "0" * 64) is False


# --------------------------------------------------------------------------- #
# copy_file
# --------------------------------------------------------------------------- #
def test_copy_file_copies_content_and_returns_size(work_dir: Path) -> None:
    source = work_dir / "src.bin"
    payload = bytes(range(256)) * 4
    source.write_bytes(payload)
    target = work_dir / "sub" / "dst.bin"

    copied = copy_file(source, target, chunk_size=16)

    assert copied == len(payload)
    assert target.read_bytes() == payload
    # 分块复制的摘要必须与源一致 —— "复制得很像"不算完成
    assert calculate_sha256(target) == calculate_sha256(source)


def test_copy_file_reports_progress(work_dir: Path) -> None:
    source = work_dir / "src.bin"
    source.write_bytes(b"b" * 300)
    seen: list[int] = []
    copy_file(source, work_dir / "dst.bin", chunk_size=16, progress_callback=lambda d, t: seen.append(d))
    assert seen[-1] == 300
    assert seen == sorted(seen)


def test_copy_file_refuses_to_copy_onto_itself(work_dir: Path) -> None:
    """Spec §8：必须避免把文件复制到自身 —— 那会把文件截成 0 字节。"""
    source = work_dir / "same.bin"
    source.write_bytes(b"payload")
    with pytest.raises(PackageError):
        copy_file(source, source)
    # 而且**不能**已经把它截断
    assert source.read_bytes() == b"payload"


def test_copy_file_refuses_equivalent_path_spelling(work_dir: Path) -> None:
    """``a.bin`` 与 ``./a.bin`` 是同一个文件，也要被挡住。"""
    source = work_dir / "a.bin"
    source.write_bytes(b"payload")
    with pytest.raises(PackageError):
        copy_file(source, work_dir / "." / "a.bin")


def test_copy_file_can_be_aborted(work_dir: Path) -> None:
    source = work_dir / "src.bin"
    source.write_bytes(b"c" * 4096)
    with pytest.raises(PackageAborted):
        copy_file(source, work_dir / "dst.bin", chunk_size=16, should_stop=lambda: True)


def test_copy_file_missing_source_raises(work_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        copy_file(work_dir / "nope.bin", work_dir / "dst.bin")


def test_file_size_helper(work_dir: Path) -> None:
    target = work_dir / "s.bin"
    target.write_bytes(b"12345")
    assert file_size(target) == 5
    with pytest.raises(FileNotFoundError):
        file_size(work_dir / "nope.bin")


def test_default_chunk_sizes_are_streaming_friendly() -> None:
    """默认块大小必须是"几 MB"级别，不能是"整个文件"（Spec §32）。"""
    assert 1024 * 1024 <= PACKAGE_SHA256_CHUNK_SIZE <= 64 * 1024 * 1024
    assert 1024 * 1024 <= PACKAGE_COPY_CHUNK_SIZE <= 64 * 1024 * 1024
