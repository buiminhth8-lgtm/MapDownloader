"""瓦片源 Provider 的轻量接口约定。

本模块只定义"契约"：一个 Provider 至少能把 ``(图层, z, x, y)`` 翻译成可请求的
URL。刻意不使用抽象基类或插件注册表 —— 本项目只有天地图一个真实数据源，
引入框架只会增加阅读成本。

约束：

- 本层不依赖 PySide6，不做任何网络请求，可被单元测试完全覆盖。
- Provider 不自带并发 / 重试策略，那些属于 ``app.downloader``（Slice 3+）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.models.map_layer import MapLayerType

__all__ = ["TileProvider", "TileTestResult"]


@runtime_checkable
class TileProvider(Protocol):
    """瓦片源接口。

    实现方需要保证：同样的 ``(layer, zoom, x, y)`` 永远产出同样的 URL
    （确定性），否则断点续传与去重无法成立。
    """

    def build_tile_url(
        self,
        layer: MapLayerType,
        zoom: int,
        x: int,
        y: int,
    ) -> str:
        """构造单张瓦片的请求 URL。

        Args:
            layer: 图层类型。
            zoom: 缩放级别。
            x: 瓦片列号。
            y: 瓦片行号（XYZ 约定，自北向南）。

        Returns:
            完整 URL。

        Raises:
            ValueError: 参数非法。
        """
        ...


@dataclass(frozen=True)
class TileTestResult:
    """一次单瓦片连通性测试的结果。

    这是个纯数据对象，不表示"瓦片已保存"——本 Slice 不写任何文件。
    """

    success: bool
    status_code: int | None
    content_type: str | None
    content_length: int
    elapsed_ms: float
    error: str | None

    @property
    def has_response(self) -> bool:
        """是否拿到了 HTTP 响应（无论成败）。"""
        return self.status_code is not None

    @property
    def elapsed_seconds(self) -> float:
        """耗时（秒），便于与 httpx 的 timeout 单位对齐。"""
        return self.elapsed_ms / 1000.0

    def summary(self) -> str:
        """压缩成一行便于写日志。

        只包含状态码 / 体积 / 耗时，不含 URL —— Token 脱敏由调用方负责。
        """
        if self.has_response:
            head = f"HTTP {self.status_code}"
        else:
            head = "无响应"
        return f"{head} · {self.content_length} bytes · {self.elapsed_ms:.0f} ms"
