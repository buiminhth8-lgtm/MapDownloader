"""MBTiles 校验器单元测试（Slice 4）。

用**手工构造的库**（包括各种坏库）覆盖校验器的每条分支——比"造一个真库再破坏它"
更可控，也更容易看清每个结论对应的是哪条规则。

校验器有一条硬性契约：**任何输入都不许抛异常**，只能通过 ``errors`` 反映问题。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.geo import tms_y_to_xyz_y
from app.mbtiles import MBTilesValidationResult, validate_mbtiles
from app.providers import JPEG_SIGNATURE, PNG_SIGNATURE

FAKE_PNG = PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + b"tile-payload" * 8
FAKE_JPEG = JPEG_SIGNATURE + b"\xff\xe0" + b"jpeg-payload" * 8

#: 一份完整的 metadata（8 个标准键）
GOOD_METADATA: list[tuple[str, str]] = [
    ("name", "beijing"),
    ("type", "baselayer"),
    ("version", "1.0"),
    ("description", "校验测试"),
    ("format", "png"),
    ("bounds", "116.000000,39.000000,116.500000,39.500000"),
    ("minzoom", "8"),
    ("maxzoom", "12"),
]

_TILES_DDL = """
CREATE TABLE tiles (
    zoom_level INTEGER NOT NULL,
    tile_column INTEGER NOT NULL,
    tile_row INTEGER NOT NULL,
    tile_data BLOB NOT NULL,
    PRIMARY KEY (zoom_level, tile_column, tile_row)
)
"""


def _make_db(
    path: Path,
    *,
    metadata: list[tuple[str, str]] | None = None,
    tiles: list[tuple[int, int, int, bytes]] | None = None,
    metadata_table: bool = True,
    tiles_table: bool = True,
) -> Path:
    """手工造库，允许任意组合"有表/有行"。"""
    conn = sqlite3.connect(str(path))
    try:
        if metadata_table:
            conn.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
            conn.executemany(
                "INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)",
                metadata if metadata is not None else GOOD_METADATA,
            )
        if tiles_table:
            conn.execute(_TILES_DDL)
            conn.executemany(
                "INSERT OR REPLACE INTO tiles "
                "(zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)",
                tiles if tiles is not None else [],
            )
        conn.commit()
    finally:
        conn.close()
    return path


# --------------------------------------------------------------------------- #
# 正常文件
# --------------------------------------------------------------------------- #
def test_valid_mbtiles_passes(work_dir: Path) -> None:
    db = _make_db(
        work_dir / "good.mbtiles",
        tiles=[(8, 1, 1, FAKE_PNG), (12, 3367, 2095, FAKE_PNG)],
    )

    result = validate_mbtiles(db)

    assert result.valid is True
    assert result.errors == []
    assert result.tile_count == 2
    assert (result.min_zoom, result.max_zoom) == (8, 12)
    assert result.file_size > 0
    assert result.status_text == "PASS"
    assert result.zoom_text == "8~12"
    # 抽样提示是 warning 而不是 error —— 它不影响可用性
    assert any("已抽样检查" in text for text in result.warnings)


def test_validator_accepts_jpeg_tiles(work_dir: Path) -> None:
    db = _make_db(
        work_dir / "jpeg.mbtiles",
        metadata=[("name", "x"), ("format", "jpg"), ("bounds", "0,0,1,1"),
                  ("minzoom", "8"), ("maxzoom", "8")],
        tiles=[(8, 1, 1, FAKE_JPEG)],
    )

    result = validate_mbtiles(db)

    assert result.valid is True
    assert result.errors == []


def test_missing_optional_metadata_is_only_a_warning(work_dir: Path) -> None:
    db = _make_db(
        work_dir / "minimal.mbtiles",
        metadata=[("name", "x"), ("format", "png"), ("bounds", "0,0,1,1"),
                  ("minzoom", "8"), ("maxzoom", "8")],
        tiles=[(8, 0, 0, FAKE_PNG)],
    )

    result = validate_mbtiles(db)

    assert result.valid is True
    assert result.errors == []
    assert any("type" in text for text in result.warnings)


def test_sample_limit_is_respected(work_dir: Path) -> None:
    db = _make_db(
        work_dir / "many.mbtiles",
        tiles=[(3, i, 0, FAKE_PNG) for i in range(6)],
    )

    result = validate_mbtiles(db, sample_limit=2)

    assert result.valid is True
    assert any("已抽样检查 2 张" in text for text in result.warnings)


# --------------------------------------------------------------------------- #
# 文件级故障
# --------------------------------------------------------------------------- #
def test_missing_file(work_dir: Path) -> None:
    result = validate_mbtiles(work_dir / "nope.mbtiles")

    assert result.valid is False
    assert any("文件不存在" in text for text in result.errors)
    assert result.file_size == 0


def test_directory_instead_of_file(work_dir: Path) -> None:
    directory = work_dir / "a-directory"
    directory.mkdir()

    result = validate_mbtiles(directory)

    assert result.valid is False
    assert any("目录" in text for text in result.errors)


def test_not_a_sqlite_file(work_dir: Path) -> None:
    bogus = work_dir / "bogus.mbtiles"
    bogus.write_bytes(b"not a database" * 32)

    result = validate_mbtiles(bogus)

    assert result.valid is False
    assert any("SQLite" in text for text in result.errors)
    assert result.tile_count == 0


def test_validate_never_raises_on_broken_input(work_dir: Path) -> None:
    """契约：任何输入都返回结论，不抛异常。"""
    bogus = work_dir / "empty.mbtiles"
    bogus.write_bytes(b"")

    result = validate_mbtiles(bogus)

    assert isinstance(result, MBTilesValidationResult)
    assert result.valid is False
    assert result.errors


# --------------------------------------------------------------------------- #
# 表结构故障
# --------------------------------------------------------------------------- #
def test_missing_metadata_table(work_dir: Path) -> None:
    db = _make_db(
        work_dir / "no-metadata.mbtiles",
        metadata_table=False,
        tiles=[(8, 1, 1, FAKE_PNG)],
    )

    result = validate_mbtiles(db)

    assert result.valid is False
    assert any("缺少 metadata 表" in text for text in result.errors)


def test_missing_tiles_table(work_dir: Path) -> None:
    db = _make_db(work_dir / "no-tiles.mbtiles", tiles_table=False)

    result = validate_mbtiles(db)

    assert result.valid is False
    assert any("缺少 tiles 表" in text for text in result.errors)


def test_empty_tiles_table(work_dir: Path) -> None:
    db = _make_db(work_dir / "empty-tiles.mbtiles", tiles=[])

    result = validate_mbtiles(db)

    assert result.valid is False
    assert result.tile_count == 0
    assert any("tiles 表为空" in text for text in result.errors)


def test_empty_metadata_table(work_dir: Path) -> None:
    db = _make_db(
        work_dir / "empty-meta.mbtiles",
        metadata=[],
        tiles=[(8, 1, 1, FAKE_PNG)],
    )

    result = validate_mbtiles(db)

    assert result.valid is False
    assert any("metadata 表为空" in text for text in result.errors)


# --------------------------------------------------------------------------- #
# metadata 内容故障
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("missing", ["name", "format", "bounds", "minzoom", "maxzoom"])
def test_missing_required_metadata_key(work_dir: Path, missing: str) -> None:
    db = _make_db(
        work_dir / f"missing-{missing}.mbtiles",
        metadata=[row for row in GOOD_METADATA if row[0] != missing],
        tiles=[(8, 1, 1, FAKE_PNG)],
    )

    result = validate_mbtiles(db)

    assert result.valid is False
    assert any(f"缺少必备字段：{missing}" in text for text in result.errors)


def test_unparsable_bounds(work_dir: Path) -> None:
    db = _make_db(
        work_dir / "bad-bounds.mbtiles",
        metadata=[(k, "not,a,bounds") if k == "bounds" else (k, v)
                  for k, v in GOOD_METADATA],
        tiles=[(8, 1, 1, FAKE_PNG)],
    )

    result = validate_mbtiles(db)

    assert result.valid is False
    assert any("bounds 无法解析" in text for text in result.errors)


def test_inverted_bounds_is_a_warning(work_dir: Path) -> None:
    db = _make_db(
        work_dir / "inverted-bounds.mbtiles",
        metadata=[(k, "116.5,39.5,116.0,39.0") if k == "bounds" else (k, v)
                  for k, v in GOOD_METADATA],
        tiles=[(8, 1, 1, FAKE_PNG)],
    )

    result = validate_mbtiles(db)

    assert result.valid is True
    assert any("不是有效范围" in text for text in result.warnings)


def test_non_integer_zoom(work_dir: Path) -> None:
    db = _make_db(
        work_dir / "bad-zoom.mbtiles",
        metadata=[(k, "eight") if k == "minzoom" else (k, v) for k, v in GOOD_METADATA],
        tiles=[(8, 1, 1, FAKE_PNG)],
    )

    result = validate_mbtiles(db)

    assert result.valid is False
    assert any("minzoom 不是整数" in text for text in result.errors)


def test_zoom_out_of_range(work_dir: Path) -> None:
    db = _make_db(
        work_dir / "zoom-too-big.mbtiles",
        metadata=[(k, "30") if k == "maxzoom" else (k, v) for k, v in GOOD_METADATA],
        tiles=[(8, 1, 1, FAKE_PNG)],
    )

    result = validate_mbtiles(db)

    assert result.valid is False
    assert any("maxzoom 超出" in text for text in result.errors)


def test_minzoom_greater_than_maxzoom(work_dir: Path) -> None:
    db = _make_db(
        work_dir / "inverted-zoom.mbtiles",
        metadata=[(k, "12" if k == "minzoom" else "8" if k == "maxzoom" else v)
                  for k, v in GOOD_METADATA],
        tiles=[(8, 1, 1, FAKE_PNG)],
    )

    result = validate_mbtiles(db)

    assert result.valid is False
    assert any("minzoom > maxzoom" in text for text in result.errors)


# --------------------------------------------------------------------------- #
# tiles 内容故障
# --------------------------------------------------------------------------- #
def test_empty_tile_data(work_dir: Path) -> None:
    db = _make_db(work_dir / "empty-blob.mbtiles", tiles=[(8, 1, 1, b"")])

    result = validate_mbtiles(db)

    assert result.valid is False
    assert any("tile_data 为空" in text for text in result.errors)


def test_invalid_tile_data(work_dir: Path) -> None:
    """html 错误页被当成瓦片写进库 —— 必须报错，而不是"能打开就算过"。"""
    db = _make_db(
        work_dir / "html-tile.mbtiles",
        tiles=[(8, 1, 1, b"<html>403 Forbidden</html>")],
    )

    result = validate_mbtiles(db)

    assert result.valid is False
    assert any("不是合法图片" in text for text in result.errors)
    # 错误信息里的坐标是 **XYZ**（与全链路一致），不是库里的 TMS 行号
    xyz_y = tms_y_to_xyz_y(8, 1)
    assert any(f"8/1/{xyz_y}" in text for text in result.errors)


def test_mixed_good_and_bad_samples_report_bad_ones(work_dir: Path) -> None:
    db = _make_db(
        work_dir / "mixed.mbtiles",
        tiles=[
            (8, 0, 0, FAKE_PNG),
            (8, 1, 0, b"garbage"),
            (8, 2, 0, FAKE_PNG),
        ],
    )

    result = validate_mbtiles(db)

    assert result.valid is False
    bad = [text for text in result.errors if "不是合法图片" in text]
    assert len(bad) == 1
    assert f"8/1/{tms_y_to_xyz_y(8, 0)}" in bad[0]


def test_format_mismatch_is_a_warning(work_dir: Path) -> None:
    """声明 png 但抽样全是 jpeg：不影响加载，但要提醒。"""
    db = _make_db(
        work_dir / "mismatch.mbtiles",
        tiles=[(8, 1, 1, FAKE_JPEG)],
    )

    result = validate_mbtiles(db)

    assert result.valid is True
    assert any("格式" in text for text in result.warnings)


def test_out_of_range_tile_row(work_dir: Path) -> None:
    """tile_row 越界（文件被写坏）必须体现在 errors 里。"""
    db = _make_db(work_dir / "bad-row.mbtiles", tiles=[(3, 0, 999, FAKE_PNG)])

    result = validate_mbtiles(db)

    assert result.valid is False
    assert any("非法瓦片编号" in text for text in result.errors)


# --------------------------------------------------------------------------- #
# 结果模型
# --------------------------------------------------------------------------- #
def test_result_as_lines_contains_key_fields() -> None:
    result = MBTilesValidationResult(
        valid=False,
        tile_count=1234,
        min_zoom=8,
        max_zoom=16,
        file_size=2048,
        errors=["tiles 表为空（没有任何瓦片）"],
        warnings=["metadata 缺少可选字段：type"],
    )

    lines = result.as_lines()

    assert "验证：FAIL" in lines
    assert "瓦片：1,234" in lines
    assert "Zoom：8~16" in lines
    assert any(line.startswith("错误：") for line in lines)
    assert any(line.startswith("提示：") for line in lines)


def test_result_zoom_text_placeholder() -> None:
    result = MBTilesValidationResult(valid=False, min_zoom=None, max_zoom=None)

    assert result.zoom_text == "—"
    assert result.status_text == "FAIL"
