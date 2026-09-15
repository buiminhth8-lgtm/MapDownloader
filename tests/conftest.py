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
