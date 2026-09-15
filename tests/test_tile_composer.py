"""底图 + 中文注记的瓦片合成（Slice 6，Spec §4 / §6）。

这一层的输入输出都是**内存里的字节**，不碰网络也不碰磁盘，因此可以放心地
逐条断言像素结果。最关键的一条是：**注记只在它自己不透明的地方覆盖底图**，
透明处必须原样保留底图 —— 这条错了，用户会看到"地图被挖了洞"。
"""

from __future__ import annotations

import io
import logging

import pytest
from PIL import Image

from app.composer import TileComposeError, TileComposer
from app.config.settings import TILE_SIZE
from app.providers import PNG_SIGNATURE

#: 底图：纯红不透明。注记：纯蓝、只有中间一小块是不透明的。
BASE_COLOR = (255, 0, 0, 255)
LABEL_COLOR = (0, 0, 255, 255)


def _png(
    size: int | tuple[int, int] = TILE_SIZE,
    color: tuple[int, int, int, int] = BASE_COLOR,
    *,
    format: str = "PNG",
    mode: str = "RGBA",
) -> bytes:
    """造一张真图片（合成要用 Pillow 打开，文件头伪造的数据在这里不够用）。"""
    if isinstance(size, int):
        size = (size, size)
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format=format)
    return buffer.getvalue()


