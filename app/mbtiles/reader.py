"""MBTiles 读取器（Slice 4；Slice 5 补齐"已有瓦片"批量接口）。

最小读取能力：元数据、瓦片总数、zoom 区间、单瓦片存在性 / 内容，
以及断点续传需要的"列出已有瓦片坐标"。

Slice 5 新增的三个接口都是**只查坐标三列、绝不读 ``tile_data``** 的形态：

- :meth:`MBTilesReader.iter_existing_tiles` —— 惰性产出 ``(z, x, y_xyz)``
- :meth:`MBTilesReader.get_existing_tiles` —— 上面那个的 ``set`` 版本（小任务用）
- :meth:`MBTilesReader.count_tiles_by_zoom` —— 只要计数，连坐标都不搬进内存

原因见 Spec §22：一个 z14 级别的库有几十万张瓦片，每张几十 KB，
"顺便 SELECT tile_data 判存在"就是几个 GB 的内存与磁盘 IO。

坐标约定（与 Writer 一致）
--------------------------

**对外一律 XYZ**：``read_tile(z, x, y_xyz)`` / ``has_tile(z, x, y_xyz)``。
MBTiles 文件内部的 TMS ``tile_row`` 由本类内部换算，复用
:func:`app.geo.xyz_y_to_tms_y`；调用方永远不需要知道 TMS 的存在。

这样做的收益是"一套坐标语义贯穿全链路"：Windows 上调试读取、Python 侧校验、
以及将来 Android 端 osmdroid 用的都是 XYZ，只有落盘那一刻翻转一次。

只读保证
--------

连接一律用 SQLite 的 **read-only URI**（``mode=ro``）打开：

- 不会因为路径写错而"凭空创建一个空库"，把 `文件不存在` 这类错误暴露出来；
- 读者不可能意外修改交付出去的 ``.mbtiles``。
"""

from __future__ import annotations

import logging
import operator
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from app.geo import (
    tms_y_to_xyz_y,
    validate_tile_axis,
    validate_zoom,
    xyz_y_to_tms_y,
)

__all__ = [
    "MBTilesReader",
    # 只读 SQL 常量：``writer.py`` 复用它做"已存在瓦片"查询，
    # 避免两处各写一份 SQL（一处改错就会出现"写的和查的不是同一张表"）。
    "TILE_EXISTS_SQL",
    "TILE_DATA_SQL",
    "EXISTING_TILES_SQL",
    "EXISTING_TILES_BY_ZOOM_SQL",
    "TILE_COUNT_BY_ZOOM_SQL",
]


_logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 读取 SQL（本模块持有全部**读** SQL；写 SQL 在 writer.py）
# --------------------------------------------------------------------------- #
_SELECT_METADATA_SQL = "SELECT name, value FROM metadata"

_SELECT_TILE_COUNT_SQL = "SELECT COUNT(*) FROM tiles"

#: 靠主键索引最左前缀做 MIN/MAX 优化，不需要全表扫描
_SELECT_ZOOM_RANGE_SQL = "SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles"

#: 单张瓦片是否存在。用 ``SELECT 1`` 而不是 ``SELECT tile_data``：
#: 存在性判断绝不能把 BLOB 读进内存（Slice 5 §22 的硬性要求）——
#: 一个 z14 级别的库有几十万张瓦片，每张几十 KB，全读一遍就是几个 GB 的
#: 内存与磁盘 IO。
TILE_EXISTS_SQL = (
    "SELECT 1 FROM tiles "
    "WHERE zoom_level = ? AND tile_column = ? AND tile_row = ? LIMIT 1"
)

#: 读取单张瓦片字节。**只在显式校验已有瓦片时使用**（默认续传路径不会走到）。
TILE_DATA_SQL = (
    "SELECT tile_data FROM tiles "
    "WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?"
)

#: 列出现有瓦片坐标。**只查三列，永不 SELECT tile_data**。
EXISTING_TILES_SQL = "SELECT zoom_level, tile_column, tile_row FROM tiles"

EXISTING_TILES_BY_ZOOM_SQL = (
    "SELECT tile_column, tile_row FROM tiles WHERE zoom_level = ?"
)

#: 按 zoom 汇总瓦片数。走主键索引的最左前缀，不读任何一行数据。
TILE_COUNT_BY_ZOOM_SQL = (
    "SELECT zoom_level, COUNT(*) FROM tiles GROUP BY zoom_level"
)

_SELECT_TABLES_SQL = "SELECT name FROM sqlite_master WHERE type = 'table'"

