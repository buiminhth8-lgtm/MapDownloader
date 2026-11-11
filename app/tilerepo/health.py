"""健康检查、故障恢复、索引重建与清理报告（Slice 42）。

三条原则
--------

1. **默认只读**。``health_check`` 绝不修改任何东西；修、恢复、重建都要显式调用。
2. **分片是事实，catalog 是缓存**（架构 §7.2）。因此"catalog 的计数不对"只是
   warning（跑一次 ``rebuild_index`` 就好），而"分片里的坐标指向不存在的内容"
   是 error（数据真的坏了）。
3. **发现不等于删除**。``prune`` 在本 Slice 只报告，``dry_run=False`` 直接报
   "本版本不支持" —— 自动回收会在用户正要导出的那一刻把内容删掉。

四类"未完成"的区分（这是恢复逻辑的核心）
----------------------------------------

==========================  ==============================================  ====================
分片里有没有这个 epoch 的行   说明                                              恢复动作
==========================  ==============================================  ====================
没有                        崩在写分片之前/写分片事务里（已回滚）              标 ``aborted``
有、catalog 没发布          崩在"分片提交之后、catalog 发布之前"              补发布 + ``done``
有、catalog 已发布           崩在"发布之后、打 done 之前"                      只打 ``done``
有、但操作是 cancelled      取消前已提交的批次（契约：不回滚）                补发布 + ``cancelled``
==========================  ==============================================  ====================

第三种最容易做错：如果恢复时又发一次"发布"，除了多一次 fsync 没有副作用（幂等）；
但如果恢复时**删掉**那些行，就等于把"已经成功写进磁盘、只是没来得及打勾"的数据
静默丢掉 —— 这正是契约里 "恢复不得静默丢失已经成功返回给调用方的写入" 的反面。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from app.config.settings import (
    TILE_REPO_HEALTH_CONTENT_SAMPLE,
    TILE_REPO_STAGING_MAX_AGE_SECONDS,
    TILE_REPO_SUPERSEDED_RETENTION_DAYS,
)
from app.tilerepo import shard as shard_module
from app.tilerepo.catalog import CATALOG_SCHEMA_VERSION, OperationState
from app.tilerepo.errors import Corrupt, RepoError, SchemaUnsupported, UnsupportedOperation
from app.tilerepo.identity import AssetId, CoordState, make_digest

if TYPE_CHECKING:  # pragma: no cover
    from app.tilerepo.store import TileRepository

__all__ = [
    "HealthIssue",
    "HealthReport",
    "PruneReport",
    "RebuildReport",
    "RecoveryReport",
    "ShardHealth",
    "health_check",
    "prune",
    "rebuild_index",
    "recover",
]

#: 问题级别。``error`` 让 ``report.ok`` 变 False；``warning`` 只提示。
LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"
LEVEL_INFO = "info"


@dataclass(frozen=True)
class HealthIssue:
    """一条问题。``scope`` 是"哪一块"（资产/分片/操作），不含绝对路径。"""

    level: str
    code: str
    scope: str
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "code": self.code,
            "scope": self.scope,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ShardHealth:
    """单个分片的检查结果。"""

    asset: str
    z_min: int
    z_max: int
    file: str
    ok: bool = True
    tile_count: int = 0
    content_count: int = 0
    published_epoch: int = 0
    registered: bool = True
    problems: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "asset": self.asset,
            "band": [self.z_min, self.z_max],
            "file": self.file,
            "ok": self.ok,
            "tile_count": self.tile_count,
            "content_count": self.content_count,
            "published_epoch": self.published_epoch,
            "registered": self.registered,
            "problems": list(self.problems),
        }


@dataclass(frozen=True)
class HealthReport:
    """``health_check`` 的结果。"""

    scope: str
    issues: tuple[HealthIssue, ...] = ()
    shards: tuple[ShardHealth, ...] = ()
    orphan_contents: int = 0
    dangling_refs: int = 0
    pending_operations: tuple[dict[str, object], ...] = ()
    stale_staging: tuple[str, ...] = ()
    leases: tuple[dict[str, object], ...] = ()
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        """没有 ``error`` 级问题（warning 不算失败）。"""
        return not any(item.level == LEVEL_ERROR for item in self.issues)

    @property
    def errors(self) -> tuple[HealthIssue, ...]:
        return tuple(item for item in self.issues if item.level == LEVEL_ERROR)

    @property
    def warnings(self) -> tuple[HealthIssue, ...]:
        return tuple(item for item in self.issues if item.level == LEVEL_WARNING)

    def as_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "ok": self.ok,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "issues": [item.as_dict() for item in self.issues],
            "shards": [item.as_dict() for item in self.shards],
            "orphan_contents": self.orphan_contents,
            "dangling_refs": self.dangling_refs,
            "pending_operations": list(self.pending_operations),
            "stale_staging": list(self.stale_staging),
            "leases": list(self.leases),
            "duration": round(self.duration, 3),
        }

    def as_lines(self) -> list[str]:
        lines = [
            f"健康检查（{self.scope}）：{'通过' if self.ok else '失败'}"
            f"（{len(self.errors)} 个错误 / {len(self.warnings)} 个警告）"
        ]
        for item in self.issues:
            lines.append(f"  [{item.level}] {item.code} @ {item.scope}：{item.detail}")
        if self.dangling_refs:
            lines.append(f"  悬空引用：{self.dangling_refs} 条")
        if self.orphan_contents:
            lines.append(f"  孤立内容：{self.orphan_contents} 份（未删除）")
        for op in self.pending_operations:
            lines.append(
                f"  未完成操作：{op.get('op_id', '')[:8]}… {op.get('kind')} "
                f"{op.get('state')}（epoch {op.get('epoch_id')}）"
            )
        if self.stale_staging:
            lines.append(f"  残留暂存目录：{len(self.stale_staging)} 个")
        for lease in self.leases:
            state = "已退出" if lease.get("alive") is False else f"{lease.get('age_seconds')} 秒无心跳"
            lines.append(f"  租约：{lease.get('lock')} 由 pid {lease.get('pid')} 持有（{state}）")
        return lines


# --------------------------------------------------------------------------- #
# 健康检查
# --------------------------------------------------------------------------- #
def health_check(repo: TileRepository, scope: str = "quick") -> HealthReport:
    """检查仓库结构与一致性（**只读**）。

    Args:
        repo: 仓库。
        scope: ``'quick'`` = 结构 + ``quick_check`` + 悬空引用 + 计数比对；
            ``'full'`` 另加：抽样复算内容摘要、孤立内容统计、抽样解码。

    Returns:
        :class:`HealthReport`。``ok`` 只受 ``error`` 影响 —— 缺图、计数漂移这类
        "可以修"的问题不应该让一次正常的健康检查变成红色。
    """
    if scope not in ("quick", "full"):
        raise ValueError(f"scope 只能是 'quick' 或 'full'，实际为 {scope!r}")
    started = time.time()
    issues: list[HealthIssue] = []
    shards: list[ShardHealth] = []
    dangling = 0
    orphan = 0

    registered = {(item.asset_id, item.z_min): item for item in repo.catalog.list_shards()}

    for key, record in registered.items():
        asset_id, z_min = key
        if not record.path or Path(record.path).is_absolute():
            issues.append(
                HealthIssue(LEVEL_ERROR, "bad_path", asset_id, f"catalog 里存了绝对路径：{record.path}")
            )
            continue
        path = repo.root / record.path
        try:
            shard_module.raise_if_escapes(repo.root, path)
        except RepoError as exc:
            issues.append(HealthIssue(LEVEL_ERROR, "path_escape", asset_id, str(exc)))
            continue
        if not path.is_file():
            issues.append(
                HealthIssue(
                    LEVEL_ERROR, "missing_shard", f"{asset_id} z{z_min}", f"catalog 指向的分片不存在：{path.name}"
                )
            )
            continue
        health, shard_dangling, max_epoch = _inspect_shard(
            asset_id, (record.z_min, record.z_max), path, record.published_epoch, issues
        )
        shards.append(health)
        dangling += shard_dangling
        # 分片本身已经不健康（打不开/schema 不认/结构坏）时，里面那个"最大 epoch"
        # 读出来的是 0 —— 再拿它比水位只会产生一条假的"水位超前"错误，
        # 把真正的病因（schema_unsupported）淹掉。
        if health.ok and record.published_epoch > max_epoch:
            issues.append(
                HealthIssue(
                    LEVEL_ERROR,
                    "watermark_ahead",
                    f"{asset_id} z{record.z_min}",
                    f"发布水位 {record.published_epoch} 超过分片内最大 epoch {max_epoch}（可能丢行）",
                )
            )
        if health.tile_count != record.tile_count or health.content_count != record.content_count:
            issues.append(
                HealthIssue(
                    LEVEL_WARNING,
                    "count_mismatch",
                    f"{asset_id} z{record.z_min}",
                    f"catalog 记 {record.tile_count}/{record.content_count}，"
                    f"分片实际 {health.tile_count}/{health.content_count}（跑 rebuild_index 可修）",
                )
            )
        if scope == "full":
            orphan += _content_health(path, issues, asset_id)

    # disk 上有、catalog 里没有的分片
    for path in shard_module.shard_paths_under(repo.root):
        relative = path.relative_to(repo.root).as_posix()
        if not any(record.path == relative for record in registered.values()):
            shards.append(
                ShardHealth(
                    asset="(未登记)",
                    z_min=-1,
                    z_max=-1,
                    file=path.name,
                    ok=True,
                    registered=False,
                )
            )
            issues.append(
                HealthIssue(
                    LEVEL_WARNING,
                    "unregistered_shard",
                    relative,
                    "分片文件存在但 catalog 没有登记（崩在发布之前，或 catalog 丢过）：跑 rebuild_index",
                )
            )

    pending = repo.catalog.unfinished_operations()
    for op in pending:
        issues.append(
            HealthIssue(
                LEVEL_WARNING,
                "pending_operation",
                str(op.get("op_id", ""))[:8],
                f"操作未完成：{op.get('kind')} state={op.get('state')} epoch={op.get('epoch_id')}"
                "（跑 recover() 接管）",
            )
        )

    stale = _stale_staging(repo.staging_dir)
    if stale:
        issues.append(
            HealthIssue(
                LEVEL_WARNING,
                "stale_staging",
                "staging",
                f"{len(stale)} 个临时目录超过 {TILE_REPO_STAGING_MAX_AGE_SECONDS // 3600} 小时",
            )
        )

    leases = [
        {"lock": path.name, **info.as_public_dict(), "alive": info.holder_alive()}
        for path, info in (
            (item, shard_module.read_lease(item)) for item in sorted(repo.lock_dir.glob("*.lock"))
        )
        if info is not None
    ]
    for lease in leases:
        if lease.get("alive") is False:
            issues.append(
                HealthIssue(
                    LEVEL_WARNING,
                    "dead_lease",
                    str(lease["lock"]),
                    f"租约持有进程 pid {lease.get('pid')} 已不存在（下一次写入会自动接管，"
                    "也可以先跑 recover()）",
                )
            )
        elif float(lease["age_seconds"]) > repo.lease_timeout:
            issues.append(
                HealthIssue(
                    LEVEL_WARNING,
                    "stale_lease",
                    str(lease["lock"]),
                    f"租约 {lease['age_seconds']} 秒无心跳（持有者可能已经退出）",
                )
            )

    if repo.catalog.meta().get("schema_version") != str(CATALOG_SCHEMA_VERSION):
        issues.append(
            HealthIssue(LEVEL_ERROR, "schema_mismatch", "catalog", "catalog 的 schema 版本与代码不符")
        )

    return HealthReport(
        scope=scope,
        issues=tuple(issues),
        shards=tuple(shards),
        orphan_contents=orphan,
        dangling_refs=dangling,
        pending_operations=tuple(pending),
        stale_staging=stale,
        leases=tuple(leases),
        duration=time.time() - started,
    )


def _inspect_shard(
    asset_id: str,
    band: tuple[int, int],
    path: Path,
    published_epoch: int,
    issues: list[HealthIssue],
) -> tuple[ShardHealth, int, int]:
    """检查单个分片（结构、``quick_check``、悬空引用、计数、最大 epoch）。

    Returns:
        ``(分片健康, 悬空引用条数, 分片内最大 epoch)``。
    """
    problems: list[str] = []
    try:
        connection = shard_module.open_shard(path, read_only=True)
    except RepoError as exc:
        issues.append(HealthIssue(LEVEL_ERROR, "shard_unreadable", f"{asset_id} {path.name}", str(exc)))
        return ShardHealth(asset_id, band[0], band[1], path.name, ok=False, problems=(str(exc),)), 0, 0
    try:
        try:
            shard_module.check_shard_schema(
                connection,
                asset=AssetId.parse(asset_id),
                band=band,
            )
        except SchemaUnsupported as exc:
            issues.append(HealthIssue(LEVEL_ERROR, "schema_unsupported", path.name, str(exc)))
            return (
                ShardHealth(asset_id, band[0], band[1], path.name, ok=False, problems=(str(exc),)),
                0,
                0,
            )
        except Corrupt as exc:
            issues.append(HealthIssue(LEVEL_ERROR, "shard_corrupt", path.name, str(exc)))
            return (
                ShardHealth(asset_id, band[0], band[1], path.name, ok=False, problems=(str(exc),)),
                0,
                0,
            )

        check = connection.execute("PRAGMA quick_check").fetchone()
        if check is None or str(check[0]).lower() != "ok":
            problems.append(f"quick_check 未通过：{check[0] if check else '无返回'}")
            issues.append(HealthIssue(LEVEL_ERROR, "quick_check_failed", path.name, problems[-1]))

        dangling = int(
            connection.execute(
                "SELECT COUNT(*) FROM coords c LEFT JOIN contents n ON n.sha256 = c.sha256 "
                "WHERE n.sha256 IS NULL"
            ).fetchone()[0]
        )
        if dangling:
            problems.append(f"悬空引用 {dangling} 条")
            issues.append(
                HealthIssue(
                    LEVEL_ERROR,
                    "dangling_ref",
                    path.name,
                    f"{dangling} 条坐标指向不存在的内容（该分片应退出服务）",
                )
            )

        tile_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM coords WHERE state = ?", (CoordState.CURRENT.value,)
            ).fetchone()[0]
        )
        content_count = int(connection.execute("SELECT COUNT(*) FROM contents").fetchone()[0])
        max_epoch = int(
            connection.execute("SELECT COALESCE(MAX(epoch_id), 0) FROM coords").fetchone()[0]
        )
        return (
            ShardHealth(
                asset=asset_id,
                z_min=band[0],
                z_max=band[1],
                file=path.name,
                ok=not problems,
                tile_count=tile_count,
                content_count=content_count,
                published_epoch=published_epoch,
                problems=tuple(problems),
            ),
            dangling,
            max_epoch,
        )
    finally:
        connection.close()


def _content_health(path: Path, issues: list[HealthIssue], asset_id: str) -> int:
    """完整检查：抽样复算摘要 + 统计孤立内容。返回孤立内容条数。"""
    connection = shard_module.open_shard(path, read_only=True)
    try:
        orphan = int(
            connection.execute(
                "SELECT COUNT(*) FROM contents n WHERE NOT EXISTS "
                "(SELECT 1 FROM coords c WHERE c.sha256 = n.sha256)"
            ).fetchone()[0]
        )
        rows = connection.execute(
            "SELECT sha256, data FROM contents ORDER BY first_seen LIMIT ?",
            (TILE_REPO_HEALTH_CONTENT_SAMPLE,),
        ).fetchall()
        for row in rows:
            digest = bytes(row["sha256"])
            payload = bytes(row["data"])
            if make_digest(payload).sha256 != digest:
                issues.append(
                    HealthIssue(
                        LEVEL_ERROR,
                        "digest_mismatch",
                        path.name,
                        f"内容摘要不符：{digest.hex()[:16]}…（抽样检查，资产 {asset_id}）",
                    )
                )
        return orphan
    finally:
        connection.close()


def _stale_staging(folder: Path) -> tuple[str, ...]:
    if not folder.is_dir():
        return ()
    now = time.time()
    stale: list[str] = []
    for entry in folder.iterdir():
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age > TILE_REPO_STAGING_MAX_AGE_SECONDS:
            stale.append(entry.name)
    return tuple(sorted(stale))


# --------------------------------------------------------------------------- #
# 恢复
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RecoveryReport:
    """``recover()`` 的结果。"""

    aborted: tuple[str, ...] = ()
    republished: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    duration: float = 0.0

    @property
    def changed(self) -> int:
        return len(self.aborted) + len(self.republished) + len(self.completed)

    def as_dict(self) -> dict[str, object]:
        return {
            "aborted": list(self.aborted),
            "republished": list(self.republished),
            "completed": list(self.completed),
            "errors": list(self.errors),
            "duration": round(self.duration, 3),
        }

    def as_lines(self) -> list[str]:
        return [
            f"恢复：中止 {len(self.aborted)} 个、补发布 {len(self.republished)} 个、"
            f"补完成 {len(self.completed)} 个、错误 {len(self.errors)} 个"
        ] + [f"  错误：{item}" for item in self.errors]


def recover(repo: TileRepository) -> RecoveryReport:
    """接管未完成的操作（**幂等**，可反复运行）。

    幂等的含义：第二次运行时，第一次处理过的操作已经是终态，不再出现在
    ``unfinished_operations()`` 里；因此第二次报告的三个计数全为 0。
    """
    started = time.time()
    aborted: list[str] = []
    republished: list[str] = []
    completed: list[str] = []
    errors: list[str] = []

    for op in repo.catalog.unfinished_operations():
        op_id = str(op.get("op_id") or "")
        state = str(op.get("state") or "")
        try:
            asset = AssetId.parse(str(op.get("asset_id") or ""))
        except ValueError:
            errors.append(f"{op_id[:8]}…：资产标识非法 {op.get('asset_id')!r}")
            repo.catalog.set_operation_state(op_id, OperationState.ABORTED)
            aborted.append(op_id)
            continue
        epoch = int(op.get("epoch_id") or 0)
        bands = _bands_with_epoch(repo, asset, epoch)

        if not bands:
            # 分片里没有这个 epoch 的行：写在半路的批次已被 SQLite 回滚，
            # 除了"记一笔"，没有别的事要做。
            repo.catalog.set_operation_state(op_id, OperationState.ABORTED)
            aborted.append(op_id)
            continue

        if state == OperationState.PUBLISHED:
            repo.catalog.set_operation_state(op_id, OperationState.DONE)
            completed.append(op_id)
            continue

        try:
            _publish_bands(repo, asset, bands, epoch)
        except RepoError as exc:
            errors.append(f"{op_id[:8]}…：补发布失败（{exc}）")
            continue
        final = (
            OperationState.CANCELLED
            if state == OperationState.CANCELLED
            else OperationState.DONE
        )
        repo.catalog.set_operation_state(op_id, final)
        republished.append(op_id)

    return RecoveryReport(
        aborted=tuple(aborted),
        republished=tuple(republished),
        completed=tuple(completed),
        errors=tuple(errors),
        duration=time.time() - started,
    )


def _bands_with_epoch(
    repo: TileRepository, asset: AssetId, epoch: int
) -> list[tuple[int, int]]:
    """哪些分片里存在这个 epoch 的行（恢复时判断"到底提交没提交"的唯一依据）。"""
    found: list[tuple[int, int]] = []
    for band in repo.bands:
        path = repo.shard_path(asset, band)
        if not path.is_file():
            continue
        try:
            connection = shard_module.open_shard(path, read_only=True)
        except RepoError:
            continue
        try:
            row = connection.execute(
                "SELECT 1 FROM coords WHERE epoch_id = ? LIMIT 1", (int(epoch),)
            ).fetchone()
        except Exception:  # noqa: BLE001 - 表结构坏的分片由 health_check 报
            row = None
        finally:
            connection.close()
        if row is not None:
            found.append(band)
    return found


def _publish_bands(
    repo: TileRepository, asset: AssetId, bands: list[tuple[int, int]], epoch: int
) -> None:
    """补发布：重算统计 + 推水位（一个 catalog 事务）。"""
    stats = {
        band: repo.shard_statistics(asset, band) for band in bands
    }
    with repo.catalog.transaction() as connection:
        for band in bands:
            path = repo.shard_path(asset, band)
            stat = stats[band]
            repo.catalog.upsert_shard(
                connection,
                asset,
                band,
                path.relative_to(repo.root).as_posix(),
                tile_count=stat["tile_count"],
                content_count=stat["content_count"],
                bytes_=stat["bytes"],
                published_epoch=epoch,
            )
            for zoom, rect in stat["coverage"].items():
                repo.catalog.replace_coverage(connection, asset, zoom, *rect)


# --------------------------------------------------------------------------- #
# 索引重建
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RebuildReport:
    """``rebuild_index`` 的结果。"""

    fixed_shards: int = 0
    registered_shards: int = 0
    dropped_entries: int = 0
    registered_snapshots: int = 0
    still_corrupt: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    duration: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "fixed_shards": self.fixed_shards,
            "registered_shards": self.registered_shards,
            "dropped_entries": self.dropped_entries,
            "registered_snapshots": self.registered_snapshots,
            "still_corrupt": list(self.still_corrupt),
            "notes": list(self.notes),
            "duration": round(self.duration, 3),
        }

    def as_lines(self) -> list[str]:
        lines = [
            f"索引重建：修正 {self.fixed_shards} 个分片、新登记 {self.registered_shards} 个、"
            f"摘除 {self.dropped_entries} 条失效登记、补登记 {self.registered_snapshots} 份快照"
        ]
        lines.extend(f"  仍损坏：{item}" for item in self.still_corrupt)
        lines.extend(f"  注：{item}" for item in self.notes)
        return lines


def rebuild_index(repo: TileRepository, asset: str | AssetId | None = None) -> RebuildReport:
    """从分片重建 catalog 登记（**catalog 整个丢了也能修**）。

    可重建的信息（都在 ``shard_meta`` 里，不依赖 catalog）：资产身份、投影、
    瓦片尺寸、zoom 带、分片内行数与覆盖矩形、最大 epoch（水位）。

    **不可重建**的信息（本模块会在 ``notes`` 里明说）：操作历史（哪次写入属于
    哪个用户动作）、原先的 ``published_epoch`` 演进过程、快照与 epoch 的对应关系
    （只能取文件头里记的 epoch）。
    """
    started = time.time()
    target = repo.asset(asset) if asset is not None else None
    fixed = registered = dropped = 0
    still_corrupt: list[str] = []
    notes: list[str] = []
    max_epoch_seen = repo.catalog.max_epoch()

    for path in shard_module.shard_paths_under(repo.root):
        try:
            connection = shard_module.open_shard(path, read_only=True)
        except RepoError as exc:
            still_corrupt.append(f"{path.name}（{exc}）")
            _drop_by_path(repo, path)
            dropped += 1
            continue
        try:
            meta = shard_module.read_shard_meta(connection)
            if not meta:
                still_corrupt.append(f"{path.name}（缺 shard_meta）")
                _drop_by_path(repo, path)
                dropped += 1
                continue
            asset_id = AssetId.parse(str(meta.get("asset_id") or ""))
            band = (int(meta.get("z_min", -1)), int(meta.get("z_max", -1)))
            if target is not None and asset_id != target:
                continue
            if str(meta.get("schema_version")) != str(shard_module.TILE_REPO_SCHEMA_VERSION):
                still_corrupt.append(
                    f"{path.name}（schema_version={meta.get('schema_version')}）"
                )
                _drop_by_path(repo, path)
                dropped += 1
                continue
            stats = _stats_from_connection(connection, path)
            max_epoch_seen = max(max_epoch_seen, stats["max_epoch"])
            before = repo.catalog.shard_registration(asset_id, band)
            coverage_before = repo.catalog.coverage_rects(asset_id)
            _register_asset(repo, asset_id, meta)
            with repo.catalog.transaction() as catalog_connection:
                repo.catalog.upsert_shard(
                    catalog_connection,
                    asset_id,
                    band,
                    path.relative_to(repo.root).as_posix(),
                    tile_count=stats["tile_count"],
                    content_count=stats["content_count"],
                    bytes_=stats["bytes"],
                    published_epoch=stats["max_epoch"] or None,
                )
                for zoom in range(band[0], band[1] + 1):
                    rect = stats["coverage"].get(zoom)
                    if rect is None:
                        repo.catalog.delete_coverage_zoom(catalog_connection, asset_id, zoom)
                    else:
                        repo.catalog.replace_coverage(
                            catalog_connection, asset_id, zoom, *rect
                        )
            if _registration_changed(before, coverage_before, band, stats):
                fixed += 1
            if before is None:
                registered += 1
        except (ValueError, Corrupt) as exc:
            still_corrupt.append(f"{path.name}（{exc}）")
            _drop_by_path(repo, path)
            dropped += 1
        finally:
            connection.close()

    # catalog 里登记了、文件却不在了的条目
    for record in repo.catalog.list_shards(target):
        path = repo.root / record.path
        if not path.is_file():
            repo.catalog.drop_shard(record.asset_id, record.z_min)
            dropped += 1
            notes.append(f"登记指向的分片已不存在：{record.path}（登记已摘除，文件未动）")

    if max_epoch_seen > repo.catalog.max_epoch():
        repo.catalog.ensure_epoch_floor(max_epoch_seen)
        notes.append(
            f"epoch 水位补齐到 {max_epoch_seen}（catalog 丢过；epochs 只追加，不重写历史）"
        )

    registered_snapshots = _register_snapshot_files(repo)
    return RebuildReport(
        fixed_shards=fixed,
        registered_shards=registered,
        dropped_entries=dropped,
        registered_snapshots=registered_snapshots,
        still_corrupt=tuple(still_corrupt),
        notes=tuple(notes),
        duration=time.time() - started,
    )


def _stats_from_connection(connection, path: Path) -> dict:
    tile_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM coords WHERE state = ?", (CoordState.CURRENT.value,)
        ).fetchone()[0]
    )
    content_count = int(connection.execute("SELECT COUNT(*) FROM contents").fetchone()[0])
    max_epoch = int(
        connection.execute("SELECT COALESCE(MAX(epoch_id), 0) FROM coords").fetchone()[0]
    )
    rows = connection.execute(
        "SELECT z, MIN(x), MAX(x), MIN(y), MAX(y) FROM coords WHERE state = ? GROUP BY z",
        (CoordState.CURRENT.value,),
    ).fetchall()
    coverage = {
        int(row[0]): (int(row[1]), int(row[2]), int(row[3]), int(row[4])) for row in rows
    }
    return {
        "tile_count": tile_count,
        "content_count": content_count,
        "max_epoch": max_epoch,
        "bytes": sum(
            _size_of(path.with_name(path.name + suffix)) for suffix in ("", "-wal", "-shm")
        ),
        "coverage": coverage,
    }


def _size_of(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _registration_changed(
    before: object, coverage_before: dict[int, tuple[int, int, int, int]], band: tuple[int, int], stats: dict
) -> bool:
    """这次重建是否**真的**改了 catalog 里的东西（决定 ``fixed_shards`` 计数）。

    第二遍重建必须是"什么都没改"——否则报告里的数字没法用来判断"是不是真修好了"。
    """
    if before is None:
        return True
    # 刻意**不比** ``bytes``：它含 ``-wal``/``-shm`` 的大小，同一个库在不同时刻
    # 量出来本来就不一样。把它算进"这次重建改了什么"，第二遍重建就会永远报
    # "修了 1 个分片"，那个数字随即失去意义（用户没法用它判断是不是真修好了）。
    if (
        int(getattr(before, "tile_count", -1)) != stats["tile_count"]
        or int(getattr(before, "content_count", -1)) != stats["content_count"]
        or int(getattr(before, "published_epoch", -1)) != stats["max_epoch"]
    ):
        return True
    for zoom in range(band[0], band[1] + 1):
        if coverage_before.get(zoom) != stats["coverage"].get(zoom):
            return True
    return False


def _register_asset(repo: TileRepository, asset: AssetId, meta: dict[str, str]) -> None:
    """按 ``shard_meta`` 补登记 source 与 asset（catalog 丢过时必需）。"""
    from app.tilerepo.catalog import SourceSpec, fingerprint_source

    if not repo.catalog.asset_exists(asset):
        spec = SourceSpec(
            source_id=str(meta.get("source_id") or asset.source_id),
            provider=str(meta.get("provider") or repo.provider),
            projection=str(meta.get("projection") or repo.projection),
            tile_size=int(meta.get("tile_size") or repo.tile_size),
            config_fingerprint=fingerprint_source(
                str(meta.get("source_id") or asset.source_id),
                str(meta.get("projection") or repo.projection),
                int(meta.get("tile_size") or repo.tile_size),
                (asset.layer,),
            ),
        )
        repo.catalog.ensure_source(spec)
        repo.catalog.ensure_asset(asset)


def _drop_by_path(repo: TileRepository, path: Path) -> None:
    """把某个文件对应的登记摘掉（按相对路径匹配，**不删文件**）。"""
    relative = path.relative_to(repo.root).as_posix()
    for record in repo.catalog.list_shards():
        if record.path == relative:
            repo.catalog.drop_shard(record.asset_id, record.z_min)


def _register_snapshot_files(repo: TileRepository) -> int:
    """把磁盘上存在、catalog 里没有的快照补登记。"""
    from app.tilerepo import snapshot as snapshot_module

    existing = {str(item["fingerprint"]) for item in repo.catalog.list_snapshots()}
    count = 0
    for path in snapshot_module.snapshot_files(repo):
        fingerprint = path.stem
        if fingerprint in existing:
            continue
        try:
            meta = snapshot_module.load_meta(repo, fingerprint)
        except RepoError:
            continue
        repo.catalog.register_snapshot(
            fingerprint,
            meta.epoch,
            path.relative_to(repo.root).as_posix(),
            meta.entries,
            {"selector": meta.selector, "recovered": True},
        )
        count += 1
    return count


# --------------------------------------------------------------------------- #
# 清理报告
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PruneReport:
    """``prune`` 的结果（本 Slice 只报告，不删除）。"""

    dry_run: bool = True
    orphan_contents: tuple[dict[str, object], ...] = ()
    superseded_expired: int = 0
    reclaimable_bytes: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "orphan_contents": list(self.orphan_contents),
            "superseded_expired": self.superseded_expired,
            "reclaimable_bytes": self.reclaimable_bytes,
            "notes": list(self.notes),
        }

    def as_lines(self) -> list[str]:
        lines = [
            f"清理报告（{'仅报告' if self.dry_run else '执行'}）："
            f"孤立内容 {len(self.orphan_contents)} 份、"
            f"过期历史 {self.superseded_expired} 行、可回收 {self.reclaimable_bytes:,} 字节"
        ]
        lines.extend(f"  注：{item}" for item in self.notes)
        return lines


def prune(repo: TileRepository, dry_run: bool = True) -> PruneReport:
    """报告孤立内容与过期历史版本。

    本 Slice **不实现删除**（``dry_run=False`` 抛 :class:`UnsupportedOperation`）：
    自动回收会与"用户正要导出"撞车（架构 §7.5 的三条前置条件里有一条就是
    "该分片没有活跃读快照"，那需要导出的租约信息，属于 Slice 49）。
    """
    if not dry_run:
        raise UnsupportedOperation(
            "Slice 42 不支持真正删除内容（dry_run=False）；清理属于 Slice 49"
        )
    threshold = time.time() - TILE_REPO_SUPERSEDED_RETENTION_DAYS * 86400
    orphans: list[dict[str, object]] = []
    expired = 0
    reclaimable = 0
    notes: list[str] = []
    for record in repo.catalog.list_shards():
        path = repo.root / record.path
        if not path.is_file():
            continue
        connection = shard_module.open_shard(path, read_only=True)
        try:
            rows = connection.execute(
                "SELECT sha256, size FROM contents n WHERE NOT EXISTS "
                "(SELECT 1 FROM coords c WHERE c.sha256 = n.sha256) LIMIT 200"
            ).fetchall()
            for row in rows:
                orphans.append(
                    {
                        "asset": record.asset_id,
                        "band": [record.z_min, record.z_max],
                        "sha256": bytes(row["sha256"]).hex()[:16],
                        "size": int(row["size"]),
                    }
                )
                reclaimable += int(row["size"])
            expired += int(
                connection.execute(
                    "SELECT COUNT(*) FROM coords WHERE state = ? AND fetched_at < ?",
                    (CoordState.SUPERSEDED.value, threshold),
                ).fetchone()[0]
            )
        finally:
            connection.close()
    if orphans:
        notes.append(
            "孤立内容只报告不删除：删除前必须确认没有导出快照引用它（Slice 49）"
        )
    return PruneReport(
        dry_run=True,
        orphan_contents=tuple(orphans),
        superseded_expired=expired,
        reclaimable_bytes=reclaimable,
        notes=tuple(notes),
    )
