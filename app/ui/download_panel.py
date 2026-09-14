"""下载配置面板。

只负责"把用户输入收集成结构化数据"（``get_form_data``）以及把用户的动作
以信号形式抛出去。真正的计算、下载、打包都在 ``app.geo`` /
``app.downloader`` / ``app.package`` 中实现，本文件不写任何业务逻辑。

Slice 0 范围：UI 与数据读取；按钮仅发信号，由 MainWindow 决定如何响应。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import (
    DEFAULT_MAX_ZOOM,
    DEFAULT_MIN_ZOOM,
    DEFAULT_OUTPUT_DIR,
    MAX_SUPPORTED_ZOOM,
    MIN_SUPPORTED_ZOOM,
)
from app.models.map_layer import LAYER_DISPLAY_NAMES, MapLayerType

#: 地图类型下拉框只放"底图"，中文注记由独立复选框控制
_BASE_LAYER_CHOICES: tuple[MapLayerType, ...] = (
    MapLayerType.VECTOR,
    MapLayerType.IMAGE,
)

#: 动作标识，与 MainWindow 的响应逻辑一一对应
ACTION_CALCULATE = "calculate"
ACTION_START = "start"
ACTION_PAUSE = "pause"
ACTION_RESUME = "resume"
ACTION_TEST_CONNECTION = "test_connection"

#: 经纬度输入框的最小宽度。
#: QDoubleSpinBox 的 ``minimumSizeHint`` 会按最大位数（``-180.000000``，11 字符）
#: 加上调节箭头，保守估算到 168px；两个一行会把左侧配置列撑到 600px 以上并挤出
#: 横向滚动条。这里把最小宽度收敛到"放得下 11 个字符 + 箭头"的宽度。
_COORD_SPIN_MIN_WIDTH = 124

#: 未计算时结果区展示的占位文案
_RESULT_PLACEHOLDER = "尚未计算"

#: 未做连通性测试时展示的占位文案
_CONNECTION_PLACEHOLDER = "尚未测试"


class _CoordSpinBox(QDoubleSpinBox):
    """收窄最小宽度的经纬度输入框（见 ``_COORD_SPIN_MIN_WIDTH`` 说明）。

    注意：``setMinimumWidth()`` 无法生效，因为 ``QLayout`` 取的是
    ``minimumSize`` 与 ``minimumSizeHint`` 的较大值，必须重写 ``minimumSizeHint``。
    """

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt 命名约定
        hint = super().minimumSizeHint()
        return QSize(min(hint.width(), _COORD_SPIN_MIN_WIDTH), hint.height())


class DownloadPanel(QGroupBox):
    """地图配置 / 下载区域。"""

    #: 用户点击了某个动作按钮，参数为 ``ACTION_*`` 之一
    action_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("地图配置 / 下载区域", parent)

        self._build_general_form()
        self._build_area_form()
        self._build_output_form()
        self._build_buttons()
        self._build_result_area()
        self._build_connection_area()

        layout = QVBoxLayout(self)
        layout.addLayout(self._general_form)
        layout.addWidget(self._area_group)
        layout.addLayout(self._output_form)
        layout.addLayout(self._button_row)
        layout.addWidget(self._result_group)
        layout.addWidget(self._connection_group)
        layout.addStretch(1)

    # ------------------------------------------------------------------ #
    # 构建子区域
    # ------------------------------------------------------------------ #
    def _build_general_form(self) -> None:
        self._token_edit = QLineEdit(self)
        self._token_edit.setPlaceholderText("天地图服务密钥（tk），例如 abcd...")
        self._token_edit.setEchoMode(QLineEdit.EchoMode.Password)

        self._show_token_check = QCheckBox("显示", self)
        self._show_token_check.setToolTip("临时显示明文 Token")

        token_row = QHBoxLayout()
        token_row.addWidget(self._token_edit, 1)
        token_row.addWidget(self._show_token_check)

        self._map_type_combo = QComboBox(self)
        for layer in _BASE_LAYER_CHOICES:
            self._map_type_combo.addItem(LAYER_DISPLAY_NAMES[layer], layer)

        self._labels_check = QCheckBox("下载中文注记图层", self)
        self._labels_check.setChecked(True)

        self._general_form = QFormLayout()
        self._general_form.addRow("Token", token_row)
        self._general_form.addRow("地图类型", self._map_type_combo)
        self._general_form.addRow("中文注记", self._labels_check)

        self._show_token_check.toggled.connect(self._on_show_token_toggled)

    def _build_area_form(self) -> None:
        self._west_spin = self._make_lon_spin(-180.0, 180.0, 116.0)
        self._south_spin = self._make_lat_spin(-90.0, 90.0, 39.0)
        self._east_spin = self._make_lon_spin(-180.0, 180.0, 117.0)
        self._north_spin = self._make_lat_spin(-90.0, 90.0, 40.0)

        self._min_zoom_spin = QSpinBox(self)
        self._min_zoom_spin.setRange(MIN_SUPPORTED_ZOOM, MAX_SUPPORTED_ZOOM)
        self._min_zoom_spin.setValue(DEFAULT_MIN_ZOOM)

        self._max_zoom_spin = QSpinBox(self)
        self._max_zoom_spin.setRange(MIN_SUPPORTED_ZOOM, MAX_SUPPORTED_ZOOM)
        self._max_zoom_spin.setValue(DEFAULT_MAX_ZOOM)

        grid = QGridLayout()
        grid.addWidget(QLabel("West", self), 0, 0)
        grid.addWidget(self._west_spin, 0, 1)
        grid.addWidget(QLabel("East", self), 0, 2)
        grid.addWidget(self._east_spin, 0, 3)
        grid.addWidget(QLabel("South", self), 1, 0)
        grid.addWidget(self._south_spin, 1, 1)
        grid.addWidget(QLabel("North", self), 1, 2)
        grid.addWidget(self._north_spin, 1, 3)
        grid.addWidget(QLabel("Min Zoom", self), 2, 0)
        grid.addWidget(self._min_zoom_spin, 2, 1)
        grid.addWidget(QLabel("Max Zoom", self), 2, 2)
        grid.addWidget(self._max_zoom_spin, 2, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        self._area_group = QGroupBox("下载范围（WGS84 经纬度）", self)
        self._area_group.setLayout(grid)

    def _build_output_form(self) -> None:
        self._output_edit = QLineEdit(str(DEFAULT_OUTPUT_DIR), self)
        self._output_edit.setPlaceholderText("离线包输出目录")

        self._browse_button = QPushButton("浏览…", self)

        output_row = QHBoxLayout()
        output_row.addWidget(self._output_edit, 1)
        output_row.addWidget(self._browse_button)

        self._output_form = QFormLayout()
        self._output_form.addRow("输出目录", output_row)

        self._browse_button.clicked.connect(self._on_browse_clicked)

    def _build_buttons(self) -> None:
        self._calculate_button = QPushButton("计算", self)
        self._start_button = QPushButton("开始下载", self)
        self._pause_button = QPushButton("暂停", self)
        self._resume_button = QPushButton("继续", self)
        self._test_connection_button = QPushButton("测试天地图连接", self)
        self._test_connection_button.setToolTip(
            "取当前范围的中心点，请求单张瓦片验证 Token 与网络（不会下载整个区域）"
        )

        self._action_buttons: dict[str, QPushButton] = {
            ACTION_CALCULATE: self._calculate_button,
            ACTION_START: self._start_button,
            ACTION_PAUSE: self._pause_button,
            ACTION_RESUME: self._resume_button,
            ACTION_TEST_CONNECTION: self._test_connection_button,
        }

        self._button_row = QHBoxLayout()
        for action, button in self._action_buttons.items():
            button.setProperty("action_id", action)
            button.clicked.connect(
                lambda _checked=False, a=action: self.action_requested.emit(a)
            )
            self._button_row.addWidget(button)

    def _build_result_area(self) -> None:
        """瓦片计算结果区。

        Slice 1 起由 MainWindow 调用 :meth:`set_calculation_result` 写入，
        面板自身不计算任何瓦片数据（公式统一在 ``app.geo``）。
        """
        self._result_label = QLabel(_RESULT_PLACEHOLDER, self)
        self._result_label.setWordWrap(True)
        self._result_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self._result_group = QGroupBox("预估结果", self)
        group_layout = QVBoxLayout(self._result_group)
        group_layout.addWidget(self._result_label)

    def _build_connection_area(self) -> None:
        """天地图服务连通性测试结果区。

        与"预估结果"分开，因为二者的触发入口与生命周期不同：
        计算结果是配置的派生值，连接结果是服务端的一次性反馈。
        """
        self._connection_label = QLabel(_CONNECTION_PLACEHOLDER, self)
        self._connection_label.setWordWrap(True)
        self._connection_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self._connection_group = QGroupBox("天地图连接", self)
        group_layout = QVBoxLayout(self._connection_group)
        group_layout.addWidget(self._connection_label)

    # ------------------------------------------------------------------ #
    # 控件工厂
    # ------------------------------------------------------------------ #
    def _make_lon_spin(self, low: float, high: float, value: float) -> QDoubleSpinBox:
        """经度输入框（-180 ~ 180，6 位小数）。"""
        spin = _CoordSpinBox(self)
        spin.setRange(low, high)
        spin.setDecimals(6)
        spin.setSingleStep(0.001)
        spin.setValue(value)
        # 重写 minimumSizeHint 还不够：QLayout 在控件显式设置了 minimumSize 时
        # 会优先采用它，因此这里必须同时 setMinimumWidth 才能真正收窄。
        spin.setMinimumWidth(_COORD_SPIN_MIN_WIDTH)
        return spin

    def _make_lat_spin(self, low: float, high: float, value: float) -> QDoubleSpinBox:
        """纬度输入框（-90 ~ 90，6 位小数）。"""
        return self._make_lon_spin(low, high, value)

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def get_form_data(self) -> dict:
        """读取当前表单内容。

        Returns:
            dict，键与含义::

                token           str            天地图密钥
                map_type        MapLayerType   底图图层枚举成员
                with_labels     bool           是否下载中文注记
                west            float          西边界经度
                south           float          南边界纬度
                east            float          东边界经度
                north           float          北边界纬度
                min_zoom        int            最小缩放级别
                max_zoom        int            最大缩放级别
                output_dir      pathlib.Path   输出目录
        """
        return {
            "token": self._token_edit.text().strip(),
            "map_type": self._map_type_combo.currentData() or MapLayerType.VECTOR,
            "with_labels": self._labels_check.isChecked(),
            "west": self._west_spin.value(),
            "south": self._south_spin.value(),
            "east": self._east_spin.value(),
            "north": self._north_spin.value(),
            "min_zoom": self._min_zoom_spin.value(),
            "max_zoom": self._max_zoom_spin.value(),
            "output_dir": Path(self._output_edit.text().strip()),
        }

    @property
    def action_buttons(self) -> dict[str, QPushButton]:
        """动作按钮字典（只读用途，如测试点击或后续禁用/启用）。"""
        return dict(self._action_buttons)

    # ------------------------------------------------------------------ #
    # 结果展示
    # ------------------------------------------------------------------ #
    def set_calculation_result(self, tile_count: int, size_text: str) -> None:
        """展示瓦片计算成功的结果。

        Args:
            tile_count: 预计瓦片数量。
            size_text: 已格式化好的容量文本（如 ``361.93 MB``），
                由调用方通过 ``app.geo.format_size`` 生成。
        """
        self._result_label.setText(
            f"预计瓦片：{tile_count:,}\n预计容量：{size_text}"
        )

    def set_calculation_error(self, message: str) -> None:
        """展示计算失败原因（不弹窗，弹窗由调用方决定）。"""
        self._result_label.setText(f"计算失败：{message}")

    def clear_calculation_result(self) -> None:
        """恢复成未计算状态。"""
        self._result_label.setText(_RESULT_PLACEHOLDER)

    @property
    def result_label(self) -> QLabel:
        """暴露结果标签，便于测试与后续扩展。"""
        return self._result_label

    # ------------------------------------------------------------------ #
    # 连通性测试展示（Slice 2）
    # ------------------------------------------------------------------ #
    def set_connection_testing(self, testing: bool) -> None:
        """进入 / 退出"测试中"状态。

        测试期间按钮禁用，避免重复发起请求；测试完成后必须调用
        ``set_connection_testing(False)`` 恢复。
        """
        self._test_connection_button.setEnabled(not testing)
        if testing:
            self._connection_label.setText("正在测试天地图连接…")

    def set_connection_result(self, lines: Sequence[str]) -> None:
        """展示连通性测试结果（多行，每行一条信息）。"""
        self._connection_label.setText("\n".join(lines))

    def set_connection_error(self, message: str) -> None:
        """展示连通性测试失败原因。"""
        self._connection_label.setText(f"连接失败：{message}")

    def clear_connection_result(self) -> None:
        """恢复成未测试状态。"""
        self._connection_label.setText(_CONNECTION_PLACEHOLDER)

    @property
    def connection_label(self) -> QLabel:
        """暴露连通性测试结果标签。"""
        return self._connection_label

    @property
    def test_connection_button(self) -> QPushButton:
        """暴露"测试天地图连接"按钮。"""
        return self._test_connection_button

    # ------------------------------------------------------------------ #
    # 内部槽
    # ------------------------------------------------------------------ #
    @Slot(bool)
    def _on_show_token_toggled(self, visible: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self._token_edit.setEchoMode(mode)

    @Slot()
    def _on_browse_clicked(self) -> None:
        current = self._output_edit.text().strip() or str(DEFAULT_OUTPUT_DIR)
        chosen = QFileDialog.getExistingDirectory(
            self, "选择离线包输出目录", current
        )
        if chosen:
            self._output_edit.setText(chosen)
