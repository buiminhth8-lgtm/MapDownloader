"""下载配置面板。

只负责"把用户输入收集成结构化数据"（``get_form_data``）以及把用户的动作
以信号形式抛出去。真正的计算、下载、打包都在 ``app.geo`` /
``app.downloader`` / ``app.package`` 中实现，本文件不写任何业务逻辑。

Slice 0 范围：UI 与数据读取；按钮仅发信号，由 MainWindow 决定如何响应。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QSize, Signal, Slot
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
    DEFAULT_MAP_NAME,
    DEFAULT_MAX_ZOOM,
    DEFAULT_MIN_ZOOM,
    DEFAULT_OUTPUT_DIR,
    MAX_SUPPORTED_ZOOM,
    MIN_SUPPORTED_ZOOM,
    UI_TEXT_ESTIMATED_MAP_TYPE,
    UI_TEXT_ESTIMATED_REQUESTS_MERGED,
    UI_TEXT_ESTIMATED_REQUESTS_SINGLE,
    UI_TEXT_ESTIMATED_ZOOM,
    UI_TEXT_LAYER_OUTPUT_MERGED,
    UI_TEXT_LAYER_OUTPUT_SINGLE,
    UI_TEXT_LAYER_PLAN,
    UI_TEXT_MBTILES_OUTPUT_NOTICE,
)
from app.models.map_layer import (
    MAP_TYPE_DISPLAY_NAMES,
    MapType,
    describe_map_type,
)
from app.ui.task_actions import TaskAction, UiTaskState, actions_for
from app.ui.validation import auto_map_name

#: 地图类型下拉框的可选项（Slice 6，Spec §2）。
#:
#: 放的是 :class:`MapType` 而不是 :class:`MapLayerType`：用户选的是"矢量地图 /
#: 影像地图"这个业务概念，注记由独立复选框控制。至于底图到底是 ``vec_w`` 还是
#: ``img_w``，由 :data:`app.models.MAP_TYPE_LAYERS` 那张唯一的映射表回答 ——
#: UI 不重复实现一遍"矢量用哪个图层"。
_MAP_TYPE_CHOICES: tuple[MapType, ...] = (
    MapType.VECTOR,
    MapType.IMAGE,
)

#: 动作标识，与 MainWindow 的响应逻辑一一对应
ACTION_CALCULATE = "calculate"
ACTION_START = "start"
ACTION_PAUSE = "pause"
ACTION_RESUME = "resume"
ACTION_CANCEL = "cancel"
ACTION_TEST_CONNECTION = "test_connection"

#: :class:`~app.ui.task_actions.TaskAction` → 动作标识。
#:
#: 两个枚举不能合成一个：``TaskAction`` 是"用户可以干什么"（纯逻辑，不 import Qt，
#: 因此可以脱离窗口单测），``ACTION_*`` 是信号里的字符串载荷（UI 内部协议）。
#: 这张表是它们之间**唯一**的翻译点。
_ACTION_IDS: dict[TaskAction, str] = {
    TaskAction.START: ACTION_START,
    TaskAction.PAUSE: ACTION_PAUSE,
    TaskAction.RESUME: ACTION_RESUME,
    TaskAction.CANCEL: ACTION_CANCEL,
}

#: 按钮上显示的中文（与 ``TaskAction.display_name`` 同义，
#: 但按钮文案属于本面板，留在本面板）
_ACTION_LABELS: dict[TaskAction, str] = {
    TaskAction.START: "开始下载",
    TaskAction.PAUSE: "暂停",
    TaskAction.RESUME: "继续",
    TaskAction.CANCEL: "取消",
}

#: 经纬度输入框的最小宽度。
#: QDoubleSpinBox 的 ``minimumSizeHint`` 会按最大位数（``-180.000000``，11 字符）
#: 加上调节箭头，保守估算到 168px；两个一行会把左侧配置列撑到 600px 以上并挤出
#: 横向滚动条。这里把最小宽度收敛到"放得下 11 个字符 + 箭头"的宽度。
_COORD_SPIN_MIN_WIDTH = 124

#: 未计算时结果区展示的占位文案
_RESULT_PLACEHOLDER = "尚未计算"

#: 未做连通性测试时展示的占位文案
_CONNECTION_PLACEHOLDER = "尚未测试"

#: 尚未下载时"下载结果"区的占位文案
_DOWNLOAD_RESULT_PLACEHOLDER = "尚未下载"

#: 折行标签（Slice 7 起放在 ``app.ui.widgets`` 里，三个面板共用）。
#: 这里保留 ``_WrappingLabel`` / ``_wrap_anywhere`` 两个名字：
#: 本模块内的用法与既有测试都按这两个私有名引用，改成新名字没有任何收益。
from app.ui.widgets import WrappingLabel as _WrappingLabel  # noqa: E402
from app.ui.widgets import apply_secondary_text as _apply_secondary_text  # noqa: E402
from app.ui.widgets import wrap_anywhere as _wrap_anywhere  # noqa: E402


class _CoordSpinBox(QDoubleSpinBox):
    """收窄最小宽度的经纬度输入框（见 ``_COORD_SPIN_MIN_WIDTH`` 说明）。

    注意：``setMinimumWidth()`` 无法生效，因为 ``QLayout`` 取的是
    ``minimumSize`` 与 ``minimumSizeHint`` 的较大值，必须重写 ``minimumSizeHint``。
    """

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt 命名约定
        hint = super().minimumSizeHint()
        return QSize(min(hint.width(), _COORD_SPIN_MIN_WIDTH), hint.height())


def _task_map_type(task) -> MapType | None:
    """从任务记录里取出地图类型；认不出来返回 ``None``。

    走鸭子类型而不是 ``isinstance``：``DownloadTask.source_map_type`` 已经是
    "优先取 map_type、缺失时由 map_layer 反推"的权威实现，这里只管调用它，
    不把那条回退规则再抄一遍。测试里的轻量替身也走得通。
    """
    resolved = getattr(task, "source_map_type", None)
    if isinstance(resolved, MapType):
        return resolved
    if isinstance(resolved, str) and resolved.strip():
        return MapType.parse(resolved)
    # 替身对象可能既没有 source_map_type 也不是 DownloadTask：退回 map_layer
    return MapType.parse(str(getattr(task, "map_layer", "") or ""))


class DownloadPanel(QGroupBox):
    """地图配置 / 下载区域。"""

    #: 用户点击了某个动作按钮，参数为 ``ACTION_*`` 之一
    action_requested = Signal(str)

    #: 输出目录或地图名称被改动 —— 由 MainWindow 据此刷新"输出文件"预览。
    #: 面板自己不去拼路径：路径规则（非法字符清理、扩展名）属于业务层，
    #: 统一由 ``app.services.build_output_path`` 提供，UI 只负责显示。
    output_config_changed = Signal()

    #: 地图类型或「中文注记」被改动（Slice 6）—— MainWindow 据此刷新
    #: 「预估结果」里的请求量行。请求量 = 瓦片数 × 每张瓦片请求次数，
    #: 两个因子分别来自"范围计算"与"图层选择"，所以两边任何一边变了都要重画。
    layer_plan_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("地图配置 / 下载区域", parent)

        #: 当前界面状态（决定四个动作按钮的可用性）。必须在构建按钮**之前**
        #: 初始化：``_build_buttons`` 结尾就会调一次 ``apply_task_state``。
        self._task_state: UiTaskState = UiTaskState.IDLE

        self._build_general_form()
        self._build_area_form()
        self._build_output_form()
        self._build_buttons()
        self._build_result_area()
        self._build_download_result_area()
        self._build_connection_area()

        layout = QVBoxLayout(self)
        layout.addLayout(self._general_form)
        layout.addWidget(self._area_group)
        layout.addLayout(self._output_form)
        layout.addLayout(self._button_row)
        layout.addWidget(self._mode_notice)
        layout.addWidget(self._result_group)
        layout.addWidget(self._download_result_group)
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
        for map_type in _MAP_TYPE_CHOICES:
            self._map_type_combo.addItem(MAP_TYPE_DISPLAY_NAMES[map_type], map_type)

        self._labels_check = QCheckBox("下载中文注记图层", self)
        self._labels_check.setChecked(True)
        self._labels_check.setToolTip(
            "勾选后把中文注记合并进底图瓦片（一张瓦片请求底图 + 注记两次，"
            "输出 PNG）；取消勾选只下载纯底图"
        )

        # 「地图类型」与「中文注记」同占一行：左侧配置列内容已经比 760px 的窗口高，
        # 而这两个控件都很短，各占一行纯属浪费纵向空间（会挤掉下方"下载状态"区）。
        type_row = QHBoxLayout()
        type_row.addWidget(self._map_type_combo, 1)
        type_row.addWidget(self._labels_check)

        # Slice 6 的动态说明（Spec §13）：这次请求哪些图层 / 产物是什么形态 /
        # 每张瓦片几次请求。三行都随上面的下拉框与复选框实时变化 ——
        # "勾上注记"这个动作会让请求量翻倍，必须在**开下载之前**就说清楚。
        self._layer_plan_label = _WrappingLabel("", self)

        self._general_form = QFormLayout()
        self._general_form.addRow("Token", token_row)
        self._general_form.addRow("地图类型", type_row)
        self._general_form.addRow("", self._layer_plan_label)

        self._show_token_check.toggled.connect(self._on_show_token_toggled)
        self._map_type_combo.currentIndexChanged.connect(self._on_layer_plan_changed)
        self._labels_check.toggled.connect(self._on_layer_plan_changed)
        self._refresh_layer_plan()

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

        #: 地图名称 —— 决定 ``<名称>.mbtiles`` 的文件名主体
        self._map_name_edit = QLineEdit(DEFAULT_MAP_NAME, self)
        self._map_name_edit.setPlaceholderText("地图名称，例如 beijing")

        self._output_form = QFormLayout()
        self._output_form.addRow("输出目录", output_row)
        self._output_form.addRow("地图名称", self._map_name_edit)

        self._browse_button.clicked.connect(self._on_browse_clicked)
        self._output_edit.textChanged.connect(self._on_output_config_changed)
        self._map_name_edit.textChanged.connect(self._on_output_config_changed)

    @Slot()
    def _on_output_config_changed(self) -> None:
        """输出目录 / 地图名称变了，通知外部刷新预览。"""
        self.output_config_changed.emit()

    def _build_buttons(self) -> None:
        self._calculate_button = QPushButton("计算", self)
        self._start_button = QPushButton(_ACTION_LABELS[TaskAction.START], self)
        self._pause_button = QPushButton(_ACTION_LABELS[TaskAction.PAUSE], self)
        self._resume_button = QPushButton(_ACTION_LABELS[TaskAction.RESUME], self)
        self._cancel_button = QPushButton(_ACTION_LABELS[TaskAction.CANCEL], self)
        self._cancel_button.setToolTip(
            "取消当前任务。已下载的瓦片会保留在 .mbtiles 里，以后仍可继续"
        )
        self._test_connection_button = QPushButton("测试天地图连接", self)
        self._test_connection_button.setToolTip(
            "取当前范围的中心点，请求单张瓦片验证 Token 与网络（不会下载整个区域）"
        )

        # 顺序即界面上从左到右的顺序：先"算清楚"，再"开跑"，最后是跑起来之后的
        # 三个控制动作。「取消」紧挨着「暂停 / 继续」——它们回答的是同一个问题
        # （"现在要不要让它停"），放在一起用户才不用来回找。
        self._action_buttons: dict[str, QPushButton] = {
            ACTION_CALCULATE: self._calculate_button,
            ACTION_START: self._start_button,
            ACTION_PAUSE: self._pause_button,
            ACTION_RESUME: self._resume_button,
            ACTION_CANCEL: self._cancel_button,
            ACTION_TEST_CONNECTION: self._test_connection_button,
        }

        self._button_row = QHBoxLayout()
        for action, button in self._action_buttons.items():
            button.setProperty("action_id", action)
            button.clicked.connect(
                lambda _checked=False, a=action: self.action_requested.emit(a)
            )
            self._button_row.addWidget(button)

        # Slice 4 起瓦片真的会落盘：这里说明输出产物形态（不再是"测试模式"）。
        self._mode_notice = QLabel(UI_TEXT_MBTILES_OUTPUT_NOTICE, self)
        self._mode_notice.setWordWrap(True)
        # 深色主题下 palette(mid) ≈ 背景色，字会整片消失（见 app/ui/widgets.py）
        _apply_secondary_text(self._mode_notice)

        # 初始态：四个控制动作都没有意义，全部保持禁用
        self.apply_task_state(UiTaskState.IDLE)

    def _build_result_area(self) -> None:
        """瓦片计算结果区。

        Slice 1 起由 MainWindow 调用 :meth:`set_calculation_result` 写入，
        面板自身不计算任何瓦片数据（公式统一在 ``app.geo``）。

        Slice 4 起多一行「输出文件」——开始下载前用户就该看到文件会落在哪。
        """
        self._result_label = _WrappingLabel(_RESULT_PLACEHOLDER, self)

        self._result_group = QGroupBox("预估结果", self)
        group_layout = QVBoxLayout(self._result_group)
        group_layout.addWidget(self._result_label)

    def _build_download_result_area(self) -> None:
        """下载结果区（Slice 4）。

        与「预估结果」分开：那个是开工前的**预测**，这个是收工后的**实测**
        （文件、真实瓦片数、真实体积、验证结论）。混在一个标签里会让人误读。
        """
        self._download_result_label = _WrappingLabel(
            _DOWNLOAD_RESULT_PLACEHOLDER, self
        )

        self._download_result_group = QGroupBox("下载结果", self)
        group_layout = QVBoxLayout(self._download_result_group)
        group_layout.addWidget(self._download_result_label)
        # 还没有结果就先不占地方：一个空的"尚未下载"分组只是噪音，
        # 而且会把下方的「下载状态」区（进度条所在）挤出可视范围。
        self._download_result_group.setVisible(False)

    def _build_connection_area(self) -> None:
        """天地图服务连通性测试结果区。

        与"预估结果"分开，因为二者的触发入口与生命周期不同：
        计算结果是配置的派生值，连接结果是服务端的一次性反馈。
        """
        self._connection_label = _WrappingLabel(_CONNECTION_PLACEHOLDER, self)

        self._connection_group = QGroupBox("天地图连接", self)
        group_layout = QVBoxLayout(self._connection_group)
        group_layout.addWidget(self._connection_label)
        # 与「下载结果」同一个理由：还没测过时它只是一句"尚未测试"，
        # 白占 53px 而左侧配置列本来就紧（760px 窗口下加一行图层说明就会
        # 挤出纵向滚动条，进而连带拽出横向滚动条）。一旦有了结果——成功、
        # 失败或"测试中"——立刻显形。
        self._connection_group.setVisible(False)

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
    # 图层计划说明（Slice 6，Spec §13）
    # ------------------------------------------------------------------ #
    def current_map_type(self) -> MapType:
        """当前选中的地图类型（下拉框为空时退回矢量地图）。"""
        return self._map_type_combo.currentData() or MapType.VECTOR

    def with_labels_selected(self) -> bool:
        """「下载中文注记图层」是否被勾选。"""
        return self._labels_check.isChecked()

    def layer_plan_text(self) -> str:
        """三行图层计划说明的**原始**文本（不含自动折行）。

        公开成方法而不是让调用方各自 ``format`` 一遍模板：文案只有一处，
        测试断言、界面显示、日志引用拿到的必然是同一份字符串。
        """
        map_type = self.current_map_type()
        with_labels = self.with_labels_selected()
        layers = " + ".join(item.value for item in map_type.layers(with_labels))
        output = (
            UI_TEXT_LAYER_OUTPUT_MERGED if with_labels else UI_TEXT_LAYER_OUTPUT_SINGLE
        )
        return UI_TEXT_LAYER_PLAN.format(
            layers=layers,
            output=output,
            requests=map_type.request_count_per_tile(with_labels),
        )

    def _refresh_layer_plan(self) -> None:
        """把当前选择算出的三行说明写进标签。"""
        self._layer_plan_label.set_text(self.layer_plan_text())

    def _on_layer_plan_changed(self, *_args) -> None:
        """下拉框 / 复选框变化：先改自己的说明，再通知外部（请求量要重算）。

        参数签名写成可变参数：两个信号一个带 ``int``（``currentIndexChanged``）
        一个带 ``bool``（``toggled``），而这里两个值都不需要。
        """
        self._refresh_layer_plan()
        self.layer_plan_changed.emit()

    # ------------------------------------------------------------------ #
    # 地图名称与配置摘要（Slice 7）
    # ------------------------------------------------------------------ #
    def map_name_text(self) -> str:
        """当前地图名称（已去掉首尾空白）。"""
        return self._map_name_edit.text().strip()

    def set_map_name(self, name: str) -> None:
        """写入地图名称（回填与自动生成共用）。"""
        self._map_name_edit.setText(str(name))

    def ensure_map_name(self) -> str | None:
        """地图名称为空时自动补一个（Spec §2.7）。

        规则在 :func:`app.ui.validation.auto_map_name`（用范围中心点），本方法
        只负责"写回输入框"——写回而不是悄悄用在内部，是因为用户必须**看得见**
        将要生成的文件叫什么；否则他会在"输出文件"那一行看到一个自己没写过的
        名字，怀疑程序弄错了。

        Returns:
            本次自动生成的名称；名称本来就非空、或范围非法导致无法生成时返回
            ``None``（后一种情况由校验层去报错，这里不抢着下结论）。
        """
        if self.map_name_text():
            return None
        data = self.get_form_data()
        try:
            name = auto_map_name(
                float(data["west"]),
                float(data["south"]),
                float(data["east"]),
                float(data["north"]),
            )
        except (TypeError, ValueError):  # pragma: no cover - 防御性分支
            return None
        self.set_map_name(name)
        return name

    def map_type_plan_text(self) -> str:
        """当前"地图类型 + 中文注记"的一行摘要（如 ``矢量地图 + 中文注记``）。"""
        return describe_map_type(self.current_map_type(), self.with_labels_selected())

    def zoom_range_text(self) -> str:
        """当前缩放区间（如 ``8 ~ 17``）。"""
        return f"{self._min_zoom_spin.value()} ~ {self._max_zoom_spin.value()}"

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def get_form_data(self) -> dict:
        """读取当前表单内容。

        Returns:
            dict，键与含义::

                token           str            天地图密钥
                map_type        MapType        地图类型（矢量 / 影像）
                with_labels     bool           是否把中文注记合成进瓦片
                west            float          西边界经度
                south           float          南边界纬度
                east            float          东边界经度
                north           float          北边界纬度
                min_zoom        int            最小缩放级别
                max_zoom        int            最大缩放级别
                output_dir      pathlib.Path   输出目录
                map_name        str            地图名称（``<名称>.mbtiles``）
        """
        return {
            "token": self._token_edit.text().strip(),
            "map_type": self.current_map_type(),
            "with_labels": self.with_labels_selected(),
            "west": self._west_spin.value(),
            "south": self._south_spin.value(),
            "east": self._east_spin.value(),
            "north": self._north_spin.value(),
            "min_zoom": self._min_zoom_spin.value(),
            "max_zoom": self._max_zoom_spin.value(),
            "output_dir": Path(self._output_edit.text().strip()),
            "map_name": self._map_name_edit.text().strip(),
        }

    @property
    def action_buttons(self) -> dict[str, QPushButton]:
        """动作按钮字典（只读用途，如测试点击或后续禁用/启用）。"""
        return dict(self._action_buttons)

    # ------------------------------------------------------------------ #
    # 直接写入单项（Slice 7：恢复界面偏好 / 回填任务参数用）
    # ------------------------------------------------------------------ #
    def set_map_type(self, map_type: MapType) -> bool:
        """选中指定的地图类型。

        Returns:
            ``True`` 表示下拉框里有这一项；``False`` 表示当前版本不支持
            （此时保持原选项不动——换成一个用户没选过的类型更糟）。
        """
        for index in range(self._map_type_combo.count()):
            if self._map_type_combo.itemData(index) is map_type:
                self._map_type_combo.setCurrentIndex(index)
                return True
        return False

    def set_with_labels(self, value: bool) -> None:
        """设置「下载中文注记图层」。"""
        self._labels_check.setChecked(bool(value))

    def set_zoom_range(self, min_zoom: int, max_zoom: int) -> None:
        """设置缩放区间。

        传进来的值可能超出控件范围（例如来自旧版本的偏好或任务记录）：
        ``setValue`` 会自动 clamp，不会抛异常。
        """
        self._min_zoom_spin.setValue(int(min_zoom))
        self._max_zoom_spin.setValue(int(max_zoom))

    def set_output_dir(self, path: str | Path) -> None:
        """设置输出目录。"""
        self._output_edit.setText(str(path))

    # ------------------------------------------------------------------ #
    # 反向填充（Slice 5：从任务记录恢复表单）
    # ------------------------------------------------------------------ #
    def set_form_data(self, task) -> None:
        """把一条任务记录回填到表单（Spec §14/§16：双击任务 → 恢复配置）。

        刻意**不回填 Token**：任务记录里根本没有 Token（Spec §9），
        而把它清空会让人以为"程序把我的 Token 弄丢了"。保持用户当前输入不动。

        地图类型优先按 Slice 6 的 ``source_map_type`` 反查；老记录（Slice ≤5）
        没有这个字段，退回用 ``map_layer`` 反查（``cva_w`` / ``cia_w`` 也能认出来）。
        认不出来就保持原选项并返回 ``False``（由调用方决定要不要提示），
        不抛异常——一个"图层不认识"的任务不该让界面炸掉。

        Args:
            task: :class:`~app.models.download_task.DownloadTask` 或任何具备
                同名属性的对象。

        Returns:
            ``True`` 表示地图类型也成功回填了；``False`` 表示没认出来。
        """
        self._west_spin.setValue(float(task.west))
        self._south_spin.setValue(float(task.south))
        self._east_spin.setValue(float(task.east))
        self._north_spin.setValue(float(task.north))
        # 任务里的 zoom 可能超出 UI 下拉范围（例如由命令行建的记录）：
        # setValue 会自动 clamp 到控件范围，不会抛异常。
        self._min_zoom_spin.setValue(int(task.min_zoom))
        self._max_zoom_spin.setValue(int(task.max_zoom))
        self._map_name_edit.setText(str(task.name))
        output_path = Path(str(task.output_path))
        # 任务存的是**文件**路径，表单要的是**目录**
        self._output_edit.setText(str(output_path.parent))

        # 中文注记：任务记录里是权威值；老记录没有这个字段 → 视作"未合成"
        self._labels_check.setChecked(bool(getattr(task, "with_labels", False)))

        map_type = _task_map_type(task)
        if map_type is None:
            return False
        for index in range(self._map_type_combo.count()):
            if self._map_type_combo.itemData(index) is map_type:
                self._map_type_combo.setCurrentIndex(index)
                return True
        return False

    # ------------------------------------------------------------------ #
    # 结果展示
    # ------------------------------------------------------------------ #
    def set_calculation_result(
        self, tile_count: int, size_text: str, output_path: str | Path | None = None
    ) -> None:
        """展示瓦片计算成功的结果（Slice 7 起含 Zoom 与地图类型，Spec §3）。

        Args:
            tile_count: 预计瓦片数量。
            size_text: 已格式化好的容量文本（如 ``361.93 MB``），
                由调用方通过 ``app.geo.format_size`` 生成。
            output_path: 预计输出的 ``.mbtiles`` 完整路径；``None`` 时该行
                显示占位符（例如地图名称为空）。

        请求量行（Slice 6）与 Zoom / 地图类型行（Slice 7）都由本面板按**当前**
        的输入算出来，因此不需要调用方多传参数：勾上中文注记后请求量翻倍这件事，
        与"面板上现在选的是什么"永远一致，不会出现两处算出不同数字。

        行序与 Spec §3 一致：瓦片数 → 请求量 → 容量 → Zoom → 地图类型，
        最后是本项目的补充（输出文件）——它是"这些东西要落到哪儿"的答案。
        """
        map_type = self.current_map_type()
        with_labels = self.with_labels_selected()
        requests = tile_count * map_type.request_count_per_tile(with_labels)
        template = (
            UI_TEXT_ESTIMATED_REQUESTS_MERGED
            if with_labels
            else UI_TEXT_ESTIMATED_REQUESTS_SINGLE
        )

        lines = [
            f"预计瓦片：{tile_count:,}",
            # 单层模板里没有 ``{tiles}``，多传一个键不会报错（str.format 忽略多余键）
            template.format(requests=requests, tiles=tile_count),
            f"预计容量：{size_text}",
            UI_TEXT_ESTIMATED_ZOOM.format(
                min_zoom=self._min_zoom_spin.value(),
                max_zoom=self._max_zoom_spin.value(),
            ),
            UI_TEXT_ESTIMATED_MAP_TYPE.format(
                map_type=describe_map_type(map_type, with_labels)
            ),
            f"输出文件：{output_path}" if output_path else "输出文件：（请填写地图名称）",
        ]
        self._result_label.set_text("\n".join(lines))

    def set_calculation_error(self, message: str) -> None:
        """展示计算失败原因（不弹窗，弹窗由调用方决定）。"""
        self._result_label.set_text(f"计算失败：{message}")

    def clear_calculation_result(self) -> None:
        """恢复成未计算状态。"""
        self._result_label.set_text(_RESULT_PLACEHOLDER)

    @property
    def result_label(self) -> QLabel:
        """暴露结果标签，便于测试与后续扩展。"""
        return self._result_label

    @property
    def map_type_combo(self) -> QComboBox:
        """暴露「地图类型」下拉框（测试与回填用）。"""
        return self._map_type_combo

    @property
    def labels_check(self) -> QCheckBox:
        """暴露「下载中文注记图层」复选框。"""
        return self._labels_check

    @property
    def layer_plan_label(self) -> QLabel:
        """暴露图层计划说明标签（三行：图层 / 产物 / 请求次数）。"""
        return self._layer_plan_label

    # ------------------------------------------------------------------ #
    # 下载结果展示（Slice 4）
    # ------------------------------------------------------------------ #
    def set_download_result(self, lines: Sequence[str]) -> None:
        """展示下载结束后的实测结果（多行）。

        内容由 :meth:`app.services.MapDownloadOutcome.as_lines` 生成——
        面板不参与拼装，避免"UI 里再算一遍瓦片数"这类两处真相问题。
        """
        self._download_result_label.set_text("\n".join(lines))
        self._download_result_group.setVisible(True)

    def set_download_result_error(self, message: str) -> None:
        """展示下载失败原因。"""
        self._download_result_label.set_text(f"下载失败：{message}")
        self._download_result_group.setVisible(True)

    def clear_download_result(self) -> None:
        """恢复成未下载状态（分组一并收起，不占纵向空间）。"""
        self._download_result_label.set_text(_DOWNLOAD_RESULT_PLACEHOLDER)
        self._download_result_group.setVisible(False)

    @property
    def download_result_label(self) -> QLabel:
        """暴露下载结果标签。"""
        return self._download_result_label

    @property
    def download_result_group(self) -> QGroupBox:
        """暴露下载结果分组（MainWindow 用它做结果可见性滚动）。"""
        return self._download_result_group

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
            self._connection_label.set_text("正在测试天地图连接…")
            self._connection_group.setVisible(True)

    def set_connection_result(self, lines: Sequence[str]) -> None:
        """展示连通性测试结果（多行，每行一条信息）。"""
        self._connection_label.set_text("\n".join(lines))
        self._connection_group.setVisible(True)

    def set_connection_error(self, message: str) -> None:
        """展示连通性测试失败原因。"""
        self._connection_label.set_text(f"连接失败：{message}")
        self._connection_group.setVisible(True)

    def clear_connection_result(self) -> None:
        """恢复成未测试状态（分组一并收起，不占纵向空间）。"""
        self._connection_label.set_text(_CONNECTION_PLACEHOLDER)
        self._connection_group.setVisible(False)

    @property
    def connection_label(self) -> QLabel:
        """暴露连通性测试结果标签。"""
        return self._connection_label

    @property
    def connection_group(self) -> QGroupBox:
        """暴露连通性测试分组（有结果时才可见）。"""
        return self._connection_group

    @property
    def test_connection_button(self) -> QPushButton:
        """暴露"测试天地图连接"按钮。"""
        return self._test_connection_button

    # ------------------------------------------------------------------ #
    # 下载状态（Slice 3；Slice 7 改为按状态矩阵统一控制）
    # ------------------------------------------------------------------ #
    def apply_task_state(self, state: UiTaskState) -> None:
        """按界面状态切换按钮可用性（Slice 7，Spec §6）。

        规则本身在 :data:`app.ui.task_actions.AVAILABLE_ACTIONS` 里，本方法只做
        "读表 → 设 ``setEnabled``"。这样"哪个状态下允许点哪个"可以被逐格断言，
        而不用把界面点一遍。

        除四个控制动作外，「计算」与「测试天地图连接」在有任务占用通道时也一并
        禁用：它们读的是**当前表单**，而表单在下载期间已经不再与正在跑的任务
        对应（用户可以随手改范围），放行只会制造"界面显示的和实际在下的是两回事"。

        Args:
            state: 目标界面状态。
        """
        self._task_state = state
        allowed = actions_for(state)

        for action, action_id in _ACTION_IDS.items():
            button = self._action_buttons.get(action_id)
            if button is not None:
                button.setEnabled(action in allowed)

        # 表单编辑类动作：只有"没有任务占用下载通道"时才可用
        idle = not state.is_busy
        self._calculate_button.setEnabled(idle)
        self._test_connection_button.setEnabled(idle)

    @property
    def task_state(self) -> UiTaskState:
        """当前界面状态。"""
        return self._task_state

    def set_download_running(self, running: bool, *, paused: bool = False) -> None:
        """按"是否在跑 / 是否暂停"切换按钮可用性（Slice 3 的旧入口）。

        保留它是因为 Slice 3~6 的调用点与测试都在用；实现已改为转发到
        :meth:`apply_task_state`，因此两条路径不可能给出不同的按钮状态。

        Args:
            running: 是否有下载在跑。
            paused: 下载是否处于暂停态（仅 ``running=True`` 时有意义）。
        """
        if not running:
            self.apply_task_state(UiTaskState.IDLE)
        elif paused:
            self.apply_task_state(UiTaskState.PAUSED)
        else:
            self.apply_task_state(UiTaskState.DOWNLOADING)

    def set_scanning(self, scanning: bool) -> None:
        """进入 / 退出"正在扫描已有瓦片"（Spec §11）。

        扫描期间四个控制动作里只放开「取消」：用户可以反悔"这次不下了"，
        但不该在还没算清要下多少张的时候点「开始下载」。
        """
        self.apply_task_state(
            UiTaskState.SCANNING if scanning else UiTaskState.IDLE
        )

    @property
    def mode_notice(self) -> QLabel:
        """暴露输出形态说明标签（Slice 4 起说明输出 ``.mbtiles``）。"""
        return self._mode_notice

    @property
    def map_name_edit(self) -> QLineEdit:
        """暴露"地图名称"输入框。"""
        return self._map_name_edit

    @property
    def output_edit(self) -> QLineEdit:
        """暴露"输出目录"输入框。"""
        return self._output_edit

    @property
    def start_button(self) -> QPushButton:
        """暴露"开始下载"按钮。"""
        return self._start_button

    @property
    def pause_button(self) -> QPushButton:
        """暴露"暂停"按钮。"""
        return self._pause_button

    @property
    def resume_button(self) -> QPushButton:
        """暴露"继续"按钮。"""
        return self._resume_button

    @property
    def cancel_button(self) -> QPushButton:
        """暴露"取消"按钮（Slice 7）。"""
        return self._cancel_button

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
