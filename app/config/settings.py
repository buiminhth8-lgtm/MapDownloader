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
# 断点续传与任务持久化（Slice 5）
# --------------------------------------------------------------------------- #
#: 恢复已有 MBTiles 时，是否连内容一起校验（读 BLOB + 判 magic bytes）。
#:
#: **默认关闭**：开启后每张已有瓦片都要读一次 BLOB，恢复一个几十万张的库会从
#: "秒级"变成"分钟级"。存在性判断只看坐标三列就足够了（Spec §22）。
DEFAULT_RESUME_VERIFY_EXISTING_TILE: bool = False

#: 校验发现已有瓦片内容为空/损坏时，是否重新下载覆盖。
#: 仅在 ``DEFAULT_RESUME_VERIFY_EXISTING_TILE=True`` 时有意义。
DEFAULT_RESUME_REDOWNLOAD_INVALID_TILE: bool = True

#: 存在性判断的策略切换阈值。
#:
#: 目标瓦片数 ``<=`` 这个值时，一次性把已有坐标读成 ``set``（内存换速度）；
#: 超过则改用主键索引逐张查询。5 万条 ``(z, x, y)`` 三元组约占 10MB，
#: 属于"可以接受但要有个上限"的量级。
DEFAULT_RESUME_CACHE_MAX_TILES: int = 50_000

#: 任务状态持久化目录与文件名（JSON，不含 Token）。
#: 与 ``DEFAULT_OUTPUT_DIR`` 同放 ``~/MapDownloader`` 下（见下方"默认输出"一节）：
#: 桌面工具以"双击运行"为主，工作目录不可靠，不能把任务文件写在 cwd 里。
TASKS_DIR_NAME: str = "data"
TASKS_FILE_NAME: str = "tasks.json"

#: JSON 结构版本号。将来字段有破坏性变化时用它做迁移判断。
TASKS_FILE_VERSION: int = 1

#: 损坏的任务文件被隔离时追加的后缀（不直接删除用户的文件）
TASKS_CORRUPT_SUFFIX: str = ".corrupt"

#: 任务进度落盘的节流参数：满足任一条件才写一次。
#:
#: 每下载一张瓦片就写一次 JSON，几十万张的任务会产生几十万次 fsync——
#: 既是磁盘灾难，也和"下载"抢同一个线程的时间。状态变化（开始/暂停/取消/
#: 失败/完成）**不受节流限制**，一律立即落盘。
DEFAULT_TASK_SAVE_INTERVAL_SECONDS: float = 1.0
DEFAULT_TASK_SAVE_TILE_INTERVAL: int = 100

#: 恢复前一致性校验时，经纬度的允许偏差。
#: metadata 里存的是 6 位小数字符串，回读成 float 会有量化误差；
#: 1e-6 度 ≈ 0.1 米，远小于单张瓦片的精度，不会把正常文件判成"不兼容"。
MBTILES_BOUNDS_TOLERANCE: float = 1e-6

#: 任务列表区展示的最近任务条数上限（避免界面上无限增长）
TASK_PANEL_MAX_ROWS: int = 200

# --------------------------------------------------------------------------- #
# 底图 + 中文注记合成（Slice 6）
# --------------------------------------------------------------------------- #
#: 合成结果的图片格式。**固定 PNG**（Spec §4）：注记层必须保留透明通道，
#: 而 JPEG 没有 alpha —— 合成后再存 JPEG 会把透明区域压成黑块。
COMPOSER_OUTPUT_FORMAT: str = "PNG"

#: 是否让 Pillow 做额外压缩优化。
#:
#: 刻意**关闭**：``optimize=True`` 会让单张瓦片的编码耗时增加数倍，
#: 而本场景是"几万张瓦片边下边合成"，下载速度才是瓶颈，几 KB 的体积收益
#: 换不来吞吐下降。
COMPOSER_PNG_OPTIMIZE: bool = False

#: 合成时底图与注记允许的尺寸差异（像素）。0 表示必须严格相等。
#: 留成常量而不是写死 0，是为了将来遇到"服务端返回 257×257"这类脏数据时
#: 有个集中的调整点；当前严格要求相等（尺寸不一致说明服务端给错了东西）。
COMPOSER_SIZE_TOLERANCE_PIXELS: int = 0

#: 合成模式写进 MBTiles ``metadata.provider`` 的值
MBTILES_PROVIDER_TIANDITU: str = "tianditu"

# --------------------------------------------------------------------------- #
# 默认输出
# --------------------------------------------------------------------------- #
DEFAULT_OUTPUT_DIR: Path = Path.home() / "MapDownloader" / "output"

#: 任务记录文件：``~/MapDownloader/data/tasks.json``。
#: 刻意与输出目录同一父目录——"这个工具产生的东西"集中在一处，用户好清理。
DEFAULT_TASKS_DIR: Path = DEFAULT_OUTPUT_DIR.parent / TASKS_DIR_NAME
DEFAULT_TASKS_FILE: Path = DEFAULT_TASKS_DIR / TASKS_FILE_NAME

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

#: 设备区占位说明。ADB / Android 导入**不在 Slice 7 的范围内**
#: （Slice 7 排给了"Windows GUI 下载管理"，Spec §29 明确禁止实现 ADB），
#: 因此这里不再承诺某个具体 Slice 编号，只如实说明"点了不会执行 adb"，
#: 不能让人误以为按钮坏了。
#: 文案与 ``main_window`` 里的动作提示保持一致。
UI_TEXT_DEVICE_HINT: str = "本版本不执行 adb 命令，设备管理（ADB）尚未实现"

