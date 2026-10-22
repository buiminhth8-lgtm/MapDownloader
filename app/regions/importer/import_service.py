"""行政区数据导入服务（Slice 26，Spec §13 / §19 / §21）。

把整条管线缝在一起，对外只有两个动作：

- :meth:`RegionImportService.validate` —— **dry-run**：读源数据、标准化、
  建层级、统计，一个字节都不写。界面的「验证」按钮与 CLI 的 ``--check``
  都走它（Spec §19）。
- :meth:`RegionImportService.import_dataset` —— 全流程 + 原子发布
  （Spec §13）。

性能纪律（Spec §21）
--------------------

行政区导入是**预处理阶段**：不做 Tile 枚举、不碰
:class:`~app.geo.RegionTileEstimator`、不读不写 MBTiles。整条路上唯一
的几何运算逐 Feature 进行（is_valid / buffer / simplify / bounds），
几千条 Feature 是秒级的事。谁把估算器接进导入器，谁就把"导一份数据"
变成了"下一遍图"。

失败语义
--------

- **数据不合格**（verdict FAIL）→ 不发布，返回带问题的
  :class:`~app.regions.importer.models.ImportReport`（``ok=False``）。
  不是异常：用户要的是"第几条不行"，不是调用栈。
- **环境跑不完**（源文件读不出、发布被占用）→ 抛
  :class:`~app.regions.importer.models.RegionImportError` /
  :class:`~app.regions.importer.region_exporter.PublishError`。
- **发布中途失败** → build 目录删掉，旧数据集原样（Spec §13）。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from app.config.settings import REGION_DATA_ROOT
from app.geo.geojson_loader import GeoJSONError
from app.regions.catalog import RegionCatalog
from app.regions.importer.catalog_generator import generate_dataset
from app.regions.importer.hierarchy_builder import build_hierarchy
from app.regions.importer.models import (
    RegionDatasetManifest,
    RegionImportConfig,
    RegionImportError,
    RegionImportIssue,
    RegionImportIssueCode,
    RegionImportResult,
    RegionSourceFeature,
    ImportReport,
)
from app.regions.importer.normalizer import count_levels, normalize_features
from app.regions.importer.region_exporter import (
    PublishError,
    discard_build,
    publish_dataset,
    validate_built_catalog,
)
from app.regions.importer.source_reader import read_source
from app.regions.models import RegionCatalogError

__all__ = ["RegionImportService"]

_logger = logging.getLogger(__name__)

#: 结果统计里"几何问题"包含的机器码（Spec §19 的 Geometry Invalid 一行）。
_GEOMETRY_ISSUE_CODES = frozenset(
    {
        RegionImportIssueCode.GEOMETRY_INVALID,
        RegionImportIssueCode.GEOMETRY_OUT_OF_RANGE,
        RegionImportIssueCode.GEOMETRY_REPAIRED,
        RegionImportIssueCode.UNSUPPORTED_GEOMETRY,
        RegionImportIssueCode.EMPTY_GEOMETRY,
        RegionImportIssueCode.UNPARSEABLE_GEOMETRY,
    }
)

#: 结果统计里"父级问题"包含的机器码（Spec §19 的 Missing Parent 一行）。
_PARENT_ISSUE_CODES = frozenset(
    {
        RegionImportIssueCode.PARENT_MISSING,
        RegionImportIssueCode.SELF_PARENT,
    }
)


def count_source_features(path: str | Path) -> int:
    """数一数源数据里的 Feature 总数（结果统计的"总区域"分母）。

    单独一个函数而不是塞进 :func:`read_source` 的返回值：读取路径要的是
    "活下来的条目"，统计要的是"原来的条目"，两个数放在一起迟早有人
    只返回一个。代价是 JSON 被解析两遍 —— 导入工具的源文件是 MB 级，
    这一点解析时间相对 shapely 的几何校验可以忽略。
    """
    target = Path(path)
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 0
    if not isinstance(document, dict):
        return 0
    if document.get("type") == "Feature":
        return 1
    features = document.get("features")
    return len(features) if isinstance(features, list) else 0


class RegionImportService:
    """行政区数据导入管线（Spec §1 的 ``import_service``）。

    Attributes:
        output_root: 默认发布目录（``resources/regions/generated/``）。
            每个配置也可以自己指定 —— 构造函数这一个只是"用户没指定时"
            的缺省值。
    """

    def __init__(self, *, output_root: Path | None = None) -> None:
        self._output_root = Path(output_root) if output_root is not None else Path(REGION_DATA_ROOT)

    @property
    def output_root(self) -> Path:
        """默认发布目录。"""
        return self._output_root

    # ------------------------------------------------------------------ #
    # 验证（dry-run，Spec §19）
    # ------------------------------------------------------------------ #
    def validate(self, config: RegionImportConfig) -> RegionImportResult:
        """跑一遍除发布以外的全流程，返回 PASS / WARNING / FAIL。

        不写任何文件（连 build 目录都不建）。FAIL 时
        :attr:`RegionImportResult.verdict` 为 ``FAIL``，调用方**禁止**发布。
        """
        issues: list[RegionImportIssue] = []
        entries, _carried, checked = self._prepare(config, issues)
        return self._build_result(issues, entries, checked)

    # ------------------------------------------------------------------ #
    # 导入（全流程 + 原子发布，Spec §13）
    # ------------------------------------------------------------------ #
    def import_dataset(self, config: RegionImportConfig) -> ImportReport:
        """导入并发布一个行政区数据集。

        Returns:
            :class:`~app.regions.importer.models.ImportReport`。
            ``report.ok`` 为 ``False`` 表示数据不合格、**没有发布**；
            为 ``True`` 表示已发布到 ``config.output_root``。

        Raises:
            RegionImportError: 源文件级别的硬错误（读不出 / 结构不对）。
            PublishError: 发布被占用等环境错误（旧数据未动）。
        """
        issues: list[RegionImportIssue] = []
        entries, carried, checked = self._prepare(config, issues)
        result = self._build_result(issues, entries, checked)
        if not result.ok:
            _logger.info("行政区数据导入未通过验证，已取消发布：%s", config.describe())
            return ImportReport(
                result=result,
                output_root=Path(config.output_root),
                manifest=RegionDatasetManifest(boundary_version=config.boundary_version),
                published_files=0,
                carried_over_country=carried,
            )

        output_root = Path(config.output_root)
        build_dir = output_root.parent / f".{output_root.name}.build-{time.time_ns()}"
        try:
            build_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise RegionImportError(
                f"构建目录建不出来：{build_dir}（{exc}）"
            ) from exc

        try:
            generated = generate_dataset(entries, config, build_dir)
            # Catalog validation：用 Slice 18 自己的尺子量一遍产物（Spec §13）。
            _catalog_result, catalog_issues = validate_built_catalog(build_dir)
            if catalog_issues:
                issues.extend(catalog_issues)
                result = self._build_result(issues, entries, checked)
                discard_build(build_dir)
                _logger.info(
                    "生成的数据集未通过目录校验，已取消发布：%s",
                    "；".join(item.describe() for item in catalog_issues[:3]),
                )
                return ImportReport(
                    result=result,
                    output_root=output_root,
                    manifest=generated.manifest,
                    published_files=0,
                    carried_over_country=carried,
                )
            publish_dataset(build_dir, output_root)
        except PublishError:
            discard_build(build_dir)
            raise
        except OSError as exc:  # 生成过程中的 IO 错误：清掉 build，旧数据不动
            discard_build(build_dir)
            raise RegionImportError(f"生成数据集时写入失败：{exc}") from exc

        _logger.info(
            "行政区数据集已发布：%s（%d 个边界文件）",
            output_root,
            generated.geojson_count,
        )
        return ImportReport(
            result=result,
            output_root=output_root,
            manifest=generated.manifest,
            published_files=generated.geojson_count,
            carried_over_country=carried,
        )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _prepare(
        self, config: RegionImportConfig, issues: list[RegionImportIssue]
    ) -> tuple[tuple[Any, ...], bool, int]:
        """读 → （沿用全国条目）→ 标准化 → 建层级。

        Returns:
            ``(entries, carried_over_country, checked)``。
        """
        features, read_issues = read_source(config)
        features = list(features)  # 沿用全国条目时要往里追加
        issues.extend(read_issues)
        checked = count_source_features(config.source_file) or len(features)

        carried = False
        if not any(item.level.is_country for item in features):
            carried = self._carry_over_country(config, features, issues)

        alive = normalize_features(
            features,
            simplify_tolerance=config.simplify_tolerance,
            issues=issues,
        )
        entries = build_hierarchy(alive, config, issues=issues)
        return entries, carried, checked

    def _carry_over_country(
        self,
        config: RegionImportConfig,
        features: list[RegionSourceFeature],
        issues: list[RegionImportIssue],
    ) -> bool:
        """源数据没有全国级条目时，从输出目录已发布数据集沿用全国条目（Spec §3 / §9）。

        只有在 ``config.country_id`` 给了、且输出目录里真有一份同 id 的
        合法全国数据时才成功。**不凭空造**：造一个没有几何的全国条目，
        界面上会显示"中国"，点下去才报错 —— 那比导入失败更难排查。
        """
        if not config.country_id:
            return False
        from app.regions.loader import RegionLoader

        output_root = Path(config.output_root)
        try:
            catalog = RegionCatalog.load(root=output_root)
        except RegionCatalogError:
            return False  # 没有已发布数据集：让 build_hierarchy 报 NO_COUNTRY_ENTRY
        wanted = config.country_id.strip().lower()
        entry = catalog.get(wanted)
        if entry is None or not entry.is_country:
            return False
        loader = RegionLoader(catalog, root=output_root)
        try:
            document = loader.load_document(entry.id)
        except (RegionCatalogError, GeoJSONError) as exc:
            issues.append(
                RegionImportIssue(
                    RegionImportIssueCode.CATALOG_INVALID,
                    f"想沿用输出目录里的全国条目 {entry.id!r}，但它的边界文件"
                    f"读不出来：{exc}",
                    entry.id,
                )
            )
            return False

        from shapely.geometry import mapping

        features.append(
            RegionSourceFeature(
                source_index=-1,
                raw_properties=dict(document.properties),
                raw_geometry=dict(mapping(document.geometry)),
                geometry=document.geometry,
                geometry_modified=True,
                geometry_note="全国条目沿用自已发布数据集（几何经一次 shapely round-trip）",
                region_id=entry.id,
                level=entry.level,  # 沿用条目的等级就是它自己的（全国级）
                parent_ref="",
                name=entry.name,
                code=entry.code or entry.id,
            )
        )
        _logger.info("源数据没有全国级条目，已沿用输出目录里的 %s", entry.id)
        return True

    @staticmethod
    def _build_result(
        issues: list[RegionImportIssue],
        entries: tuple[Any, ...],
        checked: int,
    ) -> RegionImportResult:
        """汇总问题与统计，产出 Spec §19 的结果模型。"""
        geometry_invalid = sum(
            1 for item in issues if item.code in _GEOMETRY_ISSUE_CODES
        )
        missing_parent = sum(1 for item in issues if item.code in _PARENT_ISSUE_CODES)
        duplicate_id = sum(
            1
            for item in issues
            if item.code is RegionImportIssueCode.DUPLICATE_ID
        )
        return RegionImportResult(
            issues=tuple(issues),
            counts=count_levels(entries),
            geometry_invalid=geometry_invalid,
            missing_parent=missing_parent,
            duplicate_id=duplicate_id,
            checked=checked,
        )
