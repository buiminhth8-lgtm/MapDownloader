"""启动环境检查（Slice 15 §8）。

**目的**：在用户点「开始下载」之前，把"这台机器现在能不能干活"回答清楚。

为什么要在启动时做而不是"出错时再说"：这个工具最坏的用户体验不是报错，
而是**跑了二十分钟才失败**。Token 没填、输出目录不可写、磁盘只剩 200 MB ——
这三件事全都能在启动的半秒内查出来，却全都会在下载/打包的最后一刻才暴露。

设计约束（都来自 Spec §8）：

* **本模块不 import Qt。** 纯 Python，于是它可以在启动早期（QApplication 之前）
  运行，也可以被测试直接调用。返回结构化结论，由界面决定怎么展示。
* **ADB 缺失不是错误。** 它是 :data:`CheckStatus.OPTIONAL`：只影响设备传输，
  地图制作照常。这条必须体现在数据里，而不是靠界面自己判断 ——
  界面里写 `if name == "ADB"` 这种判断迟早会漏。
* **检查本身绝不能抛异常。** 环境检查失败不该让程序起不来。
* **不做需要网络的检查。** 网络状况是另一件事（连通性测试），且它随时会变，
  放在启动阶段判定"能不能干活"会产生大量误报。
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from app.config import settings
from app.geo.tile_math import format_size

__all__ = [
    "CheckStatus",
    "EnvironmentCheck",
    "EnvironmentReport",
    "log_report",
    "run_environment_checks",
]

_logger = logging.getLogger(__name__)

#: 项目要求的 Python 最低版本 ``(major, minor)``。
#: 在这里定义而不是塞进 ``settings``：它是**构建期**的元数据（``pyproject.toml``
#: 里已有一份），运行期的代码只需要一个能用来比较的元组，放两处反而容易走样。
REQUIRED_PYTHON: tuple[int, int] = (3, 11)

#: 启动时低于这个可用空间就给一条提醒（**不拦截**，见 ``_check_free_space``）。
STARTUP_MIN_FREE_SPACE_BYTES: int = 1024 * 1024 * 1024


class CheckStatus(Enum):
    """单项检查的结论。

    刻意只有三档，且与"要不要拦住用户"一一对应：

    * :attr:`OK` —— 没问题。
    * :attr:`OPTIONAL` —— 缺了它一部分功能不可用，但**主流程照常**。
    * :attr:`BLOCKER` —— 缺了它根本干不了活，界面应当显著提示。
    """

    OK = "ok"
    OPTIONAL = "optional"
    BLOCKER = "blocker"


@dataclass(frozen=True)
class EnvironmentCheck:
    """一项检查的结论。"""

    #: 显示名（如 "Python"、"ADB"）
    name: str
    status: CheckStatus
    #: 一句话结论（可直接展示给用户）
    detail: str
    #: 缺了它会影响什么（``OK`` 时为空）
    impact: str = ""

    @property
    def is_blocker(self) -> bool:
        return self.status is CheckStatus.BLOCKER

    @property
    def is_optional_problem(self) -> bool:
        return self.status is CheckStatus.OPTIONAL


@dataclass
class EnvironmentReport:
    """全部检查的汇总。"""

    checks: list[EnvironmentCheck] = field(default_factory=list)

    @property
    def blockers(self) -> list[EnvironmentCheck]:
        """会挡住主流程的项。**空列表 = 可以开工。**"""
        return [check for check in self.checks if check.is_blocker]

    @property
    def optional_problems(self) -> list[EnvironmentCheck]:
        """只影响部分功能的项（目前只有 ADB）。"""
        return [check for check in self.checks if check.is_optional_problem]

    @property
    def can_work(self) -> bool:
        """能不能开始做地图。注意：ADB 没找到**不影响**这个结论。"""
        return not self.blockers

    def as_lines(self) -> list[str]:
        """渲染成多行文本（控制台 / 日志用）。"""
        lines: list[str] = []
        for check in self.checks:
            mark = {
                CheckStatus.OK: "✓",
                CheckStatus.OPTIONAL: "!",
                CheckStatus.BLOCKER: "✗",
            }[check.status]
            line = f"[{mark}] {check.name}：{check.detail}"
            if check.impact:
                line += f"（影响：{check.impact}）"
            lines.append(line)
        return lines

    def summary(self) -> str:
        """一句话总结。"""
        if self.blockers:
            return f"环境检查未通过：{len(self.blockers)} 项必须先解决"
        if self.optional_problems:
            return (f"可以开始制作地图（{len(self.optional_problems)} 项功能不可用："
                    + "、".join(item.name for item in self.optional_problems) + "）")
        return "环境检查全部通过"


# --------------------------------------------------------------------------- #
# 单项检查
# --------------------------------------------------------------------------- #
def _check_python() -> EnvironmentCheck:
    """Python 版本必须满足项目要求。"""
    minimum = REQUIRED_PYTHON
    current = sys.version_info
    text = f"{current.major}.{current.minor}.{current.micro}"

    if (current.major, current.minor) < minimum:
        return EnvironmentCheck(
            name="Python",
            status=CheckStatus.BLOCKER,
            detail=f"版本过低（当前 {text}，需要 "
                   f"{minimum[0]}.{minimum[1]}+）",
            impact="工具无法运行",
        )
    return EnvironmentCheck(name="Python", status=CheckStatus.OK,
                            detail=f"{text}（{sys.executable}）")


def _check_pyside6() -> EnvironmentCheck:
    """Qt 绑定必须可用 —— 没有它连窗口都建不出来。"""
    try:
        from PySide6 import QtCore  # noqa: F401
    except Exception as exc:                                   # noqa: BLE001
        return EnvironmentCheck(
            name="PySide6",
            status=CheckStatus.BLOCKER,
            detail=f"无法导入（{type(exc).__name__}: {exc}）",
            impact="界面无法启动",
        )

    version = getattr(QtCore, "__version__", "未知")
    return EnvironmentCheck(name="PySide6", status=CheckStatus.OK,
                            detail=f"Qt {version}")


def _writable_directory_problem(path: Path) -> str | None:
    """返回不可写的原因；可写时返回 ``None``。

    真的**写一个文件试试**，而不是看权限位：Windows 上只读属性、
    被占用的目录、配额限制都不会体现在权限位里。
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"无法创建目录（{exc.strerror or exc}）"

    probe = path / ".write_probe"
    try:
        probe.write_bytes(b"probe")
        probe.unlink()
    except OSError as exc:
        return f"目录不可写（{exc.strerror or exc}）"
    except Exception as exc:                                   # noqa: BLE001
        return f"目录不可写（{type(exc).__name__}: {exc}）"
    finally:
        # 就算写失败也要试着清理，别在自己的输出目录里留垃圾
        with contextlib.suppress(OSError):
            probe.unlink(missing_ok=True)
    return None


