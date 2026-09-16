"""MBTiles 读取器（Slice 4；Slice 5 补齐"已有瓦片"批量接口；Slice 8 补齐诊断接口）。

Slice 8 的边界
--------------

本模块**只回答"库里有什么"**，不回答"这算不算问题"：

- 提供 :meth:`MBTilesReader.get_zoom_stats`（逐 zoom 聚合）、
  :meth:`~MBTilesReader.count_empty_tiles`、
  :meth:`~MBTilesReader.count_duplicate_tiles`、
  :meth:`~MBTilesReader.count_tiles_in_range`、
  :meth:`~MBTilesReader.sample_tiles_stratified`、
  :meth:`~MBTilesReader.iter_tiles`（流式）、``quick_check()`` 等；
- 判定规则（什么算 ERROR、什么只算 WARNING、PASS/WARNING/FAIL 怎么定）
  全部在 :mod:`app.mbtiles.diagnostics`。

把规则塞进 Reader 会让"读取"和"结论"两件事绑死，也让诊断逻辑无法脱离
SQLite 单测；反过来，让 diagnostics 自己写 SQL 又会出现"两处各写一份查询"
的经典漂移。分界线就是：**SQL 在这里，"应该怎样"在那里**。

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

# --------------------------------------------------------------------------- #
# Slice 8：诊断用的只读查询
# --------------------------------------------------------------------------- #
# 这一组查询的共同纪律（Spec §24）：
#   1. 只用 SQL 聚合 / COUNT / GROUP BY，**绝不**把行搬进 Python 再统计；
#   2. 只有在"必须看内容"时才 SELECT tile_data，且一律带 LIMIT 或分批游标；
#   3. 全部走主键索引 (zoom_level, tile_column, tile_row) 的最左前缀。

#: 每个 zoom 的坐标范围。一次 GROUP BY 拿到 Slice 8 需要的全部规模信息，
#: 不读任何 tile_data、不把坐标搬进内存。
_ZOOM_AGGREGATE_SQL = (
    "SELECT zoom_level, COUNT(*), "
    "MIN(tile_column), MAX(tile_column), MIN(tile_row), MAX(tile_row) "
    "FROM tiles GROUP BY zoom_level ORDER BY zoom_level"
)

#: 空瓦片：``NULL`` 或零长度。**包含 ``IS NULL``**——手工造的坏库、
#: 或表结构被人改成允许 NULL 时，只判 ``length()=0`` 会漏掉 ``NULL``
#: （``length(NULL)`` 是 ``NULL`` 而不是 0）。
_COUNT_EMPTY_TILES_SQL = (
    "SELECT COUNT(*) FROM tiles WHERE tile_data IS NULL OR length(tile_data) = 0"
)

#: 重复坐标。正常库有主键兜底，这条查询恒为 0——它存在的意义是守住
#: "别人的库 / 被改过的库"这条通路：诊断工具必须能发现不符合规范的输入。
_DUPLICATE_TILES_SQL = (
    "SELECT zoom_level, tile_column, tile_row, COUNT(*) AS n FROM tiles "
    "GROUP BY zoom_level, tile_column, tile_row HAVING n > 1 "
    "ORDER BY zoom_level, tile_column, tile_row"
)

_COUNT_DUPLICATE_TILES_SQL = f"SELECT COUNT(*) FROM ({_DUPLICATE_TILES_SQL})"

#: 某一列（固定 x）上落在给定 TMS 行区间里的行号。缺失瓦片定位用：
#: 逐列判断"这一列缺不缺"，缺了才逐行展开（见 diagnostics.collect_missing_details）。
_TILE_ROWS_IN_COLUMN_SQL = (
    "SELECT tile_row FROM tiles "
    "WHERE zoom_level = ? AND tile_column = ? AND tile_row BETWEEN ? AND ?"
)

#: 矩形范围内的瓦片数（TMS 行区间）。覆盖完整性检查的核心查询，
#: 靠主键索引区间扫描，**不读 BLOB**。
_COUNT_TILES_IN_RANGE_SQL = (
    "SELECT COUNT(*) FROM tiles WHERE zoom_level = ? "
    "AND tile_column BETWEEN ? AND ? AND tile_row BETWEEN ? AND ?"
)

#: 坐标越界。``1 << zoom_level`` 在 SQL 里遇到负数/超大数会给出不可预期的结果，
#: 因此把该 zoom 的合法上界 ``2**zoom`` 由 Python 侧算好传参
#: （zoom 已被逐级校验过，见 diagnostics.check_coordinates）。
_COUNT_INVALID_COORDINATES_SQL = (
    "SELECT COUNT(*) FROM tiles WHERE zoom_level = ? "
    "AND (tile_column < 0 OR tile_column >= ? OR tile_row < 0 OR tile_row >= ?)"
)

_SELECT_INVALID_COORDINATES_SQL = (
    "SELECT tile_column, tile_row FROM tiles WHERE zoom_level = ? "
    "AND (tile_column < 0 OR tile_column >= ? OR tile_row < 0 OR tile_row >= ?) "
    "LIMIT ?"
)

#: zoom 本身非法的瓦片（超出 ``[min, max]``）。
#: 这种行不能用上面的查询表达——"合法上界"依赖 zoom 自身。
_COUNT_ZOOM_OUT_OF_RANGE_SQL = (
    "SELECT COUNT(*) FROM tiles WHERE zoom_level < ? OR zoom_level > ?"
)

_SELECT_ZOOM_OUT_OF_RANGE_SQL = (
    "SELECT zoom_level, tile_column, tile_row FROM tiles "
    "WHERE zoom_level < ? OR zoom_level > ? LIMIT ?"
)

#: 深度检查的全量游标。**必须**配合 ``fetchmany()`` 分批取——
#: ``fetchall()`` 会把几十万张瓦片的 BLOB 一次性读进内存（Spec §11/§24 禁止）。
_STREAM_TILE_DATA_SQL = (
    "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles "
    "ORDER BY zoom_level, tile_column, tile_row"
)

#: 分层抽样：在"按坐标排序"的索引上取第 N 行。
#: 用 ``OFFSET`` 而不是 ``ORDER BY random()``——后者要先给全表排序，
#: 在 50 万行的库上是一次实打实的全表 temp B-tree。
_SAMPLE_TILE_AT_OFFSET_SQL = (
    "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles "
    "WHERE zoom_level = ? ORDER BY tile_column, tile_row LIMIT 1 OFFSET ?"
)

#: SQLite 自检（Spec §4）。``quick_check`` 是轻量的那个；重量级的
#: ``integrity_check`` 只在用户显式选择深度检查时才跑。
_QUICK_CHECK_SQL = "PRAGMA quick_check"
_INTEGRITY_CHECK_SQL = "PRAGMA integrity_check"

#: 表结构探测。表名由 :meth:`MBTilesReader.get_table_columns` 先做白名单校验，
#: 再插进 SQL —— ``PRAGMA`` 不接受参数占位符。
_TABLE_INFO_SQL = 'PRAGMA table_info("{table}")'


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
    def iter_tile_coordinates(
        self,
        zoom: int | None = None,
        *,
        limit: int | None = None,
    ) -> Iterator[tuple[int, int, int]]:
        """惰性产出库里的瓦片坐标 ``(z, x, y_xyz)``（**XYZ** 语义）。

        Slice 8 的正式名字（Spec §25）。惰性生成器而不是 list：调用方可能只需要
        "边遍历边比对"，没必要为了走一遍而把所有坐标先铺到内存里。

        Args:
            zoom: 只列举该缩放级别；``None`` 表示全部级别。
            limit: 最多产出多少条；``None`` 表示不限。诊断工具用它给
                "可疑坐标明细"封顶，避免一个坏库把几百 MB 坐标灌进内存。

        Yields:
            ``(z, x, y_xyz)``。

        Raises:
            RuntimeError: 未 ``open()``。
            ValueError: ``zoom`` 非 ``None`` 且非法，或 ``limit`` 非正。
        """
        conn = self._require_open()
        count = self._check_limit(limit)
        if zoom is None:
            rows = conn.execute(
                EXISTING_TILES_SQL if count is None else f"{EXISTING_TILES_SQL} LIMIT ?",
                () if count is None else (count,),
            )
        else:
            valid_zoom = validate_zoom(zoom)
            if count is None:
                params: tuple = (valid_zoom,)
            else:
                params = (valid_zoom, count)
            cursor = conn.execute(
                EXISTING_TILES_BY_ZOOM_SQL
                + ("" if count is None else " LIMIT ?"),
                params,
            )
            rows = ((valid_zoom, column, row) for column, row in cursor)
        for level, column, row in rows:
            level_value = int(level)
            yield (level_value, int(column), tms_y_to_xyz_y(level_value, int(row)))

    def iter_existing_tiles(
        self, zoom: int | None = None
    ) -> Iterator[tuple[int, int, int]]:
        """（Slice 5 的旧名字）等价于 :meth:`iter_tile_coordinates`。

        保留它只是因为断点续传链路在用；新代码请直接用
        :meth:`iter_tile_coordinates`（带 ``limit``）。两个名字背后是同一份实现，
        不会出现"两个查询查的不是同一张表"这种漂移。
        """
        return self.iter_tile_coordinates(zoom)

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

    # ------------------------------------------------------------------ #
    # 诊断支持（Slice 8，Spec §4 / §6 / §8 / §9 / §11 / §25）
    # ------------------------------------------------------------------ #
    #: 声明："以下方法一律只读、只查需要的列"——
    #: 它们给 :mod:`app.mbtiles.diagnostics` 用，**规则不在这里**：
    #: Reader 只回答"库里有什么"，"这算不算问题"由 diagnostics 判断。
    def get_zoom_stats(self) -> list[tuple[int, int, int, int, int, int]]:
        """每个 zoom 的规模信息：``(zoom, count, min_x, max_x, min_tms_y, max_tms_y)``。

        一条 ``GROUP BY`` 查询拿到全部规模信息（Spec §6），**不读 tile_data**、
        不把坐标搬进内存 —— 一个 50 万张瓦片的库，这个方法返回的仍然是
        十几行元组。

        Returns:
            按 zoom 升序排列；库为空时返回空列表。
        """
        conn = self._require_open()
        return [
            (int(z), int(n), int(min_x), int(max_x), int(min_y), int(max_y))
            for z, n, min_x, max_x, min_y, max_y in conn.execute(_ZOOM_AGGREGATE_SQL)
        ]

    def count_empty_tiles(self) -> int:
        """``tile_data`` 为 ``NULL`` 或零长度的瓦片数（Spec §9）。

        用 SQL 的 ``COUNT(*)`` 而不是把行读出来在 Python 里判 —— 后者会把
        整个库的 BLOB 拖进内存。
        """
        conn = self._require_open()
        row = conn.execute(_COUNT_EMPTY_TILES_SQL).fetchone()
        return int(row[0]) if row else 0

    def count_duplicate_tiles(self) -> int:
        """重复坐标的**组数**（同一 ``(z, x, tms_y)`` 出现多次）。

        正常库有主键兜底，这里恒为 0；诊断工具必须能发现"别人的库 /
        被改过的库"里没有主键的情况（Spec §8）。
        """
        conn = self._require_open()
        row = conn.execute(_COUNT_DUPLICATE_TILES_SQL).fetchone()
        return int(row[0]) if row else 0

    def find_duplicate_tiles(
        self, limit: int
    ) -> list[tuple[int, int, int, int]]:
        """列出重复坐标 ``(z, x, tms_y, 出现次数)``，最多 ``limit`` 组。"""
        conn = self._require_open()
        count = self._check_limit(limit)
        if count is None:
            raise ValueError("limit 必须为正整数")
        return [
            (int(z), int(x), int(row), int(n))
            for z, x, row, n in conn.execute(
                f"{_DUPLICATE_TILES_SQL} LIMIT ?", (count,)
            )
        ]

    def count_tiles_in_range(
        self, zoom: int, min_x: int, max_x: int, min_tms_y: int, max_tms_y: int
    ) -> int:
        """统计某个 zoom 下落在矩形范围内的瓦片数（入参均为**库内坐标**）。

        覆盖完整性检查的核心查询（Spec §12）：靠主键索引做区间扫描，
        不读任何 BLOB。缺几张瓦片要精确知道，但不能为此把整个范围的坐标
        枚举出来 —— 一个 z17 的矩形可能就是几万张。
        """
        conn = self._require_open()
        zoom_value = validate_zoom(zoom)
        row = conn.execute(
            _COUNT_TILES_IN_RANGE_SQL,
            (
                zoom_value,
                operator.index(min_x),
                operator.index(max_x),
                operator.index(min_tms_y),
                operator.index(max_tms_y),
            ),
        ).fetchone()
        return int(row[0]) if row else 0

    def iter_tile_rows_in_column(
        self, zoom: int, column: int, min_tms_y: int, max_tms_y: int
    ) -> Iterator[int]:
        """逐行产出某列（固定 ``tile_column``）上的 TMS 行号。

        缺失瓦片定位用：先按列判断"这一列缺不缺"（一次计数），
        确认缺了才逐行展开 —— 否则在世界范围的大库上会退化成
        逐格扫描几亿次（见 :func:`app.mbtiles.diagnostics.collect_missing_details`）。

        Yields:
            TMS 行号（升序）。
        """
        conn = self._require_open()
        zoom_value = validate_zoom(zoom)
        rows = conn.execute(
            _TILE_ROWS_IN_COLUMN_SQL,
            (
                zoom_value,
                operator.index(column),
                operator.index(min_tms_y),
                operator.index(max_tms_y),
            ),
        )
        for (row,) in rows:
            yield int(row)

    def count_invalid_coordinates(self, zoom: int) -> int:
        """某个 zoom 下坐标越界的瓦片数（``x`` 或 ``tms_y`` 不在 ``[0, 2**z)``）。"""
        conn = self._require_open()
        tile_count = 1 << validate_zoom(zoom)
        row = conn.execute(
            _COUNT_INVALID_COORDINATES_SQL, (zoom, tile_count, tile_count)
        ).fetchone()
        return int(row[0]) if row else 0

    def find_invalid_coordinates(
        self, zoom: int, limit: int
    ) -> list[tuple[int, int]]:
        """列出某个 zoom 下越界坐标 ``(x, tms_y)``，最多 ``limit`` 条。"""
        conn = self._require_open()
        count = self._check_limit(limit)
        if count is None:
            raise ValueError("limit 必须为正整数")
        tile_count = 1 << validate_zoom(zoom)
        return [
            (int(x), int(row))
            for x, row in conn.execute(
                _SELECT_INVALID_COORDINATES_SQL,
                (zoom, tile_count, tile_count, count),
            )
        ]

    def count_tiles_with_illegal_zoom(
        self, min_zoom: int, max_zoom: int
    ) -> int:
        """zoom 本身超出 ``[min_zoom, max_zoom]`` 的瓦片数。

        这类行**无法**用 :meth:`count_invalid_coordinates` 表达 ——
        "合法上界"依赖 zoom 自身，而它的 zoom 就是错的。
        """
        conn = self._require_open()
        row = conn.execute(
            _COUNT_ZOOM_OUT_OF_RANGE_SQL,
            (operator.index(min_zoom), operator.index(max_zoom)),
        ).fetchone()
        return int(row[0]) if row else 0

    def find_illegal_zoom_tiles(
        self, min_zoom: int, max_zoom: int, limit: int
    ) -> list[tuple[int, int, int]]:
        """列出 zoom 非法的瓦片 ``(z, x, tms_y)``，最多 ``limit`` 条。"""
        conn = self._require_open()
        count = self._check_limit(limit)
        if count is None:
            raise ValueError("limit 必须为正整数")
        return [
            (int(z), int(x), int(row))
            for z, x, row in conn.execute(
                _SELECT_ZOOM_OUT_OF_RANGE_SQL,
                (operator.index(min_zoom), operator.index(max_zoom), count),
            )
        ]

    def iter_tiles(self, batch_size: int = 500) -> Iterator[tuple[int, int, int, bytes]]:
        """流式遍历全部瓦片，产出 ``(z, x, y_xyz, data)``（**XYZ** 语义）。

        深度图片扫描专用（Spec §11）。实现上走 ``fetchmany(batch_size)``
        分批从游标取，**不是** ``fetchall()``：几十万张瓦片每张几十 KB，
        一次性取回就是几个 GB 的常驻内存。

        Args:
            batch_size: 每次从游标取多少行，必须为正。

        Yields:
            ``(z, x, y_xyz, data)``；``data`` 为空时是 ``b""``。

        Raises:
            RuntimeError: 未 ``open()``。
            ValueError: ``batch_size`` 非正。
        """
        conn = self._require_open()
        size = self._check_limit(batch_size)
        if size is None:
            raise ValueError("batch_size 必须为正整数")

        cursor = conn.execute(_STREAM_TILE_DATA_SQL)
        try:
            while True:
                rows = cursor.fetchmany(size)
                if not rows:
                    return
                for level, column, row, data in rows:
                    level_value = int(level)
                    yield (
                        level_value,
                        int(column),
                        tms_y_to_xyz_y(level_value, int(row)),
                        bytes(data) if data else b"",
                    )
        finally:
            cursor.close()

    def sample_tiles_stratified(
        self,
        zoom_counts: dict[int, int],
        per_zoom: int,
        total_limit: int,
    ) -> list[tuple[int, int, int, bytes]]:
        """**分层**抽样：每个 zoom 抽若干张，而不是全挤在最小的 zoom 上。

        为什么必须分层：``ORDER BY zoom_level LIMIT 50`` 会把这 50 张全部
        落在 z8（最小 zoom），于是"z17 的瓦片是坏的"这种问题**永远抽不到**。

        为什么用 ``OFFSET`` 而不是 ``ORDER BY random()``：后者要先给全表
        排序建 temp B-tree，50 万行的库上是一次实打实的全表扫描；
        ``OFFSET`` 沿着已有索引顺序定位，代价与"偏移量"成正比且不建临时表。

        抽样结果**可复现**（同一份库每次抽到同一批）：诊断结论必须能被复现，
        否则"有时 PASS 有时 FAIL"根本没法排查。

        Args:
            zoom_counts: ``{zoom: 张数}``（来自 :meth:`count_tiles_by_zoom`）。
            per_zoom: 每个 zoom 最多抽多少张。
            total_limit: 总数上限。

        Returns:
            ``(z, x, y_xyz, data)`` 列表。
        """
        conn = self._require_open()
        per = self._check_limit(per_zoom)
        total = self._check_limit(total_limit)
        if per is None or total is None:
            raise ValueError("per_zoom / total_limit 必须为正整数")

        samples: list[tuple[int, int, int, bytes]] = []
        # 从大 zoom 往小 zoom 抽：高 zoom 的瓦片数量最多、最值得检查，
        # 而总量上限是在遍历过程中被消耗掉的。
        for zoom in sorted(zoom_counts, reverse=True):
            if len(samples) >= total:
                break
            available = int(zoom_counts[zoom])
            if available <= 0:
                continue
            wanted = min(per, available, total - len(samples))
            for offset in self._spread_offsets(available, wanted):
                row = conn.execute(
                    _SAMPLE_TILE_AT_OFFSET_SQL, (zoom, offset)
                ).fetchone()
                if row is None:
                    continue
                level, column, row_tms, data = row
                level_value = int(level)
                samples.append(
                    (
                        level_value,
                        int(column),
                        tms_y_to_xyz_y(level_value, int(row_tms)),
                        bytes(data) if data else b"",
                    )
                )
        return samples

    @staticmethod
    def _spread_offsets(available: int, wanted: int) -> list[int]:
        """在 ``[0, available)`` 上均匀取 ``wanted`` 个偏移量。

        均匀取而不是取开头几个：取开头永远只看到"地图西边缘 + 北边缘"
        那一小片，抽样就失去了意义。
        """
        if wanted <= 0:
            return []
        if wanted >= available:
            return list(range(available))
        step = available / wanted
        return [int(index * step) for index in range(wanted)]

    # ------------------------------------------------------------------ #
    # SQLite 自检（Spec §4）
    # ------------------------------------------------------------------ #
    def quick_check(self) -> list[str]:
        """``PRAGMA quick_check`` 的结果行；正常是 ``["ok"]``。

        Spec §4 明确：**不要**默认跑重量级的 ``integrity_check``。
        ``quick_check`` 跳过"索引内容与表内容是否逐行一致"这项最贵的比对，
        在几十万行的库上仍然是"秒级"而不是"分钟级"。
        """
        conn = self._require_open()
        return [str(row[0]) for row in conn.execute(_QUICK_CHECK_SQL)]

    def integrity_check(self) -> list[str]:
        """``PRAGMA integrity_check`` 的结果行（**慢**，只在深度检查时调用）。"""
        conn = self._require_open()
        return [str(row[0]) for row in conn.execute(_INTEGRITY_CHECK_SQL)]

    def get_table_columns(self, table: str) -> tuple[str, ...]:
        """某个表的列名（``PRAGMA table_info``）。

        Args:
            table: 表名，**必须**是库中已存在的表（先查 ``sqlite_master``）。
                ``PRAGMA`` 不支持参数占位符，只能把表名拼进 SQL，因此这一步
                白名单校验是防止 SQL 注入的**唯一**屏障。

        Returns:
            列名元组（按定义顺序）；表不存在时返回空元组。
        """
        conn = self._require_open()
        if table not in self.list_tables():
            return ()
        rows = conn.execute(_TABLE_INFO_SQL.format(table=table))
        return tuple(str(row[1]) for row in rows)

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
    def _check_limit(limit: int | None) -> int | None:
        """把 ``limit`` 规范化成正整数；``None`` 原样返回（表示不限）。

        存在的意义是让"上限必须为正"这条规则只有一处实现：``LIMIT 0``
        会让查询静默返回空结果，看着像"库里没数据"，而实际上是用错了参数。
        """
        if limit is None:
            return None
        try:
            count = operator.index(limit)
        except TypeError as exc:
            raise ValueError(f"limit 必须是整数，实际为 {limit!r}") from exc
        if count <= 0:
            raise ValueError(f"limit 必须为正，实际为 {count}")
        return count

    @staticmethod
    def _tms_params(z: int, x: int, y_xyz: int) -> tuple[int, int, int]:
        """把 XYZ 入参换算成入库用的 ``(zoom_level, tile_column, tile_row)``。"""
        zoom = validate_zoom(z)
        column = validate_tile_axis("x", x, zoom)
        row_xyz = validate_tile_axis("y", y_xyz, zoom)
        return (zoom, column, xyz_y_to_tms_y(zoom, row_xyz))
