"""MBTiles 基础校验（Slice 4）。

产出 :class:`~app.mbtiles.models.MBTilesValidationResult`：把"这个 .mbtiles
能不能直接用"变成一个**可展示、可断言**的结论，而不是让用户自己猜。

检查项（按"越前面越致命"排序）
------------------------------

1. 文件存在、可打开、大小可读
2. 是合法 SQLite 文件
3. ``metadata`` / ``tiles`` 两张表存在
4. ``metadata`` 必备字段齐全、``bounds`` / ``minzoom`` / ``maxzoom`` 可解析
5. ``tiles`` 表非空，zoom 区间在合法范围内
6. 抽样（默认最多 20 张）检查 ``tile_data``：非空、magic bytes 是 PNG/JPEG
7. （Slice 6）metadata 里有合成信息时校验其自洽性：``labels=true`` 必须配
   ``composition=merged``、合成地图必须是 ``format=png``，且抽样的瓦片
   必须都是 PNG

**本 Slice 不做全量扫描**：几十万张瓦片逐张 sniff 一次是分钟级操作，
放在"下载完成"这个时机不合适，留给后续 Slice。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config.settings import (
    MBTILES_VALIDATE_SAMPLE_LIMIT,
    TILE_MATH_MAX_ZOOM,
    TILE_MATH_MIN_ZOOM,
)
from app.geo import format_size
from app.mbtiles.models import (
    SUPPORTED_MAP_TYPES,
    MBTilesValidationResult,
)
from app.mbtiles.reader import MBTilesReader
from app.models.map_layer import COMPOSITION_MERGED, COMPOSITION_VALUES

# 复用 Slice 2 的 magic bytes 判定，不在这里再抄一份 PNG/JPEG 常量。
# 这一条很重要：判断"这是不是一张合法瓦片"的规则必须全链路一致，
# 否则会出现"下载时判合法、校验时判非法"的诡异现象。
from app.providers import detect_image_format

__all__ = [
    "validate_mbtiles",
    "parse_bounds",
    "parse_zoom",
    "parse_bool",
    "REQUIRED_METADATA_KEYS",
    "OPTIONAL_METADATA_KEYS",
    "COMPOSITION_METADATA_KEYS",
]


#: 缺失即 `valid=False` 的元数据键。这几项是 osmdroid 能否正确加载的关键：
#: ``format`` 决定解码方式，``bounds``/``minzoom``/``maxzoom`` 决定显示范围。
REQUIRED_METADATA_KEYS: tuple[str, ...] = (
    "name",
    "format",
    "bounds",
    "minzoom",
    "maxzoom",
)

#: 缺失只记 warning 的元数据键：属于规范性字段，不影响加载。
OPTIONAL_METADATA_KEYS: tuple[str, ...] = ("type", "version", "description")

#: Slice 6 的本工具自有字段（Spec §10）。
#:
#: 刻意**不**放进 :data:`OPTIONAL_METADATA_KEYS`：``OPTIONAL`` 里的缺失会变成
#: warning，而 Slice 4/5 生成的文件、别的工具生成的文件当然没有这几个键 ——
#: 那样每个正常文件都会平白多出四条"缺少可选字段"的噪音。
#: 它们**存在时**由 :func:`_check_composition` 严格校验（Spec §18）。
COMPOSITION_METADATA_KEYS: tuple[str, ...] = (
    "provider",
    "map_type",
    "labels",
    "composition",
)

#: 抽样里最多列出几个"非法瓦片"的坐标（避免错误信息刷屏）
_MAX_REPORTED_BAD_SAMPLES = 3


def parse_bounds(value: str) -> tuple[float, float, float, float] | None:
    """解析 ``west,south,east,north``；失败返回 ``None``。

    Slice 8 起从私有改成公开：诊断模块也要解析同一份 ``bounds`` 文本，
    而"怎么把一个字符串读成四个数"这件事必须只有一处实现 ——
    两处各写一份的结果是"校验说能解析、诊断说不能"这种自相矛盾。
    """
    parts = [piece.strip() for piece in str(value).split(",")]
    if len(parts) != 4:
        return None
    try:
        return tuple(float(piece) for piece in parts)  # type: ignore[return-value]
    except ValueError:
        return None


def parse_zoom(value: str) -> int | None:
    """解析 zoom 字段；失败返回 ``None``。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def validate_mbtiles(
    path: str | Path,
    *,
    sample_limit: int = MBTILES_VALIDATE_SAMPLE_LIMIT,
) -> MBTilesValidationResult:
    """校验一个 MBTiles 文件，返回结构化结论。

    **本函数不抛异常**：任何故障都转成 ``errors`` 里的文字。调用方（GUI/Service）
    拿到的是"结论"，不必自己写 try/except 去分辨故障类型。

    Args:
        path: MBTiles 文件路径。
        sample_limit: 抽样检查多少张瓦片的 ``tile_data``。

    Returns:
        :class:`~app.mbtiles.models.MBTilesValidationResult`
    """
    result = MBTilesValidationResult()
    file_path = Path(path)

    if not file_path.exists():
        result.errors.append(f"文件不存在：{file_path}")
        return result
    if file_path.is_dir():
        result.errors.append(f"路径是目录而不是文件：{file_path}")
        return result

    try:
        result.file_size = file_path.stat().st_size
    except OSError as exc:
        result.errors.append(f"无法读取文件信息：{exc}")
        return result

    try:
        reader = MBTilesReader(file_path)
        reader.open()
    except sqlite3.DatabaseError as exc:
        result.errors.append(f"不是合法的 SQLite 文件：{exc}")
        return result
    except OSError as exc:
        result.errors.append(f"无法打开文件：{exc}")
        return result

    try:
        tables = reader.list_tables()
    except sqlite3.DatabaseError as exc:
        result.errors.append(f"无法读取表清单：{exc}")
        reader.close()
        return result

    for name in ("metadata", "tiles"):
        if name not in tables:
            result.errors.append(f"缺少 {name} 表")

    try:
        if "metadata" in tables:
            _check_metadata(reader, result)
        if "tiles" in tables:
            _check_tiles(reader, result, sample_limit)
    finally:
        reader.close()

    result.valid = not result.errors
    return result


