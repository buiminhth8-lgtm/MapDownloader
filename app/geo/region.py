"""下载区域模型（Slice 16）。

统一"这次要下哪一块地"的表达方式，让矩形与多边形走**同一条**下载链路 ——
差别只在"候选瓦片要不要再过一道几何过滤"，而不是两套调度器。

两个概念必须分清（这是本 Slice 的核心）
---------------------------------------

- **候选范围（candidate）**：矩形的 BoundingBox，或行政区的外包矩形
  （``geometry.bounds``）。它只用来**快速缩小搜索面**，本身不决定要下什么。
- **选中瓦片（selected）**：候选范围内、**与行政区域真正相交**的那些瓦片。

矩形模式下两者恒等；多边形模式下 selected 是 candidate 的（可能很小的）子集。
UI 的预估、进度条分母、大任务提示都必须用 selected —— 用 candidate 会让
"要下 4 万张"和"实际下 2.8 万张"变成两个对不上的数字。

中国边界数据
------------

``resources/regions/china.geojson`` **默认不存在**：中国国界属于法定地图内容，
必须使用带审图号的合法授权数据，本仓库不内置也不伪造（Spec §3）。

因此本模块提供的是**规范路径 + 加载器 + 明确的缺失报错**：

- :attr:`RegionPreset.available` 回答"边界文件在不在"；
- :meth:`RegionPreset.require_available` 在缺失时抛 :class:`RegionBoundaryMissing`，
  文案里直接给出应放的位置与授权要求；
- 绝不在缺文件时退化成矩形下载 —— 那会静默下一大片境外瓦片。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from app.config.settings import (
    CHINA_REGION_FILENAME,
    CHINA_REGION_NAME,
    REGION_BOUNDARY_LICENSE_NOTICE,
    REGION_BOUNDARY_MISSING_HINT,
    REGION_RESOURCE_DIR,
)
from app.geo.bounds import GeoBounds
from app.geo.geojson_loader import (
    GeoJSONDocument,
    GeoJSONError,
    geometry_to_bounds,
    load_geojson_document,
)

__all__ = [
    "RegionType",
    "DownloadRegion",
    "RegionPreset",
    "RegionBoundaryMissing",
    "CHINA_REGION_PRESET",
    "REGION_PRESETS",
    "list_region_presets",
    "get_region_preset",
    "region_from_task",
    "region_from_metadata",
]


# --------------------------------------------------------------------------- #
# 区域类型
# --------------------------------------------------------------------------- #
class RegionType(Enum):
    """下载区域的几何类型。

    ``value`` 会被**写进 MBTiles metadata 与地图包 manifest**，因此它是一份
    持久化契约：改名等于让老包读不出区域信息。
    """

    RECTANGLE = "rectangle"
    POLYGON = "polygon"

    @classmethod
    def parse(cls, value: object) -> "RegionType":
        """宽容解析：认不出来的一律按 :attr:`RECTANGLE` 处理。

        这条宽容规则是**有意为之**（Spec §21）：老 MBTiles / 老 manifest
        没有 region 字段，未来版本也可能引入 ``multipolygon`` 之类的新取值。
        把它们当成矩形只会"多下一点"，而报错或猜成多边形都可能"少下" ——
        少下是用户看不见的缺陷，多下只是浪费，两者代价不对称。

        >>> RegionType.parse(None) is RegionType.RECTANGLE
        True
        >>> RegionType.parse("polygon") is RegionType.POLYGON
        True
        >>> RegionType.parse("multipolygon") is RegionType.RECTANGLE
        True
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            for item in cls:
                if item.value == normalized:
                    return item
        return cls.RECTANGLE

    @property
    def display_name(self) -> str:
        """界面上的类型名（与预设下拉框里的选项文案一致）。"""
        return "自定义矩形" if self is RegionType.RECTANGLE else "行政区域"


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class RegionBoundaryMissing(RuntimeError):
    """行政区边界资源不可用（文件缺失 / 读不出）。

    单独成类（而不是复用 :class:`GeoJSONError`）的理由：UI 需要把"数据没放"
    与"数据坏了"分成两种提示，前者要告诉用户去哪里放什么文件。
    """

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.path = path


