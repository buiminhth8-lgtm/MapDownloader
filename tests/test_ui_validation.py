"""下载前统一校验（Slice 7，Spec §20）。

为什么这一层值得单独测
----------------------

校验规则是"用户能不能开工"的唯一闸门，而它有三条调用路径（[开始下载]、
[测试连接]、以及将来可能的命令行入口）。如果规则散在各处，迟早会出现
"计算允许、开始下载不允许"这种自相矛盾的界面。

:mod:`app.ui.validation` 刻意不 import Qt：因此这里既不需要 ``QApplication``，
也不需要点任何按钮——每条规则都能被逐条钉住。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.models import MapType
from app.ui.validation import FormValidation, auto_map_name, validate_form


def _valid(**overrides: object) -> dict[str, object]:
    """一份**合法**的表单；用例只覆盖自己关心的那一项。

    ``map_type`` / ``with_labels`` 也放进来，是为了让这份字典与
    :meth:`DownloadPanel.get_form_data` 的真实返回值同形——校验层不该依赖
    "调用方会不会多传键"，但测试数据越接近真实越好。
    """
    data: dict[str, object] = {
        "token": "abcdefghijklmn",
        "map_type": MapType.VECTOR,
        "with_labels": True,
        "west": 116.0,
        "south": 39.0,
        "east": 116.1,
        "north": 39.1,
        "min_zoom": 12,
        "max_zoom": 12,
        "output_dir": Path("D:/maps"),
        "map_name": "beijing",
    }
    data.update(overrides)
    return data


# --------------------------------------------------------------------------- #
# 通过 / 失败的基本形态
# --------------------------------------------------------------------------- #
def test_valid_form_passes() -> None:
    result = validate_form(_valid())
    assert result.ok is True
    assert result.errors == ()
    assert result.message == ""
    assert bool(result) is True


def test_invalid_form_is_falsy() -> None:
    result = validate_form(_valid(token=""))
    assert result.ok is False
    assert bool(result) is False


def test_message_is_numbered_list_with_prefix() -> None:
    """弹窗里的文案：先一句总述，再逐条编号（用户报错时说得清"第几条"）。"""
    result = validate_form(_valid(token="", west=200.0, map_name=""))
    assert result.message.startswith(settings.UI_TEXT_VALIDATION_PREFIX)

    lines = result.message.splitlines()[1:]
    assert len(lines) == len(result.errors)
    for index, line in enumerate(lines, 1):
        assert line.startswith(f"{index}. ")


def test_all_problems_are_reported_at_once() -> None:
    """一次报全部问题，而不是让用户改一个再被拦一次。

    五个独立的问题同时构造出来：Token 空、西>=东、Zoom 顺序反了、
    输出目录为空、地图名称为空。它们分属四个检查函数，正好验证
    ``validate_form`` 真的把四组结果都汇总了。
    """
    result = validate_form(
        _valid(
            token="",
            west=100.0,
            east=10.0,
            min_zoom=17,
            max_zoom=8,
            output_dir="",
            map_name="",
        )
    )
    assert result.errors == (
        settings.UI_TEXT_VALIDATION_TOKEN_REQUIRED,
        settings.UI_TEXT_VALIDATION_WEST_EAST,
        settings.UI_TEXT_VALIDATION_ZOOM_ORDER,
        settings.UI_TEXT_VALIDATION_OUTPUT_DIR_REQUIRED,
        settings.UI_TEXT_MAP_NAME_REQUIRED,
    )


# --------------------------------------------------------------------------- #
# 逐条规则
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("token", ["", "   ", "\t\n"])
def test_token_is_required(token: str) -> None:
    """空白 Token 与空 Token 等价（表单会 strip，校验也要 strip）。"""
    result = validate_form(_valid(token=token))
    assert settings.UI_TEXT_VALIDATION_TOKEN_REQUIRED in result.errors


def test_west_equals_east_is_rejected() -> None:
    """``west == east`` 是零宽度区域，同样要拦（不是只有 > 才拦）。"""
    result = validate_form(_valid(west=116.0, east=116.0))
    assert result.errors == (settings.UI_TEXT_VALIDATION_WEST_EAST,)


def test_south_equals_north_is_rejected() -> None:
    result = validate_form(_valid(south=39.0, north=39.0))
    assert result.errors == (settings.UI_TEXT_VALIDATION_SOUTH_NORTH,)


def test_bounds_order_checks_both_axes() -> None:
    result = validate_form(_valid(west=117.0, east=116.0, south=40.0, north=39.0))
    assert result.errors == (
        settings.UI_TEXT_VALIDATION_WEST_EAST,
        settings.UI_TEXT_VALIDATION_SOUTH_NORTH,
    )


@pytest.mark.parametrize("value", [-181.0, 181.0, 999.0])
def test_longitude_range(value: float) -> None:
    result = validate_form(_valid(west=value))
    assert settings.UI_TEXT_VALIDATION_LONGITUDE_RANGE in result.errors


@pytest.mark.parametrize("value", [-91.0, 91.0])
def test_latitude_range(value: float) -> None:
    result = validate_form(_valid(south=value))
    assert settings.UI_TEXT_VALIDATION_LATITUDE_RANGE in result.errors


def test_range_errors_suppress_order_errors() -> None:
    """范围与顺序同时不合法时**只报范围**。

    ``West=-200, East=-300`` 的用户是"量级填错了"，不是"填反了"；
    这时候提示"西经度必须小于东经度"会把人引到错误的方向上。
    """
    result = validate_form(_valid(west=-200.0, east=-300.0))
    assert result.errors == (settings.UI_TEXT_VALIDATION_LONGITUDE_RANGE,)


def test_zoom_order() -> None:
    result = validate_form(_valid(min_zoom=17, max_zoom=8))
    assert result.errors == (settings.UI_TEXT_VALIDATION_ZOOM_ORDER,)


@pytest.mark.parametrize(
    ("low", "high"),
    [
        (settings.MIN_SUPPORTED_ZOOM - 1, 12),
        (12, settings.MAX_SUPPORTED_ZOOM + 1),
    ],
)
def test_zoom_service_range(low: int, high: int) -> None:
    """超出**天地图服务范围**的级别要在开工前拦下（否则下完才发现半张图是 404）。"""
    result = validate_form(_valid(min_zoom=low, max_zoom=high))
    assert len(result.errors) == 1
    assert result.errors[0] == settings.UI_TEXT_VALIDATION_ZOOM_RANGE.format(
        low=settings.MIN_SUPPORTED_ZOOM, high=settings.MAX_SUPPORTED_ZOOM
    )


def test_service_zoom_range_is_subset_of_tile_math() -> None:
    """两层范围的关系是"服务范围 ⊆ Tile Math 能力"，不能反过来。

    反过来的后果很隐蔽：校验放行了 z19+ 的请求，Tile Math 却算得出坐标，
    于是任务"合法地"失败在每一张瓦片上。这条断言把两个常量绑在一起。
    """
    assert settings.TILE_MATH_MIN_ZOOM <= settings.MIN_SUPPORTED_ZOOM
    assert settings.MAX_SUPPORTED_ZOOM <= settings.TILE_MATH_MAX_ZOOM


def test_output_dir_is_required() -> None:
    result = validate_form(_valid(output_dir=""))
    assert result.errors == (settings.UI_TEXT_VALIDATION_OUTPUT_DIR_REQUIRED,)


def test_output_dir_pointing_at_a_file_is_rejected(work_dir: Path) -> None:
    """输出目录指到一个**已存在的文件**上必须拦。

    "不存在"是允许的（服务层会创建）；"存在但是文件"若不拦，报错会推迟到
    写库那一刻——那时用户已经在等下完了。
    """
    a_file = work_dir / "not_a_dir.txt"
    a_file.write_text("x", encoding="utf-8")

    result = validate_form(_valid(output_dir=a_file))
    assert len(result.errors) == 1
    assert result.errors[0] == settings.UI_TEXT_VALIDATION_OUTPUT_DIR_IS_FILE.format(
        path=a_file
    )


def test_missing_output_dir_is_allowed(work_dir: Path) -> None:
    """目录不存在不算错：服务层会在写入时创建它。"""
    result = validate_form(_valid(output_dir=work_dir / "does-not-exist-yet"))
    assert result.ok is True


def test_map_name_is_required() -> None:
    result = validate_form(_valid(map_name="   "))
    assert result.errors == (settings.UI_TEXT_MAP_NAME_REQUIRED,)


def test_large_tile_count_is_not_a_validation_error() -> None:
    """几十万张瓦片不是**校验**错误（Spec §4：大任务只提示、不限制）。

    校验的边界是"这个任务能不能启动"，不是"这个任务值不值得"。
    地域拉满、zoom 拉满都不该产生任何一条 errors。
    """
    result = validate_form(
        _valid(west=73.0, south=18.0, east=135.0, north=54.0, min_zoom=1, max_zoom=18)
    )
    assert result.ok is True


def test_missing_keys_behave_like_empty_values() -> None:
    """残缺字典不该 ``KeyError``——只多几条"请填写 …"。"""
    result = validate_form({})
    assert result.ok is False
    assert settings.UI_TEXT_VALIDATION_TOKEN_REQUIRED in result.errors
    assert settings.UI_TEXT_VALIDATION_OUTPUT_DIR_REQUIRED in result.errors
    assert settings.UI_TEXT_MAP_NAME_REQUIRED in result.errors


def test_non_numeric_bounds_are_reported() -> None:
    result = validate_form(_valid(west="东经一百度"))
    assert "经纬度必须是数字。" in result.errors


def test_non_integer_zoom_is_reported() -> None:
    result = validate_form(_valid(min_zoom="十二"))
    assert "缩放级别必须是整数。" in result.errors


# --------------------------------------------------------------------------- #
# 自动地图名称（Spec §2.7）
# --------------------------------------------------------------------------- #
def test_auto_map_name_uses_center_coordinates() -> None:
    assert auto_map_name(116.0, 39.0, 116.1, 39.1) == "map_39.05N_116.05E"


def test_auto_map_name_is_stable_for_the_same_region() -> None:
    """同一区域重复生成 → 同一个名字。

    这不是为了好看：名字稳定，"已有文件"的确认框才有意义——否则用户第二次下
    同一块区域会得到一个全新的文件名，旧文件静静躺在旁边，谁也说不清为什么。
    """
    first = auto_map_name(116.0, 39.0, 116.1, 39.1)
    second = auto_map_name(116.0, 39.0, 116.1, 39.1)
    assert first == second


def test_auto_map_name_differs_for_different_regions() -> None:
    assert auto_map_name(116.0, 39.0, 116.1, 39.1) != auto_map_name(
        121.0, 31.0, 121.1, 31.1
    )


def test_auto_map_name_uses_direction_letters() -> None:
    """南半球 / 西半球用 S / W，且**不带负号**（``39.05S`` 比 ``-39.05S`` 好读）。"""
    assert auto_map_name(-75.0, -35.0, -74.0, -34.0) == "map_34.50S_74.50W"


def test_auto_map_name_is_a_legal_filename() -> None:
    """生成的名字必须能安全当文件名用（等于说它不含任何非法字符）。"""
    name = auto_map_name(116.0, 39.0, 116.1, 39.1)
    for illegal in '\\/:*?"<>|':
        assert illegal not in name


def test_auto_map_name_falls_back_on_garbage() -> None:
    """坐标非数值时退回默认名，而不是抛异常——它站在"点了就开始下载"的路径上。"""
    assert auto_map_name("广州", None, object(), 39.0) == settings.DEFAULT_MAP_NAME


# --------------------------------------------------------------------------- #
# FormValidation 本身
# --------------------------------------------------------------------------- #
def test_form_validation_defaults_to_ok() -> None:
    result = FormValidation()
    assert result.ok is True
    assert result.message == ""


def test_form_validation_is_immutable_and_hashable() -> None:
    """``frozen=True``：校验结果是一份"快照"，不该在传递途中被改写。"""
    result = FormValidation(("a", "b"))
    with pytest.raises(Exception):
        result.errors = ()  # type: ignore[misc]
    assert isinstance(hash(result), int)
