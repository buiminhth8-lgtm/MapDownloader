"""地图图层定义。

天地图 WMTS 服务按"图层名"区分底图与注记，并且中英文各有独立服务路径。
本模块只描述图层的静态元数据，不含任何网络请求逻辑。
"""

from __future__ import annotations

from enum import Enum


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


#: 矢量底图（含中文注记）的组合，UI 默认选项
DEFAULT_LAYER_COMBO: tuple[MapLayerType, ...] = (
    MapLayerType.VECTOR,
    MapLayerType.VECTOR_LABEL,
)

#: 图层类型的可读名称，供 UI 下拉框展示
LAYER_DISPLAY_NAMES: dict[MapLayerType, str] = {
    MapLayerType.VECTOR: "矢量底图",
    MapLayerType.VECTOR_LABEL: "矢量中文注记",
    MapLayerType.IMAGE: "影像底图",
    MapLayerType.IMAGE_LABEL: "影像中文注记",
}


def layers_for(
    base: MapLayerType, with_labels: bool = True
) -> tuple[MapLayerType, ...]:
    """按"是否需要中文注记"组合出最终要下载的图层列表。

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
