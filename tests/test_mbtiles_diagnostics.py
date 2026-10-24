"""MBTiles 诊断规则单元测试（Slice 8）。

这个文件测的是"**这份文件算不算有问题**"—— 全部规则都住在
:mod:`app.mbtiles.diagnostics` 里，所以这里既不装 GUI，也不碰 QThread：
手工造库 → 跑 ``MBTilesInspectionService.inspect()`` → 断言结构化结论。

两条贯穿全篇的契约：

1. **检查工具绝不抛异常**。任何输入（空文件、不是 SQLite、缺表、坏图片）
   都只能体现在 ``errors`` / ``warnings`` 里；
2. **PASS 是可达的**。一个正常文件必须能拿到 ``PASS``，而不是被
   "已抽样检查 N 张瓦片" 这种说明性文字抬成 ``WARNING``（Spec §20）。
"""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from app.config.settings import (
    DIAGNOSTICS_DEFAULT_SAMPLE_SIZE,
    DIAGNOSTICS_MAX_SAMPLE_SIZE,
    DIAGNOSTICS_MIN_SAMPLE_SIZE,
    MAX_MISSING_TILE_DETAILS,
)
from app.geo import GeoBounds, calculate_tile_range, tms_y_to_xyz_y, xyz_y_to_tms_y
from app.mbtiles import MBTilesDiagnosticsResult, ZoomTileStats, render_report
from app.mbtiles.diagnostics import (
    ImageProbe,
    InspectionProgress,
    probe_image,
)
from app.services import MBTilesInspectionService


