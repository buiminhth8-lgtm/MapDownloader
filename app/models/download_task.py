"""下载任务模型。

注意：本模块只描述"数据长什么样"（含"怎么被持久化"所需的字段），
不包含下载调度逻辑（调度在 ``app.downloader``）、也不做任何文件 IO
（JSON 读写由 ``app.tasks.task_store`` 负责）。

本模块是依赖图的叶子之一：只依赖 ``config``，不 import ``app.downloader``
——所以 :meth:`DownloadTask.apply_progress` 用的是鸭子类型而不是引擎模型类型。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from app.config.settings import DEFAULT_MAX_ZOOM, DEFAULT_MIN_ZOOM, DEFAULT_OUTPUT_DIR
from app.models.map_layer import MapType, composition_for


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
    """一次"区域 + 缩放级别"的下载任务描述。

    Slice 5 起这个模型**会被持久化**（``~/MapDownloader/data/tasks.json``），
    因此它是"程序重启后能恢复一次下载"的全部依据：

    - 恢复所需的参数：范围、zoom、图层、输出路径；
    - 恢复所需的进度：目标张数与四个计数器；
    - 恢复时的失败交代：``error``。

    刻意**不含**任何密钥字段：Token 只在内存与请求头里存在，绝不落盘
    （Spec §9）。序列化由 ``app.tasks.task_store`` 负责，本层保持纯数据。

    Attributes:
        map_layer: 图层标识（``MapLayerType.value``，如 ``vec_w``）。
            存字符串而不是枚举，是为了让 JSON 结构与人读的 JSON 都保持稳定；
            从磁盘读回后由业务层转成枚举（转换失败会被当成"记录损坏"处理）。
        total_tiles: 任务目标瓦片数（Tile Math 算出）。
        completed_tiles: 已处理张数 = ``skipped + succeeded + failed``。
        skipped_tiles: **已有瓦片、本次未重下**的张数。
        succeeded_tiles / failed_tiles: 本次真正发起请求的结果。
        error: 失败原因（成功/进行中为 ``None``）。
    """

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

    #: 图层标识（``MapLayerType.value``）。空串表示"未记录"（旧记录/手工构造）。
    #:
    #: 合成模式（Slice 6）下这里存的是**底图**图层（``vec_w`` / ``img_w``）：
    #: 一个任务对应"一张最终瓦片"，它必然有一张底图；注记有没有合进去由
    #: :attr:`with_labels` 单独表达。把两个图层塞进一个字符串会逼所有人去解析它。
    map_layer: str = ""

    #: 地图类型（``MapType.value``：``vector`` / ``image``）。Slice 6 新增。
    #: 空串表示"未记录"（Slice 5 及以前的记录），此时由 :attr:`map_layer` 反推。
    map_type: str = ""

    #: 本次是否把中文注记**合成**进瓦片。Slice 6 新增。
    #: 缺省的 ``False`` 不是随手取的：Slice 5 及以前的版本只下载底图，
    #: 老记录里没有这个键，读回来正好等于当时的事实。
    with_labels: bool = False

    #: 下载区域类型（``RegionType.value``：``rectangle`` / ``polygon``）。Slice 16 新增。
    #: 空串表示"这条记录没有区域信息"——Slice ≤15 建的老任务都是这个状态，
    #: 恢复时按矩形处理（那正是它们当初的下载行为）。
    region_type: str = ""

    #: 行政区标识（``china`` …）。矩形任务与老任务为空串。
    region_id: str = ""

    #: 边界数据版本，用来回答"这批瓦片是按哪一版边界裁的"。
    #: 边界数据更新后同一个 ``region_id`` 的 expected 瓦片集会变，
    #: 覆盖度检查需要它才能说清"对不上是因为换了数据"。
    #:
    #: 刻意**不把 GeoJSON 本身存进来**（Spec §13）：一份省级边界就有几 MB，
    #: 塞进 ``tasks.json`` 会让每次进度落盘都变成一次大写入。恢复时按
    #: ``region_id`` 回到预设表找边界文件即可。
    boundary_version: str = ""
    coverage_path: str = ""

    #: 进度计数器（Slice 5 起是持久化的权威来源，Spec §7）
    total_tiles: int = 0
    completed_tiles: int = 0
    succeeded_tiles: int = 0
    failed_tiles: int = 0
    skipped_tiles: int = 0

    #: 非核心字段：时间戳与进度快照
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    #: Slice 0 的占位快照。Slice 5 起由 :meth:`apply_progress` 与上面那组
    #: 扁平计数器同步维护，保证两者不会各说各话。
    progress: DownloadProgress = field(default_factory=DownloadProgress)

    #: 失败原因；正常结束/进行中为 ``None``
    error: str | None = None

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
    def source_map_type(self) -> MapType | None:
        """任务的地图类型（矢量 / 影像）；认不出来时返回 ``None``。

        Slice 6 起新建的任务都会显式记下 :attr:`map_type`；Slice 5 及以前的
        记录没有这个字段，但它们的 :attr:`map_layer` 必然属于某个地图类型
        （``vec_w`` → 矢量），所以**能反推的就反推**，不必让老记录作废。
        两条路都认不出来（例如有人手工改了 JSON）才返回 ``None``，
        由调用方决定是拒绝续传还是按默认值继续。
        """
        resolved = MapType.parse(self.map_type)
        if resolved is not None:
            return resolved
        if not self.map_layer:
            return None
        try:
            return MapType.from_layer(self.map_layer)
        except ValueError:
            return None

    @property
    def composition(self) -> str:
        """本任务的合成方式（``single`` / ``merged``）。

        由 :attr:`with_labels` 推出，**不单独存一份**：两个字段各存一次就
        迟早会出现 ``labels=true`` 配 ``composition=single`` 这种自相矛盾的
        记录，而续传校验正是靠它们比对文件。
        """
        return composition_for(self.with_labels)

    @property
    def is_valid_bounds(self) -> bool:
        """经纬度包围盒是否合法（仅做范围与顺序检查，不做投影转换）。"""
        return (
            -180.0 <= self.west < self.east <= 180.0
            and -90.0 <= self.south < self.north <= 90.0
        )

    @property
    def percent(self) -> float:
        """完成百分比，``0.0 ~ 100.0``；未知总量时返回 ``0.0``。"""
        if self.total_tiles <= 0:
            return 0.0
        return min(100.0, self.completed_tiles / self.total_tiles * 100.0)

    @property
    def remaining_tiles(self) -> int:
        """还需要下载的张数（``total - completed``，已跳过的不算欠账）。"""
        return max(0, self.total_tiles - self.completed_tiles)

    @property
    def can_resume(self) -> bool:
        """是否值得显示"继续"。

        已完成的任务不必再继续；其余状态（含首次创建、暂停、失败、取消）
        都可以"接着下"，因为续传逻辑本来就是"按已有瓦片补齐差集"。
        """
        return self.status is not DownloadTaskStatus.COMPLETED

    @property
    def is_interrupted(self) -> bool:
        """是否是"上次进程没来得及收尾"的状态（重启后要降级为 PAUSED）。"""
        return self.status.is_running

    # ------------------------------------------------------------------ #
    # 状态流转
    # ------------------------------------------------------------------ #
    def update_status(self, status: DownloadTaskStatus) -> None:
        """更新状态并刷新 ``updated_at``。"""
        self.status = status
        self.updated_at = datetime.now()

    def apply_progress(self, progress) -> None:
        """把下载引擎的进度快照同步进来（``DownloadProgress`` 引擎版）。

        刻意用**鸭子类型**（只要有那几个字段就行）而不是 import 引擎模型：
        ``app.models`` 是依赖图的叶子，不能反向依赖 ``app.downloader``
        ——否则 models 就不再是"谁都能依赖、自己不依赖谁"的那一层了。

        Args:
            progress: 任何具备 ``total/completed/succeeded/failed/skipped``
                属性的对象。
        """
        self.total_tiles = max(0, int(getattr(progress, "total", 0) or 0))
        self.completed_tiles = max(0, int(getattr(progress, "completed", 0) or 0))
        self.succeeded_tiles = max(0, int(getattr(progress, "succeeded", 0) or 0))
        self.failed_tiles = max(0, int(getattr(progress, "failed", 0) or 0))
        self.skipped_tiles = max(0, int(getattr(progress, "skipped", 0) or 0))

        # 同步 Slice 0 的占位快照，避免两个进度视图各说各话
        self.progress.tile_total = self.total_tiles
        self.progress.tile_done = self.completed_tiles
        self.progress.tile_failed = self.failed_tiles
        current_zoom = getattr(progress, "current_zoom", None)
        if current_zoom is not None:
            self.progress.current_zoom = int(current_zoom)
        self.progress.speed_tiles_per_second = float(
            getattr(progress, "tiles_per_second", 0.0) or 0.0
        )

    def progress_text(self) -> str:
        """进度文案（任务列表用），例如 ``64%（已有 32,418 / 50,000）``。"""
        text = f"{self.percent:.0f}%"
        if self.total_tiles:
            text += f"（{self.completed_tiles:,} / {self.total_tiles:,}"
            if self.skipped_tiles:
                text += f"，已有 {self.skipped_tiles:,}"
            text += "）"
        return text
