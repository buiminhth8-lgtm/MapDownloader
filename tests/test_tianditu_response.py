"""单瓦片响应合法性与 ``test_tile`` 的 HTTP 行为测试。

全部通过 ``httpx.MockTransport`` 打桩，**绝不访问真实天地图**，
因此在无网络环境下也能完整运行。
"""

from __future__ import annotations

import builtins
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import settings
from app.models import MapLayerType
from app.providers import (
    JPEG_SIGNATURE,
    PNG_SIGNATURE,
    TiandituProvider,
    describe_exception,
    describe_response,
    detect_image_format,
    is_error_content_type,
    is_valid_tile_response,
)
from app.providers import (
    test_tile as request_single_tile,
)

# 注意：``test_tile`` 以 test_ 开头，若直接按原名导入，pytest 会把它误当成测试函数
# 收集（并因缺少 fixture 报错），因此统一用别名 ``request_single_tile``。

TOKEN = "demo-token-123456"
LAYER = MapLayerType.VECTOR
ZOOM, X, Y = 12, 3372, 1552

#: 只需文件头正确 —— 校验逻辑按 magic bytes 判定，不解析图片内容
PNG_BYTES = PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + b"tile-payload" * 16
JPEG_BYTES = JPEG_SIGNATURE + b"\xe0\x00\x10JFIF\x00" + b"tile-payload" * 16
GIF_BYTES = b"GIF89a" + b"tile-payload" * 8

HTML_BYTES = b"<!DOCTYPE html><html><body>Invalid Token</body></html>"
XML_BYTES = (
    b'<?xml version="1.0" encoding="UTF-8"?><ServiceException code="InvalidToken">'
    b"Token is not valid</ServiceException>"
)
JSON_BYTES = b'{"code":401,"message":"invalid token"}'
TEXT_BYTES = "Token错误，请检查服务密钥".encode()

Handler = Callable[[httpx.Request], httpx.Response]


def _provider(handler: Handler) -> TiandituProvider:
    """构造注入了 MockTransport 的 Provider（等价于真实 Provider 的网络行为）。"""
    return TiandituProvider(TOKEN, transport=httpx.MockTransport(handler))


def _static_handler(
    content: bytes = PNG_BYTES,
    content_type: str | None = "image/png",
    status_code: int = 200,
    recorder: list[httpx.Request] | None = None,
) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        headers = {"content-type": content_type} if content_type else {}
        return httpx.Response(status_code, content=content, headers=headers)

    return handler


def _raising_handler(exc: Exception) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def _run(**kwargs: Any):
    return request_single_tile(
        _provider(kwargs.pop("handler")), LAYER, ZOOM, X, Y, **kwargs
    )


# --------------------------------------------------------------------------- #
# 响应合法性（纯函数）
# --------------------------------------------------------------------------- #
def test_png_is_valid() -> None:
    assert is_valid_tile_response(PNG_BYTES, "image/png") is True


def test_jpeg_is_valid() -> None:
    assert is_valid_tile_response(JPEG_BYTES, "image/jpeg") is True


def test_octet_stream_is_accepted() -> None:
    """服务端不给精确类型时，仍以 magic bytes 为准。"""
    assert is_valid_tile_response(PNG_BYTES, "application/octet-stream") is True
    assert is_valid_tile_response(PNG_BYTES, None) is True


def test_empty_content_is_invalid() -> None:
    assert is_valid_tile_response(b"", "image/png") is False
    assert is_valid_tile_response(b"", None) is False


@pytest.mark.parametrize(
    "content",
    [HTML_BYTES, XML_BYTES, JSON_BYTES, TEXT_BYTES, GIF_BYTES, b"\x00\x01\x02\x03" * 8],
)
def test_non_image_content_is_invalid(content: bytes) -> None:
    assert is_valid_tile_response(content, "image/png") is False
    assert is_valid_tile_response(content, None) is False


@pytest.mark.parametrize(
    "content_type",
    ["text/html", "text/html; charset=utf-8", "application/xml", "text/xml", "application/json"],
)
def test_declared_error_content_type_is_invalid(content_type: str) -> None:
    """图片头 + 错误页声明（错配）也要判失败。"""
    assert is_valid_tile_response(PNG_BYTES, content_type) is False


