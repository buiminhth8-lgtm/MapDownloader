"""统一瓦片仓库（Slice 42）。

一句话：**一次获取、多次复用**的地基 —— 原始图层按"内容寻址 + 分片 SQLite"存起来，
坐标与内容分开，写入有版本、读取有快照、坏了能查出、崩了能恢复。

本包**不**做的事（各自的 Slice 负责）：

===================  ==========================================================
不做的                谁做
===================  ==========================================================
读旧 MBTiles 导入      Slice 43（``classify_asset_kind`` 已经准备好给导入器用）
下载 / 续传 / 修复接入  Slice 44
导出成品 mbtiles       Slice 45（XYZ→TMS 的**唯一**翻转点在那里）
严格预算与自动裁剪      Slice 46
管理界面               不在本包（仓库不 import UI，有守卫测试钉住）
真正删除内容            Slice 49（``prune(dry_run=False)`` 现在会明确拒绝）
===================  ==========================================================

分层约束
--------

只依赖 ``app.config`` / ``app.geo`` / ``app.models`` / ``app.providers``（图片字节
判定）。**不依赖** ``app.ui``、``app.package``、``app.downloader``。有一条 AST 守卫
测试（``tests/test_tilerepo_layers.py``）盯着这条线，别绕过它。

最小用法
--------

::

    from app.tilerepo import TileRepository

    repo = TileRepository.open(r'D:/tiles/repo', create=True)
    repo.ensure_asset('tianditu/vec_w')

    # 写入（同坐标同内容幂等；不同内容默认拒绝）
    report = repo.put_tiles('tianditu/vec_w', [(5, 1, 2, png_bytes)])
    print(report.as_lines())

    # 查询（不存在返回 ABSENT，不抛异常）
    lookup = repo.read_tile('tianditu/vec_w', 5, 1, 2)
    if lookup.present:
        ...  # lookup.record.data

    # 缺口（只数目标级，父级回退不计入）
    gap = repo.query_existence('tianditu/vec_w', (5, 5), bounds)

    # 稳定读取视图
    meta = repo.freeze_snapshot(['tianditu/vec_w'], zoom_range=(5, 6))
    with repo.open_snapshot(meta.fingerprint) as view:
        for record in view:
            ...

    # 自检与恢复
    repo.health_check('quick')
    repo.recover()

    repo.close()

完整流程（含故障注入与索引重建）见 ``scripts/verify_tile_repository.py``；
使用与验收说明见 ``docs/tile_repository_usage.md``。
"""

from app.tilerepo.catalog import (
    CATALOG_FILENAME,
    CATALOG_SCHEMA_VERSION,
    Catalog,
    OperationState,
    ShardRegistration,
    SourceSpec,
    fingerprint_source,
)
from app.tilerepo.errors import (
    Busy,
    Cancelled,
    Conflict,
    Corrupt,
    InsufficientBudget,
    InsufficientRequiredTiles,
    InvalidTile,
    NotFound,
    RepoClosed,
    RepoError,
    SchemaUnsupported,
    SnapshotExpired,
    UnsupportedOperation,
)
from app.tilerepo.health import (
    HealthIssue,
    HealthReport,
    PruneReport,
    RebuildReport,
    RecoveryReport,
    ShardHealth,
    health_check,
    prune,
    rebuild_index,
    recover,
)
from app.tilerepo.identity import (
    AssetId,
    AssetKind,
    ContentDigest,
    CoordKey,
    CoordState,
    LookupStatus,
    TileLookup,
    TileRecord,
    classify_asset_kind,
    make_digest,
    validate_tile_bytes,
)
from app.tilerepo.shard import (
    Lease,
    acquire_lease,
    band_for_zoom,
    band_name,
    shard_path,
    shard_relative_path,
)
from app.tilerepo.snapshot import (
    SnapshotMeta,
    SnapshotSelector,
    SnapshotView,
    freeze_snapshot,
)
from app.tilerepo.stats import AssetStats, RepoStats, ZoomStats
from app.tilerepo.store import (
    AssetExistence,
    ConflictItem,
    ExistenceReport,
    Progress,
    PutReport,
    RejectedItem,
    TileRepository,
    ZoomExistence,
)

__all__ = [
    # 入口
    "TileRepository",
    # 身份与值对象
    "AssetId",
    "AssetKind",
    "ContentDigest",
    "CoordKey",
    "CoordState",
    "LookupStatus",
    "TileLookup",
    "TileRecord",
    "classify_asset_kind",
    "make_digest",
    "validate_tile_bytes",
    # 分片
    "Lease",
    "acquire_lease",
    "band_for_zoom",
    "band_name",
    "shard_path",
    "shard_relative_path",
    # catalog
    "CATALOG_FILENAME",
    "CATALOG_SCHEMA_VERSION",
    "Catalog",
    "OperationState",
    "ShardRegistration",
    "SourceSpec",
    "fingerprint_source",
    # 报告
    "AssetExistence",
    "AssetStats",
    "ConflictItem",
    "ExistenceReport",
    "HealthIssue",
    "HealthReport",
    "Progress",
    "PruneReport",
    "PutReport",
    "RebuildReport",
    "RecoveryReport",
    "RejectedItem",
    "RepoStats",
    "ShardHealth",
    "SnapshotMeta",
    "SnapshotSelector",
    "SnapshotView",
    "ZoomExistence",
    "ZoomStats",
    # 函数
    "freeze_snapshot",
    "health_check",
    "prune",
    "rebuild_index",
    "recover",
    # 错误
    "Busy",
    "Cancelled",
    "Conflict",
    "Corrupt",
    "InsufficientBudget",
    "InsufficientRequiredTiles",
    "InvalidTile",
    "NotFound",
    "RepoClosed",
    "RepoError",
    "SchemaUnsupported",
    "SnapshotExpired",
    "UnsupportedOperation",
]
