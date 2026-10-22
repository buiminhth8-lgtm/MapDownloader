"""多边形地图包的覆盖度检查（Slice 16，Spec §20 / §28）。

Spec §20 的那条要求，用一句话说是：**多边形包的"期望瓦片数"必须按行政区
几何算，不能按外包矩形算。**

理由不是"更精确一点"。按外包矩形算一个完整的中国包，会得出"缺了 60% 瓦片"
的结论 —— 而那个包是**完整**的。用户看到这个数字会去重下几万张瓦片，
花掉配额、花掉时间，最后发现"还是缺 60%"。误报比"不知道"糟糕得多，
所以边界资源不可用时这里的处置是：覆盖度标成**未知**，并如实说明原因。

这组用例同时守住"矩形包一个字节都不变"（Spec §7）。
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

from PIL import Image

from app.geo import (
    RegionPreset,
    calculate_tile_range,
    tile_x_to_lon,
    tile_y_to_lat,
)
from app.geo import region as region_module
from app.mbtiles import MBTilesMetadata, MBTilesWriter
from app.providers import PNG_SIGNATURE
from app.services import MBTilesInspectionService

ZOOM = 12
BASE_X, BASE_Y = 3372, 1552
BLOCK = 10
HIT_COLS, HIT_ROWS = 6, 10
EDGE_NUDGE = 1e-4
BOUNDARY_VERSION = "2026.09-fixture"
REGION_ID = "test-hit"


def _real_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (16, 16), (0, 0, 0, 0)).save(buffer, format="PNG")
    data = buffer.getvalue()
    assert data.startswith(PNG_SIGNATURE)
    return data


FAKE_PNG = _real_png()


# --------------------------------------------------------------------------- #
# 夹具（与 test_polygon_download / test_polygon_resume 同源）
# --------------------------------------------------------------------------- #
def block_bounds():
    from app.geo import GeoBounds

    return GeoBounds(
        west=tile_x_to_lon(BASE_X, ZOOM),
        south=tile_y_to_lat(BASE_Y + BLOCK, ZOOM) + EDGE_NUDGE,
        east=tile_x_to_lon(BASE_X + BLOCK, ZOOM) - EDGE_NUDGE,
        north=tile_y_to_lat(BASE_Y, ZOOM),
    )


def in_region_tiles() -> set[tuple[int, int]]:
    return {
        (x, y)
        for y in range(BASE_Y, BASE_Y + HIT_ROWS)
        for x in range(BASE_X, BASE_X + HIT_COLS)
    }


def out_of_region_tiles() -> set[tuple[int, int]]:
    return {
        (x, y)
        for y in range(BASE_Y, BASE_Y + HIT_ROWS)
        for x in range(BASE_X + HIT_COLS, BASE_X + HIT_COLS + 3)
    }


def hit_polygon() -> list[list[list[float]]]:
    cw = 360.0 / (2**ZOOM)
    row_h = tile_y_to_lat(BASE_Y, ZOOM) - tile_y_to_lat(BASE_Y + 1, ZOOM)
    row_h_last = tile_y_to_lat(BASE_Y + HIT_ROWS - 1, ZOOM) - tile_y_to_lat(
        BASE_Y + HIT_ROWS, ZOOM
    )
    west = tile_x_to_lon(BASE_X, ZOOM) + cw * 0.25
    east = tile_x_to_lon(BASE_X + HIT_COLS, ZOOM) - cw * 0.25
    north = tile_y_to_lat(BASE_Y, ZOOM) - row_h * 0.25
    south = tile_y_to_lat(BASE_Y + HIT_ROWS, ZOOM) + row_h_last * 0.25
    return [[[west, south], [east, south], [east, north], [west, north], [west, south]]]


BOUNDARY_FILE = "test-hit.geojson"


def register_preset(work_dir: Path, monkeypatch, *, create_file: bool = True) -> RegionPreset:
    """注册临时行政区预设（资源目录指向临时目录，仓库资源区保持干净）。"""
    if create_file:
        (work_dir / BOUNDARY_FILE).write_text(
            json.dumps(
                {
                    "type": "Feature",
                    "properties": {
                        "name": "测试命中区",
                        "id": REGION_ID,
                        "boundaryVersion": BOUNDARY_VERSION,
                    },
                    "geometry": {"type": "Polygon", "coordinates": hit_polygon()},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    item = RegionPreset(
        region_id=REGION_ID,
        name="测试命中区",
        geojson_filename=BOUNDARY_FILE,
        default_boundary_version=BOUNDARY_VERSION,
    )
    monkeypatch.setattr(region_module, "REGION_RESOURCE_DIR", work_dir)
    monkeypatch.setattr(region_module, "REGION_PRESETS", (item,))
    return item


def build_package(
    path: Path,
    tiles: set[tuple[int, int]],
    *,
    region_type: str = "polygon",
    region_id: str = REGION_ID,
    boundary_version: str = BOUNDARY_VERSION,
    zoom: int = ZOOM,
) -> None:
    """建一个指定内容的 MBTiles 包（瓦片是能被 Pillow 解码的真 PNG）。"""
    metadata = MBTilesMetadata.for_bounds(
        block_bounds(),
        zoom,
        zoom,
        name="pkg",
        region_type=region_type,
        region_id=region_id,
        boundary_version=boundary_version,
    )
    with MBTilesWriter(path) as writer:
        writer.write_metadata(metadata)
        for x, y in sorted(tiles):
            writer.write_tile(zoom, x, y, FAKE_PNG)


def full_block_tiles() -> set[tuple[int, int]]:
    return {
        (x, y)
        for y in range(BASE_Y, BASE_Y + BLOCK)
        for x in range(BASE_X, BASE_X + BLOCK)
    }


#: 缺失数量的断言必须针对**数字**，而不是"缺失"两个字。
#:
#: 中文字面上"误报成缺失"这句说明里也含"缺失"，拿子串去否定会把正确的提示
#: 判成违规。真正要禁的是"对外包矩形算出一个具体数量"这种行为，所以匹配
#: "缺失 + 数字"。
_MISSING_CLAIM = re.compile(r"缺失\s*[0-9,]+\s*张")


def inspect(path: Path):
    return MBTilesInspectionService().inspect(path)


# --------------------------------------------------------------------------- #
# 夹具自检
# --------------------------------------------------------------------------- #
def test_fixture_block_has_one_hundred_candidates() -> None:
    assert calculate_tile_range(block_bounds(), ZOOM).tile_count == 100


def test_fixture_region_has_sixty_tiles() -> None:
    assert len(in_region_tiles()) == 60


# --------------------------------------------------------------------------- #
# 完整 / 缺三张
# --------------------------------------------------------------------------- #
def test_complete_polygon_package_reports_no_missing(work_dir: Path, monkeypatch) -> None:
    """期望 60 / 实际 60 → 缺失 0，且结论是 PASS（不是 WARNING）。"""
    register_preset(work_dir, monkeypatch)
    path = work_dir / "complete.mbtiles"
    build_package(path, in_region_tiles())

    result = inspect(path)

    assert result.coverage_checked is True
    assert result.expected_tiles == 60
    assert result.actual_tiles_in_coverage == 60
    assert result.missing_tiles == 0
    assert result.status == "PASS"
    # 必须说清"期望值是按什么算的"，否则 60 这个数字没法解释
    assert any("行政区几何" in note for note in result.notices)


def test_polygon_package_missing_three_reports_exactly_three(
    work_dir: Path, monkeypatch
) -> None:
    """期望 60 / 实际 57 → 缺失 3（而不是"缺了 40 多张"）。"""
    register_preset(work_dir, monkeypatch)
    path = work_dir / "missing_three.mbtiles"
    omitted = set(sorted(in_region_tiles())[-3:])
    assert len(omitted) == 3
    build_package(path, in_region_tiles() - omitted)

    result = inspect(path)

    assert result.expected_tiles == 60
    assert result.actual_tiles_in_coverage == 57
    assert result.missing_tiles == 3
    # 明细要指得出是哪三张（XYZ 语义的 z/x/y）
    assert len(result.missing_details) == 3
    for x, y in omitted:
        assert f"{ZOOM}/{x}/{y}" in result.missing_details
    assert any("缺失 3" in warning for warning in result.warnings)
    # 缺 5% 只是 WARNING，不是 FAIL：剩下的部分完全可用
    assert result.valid is True
    assert result.status == "WARNING"


def test_out_of_region_tiles_in_the_file_do_not_change_the_numbers(
    work_dir: Path, monkeypatch
) -> None:
    """包里混着 30 张区域外瓦片 → expected / missing 都不受影响。

    这是"文件前身是矩形任务"的现实情形。若 expected 按矩形算，会得到
    "期望 100 / 实际 90 / 缺失 10" 三个**全都错**的数字。
    """
    register_preset(work_dir, monkeypatch)
    path = work_dir / "mixed.mbtiles"
    build_package(path, in_region_tiles() | out_of_region_tiles())

    result = inspect(path)

    assert result.expected_tiles == 60
    assert result.actual_tiles_in_coverage == 60
    assert result.missing_tiles == 0
    # 库里确实有 90 行 —— 说明"没缺失"不是因为没看见那 30 张
    assert result.tile_count == 90


def test_zoom_stats_carry_the_polygon_expectation(work_dir: Path, monkeypatch) -> None:
    """逐 zoom 明细里也要是区域口径（报告的表格直接读这两列）。"""
    register_preset(work_dir, monkeypatch)
    path = work_dir / "zoom_stats.mbtiles"
    omitted = set(sorted(in_region_tiles())[-3:])
    build_package(path, in_region_tiles() - omitted)

    result = inspect(path)

    stats = [item for item in result.zoom_stats if item.zoom == ZOOM]
    assert len(stats) == 1
    assert stats[0].expected_tiles == 60
    assert stats[0].missing_tiles == 3


# --------------------------------------------------------------------------- #
# 边界资源不可用 → 未知，而不是"缺一大片"
# --------------------------------------------------------------------------- #
def test_unknown_region_id_marks_coverage_unknown(work_dir: Path, monkeypatch) -> None:
    """本版本不认识这个 ``region_id`` → 覆盖度未知，**不按矩形估算**。"""
    register_preset(work_dir, monkeypatch)
    path = work_dir / "ghost_region.mbtiles"
    build_package(path, in_region_tiles(), region_id="ghost")

    result = inspect(path)

    assert result.coverage_checked is False
    assert result.missing_tiles is None
    assert result.expected_tiles is None
    assert any("未知" in warning for warning in result.warnings)
    assert any("ghost" in warning for warning in result.warnings)
    # 关键反证：绝不能出现"缺 40 张"这类由外包矩形算出来的数字
    assert not any(_MISSING_CLAIM.search(warning) for warning in result.warnings)


def test_missing_boundary_file_marks_coverage_unknown(work_dir: Path, monkeypatch) -> None:
    """预设认得出来、但边界文件不在 → 同样标未知，并给出该往哪放文件。"""
    register_preset(work_dir, monkeypatch, create_file=False)
    path = work_dir / "no_boundary.mbtiles"
    build_package(path, in_region_tiles())

    result = inspect(path)

    assert result.coverage_checked is False
    assert result.missing_tiles is None
    assert any("未知" in warning for warning in result.warnings)
    # 提示里要有"往哪放"：只说"文件不存在"，用户只能猜
    assert any(BOUNDARY_FILE in warning for warning in result.warnings)
    assert not any(_MISSING_CLAIM.search(warning) for warning in result.warnings)


def test_unknown_coverage_still_reports_the_other_findings(
    work_dir: Path, monkeypatch
) -> None:
    """覆盖度未知不该把其它结论一起吞掉（文件结构、瓦片图片都还是查过的）。"""
    register_preset(work_dir, monkeypatch)
    path = work_dir / "still_checked.mbtiles"
    build_package(path, in_region_tiles(), region_id="ghost")

    result = inspect(path)

    assert result.tile_count == 60
    assert result.min_zoom == ZOOM
    assert result.max_zoom == ZOOM
    assert result.corrupt_tiles == 0
    assert result.errors == []


# --------------------------------------------------------------------------- #
# 矩形包：行为一个字节都不变
# --------------------------------------------------------------------------- #
def test_rectangle_package_uses_the_rectangle_path(work_dir: Path, monkeypatch) -> None:
    """``region_type=rectangle`` → 期望 100（外包矩形），与 Slice ≤15 一致。"""
    register_preset(work_dir, monkeypatch)
    path = work_dir / "rectangle.mbtiles"
    build_package(
        path,
        full_block_tiles(),
        region_type="rectangle",
        region_id="",
        boundary_version="",
    )

    result = inspect(path)

    assert result.coverage_checked is True
    assert result.expected_tiles == 100
    assert result.actual_tiles_in_coverage == 100
    assert result.missing_tiles == 0
    assert result.status == "PASS"
    # 矩形不该出现"按行政区几何计算"那种提示
    assert not any("行政区几何" in note for note in result.notices)


def test_old_package_without_region_type_uses_the_rectangle_path(
    work_dir: Path, monkeypatch
) -> None:
    """Slice ≤15 的老包（metadata 里没有 ``region_type``）→ 走矩形路径。

    Spec §29 的兼容红线：老包不能被新逻辑判成"未知覆盖度"。
    """
    register_preset(work_dir, monkeypatch)
    path = work_dir / "old_package.mbtiles"
    build_package(
        path, full_block_tiles(), region_type="", region_id="", boundary_version=""
    )

    with_reader = inspect(path)

    assert with_reader.coverage_checked is True
    assert with_reader.expected_tiles == 100
    assert with_reader.missing_tiles == 0


def test_rectangle_metadata_omits_the_empty_region_keys(work_dir: Path) -> None:
    """矩形包的 metadata 里不该多出两个空值键（``region_id`` / ``boundary_version``）。"""
    path = work_dir / "keys.mbtiles"
    build_package(
        path,
        full_block_tiles(),
        region_type="rectangle",
        region_id="",
        boundary_version="",
    )

    result = inspect(path)

    assert result.metadata["region_type"] == "rectangle"
    assert "region_id" not in result.metadata
    assert "boundary_version" not in result.metadata


# --------------------------------------------------------------------------- #
# 明细上限（避免一个坏包把几万条坐标灌进内存）
# --------------------------------------------------------------------------- #
def test_missing_details_respect_the_limit(work_dir: Path, monkeypatch) -> None:
    """缺失明细有条数上限，超出的部分用计数表达。"""
    from app.config.settings import MAX_MISSING_TILE_DETAILS

    register_preset(work_dir, monkeypatch)
    path = work_dir / "many_missing.mbtiles"
    # 换到 z13：候选瓦片多 4 倍，缺失量足以超过明细上限（z12 只有 60 张，够不着）
    zoom = ZOOM + 1
    only_one = {(BASE_X * 2, BASE_Y * 2)}
    build_package(path, only_one, zoom=zoom)

    result = inspect(path)

    assert result.expected_tiles > MAX_MISSING_TILE_DETAILS, "夹具前提：缺失量要够大"
    assert result.missing_tiles == result.expected_tiles - result.actual_tiles_in_coverage
    assert result.actual_tiles_in_coverage == 1
    assert len(result.missing_details) == MAX_MISSING_TILE_DETAILS
    assert (
        result.missing_details_truncated
        == result.missing_tiles - len(result.missing_details)
    )
