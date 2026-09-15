"""应用服务层（Slice 4）。

把"多层能力"编排成"一个业务动作"。UI 只调这一层，不自己拼流程。

目前只有 :mod:`app.services.map_download_service`：

    BoundingBox + 图层 + 输出路径  →  .mbtiles 文件

完整链路（每一步都由对应 Slice 实现，本层只负责顺序与收尾）::

    BoundingBox → Tile Math → TiandituProvider → Async Downloader
        → MBTilesTileSink → MBTilesWriter → map.mbtiles → Validator
"""

from app.services.map_download_service import (
    MapDownloadOutcome,
    MapDownloadService,
    build_output_path,
    sanitize_map_name,
)

__all__ = [
    "MapDownloadOutcome",
    "MapDownloadService",
    "build_output_path",
    "sanitize_map_name",
]