#: Slice 4 起：确实会落盘，提示改为说明输出产物形态（不再是"测试模式"）
UI_TEXT_MBTILES_OUTPUT_NOTICE: str = (
    "下载结果将写入 MBTiles 单文件（<地图名称>.mbtiles），可直接供 osmdroid 读取"
)

# --------------------------------------------------------------------------- #
# 通用按钮文案
# --------------------------------------------------------------------------- #
#: ``QMessageBox`` 的**标准按钮**（Yes / No / Close / Cancel）文案来自 Qt 自己，
#: 没有装 Qt 汉化目录时显示的是英文。界面其余部分全是中文，
#: 于是完成小结会变成「打开目录」和 "Close" 并排——这是 Slice 7 实机验收
#: 截图里抓到的，纯跑 ``offscreen`` 的 pytest 看不见（那里根本不渲染按钮）。
#: 约定：凡是给用户点的标准按钮，都显式 ``setText`` 成中文；
#: 文案集中在这里，不散落到各个弹窗里。
UI_TEXT_BUTTON_CONFIRM: str = "确认"
UI_TEXT_BUTTON_CANCEL: str = "取消"
UI_TEXT_BUTTON_CLOSE: str = "关闭"
UI_TEXT_BUTTON_OK: str = "确定"

#: 大任务风险提示（只提示，不限制）
UI_TEXT_LARGE_TASK_CONFIRM_TITLE: str = "大任务确认"
UI_TEXT_LARGE_TASK_CONFIRM: str = (
    "当前范围预计需要下载 {total:,} 张瓦片，\n"
    "预计容量约 {size}，\n"
    "预计 WMTS 请求约 {requests:,} 次"
    "（开启中文注记时每张瓦片要请求底图 + 注记两次），\n"
    "可能需要较长时间并消耗天地图调用配额。\n\n"
    "确认继续下载吗？（不会限制下载规模，仅作提醒）"
)

#: 取消任务后的提示：已写入的瓦片保留，且可以在任务列表里继续
UI_TEXT_CANCELLED_KEEP_TILES: str = (
    "任务已取消，已完成瓦片已保留。\n"
    "可在「下载任务」列表里选中该任务后点「继续」，已有瓦片会自动跳过。"
)

#: 未指定地图名称时的提示
UI_TEXT_MAP_NAME_REQUIRED: str = "请先填写地图名称（用于生成 <名称>.mbtiles）"

# --------------------------------------------------------------------------- #
# UI 文案：断点续传 / 任务（Slice 5）
# --------------------------------------------------------------------------- #
#: 目标 .mbtiles 已存在时的三选一提示（继续下载 / 重新下载 / 取消）
UI_TEXT_MBTILES_EXISTS_TITLE: str = "检测到已有地图文件"
UI_TEXT_MBTILES_EXISTS: str = (
    "检测到已有地图文件：\n\n"
    "{path}\n\n"
    "已有瓦片：{existing:,} 张\n\n"
    "继续下载：复用该文件，已有瓦片会自动跳过\n"
    "重新下载：备份旧文件后从头开始（不会静默删除）"
)
UI_TEXT_MBTILES_EXISTS_CONTINUE: str = "继续下载"
UI_TEXT_MBTILES_EXISTS_RESTART: str = "重新下载"
UI_TEXT_MBTILES_EXISTS_CANCEL: str = "取消"

#: 选"重新下载"后的二次确认
UI_TEXT_MBTILES_RESTART_TITLE: str = "确认重新下载"
UI_TEXT_MBTILES_RESTART_CONFIRM: str = (
    "将把原文件重命名备份为：\n\n{backup}\n\n"
    "然后从零重新下载全部 {total:,} 张瓦片。\n\n"
    "确认继续吗？"
)

#: 恢复任务时没有 Token
UI_TEXT_RESUME_TOKEN_REQUIRED: str = (
    "恢复下载任务需要天地图 Token。\n"
    "任务记录里**不会**保存 Token（密钥不落盘），请在界面上填写 Token 后再点继续。"
)

#: 已有 MBTiles 与任务参数不一致（Spec §12 的原文）
UI_TEXT_RESUME_INCOMPATIBLE: str = (
    "现有 MBTiles 与当前任务参数不一致，无法安全继续下载。"
)

#: 关窗时仍有任务在下载（Spec §17）
UI_TEXT_EXIT_WITH_TASK_TITLE: str = "任务仍在下载"
UI_TEXT_EXIT_WITH_TASK: str = (
    "当前存在正在下载的地图任务。\n\n"
    "退出后任务将暂停，下次可以继续下载。\n\n是否退出？"
)
UI_TEXT_EXIT_CONFIRM: str = "退出"
UI_TEXT_EXIT_CANCEL: str = "取消"

#: 任务列表区
UI_TEXT_TASK_PANEL_EMPTY: str = "暂无任务记录"
UI_TEXT_TASK_RESUME: str = "继续"
UI_TEXT_TASK_DELETE: str = "删除记录"
UI_TEXT_TASK_DELETE_HINT: str = "只删除任务记录，不会删除已生成的 .mbtiles 文件"
UI_TEXT_TASK_RECOVERED: str = (
    "检测到上次退出时仍有未完成任务，已标记为「已暂停」。"
    "程序不会自动开始下载，需要你确认后手动继续。"
)

