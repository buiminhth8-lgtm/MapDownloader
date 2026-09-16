"""MBTiles 检查服务单元测试（Slice 8，Spec §15 / §26）。

这里测的是**编排层**，不是规则层（规则层在 ``test_mbtiles_diagnostics.py``）：

- 按什么顺序跑、每一阶段独立兜异常；
- 进度回调怎么被调用、回调自己炸了会不会毁掉整份结论；
- ``deep_scan`` / ``check_coverage`` / ``sample_size`` 参数是否真的生效；
- 报告落到哪里、什么编码；
- **任何输入都不许抛异常**（Spec §35）。

刻意不装 GUI：服务层是纯 Python，能脱离 PySide6 单独断言。
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
    DIAGNOSTICS_REPORT_SUFFIX,
)
from app.geo import GeoBounds, calculate_tile_range, xyz_y_to_tms_y
from app.mbtiles import diagnostics as diag
from app.mbtiles.diagnostics import InspectionProgress
from app.services import MBTilesInspectionService

GOOD_METADATA: list[tuple[str, str]] = [
    ("name", "beijing"),
    ("type", "baselayer"),
    ("version", "1.0"),
    ("description", "服务层测试"),
    ("format", "png"),
    ("bounds", "116.000000,39.000000,116.500000,39.500000"),
    ("minzoom", "12"),
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


def _png(width: int = 16, height: int = 16) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (width, height), (10, 20, 200, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def _make_full_db(path: Path) -> Path:
    """造一个覆盖完整（z12 全填）的**正常**库。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)", GOOD_METADATA
        )
        conn.execute(_TILES_DDL)
        bounds = GeoBounds(116.0, 39.0, 116.5, 39.5)
        tile_range = calculate_tile_range(bounds, 12)
        payload = _png()
        rows = [
            (12, x, xyz_y_to_tms_y(12, y), payload)
            for x in range(tile_range.min_x, tile_range.max_x + 1)
            for y in range(tile_range.min_y, tile_range.max_y + 1)
        ]
        conn.executemany(
            "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def good_db(work_dir: Path) -> Path:
    return _make_full_db(work_dir / "good.mbtiles")


# --------------------------------------------------------------------------- #
# inspect：编排与"绝不抛异常"
# --------------------------------------------------------------------------- #
def test_inspect_returns_pass_for_healthy_file(good_db: Path) -> None:
    result = MBTilesInspectionService().inspect(good_db)
    assert result.status == "PASS"
    assert result.valid is True
    assert result.path == str(good_db)
    assert result.file_size > 0
    assert result.tile_count > 0


def test_inspect_records_tables_and_sqlite_check(good_db: Path) -> None:
    result = MBTilesInspectionService().inspect(good_db)
    assert set(result.tables) == {"metadata", "tiles"}
    assert result.sqlite_check is not None


@pytest.mark.parametrize(
    "builder",
    [
        pytest.param(lambda directory: directory / "missing.mbtiles", id="文件不存在"),
        pytest.param(
            lambda directory: _write(directory / "empty.mbtiles", b""), id="空文件"
        ),
        pytest.param(
            lambda directory: _write(
                directory / "garbage.mbtiles", b"not a database at all" * 16
            ),
            id="不是 SQLite",
        ),
        pytest.param(lambda directory: _mkdir(directory), id="路径是目录"),
    ],
)
def test_inspect_never_raises_on_broken_input(builder, work_dir: Path) -> None:
    """损坏输入必须变成 FAIL，而不是异常（Spec §35 的硬性契约）。"""
    target = builder(work_dir)
    result = MBTilesInspectionService().inspect(target)  # 不 try/except，就是断言不炸
    assert result.status == "FAIL"
    assert result.valid is False
    assert result.errors


def test_inspect_handles_missing_tiles_table(work_dir: Path) -> None:
    db = work_dir / "no-tiles.mbtiles"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)", GOOD_METADATA
        )
        conn.commit()
    finally:
        conn.close()

    result = MBTilesInspectionService().inspect(db)
    assert result.status == "FAIL"
    assert any("缺少 tiles 表" in message for message in result.errors)