# --------------------------------------------------------------------------- #
# 区域预设
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RegionPreset:
    """一个可选的行政区域（Spec §14）。

    首版只内置 :data:`CHINA_REGION_PRESET`；要加 ``zhejiang`` / ``jiangsu`` /
    ``hangzhou``，只需在 :data:`REGION_PRESETS` 里补一条并放入同名 GeoJSON ——
    本模块其余部分（加载、过滤、预估、Inspector 还原）无需改动。

    Attributes:
        region_id: 稳定标识，写进任务记录、MBTiles metadata 与 manifest。
        name: 展示名（中文）。
        geojson_filename: 边界文件在 :data:`~app.config.settings.REGION_RESOURCE_DIR`
            下的文件名（**只有文件名**：不允许带路径，避免用预设做目录穿越）。
        default_boundary_version: 边界数据版本。``None`` 表示"以 GeoJSON 里
            ``properties.boundaryVersion`` 为准"——数据自带版本时不该在代码里
            再抄一份，那两者迟早不一致。
    """

    region_id: str
    name: str
    geojson_filename: str = CHINA_REGION_FILENAME
    default_boundary_version: str | None = None
    region_type: RegionType = RegionType.POLYGON

    def __post_init__(self) -> None:
        if "/" in self.geojson_filename or "\\" in self.geojson_filename:
            raise ValueError(
                f"geojson_filename 只能是文件名，不允许带路径，实际为 {self.geojson_filename!r}"
            )

    @property
    def geojson_path(self) -> Path:
        """边界文件的完整路径（不保证存在）。"""
        return REGION_RESOURCE_DIR / self.geojson_filename

    @property
    def available(self) -> bool:
        """边界文件是否已就位。"""
        return self.geojson_path.is_file()

    def missing_message(self) -> str:
        """文件缺失时给用户看的完整说明（位置 + 授权要求）。"""
        return (
            f"{REGION_BOUNDARY_MISSING_HINT.format(path=self.geojson_path)}。"
            f"{REGION_BOUNDARY_LICENSE_NOTICE}，"
            f"命名为 {self.geojson_filename} 后放入 {REGION_RESOURCE_DIR}"
        )

    def require_available(self) -> Path:
        """返回边界文件路径；缺失时抛 :class:`RegionBoundaryMissing`。"""
        path = self.geojson_path
        if not path.is_file():
            raise RegionBoundaryMissing(self.missing_message(), path=str(path))
        return path

    @property
    def boundary_version(self) -> str:
        """边界版本：优先取 GeoJSON 的 ``properties.boundaryVersion``。

        读不到文件或文件里没写时退回 :attr:`default_boundary_version`。
        这里**只读一小段**（不解析整个 JSON）不划算——GeoJSON 是单行还是多行
        不确定，直接交给加载器更稳；调用方（UI / 预估）本来就要加载几何。
        """
        return self.default_boundary_version or ""

    def load_document(self, *, attempt_repair: bool = False) -> GeoJSONDocument:
        """加载边界文件。

        Raises:
            RegionBoundaryMissing: 文件不存在。
            GeoJSONError: 文件存在但内容不合法（由加载器抛出）。
        """
        path = self.require_available()
        return load_geojson_document(path, attempt_repair=attempt_repair)


#: 内置预设：中国（Spec §14 点名的那一条）。
CHINA_REGION_PRESET = RegionPreset(
    region_id="china",
    name=CHINA_REGION_NAME,
    geojson_filename=CHINA_REGION_FILENAME,
)

#: 全部内置预设，顺序即 UI 下拉框顺序。
REGION_PRESETS: tuple[RegionPreset, ...] = (CHINA_REGION_PRESET,)


def list_region_presets() -> tuple[RegionPreset, ...]:
    """返回全部内置预设（当前只有中国）。"""
    return REGION_PRESETS


def get_region_preset(region_id: str | None) -> RegionPreset | None:
    """按 id 找预设；找不到返回 ``None``。

    ``None`` 而不是抛异常：Inspector 遇到一个"库里写着 region_id=xxx、
    但本版本不认识"的包时，正确处置是"覆盖度标 UNKNOWN"，不是炸掉整个检查。
    """
    if not region_id:
        return None
    wanted = str(region_id).strip().lower()
    for preset in REGION_PRESETS:
        if preset.region_id == wanted:
            return preset
    return None


