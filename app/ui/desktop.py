"""与操作系统桌面环境的少量交互（Slice 7，Spec §16）。

目前只有一件事：**打开目录**。单独成模块是因为它是本项目唯一一处
"调用外部程序"的代码——集中在一个文件里，就能一眼看全它做了什么、
以及失败时会怎样。

原则：**永不抛异常**。用户点「打开目录」的期望是"文件管理器弹出来"，
不是"程序报错"。打不开的原因（没有文件管理器、路径已不存在、权限不足）
统一变成 ``False`` + 一句人话，由调用方决定怎么提示。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

__all__ = ["open_directory"]

_logger = logging.getLogger(__name__)


def open_directory(path: str | Path) -> tuple[bool, str]:
    """用系统的文件管理器打开一个目录。

    Args:
        path: 目录路径。传文件路径时打开它所在的目录——调用方常常只有
            输出文件的完整路径，让它自己 ``.parent`` 一次没必要。

    Returns:
        ``(是否成功, 失败原因)``。成功时原因为空串。
        失败时原因是可以直接展示给用户的一句话。
    """
    target = Path(path)
    if not str(path).strip():
        return False, "路径为空"

    if target.is_file():
        target = target.parent

    # 目录不存在是**预期内**的情况（用户可能已经把文件删了/挪了）。
    # 这里不报"打开失败"而是给它一个准确的原因，因为两者要做的操作不同。
    if not target.exists():
        return False, f"目录不存在：{target}"

    try:
        if sys.platform == "win32":
            # os.startfile 是 Windows 上唯一不需要 shell 的做法：
            # 它把路径交给 ShellExecute，不带命令行解析，因此路径里的空格、
            # 中文、& 等字符都不会变成参数分隔符。
            os.startfile(str(target))  # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":  # pragma: no cover - 非主目标平台
            subprocess.Popen(["open", str(target)])  # noqa: S603,S607
        else:  # pragma: no cover - 非主目标平台
            subprocess.Popen(["xdg-open", str(target)])  # noqa: S603,S607
    except Exception as exc:  # noqa: BLE001 - 任何失败都不该让 GUI 崩
        _logger.warning("打开目录失败 %s：%s", target, exc)
        return False, str(exc)

    return True, ""
