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

# --------------------------------------------------------------------------- #
# 多级下载计划（Slice 20）
# --------------------------------------------------------------------------- #
#: 计划文件：与任务记录同一个目录（``~/MapDownloader/data/``）。
#:
#: 刻意与 ``tasks.json`` **分开两个文件**：它们的写入频率差了几个数量级
#: ——任务记录在下载过程中被反复覆盖（进度落盘），计划只在用户改计划时写一次。
#: 混在一个文件里，一次进度落盘就会把计划一起重写，任何一次半截写入都会
#: 同时毁掉两份数据。
PLANS_FILE_NAME: str = "plans.json"

#: 计划文件的 JSON 结构版本号（与任务文件分开计数：两者的 schema 无关）。
PLANS_FILE_VERSION: int = 1

#: 计划文件损坏时的隔离后缀（同 :data:`TASKS_CORRUPT_SUFFIX`：不删用户文件）
PLANS_CORRUPT_SUFFIX: str = ".corrupt"

#: 默认计划名。它同时是**输出子目录名**（``<输出目录>/<计划名>/``），
#: 因此必须是文件名安全的 ASCII：中文在 Windows 上能用，但会让命令行排查、
#: 外部工具与日志 grep 都变麻烦，而用户随时能在界面上改成自己的名字。
DEFAULT_PLAN_NAME: str = "offline_plan"

#: 计划级"规模偏大"的提示阈值（只警告，不禁止）。
#:
#: 单任务的大任务确认阈值是 :data:`GUI_LARGE_TASK_CONFIRM_TILES`（1 万张），
#: 但那是**一个**任务；一个计划由多个任务组成，"全国 + 若干省 + 若干市"
#: 加到一起轻易就超过它。5 万张 ≈ 1.5GB、5GB 容量两档，是"用户该看一眼"
#: 而不是"值得为它弹一次确认框"的量级（Spec §9 明确只要 warning）。
PLAN_LARGE_TILES_WARNING: int = 50_000
PLAN_LARGE_SIZE_WARNING_BYTES: int = 5 * 1024 * 1024 * 1024

# --------------------------------------------------------------------------- #
# 下载计划的**执行**记录（Slice 21）
# --------------------------------------------------------------------------- #
#: 执行记录文件：``~/MapDownloader/data/plan_executions.json``。
#:
#: 第三个文件（``plans.json`` 是"用户想要什么"，``tasks.json`` 是"每一块地的
#: 单任务记录"，这里是"这份计划跑到哪一步了"）。分开的理由与 Slice 20 分
#: ``plans.json`` 时一样，而且更硬：执行记录带着**进度**，写入频率与任务记录
#: 同级（秒级），而计划本身只在用户改计划时写一次。混在一起的话，一次进度
#: 落盘就会把用户的计划一起重写。
PLAN_EXECUTIONS_FILE_NAME: str = "plan_executions.json"

#: 执行记录的结构版本号（与前两个文件分开计数：schema 无关）。
PLAN_EXECUTIONS_FILE_VERSION: int = 1

#: 执行记录损坏时的隔离后缀（同 :data:`TASKS_CORRUPT_SUFFIX`：不删用户文件）
PLAN_EXECUTIONS_CORRUPT_SUFFIX: str = ".corrupt"

#: 执行进度落盘的节流参数（与任务记录同一套规则：满足任一即写）。
#:
#: **状态变化不受节流限制**：开始 / 暂停 / 继续 / 取消 / 每项完成 / 整份计划
#: 结束一律立即落盘。丢一秒的进度无所谓，丢"计划已经停了"会让人判断错误。
DEFAULT_PLAN_SAVE_INTERVAL_SECONDS: float = 1.0
DEFAULT_PLAN_SAVE_TILE_INTERVAL: int = 100

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

#: 计划文件：``~/MapDownloader/data/plans.json``（Slice 20）。
DEFAULT_PLANS_FILE: Path = DEFAULT_TASKS_DIR / PLANS_FILE_NAME

#: 执行记录文件：``~/MapDownloader/data/plan_executions.json``（Slice 21）。
DEFAULT_PLAN_EXECUTIONS_FILE: Path = DEFAULT_TASKS_DIR / PLAN_EXECUTIONS_FILE_NAME

# --------------------------------------------------------------------------- #
# 行政区域边界资源（Slice 16）
# --------------------------------------------------------------------------- #
#: 行政区域边界（GeoJSON）所在目录：``<仓库根>/resources/regions``。
#:
#: 目录结构见 ``resources/regions/README.md``：元数据在 ``index.json``，
#: 边界文件按层级放 ``country/`` / ``province/`` / ``city/`` 三棵子树
#: （Slice 18）。**本目录默认不含任何边界数据。** 中国国界属于法定地图内容，
#: 必须使用带审图号的合法授权数据，本仓库既不内置也不伪造；部署方需自行放入
#: :data:`CHINA_REGION_RELATIVE_PATH`。缺少该文件时，"行政区域"模式会明确报错
#: 或把覆盖度标成 UNKNOWN，而不是退化成矩形下载 —— 那会静默下载一大片境外瓦片。
REGION_RESOURCE_DIR: Path = (
    Path(__file__).resolve().parents[2] / "resources" / "regions"
)