# --------------------------------------------------------------------------- #
# UI 文案：底图 + 中文注记合成（Slice 6）
# --------------------------------------------------------------------------- #
#: 「地图类型 / 中文注记」下方的动态说明（Spec §13）。
#: 三行分别是：这次请求哪些图层、产物是什么形态、每张瓦片要几次请求。
UI_TEXT_LAYER_PLAN: str = (
    "当前下载：{layers}\n"
    "输出：{output}\n"
    "每张瓦片 {requests} 次 WMTS 请求"
)

#: 合成模式的产物说明
UI_TEXT_LAYER_OUTPUT_MERGED: str = (
    "合并后的 PNG MBTiles（底图 + 中文注记 → RGBA alpha 合成）"
)
#: 单图层模式的产物说明
UI_TEXT_LAYER_OUTPUT_SINGLE: str = "PNG MBTiles（仅底图，单图层，未合成）"

#: 「预估结果」里的请求量行。合成模式要说清"为什么是两倍"——
#: 否则用户看到"20,000 张瓦片 / 40,000 次请求"会以为程序算错了。
UI_TEXT_ESTIMATED_REQUESTS_SINGLE: str = "预计 WMTS 请求：{requests:,}"
UI_TEXT_ESTIMATED_REQUESTS_MERGED: str = (
    "预计 WMTS 请求：{requests:,}（{tiles:,} 张 × 底图+注记 2 层）"
)

#: 「预估结果」里的 Zoom 与地图类型行（Slice 7，Spec §3 要求这两项也显示出来）。
#: 地图类型的文字由 :func:`app.models.describe_map_type` 生成，
#: 界面不在这里拼"矢量地图 + 中文注记"。
UI_TEXT_ESTIMATED_ZOOM: str = "Zoom：{min_zoom} ~ {max_zoom}"
UI_TEXT_ESTIMATED_MAP_TYPE: str = "地图类型：{map_type}"

#: 已有 MBTiles 的"地图类型 / 注记配置"与当前任务不一致（Spec §12 原文）
UI_TEXT_RESUME_COMPOSITION_INCOMPATIBLE: str = (
    "现有 MBTiles 地图类型或注记配置与当前任务不一致，"
    "无法安全继续下载。"
)

#: 合成模式的任务启动说明（Spec §23）——一次性把"这次到底在下什么"讲清楚
UI_TEXT_COMPOSITION_SINGLE: str = "single（仅底图）"
UI_TEXT_COMPOSITION_MERGED: str = "merged（底图 + 中文注记已合成）"

# --------------------------------------------------------------------------- #
# Windows GUI 下载管理（Slice 7）
# --------------------------------------------------------------------------- #
#: 已有瓦片扫描（Spec §11）。扫描走的是 SQLite，**必须**在后台线程里做，
#: 因此用户在等待期间需要一句明确的话，否则会以为程序没反应。
UI_TEXT_SCANNING_EXISTING: str = "正在扫描已有瓦片…"
UI_TEXT_SCAN_RESULT: str = "已有：{existing:,}\n待下载：{remaining:,}"

#: 下载前统一校验（Spec §20）。每条都写成"用户该做什么"，而不是"哪里不合法"——
#: 弹窗是要给人看的，人关心的永远是下一步。
UI_TEXT_VALIDATION_TITLE: str = "无法开始下载"
UI_TEXT_VALIDATION_PREFIX: str = "以下问题需要先修正："
UI_TEXT_VALIDATION_TOKEN_REQUIRED: str = (
    "请填写天地图服务密钥（Token）——没有它无法向天地图请求瓦片。"
)
UI_TEXT_VALIDATION_WEST_EAST: str = "西经度必须小于东经度（West < East）。"
UI_TEXT_VALIDATION_SOUTH_NORTH: str = "南纬度必须小于北纬度（South < North）。"
UI_TEXT_VALIDATION_LONGITUDE_RANGE: str = "经度必须在 -180 ~ 180 之间。"
UI_TEXT_VALIDATION_LATITUDE_RANGE: str = "纬度必须在 -90 ~ 90 之间。"
UI_TEXT_VALIDATION_ZOOM_ORDER: str = "Min Zoom 不能大于 Max Zoom。"
UI_TEXT_VALIDATION_ZOOM_RANGE: str = (
    "缩放级别必须在 {low} ~ {high} 之间（与 Tile Math 的服务范围保持一致）。"
)
UI_TEXT_VALIDATION_OUTPUT_DIR_REQUIRED: str = "请选择输出目录（.mbtiles 的存放位置）。"
UI_TEXT_VALIDATION_OUTPUT_DIR_IS_FILE: str = (
    "输出目录指向的是一个文件，不是目录：{path}"
)

#: 地图名称为空时自动生成（Spec §2.7）。生成规则见
#: :func:`app.ui.validation.auto_map_name`——用中心点坐标，保证同名区域得到
#: 同一个名字、不同区域不会撞名。
UI_TEXT_MAP_NAME_AUTO_GENERATED: str = "地图名称为空，已自动生成：{name}"

#: 取消下载的二次确认（Spec §15）。必须明说"数据会保留"——
#: 否则用户会因为怕丢成果而不敢点，或者点完以为白下了几个小时。
UI_TEXT_CANCEL_TITLE: str = "取消下载任务"
UI_TEXT_CANCEL_CONFIRM: str = (
    "确定取消当前下载任务？\n\n"
    "已经下载的地图数据将保留，以后仍可继续。"
)

