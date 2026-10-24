"""输出文件名净化（Slice 7，Spec §21）。

为什么值得专门测
----------------

地图名称是**用户随便打的字**，而它会被直接拼进文件名。Windows 对文件名的
限制不是"最好遵守"，而是"违反之后的后果由用户承担"：

- ``*`` / ``?`` / ``:`` 等字符会被 API 拒绝，报错发生在写第一个 BLOB 之前，
  用户看到的是"下载一启动就失败"；
- 结尾的点和空格会被文件系统**静默丢弃**——于是实际落盘位置与 UI 上显示的
  「输出文件」路径不一致，而这种不一致极难排查（文件明明在那儿，按显示的名字
  却打不开）；
- ``CON`` / ``LPT1`` 这类保留名在 Windows 上根本无法创建。

因此这里逐条钉住 :func:`sanitize_map_name` 的行为。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.models import MapType
from app.services import build_output_path, sanitize_map_name


# --------------------------------------------------------------------------- #
# Windows 非法字符
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("illegal", list('\\/:*?"<>|'))
def test_illegal_characters_become_underscores(illegal: str) -> None:
    assert sanitize_map_name(f"a{illegal}b") == "a_b"


def test_multiple_illegal_characters() -> None:
    assert sanitize_map_name("北京/2024:地图*") == "北京_2024_地图_"


@pytest.mark.parametrize("code", [0x00, 0x01, 0x0A, 0x1F])
def test_control_characters_become_underscores(code: int) -> None:
    """控制字符（含换行）同样非法，不能原样进文件名。"""
    assert sanitize_map_name(f"a{chr(code)}b") == "a_b"


# --------------------------------------------------------------------------- #
# 结尾的点与空格
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", ["beijing.", "beijing ", "beijing. ", "beijing ."])
def test_trailing_dots_and_spaces_are_stripped(raw: str) -> None:
    """Windows 会静默丢掉结尾的点和空格 → 必须提前去掉，否则路径对不上。"""
    assert sanitize_map_name(raw) == "beijing"


def test_inner_spaces_are_preserved() -> None:
    """只清"结尾"的空白。中文名称里的空格是用户有意打的，删掉就是改他的东西。"""
    assert sanitize_map_name("北京 城区 地图") == "北京 城区 地图"


@pytest.mark.parametrize("raw", ["  beijing  ", "\tbeijing\n"])
def test_surrounding_whitespace_is_stripped(raw: str) -> None:
    assert sanitize_map_name(raw) == "beijing"


# --------------------------------------------------------------------------- #
# Windows 保留名
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "reserved",
    ["CON", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9"],
)
def test_reserved_names_get_a_suffix(reserved: str) -> None:
    """这些名字在 Windows 上创建不出来，必须改名而不是让下载失败。"""
    assert sanitize_map_name(reserved) == f"{reserved}_map"


def test_reserved_name_check_is_case_insensitive() -> None:
    assert sanitize_map_name("con") == "con_map"


def test_reserved_name_with_extension_still_matched() -> None:
    """``CON.txt`` 同样是保留名（比较的是第一个点之前的部分）。"""
    assert sanitize_map_name("CON.txt") == "CON.txt_map"


def test_ordinary_name_containing_reserved_words_is_untouched() -> None:
    """``console`` 不是保留名——不能用子串匹配把正常名字也改掉。"""
    assert sanitize_map_name("console") == "console"
    assert sanitize_map_name("LPT10") == "LPT10"


# --------------------------------------------------------------------------- #
# 空值兜底与长度
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", ["", "   ", "...", " . "])
def test_empty_after_cleaning_falls_back_to_default(raw: str) -> None:
    """清完什么都不剩 → 退回默认名，绝不返回空串（空文件名无法落盘）。"""
    assert sanitize_map_name(raw) == settings.DEFAULT_MAP_NAME


def test_name_is_truncated_to_a_sane_length() -> None:
    """超长名称要截断：否则会撞上文件系统的路径上限，报错发生在写入时。"""
    long_name = "北" * 500
    cleaned = sanitize_map_name(long_name)
    assert len(cleaned) == 80


def test_truncated_name_is_still_a_prefix_of_the_original() -> None:
    cleaned = sanitize_map_name("beijing" * 50)
    assert cleaned == ("beijing" * 50)[:80]


# --------------------------------------------------------------------------- #
# 不应被改动的内容
# --------------------------------------------------------------------------- #
def test_unicode_is_preserved() -> None:
    """中文、日文、emoji 都不是非法字符，必须原样保留（这是用户的名字）。"""
    for name in ("北京市", "東京都", "map_39.05N_116.05E", "地图-2024_final"):
        assert sanitize_map_name(name) == name


def test_non_string_input_is_coerced() -> None:
    """``str()`` 兜底：名称来自表单，理论上总是 str，但不该因为一个 int 就崩。"""
    assert sanitize_map_name(2024) == "2024"  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# build_output_path
# --------------------------------------------------------------------------- #
#: 用 conftest 的 ``work_dir`` 而不是 pytest 自带的 ``tmp_path``：本机的
#: ``pytest-of-<user>`` 根目录会被沙箱拦在 ``PermissionError: [WinError 5]`` 上
#: （见 conftest.py 里的说明）。
def test_build_output_path_joins_dir_and_sanitized_name(work_dir: Path) -> None:
    path = build_output_path(work_dir, "北京/2024", map_type=MapType.IMAGE)
    assert path == work_dir / "北京_2024_img.mbtiles"
    assert path.parent == work_dir


def test_build_output_path_uses_the_mbtiles_suffix(work_dir: Path) -> None:
    assert (
        build_output_path(work_dir, "beijing", map_type=MapType.VECTOR).suffix
        == ".mbtiles"
    )


def test_build_output_path_accepts_str_dir() -> None:
    path = build_output_path("D:/maps", "beijing", map_type=MapType.VECTOR)
    assert path == Path("D:/maps") / "beijing_vec.mbtiles"
    assert isinstance(path, Path)


def test_build_output_path_marks_the_map_type(work_dir: Path) -> None:
    """Slice 31：矢量与影像**不许落到同一个文件**。

    同一块地、同一段 Zoom 的两类瓦片坐标完全重合（都是 ``z/x/y``），旧命名
    只按"名称"取，于是"先下矢量再下影像"会撞在同一个 ``.mbtiles`` 上 ——
    续传校验拒绝、重下把前一份备份走，用户拿不到两份。
    """
    vector = build_output_path(work_dir, "beijing", map_type=MapType.VECTOR)
    image = build_output_path(work_dir, "beijing", map_type=MapType.IMAGE)
    assert vector != image
    assert vector.name == "beijing_vec.mbtiles"
    assert image.name == "beijing_img.mbtiles"


def test_build_output_path_marks_whether_labels_are_merged(work_dir: Path) -> None:
    """底图与"底图 + 中文注记"同样不可混写，名字也得分开。"""
    plain = build_output_path(work_dir, "beijing", map_type=MapType.VECTOR)
    labeled = build_output_path(
        work_dir, "beijing", map_type=MapType.VECTOR, with_labels=True
    )
    assert plain.name == "beijing_vec.mbtiles"
    assert labeled.name == "beijing_vec_cn.mbtiles"


def test_build_output_path_rejects_an_unknown_map_type(work_dir: Path) -> None:
    """认不出来就炸，不猜 —— 猜错等于给别人准备好一个会覆盖同名文件的路径。"""
    with pytest.raises(ValueError):
        build_output_path(work_dir, "beijing", map_type="img_wrong")


def test_build_output_path_is_stable(work_dir: Path) -> None:
    """同样的输入永远得到同样的路径——"已有文件"的确认框依赖这一点。"""
    assert build_output_path(
        work_dir, "beijing", map_type=MapType.VECTOR
    ) == build_output_path(work_dir, "beijing", map_type=MapType.VECTOR)