def _label_with_mark(
    size: int = TILE_SIZE,
    *,
    box: tuple[int, int, int, int] | None = None,
    opaque: bool = True,
) -> bytes:
    """一张"注记"：整块透明，中间一小块（默认半径 8 的方块）按需不透明。"""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    left, top, right, bottom = box or (size // 2 - 4, size // 2 - 4, size // 2 + 4, size // 2 + 4)
    image.paste(LABEL_COLOR if opaque else (0, 0, 0, 0), (left, top, right, bottom))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGBA")


# --------------------------------------------------------------------------- #
# 正常合成
# --------------------------------------------------------------------------- #
def test_compose_returns_png_of_tile_size() -> None:
    """产物必须是 PNG，且尺寸与输入一致（osmdroid 按 256×256 排版）。"""
    merged = TileComposer().compose(_png(), _label_with_mark())

    assert merged.startswith(PNG_SIGNATURE)
    with _open(merged) as image:
        assert image.size == (TILE_SIZE, TILE_SIZE)
        assert image.mode == "RGBA"


def test_label_only_covers_where_it_is_opaque() -> None:
    """注记的透明区域必须**保留底图** —— 这是 alpha 合成的全部意义。

    用左右两半分工来验证：左半透明（应看到底图的红），右半不透明（应看到注记的蓝）。
    """
    half = TILE_SIZE // 2
    label = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
    label.paste(LABEL_COLOR, (half, 0, TILE_SIZE, TILE_SIZE))
    buffer = io.BytesIO()
    label.save(buffer, format="PNG")

    merged = _open(TileComposer().compose(_png(color=BASE_COLOR), buffer.getvalue()))

    assert merged.getpixel((half // 2, half)) == BASE_COLOR  # 左半：底图
    assert merged.getpixel((half + half // 2, half)) == LABEL_COLOR  # 右半：注记
    merged.close()


def test_fully_transparent_label_keeps_base_untouched() -> None:
    """全透明注记（服务端在该处没有文字）等于底图原样。"""
    merged = _open(TileComposer().compose(_png(), _label_with_mark(opaque=False)))

    assert merged.getpixel((TILE_SIZE // 2, TILE_SIZE // 2)) == BASE_COLOR
    merged.close()


def test_semi_transparent_label_blends_with_base() -> None:
    """半透明注记要与底图混色，而不是二选一。

    用 50% 蓝压在纯红上：R 通道减半、B 通道约一半，G 保持 0。
    """
    label = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 255, 128))
    buffer = io.BytesIO()
    label.save(buffer, format="PNG")

    merged = _open(TileComposer().compose(_png(), buffer.getvalue()))
    red, green, blue, alpha = merged.getpixel((10, 10))

    assert alpha == 255
    assert green == 0
    assert 0 < blue < 200 and 60 < red < 200  # 两边都被"拉向"中间
    merged.close()


def test_jpeg_label_is_accepted() -> None:
    """注记也可能是 JPEG（服务端按 format 参数返回）——一样要能合。"""
    merged = TileComposer().compose(_png(), _png(format="JPEG", mode="RGB"))

    assert merged.startswith(PNG_SIGNATURE)


def test_composer_is_reusable_without_state_leak() -> None:
    """同一个实例连续合成多张，结果必须一致（合成几万张靠的就是这一点）。"""
    composer = TileComposer()
    base, label = _png(), _label_with_mark()

    first = composer.compose(base, label)
    second = composer.compose(base, label)

    assert first == second


# --------------------------------------------------------------------------- #
# 失败路径
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("base", "label", "keyword"),
    [
        (b"", _label_with_mark(), "底图瓦片数据为空"),
        (_png(), b"", "注记瓦片数据为空"),
        (b"not-an-image" * 10, _label_with_mark(), "底图瓦片不是可识别的图片"),
        (_png(), b"not-an-image" * 10, "注记瓦片不是可识别的图片"),
    ],
)
def test_bad_payload_raises_with_role_in_message(
    base: bytes, label: bytes, keyword: str
) -> None:
    """报错必须说清是**哪一层**出的问题（三类失败原因要分开计数，Spec §7）。"""
    with pytest.raises(TileComposeError) as excinfo:
        TileComposer().compose(base, label)

    assert keyword in str(excinfo.value)


def test_mismatched_sizes_are_rejected() -> None:
    """两层尺寸不一致说明服务端给错了东西 —— 合成出来必然是错位的图。"""
    with pytest.raises(TileComposeError) as excinfo:
        TileComposer().compose(_png(256), _png(512, (0, 0, 255, 255)))

    message = str(excinfo.value)
    assert "尺寸不一致" in message
    assert "256" in message and "512" in message


def test_non_bytes_payload_is_rejected() -> None:
    """传了字符串/None 之类的"不是字节"的东西，也要给一句人话。"""
    with pytest.raises(TileComposeError) as excinfo:
        TileComposer().compose("base", _label_with_mark())  # type: ignore[arg-type]

    assert "必须是 bytes" in str(excinfo.value)


def test_unexpected_size_warns_but_still_composes(caplog) -> None:
    """尺寸偏离预期但两层一致时不阻断：一次抖动不该让几万张任务全盘失败。"""
    composer = TileComposer(tile_size=256, strict_tile_size=False)

    with caplog.at_level(logging.WARNING):
        merged = composer.compose(_png(512), _png(512, (0, 0, 255, 255)))

    assert merged.startswith(PNG_SIGNATURE)
    assert "与预期 256×256 不符" in caplog.text


def test_strict_tile_size_rejects_unexpected_size() -> None:
    """严格模式下同样的情况直接报错（给"必须排查"的场景留的开关）。"""
    composer = TileComposer(tile_size=256, strict_tile_size=True)

    with pytest.raises(TileComposeError) as excinfo:
        composer.compose(_png(512), _png(512, (0, 0, 255, 255)))

    assert "与预期 256×256 不符" in str(excinfo.value)


def test_size_tolerance_allows_small_deviation() -> None:
    """容差范围内的尺寸偏差只告警，不报错（常量留出来就是给这种情况用的）。"""
    composer = TileComposer(tile_size=256, size_tolerance=2, strict_tile_size=True)

    merged = composer.compose(_png(257), _png(257, (0, 0, 255, 255)))

    assert merged.startswith(PNG_SIGNATURE)


# --------------------------------------------------------------------------- #
# 构造参数
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("tile_size", "tolerance"), [(0, 0), (-1, 0), (256, -1)])
def test_invalid_construction_is_rejected(tile_size: int, tolerance: int) -> None:
    with pytest.raises(ValueError):
        TileComposer(tile_size=tile_size, size_tolerance=tolerance)


def test_output_format_is_always_png() -> None:
    """合成产物的格式不可配置（Spec §4：JPEG 存不下 alpha）。"""
    assert TileComposer().output_format == "PNG"
    assert TileComposer().tile_size == TILE_SIZE
