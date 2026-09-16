"""应用服务层（Slice 4；Slice 5 增加断点续传；Slice 8 增加检查；Slice 9 增加打包）。

把"多层能力"编排成"一个业务动作"。UI 只调这一层，不自己拼流程。

目前有三个服务：

:mod:`app.services.map_download_service`
    ``BoundingBox + 图层 + 输出路径`` → ``.mbtiles`` 文件；
    ``已有 .mbtiles + 任务记录`` → 补齐缺失瓦片（断点续传）。

:mod:`app.services.mbtiles_inspection_service`
    把"检查一个已有 .mbtiles"编排成一个动作（Reader → Validator → Diagnostics），
    供 GUI 的检查对话框与下载完成后的快速诊断共用。

:mod:`app.services.map_package_service`
    把"打包"与"验证包"编排成两个动作（Slice 9）：``.mbtiles`` → 离线地图包
    （``map.mbtiles`` + ``manifest.json`` + SHA256），以及反过来校验一个包。

完整链路（每一步都由对应 Slice 实现，本层只负责顺序与收尾）::

    BoundingBox → Tile Math → TiandituProvider → Async Downloader
        → MBTilesTileSink → MBTilesWriter → map.mbtiles → Validator
        → MBTilesInspectionService（Slice 8 诊断）
        → MapPackageService（Slice 9 打包：manifest + SHA256）
        → 离线地图包（Android 导入是 Slice 10，本 Slice 不做）

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
from app.services.map_package_service import MapPackageService
from app.services.mbtiles_inspection_service import MBTilesInspectionService

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
    # Slice 8
    "MBTilesInspectionService",
    # Slice 9
    "MapPackageService",
]