#: 行政区数据导入管线的**默认发布目录**（Slice 26）：
#: ``<仓库根>/resources/regions/generated``。
#:
#: 与 :data:`REGION_RESOURCE_DIR`（手工维护的目录）分开：手工数据是"人一条
#: 条加进 index.json 的"，生成数据是"整批发布、整批替换"的，两者混在一个
#: 目录里迟早互相覆盖。发布是原子的（见
#: :mod:`app.regions.importer.region_exporter`），失败时旧目录原样。
#:
#: 生成数据集存在时（该目录下有 ``index.json``），
#: :func:`app.regions.catalog.effective_region_root` 会优先选它 ——
#: RegionCatalog 不需要知道数据是手工的还是导入器生成的（Slice 26 §16）。
#: 生成目录本身也受 ``resources/regions/**/*.geojson`` 的 gitignore 规则
#: 保护（授权数据的再分发条件由提供方决定，别顺手提交）。
REGION_DATA_ROOT: Path = REGION_RESOURCE_DIR / "generated"

#: 行政区域目录元数据的固定文件名（Slice 18）。它是**元数据**（有哪些区域、
#: 谁是谁的上级、边界文件在哪），不是边界数据，因此随程序一起进仓库。
REGION_INDEX_FILENAME: str = "index.json"

#: 中国行政区域边界文件的名字（不含目录）。
CHINA_REGION_FILENAME: str = "china.geojson"

#: 中国边界文件在 :data:`REGION_RESOURCE_DIR` 下的**相对路径**（Slice 18：
#: 全国级数据放 ``country/`` 子树）。Slice 16 时它是扁平的 ``china.geojson``，
#: 迁到子目录后用户需要把文件放到 ``resources/regions/country/`` 下 ——
#: 两处（目录元数据与内置预设）引用的是同一个字符串常量，不会各说各话。
CHINA_REGION_RELATIVE_PATH: str = f"country/{CHINA_REGION_FILENAME}"

#: 行政区域 Preset 的展示名。
CHINA_REGION_NAME: str = "中国"

#: 行政区边界资源不可用时的说明文案（UI / Inspector / 包校验共用同一句，
#: 避免三处各写一遍导致用户看到三种说法）。``{path}`` 由调用方填入。
REGION_BOUNDARY_MISSING_HINT: str = "行政区边界文件不可用：{path}"

#: 行政区边界数据缺失时，README 与界面提示里指向的获取要求。
REGION_BOUNDARY_LICENSE_NOTICE: str = (
    "中国国界属于法定地图内容，请使用带审图号的合法授权 GeoJSON 数据"
)

# --------------------------------------------------------------------------- #
# 任务区（Mission）—— 航线 Buffer（Slice 22）
# --------------------------------------------------------------------------- #
#: 任务区落盘目录名：``~/MapDownloader/data/mission_regions``。
#:
#: 与 ``tasks.json`` / ``plans.json`` 同一个 ``data/`` 下面：任务区几何是
#: **本工具产生的数据**，不是随程序分发的资源（那才是 ``resources/regions``）。
#: 每个任务区一个子目录：``<mission-id>/route.geojson``、``region.geojson``、
#: ``metadata.json``（Spec §11）。
MISSION_REGIONS_DIR_NAME: str = "mission_regions"
MISSION_REGIONS_DIR: Path = DEFAULT_TASKS_DIR / MISSION_REGIONS_DIR_NAME

#: 一个任务区目录里的三个固定文件名（Spec §11）
MISSION_ROUTE_FILENAME: str = "route.geojson"
MISSION_REGION_FILENAME: str = "region.geojson"
MISSION_METADATA_FILENAME: str = "metadata.json"

#: 任务区 id 前缀。它**是跨层契约**：下载链路、Inspector、计划层都靠这个前缀
#: 把"任务区"与"行政区目录里的 id"分开（``region_id`` 是唯一线索，见
#: ``app.geo.region`` 的动态解析注册表）。
MISSION_ID_PREFIX: str = "mission-"

#: 任务区目录元数据的版本号（与 ``TASKS_FILE_VERSION`` 同一套思路）：
#: 将来字段变了，读的时候能按版本号分流，而不是靠"有没有这个键"猜。
MISSION_METADATA_VERSION: int = 1

#: 几何版本号的前缀。完整版本号形如 ``1:5000.0:3f2a…``（结构 + Buffer + 航线摘要），
#: 由 :meth:`app.mission.MissionRegionBuilder.geometry_version` 生成。
#:
#: 它会被写进任务记录的 ``boundary_version`` 与 MBTiles metadata：**同一份几何
#: 永远得到同一个版本号**，于是"改了航线就该是一个新任务区"这件事有据可查
#: （Spec §21：绝不悄悄改一个在跑 / 已下载的任务的几何）。
MISSION_GEOMETRY_VERSION: str = "1"