# --------------------------------------------------------------------------- #
# 夹具：真的图片 + 手工造的库
# --------------------------------------------------------------------------- #
def _png(width: int = 16, height: int = 16) -> bytes:
    """一张能被 Pillow 真正解码的 PNG。"""
    buffer = io.BytesIO()
    Image.new("RGBA", (width, height), (200, 30, 30, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg(width: int = 16, height: int = 16) -> bytes:
    """一张能被 Pillow 真正解码的 JPEG。"""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (30, 200, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


#: "PNG 文件头 + 垃圾正文"：magic bytes 认得出，但 Pillow 解码必炸。
#: 这正是"只看 magic bytes 的旧校验"抓不到、而 Slice 8 必须抓到的那一类损坏。
BROKEN_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"garbage" * 16

GOOD_BOUNDS = (116.0, 39.0, 116.5, 39.5)

GOOD_METADATA: list[tuple[str, str]] = [
    ("name", "beijing"),
    ("type", "baselayer"),
    ("version", "1.0"),
    ("description", "诊断测试"),
    ("format", "png"),
    ("bounds", "116.000000,39.000000,116.500000,39.500000"),
    ("minzoom", "8"),
    ("maxzoom", "10"),
]

_TILES_DDL_WITH_PK = """
CREATE TABLE tiles (
    zoom_level INTEGER NOT NULL,
    tile_column INTEGER NOT NULL,
    tile_row INTEGER NOT NULL,
    tile_data BLOB NOT NULL,
    PRIMARY KEY (zoom_level, tile_column, tile_row)
)
"""

#: 没有主键的 tiles 表 —— 专门用来造"重复坐标"这种坏库。
_TILES_DDL_NO_PK = """
CREATE TABLE tiles (
    zoom_level INTEGER NOT NULL,
    tile_column INTEGER NOT NULL,
    tile_row INTEGER NOT NULL,
    tile_data BLOB NOT NULL
)
"""


def _make_db(
    path: Path,
    *,
    metadata: list[tuple[str, str]] | None = None,
    tiles: list[tuple[int, int, int, bytes]] | None = None,
    metadata_table: bool = True,
    tiles_table: bool = True,
    tiles_ddl: str = _TILES_DDL_WITH_PK,
) -> Path:
    """手工造库。

    Args:
        tiles: ``(zoom, x, y_xyz, data)`` —— **坐标一律 XYZ**，入库前在
            这里翻成 TMS。测试里写 XYZ 是为了跟"对外一律 XYZ"的约定保持一致，
            也避免测试自己把翻转写错。
    """
    conn = sqlite3.connect(str(path))
    try:
        if metadata_table:
            conn.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
            conn.executemany(
                "INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)",
                metadata if metadata is not None else GOOD_METADATA,
            )
        if tiles_table:
            conn.execute(tiles_ddl)
            for zoom, x, y_xyz, data in tiles or []:
                conn.execute(
                    "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) "
                    "VALUES (?, ?, ?, ?)",
                    (zoom, x, xyz_y_to_tms_y(zoom, y_xyz), data),
                )
        conn.commit()
    finally:
        conn.close()
    return path


def _fill_range(
    path: Path,
    *,
    metadata: list[tuple[str, str]] | None = None,
    zooms: tuple[int, ...] = (8, 9, 10),
    global_bounds: tuple[float, float, float, float] = GOOD_BOUNDS,
    data: bytes | None = None,
    drop: set[tuple[int, int, int]] | None = None,
    override: dict[tuple[int, int, int], bytes] | None = None,
) -> tuple[Path, int]:
    """造一个**覆盖完整**的库（按 ``calculate_tile_range`` 逐格填满）。

    期望集合与覆盖检查用的是同一套 ``calculate_tile_range``——测试自己
    推算瓦片区间才是"测出来的东西跟生产不是一回事"的根源。

    Returns:
        ``(路径, 期望瓦片总数)``。
    """
    payload = data if data is not None else _png()
    drop = drop or set()
    override = override or {}
    bounds = GeoBounds(*global_bounds)
    tiles: list[tuple[int, int, int, bytes]] = []
    expected = 0
    for zoom in zooms:
        tile_range = calculate_tile_range(bounds, zoom)
        for x in range(tile_range.min_x, tile_range.max_x + 1):
            for y_xyz in range(tile_range.min_y, tile_range.max_y + 1):
                expected += 1
                if (zoom, x, y_xyz) in drop:
                    continue
                tiles.append((zoom, x, y_xyz, override.get((zoom, x, y_xyz), payload)))
    return _make_db(path, metadata=metadata, tiles=tiles), expected


def _inspect(path: Path, **kwargs) -> MBTilesDiagnosticsResult:
    """跑一次检查；默认关掉深度扫描（慢）与覆盖检查（除非用例需要）。"""
    kwargs.setdefault("check_coverage", False)
    kwargs.setdefault("deep_scan", False)
    return MBTilesInspectionService().inspect(path, **kwargs)


# --------------------------------------------------------------------------- #
# 正常文件 —— PASS 必须可达
# --------------------------------------------------------------------------- #
def test_valid_mbtiles_passes(work_dir: Path) -> None:
    """一个结构与内容都正常的库必须判 PASS，而不是被说明性文字抬成 WARNING。"""
    db, expected = _fill_range(work_dir / "good.mbtiles")
    result = _inspect(db, check_coverage=True)

    assert result.status == "PASS"
    assert result.valid is True
    assert result.errors == []
    assert result.tile_count == expected
    assert result.expected_tiles == expected
    assert result.missing_tiles == 0
    assert result.corrupt_tiles == 0
    assert result.empty_tiles == 0
    assert result.invalid_tiles == 0
    assert result.duplicate_tiles == 0
    assert result.coverage_checked is True
    assert result.min_zoom == 8
    assert result.max_zoom == 10
    assert result.zoom_text == "8~10"
    # "已抽样检查 N 张瓦片…" 那两条被归到 notices，绝不进 warnings
    assert result.warnings == []
    assert any("全部可正常解码" in note for note in result.notices)


def test_valid_result_status_text_alias(work_dir: Path) -> None:
    db, _ = _fill_range(work_dir / "alias.mbtiles")
    result = _inspect(db)
    assert result.status_text == result.status == "PASS"


def test_zoom_stats_shape(work_dir: Path) -> None:
    db, _ = _fill_range(work_dir / "zoom.mbtiles")
    result = _inspect(db, check_coverage=True)

    assert [stats.zoom for stats in result.zoom_stats] == [8, 9, 10]
    for stats in result.zoom_stats:
        assert stats.tile_count > 0
        assert stats.expected_tiles == stats.tile_count
        assert stats.missing_tiles == 0
        assert stats.coverage_ok is True
        assert stats.x_count == stats.max_x - stats.min_x + 1
        assert stats.y_count == stats.max_y_tms - stats.min_y_tms + 1


def test_sqlite_check_recorded(work_dir: Path) -> None:
    db, _ = _fill_range(work_dir / "pragma.mbtiles")
    result = _inspect(db)
    assert result.sqlite_check is not None
    assert result.sqlite_check.strip().lower() == "ok"


def test_tables_and_columns_recorded(work_dir: Path) -> None:
    db, _ = _fill_range(work_dir / "schema.mbtiles")
    result = _inspect(db)
    assert set(result.tables) == {"metadata", "tiles"}
    for column in ("zoom_level", "tile_column", "tile_row", "tile_data"):
        assert column in result.tiles_columns


# --------------------------------------------------------------------------- #
# 文件 / 结构异常（Spec §3）
# --------------------------------------------------------------------------- #
def test_missing_file_fails(work_dir: Path) -> None:
    result = _inspect(work_dir / "nope.mbtiles")
    assert result.status == "FAIL"
    assert any("文件不存在" in message for message in result.errors)


def test_directory_path_fails(work_dir: Path) -> None:
    directory = work_dir / "a-directory"
    directory.mkdir()
    result = _inspect(directory)
    assert result.status == "FAIL"
    assert any("目录" in message for message in result.errors)


def test_empty_file_fails(work_dir: Path) -> None:
    db = work_dir / "empty.mbtiles"
    db.write_bytes(b"")
    result = _inspect(db)
    assert result.status == "FAIL"
    assert any("0 字节" in message for message in result.errors)


def test_not_sqlite_fails(work_dir: Path) -> None:
    db = work_dir / "garbage.mbtiles"
    db.write_bytes(b"this is definitely not a sqlite database" * 8)
    result = _inspect(db)
    assert result.status == "FAIL"
    assert any("不是合法的 SQLite 文件" in message for message in result.errors)


def test_missing_metadata_table_fails(work_dir: Path) -> None:
    db = _make_db(work_dir / "no-meta-table.mbtiles", metadata_table=False)
    result = _inspect(db)
    assert result.status == "FAIL"
    assert any("缺少 metadata 表" in message for message in result.errors)


def test_missing_tiles_table_fails(work_dir: Path) -> None:
    db = _make_db(work_dir / "no-tiles-table.mbtiles", tiles_table=False)
    result = _inspect(db)
    assert result.status == "FAIL"
    assert any("缺少 tiles 表" in message for message in result.errors)
    # 缺 tiles 表时不再往下跑坐标/图片，避免一串 "no such table" 噪音
    assert result.zoom_stats == []


def test_missing_tiles_column_fails(work_dir: Path) -> None:
    db = work_dir / "no-column.mbtiles"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)", GOOD_METADATA
        )
        # 少了 tile_data
        conn.execute(
            "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER)"
        )
        conn.commit()
    finally:
        conn.close()
    result = _inspect(db)
    assert result.status == "FAIL"
    assert any("缺少必需字段：tile_data" in message for message in result.errors)


# --------------------------------------------------------------------------- #
# metadata 诊断（Spec §5）
# --------------------------------------------------------------------------- #
def _metadata_with(**overrides: str) -> list[tuple[str, str]]:
    merged = dict(GOOD_METADATA)
    merged.update(overrides)
    return list(merged.items())


def test_missing_description_is_warning_not_fail(work_dir: Path) -> None:
    """Spec §20 的落点：少个 description 只该 WARNING，绝不能判 FAIL。"""
    metadata = [item for item in GOOD_METADATA if item[0] != "description"]
    db, _ = _fill_range(work_dir / "no-desc.mbtiles", metadata=metadata)
    result = _inspect(db)

    assert result.status == "WARNING"
    assert result.valid is True  # 仍然可用
    assert result.errors == []
    assert any("description" in message for message in result.warnings)


def test_unsupported_format_warns(work_dir: Path) -> None:
    """webp 是 MBTiles 规范允许、但本工具读不动的格式 → WARNING。"""
    db, _ = _fill_range(work_dir / "webp.mbtiles", metadata=_metadata_with(format="webp"))
    result = _inspect(db)
    assert result.status == "WARNING"
    assert any("无法直接读取" in message for message in result.warnings)


def test_bounds_longitude_out_of_range_fails(work_dir: Path) -> None:
    db, _ = _fill_range(
        work_dir / "bounds-lon.mbtiles",
        metadata=_metadata_with(bounds="190.000000,39.000000,200.000000,39.500000"),
    )
    result = _inspect(db)
    assert result.status == "FAIL"
    assert any("经度超出 ±180" in message for message in result.errors)


def test_bounds_latitude_out_of_range_fails(work_dir: Path) -> None:
    db, _ = _fill_range(
        work_dir / "bounds-lat.mbtiles",
        metadata=_metadata_with(bounds="116.000000,80.000000,117.000000,89.000000"),
    )
    result = _inspect(db)
    assert result.status == "FAIL"
    assert any("纬度超出 Web 墨卡托极限" in message for message in result.errors)


def test_declared_zoom_mismatch_warns(work_dir: Path) -> None:
    """metadata 说 8~10，库里只有 8~9 → WARNING（下载可能没跑完）。"""
    db, _ = _fill_range(
        work_dir / "zoom-mismatch.mbtiles",
        zooms=(8, 9),
        metadata=_metadata_with(maxzoom="10"),
    )
    result = _inspect(db)
    assert result.status == "WARNING"
    assert any("声明 maxzoom=10" in message for message in result.warnings)


# --------------------------------------------------------------------------- #
# 瓦片级异常（Spec §7 ~ §10）
# --------------------------------------------------------------------------- #
def test_corrupt_png_detected_by_sample(work_dir: Path) -> None:
    """magic bytes 对、正文是垃圾 —— 旧校验放行，Slice 8 必须抓到。"""
    db = _make_db(
        work_dir / "corrupt.mbtiles",
        metadata=_metadata_with(minzoom="8", maxzoom="8"),
        tiles=[(8, 1, 1, BROKEN_PNG)],
    )
    result = _inspect(db)
    assert result.status == "FAIL"
    assert result.sample_corrupt >= 1
    assert result.corrupt_tiles >= 1
    assert any("无法解码" in message for message in result.errors)


def test_empty_tile_data_fails(work_dir: Path) -> None:
    db = _make_db(
        work_dir / "empty-tile.mbtiles",
        metadata=_metadata_with(minzoom="8", maxzoom="8"),
        tiles=[(8, 1, 1, b"")],
    )
    result = _inspect(db)
    assert result.status == "FAIL"
    assert result.empty_tiles == 1
    assert any("tile_data 为空" in message for message in result.errors)


def test_sample_format_mismatch_warns(work_dir: Path) -> None:
    """metadata.format=png，抽样却全是 JPEG → WARNING（图片本身能解码）。"""
    db = _make_db(
        work_dir / "format-mismatch.mbtiles",
        metadata=_metadata_with(minzoom="8", maxzoom="8"),
        tiles=[(8, x, y, _jpeg()) for x in (1, 2) for y in (1, 2)],
    )
    result = _inspect(db)
    assert result.status == "WARNING"
    assert result.sample_corrupt == 0
    assert "JPEG" in result.sample_formats
    assert any("但抽样里出现了" in message for message in result.warnings)


def test_illegal_x_coordinate_fails(work_dir: Path) -> None:
    """``x`` 越界（``>= 2**z``）→ ERROR，并且列出时补上 XYZ 语义的 ``y``。"""
    db = _make_db(
        work_dir / "bad-x.mbtiles",
        metadata=_metadata_with(minzoom="8", maxzoom="8"),
        # z=8 的合法 range 是 0~255
        tiles=[(8, 999, 1, _png())],
    )
    result = _inspect(db)
    assert result.status == "FAIL"
    assert result.invalid_tiles == 1
    messages = "\n".join(result.errors)
    assert "坐标越界" in messages
    assert "column=999" in messages
    # 行号本身合法 → 必须能给出等价的 XYZ 行号
    assert "XYZ 的 y=" in messages


def test_illegal_tms_row_fails(work_dir: Path) -> None:
    """``tile_row``（TMS）越界 → ERROR。

    这条用例盯的是一个真实缺陷：越界行**无法**换算成 XYZ（``tms_y_to_xyz_y``
    按契约会抛 ``ValueError``），所以"列出越界样本"必须容错。修好之前，
    坐标检查会在格式化自己的结论时崩掉，用户只看到一句"坐标检查失败"，
    永远不知道是哪张瓦片坏了。
    """
    db = work_dir / "bad-row.mbtiles"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)",
            _metadata_with(minzoom="8", maxzoom="8"),
        )
        conn.execute(_TILES_DDL_NO_PK)
        conn.execute(
            "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) "
            "VALUES (?, ?, ?, ?)",
            (8, 1, 9999, _png()),
        )
        conn.commit()
    finally:
        conn.close()

    result = _inspect(db)
    assert result.status == "FAIL"
    assert result.invalid_tiles == 1
    messages = "\n".join(result.errors)
    assert "坐标越界" in messages
    assert "row=9999" in messages
    # 关键：这个阶段没有整体崩掉
    assert "坐标检查失败" not in messages
    # 坐标非法的行让图片抽样做不了 —— 但要说清楚原因，而不是甩一个原始异常
    assert any("无法进行图片抽样" in message for message in result.warnings)


