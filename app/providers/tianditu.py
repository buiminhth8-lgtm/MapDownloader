"""天地图 WMTS 瓦片源（Slice 2）。

职责链::

    MapLayerType  ->  TiandituProvider.build_tile_url()  ->  WMTS URL
                                                             |
                                            test_tile()  ->  单张 HTTP 请求
                                                             |
                                    is_valid_tile_response() -> 响应合法性

本模块**只做单瓦片连通性验证**：不批量下载、不落盘、不建库、不并发。
HTTP 客户端为 ``httpx``，通过 ``transport`` 参数留出测试注入点
（单元测试用 ``httpx.MockTransport``，绝不在测试中访问真实天地图）。

Token 处理约定
--------------

Token 属于敏感信息，**任何日志 / 异常 / 界面文本都不允许出现完整 Token**。
需要展示 URL 时一律走 :func:`mask_url`，需要展示 Token 时走 :func:`mask_token`。

异常约定
--------

- **参数非法**（Token 为空、zoom/x/y 越界、图层类型不对）→ 抛 ``ValueError``，
  这是调用方的编程错误，必须显式暴露而不是静默返回失败结果。
- **网络 / HTTP / 响应内容问题** → 一律转换成 :class:`TileTestResult`，
  让 UI 可以安全展示而不崩溃。
"""

from __future__ import annotations

import time
from urllib.parse import unquote_plus, urlencode

import httpx

from app.config.settings import (
    DEFAULT_TILE_TIMEOUT_SECONDS,
    HTTP_USER_AGENT,
    MAX_TILE_RESPONSE_BYTES,
    TIANDITU_ACCEPT_HEADER,
    TIANDITU_HOST_TEMPLATE,
    TIANDITU_SERVER_COUNT,
    TIANDITU_URL_SCHEME,
    TIANDITU_WMTS_PATH_TEMPLATE,
    TILE_MATH_MAX_ZOOM,
    TILE_MATH_MIN_ZOOM,
)
from app.models.map_layer import MapLayerType
from app.providers.base import TileProvider, TileTestResult

__all__ = [
    "JPEG_SIGNATURE",
    "PNG_SIGNATURE",
    "TiandituProvider",
    "TileProvider",
    "TileTestResult",
    "describe_exception",
    "describe_response",
    "detect_image_format",
    "http_error_message",
    "is_valid_tile_response",
    "mask_token",
    "mask_url",
    "server_index",
    "test_tile",
]


# --------------------------------------------------------------------------- #
# WMTS 1.0.0 协议固定值
#
# 这些是 OGC WMTS 规范与天地图服务约定的字面量，不是"可调参数"，
# 因此就近定义在本模块而不放进 settings（settings 只放可能被调整的配置）。
# --------------------------------------------------------------------------- #
WMTS_SERVICE = "WMTS"
WMTS_REQUEST = "GetTile"
WMTS_VERSION = "1.0.0"
WMTS_STYLE = "default"
WMTS_FORMAT = "tiles"

#: 查询串中 Token 的参数名
TOKEN_QUERY_KEY = "tk"

#: 图片文件头（magic bytes）。判定瓦片合法性以它为准，不只看 Content-Type。
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"

#: 明确的"非图片"Content-Type：即使内容碰巧像图片也判定失败
_ERROR_CONTENT_TYPES: tuple[str, ...] = (
    "text/html",
    "application/xhtml+xml",
    "text/xml",
    "application/xml",
    "application/json",
    "text/plain",
    "text/javascript",
    "application/javascript",
)

#: HTTP 状态码 -> 人话解释
_HTTP_STATUS_HINTS: dict[int, str] = {
    400: "请求参数被服务端拒绝",
    401: "Token 未授权（可能未生效或已失效）",
    403: "Token 可能无效，或该服务未在控制台开通",
    404: "图层或该瓦片不存在",
    429: "请求过于频繁，已被限流",
    500: "服务端内部错误",
    502: "服务端网关错误",
    503: "服务暂时不可用",
    504: "服务端网关超时",
}

#: 错误文案
_ERROR_TIMEOUT = "请求超时"
_ERROR_EMPTY_RESPONSE = "响应内容为空"
_ERROR_IMPLICIT_NETWORK = "网络错误"


