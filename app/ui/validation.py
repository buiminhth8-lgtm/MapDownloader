"""下载前统一校验（Slice 7，Spec §20）。

**一个入口、一份规则**。散落各处的 ``if not token: ...`` 会随时间漂移：
「计算」允许的行为、「测试连接」允许的行为、「开始下载」允许的行为迟早不一致，
而用户看到的界面只有一个。因此所有前置检查都在 :func:`validate_form` 里，
UI 只负责把返回的消息显示出来。

设计上刻意的几点
----------------

- **纯逻辑，不弹窗**：本模块返回一组消息，不 import Qt、不创建任何控件。
  于是"哪条规则在什么输入下触发"可以被逐条单测，而不用真的去点界面。
- **一次报全部问题**，而不是遇到第一个就返回：用户改完一个再被拦一次，
  是最容易让人关掉程序的交互。
- **只判合法性，不判合理性**：几十万张瓦片不在这里拦（Spec §4 明确说大任务
  只提示、不限制）。校验的边界是"这个任务能不能启动"，不是"这个任务值不值得"。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config.settings import (
    DEFAULT_MAP_NAME,
    MAX_LONGITUDE,
    MAX_SUPPORTED_ZOOM,
    MIN_LONGITUDE,
    MIN_SUPPORTED_ZOOM,
    TILE_MATH_MAX_ZOOM,
    TILE_MATH_MIN_ZOOM,
    UI_TEXT_MAP_NAME_REQUIRED,
    UI_TEXT_VALIDATION_LATITUDE_RANGE,
    UI_TEXT_VALIDATION_LONGITUDE_RANGE,
    UI_TEXT_VALIDATION_OUTPUT_DIR_IS_FILE,
    UI_TEXT_VALIDATION_OUTPUT_DIR_REQUIRED,
    UI_TEXT_VALIDATION_PREFIX,
    UI_TEXT_VALIDATION_SOUTH_NORTH,
    UI_TEXT_VALIDATION_TOKEN_REQUIRED,
    UI_TEXT_VALIDATION_WEST_EAST,
    UI_TEXT_VALIDATION_ZOOM_ORDER,
    UI_TEXT_VALIDATION_ZOOM_RANGE,
)

__all__ = [
    "FormValidation",
    "auto_map_name",
    "validate_form",
]

#: 纬度合法范围。经度用的是 ``settings`` 里的 ``MIN_LONGITUDE`` / ``MAX_LONGITUDE``
#: （那里已经有一份，不重复定义），纬度此前没有别的用处，因此就地定义。
MIN_LATITUDE: float = -90.0
MAX_LATITUDE: float = 90.0


@dataclass(frozen=True)
class FormValidation:
    """一次表单校验的结果。

    Attributes:
        errors: 全部问题的人话描述（空元组表示通过）。
    """

    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """是否通过校验。"""
        return not self.errors

    def __bool__(self) -> bool:
        """``if validation:`` 等价于 :attr:`ok`，让调用点读起来更短。"""
        return self.ok

    @property
    def message(self) -> str:
        """一段可以直接丢进弹窗的文本（带前缀 + 逐条编号）。

        逐条编号而不是项目符号：用户报错时说得清"是第 3 条"。
        """
        if self.ok:
            return ""
        lines = [UI_TEXT_VALIDATION_PREFIX]
        lines.extend(f"{index}. {error}" for index, error in enumerate(self.errors, 1))
        return "\n".join(lines)


def auto_map_name(west: float, south: float, east: float, north: float) -> str:
    """由范围自动生成一个地图名称（Spec §2.7）。

    用**中心点坐标**而不是时间戳或随机数：同一个区域重复下载会得到同一个
    文件名（于是"已有文件"的确认框能正常发挥作用），而不同区域不会撞名。

    Args:
        west: 西边界经度。
        south: 南边界纬度。
        east: 东边界经度。
        north: 北边界纬度。

    Returns:
        形如 ``map_39.05N_116.05E`` 的名称；坐标非数值时退回 ``offline_map``
        （由 :func:`app.services.sanitize_map_name` 的兜底值决定，
        这里不重复那条规则，只保证不抛异常）。
    """
    try:
        center_lat = (float(south) + float(north)) / 2.0
        center_lon = (float(west) + float(east)) / 2.0
    except (TypeError, ValueError):  # pragma: no cover - 防御性分支
        return DEFAULT_MAP_NAME

    ns = "N" if center_lat >= 0 else "S"
    ew = "E" if center_lon >= 0 else "W"
    # 取绝对值再拼方向字母：``-39.05S`` 这种"负号 + 方向"的双重表达很别扭，
    # 而 ``39.05S`` 读起来与坐标惯例一致。
    return f"map_{abs(center_lat):.2f}{ns}_{abs(center_lon):.2f}{ew}"


def validate_form(data: Mapping[str, Any]) -> FormValidation:
    """校验一份下载表单（Spec §20）。

    Args:
        data: :meth:`app.ui.download_panel.DownloadPanel.get_form_data` 的返回值。
            缺失的键按"空值"处理，因此传一个残缺的字典不会抛异常，
            只会多一条"请填写 …"——这比 ``KeyError`` 对调用方更友好。

    Returns:
        :class:`FormValidation`；``errors`` 为空表示可以开始下载。
    """
    errors: list[str] = []

    token = str(data.get("token") or "").strip()
    if not token:
        errors.append(UI_TEXT_VALIDATION_TOKEN_REQUIRED)

    errors.extend(_check_bounds(data))
    errors.extend(_check_zoom(data))
    errors.extend(_check_output_dir(data))
    errors.extend(_check_map_name(data))

    return FormValidation(tuple(errors))


# --------------------------------------------------------------------------- #
# 逐项规则（拆成小函数：每条规则单独可测，也便于将来单独调整措辞）
# --------------------------------------------------------------------------- #
def _check_bounds(data: Mapping[str, Any]) -> list[str]:
    """经纬度范围与顺序。

    顺序与范围分开报：``West=120, East=116`` 是"填反了"，
    ``West=-200`` 是"填错了量级"，用户要做的修改完全不同。
    """
    try:
        west = float(data.get("west"))  # type: ignore[arg-type]
        south = float(data.get("south"))  # type: ignore[arg-type]
        east = float(data.get("east"))  # type: ignore[arg-type]
        north = float(data.get("north"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ["经纬度必须是数字。"]

    errors: list[str] = []
    if not (MIN_LONGITUDE <= west <= MAX_LONGITUDE and MIN_LONGITUDE <= east <= MAX_LONGITUDE):
        errors.append(UI_TEXT_VALIDATION_LONGITUDE_RANGE)
    if not (
        MIN_LATITUDE <= south <= MAX_LATITUDE
        and MIN_LATITUDE <= north <= MAX_LATITUDE
    ):
        errors.append(UI_TEXT_VALIDATION_LATITUDE_RANGE)
    # 顺序检查放在范围检查之后：两条都错时报"填反了"更贴近用户的实际问题，
    # 说"经度超出范围"会把人引到错误的方向上。
    if not errors:
        if west >= east:
            errors.append(UI_TEXT_VALIDATION_WEST_EAST)
        if south >= north:
            errors.append(UI_TEXT_VALIDATION_SOUTH_NORTH)
    return errors


def _check_zoom(data: Mapping[str, Any]) -> list[str]:
    """缩放区间：先看能不能解析，再看顺序，最后看是否在服务范围内。"""
    try:
        min_zoom = int(data.get("min_zoom"))  # type: ignore[arg-type]
        max_zoom = int(data.get("max_zoom"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ["缩放级别必须是整数。"]

    if min_zoom > max_zoom:
        return [UI_TEXT_VALIDATION_ZOOM_ORDER]

    # Tile Math 的能力比天地图服务范围更宽（z0~z22 vs z1~z18）。这里卡的是
    # **服务范围**：超出它的级别天地图必然返回错误，与其让用户下完发现
    # 半张图是 404，不如在开工前说清楚。两层范围的关系由
    # ``test_ui_validation`` 里的断言锁住（服务范围必须是 Tile Math 的子集）。
    if not (
        MIN_SUPPORTED_ZOOM <= min_zoom <= MAX_SUPPORTED_ZOOM
        and MIN_SUPPORTED_ZOOM <= max_zoom <= MAX_SUPPORTED_ZOOM
    ):
        return [
            UI_TEXT_VALIDATION_ZOOM_RANGE.format(
                low=MIN_SUPPORTED_ZOOM, high=MAX_SUPPORTED_ZOOM
            )
        ]

    # 防御性：万一将来两个常量被改到互相矛盾，这里要能报出来而不是静默通过。
    if not (TILE_MATH_MIN_ZOOM <= min_zoom and max_zoom <= TILE_MATH_MAX_ZOOM):
        return [
            UI_TEXT_VALIDATION_ZOOM_RANGE.format(
                low=TILE_MATH_MIN_ZOOM, high=TILE_MATH_MAX_ZOOM
            )
        ]
    return []


def _check_output_dir(data: Mapping[str, Any]) -> list[str]:
    """输出目录：必须填，且不能是一个已存在的文件。

    "不存在"是允许的（服务层会创建）；"存在但是个文件"必须拦住——
    否则报错会推迟到写库那一刻，而那时用户已经在等下载了。
    """
    raw = data.get("output_dir")
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return [UI_TEXT_VALIDATION_OUTPUT_DIR_REQUIRED]

    path = Path(text)
    try:
        is_file = path.is_file()
    except OSError:  # pragma: no cover - 路径过长 / 权限异常
        return [UI_TEXT_VALIDATION_OUTPUT_DIR_IS_FILE.format(path=path)]
    if is_file:
        return [UI_TEXT_VALIDATION_OUTPUT_DIR_IS_FILE.format(path=path)]
    return []


def _check_map_name(data: Mapping[str, Any]) -> list[str]:
    """地图名称不能为空。

    UI 会在校验**之前**调用 :func:`auto_map_name` 自动补一个（Spec §2.7），
    所以正常操作下这条不会触发。保留它是因为校验层不该依赖"调用方一定先补过"
    ——一个能被绕过的校验等于没有校验。
    """
    if str(data.get("map_name") or "").strip():
        return []
    return [UI_TEXT_MAP_NAME_REQUIRED]