#: 默认 Buffer 半径（米）。5 km 对应 Spec §7 的例子：一条城市周边的航线，
#: 5 km 走廊大约覆盖 Z15~18 的作业范围。
DEFAULT_MISSION_BUFFER_METERS: float = 5000.0

#: Buffer 半径允许区间（米）。下界防"退化成一条线"，上界防"其实是在下整座城市"。
MISSION_BUFFER_METERS_MIN: float = 100.0
MISSION_BUFFER_METERS_MAX: float = 50_000.0

#: 一条航线至少要几个点。两个点才构成一条线；一个点只能生成圆，
#: 那属于"自定义区域"而不是航线任务（Spec §6）。
MISSION_MIN_WAYPOINTS: int = 2

#: 本地米制投影（AEQD）允许的最大航线跨度（度）。
#:
#: AEQD 以航线中心为原点，离中心越远形变越大。本工具的用法是"一条作业航线"，
#: 跨度通常在 0.1°~2° 量级；超过 10°（约 1100 km）的"航线"已经不适合用单点
#: AEQD 做米制 Buffer，此时**直接报错**而不是给一个悄悄失真的结果（Spec §10）。
MISSION_MAX_ROUTE_SPAN_DEGREES: float = 10.0

#: 任务区几何与元数据里记录的范围允许的差异（度）。
#: 两者本来就是同一份浮点数写出去的，差异只可能来自"文件被改过" —— 一旦超出
#: 这个容差就报错，而不是拿着一个和元数据对不上的几何去续传（那会改变瓦片集）。
MISSION_BOUNDS_TOLERANCE: float = 1e-6

#: 任务区界面上 Buffer 输入框使用的单位换算（1 km = 1000 m）。
#: 界面按 km 输入（"5" 比 "5000" 好读），业务层一律按米 —— 换算只在**入口**做一次。
MISSION_BUFFER_KM_DECIMALS: int = 2

# UI 文案（Slice 22）。与其余 UI_TEXT_* 一样：文案属于配置，不散落在控件代码里。
UI_TEXT_MISSION_DIALOG_TITLE: str = "任务区域（航线 Buffer）"
UI_TEXT_MISSION_NAME_REQUIRED: str = "请先填写任务区名称"
UI_TEXT_MISSION_ROUTE_REQUIRED: str = "请先选择航线文件（GeoJSON LineString）"
UI_TEXT_MISSION_BUFFER_RANGE: str = (
    "Buffer 半径必须在 {min_m:.1f} ~ {max_m:.1f} 米"
    "（界面按 km 输入）之间"
)
UI_TEXT_MISSION_DELETE_REFERENCED: str = "该任务区域正在被下载计划引用，无法删除"
UI_TEXT_MISSION_EMPTY: str = "暂无任务区"
UI_TEXT_MISSION_NOT_FOUND: str = "任务区不存在或几何文件已不可用：{mission_id}"

# --------------------------------------------------------------------------- #
# 下载风险分析（Slice 23）
# --------------------------------------------------------------------------- #
#: 危险任务的**强确认词**（Spec §12）：必须手输这个字符串才能继续下载。
#:
#: 为什么不是"再点一次确认"：LARGE 那档已经是一次确认了，危险档必须拉开档次
#: —— 一个可以无脑点的按钮，和一个要看清屏幕再敲 8 个字符的输入框，
#: 拦下来的任务完全不是一回事。放在配置里是因为界面、测试与文档引用的是
#: **同一个**字面量（在测试里写死 "CONTINUE" 就成了第二份真值）。
DANGEROUS_CONFIRM_WORD: str = "CONTINUE"

# UI 文案（Slice 23）。与其余 UI_TEXT_* 一样：文案属于配置，不散落在控件代码里。
UI_TEXT_RISK_GROUP_TITLE: str = "风险提示"
UI_TEXT_BUTTON_RISK_DETAILS: str = "查看详情"
UI_TEXT_BUTTON_APPLY_RECOMMENDED_ZOOM: str = "采用推荐 Zoom"
UI_TEXT_RISK_NONE: str = "尚未分析（请先点「计算」）"

#: 「采用推荐 Zoom」的悬停说明。它会**改用户填好的 Zoom**，所以必须说清
#: "改的是哪两个框、改成什么"，而且只有用户主动点才会生效（Spec §11）。
UI_TEXT_RISK_APPLY_TOOLTIP: str = (
    "把 Min / Max Zoom 改成该等级的推荐区间，然后重新计算。"
    "只有你点这个按钮才会改，分析过程不会自动修改你的输入"
)

#: 「采用推荐 Zoom」在**无事可做**时（自定义等级没有推荐区间，或当前已经是
#: 推荐区间）的悬停说明。按钮置灰而不隐藏：用户需要看到"这里本来有个功能，
#: 只是现在用不上"，而不是怀疑界面缺了什么。
UI_TEXT_RISK_APPLY_DISABLED: str = "当前无需调整 Zoom（已是推荐区间或不套用等级建议）"

