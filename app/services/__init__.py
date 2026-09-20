"""应用服务层（Slice 4；Slice 5 增加断点续传；Slice 8 检查；Slice 9 打包；Slice 10 传输）。

把"多层能力"编排成"一个业务动作"。UI 只调这一层，不自己拼流程。

目前有四个服务：

:mod:`app.services.map_download_service`
    ``BoundingBox + 图层 + 输出路径`` → ``.mbtiles`` 文件；
    ``已有 .mbtiles + 任务记录`` → 补齐缺失瓦片（断点续传）。

:mod:`app.services.mbtiles_inspection_service`
    把"检查一个已有 .mbtiles"编排成一个动作（Reader → Validator → Diagnostics），
    供 GUI 的检查对话框与下载完成后的快速诊断共用。

:mod:`app.services.map_package_service`
    把"打包"与"验证包"编排成两个动作（Slice 9）：``.mbtiles`` → 离线地图包
    （``map.mbtiles`` + ``manifest.json`` + SHA256），以及反过来校验一个包。

:mod:`app.services.android_transfer_service`
    把"设备发现 + 包校验 + 传输"编排成三个动作（Slice 10）：ADB → 设备列表；
    地图包 → PASS/FAIL；``设备 + 包`` → 推送到
    ``/sdcard/Download/TiandituOfflineMap/import/<manifest.id>/``。

完整链路（每一步都由对应 Slice 实现，本层只负责顺序与收尾）::

    BoundingBox → Tile Math → TiandituProvider → Async Downloader
        → MBTilesTileSink → MBTilesWriter → map.mbtiles → Validator
        → MBTilesInspectionService（Slice 8 诊断）
        → MapPackageService（Slice 9 打包：manifest + SHA256）
        → 离线地图包
        → AndroidTransferService（Slice 10：ADB → 设备 → 公共导入区）
        → Slice 11（Android 端导入器：把包搬进 App 私有目录并交给 osmdroid）

续传时多绕一圈（Slice 5）::

    DownloadTask(output_path) → 读已有 metadata → 一致性校验
        → ExistingTileIndex（已有瓦片跳过）→ 只下缺失的 → 写回同一个文件
"""

from app.services.android_transfer_service import AdbStatus, AndroidTransferService
from app.services.map_download_service import (
    MapDownloadOutcome,
    MapDownloadService,
    MapResumeError,
    ResumeCompatibility,
    build_output_path,
    check_resume_compatibility,
    count_existing_region_tiles,
    count_existing_tiles,
    read_existing_metadata,
    region_target_tiles,
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
    # Slice 16
    "region_target_tiles",
    "count_existing_region_tiles",
    # Slice 8
    "MBTilesInspectionService",
    # Slice 9
    "MapPackageService",
    # Slice 10
    "AndroidTransferService",
    "AdbStatus",
]