def test_detect_image_format() -> None:
    assert detect_image_format(PNG_BYTES) == "PNG"
    assert detect_image_format(JPEG_BYTES) == "JPEG"
    assert detect_image_format(HTML_BYTES) is None
    assert detect_image_format(b"") is None


def test_is_error_content_type() -> None:
    assert is_error_content_type("text/html; charset=utf-8") is True
    assert is_error_content_type("IMAGE/PNG") is False
    assert is_error_content_type("image/jpeg") is False
    assert is_error_content_type(None) is False
    assert is_error_content_type("") is False


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"", "响应内容为空"),
        (HTML_BYTES, "HTML"),
        (XML_BYTES, "XML"),
        (JSON_BYTES, "JSON"),
        (TEXT_BYTES, "纯文本"),
        (PNG_BYTES, "PNG 图片"),
    ],
)
def test_describe_response(content: bytes, expected: str) -> None:
    assert expected in describe_response(content, "image/png")


# --------------------------------------------------------------------------- #
# test_tile：成功路径
# --------------------------------------------------------------------------- #
def test_successful_png_tile() -> None:
    requests: list[httpx.Request] = []
    result = _run(handler=_static_handler(recorder=requests))

    assert result.success is True
    assert result.status_code == 200
    assert result.content_type == "image/png"
    assert result.content_length == len(PNG_BYTES)
    assert result.error is None
    assert result.elapsed_ms >= 0.0
    assert result.has_response is True

    # 确实请求了 Provider 生成的 URL，且带上了 Accept 头
    assert len(requests) == 1
    assert str(requests[0].url) == TiandituProvider(TOKEN).build_tile_url(LAYER, ZOOM, X, Y)
    assert "image/png" in requests[0].headers["accept"]


def test_successful_jpeg_tile() -> None:
    result = _run(handler=_static_handler(JPEG_BYTES, "image/jpeg"))
    assert result.success is True
    assert result.content_type == "image/jpeg"


def test_summary_contains_status_size_and_elapsed() -> None:
    result = _run(handler=_static_handler())
    summary = result.summary()
    assert "HTTP 200" in summary
    assert f"{len(PNG_BYTES)} bytes" in summary
    assert "ms" in summary
    assert TOKEN not in summary


# --------------------------------------------------------------------------- #
# test_tile：失败路径
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status_code", [401, 403])
def test_token_related_http_errors(status_code: int) -> None:
    result = _run(handler=_static_handler(HTML_BYTES, "text/html", status_code))

    assert result.success is False
    assert result.status_code == status_code
    assert f"HTTP {status_code}" in (result.error or "")
    assert "Token" in (result.error or "")
    assert result.content_length == len(HTML_BYTES)


@pytest.mark.parametrize("status_code", [404, 500, 503])
def test_other_http_errors(status_code: int) -> None:
    result = _run(handler=_static_handler(b"", "text/plain", status_code))

    assert result.success is False
    assert f"HTTP {status_code}" in (result.error or "")


def test_empty_response_body_fails() -> None:
    result = _run(handler=_static_handler(b"", "image/png", 200))

    assert result.success is False
    assert result.error == "响应内容为空"
    assert result.content_length == 0


@pytest.mark.parametrize(
    "content", [HTML_BYTES, XML_BYTES, JSON_BYTES, TEXT_BYTES, GIF_BYTES]
)
def test_error_page_with_http_200_is_not_a_tile(content: bytes) -> None:
    """HTTP 200 也可能返回错误页，必须靠内容判定。"""
    result = _run(handler=_static_handler(content, "text/html"))

    assert result.success is False
    assert "不是有效的图片瓦片" in (result.error or "")


def test_timeout_becomes_result() -> None:
    result = _run(handler=_raising_handler(httpx.ReadTimeout("too slow")))

    assert result.success is False
    assert result.status_code is None
    assert "请求超时" in (result.error or "")


def test_connect_error_becomes_result() -> None:
    result = _run(handler=_raising_handler(httpx.ConnectError("dns failure")))

    assert result.success is False
    assert result.status_code is None
    assert "网络错误" in (result.error or "")