def test_stage_failure_is_isolated(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """某一项检查自己炸掉，只让**那一项**记错，后面的检查继续跑。"""
    db = _make_full_db(work_dir / "isolated.mbtiles")

    def boom(*args, **kwargs):
        raise RuntimeError("模拟规则层崩溃")

    # 只让"空瓦片"这一项崩掉
    monkeypatch.setattr(diag, "check_empty_tiles", boom)

    result = MBTilesInspectionService().inspect(db, check_coverage=True)

    assert any("空瓦片检查失败" in message for message in result.errors)
    assert "模拟规则层崩溃" in "\n".join(result.errors)
    # 其他阶段照常产出
    assert result.tile_count > 0
    assert result.zoom_stats
    assert any("全部可正常解码" in note for note in result.notices)


def test_reader_connection_released(good_db: Path) -> None:
    """检查完必须关掉连接：不然后面想删掉这个坏文件会撞上"文件被占用"。"""
    MBTilesInspectionService().inspect(good_db)
    good_db.unlink()
    assert not good_db.exists()


# --------------------------------------------------------------------------- #
# 参数：check_coverage / deep_scan
# --------------------------------------------------------------------------- #
def test_check_coverage_toggle(good_db: Path) -> None:
    service = MBTilesInspectionService()
    without = service.inspect(good_db, check_coverage=False)
    assert without.coverage_checked is False
    assert without.missing_tiles is None

    with_coverage = service.inspect(good_db, check_coverage=True)
    assert with_coverage.coverage_checked is True
    assert with_coverage.missing_tiles == 0


def test_deep_scan_toggle(good_db: Path) -> None:
    service = MBTilesInspectionService()
    quick = service.inspect(good_db, deep_scan=False)
    assert quick.deep_scan is False
    assert quick.deep_scan_complete is False
    assert quick.deep_scanned_tiles == 0

    deep = service.inspect(good_db, deep_scan=True)
    assert deep.deep_scan is True
    assert deep.deep_scan_complete is True
    assert deep.deep_scanned_tiles == deep.tile_count
    assert deep.corrupt_tiles == 0


def test_deep_scan_uses_integrity_check(good_db: Path) -> None:
    """深度检查把 SQLite 自检从 quick_check 升级成 integrity_check（Spec §4）。"""
    result = MBTilesInspectionService().inspect(good_db, deep_scan=True)
    assert result.sqlite_check is not None
    assert result.sqlite_check.strip().lower() == "ok"


# --------------------------------------------------------------------------- #
# sample_size 规范化
# --------------------------------------------------------------------------- #
def test_sample_size_below_minimum_is_clamped(good_db: Path) -> None:
    result = MBTilesInspectionService().inspect(good_db, sample_size=0)
    assert result.sample_size <= DIAGNOSTICS_MIN_SAMPLE_SIZE
    assert any("小于下限" in message for message in result.warnings)


def test_sample_size_above_maximum_is_clamped(good_db: Path) -> None:
    result = MBTilesInspectionService().inspect(good_db, sample_size=100_000)
    assert result.sample_size <= DIAGNOSTICS_MAX_SAMPLE_SIZE
    assert any("超过上限" in message for message in result.warnings)


def test_sample_size_non_integer_falls_back_to_default(good_db: Path) -> None:
    result = MBTilesInspectionService().inspect(good_db, sample_size="五十")  # type: ignore[arg-type]
    assert any(
        f"已改用默认值 {DIAGNOSTICS_DEFAULT_SAMPLE_SIZE}" in message
        for message in result.warnings
    )


def test_sample_size_in_range_is_respected(good_db: Path) -> None:
    result = MBTilesInspectionService().inspect(good_db, sample_size=10)
    assert result.sample_size > 0
    assert not any("抽样张数" in message for message in result.warnings)


def test_injected_validator_is_used(good_db: Path) -> None:
    """编排层确实调用了 Slice 4/6 校验器（而不是自己重写了一套规则）。"""
    seen: list[int] = []

    def fake_validator(path, *, sample_limit=0):
        seen.append(sample_limit)
        from app.mbtiles import validate_mbtiles

        return validate_mbtiles(path, sample_limit=sample_limit)

    MBTilesInspectionService(validator=fake_validator).inspect(good_db, sample_size=7)
    assert seen == [7]


# --------------------------------------------------------------------------- #
# 进度回调
# --------------------------------------------------------------------------- #
def test_progress_callback_receives_stages(good_db: Path) -> None:
    events: list[InspectionProgress] = []
    MBTilesInspectionService().inspect(
        good_db, check_coverage=True, progress_callback=events.append
    )

    assert events
    assert all(isinstance(event, InspectionProgress) for event in events)
    assert events[0].stage == "开始"
    assert events[-1].stage == "结束"
    stages = {event.stage for event in events}
    for expected in ("数据库结构", "SQLite 自检", "Zoom 统计", "图片", "覆盖范围"):
        assert expected in stages


def test_progress_percent_is_monotonic_within_coverage(good_db: Path) -> None:
    events: list[InspectionProgress] = []
    MBTilesInspectionService().inspect(
        good_db, progress_callback=events.append
    )
    percents = [event.percent for event in events if event.stage == "覆盖范围"]
    assert percents == sorted(percents)


def test_broken_progress_callback_does_not_break_inspection(good_db: Path) -> None:
    """进度上报是锦上添花：一个 GUI 回调炸掉不该让整份诊断结果丢失。"""

    def explode(_progress: InspectionProgress) -> None:
        raise RuntimeError("界面回调崩了")

    result = MBTilesInspectionService().inspect(good_db, progress_callback=explode)
    assert result.status == "PASS"


# --------------------------------------------------------------------------- #
# 报告导出（Spec §26）
# --------------------------------------------------------------------------- #
def test_export_report_default_path(good_db: Path) -> None:
    service = MBTilesInspectionService()
    result = service.inspect(good_db, check_coverage=True)
    report = service.export_report(result)

    assert report.name == good_db.name + DIAGNOSTICS_REPORT_SUFFIX
    assert report.parent == good_db.parent
    assert report.exists()

    text = report.read_text(encoding="utf-8")
    assert str(good_db) in text
    assert "PASS" in text


def test_export_report_explicit_path(good_db: Path, work_dir: Path) -> None:
    service = MBTilesInspectionService()
    result = service.inspect(good_db)
    target = work_dir / "somewhere" / "report.txt"
    target.parent.mkdir()

    written = service.export_report(result, target)
    assert written == target
    assert target.read_text(encoding="utf-8").strip()


def test_export_report_is_utf8_with_chinese(good_db: Path) -> None:
    """报告里有中文（"缺失瓦片""结论"），必须按 UTF-8 落盘。"""
    service = MBTilesInspectionService()
    report = service.export_report(service.inspect(good_db))
    raw = report.read_bytes()
    assert "结论".encode("utf-8") in raw
    raw.decode("utf-8")  # 不抛 → 编码正确


def test_export_report_reflects_failure(work_dir: Path) -> None:
    broken = work_dir / "broken.mbtiles"
    broken.write_bytes(b"definitely not sqlite" * 8)
    service = MBTilesInspectionService()
    result = service.inspect(broken)
    report = service.export_report(result)

    text = report.read_text(encoding="utf-8")
    assert "FAIL" in text
    assert str(broken) in text


def test_export_report_does_not_touch_source(good_db: Path) -> None:
    """导出报告绝不能改动 .mbtiles 本身（只读保证，Spec §23）。"""
    before = good_db.read_bytes()
    service = MBTilesInspectionService()
    service.export_report(service.inspect(good_db, check_coverage=True))
    assert good_db.read_bytes() == before


# --------------------------------------------------------------------------- #
# 只读保证
# --------------------------------------------------------------------------- #
def test_inspection_does_not_modify_database(good_db: Path) -> None:
    before = good_db.read_bytes()
    MBTilesInspectionService().inspect(good_db, check_coverage=True, deep_scan=True)
    assert good_db.read_bytes() == before


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def _mkdir(path: Path) -> Path:
    target = path / "a-directory"
    target.mkdir()
    return target