# 风险详情对话框（Spec §14）
UI_TEXT_RISK_DETAILS_TITLE: str = "风险详情"
UI_TEXT_RISK_COLUMN_ZOOM: str = "Zoom"
UI_TEXT_RISK_COLUMN_TILES: str = "选中瓦片"
UI_TEXT_RISK_COLUMN_SIZE: str = "预计容量"
UI_TEXT_RISK_COLUMN_RECOMMENDED: str = "推荐"
UI_TEXT_RISK_CELL_RECOMMENDED: str = "推荐"
UI_TEXT_RISK_CELL_OVER: str = "高于推荐"
UI_TEXT_RISK_CELL_NO_RULE: str = "—"
UI_TEXT_RISK_DETAILS_TOTAL_ROW: str = "合计"

#: 计划表格里的「风险」列（Spec §15）。列头与单元格都在这里，免得两处不一致。
UI_TEXT_RISK_COLUMN_LEVEL: str = "风险"
UI_TEXT_RISK_CELL_UNKNOWN: str = "—"

# 单任务与计划任务的确认框（Spec §12 / §16）。正文由报告自己产出
# （``DownloadRiskReport.as_lines`` / ``PlanRiskReport.as_lines``），
# 这里只放"结尾那一问"与标题，免得同一份数字在两处各格式化一次。
UI_TEXT_RISK_CONFIRM_TITLE_LARGE: str = "任务规模确认"
UI_TEXT_RISK_CONFIRM_TITLE_VERY_LARGE: str = "任务规模警告"
UI_TEXT_RISK_CONFIRM_QUESTION: str = "确认开始下载吗？（不会修改任何配置，仅作提醒）"
UI_TEXT_RISK_PLAN_CONFIRM_TITLE: str = "下载计划风险确认"
UI_TEXT_RISK_PLAN_CONFIRM_QUESTION: str = "确认开始执行这份计划吗？"

#: 计划风险确认框的四个去处（Spec §16）。**动作范围写进按钮文案**：
#: 计划里有好几项，"禁用该项"到底指哪一项必须说清，否则用户只能猜。
UI_TEXT_BUTTON_APPLY_RECOMMENDED_ALL: str = "采用推荐 Zoom"
UI_TEXT_BUTTON_DISABLE_DANGEROUS: str = "停用危险项"
UI_TEXT_BUTTON_BACK_TO_EDIT: str = "返回修改"

#: 危险档的强确认（Spec §12）：要用户**手输**确认词
UI_TEXT_DANGEROUS_CONFIRM_TITLE: str = "危险任务强确认"
UI_TEXT_DANGEROUS_CONFIRM_QUESTION: str = (
    "这个规模已经进入「危险」档：可能几天都下不完、把磁盘写满、"
    "并大量消耗天地图调用配额。\n\n"
    "确实要执行的话，请在下面输入 {word}（区分大小写）后点「{button}」。"
)
UI_TEXT_DANGEROUS_CONFIRM_PLACEHOLDER: str = "在这里输入 {word}"
UI_TEXT_BUTTON_CONTINUE_ANYWAY: str = "仍然继续"

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

#: 设备区提示语。Slice 0 的占位文案（"本版本不执行 adb 命令…"）在 Slice 10
#: 已经作废，定义挪到下面 Slice 10 的文案区，**只此一处**——
#: 同名常量定义两次时后一个静默生效，改前一个等于改注释。

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


# --------------------------------------------------------------------------- #
# Slice 10：ADB 设备管理与离线地图包传输（Spec §2 ~ §47）
# --------------------------------------------------------------------------- #
#: 用户配置 ADB 路径用的**环境变量**名（Spec §2）。
#: 解析优先级：用户配置 > 这个环境变量 > :data:`DEFAULT_ADB_EXECUTABLE`。
#: 解析函数是 :func:`app.android.adb_client.resolve_adb_path`（配置项留在这里，
#: 但"怎么选"是行为，不该塞进一个纯常量模块）。
ADB_PATH_ENV_VAR: str = "ADB_PATH"

#: 什么都没配置时的默认值：交给系统 ``PATH`` 去找 ``adb``。
#:
#: **绝不**写死开发机上的 ``C:\Users\<某人>\AppData\Local\Android\Sdk\platform-tools\adb.exe``：
#: 那种路径在别人机器上必然不存在，还会把开发者的目录结构泄露进仓库。
DEFAULT_ADB_EXECUTABLE: str = "adb"

#: 设备端的公共临时导入区（Spec §2 / §14）。
#:
#: 刻意选 ``/sdcard`` 下的公共目录：不碰 ``/data/data``、``/data/user``、
#: ``/system``、``/vendor``，因此**不需要 root、不需要 adb remount**。
#: 最终"从导入区搬进 App 私有目录"是 Android 端（Slice 11）的事。
DEFAULT_ANDROID_IMPORT_DIR: str = "/sdcard/Download/TiandituOfflineMap/import"

#: 传输中的临时后缀（Spec §20）。大文件先落 ``map.mbtiles.part``，传完再
#: ``adb shell mv`` 改成正式名字——这样 Android 端永远不会看到一个"名字齐了、
#: 内容却只写了一半"的包。
TRANSFER_PART_SUFFIX: str = ".part"