# --------------------------------------------------------------------------- #
# 各项检查
# --------------------------------------------------------------------------- #
def _check_metadata(reader: MBTilesReader, result: MBTilesValidationResult) -> None:
    """第 4 项：必备字段 + 关键字段可解析。"""
    try:
        metadata = reader.get_metadata()
    except sqlite3.DatabaseError as exc:
        result.errors.append(f"无法读取 metadata：{exc}")
        return

    if not metadata:
        result.errors.append("metadata 表为空")
        return

    for key in REQUIRED_METADATA_KEYS:
        if key not in metadata:
            result.errors.append(f"metadata 缺少必备字段：{key}")
    for key in OPTIONAL_METADATA_KEYS:
        if key not in metadata:
            result.warnings.append(f"metadata 缺少可选字段：{key}")

    bounds = metadata.get("bounds")
    if bounds is not None:
        parsed = parse_bounds(bounds)
        if parsed is None:
            result.errors.append(f"metadata.bounds 无法解析为 west,south,east,north：{bounds!r}")
        else:
            west, south, east, north = parsed
            if west >= east or south >= north:
                result.warnings.append(
                    f"metadata.bounds 不是有效范围（west<east、south<north）：{bounds}"
                )

    declared: list[int] = []
    for key in ("minzoom", "maxzoom"):
        raw = metadata.get(key)
        if raw is None:
            continue
        value = parse_zoom(raw)
        if value is None:
            result.errors.append(f"metadata.{key} 不是整数：{raw!r}")
        elif not TILE_MATH_MIN_ZOOM <= value <= TILE_MATH_MAX_ZOOM:
            result.errors.append(
                f"metadata.{key} 超出 [{TILE_MATH_MIN_ZOOM}, {TILE_MATH_MAX_ZOOM}]：{value}"
            )
        else:
            declared.append(value)
    if len(declared) == 2 and declared[0] > declared[1]:
        result.errors.append(
            f"metadata.minzoom > maxzoom：{declared[0]} > {declared[1]}"
        )

    _check_composition(metadata, result)


def parse_bool(value: str) -> bool | None:
    """解析 ``true`` / ``false``（大小写不敏感）；不认识返回 ``None``。"""
    text = str(value).strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def _check_composition(
    metadata: dict[str, str], result: MBTilesValidationResult
) -> None:
    """Slice 6 第 7 项：合成信息（Spec §18）。

    只在 metadata **存在**这几个键时校验。检查三件事：

    1. 取值本身合法（``map_type`` ∈ vector/image，``labels`` ∈ true/false，
       ``composition`` ∈ single/merged）。
    2. ``labels`` 与 ``composition`` **互相自洽** —— ``labels=true`` 却写着
       ``composition=single`` 说明有一个字段是错的，而用户无法分辨该信哪个。
    3. 合成地图必须是 PNG。注记依赖透明通道，而 JPEG 存不下 alpha；
       一个 ``format=jpg`` 的"合成地图"必然是坏的。

    刻意**不做**图像识别判断"文字到底在不在"：那需要 OCR，既慢又不可靠，
    而且"底色恰好是白色"的注记层无法与"没有注记"区分。
    """
    result.provider = (metadata.get("provider") or "").strip() or None

    raw_map_type = (metadata.get("map_type") or "").strip().lower()
    if raw_map_type:
        if raw_map_type not in SUPPORTED_MAP_TYPES:
            result.errors.append(
                f"metadata.map_type 取值不认识：{raw_map_type!r}"
                f"（应为 {sorted(SUPPORTED_MAP_TYPES)} 之一）"
            )
        else:
            result.map_type = raw_map_type

    raw_labels = metadata.get("labels")
    if raw_labels is not None and str(raw_labels).strip():
        parsed = parse_bool(raw_labels)
        if parsed is None:
            result.errors.append(
                f"metadata.labels 必须是 true / false：{raw_labels!r}"
            )
        else:
            result.labels = parsed

    raw_composition = (metadata.get("composition") or "").strip().lower()
    if raw_composition:
        if raw_composition not in COMPOSITION_VALUES:
            result.errors.append(
                f"metadata.composition 取值不认识：{raw_composition!r}"
                f"（应为 {sorted(COMPOSITION_VALUES)} 之一）"
            )
        else:
            result.composition = raw_composition

    if result.labels is True and result.composition != COMPOSITION_MERGED:
        result.errors.append(
            "metadata.labels=true 但 composition 不是 merged"
            f"（实际 {result.composition!r}）：注记层的存在与合成方式自相矛盾"
        )
    if result.is_merged and result.labels is not True:
        result.errors.append(
            "metadata.composition=merged 但 labels 不是 true"
        )

    if result.labels is True or result.is_merged:
        declared_format = (metadata.get("format") or "").strip().lower()
        if declared_format and declared_format != "png":
            result.errors.append(
                f"合成地图必须使用 PNG（注记依赖透明通道），实际 format={declared_format!r}"
            )


