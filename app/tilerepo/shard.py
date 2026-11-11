"""分片：目录规则、DDL、连接与写租约（Slice 42）。

分片规则（架构 §6.2 的落地）
----------------------------

**按 ``(图层, zoom 带)`` 切分**，最多 16 个文件，路由是纯函数::

    shards/<source_id>/<projection>/<tile_size>/<layer>/z<min:02d>-<max:02d>.sqlite
    shards/tianditu/EPSG_3857/256/vec_w/z00-06.sqlite

被否掉的三个方案（原因写在架构 §6.2）：单库分区（不可 VACUUM）、一级一库（76 个
文件）、哈希分桶（导出要开全部桶）。

两个刻意的设计点
----------------

1. **分片路径一律相对仓库根**。catalog 里存 ``shards/...`` 而不是绝对路径：
   仓库可以整个搬走（用户的目录名不该被写进数据库，也不该出现在报告里），
   而"防路径穿越"只需在拼接时校验一次。
2. **``contents`` 是普通 rowid 表，``coords`` 才是 ``WITHOUT ROWID``**。
   大 BLOB 放进 ``WITHOUT ROWID`` 的表里等于塞进主键 B 树，溢出页会把索引撑爆
   （架构 §6.3 的理由）；坐标行的字段都小，``WITHOUT ROWID`` 反而让点查少一跳。

写租约
------

同分片**单写者**，靠一个文件租约实现::

    lock/<source>-<layer>-z<min>-<max>.lock   内容 = {token, pid, host, label, ...}

为什么不依赖 ``database is locked``：那个错误要等到真的撞上才知道，且分不清
"别人在写"与"库坏了"；文件租约能在**动手之前**给出 ``Busy``，还带着持有者信息。
为什么不用操作系统的文件锁（``msvcrt.locking`` / ``fcntl``）：项目现有代码零依赖
这两者，而"文件存在 + 心跳 mtime"在 Windows 上的行为可预测得多（能被删除、
能被接管、不持有句柄）。

崩溃接管：心跳（文件 mtime）超过 :data:`TILE_REPO_LEASE_TIMEOUT_SECONDS` 即视为
持有者已死。接管走一个二次互斥（``*.takeover``），避免两个进程同时"接管成功"。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.config.settings import (
    TILE_REPO_LEASE_HEARTBEAT_SECONDS,
    TILE_REPO_LEASE_TIMEOUT_SECONDS,
    TILE_REPO_SCHEMA_VERSION,
    TILE_REPO_SQLITE_BUSY_TIMEOUT_MS,
    TILE_REPO_ZOOM_BANDS,
)
from app.tilerepo.errors import Busy, Corrupt, SchemaUnsupported
from app.tilerepo.identity import AssetId

__all__ = [
    "DDL",
    "REQUIRED_META_KEYS",
    "SHARD_SUFFIX",
    "Lease",
    "LeaseInfo",
    "acquire_lease",
    "band_for_zoom",
    "band_name",
    "check_shard_schema",
    "lease_holder_alive",
    "list_leases",
    "open_shard",
    "raise_if_escapes",
    "read_shard_meta",
    "release_all_leases",
    "shard_lock_name",
    "shard_path",
    "shard_paths_under",
    "shard_relative_path",
    "shard_write_lock",
    "table_names",
    "write_shard_meta",
]

#: 分片文件扩展名。刻意**不用** ``.mbtiles``：仓库格式不是交付格式，
#: 用同一个扩展名会诱导别人拿 MBTiles 工具去打开它（然后困惑为什么没有 ``tiles`` 表）。
SHARD_SUFFIX: str = ".sqlite"

#: ``shard_meta`` 必须存在的键。缺任意一个都说明"这个分片不是本代码写的"
#: 或者是写到一半崩了 —— 两种情况都必须拒绝服务（架构 §7.3）。
REQUIRED_META_KEYS: tuple[str, ...] = (
    "schema_version",
    "asset_id",
    "source_id",
    "layer",
    "projection",
    "tile_size",
    "z_min",
    "z_max",
)

_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# --------------------------------------------------------------------------- #
# 分片 DDL
# --------------------------------------------------------------------------- #
#: 每个分片文件独立建这套表。
#:
#: ``coords`` 的 ``y`` 是 **XYZ**（不是 TMS）；``state`` 的数值是持久化契约
#: （见 :class:`app.tilerepo.identity.CoordState`）。
#: 外键 ``coords.sha256 -> contents.sha256`` 不是装饰：它在 SQLite 层面保证
#: "不可能出现指向不存在内容的坐标行"，是"不产生指向未提交内容的可用索引"的
#: 结构性保障（写入顺序因此固定为 先 contents 后 coords）。
DDL: str = """
CREATE TABLE IF NOT EXISTS shard_meta (
  name  TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS contents (
  sha256     BLOB    PRIMARY KEY,
  size       INTEGER NOT NULL,
  encoding   TEXT    NOT NULL,
  first_seen REAL    NOT NULL,
  data       BLOB    NOT NULL
);

CREATE TABLE IF NOT EXISTS coords (
  z          INTEGER NOT NULL,
  x          INTEGER NOT NULL,
  y          INTEGER NOT NULL,
  sha256     BLOB    NOT NULL,
  state      INTEGER NOT NULL,
  fetched_at REAL    NOT NULL,
  epoch_id   INTEGER NOT NULL,
  PRIMARY KEY (z, x, y, state),
  FOREIGN KEY (sha256) REFERENCES contents (sha256)
) WITHOUT ROWID;
"""


# --------------------------------------------------------------------------- #
# zoom 带与路径
# --------------------------------------------------------------------------- #
def band_for_zoom(
    zoom: int, bands: tuple[tuple[int, int], ...] = TILE_REPO_ZOOM_BANDS
) -> tuple[int, int]:
    """这个 zoom 属于哪个带。

    Raises:
        ValueError: 没有任何带覆盖该 zoom（配置被改坏了，不能静默兜底）。

    >>> band_for_zoom(6)
    (0, 6)
    >>> band_for_zoom(7)
    (7, 11)
    """
    if type(zoom) is not int:
        raise ValueError(f"zoom 必须是整数，实际类型为 {type(zoom).__name__}")
    for z_min, z_max in bands:
        if z_min <= zoom <= z_max:
            return (z_min, z_max)
    raise ValueError(f"zoom {zoom} 不在任何分片带内；当前带为 {list(bands)}")


def band_name(band: tuple[int, int]) -> str:
    """``(0, 6)`` → ``'z00-06'``（文件名片段）。

    >>> band_name((7, 11))
    'z07-11'
    """
    z_min, z_max = band
    return f"z{z_min:02d}-{z_max:02d}"


def _slug(value: str, *, what: str) -> str:
    """把一段标识变成安全的路径片段；不安全就拒绝，不做"清洗后凑合用"。"""
    text = str(value).strip()
    if not text or not _SLUG_RE.match(text):
        raise ValueError(f"{what} 不能作为目录名：{value!r}（只允许字母数字与 . _ -）")
    if text in {".", ".."}:
        raise ValueError(f"{what} 不能是 {text!r}")
    return text


def shard_relative_path(
    asset: AssetId,
    band: tuple[int, int],
    *,
    projection: str = "EPSG:3857",
    tile_size: int = 256,
) -> PurePosixPath:
    """分片在仓库内的**相对**路径（catalog 里存的就是它）。

    >>> shard_relative_path(AssetId('tianditu', 'vec_w'), (0, 6)).as_posix()
    'shards/tianditu/EPSG_3857/256/vec_w/z00-06.sqlite'
    """
    source = _slug(asset.source_id, what="source_id")
    layer = _slug(asset.layer, what="layer")
    projection_slug = _slug(str(projection).replace(":", "_"), what="projection")
    if type(tile_size) is not int or tile_size <= 0:
        raise ValueError(f"tile_size 必须是正整数，实际为 {tile_size!r}")
    return PurePosixPath(
        "shards", source, projection_slug, str(tile_size), layer, f"{band_name(band)}{SHARD_SUFFIX}"
    )


def raise_if_escapes(root: Path, target: Path) -> Path:
    """确保 ``target`` 落在 ``root`` 内（防路径穿越）。

    这条检查针对的是**从数据库读回来的路径**：catalog 里的 ``path`` 是相对路径，
    但仓库文件是可以被手工编辑的 —— 一个 ``../../Windows`` 不该让健康检查去
    读别人的文件。

    Raises:
        Corrupt: 越界（这是数据损坏，不是调用方的编程错误）。
    """
    root_resolved = Path(root).resolve()
    target_resolved = Path(target).resolve()
    if target_resolved != root_resolved and root_resolved not in target_resolved.parents:
        raise Corrupt(f"分片路径越出仓库根：{target}（根为 {root}）")
    return target_resolved


def shard_path(
    root: Path,
    asset: AssetId,
    band: tuple[int, int],
    *,
    projection: str = "EPSG:3857",
    tile_size: int = 256,
) -> Path:
    """分片的绝对路径（已做越界校验）。"""
    relative = shard_relative_path(
        asset, band, projection=projection, tile_size=tile_size
    )
    return raise_if_escapes(root, Path(root).joinpath(*relative.parts))


def shard_paths_under(root: Path) -> list[Path]:
    """仓库里现有的全部分片文件（用于"catalog 丢了也能重建"）。

    只认 ``shards/**/*.sqlite``，不认 ``staging/`` 与 ``snapshots/`` —— 后两者
    不是事实来源。
    """
    base = Path(root) / "shards"
    if not base.is_dir():
        return []
    return sorted(path for path in base.rglob(f"*{SHARD_SUFFIX}") if path.is_file())


# --------------------------------------------------------------------------- #
# 连接与 schema
# --------------------------------------------------------------------------- #
def open_shard(
    path: Path,
    *,
    create: bool = False,
    read_only: bool = False,
    busy_timeout_ms: int = TILE_REPO_SQLITE_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """打开一个分片连接。

    连接**不跨线程共享**（``check_same_thread`` 保持默认的 True），每次操作开一个、
    用完即关：SQLite 的连接建立成本在本项目的量级下可以忽略，而"连接被别的线程
    拿着"是多线程 bug 的经典来源。

    只读路径用 ``mode=rw`` + ``PRAGMA query_only=ON``，**不是** ``mode=ro``：
    WAL 模式的库用纯只读打开时，SQLite 需要写 ``-shm``，在只读目录/缺 ``-shm``
    时会直接 ``unable to open database file``。``query_only`` 拿到的语义相同
    （任何写语句立即报错），但不需要对目录有写权限。这一条与
    ``app/planning/storage_budget.py`` 里读旧库的写法一致。

    ``isolation_level=None`` 让连接处于**自动提交**模式，事务边界由调用方用
    ``BEGIN IMMEDIATE`` / ``COMMIT`` 显式划定。不这么设的话，Python 的 sqlite3
    会在第一条 DML 前偷偷开一个事务，随后那句 ``BEGIN IMMEDIATE`` 就会
    ``cannot start a transaction within a transaction`` —— 而"到底谁开了事务"
    在堆栈里完全看不出来。

    Raises:
        Corrupt: 文件不存在（且 ``create=False``），或打开失败。
    """
    target = Path(path)
    if not create and not target.is_file():
        raise Corrupt(f"分片文件不存在：{target.name}")
    if create:
        target.parent.mkdir(parents=True, exist_ok=True)
        uri = f"file:{target.as_posix()}?mode=rwc"
    else:
        uri = f"file:{target.as_posix()}?mode=rw"
    connection = sqlite3.connect(
        uri, uri=True, timeout=busy_timeout_ms / 1000.0, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        if read_only:
            connection.execute("PRAGMA query_only = ON")
        else:
            # WAL：读不阻塞写；synchronous=NORMAL 表示"进程崩溃不丢已提交数据，
            # 但硬件掉电可能丢最近几个事务"。这是刻意的取舍，别写成"绝对安全"
            # —— 见 docs/tile_repository_usage.md「持久性承诺」。
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA foreign_keys = ON")
    except sqlite3.DatabaseError as exc:
        connection.close()
        raise Corrupt(f"打开分片失败：{target.name}（{exc}）") from exc
    return connection


def table_names(connection: sqlite3.Connection) -> list[str]:
    """库里现有的表/视图名（诊断用）。"""
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def read_shard_meta(connection: sqlite3.Connection) -> dict[str, str]:
    """读 ``shard_meta``；表不存在时返回空字典（用于判定"这不是本代码写的库"）。"""
    try:
        rows = connection.execute("SELECT name, value FROM shard_meta").fetchall()
    except sqlite3.DatabaseError:
        return {}
    return {str(name): str(value) for name, value in rows}


def write_shard_meta(connection: sqlite3.Connection, values: dict[str, object]) -> None:
    """写 ``shard_meta``（在**创建分片的那一个事务**里调用）。"""
    connection.executemany(
        "INSERT OR REPLACE INTO shard_meta (name, value) VALUES (?, ?)",
        [(str(key), str(value)) for key, value in values.items()],
    )


def check_shard_schema(
    connection: sqlite3.Connection,
    *,
    expected_version: int = TILE_REPO_SCHEMA_VERSION,
    asset: AssetId | None = None,
    band: tuple[int, int] | None = None,
) -> dict[str, str]:
    """确认分片的 schema 与自述信息可用。

    Raises:
        Corrupt: 缺表、缺 meta 键、自述的 asset/band 与文件名不符。
        SchemaUnsupported: schema 版本不认识（比代码新 / 没写）。
    """
    meta = read_shard_meta(connection)
    if not meta:
        raise Corrupt("分片缺少 shard_meta 表或表为空（可能是写到一半的库）")
    missing = [key for key in REQUIRED_META_KEYS if key not in meta]
    if missing:
        raise Corrupt(f"分片 shard_meta 缺少字段：{missing}")
    version = meta.get("schema_version", "")
    if version != str(expected_version):
        raise SchemaUnsupported(
            f"分片 schema_version={version!r}，本代码支持 {expected_version}"
        )
    if asset is not None and meta.get("asset_id") != asset.text:
        raise Corrupt(
            f"分片自述 asset_id={meta.get('asset_id')!r} 与路径推定的 {asset.text!r} 不符"
        )
    if band is not None and (int(meta.get("z_min", -1)), int(meta.get("z_max", -1))) != band:
        raise Corrupt(
            f"分片自述 zoom 带 (z{meta.get('z_min')}-{meta.get('z_max')}) 与路径推定的 {band} 不符"
        )
    return meta


# --------------------------------------------------------------------------- #
# 写租约
# --------------------------------------------------------------------------- #
#: 租约文件的键集合（写进去的就是这些，``token`` 用于"只删自己的锁"）。
_LEASE_KEYS: tuple[str, ...] = ("token", "pid", "host", "label", "created_at")

_LEASE_ENV_LOCK = threading.RLock()


class _HeldEntry:
    """进程内的一把租约 + 重入计数。"""

    __slots__ = ("count", "lease")

    def __init__(self, lease: Lease, count: int = 1) -> None:
        self.lease = lease
        self.count = count


#: 进程内已持有的租约：``path -> _HeldEntry``。
#:
#: 为什么要 refcount：同一进程里"外层导入 → 内层逐批写入"会两次申请同一把锁。
#: 直接抛 ``Busy`` 会让调用方被迫在两层之间传租约对象（接口变丑且容易漏）；
#: 允许重入、按次数递减释放，语义更接近 ``RLock``。
_HELD: dict[Path, _HeldEntry] = {}


def _pid_alive(pid: int) -> bool | None:
    """判断本机某个 pid 是否还活着。``None`` = 判断不了。

    Windows 上**绝不能**用 ``os.kill(pid, 0)`` 判活：CPython 在 Windows 把
    "非 Ctrl-C 信号"映射成 ``TerminateProcess(handle, sig)``，于是 ``sig=0``
    会真的把那个进程以退出码 0 结束掉 —— 一个"只读检查"变成"杀掉别人的写入者"，
    这是本模块里最危险的一行，所以走 ``OpenProcess`` + ``GetExitCodeProcess``。
    """
    if pid <= 0:
        return None
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None
        return True
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        if ctypes.get_last_error() == error_invalid_parameter:
            return False
        return None
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return int(code.value) == still_active
    finally:
        kernel32.CloseHandle(handle)


def lease_holder_alive(info: LeaseInfo) -> bool | None:
    """租约持有者是否还活着（给健康检查用；语义同 :meth:`LeaseInfo.holder_alive`）。"""
    return info.holder_alive()


@dataclass(frozen=True)
class LeaseInfo:
    """租约文件的内容（不含任何敏感信息）。"""

    token: str
    pid: int
    host: str
    label: str
    created_at: float
    heartbeat_at: float

    def expired(self, timeout: float, *, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        return (moment - self.heartbeat_at) > timeout

    def holder_alive(self) -> bool | None:
        """持有者进程是否还活着。``None`` = 判断不了（跨主机 / 无权限）。

        为什么除了"心跳超时"还要看这个：被强杀（``os._exit``/任务管理器结束进程）
        的写者不会删自己的租约文件，心跳也就停在那一刻。只等超时的话，用户重启
        程序后还要白等一整个 ``lease_timeout`` 才能继续往同一个分片写 —— 而那个
        锁的主人其实早就死了。判活能把这个等待降到 0。

        代价是 pid 可能被复用：那样只会"误判成活着"，退化成等超时，
        方向是安全的（宁可多等，不会误抢活着的写者）。
        """
        if self.pid <= 0 or self.host != socket.gethostname():
            return None
        return _pid_alive(self.pid)

    def as_dict(self) -> dict[str, object]:
        return {
            "token": self.token,
            "pid": self.pid,
            "host": self.host,
            "label": self.label,
            "created_at": self.created_at,
            "heartbeat_at": self.heartbeat_at,
        }

    def as_public_dict(self) -> dict[str, object]:
        """给 ``Busy`` 携带的形态：不带 ``token``（事务性令牌不该进日志）。"""
        return {
            "pid": self.pid,
            "host": self.host,
            "label": self.label,
            "age_seconds": round(time.time() - self.heartbeat_at, 1),
        }


class Lease:
    """一把已持有的写租约。

    用法::

        lease = acquire_lease(lock_dir, 'tianditu-vec_w-z00-06', timeout=90)
        try:
            ...写入...
        finally:
            lease.release()

    或直接 ``with``（``__exit__`` 调 :meth:`release`）。
    """

    def __init__(self, path: Path, info: LeaseInfo) -> None:
        self.path = Path(path)
        self.info = info
        self._released = False
        self._last_heartbeat = time.time()

    # -- 心跳 ---------------------------------------------------------- #
    def heartbeat(self, *, force: bool = False) -> None:
        """刷新心跳（文件 mtime）。默认按 :data:`TILE_REPO_LEASE_HEARTBEAT_SECONDS` 节流。

        节流的意义：每写一张瓦片都 ``utime`` 一次会把 IO 放大到无意义的程度。
        """
        if self._released:
            return
        now = time.time()
        if not force and (now - self._last_heartbeat) < TILE_REPO_LEASE_HEARTBEAT_SECONDS:
            return
        # 租约文件被别人删掉（或目录被清理）不该让正在进行的写入崩掉：
        # 数据完整性由 SQLite 事务保证，租约只负责"劝退并发写者"。
        with contextlib.suppress(OSError):
            os.utime(self.path, None)
        self._last_heartbeat = now

    # -- 释放 ---------------------------------------------------------- #
    def release(self) -> None:
        """释放（按 refcount 递减；归零时才真的删文件）。幂等。"""
        if self._released:
            return
        with _LEASE_ENV_LOCK:
            entry = _HELD.get(self.path)
            if entry is None:
                self._released = True
                return
            entry.count -= 1
            if entry.count > 0:
                return
            _HELD.pop(self.path, None)
        self._delete_if_ours()
        self._released = True

    def _delete_if_ours(self) -> None:
        current = read_lease(self.path)
        if current is not None and current.token != self.info.token:
            return  # 已经被别人接管：别删别人的锁
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def __enter__(self) -> Lease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    @property
    def released(self) -> bool:
        return self._released


def read_lease(path: Path) -> LeaseInfo | None:
    """读租约文件；不存在或内容坏掉返回 ``None``。

    心跳取**文件 mtime**而不是文件里的字段：mtime 由 :meth:`Lease.heartbeat`
    直接刷新，不需要重写内容（重写内容会带来"读到半个 JSON"的窗口）。
    """
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
        stat = target.stat()
    except (OSError, ValueError):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    token = str(payload.get("token") or "")
    if not token:
        return None
    return LeaseInfo(
        token=token,
        pid=int(payload.get("pid") or 0),
        host=str(payload.get("host") or ""),
        label=str(payload.get("label") or ""),
        created_at=float(payload.get("created_at") or stat.st_mtime),
        heartbeat_at=stat.st_mtime,
    )


def list_leases(lock_dir: Path) -> list[LeaseInfo]:
    """全部租约（健康检查用：报告谁还拿着锁、拿了多久）。"""
    base = Path(lock_dir)
    if not base.is_dir():
        return []
    found: list[LeaseInfo] = []
    for path in sorted(base.glob("*.lock")):
        info = read_lease(path)
        if info is not None:
            found.append(info)
    return found


def _lease_payload(label: str) -> dict[str, object]:
    return {
        "token": uuid.uuid4().hex,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "label": label,
        "created_at": time.time(),
    }


def acquire_lease(
    lock_dir: Path,
    name: str,
    *,
    timeout: float = TILE_REPO_LEASE_TIMEOUT_SECONDS,
    label: str = "",
) -> Lease:
    """申请一把写租约；拿不到抛 :class:`Busy`（**不排队、不阻塞**）。

    Args:
        lock_dir: 仓库的 ``lock/`` 目录（不存在会自动创建）。
        name: 锁名（``<source>-<layer>-<band>``，由 :func:`shard_lock_name` 生成）。
        timeout: 多久没心跳视为持有者已死。
        label: 人读的用途说明（进 ``Busy`` 提示，便于排查"谁占着"）。

    Raises:
        Busy: 有人持有且未过期。携带 ``holder``（pid/host/label/aged）。
    """
    base = Path(lock_dir)
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{_slug(name, what='租约名')}.lock"

    with _LEASE_ENV_LOCK:
        entry = _HELD.get(path)
        if entry is not None:
            entry.count += 1
            return entry.lease

        info = _create_lease_file(path, label)
        if info is None:
            existing = read_lease(path)
            if existing is not None and (
                existing.expired(timeout) or existing.holder_alive() is False
            ):
                info = _take_over(path, label, timeout)
            if info is None:
                holder = existing.as_public_dict() if existing is not None else {}
                raise Busy(
                    "分片写租约被占用（另一个写入者正在写这个分片）",
                    lock=path.name,
                    holder=holder,
                )
        lease = Lease(path, info)
        _HELD[path] = _HeldEntry(lease)
        return lease


def _create_lease_file(path: Path, label: str) -> LeaseInfo | None:
    payload = _lease_payload(label)
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    except OSError as exc:  # pragma: no cover - 目录权限问题的兜底
        raise Busy(f"无法创建租约文件：{path.name}（{exc}）") from exc
    try:
        os.write(handle, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    finally:
        os.close(handle)
    return read_lease(path)


def _take_over(path: Path, label: str, timeout: float) -> LeaseInfo | None:
    """接管一个过期租约。

    走 ``*.takeover`` 二次互斥：两个进程同时判定"旧锁过期"时，只有一个能进这段
    代码。没有它，两个进程会各写一次租约文件、各自读到自己的 token，**都以为
    自己拿到了锁** —— 这正是"多写者悄悄写坏同一个分片"的经典成因。
    """
    guard = path.with_name(path.name + ".takeover")
    try:
        handle = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # 别人正在接管；把明显陈旧的接管标记清掉，避免它永久挡住接管。
        try:
            if (time.time() - guard.stat().st_mtime) > timeout:
                guard.unlink()
        except OSError:
            pass
        return None
    except OSError:
        return None
    try:
        current = read_lease(path)
        if current is not None and not (
            current.expired(timeout) or current.holder_alive() is False
        ):
            return None
        payload = _lease_payload(label)
        temp = path.with_name(f"{path.name}.{payload['token']}.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
        return read_lease(path)
    finally:
        os.close(handle)
        with contextlib.suppress(OSError):
            guard.unlink()


def release_all_leases() -> int:
    """释放本进程持有的全部租约（``TileRepository.close()`` 收尾用）。

    Returns:
        实际释放的**不同**锁的数量（不是 refcount 总和）。
    """
    with _LEASE_ENV_LOCK:
        leases = [entry.lease for entry in _HELD.values()]
        _HELD.clear()
    for lease in leases:
        if not lease.released:
            lease._delete_if_ours()  # noqa: SLF001 - 同模块内的收尾路径
            lease._released = True  # noqa: SLF001
    return len(leases)


def shard_lock_name(asset: AssetId, band: tuple[int, int]) -> str:
    """租约文件名（不含扩展名）: ``tianditu-vec_w-z00-06``。"""
    return f"{asset.source_id}-{asset.layer}-{band_name(band)}"


#: 进程内"同一分片写锁"的注册表（``path -> RLock``）。
_SHARD_WRITE_LOCKS: dict[str, threading.RLock] = {}
_SHARD_WRITE_LOCKS_GUARD = threading.Lock()


def shard_write_lock(path: Path) -> threading.RLock:
    """同一分片在**进程内**的写锁。

    为什么文件租约还不够：租约是进程级的（本进程内重入持有同一个 ``Lease``），
    所以两个线程可以同时往同一个分片写。跨进程由文件租约 + SQLite 写者锁兜住，
    进程内则用这把 ``RLock`` 串行化 —— 实测两个线程同时做
    ``PRAGMA journal_mode=WAL`` / ``BEGIN IMMEDIATE`` 时，Windows 上会拿到
    ``attempt to write a readonly database``（与权限无关的连接级竞态）。

    用 ``resolve()`` 后的绝对路径做键：同一个分片经不同相对路径拼进来时，
    键必须是同一个，否则"两把锁"等于没锁。
    """
    key = Path(path).resolve().as_posix()
    with _SHARD_WRITE_LOCKS_GUARD:
        lock = _SHARD_WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _SHARD_WRITE_LOCKS[key] = lock
        return lock


#: 租约默认值（再导出一遍，方便调用方不 import settings）。
DEFAULT_LEASE_TIMEOUT: float = TILE_REPO_LEASE_TIMEOUT_SECONDS
