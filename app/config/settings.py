"""全局配置常量。

本模块是唯一的"魔数来源"：缩放下限、窗口尺寸、日志级别等都应从这里读取，
禁止在 UI 或业务代码中硬编码。

约定：本模块只允许包含不可变常量与纯函数，不得 import 任何 app 内部模块，
      以保证它是依赖图中的叶子节点（无循环依赖风险）。
"""

from __future__ import annotations

from pathlib import Path

# --------------------------------------------------------------------------- #
# 应用元信息
# --------------------------------------------------------------------------- #
APP_NAME: str = "天地图离线地图工具"
APP_VERSION: str = "0.1.0"
ORG_NAME: str = "MapDownloader"

# --------------------------------------------------------------------------- #
# 窗口
# --------------------------------------------------------------------------- #
DEFAULT_WINDOW_WIDTH: int = 1200
DEFAULT_WINDOW_HEIGHT: int = 760
WINDOW_MIN_WIDTH: int = 900
WINDOW_MIN_HEIGHT: int = 600

# --------------------------------------------------------------------------- #
# 瓦片 / 缩放级别
# --------------------------------------------------------------------------- #
#: 天地图 Web 墨卡托（EPSG:3857）矢量底图支持的缩放范围（UI 下拉框用）
MIN_SUPPORTED_ZOOM: int = 1
MAX_SUPPORTED_ZOOM: int = 18

#: Tile Math 层支持的缩放范围（纯计算，比天地图服务范围更宽）
TILE_MATH_MIN_ZOOM: int = 0
TILE_MATH_MAX_ZOOM: int = 22

DEFAULT_MIN_ZOOM: int = 8
DEFAULT_MAX_ZOOM: int = 17

#: 256 像素瓦片边长，瓦片四叉树计算用
TILE_SIZE: int = 256

#: Web 墨卡托纬度极限（EPSG:3857 抹平的极点）。
#: 精确值为 atan(sinh(pi)) * 180 / pi ≈ 85.0511287798066，按惯例取 8 位小数。
WEB_MERCATOR_MAX_LAT: float = 85.05112878

#: 经度合法范围
MIN_LONGITUDE: float = -180.0
MAX_LONGITUDE: float = 180.0

# --------------------------------------------------------------------------- #
# 容量预估
# --------------------------------------------------------------------------- #
#: 预估单张瓦片平均体积（字节）。天地图 PNG 瓦片实测大致 20~60KB。
DEFAULT_AVERAGE_TILE_SIZE: int = 30 * 1024

# --------------------------------------------------------------------------- #
# 天地图 WMTS 服务
# --------------------------------------------------------------------------- #
#: 天地图瓦片服务使用的协议（天地图同时支持 http/https，默认走 https）
TIANDITU_URL_SCHEME: str = "https"

#: 分片主机模板，``{index}`` 由 :func:`app.providers.tianditu.server_index` 填充
TIANDITU_HOST_TEMPLATE: str = "t{index}.tianditu.gov.cn"

#: 分片主机数量（天地图提供 t0 ~ t7）
TIANDITU_SERVER_COUNT: int = 8

#: WMTS 请求路径模板：``{layer}`` 为图层标识（``vec_w`` 等）
TIANDITU_WMTS_PATH_TEMPLATE: str = "{layer}/wmts"

#: 单瓦片连通性测试的默认超时（秒）
DEFAULT_TILE_TIMEOUT_SECONDS: float = 10.0

#: 瓦片请求携带的 Accept 头（部分服务会据此裁剪错误页）
TIANDITU_ACCEPT_HEADER: str = "image/png,image/jpeg;q=0.9,*/*;q=0.8"

#: 瓦片请求的 User-Agent，便于服务端识别调用方。
#: 必须是纯 ASCII —— HTTP 头不允许非 ASCII 字符，因此这里用 ORG_NAME
#: 而不是中文的 APP_NAME（否则 httpx 会在编码请求头时抛 UnicodeEncodeError）。
HTTP_USER_AGENT: str = f"{ORG_NAME}/{APP_VERSION}"

#: 单瓦片响应体积上限（字节）。超过即视为异常响应（例如错误页 / 大文件），
#: 不再当作瓦片继续处理。
MAX_TILE_RESPONSE_BYTES: int = 4 * 1024 * 1024

# --------------------------------------------------------------------------- #
# 默认输出
# --------------------------------------------------------------------------- #
DEFAULT_OUTPUT_DIR: Path = Path.home() / "MapDownloader" / "output"

# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
LOG_LEVEL: int = 20  # logging.INFO，避免在此 import logging 造成额外耦合
LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"

# --------------------------------------------------------------------------- #
# UI 文案（集中管理，便于后续国际化）
# --------------------------------------------------------------------------- #
UI_TEXT_APP_TITLE: str = APP_NAME
UI_TEXT_DEVICE_EMPTY: str = "暂无设备"
