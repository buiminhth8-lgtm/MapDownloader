"""启动环境检查的单元测试（Slice 15 §8）。

要钉住的是**判定分级**，不是"能不能读磁盘"：

* 哪些情况是 ``BLOCKER``（缺了干不了活）—— 输出目录不可写、Python 版本过低；
* 哪些是 ``OPTIONAL``（缺了一部分功能不可用）—— **ADB**。

第二条是最容易写错、也是 Spec §8 反复强调的一条：**ADB 不存在不该阻止用户
做地图**。它的失败模式很隐蔽 —— 一个把 ADB 标成 BLOCKER 的实现，在自己的开发机
上永远看不出来（那里 ADB 装着的）。

还有一条硬要求：**检查本身不能抛异常**。环境检查失败会让程序起不来，
那是比"某个检查项没跑成"严重得多的事故。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

from app.services.environment_check import (
    CheckStatus,
    EnvironmentCheck,
    EnvironmentReport,
    _check_free_space,
    _check_output_dir,
    _check_python,
    _writable_directory_problem,
    run_environment_checks,
)


# --------------------------------------------------------------------------- #
# 分级语义
# --------------------------------------------------------------------------- #
def test_python_check_passes_on_this_interpreter() -> None:
    """本项目跑得起来，说明这台机器的 Python 至少够用。"""
    check = _check_python()
    assert check.status is CheckStatus.OK
    assert f"{sys.version_info.major}.{sys.version_info.minor}" in check.detail


def test_output_dir_check_passes_for_a_writable_temp_dir() -> None:
    """可写目录必须是 OK —— 否则所有用例都会变成"环境不对"。

    用 ``tempfile.mkdtemp()`` 而不是 pytest 的 ``tmp_path``：本机沙箱把
    ``pytest-of-<user>`` 那个目录拦成 ``PermissionError: [WinError 5]``，
    于是"检查一个可写目录"这个用例反而会因为夹具建不出来而报错。
    """
    with tempfile.TemporaryDirectory(prefix="envcheck_") as raw:
        check = _check_output_dir(Path(raw) / "output")
        assert check.status is CheckStatus.OK


def test_output_dir_check_blocks_when_unwritable() -> None:
    """不可写的路径必须是 BLOCKER。

    用 Windows 上**一定不存在**的盘符：它在两个平台上都能稳定复现
    "建不出目录"，比伪造权限可靠。
    """
    check = _check_output_dir(Path("Z:/definitely/not/here"))
    assert check.status is CheckStatus.BLOCKER
    assert "无法保存" in check.impact


def test_writable_probe_leaves_no_trace() -> None:
    """探测可写性时写的临时文件必须被清掉。

    否则用户的输出目录里会莫名其妙多出一个 ``.write_probe``；
    每次启动都探一次，还会积累多个。
    """
    with tempfile.TemporaryDirectory(prefix="envcheck_") as raw:
        target = Path(raw)
        assert _writable_directory_problem(target) is None
        leftovers = [item.name for item in target.iterdir()]
        assert leftovers == [], f"探测留下了垃圾文件：{leftovers}"


# --------------------------------------------------------------------------- #
# ADB：可选项，不是阻塞项（Spec §8 的核心）
# --------------------------------------------------------------------------- #
def test_adb_missing_is_optional_not_blocker(monkeypatch, work_dir: Path) -> None:
    """ADB 不存在时，其余条件正常的环境仍然能做地图。"""
    from app.config import settings
    # 这里只验证 ADB 可选性；目录仍做真实写入探测，但不依赖用户主目录权限。
    monkeypatch.setattr(settings, "DEFAULT_OUTPUT_DIR", work_dir / "output")
    report = run_environment_checks(adb_path="D:/no/such/adb.exe")

    adb = next(item for item in report.checks if item.name == "ADB")
    assert adb.status is CheckStatus.OPTIONAL, (
        "ADB 缺失被标成了其他等级 —— 这会让没装 ADB 的用户连地图都做不了"
    )
    assert adb.impact, "可选项也要说清影响范围"
    assert "手动" in adb.impact, "应当告诉用户还有别的办法把包传到设备"

    assert not adb.is_blocker
    assert report.can_work, "ADB 缺失不该让 can_work 变成 False"
    assert report.blockers == []
    assert adb in report.optional_problems


def test_adb_present_is_reported_as_ok_or_optional() -> None:
    """本机装了 ADB 时是 OK；没装时是 OPTIONAL —— 两种都不能是 BLOCKER。"""
    report = run_environment_checks()
    adb = next(item for item in report.checks if item.name == "ADB")
    assert adb.status in (CheckStatus.OK, CheckStatus.OPTIONAL)
    assert not adb.is_blocker


# --------------------------------------------------------------------------- #
# 空间：只提醒，不拦截
# --------------------------------------------------------------------------- #
def test_free_space_check_never_blocks() -> None:
    """剩余空间是**提示性**的。

    包多大在用户输入地理范围之前根本不知道，所以固定阈值做不了判断 ——
    它只能在"明显偏紧"时提醒一句。
    """
    with tempfile.TemporaryDirectory(prefix="envcheck_") as raw:
        check = _check_free_space(Path(raw))
    assert check.status in (CheckStatus.OK, CheckStatus.OPTIONAL)
    assert not check.is_blocker


# --------------------------------------------------------------------------- #
# 汇总对象
# --------------------------------------------------------------------------- #
def test_report_summary_reflects_blockers() -> None:
    """带阻塞项时，总结必须直说"未通过"。"""
    report = EnvironmentReport(checks=[
        EnvironmentCheck("Python", CheckStatus.OK, "3.13.14"),
        EnvironmentCheck("输出目录", CheckStatus.BLOCKER, "不可写", "无法保存地图包"),
    ])
    assert not report.can_work
    assert len(report.blockers) == 1
    assert "未通过" in report.summary()


def test_report_summary_mentions_optional_problems() -> None:
    """只有可选项缺失时，总结要说"可以开始"，同时点出哪项不可用。"""
    report = EnvironmentReport(checks=[
        EnvironmentCheck("Python", CheckStatus.OK, "3.13.14"),
        EnvironmentCheck("ADB", CheckStatus.OPTIONAL, "未找到", "无法传输到设备"),
    ])
    assert report.can_work
    assert "可以开始" in report.summary()
    assert "ADB" in report.summary()


def test_report_lines_mark_each_status_distinctly() -> None:
    """三种状态在文本里要能区分开 —— 用户是靠这几行判断该不该往下走的。"""
    report = EnvironmentReport(checks=[
        EnvironmentCheck("A", CheckStatus.OK, "fine"),
        EnvironmentCheck("B", CheckStatus.OPTIONAL, "partial", "某功能不可用"),
        EnvironmentCheck("C", CheckStatus.BLOCKER, "broken", "完全不可用"),
    ])
    lines = report.as_lines()
    assert len(lines) == 3
    assert lines[0].startswith("[✓]")
    assert lines[1].startswith("[!]")
    assert lines[2].startswith("[✗]")
    assert "某功能不可用" in lines[1]
    assert "完全不可用" in lines[2]


# --------------------------------------------------------------------------- #
# 健壮性：检查本身绝不抛异常
# --------------------------------------------------------------------------- #
def test_checks_never_raise_even_when_a_check_explodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单项检查炸了，要降级成一条提示，而不是让启动流程挂掉。"""
    import app.services.environment_check as module

    def explode(*args, **kwargs):
        raise RuntimeError("模拟的检查器内部错误")

    monkeypatch.setattr(module, "_check_python", explode)

    report = run_environment_checks()

    broken = next(item for item in report.checks if item.name == "Python")
    assert broken.status is CheckStatus.OPTIONAL
    assert "检查失败" in broken.detail
    # 其他项照常跑完
    assert len(report.checks) >= 5


def test_report_is_produced_even_for_a_hopeless_environment() -> None:
    """完全跑不动的环境（不存在的输出目录）也要给出完整报告，而不是抛异常。"""
    report = run_environment_checks(output_dir="Z:/nope/nope/nope")

    assert not report.can_work
    assert any(item.name == "输出目录" for item in report.blockers)
    # 其余各项仍然有结论
    assert len(report.checks) == 6
    assert all(item.detail for item in report.checks), "每项都要有说明"
