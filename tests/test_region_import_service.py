"""RegionImportService：管线编排、原子发布、reload 与全链路集成（Slice 26，Spec §13 / §16 / §17 / §22 ~ §25 / §30）。

这一组是**集成层**：单模块的正确性由前面四个测试文件守，这里守"缝在一起
之后仍然成立"的那些事 ——

- validate 是 dry-run（一个字节都不写）；
- 数据不合格时**不发布**，且已发布的旧数据集原样（Spec §13）；
- 发布成功的数据集能被 Slice 18 的目录 / 服务 / Zoom 策略 / 瓦片估算
  **原样**使用（Spec §22 / §23 / §30）；
- reload 之后界面级联立刻读到新数据（Spec §17 / §24）；
- 没有生成数据集时，旧 Region 体系行为不变（Spec §27 兼容性）。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import settings as settings_module
from app.geo import RegionTileEstimator
from app.planning.region_level import RegionLevel
from app.regions import RegionCatalog, RegionService
from app.regions.importer import RegionImportService
from app.regions.importer.models import RegionImportIssueCode

from tests.conftest import REGION_FIXTURE_ROOT, REGION_IMPORT_FIXTURE_ROOT

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture
def service(region_import_output_root: Path) -> RegionImportService:
    """发布目录指向工作区的导入服务。"""
    return RegionImportService(output_root=region_import_output_root)


@pytest.fixture
def published(service: RegionImportService, import_config_factory):
    """先把 sample 夹具发布一遍，返回 (service, config_factory, report)。"""
    report = service.import_dataset(import_config_factory())
    assert report.ok
    return service, import_config_factory, report


# --------------------------------------------------------------------------- #
# validate：dry-run（Spec §19）
# --------------------------------------------------------------------------- #
def test_validate_writes_nothing(service: RegionImportService, import_config_factory) -> None:
    config = import_config_factory()
    result = service.validate(config)

    assert result.verdict.value == "pass"
    assert not config.output_root.exists(), "dry-run 连发布目录都不该建"
    # 父目录里也不该留下 build 残骸。
    leftovers = list(config.output_root.parent.glob(f".{config.output_root.name}.*"))
    assert leftovers == []


def test_validate_reports_the_spec19_statistics(
    service: RegionImportService, import_config_factory
) -> None:
    result = service.validate(import_config_factory())

    lines = result.summary_lines()
    assert lines[0] == "总区域：5"
    assert lines[1] == "COUNTRY：1"
    assert lines[2] == "PROVINCE：2"
    assert lines[3] == "CITY：2"
    assert lines[4] == "COUNTY：0"
    assert lines[5] == "Geometry Invalid：0"
    assert lines[6] == "Missing Parent：0"
    assert lines[7] == "Duplicate ID：0"
    assert lines[-1] == "结果：PASS"


@pytest.mark.parametrize(
    "source, expected_code",
    [
        ("duplicate_id.geojson", RegionImportIssueCode.DUPLICATE_ID),
        ("missing_parent.geojson", RegionImportIssueCode.PARENT_MISSING),
        ("cycle.geojson", RegionImportIssueCode.PARENT_CYCLE),
        ("wrong_level.geojson", RegionImportIssueCode.CITY_UNDER_COUNTRY),
        ("bad_geometry.geojson", RegionImportIssueCode.UNSUPPORTED_GEOMETRY),
    ],
)
def test_validate_fails_on_broken_data(
    service: RegionImportService,
    import_config_factory,
    source: str,
    expected_code: RegionImportIssueCode,
) -> None:
    result = service.validate(import_config_factory(source))

    assert result.verdict.value == "fail"
    assert any(item.code is expected_code for item in result.issues)
    assert not config_output_exists(import_config_factory, source)


def config_output_exists(import_config_factory, source: str) -> bool:
    config = import_config_factory(source)
    return config.output_root.exists()


def test_validate_warns_on_repairable_geometry(
    service: RegionImportService, import_config_factory
) -> None:
    result = service.validate(import_config_factory("repairable.geojson"))

    assert result.verdict.value == "warning"  # 修好了 → 可继续，但要记账
    assert result.geometry_invalid == 1


# --------------------------------------------------------------------------- #
# import：发布布局（Spec §10 / §13）
# --------------------------------------------------------------------------- #
def test_import_publishes_the_full_dataset(
    service: RegionImportService, import_config_factory
) -> None:
    report = service.import_dataset(import_config_factory())

    assert report.ok
    root = report.output_root
    assert (root / "country" / "test_country.geojson").is_file()
    assert (root / "province" / "test_province_a.geojson").is_file()
    assert (root / "province" / "test_province_b.geojson").is_file()
    assert (root / "city" / "test_city_a1.geojson").is_file()
    assert (root / "city" / "test_city_a2.geojson").is_file()
    assert (root / "index.json").is_file()
    assert (root / "manifest.json").is_file()
    assert report.published_files == 5
    assert report.manifest.region_count == 5
    # 发布后没有 build / backup 残骸。
    assert list(root.parent.glob(f".{root.name}.*")) == []


def test_import_failure_keeps_the_old_dataset(
    service: RegionImportService, import_config_factory
) -> None:
    """数据不合格 → 不发布，旧数据集一个字节都没动（Spec §13）。"""
    first = service.import_dataset(import_config_factory())
    assert first.ok
    before = snapshot(import_config_factory().output_root)

    report = service.import_dataset(import_config_factory("duplicate_id.geojson"))

    assert not report.ok
    assert snapshot(import_config_factory().output_root) == before


def test_catalog_validation_gate_blocks_publishing(
    service: RegionImportService,
    import_config_factory,
    monkeypatch,
) -> None:
    """生成产物过不了目录校验 → 不发布（发布前最后一道关）。"""
    from app.regions.importer import import_service as import_service_module
    from app.regions.models import CatalogIssue, CatalogIssueCode, IssueSeverity

    def _fake_validate(build_dir):
        from app.regions.models import CatalogValidationResult

        return (
            CatalogValidationResult(
                issues=(
                    CatalogIssue(
                        CatalogIssueCode.DUPLICATE_ID,
                        "故意制造一条目录期错误",
                        "test_country",
                        IssueSeverity.ERROR,
                    ),
                )
            ),
            [
                import_service_module.RegionImportIssue(
                    RegionImportIssueCode.CATALOG_INVALID,
                    "生成的数据集未通过目录校验",
                    "生成产物",
                )
            ],
        )

    monkeypatch.setattr(import_service_module, "validate_built_catalog", _fake_validate)

    report = service.import_dataset(import_config_factory())

    assert not report.ok
    assert any(
        item.code is RegionImportIssueCode.CATALOG_INVALID for item in report.result.issues
    )
    assert not import_config_factory().output_root.exists()


def test_second_publish_replaces_the_first_atomically(
    service: RegionImportService, import_config_factory
) -> None:
    service.import_dataset(import_config_factory(boundary_version="v1"))
    report = service.import_dataset(
        import_config_factory("alt_fields.geojson", id_field="GBCODE",
                              name_field="NAME", level_field="LEVEL",
                              parent_field="PARENT", boundary_version="v2")
    )

    assert report.ok
    root = report.output_root
    payload = json.loads((root / "index.json").read_text(encoding="utf-8"))
    assert payload["boundaryVersion"] == "v2"
    assert {item["id"] for item in payload["regions"]} == {
        "alt_country",
        "alt_province",
        "alt_city",
    }
    # 上一批的文件不残留（整目录替换，不是合并）。
    assert not (root / "province" / "test_province_a.geojson").exists()


# --------------------------------------------------------------------------- #
# country_id：覆写与沿用（Spec §3 / §9）
# --------------------------------------------------------------------------- #
def test_country_id_override_publishes_under_the_new_id(
    service: RegionImportService, import_config_factory
) -> None:
    report = service.import_dataset(
        import_config_factory(country_id="china")
    )

    assert report.ok
    root = report.output_root
    assert (root / "country" / "china.geojson").is_file()
    assert not (root / "country" / "test_country.geojson").exists()
    payload = json.loads((root / "index.json").read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in payload["regions"]}
    assert by_id["test_province_a"]["parentId"] == "china"


def test_province_only_data_carries_over_the_published_country(
    service: RegionImportService, import_config_factory
) -> None:
    """源数据没有全国条目 + country_id → 沿用输出目录里已发布的全国数据。"""
    service.import_dataset(import_config_factory())
    report = service.import_dataset(
        import_config_factory("province_only.geojson", country_id="test_country",
                              boundary_version="v2")
    )

    assert report.ok
    assert report.carried_over_country is True
    root = report.output_root
    payload = json.loads((root / "index.json").read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in payload["regions"]}
    assert set(by_id) == {"test_country", "test_province_a", "test_city_a1"}
    assert by_id["test_province_a"]["parentId"] == "test_country"
    # 沿用的全国条目文件真的在。
    assert (root / "country" / "test_country.geojson").is_file()


def test_province_only_without_country_id_fails(
    service: RegionImportService, import_config_factory
) -> None:
    """不给 country_id 也不给全国数据 → FAIL，不凭空造全国条目。"""
    report = service.import_dataset(import_config_factory("province_only.geojson"))

    assert not report.ok
    assert any(
        item.code is RegionImportIssueCode.NO_COUNTRY_ENTRY
        for item in report.result.issues
    )
    assert not import_config_factory().output_root.exists()


# --------------------------------------------------------------------------- #
# 生成数据集接入现有体系（Spec §22 / §23 / §30）
# --------------------------------------------------------------------------- #
def test_generated_catalog_answers_hierarchy_queries(
    published, import_config_factory
) -> None:
    _service, _factory, report = published
    service = RegionService(root=report.output_root)

    assert [item.id for item in service.list_countries()] == ["test_country"]
    assert sorted(item.id for item in service.list_provinces("test_country")) == [
        "test_province_a",
        "test_province_b",
    ]
    assert sorted(item.id for item in service.list_cities("test_province_a")) == [
        "test_city_a1",
        "test_city_a2",
    ]
    assert service.path_text("test_city_a1") == "测试国 / 测试甲省 / 测试甲一市"


def test_generated_regions_get_the_right_zoom_recommendations(
    published, import_config_factory
) -> None:
    """Spec §22：Zoom 值仍然只来自 ZoomStrategyService（导入器不存 Zoom）。"""
    _service, _factory, report = published
    service = RegionService(root=report.output_root)

    assert service.get_zoom_recommendation("test_country").range_text == "Z4~10"
    assert service.get_zoom_recommendation("test_province_a").range_text == "Z9~13"
    assert service.get_zoom_recommendation("test_city_a1").range_text == "Z12~16"


def test_generated_regions_feed_the_polygon_download_chain(
    published, import_config_factory
) -> None:
    """Spec §23：RegionService → DownloadRegion → Estimator 全通。"""
    _service, _factory, report = published
    service = RegionService(root=report.output_root)

    for region_id, level in (
        ("test_country", RegionLevel.COUNTRY),
        ("test_province_a", RegionLevel.PROVINCE),
        ("test_city_a1", RegionLevel.CITY),
    ):
        region = service.get_download_region(region_id)
        assert region.region_level is level
        assert region.boundary_version == "2026-10-test"
        estimate = RegionTileEstimator(region).estimate(
            service.get_zoom_recommendation(region_id).min_zoom,
            service.get_zoom_recommendation(region_id).max_zoom,
        )
        assert estimate.selected_tiles > 0
        # 矩形夹具：selected == candidate（几何就是外包矩形）。
        assert estimate.selected_tiles == estimate.candidate_tiles


def test_generated_regions_resolve_for_resume(
    published, import_config_factory
) -> None:
    """续传路径：region_id → 几何（Slice 25 挂上的解析器对生成数据同样成立）。"""
    _service, _factory, report = published
    service = RegionService(root=report.output_root)

    resolved, note = service.resolve_region_geometry(
        "test_city_a1", service.get_download_region("test_city_a1").bounds
    )
    assert resolved is not None
    assert note == ""
    assert resolved.region_id == "test_city_a1"


# --------------------------------------------------------------------------- #
# reload 与界面级联（Spec §17 / §24）
# --------------------------------------------------------------------------- #
def test_reload_switches_to_the_generated_dataset(
    region_import_output_root: Path,
    import_config_factory,
    monkeypatch,
    qtbot=None,
) -> None:
    """发布前用手工目录，发布 + reload 后读到生成数据集，无需重启。"""
    # 让"生成优先"的根指向本用例的发布目录（否则 service 读的是真实的
    # resources/regions/generated，与发布目录对不上）。
    monkeypatch.setattr(settings_module, "REGION_DATA_ROOT", region_import_output_root)
    # 手工目录回到"发行状态"（只有随仓库分发的 index.json），与机器上部署了什么无关。
    # 注意这里要的是 REGION_RESOURCE_DIR（发行态手工根），不是 REGION_DATA_ROOT（生成目录）。
    handmade = region_import_output_root.parent / "handmade"
    handmade.mkdir()
    shutil.copy(settings_module.REGION_RESOURCE_DIR / "index.json", handmade / "index.json")
    monkeypatch.setattr("app.geo.region.REGION_RESOURCE_DIR", handmade)

    service = RegionService(prefer_generated=True)
    assert "test_country" not in service.catalog

    importer = RegionImportService(output_root=region_import_output_root)
    report = importer.import_dataset(import_config_factory())
    assert report.ok

    reloaded = service.reload()
    assert "test_country" in reloaded
    assert service.catalog_error == ""
    assert [item.id for item in service.list_countries()] == ["test_country"]


def test_reload_failure_keeps_the_old_catalog(
    region_import_output_root: Path,
    import_config_factory,
    monkeypatch,
) -> None:
    """坏 generated/index.json 不能把用户退化成扁平预设（Spec §17）。

    发布是原子的，正常不会留下坏文件；但目录可能被外部弄坏。此时
    ``effective_region_root()`` 不该把"文件在但读不出"当成"已发布"——
    否则用户会从上一份能用的手工数据，退化成内置扁平预设（层级全丢）。
    正确行为是**回退到手工目录**，数据原样。
    """
    monkeypatch.setattr(settings_module, "REGION_DATA_ROOT", region_import_output_root)
    handmade = region_import_output_root.parent / "handmade"
    handmade.mkdir()
    shutil.copy(
        settings_module.REGION_RESOURCE_DIR / "index.json", handmade / "index.json"
    )
    monkeypatch.setattr("app.geo.region.REGION_RESOURCE_DIR", handmade)

    service = RegionService(prefer_generated=True)
    before = service.catalog
    assert len(before) >= 1

    # 往生成目录塞一个坏 index.json（模拟"外部弄坏 / 半截写入"）。
    region_import_output_root.mkdir(parents=True)
    (region_import_output_root / "index.json").write_text("{ broken", encoding="utf-8")

    after = service.reload()
    # 坏文件不算"已发布"：root 回退到手工目录，读到的是同一份数据。
    assert after.root == before.root
    assert len(after) == len(before)
    assert [item.id for item in after] == [item.id for item in before]


def test_reload_failure_when_every_root_is_broken_keeps_old_catalog(
    region_import_output_root: Path,
    monkeypatch,
) -> None:
    """手工根自己也坏掉时，reload 才真正失败，且不崩、保留旧目录（Spec §17）。"""
    handmade = region_import_output_root.parent / "handmade_broken"
    handmade.mkdir()
    # 手工根塞坏 index.json；generated 不存在 → effective_region_root 回退到它。
    (handmade / "index.json").write_text("{ broken", encoding="utf-8")
    monkeypatch.setattr("app.geo.region.REGION_RESOURCE_DIR", handmade)

    service = RegionService(prefer_generated=True)
    before = service.catalog  # 首次读就失败 → 内置扁平预设兜底
    assert service.catalog_error  # 记录了这个坏根

    after = service.reload()
    # reload 仍失败：不抛异常，catalog 继续是那份兜底（内容等价），错误原因仍在。
    assert [item.id for item in after] == [item.id for item in before]
    assert service.catalog_error
    assert len(service.catalog) == len(before)


def test_download_panel_cascade_reads_generated_data(
    qapp,
    published,
    import_config_factory,
) -> None:
    """Spec §24：DownloadPanel 的国家 → 省 → 市级联直接读生成 Catalog。"""
    from app.ui.download_panel import DownloadPanel

    _service, _factory, report = published
    service = RegionService(root=report.output_root)
    panel = DownloadPanel(region_service=service)

    country_ids = [
        panel.country_combo.itemData(index).id
        for index in range(panel.country_combo.count())
    ]
    assert "test_country" in country_ids

    panel.region_controller.select_country("test_country")
    panel.reload_region_cascade()
    province_ids = [
        panel.province_combo.itemData(index).id
        for index in range(panel.province_combo.count())
        if panel.province_combo.itemData(index) is not None
    ]
    assert set(province_ids) == {"test_province_a", "test_province_b"}

    panel.region_controller.select_province("test_province_a")
    panel.reload_region_cascade()
    city_ids = [
        panel.city_combo.itemData(index).id
        for index in range(panel.city_combo.count())
        if panel.city_combo.itemData(index) is not None
    ]
    assert set(city_ids) == {"test_city_a1", "test_city_a2"}


def test_panel_sees_new_data_after_reload(
    qapp,
    region_import_output_root: Path,
    import_config_factory,
    monkeypatch,
) -> None:
    """导入 → reload → 重填下拉：面板不需要重建（Spec §17 / §24）。"""
    from app.ui.download_panel import DownloadPanel

    handmade = region_import_output_root.parent / "handmade"
    handmade.mkdir()
    shutil.copy(
        settings_module.REGION_RESOURCE_DIR / "index.json", handmade / "index.json"
    )
    monkeypatch.setattr("app.geo.region.REGION_RESOURCE_DIR", handmade)
    monkeypatch.setattr(settings_module, "REGION_DATA_ROOT", region_import_output_root)

    service = RegionService(prefer_generated=True)
    panel = DownloadPanel(region_service=service)
    before = {
        panel.country_combo.itemData(index).id
        for index in range(panel.country_combo.count())
    }
    assert "test_country" not in before

    importer = RegionImportService(output_root=region_import_output_root)
    assert importer.import_dataset(import_config_factory()).ok

    service.reload()
    panel.reload_region_cascade()
    after = {
        panel.country_combo.itemData(index).id
        for index in range(panel.country_combo.count())
    }
    assert "test_country" in after


# --------------------------------------------------------------------------- #
# 兼容性（Spec §27）
# --------------------------------------------------------------------------- #
def test_existing_fixture_catalog_is_untouched(import_config_factory) -> None:
    """导入管线不碰仓库里的合成目录（6 条记录原样）。"""
    catalog = RegionCatalog.load(root=REGION_FIXTURE_ROOT)
    assert len(catalog) == 6

    service = RegionImportService(output_root=import_config_factory().output_root)
    assert service.import_dataset(import_config_factory()).ok

    assert len(RegionCatalog.load(root=REGION_FIXTURE_ROOT)) == 6


def test_handmade_catalog_keeps_working_without_generated_data(
    monkeypatch, region_root: Path
) -> None:
    """没有生成数据集时，现有 Region Root 的行为不变。"""
    from app.geo import region as region_module

    monkeypatch.setattr(region_module, "REGION_RESOURCE_DIR", region_root)
    monkeypatch.setattr(
        settings_module, "REGION_DATA_ROOT", region_root.parent / "no_generated_here"
    )

    service = RegionService(prefer_generated=True)
    assert service.root == region_root.resolve()
    assert len(service.catalog) == 6  # 合成目录：demoland 那一套
    assert service.catalog_error == ""


def test_importer_does_not_touch_tile_math(import_config_factory) -> None:
    """Spec §21 / §29：导入管线不枚举瓦片、不碰估算器与 MBTiles。

    静态守卫：import_service 的**真实 import 语句**里不许出现 estimator /
    mbtiles 字样（这类依赖一旦接进去，"导一份数据"就会变成"下一遍图"）。
    只看 ``import`` / ``from ... import`` 行——docstring 里解释"为什么不该
    引用它们"的说明文字不算引用。
    """
    source = PROJECT_ROOT / "app" / "regions" / "importer" / "import_service.py"
    import_lines = [
        line
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith(("import ", "from "))
    ]
    for forbidden in ("RegionTileEstimator", "MBTiles", "mbtiles", "estimate("):
        for line in import_lines:
            assert forbidden not in line, (
                f"import_service 不该 import {forbidden}，命中行：{line!r}"
            )


# --------------------------------------------------------------------------- #
# CLI（Spec §20）
# --------------------------------------------------------------------------- #
def _run_cli(args: list[str]) -> int:
    """就地跑 CLI 的 main()（不起子进程：导入是秒级，且要复用当前解释器）。"""
    script = PROJECT_ROOT / "scripts" / "import_regions.py"
    spec = importlib.util.spec_from_file_location("import_regions_cli", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.main(args)


def test_cli_publishes_and_exits_zero(
    region_import_output_root: Path, import_config_factory, capsys
) -> None:
    code = _run_cli(
        [
            "--source",
            str(REGION_IMPORT_FIXTURE_ROOT / "sample.geojson"),
            "--boundary-version",
            "2026-10-cli",
            "--output",
            str(region_import_output_root),
            "--source-name",
            "CLI 测试",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "结果：PASS" in out
    assert "已发布到" in out
    assert (region_import_output_root / "index.json").is_file()


def test_cli_check_mode_exits_nonzero_on_bad_data(
    region_import_output_root: Path, capsys
) -> None:
    code = _run_cli(
        [
            "--source",
            str(REGION_IMPORT_FIXTURE_ROOT / "duplicate_id.geojson"),
            "--boundary-version",
            "t",
            "--output",
            str(region_import_output_root),
            "--check",
        ]
    )

    assert code == 1
    assert "结果：FAIL" in capsys.readouterr().out
    assert not region_import_output_root.exists(), "--check 一个字节都不写"


def test_cli_exits_two_on_missing_source(region_import_output_root: Path) -> None:
    code = _run_cli(
        [
            "--source",
            str(REGION_IMPORT_FIXTURE_ROOT / "nope.geojson"),
            "--boundary-version",
            "t",
            "--output",
            str(region_import_output_root),
        ]
    )
    assert code == 2


def test_cli_accepts_alternative_field_names_and_level_aliases(
    region_import_output_root: Path, capsys
) -> None:
    code = _run_cli(
        [
            "--source",
            str(REGION_IMPORT_FIXTURE_ROOT / "alt_fields.geojson"),
            "--id-field",
            "GBCODE",
            "--name-field",
            "NAME",
            "--level-field",
            "LEVEL",
            "--parent-field",
            "PARENT",
            "--boundary-version",
            "t",
            "--output",
            str(region_import_output_root),
        ]
    )

    assert code == 0
    assert "结果：PASS" in capsys.readouterr().out


def test_cli_rejects_bad_level_alias(region_import_output_root: Path) -> None:
    code = _run_cli(
        [
            "--source",
            str(REGION_IMPORT_FIXTURE_ROOT / "sample.geojson"),
            "--boundary-version",
            "t",
            "--output",
            str(region_import_output_root),
            "--level-alias",
            "village=town",
        ]
    )
    assert code == 2


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def snapshot(root: Path) -> dict[str, bytes]:
    """目录内容的字节快照（原子性断言用：比 mtime 靠谱，比递归 diff 简单）。"""
    if not root.is_dir():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
