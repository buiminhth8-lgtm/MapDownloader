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
# 批量下载引擎（Slice 3）
# --------------------------------------------------------------------------- #
#: 每个瓦片的请求超时（秒）。
#:
#: 刻意与 ``DEFAULT_TILE_TIMEOUT_SECONDS`` 分开：那个是"人点一下等结果"的
#: 单瓦片连通性测试超时，这个是批量下载时单请求的上限，二者会随使用场景分叉
#: （批量场景更希望快速失败并交给重试，而不是长时间挂住一个 worker）。
DEFAULT_REQUEST_TIMEOUT: float = 10.0

#: 默认并发 worker 数
DEFAULT_DOWNLOAD_CONCURRENCY: int = 6

#: 并发下限与上限。上限存在的理由是"不要打爆服务端"——
#: 天地图对未授权/超额的调用会直接拒绝，过高的并发只会换来一片失败。
MIN_DOWNLOAD_CONCURRENCY: int = 1
MAX_DOWNLOAD_CONCURRENCY: int = 16

#: 默认最大尝试次数（含首次请求）。``3`` 表示最多重试 2 次。
DEFAULT_MAX_RETRIES: int = 3

#: 重试退避：``base * 2**(attempt-1)``，并以 ``max`` 封顶
DEFAULT_RETRY_BASE_DELAY_SECONDS: float = 0.5
DEFAULT_RETRY_MAX_DELAY_SECONDS: float = 5.0

#: 默认请求速率上限（每秒请求数）。``<= 0`` 表示关闭限速。
DEFAULT_REQUESTS_PER_SECOND: float = 8.0

#: 进度回调的最小间隔（秒）。约 6.7 次/秒，落在"5~10 次/秒"的建议区间内，
#: 避免高频 Signal 把 PySide6 事件队列压垮。
PROGRESS_EMIT_INTERVAL_SECONDS: float = 0.15

#: 计算 tiles/s 的滑动窗口长度（秒）
PROGRESS_SPEED_WINDOW_SECONDS: float = 3.0

#: 队列容量倍数：``queue_maxsize = concurrency * QUEUE_CAPACITY_FACTOR``。
#: 队列必须是有界的——否则生产端会抢在消费端前面把整个瓦片列表塞进内存，
#: 与"边下载边消费"的目标相悖。
QUEUE_CAPACITY_FACTOR: int = 4

#: 取消 / 暂停时最多在日志里逐条打印多少个失败瓦片（其余只计数）
MAX_LOGGED_TILE_FAILURES: int = 3

#: 收尾汇总里最多展示几条「失败原因」。失败原因已按张数聚合，
#: 因此这里限制的是**去重后的原因种类数**，不会因为失败瓦片多而刷屏。
MAX_REPORTED_FAILURE_REASONS: int = 3

# --------------------------------------------------------------------------- #
# MBTiles（Slice 4）
# --------------------------------------------------------------------------- #
#: MBTiles 单文件扩展名
MBTILES_FILE_SUFFIX: str = ".mbtiles"

#: 批量事务大小：累计这么多张瓦片就 COMMIT 一次。
#:
#: 太小 → 每个瓦片一次 fsync，写盘成为瓶颈；太大 → 崩溃时丢失的已下载瓦片变多。
#: 200 是"几千张瓦片的任务里提交次数个位数、单次事务体量又不大"的折中。
DEFAULT_MBTILES_BATCH_SIZE: int = 200

#: 单 Writer 的异步写队列容量。
#:
#: 上界存在的意义是**内存**：队列里的元素持有瓦片字节（几十 KB/张），
#: 队列必须能对下载端形成反压，否则下载速度一快就会把几万张瓦片堆在内存里，
#: 与"边下载边写盘"的目标直接相悖。下界（100）保证 SQLite 偶发卡顿时
#: 下载端不至于频繁停顿。取 200 ⇒ 峰值常驻内存约 200 × 30KB ≈ 6MB。
DEFAULT_MBTILES_QUEUE_CAPACITY: int = 200

#: 写入队列的取值区间（供校验与 UI 提示引用）
MIN_MBTILES_QUEUE_CAPACITY: int = 100
MAX_MBTILES_QUEUE_CAPACITY: int = 500

#: 校验器最多抽样检查多少张瓦片的图片 magic bytes。
#: 全量扫描几十万张瓦片是 Slice 5 之后的事，本 Slice 只做抽样。
MBTILES_VALIDATE_SAMPLE_LIMIT: int = 20

#: 超过这个瓦片数就在开始前弹一次确认框（**只提示，不限制**）。
#: 10,000 张 ≈ 300MB 量级，属于"值得先提醒一声"的规模。
GUI_LARGE_TASK_CONFIRM_TILES: int = 10_000

#: 默认地图名称（用于生成 ``<名称>.mbtiles``）
DEFAULT_MAP_NAME: str = "offline_map"

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

#: 需要单独压级别的第三方日志器。
#:
#: ``httpx`` 在 INFO 级别会把**完整请求 URL** 打进日志，而天地图的 URL 里带着
#: ``tk=`` 参数——等于把 Token 明文写进控制台。这属于硬性禁止项，因此这些
#: 日志器一律压到 WARNING（只在真正出问题时才出声）。
HTTP_LIBRARY_LOGGER_NAMES: tuple[str, ...] = ("httpx", "httpcore")
HTTP_LIBRARY_LOG_LEVEL: int = 30  # logging.WARNING

# --------------------------------------------------------------------------- #
# UI 文案（集中管理，便于后续国际化）
# --------------------------------------------------------------------------- #
UI_TEXT_APP_TITLE: str = APP_NAME
UI_TEXT_DEVICE_EMPTY: str = "暂无设备"

#: Slice 4 起：确实会落盘，提示改为说明输出产物形态（不再是"测试模式"）
UI_TEXT_MBTILES_OUTPUT_NOTICE: str = (
    "下载结果将写入 MBTiles 单文件（<地图名称>.mbtiles），可直接供 osmdroid 读取"
)

#: 大任务风险提示（只提示，不限制）
UI_TEXT_LARGE_TASK_CONFIRM_TITLE: str = "大任务确认"
UI_TEXT_LARGE_TASK_CONFIRM: str = (
    "当前范围预计需要下载 {total:,} 张瓦片，\n"
    "预计容量约 {size}，可能需要较长时间并消耗天地图调用配额。\n\n"
    "确认继续下载吗？（不会限制下载规模，仅作提醒）"
)

#: 取消任务后的提示：已写入的瓦片保留
UI_TEXT_CANCELLED_KEEP_TILES: str = (
    "任务已取消，已完成瓦片已保留。\n"
    "Slice 5 将支持从已有 MBTiles 断点续传。"
)

#: 未指定地图名称时的提示
UI_TEXT_MAP_NAME_REQUIRED: str = "请先填写地图名称（用于生成 <名称>.mbtiles）"

