from __future__ import annotations

from dataclasses import replace

import pytest

from app.geo import DownloadRegion, GeoBounds
from app.geo.region_estimator import ZoomRegionStats
from app.mbtiles import MBTilesMetadata, MBTilesWriter
from app.models import MapType
from app.planning.storage_budget import (
    GIB,
    BudgetCancelled,
    BudgetInput,
    BudgetSettings,
    assess_budget,
)

BOUNDS = GeoBounds(116.2, 39.7, 116.6, 40.1)


def request(work_dir, *, mode=MapType.VECTOR, labels=True, counts=((12, 100),), resume=True):
    return BudgetInput("map", "测试地图", DownloadRegion.rectangle("测试", BOUNDS),
                       min(z for z, _ in counts), max(z for z, _ in counts),
                       work_dir / "target.mbtiles", mode, labels,
                       tuple(ZoomRegionStats(z, n, n, 0) for z, n in counts), resume)


def assess(*requests, settings=None, **kwargs):
    return assess_budget(requests, settings, disk_probe=lambda p: ("test-disk", 1000 * GIB), **kwargs)


def seed(path, *, mode=MapType.VECTOR, labels=True, zoom=12, length=2048):
    metadata = MBTilesMetadata.for_bounds(
        BOUNDS, zoom, zoom, name="sample", source_map_type=mode.value,
        with_labels=labels, composition="merged" if labels else "single",
    )
    with MBTilesWriter(path) as writer:
        writer.write_metadata(metadata)
        for i in range(12):
            writer.write_tile(zoom, int(3370 * 2 ** (zoom - 12)) + i, int(1552 * 2 ** (zoom - 12)), b"\x89PNG\r\n\x1a\n" + b"x" * (length - 8))


def test_satellite_and_vector_use_different_default_models(work_dir):
    vector = assess(request(work_dir)).items[0]
    image = assess(request(work_dir, mode=MapType.IMAGE)).items[0]
    assert image.new_bytes > vector.new_bytes * 3
    assert "默认模型" in image.zooms[0].source
    assert image.zooms[0].sample_count == 0


def test_local_samples_match_mode_labels_and_zoom(work_dir):
    seed(work_dir / "vector.mbtiles", length=4096)
    seed(work_dir / "satellite.mbtiles", mode=MapType.IMAGE, length=32768)
    seed(work_dir / "base-only.mbtiles", labels=False, length=16384)
    seed(work_dir / "other-zoom.mbtiles", zoom=13, length=65536)
    result = assess(request(work_dir, counts=((12, 100), (13, 20))))
    assert result.items[0].zooms[0].mean_bytes == 4096
    assert result.items[0].zooms[1].mean_bytes == 65536
    satellite = assess(request(work_dir, mode=MapType.IMAGE)).items[0].zooms[0]
    assert satellite.mean_bytes == 32768
    base = assess(request(work_dir, labels=False)).items[0].zooms[0]
    assert base.mean_bytes == 16384


def test_missing_zoom_samples_do_not_reuse_low_zoom_average(work_dir):
    seed(work_dir / "low.mbtiles", zoom=10, length=1024)
    result = assess(request(work_dir, counts=((18, 100),))).items[0].zooms[0]
    assert result.sample_count == 0 and result.mean_bytes > 1024


def test_resume_counts_only_existing_coordinates_inside_target(work_dir):
    req = request(work_dir, counts=((12, 100),))
    seed(req.output_path)
    result = assess(req)
    zoom = result.items[0].zooms[0]
    assert 0 < zoom.existing < 12  # 其余种子瓦片在当前 bounds 之外。
    assert zoom.missing == zoom.total - zoom.existing
    assert result.requests == zoom.missing * 2
    assert result.items[0].existing_file_bytes == req.output_path.stat().st_size


def test_restart_does_not_credit_existing_tiles_or_delete_file(work_dir):
    req = request(work_dir, resume=False)
    seed(req.output_path)
    before = req.output_path.read_bytes()
    result = assess(req)
    assert result.items[0].zooms[0].existing == 0
    assert result.requests == 200
    assert req.output_path.read_bytes() == before