def _check_output_dir(path: Path | None = None) -> EnvironmentCheck:
    """输出目录必须能创建、能写。"""
    target = Path(path) if path is not None else settings.DEFAULT_OUTPUT_DIR
    problem = _writable_directory_problem(target)

    if problem is not None:
        return EnvironmentCheck(
            name="输出目录",
            status=CheckStatus.BLOCKER,
            detail=f"{target} —— {problem}",
            impact="无法保存下载的瓦片与地图包",
        )
    return EnvironmentCheck(name="输出目录", status=CheckStatus.OK,
                            detail=str(target))


def _check_free_space(path: Path | None = None) -> EnvironmentCheck:
    """剩余空间。**只报告，不拦截** —— 包多大在用户输入范围之前是不知道的。"""
    target = Path(path) if path is not None else settings.DEFAULT_OUTPUT_DIR
    # 目录可能还不存在，往上找到第一个存在的祖先
    probe = target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent

    try:
        usage = shutil.disk_usage(probe)
    except Exception as exc:                                   # noqa: BLE001
        return EnvironmentCheck(
            name="剩余空间",
            status=CheckStatus.OPTIONAL,
            detail=f"无法确认（{type(exc).__name__}: {exc}）",
            impact="无法预估能否装下地图包",
        )

    free_text = format_size(usage.free)
    # 阈值取"低于 1 GB 就提醒"：本工具产出的包从几 MB 到几 GB 都有，
    # 用一个固定阈值只能做**提醒**，做不了判断。真要判断得等用户输入范围。
    if usage.free < STARTUP_MIN_FREE_SPACE_BYTES:
        return EnvironmentCheck(
            name="剩余空间",
            status=CheckStatus.OPTIONAL,
            detail=f"{free_text} 可用（{probe}）",
            impact="空间偏紧，大地图可能放不下",
        )
    return EnvironmentCheck(name="剩余空间", status=CheckStatus.OK,
                            detail=f"{free_text} 可用（{probe}）")


