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

from app.config.settings import (
    MBTILES_PROVIDER_TIANDITU,
    TILE_MATH_MAX_ZOOM,
    TILE_MATH_MIN_ZOOM,
)
from app.geo import GeoBounds, format_size
from app.models.map_layer import (
    COMPOSITION_MERGED,
    COMPOSITION_SINGLE,
    COMPOSITION_VALUES,
    MapType,
)

__all__ = [
    "METADATA_KEY_ORDER",
    "SUPPORTED_MAP_TYPES",
    "SUPPORTED_REGION_TYPES",
    "SUPPORTED_TILE_FORMATS",
    "MBTilesMetadata",
    "MBTilesValidationResult",
]


#: MBTiles 允许的 ``format`` 取值。写错（例如 ``"png "`` 带空格）会让
#: osmdroid 侧静默显示空白，因此在入口就拒绝，而不是等到了手机上才发现。
SUPPORTED_TILE_FORMATS: frozenset[str] = frozenset(
    {"png", "jpg", "jpeg", "webp", "pbf"}
)

#: 允许出现在 ``metadata.map_type`` 里的取值。
#:
#: 刻意从 :class:`~app.models.MapType` 派生而不是自己写一遍字符串：
#: 这个值会被**写进文件**，而 Slice 5 的续传一致性校验会拿它跟任务记录比对。
#: 两处只要有一个字不同，用户就会遇到"明明参数一样却说不一致"。
SUPPORTED_MAP_TYPES: frozenset[str] = frozenset(item.value for item in MapType)

#: ``metadata`` 表中本工具会写入的键，顺序即写入顺序。
#:
#: 固定顺序不是为了好看：它让"同一份配置生成的 metadata"逐字节可比，
#: 测试与人工 diff 才有意义。
#:
#: 前 8 个是 MBTiles 标准字段；中间 4 个是 Slice 6 增加的本工具自有字段，
#: 它们回答"这个包是怎么来的、能不能接着往里写"（Spec §10 / §12）；
#: 最后 3 个是 Slice 16 的区域信息（Spec §19）。
#: 不是每个文件都有后几项 —— Slice 4/5 生成的文件没有，
#: 因此 :meth:`MBTilesMetadata.as_rows` 会跳过取值为空的那几个。
METADATA_KEY_ORDER: tuple[str, ...] = (
    "name",
    "type",
    "version",
    "description",
    "format",
    "bounds",
    "minzoom",
    "maxzoom",
    "provider",
    "map_type",
    "labels",
    "composition",
    "region_type",
    "region_id",
    "boundary_version",
    # Slice 22：只有任务区会写这两项（见 MBTilesMetadata 的字段说明）
    "region_level",
    "buffer_meters",
    "coverage_data",
)