def test_oversized_response_fails() -> None:
    oversized = PNG_SIGNATURE + b"\x00" * (settings.MAX_TILE_RESPONSE_BYTES + 1)
    result = _run(handler=_static_handler(oversized))

    assert result.success is False
    assert "体积异常" in (result.error or "")


def test_timeout_argument_is_accepted() -> None:
    result = _run(handler=_static_handler(), timeout=0.5)
    assert result.success is True


# --------------------------------------------------------------------------- #
# test_tile：非法参数与副作用
# --------------------------------------------------------------------------- #
def test_illegal_arguments_raise_before_request() -> None:
    requests: list[httpx.Request] = []
    provider = _provider(_static_handler(recorder=requests))

    with pytest.raises(ValueError):
        request_single_tile(provider, LAYER, 23, 0, 0)
    with pytest.raises(ValueError):
        request_single_tile(provider, LAYER, ZOOM, 1 << ZOOM, 0)
    with pytest.raises(ValueError):
        request_single_tile(provider, "vec_w", ZOOM, X, Y)  # type: ignore[arg-type]

    assert requests == []  # 参数不合法时一个请求都不该发出


def test_empty_token_cannot_even_build_provider() -> None:
    with pytest.raises(ValueError, match="token"):
        request_single_tile(TiandituProvider(""), LAYER, ZOOM, X, Y)


def test_test_tile_writes_no_files(monkeypatch) -> None:
    """本 Slice 只做连通性验证：整个调用过程不允许产生任何落盘写入。"""
    written: list[tuple[object, str]] = []
    real_open = builtins.open

    def guard(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
            written.append((file, str(mode)))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guard)
    cwd_before = set(os.listdir(Path.cwd()))

    result = _run(handler=_static_handler())

    assert result.success is True
    assert written == []
    assert set(os.listdir(Path.cwd())) == cwd_before


# --------------------------------------------------------------------------- #
# describe_exception：异常文案必须**一定**可读（Slice 4 真实网络验证时发现）
# --------------------------------------------------------------------------- #
def test_describe_exception_uses_message_when_present() -> None:
    assert describe_exception(ValueError("boom")) == "boom"
    assert describe_exception(ValueError("  padded  ")) == "padded"


def test_describe_exception_falls_back_to_type_name() -> None:
    """完全没有文本时也不能返回空串。"""
    assert describe_exception(OSError()) == "OSError"


def test_describe_exception_walks_to_deepest_message() -> None:
    """顶层无消息时沿 __cause__ 往下找——这正是 httpx 连接层异常的真实形态。"""
    inner = ValueError("inner detail")
    middle = RuntimeError()
    middle.__cause__ = inner
    outer = OSError()
    outer.__cause__ = middle

    assert describe_exception(outer) == "OSError：inner detail"


def test_describe_exception_reports_chain_when_entirely_silent() -> None:
    """整条链都没有文本时，至少把类型链给出来。"""
    leaf = RuntimeError()
    root = OSError()
    root.__cause__ = leaf

    text = describe_exception(root)
    assert text == "OSError ← RuntimeError"


def test_describe_exception_handles_cycles() -> None:
    """异常链理论上可以成环（手工构造），不能因此死循环。"""
    first = OSError()
    second = RuntimeError()
    first.__cause__ = second
    second.__cause__ = first

    assert describe_exception(first) == "OSError ← RuntimeError"


def test_network_error_message_is_never_empty() -> None:
    """TLS 中断这类"消息消失"的异常，落到日志里必须仍然有可读信息。

    真实形态：``httpx.ConnectError`` 的 ``str()`` 为空，真正的 SSL 错误藏在
    ``__cause__`` 链的第三层。没有这个回退，用户看到的就只有一句"网络错误："。
    """
    ssl_error = RuntimeError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF")
    # 模拟 httpx.ConnectError：类型在、消息没了（真实形态就是 str() == ""）
    root = httpx.ConnectError("")
    root.__cause__ = ssl_error

    def handler(request: httpx.Request) -> httpx.Response:
        raise root

    result = request_single_tile(_provider(handler), LAYER, ZOOM, X, Y)

    assert result.success is False
    assert result.error is not None
    assert result.error.strip() != "网络错误："          # 不能只剩一个冒号
    assert "UNEXPECTED_EOF_WHILE_READING" in result.error
    assert "ConnectError" in result.error
