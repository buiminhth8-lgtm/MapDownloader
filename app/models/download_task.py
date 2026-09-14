"""下载任务模型。

注意：本模块只描述"数据长什么样"，不包含任何下载调度逻辑
（调度在 ``app.downloader``，Slice 2 实现）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from app.config.settings import DEFAULT_MAX_ZOOM, DEFAULT_MIN_ZOOM, DEFAULT_OUTPUT_DIR


class DownloadTaskStatus(Enum):
    """下载任务生命周期状态。"""

    CREATED = "created"
    CALCULATING = "calculating"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    PACKAGING = "packaging"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def display_name(self) -> str:
        """状态的中文展示名，供 UI 直接使用。"""
        return _STATUS_DISPLAY_NAMES[self]

    @property
    def is_terminal(self) -> bool:
        """是否为终态（不会再自行流转）。"""
        return self in (
            DownloadTaskStatus.COMPLETED,
            DownloadTaskStatus.FAILED,
            DownloadTaskStatus.CANCELLED,
        )

    @property
    def is_running(self) -> bool:
        """是否处于"可被暂停/继续"的运行区间。"""
        return self in (
            DownloadTaskStatus.CALCULATING,
            DownloadTaskStatus.DOWNLOADING,
            DownloadTaskStatus.PACKAGING,
            DownloadTaskStatus.VALIDATING,
        )


_STATUS_DISPLAY_NAMES: dict[DownloadTaskStatus, str] = {
    DownloadTaskStatus.CREATED: "已创建",
    DownloadTaskStatus.CALCULATING: "计算中",
    DownloadTaskStatus.DOWNLOADING: "下载中",
    DownloadTaskStatus.PAUSED: "已暂停",
    DownloadTaskStatus.PACKAGING: "打包中",
    DownloadTaskStatus.VALIDATING: "校验中",
    DownloadTaskStatus.COMPLETED: "已完成",
    DownloadTaskStatus.FAILED: "失败",
    DownloadTaskStatus.CANCELLED: "已取消",
}


def new_task_id() -> str:
    """生成任务 ID（短 uuid，够用且不暴露自增信息）。"""
    return uuid.uuid4().hex[:12]


@dataclass
class DownloadProgress:
    """任务进度快照。Slice 0 只用默认值，Slice 2 起由下载器实时写入。"""

    current_zoom: int = 0
    tile_total: int = 0
    tile_done: int = 0
    tile_failed: int = 0
    #: 每秒完成瓦片数（瞬时速度，由下载器平滑计算）
    speed_tiles_per_second: float = 0.0

    @property
    def percent(self) -> float:
        """总进度百分比，0.0 ~ 100.0。"""
        if self.tile_total <= 0:
            return 0.0
        return min(100.0, self.tile_done / self.tile_total * 100.0)


@dataclass
class DownloadTask:
    """一次"区域 + 缩放级别"的下载任务描述。"""

    id: str = field(default_factory=new_task_id)
    name: str = "未命名任务"
    status: DownloadTaskStatus = DownloadTaskStatus.CREATED
    min_zoom: int = DEFAULT_MIN_ZOOM
    max_zoom: int = DEFAULT_MAX_ZOOM
    west: float = 0.0
    south: float = 0.0
    east: float = 0.0
    north: float = 0.0
    output_path: Path = field(default_factory=lambda: DEFAULT_OUTPUT_DIR)

    #: 非核心字段：时间戳与进度快照
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    progress: DownloadProgress = field(default_factory=DownloadProgress)

    # ------------------------------------------------------------------ #
    # 派生信息
    # ------------------------------------------------------------------ #
    @property
    def zoom_levels(self) -> list[int]:
        """任务覆盖的缩放级别列表（含首尾）。"""
        if self.max_zoom < self.min_zoom:
            return []
        return list(range(self.min_zoom, self.max_zoom + 1))

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(west, south, east, north) 形式的包围盒。"""
        return (self.west, self.south, self.east, self.north)

    @property
    def is_valid_bounds(self) -> bool:
        """经纬度包围盒是否合法（仅做范围与顺序检查，不做投影转换）。"""
        return (
            -180.0 <= self.west < self.east <= 180.0
            and -90.0 <= self.south < self.north <= 90.0
        )

    # ------------------------------------------------------------------ #
    # 状态流转
    # ------------------------------------------------------------------ #
    def update_status(self, status: DownloadTaskStatus) -> None:
        """更新状态并刷新 ``updated_at``。"""
        self.status = status
        self.updated_at = datetime.now()