# --------------------------------------------------------------------------- #
# 统一区域模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DownloadRegion:
    """一次下载的区域（Spec §2）。

    Attributes:
        region_type: 矩形 / 多边形。
        name: 展示名（"自定义矩形" / "中国"）。
        bounds: 矩形范围，**或**多边形候选范围（``geometry.bounds``）。
            多边形模式下它**只用于快速筛掉绝大部分瓦片**，最终每一张瓦片
            仍要经 :class:`~app.geo.PolygonTileRegionFilter` 判定。
        region_id: 行政区标识（``china``）；自定义矩形为 ``None``。
        boundary_version: 边界数据版本（可追溯"这批瓦片是按哪版边界裁的"）。
        geojson_path: 边界文件路径（字符串，便于写进任务记录）。

    Raises:
        ValueError: 名称为空；或多边形模式缺 :attr:`geojson_path`
            （没有边界文件就无从过滤，放行等于静默下载整个外包矩形）。
    """

    region_type: RegionType
    name: str
    bounds: GeoBounds
    region_id: str | None = None
    boundary_version: str | None = None
    geojson_path: str | None = None

    def __post_init__(self) -> None:
        region_type = RegionType.parse(self.region_type)
        object.__setattr__(self, "region_type", region_type)
        object.__setattr__(self, "name", str(self.name).strip())
        if not self.name:
            raise ValueError("DownloadRegion.name 不能为空")

        path = (self.geojson_path or "").strip()
        if region_type is RegionType.POLYGON and not path:
            raise ValueError(
                "多边形区域必须给出 geojson_path：没有边界文件就无法做瓦片过滤，"
                "退化成候选矩形会静默下载区域外的瓦片"
            )
        object.__setattr__(self, "geojson_path", path or None)

        region_id = (self.region_id or "").strip()
        object.__setattr__(self, "region_id", region_id or None)
        version = (self.boundary_version or "").strip()
        object.__setattr__(self, "boundary_version", version or None)

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #
    @classmethod
    def rectangle(cls, name: str, bounds: GeoBounds) -> "DownloadRegion":
        """构造自定义矩形区域（与 Slice 0~15 的行为完全一致）。"""
        return cls(region_type=RegionType.RECTANGLE, name=name, bounds=bounds)

    @classmethod
    def from_preset(
        cls,
        preset: RegionPreset,
        *,
        bounds: GeoBounds | None = None,
        document: GeoJSONDocument | None = None,
    ) -> "DownloadRegion":
        """由一个预设构造多边形区域。

        Args:
            preset: 区域预设。
            bounds: 已经算好的外包范围；给了就**不再读文件**。
            document: 已经加载好的几何文档；给了就复用它取范围，
                避免"UI 为了显示范围读一遍、预估时又读一遍"。

        Raises:
            RegionBoundaryMissing: 边界文件不存在（且没有现成的 ``bounds``）。
            GeoJSONError: 边界文件不合法。
        """
        if bounds is None:
            doc = document if document is not None else preset.load_document()
            bounds = geometry_to_bounds(doc.geometry)
        version = preset.boundary_version
        if document is not None and not version:
            version = document.boundary_version
        return cls(
            region_type=RegionType.POLYGON,
            name=preset.name,
            bounds=bounds,
            region_id=preset.region_id,
            boundary_version=version or None,
            geojson_path=str(preset.geojson_path),
        )

    # ------------------------------------------------------------------ #
    # 派生信息
    # ------------------------------------------------------------------ #
    @property
    def is_polygon(self) -> bool:
        """是否需要走几何过滤。"""
        return self.region_type is RegionType.POLYGON

    @property
    def preset(self) -> RegionPreset | None:
        """多边形区域对应的预设（用于 Inspector 按 region_id 找边界文件）。"""
        return get_region_preset(self.region_id) if self.is_polygon else None

    def describe(self) -> str:
        """一行展示文案，如 ``中国（行政区域）`` / ``自定义矩形``。"""
        if self.is_polygon:
            suffix = f"，{self.boundary_version}" if self.boundary_version else ""
            return f"{self.name}（行政区域{suffix}）"
        return "自定义矩形"

    def bounds_text(self) -> str:
        """候选范围的紧凑文本（UI 的"范围："行用）。"""
        return (
            f"{self.bounds.west:.4f}, {self.bounds.south:.4f}, "
            f"{self.bounds.east:.4f}, {self.bounds.north:.4f}"
        )

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #
    def task_fields(self) -> dict[str, Any]:
        """写进 ``tasks.json`` 的字段（Spec §13）。

        **不写 GeoJSON 本身**：一份省级边界就有几 MB，把一个几 MB 的字符串
        塞进 tasks.json 会让每次进度落盘都变成一次大写入。恢复时按
        :attr:`region_id` 回到 :data:`REGION_PRESETS` 找路径即可。
        """
        return {
            "region_type": self.region_type.value,
            "region_id": self.region_id or "",
            "boundary_version": self.boundary_version or "",
        }

    def metadata_rows(self) -> dict[str, str]:
        """写进 MBTiles ``metadata`` 表的键值（Spec §19）。

        矩形也写 ``region_type=rectangle``：显式写出来，Inspector 才不用靠
        "没有这个键"来猜。``region_id`` / ``boundary_version`` 只在有值时写，
        避免矩形包多出两个空值键。
        """
        rows = {"region_type": self.region_type.value}
        if self.region_id:
            rows["region_id"] = self.region_id
        if self.boundary_version:
            rows["boundary_version"] = self.boundary_version
        return rows

    def manifest_region(self) -> dict[str, str]:
        """写进地图包 manifest 的 ``region`` 子对象（Spec §21）。"""
        data = {"type": self.region_type.value}
        if self.region_id:
            data["id"] = self.region_id
        if self.boundary_version:
            data["boundaryVersion"] = self.boundary_version
        return data

    def with_bounds(self, bounds: GeoBounds) -> "DownloadRegion":
        """换一个候选范围（返回新实例；本类型不可变）。"""
        return replace(self, bounds=bounds)