def test_new_capacity_and_final_capacity_are_separate(work_dir):
    req = request(work_dir)
    seed(req.output_path)
    item = assess(req).items[0]
    assert item.final_bytes > item.new_bytes
    assert item.final_upper_bytes >= item.final_bytes


def test_geometry_target_is_estimated_when_counts_not_supplied(work_dir):
    req = replace(request(work_dir), counts=())
    result = assess(req)
    assert result.items[0].zooms[0].total > 0


def test_budget_uses_conservative_final_size_and_provides_zoom_savings(work_dir):
    req = request(work_dir, counts=((16, 100), (17, 400), (18, 1600)))
    report = assess(req, settings=BudgetSettings(budget_gib=0.001, reserve_gib=0))
    assert report.over_budget and report.disk_ok
    suggestion = next(s for s in report.suggestions if s.max_zoom == 17)
    assert suggestion.item_id == req.id and suggestion.saved_bytes > 0
    assert "少预留" in suggestion.describe()
    assert req.max_zoom == 18  # 仅给建议，不自动修改请求。


def test_no_suggestion_drops_below_minimum_zoom(work_dir):
    report = assess(request(work_dir, counts=((18, 100),)))
    assert report.suggestions == ()


def test_packaging_copy_changes_disk_requirement_not_request_count(work_dir):
    req = request(work_dir)
    plain = assess(req, settings=BudgetSettings(reserve_gib=0))
    packaged = assess(req, settings=BudgetSettings(reserve_gib=0, packaging_copy=True))
    assert packaged.requests == plain.requests
    assert packaged.disks["test-disk"][1] > plain.disks["test-disk"][1]


def test_same_disk_reserve_is_counted_once_and_different_disks_separately(work_dir):
    a = request(work_dir)
    b = replace(a, id="second", output_path=work_dir / "second" / "map.mbtiles")
    same = assess(a, b, settings=BudgetSettings(reserve_gib=2))
    assert len(same.disks) == 1
    assert same.disks["test-disk"][1] == sum(i.upper_bytes for i in same.items) + 2 * GIB
    different = assess_budget((a, b), BudgetSettings(reserve_gib=2),
                              disk_probe=lambda p: (str(p), 100 * GIB))
    assert len(different.disks) == 2


@pytest.mark.parametrize("free", [0, None])
def test_insufficient_or_unknown_disk_cannot_pass(free, work_dir):
    result = assess_budget((request(work_dir),), disk_probe=lambda p: ("disk", free))
    assert not result.disk_ok and result.needs_confirmation


def test_cancel_leaves_source_and_target_unchanged(work_dir):
    req = request(work_dir)
    seed(req.output_path)
    before = req.output_path.read_bytes()
    with pytest.raises(BudgetCancelled):
        assess(req, should_cancel=lambda: True)
    assert req.output_path.read_bytes() == before


def test_corrupt_sample_is_ignored_and_model_remains_available(work_dir):
    (work_dir / "broken.mbtiles").write_bytes(b"not a database")
    assert assess(request(work_dir)).items[0].zooms[0].sample_count == 0


def test_existing_wrong_mode_is_rejected(work_dir):
    req = request(work_dir)
    seed(req.output_path, mode=MapType.IMAGE)
    with pytest.raises(ValueError, match="不匹配"):
        assess(req)


def test_overlapping_targets_are_not_double_counted(work_dir):
    req = request(work_dir)
    with pytest.raises(ValueError, match="同一个输出文件"):
        assess(req, replace(req, id="second"))


@pytest.mark.parametrize("budget,reserve", [(0, 2), (-1, 2), (float("inf"), 0), (1, -1), (1, float("nan"))])
def test_invalid_budget_values_are_rejected(budget, reserve):
    with pytest.raises(ValueError):
        BudgetSettings(budget_gib=budget, reserve_gib=reserve)


def test_budget_preferences_default_roundtrip_and_corruption_fallback(preferences):
    assert preferences.storage_budget == BudgetSettings()
    wanted = BudgetSettings(False, 32, 3, True)
    preferences.set_storage_budget(wanted)
    assert preferences.storage_budget == wanted
    assert "download/storage_budget" in preferences.stored_keys()
    preferences.settings.setValue("download/storage_budget", '{"enabled":"bad"}')
    assert preferences.storage_budget == BudgetSettings()
