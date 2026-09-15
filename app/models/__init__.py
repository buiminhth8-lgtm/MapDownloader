"""数据模型层。

本层是纯数据描述：只有 ``dataclass`` / ``Enum`` / 纯属性计算，
不 import PySide6、不发起网络请求、不读写磁盘。

依赖方向：``models`` 只依赖 ``config``，UI 与业务层反向依赖本层。
"""

from app.models.device import Device, DeviceState
from app.models.download_task import (
    DownloadProgress,
    DownloadTask,
    DownloadTaskStatus,
    new_task_id,
)
from app.models.map_layer import (
    COMPOSITION_MERGED,
    COMPOSITION_SINGLE,
    COMPOSITION_VALUES,
    DEFAULT_LAYER_COMBO,
    LAYER_DISPLAY_NAMES,
    MAP_TYPE_DISPLAY_NAMES,
    MAP_TYPE_LAYERS,
    MAP_TYPE_SHORT_NAMES,
    MapLayerType,
    MapType,
    composition_for,
    describe_map_type,
    layers_for,
)
from app.models.map_package import MapPackage, new_package_id

__all__ = [
    # map_layer
    "MapLayerType",
    "MapType",
    "MAP_TYPE_LAYERS",
    "MAP_TYPE_DISPLAY_NAMES",
    "MAP_TYPE_SHORT_NAMES",
    "LAYER_DISPLAY_NAMES",
    "COMPOSITION_SINGLE",
    "COMPOSITION_MERGED",
    "COMPOSITION_VALUES",
    "composition_for",
    "describe_map_type",
    "DEFAULT_LAYER_COMBO",
    "layers_for",
    # download_task
    "DownloadTask",
    "DownloadTaskStatus",
    "DownloadProgress",
    "new_task_id",
    # map_package
    "MapPackage",
    "new_package_id",
    # device
    "Device",
    "DeviceState",
]