# --- 各种 adb 调用的超时（秒）------------------------------------------------
# 分开定义是因为合理量级差得远：``version`` / ``getprop`` 是毫秒级，
# 而 ``push`` 一个 GB 级文件是分钟级。用一个大一统的超时只能二选一：
# 要么把慢操作误杀，要么让快操作卡住界面。
ADB_VERSION_TIMEOUT_SECONDS: float = 10.0
ADB_DEVICES_TIMEOUT_SECONDS: float = 15.0
ADB_SHELL_TIMEOUT_SECONDS: float = 20.0
ADB_FILE_SIZE_TIMEOUT_SECONDS: float = 20.0
ADB_REMOTE_DELETE_TIMEOUT_SECONDS: float = 30.0
#: 远端算 SHA256 要读完整个文件，给得宽一些（大文件 + 慢闪存）
ADB_SHA256_TIMEOUT_SECONDS: float = 300.0
#: 空间检查（``df``）偶尔会因为挂载点被占住而卡住，不值得等太久
ADB_DF_TIMEOUT_SECONDS: float = 20.0

#: 读 ``adb push`` 输出时的轮询间隔（秒）。0.15 约等于 Slice 3 的进度节流，
#: 再密也只是刷更多无意义的百分比。
ADB_PUSH_POLL_INTERVAL_SECONDS: float = 0.15

#: 传输阶段的进度区间（Spec §30）。与 Slice 9 的 ``PACKAGE_BUILD_STAGE_RANGES``
#: 同一个思路：区间在配置里定死，界面与传输层不会各算一套。
#: 大头给 ``mbtiles``（字节全在那儿），其余各占几个点。
TRANSFER_STAGE_RANGES: dict[str, tuple[int, int]] = {
    "check": (0, 6),
    "device": (6, 10),
    "space": (10, 14),
    "mkdir": (14, 18),
    "mbtiles": (18, 88),
    "rename": (88, 92),
    "manifest": (92, 97),
    "verify": (97, 100),
    # ``done`` 是收尾帧：区间收成一点，进度条才会**停在 100** 而不是
    # 停在 ``verify`` 的起点。缺了它 ``_ProgressReporter`` 会退回默认区间
    # ``(0, 100)``，于是最后一帧变成 0% —— 进度条在成功那一刻倒退到底。
    "done": (100, 100),
}

#: 传输阶段的中文文案（Spec §30 / §34 的日志与状态行都用这一套）
TRANSFER_STAGE_TEXT: dict[str, str] = {
    "check": "正在验证本地地图包…",
    "device": "正在检查设备状态…",
    "space": "正在检查设备存储空间…",
    "mkdir": "正在创建远端目录…",
    "mbtiles": "正在传输 map.mbtiles…",
    "rename": "正在整理远端文件…",
    "manifest": "正在传输 manifest.json…",
    "verify": "正在校验远端文件…",
    "done": "传输完成",
}

#: 取消时写进日志的一句（单独一条，因为它是**结果**而不是阶段）
TRANSFER_STAGE_CANCELLED_TEXT: str = "传输已取消"

#: 远端目录名的字符白名单（Spec §15）。``manifest.id`` 要经过它才能拼进
#: Android 路径 —— 只留 ``A-Z a-z 0-9 _ - .``，其余一律换成 ``_``。
#:
#: 注意 ``.`` 是**允许**的（``beijing_20260916`` 里没有，但 ``v1.2`` 里可能有），
#: 而 ``..`` 这种纯点组合会被单独的规则挡掉（见 ``sanitize_remote_id``）。
ANDROID_REMOTE_ID_PATTERN: str = r"[^A-Za-z0-9_.-]"

#: 远端目录名的长度上限（文件系统与 adb 命令行的双重考虑）
ANDROID_REMOTE_ID_MAX_LENGTH: int = 64

#: 规整之后什么都不剩时的兜底名字（不能是空串：空串拼出来是父目录本身）
ANDROID_REMOTE_ID_FALLBACK: str = "package"

#: 远端 SHA256 的命令候选（Spec §26）。按顺序试，第一个能跑出 64 位十六进制
#: 的就是它。Android 上的 ``sha256sum`` 可能是 toybox 提供的，也可能完全没有
#: —— 那是**正常情况**，不是错误。
ADB_SHA256_COMMAND_CANDIDATES: tuple[tuple[str, ...], ...] = (
    ("sha256sum",),
    ("toybox", "sha256sum"),
    ("busybox", "sha256sum"),
)

#: 远端文件大小的命令候选（Spec §25）。``stat -c %s`` 在 toybox 上不一定支持
#: （常见输出是 ``stat: Unknown option``），退到 ``wc -c`` 几乎总能work。
ADB_SIZE_COMMAND_CANDIDATES: tuple[tuple[str, ...], ...] = (
    ("stat", "-c", "%s"),
    ("wc", "-c"),
)

#: ``sha256sum`` 输出里的摘要长度（64 位十六进制）
ADB_SHA256_HEX_LENGTH: int = 64