#: 抽样查询。``ORDER BY`` 保证"同一份库每次抽到的是同一批瓦片"——
#: 校验结论必须可复现，否则"有时 PASS 有时 FAIL"根本没法排查。
_SELECT_SAMPLE_TILES_SQL = (
    "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles "
    "ORDER BY zoom_level, tile_column, tile_row LIMIT ?"
)


class MBTilesReader:
    """读取 MBTiles 单文件。

    用法::

        with MBTilesReader(path) as reader:
            metadata = reader.get_metadata()
            count = reader.get_tile_count()
            blob = reader.read_tile(12, 3367, 1563)     # XYZ 入参

    Raises:
        FileNotFoundError: 文件不存在。
        sqlite3.DatabaseError: 不是合法的 SQLite 文件。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            path: MBTiles 文件路径。
            logger: 可选日志器。
        """
        self._path = Path(path)
        self._logger = logger or _logger
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    @property
    def path(self) -> Path:
        """文件路径。"""
        return self._path

    @property
    def is_open(self) -> bool:
        """连接是否已打开。"""
        return self._conn is not None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def open(self) -> None:
        """以只读方式打开（幂等）。

        Raises:
            FileNotFoundError: 文件不存在。
            sqlite3.DatabaseError: 文件不是合法的 SQLite 数据库。
        """
        if self._conn is not None:
            return
        if not self._path.is_file():
            raise FileNotFoundError(f"MBTiles 文件不存在：{self._path}")

        # 用 URI 形式强制只读；as_uri() 会顺手做百分号编码（空格等），
        # 比自己拼 "file:..." 更不容易在 Windows 路径上翻车。
        uri = f"{self._path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        try:
            # 轻量探针：文件不是 SQLite 库时在这里就会抛 DatabaseError
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except BaseException:
            conn.close()
            raise
        self._conn = conn
        self._logger.debug("MBTiles 已以只读方式打开：%s", self._path)

    def close(self) -> None:
        """关闭连接（幂等）。"""
        conn = self._conn
        self._conn = None
        if conn is not None:
            conn.close()

    def __enter__(self) -> "MBTilesReader":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def get_metadata(self) -> dict[str, str]:
        """读取 ``metadata`` 表的全部键值。

        Returns:
            ``{key: value}``；表存在但没有行时返回空 dict。

        Raises:
            RuntimeError: 未 ``open()``。
            sqlite3.DatabaseError: ``metadata`` 表不存在。
        """
        conn = self._require_open()
        return {name: value for name, value in conn.execute(_SELECT_METADATA_SQL)}

    def get_tile_count(self) -> int:
        """瓦片总数。

        Raises:
            RuntimeError: 未 ``open()``。
            sqlite3.DatabaseError: ``tiles`` 表不存在。
        """
        conn = self._require_open()
        row = conn.execute(_SELECT_TILE_COUNT_SQL).fetchone()
        return int(row[0]) if row else 0

    def get_zoom_range(self) -> tuple[int, int] | None:
        """实际存在的 zoom 区间 ``(min, max)``；库中没有瓦片时返回 ``None``。"""
        conn = self._require_open()
        row = conn.execute(_SELECT_ZOOM_RANGE_SQL).fetchone()
        if not row or row[0] is None:
            return None
        return (int(row[0]), int(row[1]))

    def has_tile(self, z: int, x: int, y_xyz: int) -> bool:
        """判断某张瓦片是否在库中（入参为 **XYZ**）。

        走主键唯一索引，**不读 BLOB**：这是断点续传"这张已经下过了吗"的主查询。

        Raises:
            RuntimeError: 未 ``open()``。
            ValueError: z/x/y 非法。
        """
        conn = self._require_open()
        params = self._tms_params(z, x, y_xyz)
        return conn.execute(TILE_EXISTS_SQL, params).fetchone() is not None

    # ------------------------------------------------------------------ #
    # 已有瓦片批量列举（Slice 5：断点续传用）
    # ------------------------------------------------------------------ #
    def iter_existing_tiles(
        self, zoom: int | None = None
    ) -> Iterator[tuple[int, int, int]]:
        """惰性产出库里已有的瓦片坐标 ``(z, x, y_xyz)``（**XYZ** 语义）。

        惰性生成器而不是 list：调用方可能只需要"边遍历边比对"，
        没必要为了走一遍而把所有坐标先铺到内存里。

        Args:
            zoom: 只列举该缩放级别；``None`` 表示全部级别。

        Yields:
            ``(z, x, y_xyz)``。

        Raises:
            RuntimeError: 未 ``open()``。
            ValueError: ``zoom`` 非 ``None`` 且非法。
        """
        conn = self._require_open()
        if zoom is None:
            rows = conn.execute(EXISTING_TILES_SQL)
        else:
            valid_zoom = validate_zoom(zoom)
            rows = (
                (valid_zoom, column, row)
                for column, row in conn.execute(
                    EXISTING_TILES_BY_ZOOM_SQL, (valid_zoom,)
                )
            )
        for level, column, row in rows:
            level_value = int(level)
            yield (level_value, int(column), tms_y_to_xyz_y(level_value, int(row)))

    def get_existing_tiles(
        self, zoom: int | None = None
    ) -> set[tuple[int, int, int]]:
        """列出库里已有的瓦片坐标集合 ``{(z, x, y_xyz)}``（**XYZ** 语义）。

        这是 Spec 要求的高效接口：**只查 ``zoom_level / tile_column /
        tile_row`` 三列**，从不 ``SELECT tile_data``。

        Args:
            zoom: 只取该缩放级别；``None`` 表示全部级别。

        Returns:
            坐标集合；库为空时返回空集合。

        Raises:
            RuntimeError: 未 ``open()``。
            ValueError: ``zoom`` 非 ``None`` 且非法。
        """
        return set(self.iter_existing_tiles(zoom))

    def count_tiles_by_zoom(self) -> dict[int, int]:
        """按缩放级别统计瓦片数，``{zoom: 张数}``（库为空时返回空 dict）。

        比 ``get_existing_tiles()`` 更省内存：只回一行一个 zoom 的计数，
        不需要把几十万个坐标搬进 Python。适合"只想看看已有多少"的场景。
        """
        conn = self._require_open()
        return {
            int(level): int(count)
            for level, count in conn.execute(TILE_COUNT_BY_ZOOM_SQL)
        }

    def read_tile(self, z: int, x: int, y_xyz: int) -> bytes | None:
        """读取一张瓦片的字节（入参为 **XYZ**）。

        Returns:
            瓦片字节；不存在时返回 ``None``（不抛异常——"库里没有"是正常状态，
            调用方自己决定是警告还是忽略）。

        Raises:
            RuntimeError: 未 ``open()``。
            ValueError: z/x/y 非法。
        """
        conn = self._require_open()
        params = self._tms_params(z, x, y_xyz)
        row = conn.execute(TILE_DATA_SQL, params).fetchone()
        if row is None:
            return None
        return bytes(row[0])

    # ------------------------------------------------------------------ #
    # 校验支持（validator 用的只读辅助）
    # ------------------------------------------------------------------ #
    def list_tables(self) -> set[str]:
        """列出库里的表名。

        有了它，校验器就能区分"缺 metadata 表"与"表在但没数据"这两种
        完全不同的故障，而不必去匹配 ``sqlite3`` 的报错文本。
        """
        conn = self._require_open()
        return {str(row[0]) for row in conn.execute(_SELECT_TABLES_SQL)}

    def sample_tiles(self, limit: int) -> list[tuple[int, int, int, bytes]]:
        """抽样读取若干张瓦片，返回 ``(z, x, y_xyz, data)``（**XYZ** 语义）。

        用于校验时检查 ``tile_data`` 是否真的是图片字节——全量扫描几十万张
        瓦片是后续 Slice 的事。

        Args:
            limit: 最多抽取多少张，必须为正。

        Raises:
            RuntimeError: 未 ``open()``。
            ValueError: ``limit`` 非正，或库里存在非法的瓦片编号
                （说明文件被写坏了，这个信号本身有价值，不该被吞掉）。
        """
        conn = self._require_open()
        try:
            count = operator.index(limit)
        except TypeError as exc:
            raise ValueError(f"limit 必须是整数，实际为 {limit!r}") from exc
        if count <= 0:
            raise ValueError(f"limit 必须为正，实际为 {count}")

        samples: list[tuple[int, int, int, bytes]] = []
        for zoom, column, row, data in conn.execute(_SELECT_SAMPLE_TILES_SQL, (count,)):
            zoom_value = int(zoom)
            y_xyz = tms_y_to_xyz_y(zoom_value, int(row))
            samples.append(
                (zoom_value, int(column), y_xyz, bytes(data) if data else b"")
            )
        return samples

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _require_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("MBTilesReader 尚未 open()，请先调用 open() 或使用 with")
        return self._conn

    @staticmethod
    def _tms_params(z: int, x: int, y_xyz: int) -> tuple[int, int, int]:
        """把 XYZ 入参换算成入库用的 ``(zoom_level, tile_column, tile_row)``。"""
        zoom = validate_zoom(z)
        column = validate_tile_axis("x", x, zoom)
        row_xyz = validate_tile_axis("y", y_xyz, zoom)
        return (zoom, column, xyz_y_to_tms_y(zoom, row_xyz))
