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
from collections.abc import Sequence

from app.config.settings import DEFAULT_MAP_NAME

__all__ = [
    "CHINA_NATIONWIDE_MAP_NAME",
    "NATIONWIDE_BASE_SUFFIX",
    "nationwide_map_name",
    "region_map_name",
    "sanitize_filename",
    "sanitize_map_name",
]


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


# --------------------------------------------------------------------------- #
# 按行政区域自动命名（Slice 43）
# --------------------------------------------------------------------------- #
#: 全国级下载的固定名字。用户点的是"中国 + 全部省份"，产出的却叫
#: ``中国_矢量.mbtiles`` 会让人以为这是个国家名录之类的东西；"全国基础包"才是
#: 那个包的用途（一张全国底图，作为其他包的底座）。
CHINA_NATIONWIDE_MAP_NAME: str = "全国基础包"

#: 非中国国家的全国级名字后缀（``示例国`` → ``示例国基础包``）。
#: 不套用"全国基础包"：那个词是给中国的，别的国家叫什么就写什么。
NATIONWIDE_BASE_SUFFIX: str = "基础包"


def region_map_name(names: Sequence[str]) -> str:
    """把"省 / 市 / 县"这条名称链拼成地图名称的主体。

    规则（与界面上的级联选择一一对应）::

        中国 + 全部省份            → 全国基础包          （不经过本函数）
        中国 + 江苏省 + 全部        → 江苏省
        中国 + 江苏省 + 苏州市      → 江苏省_苏州市
        中国 + 江苏省 + 苏州市 + 昆山市 → 江苏省_苏州市_昆山市

    也就是说**国家级之后的所有层级都保留**，用 ``_`` 连接。不按等级写死
    "市级拼两段"这类规则，是因为直辖市与省直辖县根本不遵循那条：
    ``北京市 + 东城区`` 只有两段，``江苏省 + 昆山市``（省直辖县级市）同样只有两段，
    写死段数会让这两类区域凭空缺一级或多出一级。按"实际选中的这条链"拼，
    两种特殊情况自动就是对的。

    分隔符用 ``_`` 而不是空格：文件名里空格在命令行、``adb push``、脚本里都要
    额外引号，而 ``江苏省_苏州市`` 已经足够可读。

    Args:
        names: 国家**之后**的名称链，从最上级到当前区域（例如
            ``["江苏省", "苏州市"]``）。空序列在调用方处理（那代表国家级）。

    Returns:
        拼接后的名称主体；空项会被跳过，因此不会出现 ``江苏省__苏州市``。

    >>> region_map_name(["江苏省"])
    '江苏省'
    >>> region_map_name(["江苏省", "苏州市"])
    '江苏省_苏州市'
    >>> region_map_name(["北京市", "东城区"])
    '北京市_东城区'
    """
    return "_".join(part for part in (str(name).strip() for name in names) if part)


def nationwide_map_name(country_name: str, *, is_china: bool = False) -> str:
    """全国级下载的名称主体。

    Args:
        country_name: 国家名（国家节点的 ``name``）。
        is_china: 这个国家是不是中国（由调用方按 ``region_id`` 判定，不按名字
            猜 —— 名字是可以被数据包改的）。

    Returns:
        中国 → :data:`CHINA_NATIONWIDE_MAP_NAME`；其余国家 → ``<国名>基础包``。

    >>> nationwide_map_name("中国", is_china=True)
    '全国基础包'
    >>> nationwide_map_name("示例国")
    '示例国基础包'
    """
    if is_china:
        return CHINA_NATIONWIDE_MAP_NAME
    return f"{str(country_name).strip()}{NATIONWIDE_BASE_SUFFIX}"
