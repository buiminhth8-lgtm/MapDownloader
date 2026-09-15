"""下载引擎的数据模型（Slice 3）。

本模块只描述"数据长什么样"，不含任何调度 / 网络 / 落盘逻辑——那些在
``tile_downloader``（单瓦片）与 ``download_manager``（批量调度）里。

边界约定
--------

- 本层不依赖 PySide6，也不依赖 ``httpx``：可被纯单元测试完全覆盖。
- 本层的瓦片坐标是"输入"：由调用方用 Slice 1 的 Tile Math 算好后传进来，
  这里不做任何投影换算。
- :attr:`TileDownloadResult.content` 持有瓦片字节，**只在内存中传递**。
  Slice 3 不落盘，消费方通过 ``on_tile`` 回调接管（Slice 4 的 MBTilesWriter）。

命名说明
--------

本模块的 :class:`DownloadProgress` 与 ``app.models.download_task.DownloadProgress``
同名但职责不同：后者是"任务模型里的持久化进度快照"，前者是"下载引擎向前抛的
实时统计"。二者刻意不合并——任务模型属于 UI 状态，引擎模型属于运行时数据。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from app.geo import format_size

__all__ = [
    "TileCoordinate",
    "TileFailureKind",
    "TileDownloadResult",
    "DownloadProgress",
    "DownloadSummary",
    "TileCallback",
    "ProgressCallback",
]


# --------------------------------------------------------------------------- #
# 瓦片坐标
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, order=True)
class TileCoordinate:
    """一张瓦片的 XYZ 坐标。

    ``order=True`` 让瓦片按 ``(z, x, y)`` 可排序，便于日志与测试做确定性比较。
    """

    z: int
    x: int
    y: int

    def __str__(self) -> str:
        """``"z/x/y"`` 形式，日志里最常用的写法。"""
        return f"{self.z}/{self.x}/{self.y}"

    @property
    def as_tuple(self) -> tuple[int, int, int]:
        """``(z, x, y)``，便于解包。"""
        return (self.z, self.x, self.y)

    @property
    def tiles_per_axis(self) -> int:
        """该缩放级别下每轴的瓦片数 ``2**z``。"""
        return 1 << self.z


# --------------------------------------------------------------------------- #
# 失败分类
# --------------------------------------------------------------------------- #
class TileFailureKind(Enum):
    """瓦片下载失败的原因分类。

    存在的意义是让"要不要重试"变成**结构化判断**而不是字符串匹配：
    重试策略只认这个枚举，UI 也能据此给出人话提示。
    """

    #: HTTP 非 200（是否重试由状态码决定）
    HTTP_STATUS = "http_status"
    #: 连接层面的异常（DNS / 连接被拒 / 中断）
    NETWORK = "network"
    #: 请求超时
    TIMEOUT = "timeout"
    #: 响应体为空
    EMPTY_RESPONSE = "empty_response"
    #: 响应体不是合法图片瓦片（HTML / XML / JSON 错误页等）
    INVALID_CONTENT = "invalid_content"
    #: 响应体超过上限
    RESPONSE_TOO_LARGE = "response_too_large"
    #: 兜底：未预期的异常（会被重试，但次数受 max_attempts 约束）
    UNEXPECTED = "unexpected"

    @property
    def display_name(self) -> str:
        """中文展示名。"""
        return _FAILURE_DISPLAY_NAMES[self]


_FAILURE_DISPLAY_NAMES: dict[TileFailureKind, str] = {
    TileFailureKind.HTTP_STATUS: "HTTP 错误",
    TileFailureKind.NETWORK: "网络错误",
    TileFailureKind.TIMEOUT: "请求超时",
    TileFailureKind.EMPTY_RESPONSE: "空响应",
    TileFailureKind.INVALID_CONTENT: "响应不是有效瓦片",
    TileFailureKind.RESPONSE_TOO_LARGE: "响应体积异常",
    TileFailureKind.UNEXPECTED: "未知错误",
}


# --------------------------------------------------------------------------- #
# 单瓦片结果
# --------------------------------------------------------------------------- #
@dataclass
class TileDownloadResult:
    """一次瓦片下载（含重试）的最终结果。

    Attributes:
        tile: 瓦片坐标。
        success: 是否成功拿到一张合法瓦片。
        content: 瓦片字节；失败时为 ``None``。**不落盘**。
        status_code: 最后一次尝试的 HTTP 状态码；未拿到响应时为 ``None``。
        elapsed_ms: 整个下载（含所有重试与退避等待）的墙钟耗时，毫秒。
        attempts: 实际发起的请求次数（成功时 ``>= 1``）。
        error: 失败原因的人话描述；成功时为 ``None``。
        failure_kind: 失败分类；成功时为 ``None``。这是本 Slice 在 Spec 的
            7 个字段之外补的一个**带默认值**的字段，用于让重试判定与 UI
            提示摆脱字符串匹配；按位置构造原有 7 个字段的代码不受影响。
    """

    tile: TileCoordinate
    success: bool
    content: bytes | None
    status_code: int | None
    elapsed_ms: float
    attempts: int
    error: str | None
    failure_kind: TileFailureKind | None = None

    # ------------------------------------------------------------------ #
    # 派生信息
    # ------------------------------------------------------------------ #
    @property
    def content_length(self) -> int:
        """瓦片字节数；失败时为 0。"""
        return len(self.content) if self.content else 0

    @property
    def has_response(self) -> bool:
        """是否拿到了 HTTP 响应（无论成败）。"""
        return self.status_code is not None

    @property
    def has_content(self) -> bool:
        """是否持有可落盘的瓦片字节。"""
        return bool(self.content)

    @property
    def was_retried(self) -> bool:
        """是否发生过重试。"""
        return self.attempts > 1

    def summary(self) -> str:
        """压成一行便于写日志。不含 URL —— Token 脱敏由调用方负责。"""
        head = f"HTTP {self.status_code}" if self.has_response else "无响应"
        state = "成功" if self.success else f"失败（{self.error or '未知错误'}）"
        return (
            f"{self.tile} {state} · {head} · {self.content_length} bytes"
            f" · 尝试 {self.attempts} 次 · {self.elapsed_ms:.0f} ms"
        )


# --------------------------------------------------------------------------- #
# 进度
# --------------------------------------------------------------------------- #
@dataclass
class DownloadProgress:
    """下载进度的实时快照。

    ``total`` 在任务开始前就由 Slice 1 的瓦片统计得出，因此进度条从一开始
    就有正确的分母，而不是"边发现边增长"。

    ``skipped`` 只在被取消时有意义：表示因取消而**未下载**的瓦片数。
    本 Slice 没有断点续传，这些瓦片不会在后续被补下。
    """

    total: int = 0
    completed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    bytes_downloaded: int = 0
    tiles_per_second: float = 0.0
    current_zoom: int | None = None

    @property
    def percent(self) -> float:
        """完成百分比，``0.0 ~ 100.0``。"""
        if self.total <= 0:
            return 100.0 if self.completed > 0 else 0.0
        return min(100.0, self.completed / self.total * 100.0)

    @property
    def remaining(self) -> int:
        """尚未处理的瓦片数（不含被跳过的）。"""
        return max(0, self.total - self.completed - self.skipped)

    @property
    def success_rate(self) -> float:
        """已完成部分里的成功率，``0.0 ~ 1.0``。"""
        if self.completed <= 0:
            return 0.0
        return self.succeeded / self.completed


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
@dataclass
class DownloadSummary:
    """一次批量下载的最终汇总，由 ``DownloadManager.download`` 返回。"""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    bytes_downloaded: int = 0
    duration_seconds: float = 0.0
    average_tiles_per_second: float = 0.0
    cancelled: bool = False

    @property
    def completed(self) -> int:
        """已处理（成功 + 失败）的瓦片数。"""
        return self.succeeded + self.failed

    @property
    def is_success(self) -> bool:
        """是否"跑完了且一张都没失败"。"""
        return not self.cancelled and self.failed == 0 and self.succeeded > 0

    def as_lines(self) -> list[str]:
        """多行展示文案，供 GUI / 日志直接使用。"""
        lines = [
            f"总瓦片：{self.total:,}",
            f"成功：{self.succeeded:,}",
            f"失败：{self.failed:,}",
        ]
        if self.skipped:
            lines.append(f"跳过：{self.skipped:,}")
        lines.append(f"下载量：{format_size(self.bytes_downloaded)}")
        lines.append(f"耗时：{self.duration_seconds:.2f} s")
        lines.append(f"平均速度：{self.average_tiles_per_second:.1f} 瓦片/秒")
        if self.cancelled:
            lines.append("状态：已取消（瓦片未落盘）")
        return lines


# --------------------------------------------------------------------------- #
# 回调类型
#
# 定义在数据类之后，才能直接引用 TileDownloadResult（不靠字符串前向引用）。
# --------------------------------------------------------------------------- #
#: 单瓦片回调：下载**成功**时被调用，返回 ``None`` 或 awaitable 均可。
#:
#: 这是给 Slice 4 的 MBTilesWriter 预留的消费口：引擎只负责"把瓦片交出去"，
#: 不关心它被写进数据库还是被丢弃。失败瓦片不进这个回调 —— 失败通过进度里的
#: ``failed`` 计数与最终 :class:`DownloadSummary` 暴露。
TileCallback = Callable[[TileDownloadResult], Awaitable[None] | None]

#: 进度回调：引擎按 ``PROGRESS_EMIT_INTERVAL_SECONDS`` 节流后调用。
ProgressCallback = Callable[[DownloadProgress], Awaitable[None] | None]