#: 设备断开判定：``adb push`` 的 stderr 里出现这些片段就认为是设备掉了。
#: 大小写不敏感。宁可多认几种——"传了一半设备没了"必须和"文件写坏了"分开报。
ADB_DISCONNECT_HINTS: tuple[str, ...] = (
    "device offline",
    "device not found",
    "no devices/emulators found",
    "device unauthorized",
    "connection reset by peer",
    "closed",
)

#: 补充设备信息用的 ``getprop`` 属性名（Spec §7）。
#:
#: 只取这四个：厂商 / 型号 / Android 版本 / SDK。其余上百个属性对本工具没有
#: 用途，多问一个就多一次 adb 往返。
ANDROID_PROP_MANUFACTURER: str = "ro.product.manufacturer"
ANDROID_PROP_MODEL: str = "ro.product.model"
ANDROID_PROP_ANDROID_VERSION: str = "ro.build.version.release"
ANDROID_PROP_SDK_VERSION: str = "ro.build.version.sdk"

#: ``adb devices -l`` 的输出解析（Spec §6）。
#: 表头这一行**不是设备**，必须跳过；否则界面上会多出一台叫 "List" 的设备。
ADB_DEVICES_HEADER: str = "List of devices attached"

# --- GUI 文案（Spec §9 ~ §11 / §30 ~ §34）------------------------------------
#: 设备列表的空状态（沿用 Slice 0 起的文案与控件形状，测试也认它）
# UI_TEXT_DEVICE_EMPTY 已在上面定义，此处不重复。

#: 面板上的常规提示：ADB 可用、还没选设备时显示
UI_TEXT_DEVICE_HINT: str = "选择一台已授权的设备，再选一个标准离线地图包后传输。"

#: ADB 不存在（Spec §10）。两句话：为什么、以及怎么办。
UI_TEXT_DEVICE_ADB_MISSING: str = (
    "未检测到 ADB。\n"
    "请安装 Android Platform Tools，或配置 ADB_PATH。"
)

#: 设备未授权（Spec §9）。**不提供**任何"自动绕过"的入口——那是用户必须在
#: 手机上点一下的事，程序替他绕过等于绕过了 Android 的安全模型。
UI_TEXT_DEVICE_UNAUTHORIZED: str = (
    "设备未授权。\n"
    "请检查 Android 设备，并确认「允许 USB 调试」，然后点击 [刷新设备]。"
)

#: 设备离线（Spec §35）
UI_TEXT_DEVICE_OFFLINE: str = "设备已离线。请重新插拔 USB 或重启 adb，然后点击 [刷新设备]。"

#: 设备列表里的一行（Spec §6 解析出来的字段）
UI_TEXT_DEVICE_LINE: str = "{name} — {state}"
UI_TEXT_DEVICE_LINE_UNKNOWN: str = "未知设备"

#: 设备详情区的字段名（Spec §11 的"状态 / 厂商 / 型号 / Android / SDK"）
UI_TEXT_DEVICE_FIELD_ADB: str = "ADB"
UI_TEXT_DEVICE_FIELD_ADB_STATUS: str = "状态"
UI_TEXT_DEVICE_FIELD_STATE: str = "状态"
UI_TEXT_DEVICE_FIELD_MANUFACTURER: str = "厂商"
UI_TEXT_DEVICE_FIELD_MODEL: str = "型号"
UI_TEXT_DEVICE_FIELD_ANDROID: str = "Android"
UI_TEXT_DEVICE_FIELD_SDK: str = "SDK"
UI_TEXT_DEVICE_FIELD_SERIAL: str = "序列号"

#: 面板分组标题（Spec §11）
UI_TEXT_DEVICE_GROUP: str = "Android设备"
UI_TEXT_DEVICE_GROUP_PACKAGE: str = "地图包"
UI_TEXT_DEVICE_GROUP_TRANSFER: str = "传输"

#: 按钮（Spec §11 / §30）
UI_TEXT_DEVICE_BUTTON_REFRESH: str = "刷新设备"
UI_TEXT_DEVICE_BUTTON_IMPORT: str = "传输到设备"
UI_TEXT_DEVICE_BUTTON_CHOOSE_PACKAGE: str = "选择地图包"
UI_TEXT_DEVICE_BUTTON_VALIDATE: str = "验证地图包"
UI_TEXT_DEVICE_BUTTON_CANCEL: str = "取消传输"

#: ADB 状态行的文案
UI_TEXT_DEVICE_ADB_AVAILABLE: str = "可用"
UI_TEXT_DEVICE_ADB_UNAVAILABLE: str = "不可用"
UI_TEXT_DEVICE_ADB_VERSION: str = "ADB：{path}\n版本：{version}\n状态：{status}"

#: 远端目录标签（Spec §11）
UI_TEXT_DEVICE_REMOTE_LABEL: str = "远端目录"
UI_TEXT_DEVICE_PACKAGE_LABEL: str = "地图包"

#: 传输状态（Spec §30）
UI_TEXT_TRANSFER_STATUS_READY: str = "READY"
UI_TEXT_TRANSFER_STATUS_RUNNING: str = "TRANSFERRING"
UI_TEXT_TRANSFER_STATUS_DONE: str = "COMPLETED"
UI_TEXT_TRANSFER_STATUS_FAILED: str = "FAILED"
UI_TEXT_TRANSFER_STATUS_CANCELLED: str = "CANCELLED"

