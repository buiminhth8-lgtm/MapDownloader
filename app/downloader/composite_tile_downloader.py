"""底图 + 中文注记的合成瓦片下载器（Slice 6）。

它回答的问题比 :class:`~app.downloader.TileDownloader` 大一号：

    TileDownloader          : 一张瓦片 → 一次 HTTP 请求 → 一层图片字节
    CompositeTileDownloader : 一张瓦片 → 两层图片字节 → 合成 → 一张最终 PNG

拓扑（Spec §5 / §6）::

    TileCoordinate
          │
          ├──────────────┐
          ↓              ↓
      vec_w 下载      cva_w 下载        ← asyncio.gather 并发，但**共用**
          │              │              同一个 RateLimiter / AsyncClient /
          └──────┬───────┘              RetryPolicy，所以不会绕过限速
                 ↓
            TileComposer
                 ↓
            final PNG (bytes)

严格模式（Spec §7）
-------------------

    底图成功 + 注记成功 → 合成 → success
    任何一步失败          → failed（**不退化为"只有底图"**）

"失败就整张失败"是刻意的：当用户勾选了"中文注记"，他要的就是一张有字的地图。
悄悄降级成无字底图会得到一份"有的地块有字、有的没有"的地图 ——
而这种地图在手机上看起来完全正常，只是内容不全，属于最难被发现的那类缺陷。

不复制的部分
------------

HTTP 请求、超时、限速、重试、响应合法性判定**全部复用**
:class:`~app.downloader.TileDownloader`：本模块只是"把同一个下载器用两次，
再把结果合起来"。任何一处重复实现都会导致两条链路的限速与重试行为不一致。
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.composer import TileComposeError, TileComposer
from app.downloader.models import TileCoordinate, TileDownloadResult, TileFailureKind
from app.downloader.tile_downloader import TileDownloader
from app.models.map_layer import MapLayerType, MapType

# describe_exception 与单层下载器共用：连接层异常的 str() 可能是空串，
# 不沿 __cause__ 往下找的话，用户只会看到一句"底图下载失败："。
from app.providers import describe_exception

__all__ = ["CompositeTileDownloader"]


_logger = logging.getLogger(__name__)


class CompositeTileDownloader:
    """把"底图 + 注记"下载并合成成一张 PNG 的下载器。

    用法::

        async with TileDownloader(provider) as inner:
            downloader = CompositeTileDownloader(inner)
            result = await downloader.download_tile(tile, MapType.VECTOR)

    生命周期：本类**不拥有**内部的 :class:`TileDownloader`（不负责关闭它）。
    这样"谁创建谁关闭"这条规则只有一个答案，不必判断"这个实例是不是我造的"。
    """

    def __init__(
        self,
        downloader: TileDownloader,
        composer: TileComposer | None = None,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            downloader: 单层瓦片下载器（持有复用的 ``AsyncClient`` 与限速器）。
            composer: 图像合成器，``None`` 时用 :class:`TileComposer` 默认值。
            logger: 日志器，默认用本模块的 logger。

        Raises:
            TypeError: ``downloader`` 不是 :class:`TileDownloader`。
        """
        if not isinstance(downloader, TileDownloader):
            raise TypeError(
                f"downloader 必须是 TileDownloader，实际为 {type(downloader).__name__}"
            )

        self._downloader = downloader
        self._composer = composer or TileComposer()
        self._logger = logger or _logger

    # ------------------------------------------------------------------ #
    # 只读状态
    # ------------------------------------------------------------------ #
    @property
    def downloader(self) -> TileDownloader:
        """底层单层下载器（限速 / 重试都发生在它里面）。"""
        return self._downloader

    @property
    def composer(self) -> TileComposer:
        """图像合成器。"""
        return self._composer

    # ------------------------------------------------------------------ #
    # 下载
    # ------------------------------------------------------------------ #
    async def download_tile(
        self,
        tile: TileCoordinate,
        map_type: MapType,
    ) -> TileDownloadResult:
        """下载并合成一张瓦片。

        Args:
            tile: 瓦片坐标（XYZ）。
            map_type: 地图类型。矢量 → ``vec_w + cva_w``；影像 → ``img_w + cia_w``。

        Returns:
            :class:`TileDownloadResult`。成功时 ``content`` 是**合成后的 PNG**；
            失败时 ``failure_kind`` 是 :attr:`TileFailureKind.COMPOSITE_BASE_FAILED`
            / ``COMPOSITE_LABEL_FAILED`` / ``COMPOSITE_FAILED` 之一，
            ``error`` 里带着**具体是哪一层、因为什么**而失败。

            **不向上抛异常**（除参数非法）：网络抖动、图片损坏都会被折叠成结果，
            与单层下载器的约定保持一致。

        Raises:
            ValueError: ``tile`` / ``map_type`` 类型不对。这属于调用方的编程错误，
                重试多少次都是同样结果，因此直接暴露（批量场景由 worker 兜底）。
        """
        if not isinstance(tile, TileCoordinate):
            raise ValueError(
                f"tile 必须是 TileCoordinate，实际为 {type(tile).__name__}"
            )
        if not isinstance(map_type, MapType):
            raise ValueError(
                f"map_type 必须是 MapType 成员，实际为 {map_type!r}；"
                f"支持 {[item.value for item in MapType]}"
            )

        base_layer, label_layer = map_type.base_layer, map_type.label_layer
        started = time.perf_counter()

        # 并发取两层。注意两者共用同一个 TileDownloader：
        # - 同一个 AsyncClient → 连接复用，不会为每个图层重新握手；
        # - 同一个 RateLimiter → 限速是**全局**的，"最终 1 万张瓦片 ≈ 2 万次
        #   请求"里的每一次请求都受 8 req/s 的约束，不会因为是合成模式就翻倍
        #   打服务端。
        # return_exceptions=True：一个图层炸了不能顺手把另一个也取消掉，
        # 否则失败原因会变得含糊（到底是谁先挂的）。
        base_raw, label_raw = await asyncio.gather(
            self._downloader.download_tile(tile, base_layer),
            self._downloader.download_tile(tile, label_layer),
            return_exceptions=True,
        )

        base = self._as_result(tile, base_raw, base_layer)
        label = self._as_result(tile, label_raw, label_layer)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        attempts = base.attempts + label.attempts

        if not base.success or not base.content:
            return self._failure(
                tile,
                TileFailureKind.COMPOSITE_BASE_FAILED,
                f"底图图层 {base_layer.value} 下载失败：{base.error or '未知错误'}",
                status_code=base.status_code,
                elapsed_ms=elapsed_ms,
                attempts=attempts,
            )
        if not label.success or not label.content:
            return self._failure(
                tile,
                TileFailureKind.COMPOSITE_LABEL_FAILED,
                f"注记图层 {label_layer.value} 下载失败：{label.error or '未知错误'}",
                status_code=label.status_code,
                elapsed_ms=elapsed_ms,
                attempts=attempts,
            )

        try:
            merged = self._composer.compose(base.content, label.content)
        except TileComposeError as exc:
            # 两层都拿到了，是图片本身的问题（无损/尺寸不一致）。这类失败
            # 重试也是同样的结果，所以直接判定失败、不进重试。
            self._logger.warning(
                "瓦片 %s 合成失败（%s + %s）：%s",
                tile,
                base_layer.value,
                label_layer.value,
                exc,
            )
            return self._failure(
                tile,
                TileFailureKind.COMPOSITE_FAILED,
                f"底图与注记合成失败：{exc}",
                status_code=base.status_code,
                elapsed_ms=elapsed_ms,
                attempts=attempts,
            )

        return TileDownloadResult(
            tile=tile,
            success=True,
            content=merged,
            # 用底图的响应码：两层都必须是 200 才走到这里，报哪个都一样，
            # 而底图是这条链路的"主"层，日志里更好读。
            status_code=base.status_code,
            elapsed_ms=elapsed_ms,
            attempts=attempts,
            error=None,
            failure_kind=None,
        )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _as_result(
        tile: TileCoordinate,
        value: TileDownloadResult | BaseException,
        layer: MapLayerType,
    ) -> TileDownloadResult:
        """把 ``asyncio.gather(return_exceptions=True)`` 的产出归一成结果对象。

        未预期的异常在这里被折叠成失败（单层下载器的重试已经跑完了），
        这样批量调度里不会因为一个图层的意外异常而少统计一张瓦片。
        """
        if isinstance(value, TileDownloadResult):
            return value
        return TileDownloadResult(
            tile=tile,
            success=False,
            content=None,
            status_code=None,
            elapsed_ms=0.0,
            attempts=0,
            error=f"{layer.value} 图层下载异常：{describe_exception(value)}",
            failure_kind=TileFailureKind.UNEXPECTED,
        )

    @staticmethod
    def _failure(
        tile: TileCoordinate,
        kind: TileFailureKind,
        error: str,
        *,
        status_code: int | None,
        elapsed_ms: float,
        attempts: int,
    ) -> TileDownloadResult:
        """构造一张合成失败的结果。"""
        return TileDownloadResult(
            tile=tile,
            success=False,
            content=None,
            status_code=status_code,
            elapsed_ms=elapsed_ms,
            attempts=attempts,
            error=error,
            failure_kind=kind,
        )