# --------------------------------------------------------------------------- #
# 参数校验
# --------------------------------------------------------------------------- #
def _require_token(token: str) -> str:
    """校验 Token 非空，返回去除首尾空白后的值。"""
    if not isinstance(token, str):
        raise ValueError(f"token 必须是字符串，实际为 {type(token).__name__}")
    stripped = token.strip()
    if not stripped:
        raise ValueError("token 不能为空，请填入天地图控制台申请的服务密钥（tk）")
    return stripped


def _validate_zoom(zoom: int) -> int:
    """校验缩放级别在 ``[0, 22]`` 且为整数。"""
    try:
        value = zoom.__index__()
    except AttributeError as exc:
        raise ValueError(f"zoom 必须是整数，实际为 {zoom!r}") from exc
    if not TILE_MATH_MIN_ZOOM <= value <= TILE_MATH_MAX_ZOOM:
        raise ValueError(
            f"zoom 必须在 [{TILE_MATH_MIN_ZOOM}, {TILE_MATH_MAX_ZOOM}] 之间，实际为 {value}"
        )
    return value


def _validate_axis(name: str, value: int, zoom: int) -> int:
    """校验瓦片编号在 ``[0, 2**zoom - 1]``。"""
    try:
        number = value.__index__()
    except AttributeError as exc:
        raise ValueError(f"{name} 必须是整数，实际为 {value!r}") from exc
    limit = 1 << zoom
    if not 0 <= number < limit:
        raise ValueError(f"{name} 必须在 [0, {limit - 1}] 之间（zoom={zoom}），实际为 {number}")
    return number


def _validate_layer(layer: MapLayerType) -> MapLayerType:
    """校验图层类型是受支持的 :class:`MapLayerType` 成员。"""
    if not isinstance(layer, MapLayerType):
        raise ValueError(
            f"layer 必须是 MapLayerType 成员，实际为 {layer!r}；"
            f"支持 {[item.value for item in MapLayerType]}"
        )
    return layer


# --------------------------------------------------------------------------- #
# URL 构造辅助
# --------------------------------------------------------------------------- #
def server_index(x: int, y: int) -> int:
    """按 ``(x + y) % 8`` 确定性地选择分片主机。

    刻意不用随机数：同样的瓦片必须落到同样的主机，否则断点续传与
    缓存命中都无法成立，测试也无从断言。

    >>> server_index(0, 0)
    0
    >>> server_index(3, 4)
    7
    >>> server_index(9, 9)
    2
    """
    return (x + y) % TIANDITU_SERVER_COUNT


def server_host(index: int) -> str:
    """分片序号 -> 主机名（``t0.tianditu.gov.cn`` …）。"""
    if not 0 <= index < TIANDITU_SERVER_COUNT:
        raise ValueError(f"分片序号必须在 [0, {TIANDITU_SERVER_COUNT - 1}] 之间，实际为 {index}")
    return TIANDITU_HOST_TEMPLATE.format(index=index)


def mask_token(token: str) -> str:
    """Token 脱敏，用于日志与界面展示。

    规则：保留极少数首尾字符，中间以 ``****`` 代替，保证肉眼可核对但又无法复原。

    >>> mask_token("abcdefghijklmn")
    'abcd****lmn'
    >>> mask_token("short")
    'sh****rt'
    >>> mask_token("")
    '(未设置)'
    """
    if not token:
        return "(未设置)"
    length = len(token)
    if length <= 3:
        return "*" * length
    if length < 12:
        return f"{token[:2]}****{token[-2:]}"
    return f"{token[:4]}****{token[-3:]}"


def mask_url(url: str) -> str:
    """把 URL 查询串里的 ``tk`` 参数替换为脱敏值。

    其它参数原样保留，便于排查问题；Token 位置保持可读（不显示百分号编码）。
    """
    prefix, separator, query = url.partition("?")
    if not separator:
        return url

    parts: list[str] = []
    for item in query.split("&"):
        key, equal, value = item.partition("=")
        if equal and key == TOKEN_QUERY_KEY:
            parts.append(f"{key}={mask_token(unquote_plus(value))}")
        else:
            parts.append(item)
    return f"{prefix}?{'&'.join(parts)}"