#: 传输进度区的字段名（Spec §30 的"当前文件 / 速度 / 已传输"）
UI_TEXT_TRANSFER_FIELD_STATUS: str = "传输状态"
UI_TEXT_TRANSFER_FIELD_FILE: str = "当前文件"
UI_TEXT_TRANSFER_FIELD_SPEED: str = "速度"
UI_TEXT_TRANSFER_FIELD_BYTES: str = "已传输"
#: 进度条上的文字。``%p%`` 是 **Qt 自己的**占位符（换成 ``{percent}%`` 会原样
#: 显示成 "{percent}%"——实体验收截图里抓到的），所以这一条是全项目唯一不用
#: ``str.format`` 占位符的文案。
UI_TEXT_TRANSFER_PROGRESS_BAR: str = "%p%"
UI_TEXT_TRANSFER_BYTES: str = "{done} / {total}"
UI_TEXT_TRANSFER_SPEED: str = "{speed}/s"
UI_TEXT_TRANSFER_SPEED_UNKNOWN: str = "—"
UI_TEXT_TRANSFER_CURRENT_FILE: str = "map.mbtiles.part"
UI_TEXT_TRANSFER_PLACEHOLDER: str = "—"

#: 传输前确认框（Spec §31）
UI_TEXT_TRANSFER_CONFIRM_TITLE: str = "开始传输"
UI_TEXT_TRANSFER_CONFIRM_BODY: str = (
    "设备：\n{serial}\n{device}\n\n"
    "地图：\n{package}\n\n"
    "大小：\n{size}\n\n"
    "远端：\n{remote}\n\n"
    "是否开始？"
)
UI_TEXT_TRANSFER_CONFIRM_START: str = "开始"
UI_TEXT_TRANSFER_CONFIRM_CANCEL: str = "取消"

#: 远端已存在同名包（Spec §24）。**不允许**静默覆盖。
UI_TEXT_TRANSFER_EXISTS_TITLE: str = "设备中已存在同名地图包"
UI_TEXT_TRANSFER_EXISTS_BODY: str = (
    "设备中已存在同名地图包：\n{package_id}\n\n"
    "远端路径：\n{remote}\n\n"
    "选择「覆盖」会先删除设备上的旧地图包，再重新传输。"
)
UI_TEXT_TRANSFER_BUTTON_OVERWRITE: str = "覆盖"

#: 传输完成（Spec §32 / §33）。SHA256 不可用时状态是 WARNING，不是 FAIL。
UI_TEXT_TRANSFER_COMPLETE_TITLE: str = "地图包传输完成"
UI_TEXT_TRANSFER_COMPLETE_BODY: str = (
    "设备：\n{device}\n\n"
    "远端目录：\n{remote}\n\n"
    "文件：\n{files}\n\n"
    "文件大小：\n{size_status}\n\n"
    "SHA256：\n{checksum_status}\n\n"
    "状态：\n{status}"
)
UI_TEXT_TRANSFER_COMPLETE_FILES: str = "map.mbtiles\nmanifest.json"
UI_TEXT_TRANSFER_STATUS_WAITING_IMPORT: str = "等待 Android App 导入"
#: SHA256 不可用时的措辞（Spec §33）：说清楚"没验证"，但**不**说"失败"
UI_TEXT_TRANSFER_SHA256_UNAVAILABLE: str = "设备不支持远端 SHA256，未验证"

#: 传输失败（Spec §35 / §41）
UI_TEXT_TRANSFER_FAILED_TITLE: str = "地图包传输失败"
UI_TEXT_TRANSFER_FAILED_BODY: str = "原因：\n{reason}"
UI_TEXT_TRANSFER_CANCELLED_TITLE: str = "传输已取消"
UI_TEXT_TRANSFER_CANCELLED_BODY: str = (
    "传输已取消。\n\n"
    "设备上可能残留 map.mbtiles.part，下次传输会自动清理；\n"
    "已经存在的完整地图包不会被删除。"
)

#: 设备面板的状态行与错误（Spec §10 / §11 / §12 / §14）
UI_TEXT_TRANSFER_NEED_DEVICE: str = "请先选择一台状态为「已连接」的设备"
UI_TEXT_TRANSFER_NEED_PACKAGE: str = "请先选择一个地图包目录（内含 map.mbtiles 与 manifest.json）"
UI_TEXT_TRANSFER_PACKAGE_INVALID: str = "地图包校验未通过，已停止传输：{reasons}"
UI_TEXT_TRANSFER_RUNNING: str = "正在传输…"
UI_TEXT_TRANSFER_BUSY: str = "上一次传输尚未结束，已忽略本次请求"
UI_TEXT_TRANSFER_CHOOSE_PACKAGE_TITLE: str = "选择离线地图包目录"
UI_TEXT_TRANSFER_CANCELLING: str = "正在取消传输…"

