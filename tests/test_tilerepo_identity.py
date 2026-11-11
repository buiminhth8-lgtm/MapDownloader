"""资产身份、坐标与内容摘要（Slice 42）。

这一层是纯计算，测的是"什么叫一张合法瓦片"和"什么算同一个坐标" —— 两条定义
一旦漂移，上层所有幂等/冲突判定都会跟着错，而且错得很安静。
"""

from __future__ import annotations

import hashlib

import pytest

from app.tilerepo import (
    AssetId,
    AssetKind,
    ContentDigest,
    CoordKey,
    CoordState,
    InvalidTile,
    classify_asset_kind,
    make_digest,
    validate_tile_bytes,
)
from tests.tilerepo_fakes import jpeg_bytes, png_bytes


# --------------------------------------------------------------------------- #
# 坐标
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("z", "x", "y"),
    [(0, 0, 0), (1, 1, 1), (5, 31, 0), (12, 0, 4095), (22, 4194303, 4194303)],
)
def test_coord_key_accepts_boundary_values(z: int, x: int, y: int) -> None:
    coord = CoordKey(z, x, y)
    assert coord.as_tuple() == (z, x, y)
    assert coord.tile_count == 2**z


@pytest.mark.parametrize(
    ("z", "x", "y"),
    [
        (-1, 0, 0),
        (23, 0, 0),
        (5, 32, 0),
        (5, 0, 32),
        (0, 1, 0),
    ],
)
def test_coord_key_rejects_out_of_range(z: int, x: int, y: int) -> None:
    with pytest.raises(ValueError):
        CoordKey(z, x, y)


@pytest.mark.parametrize(("z", "x", "y"), [(True, 0, 0), (5.0, 0, 0), (5, "1", 0)])
def test_coord_key_rejects_non_int(z: object, x: object, y: object) -> None:
    """``True`` 是 ``int`` 的子类，混进坐标会得到"看起来对"的瓦片。"""
    with pytest.raises(ValueError):
        CoordKey(z, x, y)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 资产身份
# --------------------------------------------------------------------------- #
def test_asset_id_text_and_role() -> None:
    base = AssetId("tianditu", "vec_w")
    label = AssetId("tianditu", "cva_w")
    assert base.text == "tianditu/vec_w"
    assert base.role == "base"
    assert label.role == "label"
    assert base.is_label is False and label.is_label is True


def test_asset_id_parse_round_trip() -> None:
    assert AssetId.parse("tianditu/cva_w") == AssetId("tianditu", "cva_w")
    assert AssetId.try_parse("乱写的") is None
    assert AssetId.try_parse(None) is None


@pytest.mark.parametrize(
    "value",
    ["tianditu", "tianditu/", "/vec_w", "tianditu/vec_w/x", "tianditu/vec", "tianditu/img_w2"],
)
def test_asset_id_parse_rejects_malformed(value: str) -> None:
    with pytest.raises(ValueError):
        AssetId.parse(value)


def test_source_id_cannot_contain_slash() -> None:
    with pytest.raises(ValueError):
        AssetId("tian/ditu", "vec_w")


def test_base_and_label_stay_in_separate_assets() -> None:
    """底图与注记在同一坐标上也必须落在不同资产（不同分片）里。"""
    assert AssetId("tianditu", "vec_w") != AssetId("tianditu", "cva_w")


# --------------------------------------------------------------------------- #
# 内容摘要与校验
# --------------------------------------------------------------------------- #
def test_digest_detects_encoding_and_size() -> None:
    png = png_bytes()
    digest = make_digest(png)
    assert digest.encoding == "png"
    assert digest.size == len(png)
    assert digest.sha256 == hashlib.sha256(png).digest()
    assert len(digest.hex) == 64

    jpeg = jpeg_bytes()
    assert make_digest(jpeg).encoding == "jpg"


def test_digest_rejects_non_image_bytes() -> None:
    with pytest.raises(ValueError):
        make_digest(b"<html>error</html>")


def test_validate_tile_bytes_accepts_a_real_tile() -> None:
    digest = validate_tile_bytes(png_bytes())
    assert digest.size > 0


@pytest.mark.parametrize(
    ("data", "hint"),
    [
        (b"", "空"),
        (b"<html>404</html>", "magic"),
        (png_bytes(size=(128, 128)), "尺寸"),
        (jpeg_bytes(size=(512, 512)), "尺寸"),
    ],
)
def test_validate_tile_bytes_rejects(data: bytes, hint: str) -> None:
    with pytest.raises(InvalidTile):
        validate_tile_bytes(data)


def test_deep_validation_catches_a_truncated_png() -> None:
    """magic 与头部尺寸都对、压缩流坏掉的 PNG —— 只有深检能挡住它。"""
    broken = png_bytes()[:-100]
    assert validate_tile_bytes(broken).encoding == "png"  # 快检放过
    with pytest.raises(InvalidTile):
        validate_tile_bytes(broken, deep=True)


def test_tile_size_is_configurable() -> None:
    small = png_bytes(size=(64, 64))
    assert validate_tile_bytes(small, tile_size=64).size == len(small)
    with pytest.raises(InvalidTile):
        validate_tile_bytes(small, tile_size=256)


# --------------------------------------------------------------------------- #
# 状态与资产类型
# --------------------------------------------------------------------------- #
def test_coord_state_values_are_the_persisted_contract() -> None:
    assert {item.name: item.value for item in CoordState} == {
        "CURRENT": 0,
        "SUPERSEDED": 1,
        "QUARANTINED": 2,
    }
    assert CoordState.parse(1) is CoordState.SUPERSEDED
    with pytest.raises(ValueError):
        CoordState.parse(9)


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"composition": "merged", "labels": "true", "map_type": "vector"}, AssetKind.SYNTHETIC_MERGED),
        ({"composition": "single", "labels": "false", "map_type": "image"}, AssetKind.SYNTHETIC_SINGLE),
        ({}, AssetKind.UNKNOWN),
        (None, AssetKind.UNKNOWN),
        # 契约 §3.2 的判定顺序：composition+labels 能定性就不再看 map_type
        ({"composition": "single", "labels": "false"}, AssetKind.SYNTHETIC_SINGLE),
        # 缺 composition / map_type → 判不出来，不猜
        ({"labels": "true", "map_type": "vector"}, AssetKind.UNKNOWN),
        ({}, AssetKind.UNKNOWN),
        # 组合认得但语义奇怪（merged + 无注记）→ 兜底当合成，不当原始
        ({"composition": "merged", "labels": "false", "map_type": "vector"}, AssetKind.SYNTHETIC_MERGED),
    ],
)
def test_classify_asset_kind(metadata: dict | None, expected: AssetKind) -> None:
    assert classify_asset_kind(metadata) is expected


def test_asset_kind_helpers() -> None:
    assert AssetKind.RAW_BASE.is_raw and AssetKind.RAW_LABEL.is_raw
    assert AssetKind.SYNTHETIC_MERGED.is_synthetic
    assert AssetKind.UNKNOWN.is_raw is False


def test_content_digest_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError):
        ContentDigest(b"short", 10, "png")
    with pytest.raises(ValueError):
        ContentDigest(b"0" * 32, 0, "png")
    with pytest.raises(ValueError):
        ContentDigest(b"0" * 32, 10, "webp")
