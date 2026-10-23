"""Zoom ↔ 地面分辨率换算，以及**无人机图基底**的推荐档位（Slice 29）。

为什么需要这个模块
------------------

``Zoom`` 是个"投影空间"的抽象数字，它本身不告诉你"能看清什么"。同一级
Zoom 在哈尔滨和广州的地面分辨率差 30%（墨卡托按纬度拉伸），而"能不能
看清单栋房子"只跟**米/像素**有关。给无人机选图基底时，用户脑子里想的
是"我要 1 米分辨率"或"能看清房子就行"，不是"我要 Z17"。

换算公式（Web 墨卡托，EPSG:3857）::

    赤道处: resolution(z) = 40075016.6856 / (256 × 2^z)   米/像素
    纬度 φ:  resolution(z, φ) = 上式 × cos(φ)

``cos(φ)`` 是**缩小**而不是放大：纬度越高，同样的 Zoom 覆盖的地面越少，
每像素对应的米数越少（图更清楚）。北纬 32° 上 Z17 ≈ 1.01 米/像素，
北纬 45° 上只有 0.84 —— 差 20%，足够让"按经验抄的 Z 值"在不同的作业
地点表现不一致。

数据源的天花板（比 Zoom 上限更重要）
------------------------------------

天地图主站 WMTS 的 ``TILEMATRIX`` 上限就是 **Z18**（``_w`` 矩阵集 1~18 级），
再往上没有瓦片。但"有瓦片"不等于"有数据"：地方节点公开的成果目录显示，
农村全域影像来自**高分一号 / 高分六号（2 米）**，城区才有高分二号 / 七号
（0.8 米）。这意味着：

- **Z16（约 2.0 米/像素）是农村"原生分辨率"的饱和点** —— 再往下解像力
  不再增加，只是把 2 米影像插值放大。
- **Z17（约 1.0 米/像素）只有城区和有高分影像的地区有真实增益**。
- **Z18（约 0.5 米/像素）** 在多数地区是插值，体积却是 Z17 的 4 倍。

所以"越高越好"在这里是错的：超出数据源精度的层级**只买体积，不买信息**。
:data:`UAV_ZOOM_PRESETS` 就是按这个事实定的。

推荐值怎么用
------------

给无人机图基底选档位时按作业类型选，不要按"行政区等级"硬套：

===============  ==========  ==============  ==========================
用途              Zoom        米/像素(@32°N)   体积（10 km² 作业区）
===============  ==========  ==============  ==========================
转场 / 大范围索引  Z8~Z11      518~62          约 0.1~1.5 MB
航线规划（主力）   **Z14~Z16**  8.1~2.0         0.9~11.7 MB
作业区详查        Z16~Z17      2.0~1.0         11.7~45.7 MB
关键点位 / 取证    Z17~Z18      1.0~0.5         45.7~178 MB
===============  ==========  ==============  ==========================

要一个数就给 :data:`UAV_BASELINE_ZOOM`（**Z16**）：它是"能辨认单栋农舍、
双车道公路、田块边界、大棚轮廓"的最低层级，同时正好是农村影像的原生
分辨率，再往上不花钱也买不到信息。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.config.settings import MAX_SUPPORTED_ZOOM, MIN_SUPPORTED_ZOOM

__all__ = [
    "EARTH_CIRCUMFERENCE_METERS",
    "UAV_BASELINE_ZOOM",
    "UAV_ZOOM_PRESETS",
    "UavZoomPreset",
    "describe_resolution",
    "meters_per_pixel",
    "resolution_range",
    "zoom_covering",
]


#: WGS84 赤道周长（米）。Web 墨卡托把赤道拉伸到 ``256 × 2^z`` 像素，
#: 因此 Z0 的一个像素 = 这个值 / 256 ≈ 156543.03 米。
EARTH_CIRCUMFERENCE_METERS: float = 40075016.68557849

#: 无人机图基底的**默认档**（要一个数的时候用它）。
#:
#: 选 Z16 而不是 Z18 的理由是"数据饱和点"：天地图农村影像的原生分辨率是
#: 2 米（高分一号 / 六号），对应约 Z16。Z17/Z18 在多数非城区只是插值放大，
#: 体积却是 4 倍 / 16 倍。城区想要 Z17~Z18 时用 :data:`UAV_ZOOM_PRESETS`
#: 里的 ``survey`` 档，或直接手填自定义 Zoom。
UAV_BASELINE_ZOOM: int = 16


@dataclass(frozen=True)
class UavZoomPreset:
    """一个**按用途**（而不是按行政等级）划分的 Zoom 档位。

    Attributes:
        key: 稳定标识（日志 / 配置用），如 ``"planning"``。
        name: 中文名，直接上界面。
        min_zoom: 区间下界（含）。
        max_zoom: 区间上界（含）。
        purpose: 一句话说明这一档能看清什么、代价是多少。
    """

    key: str
    name: str
    min_zoom: int
    max_zoom: int
    purpose: str

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("key 不能为空")
        if not self.name.strip():
            raise ValueError("name 不能为空")
        if not MIN_SUPPORTED_ZOOM <= self.min_zoom <= self.max_zoom:
            raise ValueError(
                f"区间不合法：Z{self.min_zoom}~Z{self.max_zoom}，"
                f"下界必须在 [{MIN_SUPPORTED_ZOOM}, {self.max_zoom}] 之间"
            )
        if self.max_zoom > MAX_SUPPORTED_ZOOM:
            raise ValueError(
                f"上界 Z{self.max_zoom} 超出支持范围 Z{MAX_SUPPORTED_ZOOM}"
                "（天地图 TILEMATRIX 上限就是 Z18，再往上没有瓦片）"
            )

    @property
    def range_text(self) -> str:
        """``Z14~16`` 这样的紧凑文本。"""
        return f"Z{self.min_zoom}~{self.max_zoom}"


#: 无人机图基底的四个档位（按用途，不按行政等级）。
#:
#: 体积是按 **10 km² 作业区**、30 KB/瓦片粗估的（见 ``docs/zoom_strategy.md``
#: 的实测表）。真实体积随纬度、图层类型（矢量比影像小）浮动，只用于量级判断。
UAV_ZOOM_PRESETS: tuple[UavZoomPreset, ...] = (
    UavZoomPreset(
        key="transit",
        name="转场 / 大范围索引",
        min_zoom=8,
        max_zoom=11,
        purpose="看省界、主要水系、城市位置与主干路网；单屏覆盖几十到几百公里。",
    ),
    UavZoomPreset(
        key="planning",
        name="航线规划（主力）",
        min_zoom=14,
        max_zoom=16,
        purpose="看清乡镇路网、成片田块、村庄轮廓与独立建筑群；单屏 1~3 公里。",
    ),
    UavZoomPreset(
        key="survey",
        name="作业区详查",
        min_zoom=16,
        max_zoom=17,
        purpose="看清单栋农舍、大棚、水塘、围墙与双车道公路；单屏 300~600 米。",
    ),
    UavZoomPreset(
        key="inspect",
        name="关键点位 / 取证",
        min_zoom=17,
        max_zoom=18,
        purpose=(
            "城区能到 0.5 米/像素；农村多为 2 米影像的插值放大，体积是上一档 4 倍。"
        ),
    ),
)


def meters_per_pixel(zoom: int, latitude: float = 0.0) -> float:
    """给定 Zoom 与纬度，返回一个像素对应的地面距离（米）。

    Args:
        zoom: 缩放级别（``1 ~ 22``）。
        latitude: 纬度（度）。赤道为 0，北纬为正。Web 墨卡托在极区急剧
            拉伸，超过 ±85.05° 无定义 —— 这里按 ``|latitude| <= 85.05`` 校验。

    Returns:
        米/像素。数值越小越清楚。

    Raises:
        ValueError: ``zoom`` 不是整数，或 ``latitude`` 超出墨卡托定义域。

    >>> round(meters_per_pixel(0), 2)
    156543.03
    >>> round(meters_per_pixel(17, 32.0), 2)
    1.01
    """
    if isinstance(zoom, bool) or not isinstance(zoom, int):
        raise ValueError(f"zoom 必须是整数，实际为 {type(zoom).__name__}")
    if not MIN_SUPPORTED_ZOOM <= zoom <= 22:
        raise ValueError(
            f"zoom 必须在 [{MIN_SUPPORTED_ZOOM}, 22] 之间，实际为 {zoom}"
        )
    lat = float(latitude)
    if not -85.05112878 <= lat <= 85.05112878:
        raise ValueError(f"latitude 超出 Web 墨卡托定义域，实际为 {lat}")
    equator = EARTH_CIRCUMFERENCE_METERS / (256 * (2**zoom))
    return equator * math.cos(math.radians(lat))


def resolution_range(
    min_zoom: int, max_zoom: int, latitude: float = 0.0
) -> tuple[float, float]:
    """区间的分辨率范围，返回 ``(较粗的一端的米/像素, 较细的一端)``。

    Zoom 越大越细，因此返回值是 ``(min_zoom 的米/像素, max_zoom 的米/像素)``，
    第一个数必然 **大于等于** 第二个。

    Raises:
        ValueError: ``min_zoom > max_zoom``。
    """
    if min_zoom > max_zoom:
        raise ValueError(
            f"min_zoom 不能大于 max_zoom，实际为 {min_zoom} > {max_zoom}"
        )
    return meters_per_pixel(min_zoom, latitude), meters_per_pixel(max_zoom, latitude)


def zoom_covering(meters: float, latitude: float = 0.0) -> int:
    """要"至少这么清楚"，最低需要哪一级 Zoom。

    "至少"指米/像素 **不大于** ``meters``，因此结果是向上取整（更细）。

    Args:
        meters: 期望的地面分辨率（米/像素），必须为正。
        latitude: 纬度（度）。

    Returns:
        满足要求的**最小** Zoom（数字越小越粗）。

    Raises:
        ValueError: ``meters`` 不是正数，或所需 Zoom 超出支持范围。

    >>> zoom_covering(2.0, 32.0)   # 2 米/像素 ≈ Z16
    16
    """
    mpp = float(meters)
    if not math.isfinite(mpp) or mpp <= 0:
        raise ValueError(f"meters 必须是正数，实际为 {meters!r}")
    for zoom in range(MIN_SUPPORTED_ZOOM, MAX_SUPPORTED_ZOOM + 1):
        if meters_per_pixel(zoom, latitude) <= mpp:
            return zoom
    raise ValueError(
        f"{mpp} 米/像素超出支持范围：Z{MAX_SUPPORTED_ZOOM} 在纬度 "
        f"{latitude} 上也只有 {meters_per_pixel(MAX_SUPPORTED_ZOOM, latitude):.3f} 米/像素"
    )


def describe_resolution(
    min_zoom: int, max_zoom: int, latitude: float = 0.0
) -> str:
    """界面上那一行"这相当于多清楚"，如 ``约 8.1 ~ 2.0 米/像素``。

    超过 1 米/像素保留一位小数；小于 1 米时保留两位（0.51 与 0.5 对
    "能不能看清车"是有区别的，虽然对底图用途影响有限）。
    """
    coarse, fine = resolution_range(min_zoom, max_zoom, latitude)
    return f"约 {_format_meters(coarse)} ~ {_format_meters(fine)} 米/像素"


def _format_meters(value: float) -> str:
    """按量级选小数位：≥10 取整、≥1 一位、<1 两位。"""
    if value >= 10:
        return f"{value:.0f}"
    if value >= 1:
        return f"{value:.1f}"
    return f"{value:.2f}"