def test_illegal_zoom_fails(work_dir: Path) -> None:
    """``zoom_level`` 超出 [0, 22] → ERROR（这种行连"合法上界"都无从定义）。"""
    db = work_dir / "bad-zoom.mbtiles"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)",
            _metadata_with(minzoom="8", maxzoom="8"),
        )
        conn.execute(_TILES_DDL_NO_PK)
        # 直接写 raw 行：z=99 传不进 xyz_y_to_tms_y（那个换算本身会拒绝非法 zoom）
        conn.execute(
            "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) "
            "VALUES (?, ?, ?, ?)",
            (99, 1, 1, _png()),
        )
        conn.commit()
    finally:
        conn.close()

    result = _inspect(db)
    assert result.status == "FAIL"
    assert result.invalid_tiles == 1
    assert any("zoom_level 超出" in message for message in result.errors)


def test_duplicate_tiles_detected(work_dir: Path) -> None:
    """没有主键的库可以塞进重复坐标 —— 不同客户端可能读到不同字节。"""
    db = work_dir / "dup.mbtiles"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)",
            _metadata_with(minzoom="8", maxzoom="8"),
        )
        conn.execute(_TILES_DDL_NO_PK)
        row = (8, 1, xyz_y_to_tms_y(8, 1), _png())
        conn.execute(
            "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) "
            "VALUES (?, ?, ?, ?)",
            row,
        )
        conn.execute(
            "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) "
            "VALUES (?, ?, ?, ?)",
            row,
        )
        conn.commit()
    finally:
        conn.close()

    result = _inspect(db)
    assert result.status == "FAIL"
    assert result.duplicate_tiles == 1
    assert any("重复坐标" in message for message in result.errors)