#: 日志（Spec §34）。逐条对照：ADB detected / Devices refreshed / Selected device /
#: Package validation PASS / Transfer started / Remote directory created /
#: Pushing ... / Remote size verified / Remote SHA256 verified / Transfer completed
LOG_ADB_DETECTED: str = "ADB detected: {path}（{version}）"
LOG_ADB_MISSING: str = "ADB not found（{path}）：{error}"
LOG_DEVICES_REFRESHED: str = "Devices refreshed: {count} 台"
LOG_DEVICE_SELECTED: str = "Selected device {serial}（{name}）"
LOG_PACKAGE_VALIDATED: str = "Package validation PASS: {path}"
LOG_PACKAGE_INVALID: str = "Package validation FAIL: {reasons}"
LOG_TRANSFER_STARTED: str = "Transfer started: {package_id} → {remote}"
LOG_TRANSFER_REMOTE_MKDIR: str = "Remote directory created: {remote}"
LOG_TRANSFER_PUSH: str = "Pushing {file} …"
LOG_TRANSFER_SIZE_OK: str = "Remote size verified: {file}（{size} 字节）"
LOG_TRANSFER_SHA256_OK: str = "Remote SHA256 verified: {file}"
LOG_TRANSFER_SHA256_SKIPPED: str = "Remote SHA256 unavailable; file size verified only."
LOG_TRANSFER_COMPLETED: str = "Transfer completed: {remote}"
LOG_TRANSFER_CANCELLED: str = "Transfer cancelled by user"
LOG_TRANSFER_FAILED: str = "Transfer failed: {reason}"
LOG_TRANSFER_PART_CLEANED: str = "Residual .part removed: {path}"

#: 失败原因文案（Spec §16 ~ §35）
TRANSFER_ERROR_DEVICE_UNKNOWN: str = "设备不存在：{serial}"
TRANSFER_ERROR_DEVICE_NOT_READY: str = "设备当前不可传输（{state}）：{serial}"
TRANSFER_ERROR_DEVICE_UNAUTHORIZED: str = "设备未授权，无法传输：{serial}。请在手机上允许 USB 调试后重试。"
TRANSFER_ERROR_DEVICE_OFFLINE: str = "设备已离线：{serial}"
TRANSFER_ERROR_NO_SPACE: str = "Android设备存储空间不足：需要 {needed}，可用 {available}"
TRANSFER_ERROR_MKDIR: str = "创建远端目录失败：{remote}（{error}）"
TRANSFER_ERROR_PUSH_MBTILES: str = "传输 map.mbtiles 失败：{error}"
TRANSFER_ERROR_PUSH_MANIFEST: str = "传输 manifest.json 失败：{error}"
TRANSFER_ERROR_RENAME: str = "整理远端文件失败：{error}"
TRANSFER_ERROR_SIZE_MISMATCH: str = "远端文件大小不一致：{file} 本地 {expected} 字节，远端 {actual} 字节"
TRANSFER_ERROR_SIZE_UNKNOWN: str = "无法读取远端文件大小：{file}"
TRANSFER_ERROR_SHA256_MISMATCH: str = "远端 SHA256 mismatch：{file} manifest 记录 {expected}，远端 {actual}"
TRANSFER_ERROR_DISCONNECTED: str = "Device disconnected during transfer"
TRANSFER_ERROR_CANCELLED: str = "Transfer cancelled"
TRANSFER_ERROR_REMOTE_DELETE: str = "删除远端旧地图包失败：{remote}（{error}）"
TRANSFER_ERROR_PACKAGE_DIR_MISSING: str = "地图包目录不存在：{path}"
TRANSFER_ERROR_LOCAL_FILE_MISSING: str = "地图包里缺少文件：{path}"
TRANSFER_ERROR_NOT_DIR: str = "地图包路径不是目录：{path}"
TRANSFER_ERROR_REMOTE_ROOT: str = "远端目录不安全，已停止传输：{root}"
TRANSFER_ERROR_LOCAL_SIZE_MISMATCH: str = (
    "本地地图包不完整：{file} manifest 记录 {expected} 字节，实际 {actual} 字节"
)

#: 软警告（Spec §17 / §26）。WARNING 与 FAIL 的区别必须能被用户一眼看出来。
TRANSFER_WARNING_DF_UNPARSABLE: str = "无法确认设备剩余空间，已继续传输"
TRANSFER_WARNING_SHA256_UNAVAILABLE: str = "Remote SHA256 unavailable; file size verified only."
TRANSFER_WARNING_PART_LEFT: str = "设备上残留未完成的临时文件：{path}"
TRANSFER_WARNING_OLD_PART_CLEANED: str = "已清理上次残留的临时文件：{path}"
TRANSFER_WARNING_STORAGE_LOW: str = "设备剩余空间偏紧（可用 {available}，需要 {needed}）"

#: 结果行模板（Spec §32 的完成框与结果对象共用）
TRANSFER_LINE_REMOTE_DIR: str = "远端目录：{path}"
TRANSFER_LINE_MBTILES_SIZE: str = "map.mbtiles：{size}"
TRANSFER_LINE_MANIFEST_SIZE: str = "manifest.json：{size}"

