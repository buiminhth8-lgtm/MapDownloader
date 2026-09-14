"""TiandituProvider 纯逻辑测试：URL 构造、分片选择、参数校验、Token 脱敏。

本文件**不访问网络**——HTTP 行为在 ``test_tianditu_response.py`` 中用
``httpx.MockTransport`` 覆盖。

安全约定：测试中的 Token 一律是假值，禁止把真实密钥写进本文件或任何提交内容。
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from app.config import settings
from app.models import MapLayerType
from app.providers import (
    TiandituProvider,
    TileProvider,
    mask_token,
    mask_url,
    server_host,
    server_index,
)

#: 假 Token（长度 ≥ 12 以便验证"首 4 + 尾 3"的脱敏规则）
TOKEN = "abcdefghijklmn"

#: 北京附近 z12 的一张瓦片，用于断言 URL 结构
BEIJING_TILE = (3372, 1552)


@pytest.fixture()
def provider() -> TiandituProvider:
    return TiandituProvider(TOKEN)


def _split(url: str) -> tuple[str, dict[str, str]]:
    """把 URL 拆成 (scheme://host/path, 查询参数字典)。"""
    parts = urlsplit(url)
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    return f"{parts.scheme}://{parts.netloc}{parts.path}", query


# --------------------------------------------------------------------------- #
# URL 结构
# --------------------------------------------------------------------------- #
def test_url_structure(provider: TiandituProvider) -> None:
    x, y = BEIJING_TILE
    endpoint, query = _split(provider.build_tile_url(MapLayerType.VECTOR, 12, x, y))

    assert endpoint == f"https://t{server_index(x, y)}.tianditu.gov.cn/vec_w/wmts"
    assert endpoint.startswith(f"{settings.TIANDITU_URL_SCHEME}://")
    assert query["TILEMATRIX"] == "12"
    assert query["TILECOL"] == str(x)
    assert query["TILEROW"] == str(y)
    assert query["tk"] == TOKEN


@pytest.mark.parametrize(
    ("layer", "layer_id", "layer_param"),
    [
        (MapLayerType.VECTOR, "vec_w", "vec"),
        (MapLayerType.VECTOR_LABEL, "cva_w", "cva"),
        (MapLayerType.IMAGE, "img_w", "img"),
        (MapLayerType.IMAGE_LABEL, "cia_w", "cia"),
    ],
)
def test_layer_mapping(
    provider: TiandituProvider, layer: MapLayerType, layer_id: str, layer_param: str
) -> None:
    """四种图层：URL 路径用完整标识，LAYER 参数只用前半段。"""
    endpoint, query = _split(provider.build_tile_url(layer, 8, 168, 77))

    assert endpoint.endswith(f"/{layer_id}/wmts")
    assert query["LAYER"] == layer_param


def test_wmts_parameters_are_complete(provider: TiandituProvider) -> None:
    _endpoint, query = _split(provider.build_tile_url(MapLayerType.IMAGE, 5, 3, 4))

    assert query == {
        "SERVICE": "WMTS",
        "REQUEST": "GetTile",
        "VERSION": "1.0.0",
        "LAYER": "img",
        "STYLE": "default",
        "TILEMATRIXSET": "w",
        "FORMAT": "tiles",
        "TILEMATRIX": "5",
        "TILEROW": "4",
        "TILECOL": "3",
        "tk": TOKEN,
    }


def test_url_is_deterministic(provider: TiandituProvider) -> None:
    """同一瓦片必须永远得到同一个 URL（断点续传与缓存的前提）。"""
    first = provider.build_tile_url(MapLayerType.VECTOR, 10, 843, 388)
    second = provider.build_tile_url(MapLayerType.VECTOR, 10, 843, 388)
    assert first == second


def test_provider_satisfies_protocol(provider: TiandituProvider) -> None:
    assert isinstance(provider, TileProvider)


def test_layer_service_name_and_matrix_set() -> None:
    assert MapLayerType.VECTOR.service_name == "vec"
    assert MapLayerType.VECTOR_LABEL.service_name == "cva"
    assert MapLayerType.IMAGE.service_name == "img"
    assert MapLayerType.IMAGE_LABEL.service_name == "cia"
    assert all(layer.matrix_set == "w" for layer in MapLayerType)


# --------------------------------------------------------------------------- #
# 分片选择
# --------------------------------------------------------------------------- #
def test_server_index_is_x_plus_y_modulo_8() -> None:
    assert server_index(0, 0) == 0
    assert server_index(3, 4) == 7
    assert server_index(9, 9) == 2
    assert server_index(3372, 1552) == (3372 + 1552) % 8


def test_server_index_covers_all_eight_shards() -> None:
    assert {server_index(x, 0) for x in range(8)} == set(range(8))
    assert {server_index(x, 1) for x in range(8)} == set(range(8))


def test_all_shards_are_used_by_urls(provider: TiandituProvider) -> None:
    """t0 ~ t7 都能被真实瓦片编号命中。"""
    hosts = {
        urlsplit(provider.build_tile_url(MapLayerType.VECTOR, 6, x, 0)).netloc
        for x in range(8)
    }
    assert hosts == {f"t{index}.tianditu.gov.cn" for index in range(8)}


def test_server_host_bounds() -> None:
    assert server_host(0) == "t0.tianditu.gov.cn"
    assert server_host(7) == "t7.tianditu.gov.cn"
    with pytest.raises(ValueError, match="分片序号"):
        server_host(8)
    with pytest.raises(ValueError, match="分片序号"):
        server_host(-1)


# --------------------------------------------------------------------------- #
# Token 处理
# --------------------------------------------------------------------------- #
def test_token_url_encoding() -> None:
    """含特殊字符的 Token 必须被百分号编码，不能破坏查询串结构。"""
    raw_token = "a b&c=d/e?f"
    provider = TiandituProvider(raw_token)
    url = provider.build_tile_url(MapLayerType.VECTOR, 4, 5, 6)

    assert "%26" in url and "%3D" in url
    assert "&c=" not in url  # 不能把 Token 的内容当成新的查询参数

    _endpoint, query = _split(url)
    assert query["tk"] == raw_token
    assert query["TILECOL"] == "5"


def test_token_whitespace_is_stripped() -> None:
    provider = TiandituProvider(f"  {TOKEN}\n")
    assert provider.token == TOKEN


@pytest.mark.parametrize("token", ["", "   ", "\t\n", None, 12345])
def test_empty_or_invalid_token_rejected(token: object) -> None:
    with pytest.raises(ValueError, match="token"):
        TiandituProvider(token)  # type: ignore[arg-type]


def test_mask_token_hides_middle() -> None:
    assert mask_token(TOKEN) == "abcd****lmn"
    assert mask_token("short") == "sh****rt"
    assert mask_token("abc") == "***"
    assert mask_token("") == "(未设置)"
    # 任何长度都不能完整暴露
    for sample in ("a", "abc", "abcdefgh", "abcdefghijklmnopqrst"):
        assert TOKEN not in mask_token(sample)
        assert sample not in mask_token(sample) or len(sample) <= 3


def test_mask_url_masks_only_token(provider: TiandituProvider) -> None:
    url = provider.build_tile_url(MapLayerType.VECTOR_LABEL, 12, *BEIJING_TILE)
    masked = mask_url(url)

    assert TOKEN not in masked
    assert "tk=abcd****lmn" in masked
    assert "SERVICE=WMTS" in masked and "TILECOL=3372" in masked
    # 除 tk 外其余部分完全一致
    assert masked.replace("abcd****lmn", TOKEN) == url


def test_mask_url_without_query_is_unchanged() -> None:
    assert mask_url("https://t0.tianditu.gov.cn/vec_w/wmts") == (
        "https://t0.tianditu.gov.cn/vec_w/wmts"
    )


def test_repr_does_not_leak_token(provider: TiandituProvider) -> None:
    text = repr(provider)
    assert TOKEN not in text
    assert "abcd****lmn" in text


def test_provider_exposes_masked_token(provider: TiandituProvider) -> None:
    assert provider.token_masked == "abcd****lmn"
    assert provider.token == TOKEN


# --------------------------------------------------------------------------- #
# 参数校验
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("zoom", [0, 1, 12, 22])
def test_valid_zoom_accepted(provider: TiandituProvider, zoom: int) -> None:
    limit = (1 << zoom) - 1
    url = provider.build_tile_url(MapLayerType.VECTOR, zoom, limit, 0)
    assert f"TILECOL={limit}" in url


@pytest.mark.parametrize("zoom", [-1, 23, 100])
def test_zoom_out_of_range_rejected(provider: TiandituProvider, zoom: int) -> None:
    with pytest.raises(ValueError, match="zoom"):
        provider.build_tile_url(MapLayerType.VECTOR, zoom, 0, 0)


@pytest.mark.parametrize("zoom", [12.0, "12", None])
def test_zoom_must_be_integer(provider: TiandituProvider, zoom: object) -> None:
    with pytest.raises(ValueError, match="zoom"):
        provider.build_tile_url(MapLayerType.VECTOR, zoom, 0, 0)  # type: ignore[arg-type]


def test_tile_index_bounds(provider: TiandituProvider) -> None:
    zoom = 3
    limit = (1 << zoom) - 1

    # 边界值合法
    provider.build_tile_url(MapLayerType.VECTOR, zoom, 0, 0)
    provider.build_tile_url(MapLayerType.VECTOR, zoom, limit, limit)

    for x, y in ((limit + 1, 0), (0, limit + 1), (-1, 0), (0, -1)):
        with pytest.raises(ValueError, match="必须在"):
            provider.build_tile_url(MapLayerType.VECTOR, zoom, x, y)


@pytest.mark.parametrize("axis_value", [1.5, "3", None])
def test_tile_index_must_be_integer(provider: TiandituProvider, axis_value: object) -> None:
    with pytest.raises(ValueError, match="必须是整数"):
        provider.build_tile_url(MapLayerType.VECTOR, 3, axis_value, 0)  # type: ignore[arg-type]


@pytest.mark.parametrize("layer", ["vec_w", None, 3, MapLayerType.VECTOR.value])
def test_invalid_layer_rejected(provider: TiandituProvider, layer: object) -> None:
    with pytest.raises(ValueError, match="layer"):
        provider.build_tile_url(layer, 3, 1, 1)  # type: ignore[arg-type]


def test_error_message_lists_supported_layers(provider: TiandituProvider) -> None:
    with pytest.raises(ValueError) as excinfo:
        provider.build_tile_url("vec_w", 3, 1, 1)  # type: ignore[arg-type]
    message = str(excinfo.value)
    for layer in MapLayerType:
        assert layer.value in message
