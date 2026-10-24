"""Slice 37 离线验收：真实 SQLite + 有效 PNG，不访问地图服务。"""
from __future__ import annotations

import io
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from app.geo import DownloadRegion, GeoBounds
from app.geo.region_estimator import ZoomRegionStats
from app.mbtiles import MBTilesMetadata, MBTilesWriter
from app.models import MapType
from app.planning.storage_budget import GIB, BudgetInput, BudgetSettings, assess_budget


def main():
    with tempfile.TemporaryDirectory(prefix="budget-acceptance-") as directory:
        root = Path(directory)
        bounds = GeoBounds(116.2, 39.7, 116.6, 40.1)
        region = DownloadRegion.rectangle("验收区域", bounds)
        rng = random.Random(37)
        for mode in (MapType.VECTOR, MapType.IMAGE):
            image = Image.new("RGB", (256, 256), "white") if mode is MapType.VECTOR else Image.frombytes(
                "RGB", (256, 256), rng.randbytes(256 * 256 * 3))
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            image.close()
            path = root / f"sample-{mode.value}.mbtiles"
            with MBTilesWriter(path) as writer:
                writer.write_metadata(MBTilesMetadata.for_bounds(
                    bounds, 12, 12, name=mode.value, source_map_type=mode.value,
                    with_labels=True, composition="merged"))
                for x in range(3370, 3382):
                    writer.write_tile(12, x, 1552, stream.getvalue())
        counts = (ZoomRegionStats(12, 100, 100, 0), ZoomRegionStats(13, 400, 400, 0))
        inputs = tuple(BudgetInput(mode.value, mode.value, region, 12, 13,
                                  root / f"target-{mode.value}.mbtiles", mode, True, counts)
                       for mode in (MapType.VECTOR, MapType.IMAGE))
        report = assess_budget(inputs, BudgetSettings(budget_gib=0.01, reserve_gib=1),
                               disk_probe=lambda p: ("acceptance-disk", 100 * GIB))
        assert report.items[1].zooms[0].mean_bytes > report.items[0].zooms[0].mean_bytes * 10
        print("[PASS] 卫星与矢量分别从同模式、同 Zoom 的有效 PNG 抽样预测")
        assert report.items[0].zooms[1].sample_count == 0
        print("[PASS] 没有对应 Zoom 样本时明确使用默认模型及保守范围")
        assert report.over_budget and report.suggestions and report.disk_ok
        print("[PASS] 超预算给出具体 Zoom 上限与减少新增空间建议，不改请求")
        assert inputs[0].max_zoom == 13
        assert report.requests == 1000 * 2
        print("[PASS] 新增 HTTP 请求与最终存储容量分开计算，注记计两次请求")
        assert len(report.disks) == 1
        assert report.disks["acceptance-disk"][1] == sum(i.upper_bytes for i in report.items) + GIB
        print("[PASS] 同盘多个任务共用一份保留空间")
        blocked = assess_budget(inputs, disk_probe=lambda p: ("acceptance-disk", 0))
        assert not blocked.disk_ok
        print("[PASS] 磁盘不足不能通过预检")
        assert len(list(root.glob("*.mbtiles"))) == 2
        print("[PASS] 预测不创建目标地图文件，不调用地图服务")
        for line in report.as_lines()[:7]:
            print(line)
    print("容量预算离线验收全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