#: 下载完成弹窗（Spec §16）
UI_TEXT_COMPLETE_TITLE: str = "下载完成"
#: Slice 8（Spec §22）追加了最后两行：完成小结要能一眼看出"有没有缺瓦片、
#: 抽样的图片有没有坏的"——这两件事原先只写在 ``validation.warnings`` 里，
#: 用户得去翻日志才知道。
UI_TEXT_COMPLETE_BODY: str = (
    "文件：{path}\n"
    "地图：{map_type}\n"
    "Zoom：{zooms}\n"
    "瓦片：{tiles:,}\n"
    "文件大小：{size}\n"
    "验证：{validation}\n"
    "缺失瓦片：{missing}\n"
    "抽样损坏：{corrupt}"
)
UI_TEXT_OPEN_DIRECTORY: str = "打开目录"
UI_TEXT_OPEN_DIRECTORY_FAILED: str = "无法打开目录：{path}\n（{error}）"
UI_TEXT_OPEN_DIRECTORY_NEED_PATH: str = "还没有输出文件，暂时无法打开目录"

#: 任务失败提示（Spec §17）。重点是两句：为什么失败、以及成果还在。
UI_TEXT_FAILED_TITLE: str = "任务失败"
UI_TEXT_FAILED_BODY: str = (
    "原因：{reason}\n\n"
    "本次成功：{succeeded:,} 张\n"
    "本次失败：{failed:,} 张\n\n"
    "已有地图数据已保留。可以点「继续」补齐缺失的瓦片。"
)

#: 日志区「自动滚动」开关（Spec §18）
UI_TEXT_LOG_AUTOSCROLL: str = "自动滚动"
UI_TEXT_LOG_AUTOSCROLL_HINT: str = (
    "开启后日志自动滚到最新一行；查看历史时建议关掉，否则会被不断顶走"
)

#: 任务列表下方的选中信息区（Spec §10）。Token 不在其中——
#: 任务记录里没有它，回填时也只说"需要重新填写"。
UI_TEXT_TASK_INFO_EMPTY: str = "选中一条任务可查看详情"
UI_TEXT_TASK_INFO: str = (
    "任务信息：{name}\n"
    "状态：{status}（Zoom {min_zoom} ~ {max_zoom}，{map_type}）\n"
    "MBTiles：{path}\n"
    "{file_state}"
)
#: 文件状态两句话都只说"能不能继续"，**不说"已有多少张瓦片"**：
#: 数瓦片要查 SQLite，而那是禁止在 Qt 主线程做的事（Spec §7）。
#: 想要那个数字，路径就在上面，点「继续」时程序会去扫描并报出来。
UI_TEXT_TASK_INFO_FILE_EXISTS: str = "文件已存在（{size}），继续时已有瓦片会自动跳过"
UI_TEXT_TASK_INFO_FILE_MISSING: str = "文件尚不存在，继续时会新建"

# --------------------------------------------------------------------------- #
# MBTiles 检查与诊断（Slice 8）
# --------------------------------------------------------------------------- #
#: 三档结论（Spec §20）。判定规则收敛在
#: :meth:`app.mbtiles.diagnostics.MBTilesDiagnosticsResult.status`：
#: 有 ERROR → FAIL；只有 WARNING → WARNING；都没有 → PASS。
DIAGNOSTICS_STATUS_PASS: str = "PASS"
DIAGNOSTICS_STATUS_WARNING: str = "WARNING"
DIAGNOSTICS_STATUS_FAIL: str = "FAIL"

#: 本工具"能直接读"的瓦片格式（Spec §5）。
#:
#: 刻意比 :data:`SUPPORTED_TILE_FORMATS` 窄：``webp`` / ``pbf`` 是 MBTiles 规范
#: 允许的取值，但 osmdroid 与本项目的合成链路都读不了。遇到它们只记 WARNING
#: 而不是 ERROR —— 文件对别的客户端可能仍然有效，诊断工具不该替用户判死刑。
DIAGNOSTICS_SUPPORTED_FORMATS: frozenset[str] = frozenset({"png", "jpg", "jpeg"})

#: 快速检查/深度检查默认抽样多少张瓦片（Spec §10 / §22）。
#: 50 张足够覆盖"每个 zoom 各抽几张 + 多种图片格式"，又远小于全量。
DIAGNOSTICS_DEFAULT_SAMPLE_SIZE: int = 50

#: 抽样数量的可接受区间。``1`` 是下限（0 等于不做检查却还报 PASS）；
#: 上限防止有人在 GUI 上填个 100 万把"快速检查"变成全量扫描。
DIAGNOSTICS_MIN_SAMPLE_SIZE: int = 1
DIAGNOSTICS_MAX_SAMPLE_SIZE: int = 500

#: 深度检查（``deep_scan=True``）的候选抽样数。深度扫描本身就是全量，
#: 这里的抽样结果只作为"概览"展示，取一个比快速检查略大的值即可。
DIAGNOSTICS_DEEP_SAMPLE_SIZE: int = 100

#: 深度图片扫描时每次从 SQLite 取多少行（Spec §11）。
#: 500 × 单张几十 KB ≈ 十几 MB 峰值，属于"流式读取"而非"全量载入"。
DIAGNOSTICS_DEEP_SCAN_BATCH_SIZE: int = 500

#: 缺失瓦片明细最多展开多少条（Spec §13）。一个 z17 级别的库缺几万张是常事，
#: 全展开既没人看得完，也会把报告文件撑到几十兆。
MAX_MISSING_TILE_DETAILS: int = 100

#: 缺失比例超过多少时把 WARNING 写得更醒目（Spec §21）。
#: 1% 是"明显缺了一块"的量级；低于它的零散缺失只记普通提示，
#: **绝不因此判 FAIL** —— 库结构与图片都有效时，文件仍然是局部可用的。
DIAGNOSTICS_MISSING_WARNING_RATIO: float = 0.01

