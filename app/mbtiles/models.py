"""MBTiles 数据模型（Slice 4）。

本模块只描述"MBTiles 里的数据长什么样"，不含任何 SQL / 文件 IO：

- :class:`MBTilesMetadata` —— ``metadata`` 表的**结构化视图**（9 个字段 → 8 个标准键）
- :class:`MBTilesValidationResult` —— 校验结论

坐标约定（本 Slice 最重要的一条）
---------------------------------

- **对外（工具内部 / GUI / Reader / Writer 的入参）一律使用 XYZ**：
  ``y`` 自北向南增长，左上角为原点。
- **MBTiles 文件内部一律使用 TMS**：``tile_row`` 自南向北增长。

两者的换算只允许有**一处**实现：:func:`app.geo.xyz_y_to_tms_y`。
本层刻意不提供任何换算方法，避免有人图省事在这里再写一遍。

边界约定
--------

- 本层不 import ``sqlite3``、不 import PySide6、不依赖 ``httpx``：
  可被纯单元测试完全覆盖。
- 经纬度合法性**复用** Slice 1 的 :class:`~app.geo.GeoBounds`，
  不重复写一套 ±180 / ±85.05112878 的边界判断。
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field

from app.config.settings import TILE_MATH_MAX_ZOOM, TILE_MATH_MIN_ZOOM
from app.geo import GeoBounds, format_size

__all__ = [
    "MBTilesMetadata",
    "MBTilesValidationResult",
    "SUPPORTED_TILE_FORMATS",
    "METADATA_KEY_ORDER",
]


#: MBTiles 允许的 ``format`` 取值。写错（例如 ``"png "`` 带空格）会让
#: osmdroid 侧静默显示空白，因此在入口就拒绝，而不是等到了手机上才发现。
SUPPORTED_TILE_FORMATS: frozenset[str] = frozenset(
    {"png", "jpg", "jpeg", "webp", "pbf"}
)

#: ``metadata`` 表中本工具会写入的键，顺序即写入顺序。
#:
#: 固定顺序不是为了好看：它让"同一份配置生成的 metadata"逐字节可比，
#: 测试与人工 diff 才有意义。
METADATA_KEY_ORDER: tuple[str, ...] = (
    "name",
    "type",
    "version",
    "description",
    "format",
    "bounds",
    "minzoom",
    "maxzoom",
)


@dataclass
class MBTilesMetadata:
    """MBTiles ``metadata`` 表的结构化内容。

    Attributes:
        name: 地图名称，也会被用作默认文件名。
        description: 人类可读描述。
        min_zoom / max_zoom: 覆盖的缩放级别区间（含两端）。
        west / south / east / north: 覆盖范围（WGS84 经纬度）。
        format: 瓦片图片格式，见 :data:`SUPPORTED_TILE_FORMATS`。
        map_type: MBTiles 的 ``type`` 字段，底图固定 ``"baselayer"``。
        version: MBTiles 规范版本，固定 ``"1.0"``。

    Raises:
        ValueError: 名称为空、缩放区间非法、范围非法，或格式化参数不可识别。
    """

    name: str
    description: str
    min_zoom: int
    max_zoom: int
    west: float
    south: float
    east: float
    north: float
    format: str = "png"
    map_type: str = "baselayer"
    version: str = "1.0"

    def __post_init__(self) -> None:
        name = str(self.name).strip()
        if not name:
            raise ValueError("MBTiles 的 name 不能为空")
        self.name = name
        self.description = str(self.description)

        self.format = str(self.format).strip().lower()
        if self.format not in SUPPORTED_TILE_FORMATS:
            raise ValueError(
                f"format 必须是 {sorted(SUPPORTED_TILE_FORMATS)} 之一，"
                f"实际为 {self.format!r}"
            )

        self.map_type = str(self.map_type).strip()
        if not self.map_type:
            raise ValueError("MBTiles 的 type（map_type）不能为空")
        self.version = str(self.version).strip()
        if not self.version:
            raise ValueError("MBTiles 的 version 不能为空")

        try:
            min_zoom = operator.index(self.min_zoom)
            max_zoom = operator.index(self.max_zoom)
        except TypeError as exc:
            raise ValueError(
                f"min_zoom / max_zoom 必须是整数，实际为 {self.min_zoom!r} / {self.max_zoom!r}"
            ) from exc
        if not TILE_MATH_MIN_ZOOM <= min_zoom <= TILE_MATH_MAX_ZOOM:
            raise ValueError(
                f"min_zoom 必须在 [{TILE_MATH_MIN_ZOOM}, {TILE_MATH_MAX_ZOOM}] 之间，"
                f"实际为 {min_zoom}"
            )
        if not TILE_MATH_MIN_ZOOM <= max_zoom <= TILE_MATH_MAX_ZOOM:
            raise ValueError(
                f"max_zoom 必须在 [{TILE_MATH_MIN_ZOOM}, {TILE_MATH_MAX_ZOOM}] 之间，"
                f"实际为 {max_zoom}"
            )
        if min_zoom > max_zoom:
            raise ValueError(f"min_zoom 不能大于 max_zoom，实际为 {min_zoom} > {max_zoom}")
        self.min_zoom = min_zoom
        self.max_zoom = max_zoom

        # 借 GeoBounds 完成经纬度校验（±180 / ±85.05112878 / west<east / south<north）
        GeoBounds(self.west, self.south, self.east, self.north)

    # ------------------------------------------------------------------ #
    # 派生信息
    # ------------------------------------------------------------------ #
    @property
    def bounds(self) -> GeoBounds:
        """范围的 :class:`~app.geo.GeoBounds` 视图（每次访问都重新校验）。"""
        return GeoBounds(self.west, self.south, self.east, self.north)

    @property
    def bounds_text(self) -> str:
        """``metadata.bounds`` 的字符串值：``west,south,east,north``。

        固定 6 位小数，**不用** ``repr(float)``：后者在不同平台上可能给出
        ``116.19999999999999`` 这类字符串，导致同一份范围生成的 MBTiles
        元数据不可比。
        """
        return (
            f"{self.west:.6f},{self.south:.6f},"
            f"{self.east:.6f},{self.north:.6f}"
        )

    @property
    def zoom_range(self) -> tuple[int, int]:
        """``(min_zoom, max_zoom)``。"""
        return (self.min_zoom, self.max_zoom)

    def as_dict(self) -> dict[str, str]:
        """返回 ``metadata`` 表的键值对（全部为 TEXT）。"""
        return {
            "name": self.name,
            "type": self.map_type,
            "version": self.version,
            "description": self.description,
            "format": self.format,
            "bounds": self.bounds_text,
            "minzoom": str(self.min_zoom),
            "maxzoom": str(self.max_zoom),
        }

    def as_rows(self) -> list[tuple[str, str]]:
        """返回 ``(key, value)`` 列表，顺序与 :data:`METADATA_KEY_ORDER` 一致。"""
        values = self.as_dict()
        return [(key, values[key]) for key in METADATA_KEY_ORDER]

    @classmethod
    def for_bounds(
        cls,
        bounds: GeoBounds,
        min_zoom: int,
        max_zoom: int,
        *,
        name: str,
        description: str = "",
        image_format: str = "png",
        map_type: str = "baselayer",
        version: str = "1.0",
    ) -> "MBTilesMetadata":
        """便捷构造：直接用 :class:`~app.geo.GeoBounds` 建元数据。

        下载链路的入参本来就是 ``GeoBounds``，有这个工厂可以避免调用方
        到处写 ``bounds.west, bounds.south, bounds.east, bounds.north``
        ——那种写法一旦顺序写错（把 east/north 弄反）极难发现。
        """
        return cls(
            name=name,
            description=description,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            west=bounds.west,
            south=bounds.south,
            east=bounds.east,
            north=bounds.north,
            format=image_format,
            map_type=map_type,
            version=version,
        )


# --------------------------------------------------------------------------- #
# 校验结论
# --------------------------------------------------------------------------- #
@dataclass
class MBTilesValidationResult:
    """:func:`app.mbtiles.validator.validate_mbtiles` 的结论。

    ``errors`` 与 ``warnings`` 的分工：

    - **errors**：文件不可用（打不开、缺表、零瓦片、tile_data 全空……）→ ``valid=False``
    - **warnings**：可用但可疑（metadata 缺字段、zoom 与 metadata 声明不一致……）

    这样"手机加载不出来"和"能加载但元数据不完整"不会被混为一谈。
    """

    valid: bool = False
    tile_count: int = 0
    min_zoom: int | None = None
    max_zoom: int | None = None
    file_size: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def status_text(self) -> str:
        """``"PASS"`` / ``"FAIL"``，供 GUI 直接展示。"""
        return "PASS" if self.valid else "FAIL"

    @property
    def zoom_text(self) -> str:
        """``"8~16"``；没有瓦片时为 ``"—"``。"""
        if self.min_zoom is None or self.max_zoom is None:
            return "—"
        return f"{self.min_zoom}~{self.max_zoom}"

    def as_lines(self) -> list[str]:
        """多行展示文案（GUI 结果区 / 日志用）。"""
        lines = [
            f"验证：{self.status_text}",
            f"瓦片：{self.tile_count:,}",
            f"Zoom：{self.zoom_text}",
            f"文件大小：{format_size(self.file_size)}",
        ]
        lines.extend(f"错误：{message}" for message in self.errors)
        lines.extend(f"提示：{message}" for message in self.warnings)
        return lines
