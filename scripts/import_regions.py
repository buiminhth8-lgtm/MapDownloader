"""行政区数据导入 CLI（Slice 26，Spec §20）。

把一份授权行政区 GeoJSON 发布成 RegionCatalog 直接可用的生成数据集::

    python scripts/import_regions.py \\
        --source regions.geojson \\
        --id-field code --name-field name \\
        --level-field level --parent-field parent \\
        --boundary-version 2026-10 \\
        --output resources/regions/generated

常用可选项：

- ``--check``：只验证不发布（dry-run，一个字节都不写）；
- ``--country-id``：全国条目的期望 id（源数据没有全国条目时，沿用输出
  目录里已发布的同 id 全国数据，详见 import_service 的说明）；
- ``--source-name`` / ``--source-license`` / ``--source-description``：
  写进 dataset manifest 的来源与授权信息（Spec §14 / §15）；
- ``--simplify-tolerance``：几何简化容差（度）。**默认不简化** ——
  简化会改变边界，可能影响「哪些瓦片落在区域内」（Spec §6）；
- ``--level-alias key=value``：追加等级映射（可重复），例如
  ``--level-alias level2=province``。

退出码：``0`` = 验证通过且已发布（或 ``--check`` 通过）；``1`` = 数据
不合格（未发布）；``2`` = 环境错误（文件读不出 / 输出被占用）。

本工具**不抓数据**：只处理 ``--source`` 指向的本机文件，不在线抓取行政
边界、不调用第三方地图接口下载边界（Spec §29）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config.settings import REGION_DATA_ROOT  # noqa: E402
from app.planning.region_level import RegionLevel  # noqa: E402
from app.regions.importer import (  # noqa: E402
    RegionImportConfig,
    RegionImportError,
    RegionImportService,
)
from app.regions.importer.region_exporter import PublishError  # noqa: E402

#: 退出码：数据不合格（Spec §20：CLI 失败必须 non-zero exit code）。
EXIT_DATA_INVALID = 1

#: 退出码：环境错误（源文件读不出 / 发布位置被占用）。
EXIT_ENVIRONMENT = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="行政区数据导入：授权 GeoJSON → RegionCatalog 生成数据集",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="源 GeoJSON 文件（FeatureCollection；Polygon / MultiPolygon）",
    )
    parser.add_argument("--id-field", default="code", help="取 region id 的属性名（默认 code）")
    parser.add_argument("--name-field", default="name", help="取展示名的属性名（默认 name）")
    parser.add_argument(
        "--level-field", default="level", help="取行政等级的属性名（默认 level）"
    )
    parser.add_argument(
        "--parent-field",
        default="parent",
        help="取父级 id 的属性名（默认 parent；传空串表示源数据没有父级列）",
    )
    parser.add_argument(
        "--boundary-version",
        required=True,
        help="数据集版本号（写进每个边界文件、index.json 与 manifest）",
    )
    parser.add_argument(
        "--output",
        default=str(REGION_DATA_ROOT),
        help=f"发布目录（默认 {REGION_DATA_ROOT}）",
    )
    parser.add_argument(
        "--country-id",
        default=None,
        help="全国级条目的期望 id；源数据没有全国条目时沿用输出目录里已发布的同 id 数据",
    )
    parser.add_argument("--source-name", default="", help="数据来源名称（写进 manifest）")
    parser.add_argument("--source-license", default="", help="授权说明，如审图号（写进 manifest）")
    parser.add_argument("--source-description", default="", help="补充说明（写进 manifest）")
    parser.add_argument(
        "--simplify-tolerance",
        type=float,
        default=None,
        help="几何简化容差（度）；默认不简化（Spec §6）",
    )
    parser.add_argument(
        "--level-alias",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="追加等级映射，可重复；VALUE 取值 country/province/city",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只验证不发布（dry-run）",
    )
    return parser


def parse_level_aliases(items: list[str]) -> dict[str, str]:
    """解析 ``--level-alias key=value``（值必须是三个行政等级之一）。"""
    aliases: dict[str, str] = {}
    allowed = {item.value for item in RegionLevel if item.is_administrative}
    for item in items:
        if "=" not in item:
            raise ValueError(f"--level-alias 的格式应是 key=value，实际是 {item!r}")
        key, _, value = item.partition("=")
        key = key.strip()
        value = value.strip().lower()
        if not key or value not in allowed:
            raise ValueError(
                f"--level-alias {item!r} 不成立：value 必须是 "
                + "、".join(sorted(allowed))
            )
        aliases[key] = value
    return aliases


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        level_aliases = parse_level_aliases(args.level_alias)
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return EXIT_ENVIRONMENT

    config = RegionImportConfig(
        source_file=Path(args.source),
        id_field=args.id_field,
        name_field=args.name_field,
        level_field=args.level_field,
        parent_field=args.parent_field.strip() or None,
        output_root=Path(args.output),
        boundary_version=args.boundary_version,
        country_id=args.country_id,
        source_name=args.source_name,
        source_license=args.source_license,
        source_description=args.source_description,
        simplify_tolerance=args.simplify_tolerance,
        level_aliases=level_aliases or None,
    )

    service = RegionImportService(output_root=Path(args.output))

    try:
        if args.check:
            result = service.validate(config)
            _print_result(result, published=None)
            return 0 if result.ok else EXIT_DATA_INVALID
        report = service.import_dataset(config)
    except RegionImportError as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return EXIT_ENVIRONMENT
    except PublishError as exc:
        print(f"发布失败：{exc}", file=sys.stderr)
        return EXIT_ENVIRONMENT

    _print_result(report.result, published=report.output_root if report.ok else None)
    if report.ok:
        print(f"数据集 manifest：{report.manifest.summary_text()}")
        return 0
    return EXIT_DATA_INVALID


def _print_result(result, *, published: Path | None) -> None:
    """Spec §19 的统计行 + 问题明细（界面与 CLI 共用同一套行）。"""
    print("—" * 60)
    for line in result.summary_lines():
        print(line)
    if result.issues:
        print("问题明细：")
        for issue in result.issues[:50]:
            print(f"  - {issue.describe()}")
        if len(result.issues) > 50:
            print(f"  ……另有 {len(result.issues) - 50} 条")
    if published is not None:
        print(f"已发布到：{published}")
    print("—" * 60)


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