# --------------------------------------------------------------------------- #
# 覆盖完整性（Spec §12 ~ §14）
# --------------------------------------------------------------------------- #
#: ``GOOD_BOUNDS`` 在 z8 下**只映射到 1 张瓦片**（经纬度跨度不到一格）。
#: 想造"缺几张"的场景必须往上走几级 —— 用 z12 时同一范围的瓦片区间是 7×6。
COVERAGE_ZOOM = 12

_WIDE_BOUNDS = (116.0, 39.0, 117.0, 40.0)
"""1°×1°：z12 下约 12×15 张，专门用来测"缺失明细封顶"。"""


def test_coverage_complete_reports_notice(work_dir: Path) -> None:
    db, expected = _fill_range(work_dir / "cov-ok.mbtiles")
    result = _inspect(db, check_coverage=True)
    assert result.expected_tiles == expected
    assert result.actual_tiles_in_coverage == expected
    assert result.missing_tiles == 0
    assert result.missing_details == []
    assert any("无缺失" in note for note in result.notices)
    assert result.status == "PASS"


def test_coverage_missing_counts_and_xyz_details(work_dir: Path) -> None:
    """缺 3 张 → WARNING；明细是 XYZ 语义的 ``z/x/y``。"""
    tile_range = calculate_tile_range(GeoBounds(*GOOD_BOUNDS), COVERAGE_ZOOM)
    missing = {
        (COVERAGE_ZOOM, tile_range.min_x, tile_range.min_y),
        (COVERAGE_ZOOM, tile_range.min_x, tile_range.min_y + 1),
        (COVERAGE_ZOOM, tile_range.min_x, tile_range.min_y + 2),
    }
    db, _ = _fill_range(
        work_dir / "cov-missing.mbtiles",
        zooms=(COVERAGE_ZOOM,),
        metadata=_metadata_with(minzoom=str(COVERAGE_ZOOM), maxzoom=str(COVERAGE_ZOOM)),
        drop=missing,
    )
    result = _inspect(db, check_coverage=True)

    assert result.status == "WARNING"
    assert result.valid is True  # 缺失不判 FAIL（Spec §21）
    assert result.missing_tiles == 3
    assert result.missing_details_truncated == 0
    assert sorted(result.missing_details) == sorted(
        f"{COVERAGE_ZOOM}/{x}/{y}" for _, x, y in missing
    )
    assert any("覆盖完整性" in message for message in result.warnings)


