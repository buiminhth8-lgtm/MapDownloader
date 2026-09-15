"""地图图层定义（Slice 0 起；Slice 6 增加"地图类型"与合成配置）。

天地图 WMTS 服务按"图层名"区分底图与注记，并且中英文各有独立服务路径。
本模块只描述图层的静态元数据，不含任何网络请求逻辑。

两层概念，别混淆
----------------

- :class:`MapLayerType` —— **单个 WMTS 图层**（``vec_w`` / ``cva_w`` / ``img_w`` /
  ``cia_w``）。它是"一次 HTTP 请求打到哪个服务路径"的答案。
- :class:`MapType` —— **业务意义上的地图类型**（矢量地图 / 影像地图）。
  用户在下拉框里选的是它；它再映射到 :data:`MAP_TYPE_LAYERS` 里的
  ``(底图, 注记)`` 两个 :class:`MapLayerType`。

为什么要有 :class:`MapType`
---------------------------

没有它的话，"矢量 + 注记"这个组合会在 UI（拼提示文案）、Service（决定调哪个
下载器）、Downloader（决定请求哪些图层）三处各写一遍 —— 三份映射迟早会漂移，
而漂移的表现是"界面说要下 cva_w，实际请求的是 cia_w"这种极难发现的错误。
Slice 6 把映射收敛到本模块的 :data:`MAP_TYPE_LAYERS` 这一张表里。

Slice 6 新增的合成配置（写进 MBTiles metadata，供续传一致性校验使用）::

    composition=single  只有底图，没有注记
    composition=merged  底图 + 中文注记，已被合成进同一张 PNG

两者的瓦片**不可混写**（Spec §12）：把注记瓦片写进一个没有注记的库，
用户会得到一份"有的地块有字、有的没有"的地图。
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "MapLayerType",
    "MapType",
    "MAP_TYPE_LAYERS",
    "MAP_TYPE_DISPLAY_NAMES",
    "MAP_TYPE_SHORT_NAMES",
    "LAYER_DISPLAY_NAMES",
    "COMPOSITION_SINGLE",
    "COMPOSITION_MERGED",
    "COMPOSITION_VALUES",
    "DEFAULT_LAYER_COMBO",
    "composition_for",
    "describe_map_type",
    "layers_for",
]


class MapLayerType(Enum):
    """天地图图层类型。

    value 即天地图服务中的图层标识（``T=vec_w`` 等），不要随意改动。
    """

    VECTOR = "vec_w"
    VECTOR_LABEL = "cva_w"
    IMAGE = "img_w"
    IMAGE_LABEL = "cia_w"

    # ------------------------------------------------------------------ #
    # 便捷属性
    # ------------------------------------------------------------------ #
    @property
    def service_name(self) -> str:
        """WMTS 请求中 ``LAYER`` 参数的值。

        天地图把图层标识拆成两处：URL 路径用完整标识（``vec_w``），
        ``LAYER`` 参数只用后半段（``vec``）。

        >>> MapLayerType.VECTOR.service_name
        'vec'
        >>> MapLayerType.IMAGE_LABEL.service_name
        'cia'
        """
        head, _, matrix_set = self.value.partition("_")
        if not head or not matrix_set:
            raise ValueError(f"图层标识格式非法：{self.value!r}，期望形如 'vec_w'")
        return head

    @property
    def matrix_set(self) -> str:
        """瓦片矩阵集标识（``vec_w`` 的 ``w`` 表示 Web 墨卡托）。"""
        _, _, matrix_set = self.value.partition("_")
        if not matrix_set:
            raise ValueError(f"图层标识格式非法：{self.value!r}，期望形如 'vec_w'")
        return matrix_set

    @property
    def is_label(self) -> bool:
        """是否为注记（文字标注）图层。"""
        return self in (MapLayerType.VECTOR_LABEL, MapLayerType.IMAGE_LABEL)

    @property
    def is_image(self) -> bool:
        """是否为影像图层。"""
        return self in (MapLayerType.IMAGE, MapLayerType.IMAGE_LABEL)

    @property
    def base_layer(self) -> "MapLayerType":
        """返回与之配对的底图图层；本身是底图时返回自身。

        >>> MapLayerType.VECTOR_LABEL.base_layer
        <MapLayerType.VECTOR: 'vec_w'>
        >>> MapLayerType.IMAGE.base_layer
        <MapLayerType.IMAGE: 'img_w'>
        """
        if self is MapLayerType.VECTOR_LABEL:
            return MapLayerType.VECTOR
        if self is MapLayerType.IMAGE_LABEL:
            return MapLayerType.IMAGE
        return self

    @property
    def label_layer(self) -> "MapLayerType":
        """返回与之配对的注记图层；本身是注记时返回自身。"""
        if self is MapLayerType.VECTOR:
            return MapLayerType.VECTOR_LABEL
        if self is MapLayerType.IMAGE:
            return MapLayerType.IMAGE_LABEL
        return self


#: 地图类型 → ``(底图, 注记)`` 图层映射。
#:
#: **全项目唯一的一份映射**（Spec §2）：UI、Service、Downloader 都从这里取，
#: 不允许在别处再写一遍 ``if 矢量: 用 vec_w``。
MAP_TYPE_LAYERS: dict["MapType", tuple[MapLayerType, MapLayerType]] = {}


class MapType(Enum):
    """业务意义上的地图类型（用户在界面上选的那个）。

    value 是写进 MBTiles ``metadata.map_type`` 的字符串，不要随意改动
    —— 老文件里的值要靠它识别（Spec §12 的续传一致性校验）。
    """

    VECTOR = "vector"
    IMAGE = "image"

    # ------------------------------------------------------------------ #
    # 派生信息
    # ------------------------------------------------------------------ #
    @property
    def display_name(self) -> str:
        """中文展示名（界面下拉框用）。"""
        return MAP_TYPE_DISPLAY_NAMES[self]

    @property
    def base_layer(self) -> MapLayerType:
        """底图图层（不注记）。

        >>> MapType.VECTOR.base_layer.value
        'vec_w'
        >>> MapType.IMAGE.base_layer.value
        'img_w'
        """
        return MAP_TYPE_LAYERS[self][0]

    @property
    def label_layer(self) -> MapLayerType:
        """中文注记图层。

        >>> MapType.VECTOR.label_layer.value
        'cva_w'
        >>> MapType.IMAGE.label_layer.value
        'cia_w'
        """
        return MAP_TYPE_LAYERS[self][1]

    @property
    def is_image(self) -> bool:
        """是否是影像地图（影像底图的合成规则与矢量一致，但这个判断常被用到）。"""
        return self is MapType.IMAGE

    def layers(self, with_labels: bool = True) -> tuple[MapLayerType, ...]:
        """本次要下载的图层列表，底图在前、注记在后。

        >>> [item.value for item in MapType.VECTOR.layers(True)]
        ['vec_w', 'cva_w']
        >>> [item.value for item in MapType.VECTOR.layers(False)]
        ['vec_w']
        """
        if with_labels:
            return (self.base_layer, self.label_layer)
        return (self.base_layer,)

    def request_count_per_tile(self, with_labels: bool = True) -> int:
        """每张**最终瓦片**需要几次 WMTS 请求。

        它恒等于图层层数 —— 每张最终瓦片都要为它的每一层发一次请求。
        单独开这个方法是因为这个数字会被 UI（预估请求量）、README 与测试
        三处用到，写死 ``2`` 的地方越多，"将来支持三图层"时越容易漏改。

        >>> MapType.VECTOR.request_count_per_tile(True)
        2
        >>> MapType.VECTOR.request_count_per_tile(False)
        1
        """
        return len(self.layers(with_labels))

    @classmethod
    def from_layer(cls, layer: "MapLayerType | MapType | str") -> "MapType":
        """由图层（或字符串标识）反查地图类型。

        底图与注记都能反查：``vec_w`` / ``cva_w`` 都得到 ``VECTOR``。

        >>> MapType.from_layer(MapLayerType.IMAGE_LABEL) is MapType.IMAGE
        True
        >>> MapType.from_layer("vec_w") is MapType.VECTOR
        True

        Raises:
            ValueError: 无法识别的图层。
        """
        if isinstance(layer, MapType):
            return layer
        if isinstance(layer, MapLayerType):
            return cls.VECTOR if not layer.is_image else cls.IMAGE
        text = str(layer).strip()
        for item in MapLayerType:
            if item.value == text:
                return cls.from_layer(item)
        for item in cls:
            if item.value == text:
                return item
        raise ValueError(
            f"无法识别的地图类型或图层：{layer!r}；"
            f"支持图层 {[item.value for item in MapLayerType]} "
            f"或地图类型 {[item.value for item in cls]}"
        )

    @classmethod
    def parse(cls, value: str | None) -> "MapType | None":
        """宽松解析（给"从磁盘读回来的记录"用）。

        与 :meth:`from_layer` 的区别：认不出来时返回 ``None`` 而不是抛异常
        —— 一条老记录里的空字符串不该让界面炸掉，业务层自行决定怎么处理。
        """
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            return cls.from_layer(text)
        except ValueError:
            return None


#: 填上 :data:`MAP_TYPE_LAYERS`。刻意放在 :class:`MapType` 定义之后，
#: 这样键可以用枚举成员本身（而不是字符串），避免"表里的键写错了拼写"这类问题。
MAP_TYPE_LAYERS.update(
    {
        MapType.VECTOR: (MapLayerType.VECTOR, MapLayerType.VECTOR_LABEL),
        MapType.IMAGE: (MapLayerType.IMAGE, MapLayerType.IMAGE_LABEL),
    }
)

#: 地图类型的可读名称，供 UI 下拉框展示
MAP_TYPE_DISPLAY_NAMES: dict[MapType, str] = {
    MapType.VECTOR: "矢量地图",
    MapType.IMAGE: "影像地图",
}

#: 地图类型的**短**名称。任务列表要给 9 列留位置，「矢量地图 + 中文注记」
#: 这样的长文案会把窄列挤爆，因此另存一份短的。
MAP_TYPE_SHORT_NAMES: dict[MapType, str] = {
    MapType.VECTOR: "矢量",
    MapType.IMAGE: "影像",
}

#: 图层类型的可读名称（Slice 2 起保留：日志与连接测试里按"单个图层"展示）
LAYER_DISPLAY_NAMES: dict[MapLayerType, str] = {
    MapLayerType.VECTOR: "矢量底图",
    MapLayerType.VECTOR_LABEL: "矢量中文注记",
    MapLayerType.IMAGE: "影像底图",
    MapLayerType.IMAGE_LABEL: "影像中文注记",
}

#: 只下载底图（无中文注记）
COMPOSITION_SINGLE: str = "single"
#: 底图 + 中文注记，已合成进同一张瓦片
COMPOSITION_MERGED: str = "merged"
#: 合法的 composition 取值
COMPOSITION_VALUES: frozenset[str] = frozenset({COMPOSITION_SINGLE, COMPOSITION_MERGED})


def composition_for(with_labels: bool) -> str:
    """由"是否要中文注记"推出 composition 取值。

    单独一个函数而不是三元表达式散落各处：这个映射同时被
    "写 metadata" 与 "续传一致性校验" 用到，两处必须永远一致。

    >>> composition_for(True)
    'merged'
    >>> composition_for(False)
    'single'
    """
    return COMPOSITION_MERGED if with_labels else COMPOSITION_SINGLE


#: 矢量底图（含中文注记）的组合，Slice 0 起就是 UI 默认选项
DEFAULT_LAYER_COMBO: tuple[MapLayerType, ...] = (
    MapLayerType.VECTOR,
    MapLayerType.VECTOR_LABEL,
)


def describe_map_type(
    map_type: "MapType | None",
    with_labels: bool,
    *,
    short: bool = False,
    missing: str = "—",
) -> str:
    """把"地图类型 + 是否含中文注记"说成一句人能读的话（Slice 7）。

    这个组合在界面上要出现至少五处（预估结果、任务列表、下载完成弹窗、
    失败弹窗、启动日志）。每处各拼一遍 ``f"{名字} + 中文注记"`` 的结果是
    某天改文案时只剩四处跟着改了——而且不会有人发现那处漏的。

    Args:
        map_type: 地图类型；``None``（老记录认不出来）时返回 ``missing``。
        with_labels: 是否把中文注记合成进了瓦片。
        short: ``True`` 时用短名（``矢量+注记``），给窄列用。
        missing: ``map_type`` 为 ``None`` 时的占位文案。

    Returns:
        诸如 ``矢量地图 + 中文注记`` / ``影像`` / ``—`` 的字符串。

    >>> describe_map_type(MapType.VECTOR, True)
    '矢量地图 + 中文注记'
    >>> describe_map_type(MapType.IMAGE, False, short=True)
    '影像'
    >>> describe_map_type(None, True)
    '—'
    """
    if map_type is None:
        return missing
    if short:
        base = MAP_TYPE_SHORT_NAMES[map_type]
        return f"{base}+注记" if with_labels else base
    base = MAP_TYPE_DISPLAY_NAMES[map_type]
    return f"{base} + 中文注记" if with_labels else base


def layers_for(
    base: MapLayerType, with_labels: bool = True
) -> tuple[MapLayerType, ...]:
    """按"是否需要中文注记"组合出最终要下载的图层列表。

    Slice 6 起新代码请优先用 :meth:`MapType.layers`（语义更清楚）；
    本函数保留是为了不破坏 Slice 2~5 已有的调用与测试。

    Args:
        base: 底图图层类型。
        with_labels: 是否附带对应的中文注记图层。

    Returns:
        有序的图层元组，底图在前、注记在后。
    """
    base = base.base_layer
    if with_labels:
        return (base, base.label_layer)
    return (base,)