#: 诊断报告的后缀（Spec §26）：``beijing.mbtiles`` → ``beijing.mbtiles.report.txt``
DIAGNOSTICS_REPORT_SUFFIX: str = ".report.txt"

#: GUI：选择 MBTiles 文件时的过滤器
UI_TEXT_INSPECT_FILE_FILTER: str = "MBTiles 文件 (*.mbtiles);;所有文件 (*)"

#: 检查工具（Spec §16 / §17）
UI_TEXT_INSPECT_BUTTON: str = "检查 MBTiles"
UI_TEXT_INSPECT_BUTTON_HINT: str = (
    "检查任意 .mbtiles 的结构、metadata、Zoom 统计、坐标、覆盖完整性与图片抽样。"
    "选中一条任务时会自动带上它的输出文件，也可以在检查窗口里另选文件。"
)
UI_TEXT_INSPECT_TITLE: str = "MBTiles 检查"
UI_TEXT_INSPECT_CHOOSE_FILE: str = "选择文件…"
UI_TEXT_INSPECT_QUICK: str = "快速检查"
UI_TEXT_INSPECT_DEEP: str = "深度检查"
UI_TEXT_INSPECT_DEEP_HINT: str = (
    "深度检查会逐张读取全部 tile_data 校验图片，50 万张瓦片可能耗时数分钟。"
    "日常使用请用「快速检查」。"
)
UI_TEXT_INSPECT_EXPORT: str = "导出报告"
UI_TEXT_INSPECT_NEED_FILE: str = "请先选择一个 .mbtiles 文件"
UI_TEXT_INSPECT_RUNNING: str = "正在检查…"
UI_TEXT_INSPECT_TESTING: str = "检查中，请稍候…"
UI_TEXT_INSPECT_FILE_LABEL: str = "文件"
UI_TEXT_INSPECT_SIZE_LABEL: str = "大小"
UI_TEXT_INSPECT_STATUS_LABEL: str = "状态"
UI_TEXT_INSPECT_EMPTY_HINT: str = "尚未选择文件。选择一个 .mbtiles 后点「快速检查」。"
UI_TEXT_INSPECT_GROUP_BASIC: str = "基本信息"
UI_TEXT_INSPECT_GROUP_ZOOM: str = "Zoom 统计"
UI_TEXT_INSPECT_GROUP_SAMPLE: str = "图片抽样"
UI_TEXT_INSPECT_GROUP_DIAG: str = "诊断结果"
UI_TEXT_INSPECT_EXPORT_DONE: str = "诊断报告已导出：{path}"
UI_TEXT_INSPECT_EXPORT_FAILED: str = "导出诊断报告失败：{error}"
UI_TEXT_INSPECT_FAILED: str = "检查失败：{error}"

#: 基本信息区的字段名（顺序即显示顺序）。
#: 用中文标签 + 原始键名的值：界面语言与全局一致，而 metadata 的键名本身
#: （``name`` / ``format`` / ``bounds`` …）是 MBTiles 规范的一部分，
#: 翻译了反而对不上号，所以它们出现在值里，不出现在标签里。
UI_TEXT_INSPECT_FIELD_NAME: str = "名称"
UI_TEXT_INSPECT_FIELD_FORMAT: str = "图片格式"
UI_TEXT_INSPECT_FIELD_BOUNDS: str = "范围"
UI_TEXT_INSPECT_FIELD_ZOOM: str = "Zoom"
UI_TEXT_INSPECT_FIELD_TILES: str = "瓦片数"
UI_TEXT_INSPECT_FIELD_MAP_TYPE: str = "地图类型"
UI_TEXT_INSPECT_FIELD_COMPOSITION: str = "合成方式"
UI_TEXT_INSPECT_FIELD_SCHEMA: str = "数据库"
UI_TEXT_INSPECT_FIELD_CHECK: str = "SQLite 自检"

#: Zoom 统计表的列名（顺序即列顺序）
UI_TEXT_INSPECT_COLUMN_ZOOM: str = "Zoom"
UI_TEXT_INSPECT_COLUMN_TILES: str = "瓦片"
UI_TEXT_INSPECT_COLUMN_EXPECTED: str = "期望"
UI_TEXT_INSPECT_COLUMN_MISSING: str = "缺失"

#: 抽样结果的四行说明（Spec §17）
UI_TEXT_INSPECT_SAMPLE_SUMMARY: str = (
    "抽样：{samples} 张\n"
    "有效：{valid} 张\n"
    "损坏：{corrupt} 张\n"
    "空瓦片：{empty} 张"
)
UI_TEXT_INSPECT_SAMPLE_FORMATS: str = "格式：{formats}"

#: 值缺失时的占位符（"没有这一项"与"这一项是 0"必须能分辨）
UI_TEXT_INSPECT_UNKNOWN: str = "—"

#: 诊断结论的配色（深浅两套主题都要能看清，因此取中等亮度 + 加粗，
#: 颜色只是辅助信号，文字本身已经写明 PASS / WARNING / FAIL）
UI_TEXT_INSPECT_COLOR_PASS: str = "#2f9e44"
UI_TEXT_INSPECT_COLOR_WARNING: str = "#e8a33d"
UI_TEXT_INSPECT_COLOR_FAIL: str = "#e03131"

