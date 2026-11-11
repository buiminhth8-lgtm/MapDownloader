"""资产身份与内容摘要（Slice 42）。

仓库里的一条瓦片由**三个互相独立**的东西定位（契约见
``docs/tile_repository_contracts.md`` §1.1）：

============================  ==========================================
``AssetId`` + ``CoordKey``    这张瓦片画的是地球上的哪一块、哪一层
``epoch``                     这个坐标上的内容处于第几代（写入批次）
``ContentDigest``             字节到底是哪一份（sha256 + 长度 + 编码）
============================  ==========================================

三条硬约定，写在这里是为了让改代码的人先看到：

1. **坐标一律 XYZ**。TMS 只允许出现在写 MBTiles 的那一行（Slice 45 的 exporter）。
   本模块没有、也不该有 ``xyz_y_to_tms_y`` —— 仓库层多一次翻转，导出期就会少一次
   或多次，而二次翻转的表现是"地图上下颠倒"，只能靠人肉看图发现。
2. **token 不进身份**。``AssetId`` 只有 ``source_id``（产品标识，如 ``tianditu``）
   与 ``layer``；``sources.config_fingerprint`` 也不含 token。
3. **编码（``png``/``jpg``）不属于坐标身份**，它是内容实体的属性 —— 同一坐标换了
   编码，字节必然不同，于是自然走"同坐标不同内容"的冲突/更新规则。

校验规则**复用项目现有实现**，不另造一套：magic bytes 走
:func:`app.providers.tianditu.detect_image_format`，深度解码走
:func:`app.providers.image_validation.verify_tile_image`。这两处是"什么叫一张
合法瓦片"的唯一出处（下载侧与修复侧都在用它们）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum, StrEnum
from io import BytesIO

from app.config.settings import TILE_REPO_MAX_ZOOM, TILE_REPO_TILE_SIZE
from app.models.map_layer import MapLayerType
from app.providers.tianditu import detect_image_format

__all__ = [
    "ASSET_KIND_VALUES",
    "MAX_ZOOM",
    "AssetId",
    "AssetKind",
    "ContentDigest",
    "CoordKey",
    "CoordState",
    "LookupStatus",
    "TileLookup",
    "TileRecord",
    "classify_asset_kind",
    "make_digest",
    "validate_tile_bytes",
]

#: 仓库接受的 zoom 上界（与 ``app.geo.tile_math`` 同源，避免两处各写一个 22）。
MAX_ZOOM: int = TILE_REPO_MAX_ZOOM

#: 合法的图层名 = :class:`~app.models.map_layer.MapLayerType` 的取值。
#: 刻意从枚举里取，而不是在这里抄一份 ``{'vec_w', ...}``：抄一份就会漂移。
LAYER_VALUES: frozenset[str] = frozenset(item.value for item in MapLayerType)

#: 内容编码取值。与下载侧 ``detect_image_format`` 的返回值对应（小写化后）。
ENCODING_VALUES: frozenset[str] = frozenset({"png", "jpg"})

_ENCODING_BY_FORMAT: dict[str, str] = {"PNG": "png", "JPEG": "jpg"}


# --------------------------------------------------------------------------- #
# 资产身份
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, order=True)
class AssetId:
    """一条"逻辑流"的身份：``数据源/图层``。

    ``projection`` 与 ``tile_size`` 刻意**不在**这里：它们是数据源的属性，
    同一数据源内不会变。把它们塞进每一行，等于在几千万行里重复一个恒定值，
    而且会诱导调用方"用投影去区分资产"—— 换投影就是换数据源，不该走到那条路。

    Attributes:
        source_id: 产品级标识（``tianditu``），**不是**凭证。
        layer: WMTS 图层名，取值见 :data:`LAYER_VALUES`。

    Raises:
        ValueError: ``source_id`` 为空/含斜杠，或 ``layer`` 不在白名单内
            （这是调用方的编程错误，必须当场暴露，不能兜底成某个默认图层）。
    """

    source_id: str
    layer: str

    def __post_init__(self) -> None:
        source = str(self.source_id).strip()
        layer = str(self.layer).strip()
        if not source:
            raise ValueError("source_id 不能为空")
        if "/" in source or "\\" in source:
            raise ValueError(f"source_id 不能包含斜杠：{source!r}（它会进目录名）")
        if layer not in LAYER_VALUES:
            raise ValueError(
                f"未知图层 {layer!r}；只接受 {sorted(LAYER_VALUES)}"
            )
        object.__setattr__(self, "source_id", source)
        object.__setattr__(self, "layer", layer)

    @property
    def text(self) -> str:
        """``'tianditu/vec_w'`` —— 落在 catalog 与报告里的规范写法。

        >>> AssetId('tianditu', 'vec_w').text
        'tianditu/vec_w'
        """
        return f"{self.source_id}/{self.layer}"

    @property
    def layer_type(self) -> MapLayerType:
        """对应的枚举成员（拿 ``is_label`` 之类的语义）。"""
        return MapLayerType(self.layer)

    @property
    def role(self) -> str:
        """``'base'``（底图）或 ``'label'``（注记）。

        底图与注记**永不互相覆盖**，即使 ``z/x/y`` 完全相同 —— 它们在同一坐标系
        的同一格上，但画的是两件事。仓库用不同的 ``AssetId``（不同分片文件）
        隔离它们，因此这条语义是结构性的，不靠写入侧的 if 判断。
        """
        return "label" if self.layer_type.is_label else "base"

    @property
    def is_label(self) -> bool:
        return self.layer_type.is_label

    @classmethod
    def parse(cls, value: str | AssetId) -> AssetId:
        """严格解析 ``'source/layer'``。

        Raises:
            ValueError: 格式不对或图层未知。
        """
        if isinstance(value, AssetId):
            return value
        text = str(value).strip()
        source, sep, layer = text.partition("/")
        if not sep or not source or not layer:
            raise ValueError(f"资产标识应形如 'tianditu/vec_w'，实际为 {value!r}")
        if "/" in layer:
            raise ValueError(f"资产标识里只能有一个斜杠，实际为 {value!r}")
        return cls(source, layer)

    @classmethod
    def try_parse(cls, value: str | AssetId | None) -> AssetId | None:
        """宽松解析：认不出来返回 ``None``（给"从磁盘读回来的记录"用）。"""
        if value is None:
            return None
        try:
            return cls.parse(value)
        except ValueError:
            return None


# --------------------------------------------------------------------------- #
# 坐标身份
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, order=True)
class CoordKey:
    """一个 XYZ 瓦片坐标（**不是** TMS）。

    Attributes:
        z: 缩放级别，``0 ~ 22``。
        x: 列号，``0 ~ 2**z - 1``。
        y: 行号（XYZ 语义：0 在最北），``0 ~ 2**z - 1``。

    Raises:
        ValueError: 越界。越界坐标**不入仓**（导入侧把它记进 ledger 后跳过），
            因为一张 ``z=25`` 的瓦片没有任何下游能解释它。
    """

    z: int
    x: int
    y: int

    def __post_init__(self) -> None:
        zoom = _require_int("z", self.z)
        column = _require_int("x", self.x)
        row = _require_int("y", self.y)
        if not 0 <= zoom <= MAX_ZOOM:
            raise ValueError(f"z 必须在 [0, {MAX_ZOOM}] 之间，实际为 {zoom}")
        limit = 1 << zoom
        if not 0 <= column < limit:
            raise ValueError(f"x 必须在 [0, {limit - 1}] 之间（z={zoom}），实际为 {column}")
        if not 0 <= row < limit:
            raise ValueError(f"y 必须在 [0, {limit - 1}] 之间（z={zoom}），实际为 {row}")
        object.__setattr__(self, "z", zoom)
        object.__setattr__(self, "x", column)
        object.__setattr__(self, "y", row)

    @property
    def tile_count(self) -> int:
        """该级别每轴的瓦片数（``2**z``）。"""
        return 1 << self.z

    def as_tuple(self) -> tuple[int, int, int]:
        return (self.z, self.x, self.y)


def _require_int(name: str, value: object) -> int:
    """严格取整：``bool`` 与浮点都不接受。

    ``True`` 是 ``int`` 的子类，``True == 1`` —— 放进坐标会得到一个"看起来对"
    的瓦片，排查起来极费劲。
    """
    if type(value) is not int:
        raise ValueError(f"{name} 必须是整数（不接受 bool/float），实际类型为 {type(value).__name__}")
    return value


# --------------------------------------------------------------------------- #
# 内容摘要
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ContentDigest:
    """一份内容实体的身份：``sha256`` + 长度 + 编码。

    Attributes:
        sha256: 32 字节原始摘要（不是 hex 字符串 —— 分片里按 BLOB 存，省一半空间）。
        size: 字节数，必须等于 ``len(data)``。
        encoding: ``'png'`` 或 ``'jpg'``，必须与字节的 magic bytes 一致。

    Raises:
        ValueError: 摘要不是 32 字节、长度非正、编码未知。
    """

    sha256: bytes
    size: int
    encoding: str

    def __post_init__(self) -> None:
        digest = bytes(self.sha256)
        if len(digest) != 32:
            raise ValueError(f"sha256 必须是 32 字节，实际为 {len(digest)}")
        if type(self.size) is not int or self.size <= 0:
            raise ValueError(f"size 必须是正整数，实际为 {self.size!r}")
        if self.encoding not in ENCODING_VALUES:
            raise ValueError(
                f"编码必须是 {sorted(ENCODING_VALUES)} 之一，实际为 {self.encoding!r}"
            )
        object.__setattr__(self, "sha256", digest)

    @property
    def hex(self) -> str:
        """64 位小写 hex（写进报告与快照清单的形态）。"""
        return self.sha256.hex()

    @classmethod
    def from_bytes(cls, data: bytes) -> ContentDigest:
        """从字节算出摘要（**不做**合法性校验，校验见 :func:`validate_tile_bytes`）。

        >>> ContentDigest.from_bytes(b'\\x89PNG\\r\\n\\x1a\\n' + b'0' * 8).encoding
        'png'
        """
        raw = bytes(data)
        encoding = _ENCODING_BY_FORMAT.get(detect_image_format(raw) or "")
        if encoding is None:
            raise ValueError("字节的 magic bytes 既不是 PNG 也不是 JPEG")
        return cls(hashlib.sha256(raw).digest(), len(raw), encoding)


def make_digest(data: bytes) -> ContentDigest:
    """``ContentDigest.from_bytes`` 的函数式别名（调用点更短）。"""
    return ContentDigest.from_bytes(data)


# --------------------------------------------------------------------------- #
# 瓦片校验（复用项目既有规则）
# --------------------------------------------------------------------------- #
def validate_tile_bytes(
    data: bytes | None,
    *,
    tile_size: int = TILE_REPO_TILE_SIZE,
    deep: bool = False,
) -> ContentDigest:
    """校验"这是一张能进仓库的瓦片"，返回它的摘要。

    两级，与项目现有口径一致（下载侧只做快检、修复侧才做深检）:

    1. **快检**（始终做）：非空 → magic bytes 是 PNG/JPEG → 头部尺寸等于
       ``tile_size``。``Image.open`` 只解析头部，不解像素，单张耗时可忽略。
    2. **深检**（``deep=True``）：Pillow ``verify()`` + ``load()``。用于导入验收
       与修复复核；批量写入默认不开（几万张全解码会比下载还慢）。

    Args:
        data: 瓦片字节。
        tile_size: 期望边长（来自数据源规格，默认 256）。
        deep: 是否做完整的容器与像素校验。

    Returns:
        校验通过的内容摘要。

    Raises:
        InvalidTile: 空、magic bytes 不对、尺寸不符、深检解码失败。
    """
    from app.tilerepo.errors import InvalidTile  # 循环导入规避：errors 不依赖本模块

    if not data:
        raise InvalidTile("瓦片字节为空")
    raw = bytes(data)
    try:
        digest = ContentDigest.from_bytes(raw)
    except ValueError as exc:
        raise InvalidTile(str(exc)) from exc

    width, height = _probe_size(raw)
    if tile_size and (width, height) != (tile_size, tile_size):
        raise InvalidTile(
            f"瓦片尺寸应为 {tile_size}×{tile_size}，实际为 {width}×{height}"
        )

    if deep:
        try:
            from app.providers.image_validation import verify_tile_image

            verify_tile_image(raw)
        except Exception as exc:  # noqa: BLE001 - 任何解码失败都算"这张瓦片坏了"
            raise InvalidTile(f"深度解码失败：{exc}") from exc
    return digest


def _probe_size(data: bytes) -> tuple[int, int]:
    """读图片头部尺寸（不解像素）。"""
    from PIL import Image

    try:
        with Image.open(BytesIO(data)) as image:
            return image.size
    except Exception as exc:  # noqa: BLE001 - 截断的头部同样算不合法
        from app.tilerepo.errors import InvalidTile

        raise InvalidTile(f"无法读取图片尺寸：{exc}") from exc


# --------------------------------------------------------------------------- #
# 状态与查询结果
# --------------------------------------------------------------------------- #
class CoordState(Enum):
    """``coords.state`` 的取值。**数值是持久化契约**，不能改。"""

    CURRENT = 0
    SUPERSEDED = 1
    QUARANTINED = 2

    @property
    def label(self) -> str:
        return {
            CoordState.CURRENT: "current",
            CoordState.SUPERSEDED: "superseded",
            CoordState.QUARANTINED: "quarantined",
        }[self]

    @classmethod
    def parse(cls, value: object) -> CoordState:
        """由数据库里的整数还原；认不出来抛 ``ValueError``。"""
        try:
            return cls(int(value))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"未知的坐标状态：{value!r}") from exc


class LookupStatus(StrEnum):
    """单张瓦片查询的结果分类。

    刻意**不用异常**表达"没有"：缺口计算要逐坐标循环，把缺图抛成异常会让
    正常路径变成 try/except 迷宫，而且丢掉"为什么没有"这个信息。

    - ``PRESENT``：读到内容，``record`` 已填。
    - ``ABSENT``：这个坐标从来没有过。
    - ``UNPUBLISHED``：内容已写入分片但**尚未发布**（写入中断/取消/正在提交）。
      查询要能把它与"根本没有"分开，否则崩在半路的写入会被当成缺口重下。
    - ``QUARANTINED``：坐标存在但当前版本被判坏（深检失败），等待修复。
    - ``CORRUPT``：索引指向的内容不在了，或摘要对不上。要报警，不能当缺图。
    """

    PRESENT = "present"
    ABSENT = "absent"
    UNPUBLISHED = "unpublished"
    QUARANTINED = "quarantined"
    CORRUPT = "corrupt"


@dataclass(frozen=True)
class TileRecord:
    """一张读出来的瓦片（内容已载入内存，调用方用完即弃）。"""

    asset: AssetId
    coord: CoordKey
    digest: ContentDigest
    epoch: int
    state: CoordState
    fetched_at: float
    data: bytes

    @property
    def size(self) -> int:
        return self.digest.size


@dataclass(frozen=True)
class TileLookup:
    """单点查询结果。

    Attributes:
        status: 见 :class:`LookupStatus`。
        record: ``status is PRESENT`` 时有值。
        reason: 人读的原因（``CORRUPT`` 时说明是缺内容还是摘要不符）。
    """

    status: LookupStatus
    record: TileRecord | None = None
    reason: str = ""

    @property
    def present(self) -> bool:
        return self.status is LookupStatus.PRESENT


# --------------------------------------------------------------------------- #
# 资产类型：原始图层 vs 合成资产
# --------------------------------------------------------------------------- #
class AssetKind(StrEnum):
    """资产类型（契约 §3.1）。

    只有 ``RAW_*`` 能进仓库的"当前代"；``SYNTHETIC_*`` 是合成产物，
    **不可拆层**（``alpha_composite`` 之后没有反向路径），只能整块复用。
    """

    RAW_BASE = "raw_base"
    RAW_LABEL = "raw_label"
    SYNTHETIC_SINGLE = "synthetic_single"
    SYNTHETIC_MERGED = "synthetic_merged"
    UNKNOWN = "unknown"

    @property
    def is_raw(self) -> bool:
        return self in (AssetKind.RAW_BASE, AssetKind.RAW_LABEL)

    @property
    def is_synthetic(self) -> bool:
        return self in (AssetKind.SYNTHETIC_SINGLE, AssetKind.SYNTHETIC_MERGED)


#: 供报告/文档遍历用的稳定顺序
ASSET_KIND_VALUES: tuple[str, ...] = tuple(item.value for item in AssetKind)


def classify_asset_kind(metadata: dict[str, str] | None) -> AssetKind:
    """按 MBTiles metadata 判定资产类型（契约 §3.2 的判定式）。

    这是**纯函数**，Slice 42 不导入任何旧文件（那是 Slice 43），但把它放在这里
    是为了让"旧包分类"只有一个实现 —— 43 的导入器直接调用，不再抄一遍。

    兜底方向是刻意的：判不出来就当**合成资产**。反过来的代价是"给已经带字的图
    再叠一层字"，错得看不出来；而这个方向的代价只是错过一次复用机会。

    >>> classify_asset_kind({'composition': 'merged', 'labels': 'true'})
    <AssetKind.SYNTHETIC_MERGED: 'synthetic_merged'>
    >>> classify_asset_kind(None)
    <AssetKind.UNKNOWN: 'unknown'>
    """
    if not metadata:
        return AssetKind.UNKNOWN
    composition = str(metadata.get("composition") or "").strip()
    labels = str(metadata.get("labels") or "").strip().lower() == "true"
    map_type = str(metadata.get("map_type") or "").strip()
    if composition == "merged" and labels:
        return AssetKind.SYNTHETIC_MERGED
    if composition == "single" and not labels:
        return AssetKind.SYNTHETIC_SINGLE
    if not composition or not map_type:
        return AssetKind.UNKNOWN
    return AssetKind.SYNTHETIC_MERGED
