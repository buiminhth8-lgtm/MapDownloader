"""异步批量瓦片下载引擎（Slice 3）。

对外入口::

    from app.downloader import DownloadManager, TileDownloader, TileCoordinate

    async with TileDownloader(provider) as downloader:
        manager = DownloadManager(downloader, concurrency=6)
        summary = await manager.download(bounds, 8, 12, layer, on_progress=...)

模块划分
--------

==================== ==========================================================
``models``           数据模型：瓦片坐标、单瓦片结果、进度、汇总、回调类型
``retry_policy``     指数退避与"这个失败值不值得重试"（纯逻辑）
``rate_limiter``     预约式异步限速（纯逻辑，构造不需要事件循环）
``tile_downloader``  单瓦片：连接复用 + 超时 + 限速 + 重试 + 响应判定
``download_manager`` 批量：瓦片枚举 → 有界队列 → 固定 worker → 回调
==================== ==========================================================

边界约定
--------

- 本层**不落盘**：瓦片字节通过 ``on_tile`` 回调交给消费方。
  ``MBTilesWriter`` 属于 Slice 4，会实现同一个回调签名。
- 本层**不 import PySide6**：GUI 通过 ``app.ui.download_worker`` 的线程胶水
  驱动它，因此引擎可以在纯 ``asyncio`` 环境（含单元测试）中独立运行。
- 本层**不 import `app.mbtiles` / `app.package`**：没有 SQLite，没有 ADB。
"""

from app.downloader.download_manager import DownloadManager, iter_tile_coordinates
from app.downloader.models import (
    DownloadProgress,
    DownloadSummary,
    ProgressCallback,
    TileCallback,
    TileCoordinate,
    TileDownloadResult,
    TileFailureKind,
)
from app.downloader.rate_limiter import RateLimiter
from app.downloader.retry_policy import (
    RETRYABLE_STATUS_CODES,
    RetryPolicy,
    is_retryable_status_code,
    should_retry,
)
from app.downloader.tile_downloader import TileDownloader

__all__ = [
    # 调度
    "DownloadManager",
    "iter_tile_coordinates",
    # 单瓦片
    "TileDownloader",
    # 策略
    "RetryPolicy",
    "should_retry",
    "is_retryable_status_code",
    "RETRYABLE_STATUS_CODES",
    "RateLimiter",
    # 模型
    "TileCoordinate",
    "TileDownloadResult",
    "TileFailureKind",
    "DownloadProgress",
    "DownloadSummary",
    "TileCallback",
    "ProgressCallback",
]