#: 报告文件的行模板（Spec §26 要求的内容清单；措辞跟界面一致，用中文）。
#: 字段名（``name`` / ``format`` / ``bounds`` …）保持原样：那是 MBTiles 规范
#: 里的键名，翻译了反而对不上号。
REPORT_FILE_LINE: str = "文件：{path}"
REPORT_SIZE_LINE: str = "大小：{size}（{bytes} 字节）"
REPORT_SQLITE_LINE: str = "SQLite quick_check：{result}"
REPORT_TILE_COUNT_LINE: str = "瓦片总数：{count}"
REPORT_EXPECTED_LINE: str = "期望总数：{count}"
REPORT_MISSING_LINE: str = "缺失总数：{count}"
REPORT_SAMPLE_LINE: str = (
    "图片抽样：samples={samples} valid={valid} corrupt={corrupt} empty={empty}"
)
REPORT_RESULT_LINE: str = "结论：{result}"
REPORT_METADATA_HEADER: str = "Metadata："
REPORT_ZOOM_HEADER: str = "Zoom 统计（Zoom | Tiles | Expected | Missing）："
REPORT_ERRORS_HEADER: str = "Errors："
REPORT_WARNINGS_HEADER: str = "Warnings："
#: 普通提示：只是说明检查做了什么，不影响 PASS/WARNING 判定（Spec §20）
REPORT_NOTICES_HEADER: str = "Notices："
REPORT_NONE: str = "（无）"
REPORT_MISSING_DETAIL_HEADER: str = "缺失瓦片（前 {count} 个，XYZ）："
REPORT_MISSING_DETAIL_TRUNCATED: str = "  …另有 {count:,} 个缺失瓦片未展开显示。"
REPORT_DEEP_SCAN_LINE: str = "深度扫描：扫描 {scanned:,} 张，损坏 {corrupt:,} 张"
REPORT_SAMPLE_FORMATS_LINE: str = "抽样格式：{formats}"


# --------------------------------------------------------------------------- #
# Slice 9：离线地图包（Spec §2 ~ §32）
# --------------------------------------------------------------------------- #
#: manifest 的格式版本（Spec §2 / §26）。Android 侧读到更大的值就该拒绝，
#: 而不是"尽力解析"——本工具**不实现**升级机制，只负责把这个数字写准。
PACKAGE_FORMAT_VERSION: int = 1

#: 地图包内的固定文件名（Spec §2）。这两个名字是**协议的一部分**：
#: Android 导入器按名字找文件，改一个字就等于换了一种包格式。
PACKAGE_MANIFEST_FILENAME: str = "manifest.json"
PACKAGE_MBTILES_FILENAME: str = "map.mbtiles"

#: 预览图文件名（Spec §22：可选能力，本 Slice 不生成）
PACKAGE_PREVIEW_FILENAME: str = "preview.png"

#: manifest 里 provider / tileScheme 的默认值（Spec §2）
PACKAGE_DEFAULT_PROVIDER: str = "tianditu"
PACKAGE_DEFAULT_TILE_SCHEME: str = "web_mercator"

#: 构建中的临时目录后缀（Spec §15）：``<name>.building``。
#: 正式目录只会在**全部步骤成功之后**由一次 ``os.replace`` 出现。
PACKAGE_BUILDING_SUFFIX: str = ".building"

#: 覆盖已有地图包时，旧目录改名成 ``<name>.backup-<时间戳>``（Spec §16）。
#: 与 Slice 5 的"重新下载是改名备份"一致：绝不静默删除用户数据。
PACKAGE_BACKUP_SUFFIX_FORMAT: str = ".backup-%Y%m%d-%H%M%S"

#: 分块复制与流式 SHA256 的块大小（Spec §4 / §9 / §32）。
#: 4 MiB 是"系统调用次数"与"单次内存峰值"之间的折中：GB 级文件不会整读进内存。
PACKAGE_COPY_CHUNK_SIZE: int = 4 * 1024 * 1024
PACKAGE_SHA256_CHUNK_SIZE: int = 4 * 1024 * 1024

#: 构建进度条按阶段分段（Spec §18）。每段的百分比区间在这里定死，
#: 免得 GUI 与 service 各算一套、进度条忽快忽慢。
#: 区间取值依据：复制（字节最多）与 SHA256（要读全文件）各占大头。
PACKAGE_BUILD_STAGE_RANGES: dict[str, tuple[int, int]] = {
    "check": (0, 6),
    "copy": (6, 66),
    "sha256": (66, 90),
    "manifest": (90, 94),
    "validate": (94, 100),
}

#: 校验阶段的分段（Spec §21 的四行结论对应四步）
PACKAGE_VALIDATE_STAGE_RANGES: dict[str, tuple[int, int]] = {
    "manifest": (0, 10),
    "size": (10, 20),
    "checksum": (20, 70),
    "mbtiles": (70, 90),
    "metadata": (90, 100),
}

#: 地图包构建前的快速诊断抽样张数（Spec §23 复用 Slice 8）。
#: 打包前只需要"这个库基本是好的"，不需要全量图片扫描（那是分钟级）。
PACKAGE_PRECHECK_SAMPLE_SIZE: int = 20

#: bounds 比对容差：manifest 里存的是十进制小数，读回来与 metadata 的字符串
#: 解析结果做浮点比较，必须给容差。1e-6 ≈ 0.1 米，远小于一个瓦片的尺寸。
PACKAGE_BOUNDS_TOLERANCE: float = 1e-6

#: 默认输出目录名（放在用户选择的目录下）：``<output>/packages``
PACKAGE_DEFAULT_DIR_NAME: str = "packages"

#: SHA256 的十六进制长度（校验用，别写裸 64）
PACKAGE_SHA256_LENGTH: int = 64