# --------------------------------------------------------------------------- #
# 响应合法性
# --------------------------------------------------------------------------- #
def detect_image_format(content: bytes) -> str | None:
    """按 magic bytes 判断图片格式，返回 ``"PNG"`` / ``"JPEG"`` / ``None``。"""
    if content.startswith(PNG_SIGNATURE):
        return "PNG"
    if content.startswith(JPEG_SIGNATURE):
        return "JPEG"
    return None


def is_error_content_type(content_type: str | None) -> bool:
    """Content-Type 是否明确表示"这不是图片"。"""
    if not content_type:
        return False
    main = content_type.split(";", 1)[0].strip().lower()
    return main in _ERROR_CONTENT_TYPES


def _sniff_text_format(content: bytes) -> str | None:
    """识别常见的错误页形态：HTML / XML / JSON / 纯文本。

    只嗅探前 512 字节——错误页的特征一定出现在开头，没必要扫全文。
    """
    head = content[:512].lstrip(b"\xef\xbb\xbf").lstrip()

    if head.startswith((b"{", b"[")):
        return "JSON"
    lowered = head.lower()
    if lowered.startswith((b"<!doctype html", b"<html")):
        return "HTML"
    if b"serviceexception" in lowered:
        return "XML（ServiceException）"
    if lowered.startswith((b"<?xml", b"<ows")):
        return "XML"
    if b"<html" in lowered[:256]:
        return "HTML"

    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # 全可打印（含中文）→ 视为纯文本错误信息，例如 "Token错误"
    if text and all(character.isprintable() or character.isspace() for character in text):
        return "纯文本"
    return None


def describe_response(content: bytes, content_type: str | None) -> str:
    """给出一句"这个响应是什么"的说明，用于失败原因。"""
    if not content:
        return _ERROR_EMPTY_RESPONSE

    image_format = detect_image_format(content)
    if image_format is not None:
        if is_error_content_type(content_type):
            return f"Content-Type={content_type} 与图片内容不符"
        return f"{image_format} 图片"

    sniffed = _sniff_text_format(content)
    if sniffed is not None:
        return f"{sniffed} 响应，不是图片瓦片"
    return "无法识别的二进制内容，不是图片瓦片"