def test_coverage_missing_is_never_fail(work_dir: Path) -> None:
    """缺得再多也只是 WARNING —— 结构与图片都有效时文件仍然局部可用（Spec §21）。"""
    tile_range = calculate_tile_range(GeoBounds(*GOOD_BOUNDS), COVERAGE_ZOOM)
    drop = {
        (COVERAGE_ZOOM, x, y)
        for x in range(tile_range.min_x, tile_range.max_x + 1)
        for y in range(tile_range.min_y, tile_range.max_y + 1)
        if (x, y) != (tile_range.min_x, tile_range.min_y)  # 至少留一张，别让库变空
    }
    db, _ = _fill_range(
        work_dir / "cov-mostly-missing.mbtiles",
        zooms=(COVERAGE_ZOOM,),
        metadata=_metadata_with(minzoom=str(COVERAGE_ZOOM), maxzoom=str(COVERAGE_ZOOM)),
        drop=drop,
    )
    result = _inspect(db, check_coverage=True)

    assert result.status == "WARNING"
    assert result.valid is True
    assert result.errors == []
    assert result.missing_tiles and result.missing_tiles > 0
    assert any("成片空白" in message for message in result.warnings)


def test_coverage_missing_details_capped(work_dir: Path) -> None:
    """缺失明细封顶 ``MAX_MISSING_TILE_DETAILS``，多出来的记进 ``truncated``。"""
    tile_range = calculate_tile_range(GeoBounds(*_WIDE_BOUNDS), COVERAGE_ZOOM)
    assert tile_range.tile_count > MAX_MISSING_TILE_DETAILS  # 夹具前提

    keep = {
        (COVERAGE_ZOOM, x, y)
        for x in range(tile_range.min_x, tile_range.max_x + 1)
        for y in range(tile_range.min_y, tile_range.max_y + 1)
    }
    # 只留最后 5 张，其余全丢 → 缺失数远超过明细上限
    drop = set(sorted(keep)[:-5])
    db, expected = _fill_range(
        work_dir / "cov-cap.mbtiles",
        zooms=(COVERAGE_ZOOM,),
        global_bounds=_WIDE_BOUNDS,
        metadata=_metadata_with(
            # bounds 必须跟着一起放宽 —— 覆盖检查算的是 metadata 声明的范围
            bounds="116.000000,39.000000,117.000000,40.000000",
            minzoom=str(COVERAGE_ZOOM),
            maxzoom=str(COVERAGE_ZOOM),
        ),
        drop=drop,
    )
    result = _inspect(db, check_coverage=True)

    assert expected == tile_range.tile_count
    assert result.missing_tiles == expected - 5
    assert len(result.missing_details) == MAX_MISSING_TILE_DETAILS
    assert result.missing_details_truncated == result.missing_tiles - MAX_MISSING_TILE_DETAILS
    assert any("未展开" in message for message in result.warnings)


