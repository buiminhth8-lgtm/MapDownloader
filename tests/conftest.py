"""pytest 共享夹具。

关键点：必须在导入 PySide6 之前把 ``QT_QPA_PLATFORM`` 设为 ``offscreen``，
这样 CI / 无显示环境下也能装配 QWidget。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.main import silence_noisy_http_loggers  # noqa: E402
from app.ui.preferences import UiPreferences  # noqa: E402

#: 与生产启动保持一致：把 httpx / httpcore 压到 WARNING。
#:
#: 不只是"少点噪音"——httpx 的 INFO 日志会把**带 ``tk=`` 的完整 URL** 打出来
#: （Slice 2 的硬性禁止项）。生产里 ``app.main`` 会关掉它，测试里如果不管，
#: 任何一处 ``caplog`` / 失败报告都可能把假 Token 抄进产物里。
silence_noisy_http_loggers()


@pytest.fixture(scope="session")
def qapp():
    """整个测试会话共用一个 QApplication 实例。"""
    app = QApplication.instance()
    created = app is None
    if created:
        app = QApplication([])
    yield app
    if created:
        app.quit()


@pytest.fixture
def work_dir() -> Path:
    """每个用例一个干净的可写临时目录。

    刻意**不用** pytest 自带的 ``tmp_path``：本机的 ``pytest-of-<user>`` 根目录
    会被沙箱拦在 ``PermissionError: [WinError 5]`` 上，导致所有用到它的用例直接
    ERROR。``tempfile.mkdtemp()`` 走的是另一条路径，可以正常创建。

    Yields:
        临时目录路径；用例结束后尽力清理。
    """
    path = Path(tempfile.mkdtemp(prefix="mbtiles-test-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def tasks_file() -> Path:
    """任务记录文件（Slice 5 ``tasks.json``）的独立临时路径。

    刻意**不**放进 ``work_dir``：好多个用例会断言 ``work_dir`` 里"只有那个
    .mbtiles"，任务记录要是也写进去就会把那些断言搅黄。

    Yields:
        形如 ``<临时目录>/tasks.json`` 的路径。
    """
    directory = Path(tempfile.mkdtemp(prefix="tasks-test-"))
    try:
        yield directory / "tasks.json"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def preferences():
    """界面偏好（Slice 7）的**隔离**实例：写到临时 ini，而不是注册表。

    这不是"测起来方便"的问题。生产里 ``UiPreferences()`` 会落到
    ``HKEY_CURRENT_USER\\Software\\MapDownloader\\<APP_NAME>``，而主窗口在
    ``closeEvent`` 里会 ``save_geometry`` + ``set_output_dir``。测试只要用默认
    实例，一个用例点过「开始下载」，下一个用例装配窗口时就会**恢复**上一个
    用例的形状与地图类型：窗口变成 900 宽、下拉框变成"影像"、Zoom 变成 12——
    于是断言默认值的用例开始成片地红，而且失败信息会指向"业务逻辑坏了"，
    完全看不出真凶是注册表。

    落点改到临时 ini 之后，每个用例都是一张白纸，注册表也不再被污染。

    Yields:
        ``UiPreferences`` 实例，底层是临时目录里的 IniFormat ``QSettings``。
    """
    from PySide6.QtCore import QSettings  # noqa: PLC0415 - 与 qapp 同层延迟导入

    directory = Path(tempfile.mkdtemp(prefix="prefs-test-"))
    settings = QSettings(str(directory / "ui.ini"), QSettings.Format.IniFormat)
    try:
        yield UiPreferences(settings)
    finally:
        settings.clear()
        shutil.rmtree(directory, ignore_errors=True)