#: ``metadata.region_type`` 允许的取值（Spec §19）。
#:
#: 刻意写成一份独立常量而不是 import ``app.geo.RegionType``：本模块（以及整个
#: ``app.mbtiles``）的单元测试不该为了一个枚举把 Shapely 拉进来。两者取值必须
#: 一致这件事由 ``tests/test_mbtiles_region_metadata.py`` 里的同步断言守住 ——
#: 靠测试发现漂移，比靠"记得同时改两处"可靠。
SUPPORTED_REGION_TYPES: frozenset[str] = frozenset({"rectangle", "polygon", "coverage"})


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
        provider: 瓦片来源标识（本工具固定 ``"tianditu"``）。
        source_map_type: **本工具自有字段**，写进 ``metadata.map_type``
            （``vector`` / ``image``）。注意它与上面的 ``map_type``（MBTiles
            标准的 ``type`` 键）是两回事 —— 那个占用了 ``map_type`` 这个属性名，
            所以这里的属性只能换个名字，而键名保持 Spec §10 要求的 ``map_type``。
        with_labels: 是否包含中文注记层；``None`` 表示"不写这个键"
            （老版本生成的文件读回来就是 ``None``）。
        composition: ``single`` / ``merged``，见 :data:`COMPOSITION_VALUES`。

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
    # --- Slice 6：合成信息 ---
    provider: str = ""
    source_map_type: str = ""
    with_labels: bool | None = None
    composition: str = ""
    # --- Slice 16：区域信息（Spec §19）---
    #: ``rectangle`` / ``polygon``；空串表示"这个文件没有记录区域信息"
    #: （Slice ≤15 生成的包读回来就是 ``""``，Inspector 据此按矩形处理）。
    region_type: str = ""
    #: 行政区标识（``china``）或任务区标识（``mission-001``）；矩形或老文件为空串。
    region_id: str = ""
    #: 边界数据版本，用于追溯"这批瓦片是按哪一版边界裁的"。
    #: 任务区写的是**几何版本号**（``1:5000.0:9c1f…``）—— 语义完全相同。
    boundary_version: str = ""
    # --- Slice 22：任务区补充信息（Spec §20）---
    #: 区域等级（``mission``）。**只有任务区写**：行政区的等级能从 ``region_id``
    #: 反查出来，多写一份只会多一处不同步的地方；任务区的几何在用户数据目录里，
    #: 反查不出来，所以必须落在文件里。
    region_level: str = ""
    #: 任务区走廊半径（米，字符串形式）。非任务区为空串。
    #: 它的用处是回答"这个包是按几公里的走廊下的" —— 一个 20 km 的任务区包
    #: 与一个 500 m 的包，Tile 数可能差上百倍，只写 bounds 是解释不清的。
    buffer_meters: str = ""
    coverage_data: str = ""

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

        # --- Slice 6：合成信息（空值表示"本文件不记录这一项"）---
        self.provider = str(self.provider).strip()
        self.source_map_type = str(self.source_map_type).strip().lower()
        if self.source_map_type and self.source_map_type not in SUPPORTED_MAP_TYPES:
            raise ValueError(
                f"map_type 必须是 {sorted(SUPPORTED_MAP_TYPES)} 之一，"
                f"实际为 {self.source_map_type!r}"
            )
        self.composition = str(self.composition).strip().lower()
        if self.composition and self.composition not in COMPOSITION_VALUES:
            raise ValueError(
                f"composition 必须是 {sorted(COMPOSITION_VALUES)} 之一，"
                f"实际为 {self.composition!r}"
            )
        if self.with_labels is not None:
            self.with_labels = bool(self.with_labels)

        # --- Slice 16：区域信息（空值表示"本文件不记录这一项"）---
        self.region_type = str(self.region_type).strip().lower()
        if self.region_type and self.region_type not in SUPPORTED_REGION_TYPES:
            raise ValueError(
                f"region_type 必须是 {sorted(SUPPORTED_REGION_TYPES)} 之一，"
                f"实际为 {self.region_type!r}"
            )
        self.region_id = str(self.region_id).strip()
        self.boundary_version = str(self.boundary_version).strip()

        # --- Slice 22：任务区补充信息（空值表示"本文件不记录这一项"）---
        self.region_level = str(self.region_level).strip().lower()
        radius = self.buffer_meters
        if radius is None or str(radius).strip() == "":
            self.buffer_meters = ""
        else:
            try:
                meters = float(radius)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"buffer_meters 必须是数字，实际为 {radius!r}"
                ) from exc
            # 写成稳定的字符串（与 ``DownloadRegion.metadata_rows`` 同一格式）：
            # 5000.0 → "5000"，1500.5 → "1500.5"。
            text = f"{meters:.1f}"
            self.buffer_meters = text[:-2] if text.endswith(".0") else text

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

    @property
    def labels_text(self) -> str:
        """``metadata.labels`` 的字符串值：``"true"`` / ``"false"``。

        MBTiles 的 metadata 是 ``TEXT`` 键值表，因此布尔值必须转成小写字符串
        —— 写 ``"True"`` 虽然同样能被我们读回来，但别的工具按
        ``value == "true"`` 判断时会认不出来。
        """
        return "true" if self.with_labels else "false"

    @property
    def is_merged(self) -> bool:
        """是否是"底图 + 中文注记已合成"的产物。"""
        return self.composition == COMPOSITION_MERGED

    def as_dict(self) -> dict[str, str]:
        """返回 ``metadata`` 表的键值对（全部为 TEXT）。

        Slice 6 的四个自有字段**只在有值时写入**：一个 Slice 4 时代生成、
        被本 Slice 重新读取的文件不该因为我们写了一遍元数据就多出
        ``map_type=""`` 这种没有意义的内容。
        """
        values = {
            "name": self.name,
            "type": self.map_type,
            "version": self.version,
            "description": self.description,
            "format": self.format,
            "bounds": self.bounds_text,
            "minzoom": str(self.min_zoom),
            "maxzoom": str(self.max_zoom),
        }
        if self.provider:
            values["provider"] = self.provider
        if self.source_map_type:
            values["map_type"] = self.source_map_type
        if self.with_labels is not None:
            values["labels"] = self.labels_text
        if self.composition:
            values["composition"] = self.composition
        if self.coverage_data:
            values["coverage_data"] = self.coverage_data
        if self.region_type:
            values["region_type"] = self.region_type
        if self.region_id:
            values["region_id"] = self.region_id
        if self.boundary_version:
            values["boundary_version"] = self.boundary_version
        if self.region_level:
            values["region_level"] = self.region_level
        if self.buffer_meters:
            values["buffer_meters"] = self.buffer_meters
        return values

    def as_rows(self) -> list[tuple[str, str]]:
        """返回 ``(key, value)`` 列表，顺序与 :data:`METADATA_KEY_ORDER` 一致。"""
        values = self.as_dict()
        return [(key, values[key]) for key in METADATA_KEY_ORDER if key in values]

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
        provider: str = MBTILES_PROVIDER_TIANDITU,
        source_map_type: str = MapType.VECTOR.value,
        with_labels: bool = False,
        composition: str = COMPOSITION_SINGLE,
        region_type: str = "",
        region_id: str = "",
        boundary_version: str = "",
        region_level: str = "",
        buffer_meters: str | float | None = "",
    ) -> MBTilesMetadata:
        """便捷构造：直接用 :class:`~app.geo.GeoBounds` 建元数据。

        下载链路的入参本来就是 ``GeoBounds``，有这个工厂可以避免调用方
        到处写 ``bounds.west, bounds.south, bounds.east, bounds.north``
        ——那种写法一旦顺序写错（把 east/north 弄反）极难发现。

        默认值即"本工具的默认配置"：天地图矢量底图、不含中文注记、未合成。
        影像地图或合成模式必须显式传参（下载服务层会传）。
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
            provider=provider,
            source_map_type=source_map_type,
            with_labels=with_labels,
            composition=composition,
            region_type=region_type,
            region_id=region_id,
            boundary_version=boundary_version,
            region_level=region_level,
            buffer_meters="" if buffer_meters is None else buffer_meters,
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
    # --- Slice 6：从 metadata 里读回的合成信息（读不出为 None）---
    provider: str | None = None
    map_type: str | None = None
    labels: bool | None = None
    composition: str | None = None

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

    @property
    def is_merged(self) -> bool:
        """是否是"底图 + 中文注记已合成"的产物。"""
        return self.composition == COMPOSITION_MERGED

    @property
    def composition_text(self) -> str:
        """一行合成说明；metadata 里没有这一项时返回空串。"""
        if not self.composition:
            return ""
        if self.composition == COMPOSITION_MERGED:
            return f"合成：{self.composition}（底图 + 中文注记）"
        return f"合成：{self.composition}（仅底图）"

    def as_lines(self) -> list[str]:
        """多行展示文案（GUI 结果区 / 日志用）。"""
        lines = [
            f"验证：{self.status_text}",
            f"瓦片：{self.tile_count:,}",
            f"Zoom：{self.zoom_text}",
            f"文件大小：{format_size(self.file_size)}",
        ]
        composition_line = self.composition_text
        if composition_line:
            lines.append(composition_line)
        if self.map_type:
            lines.append(f"地图类型：{self.map_type}")
        lines.extend(f"错误：{message}" for message in self.errors)
        lines.extend(f"提示：{message}" for message in self.warnings)
        return lines