# --- 构建 / 校验的结论文案（Spec §14 / §20 / §21）-----------------------------
UI_TEXT_PACKAGE_STATUS_LABEL: str = "验证"
UI_TEXT_PACKAGE_MANIFEST_LABEL: str = "Manifest"
UI_TEXT_PACKAGE_MBTILES_LABEL: str = "MBTiles"
UI_TEXT_PACKAGE_SHA256_LABEL: str = "SHA256"
UI_TEXT_PACKAGE_RESULT_LABEL: str = "结果"
UI_TEXT_PACKAGE_UNKNOWN: str = "—"

#: 构建阶段的进度文案（Spec §18 逐字要求这五句）
UI_TEXT_PACKAGE_STAGE_CHECK: str = "正在检查 MBTiles…"
UI_TEXT_PACKAGE_STAGE_COPY: str = "正在复制地图文件…"
UI_TEXT_PACKAGE_STAGE_SHA256: str = "正在计算 SHA256…"
UI_TEXT_PACKAGE_STAGE_MANIFEST: str = "正在生成 manifest…"
UI_TEXT_PACKAGE_STAGE_VALIDATE: str = "正在验证地图包…"
UI_TEXT_PACKAGE_STAGE_DONE: str = "地图包生成完成"

#: 校验阶段的进度文案
UI_TEXT_PACKAGE_STAGE_READ_MANIFEST: str = "正在读取 manifest…"
UI_TEXT_PACKAGE_STAGE_SIZE: str = "正在核对文件大小…"
UI_TEXT_PACKAGE_STAGE_MBTILES: str = "正在检查 MBTiles…"
UI_TEXT_PACKAGE_STAGE_METADATA: str = "正在比对 metadata…"

#: 复制 / 哈希的细粒度进度（``{done}`` / ``{total}`` 已是可读大小）
UI_TEXT_PACKAGE_PROGRESS_COPY: str = "正在复制地图文件…{done} / {total}"
UI_TEXT_PACKAGE_PROGRESS_SHA256: str = "正在计算 SHA256…{percent}%"

#: GUI 文案（Spec §17 / §20 / §21）
UI_TEXT_PACKAGE_TITLE: str = "生成离线地图包"
UI_TEXT_PACKAGE_VALIDATE_TITLE: str = "验证离线地图包"
UI_TEXT_PACKAGE_BUTTON: str = "生成地图包"
UI_TEXT_PACKAGE_BUTTON_HINT: str = (
    "把 .mbtiles 整理成标准离线地图包（map.mbtiles + manifest.json + SHA256），"
    "供 Android 端导入。选中一条任务时会带上它的输出文件。"
)
UI_TEXT_PACKAGE_BUTTON_VALIDATE: str = "验证地图包"
UI_TEXT_PACKAGE_BUTTON_VALIDATE_HINT: str = (
    "校验一个离线地图包：manifest 是否合法、文件大小与 SHA256 是否对得上、"
    "MBTiles 是否有效、metadata 与 manifest 是否一致。"
)
UI_TEXT_PACKAGE_SOURCE_LABEL: str = "源 MBTiles"
UI_TEXT_PACKAGE_NAME_LABEL: str = "地图名称"
UI_TEXT_PACKAGE_MAP_TYPE_LABEL: str = "地图类型"
UI_TEXT_PACKAGE_ZOOM_LABEL: str = "Zoom"
UI_TEXT_PACKAGE_TILES_LABEL: str = "瓦片"
UI_TEXT_PACKAGE_OUTPUT_LABEL: str = "输出目录"
UI_TEXT_PACKAGE_PACKAGE_LABEL: str = "地图包目录"
UI_TEXT_PACKAGE_CHOOSE_FILE: str = "选择文件…"
UI_TEXT_PACKAGE_CHOOSE_DIR: str = "选择目录…"
UI_TEXT_PACKAGE_GROUP_SOURCE: str = "地图信息"
UI_TEXT_PACKAGE_GROUP_INFO: str = "地图信息（来自 MBTiles）"
UI_TEXT_PACKAGE_GROUP_PACKAGE: str = "地图包内容"
UI_TEXT_PACKAGE_GROUP_DIAG: str = "检查与验证结果"
#: 还没生成过任何包时，「地图包内容」区顶部的那句话
UI_TEXT_PACKAGE_BODY_PLACEHOLDER: str = "尚未生成地图包"
UI_TEXT_PACKAGE_FILE_FILTER: str = "MBTiles 文件 (*.mbtiles);;所有文件 (*)"
UI_TEXT_PACKAGE_COMPLETE_TITLE: str = "地图包生成完成"
UI_TEXT_PACKAGE_COMPLETE_BODY: str = (
    "目录：{directory}\n"
    "MBTiles：{size}\n"
    "瓦片：{tiles}\n"
    "Zoom：{zooms}\n"
    "SHA256：{sha256}\n"
    "验证：{validation}"
)
UI_TEXT_PACKAGE_FAILED_TITLE: str = "地图包生成失败"
UI_TEXT_PACKAGE_VALIDATE_DONE_TITLE: str = "地图包验证完成"
UI_TEXT_PACKAGE_VALIDATE_BODY: str = (
    "Manifest：{manifest}\n"
    "MBTiles：{mbtiles}\n"
    "SHA256：{checksum}\n"
    "\n"
    "Result：{result}"
)
UI_TEXT_PACKAGE_NEED_SOURCE: str = "请先选择一个 .mbtiles 文件"
UI_TEXT_PACKAGE_NEED_DIR: str = "请先选择输出目录"
UI_TEXT_PACKAGE_NEED_NAME: str = "请填写地图名称"
UI_TEXT_PACKAGE_NEED_PACKAGE: str = "请先选择一个地图包目录"
UI_TEXT_PACKAGE_RUNNING: str = "正在生成地图包，请稍候…"
UI_TEXT_PACKAGE_VALIDATING: str = "正在验证地图包，请稍候…"
UI_TEXT_PACKAGE_FAILED: str = "生成地图包失败：{error}"
UI_TEXT_PACKAGE_VALIDATE_FAILED: str = "验证失败：{error}"
UI_TEXT_PACKAGE_PASS_STATUS: str = "PASS"