def _check_adb(adb_path: str | None = None) -> EnvironmentCheck:
    """ADB 是可选的。

    **这条检查的全部意义在于"不影响主流程"。** 一个没装 ADB 的用户照样能做出
    地图包（用 U 盘 / 网盘传到手机），把它标成 BLOCKER 会让工具在最开始就
    拒绝服务 —— 那是错的。

    注意这里必须调 ``AdbClient.is_available()`` 而不是只看
    ``resolve_adb_path()`` 的返回值：那个函数**刻意不校验存在性**
    （见它自己的文档），拿它的返回值当"ADB 可用"会让"没装 adb"显示成一个大大的
    ✓ —— 比不做检查更误导人。
    """
    unavailable = EnvironmentCheck(
        name="ADB",
        status=CheckStatus.OPTIONAL,
        detail="未找到 adb",
        impact="无法从工具内直接传输到设备，仍可手动复制地图包",
    )
    try:
        from app.android.adb_client import AdbClient

        client = AdbClient(adb_path=adb_path) if adb_path else AdbClient()
        if not client.is_available():
            return EnvironmentCheck(
                name="ADB",
                status=CheckStatus.OPTIONAL,
                detail=f"不可用（{client.adb_path}）",
                impact=unavailable.impact,
            )
        version = (client.version_text() or "").strip()
    except Exception as exc:                                   # noqa: BLE001
        _logger.debug("检查 ADB 失败：%s", exc)
        return unavailable

    return EnvironmentCheck(
        name="ADB",
        status=CheckStatus.OK,
        detail=f"{client.adb_path}" + (f" · {version}" if version else ""),
    )


def _check_device_storage_path() -> EnvironmentCheck:
    """``~/MapDownloader`` 能不能建出来。

    这个目录同时放输出与任务记录。它建不出来时，用户会看到"任务列表空了"
    之类莫名其妙的现象 —— 与其那样，不如启动时说清楚。
    """
    base = settings.DEFAULT_OUTPUT_DIR.parent
    problem = _writable_directory_problem(base)

    if problem is not None:
        return EnvironmentCheck(
            name="工作目录",
            status=CheckStatus.BLOCKER,
            detail=f"{base} —— {problem}",
            impact="任务记录与地图包都无法保存",
        )
    return EnvironmentCheck(name="工作目录", status=CheckStatus.OK, detail=str(base))


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def run_environment_checks(
    output_dir: str | Path | None = None,
    adb_path: str | None = None,
) -> EnvironmentReport:
    """跑一遍全部环境检查。

    **本函数不抛异常**：任何一项检查自己崩了都会被降级成一条
    ``OPTIONAL`` 记录，而不是让启动流程挂掉。

    Args:
        output_dir: 覆盖默认输出目录（界面里用户改过时传进来）。
        adb_path: 覆盖 ADB 路径（用户在偏好设置里指定过时传进来）。

    Returns:
        :class:`EnvironmentReport`
    """
    report = EnvironmentReport()

    checks = (
        ("Python", _check_python, ()),
        ("PySide6", _check_pyside6, ()),
        ("工作目录", _check_device_storage_path, ()),
        ("输出目录", _check_output_dir, (output_dir,)),
        ("剩余空间", _check_free_space, (output_dir,)),
        ("ADB", _check_adb, (adb_path,)),
    )

    for name, func, args in checks:
        try:
            report.checks.append(func(*args))
        except Exception as exc:                               # noqa: BLE001
            # 一项检查自己炸了，不该让整个启动流程跟着炸。
            _logger.warning("环境检查「%s」异常：%s", name, exc, exc_info=True)
            report.checks.append(EnvironmentCheck(
                name=name,
                status=CheckStatus.OPTIONAL,
                detail=f"检查失败（{type(exc).__name__}: {exc}）",
                impact="该项状态未知",
            ))

    return report


def log_report(report: EnvironmentReport, logger: logging.Logger | None = None) -> None:
    """把结论写进日志（启动时调用）。"""
    target = logger or _logger
    for line in report.as_lines():
        target.info("环境检查 %s", line)
    target.info("环境检查结论：%s", report.summary())
