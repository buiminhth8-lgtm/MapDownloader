"""底图 + 中文注记的合成下载器（Slice 6，Spec §6 / §7）。

覆盖三个要点：

1. **一张最终瓦片 = 两次 WMTS 请求**，且两次都打在正确的图层上；
2. 三类失败原因分开归类（``COMPOSITE_BASE_FAILED`` / ``COMPOSITE_LABEL_FAILED``
   / ``COMPOSITE_FAILED``）—— 用户要能分辨"网络问题"还是"图片问题"；
3. 生命周期：合成下载器**不拥有**内部单层下载器（谁创建谁关闭）。
"""

from __future__ import annotations

import asyncio
import io

import httpx
import pytest
from PIL import Image

from app.composer import TileComposeError, TileComposer
from app.downloader import (
    CompositeTileDownloader,
    RateLimiter,
    RetryPolicy,
    TileCoordinate,
    TileDownloader,
    TileFailureKind,
)
from app.models import MapLayerType, MapType
from app.providers import PNG_SIGNATURE, TiandituProvider

FAKE_TOKEN = "abcdefghijklmn"
NO_RETRY = RetryPolicy(max_attempts=1, base_delay_seconds=0.0, max_delay_seconds=0.0)
NO_RATE_LIMIT = RateLimiter.disabled()

TILE = TileCoordinate(z=12, x=3367, y=1564)


def _png(color: tuple[int, int, int, int] = (255, 0, 0, 255)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (256, 256), color).save(buffer, format="PNG")
    return buffer.getvalue()


class LayerServer:
    """按图层名分别作答的假天地图服务。

    ``layers`` 参数形如 ``{"vec_w": <bytes|int|"junk">}``：给 bytes 就正常返回，
    给状态码就返回该错误码，给 ``"junk"`` 就返回一段不是图片的正文。
    """

    def __init__(self, layers: dict[str, object] | None = None) -> None:
        self._layers = layers or {}
        self.requests: list[str] = []

    @staticmethod
    def _layer_of(request: httpx.Request) -> str:
        # 天地图把图层拆在 URL 路径（vec_w）与 LAYER 参数（vec）两处，
        # 这里按请求真实用到的那个取值，避免"测试假设"与生产不一致。
        return str(request.url.params.get("LAYER", ""))

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        layer = self._layer_of(request)
        self.requests.append(layer)
        payload = self._layers.get(layer)
        if isinstance(payload, int):
            return httpx.Response(payload, content=b"<html>err</html>")
        if payload == "junk":
            return httpx.Response(
                200, content=b"<html>not an image</html>", headers={"content-type": "text/html"}
            )
        if isinstance(payload, bytes):
            return httpx.Response(200, content=payload, headers={"content-type": "image/png"})
        return httpx.Response(200, content=_png(), headers={"content-type": "image/png"})


def _make_pair(server: LayerServer):
    """造一对 ``(底层下载器, 合成下载器)``。"""
    inner = TileDownloader(
        TiandituProvider(FAKE_TOKEN),
        transport=httpx.MockTransport(server),
        retry_policy=NO_RETRY,
        rate_limiter=NO_RATE_LIMIT,
    )
    return inner, CompositeTileDownloader(inner)


# --------------------------------------------------------------------------- #
# 两次请求
# --------------------------------------------------------------------------- #
def test_compose_mode_issues_two_requests_one_per_layer() -> None:
    """一张最终瓦片要请求底图 + 中文注记各一次（Spec §6）。"""
    server = LayerServer()

    async def run():
        inner, downloader = _make_pair(server)
        try:
            return await downloader.download_tile(TILE, MapType.VECTOR)
        finally:
            await inner.close()

    result = asyncio.run(run())

    assert result.success is True
    assert sorted(server.requests) == ["cva", "vec"]
    # attempts 是**两层的和**：进度里的"尝试次数"必须反映真实请求量
    assert result.attempts == 2


