"""离线地图包的数据模型与 manifest 格式定义（Slice 9，Spec §2 / §3 / §10 / §13）。

本模块是整个 Slice 里**唯一**定义"manifest.json 长什么样"的地方
------------------------------------------------------------------

字段名（camelCase）、嵌套结构、字段顺序、默认值、合法性边界，全部在这里。
``package_builder`` 只负责把对象写成文件，``package_reader`` 只负责把文件读成
对象，``package_validator`` 只负责比对 —— 三边都不自己拼字段名。
这条规矩的价值在于：Android 端按名字解析 JSON，字段名一旦在三个文件里各写一份，
迟早会出现"builder 写 ``mapType``、reader 读 ``map_type``"这种只能靠联调发现的错误。

为什么 manifest 是**协议**而不是"我们自己的一份配置"
----------------------------------------------------

Spec §25 要求它"简单、稳定、Android 易解析"，因此这里刻意做几件事：

- 只写 JSON 原生类型（``str`` / ``int`` / ``float`` / ``bool`` / ``null`` / 嵌套对象），
  **不写** Python 特有的 tuple / set / ``NaN`` / ``Infinity``
  （``json.dumps`` 会把 ``float('nan')`` 写成裸 ``NaN``，那不是合法 JSON，
  Android 的严格解析器会直接拒绝 —— 本 Slice 的字段校验把这条路堵死）；
- ``mbtiles.file`` **只允许包内的相对文件名**，绝不写 ``D:\\xxx\\map.mbtiles``
  这类绝对路径（那种包换个系统就打不开了）；
- ``formatVersion`` 单独一个字段，Android 侧 ``if (formatVersion > supported) reject``。

坐标与缩放区间直接复用 :class:`~app.geo.GeoBounds` 与 Slice 0 的缩放常量做校验，
不另写一套边界 —— 本项目里"同一件事两套判定"从来都是 bug 的温床。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.config.settings import (
    PACKAGE_DEFAULT_PROVIDER,
    PACKAGE_DEFAULT_TILE_SCHEME,
    PACKAGE_FORMAT_VERSION,
    PACKAGE_SHA256_LENGTH,
    TILE_MATH_MAX_ZOOM,
    TILE_MATH_MIN_ZOOM,
    UI_TEXT_PACKAGE_UNKNOWN,
)
from app.geo import GeoBounds, format_size
from app.mbtiles import SUPPORTED_TILE_FORMATS

__all__ = [
    "MANIFEST_BOUNDS_FIELDS",
    "MANIFEST_FIELD_ORDER",
    "MANIFEST_MBTILES_FIELDS",
    "MANIFEST_REGION_FIELDS",
    "ManifestError",
    "ManifestPathError",
    "MapPackageManifest",
    "PackageAborted",
    "PackageBuildResult",
    "PackageError",
    "PackageProgress",
    "PackageRegion",
    "PackageValidationResult",
    "UnsupportedFormatVersion",
    "now_iso",
    "parse_manifest_dict",
    "render_manifest_json",
]


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class PackageError(RuntimeError):
    """地图包相关错误的基类。

    单独一棵异常树（而不是复用 ``OSError`` / ``ValueError``）的理由：GUI 需要把
    "这个包读不出来"与"磁盘写失败了"分成完全不同的措辞，用户据此决定是换个包
    还是查磁盘。测试也靠类型精确断言，不靠匹配文案。
    """


class ManifestError(PackageError):
    """``manifest.json`` 缺失、不是合法 JSON，或字段非法（Spec §11）。"""


class ManifestPathError(ManifestError):
    """``mbtiles.file`` 指向了包目录之外（Spec §11 的路径穿越）。

    单独一个类型：它不是"字段写错了"，而是**一份有敌意的 manifest**。
    调用方（尤其是未来从网上下包的那条链路）可能需要对它做不同的处置。
    """


class UnsupportedFormatVersion(ManifestError):
    """``formatVersion`` 高于本工具支持的版本（Spec §26）。"""


class PackageAborted(PackageError):
    """操作被调用方主动中止（用户关掉了对话框）。

    单独一个类型：它不是"失败"，不该在界面上显示成红色的错误，也不该被
    计进"打包失败"的统计里。构建器捕获它之后只做一件事：把 ``.building``
    临时目录清掉，然后如实回报"已中止"。
    """


# --------------------------------------------------------------------------- #
# manifest 的字段定义
# --------------------------------------------------------------------------- #
#: 顶层字段的书写顺序。**固定**，不是"随便排"：manifest 是给人看、给 diff 比、
#: 给 Android 解析的文件，同一份数据每次生成出来的字节序都不同是无法接受的
#: （打包脚本做增量比对时会全军覆没）。
MANIFEST_FIELD_ORDER: tuple[str, ...] = (
    "formatVersion",
    "id",
    "name",
    "description",
    "provider",
    "mapType",
    "labels",
    "composition",
    "tileScheme",
    "tileFormat",
    "minZoom",
    "maxZoom",
    "bounds",
    "region",
    "tileCount",
    "mbtiles",
    "createdAt",
)

#: ``region`` 子对象的字段顺序（Slice 16；Slice 22 追加三个可选键）。
#:
#: 与 ``description`` 同属**可选**字段：旧包没有它，读到 ``None`` 时按矩形处理
#: （Spec §21/§29）。因此它虽然进了 :data:`MANIFEST_FIELD_ORDER`（决定书写位置），
#: 却**不**是必填项 —— 判定"哪些字段必填"的表在协议测试里，那边把它排除掉。
#:
#: ``level`` / ``bufferMeters`` / ``geometryVersion``（Slice 22）只对**任务区**
#: 出现：行政区包多写一个"等级"是噪声（等级能从 ``id`` 反查出来），而任务区的
#: 几何在用户数据目录里、反查不出来，必须把这个事实落在包里（Spec §19）。
MANIFEST_REGION_FIELDS: tuple[str, ...] = (
    "type",
    "id",
    "boundaryVersion",
    "level",
    "bufferMeters",
    "geometryVersion",
    "coverage",
)

#: ``bounds`` 子对象的字段顺序（west, south, east, north —— 与 MBTiles 的
#: ``bounds`` 字符串同序，避免两处顺序不一致时靠人眼核对）
MANIFEST_BOUNDS_FIELDS: tuple[str, ...] = ("west", "south", "east", "north")

#: ``mbtiles`` 子对象的字段顺序
MANIFEST_MBTILES_FIELDS: tuple[str, ...] = ("file", "size", "sha256")

#: ``tileFormat`` 允许的值（复用 MBTiles 侧那一份，别另立一套）
_PACKAGE_TILE_FORMATS = frozenset(value for value in SUPPORTED_TILE_FORMATS)

#: ``id`` 的合法形态：小写字母 / 数字 / 下划线 / 连字符。
#: 它会被 Android 当作包标识（放在目录名、SharedPreferences 键里），
#: 允许空格和中文等于把编码问题留给下游。
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: 相对文件名的合法形态：不允许任何路径分隔符、盘符与 ``..``（Spec §25）。
#: 只允许"一个普通的文件名"，这比"解析后判断有没有跑出包目录"更早、更狠。
_BARE_FILENAME_PATTERN = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]+$")

_HEX_SHA256_PATTERN = re.compile(rf"^[0-9a-fA-F]{{{PACKAGE_SHA256_LENGTH}}}$")


def now_iso() -> str:
    """当前本地时间的 ISO 8601 字符串（带时区偏移），如 ``2026-09-16T09:00:00+08:00``。

    刻意**不用** ``datetime.utcnow()``（naive，且已废弃）：manifest 里的时间给
    人和 Android 看，缺时区的时间戳在跨时区场景下就是个谜。
    """
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- #
# 区域信息（Slice 16，Spec §21）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PackageRegion:
    """manifest 里**可选**的 ``region`` 子对象。

    它记录"这个包是按哪块区域裁出来的"，让下游不必打开 MBTiles 就能知道
    "为什么实际覆盖范围比 ``bounds`` 小"。

    "可选"是硬要求：旧包（Slice ≤15）没有这个字段，读到时是 ``None``，
    按矩形处理 —— 那时候的包本来就是按 bounding box 下的。

    Attributes:
        region_type: ``rectangle`` / ``polygon``。
        region_id: 行政区标识（``china``）或任务区标识（``mission-001``）；
            矩形为 ``None``。
        boundary_version: 边界数据版本；未知为 ``None``。任务区的几何版本号
            也走这个字段（下载链路上它们的语义完全相同：**这批瓦片是按哪一版
            几何裁的**）。
        level: 区域等级（``country`` / ``province`` / ``city`` / ``county`` /
            ``mission`` / ``custom``）。**只要有等级就写**（Slice 25 起）：它是跨端契约，
            Android 侧没有区域目录、反查不出等级，靠这个键才能排"哪一级更贴地"
            （Slice 22 只给任务区写，见 ``app/geo/region.py::metadata_rows``
            的说明）。老包没有这个键 → 读到时为 ``None``。
        buffer_meters: 任务区走廊半径（米）；非任务区为 ``None``。
        geometry_version: 任务区几何版本号；非任务区为 ``None``。

    取值刻意**不做白名单校验**（只做归一化）：manifest 由本工具生成，
    但也会被别的工具/未来版本读。一个写着 ``multipolygon`` 的包仍然是
    可以加载的包 —— 拒收它属于"因为一个附加信息不认识就否掉整个交付物"，
    正是 Spec §22 要求避免的。
    """

    region_type: str = "rectangle"
    region_id: str | None = None
    boundary_version: str | None = None
    level: str | None = None
    buffer_meters: float | None = None
    geometry_version: str | None = None
    coverage: dict | None = None

    def __post_init__(self) -> None:
        # 空串 / 空白都落回 rectangle：连 ``{"region": {}}`` 这种残缺写法
        # 也按最保守的解释处理（矩形 = 只多下、不少下）。
        normalized = str(self.region_type or "").strip().lower() or "rectangle"
        object.__setattr__(self, "region_type", normalized)
        region_id = str(self.region_id).strip() if self.region_id else ""
        object.__setattr__(self, "region_id", region_id or None)
        version = str(self.boundary_version).strip() if self.boundary_version else ""
        object.__setattr__(self, "boundary_version", version or None)
        level = str(self.level).strip().lower() if self.level else ""
        object.__setattr__(self, "level", level or None)
        geometry_version = (
            str(self.geometry_version).strip() if self.geometry_version else ""
        )
        object.__setattr__(self, "geometry_version", geometry_version or None)
        if self.buffer_meters is None:
            object.__setattr__(self, "buffer_meters", None)
        else:
            try:
                radius = float(self.buffer_meters)
            except (TypeError, ValueError) as exc:
                raise ManifestError(
                    f"manifest.region.bufferMeters 不是数字：{self.buffer_meters!r}"
                ) from exc
            object.__setattr__(self, "buffer_meters", radius)

    def to_dict(self) -> dict[str, Any]:
        """转成 JSON 结构（Slice 16 的三个键 + 区域等级等可选键）。

        空字段**不写**：``"id": null`` 与"没有 id"在 Android 侧是两种语义
        （前者可能被反序列化成字符串 ``"null"``），与 ``description`` 同一处理。
        键的**出现与否**因此完全由"这个包知不知道自己这一项"决定 ——
        老包（Slice ≤21）里 ``level`` 不会出现，读到时按"没有等级信息"处理。
        """
        data: dict[str, Any] = {"type": self.region_type}
        if self.coverage is not None:
            if not isinstance(self.coverage, dict):
                raise ManifestError("manifest.region.coverage 必须是对象")
            data["coverage"] = self.coverage
        if self.region_id:
            data["id"] = self.region_id
        if self.boundary_version:
            data["boundaryVersion"] = self.boundary_version
        if self.level:
            data["level"] = self.level
        if self.buffer_meters is not None:
            data["bufferMeters"] = self.buffer_meters
        if self.geometry_version:
            data["geometryVersion"] = self.geometry_version
        return {key: data[key] for key in MANIFEST_REGION_FIELDS if key in data}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PackageRegion:
        """从 JSON 结构还原。

        Raises:
            ManifestError: ``region`` 不是 JSON 对象。
        """
        if not isinstance(data, Mapping):
            raise ManifestError(
                f"manifest.region 必须是 JSON 对象，实际为 {type(data).__name__}"
            )
        return cls(
            region_type=str(data.get("type", "") or ""),
            region_id=data.get("id"),
            boundary_version=data.get("boundaryVersion"),
            level=data.get("level"),
            buffer_meters=data.get("bufferMeters"),
            geometry_version=data.get("geometryVersion"),
            coverage=data.get("coverage"),
        )

    @classmethod
    def for_rectangle(cls) -> PackageRegion:
        """矩形包的区域记录（Spec §21：矩形也要显式写出来）。"""
        return cls(region_type="rectangle")


# --------------------------------------------------------------------------- #
# 地图包 manifest
# --------------------------------------------------------------------------- #
@dataclass
class MapPackageManifest:
    """离线地图包的 ``manifest.json``（Spec §2）。

    Attributes:
        format_version: manifest 格式版本，当前恒为 ``1``（Spec §26）。
        id: 包的稳定标识（小写字母 / 数字 / ``_`` / ``-``），Android 侧当主键用。
        name: 人类可读名称，也是默认的包目录名。
        provider: 瓦片来源，本工具固定 ``"tianditu"``。
        map_type: 地图类型（``vector`` / ``image``），来自 MBTiles metadata。
        labels: 是否包含中文注记层。
        composition: ``single``（单图层）/ ``merged``（底图与注记已合成）。
        tile_scheme: 瓦片坐标方案，固定 ``"web_mercator"``。
        tile_format: 瓦片图片格式（``png`` / ``jpg`` / ``jpeg``）。
        min_zoom / max_zoom: 覆盖的缩放区间（含两端）。
        west / south / east / north: 覆盖范围（WGS84）。
        tile_count: 包内瓦片总数。
        mbtiles_file: 包内 MBTiles 的**相对文件名**，按 Spec §25 恒为 ``map.mbtiles``。
        mbtiles_size: MBTiles 的字节数。
        mbtiles_sha256: MBTiles 的 SHA256（64 位小写 hex，Spec §4）。
        created_at: 生成时间（ISO 8601，带时区）。
        description: 可选描述；``None`` 表示"不写这个字段"。

    Raises:
        ManifestError: 任何字段非法（空串、缩放越界、范围非法、SHA256 形态不对、
            文件名带路径、时间不可解析……）。
        ManifestPathError: ``mbtiles_file`` 试图指向包外。
        UnsupportedFormatVersion: ``format_version`` 小于 1。
    """

    format_version: int

    id: str
    name: str

    provider: str
    map_type: str
    labels: bool
    composition: str

    tile_scheme: str
    tile_format: str

    min_zoom: int
    max_zoom: int

    west: float
    south: float
    east: float
    north: float

    tile_count: int

    mbtiles_file: str
    mbtiles_size: int
    mbtiles_sha256: str

    created_at: str

    description: str | None = None

    #: 区域信息（Slice 16，Spec §21）。``None`` = "不写这个字段"
    #: （与 :attr:`description` 同一约定）；**旧包没有它，读到 ``None``
    #: 时一律按矩形处理**。直接构造函数时也可以传 dict，``__post_init__``
    #: 会转成 :class:`PackageRegion`。
    region: PackageRegion | None = None

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #
    def __post_init__(self) -> None:
        self.format_version = _require_int(self.format_version, "formatVersion")
        if self.format_version < 1:
            raise UnsupportedFormatVersion(
                f"formatVersion 必须 >= 1，实际为 {self.format_version}"
            )

        self.id = _require_text(self.id, "id")
        if not _ID_PATTERN.match(self.id):
            raise ManifestError(
                f"id 只能由小写字母、数字、下划线、连字符组成且以字母或数字开头，"
                f"实际为 {self.id!r}"
            )

        self.name = _require_text(self.name, "name")
        self.provider = _require_text(self.provider, "provider")
        self.tile_scheme = _require_text(self.tile_scheme, "tileScheme")

        self.map_type = _require_text(self.map_type, "mapType").lower()
        self.composition = _require_text(self.composition, "composition").lower()

        if not isinstance(self.labels, bool):
            raise ManifestError(f"labels 必须是布尔值，实际为 {self.labels!r}")

        self.tile_format = _require_text(self.tile_format, "tileFormat").lower()
        if self.tile_format not in _PACKAGE_TILE_FORMATS:
            raise ManifestError(
                f"tileFormat 必须是 {sorted(_PACKAGE_TILE_FORMATS)} 之一，"
                f"实际为 {self.tile_format!r}"
            )

        self.min_zoom = _require_zoom(self.min_zoom, "minZoom")
        self.max_zoom = _require_zoom(self.max_zoom, "maxZoom")
        if self.min_zoom > self.max_zoom:
            raise ManifestError(
                f"minZoom 不能大于 maxZoom，实际为 {self.min_zoom} > {self.max_zoom}"
            )

        # 借 GeoBounds 做完经纬度校验：±180 / ±85.05112878 / west<east / south<north。
        # 不在这里另写一遍——切片 0 已经把这条规则写死过一次了。
        self.west, self.south, self.east, self.north = _require_bounds(
            self.west, self.south, self.east, self.north
        )

        self.tile_count = _require_int(self.tile_count, "tileCount")
        if self.tile_count < 0:
            raise ManifestError(f"tileCount 不能为负，实际为 {self.tile_count}")

        self.mbtiles_file = _require_bare_filename(self.mbtiles_file, "mbtiles.file")

        self.mbtiles_size = _require_int(self.mbtiles_size, "mbtiles.size")
        if self.mbtiles_size < 0:
            raise ManifestError(f"mbtiles.size 不能为负，实际为 {self.mbtiles_size}")

        self.mbtiles_sha256 = _require_sha256(self.mbtiles_sha256, "mbtiles.sha256")

        self.created_at = _require_timestamp(self.created_at, "createdAt")

        if self.description is not None:
            self.description = str(self.description)

        # 区域信息（Slice 16）：允许直接传 dict（便于从 JSON 构造），
        # 归一化成 PackageRegion；``None`` 保持为 ``None``（= 不写这个字段）。
        if self.region is not None and not isinstance(self.region, PackageRegion):
            self.region = PackageRegion.from_dict(self.region)

    # ------------------------------------------------------------------ #
    # 派生信息
    # ------------------------------------------------------------------ #
    @property
    def bounds(self) -> GeoBounds:
        """范围的 :class:`~app.geo.GeoBounds` 视图。"""
        return GeoBounds(self.west, self.south, self.east, self.north)

    @property
    def bounds_text(self) -> str:
        """``west,south,east,north``，6 位小数（与 MBTiles metadata 同格式）。"""
        return (
            f"{self.west:.6f},{self.south:.6f},"
            f"{self.east:.6f},{self.north:.6f}"
        )

    @property
    def zoom_text(self) -> str:
        """``8~17`` / ``8``（区间只有一级时不写重复的数字）。"""
        if self.min_zoom == self.max_zoom:
            return str(self.min_zoom)
        return f"{self.min_zoom}~{self.max_zoom}"

    @property
    def size_text(self) -> str:
        """MBTiles 大小的可读形式（``1.42 GB``）。"""
        return format_size(self.mbtiles_size)

    @property
    def sha256_short(self) -> str:
        """SHA256 的前 12 位，给界面用（完整 64 位在界面上会把一行撑爆）。"""
        return self.mbtiles_sha256[:12]

    @property
    def is_merged(self) -> bool:
        """是否是"底图 + 中文注记已合成"的包（Spec §24）。"""
        return self.composition == "merged"

    # ------------------------------------------------------------------ #
    # 序列化
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        """转成 manifest 的 JSON 结构（camelCase + 嵌套子对象）。

        ``description`` 为 ``None`` 时**不写该键**：一个可选字段写成 ``null``
        与"没有这个字段"在 Android 侧是两种不同的语义（前者会被某些 Gson
        配置反序列化成 ``"null"`` 字符串）。Spec §22 也说了本 Slice 不产生
        ``preview`` 字段，同理不产生空描述。
        """
        data: dict[str, Any] = {
            "formatVersion": int(self.format_version),
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "mapType": self.map_type,
            "labels": bool(self.labels),
            "composition": self.composition,
            "tileScheme": self.tile_scheme,
            "tileFormat": self.tile_format,
            "minZoom": int(self.min_zoom),
            "maxZoom": int(self.max_zoom),
            "bounds": {
                "west": float(self.west),
                "south": float(self.south),
                "east": float(self.east),
                "north": float(self.north),
            },
            "tileCount": int(self.tile_count),
            "mbtiles": {
                "file": self.mbtiles_file,
                "size": int(self.mbtiles_size),
                "sha256": self.mbtiles_sha256,
            },
            "createdAt": self.created_at,
        }
        if self.description is not None:
            data["description"] = self.description
        if self.region is not None:
            # 与 description 同一约定：没有就不写这个键，而不是写 null。
            data["region"] = self.region.to_dict()
        # 按固定顺序重排：``dict`` 保序，但**必须**显式排一遍，
        # 否则以后往上面塞新键时会不小心写到末尾，破坏"同一份数据字节稳定"。
        return {key: data[key] for key in MANIFEST_FIELD_ORDER if key in data}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MapPackageManifest:
        """从 manifest 的 JSON 结构还原（Spec §11 的字段校验入口）。

        未知字段**忽略**（不报错）：未来版本加字段时，老解析器要能装作没看见，
        而不是把整个包装成一个坏包。版本兼容由 ``formatVersion`` 负责。

        Raises:
            ManifestError: 结构不是对象、字段缺失或类型不对。
            ManifestPathError: ``mbtiles.file`` 带路径（含 ``../``）。
            UnsupportedFormatVersion: ``formatVersion`` 超前。
        """
        if not isinstance(data, Mapping):
            raise ManifestError(f"manifest 顶层必须是 JSON 对象，实际为 {type(data).__name__}")

        bounds = _require_mapping(data.get("bounds"), "bounds")
        mbtiles = _require_mapping(data.get("mbtiles"), "mbtiles")

        version = _require_int(_require_key(data, "formatVersion"), "formatVersion")
        if version > PACKAGE_FORMAT_VERSION:
            raise UnsupportedFormatVersion(
                f"formatVersion={version} 高于本工具支持的 {PACKAGE_FORMAT_VERSION}"
            )

        description = data.get("description")
        if description is not None:
            description = str(description)

        # 区域信息（Slice 16）：**旧包没有这个键** → ``None``，上层按矩形处理
        # （Spec §21/§29：不破坏 Slice 15 已固化的旧包兼容性）。
        region_payload = data.get("region")
        region = (
            PackageRegion.from_dict(region_payload)
            if region_payload is not None
            else None
        )

        return cls(
            format_version=version,
            id=_require_text(_require_key(data, "id"), "id"),
            name=_require_text(_require_key(data, "name"), "name"),
            provider=_require_text(data.get("provider", PACKAGE_DEFAULT_PROVIDER), "provider"),
            map_type=_require_text(_require_key(data, "mapType"), "mapType"),
            labels=_require_bool(_require_key(data, "labels"), "labels"),
            composition=_require_text(_require_key(data, "composition"), "composition"),
            tile_scheme=_require_text(
                data.get("tileScheme", PACKAGE_DEFAULT_TILE_SCHEME), "tileScheme"
            ),
            tile_format=_require_text(_require_key(data, "tileFormat"), "tileFormat"),
            min_zoom=_require_zoom(_require_key(data, "minZoom"), "minZoom"),
            max_zoom=_require_zoom(_require_key(data, "maxZoom"), "maxZoom"),
            west=_require_number(_require_key(bounds, "west"), "bounds.west"),
            south=_require_number(_require_key(bounds, "south"), "bounds.south"),
            east=_require_number(_require_key(bounds, "east"), "bounds.east"),
            north=_require_number(_require_key(bounds, "north"), "bounds.north"),
            tile_count=_require_int(_require_key(data, "tileCount"), "tileCount"),
            mbtiles_file=_require_text(_require_key(mbtiles, "file"), "mbtiles.file"),
            mbtiles_size=_require_int(_require_key(mbtiles, "size"), "mbtiles.size"),
            mbtiles_sha256=_require_text(_require_key(mbtiles, "sha256"), "mbtiles.sha256"),
            created_at=_require_text(_require_key(data, "createdAt"), "createdAt"),
            description=description,
            region=region,
        )


# --------------------------------------------------------------------------- #
# 构建 / 校验结论
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PackageProgress:
    """一步构建 / 校验的进度快照（Spec §18）。

    ``percent < 0`` 表示"这一步的总量不可预知"（例如还没数出要复制多少字节），
    界面据此切到不确定模式的进度条 —— 与 Slice 8 的
    :class:`~app.mbtiles.InspectionProgress` 同一套语义，不另立规矩。
    """

    #: 阶段标识（``check`` / ``copy`` / ``sha256`` / ``manifest`` / ``validate`` …）
    stage: str
    #: 给用户看的一句话
    message: str
    #: 总体进度百分比（``-1`` = 不确定）
    percent: int = -1
    #: 当前步进与总量（字节数或张数，视阶段而定）
    current: int = 0
    total: int = 0

    @property
    def determinate(self) -> bool:
        """进度是否可量化（可量化才允许把进度条切到 0~100 模式）。"""
        return self.percent >= 0


@dataclass
class PackageValidationResult:
    """``MapPackageValidator.validate()`` 的结论（Spec §13）。

    ``valid`` 的判定规则（Spec §13）：**没有 errors 就是 PASS**。
    ``warnings`` 只说明"有东西对不上但不致命"（例如非关键 metadata 不一致），
    不影响结论。

    ``checksum_valid`` / ``mbtiles_valid`` 是**三行结论**的原始事实：

    - ``checksum_valid=False`` 有两种可能：校验没通过（有 error），或者压根
      没校验（``verify_checksum=False``，只有 warning）。分辨方式是看
      :attr:`errors` —— 这是刻意的：``valid`` 才是"这个包能不能用"的答案，
      三个布尔只是"这四步各自做了什么"。
    """

    valid: bool = False
    package_dir: str = ""

    manifest_valid: bool = False
    mbtiles_valid: bool = False
    checksum_valid: bool = False

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    #: 解析出来的 manifest（解析失败为 ``None``）
    manifest: MapPackageManifest | None = None
    #: 包内 MBTiles 的实际路径（找不到为 ``None``）
    mbtiles_path: str | None = None
    #: MBTiles 的实际大小与 SHA256（没算过为 ``None``）
    mbtiles_size: int | None = None
    actual_sha256: str | None = None

    @property
    def status(self) -> str:
        """``PASS`` / ``FAIL``（与 Slice 8 的三个结论词保持同一套大小写）。"""
        return "PASS" if self.valid else "FAIL"

    @property
    def status_text(self) -> str:
        """中文一句话结论（给界面/日志用）。"""
        if self.valid:
            return "PASS" if not self.warnings else "PASS（有警告）"
        return "FAIL"

    def as_lines(self) -> list[str]:
        """逐行展开，供报告 / 文本区展示。顺序固定：错误在前、警告在后。"""
        lines = [f"结论：{self.status}"]
        lines.extend(f"[ERROR] {message}" for message in self.errors)
        lines.extend(f"[WARNING] {message}" for message in self.warnings)
        return lines


@dataclass
class PackageBuildResult:
    """``MapPackageBuilder.build()`` 的结论（Spec §10）。

    ``success=False`` 有**三种**不同的含义，靠 :attr:`needs_confirmation` 区分：

    - ``needs_confirmation=True``：MBTiles 检查是 WARNING，还没开始生成，
      等用户拍板（Spec §23）。这不是失败，界面上不能报"生成失败"。
    - 否则有 ``errors``：真的失败了，``package_dir`` 保证**没有被半成品污染**
      （Spec §15）。
    """

    success: bool = False

    package_dir: str | None = None
    manifest_path: str | None = None
    mbtiles_path: str | None = None
    sha256: str | None = None

    mbtiles_size: int = 0
    tile_count: int = 0
    min_zoom: int | None = None
    max_zoom: int | None = None

    #: 构建前的 MBTiles 检查结论（``PASS`` / ``WARNING`` / ``FAIL``）
    diagnostics_status: str = ""

    #: 构建完成后的自校验结论（Spec §5 的最后一步）
    validation: PackageValidationResult | None = None

    #: 需要用户确认才能继续（MBTiles 带警告时）
    needs_confirmation: bool = False
    #: 需要确认的**原因**：``"warning"``（MBTiles 有警告，Spec §23）或
    #: ``"exists"``（目标地图包已存在，Spec §16）。空串表示不是在等确认。
    #: 界面据此决定弹哪个确认框 —— 一个布尔量说不清"要问什么"。
    confirmation_reason: str = ""
    #: 是否原地构建（源文件已经是 ``<包>/map.mbtiles``，不重复复制，Spec §8）
    in_place: bool = False
    #: 是否真的复制了文件
    copied: bool = False
    #: 原有地图包被改名备份后的路径（覆盖构建时为非空）
    backup_path: str | None = None

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def validation_status(self) -> str:
        """自校验的结论词；没跑过返回 ``"—"``（"不知道"与"FAIL"必须能分辨）。"""
        if self.validation is None:
            return UI_TEXT_PACKAGE_UNKNOWN
        return self.validation.status

    @property
    def mbtiles_size_text(self) -> str:
        """MBTiles 大小的可读形式。"""
        return format_size(self.mbtiles_size)

    @property
    def zoom_text(self) -> str:
        """``8~17``（未设置时为 ``"—"``）。"""
        if self.min_zoom is None or self.max_zoom is None:
            return UI_TEXT_PACKAGE_UNKNOWN
        if self.min_zoom == self.max_zoom:
            return str(self.min_zoom)
        return f"{self.min_zoom}~{self.max_zoom}"

    def as_lines(self) -> list[str]:
        """逐行展开，供日志区 / 验收脚本展示。"""
        lines: list[str] = []
        if self.package_dir:
            lines.append(f"地图包目录：{self.package_dir}")
        if self.sha256:
            lines.append(f"SHA256：{self.sha256}")
        if self.validation is not None:
            lines.append(f"验证结论：{self.validation.status}")
        lines.extend(f"[ERROR] {message}" for message in self.errors)
        lines.extend(f"[WARNING] {message}" for message in self.warnings)
        return lines


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def render_manifest_json(manifest: MapPackageManifest) -> str:
    """把 manifest 渲染成写入磁盘的 JSON 文本（Spec §3）。

    ``indent=2`` + ``ensure_ascii=False``：中文名称直接以 UTF-8 出现，用户
    拿记事本打开能看懂，Android 侧 ``InputStream`` 按 UTF-8 读即可。

    ``allow_nan=False``：宁可在这里抛 ``ValueError``，也不要写出一个带裸
    ``NaN`` 的"JSON" —— 那不是合法 JSON，严格解析器会拒收，而错误会推迟到
    Android 导入时才爆发。

    末尾补一个换行：文本文件以换行结尾是 POSIX 惯例，`git diff` / `cat`
    的表现也更正常。
    """
    text = json.dumps(
        manifest.to_dict(), ensure_ascii=False, indent=2, allow_nan=False
    )
    return text + "\n"


def parse_manifest_dict(data: Any) -> MapPackageManifest:
    """把已解析的 JSON 数据结构转成 :class:`MapPackageManifest`。

    与 :meth:`MapPackageManifest.from_dict` 的区别：这个函数把**所有**
    字段级异常统一收敛成 :class:`ManifestError` 子类（``from_dict`` 里
    ``GeoBounds`` 抛的是 ``ValueError``，直接往上传会混进"业务错误"里）。
    """
    try:
        return MapPackageManifest.from_dict(data)
    except (ManifestError, UnsupportedFormatVersion):
        raise
    except ValueError as exc:
        raise ManifestError(f"manifest 字段非法：{exc}") from exc


# --------------------------------------------------------------------------- #
# 字段读取 / 校验的小工具
# --------------------------------------------------------------------------- #
def _require_key(container: Mapping[str, Any], key: str) -> Any:
    """取必填键；缺失时报错信息里带上键名（而不是一个 KeyError 堆栈）。"""
    if key not in container:
        raise ManifestError(f"manifest 缺少必需字段：{key}")
    return container[key]


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{label} 必须是 JSON 对象，实际为 {type(value).__name__}")
    return value


def _require_text(value: Any, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ManifestError(f"{label} 必须是字符串，实际为 {type(value).__name__}")
    text = str(value).strip()
    if not text:
        raise ManifestError(f"{label} 不能为空")
    return text


def _require_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ManifestError(f"{label} 必须是整数，实际为布尔值")
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            raise ManifestError(f"{label} 必须是整数，实际为 {value!r}")
        return int(value)
    if not isinstance(value, int):
        raise ManifestError(f"{label} 必须是整数，实际为 {type(value).__name__}")
    return value


def _require_zoom(value: Any, label: str) -> int:
    zoom = _require_int(value, label)
    if not TILE_MATH_MIN_ZOOM <= zoom <= TILE_MATH_MAX_ZOOM:
        raise ManifestError(
            f"{label} 必须在 [{TILE_MATH_MIN_ZOOM}, {TILE_MATH_MAX_ZOOM}] 之间，"
            f"实际为 {zoom}"
        )
    return zoom


def _require_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestError(f"{label} 必须是数字，实际为 {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        # ``NaN`` / ``Infinity`` 会让 ``json.dumps`` 写出非法 JSON（Spec §25）
        raise ManifestError(f"{label} 不能是 {value!r}（NaN / Infinity 不是合法 JSON）")
    return number


def _require_bounds(
    west: Any, south: Any, east: Any, north: Any
) -> tuple[float, float, float, float]:
    values = (
        _require_number(west, "bounds.west"),
        _require_number(south, "bounds.south"),
        _require_number(east, "bounds.east"),
        _require_number(north, "bounds.north"),
    )
    try:
        GeoBounds(*values)
    except ValueError as exc:
        raise ManifestError(f"bounds 非法：{exc}") from exc
    return values


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestError(f"{label} 必须是布尔值（true/false），实际为 {value!r}")
    return value


def _require_bare_filename(value: Any, label: str) -> str:
    """``mbtiles.file`` 必须是**纯文件名**（Spec §25）。

    ``../x.mbtiles`` / ``..\\x.mbtiles`` / ``D:\\maps\\x.mbtiles`` / ``sub/x.mbtiles``
    一律拒绝。这一层比"解析路径后判断有没有跑出包目录"更早生效：它连
    "看起来像路径"都不允许，Android 侧照着拼路径永远不会拼出包外的位置。
    """
    text = _require_text(value, label)
    if not _BARE_FILENAME_PATTERN.match(text):
        raise ManifestPathError(
            f"{label} 必须是包内的纯文件名（不允许路径分隔符或盘符），实际为 {text!r}"
        )
    if text in {".", ".."} or text.strip(".") == "":
        raise ManifestPathError(f"{label} 不是合法的文件名：{text!r}")
    return text


def _require_sha256(value: Any, label: str) -> str:
    text = _require_text(value, label)
    if not _HEX_SHA256_PATTERN.match(text):
        raise ManifestError(
            f"{label} 必须是 {PACKAGE_SHA256_LENGTH} 位十六进制 SHA256，实际为 {text!r}"
        )
    return text.lower()


def _require_timestamp(value: Any, label: str) -> str:
    """``createdAt`` 必须是可解析的 ISO 8601 且**带时区**（Spec §3）。"""
    text = _require_text(value, label)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ManifestError(f"{label} 不是合法的 ISO 8601 时间：{text!r}") from exc
    if parsed.tzinfo is None:
        raise ManifestError(f"{label} 必须带时区偏移（如 +08:00），实际为 {text!r}")
    return text
