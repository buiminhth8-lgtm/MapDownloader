"""测试用的区域构造助手。

自定义矩形（``RegionType.RECTANGLE`` / ``DownloadRegion.rectangle``）已随界面
入口一起移除，但不少用例只需要"一块带几何的下载区域"来驱动预算、统计、
策略等与区域形状无关的逻辑。这里给一个统一入口，避免每个文件各拼一份
``DownloadRegion(...)``。

几何默认用 ``fixtures/regions/square.geojson``（116~117E / 39~40N）。
需要别的形状时显式传 ``geojson_path``；只关心外包矩形、不读文件的用例
传什么路径都不影响结果。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.geo import DownloadRegion, GeoBounds, RegionType

#: 合成方框边界（见 ``fixtures/regions/square.geojson``）
SQUARE_GEOJSON = Path(__file__).resolve().parent / "fixtures" / "regions" / "square.geojson"

#: 与 :data:`SQUARE_GEOJSON` 一致的外包矩形
SQUARE_BOUNDS = GeoBounds(116.0, 39.0, 117.0, 40.0)


def polygon_region(
    name: str = "测试区域",
    bounds: GeoBounds | None = None,
    *,
    geojson_path: str | Path | None = None,
    region_id: str = "test-square",
    **fields: Any,
) -> DownloadRegion:
    """造一块行政区域（多边形）。

    Args:
        name: 展示名。
        bounds: 候选外包矩形；缺省是与默认几何一致的 :data:`SQUARE_BOUNDS`。
        geojson_path: 边界文件；缺省是合成方框。
        region_id: 区域标识（写进任务记录 / metadata 的那个）。
        **fields: 其余 ``DownloadRegion`` 字段（如 ``region_level``、``buffer_meters``）。
    """
    return DownloadRegion(
        region_type=RegionType.POLYGON,
        name=name,
        bounds=bounds if bounds is not None else SQUARE_BOUNDS,
        region_id=region_id,
        geojson_path=str(geojson_path if geojson_path is not None else SQUARE_GEOJSON),
        **fields,
    )


def rect_region(
    name: str,
    bounds: GeoBounds,
    directory: str | Path,
    *,
    region_id: str = "test-rect",
    **fields: Any,
) -> DownloadRegion:
    """造一块**恰好等于 ``bounds``** 的行政区域（在现场写一份矩形 GeoJSON）。

    用途是替代旧的"矩形下载"：那些用例真正需要的只是"一整块范围、边界不裁掉
    任何瓦片"，而不是"矩形这种几何类型"。把它写成多边形，导出 / 预算 / 覆盖度
    的链路与生产完全一致，又不会让已经被删除的 ``rectangle`` 悄悄复活。

    Args:
        directory: 写 GeoJSON 的目录（用例传 ``tmp_path``）。
    """
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{region_id}.geojson"
    payload = {
        "type": "Feature",
        "properties": {"name": name, "id": region_id, "boundaryVersion": "test"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [bounds.west, bounds.south],
                    [bounds.east, bounds.south],
                    [bounds.east, bounds.north],
                    [bounds.west, bounds.north],
                    [bounds.west, bounds.south],
                ]
            ],
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return polygon_region(name, bounds, geojson_path=path, region_id=region_id, **fields)


def register_for_resume(region: DownloadRegion) -> DownloadRegion:
    """把区域挂进**动态解析表**，让 ``region_from_task`` 能按 id 找回来。

    为什么需要它：一条任务记录只落盘 ``region_type`` / ``region_id`` /
    ``boundary_version``（几何不进 ``tasks.json``）。续传时服务会拿 ``region_id``
    回预设目录找边界文件 —— 而用例里那些 ``test-rect`` 之类的合成 id 不在任何
    目录里。动态解析器正是生产里任务区（Slice 22）用的那条通道：几何在用户的
    数据目录里，按 id 现查。

    注册表由 ``tests/conftest.py`` 的 autouse 夹具在每个用例结束后清空，
    因此这里不需要配对的反注册。

    Returns:
        原样返回 ``region``，方便链式书写。
    """
    from app.geo import region as region_module

    wanted = (region.region_id or "").strip()

    def resolver(region_id: str, bounds: GeoBounds):
        if (region_id or "").strip() != wanted:
            return None
        # 记录里的范围可能与几何略有出入（老记录尤其如此），以**记录**为准：
        # 续传要下的就是记录里那块地，几何只用来判断"哪几张瓦片在区域内"。
        return region, ""

    region_module.register_region_resolver(resolver)
    return region