@pytest.mark.parametrize(
    ("map_type", "expected"),
    [
        (MapType.VECTOR, ["cva", "vec"]),
        (MapType.IMAGE, ["cia", "img"]),
    ],
)
def test_requests_go_to_the_layers_of_the_map_type(
    map_type: MapType, expected: list[str]
) -> None:
    """矢量要 ``vec``+``cva``，影像要 ``img``+``cia`` —— 不能串台。"""
    server = LayerServer()

    async def run():
        inner, downloader = _make_pair(server)
        try:
            return await downloader.download_tile(TILE, map_type)
        finally:
            await inner.close()

    asyncio.run(run())

    assert sorted(server.requests) == expected


def test_successful_result_is_png() -> None:
    """成功时交给 MBTiles 的必须是合成后的 PNG，而不是某一层的原始字节。"""
    server = LayerServer({"vec": _png((255, 0, 0, 255)), "cva": _png((0, 0, 255, 255))})

    async def run():
        inner, downloader = _make_pair(server)
        try:
            return await downloader.download_tile(TILE, MapType.VECTOR)
        finally:
            await inner.close()

    result = asyncio.run(run())

    assert result.success is True
    assert result.content is not None
    with Image.open(io.BytesIO(result.content)) as image:
        assert image.size == (256, 256)
        assert image.mode == "RGBA"
    assert result.failure_kind is None


# --------------------------------------------------------------------------- #
# 三类失败原因
# --------------------------------------------------------------------------- #
def test_base_failure_is_reported_as_base() -> None:
    """底图挂了 → ``COMPOSITE_BASE_FAILED``，并且报错里点名 ``vec_w``。"""
    server = LayerServer({"vec": 500})

    async def run():
        inner, downloader = _make_pair(server)
        try:
            return await downloader.download_tile(TILE, MapType.VECTOR)
        finally:
            await inner.close()

    result = asyncio.run(run())

    assert result.success is False
    assert result.failure_kind is TileFailureKind.COMPOSITE_BASE_FAILED
    assert "vec_w" in (result.error or "")
    assert result.content is None


def test_label_failure_is_reported_as_label() -> None:
    """注记挂了 → ``COMPOSITE_LABEL_FAILED``（底图是好的，别混为一谈）。"""
    server = LayerServer({"cva": 404})

    async def run():
        inner, downloader = _make_pair(server)
        try:
            return await downloader.download_tile(TILE, MapType.VECTOR)
        finally:
            await inner.close()

    result = asyncio.run(run())

    assert result.failure_kind is TileFailureKind.COMPOSITE_LABEL_FAILED
    assert "cva_w" in (result.error or "")


def test_uncomposable_images_are_reported_as_compose_failure() -> None:
    """两层都拿到了、但其中一张解不开 → ``COMPOSITE_FAILED``。

    这一类**不该重试**：同样的字节再来一次还是合不出来。

    注记这里刻意用"PNG 文件头 + 垃圾正文"：单层下载器按 magic bytes 判定，
    会认为它是一张合法 PNG 放行；只有真正解码时才会失败——这正是合成模式
    独有的失败面，也是它必须与"下载失败"分开计数的原因。
    """
    server = LayerServer({"vec": _png(), "cva": PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + b"junk"})

    async def run():
        inner, downloader = _make_pair(server)
        try:
            return await downloader.download_tile(TILE, MapType.VECTOR)
        finally:
            await inner.close()

    result = asyncio.run(run())

    assert result.failure_kind is TileFailureKind.COMPOSITE_FAILED
    assert "合成失败" in (result.error or "")
    # 不该再打服务端：合成失败不进重试
    assert server.requests.count("vec") == 1
    assert server.requests.count("cva") == 1


