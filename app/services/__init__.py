"""应用服务层（Slice 4；Slice 5 增加断点续传）。

把"多层能力"编排成"一个业务动作"。UI 只调这一层，不自己拼流程。

目前只有 :mod:`app.services.map_download_service`：

    BoundingBox + 图层 + 输出路径  →  .mbtiles 文件
    已有 .mbtiles + 任务记录        →  补齐缺失瓦片（断点续传）

完整链路（每一步都由对应 Slice 实现，本层只负责顺序与收尾）::

    BoundingBox → Tile Math → TiandituProvider → Async Downloader
        → MBTilesTileSink → MBTilesWriter → map.mbtiles → Validator

续传时多绕一圈（Slice 5）::

    DownloadTask(output_path) → 读已有 metadata → 一致性校验
        → ExistingTileIndex（已有瓦片跳过）→ 只下缺失的 → 写回同一个文件
"""

from app.services.map_download_service import (
    MapDownloadOutcome,
    MapDownloadService,
    MapResumeError,
    ResumeCompatibility,
    build_output_path,
    check_resume_compatibility,
    count_existing_tiles,
    read_existing_metadata,
    sanitize_map_name,
)

__all__ = [
    "MapDownloadOutcome",
    "MapDownloadService",
    "MapResumeError",
    "ResumeCompatibility",
    "check_resume_compatibility",
    "count_existing_tiles",
    "read_existing_metadata",
    "build_output_path",
    "sanitize_map_name",
]
