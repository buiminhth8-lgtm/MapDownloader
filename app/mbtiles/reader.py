"""MBTiles 读取器（Slice 4）。

最小读取能力：元数据、瓦片总数、zoom 区间、单瓦片存在性 / 内容。

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
from pathlib import Path

from app.geo import (
    tms_y_to_xyz_y,
    validate_tile_axis,
    validate_zoom,
    xyz_y_to_tms_y,
)

__all__ = ["MBTilesReader"]


_logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 读取 SQL（本模块持有全部**读** SQL；写 SQL 在 writer.py）
# --------------------------------------------------------------------------- #
_SELECT_METADATA_SQL = "SELECT name, value FROM metadata"

_SELECT_TILE_COUNT_SQL = "SELECT COUNT(*) FROM tiles"

#: 靠主键索引最左前缀做 MIN/MAX 优化，不需要全表扫描
_SELECT_ZOOM_RANGE_SQL = "SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles"

_SELECT_TILE_EXISTS_SQL = (
    "SELECT 1 FROM tiles "
    "WHERE zoom_level = ? AND tile_column = ? AND tile_row = ? LIMIT 1"
)

_SELECT_TILE_DATA_SQL = (
    "SELECT tile_data FROM tiles "
    "WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?"
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

        Raises:
            RuntimeError: 未 ``open()``。
            ValueError: z/x/y 非法。
        """
        conn = self._require_open()
        params = self._tms_params(z, x, y_xyz)
        return conn.execute(_SELECT_TILE_EXISTS_SQL, params).fetchone() is not None

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
        row = conn.execute(_SELECT_TILE_DATA_SQL, params).fetchone()
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
