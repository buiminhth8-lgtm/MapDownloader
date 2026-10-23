"""算"选哪一级 Zoom"的两本账：地面分辨率与体积代价（Slice 29）。

给无人机图基底选 Zoom 时有两个硬约束，这个脚本把两条都算出来：

1. **地面分辨率**（米/像素）—— 决定"能不能看清那栋房子"。同一级 Zoom 随纬度
   变化 ±15%，所以不能只看 Zoom 数字；
2. **体积** —— 瓦片数每级 ×4，大范围下到高 Zoom 是不可行的。

脚本的输出去向：``docs/zoom_strategy.md`` 第 10 节的三张表。数字变了要同步文档 ——
文档里的体积表不是估的，是这个脚本跑出来的。

跑法::

    .venv/Scripts/python.exe scripts/analyze_zoom_volume.py
    .venv/Scripts/python.exe scripts/analyze_zoom_volume.py --serial-csv   # 只导出 CSV

它**不访问网络、不写库**，只用区域目录里的 bounds 做几何估算。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:  # 允许 `python scripts/xxx.py` 直接跑
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config.settings import (  # noqa: E402
    DEFAULT_AVERAGE_TILE_SIZE,
    REGION_DATA_ROOT,
)
from app.geo.bounds import GeoBounds  # noqa: E402
from app.geo.tile_math import estimate_size_bytes, estimate_tile_count  # noqa: E402

#: 报告里用的三个参考纬度。22.5°（华南）/ 32°（长江中下游）/ 45°（东北），
#: 覆盖中国无人机作业的主要范围。
REFERENCE_LATITUDES = (22.5, 32.0, 45.0)

#: 分析用的区域 id（江苏省 / 南京市 / 南京鼓楼区）。
#: 它们只是"大 / 中 / 小"三个代表，换成别的省市结论不变。
SAMPLE_REGIONS = (
    ("江苏省", "156320000"),
    ("南京市", "156320100"),
    ("南京鼓楼区", "156320106"),
)

#: 全国矩形（含海域）。比精确国界大，是**保守**估计 —— 真实下载量只会更小。
CHINA_BOUNDS = GeoBounds(73.5, 18.1, 135.1, 53.6)


def meters_per_pixel(zoom: int, latitude: float) -> float:
    """Web 墨卡托在给定纬度的地面分辨率（米/像素）。

    与 ``app.planning.ground_resolution.meters_per_pixel`` 同一公式。这里单独写
    一份是为了让本脚本能独立运行（也顺便当作那份实现的交叉校验：两者不一致
    说明有一个错了）。
    """
    return 40075016.68557849 * math.cos(math.radians(latitude)) / (256 * (2**zoom))


def _megabytes(tiles: int) -> float:
    return estimate_size_bytes(tiles, DEFAULT_AVERAGE_TILE_SIZE) / 1024 / 1024


def _human(size_mb: float) -> str:
    """大于 1024 MB 时换成 GB，避免表里全是六位数。"""
    return f"{size_mb / 1024:.1f} GB" if size_mb >= 1024 else f"{size_mb:.1f} MB"


def _area_km2(bounds: GeoBounds) -> float:
    mid_lat = (bounds.south + bounds.north) / 2
    lon_km = (bounds.east - bounds.west) * 111.32 * math.cos(math.radians(mid_lat))
    lat_km = (bounds.north - bounds.south) * 110.57
    return lon_km * lat_km


def report_resolution() -> None:
    print("=" * 84)
    print("一、地面分辨率（米/像素）—— 选 Zoom 的唯一硬指标")
    print("=" * 84)
    header = f"{'Zoom':>5} | " + " | ".join(f"lat={la:>4}°" for la in REFERENCE_LATITUDES)
    print(f"{header} | 1200px 屏覆盖")
    print("-" * 84)
    for zoom in range(4, 19):
        cells = [meters_per_pixel(zoom, la) for la in REFERENCE_LATITUDES]
        span_km = 1200 * cells[1] / 1000
        print(
            f"{zoom:>5} | " + " | ".join(f"{c:>7.2f}" for c in cells)
            + f" | {span_km:>7.1f} km"
        )


def report_regions() -> None:
    print()
    print("=" * 84)
    print(f"二、体积代价（按 {DEFAULT_AVERAGE_TILE_SIZE // 1024} KB/瓦片，项目默认保守值）")
    print("=" * 84)

    index_path = Path(REGION_DATA_ROOT) / "index.json"
    if not index_path.exists():
        print(f"找不到区域目录 {index_path}，跳过这一段（先跑导入管线）")
        return
    entries = {row["id"]: row for row in json.loads(index_path.read_text("utf-8"))["regions"]}

    def show(label: str, bounds: GeoBounds, zooms: range) -> None:
        print(f"\n{label}  约 {_area_km2(bounds):,.0f} km²")
        print(f"  {'Zoom':>5} {'瓦片数':>12} {'体积':>10} {'m/px':>7}")
        mid_lat = (bounds.south + bounds.north) / 2
        for zoom in zooms:
            tiles = estimate_tile_count(bounds, zoom, zoom)
            print(
                f"  {zoom:>5} {tiles:>12,} {_human(_megabytes(tiles)):>10}"
                f" {meters_per_pixel(zoom, mid_lat):>7.2f}"
            )

    show("全国（矩形含海域，保守）", CHINA_BOUNDS, range(4, 15))
    for label, region_id in SAMPLE_REGIONS:
        row = entries.get(region_id)
        if row is None:
            print(f"\n{label}（{region_id}）不在目录里，跳过")
            continue
        b = row["bounds"]
        bounds = GeoBounds(b["west"], b["south"], b["east"], b["north"])
        show(f"{label} {region_id}", bounds, range(9, 19))


def report_work_area() -> None:
    print()
    print("=" * 84)
    print("三、作业区体积 —— 无人机真正要下高 Zoom 的范围")
    print("=" * 84)
    for side_km in (1, 5, 10, 30):
        dlat = side_km / 110.57
        dlon = side_km / (111.32 * math.cos(math.radians(32.0)))
        bounds = GeoBounds(118.7, 32.0, 118.7 + dlon, 32.0 + dlat)
        print(f"\n{side_km}×{side_km} km²（北纬 32°）")
        for zoom in range(14, 19):
            tiles = estimate_tile_count(bounds, zoom, zoom)
            print(
                f"  Z{zoom}: {tiles:>8,} 瓦片  {_human(_megabytes(tiles)):>9}"
                f"  {meters_per_pixel(zoom, 32.0):>5.2f} m/px"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--section",
        choices=("all", "resolution", "regions", "area"),
        default="all",
        help="只跑某一段（默认全部）",
    )
    args = parser.parse_args()

    if args.section in ("all", "resolution"):
        report_resolution()
    if args.section in ("all", "regions"):
        report_regions()
    if args.section in ("all", "area"):
        report_work_area()

    print()
    print("提示：上表的数字就是 docs/zoom_strategy.md 第 10 节的来源。")
    print("      结论：全国到 Z10（约 350 MB~670 MB），作业区才下 Z16~Z18。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