def _check_tiles(
    reader: MBTilesReader,
    result: MBTilesValidationResult,
    sample_limit: int,
) -> None:
    """第 5~6 项：非空、zoom 区间合理、抽样数据合法。"""
    try:
        tile_count = reader.get_tile_count()
    except sqlite3.DatabaseError as exc:
        result.errors.append(f"无法统计瓦片：{exc}")
        return
    result.tile_count = tile_count
    if tile_count <= 0:
        result.errors.append("tiles 表为空（没有任何瓦片）")
        return

    try:
        zoom_range = reader.get_zoom_range()
    except sqlite3.DatabaseError as exc:
        result.errors.append(f"无法读取 zoom 区间：{exc}")
        return
    if zoom_range is None:
        result.errors.append("无法确定 zoom 区间")
        return
    result.min_zoom, result.max_zoom = zoom_range
    if not (
        TILE_MATH_MIN_ZOOM <= zoom_range[0] <= zoom_range[1] <= TILE_MATH_MAX_ZOOM
    ):
        result.errors.append(
            f"zoom 区间不合理：{zoom_range[0]}~{zoom_range[1]}"
        )

    _check_sample_tiles(reader, result, sample_limit)


def _check_sample_tiles(
    reader: MBTilesReader,
    result: MBTilesValidationResult,
    sample_limit: int,
) -> None:
    """抽样检查 tile_data：非空 + 图片 magic 合法。"""
    try:
        samples = reader.sample_tiles(sample_limit)
    except ValueError as exc:
        # 库里存在越界的 z/x/y —— 文件被写坏了
        result.errors.append(f"tiles 表存在非法瓦片编号：{exc}")
        return
    except sqlite3.DatabaseError as exc:
        result.errors.append(f"无法抽样读取瓦片：{exc}")
        return

    if not samples:
        result.errors.append("抽样结果为空（tiles 表读不出任何瓦片）")
        return

    empty_tiles: list[str] = []
    invalid_tiles: list[str] = []
    mismatched_formats: set[str] = set()

    for z, x, y, data in samples:
        coordinate = f"{z}/{x}/{y}"
        if not data:
            empty_tiles.append(coordinate)
            continue
        detected = detect_image_format(data)
        if detected is None:
            invalid_tiles.append(coordinate)
        else:
            mismatched_formats.add(detected)

    if empty_tiles:
        result.errors.append(
            f"抽样 {len(samples)} 张中有 {len(empty_tiles)} 张 tile_data 为空："
            f"{', '.join(empty_tiles[:_MAX_REPORTED_BAD_SAMPLES])}"
        )
    if invalid_tiles:
        result.errors.append(
            f"抽样 {len(samples)} 张中有 {len(invalid_tiles)} 张不是合法图片"
            f"（magic bytes 既不是 PNG 也不是 JPEG）："
            f"{', '.join(invalid_tiles[:_MAX_REPORTED_BAD_SAMPLES])}"
        )
    if mismatched_formats and not invalid_tiles and not empty_tiles:
        result.warnings.append(
            f"抽样 {len(samples)} 张的图片格式为 {sorted(mismatched_formats)}"
            "，请确认与 metadata.format 一致"
        )
    if result.is_merged:
        # 合成产物统一 PNG（Spec §4）：即使底图来源是 JPEG，合成后也必须是 PNG。
        # 出现别的格式说明有瓦片没走合成链路（例如被单层下载器直接写进来了）。
        non_png = mismatched_formats - {"PNG"}
        if non_png:
            result.errors.append(
                "合成地图（composition=merged）的瓦片必须是 PNG，"
                "抽样中出现了：" + "、".join(sorted(non_png))
            )
    result.warnings.append(
        f"已抽样检查 {len(samples)} 张瓦片（共 {result.tile_count:,} 张，"
        f"大小 {format_size(result.file_size)}）"
    )