def region_from_task(task: object) -> DownloadRegion | None:
    """从任务记录（或任何有同名属性的对象）还原 :class:`DownloadRegion`。

    走鸭子类型而不是 ``isinstance(task, DownloadTask)``：``app.geo`` 是纯计算层，
    不该 import ``app.models``（那会把数据模型层拉进 geo 的依赖）。

    还原规则：

    - 记录里的 ``region_type`` 认不出、或就是 ``rectangle`` → 矩形区域；
    - 是多边形但 ``region_id`` 找不到对应预设 → 返回 ``None``
      （调用方据此提示"这个任务的行政区边界已不可用"，而不是猜一个形状）。

    Returns:
        :class:`DownloadRegion`；多边形无法还原时返回 ``None``。

    Raises:
        ValueError: 记录里的经纬度/缩放不构成合法范围。
    """
    west = float(getattr(task, "west", 0.0))
    south = float(getattr(task, "south", 0.0))
    east = float(getattr(task, "east", 0.0))
    north = float(getattr(task, "north", 0.0))
    bounds = GeoBounds(west=west, south=south, east=east, north=north)

    region_type = RegionType.parse(getattr(task, "region_type", ""))
    if region_type is RegionType.RECTANGLE:
        return DownloadRegion.rectangle(str(getattr(task, "name", "") or ""), bounds)

    region_id = str(getattr(task, "region_id", "") or "").strip()
    preset = get_region_preset(region_id)
    if preset is None:
        return None
    version = str(getattr(task, "boundary_version", "") or "").strip()
    return DownloadRegion(
        region_type=RegionType.POLYGON,
        name=preset.name,
        bounds=bounds,
        region_id=preset.region_id,
        boundary_version=version or preset.boundary_version or None,
        geojson_path=str(preset.geojson_path),
    )


def region_from_metadata(
    region_type: str | None,
    region_id: str | None,
    boundary_version: str | None,
    bounds: GeoBounds,
) -> tuple[DownloadRegion | None, str]:
    """从 MBTiles metadata 的三个键还原区域（Inspector 用）。

    Returns:
        ``(region, note)``：``region`` 为 ``None`` 表示"无法还原"，``note``
        是给用户看的说明（可直接放进诊断结果的 WARNING）。

        与 :func:`region_from_task` 的差别在于**多返回一句解释**：Inspector 需要
        把"边界文件找不到了"讲给用户听，而下载链路只需要知道成功还是失败。
    """
    if RegionType.parse(region_type) is RegionType.RECTANGLE:
        return DownloadRegion.rectangle("自定义矩形", bounds), ""

    wanted = (region_id or "").strip()
    preset = get_region_preset(wanted)
    if preset is None:
        return None, (
            f"行政区边界资源不可用：本版本不认识 region_id={wanted!r}"
            if wanted
            else "行政区边界资源不可用：metadata 缺少 region_id"
        )
    if not preset.available:
        return None, preset.missing_message()
    return (
        DownloadRegion(
            region_type=RegionType.POLYGON,
            name=preset.name,
            bounds=bounds,
            region_id=preset.region_id,
            boundary_version=(boundary_version or "").strip()
            or preset.boundary_version
            or None,
            geojson_path=str(preset.geojson_path),
        ),
        "",
    )
