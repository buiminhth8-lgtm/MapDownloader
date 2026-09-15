"""MBTiles 读写层（Slice 4）。

对外入口::

    from app.mbtiles import (MBTilesWriter, MBTilesTileSink, MBTilesReader,
                             MBTilesMetadata, validate_mbtiles)

    with MBTilesWriter(path) as writer:
        writer.write_metadata(MBTilesMetadata.for_bounds(bounds, 8, 12, name="beijing"))
        sink = MBTilesTileSink(writer)
        await manager.download(bounds, 8, 12, layer, on_tile=sink.consume)

模块划分
--------

============= ===============================================================
``models``    ``MBTilesMetadata`` / ``MBTilesValidationResult``（纯数据 + 校验）
``writer``    ``MBTilesWriter``（SQLite 写入）+ ``MBTilesTileSink``（单消费者异步管线）
``reader``    ``MBTilesReader``（只读；对外 XYZ，内部 TMS）
``validator`` ``validate_mbtiles()``（结构与抽样数据校验）
============= ===============================================================

坐标约定（贯穿全项目）
----------------------

- **对外 / 工具内部 / API 一律 XYZ**：``y`` 自北向南。
- **MBTiles 文件内部一律 TMS**：``tile_row`` 自南向北。

翻转只在 writer/reader 内部发生，且只调用
:func:`app.geo.xyz_y_to_tms_y` / :func:`app.geo.tms_y_to_xyz_y`
——公式只有一处实现，不允许在本包内再写一遍。

边界约定
--------

- ``sqlite3`` 只出现在 ``writer`` / ``reader`` / ``validator`` 里；
  ``app.downloader`` **完全不依赖本包**（下载引擎只认 ``on_tile`` 回调）。
- 本包不实现断点续传、不做图层合成、不碰 ADB（Slice 5+）。
"""

from app.mbtiles.models import (
    METADATA_KEY_ORDER,
    SUPPORTED_TILE_FORMATS,
    MBTilesMetadata,
    MBTilesValidationResult,
)
from app.mbtiles.reader import MBTilesReader
from app.mbtiles.validator import (
    OPTIONAL_METADATA_KEYS,
    REQUIRED_METADATA_KEYS,
    validate_mbtiles,
)
from app.mbtiles.writer import MBTilesTileSink, MBTilesWriteError, MBTilesWriter

__all__ = [
    # 模型
    "MBTilesMetadata",
    "MBTilesValidationResult",
    "METADATA_KEY_ORDER",
    "SUPPORTED_TILE_FORMATS",
    # 写入
    "MBTilesWriter",
    "MBTilesTileSink",
    "MBTilesWriteError",
    # 读取
    "MBTilesReader",
    # 校验
    "validate_mbtiles",
    "REQUIRED_METADATA_KEYS",
    "OPTIONAL_METADATA_KEYS",
]
