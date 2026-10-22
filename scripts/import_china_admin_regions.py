#!/usr/bin/env python
"""导入天地图中国行政区划三份数据，生成「国 → 省 → 市 → 县」四级 RegionCatalog。

数据来源：天地图官网行政区划可视化数据（用户自己下载的三份 GeoJSON）。
本脚本只做**数据整理 + 导入**：不下载、不抓取、不分发边界数据（Spec §29）。

三份数据的结构（实测）：

- 省：42 个 Feature（含"境界线"），字段 ``{name, gb}``，gb = ``"156" + 6 位 GB 码``
- 市：388 个 Feature，字段 ``{name, gb}``
- 县：2897 个 Feature，字段 ``{name, gb}``

处理步骤：

1. 过滤 ``gb`` 为空的"境界线"Feature（国界 / 省界线，不是行政区）。
2. 合成全国条目（``id=china``，几何 = 42 个省几何的 ``unary_union``）——
   三份数据里没有全国边界，而层级规则要求省级挂在国家级下面；这里**不凭空
   造一个矩形**，用省界的并集当全国边界（全国范围下载时按它精确裁剪）。
3. 父级关系从 ``gb`` 前缀推导：
   - 市 → 省（``gb[:3] + gb[3:5] + "0000"``）
   - 县 → 市（``gb[:3] + gb[3:7] + "00"``）；直辖市 / 省直辖县级市**没有市级
     上级**，直接挂省（``gb[:3] + gb[3:5] + "0000"``）。
4. 合并成一份带 ``level`` / ``parent`` 字段的 FeatureCollection。
5. 走 Slice 26 导入管线，原子发布到 ``resources/regions/generated/``。

用法：:

    python scripts/import_china_admin_regions.py \\
        --province "Downloads/中国_省 (1).geojson" \\
        --city "Downloads/中国_市.geojson" \\
        --county "Downloads/中国_县.geojson" \\
        --boundary-version 2026-10 \\
        --check          # 先只验证不发布

验证通过后去掉 ``--check`` 真正发布。失败以 non-zero exit code 退出。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

#: 全国条目的固定 id（与 resources/regions/index.json 里 china 预设一致）。
CHINA_ID = "china"

#: 省 / 市 / 县在合并数据里的 level 字面量（导入管线经 DEFAULT_LEVEL_ALIASES 映射）。
_PROVINCE_LEVEL = "province"
_CITY_LEVEL = "city"
_COUNTY_LEVEL = "county"


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _is_region(feature: dict) -> bool:
    """是不是行政区（过滤 gb 为空的"境界线"）。"""
    return bool((feature.get("properties") or {}).get("gb", ""))


def _merge(
    province_file: Path,
    city_file: Path,
    county_file: Path,
    merged_path: Path,
) -> tuple[int, int, int]:
    """把三份数据合并成一份带 level/parent 的 FeatureCollection。

    gb 编码（``"156" + 6 位 GB 码``）：省码 = ``gb[3:5]``、市码 = ``gb[5:7]``、
    县码 = ``gb[7:9]``。因此省的后四位是 ``0000``、市的后两位是 ``00``。

    天地图把三类"行政级别有歧义"的单位放进了多个文件，这里按编码规则去重：

    - **直辖市 / 港澳台**（省 ∩ 市，7 个）：省文件里已是省级，跳过市文件那份；
    - **澳门**（省 ∩ 市 ∩ 县）：同上，跳过市、县两份；
    - **省直辖县级市 / 不设区的市**（市 ∩ 县，40 个）：
      ``gb`` 后两位 ``00`` 的是不设区的地级市（东莞 / 中山 / 嘉峪关 / 儋州）→
      归市，跳过县那份；后两位非 ``00`` 的是省直辖县级市 / 县（济源 / 仙桃 /
      海南直辖县 / 新疆兵团市）→ 归县、挂省，跳过市那份。

    Returns:
        ``(省数, 市数, 县数)``（去重后的有效行政区数）。
    """
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union

    provs = [f for f in _load(province_file)["features"] if _is_region(f)]
    cities = [f for f in _load(city_file)["features"] if _is_region(f)]
    counties = [f for f in _load(county_file)["features"] if _is_region(f)]

    prov_gbs = {f["properties"]["gb"] for f in provs}
    city_gbs = {f["properties"]["gb"] for f in cities}
    county_gbs = {f["properties"]["gb"] for f in counties}

    features: list[dict] = []

    # 全国条目：几何 = 所有省几何的并集（省界拼成国界，不造矩形）。
    china_geometry = mapping(unary_union([shape(f["geometry"]) for f in provs]))
    features.append(
        {
            "type": "Feature",
            "properties": {
                "gb": CHINA_ID,
                "name": "中国",
                "level": "country",
                "parent": "",
            },
            "geometry": china_geometry,
        }
    )

    # 省 → 全国（34 个，全部有效）。
    for feature in provs:
        props = dict(feature["properties"])
        props["level"] = _PROVINCE_LEVEL
        props["parent"] = CHINA_ID
        features.append(
            {"type": "Feature", "properties": props, "geometry": feature["geometry"]}
        )

    # 市 → 省（跳过省文件里已有的直辖市/港澳台，跳过归县级的省直辖县级市）。
    n_city = 0
    for feature in cities:
        gb = feature["properties"]["gb"]
        if gb in prov_gbs:
            continue  # 直辖市 / 港澳台：省文件里已是省级
        if gb in county_gbs and gb[7:9] != "00":
            continue  # 省直辖县级市：归县级（下面县循环处理）
        props = dict(feature["properties"])
        props["level"] = _CITY_LEVEL
        props["parent"] = gb[:3] + gb[3:5] + "0000"
        features.append(
            {"type": "Feature", "properties": props, "geometry": feature["geometry"]}
        )
        n_city += 1

    # 县 → 市（市不存在时 → 省，省直辖县/直辖市的下辖区）。
    n_county = 0
    for feature in counties:
        gb = feature["properties"]["gb"]
        if gb in prov_gbs:
            continue  # 澳门：省文件里已是省级
        if gb in city_gbs and gb[7:9] == "00":
            continue  # 不设区的市：上面市循环已归市级
        props = dict(feature["properties"])
        props["level"] = _COUNTY_LEVEL
        city_parent = gb[:3] + gb[3:7] + "00"
        if city_parent in city_gbs and city_parent not in prov_gbs:
            props["parent"] = city_parent
        else:
            props["parent"] = gb[:3] + gb[3:5] + "0000"
        features.append(
            {"type": "Feature", "properties": props, "geometry": feature["geometry"]}
        )
        n_county += 1

    document = {"type": "FeatureCollection", "features": features}
    merged_path.write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    return len(provs), n_city, n_county


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="导入天地图中国行政区划（省/市/县）→ 国省市县四级 RegionCatalog"
    )
    parser.add_argument("--province", required=True, help="省 GeoJSON 文件")
    parser.add_argument("--city", required=True, help="市 GeoJSON 文件")
    parser.add_argument("--county", required=True, help="县 GeoJSON 文件")
    parser.add_argument("--boundary-version", required=True, help="数据集版本号")
    parser.add_argument(
        "--output",
        default=None,
        help="发布目录（默认 resources/regions/generated）",
    )
    parser.add_argument("--source-name", default="天地图中国行政区划", help="数据来源名称")
    parser.add_argument("--source-license", default="", help="授权说明（如审图号）")
    parser.add_argument("--check", action="store_true", help="只验证不发布（dry-run）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    from app.config.settings import REGION_DATA_ROOT
    from app.regions.importer import RegionImportConfig, RegionImportService

    output_root = Path(args.output) if args.output else REGION_DATA_ROOT

    province_file = Path(args.province)
    city_file = Path(args.city)
    county_file = Path(args.county)
    for path in (province_file, city_file, county_file):
        if not path.is_file():
            print(f"源文件不存在：{path}", file=sys.stderr)
            return 2

    # 合并到临时文件（不进发布目录；导入管线读它）。
    with tempfile.TemporaryDirectory(prefix="china-admin-") as tmp:
        merged_path = Path(tmp) / "china_admin.geojson"
        n_prov, n_city, n_county = _merge(
            province_file, city_file, county_file, merged_path
        )
        print(
            f"已整理：省 {n_prov} / 市 {n_city} / 县 {n_county}（+ 合成全国 1）"
        )

        config = RegionImportConfig(
            source_file=merged_path,
            id_field="gb",
            name_field="name",
            level_field="level",
            parent_field="parent",
            output_root=output_root,
            boundary_version=args.boundary_version,
            source_name=args.source_name,
            source_license=args.source_license,
        )
        service = RegionImportService(output_root=output_root)

        if args.check:
            result = service.validate(config)
            print("\n".join(result.summary_lines()))
            return 0 if result.ok else 1

        report = service.import_dataset(config)
        if not report.ok:
            print("\n".join(report.result.summary_lines()), file=sys.stderr)
            return 1
        print("\n".join(report.result.summary_lines()))
        print(f"已发布到：{report.output_root}")
        print(f"manifest：{report.manifest.summary_text()}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
