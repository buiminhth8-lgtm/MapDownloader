"""瓦片源 Provider 层。

Slice 2 已实现（天地图 WMTS Provider 与单瓦片连通性验证）：

- :class:`~app.providers.base.TileProvider` —— 轻量接口约定（构造 URL）
- :class:`~app.providers.tianditu.TiandituProvider` —— 天地图 WMTS URL 构造
  （矢量 ``vec_w``/``cva_w``、影像 ``img_w``/``cia_w``，仅 Web 墨卡托 ``_w``）
- ``t0 ~ t7`` 确定性分片（``(x + y) % 8``）
- :func:`~app.providers.tianditu.test_tile` —— 单张瓦片连通性测试（不落盘）
- :func:`~app.providers.tianditu.is_valid_tile_response` —— 按 magic bytes
  判定 PNG / JPEG，识别 HTML / XML / JSON / 纯文本错误页
- :func:`~app.providers.tianditu.http_error_message` —— HTTP 状态码语义的唯一
  出处（连通性测试与批量下载引擎共用，避免各自抄一份映射表）
- Token 脱敏工具 :func:`~app.providers.tianditu.mask_token` /
  :func:`~app.providers.tianditu.mask_url`

约束：本层不依赖 PySide6；不含并发、重试、批量下载（属于 ``app.downloader``，
Slice 3+）；不写任何文件。UI 不得自行拼接瓦片 URL，必须调用本层。
"""

from app.providers.base import TileProvider, TileTestResult
from app.providers.tianditu import (
    JPEG_SIGNATURE,
    PNG_SIGNATURE,
    TiandituProvider,
    describe_response,
    detect_image_format,
    http_error_message,
    is_error_content_type,
    is_valid_tile_response,
    mask_token,
    mask_url,
    server_host,
    server_index,
    test_tile,
)

__all__ = [
    # base
    "TileProvider",
    "TileTestResult",
    # tianditu
    "TiandituProvider",
    "test_tile",
    "is_valid_tile_response",
    "is_error_content_type",
    "describe_response",
    "detect_image_format",
    "http_error_message",
    "mask_token",
    "mask_url",
    "server_index",
    "server_host",
    "PNG_SIGNATURE",
    "JPEG_SIGNATURE",
]
