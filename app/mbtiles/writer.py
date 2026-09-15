"""MBTiles 写入器与"单 Writer"异步写入管线（Slice 4）。

两条链路，刻意分开
------------------

**同步部分**（:class:`MBTilesWriter`）：只管"给定 z/x/y_xyz + 字节，写进 SQLite"。
所有 SQL、事务、PRAGMA、连接生命周期都收敛在这里，别的模块不碰 ``sqlite3``。

**异步部分**（:class:`MBTilesTileSink`）：把 Slice 3 的多 worker 下载结果，
收拢成**单消费者**写入。

为什么要单消费者
----------------

Slice 3 的 :class:`~app.downloader.DownloadManager` 会并发起 6 个 HTTP worker。
如果每个 worker 直接写库：

- SQLite 连接不是线程/协程安全的，并发写同一个连接会直接 ``ProgrammingError``；
- 就算每个 worker 各开一个连接，多写者会互相抢写锁，反而更慢，
  而且 WAL 下写锁竞争会让下载端莫名长时间停顿。

所以拓扑固定为::

    HTTP Worker 1 ─┐
    HTTP Worker 2 ─┤
    HTTP Worker 3 ─┼→ 有界 asyncio.Queue → 单消费者协程 → MBTilesWriter → xxx.mbtiles
    HTTP Worker 4 ─┤
    HTTP Worker 5 ─┤
    HTTP Worker 6 ─┘

队列**必须有界**：队列元素持有瓦片字节（每张几十 KB），无界队列意味着
"下载多快就堆多少在内存里"，直接违背"边下载边写盘"。有界队列会让
``await queue.put(...)`` 反压回 HTTP worker —— 写盘慢时下载自然降速，
这是本设计里唯一的限流点，也是最省事的那个。

解耦边界
--------

``DownloadManager`` **不知道** MBTiles 的存在：它只认一个 ``on_tile`` 回调。
本模块的 ``MBTilesTileSink.consume`` 就是那个回调。
反向依赖（本模块 import ``app.downloader``）只为拿到
:class:`~app.downloader.TileDownloadResult` 类型，不调用引擎的任何逻辑。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import suppress
from pathlib import Path

from app.config.settings import (
    DEFAULT_MBTILES_BATCH_SIZE,
    DEFAULT_MBTILES_QUEUE_CAPACITY,
    MAX_MBTILES_QUEUE_CAPACITY,
    MIN_MBTILES_QUEUE_CAPACITY,
)
from app.downloader import TileDownloadResult
from app.geo import validate_tile_axis, validate_zoom, xyz_y_to_tms_y
from app.mbtiles.models import MBTilesMetadata

__all__ = ["MBTilesWriteError", "MBTilesWriter", "MBTilesTileSink"]


_logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# SQL（本模块持有全部**写** SQL；读 SQL 在 reader.py）
# --------------------------------------------------------------------------- #
#: 标准 MBTiles ``metadata`` 表：name/value 两列的键值表，name 为主键。
_CREATE_METADATA_SQL = """
CREATE TABLE IF NOT EXISTS metadata (
    name TEXT PRIMARY KEY,
    value TEXT
)
"""

#: 标准 MBTiles ``tiles`` 表。``tile_row`` 是 **TMS** 行号（自南向北）。
_CREATE_TILES_SQL = """
CREATE TABLE IF NOT EXISTS tiles (
    zoom_level INTEGER NOT NULL,
    tile_column INTEGER NOT NULL,
    tile_row INTEGER NOT NULL,
    tile_data BLOB NOT NULL,
    PRIMARY KEY (
        zoom_level,
        tile_column,
        tile_row
    )
)
"""

#: 用 ``INSERT OR REPLACE`` 而不是裸 ``INSERT``：
#: 重复瓦片（同一 z/x/y 被写了两次）不应该让整个任务失败——上传失败重下、
#: 用户重跑同一区域都是正常操作。语义是"后写覆盖先写"，与 tilelive 等
#: 主流 MBTiles 工具一致。
_INSERT_TILE_SQL = (
    "INSERT OR REPLACE INTO tiles "
    "(zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)"
)

_INSERT_METADATA_SQL = "INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)"

#: SQLite 性能设置。三者的取舍：
#:
#: - ``journal_mode=WAL``：写不阻塞读，且批量写更快。代价是会产生 ``-wal``
#:   旁挂文件，因此 :meth:`MBTilesWriter.close` 里必须 checkpoint 回主库，
#:   保证交付出去的 ``xxx.mbtiles`` 单文件自洽。
#: - ``synchronous=NORMAL``：WAL 下的推荐值——只在 checkpoint 时 fsync，
#:   断电最多丢最后一个事务，不会损坏数据库文件。**不**用 ``OFF``。
#: - ``temp_store=MEMORY``：避免临时表写盘。
_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA temp_store=MEMORY",
)

#: 把 WAL 内容合并回主库文件。交付单文件前必须做，否则 ``.mbtiles`` 可能
#: 只有表结构、数据还躺在 ``-wal`` 里。
_CHECKPOINT_SQL = "PRAGMA wal_checkpoint(TRUNCATE)"

#: 收尾时退出 WAL 模式，切回 rollback journal。
#:
#: 为什么必须做（实测结论）：只要库头写着 WAL 模式，**后续任何一次只读打开**
#: 都会重新创建 ``-wal`` / ``-shm`` 两个旁挂文件，而只读连接没有权限删除它们，
#: 于是"校验/读取"一遍之后，用户的输出目录里就多了 ``beijing.mbtiles-wal`` 和
#: ``beijing.mbtiles-shm`` 两个陌生文件。
#:
#: WAL 只在**写入阶段**有价值（写不阻塞读、批量写更快）；收尾时切回 DELETE
#: 后，交付出去的就是一个自洽的单文件，读它也不会再产生任何旁挂文件。
#: 数据已经在 checkpoint 时落进主文件，切换不丢任何东西。
_SET_JOURNAL_DELETE_SQL = "PRAGMA journal_mode=DELETE"

# 有意**不**额外建索引：``tiles`` 的主键
# ``(zoom_level, tile_column, tile_row)`` 已经隐式建成唯一索引，
# 按 ``zoom_level`` 前缀查询可以直接走它。再单独建
# ``INDEX ON tiles(zoom_level)`` 只会白占空间、拖慢写入。


class MBTilesWriteError(RuntimeError):
    """MBTiles 写入失败（磁盘满、库损坏、非法瓦片等）。

    单独定一个异常类型，是为了让调用方能明确区分
    "下载失败（单张瓦片）"与"写盘失败（整个任务不可继续）"这两件事。
    """


# --------------------------------------------------------------------------- #
# 同步写入器
# --------------------------------------------------------------------------- #
class MBTilesWriter:
    """把瓦片写进 MBTiles 单文件。

    用法::

        with MBTilesWriter(path, batch_size=200) as writer:
            writer.write_metadata(metadata)
            writer.write_tile(12, 3367, 1563, png_bytes)

    坐标：入参 ``y_xyz`` 是 **XYZ**（自北向南），落库时自动翻转成
    **TMS** ``tile_row`` —— 换算复用 :func:`app.geo.xyz_y_to_tms_y`，
    本模块不重复实现该公式。

    事务：使用 ``isolation_level=None``（关闭 pysqlite 的隐式事务管理），
    由本类显式 ``BEGIN`` / ``COMMIT``。批量缓冲到 ``batch_size`` 张才提交一次。

    Attributes:
        path: 目标文件路径。
        tiles_written: 已提交的瓦片**次数**（重复写入会重复计数；
            库里的实际行数请用 :class:`~app.mbtiles.reader.MBTilesReader` 统计）。
        batches_committed: 已提交的事务数。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        batch_size: int = DEFAULT_MBTILES_BATCH_SIZE,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            path: MBTiles 文件路径。父目录不存在时会自动创建。
            batch_size: 累计多少张瓦片提交一次事务，必须为正。
            logger: 可选日志器。

        Raises:
            ValueError: ``batch_size`` 非正。
        """
        try:
            size = int(batch_size)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"batch_size 必须是整数，实际为 {batch_size!r}") from exc
        if size <= 0:
            raise ValueError(f"batch_size 必须为正，实际为 {size}")

        self._path = Path(path)
        self._batch_size = size
        self._logger = logger or _logger

        self._conn: sqlite3.Connection | None = None
        self._pending: list[tuple[int, int, int, bytes]] = []
        self._closed = False
        self._metadata_written = False
        self._tiles_written = 0
        self._batches_committed = 0

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    @property
    def path(self) -> Path:
        """目标文件路径。"""
        return self._path

    @property
    def is_open(self) -> bool:
        """连接是否已打开且未关闭。"""
        return self._conn is not None and not self._closed

    @property
    def tiles_written(self) -> int:
        """已提交的瓦片次数。"""
        return self._tiles_written

    @property
    def batches_committed(self) -> int:
        """已提交的事务数。"""
        return self._batches_committed

    @property
    def pending_tiles(self) -> int:
        """尚未提交、仍在缓冲里的瓦片数。"""
        return len(self._pending)

    @property
    def batch_size(self) -> int:
        """批量事务大小。"""
        return self._batch_size

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def open(self) -> None:
        """创建父目录、打开连接并建表（幂等）。

        Raises:
            RuntimeError: 实例已被关闭（``close()`` 之后不能复用）。
            sqlite3.Error: 打不开文件 / 建表失败。
        """
        if self._closed:
            raise RuntimeError("MBTilesWriter 已关闭，请新建实例而不是复用")
        if self._conn is not None:
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)

        # isolation_level=None -> 关闭 pysqlite 的隐式 BEGIN，事务由本类显式控制
        conn = sqlite3.connect(str(self._path), isolation_level=None)
        try:
            for pragma in _PRAGMAS:
                conn.execute(pragma)
            conn.execute(_CREATE_METADATA_SQL)
            conn.execute(_CREATE_TILES_SQL)
            # 目标文件可能已存在且已有元数据（Slice 5 的断点续传会走这条路），
            # 此时不应要求调用方重新写一遍 metadata 才能写瓦片。
            row = conn.execute("SELECT COUNT(*) FROM metadata").fetchone()
            self._metadata_written = bool(row and row[0])
        except BaseException:
            # 打开阶段失败绝不留半个连接
            conn.close()
            raise

        self._conn = conn
        self._logger.debug("MBTiles 已打开：%s（batch_size=%d）", self._path, self._batch_size)

    def write_metadata(self, metadata: MBTilesMetadata) -> None:
        """写入 ``metadata`` 表（8 个标准键，幂等覆盖）。

        Args:
            metadata: 结构化元数据。

        Raises:
            RuntimeError: 未 ``open()``，或实例已关闭。
            TypeError: 传入的不是 :class:`MBTilesMetadata`。
            sqlite3.Error: 写库失败。
        """
        if not isinstance(metadata, MBTilesMetadata):
            raise TypeError(
                f"metadata 必须是 MBTilesMetadata，实际为 {type(metadata).__name__}"
            )
        conn = self._require_open()
        if self._pending:
            # 防御：不要在已有未提交事务时再 BEGIN（SQLite 会报
            # "cannot start a transaction within a transaction"）。
            self.flush()

        self._execute_in_transaction(conn, _INSERT_METADATA_SQL, metadata.as_rows())
        self._metadata_written = True
        self._logger.debug("MBTiles 元数据已写入：%s", self._path.name)

    def write_tile(self, z: int, x: int, y_xyz: int, data: bytes) -> None:
        """写入一张瓦片（进入批量缓冲，达到 ``batch_size`` 时自动提交）。

        Args:
            z: 缩放级别，``0 ~ 22``。
            x: XYZ 列号，``0 <= x < 2**z``。
            y_xyz: **XYZ** 行号（自北向南），``0 <= y < 2**z``。
            data: 瓦片字节，非空。

        Raises:
            RuntimeError: 未 ``open()`` / 已关闭 / 还没写 metadata。
            ValueError: z/x/y 越界，或 ``data`` 为空。
            TypeError: ``data`` 不是字节类型。
            sqlite3.Error: 提交失败（例如磁盘写满）。
        """
        self._require_open()
        if not self._metadata_written:
            raise RuntimeError(
                "写瓦片前必须先调用 write_metadata()："
                "缺 metadata 的 MBTiles 是无效文件（osmdroid 侧会直接加载失败）"
            )

        # 校验并复用 Slice 1 的规则：zoom 区间只此一处定义，
        # y 的翻转也只此一处实现。x 需要单独校验（翻转函数只管 y）。
        zoom = validate_zoom(z)
        column = validate_tile_axis("x", x, zoom)
        row_xyz = validate_tile_axis("y", y_xyz, zoom)
        tile_row = xyz_y_to_tms_y(zoom, row_xyz)

        if isinstance(data, (bytearray, memoryview)):
            payload = bytes(data)
        elif isinstance(data, bytes):
            payload = data
        else:
            raise TypeError(f"瓦片数据必须是 bytes，实际为 {type(data).__name__}")
        if not payload:
            raise ValueError(
                f"空瓦片数据禁止写入 MBTiles（{zoom}/{column}/{row_xyz}）"
            )

        self._pending.append((zoom, column, tile_row, payload))
        if len(self._pending) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        """把缓冲里的瓦片一次性 ``executemany`` 并提交（无缓冲时是空操作）。

        Raises:
            RuntimeError: 未 ``open()`` / 已关闭。
            sqlite3.Error: 提交失败。
        """
        conn = self._require_open()
        if not self._pending:
            return

        # 先搬走再写：提交失败时不能让同一批瓦片留在缓冲里被重复写入
        rows = self._pending
        self._pending = []
        self._execute_in_transaction(conn, _INSERT_TILE_SQL, rows)
        self._tiles_written += len(rows)
        self._batches_committed += 1
        self._logger.debug(
            "MBTiles 提交 %d 张瓦片（累计 %d 张 / %d 次事务）",
            len(rows),
            self._tiles_written,
            self._batches_committed,
        )

    def close(self) -> None:
        """落盘剩余瓦片、checkpoint 并关闭连接（幂等）。

        无论成功与否，连接都会被关闭；失败会向上抛出，**不会**被吞掉
        ——磁盘写满这类错误必须让用户看见。

        Raises:
            sqlite3.Error: ``flush()`` 或 checkpoint 失败。
        """
        if self._closed:
            return
        conn = self._conn
        if conn is None:
            self._closed = True
            return

        try:
            self.flush()
            # 1) 把 -wal 合并回主文件，保证交付的 .mbtiles 单文件自洽
            conn.execute(_CHECKPOINT_SQL)
            # 2) 退出 WAL 模式：否则后续只读打开会重新生成 -wal / -shm
            conn.execute(_SET_JOURNAL_DELETE_SQL)
        finally:
            self._conn = None
            self._closed = True
            conn.close()
            self._logger.debug("MBTiles 已关闭：%s", self._path.name)

    def abort(self) -> None:
        """丢弃未提交的缓冲、回滚并关闭（异常路径用，不抛错）。

        与 :meth:`close` 的区别：``close`` 会尽力把缓冲写完（正常收尾），
        ``abort`` 明确表示"剩下的不要了"（出错收尾）。
        """
        if self._closed:
            return
        self._pending.clear()
        conn = self._conn
        self._conn = None
        self._closed = True
        if conn is None:
            return
        with suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        with suppress(sqlite3.Error):
            conn.execute(_CHECKPOINT_SQL)
            conn.execute(_SET_JOURNAL_DELETE_SQL)
        conn.close()
        self._logger.debug("MBTiles 已中止并关闭：%s", self._path.name)

    # ------------------------------------------------------------------ #
    # context manager
    # ------------------------------------------------------------------ #
    def __enter__(self) -> "MBTilesWriter":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        """正常退出 → ``close()``；异常退出 → ``abort()``（不吞异常）。"""
        if exc_type is None:
            self.close()
        else:
            self.abort()
        return False

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _require_open(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("MBTilesWriter 已关闭")
        if self._conn is None:
            raise RuntimeError("MBTilesWriter 尚未 open()，请先调用 open() 或使用 with")
        return self._conn

    def _execute_in_transaction(
        self,
        conn: sqlite3.Connection,
        sql: str,
        params,
    ) -> None:
        """显式 ``BEGIN`` → ``executemany`` → ``COMMIT``，失败则 ``ROLLBACK``。

        元数据（8 行）与瓦片（最多 ``batch_size`` 行）都是"一批行"，
        因此统一走 ``executemany``，不需要两条代码路径。
        """
        conn.execute("BEGIN")
        try:
            conn.executemany(sql, params)
        except BaseException:
            with suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


# --------------------------------------------------------------------------- #
# 异步写入管线
# --------------------------------------------------------------------------- #
class MBTilesTileSink:
    """Slice 3 下载结果的 MBTiles 落盘适配器（单 Writer 消费模型）。

    作为 ``DownloadManager.download(on_tile=...)`` 的回调使用::

        sink = MBTilesTileSink(writer)
        await manager.download(bounds, 8, 12, layer, on_tile=sink.consume)
        await sink.drain()          # 等队列排空 + commit
        writer.close()

    规则：

    - ``success=False`` 或没有 ``content`` 的结果**直接丢弃**（不写库）。
      这不是"静默丢数据"——失败计数由 ``DownloadManager`` 统计并通过
      ``DownloadSummary.failures_by_reason`` 汇报。
    - ``consume()`` 里 ``await queue.put(...)`` 会把反压传给 HTTP worker，
      因此队列不会被撑爆，内存占用与下载速度解耦。
    - 消费者是**单个**协程：SQLite 连接只在它手里，Http worker 从不碰库。
    - 写库出错时：记下第一个异常，**继续消费并丢弃**——不能让队列堵死
      （否则 worker 永久阻塞在 ``put`` 上，任务永远不结束），
      最终由 :meth:`drain` 把异常抛给调用方。
    """

    def __init__(
        self,
        writer: MBTilesWriter,
        *,
        queue_capacity: int = DEFAULT_MBTILES_QUEUE_CAPACITY,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            writer: 已 ``open()`` 的写入器。
            queue_capacity: 写队列容量，必须落在
                ``[MIN_MBTILES_QUEUE_CAPACITY, MAX_MBTILES_QUEUE_CAPACITY]``。
            logger: 可选日志器。

        Raises:
            TypeError: ``writer`` 类型不对。
            ValueError: 队列容量越界，或 writer 尚未打开。
        """
        if not isinstance(writer, MBTilesWriter):
            raise TypeError(
                f"writer 必须是 MBTilesWriter，实际为 {type(writer).__name__}"
            )
        if not writer.is_open:
            raise ValueError("MBTilesTileSink 要求 writer 已经 open()")

        try:
            capacity = int(queue_capacity)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"queue_capacity 必须是整数，实际为 {queue_capacity!r}"
            ) from exc
        if not MIN_MBTILES_QUEUE_CAPACITY <= capacity <= MAX_MBTILES_QUEUE_CAPACITY:
            raise ValueError(
                f"queue_capacity 必须在 "
                f"[{MIN_MBTILES_QUEUE_CAPACITY}, {MAX_MBTILES_QUEUE_CAPACITY}] 之间，"
                f"实际为 {capacity}"
            )

        self._writer = writer
        self._queue: asyncio.Queue[tuple[int, int, int, bytes] | None] = asyncio.Queue(
            maxsize=capacity
        )
        self._logger = logger or _logger
        self._capacity = capacity

        self._task: asyncio.Task | None = None
        self._failure: BaseException | None = None
        self._closed = False

        self._accepted = 0
        self._written = 0
        self._discarded = 0
        self._skipped = 0

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    @property
    def queue_capacity(self) -> int:
        """队列容量。"""
        return self._capacity

    @property
    def accepted(self) -> int:
        """已进入队列的瓦片数（不含被跳过的失败瓦片）。"""
        return self._accepted

    @property
    def written(self) -> int:
        """已写进 writer 缓冲/数据库的瓦片数（不含写库失败后丢弃的）。"""
        return self._written

    @property
    def discarded(self) -> int:
        """因写库失败而被丢弃的瓦片数。"""
        return self._discarded

    @property
    def skipped(self) -> int:
        """因下载失败/空内容而未写库的瓦片数。"""
        return self._skipped

    @property
    def failure(self) -> BaseException | None:
        """写库过程中出现的第一个异常（``None`` 表示一切正常）。"""
        return self._failure

    @property
    def is_running(self) -> bool:
        """消费者协程是否在跑。"""
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """启动单消费者协程（幂等）。

        必须在事件循环内调用；``consume()`` 会在需要时自动调用它，
        因此忘记显式 ``start()`` 不会导致任务卡住。
        """
        if self._closed:
            raise MBTilesWriteError("MBTilesTileSink 已关闭")
        if self._task is not None:
            return
        self._task = asyncio.get_running_loop().create_task(
            self._consume_loop(), name="mbtiles-writer"
        )

    async def consume(self, result: TileDownloadResult) -> None:
        """``on_tile`` 回调：成功瓦片入队，失败瓦片跳过。

        Raises:
            MBTilesWriteError: sink 已关闭。
            TypeError: 传入的不是 :class:`TileDownloadResult`。
        """
        if self._closed:
            raise MBTilesWriteError("MBTilesTileSink 已关闭，不能再接收瓦片")
        if not isinstance(result, TileDownloadResult):
            raise TypeError(
                f"consume() 需要 TileDownloadResult，实际为 {type(result).__name__}"
            )

        if not result.success or not result.content:
            self._skipped += 1
            return

        self.start()
        tile = result.tile
        # 这里会阻塞 —— 反压交给 HTTP worker，是"边下载边写盘"的关键
        await self._queue.put((tile.z, tile.x, tile.y, result.content))
        self._accepted += 1

    async def drain(self) -> int:
        """等队列排空、把缓冲提交，返回写入张数。

        取消任务时也应调用：**已成功下载并进队列的瓦片会被写完**，
        不会因为用户点了取消而丢掉。

        Returns:
            实际交给 writer 的瓦片数。

        Raises:
            MBTilesWriteError: 写库过程中出现过异常（第一个异常作为 ``__cause__``）。
        """
        if self._task is not None:
            # 哨兵排在队尾，保证它前面的瓦片都先被消费
            await self._queue.put(None)
            await self._task
            self._task = None

        if self._failure is None:
            try:
                self._writer.flush()
            except Exception as exc:  # noqa: BLE001 - 转成统一错误类型上报
                self._failure = exc

        if self._failure is not None:
            raise MBTilesWriteError(
                f"MBTiles 写入失败：{self._failure}"
            ) from self._failure
        return self._written

    async def aclose(self) -> None:
        """``drain()`` + 关闭 writer（异常也会关闭，不会泄漏连接）。"""
        if self._closed:
            return
        self._closed = True
        try:
            await self.drain()
        finally:
            self._writer.close()

    async def __aenter__(self) -> "MBTilesTileSink":
        self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            await self.aclose()
        else:
            # 主体已异常：尽力把已进队列的瓦片落盘（取消场景），
            # 再关闭 writer；原异常优先向上传播。
            with suppress(Exception):
                await self.drain()
            self._closed = True
            self._writer.close()
        return False

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    async def _consume_loop(self) -> None:
        """单消费者：串行写库，出错后转为"消费但丢弃"以保证队列不堵死。"""
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                if self._failure is not None:
                    # 已经出过错：继续把队列排空（丢弃），避免下载端永久阻塞
                    self._discarded += 1
                    continue
                zoom, column, tile_row, data = item
                try:
                    self._writer.write_tile(zoom, column, tile_row, data)
                except Exception as exc:  # noqa: BLE001 - 统一上报
                    self._failure = exc
                    self._discarded += 1
                    self._logger.error(
                        "写入 MBTiles 失败，后续瓦片将不再入库：%r", exc
                    )
                else:
                    self._written += 1
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ #
    # 仅供测试与诊断
    # ------------------------------------------------------------------ #
    @property
    def queue_size(self) -> int:
        """当前队列长度。"""
        return self._queue.qsize()