def test_composer_crash_is_folded_into_compose_failure() -> None:
    """合成器抛出的 ``TileComposeError`` 必须被折叠成结果，而不是冒泡。"""

    class BoomComposer(TileComposer):
        def compose(self, base_data: bytes, label_data: bytes) -> bytes:  # noqa: ARG002
            raise TileComposeError("模拟合成器崩溃")

    server = LayerServer()

    async def run():
        inner = TileDownloader(
            TiandituProvider(FAKE_TOKEN),
            transport=httpx.MockTransport(server),
            retry_policy=NO_RETRY,
            rate_limiter=NO_RATE_LIMIT,
        )
        downloader = CompositeTileDownloader(inner, BoomComposer())
        try:
            return await downloader.download_tile(TILE, MapType.VECTOR)
        finally:
            await inner.close()

    result = asyncio.run(run())

    assert result.success is False
    assert result.failure_kind is TileFailureKind.COMPOSITE_FAILED
    assert "模拟合成器崩溃" in (result.error or "")


def test_base_and_label_failures_are_counted_separately() -> None:
    """两类失败必须能被分别统计 —— 这是 Spec §7 的全部意义所在。"""
    kinds: set[TileFailureKind] = set()
    for layers in ({"vec": 500}, {"cva": 500}):
        server = LayerServer(layers)

        async def run(server=server):
            inner, downloader = _make_pair(server)
            try:
                return await downloader.download_tile(TILE, MapType.VECTOR)
            finally:
                await inner.close()

        result = asyncio.run(run())
        kinds.add(result.failure_kind)

    assert kinds == {
        TileFailureKind.COMPOSITE_BASE_FAILED,
        TileFailureKind.COMPOSITE_LABEL_FAILED,
    }


# --------------------------------------------------------------------------- #
# 约定与生命周期
# --------------------------------------------------------------------------- #
def test_composite_downloader_does_not_own_inner_downloader() -> None:
    """关闭内部下载器之后再合成会失败——说明本类**没有**偷偷持有它的生命周期。"""
    inner = TileDownloader(TiandituProvider(FAKE_TOKEN))
    downloader = CompositeTileDownloader(inner)

    assert downloader.downloader is inner

    async def run():
        await inner.close()
        return await downloader.download_tile(TILE, MapType.VECTOR)

    result = asyncio.run(run())

    assert result.success is False  # 连接已关，两层都拿不到
    assert result.failure_kind is TileFailureKind.COMPOSITE_BASE_FAILED


def test_requires_a_real_tile_downloader() -> None:
    """传错类型当场拒绝：这类错误重试多少次都一样，越早炸越好。"""
    with pytest.raises(TypeError):
        CompositeTileDownloader(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("tile", "map_type"),
    [
        ("12/3367/1564", MapType.VECTOR),  # tile 不是 TileCoordinate
        (TILE, MapLayerType.VECTOR),  # map_type 传成了单个图层
        (TILE, "vector"),  # map_type 传成了字符串
    ],
)
def test_invalid_arguments_are_rejected(tile, map_type) -> None:
    """参数非法属于调用方的编程错误，直接抛 ``ValueError`` 而不是折叠成结果。"""
    inner = TileDownloader(TiandituProvider(FAKE_TOKEN))
    downloader = CompositeTileDownloader(inner)

    with pytest.raises(ValueError):
        asyncio.run(downloader.download_tile(tile, map_type))

    asyncio.run(inner.close())


def test_custom_composer_is_used() -> None:
    """注入的合成器必须真的被调用（否则"可注入"就是个假承诺）。"""
    calls: list[tuple[int, int]] = []
    real = TileComposer()

    class SpyComposer(TileComposer):
        def compose(self, base_data: bytes, label_data: bytes) -> bytes:
            calls.append((len(base_data), len(label_data)))
            return real.compose(base_data, label_data)

    server = LayerServer()

    async def run():
        inner = TileDownloader(
            TiandituProvider(FAKE_TOKEN),
            transport=httpx.MockTransport(server),
            retry_policy=NO_RETRY,
            rate_limiter=NO_RATE_LIMIT,
        )
        downloader = CompositeTileDownloader(inner, SpyComposer())
        try:
            return await downloader.download_tile(TILE, MapType.VECTOR)
        finally:
            await inner.close()

    result = asyncio.run(run())

    assert result.success is True
    assert len(calls) == 1
    assert calls[0] == (len(_png()), len(_png()))
