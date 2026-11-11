"""仓库服务入口：写入、读取、缺口、快照、健康检查（Slice 42）。

这一层是**唯一对外的门**。调用方（Slice 43 导入 / 44 下载 / 45 导出 / 46 预算）
不应该知道分片路径怎么拼、catalog 有哪几张表 —— 那些都是本模块的内部实现。

一次写入的完整时序（也是崩溃恢复的依据）::

    begin_operation()        ← 写前日志（catalog.operations, state=pending）
      │
    per (band, chunk):
      acquire_lease()        ← 分片级写租约（拿不到 → Busy，不排队）
      BEGIN IMMEDIATE
        insert contents      ← 先内容后坐标（外键保证不出现悬空引用）
        insert/update coords
      COMMIT                 ← 事实落盘；此后进程被杀也不会丢
      │
    publish()                ← 一个 catalog 事务：统计 + 覆盖矩形 + 发布水位 + state=published
      │
    set_operation_state(done)

三个"看不见但很重要"的点：

1. **可见性靠发布水位**（``shards.published_epoch``）。分片提交了但没发布的行，
   查询会明确返回 ``UNPUBLISHED`` —— 不是"缺图"，也不是"有效资产"。
2. **取消不回滚已提交的批次**。这是契约（§5 统一约定）：回滚会让"重放一次"
   变成唯一选择，而重放对调用方是额外的流量。取消后照样发布，报告里写明
   ``cancelled=True`` + 实际写进去多少。
3. **同名冲突默认拒绝**。同坐标不同内容 → ``Conflict``（不写、旧值不动）。
   这是对现状 ``INSERT OR REPLACE`` 无条件覆盖的正面纠偏（架构 D7）。
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.config.settings import (
    DEFAULT_TILE_REPO_ROOT,
    TILE_REPO_BATCH_SIZE,
    TILE_REPO_LEASE_TIMEOUT_SECONDS,
    TILE_REPO_MISSING_SAMPLE_LIMIT,
    TILE_REPO_MISSING_SAMPLE_MAX_COLUMNS,
    TILE_REPO_PROJECTION,
    TILE_REPO_PROVIDER,
    TILE_REPO_SOURCE_ID,
    TILE_REPO_SQLITE_BUSY_TIMEOUT_MS,
    TILE_REPO_STREAM_BATCH_SIZE,
    TILE_REPO_TILE_SIZE,
)
from app.geo import GeoBounds, calculate_tile_range
from app.tilerepo import shard as shard_module
from app.tilerepo import snapshot as snapshot_module
from app.tilerepo._testhooks import inject as _inject_fault
from app.tilerepo.catalog import (
    Catalog,
    OperationState,
    SourceSpec,
    fingerprint_source,
)
from app.tilerepo.errors import (
    Corrupt,
    NotFound,
    RepoClosed,
    RepoError,
)
from app.tilerepo.identity import (
    AssetId,
    ContentDigest,
    CoordKey,
    CoordState,
    LookupStatus,
    TileLookup,
    TileRecord,
    make_digest,
    validate_tile_bytes,
)

__all__ = [
    "AssetExistence",
    "ConflictItem",
    "ExistenceReport",
    "Progress",
    "PutReport",
    "RejectedItem",
    "TileRepository",
    "ZoomExistence",
]

#: 仓库根下的固定子目录（初始化时创建）。
REPO_DIRECTORIES: tuple[str, ...] = ("shards", "snapshots", "repair", "staging", "lock")

def _never() -> bool:
    """判定"没有错误"的哨兵（``should_stop`` 未提供时用）。"""
    return False


#: 哨兵：``should_stop`` 未提供时统一用它，避免每处都判 ``None``。
_NEVER: Callable[[], bool] = _never


# --------------------------------------------------------------------------- #
# 报告对象
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Progress:
    """进度（**不含** token、不含绝对路径 —— 路径由调用方自己拼）。"""

    stage: str
    done: int
    total: int | None = None
    bytes_: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "bytes": self.bytes_,
        }


@dataclass(frozen=True)
class ConflictItem:
    """一个"同坐标不同内容"的冲突。"""

    coord: tuple[int, int, int]
    existing_sha256: str
    incoming_sha256: str
    existing_epoch: int

    def as_dict(self) -> dict[str, object]:
        return {
            "z": self.coord[0],
            "x": self.coord[1],
            "y": self.coord[2],
            "existing": self.existing_sha256,
            "incoming": self.incoming_sha256,
            "existing_epoch": self.existing_epoch,
        }


@dataclass(frozen=True)
class RejectedItem:
    """一条被拒绝的输入（坐标非法 / 不是合法图片 / 尺寸不符）。"""

    coord: tuple[int, int, int] | None
    reason: str
    code: str

    def as_dict(self) -> dict[str, object]:
        return {"coord": self.coord, "reason": self.reason, "code": self.code}


@dataclass(frozen=True)
class PutReport:
    """一次 ``put_tiles`` 的结果。

    ``written`` / ``unchanged`` / ``replaced`` / ``quarantined`` 是**四类互斥**的
    结果，加起来等于"通过校验的条数"；``rejected`` 是没通过校验的；
    ``conflicts`` 是被拒绝写入的已存在坐标（默认策略下）。
    """

    asset: str
    epoch_id: int
    op_id: str
    written: int = 0
    unchanged: int = 0
    replaced: int = 0
    quarantined: int = 0
    duplicates_dropped: int = 0
    conflicts: tuple[ConflictItem, ...] = ()
    rejected: tuple[RejectedItem, ...] = ()
    shards: tuple[str, ...] = ()
    published: bool = False
    cancelled: bool = False

    @property
    def accepted(self) -> int:
        return self.written + self.unchanged + self.replaced + self.quarantined

    def as_dict(self) -> dict[str, object]:
        return {
            "asset": self.asset,
            "epoch": self.epoch_id,
            "operation": self.op_id,
            "written": self.written,
            "unchanged": self.unchanged,
            "replaced": self.replaced,
            "quarantined": self.quarantined,
            "duplicates_dropped": self.duplicates_dropped,
            "accepted": self.accepted,
            "conflicts": [item.as_dict() for item in self.conflicts],
            "rejected": [item.as_dict() for item in self.rejected],
            "shards": list(self.shards),
            "published": self.published,
            "cancelled": self.cancelled,
        }

    def as_lines(self) -> list[str]:
        lines = [
            f"资产 {self.asset}：写入 {self.written} 张，未变 {self.unchanged} 张，"
            f"替换 {self.replaced} 张，隔离 {self.quarantined} 张",
        ]
        if self.duplicates_dropped:
            lines.append(f"批内重复坐标（以最后一次为准）：{self.duplicates_dropped} 条")
        if self.conflicts:
            lines.append(f"冲突（已保留旧值，未写入）：{len(self.conflicts)} 条")
        if self.rejected:
            lines.append(f"非法输入（未写入）：{len(self.rejected)} 条")
        if self.cancelled:
            lines.append("已取消：已提交的批次保留并已发布，未提交的批次不存在")
        return lines


@dataclass(frozen=True)
class ZoomExistence:
    """单个 zoom 的缺口统计（``missing`` 只数**目标级**缺口）。"""

    zoom: int
    total: int
    present: int
    missing: int
    missing_sample: tuple[tuple[int, int], ...] = ()
    sample_truncated: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "zoom": self.zoom,
            "total": self.total,
            "present": self.present,
            "missing": self.missing,
            "missing_sample": [list(item) for item in self.missing_sample],
            "sample_truncated": self.sample_truncated,
        }


@dataclass(frozen=True)
class AssetExistence:
    """单个资产的缺口统计。"""

    asset: str
    zooms: tuple[ZoomExistence, ...] = ()

    @property
    def total(self) -> int:
        return sum(item.total for item in self.zooms)

    @property
    def present(self) -> int:
        return sum(item.present for item in self.zooms)

    @property
    def missing(self) -> int:
        return sum(item.missing for item in self.zooms)

    def as_dict(self) -> dict[str, object]:
        return {
            "asset": self.asset,
            "total": self.total,
            "present": self.present,
            "missing": self.missing,
            "zooms": [item.as_dict() for item in self.zooms],
        }


@dataclass(frozen=True)
class ExistenceReport:
    """``query_existence`` 的结果。

    注意 ``parent_fallback_available`` 这类信息**不在**这里：它是导出期的提示，
    且明确"不计入 present"（契约 §5.5）。本 Slice 只回答"目标级有没有"。
    """

    assets: tuple[AssetExistence, ...] = ()
    cancelled: bool = False

    @property
    def total(self) -> int:
        return sum(item.total for item in self.assets)

    @property
    def present(self) -> int:
        return sum(item.present for item in self.assets)

    @property
    def missing(self) -> int:
        return sum(item.missing for item in self.assets)

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "present": self.present,
            "missing": self.missing,
            "cancelled": self.cancelled,
            "assets": [item.as_dict() for item in self.assets],
        }

    def as_lines(self) -> list[str]:
        lines = [f"目标级：共 {self.total:,} 张，已有 {self.present:,} 张，缺 {self.missing:,} 张"]
        for asset in self.assets:
            for zoom in asset.zooms:
                if zoom.missing:
                    lines.append(
                        f"  {asset.asset} Z{zoom.zoom}：缺 {zoom.missing:,} / {zoom.total:,}"
                    )
        return lines


# --------------------------------------------------------------------------- #
# 仓库
# --------------------------------------------------------------------------- #
@dataclass
class _PutAccumulator:
    """写入过程中的可变累加器（对外的报告是冻结的 dataclass）。"""

    written: int = 0
    unchanged: int = 0
    replaced: int = 0
    quarantined: int = 0
    duplicates: int = 0
    conflicts: list[ConflictItem] = field(default_factory=list)
    rejected: list[RejectedItem] = field(default_factory=list)
    bands: dict[tuple[int, int], Path] = field(default_factory=dict)

    @property
    def accepted(self) -> int:
        return self.written + self.unchanged + self.replaced + self.quarantined


class TileRepository:
    """一个本地瓦片仓库。

    并发模型（与文档一致，不要自行放宽）::

        同进程多线程   支持。分片写入按 (asset, band) 串行（进程内锁 + 租约重入），
                       读与写可并行（WAL）。
        多进程写同一分片  拒绝（文件租约 → Busy）。不做排队。
        多进程读         支持。
        catalog 写入    靠 SQLite 自己的写者锁（busy_timeout 后报 Busy）。

    生命周期::

        repo = TileRepository.open(root)          # 已存在的仓库
        repo = TileRepository.open(root, create=True)   # 不存在就建
        ...
        repo.close()                              # 释放租约；之后的调用抛 RepoClosed
    """

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        create: bool = False,
        source_id: str = TILE_REPO_SOURCE_ID,
        provider: str = TILE_REPO_PROVIDER,
        projection: str = TILE_REPO_PROJECTION,
        tile_size: int = TILE_REPO_TILE_SIZE,
        bands: tuple[tuple[int, int], ...] | None = None,
        lease_timeout: float = TILE_REPO_LEASE_TIMEOUT_SECONDS,
        batch_size: int = TILE_REPO_BATCH_SIZE,
        stream_batch_size: int = TILE_REPO_STREAM_BATCH_SIZE,
    ) -> None:
        self.root = Path(root) if root is not None else Path(DEFAULT_TILE_REPO_ROOT)
        self.source_id = source_id
        self.provider = provider
        self.projection = projection
        self.tile_size = tile_size
        self.lease_timeout = float(lease_timeout)
        self.batch_size = int(batch_size)
        self.stream_batch_size = int(stream_batch_size)
        self._closed = False
        self._leases: list[shard_module.Lease] = []
        self._keepers: dict[Path, sqlite3.Connection] = {}
        self._keepers_guard = threading.Lock()
        self._created = False

        if create:
            self._prepare_directories()
        self._catalog = Catalog(self.root / "catalog.sqlite", create=create)
        self._created = self._catalog.created
        self._bands = tuple(bands) if bands is not None else self._catalog.zoom_bands()
        if create:
            self._ensure_source()
        self._created = self._catalog.created

    # -- 构造 ---------------------------------------------------------- #
    @classmethod
    def open(
        cls,
        root: Path | str | None = None,
        *,
        create: bool = False,
        **kwargs: object,
    ) -> TileRepository:
        """打开仓库；``create=True`` 时不存在就初始化。

        Raises:
            NotFound: 仓库不存在且 ``create=False``。
            SchemaUnsupported: catalog 的 schema 版本不认识。
        """
        return cls(root, create=create, **kwargs)  # type: ignore[arg-type]

    @property
    def created(self) -> bool:
        """本次调用是否**首次**创建了 catalog（用于"重复打开不重建"的验收）。"""
        return self._created

    def _prepare_directories(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for name in REPO_DIRECTORIES:
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def _ensure_source(self) -> None:
        spec = SourceSpec(
            source_id=self.source_id,
            provider=self.provider,
            projection=self.projection,
            tile_size=self.tile_size,
            config_fingerprint=fingerprint_source(
                self.source_id, self.projection, self.tile_size, ("vec_w", "cva_w", "img_w", "cia_w")
            ),
        )
        self._catalog.ensure_source(spec)

    # -- 路径 ---------------------------------------------------------- #
    @property
    def catalog(self) -> Catalog:
        return self._catalog

    @property
    def lock_dir(self) -> Path:
        return self.root / "lock"

    @property
    def staging_dir(self) -> Path:
        return self.root / "staging"

    @property
    def snapshots_dir(self) -> Path:
        return self.root / "snapshots"

    @property
    def bands(self) -> tuple[tuple[int, int], ...]:
        return self._bands

    def asset(self, value: str | AssetId) -> AssetId:
        """解析资产标识（失败抛 ``ValueError``，那是调用方的编程错误）。"""
        return AssetId.parse(value)

    def shard_path(self, asset: str | AssetId, band: tuple[int, int]) -> Path:
        """某个分片的绝对路径（已做越界校验）。"""
        return shard_module.shard_path(
            self.root,
            self.asset(asset),
            band,
            projection=self.projection,
            tile_size=self.tile_size,
        )

    def band_for(self, zoom: int) -> tuple[int, int]:
        return shard_module.band_for_zoom(zoom, self._bands)

    # -- 生命周期 ------------------------------------------------------ #
    def _require_open(self) -> None:
        if self._closed:
            raise RepoClosed("仓库已关闭（TileRepository.close() 之后的调用一律拒绝）")

    def close(self) -> None:
        """释放租约、关掉常驻连接。幂等。"""
        if self._closed:
            return
        for lease in self._leases:
            lease.release()
        self._leases.clear()
        with self._keepers_guard:
            keepers = list(self._keepers.values())
            self._keepers.clear()
        for keeper in keepers:
            # 收尾失败不该掩盖原异常
            with contextlib.suppress(sqlite3.Error):
                keeper.close()
        self._catalog.close()
        self._closed = True

    @property
    def closed(self) -> bool:
        """是否已关闭（关闭后所有操作抛 :class:`RepoClosed`）。"""
        return self._closed

    def __enter__(self) -> TileRepository:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- 注册 ---------------------------------------------------------- #
    def ensure_asset(self, asset: str | AssetId) -> AssetId:
        """登记资产（幂等）。写之前必须先登记：未登记的资产查询会 ``NotFound``。"""
        self._require_open()
        parsed = self.asset(asset)
        if not self._catalog.asset_exists(parsed):
            self._catalog.ensure_asset(parsed)
        return parsed

    # -- 写入 ---------------------------------------------------------- #
    def put_tiles(
        self,
        asset: str | AssetId,
        items: Iterable[tuple[int, int, int, bytes] | tuple[CoordKey, bytes]],
        *,
        allow_replace: bool = False,
        epoch_id: int | None = None,
        deep: bool = False,
        should_stop: Callable[[], bool] | None = None,
        progress: Callable[[Progress], None] | None = None,
    ) -> PutReport:
        """写入一批瓦片（幂等）。

        Args:
            asset: 资产标识（``'tianditu/vec_w'``）。
            items: 可迭代的 ``(z, x, y, data)``（也接受 ``(CoordKey, data)``）。
                允许生成器：内部按 ``batch_size`` 分批，不会把整批 materialize。
            allow_replace: 同坐标不同内容时是否授权替换（默认 **拒绝**）。
            epoch_id: 指定写入批次；``None`` 表示新建一个 epoch。
            deep: 是否对每张瓦片做完整解码校验（慢；导入验收时才开）。
            should_stop: 取消回调（按条目检查）。取消**不回滚已提交的批次**。
            progress: 进度回调。

        Returns:
            :class:`PutReport`。``conflicts`` 与 ``rejected`` 都在里面，
            不会因为存在冲突就抛异常 —— 除非**全部**条目都冲突（那多半是调用方
            写错了资产，静默返回 0 反而更难发现）。

        Raises:
            NotFound: 资产未登记。
            Corrupt: 摘要冲突 / 分片自述与路径不符 / 分片结构坏。
            Busy: 分片被别的**进程**占用。
            RepoClosed: 仓库已关闭。
        """
        self._require_open()
        target = self.asset(asset)
        if not self._catalog.asset_exists(target):
            raise NotFound(f"资产未登记：{target.text}（先调用 ensure_asset()）")

        epoch = int(epoch_id) if epoch_id is not None else self._catalog.create_epoch(
            "put", target.text
        )
        if epoch_id is not None and not self._catalog.epoch_exists(epoch):
            raise NotFound(f"epoch {epoch} 不存在（epoch 只由 create_epoch 追加）")

        stop = should_stop or _NEVER
        acc = _PutAccumulator()
        op_id = self._catalog.begin_operation(
            "put", target.text, epoch, {"batch_size": self.batch_size}
        )
        _inject_fault("after_operation")
        self._catalog.set_operation_state(op_id, OperationState.WRITING)

        buffer: list[tuple[CoordKey, bytes, ContentDigest]] = []
        seen: dict[tuple[int, int, int], int] = {}
        cancelled = False
        total_bytes = 0
        published = False
        try:
            for raw in items:
                if stop():
                    cancelled = True
                    break
                parsed = self._parse_item(raw, acc)
                if parsed is None:
                    continue
                coord, data, digest = parsed
                key = coord.as_tuple()
                if key in seen:
                    # 批内重复坐标：以最后一次为准（与现状 batch 语义一致）。
                    buffer[seen[key]] = (coord, data, digest)
                    acc.duplicates += 1
                    continue
                seen[key] = len(buffer)
                buffer.append((coord, data, digest))
                total_bytes += len(data)
                if len(buffer) >= self.batch_size:
                    self._flush_buffer(target, buffer, acc, epoch, allow_replace)
                    if progress is not None:
                        progress(Progress("write", acc.accepted, None, total_bytes))
                    buffer = []
                    seen = {}
            # 取消时**不写**剩下这一批：未提交的东西根本不存在，
            # 比"写了一半再回滚"干净（回滚也只能回滚这一批）。
            if buffer and not cancelled:
                self._flush_buffer(target, buffer, acc, epoch, allow_replace)

            if acc.bands:
                _inject_fault("before_publish")
                self._catalog.set_operation_state(op_id, OperationState.PUBLISHING)
                self._publish(target, acc.bands, epoch, op_id, acc)
                published = True
                _inject_fault("after_publish")
            final_state = OperationState.CANCELLED if cancelled else OperationState.DONE
            self._best_effort_state(op_id, final_state)
        except BaseException:
            self._best_effort_state(op_id, OperationState.FAILED)
            raise

        if progress is not None:
            progress(Progress("done", acc.accepted, None, total_bytes))

        report = PutReport(
            asset=target.text,
            epoch_id=epoch,
            op_id=op_id,
            written=acc.written,
            unchanged=acc.unchanged,
            replaced=acc.replaced,
            quarantined=acc.quarantined,
            duplicates_dropped=acc.duplicates,
            conflicts=tuple(acc.conflicts),
            rejected=tuple(acc.rejected),
            shards=tuple(sorted(p.name for p in acc.bands.values())),
            published=published,
            cancelled=cancelled,
        )
        return report

    def _best_effort_state(self, op_id: str, state: str) -> None:
        """改操作状态；失败只吞掉（不能让收尾动作盖住原始异常）。"""
        with contextlib.suppress(Exception):
            self._catalog.set_operation_state(op_id, state)

    def _parse_item(
        self,
        raw: object,
        acc: _PutAccumulator,
    ) -> tuple[CoordKey, bytes, ContentDigest] | None:
        """把一条输入变成 ``(坐标, 字节, 摘要)``；不合法就记进 ``rejected``。"""
        try:
            if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[0], CoordKey):
                coord = raw[0]
                data = raw[1]
            else:
                z, x, y, data = raw  # type: ignore[misc]
                coord = CoordKey(z, x, y)
        except (TypeError, ValueError) as exc:
            acc.rejected.append(
                RejectedItem(None, f"条目格式或坐标非法：{exc}", "invalid_coord")
            )
            return None
        if not isinstance(data, (bytes, bytearray, memoryview)):
            acc.rejected.append(
                RejectedItem(coord.as_tuple(), "瓦片内容必须是 bytes", "invalid_type")
            )
            return None
        payload = bytes(data)
        try:
            digest = validate_tile_bytes(payload, tile_size=self.tile_size, deep=False)
        except RepoError as exc:
            acc.rejected.append(RejectedItem(coord.as_tuple(), str(exc), exc.code))
            return None
        return coord, payload, digest

    def _flush_buffer(
        self,
        asset: AssetId,
        buffer: list[tuple[CoordKey, bytes, ContentDigest]],
        acc: _PutAccumulator,
        epoch: int,
        allow_replace: bool,
    ) -> None:
        """按 band 分组，逐组一个事务写下去。"""
        groups: dict[tuple[int, int], list[tuple[CoordKey, bytes, ContentDigest]]] = {}
        for coord, data, digest in buffer:
            groups.setdefault(self.band_for(coord.z), []).append((coord, data, digest))
        for band, entries in groups.items():
            self._write_group(asset, band, entries, acc, epoch, allow_replace)
        buffer.clear()

    def _write_group(
        self,
        asset: AssetId,
        band: tuple[int, int],
        entries: list[tuple[CoordKey, bytes, ContentDigest]],
        acc: _PutAccumulator,
        epoch: int,
        allow_replace: bool,
    ) -> None:
        path = self.shard_path(asset, band)
        lease = self._acquire(asset, band)
        # 进程内同一分片串行化：文件租约是**进程级**的（本进程内重入），
        # 两个线程会同时持有它。跨进程靠租约 + SQLite 写者锁，进程内靠这把 RLock。
        with shard_module.shard_write_lock(path):
            connection = shard_module.open_shard(path, create=True)
            # 文件此刻一定存在：先把常驻连接留好，别让后面那些"开-关一次"的连接
            # 在收尾时把 -wal/-shm 拆掉（Windows 上的 READONLY 竞态）。
            self._keeper(path)
            try:
                self._ensure_shard_schema(connection, asset, band)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    verified: set[bytes] = set()
                    now = time.time()
                    for coord, data, digest in entries:
                        self._write_one(
                            connection, coord, data, digest, epoch, now, allow_replace, verified, acc
                        )
                    _inject_fault("during_shard_write")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            except sqlite3.DatabaseError as exc:
                # 文件不是 SQLite / 结构坏到连建表都失败：同样是"损坏"，不是"写入失败"
                raise Corrupt(f"分片无法写入：{path.name}（{exc}）") from exc
            finally:
                connection.close()
                lease.heartbeat()
        acc.bands[band] = path
        _inject_fault("after_shard_commit")

    def _ensure_shard_schema(
        self, connection: sqlite3.Connection, asset: AssetId, band: tuple[int, int]
    ) -> None:
        """建表（幂等）并写自述信息。

        DDL 用 ``IF NOT EXISTS``，``shard_meta`` 缺失或不全就补齐，于是"建表/写 meta
        执行到一半崩了"的下一次写入会**自愈** —— 那种库里一行数据都没有，重建不会
        丢任何东西。已经写全 meta 的分片则走正常校验（别人的库不会被我覆盖）。
        """
        connection.executescript(shard_module.DDL)
        meta = shard_module.read_shard_meta(connection)
        required = shard_module.REQUIRED_META_KEYS
        if meta and all(key in meta for key in required):
            shard_module.check_shard_schema(connection, asset=asset, band=band)
            return
        if meta and meta.get("asset_id") not in (None, asset.text):
            raise Corrupt(
                f"分片自述 asset_id={meta.get('asset_id')!r} 与本次写入的 {asset.text!r} 不符"
            )
        connection.execute("BEGIN IMMEDIATE")
        try:
            shard_module.write_shard_meta(
                connection,
                {
                    "schema_version": shard_module.TILE_REPO_SCHEMA_VERSION,
                    "asset_id": asset.text,
                    "source_id": asset.source_id,
                    "layer": asset.layer,
                    "projection": self.projection,
                    "tile_size": self.tile_size,
                    "provider": self.provider,
                    "z_min": band[0],
                    "z_max": band[1],
                    "created_at": time.time(),
                },
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def _write_one(
        self,
        connection: sqlite3.Connection,
        coord: CoordKey,
        data: bytes,
        digest: ContentDigest,
        epoch: int,
        now: float,
        allow_replace: bool,
        verified: set[bytes],
        acc: _PutAccumulator,
    ) -> None:
        """写入单张（在调用方的事务里）。

        顺序刻意是"**先判定、后落内容**"：冲突的条目不该在 ``contents`` 里留下
        一份永远没人引用的字节（那会让 `prune` 报告一堆假孤儿，也会白白吃磁盘）。
        """
        row = connection.execute(
            "SELECT sha256, epoch_id FROM coords WHERE z = ? AND x = ? AND y = ? AND state = ?",
            (coord.z, coord.x, coord.y, CoordState.CURRENT.value),
        ).fetchone()

        if row is not None:
            existing = bytes(row[0])
            existing_epoch = int(row[1])
            if existing == digest.sha256:
                # 幂等：只刷新 fetched_at（无 ETag 时，"内容没变"的唯一证据就是它）。
                connection.execute(
                    "UPDATE coords SET fetched_at = ? "
                    "WHERE z = ? AND x = ? AND y = ? AND state = ?",
                    (now, coord.z, coord.x, coord.y, CoordState.CURRENT.value),
                )
                acc.unchanged += 1
                return
            if not allow_replace:
                acc.conflicts.append(
                    ConflictItem(
                        coord.as_tuple(),
                        existing.hex(),
                        digest.sha256.hex(),
                        existing_epoch,
                    )
                )
                return

        self._ensure_content(connection, digest, data, verified, now)
        if row is not None:
            # 主键是 (z, x, y, state)：同一坐标**只能有一行 superseded**。
            # 二次替换时必须先把上一代历史清掉，否则 INSERT 会撞
            # `UNIQUE constraint failed: coords.z, coords.x, coords.y, coords.state`
            # —— 而那个错误对用户完全不可理解（"替换"怎么会和主键有关）。
            #
            # 代价必须写进文档：同一坐标只保留**一代**历史（最近一次被替换掉的
            # 那一版）。更早的版本在第三次替换时丢弃，被丢弃内容会变成
            # `prune` 报告里的孤儿（不是静默消失，但确实不再可读）。
            connection.execute(
                "DELETE FROM coords WHERE z = ? AND x = ? AND y = ? AND state = ?",
                (coord.z, coord.x, coord.y, CoordState.SUPERSEDED.value),
            )
            connection.execute(
                "UPDATE coords SET state = ? WHERE z = ? AND x = ? AND y = ? AND state = ?",
                (
                    CoordState.SUPERSEDED.value,
                    coord.z,
                    coord.x,
                    coord.y,
                    CoordState.CURRENT.value,
                ),
            )
            acc.replaced += 1
        else:
            acc.written += 1
        connection.execute(
            "INSERT INTO coords (z, x, y, sha256, state, fetched_at, epoch_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (coord.z, coord.x, coord.y, digest.sha256, CoordState.CURRENT.value, now, epoch),
        )

    def _ensure_content(
        self,
        connection: sqlite3.Connection,
        digest: ContentDigest,
        data: bytes,
        verified: set[bytes],
        now: float,
    ) -> bool:
        """确保内容实体存在，返回是否**新增**。

        命中已有摘要时**仍要核对字节**：sha256 撞车的概率可以忽略，但"摘要算错/
        写错列"这类 bug 不是概率问题。同一事务里核对过的摘要记进 ``verified``，
        避免同一批里几千张相同瓦片重复读 BLOB。
        """
        row = connection.execute(
            "SELECT size FROM contents WHERE sha256 = ?", (digest.sha256,)
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO contents (sha256, size, encoding, first_seen, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (digest.sha256, digest.size, digest.encoding, now, data),
            )
            return True
        if int(row[0]) != digest.size:
            raise Corrupt(
                f"摘要冲突：{digest.hex[:16]}… 已存在的内容长度 {int(row[0])} ≠ 本次 {digest.size}"
            )
        if digest.sha256 in verified:
            return False
        existing = connection.execute(
            "SELECT data FROM contents WHERE sha256 = ?", (digest.sha256,)
        ).fetchone()
        if existing is None or bytes(existing[0]) != data:
            raise Corrupt(
                f"摘要冲突：{digest.hex[:16]}… 的已存字节与本次不同（拒绝合并，请隔离该分片）"
            )
        verified.add(digest.sha256)
        return False

    # -- 租约 ---------------------------------------------------------- #
    def _acquire(self, asset: AssetId, band: tuple[int, int]) -> shard_module.Lease:
        lease = shard_module.acquire_lease(
            self.lock_dir,
            shard_module.shard_lock_name(asset, band),
            timeout=self.lease_timeout,
            label=f"{asset.text} {shard_module.band_name(band)}",
        )
        self._leases.append(lease)
        return lease

    # -- 只读连接 ------------------------------------------------------ #
    def _keeper(self, path: Path) -> None:
        """给一个**已存在**的分片留一个空闲常驻连接。

        理由与 ``Catalog`` 里的常驻连接相同：WAL 的 ``-wal``/``-shm`` 由"最后一个
        关闭的连接"清理。一边频繁开关连接、一边在写，Windows 上实测会偶发
        ``attempt to write a readonly database``（清理竞态，与权限无关）。
        常驻连接不做任何查询，只负责别让 shm 在别人手里被删掉。

        刻意**不创建文件**：分片不存在时什么都不做 —— 读路径要能如实回答
        "这个 zoom 带还没有分片"，凭空建一个空库会把它变成"损坏"。
        """
        if not path.is_file():
            return
        with self._keepers_guard:
            if path in self._keepers:
                return
            keeper = sqlite3.connect(
                f"file:{path.as_posix()}?mode=rw",
                uri=True,
                timeout=TILE_REPO_SQLITE_BUSY_TIMEOUT_MS / 1000.0,
                isolation_level=None,
                check_same_thread=False,
            )
            try:
                keeper.execute("PRAGMA busy_timeout = 0")
                keeper.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            except sqlite3.DatabaseError as exc:
                keeper.close()
                raise Corrupt(f"分片无法读取：{path.name}（{exc}）") from exc
            self._keepers[path] = keeper

    @contextlib.contextmanager
    def _read_shard(self, path: Path) -> Iterator[sqlite3.Connection]:
        """只读打开一个分片，并把 SQLite 的底层错误统一成 :class:`Corrupt`。

        为什么要包一层：文件被外部工具写坏（或被换成非 SQLite 文件）时，
        ``sqlite3`` 抛的是 ``DatabaseError: file is not a database`` ——
        调用方按契约只认 ``Corrupt``，"读失败"和"文件坏了"在 UI 上是两种提示
        （前者请重试，后者请隔离/重建）。不统一的话，上层只能 ``except Exception``。
        """
        self._keeper(path)
        connection = shard_module.open_shard(path, read_only=True)
        try:
            yield connection
        except sqlite3.DatabaseError as exc:
            raise Corrupt(f"分片无法读取：{path.name}（{exc}）") from exc
        finally:
            connection.close()

    # -- 发布 ---------------------------------------------------------- #
    def _publish(
        self,
        asset: AssetId,
        bands: dict[tuple[int, int], Path],
        epoch: int,
        op_id: str,
        acc: _PutAccumulator,
    ) -> None:
        """把已提交的分片"发布"到 catalog（一个事务，含水位与操作状态）。"""
        stats = {
            band: self.shard_statistics(asset, band, path) for band, path in bands.items()
        }
        with self._catalog.transaction() as connection:
            for band, stat in stats.items():
                path = bands[band]
                relative = path.relative_to(self.root).as_posix()
                self._catalog.upsert_shard(
                    connection,
                    asset,
                    band,
                    relative,
                    tile_count=stat["tile_count"],
                    content_count=stat["content_count"],
                    bytes_=stat["bytes"],
                    published_epoch=epoch,
                )
                for zoom, rect in stat["coverage"].items():
                    self._catalog.replace_coverage(connection, asset, zoom, *rect)
            self._catalog.update_operation(
                connection,
                op_id,
                OperationState.PUBLISHED,
                {
                    "written": acc.written,
                    "unchanged": acc.unchanged,
                    "replaced": acc.replaced,
                    "conflicts": len(acc.conflicts),
                    "rejected": len(acc.rejected),
                    "shards": sorted(path.name for path in bands.values()),
                },
            )

    def shard_statistics(
        self, asset: str | AssetId, band: tuple[int, int], path: Path | None = None
    ) -> dict:
        """一个分片的真实统计（``tile_count`` / ``content_count`` / 字节 / 覆盖矩形）。

        每次发布都**真数一遍**（``COUNT(*)``），不做增量累加：增量累加在"崩溃后
        只提交了一半的分片"上会漂移，而 catalog 一旦漂移，健康检查就永远在报警。
        PK 索引上的 ``COUNT(*)`` 在几十万行的量级是毫秒级，这个代价换"数字永远是真的"。
        """
        target = path if path is not None else self.shard_path(asset, band)
        if not target.is_file():
            return {"tile_count": 0, "content_count": 0, "bytes": 0, "coverage": {}}
        with self._read_shard(target) as connection:
            tile_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM coords WHERE state = ?", (CoordState.CURRENT.value,)
                ).fetchone()[0]
            )
            content_count = int(
                connection.execute("SELECT COUNT(*) FROM contents").fetchone()[0]
            )
            rows = connection.execute(
                "SELECT z, MIN(x), MAX(x), MIN(y), MAX(y) FROM coords "
                "WHERE state = ? GROUP BY z",
                (CoordState.CURRENT.value,),
            ).fetchall()
        coverage = {
            int(row[0]): (int(row[1]), int(row[2]), int(row[3]), int(row[4])) for row in rows
        }
        return {
            "tile_count": tile_count,
            "content_count": content_count,
            "bytes": _file_bytes(target),
            "coverage": coverage,
        }

    # -- 读取 ---------------------------------------------------------- #
    def read_tile(
        self,
        asset: str | AssetId,
        z: int,
        x: int,
        y: int,
        *,
        epoch: int | None = None,
        include_unpublished: bool = False,
        verify: bool = False,
    ) -> TileLookup:
        """查一张瓦片。

        Args:
            epoch: 指定版本（``None`` = 当前可见版本）。
            include_unpublished: 是否连"已提交但未发布"的行一起看。
                只有**维护/验收**路径该打开它（普通查询打开它等于绕过发布语义）。
            verify: 是否复算内容摘要（``CORRUPT`` 检测；健康检查用）。

        Returns:
            :class:`TileLookup` —— 不存在返回 ``ABSENT``，**不抛异常**。

        Raises:
            NotFound: 资产未登记。
            Corrupt: 分片文件坏了（打不开/自述不符）。
            RepoClosed: 仓库已关闭。
        """
        self._require_open()
        target = self.asset(asset)
        if not self._catalog.asset_exists(target):
            raise NotFound(f"资产未登记：{target.text}")
        coord = CoordKey(z, x, y)
        band = self.band_for(coord.z)
        path = self.shard_path(target, band)
        if not path.is_file():
            return TileLookup(LookupStatus.ABSENT, reason="该 zoom 带还没有分片文件")

        watermark = self._catalog.published_epoch(target, band)
        with self._read_shard(path) as connection:
            row = self._select_coord(connection, coord, epoch)
            if row is None:
                return TileLookup(LookupStatus.ABSENT)
            state = CoordState.parse(row["state"])
            row_epoch = int(row["epoch_id"])
            digest_blob = bytes(row["sha256"])
            if epoch is None and not include_unpublished and row_epoch > watermark:
                return TileLookup(
                    LookupStatus.UNPUBLISHED,
                    reason=f"epoch {row_epoch} 尚未发布（水位 {watermark}）",
                )
            content = connection.execute(
                "SELECT size, encoding, data FROM contents WHERE sha256 = ?", (digest_blob,)
            ).fetchone()
            if content is None:
                return TileLookup(
                    LookupStatus.CORRUPT,
                    reason=f"坐标指向的内容不存在：{digest_blob.hex()[:16]}…",
                )
            payload = bytes(content["data"])
            digest = ContentDigest(digest_blob, int(content["size"]), str(content["encoding"]))
            if verify:
                actual = make_digest(payload)
                if actual.sha256 != digest.sha256 or actual.size != digest.size:
                    return TileLookup(
                        LookupStatus.CORRUPT,
                        reason=f"内容摘要不符：索引 {digest.hex[:16]}… 实际 {actual.hex[:16]}…",
                    )
            if state is CoordState.QUARANTINED:
                return TileLookup(
                    LookupStatus.QUARANTINED,
                    reason="该坐标当前版本被判坏，等待修复",
                )
            record = TileRecord(
                asset=target,
                coord=coord,
                digest=digest,
                epoch=row_epoch,
                state=state,
                fetched_at=float(row["fetched_at"]),
                data=payload,
            )
            return TileLookup(LookupStatus.PRESENT, record=record)

    @staticmethod
    def _select_coord(
        connection: sqlite3.Connection, coord: CoordKey, epoch: int | None
    ) -> sqlite3.Row | None:
        """取一条坐标行：指定 epoch 就取那一代，否则优先 ``current``。"""
        if epoch is not None:
            return connection.execute(
                "SELECT sha256, state, epoch_id, fetched_at FROM coords "
                "WHERE z = ? AND x = ? AND y = ? AND epoch_id = ? "
                "ORDER BY state ASC LIMIT 1",
                (coord.z, coord.x, coord.y, int(epoch)),
            ).fetchone()
        row = connection.execute(
            "SELECT sha256, state, epoch_id, fetched_at FROM coords "
            "WHERE z = ? AND x = ? AND y = ? AND state = ?",
            (coord.z, coord.x, coord.y, CoordState.CURRENT.value),
        ).fetchone()
        if row is not None:
            return row
        return connection.execute(
            "SELECT sha256, state, epoch_id, fetched_at FROM coords "
            "WHERE z = ? AND x = ? AND y = ? ORDER BY state ASC, epoch_id DESC LIMIT 1",
            (coord.z, coord.x, coord.y),
        ).fetchone()

    def iter_versions(
        self, asset: str | AssetId, z: int, x: int, y: int
    ) -> Iterator[tuple[CoordState, int, ContentDigest, float]]:
        """某坐标的全部历史版本（``state, epoch, digest, fetched_at``），按 epoch 升序。"""
        self._require_open()
        target = self.asset(asset)
        coord = CoordKey(z, x, y)
        path = self.shard_path(target, self.band_for(coord.z))
        if not path.is_file():
            return
        with self._read_shard(path) as connection:
            rows = connection.execute(
                "SELECT c.state, c.epoch_id, c.sha256, c.fetched_at, n.size, n.encoding "
                "FROM coords c LEFT JOIN contents n ON n.sha256 = c.sha256 "
                "WHERE c.z = ? AND c.x = ? AND c.y = ? ORDER BY c.epoch_id",
                (coord.z, coord.x, coord.y),
            ).fetchall()
        for row in rows:
            if row["size"] is None:
                continue
            yield (
                CoordState.parse(row["state"]),
                int(row["epoch_id"]),
                ContentDigest(bytes(row["sha256"]), int(row["size"]), str(row["encoding"])),
                float(row["fetched_at"]),
            )

    def iter_tiles(
        self,
        asset: str | AssetId,
        *,
        zoom_range: tuple[int, int] | None = None,
        with_data: bool = False,
        include_unpublished: bool = False,
        state: CoordState = CoordState.CURRENT,
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[TileRecord]:
        """流式遍历一个资产的瓦片（**不把全仓坐标载入内存**）。

        实现要点：按 band 升序、每个分片一条 SQL、``fetchmany`` 分批取。
        ``coords`` 的主键是 ``(z, x, y, state)``，因此 ``ORDER BY z, x, y`` 可以
        直接吃主键顺序，不需要临时 B 树（测试里用 ``EXPLAIN QUERY PLAN`` 钉住这条）。

        Args:
            zoom_range: 闭区间；``None`` = 全部分片带。
            with_data: 是否把图片字节一起读出来（遍历统计时关掉，省内存）。
            include_unpublished: 维护用（默认只吐"可见"的行）。
            state: 只吐这一种状态（默认 current）。
            should_stop: 取消回调；返回 True 时**正常结束**并释放连接。
        """
        self._require_open()
        target = self.asset(asset)
        stop = should_stop or _NEVER
        low, high = (0, 22) if zoom_range is None else zoom_range
        for band in self._bands:
            if band[1] < low or band[0] > high:
                continue
            path = self.shard_path(target, band)
            if not path.is_file():
                continue
            watermark = self._catalog.published_epoch(target, band)
            limit_epoch = 1 << 62 if include_unpublished else watermark
            with self._read_shard(path) as connection:
                sql = (
                    "SELECT c.z, c.x, c.y, c.sha256, c.state, c.epoch_id, c.fetched_at, "
                    "n.size, n.encoding"
                    + (", n.data" if with_data else "")
                    + " FROM coords c JOIN contents n ON n.sha256 = c.sha256 "
                    "WHERE c.z BETWEEN ? AND ? AND c.state = ? AND c.epoch_id <= ? "
                    "ORDER BY c.z, c.x, c.y"
                )
                cursor = connection.execute(
                    sql, (max(low, band[0]), min(high, band[1]), state.value, limit_epoch)
                )
                while True:
                    rows = cursor.fetchmany(self.stream_batch_size)
                    if not rows:
                        break
                    for row in rows:
                        if stop():
                            return
                        digest = ContentDigest(
                            bytes(row["sha256"]), int(row["size"]), str(row["encoding"])
                        )
                        yield TileRecord(
                            asset=target,
                            coord=CoordKey(int(row["z"]), int(row["x"]), int(row["y"])),
                            digest=digest,
                            epoch=int(row["epoch_id"]),
                            state=CoordState.parse(row["state"]),
                            fetched_at=float(row["fetched_at"]),
                            data=bytes(row["data"]) if with_data else b"",
                        )

    # -- 缺口 ---------------------------------------------------------- #
    def query_existence(
        self,
        assets: str | AssetId | Sequence[str | AssetId],
        zoom_range: tuple[int, int],
        bounds: GeoBounds | None = None,
        *,
        region_filter: object | None = None,
        missing_sample_limit: int = TILE_REPO_MISSING_SAMPLE_LIMIT,
        max_sample_columns: int = TILE_REPO_MISSING_SAMPLE_MAX_COLUMNS,
        should_stop: Callable[[], bool] | None = None,
    ) -> ExistenceReport:
        """按 zoom 统计"要求多少张 / 已有多少张 / 缺多少张"。

        ``missing`` 只数**目标级**缺口：父级回退（Android 的 ``MapTileApproximater``）
        **不计入** present。把它算进来会产出"宣称 Z16 齐全、实际全是 Z14 放大"的包
        ——而用户要 Z16 就是为了看得更细（契约 §6.6）。

        Args:
            assets: 一个或多个资产。merged 交付要分别对底图和注记算缺口。
            zoom_range: 闭区间。
            bounds: 地理范围；``None`` = 该级别全球。
            region_filter: 可选，带 ``intersects_tile_xyz(z, x, y)`` 的对象
                （复用 :mod:`app.geo.tile_region_filter`）。给了它就必须同时给
                ``bounds``（"选中张数"要靠它算）。
            missing_sample_limit: 每级最多列多少个缺失坐标样例。
            max_sample_columns: 生成样例时最多扫多少列（超出标"已截断"）。

        Returns:
            :class:`ExistenceReport`（只读，幂等）。

        Raises:
            NotFound: 资产未登记。
            ValueError: 只给了 ``region_filter`` 没给 ``bounds``。
        """
        self._require_open()
        if region_filter is not None and bounds is None:
            raise ValueError("给了 region_filter 就必须给 bounds（否则无法确定候选区间）")
        targets = (
            [assets] if isinstance(assets, (str, AssetId)) else list(assets)
        )
        stop = should_stop or _NEVER
        low, high = int(zoom_range[0]), int(zoom_range[1])
        if low > high:
            raise ValueError(f"zoom_range 起点不能大于终点：{zoom_range}")

        report: list[AssetExistence] = []
        cancelled = False
        for raw_asset in targets:
            target = self.asset(raw_asset)
            if not self._catalog.asset_exists(target):
                raise NotFound(f"资产未登记：{target.text}")
            zooms: list[ZoomExistence] = []
            for zoom in range(low, high + 1):
                if stop():
                    cancelled = True
                    break
                zooms.append(
                    self._zoom_existence(
                        target,
                        zoom,
                        bounds,
                        region_filter,
                        missing_sample_limit,
                        max_sample_columns,
                        stop,
                    )
                )
            report.append(AssetExistence(target.text, tuple(zooms)))
            if cancelled:
                break
        return ExistenceReport(tuple(report), cancelled=cancelled)

    def _zoom_existence(
        self,
        asset: AssetId,
        zoom: int,
        bounds: GeoBounds | None,
        region_filter: object | None,
        sample_limit: int,
        max_columns: int,
        stop: Callable[[], bool],
    ) -> ZoomExistence:
        band = self.band_for(zoom)
        path = self.shard_path(asset, band)
        if bounds is None:
            limit = 1 << zoom
            x_min = y_min = 0
            x_max = y_max = limit - 1
            total = limit * limit
        else:
            tile_range = calculate_tile_range(bounds, zoom)
            x_min, x_max = tile_range.min_x, tile_range.max_x
            y_min, y_max = tile_range.min_y, tile_range.max_y
            total = tile_range.tile_count
        if region_filter is not None:
            stats = region_filter.count_selected_tiles(bounds, zoom)  # type: ignore[attr-defined]
            total = int(getattr(stats, "selected_tiles", total))

        if not path.is_file():
            # 整个分片都不存在：所有坐标都缺，但样例仍然给出来 ——
            # "缺了 4 张，样例是 ()" 会被读成"不知道该缺哪些"。
            sample, truncated = self._missing_sample(
                None,
                zoom,
                watermark=0,
                x_min=x_min,
                x_max=x_max,
                y_min=y_min,
                y_max=y_max,
                region_filter=region_filter,
                missing=total,
                sample_limit=sample_limit,
                max_columns=max_columns,
            )
            return ZoomExistence(zoom, total, 0, total, sample, truncated)

        watermark = self._catalog.published_epoch(asset, band)
        with self._read_shard(path) as connection:
            if region_filter is None:
                present = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM coords WHERE z = ? AND state = ? "
                        "AND epoch_id <= ? AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
                        (
                            zoom,
                            CoordState.CURRENT.value,
                            watermark,
                            x_min,
                            x_max,
                            y_min,
                            y_max,
                        ),
                    ).fetchone()[0]
                )
            else:
                present = 0
                cursor = connection.execute(
                    "SELECT x, y FROM coords WHERE z = ? AND state = ? AND epoch_id <= ? "
                    "AND x BETWEEN ? AND ? AND y BETWEEN ? AND ? ORDER BY x, y",
                    (zoom, CoordState.CURRENT.value, watermark, x_min, x_max, y_min, y_max),
                )
                while True:
                    rows = cursor.fetchmany(self.stream_batch_size)
                    if not rows:
                        break
                    for row in rows:
                        if stop():
                            break
                        if region_filter.intersects_tile_xyz(zoom, int(row[0]), int(row[1])):  # type: ignore[attr-defined]
                            present += 1
            missing = max(0, total - present)
            sample, truncated = self._missing_sample(
                connection,
                zoom,
                watermark,
                x_min,
                x_max,
                y_min,
                y_max,
                region_filter,
                missing,
                sample_limit,
                max_columns,
            )
        return ZoomExistence(zoom, total, present, missing, sample, truncated)

    def _missing_sample(
        self,
        connection: sqlite3.Connection | None,
        zoom: int,
        watermark: int,
        x_min: int,
        x_max: int,
        y_min: int,
        y_max: int,
        region_filter: object | None,
        missing: int,
        sample_limit: int,
        max_columns: int,
    ) -> tuple[tuple[tuple[int, int], ...], bool]:
        """生成缺失坐标样例（按列扫、有上限，**不会为了凑样例遍历整个 zoom**）。

        ``connection=None`` 表示"分片文件根本不存在"，此时所有坐标都缺。

        Returns:
            ``(样例, 是否被截断)``。截断时样例只覆盖前若干列 —— 报告里必须
            如实标注，否则"样例里没有"会被误读成"那里不缺"。
        """
        if missing <= 0 or sample_limit <= 0:
            return (), False
        found: list[tuple[int, int]] = []
        columns_scanned = 0
        for x in range(x_min, x_max + 1):
            if len(found) >= sample_limit:
                break
            if columns_scanned >= max_columns:
                return tuple(found), True
            columns_scanned += 1
            present_y: set[int] = set()
            if connection is not None:
                rows = connection.execute(
                    "SELECT y FROM coords WHERE z = ? AND x = ? AND state = ? AND epoch_id <= ? "
                    "AND y BETWEEN ? AND ? ORDER BY y",
                    (zoom, x, CoordState.CURRENT.value, watermark, y_min, y_max),
                ).fetchall()
                present_y = {int(row[0]) for row in rows}
            for y in range(y_min, y_max + 1):
                if y in present_y:
                    continue
                if region_filter is not None and not region_filter.intersects_tile_xyz(  # type: ignore[attr-defined]
                    zoom, x, y
                ):
                    continue
                found.append((x, y))
                if len(found) >= sample_limit:
                    return tuple(found), x < x_max
        return tuple(found), False

    # -- 快照 ---------------------------------------------------------- #
    def freeze_snapshot(
        self,
        assets: Sequence[str | AssetId] | None = None,
        *,
        zoom_range: tuple[int, int] | None = None,
        bounds: GeoBounds | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> snapshot_module.SnapshotMeta:
        """冻结一份稳定读取视图（跨分片的**逻辑**快照，不是 SQLite 连接快照）。"""
        self._require_open()
        return snapshot_module.freeze_snapshot(
            self,
            assets=assets,
            zoom_range=zoom_range,
            bounds=bounds,
            should_stop=should_stop,
        )

    def open_snapshot(self, fingerprint: str) -> snapshot_module.SnapshotView:
        """打开一份已冻结的快照（用完 ``close()`` 或用 ``with``）。"""
        self._require_open()
        return snapshot_module.SnapshotView.open(self, fingerprint)

    # -- 健康 / 维护 --------------------------------------------------- #
    def health_check(self, scope: str = "quick") -> object:
        """健康检查（默认只读）。见 :func:`app.tilerepo.health.health_check`。"""
        self._require_open()
        from app.tilerepo import health

        return health.health_check(self, scope=scope)

    def recover(self) -> object:
        """接管未完成的操作（幂等）。见 :func:`app.tilerepo.health.recover`。"""
        self._require_open()
        from app.tilerepo import health

        return health.recover(self)

    def rebuild_index(self, asset: str | AssetId | None = None) -> object:
        """从分片重建 catalog 登记（catalog 丢了也能修）。"""
        self._require_open()
        from app.tilerepo import health

        return health.rebuild_index(self, asset=asset)

    def prune(self, dry_run: bool = True) -> object:
        """孤立内容/过期版本报告。**本 Slice 只支持 ``dry_run``**。"""
        self._require_open()
        from app.tilerepo import health

        return health.prune(self, dry_run=dry_run)

    def stats(self, asset: str | AssetId | None = None) -> object:
        """容量与状态统计（逻辑字节与物理占用分开）。"""
        self._require_open()
        from app.tilerepo import stats

        return stats.collect(self, asset=asset)


def _file_bytes(path: Path) -> int:
    """``.sqlite`` + ``-wal`` + ``-shm`` 的实际占用（WAL 里的数据也算仓库占用）。"""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = path.with_name(path.name + suffix)
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total
