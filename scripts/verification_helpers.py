"""验收脚本的异步界面等待；不依赖机器速度或空转固定次数。"""
from __future__ import annotations

import subprocess
import time


def wait_for_region_estimate(app, window, timeout=60.0):
    deadline = time.monotonic() + timeout
    while window._estimate_worker is not None:
        app.processEvents()
        if time.monotonic() >= deadline:
            window._cancel_region_estimate()
            raise TimeoutError("区域后台估算未在验收期限内完成")
        time.sleep(0.005)
    app.processEvents()


def tracked_region_boundaries(root):
    """检查 Git 分发内容，不把用户本机已导入的授权数据误当作内置资源。"""
    completed = subprocess.run(
        ["git", "ls-files", "--", "resources/regions"], cwd=root,
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return [name for name in completed.stdout.splitlines() if name.endswith(".geojson")]