def is_valid_tile_response(content: bytes, content_type: str | None = None) -> bool:
    """判断响应体是否是一张合法的图片瓦片。

    判定顺序（**不只看 Content-Type**）：

    1. 空响应 → 失败。
    2. magic bytes 不是 PNG / JPEG → 失败（覆盖 HTML / XML / JSON / 纯文本错误页）。
    3. Content-Type 明确是文本类（``text/html`` 等）→ 失败，避免"图片头 + 错误页声明"的错配。

    Args:
        content: 响应体原始字节。
        content_type: 响应头中的 Content-Type，可为 ``None``。

    Returns:
        合法返回 ``True``，否则 ``False``。
    """
    if not content:
        return False
    if detect_image_format(content) is None:
        return False
    return not is_error_content_type(content_type)


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #
class TiandituProvider:
    """天地图 WMTS 瓦片源。

    >>> provider = TiandituProvider("demo-token-123456")
    >>> provider.build_tile_url(MapLayerType.VECTOR, 1, 1, 0)
    'https://t1.tianditu.gov.cn/vec_w/wmts?SERVICE=WMTS&...'

    仅支持 Web 墨卡托（``_w``）服务：矢量 ``vec_w`` / ``cva_w``，
    影像 ``img_w`` / ``cia_w``。
    """

    def __init__(
        self,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """
        Args:
            token: 天地图服务密钥。首尾空白会被裁掉；空值抛 ``ValueError``。
            transport: httpx 传输层，仅供测试注入 ``httpx.MockTransport``；
                生产环境保持 ``None``（走真实网络）。

        Raises:
            ValueError: Token 为空或不是字符串。
        """
        self._token = _require_token(token)
        self._transport = transport

    # ------------------------------------------------------------------ #
    # 基本信息
    # ------------------------------------------------------------------ #
    @property
    def token(self) -> str:
        """完整 Token。

        刻意保留访问入口（签名需要），但**调用方不得把它写进日志**；
        展示用途请使用 :attr:`token_masked`。
        """
        return self._token

    @property
    def token_masked(self) -> str:
        """脱敏 Token，可安全展示。"""
        return mask_token(self._token)

    def __repr__(self) -> str:
        # 避免调试器 / 异常回溯里泄露完整 Token
        return f"TiandituProvider(token={self.token_masked!r})"

    # ------------------------------------------------------------------ #
    # URL 构造
    # ------------------------------------------------------------------ #
    def build_tile_url(
        self,
        layer: MapLayerType,
        zoom: int,
        x: int,
        y: int,
    ) -> str:
        """构造天地图 WMTS 单瓦片请求 URL。

        Args:
            layer: 图层类型，必须是 :class:`MapLayerType` 成员。
            zoom: 缩放级别，``0 ~ 22``。
            x: 瓦片列号，``0 <= x < 2**zoom``。
            y: 瓦片行号（XYZ，自北向南），``0 <= y < 2**zoom``。

        Returns:
            形如
            ``https://t{i}.tianditu.gov.cn/{layer}_w/wmts?SERVICE=WMTS&...&tk=...``
            的完整 URL，其中 ``i = (x + y) % 8``。

        Raises:
            ValueError: Token 为空、zoom/x/y 越界、图层类型不受支持。
        """
        layer_type = _validate_layer(layer)
        zoom_value = _validate_zoom(zoom)
        x_value = _validate_axis("x", x, zoom_value)
        y_value = _validate_axis("y", y, zoom_value)

        parameters = {
            "SERVICE": WMTS_SERVICE,
            "REQUEST": WMTS_REQUEST,
            "VERSION": WMTS_VERSION,
            "LAYER": layer_type.service_name,
            "STYLE": WMTS_STYLE,
            "TILEMATRIXSET": layer_type.matrix_set,
            "FORMAT": WMTS_FORMAT,
            "TILEMATRIX": zoom_value,
            "TILEROW": y_value,
            "TILECOL": x_value,
            TOKEN_QUERY_KEY: _require_token(self._token),
        }

        host = server_host(server_index(x_value, y_value))
        path = TIANDITU_WMTS_PATH_TEMPLATE.format(layer=layer_type.value)
        return f"{TIANDITU_URL_SCHEME}://{host}/{path}?{urlencode(parameters)}"

    # ------------------------------------------------------------------ #
    # HTTP 客户端
    # ------------------------------------------------------------------ #
    def create_client(self, timeout: float = DEFAULT_TILE_TIMEOUT_SECONDS) -> httpx.Client:
        """构造一次性 HTTP 客户端。

        客户端由调用方负责关闭（推荐 ``with`` 语句）。保留为实例方法是为了让
        测试注入的 ``transport`` 自然生效，无需在业务代码里打补丁。
        """
        return _build_client(self._transport, timeout)


# --------------------------------------------------------------------------- #
# HTTP 状态码 -> 人话
# --------------------------------------------------------------------------- #
def describe_exception(exc: BaseException) -> str:
    """把异常转成**一定非空**的一行说明。

    为什么需要这个：``str(exc)`` 为空是真实存在且很常见的情况。以 TLS 被中断
    为例，异常链是这样的（实测）::

        httpx.ConnectError        str=''        args=('',)
        └─ httpcore.ConnectError  str=''        args=(EndOfStream(),)
           └─ anyio.EndOfStream   str=''
              └─ ssl.SSLEOFError  str='[SSL: UNEXPECTED_EOF_WHILE_READING] ...'

    最外层那句 ``f"网络错误：{exc}"`` 于是退化成一句"网络错误："——用户既分不清
    是 DNS、TLS 还是被墙，也没法拿这句话去搜索。

    本函数沿 ``__cause__`` / ``__context__`` 往下找**第一段非空文本**（顶层有文本
    就直接用它，没有就一路往下），并保留链上的异常类型名作为前缀。

    >>> describe_exception(ValueError("boom"))
    'boom'
    >>> describe_exception(OSError())
    'OSError'
    """
    names: list[str] = []
    seen: set[int] = set()
    node: BaseException | None = exc
    detail = ""

    while node is not None and id(node) not in seen:
        seen.add(id(node))
        name = type(node).__name__
        if name not in names:
            names.append(name)
        text = str(node).strip()
        if text:
            detail = text
            break
        node = node.__cause__ or node.__context__

    top = names[0] if names else type(exc).__name__
    if not detail:
        # 整条链都没有文本：至少把类型名给出来（"←" 表示原因链）
        return " ← ".join(names) if names else top
    if len(names) == 1:
        return detail
    return f"{top}：{detail}"


def http_error_message(status_code: int) -> str:
    """把 HTTP 状态码翻译成带排查提示的错误文案。

    公开导出是因为它是"状态码语义"的唯一出处：单瓦片连通性测试（本模块）与
    批量下载引擎（``app.downloader``）都依赖它，不允许各自再抄一份映射表。

    >>> http_error_message(403)
    'HTTP 403 — Token 可能无效，或该服务未在控制台开通'
    """
    hint = _HTTP_STATUS_HINTS.get(status_code)
    if hint is None:
        hint = "服务端错误" if status_code >= 500 else "请求被拒绝"
    return f"HTTP {status_code} — {hint}"


# --------------------------------------------------------------------------- #
# 单瓦片连通性测试
# --------------------------------------------------------------------------- #
def _build_client(
    transport: httpx.BaseTransport | None, timeout: float
) -> httpx.Client:
    """构造一次性 HTTP 客户端（单请求用完即关）。"""
    return httpx.Client(
        timeout=timeout,
        transport=transport,
        follow_redirects=True,
        headers={
            "Accept": TIANDITU_ACCEPT_HEADER,
            "User-Agent": HTTP_USER_AGENT,
        },
    )


def test_tile(
    provider: TiandituProvider,
    layer: MapLayerType,
    zoom: int,
    x: int,
    y: int,
    timeout: float = DEFAULT_TILE_TIMEOUT_SECONDS,
) -> TileTestResult:
    """请求单张瓦片并验证响应，用于验证 Token 与网络连通性。

    **不写任何文件**：响应体只在内存中做合法性判断，随即丢弃。

    Args:
        provider: 瓦片源。
        layer: 图层类型。
        zoom: 缩放级别。
        x: 瓦片列号。
        y: 瓦片行号。
        timeout: 超时秒数。

    Returns:
        :class:`TileTestResult`。网络异常、HTTP 非 200、空响应、非图片响应
        均转换为 ``success=False`` 的结果，调用方（GUI / 线程）不会收到异常。

    Raises:
        ValueError: 参数非法（Token 为空 / zoom、x、y 越界 / 图层类型不对）。
            这属于调用方错误，不做静默吞并；GUI 侧在调用前已自行校验，
            后台线程另有兜底捕获。
    """
    url = provider.build_tile_url(layer, zoom, x, y)

    started = time.perf_counter()
    try:
        with provider.create_client(timeout) as client:
            response = client.get(url)
    except httpx.TimeoutException:
        return TileTestResult(
            success=False,
            status_code=None,
            content_type=None,
            content_length=0,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            error=f"{_ERROR_TIMEOUT}（>{timeout} 秒）",
        )
    except httpx.HTTPError as exc:
        return TileTestResult(
            success=False,
            status_code=None,
            content_type=None,
            content_length=0,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            error=f"{_ERROR_IMPLICIT_NETWORK}：{describe_exception(exc)}",
        )

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    status_code = response.status_code
    content_type = response.headers.get("content-type")
    content = response.content
    content_length = len(content)

    def _failure(error: str) -> TileTestResult:
        return TileTestResult(
            success=False,
            status_code=status_code,
            content_type=content_type,
            content_length=content_length,
            elapsed_ms=elapsed_ms,
            error=error,
        )

    if status_code != 200:
        return _failure(http_error_message(status_code))
    if content_length == 0:
        return _failure(_ERROR_EMPTY_RESPONSE)
    if content_length > MAX_TILE_RESPONSE_BYTES:
        return _failure(
            f"响应体积异常（{content_length} 字节，超过上限 {MAX_TILE_RESPONSE_BYTES} 字节）"
        )
    if not is_valid_tile_response(content, content_type):
        return _failure(f"响应不是有效的图片瓦片：{describe_response(content, content_type)}")

    return TileTestResult(
        success=True,
        status_code=status_code,
        content_type=content_type,
        content_length=content_length,
        elapsed_ms=elapsed_ms,
        error=None,
    )
