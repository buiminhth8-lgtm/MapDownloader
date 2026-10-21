"""目录校验：快速档与深度档（Slice 18，Spec §13）。

两档的差别不是"严格程度"，而是**代价**：

- 快速档只 stat 文件，一个字节都不读 —— 可以在启动、保存、刷新时随手跑；
- 深度档把每个 GeoJSON 都解析一遍，一百个省就是几百 MB，
  只能在排查、验收、CI 里显式打开。

所以这一组用例的重点是**"快速档真的没读文件"**：把"能不能跑得快"变成一条可
断言的契约，而不是一句注释里的承诺。否则某天有人在快速档里顺手加一句
"顺便检查一下几何"，谁也不会发现 —— 直到用户报告"启动要三十秒"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.planning import RegionLevel
from app.regions import (
    RegionCatalog,
    RegionLoader,
    RegionService,
    validate_catalog,
)
from app.regions.models import CatalogIssueCode, IssueSeverity, RegionPreset

OTHER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "regions"


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _entry(region_id: str, relative: str, **overrides) -> RegionPreset:
    base = {
        "id": region_id,
        "name": f"示例-{region_id}",
        "level": RegionLevel.PROVINCE,
        "parent_id": None,
        "geojson_path": relative,
    }
    base.update(overrides)
    return RegionPreset(**base)


def _catalog(root: Path, *entries: RegionPreset) -> RegionCatalog:
    return RegionCatalog(entries, root=root)


def _install(root: Path, relative: str, source_name: str) -> RegionPreset:
    import shutil

    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(OTHER_FIXTURES / source_name, target)
    return _entry(Path(relative).stem, relative)


def _codes(result) -> set[CatalogIssueCode]:
    return {item.code for item in result.issues}


# --------------------------------------------------------------------------- #
# 快速档
# --------------------------------------------------------------------------- #
def test_fast_validation_does_not_parse_any_geojson(region_root: Path, monkeypatch) -> None:
    """快速档必须**一个文件都不读**（Spec §13："普通启动不要解析所有大 GeoJSON"）。"""
    from app.geo import geojson_loader
    from app.regions import loader as loader_module

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("快速档不该解析 GeoJSON")

    # 两条入口都堵上：加载器自己的引用 + 底层 geojson_loader 的原函数。
    # 只堵一条的话，将来有人绕开加载器直接调底层，这条用例就悄悄失效了。
    monkeypatch.setattr(loader_module, "load_geojson_document", boom)
    monkeypatch.setattr(geojson_loader, "load_geojson_document", boom)

    result = validate_catalog(RegionCatalog.load(root=region_root), deep=False)

    assert result.deep is False
    assert result.ok is True
    assert result.checked == 6


def test_fast_validation_checks_the_file_exists(region_root: Path) -> None:
    (region_root / "city" / "rivertown.geojson").unlink()
    catalog = RegionCatalog.load(root=region_root)

    result = validate_catalog(catalog)

    assert result.has(CatalogIssueCode.GEOJSON_NOT_FOUND)
    (issue,) = [item for item in result.issues if item.code is CatalogIssueCode.GEOJSON_NOT_FOUND]
    assert issue.region_id == "rivertown"
    assert issue.severity is IssueSeverity.WARNING
    assert issue.is_error is False


def test_a_missing_boundary_does_not_make_the_catalog_invalid(
    region_root: Path,
) -> None:
    """缺数据是可预期的部署状态 —— 校验"通过"，但带着提醒。"""
    (region_root / "city" / "rivertown.geojson").unlink()

    result = validate_catalog(RegionCatalog.load(root=region_root))

    assert result.ok is True
    assert len(result.warnings) == 1
    assert len(result.errors) == 0


def test_fast_validation_reports_a_path_that_escapes_the_root(region_root: Path) -> None:
    """越界路径在目录层就被拦了，所以这里只能靠"绕过模型"的方式造出来。"""
    entry = _entry("evil", "city/rivertown.geojson")
    object.__setattr__(entry, "geojson_path", "../escape.geojson")

    result = validate_catalog(_catalog(region_root, entry))

    assert result.has(CatalogIssueCode.UNSAFE_PATH)
    assert result.ok is False


def test_fast_validation_carries_the_loading_issues(region_root: Path) -> None:
    """读目录时记下的问题（重复 id 之类）要出现在校验结果里，不能丢。"""
    payload = {
        "version": 1,
        "regions": [
            {"id": "a", "name": "A", "level": "country", "geojson": "country/demoland.geojson"},
            {"id": "a", "name": "重复的", "level": "country", "geojson": "country/demoland.geojson"},
        ],
    }
    catalog = RegionCatalog.from_payload(payload, root=region_root)

    result = validate_catalog(catalog)

    assert result.has(CatalogIssueCode.DUPLICATE_ID)
    assert result.checked == 1, "被跳过的那条不计入"


def test_validate_rejects_a_non_catalog() -> None:
    with pytest.raises(ValueError, match="RegionCatalog"):
        validate_catalog([], deep=False)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 深度档
# --------------------------------------------------------------------------- #
def test_deep_validation_passes_on_a_healthy_catalog(region_catalog: RegionCatalog) -> None:
    result = validate_catalog(region_catalog, deep=True)

    assert result.ok is True
    assert result.issues == ()
    assert result.deep is True


def test_deep_validation_reads_every_boundary(region_catalog: RegionCatalog, monkeypatch) -> None:
    """深度档要看得出它**确实读了**（否则"深度"是假的）。"""
    calls: list[str] = []
    original = RegionLoader.load_document

    def spy(self, region_id, **kwargs):  # noqa: ANN001, ANN202
        calls.append(self.region(region_id).id)
        return original(self, region_id, **kwargs)

    monkeypatch.setattr(RegionLoader, "load_document", spy)

    validate_catalog(region_catalog, deep=True)

    assert sorted(calls) == [
        "demoland",
        "hilltown",
        "lakecity",
        "northland",
        "rivertown",
        "southland",
    ]


@pytest.mark.parametrize(
    "source, region_id",
    [
        ("broken_json.geojson", "broken"),
        ("point_only.geojson", "points"),
        ("line_string.geojson", "lines"),
        ("empty_features.geojson", "empty"),
        ("bowtie.geojson", "bowtie"),
    ],
)
def test_deep_validation_flags_unusable_boundaries(
    region_root: Path, source: str, region_id: str
) -> None:
    """坏 JSON / 只有点线 / 空 / 自相交 —— 都归到"这条数据没有可用几何"。"""
    entry = _install(region_root, f"province/{region_id}.geojson", source)
    catalog = _catalog(region_root, entry)

    result = validate_catalog(catalog, deep=True)

    assert result.has(CatalogIssueCode.BOUNDARY_UNREADABLE)
    (issue,) = result.errors
    assert issue.region_id == region_id
    assert issue.message, "要把加载器说的原因带上，用户才知道去修什么"


def test_deep_validation_does_not_repeat_the_missing_file_warning(
    region_root: Path,
) -> None:
    """文件不在时深度档不再报一次 —— 同一个毛病报两遍只会稀释信号。"""
    (region_root / "city" / "rivertown.geojson").unlink()
    catalog = RegionCatalog.load(root=region_root)

    result = validate_catalog(catalog, deep=True)

    assert result.count(CatalogIssueCode.GEOJSON_NOT_FOUND) == 1
    assert result.count(CatalogIssueCode.BOUNDARY_UNREADABLE) == 0


def test_deep_validation_catches_a_handwritten_bounds(region_root: Path) -> None:
    """``index.json`` 里手抄的 bounds 与几何对不上 → 报错（Spec §11）。

    这正是"手填 73/3/136/54"那类做法的检测器：它看起来很像真的，
    但会让候选瓦片数与真实边界悄悄错开。
    """
    entry = _entry(
        "rivertown",
        "city/rivertown.geojson",
        level=RegionLevel.CITY,
        bounds=None,
    )
    from app.geo import GeoBounds

    entry = entry.with_bounds(GeoBounds(100.0, 20.0, 130.0, 50.0))
    catalog = _catalog(region_root, entry)

    result = validate_catalog(catalog, deep=True)

    assert result.has(CatalogIssueCode.BOUNDS_MISMATCH)
    (issue,) = result.errors
    assert issue.region_id == "rivertown"
    assert "删掉" in issue.message, "要给出可执行的修法"


def test_deep_validation_accepts_a_correct_bounds(region_root: Path) -> None:
    """对照组：写对了的 bounds 不报错（否则上一条可能只是"凡有 bounds 就报"）。"""
    from app.geo import GeoBounds

    entry = _entry("rivertown", "city/rivertown.geojson", level=RegionLevel.CITY)
    entry = entry.with_bounds(GeoBounds(110.0, 33.0, 111.0, 34.0))

    result = validate_catalog(_catalog(region_root, entry), deep=True)

    assert result.ok is True, result.as_lines()


# --------------------------------------------------------------------------- #
# 报告形态
# --------------------------------------------------------------------------- #
def test_report_lines_are_human_readable(region_root: Path) -> None:
    """报告正文说人话（不带机器码），机器码只出现在末尾的统计行里。

    这条沿用 Slice 14 的约定：错误码给日志与测试，界面与报告给人话。
    """
    (region_root / "city" / "rivertown.geojson").unlink()
    result = validate_catalog(RegionCatalog.load(root=region_root))

    lines = result.as_lines()
    body = lines[:-1]
    assert all("geojson_not_found" not in line for line in body)
    assert any("rivertown" in line for line in body)
    assert "机器码统计：geojson_not_found×1" in lines[-1]
    assert lines[0].startswith("行政区目录校验：通过")


def test_issue_distinguishes_log_line_from_display_line() -> None:
    from app.regions.models import CatalogIssue

    issue = CatalogIssue(CatalogIssueCode.PARENT_MISSING, "上级没了", "x")

    assert issue.as_line() == "error parent_missing [x] 上级没了"
    assert issue.describe() == "x：上级没了"
    assert "parent_missing" not in issue.describe()


def test_result_helpers_count_and_lookup() -> None:
    from app.regions.models import CatalogIssue, CatalogValidationResult

    result = CatalogValidationResult(
        issues=(
            CatalogIssue(CatalogIssueCode.DUPLICATE_ID, "重复", "a"),
            CatalogIssue(
                CatalogIssueCode.GEOJSON_NOT_FOUND,
                "没放",
                "b",
                severity=IssueSeverity.WARNING,
            ),
        ),
        checked=2,
    )

    assert result.ok is False, "有一条 error（重复 id）"
    assert result.count(CatalogIssueCode.DUPLICATE_ID) == 1
    assert result.has(CatalogIssueCode.GEOJSON_NOT_FOUND) is True
    assert result.has(CatalogIssueCode.PARENT_CYCLE) is False
    assert len(result.errors) == 1 and len(result.warnings) == 1


def test_issue_rejects_unknown_codes_and_severities() -> None:
    from app.regions.models import CatalogIssue

    with pytest.raises(ValueError, match="code"):
        CatalogIssue("duplicate_id", "x")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="severity"):
        CatalogIssue(CatalogIssueCode.DUPLICATE_ID, "x", severity="warning")  # type: ignore[arg-type]


def test_every_issue_code_is_part_of_the_contract() -> None:
    """机器码集合就是这份契约的清单：增删都要有人改这一行。

    留着"永远不会被产生"的僵尸枚举最坑：看代码的人会以为某条路径存在，
    于是照它写分支 —— 而那段分支永远走不到。
    """
    assert {item.value for item in CatalogIssueCode} == {
        "index_version_unsupported",
        "entry_not_object",
        "missing_id",
        "duplicate_id",
        "missing_name",
        "missing_level",
        "unknown_level",
        "bounds_malformed",
        "unsafe_path",
        "missing_geojson_path",
        "geojson_not_found",
        "parent_missing",
        "self_parent",
        "country_has_parent",
        "parent_level_invalid",
        "parent_cycle",
        "boundary_unreadable",
        "bounds_mismatch",
    }


# --------------------------------------------------------------------------- #
# 通过服务层（Spec §13 的"启动或测试时可执行"）
# --------------------------------------------------------------------------- #
def test_service_exposes_validation(region_root: Path) -> None:
    service = RegionService(root=region_root)

    assert service.validate().ok is True
    assert service.validate(deep=True).ok is True


def test_service_validation_reports_a_broken_file(region_root: Path) -> None:
    (region_root / "country" / "demoland.geojson").write_text("{坏了", encoding="utf-8")

    result = RegionService(root=region_root).validate(deep=True)

    assert result.has(CatalogIssueCode.BOUNDARY_UNREADABLE)


def test_shipped_catalog_is_valid_but_warns_about_the_missing_boundary(
    shipped_catalog_root: Path,
) -> None:
    """仓库自带目录：本身合法（ok=True），只是还缺那份要用户自己提供的国界数据。

    ``shipped_catalog_root``：验的是**发行状态**（仓库只带 index.json，不带边界）。
    开发/验收机上可能已经部署了授权边界数据，直接读默认目录会让这条用例
    在别人的机器上结论相反（Slice 25 实测）。
    """
    result = RegionService(root=shipped_catalog_root).validate()

    assert result.ok is True
    assert result.checked == 1
    assert result.has(CatalogIssueCode.GEOJSON_NOT_FOUND)


def test_index_version_is_read_from_json(region_root: Path) -> None:
    """``version`` 只是声明，不会因为"多了"就拒读（见目录用例），但也没被忽略。"""
    path = region_root / "index.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = 2
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = validate_catalog(RegionCatalog.load(root=region_root))

    assert result.has(CatalogIssueCode.INDEX_VERSION_UNSUPPORTED)
    assert result.ok is False