def test_coverage_skipped_without_bounds(work_dir: Path) -> None:
    """metadata 没有可用 bounds → 跳过并记提示，不报错。"""
    metadata = [item for item in GOOD_METADATA if item[0] != "bounds"]
    db = _make_db(
        work_dir / "no-bounds.mbtiles",
        metadata=metadata,
        tiles=[(8, 1, 1, _png())],
    )
    result = _inspect(db, check_coverage=True)
    assert result.coverage_checked is False
    assert result.missing_tiles is None
    assert any("无法进行覆盖完整性检查" in note for note in result.notices)


def test_coverage_skipped_on_empty_db(work_dir: Path) -> None:
    db = _make_db(work_dir / "no-tiles.mbtiles", tiles=[])
    result = _inspect(db, check_coverage=True)
    assert any("库中没有瓦片" in note for note in result.notices)


def test_coverage_abort_keeps_per_zoom_but_drops_totals(work_dir: Path) -> None:
    """覆盖检查被中止时，全局合计必须置空 —— 半截的合计比没有更危险。"""
    db, _ = _fill_range(
        work_dir / "cov-abort.mbtiles",
        zooms=(8, 9, 10, 11, 12),
    )
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    result = MBTilesInspectionService().inspect(db, check_coverage=True, should_stop=should_stop)
    assert result.coverage_checked is False
    assert result.expected_tiles is None
    assert result.missing_tiles is None
    assert any("覆盖完整性检查被中止" in note for note in result.notices)


# --------------------------------------------------------------------------- #
# 合成信息（Slice 6 复用）
# --------------------------------------------------------------------------- #
def test_merged_composition_recognized(work_dir: Path) -> None:
    """带 Slice 6 合成字段的库：结论里能认出"已合成"。"""
    metadata = _metadata_with(
        provider="tianditu",
        map_type="vector",
        labels="true",
        composition="merged",
    )
    db, _ = _fill_range(work_dir / "merged.mbtiles", metadata=metadata)
    result = _inspect(db)
    assert result.is_merged is True
    assert result.composition == "merged"
    assert result.provider == "tianditu"
    assert result.map_type == "vector"
    assert result.labels is True


def test_composition_inconsistency_fails(work_dir: Path) -> None:
    """Slice 6 的自洽性规则由既有校验器把关，诊断结果必须并进来。"""
    metadata = _metadata_with(provider="tianditu", labels="true")  # 有 labels 却没有 map_type
    db, _ = _fill_range(work_dir / "bad-merged.mbtiles", metadata=metadata)
    result = _inspect(db)
    assert result.status == "FAIL"
    assert result.errors  # 来自 validate_mbtiles 的合成自洽性 ERROR


# --------------------------------------------------------------------------- #
# 深度扫描（Spec §11）
# --------------------------------------------------------------------------- #
def test_deep_scan_finds_every_corrupt_tile(work_dir: Path) -> None:
    """深度检查必须抓到**全部**坏瓦片，而不只是抽样撞上的那几张。"""
    corrupt_at = {(8, 1, 1), (8, 1, 2), (8, 1, 3)}
    db = _make_db(
        work_dir / "deep.mbtiles",
        metadata=_metadata_with(minzoom="8", maxzoom="8"),
        tiles=[
            (8, x, y, BROKEN_PNG if (8, x, y) in corrupt_at else _png())
            for x in range(1, 6)
            for y in range(1, 6)
        ],
    )
    result = _inspect(db, deep_scan=True, sample_size=1)

    assert result.deep_scan is True
    assert result.deep_scan_complete is True
    assert result.deep_scanned_tiles == 25
    assert result.corrupt_tiles == 3
    assert result.status == "FAIL"
    assert any("深度检查" in message for message in result.errors)