#: 地图包结论的配色（与 Slice 8 检查窗口同一套语义色；实际显示时会按
#: 当前主题把颜色朝黑/朝白压一下，保证深浅两套主题都读得清）
UI_TEXT_PACKAGE_COLOR_PASS: str = "#2f9e44"
UI_TEXT_PACKAGE_COLOR_FAIL: str = "#e03131"

#: 覆盖确认（Spec §16）。必须说清"旧包去哪了"，否则用户以为被删了。
UI_TEXT_PACKAGE_EXISTS_TITLE: str = "地图包已存在"
UI_TEXT_PACKAGE_EXISTS_BODY: str = (
    "地图包已存在：\n{path}\n\n"
    "选择「覆盖」会把现有地图包改名备份（不会删除），然后用新的包替换它。"
)
UI_TEXT_PACKAGE_BUTTON_OVERWRITE: str = "覆盖"
UI_TEXT_PACKAGE_BUTTON_RENAME: str = "选择其他名称"
UI_TEXT_PACKAGE_BACKUP_DONE: str = "已备份原有地图包：{path}"

#: MBTiles 带 WARNING 时的二次确认（Spec §23）
UI_TEXT_PACKAGE_WARNING_TITLE: str = "MBTiles 存在警告"
UI_TEXT_PACKAGE_WARNING_BODY: str = (
    "MBTiles 存在警告，是否继续生成地图包？\n\n{warnings}"
)
UI_TEXT_PACKAGE_BUTTON_CONTINUE: str = "继续生成"

#: 失败原因文案（Spec §6 / §8 / §12 / §14 / §16）
PACKAGE_ERROR_SOURCE_MISSING: str = "源文件不存在：{path}"
PACKAGE_ERROR_SOURCE_NOT_FILE: str = "源路径不是文件：{path}"
PACKAGE_ERROR_SOURCE_INSPECT_FAIL: str = "MBTiles 检查未通过（FAIL），不生成地图包：{reasons}"
PACKAGE_ERROR_SOURCE_WARNING: str = "MBTiles 存在警告，需要确认后才能生成地图包"
PACKAGE_ERROR_METADATA_MISSING: str = "MBTiles metadata 缺少关键字段，无法生成 manifest：{fields}"
PACKAGE_ERROR_EXISTS: str = "目标目录已存在：{path}"
PACKAGE_ERROR_COPY_SELF: str = "源文件与目标位置是同一个文件，已跳过复制"
PACKAGE_ERROR_NO_TILE: str = "MBTiles 里没有瓦片，不生成地图包"
PACKAGE_ERROR_BUILD_FAILED: str = "生成地图包失败：{error}"
PACKAGE_ERROR_MANIFEST_MISSING: str = "缺少 manifest.json：{path}"
PACKAGE_ERROR_MANIFEST_JSON: str = "manifest.json 不是合法的 JSON：{error}"
PACKAGE_ERROR_MANIFEST_FORMAT_VERSION: str = (
    "manifest 的 formatVersion={version} 不受支持（本工具支持 {supported}）"
)
PACKAGE_ERROR_MANIFEST_FIELD: str = "manifest 字段非法：{error}"
PACKAGE_ERROR_MANIFEST_PATH: str = "manifest 的 mbtiles.file 指向包外：{value}"
PACKAGE_ERROR_MBTILES_MISSING: str = "地图包里缺少 MBTiles 文件：{path}"
PACKAGE_ERROR_SIZE_MISMATCH: str = "MBTiles 大小不一致：manifest 记录 {expected} 字节，实际 {actual} 字节"
PACKAGE_ERROR_SHA256_MISMATCH: str = "MBTiles SHA256 mismatch：manifest 记录 {expected}，实际 {actual}"
PACKAGE_ERROR_MBTILES_INVALID: str = "MBTiles 检查未通过（{status}）：{reasons}"
PACKAGE_WARNING_METADATA_MISSING: str = "MBTiles metadata 缺少 {field}，无法与 manifest 比对"
PACKAGE_WARNING_METADATA_DIFF: str = "MBTiles metadata 的 {field} 与 manifest 不一致：{actual} ≠ {expected}"
PACKAGE_ERROR_METADATA_DIFF: str = "关键字段不一致：{field} —— manifest {expected}，MBTiles {actual}"
PACKAGE_WARNING_CHECKSUM_SKIPPED: str = "已跳过 SHA256 校验（verify_checksum=False）"
PACKAGE_WARNING_MBTILES_SKIPPED: str = "已跳过 MBTiles 检查（inspect_mbtiles=False）"

#: 构建结果里的一行说明
PACKAGE_LINE_DIRECTORY: str = "地图包目录：{path}"
PACKAGE_LINE_MANIFEST: str = "manifest：{path}"
PACKAGE_LINE_MBTILES: str = "map.mbtiles：{path}"
PACKAGE_LINE_SHA256: str = "SHA256：{sha256}"
PACKAGE_LINE_VALIDATION: str = "验证结论：{result}"

