"""传输的数据模型与远端路径规则（Slice 10，Spec §13 ~ §15）。

本模块只有三样东西，都属于"协议"而不是"流程"：

1. :class:`TransferProgress` / :class:`TransferResult` / :class:`TransferPlan`
   —— 传输层与界面之间的**数据契约**（Spec §13）；
2. :func:`sanitize_remote_id` —— ``manifest.id`` 能不能拼进 Android 路径
   （Spec §15）；
3. :func:`remote_dir_for` —— 最终的远端目录怎么拼（Spec §14）。

为什么路径规则要单独拎出来
--------------------------

远端路径是**唯一一处**把"来自文件的内容"（``manifest.id``）拼进"设备端命令
参数"的地方。两个风险叠在同一行代码上：

- **路径穿越**：``id = "../../abc"`` 会让 ``mkdir -p /sdcard/.../import/../../abc``
  跑到导入区外面去；
- **参数注入**：``id = "-rf"`` 这种以 ``-`` 开头的名字会被设备端的 ``mv`` /
  ``rm`` 当成选项解析，而不是文件名。

因此这里做的是**白名单**（只放行 ``A-Za-z0-9_.-``），而不是"过滤掉危险字符"
——黑名单永远会漏。规则是纯函数，Spec §43 可以直接对它下断言。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.config.settings import (
    ANDROID_REMOTE_ID_FALLBACK,
    ANDROID_REMOTE_ID_MAX_LENGTH,
    ANDROID_REMOTE_ID_PATTERN,
    TRANSFER_PART_SUFFIX,
)
from app.geo import format_size

__all__ = [
    "TransferPlan",
    "TransferProgress",
    "TransferResult",
    "is_safe_remote_root",
    "join_remote",
    "normalize_remote_root",
    "part_path",
    "remote_dir_for",
    "sanitize_remote_id",
]

#: 危险字符（Spec §15 点名的那批，外加换行与反斜杠）。
#: 白名单之外的字符其实都会被换成 ``_``，这里再列一遍是为了让"为什么是这些"
#: 变得可读、可测，而不是只留一个正则。
_FORBIDDEN_IN_REMOTE_ROOT: tuple[str, ...] = (
    "..",
    ";",
    "|",
    "&",
    "$",
    "`",
    "\n",
    "\r",
    "\\",
)

_ID_CLEANER = re.compile(ANDROID_REMOTE_ID_PATTERN)

#: 连续两个及以上的点。``..`` 在路径里永远是**语义**（上一级），不是名字的一部分。
#: 先把它压掉，结果里就不会出现 ``..`` 子串——评审时不必再问"这样安全吗"。
_MULTI_DOT = re.compile(r"\.{2,}")


def sanitize_remote_id(
    value: str, *, fallback: str = ANDROID_REMOTE_ID_FALLBACK
) -> str:
    """把 ``manifest.id`` 规整成可以安全拼进 Android 路径的一段名字（Spec §15）。

    规则（顺序有讲究）:

    1. **连续的点压成 ``_``**：``..`` 是路径语义，压掉之后结果里不会再有 ``..``；
    2. **白名单替换**：``A-Z a-z 0-9 _ - .`` 之外的字符一律变 ``_``
       （于是 ``/`` 与 ``;`` ``|`` 这些根本进不来，路径穿越与命令拼接同时失效）；
    3. 去掉**首尾的点**（``.hidden`` 会被文件管理器藏起来，也没人拿它当包名）；
    4. 去掉**开头的连字符**：``-rf`` 会被设备端命令当成选项解析，而不是文件名
       （``rm -rf -rf`` 的后果不需要演示）；
    5. 截断到 :data:`ANDROID_REMOTE_ID_MAX_LENGTH` 个字符，再收一次首尾的点/横线；
    6. 清完为空 → 用 ``fallback``（空串拼出来是父目录本身，绝不能返回空）。

    >>> sanitize_remote_id("beijing_20260916")
    'beijing_20260916'
    >>> sanitize_remote_id("../../abc")
    '____abc'
    >>> sanitize_remote_id("-rf")
    'rf'
    >>> sanitize_remote_id("v1.2")
    'v1.2'
    """
    text = str(value or "").strip()
    cleaned = _MULTI_DOT.sub("_", text)
    cleaned = _ID_CLEANER.sub("_", cleaned)
    cleaned = cleaned.strip(".")
    cleaned = cleaned.lstrip("-")
    cleaned = cleaned[:ANDROID_REMOTE_ID_MAX_LENGTH]
    # 截断之后可能又露出尾部/开头的点或横线，再收一次（顺序：截断 → 再清理）
    cleaned = cleaned.strip(".-")
    return cleaned or fallback


def normalize_remote_root(root: str) -> str:
    """规范化远端根目录：去掉尾部斜杠，保证以 ``/`` 开头。

    不解析 ``..``（那是 :func:`is_safe_remote_root` 的活）：这里只做"长相"上的
    统一，让 ``/sdcard/x`` 与 ``/sdcard/x/`` 得到同一个结果。
    """
    text = str(root or "").strip().replace("\\", "/")
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/") or "/"


def is_safe_remote_root(root: str) -> bool:
    """远端根目录是否安全（Spec §14 / §15）。

    拒绝项：路径穿越（``..``）、shell 元字符（``;`` ``|`` ``&`` ``$`` ``` ``）、
    反斜杠、换行、空串、以及根目录 ``/`` 本身（那等于把整机当导入区）。
    """
    text = str(root or "").strip()
    if not text or text.strip("/") == "":
        return False
    if not text.startswith("/"):
        return False
    return not any(token in text for token in _FORBIDDEN_IN_REMOTE_ROOT)


def remote_dir_for(root: str, package_id: str) -> str:
    """``<远端根目录>/<规整后的 id>``（Spec §14）。

    ``package_id`` 会先过 :func:`sanitize_remote_id`，因此**这个函数是安全的**：
    即使调用方把 ``"../../abc"`` 递进来，结果也一定落在 ``root`` 之下。
    """
    return join_remote(root, sanitize_remote_id(package_id))


def join_remote(root: str, *parts: str) -> str:
    """把若干段拼成一个远端路径。

    每一段都先去掉首尾斜杠，因此 ``join_remote("/sdcard/x/", "map.mbtiles")``
    与 ``join_remote("/sdcard/x", "/map.mbtiles")`` 得到同一个结果——
    少一种"多一个斜杠"的低级错。
    """
    base = normalize_remote_root(root)
    tails = [str(part).strip("/") for part in parts if str(part or "").strip("/")]
    return f"{base}/{'/'.join(tails)}" if tails else base


def part_path(remote_path: str) -> str:
    """大文件的传输中名字（Spec §20）：``map.mbtiles`` → ``map.mbtiles.part``。"""
    return f"{remote_path}{TRANSFER_PART_SUFFIX}"


@dataclass(frozen=True)
class TransferProgress:
    """一次传输进度的快照（Spec §13）。

    字段刻意与 Spec 一致，另外补了两个"给界面直接用"的派生属性。**不要在传输
    层里格式化文本**：格式化属于展示，属于界面；传输层只报数字与阶段。

    Attributes:
        stage: 阶段名（``check`` / ``space`` / ``mkdir`` / ``mbtiles`` /
            ``rename`` / ``manifest`` / ``verify`` / ``done``）。
        current_file: 当前正在传的文件名；阶段没有具体文件时为 ``None``。
        bytes_transferred: 已传字节（按已完成文件 + 当前文件的进度累计）。
        total_bytes: 本次要传的总字节。
        percent: 整体百分比（0~100）。
        speed_bytes_per_second: 瞬时速度；算不出来时为 ``0.0``。
        message: 可选的附加说明（例如"设备不支持远端 SHA256"）。
    """

    stage: str
    current_file: str | None
    bytes_transferred: int
    total_bytes: int
    percent: float
    speed_bytes_per_second: float
    message: str | None = None

    @property
    def size_text(self) -> str:
        """``1.03 GB / 1.42 GB``（Spec §30 的"已传输"一行）。"""
        return f"{format_size(self.bytes_transferred)} / {format_size(self.total_bytes)}"

    @property
    def speed_text(self) -> str:
        """``23.4 MB/s``；速度未知时给占位符而不是编一个 0。"""
        if self.speed_bytes_per_second <= 0:
            return "—"
        return f"{format_size(int(self.speed_bytes_per_second))}/s"


@dataclass
class TransferPlan:
    """传输前的"预览"（Spec §31 的确认框就靠它填）。

    由服务层在**真正动手之前**算出来：设备是谁、包叫什么、多大、要落到哪。
    用户点"开始"之后，传输层再按同一套规则重算一遍（不把这个对象当作可信输入
    ——它经过了一次用户交互，中间可能什么都变了）。
    """

    serial: str = ""
    device_name: str = ""
    device: object | None = None
    package_dir: str = ""
    package_id: str = ""
    package_name: str = ""
    remote_dir: str = ""
    mbtiles_size: int = 0
    manifest_size: int = 0
    tile_count: int = 0
    #: 不能开始的原因；非 ``None`` 表示计划不可用
    error: str | None = None

    @property
    def total_bytes(self) -> int:
        """本次要传的总字节（两个文件之和）。"""
        return self.mbtiles_size + self.manifest_size

    @property
    def size_text(self) -> str:
        """总大小的可读形式。"""
        return format_size(self.total_bytes)

    @property
    def is_valid(self) -> bool:
        """计划是否可用。"""
        return self.error is None

    @property
    def manifest_path(self) -> str:
        """本地 manifest.json 的完整路径。"""
        return self.package_dir

    @property
    def mbtiles_path(self) -> Path:
        """本地 map.mbtiles 的路径。"""
        return Path(self.package_dir) / "map.mbtiles"


@dataclass
class TransferResult:
    """一次传输的结论（Spec §13）。

    与 :class:`app.package.PackageBuildResult` 同一套约定：**从不抛异常**，
    失败都进 :attr:`errors`；``success=False`` 且 :attr:`errors` 为空是不允许的
    （那意味着有失败路径忘了写原因）。

    :attr:`needs_confirmation` / :attr:`confirmation_reason` 是给 Spec §24 用的
    （设备上已有同名包时先问用户）：它是"等你拍板"，**不是**失败。
    """

    success: bool = False

    serial: str = ""
    local_package_dir: str = ""
    remote_package_dir: str | None = None

    bytes_transferred: int = 0
    duration_seconds: float = 0.0

    #: 远端 SHA256 是否被验证过。设备不支持 ``sha256sum`` 时是 ``False``
    #: ——那是 WARNING 不是 FAIL（Spec §26 / §33）。
    checksum_verified: bool = False
    #: 远端文件大小是否与本地一致（Spec §25）
    size_verified: bool = False

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    #: 用户在设备已有同名包时选了「覆盖」
    overwritten: bool = False
    #: 需要用户先确认（Spec §24）
    needs_confirmation: bool = False
    confirmation_reason: str = ""
    #: 被用户取消（Spec §23）
    cancelled: bool = False

    #: 远端两个文件的字节数（校验用，方便报告与排查）
    remote_mbtiles_size: int | None = None
    remote_manifest_size: int | None = None

    @property
    def duration_text(self) -> str:
        """耗时（``12.3 秒``）。"""
        return f"{self.duration_seconds:.1f} 秒"

    @property
    def status_text(self) -> str:
        """三态结论（Spec §32 / §33）：COMPLETED / WARNING / FAILED / CANCELLED。"""
        if self.cancelled:
            return "CANCELLED"
        if not self.success:
            return "FAILED"
        return "COMPLETED" if self.checksum_verified else "WARNING"

    @property
    def checksum_text(self) -> str:
        """SHA256 那一行的值（Spec §33：不可用要说"未验证"，不能说"失败"）。"""
        return "PASS" if self.checksum_verified else "未验证"

    @property
    def size_status_text(self) -> str:
        """文件大小那一行的值。"""
        return "PASS" if self.size_verified else "FAIL"

    def as_lines(self) -> list[str]:
        """结果的可读行（日志与报告共用）。"""
        lines = [
            f"serial: {self.serial}",
            f"local: {self.local_package_dir}",
            f"remote: {self.remote_package_dir or '—'}",
            f"bytes: {self.bytes_transferred}",
            f"duration: {self.duration_text}",
            f"size_verified: {self.size_verified}",
            f"checksum_verified: {self.checksum_verified}",
            f"status: {self.status_text}",
        ]
        lines.extend(f"error: {item}" for item in self.errors)
        lines.extend(f"warning: {item}" for item in self.warnings)
        return lines