def test_deep_scan_reports_abort_as_incomplete(work_dir: Path) -> None:
    """中途放弃时必须标明"没扫完"——半截数字当成全量结论是最难查的一类 bug。"""
    from app.config.settings import DIAGNOSTICS_DEEP_SCAN_BATCH_SIZE

    total = DIAGNOSTICS_DEEP_SCAN_BATCH_SIZE * 3
    db = _make_db(
        work_dir / "abort.mbtiles",
        metadata=_metadata_with(minzoom="12", maxzoom="12"),
        # z12 合法行号上界是 4095，够放 1500 张
        tiles=[(12, 1, y, _png()) for y in range(1, total + 1)],
    )

    seen = {"calls": 0}

    def should_stop() -> bool:
        seen["calls"] += 1
        return seen["calls"] > 1

    result = MBTilesInspectionService().inspect(
        db, check_coverage=False, deep_scan=True, should_stop=should_stop
    )
    assert result.deep_scan is True
    assert result.deep_scan_complete is False
    assert 0 < result.deep_scanned_tiles <= DIAGNOSTICS_DEEP_SCAN_BATCH_SIZE
    assert result.deep_scanned_tiles < total
    assert any("深度检查被中止" in message for message in result.warnings)


# --------------------------------------------------------------------------- #
# probe_image（Spec §10）—— 单张瓦片的两级判定
# --------------------------------------------------------------------------- #
def test_probe_image_accepts_real_png() -> None:
    probe = probe_image(_png(8, 8))
    assert probe.ok is True
    assert probe.format == "PNG"
    assert probe.width == 8
    assert probe.height == 8


def test_probe_image_rejects_garbage_magic() -> None:
    probe = probe_image(b"<html>404 not found</html>")
    assert probe.ok is False
    assert "magic bytes" in probe.error


def test_probe_image_rejects_truncated_png() -> None:
    """magic 对但内容坏 —— 只有真解码才抓得到。"""
    probe = probe_image(BROKEN_PNG)
    assert probe.ok is False
    assert probe.error


def test_probe_error_is_stable_and_leaks_no_object_address() -> None:
    """错误文案必须是**稳定**的：Pillow 原文里的对象地址会让两份报告对不上。

    这条是实机看界面时发现的：``cannot identify image file <_io.BytesIO object
    at 0x000002597B604310>`` 直接被写进了界面和报告 —— 用户看不懂，
    而且同一份坏文件每次跑出来的文案都不一样，根本没法比对。
    """
    probe = probe_image(BROKEN_PNG)
    assert "0x" not in probe.error
    assert "BytesIO" not in probe.error
    assert "无法识别图片内容" in probe.error

    # 再跑一次，文案必须一模一样（同一份字节 → 同一句结论）
    assert probe_image(BROKEN_PNG).error == probe.error


def test_describe_probe_error_translates_known_causes() -> None:
    from app.mbtiles.diagnostics import _describe_probe_error

    assert "截断" in _describe_probe_error(
        Exception("image file is truncated (13 bytes not processed)")
    )
    assert "CRC" in _describe_probe_error(Exception("bad CRC-32 for IHDR"))
    # 认不出的异常退回原文，但压成一行
    assert _describe_probe_error(Exception("some\nweird\nerror")) == "some weird error"
    # 空消息退回异常类名，而不是空串
    assert _describe_probe_error(RuntimeError()) == "RuntimeError"


def test_probe_image_handles_empty() -> None:
    probe = probe_image(b"")
    assert probe.ok is False


# --------------------------------------------------------------------------- #
# 结论模型本身（Spec §20）
# --------------------------------------------------------------------------- #
def test_status_rules() -> None:
    result = MBTilesDiagnosticsResult(path="x.mbtiles")
    assert result.valid is False
    assert result.status == "FAIL"

    result.valid = True
    assert result.status == "PASS"

    result.warnings.append("有点可疑")
    assert result.status == "WARNING"

    # notices 永远不影响结论
    result.warnings.clear()
    result.notices.append("只是说明")
    assert result.status == "PASS"


def test_zoom_tile_stats_helpers() -> None:
    stats = ZoomTileStats(
        zoom=8,
        tile_count=6,
        min_x=1,
        max_x=2,
        min_y_tms=3,
        max_y_tms=5,
    )
    assert stats.x_count == 2
    assert stats.y_count == 3
    assert stats.missing_ratio is None
    assert stats.coverage_ok is None

    stats.expected_tiles = 8
    stats.missing_tiles = 2
    assert stats.missing_ratio == 0.25
    assert stats.coverage_ok is False


