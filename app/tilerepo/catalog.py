"""中央 catalog：注册、epoch 水位、操作日志、发布（Slice 42）。

**分片是事实，catalog 是缓存**
------------------------------

这是本模块唯一需要记住的一句话。SQLite **没有跨多个数据库文件的原子提交**
（``ATTACH`` 能跨库写，但崩溃时各库各自回滚，没有两阶段提交），所以写入顺序
被固定成::

    1. 写分片并提交          ← 事实落盘（contents 与 coords 同一事务，原子）
    2. 更新 catalog 并提交   ← 缓存追上来（shards 统计 / coverage / 水位）
    3. 标记操作完成

任何"catalog 说 A、分片说 B"的冲突，一律以分片为准，并触发该分片的重建登记
（:func:`app.tilerepo.health.rebuild_index`）。中断在第 2 步只造成 catalog 落后，
不会造成数据丢失。

于是 catalog 里**没有逐瓦片行**，这是硬约束：Z13 单级全球是 ``4**13`` =
6,710 万张，把每张都放进中央索引，索引本身就会超过瓦片数据量级。逐坐标的事实
由分片主键回答，catalog 只回答"哪些分片存在、覆盖哪几级、粗范围在哪、水位到哪"。

可见性水位
----------

「写入已提交」不等于「可以当资产用」。本模块用 ``shards.published_epoch`` 当
**发布水位**：读取时只认 ``epoch_id <= published_epoch`` 的行。于是::

    崩在"分片提交之后、catalog 发布之前"  → 数据在，但不可见（不是半成品资产）
    health_check 报告它，recover() 把它补发布 → 数据不会丢

这条水位是整个故障恢复语义的支点，不要为了"少一次查询"把它去掉。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.config.settings import (
    TILE_REPO_SCHEMA_VERSION,
    TILE_REPO_SQLITE_BUSY_TIMEOUT_MS,
    TILE_REPO_ZOOM_BANDS,
)
from app.tilerepo.errors import Corrupt, NotFound, SchemaUnsupported
from app.tilerepo.identity import AssetId

__all__ = [
    "CATALOG_FILENAME",
    "CATALOG_SCHEMA_VERSION",
    "DDL",
    "OPERATION_STATES",
    "Catalog",
    "OperationState",
    "ShardRegistration",
    "SourceSpec",
]

CATALOG_FILENAME: str = "catalog.sqlite"

#: catalog 自己的 schema 版本。与分片的 ``schema_version`` 各自独立演进
#: （catalog 加一张表不影响分片布局），但**都记在各自库里**，认不出来就拒绝服务。
CATALOG_SCHEMA_VERSION: int = TILE_REPO_SCHEMA_VERSION


class OperationState:
    """``operations.state`` 的取值（**持久化契约**，不要改名）。

    状态机::

        pending ──▶ writing ──▶ shard_committed ──▶ publishing ──▶ published ──▶ done
           │            │                │                              │
           ├─▶ aborted  └─▶ cancelled ───┘                              └─▶ failed

    ``pending`` / ``writing`` / ``shard_committed`` / ``publishing`` 是**未完成**态，
    ``recover()`` 会接管它们；``published`` 也归未完成：数据已可见，只差"打个勾"。
    """

    PENDING = "pending"
    WRITING = "writing"
    SHARD_COMMITTED = "shard_committed"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    DONE = "done"
    ABORTED = "aborted"
    CANCELLED = "cancelled"
    FAILED = "failed"

    #: 需要 ``recover()`` 处理的状态
    UNFINISHED: tuple[str, ...] = (PENDING, WRITING, SHARD_COMMITTED, PUBLISHING, PUBLISHED)
    #: 终态
    FINAL: tuple[str, ...] = (DONE, ABORTED, CANCELLED, FAILED)


OPERATION_STATES: tuple[str, ...] = (
    OperationState.PENDING,
    OperationState.WRITING,
    OperationState.SHARD_COMMITTED,
    OperationState.PUBLISHING,
    OperationState.PUBLISHED,
    OperationState.DONE,
    OperationState.ABORTED,
    OperationState.CANCELLED,
    OperationState.FAILED,
)

#: catalog DDL。
#:
#: ``operations`` 是**写前日志**：它在碰分片之前就落盘，于是"这个进程死在写入
#: 中途"是可以被后来者发现的。``epochs`` 只追加（INSERT），不 UPDATE、不 DELETE。
DDL: str = """
CREATE TABLE IF NOT EXISTS catalog_meta (
  name  TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS sources (
  source_id          TEXT PRIMARY KEY,
  provider           TEXT NOT NULL,
  projection         TEXT NOT NULL,
  tile_size          INTEGER NOT NULL,
  config_fingerprint TEXT NOT NULL,
  created_at         REAL NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS assets (
  asset_id   TEXT PRIMARY KEY,
  source_id  TEXT NOT NULL,
  layer      TEXT NOT NULL,
  role       TEXT NOT NULL,
  created_at REAL NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS zoom_bands (
  z_min INTEGER PRIMARY KEY,
  z_max INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS shards (
  asset_id        TEXT NOT NULL,
  z_min           INTEGER NOT NULL,
  z_max           INTEGER NOT NULL,
  path            TEXT NOT NULL,
  tile_count      INTEGER NOT NULL DEFAULT 0,
  content_count   INTEGER NOT NULL DEFAULT 0,
  bytes           INTEGER NOT NULL DEFAULT 0,
  published_epoch INTEGER NOT NULL DEFAULT 0,
  checked_at      REAL,
  PRIMARY KEY (asset_id, z_min)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS epochs (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  at       REAL NOT NULL,
  kind     TEXT NOT NULL,
  asset_id TEXT NOT NULL DEFAULT '',
  detail   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS operations (
  op_id      TEXT PRIMARY KEY,
  kind       TEXT NOT NULL,
  asset_id   TEXT NOT NULL,
  epoch_id   INTEGER NOT NULL,
  state      TEXT NOT NULL,
  detail     TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS operations_by_state ON operations (state);

CREATE TABLE IF NOT EXISTS coverage (
  asset_id TEXT NOT NULL,
  z        INTEGER NOT NULL,
  x_min    INTEGER NOT NULL,
  x_max    INTEGER NOT NULL,
  y_min    INTEGER NOT NULL,
  y_max    INTEGER NOT NULL,
  PRIMARY KEY (asset_id, z)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS snapshots (
  fingerprint TEXT PRIMARY KEY,
  created_at  REAL NOT NULL,
  epoch_id    INTEGER NOT NULL,
  path        TEXT NOT NULL,
  entries     INTEGER NOT NULL,
  detail      TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS legacy_assets (
  id            TEXT PRIMARY KEY,
  path          TEXT NOT NULL,
  kind          TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  note          TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;
"""


@dataclass(frozen=True)
class SourceSpec:
    """数据源规格（``sources`` 一行）。

    ``config_fingerprint`` 覆盖 **source_id / projection / tile_size**，
    **不含 token、不含 base_url**：Slice 42 没有下载入口，拿不到 URL 模板；
    Slice 44 接入下载后可以把"去掉 token 的 URL 模板"并进来，那时会改指纹 ——
    改指纹不该让任何资产失效（指纹只用于"这两份配置是不是同一个"）。
    """

    source_id: str
    provider: str
    projection: str
    tile_size: int
    config_fingerprint: str


@dataclass(frozen=True)
class ShardRegistration:
    """``shards`` 一行（catalog 视角的分片状态）。"""

    asset_id: str
    z_min: int
    z_max: int
    path: str
    tile_count: int = 0
    content_count: int = 0
    bytes: int = 0
    published_epoch: int = 0
    checked_at: float | None = None


def fingerprint_source(
    source_id: str, projection: str, tile_size: int, layers: tuple[str, ...]
) -> str:
    """算 ``config_fingerprint``：对**非敏感**配置做 sha256。

    刻意不含 token：改 token 不该让任何资产"失效"，也不该改变任何报告里的数字。
    """
    payload = json.dumps(
        {
            "source_id": source_id,
            "projection": projection,
            "tile_size": tile_size,
            "layers": sorted(layers),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Catalog:
    """``catalog.sqlite`` 的访问层。

    每次操作开一个连接、用完即关（与分片同一策略）。写操作用 ``BEGIN IMMEDIATE``
    + ``busy_timeout``：**catalog 级"锁"就是 SQLite 自己的写者锁**。

    为什么不像分片那样用文件租约：SQLite 的写者锁本身就是跨进程互斥，再加一层
    文件租约只会引入"两个锁不一致"的新故障模式。这是对 Slice 41 契约 §2.4
    "catalog 级租约"的一处落地调整，已记入 docs/tile_repository_usage.md。

    为什么还要留一个**常驻连接**
    -----------------------------

    WAL 模式下的 ``-wal`` / ``-shm`` 由"最后一个关闭的连接"清理。如果一边有线程
    反复"开连接、查一次、关连接"，一边有线程在写，关闭动作就可能正好落在
    写者已经映射了 shm 之后 —— Windows 上这种情况实测会拿到
    ``sqlite3.OperationalError: attempt to write a readonly database``
    （与"只读权限"毫无关系，纯粹是清理竞态）。

    本机压测（300 轮写 + 并发读churn）：不留常驻连接时 6/300 次失败，
    留一个空闲连接后 0/300。所以这里刻意的留一个不做任何事的连接，
    它唯一的职责就是"别让 shm 在别人手里被删掉"。
    """

    def __init__(self, path: Path, *, create: bool = False) -> None:
        self.path = Path(path)
        self.created = False
        if create:
            self._initialize()
        elif not self.path.is_file():
            raise NotFound(f"仓库 catalog 不存在：{self.path.name}（仓库还没初始化？）")
        self._verify_schema()
        self._keeper = self._open_keeper()

    def close(self) -> None:
        """关掉常驻连接。幂等（``TileRepository.close()`` 会调它）。"""
        keeper = getattr(self, "_keeper", None)
        if keeper is None:
            return
        self._keeper = None
        # 关闭失败不该盖住调用方的异常（收尾路径的常见取舍）
        with contextlib.suppress(sqlite3.Error):
            keeper.close()

    def _open_keeper(self) -> sqlite3.Connection:
        """空闲常驻连接：只用来阻止 ``-wal``/``-shm`` 被"最后一个关闭者"拆掉。"""
        keeper = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=rw",
            uri=True,
            timeout=TILE_REPO_SQLITE_BUSY_TIMEOUT_MS / 1000.0,
            isolation_level=None,
            check_same_thread=False,
        )
        keeper.execute("PRAGMA busy_timeout = 0")
        # 走一次真实读：连接真正打开文件、映射 shm 之后才起"保活"作用
        keeper.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        return keeper

    # -- 连接 ---------------------------------------------------------- #
    @contextmanager
    def connect(self):
        """只读连接（``query_only``）。"""
        connection = self._open()
        try:
            connection.execute("PRAGMA query_only = ON")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        """写事务（``BEGIN IMMEDIATE``）：拿不到写锁就按 ``busy_timeout`` 后报错。"""
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            connection.commit()
        finally:
            connection.close()

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=rw",
            uri=True,
            timeout=TILE_REPO_SQLITE_BUSY_TIMEOUT_MS / 1000.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(TILE_REPO_SQLITE_BUSY_TIMEOUT_MS)}")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    # -- 初始化 -------------------------------------------------------- #
    def _initialize(self) -> None:
        """建 catalog（幂等：已存在且版本正确时什么都不做）。"""
        first_time = not self.path.is_file()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=rwc",
            uri=True,
            timeout=TILE_REPO_SQLITE_BUSY_TIMEOUT_MS / 1000.0,
            isolation_level=None,
        )
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            # catalog 是**权威记账**（操作日志、epoch 水位）。这里用 FULL 而不是
            # NORMAL：多花一次 fsync，换"崩了也知道崩之前批准到哪"。
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(DDL)
            connection.executemany(
                "INSERT OR IGNORE INTO catalog_meta (name, value) VALUES (?, ?)",
                [
                    ("schema_version", str(CATALOG_SCHEMA_VERSION)),
                    ("created_at", str(time.time())),
                    ("repo_id", uuid.uuid4().hex),
                ],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO zoom_bands (z_min, z_max) VALUES (?, ?)",
                list(TILE_REPO_ZOOM_BANDS),
            )
        finally:
            connection.close()
        self.created = first_time

    def _verify_schema(self) -> None:
        meta = self.meta()
        version = meta.get("schema_version")
        if version is None:
            raise Corrupt("catalog 缺少 schema_version（可能是写入中断的库）")
        if version != str(CATALOG_SCHEMA_VERSION):
            raise SchemaUnsupported(
                f"catalog schema_version={version}，本代码支持 {CATALOG_SCHEMA_VERSION}"
            )
        with self.connect() as connection:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        missing = {"sources", "assets", "shards", "epochs", "operations", "coverage"} - names
        if missing:
            raise Corrupt(f"catalog 缺少表：{sorted(missing)}")

    def meta(self) -> dict[str, str]:
        with self.connect() as connection:
            try:
                rows = connection.execute("SELECT name, value FROM catalog_meta").fetchall()
            except sqlite3.DatabaseError:
                return {}
        return {str(row[0]): str(row[1]) for row in rows}

    # -- 注册 ---------------------------------------------------------- #
    def ensure_source(self, spec: SourceSpec) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO sources "
                "(source_id, provider, projection, tile_size, config_fingerprint, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    spec.source_id,
                    spec.provider,
                    spec.projection,
                    spec.tile_size,
                    spec.config_fingerprint,
                    time.time(),
                ),
            )

    def ensure_asset(self, asset: AssetId) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO assets (asset_id, source_id, layer, role, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (asset.text, asset.source_id, asset.layer, asset.role, time.time()),
            )

    def asset_exists(self, asset: AssetId) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM assets WHERE asset_id = ?", (asset.text,)
            ).fetchone()
        return row is not None

    def list_assets(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT asset_id FROM assets ORDER BY asset_id").fetchall()
        return [str(row[0]) for row in rows]

    def zoom_bands(self) -> tuple[tuple[int, int], ...]:
        """catalog 里记录的 band 表（**真值来自库，不是代码常量**）。"""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT z_min, z_max FROM zoom_bands ORDER BY z_min"
            ).fetchall()
        if not rows:
            return tuple(TILE_REPO_ZOOM_BANDS)
        return tuple((int(row[0]), int(row[1])) for row in rows)

    def replace_zoom_bands(self, bands: tuple[tuple[int, int], ...]) -> None:
        """改分片带（需要随之重建索引；Slice 49 的维护动作）。"""
        with self.transaction() as connection:
            connection.execute("DELETE FROM zoom_bands")
            connection.executemany(
                "INSERT INTO zoom_bands (z_min, z_max) VALUES (?, ?)", list(bands)
            )

    # -- epoch --------------------------------------------------------- #
    def create_epoch(self, kind: str, asset_id: str = "", detail: str = "") -> int:
        """追加一个 epoch 并返回它的 id（**只追加，永不修改**）。"""
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO epochs (at, kind, asset_id, detail) VALUES (?, ?, ?, ?)",
                (time.time(), kind, asset_id, detail),
            )
            return int(cursor.lastrowid or 0)

    def max_epoch(self) -> int:
        """仓库当前最高 epoch（全仓水位，快照冻结用它当"逻辑时间"）。"""
        with self.connect() as connection:
            row = connection.execute("SELECT COALESCE(MAX(id), 0) FROM epochs").fetchone()
        return int(row[0])

    def epoch_exists(self, epoch_id: int) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM epochs WHERE id = ?", (int(epoch_id),)
            ).fetchone()
        return row is not None

    def ensure_epoch_floor(self, value: int, detail: str = "") -> None:
        """让 ``MAX(epochs.id)`` 至少到 ``value``（**只追加**，不重写历史）。

        catalog 丢过之后（用户删了 ``catalog.sqlite``，分片还在），重建时从分片里
        读到的是 epoch 12、15 这类数字，而新 catalog 的 ``epochs`` 表是空的 ——
        不补这一行，水位会从 1 重新开始，"冻结水位 E"就会小于分片里的行，
        于是一份全新的快照会莫名其妙地一行都读不出来。
        """
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO epochs (id, at, kind, asset_id, detail) "
                "VALUES (?, ?, 'recover', '', ?)",
                (int(value), time.time(), detail or "重建索引时补齐水位"),
            )

    def epoch_detail(self, epoch_id: int) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, at, kind, asset_id, detail FROM epochs WHERE id = ?",
                (int(epoch_id),),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "at": float(row["at"]),
            "kind": str(row["kind"]),
            "asset_id": str(row["asset_id"]),
            "detail": str(row["detail"]),
        }

    # -- 操作日志 ------------------------------------------------------ #
    def begin_operation(
        self, kind: str, asset_id: str, epoch_id: int, detail: dict | None = None
    ) -> str:
        """写一条 ``pending`` 操作记录并返回 ``op_id``（写前日志）。"""
        op_id = uuid.uuid4().hex
        now = time.time()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO operations "
                "(op_id, kind, asset_id, epoch_id, state, detail, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    op_id,
                    kind,
                    asset_id,
                    int(epoch_id),
                    OperationState.PENDING,
                    json.dumps(detail or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return op_id

    def update_operation(
        self,
        connection: sqlite3.Connection,
        op_id: str,
        state: str,
        detail: dict | None = None,
    ) -> None:
        """在调用方的事务里改操作状态（发布时与 catalog 更新一起提交）。"""
        if state not in OPERATION_STATES:
            raise ValueError(f"未知的操作状态：{state!r}")
        if detail is None:
            connection.execute(
                "UPDATE operations SET state = ?, updated_at = ? WHERE op_id = ?",
                (state, time.time(), op_id),
            )
        else:
            connection.execute(
                "UPDATE operations SET state = ?, detail = ?, updated_at = ? WHERE op_id = ?",
                (state, json.dumps(detail, ensure_ascii=False), time.time(), op_id),
            )

    def set_operation_state(self, op_id: str, state: str, detail: dict | None = None) -> None:
        with self.transaction() as connection:
            self.update_operation(connection, op_id, state, detail)

    def get_operation(self, op_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE op_id = ?", (op_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def unfinished_operations(self) -> list[dict[str, object]]:
        """未完成的操作（健康检查与恢复的输入）。"""
        placeholders = ",".join("?" for _ in OperationState.UNFINISHED)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM operations WHERE state IN ({placeholders}) ORDER BY created_at",
                list(OperationState.UNFINISHED),
            ).fetchall()
        return [dict(row) for row in rows]

    def operations_by_state(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) FROM operations GROUP BY state"
            ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    # -- 分片注册 ------------------------------------------------------ #
    def upsert_shard(
        self,
        connection: sqlite3.Connection,
        asset: AssetId,
        band: tuple[int, int],
        relative_path: str,
        *,
        tile_count: int,
        content_count: int,
        bytes_: int,
        published_epoch: int | None = None,
        checked_at: float | None = None,
    ) -> None:
        """在**调用方的事务里**登记/更新一个分片。

        为什么要接 ``connection``：发布是"统计 + 覆盖率 + 水位 + 操作状态"四件事，
        它们必须在同一个 catalog 事务里提交 —— 否则会出现"水位推了但计数没更新"
        这种一半新一半旧的 catalog，而 catalog 落后是可以靠重建修的、**不一致**
        则会让健康检查永远报警。
        """
        now = time.time()
        connection.execute(
            "INSERT OR IGNORE INTO shards "
            "(asset_id, z_min, z_max, path, tile_count, content_count, bytes, published_epoch) "
            "VALUES (?, ?, ?, ?, 0, 0, 0, 0)",
            (asset.text, int(band[0]), int(band[1]), relative_path),
        )
        connection.execute(
            "UPDATE shards SET path = ?, tile_count = ?, content_count = ?, bytes = ?, "
            "checked_at = ? WHERE asset_id = ? AND z_min = ?",
            (
                relative_path,
                int(tile_count),
                int(content_count),
                int(bytes_),
                now if checked_at is None else float(checked_at),
                asset.text,
                int(band[0]),
            ),
        )
        if published_epoch is not None:
            connection.execute(
                "UPDATE shards SET published_epoch = MAX(published_epoch, ?) "
                "WHERE asset_id = ? AND z_min = ?",
                (int(published_epoch), asset.text, int(band[0])),
            )

    def register_shard(
        self,
        asset: AssetId,
        band: tuple[int, int],
        relative_path: str,
        *,
        tile_count: int = 0,
        content_count: int = 0,
        bytes_: int = 0,
        published_epoch: int | None = None,
        checked_at: float | None = None,
    ) -> None:
        """单条登记（自带事务）；发布路径请用 :meth:`upsert_shard`。"""
        with self.transaction() as connection:
            self.upsert_shard(
                connection,
                asset,
                band,
                relative_path,
                tile_count=tile_count,
                content_count=content_count,
                bytes_=bytes_,
                published_epoch=published_epoch,
                checked_at=checked_at,
            )

    def set_published_epoch(self, asset: AssetId, band: tuple[int, int], epoch_id: int) -> None:
        """把发布水位推到 ``epoch_id``（**只增不减**）。"""
        with self.transaction() as connection:
            connection.execute(
                "UPDATE shards SET published_epoch = MAX(published_epoch, ?) "
                "WHERE asset_id = ? AND z_min = ?",
                (int(epoch_id), asset.text, int(band[0])),
            )

    def published_epoch(self, asset: AssetId, band: tuple[int, int]) -> int:
        """该分片的发布水位；未登记的分片返回 ``0``（= 什么都不可见）。"""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT published_epoch FROM shards WHERE asset_id = ? AND z_min = ?",
                (asset.text, int(band[0])),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def list_shards(self, asset: AssetId | None = None) -> list[ShardRegistration]:
        with self.connect() as connection:
            if asset is None:
                rows = connection.execute(
                    "SELECT * FROM shards ORDER BY asset_id, z_min"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM shards WHERE asset_id = ? ORDER BY z_min", (asset.text,)
                ).fetchall()
        return [
            ShardRegistration(
                asset_id=str(row["asset_id"]),
                z_min=int(row["z_min"]),
                z_max=int(row["z_max"]),
                path=str(row["path"]),
                tile_count=int(row["tile_count"]),
                content_count=int(row["content_count"]),
                bytes=int(row["bytes"]),
                published_epoch=int(row["published_epoch"]),
                checked_at=None if row["checked_at"] is None else float(row["checked_at"]),
            )
            for row in rows
        ]

    def shard_registration(self, asset: AssetId, band: tuple[int, int]) -> ShardRegistration | None:
        for item in self.list_shards(asset):
            if item.z_min == band[0]:
                return item
        return None

    def drop_shard(self, asset_id: str, z_min: int) -> None:
        """把分片从 catalog 摘除（**不删文件**）。"""
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM shards WHERE asset_id = ? AND z_min = ?", (asset_id, int(z_min))
            )

    # -- 覆盖矩形 ------------------------------------------------------ #
    def expand_coverage(
        self,
        connection: sqlite3.Connection,
        asset: AssetId,
        zoom: int,
        x_min: int,
        x_max: int,
        y_min: int,
        y_max: int,
    ) -> None:
        """把某级的覆盖矩形往外扩（单调扩，只在发布事务里调用）。

        覆盖矩形**不是"齐全"的证据**（契约 §2.1）：它只说"这些列/行出现过瓦片"，
        导出期的齐全性判定必须回到分片逐坐标核对。为了不让下游误用，读取接口
        只叫 :meth:`coverage_rects`，返回的是**外包矩形**。
        """
        row = connection.execute(
            "SELECT x_min, x_max, y_min, y_max FROM coverage WHERE asset_id = ? AND z = ?",
            (asset.text, int(zoom)),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO coverage (asset_id, z, x_min, x_max, y_min, y_max) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (asset.text, int(zoom), int(x_min), int(x_max), int(y_min), int(y_max)),
            )
            return
        connection.execute(
            "UPDATE coverage SET x_min = MIN(x_min, ?), x_max = MAX(x_max, ?), "
            "y_min = MIN(y_min, ?), y_max = MAX(y_max, ?) WHERE asset_id = ? AND z = ?",
            (int(x_min), int(x_max), int(y_min), int(y_max), asset.text, int(zoom)),
        )

    def replace_coverage(
        self,
        connection: sqlite3.Connection,
        asset: AssetId,
        zoom: int,
        x_min: int,
        x_max: int,
        y_min: int,
        y_max: int,
    ) -> None:
        """按分片实际内容**重算**某级的覆盖矩形（重建索引用）。"""
        connection.execute(
            "INSERT OR REPLACE INTO coverage (asset_id, z, x_min, x_max, y_min, y_max) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (asset.text, int(zoom), int(x_min), int(x_max), int(y_min), int(y_max)),
        )

    def delete_coverage_zoom(self, connection: sqlite3.Connection, asset: AssetId, zoom: int) -> None:
        connection.execute(
            "DELETE FROM coverage WHERE asset_id = ? AND z = ?", (asset.text, int(zoom))
        )

    def coverage_rects(self, asset: AssetId) -> dict[int, tuple[int, int, int, int]]:
        """``{z: (x_min, x_max, y_min, y_max)}``（外包矩形，不代表齐全）。"""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT z, x_min, x_max, y_min, y_max FROM coverage WHERE asset_id = ? ORDER BY z",
                (asset.text,),
            ).fetchall()
        return {
            int(row[0]): (int(row[1]), int(row[2]), int(row[3]), int(row[4])) for row in rows
        }

    # -- 快照 ---------------------------------------------------------- #
    def register_snapshot(
        self,
        fingerprint: str,
        epoch_id: int,
        relative_path: str,
        entries: int,
        detail: dict | None = None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO snapshots "
                "(fingerprint, created_at, epoch_id, path, entries, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    fingerprint,
                    time.time(),
                    int(epoch_id),
                    relative_path,
                    int(entries),
                    json.dumps(detail or {}, ensure_ascii=False),
                ),
            )

    def list_snapshots(self) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM snapshots ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_snapshot(self, fingerprint: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM snapshots WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return dict(row) if row is not None else None
