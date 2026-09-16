"""文件名规整（Slice 7 起；Slice 9 把它挪到这里）。

这个函数原本住在 :mod:`app.services.map_download_service` 里，Slice 9 的
离线包构建器（``app.package``）也要用它来定包目录名，于是出现了依赖方向问题：

    app.package.package_builder → app.services（拿 sanitize_map_name）
    app.services.map_package_service → app.package.package_builder

两条边一撞就是一个**循环导入**（``app.services.__init__`` 一执行就要 import
包构建器，而包构建器又要回头 import 服务层）。函数本身是纯字符串处理、
不依赖任何服务，所以正确的修法是**把它挪到更低的层**，而不是在函数体里写
延迟 import 把问题盖住。

放在 ``app.models`` 这个叶子包里的理由与 :func:`app.models.describe_map_type`
完全一样：纯函数、零依赖、可脱离一切框架单测。

``app.services`` 仍然原样导出 :func:`sanitize_map_name`（re-export），
所以既有调用方与既有测试一行都不用改。
"""

from __future__ import annotations

import re

from app.config.settings import DEFAULT_MAP_NAME

__all__ = ["sanitize_map_name", "sanitize_filename"]


#: Windows 文件名非法字符（``\\ / : * ? " < > |``）与其余控制字符
_ILLEGAL_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

#: 文件名里不允许出现的保留名（不带扩展名比较）
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

#: 名称的最大长度（避免超出文件系统上限）
_MAX_NAME_LENGTH = 80


def sanitize_map_name(name: str) -> str:
    """把用户输入的地图名称规整成可安全用作文件名/目录名的形式。

    只做"最小必要的清理"，不改变用户看得懂的部分：

    - 去掉首尾空白，替换 Windows 非法字符与控制字符为 ``_``；
    - 去掉结尾的点和空格（Windows 会静默丢弃，导致路径与预期不一致）；
    - 命中 Windows 保留名（``CON`` / ``LPT1`` …）时加后缀；
    - 清完为空 → 回退到 ``DEFAULT_MAP_NAME``。

    Args:
        name: 原始名称。

    Returns:
        可直接当作文件名主体使用的字符串（不含扩展名）。

    >>> sanitize_map_name("  北京 地图  ")
    '北京 地图'
    >>> sanitize_map_name("a/b:c")
    'a_b_c'
    """
    cleaned = _ILLEGAL_NAME_CHARS.sub("_", str(name).strip()).rstrip(". ")
    if not cleaned:
        return DEFAULT_MAP_NAME
    if cleaned.split(".")[0].upper() in _RESERVED_NAMES:
        cleaned = f"{cleaned}_map"
    return cleaned[:_MAX_NAME_LENGTH]


#: 别名：Slice 9 的 Spec 把这一步叫做 ``sanitize_filename()``。
#: 项目里的既有名字是 ``sanitize_map_name``（Slice 7 起的全部调用方与测试都用它），
#: 因此保留原名字、加一个别名，而不是为一个叫法去改十几处调用点。
sanitize_filename = sanitize_map_name