def test_inspection_progress_percent() -> None:
    assert InspectionProgress("阶段", "消息", 0, 0).percent == -1
    assert InspectionProgress("阶段", "消息", 1, 4).percent == 25
    assert InspectionProgress("阶段", "消息", 9, 4).percent == 100


def test_to_validation_bridges_legacy_result(work_dir: Path) -> None:
    """下载完成链路读的还是 ``MBTilesValidationResult``，字段语义不能变。"""
    db, _ = _fill_range(work_dir / "bridge.mbtiles")
    result = _inspect(db, check_coverage=True)
    validation = result.to_validation()
    assert validation.valid is True
    assert validation.tile_count == result.tile_count
    assert validation.min_zoom == 8
    assert validation.max_zoom == 10


def test_to_validation_surfaces_missing_tiles(work_dir: Path) -> None:
    tile_range = calculate_tile_range(GeoBounds(*GOOD_BOUNDS), COVERAGE_ZOOM)
    db, _ = _fill_range(
        work_dir / "bridge-missing.mbtiles",
        zooms=(COVERAGE_ZOOM,),
        metadata=_metadata_with(minzoom=str(COVERAGE_ZOOM), maxzoom=str(COVERAGE_ZOOM)),
        drop={(COVERAGE_ZOOM, tile_range.min_x, tile_range.min_y)},
    )
    result = _inspect(db, check_coverage=True)
    validation = result.to_validation()
    assert any("缺失 1 张瓦片" in message for message in validation.warnings)


# --------------------------------------------------------------------------- #
# 文本报告（Spec §26）
# --------------------------------------------------------------------------- #
def test_render_report_contains_key_sections(work_dir: Path) -> None:
    db, _ = _fill_range(work_dir / "report.mbtiles")
    result = _inspect(db, check_coverage=True)
    text = render_report(result)

    assert str(db) in text
    assert "PASS" in text
    assert "metadata" in text.lower()
    assert text.endswith("\n")


def test_render_report_lists_errors_and_missing_details(work_dir: Path) -> None:
    tile_range = calculate_tile_range(GeoBounds(*GOOD_BOUNDS), COVERAGE_ZOOM)
    drop = {(COVERAGE_ZOOM, tile_range.min_x, tile_range.min_y)}
    db, _ = _fill_range(
        work_dir / "report-bad.mbtiles",
        zooms=(COVERAGE_ZOOM,),
        metadata=_metadata_with(minzoom=str(COVERAGE_ZOOM), maxzoom=str(COVERAGE_ZOOM)),
        drop=drop,
        override={(COVERAGE_ZOOM, tile_range.min_x, tile_range.min_y + 1): BROKEN_PNG},
    )
    result = _inspect(db, check_coverage=True)
    assert result.status == "FAIL"
    text = render_report(result)

    assert "FAIL" in text
    assert f"{COVERAGE_ZOOM}/{tile_range.min_x}/{tile_range.min_y}" in text
    assert "Errors" in text
    assert "Warnings" in text
    assert "Notices" in text


# --------------------------------------------------------------------------- #
# 夹具自检：TMS 存储约定
# --------------------------------------------------------------------------- #
def test_fixture_stores_tms_rows(work_dir: Path) -> None:
    """夹具把 XYZ 翻成 TMS 入库 —— 这条断言守住"测试自己没把翻转写错"。"""
    db = _make_db(
        work_dir / "tms.mbtiles",
        metadata=_metadata_with(minzoom="8", maxzoom="8"),
        tiles=[(8, 1, 1, _png())],
    )
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT tile_row FROM tiles WHERE zoom_level=8 AND tile_column=1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == xyz_y_to_tms_y(8, 1)
    assert tms_y_to_xyz_y(8, row[0]) == 1


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (DIAGNOSTICS_DEFAULT_SAMPLE_SIZE, DIAGNOSTICS_DEFAULT_SAMPLE_SIZE),
        (DIAGNOSTICS_MIN_SAMPLE_SIZE, DIAGNOSTICS_MIN_SAMPLE_SIZE),
        (DIAGNOSTICS_MAX_SAMPLE_SIZE, DIAGNOSTICS_MAX_SAMPLE_SIZE),
    ],
)
def test_sample_size_kept_within_bounds(given: int, expected: int, work_dir: Path) -> None:
    db, _ = _fill_range(work_dir / f"sample-{given}.mbtiles")
    result = _inspect(db, sample_size=given)
    assert result.sample_size <= expected


def test_image_probe_dataclass_defaults() -> None:
    probe = ImageProbe(ok=True)
    assert probe.format is None
    assert probe.width == 0
    assert probe.height == 0
    assert probe.error == ""
