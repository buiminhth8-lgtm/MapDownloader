"""数据模型层单元测试。"""

from __future__ import annotations

from pathlib import Path

from app.config import settings
from app.models import (
    Device,
    DeviceState,
    DownloadProgress,
    DownloadTask,
    DownloadTaskStatus,
    MapLayerType,
    MapPackage,
    layers_for,
)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def test_settings_defaults() -> None:
    assert settings.APP_NAME == "天地图离线地图工具"
    assert settings.APP_VERSION == "0.1.0"
    assert settings.DEFAULT_MIN_ZOOM == 8
    assert settings.DEFAULT_MAX_ZOOM == 17
    assert settings.DEFAULT_WINDOW_WIDTH == 1200
    assert settings.DEFAULT_WINDOW_HEIGHT == 760


# --------------------------------------------------------------------------- #
# 图层
# --------------------------------------------------------------------------- #
def test_map_layer_values_match_tianditu_service() -> None:
    assert MapLayerType.VECTOR.value == "vec_w"
    assert MapLayerType.VECTOR_LABEL.value == "cva_w"
    assert MapLayerType.IMAGE.value == "img_w"
    assert MapLayerType.IMAGE_LABEL.value == "cia_w"


def test_map_layer_relations() -> None:
    assert MapLayerType.VECTOR_LABEL.is_label is True
    assert MapLayerType.VECTOR.is_label is False
    assert MapLayerType.IMAGE.is_image is True
    assert MapLayerType.VECTOR.is_image is False

    assert MapLayerType.VECTOR_LABEL.base_layer is MapLayerType.VECTOR
    assert MapLayerType.IMAGE.base_layer is MapLayerType.IMAGE
    assert MapLayerType.VECTOR.label_layer is MapLayerType.VECTOR_LABEL
    assert MapLayerType.IMAGE_LABEL.label_layer is MapLayerType.IMAGE_LABEL


def test_layers_for_respects_label_switch() -> None:
    assert layers_for(MapLayerType.VECTOR, with_labels=True) == (
        MapLayerType.VECTOR,
        MapLayerType.VECTOR_LABEL,
    )
    assert layers_for(MapLayerType.VECTOR, with_labels=False) == (
        MapLayerType.VECTOR,
    )
    # 传入注记图层时应自动归位到底图
    assert layers_for(MapLayerType.IMAGE_LABEL, with_labels=True) == (
        MapLayerType.IMAGE,
        MapLayerType.IMAGE_LABEL,
    )


# --------------------------------------------------------------------------- #
# 下载任务
# --------------------------------------------------------------------------- #
def test_task_status_display_and_flags() -> None:
    assert DownloadTaskStatus.PAUSED.display_name == "已暂停"
    assert DownloadTaskStatus.COMPLETED.is_terminal is True
    assert DownloadTaskStatus.DOWNLOADING.is_terminal is False
    assert DownloadTaskStatus.DOWNLOADING.is_running is True
    assert DownloadTaskStatus.PAUSED.is_running is False


def test_download_task_defaults() -> None:
    task = DownloadTask()
    assert task.id  # 自动生成
    assert task.status is DownloadTaskStatus.CREATED
    assert task.min_zoom == settings.DEFAULT_MIN_ZOOM
    assert task.max_zoom == settings.DEFAULT_MAX_ZOOM
    assert isinstance(task.output_path, Path)
    assert task.progress.tile_total == 0


def test_download_task_zoom_levels() -> None:
    task = DownloadTask(min_zoom=8, max_zoom=10)
    assert task.zoom_levels == [8, 9, 10]
    assert DownloadTask(min_zoom=10, max_zoom=8).zoom_levels == []


def test_download_task_bounds_validation() -> None:
    valid = DownloadTask(west=116.0, south=39.0, east=117.0, north=40.0)
    assert valid.is_valid_bounds is True
    assert valid.bounds == (116.0, 39.0, 117.0, 40.0)

    # west >= east
    assert DownloadTask(west=117.0, south=39.0, east=116.0, north=40.0).is_valid_bounds is False
    # 纬度越界
    assert DownloadTask(west=116.0, south=-95.0, east=117.0, north=40.0).is_valid_bounds is False


def test_download_progress_percent() -> None:
    assert DownloadProgress().percent == 0.0
    assert DownloadProgress(tile_total=0, tile_done=10).percent == 0.0
    assert DownloadProgress(tile_total=10, tile_done=5).percent == 50.0
    # 不会超过 100
    assert DownloadProgress(tile_total=10, tile_done=20).percent == 100.0


def test_download_task_update_status_touches_timestamp() -> None:
    task = DownloadTask()
    before = task.updated_at
    task.update_status(DownloadTaskStatus.DOWNLOADING)
    assert task.status is DownloadTaskStatus.DOWNLOADING
    assert task.updated_at >= before


# --------------------------------------------------------------------------- #
# 离线包
# --------------------------------------------------------------------------- #
def test_map_package_helpers() -> None:
    package = MapPackage(name="北京", file_size=2 * 1024 * 1024)
    assert package.id
    assert package.size_mb == 2.0
    assert package.is_verified is False
    package.sha256 = "deadbeef"
    assert package.is_verified is True


# --------------------------------------------------------------------------- #
# 设备
# --------------------------------------------------------------------------- #
def test_device_display_and_state() -> None:
    assert DeviceState.DEVICE.is_usable is True
    assert DeviceState.UNAUTHORIZED.is_usable is False
    assert DeviceState.OFFLINE.display_name == "离线"

    assert Device().display_name == "未知设备 — 未知"
    assert Device(serial="ABC123").display_name == "ABC123 — 未知"
    device = Device(serial="ABC123", model="Pixel 7", state=DeviceState.DEVICE)
    assert device.display_name == "Pixel 7 (ABC123) — 已连接"
