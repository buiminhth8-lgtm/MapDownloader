"""天地图连通性测试的后台执行器。

这是薄薄的**线程胶水**：把一次 :func:`app.providers.test_tile` 调用放到 UI 主线程
之外，并把结果通过信号送回主线程。

分工说明：URL 构造与 HTTP 细节全在 ``app.providers``；``MainWindow`` 只负责
"读表单 → 建 worker → 展示结果"。本类不含任何业务规则。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal

from app.config.settings import DEFAULT_TILE_TIMEOUT_SECONDS
from app.models.map_layer import MapLayerType
from app.providers import TiandituProvider, TileTestResult, test_tile

__all__ = ["ConnectionTestWorker"]

#: 后台线程内的兜底错误前缀
_UNEXPECTED_ERROR_TEMPLATE = "测试异常：{error}"


class ConnectionTestWorker(QThread):
    """在后台线程执行一次单瓦片连通性测试。

    为什么必须独立线程：``test_tile`` 会发起真实网络请求（最长 ``timeout`` 秒），
    在 UI 主线程里执行会让整个窗口冻结。

    信号名刻意不叫 ``finished``——``QThread`` 自己已经有一个 ``finished``。
    """

    #: 测试结束，携带 :class:`~app.providers.base.TileTestResult`（用 ``object``
    #: 传递，避免 Qt 元类型系统对自定义 dataclass 的依赖）
    result_ready = Signal(object)

    def __init__(
        self,
        provider: TiandituProvider,
        layer: MapLayerType,
        zoom: int,
        x: int,
        y: int,
        timeout: float = DEFAULT_TILE_TIMEOUT_SECONDS,
        parent: QObject | None = None,
    ) -> None:
        """
        Args:
            provider: 天地图瓦片源（持有 Token，不对外展示）。
            layer: 要测试的图层。
            zoom: 测试用的缩放级别。
            x: 瓦片列号。
            y: 瓦片行号。
            timeout: 网络超时（秒）。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.provider = provider
        self.layer = layer
        self.zoom = zoom
        self.x = x
        self.y = y
        self.timeout = timeout

    def run(self) -> None:  # noqa: D102 - QThread 约定的入口
        try:
            result = test_tile(
                self.provider,
                self.layer,
                self.zoom,
                self.x,
                self.y,
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            # 兜底：任何意外（含 ValueError）都转成结果对象，
            # 绝不让异常冒泡进 Qt 事件循环导致进程崩溃。
            result = TileTestResult(
                success=False,
                status_code=None,
                content_type=None,
                content_length=0,
                elapsed_ms=0.0,
                error=_UNEXPECTED_ERROR_TEMPLATE.format(error=exc),
            )
        self.result_ready.emit(result)
