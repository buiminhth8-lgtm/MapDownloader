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
    QButtonGroup,
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
    QRadioButton,
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
from app.geo import (
    DownloadRegion,
    GeoBounds,
    GeoJSONDocument,
    GeoJSONError,
    RegionBoundaryMissing,
    RegionType,
    estimate_size_bytes,
    format_size,
    geometry_to_bounds,
)
from app.geo.region import RegionPreset
from app.models.map_layer import (
    MAP_TYPE_DISPLAY_NAMES,
    MapType,
    describe_map_type,
)
from app.planning import (
    RegionLevel,
    ZoomRecommendation,
)
from app.regions import RegionService
from app.ui.region_selection import RegionSelectionController, ZoomMode
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

#: 省份 / 城市下拉里"不限定"的那一项（Spec §1 的「全部/请选择」）。
#:
#: 它的 ``itemData`` 是 ``None``：选中它意味着"这一级不参与"，
#: 于是等级自动落回上一级（只选中国 → COUNTRY，选了省 → PROVINCE）。
_ANY_PROVINCE_TEXT = "全部（不限定省份）"
_ANY_CITY_TEXT = "全部（不限定城市）"

#: 省下面确实没有城市级数据时，城市下拉里那一项改成的文案。
#: 这与"还没选省"是两回事：后者是用户的操作顺序问题，前者是数据问题。
_NO_CITY_TEXT = "（该省暂无城市级数据）"

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

    #: 下载区域（区域类型 / 行政区）变了（Slice 16）。
    #:
    #: 必须发这个信号：一次"计算"的结果是**绑定在当时那个区域上**的。
    #: 从「自定义矩形」切到「行政区域」而边界文件又不可用时，界面若还留着
    #: 上一次算出来的矩形张数，用户看到的就是一个**静默退回矩形**的结论 ——
    #: Spec §16/§32 明令禁止的那种。主窗口收到本信号后会把旧结论清掉。
    region_changed = Signal()

    #: 地图类型或「中文注记」被改动（Slice 6）—— MainWindow 据此刷新
    #: 「预估结果」里的请求量行。请求量 = 瓦片数 × 每张瓦片请求次数，
    #: 两个因子分别来自"范围计算"与"图层选择"，所以两边任何一边变了都要重画。
    layer_plan_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("地图配置 / 下载区域", parent)

        #: 当前界面状态（决定四个动作按钮的可用性）。必须在构建按钮**之前**
        #: 初始化：``_build_buttons`` 结尾就会调一次 ``apply_task_state``。
        self._task_state: UiTaskState = UiTaskState.IDLE

        #: 行政区目录服务（Slice 18）。构造时不传参数 → 它跟随
        #: ``app.geo.region.REGION_RESOURCE_DIR`` 这个全局，验收脚本把资源重定向到
        #: 临时目录时面板会自动跟着走（与 Slice 16 的行为一致）。
        self._region_service = RegionService()

        #: 级联选择 + Zoom 模式（Slice 19，Spec §12）。面板只画控件、转发事件、
        #: 渲染结论；"国家 → 省 → 市"的推导与"等级 → Zoom"的映射都在控制器里
        #: —— 本类因此**没有一处** Zoom 区间常量，也**没有一处**解析
        #: ``index.json`` 的代码。
        self._controller = RegionSelectionController(self._region_service)

        #: 界面正在被程序自己改写（回填下拉、应用推荐值）。
        #: 没有它的话，"设置下拉项"会立刻回调进来再改一遍下拉项 ——
        #: 表现为切换省时城市列表被清两次、或者推荐值把用户刚选的东西顶掉。
        self._syncing: bool = False

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

        # --- Slice 16：区域类型（自定义矩形 / 行政区域）---
        # 缓存当前行政区域的几何与加载错误：边界文件可能有几 MB，切换时加载一次
        # 就够，"计算"与"开始下载"都复用这份缓存，不在每次读表单时重新解析。
        self._region_document: GeoJSONDocument | None = None
        self._region_error: str = ""

        self._region_type_combo = QComboBox(self)
        for candidate in (RegionType.RECTANGLE, RegionType.POLYGON):
            self._region_type_combo.addItem(candidate.display_name, candidate)
        self._region_type_combo.setToolTip(
            "自定义矩形：按下面四个经纬度下载整个矩形\n"
            "行政区域：按行政区边界精确裁剪，只下载与区域相交的瓦片"
        )

        # --- Slice 19：国家 → 省 → 市 三段级联（Spec §1 / §2）---
        # 三段下拉的条目一律来自 RegionService（控制器负责问），面板不解析
        # index.json、不扫 GeoJSON、不推导 parent。
        self._country_combo = QComboBox(self)
        self._province_combo = QComboBox(self)
        self._city_combo = QComboBox(self)
        for combo in (self._country_combo, self._province_combo, self._city_combo):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(12)
        self._province_combo.setToolTip("留空表示不限定省份 —— 这时下载的是整个国家")
        self._city_combo.setToolTip("留空表示不限定城市 —— 这时下载的是整个省")

        #: 「下载级别」是**推导**出来的（市 → 省 → 国），不是让用户选的第四项。
        #: 让它可点会与三段下拉互相打架（"选了城市却把级别设成国家级"该怎么算？），
        #: 而 Spec §2 给的本来就是推导规则。
        self._level_title = QLabel("下载级别", self)
        self._level_label = _WrappingLabel("", self)

        #: 行政区域模式下代替手工输入的范围提示（只读展示）
        self._bounds_hint_label = _WrappingLabel("", self)

        # --- Slice 19：下载策略（推荐 / 自定义，Spec §1 / §4 / §5 / §6）---
        self._zoom_mode_title = QLabel("下载策略", self)
        self._zoom_recommended_radio = QRadioButton(ZoomMode.RECOMMENDED.display_name, self)
        self._zoom_custom_radio = QRadioButton(ZoomMode.CUSTOM.display_name, self)
        self._zoom_recommended_radio.setChecked(True)
        # 两个单选必须互斥。放进 QButtonGroup 而不是靠父子关系：放在同一个
        # 容器里也能互斥，但那要求它们同父 —— 而这里为了排版它们是分开加到
        # 一行布局里的，靠 autoExclusive 会时灵时不灵。
        self._zoom_mode_group = QButtonGroup(self)
        self._zoom_mode_group.addButton(self._zoom_recommended_radio)
        self._zoom_mode_group.addButton(self._zoom_custom_radio)

        # --- Slice 17：Zoom 建议（Spec §11）---
        # 只在"选了带等级的行政区"时出现。矩形 / 自定义区域不套用等级规则，
        # 那一行会整行隐藏（QGridLayout 会把高度压到 0，不留空白）。
        self._zoom_hint_title = QLabel("Zoom 建议", self)
        self._zoom_hint_label = _WrappingLabel("", self)

        #: 推荐说明（Spec §1）。文案取自策略层的 ``description``，界面不抄一份
        #: —— 否则改了策略表里的说法，界面上还是旧句子。
        self._zoom_explain_title = QLabel("推荐说明", self)
        self._zoom_explain_label = _WrappingLabel("", self)

        # 每行的**标题**也要留住引用：QGridLayout 只在"整行控件都隐藏"时才把
        # 行高压到 0。只藏控件、留着标题的话，矩形模式下会多出「国家」「省份」
        # 「城市」三个孤零零的标签和三条空白 —— 而 Spec §7 要求矩形模式下界面
        # 与 Slice ≤18 一模一样。
        self._country_title = QLabel("国家", self)
        self._province_title = QLabel("省份", self)
        self._city_title = QLabel("城市", self)

        grid = QGridLayout()
        grid.addWidget(QLabel("区域类型", self), 0, 0)
        grid.addWidget(self._region_type_combo, 0, 1, 1, 3)

        grid.addWidget(self._country_title, 1, 0)
        grid.addWidget(self._country_combo, 1, 1, 1, 3)
        grid.addWidget(self._province_title, 2, 0)
        grid.addWidget(self._province_combo, 2, 1, 1, 3)
        grid.addWidget(self._city_title, 3, 0)
        grid.addWidget(self._city_combo, 3, 1, 1, 3)
        grid.addWidget(self._level_title, 4, 0)
        grid.addWidget(self._level_label, 4, 1, 1, 3)

        self._bounds_hint_title = QLabel("边界范围", self)
        grid.addWidget(self._bounds_hint_title, 5, 0)
        grid.addWidget(self._bounds_hint_label, 5, 1, 1, 3)

        grid.addWidget(QLabel("West", self), 6, 0)
        grid.addWidget(self._west_spin, 6, 1)
        grid.addWidget(QLabel("East", self), 6, 2)
        grid.addWidget(self._east_spin, 6, 3)
        grid.addWidget(QLabel("South", self), 7, 0)
        grid.addWidget(self._south_spin, 7, 1)
        grid.addWidget(QLabel("North", self), 7, 2)
        grid.addWidget(self._north_spin, 7, 3)

        self._zoom_mode_row = QWidget(self)
        mode_row = QHBoxLayout(self._zoom_mode_row)
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.addWidget(self._zoom_recommended_radio)
        mode_row.addWidget(self._zoom_custom_radio)
        mode_row.addStretch(1)
        grid.addWidget(self._zoom_mode_title, 8, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self._zoom_mode_row, 8, 1, 1, 3)

        # 顺序照 Spec §1：策略 → 推荐 → Min/Max → 说明
        grid.addWidget(self._zoom_hint_title, 9, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self._zoom_hint_label, 9, 1, 1, 3)
        grid.addWidget(QLabel("Min Zoom", self), 10, 0)
        grid.addWidget(self._min_zoom_spin, 10, 1)
        grid.addWidget(QLabel("Max Zoom", self), 10, 2)
        grid.addWidget(self._max_zoom_spin, 10, 3)
        grid.addWidget(self._zoom_explain_title, 11, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self._zoom_explain_label, 11, 1, 1, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        self._area_group = QGroupBox("下载范围（WGS84 经纬度）", self)
        self._area_group.setLayout(grid)

        # 先填好三段下拉再挂信号：填的过程中会 clear/addItem，那一串
        # currentIndexChanged 没必要各跑一遍级联逻辑。
        self._reload_region_cascade()

        self._region_type_combo.currentIndexChanged.connect(self._on_region_changed)
        self._country_combo.currentIndexChanged.connect(self._on_cascade_changed)
        self._province_combo.currentIndexChanged.connect(self._on_cascade_changed)
        self._city_combo.currentIndexChanged.connect(self._on_cascade_changed)
        self._zoom_recommended_radio.toggled.connect(self._on_zoom_mode_toggled)
        # Zoom 一改就重算建议：用户把 Max Zoom 从 10 拉到 18 的**当下**就该看到
        # 那句提醒，而不是等他点了「计算」才发现。
        self._min_zoom_spin.valueChanged.connect(self._refresh_zoom_policy_hint)
        self._max_zoom_spin.valueChanged.connect(self._refresh_zoom_policy_hint)
        # 初始为矩形：行政区域那几行收起，经纬度框可编辑 —— 与 Slice ≤15 一模一样
        self._apply_region_mode()

    # ------------------------------------------------------------------ #
    # 区域（Slice 16，Spec §14 ~ §16）
    # ------------------------------------------------------------------ #
    def _reload_region_cascade(self) -> None:
        """把国家 / 省 / 市三段下拉全部重填一遍。

        数据来源是 :class:`~app.regions.RegionService`（Slice 18，Spec §15）——
        它背后是 ``resources/regions/index.json`` 那份**目录元数据**，因此将来加
        省份只需要往目录里补记录 + 放文件，界面这一行不用改。
        服务内部会在目录不可用时退回内置预设（并在日志里留一条 ERROR），
        所以这里不必再写兜底分支。

        边界文件缺失的条目**照样列出来**（tooltip 里带"未就绪"标记）：用户需要
        知道"这个功能存在，只是还缺一份数据"，而不是面对一个空下拉框猜为什么
        没反应。选中后提示行会说明该往哪个路径放什么文件（Spec §3）。
        """
        self._syncing = True
        try:
            self._fill_country_combo()
            self._fill_province_combo()
            self._fill_city_combo()
        finally:
            self._syncing = False

    def _fill_country_combo(self) -> None:
        """填「国家」下拉并选中当前那一个。"""
        self._country_combo.clear()
        countries = self._controller.list_countries()
        for entry in countries:
            self._add_entry(self._country_combo, entry)
        wanted = self._controller.country
        if wanted is not None:
            self._select_entry(self._country_combo, wanted.id)
        elif countries:
            # 目录里只有一个国家（当前就是这种）时，Qt 默认就选中第 0 项；
            # 但"默认选中"与"控制器知道选中了它"是两件事 —— 这里显式同步一次，
            # 免得出现"界面显示中国、控制器却认为什么都没选"。
            self._controller.select_country(countries[0].id)

    def _fill_province_combo(self) -> None:
        """填「省份」下拉（首个条目是"全部"，表示不限定省）。"""
        self._province_combo.clear()
        if self._controller.country is None:
            return
        self._province_combo.addItem(_ANY_PROVINCE_TEXT, None)
        for entry in self._controller.list_provinces():
            self._add_entry(self._province_combo, entry)
        wanted = self._controller.province
        if wanted is not None and self._select_entry(self._province_combo, wanted.id):
            return
        # 省被清掉（换了国家）或目录里没有这个 id → 落回"全部"
        self._province_combo.setCurrentIndex(0)
        self._controller.select_province(None)

    def _fill_city_combo(self) -> None:
        """填「城市」下拉（首个条目是"全部"；省下没有城市时只剩它）。"""
        self._city_combo.clear()
        province = self._controller.province
        if province is None:
            # 还没选省 → 城市无可选项。留着空下拉而不是禁用：禁用会让用户
            # 以为"这个功能坏了"，而"先选省份"是能自己推断出来的。
            self._city_combo.addItem(_ANY_CITY_TEXT, None)
            self._controller.select_city(None)
            return
        self._city_combo.addItem(_ANY_CITY_TEXT, None)
        for entry in self._controller.list_cities():
            self._add_entry(self._city_combo, entry)
        if self._city_combo.count() == 1:
            # 省下面确实没有城市数据（目录只到省级）：说清楚，别让用户空等
            self._city_combo.setItemText(0, _NO_CITY_TEXT)
        wanted = self._controller.city
        if wanted is not None and self._select_entry(self._city_combo, wanted.id):
            return
        self._city_combo.setCurrentIndex(0)
        self._controller.select_city(None)

    def _add_entry(self, combo: QComboBox, entry) -> None:
        """往下拉里加一条目录条目。

        "就绪了没有"问的是加载器（``RegionService.boundary_available``），
        不是条目自己 —— 目录条目只有元数据，文件在不在是磁盘事实（Slice 18）。

        未就绪的条目**照样列出来**，但要把这件事写在字面上与 tooltip 里：
        用户需要知道"这个功能存在，只是还缺一份数据"，而不是选完发现没反应。
        """
        available = self._controller.service.boundary_available(entry.id)
        text = entry.name if available else f"{entry.name}（边界文件未就绪）"
        combo.addItem(text, entry)
        tooltip = str(entry.geojson_path)
        if not available:
            tooltip = f"{tooltip}（边界文件未就绪）"
        combo.setItemData(combo.count() - 1, tooltip, Qt.ItemDataRole.ToolTipRole)

    @staticmethod
    def _select_entry(combo: QComboBox, region_id: str) -> bool:
        """在下拉里按 id 选中一项；找不到返回 ``False``。"""
        for index in range(combo.count()):
            entry = combo.itemData(index)
            if entry is not None and entry.id == region_id:
                combo.setCurrentIndex(index)
                return True
        return False

    def current_region_type(self) -> RegionType:
        """当前选中的区域类型（下拉框为空时退回矩形）。"""
        return self._region_type_combo.currentData() or RegionType.RECTANGLE

    def selected_region_preset(self) -> RegionPreset | None:
        """当前生效的行政区条目（市 → 省 → 国）；没有选中时返回 ``None``。

        返回的是 Slice 16 的**扁平**预设（``app.geo.region.RegionPreset``）：
        目录条目 → 扁平预设的转换只有一处（``RegionLoader.to_geo_preset``），
        是 Slice 18 定下的唯一方向，这里照用。
        """
        entry = self._controller.selected
        if entry is None:
            return None
        return self._controller.service.loader.to_geo_preset(entry)

    @property
    def region_error(self) -> str:
        """当前行政区域的加载错误（空串表示没问题）。"""
        return self._region_error

    @property
    def region_geometry(self):
        """已加载的边界几何；不是行政区域模式（或加载失败）时为 ``None``。

        给 :class:`~app.geo.RegionTileEstimator` 用：它在构造函数里要
        prepared 一份几何，而面板**已经读过一次盘**了。让预估再读一遍不仅浪费，
        还会造成"用户看到的区域"与"实际裁剪的区域"可能来自两次不同的读取。
        """
        document = self._region_document
        return None if document is None else document.geometry

    def current_bounds(self) -> GeoBounds:
        """当前四个经纬度框组成的范围。"""
        return GeoBounds(
            west=self._west_spin.value(),
            south=self._south_spin.value(),
            east=self._east_spin.value(),
            north=self._north_spin.value(),
        )

    def current_region(self) -> DownloadRegion | None:
        """当前表单对应的 :class:`~app.geo.DownloadRegion`（Spec §2）。

        **本方法永不抛异常**，返回 ``None`` 表示"拿不到可信的区域"：

        * 行政区域模式下边界文件加载失败（``preset``/``document`` 缺失，
          或几何非法）；
        * 矩形模式下四个经纬度框本身非法（``current_bounds()`` 会抛
          ``ValueError``）。

        调用方**必须**据此报错并停止，**不能**退回矩形：那会静默下一大片
        区域外的瓦片（Spec §16/§32）。至于"错误提示怎么写"，矩形下由
        边界校验负责（``west 必须小于 east``），行政区域下由引导文案负责
        —— 所以这里只吞异常，不在这里解释原因。
        """
        try:
            if self.current_region_type() is RegionType.RECTANGLE:
                return DownloadRegion.rectangle("自定义矩形", self.current_bounds())

            # 行政区域一律问控制器 —— 它背后是 RegionService.get_download_region
            # （Spec §3）。面板不自己拼 DownloadRegion：一旦拼了，"界面上的区域"
            # 与"目录说的区域"就有了两条构造路径。
            if self._region_document is None:
                return None
            # ``bounds`` 用**四个经纬度框里的值**，不用几何的原始外包矩形。
            # 这不是偷懒，是三点都指向同一个选择：
            #
            # 1. 输入框里显示的就是这个数 —— "界面显示的范围"与"实际下载依据"
            #    必须是同一个（Slice 16 定的红线）；
            # 2. 输入框只有 6 位小数，**与写进 MBTiles metadata 的精度一致**。
            #    拿原始几何（9 位小数）去枚举候选、却把 6 位的值写进 metadata，
            #    会让 Inspector 从 metadata 反推出来的瓦片范围**多出一圈**
            #    （边界恰好落在瓦片边上时），于是完整包被判成"缺失 10 张"、
            #    打包器拒绝出包 —— 一处纯粹由精度不一致造出来的假警报；
            # 3. Slice ≤18 本来就是这么传的（``bounds=self.current_bounds()``），
            #    改掉它就是行为变更，而这个 Slice 明确要求既有链路完全兼容。
            return self._controller.download_region(
                bounds=self.current_bounds(), document=self._region_document
            )
        except (GeoJSONError, RegionBoundaryMissing, ValueError):
            return None

    def current_region_error(self) -> str:
        """当前区域拿不到时给用户看的那句话（拿得到时是空串）。

        矩形模式下的非法范围由 ``GeoBounds`` 自己报（例如
        ``west 必须小于 east``），这里只负责行政区域那一路：用户看到的是
        "文件该放哪"，而不是 ``RegionBoundaryMissing`` 这种类名。
        """
        if self.current_region() is not None:
            return ""
        if self.current_region_type() is RegionType.RECTANGLE:
            try:
                self.current_bounds()
            except ValueError as exc:
                return str(exc)
            return ""
        # 面板自己的加载错误优先（它带着"该把文件放哪"的引导）；否则用控制器
        # 那一份（"还没选区域"、"目录里没有这个 id"）。
        return self._region_error or self._controller.error or "行政区域边界文件未就绪"

    @Slot()
    def _on_region_changed(self, *_args) -> None:
        """区域类型变化：切换界面形态并按需加载边界文件。

        参数写成可变参数：``currentIndexChanged`` 带一个 ``int``，而
        ``reload_region_boundary`` 那一路不带 —— 签名统一成 ``*_args``
        后不必为它们各写一个槽。
        """
        self._apply_region_mode()
        self.region_changed.emit()

    @Slot()
    def _on_cascade_changed(self, *_args) -> None:
        """国家 / 省份 / 城市下拉变化：级联刷新（Spec §2）。

        "选择国家 → 刷新省、清空市；选择省 → 刷新市"这三条规则写在这里而不是
        三个各自的槽里：它们本来就是一个动作的三个分支，拆开后**换国家却忘了
        清城市**这种漏改迟早会发生。

        ``_syncing`` 是必须的：下面会 ``clear()`` 再 ``addItem()``，
        每一次都会触发 ``currentIndexChanged`` 再回到本方法。
        """
        if self._syncing:
            return
        self._syncing = True
        try:
            country = self._country_combo.currentData()
            if country is None:
                self._controller.select_country(None)
            elif self._controller.country is None or self._controller.country.id != country.id:
                # 只有"真的换了国家"才重选：否则每次刷新都把省、市清掉一次。
                self._controller.select_country(country.id)
            else:
                # 同一国家内选省：先同步省，再按需同步市
                self._controller.select_province(
                    self._entry_id_of(self._province_combo)
                )
                if self._controller.province is not None:
                    self._controller.select_city(self._entry_id_of(self._city_combo))
            self._fill_province_combo()
            self._fill_city_combo()
        finally:
            self._syncing = False
        self._apply_region_mode()
        self.region_changed.emit()

    @staticmethod
    def _entry_id_of(combo: QComboBox) -> str | None:
        """取下拉当前项的 id（"全部"这一项返回 ``None``）。"""
        entry = combo.currentData()
        return None if entry is None else entry.id

    def _apply_region_mode(self) -> None:
        """按区域类型切换控件可见性与可编辑性（Spec §1 / §7）。"""
        is_polygon = self.current_region_type() is RegionType.POLYGON

        # 行政区域那一整块：矩形模式下全部收起。QGridLayout 在整行控件都隐藏时
        # 会把这行的高度压到 0，所以不会留下空白条 —— 矩形模式的界面与
        # Slice ≤18 **逐像素一致**（Spec §7 要求的"旧行为完全兼容"）。
        for widget in self._polygon_only_widgets():
            widget.setVisible(is_polygon)

        # 行政区域模式下经纬度只读：范围由边界文件决定。允许手改会造成
        # "界面显示的范围"与"实际裁剪依据（GeoJSON）"对不上 —— 而用户看不出这点。
        for spin in (
            self._west_spin,
            self._south_spin,
            self._east_spin,
            self._north_spin,
        ):
            spin.setReadOnly(is_polygon)

        if is_polygon:
            self._load_selected_region()
        else:
            self._region_document = None
            self._region_error = ""

        # 等级 / 模式可能刚变（矩形 ↔ 行政区域、换了一个行政区）→
        # 控件可编辑性与建议行都跟着变
        self._apply_zoom_mode()
        self._refresh_zoom_policy_hint()

    def _polygon_only_widgets(self) -> tuple[QWidget, ...]:
        """只在「行政区域」模式下出现的控件。

        每次重新拼一份而不是缓存成常量：本方法在 ``__init__`` 里就会被调用，
        那时部分控件才刚建好 —— 缓存一份"第一次调用时还不完整的列表"是最容易
        踩的空指针。
        """
        return (
            self._country_title,
            self._country_combo,
            self._province_title,
            self._province_combo,
            self._city_title,
            self._city_combo,
            self._level_title,
            self._level_label,
            self._bounds_hint_title,
            self._bounds_hint_label,
            self._zoom_mode_title,
            self._zoom_mode_row,
            self._zoom_explain_title,
            self._zoom_explain_label,
        )

    def _load_selected_region(self) -> None:
        """加载当前生效区域的边界，缓存几何并回填只读范围（Spec §8）。

        范围**一定**来自 GeoJSON 的 ``geometry.bounds``（经
        ``geometry_to_bounds``）。这里没有任何"顺手填一个 73/3/136/54"的分支：
        手填范围与真实边界对不上时，候选瓦片数会跟着错，而用户从界面上看不出来。
        """
        self._region_document = None
        self._region_error = ""
        entry = self._controller.selected
        if entry is None:
            self._region_error = self._controller.error or "还没有选择行政区域"
            self._bounds_hint_label.set_text(self._region_error)
            self._level_label.set_text("—")
            return

        self._level_label.set_text(
            f"{entry.level.display_name} · {self._controller.path_text}"
        )

        if not self._controller.boundary_available:
            # 边界文件缺失是**部署状态**，不是"退化成矩形"的理由。文案来自
            # 加载器的 missing_message（它写清了放哪、叫什么、要什么授权）。
            self._region_error = self._controller.missing_message()
            self._bounds_hint_label.set_text(f"边界不可用：{self._region_error}")
            return

        try:
            document = self._controller.load_document()
        except (GeoJSONError, RegionBoundaryMissing) as exc:
            self._region_error = str(exc)
            self._bounds_hint_label.set_text(f"边界不可用：{exc}")
            return

        try:
            bounds = geometry_to_bounds(document.geometry)
        except (GeoJSONError, ValueError) as exc:
            self._region_error = str(exc)
            self._bounds_hint_label.set_text(f"边界不可用：{exc}")
            return

        self._region_document = document
        # 回填四个（只读）框：用户能看见"这次按哪块范围算候选瓦片"
        self._west_spin.setValue(bounds.west)
        self._south_spin.setValue(bounds.south)
        self._east_spin.setValue(bounds.east)
        self._north_spin.setValue(bounds.north)
        self._bounds_hint_label.set_text(
            f"{bounds.west:.4f}, {bounds.south:.4f}, "
            f"{bounds.east:.4f}, {bounds.north:.4f}"
            "（候选范围，最终只下载与行政区边界相交的瓦片）"
        )

    def reload_region_boundary(self) -> None:
        """重新加载边界文件（用户刚按提示放好数据后手动刷新用）。"""
        self._reload_region_cascade()
        self._apply_region_mode()
        # 边界文件刚补上/刚换过 → 之前基于"边界不可用"下的结论（或更早的矩形
        # 结论）都已经不成立了，同样要作废重来。
        self.region_changed.emit()

    # ------------------------------------------------------------------ #
    # Zoom 建议（Slice 17，Spec §11）
    # ------------------------------------------------------------------ #
    def current_region_level(self) -> RegionLevel:
        """当前区域对应的规划等级（Spec §2）。

        等级来自**目录条目**而不是已加载的区域对象：边界文件还没放好时，用户
        依然应该看到"选中的是哪个区域、它建议多少 Zoom"。矩形模式没有选中任何
        行政区域 → :attr:`RegionLevel.CUSTOM`（不套用等级规则，Spec §7）。
        """
        if self.current_region_type() is not RegionType.POLYGON:
            return RegionLevel.CUSTOM
        return self._controller.level

    def current_region_id(self) -> str | None:
        """**当前生效**的行政区域 id（Spec §3）；矩形模式下是 ``None``。

        与 :meth:`current_region_level` 同一条规矩：级联下拉里那三个选择在
        矩形模式下**不参与下载**，所以对外一律报 ``None`` —— 否则任务记录与
        偏好里会写上一个"看起来在下这个区域"、实际却在下矩形的 id。
        """
        if self.current_region_type() is not RegionType.POLYGON:
            return None
        return self._controller.selected_region_id

    def current_zoom_recommendation(self) -> ZoomRecommendation | None:
        """推荐 / 评估结论（只读，不改控件值）。

        推荐模式下比的是**推荐值**（控件里显示的也正是它），自定义模式下比的是
        用户的值 —— 两种都由控制器按模式决定（Spec §4 / §5）。
        策略层抛异常时返回 ``None``（原因在 :attr:`region_error` 里那句话上）。
        """
        return self._controller.recommendation(
            self._min_zoom_spin.value(), self._max_zoom_spin.value()
        )

    # ------------------------------------------------------------------ #
    # 下载策略：推荐 / 自定义（Slice 19，Spec §4 / §5 / §6）
    # ------------------------------------------------------------------ #
    @property
    def zoom_mode(self) -> ZoomMode:
        """当前下载策略。"""
        return self._controller.zoom_mode

    def set_zoom_mode(self, mode: object) -> ZoomMode:
        """切换下载策略并同步控件，返回真正生效的那个。

        恢复界面偏好与验收脚本用。语义与点单选按钮完全一致：
        切到**推荐**会把当前区域的推荐区间填进 Min/Max（Spec §6），
        切到**自定义**则一个数字都不动 —— 保留的正是"切过来的那一刻"的值。
        """
        self._controller.set_zoom_mode(mode)
        self._apply_zoom_mode()
        self._refresh_zoom_policy_hint()
        return self._controller.zoom_mode

    @Slot(bool)
    def _on_zoom_mode_toggled(self, _checked: bool) -> None:
        """用户点了「推荐 Zoom」/「自定义 Zoom」。

        只有**推荐**那个单发的 ``toggled`` 就够了：两个按钮互斥，取消选中
        自定义必然意味着选中推荐，反之亦然。接两个信号会在一次点击里跑两遍。
        """
        if self._syncing:
            return
        mode = (
            ZoomMode.RECOMMENDED
            if self._zoom_recommended_radio.isChecked()
            else ZoomMode.CUSTOM
        )
        self._controller.set_zoom_mode(mode)
        self._apply_zoom_mode()
        self._refresh_zoom_policy_hint()

    def _apply_zoom_mode(self) -> None:
        """把「下载策略」落到控件上（Spec §4 / §6 / §7）。

        两条规则：

        * **推荐模式下 Min/Max 自动填成推荐值，且只读**。让它们可编辑会造出
          "界面显示 4~10、实际按别的算"这种对不上的状态；想改就先切到自定义，
          那是一次明确的动作（Spec §5）。
        * **没有行政等级时（矩形模式）什么都不做**：矩形不套用国家 / 省 / 市
          的推荐策略（Spec §7），既不下发推荐值，也不把控件设成只读。
        """
        self._syncing = True
        try:
            level = self.current_region_level()
            has_policy = (
                self.current_region_type() is RegionType.POLYGON
                and level.is_administrative
            )

            self._zoom_recommended_radio.setChecked(
                self._controller.zoom_mode is ZoomMode.RECOMMENDED
            )
            self._zoom_custom_radio.setChecked(
                self._controller.zoom_mode is ZoomMode.CUSTOM
            )

            if not has_policy:
                self._min_zoom_spin.setReadOnly(False)
                self._max_zoom_spin.setReadOnly(False)
                return

            recommended_mode = self._controller.zoom_mode is ZoomMode.RECOMMENDED
            self._min_zoom_spin.setReadOnly(recommended_mode)
            self._max_zoom_spin.setReadOnly(recommended_mode)
            if recommended_mode:
                low, high = self._controller.effective_range(
                    self._min_zoom_spin.value(), self._max_zoom_spin.value()
                )
                # 顺序有讲究：先设上界再设下界，避免中间态出现 min > max
                # （那会让"刷新建议"这一槽拿到一对非法值）。
                if high < low:
                    self._max_zoom_spin.setValue(high)
                    self._min_zoom_spin.setValue(low)
                else:
                    self._min_zoom_spin.setValue(low)
                    self._max_zoom_spin.setValue(high)
        finally:
            self._syncing = False

    @property
    def zoom_policy_text(self) -> str:
        """Zoom 建议行的当前文本（自定义等级时是空串）。"""
        return self._zoom_hint_label.text()

    @property
    def zoom_explain_text(self) -> str:
        """「推荐说明」那一行的文本（Spec §1）。"""
        return self._zoom_explain_label.text()

    @property
    def level_text(self) -> str:
        """「下载级别」那一行的文本（如 ``城市级 · 中国 / 浙江省 / 杭州市``）。"""
        return self._level_label.text()

    @Slot()
    def _refresh_zoom_policy_hint(self, *_args) -> None:
        """刷新 Zoom 建议行（Spec §11）。

        四条约束，都在这里体现：

        1. **自定义等级不显示**：矩形 / CUSTOM 区域没有"行政等级建议"可言，
           显示一行恒定的"全范围"只是噪音；
        2. **只给建议，不改用户输入**：本方法只读 spin 的值，没有任何 ``setValue``；
        3. **不阻塞**：超范围只是一句话，不弹窗、不禁用「计算」与「开始下载」；
        4. **不吞异常**：策略层拒绝这一对 Zoom（例如自定义模式下 Min > Max）时，
           显示那句人话而不是让异常从 Qt 槽里漏出去被静默吞掉。
        """
        if self.current_region_type() is not RegionType.POLYGON:
            self._hide_zoom_hint()
            return
        level = self.current_region_level()
        if level.is_custom:
            self._hide_zoom_hint()
            return

        recommendation = self.current_zoom_recommendation()
        if recommendation is None:
            # 策略层拒绝了这一对值 → 说清楚，而不是留着一个空行
            self._hide_zoom_hint()
            self._zoom_hint_title.setVisible(True)
            self._zoom_hint_label.setVisible(True)
            self._zoom_hint_label.set_text(self._controller.error or "Zoom 不可用")
            return

        self._zoom_hint_title.setVisible(True)
        self._zoom_hint_label.setVisible(True)
        self._zoom_hint_label.set_text("\n".join(recommendation.as_lines()))
        # 推荐说明只在真的建议时出现；文案来自策略层（界面不抄一份）
        if recommendation.reason:
            self._zoom_explain_title.setVisible(True)
            self._zoom_explain_label.setVisible(True)
            self._zoom_explain_label.set_text(recommendation.reason)
        else:  # pragma: no cover - 策略层的 description 不允许为空
            self._zoom_explain_title.setVisible(False)
            self._zoom_explain_label.setVisible(False)

    def _hide_zoom_hint(self) -> None:
        """整行收起建议与说明（矩形 / 自定义等级）。"""
        for widget in (
            self._zoom_hint_title,
            self._zoom_hint_label,
            self._zoom_explain_title,
            self._zoom_explain_label,
        ):
            widget.setVisible(False)
        self._zoom_hint_label.set_text("")
        self._zoom_explain_label.set_text("")

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
                region_type     RegionType     区域类型（矩形 / 多边形，Slice 16）
                region          DownloadRegion | None
                                下载区域；行政区域模式下边界文件加载失败时为
                                ``None``（调用方须报错并停止，不得退回矩形）
                region_id       str | None     当前生效的行政区域 id（市→省→国，
                                Slice 19）；没选是 ``None``
                region_level    RegionLevel    当前等级（矩形 / 未选 → CUSTOM）
                zoom_mode       ZoomMode       下载策略（推荐 / 自定义，Slice 19）

        注意 ``min_zoom`` / ``max_zoom`` 是**实际会拿去下载**的那一对：推荐模式下
        就是等级建议值（控件里显示的也一样），自定义模式下就是用户的值。调用方
        不需要再判断模式 —— 拿到什么就下什么。
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
            "region_type": self.current_region_type(),
            "region": self.current_region(),
            "region_id": self.current_region_id(),
            "region_level": self.current_region_level(),
            "zoom_mode": self._controller.zoom_mode,
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
        """设置缩放区间（**用户的**值），并把下载策略切到「自定义」。

        为什么这里要顺手切模式：本方法是"这就是我要用的两个数"的语义
        （恢复偏好、恢复任务记录、验收脚本都用它）。留在推荐模式下会造出
        一个自相矛盾的状态 —— 控件上写着 8~18、模式却声称"按推荐值"，
        而下一次区域一变，那对数字就会被推荐值**悄悄**顶掉。想按推荐值走
        就显式调 :meth:`set_zoom_mode`。

        传进来的值可能超出控件范围（例如来自旧版本的偏好或任务记录）：
        ``setValue`` 会自动 clamp，不会抛异常。
        """
        self._controller.set_zoom_mode(ZoomMode.CUSTOM)
        self._min_zoom_spin.setValue(int(min_zoom))
        self._max_zoom_spin.setValue(int(max_zoom))
        self._apply_zoom_mode()
        self._refresh_zoom_policy_hint()

    def set_output_dir(self, path: str | Path) -> None:
        """设置输出目录。"""
        self._output_edit.setText(str(path))

    # ------------------------------------------------------------------ #
    # 反向填充（Slice 5：从任务记录恢复表单）
    # ------------------------------------------------------------------ #
    def set_region_type(self, region_type: RegionType) -> bool:
        """切换区域类型；下拉框里没有这一项时返回 ``False``。

        与 :meth:`set_map_type` 同一约定：认不出来就保持原选项不动，
        换成用户没选过的类型更糟。
        """
        for index in range(self._region_type_combo.count()):
            if self._region_type_combo.itemData(index) is region_type:
                self._region_type_combo.setCurrentIndex(index)
                return True
        return False

    def set_region_id(self, region_id: str) -> bool:
        """按 ``region_id`` 选中行政区并**铺满三段级联**（回填任务记录 / 偏好用）。

        拆到"国家 / 省 / 市"三段这件事交给控制器（它走目录的祖先链），
        因此这里不必知道"杭州市挂在浙江省下"。

        找不到对应条目时**保持矩形**并把原因记进 :attr:`region_error`：
        一个本版本不认识的 ``region_id`` 不该让表单卡在半截状态。

        Raises:
            RegionBoundaryMissing: 不抛 —— 边界缺失只体现在 ``region_error``
                与提示行上，用户仍然可以看见"选了哪个区域、缺什么文件"。
        """
        wanted = (region_id or "").strip().lower()
        if not wanted:
            return False
        # 先切到「行政区域」再选：反过来的话，切换区域类型那一下会把刚选好的
        # 级联刷掉一次（`set_region_type` 触发的重绘会重建三段下拉）。
        if not self.set_region_type(RegionType.POLYGON):
            return False
        if not self._controller.select_region(wanted):
            self._region_error = (
                self._controller.error
                or f"本版本没有行政区域 {region_id!r} 的边界数据"
            )
            return False
        self._syncing = True
        try:
            self._fill_country_combo()
            self._fill_province_combo()
            self._fill_city_combo()
        finally:
            self._syncing = False
        self._apply_region_mode()
        return True

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

        # 区域（Slice 16）：老任务没有这三个字段 → 保持矩形，与它们当初的下载
        # 行为一致（Spec §29）。认不出的 region_id 也落回矩形并记下原因 ——
        # 表单显示的范围必须与实际会发生的下载行为一致。
        if RegionType.parse(getattr(task, "region_type", "")) is RegionType.POLYGON:
            if not self.set_region_id(str(getattr(task, "region_id", "") or "")):
                self.set_region_type(RegionType.RECTANGLE)

        # Zoom 落**最后**（Slice 19）。任务记录里那一对是当时**真的用来下载**的
        # 值，而恢复一个行政区域会把模式带到「推荐」——推荐模式下控件里是等级
        # 建议值，上面第 1362 行写进去的就会被顶掉。所以这里再落一次，并借
        # `set_zoom_range` 把模式切成「自定义」：这正是"我要的就是这两个数"。
        self.set_zoom_range(int(task.min_zoom), int(task.max_zoom))

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
        self,
        tile_count: int,
        size_text: str,
        output_path: str | Path | None = None,
        *,
        estimate=None,
    ) -> None:
        """展示瓦片计算成功的结果（Slice 7 起含 Zoom 与地图类型，Spec §3）。

        Args:
            tile_count: 预计瓦片数量。行政区域模式下应当是**区域内**的张数
                （``selected_tiles``），不是候选数。
            size_text: 已格式化好的容量文本（如 ``361.93 MB``），
                由调用方通过 ``app.geo.format_size`` 生成。
            output_path: 预计输出的 ``.mbtiles`` 完整路径；``None`` 时该行
                显示占位符（例如地图名称为空）。
            estimate: :class:`~app.geo.RegionTileEstimate`（Slice 16）。
                多边形区域时用来展示"候选 / 区域内 / 过滤 / 比例"与逐 Zoom 明细
                （Spec §17）；矩形（或 ``None``）时**不显示这些行** ——
                对矩形而言它们恒等于"候选 = 选中、过滤 = 0"，纯属噪音，
                且会让 Slice ≤15 的界面文案发生变化。

        请求量行（Slice 6）与 Zoom / 地图类型行（Slice 7）都由本面板按**当前**
        的输入算出来，因此不需要调用方多传参数：勾上中文注记后请求量翻倍这件事，
        与"面板上现在选的是什么"永远一致，不会出现两处算出不同数字。
        """
        map_type = self.current_map_type()
        with_labels = self.with_labels_selected()
        requests = tile_count * map_type.request_count_per_tile(with_labels)
        template = (
            UI_TEXT_ESTIMATED_REQUESTS_MERGED
            if with_labels
            else UI_TEXT_ESTIMATED_REQUESTS_SINGLE
        )

        is_polygon = estimate is not None and estimate.is_polygon
        level = self.current_region_level()
        recommendation = (
            self._controller.recommendation(
                self._min_zoom_spin.value(), self._max_zoom_spin.value()
            )
            if level.is_administrative
            else None
        )

        lines: list[str] = []
        if is_polygon:
            lines.extend(
                [
                    f"区域：{estimate.region_name}",
                    # 级别与推荐区间都取自**当前区域**（目录条目 → 策略表），
                    # 不在结果区里另算一份（Slice 19，Spec §9）。
                    f"级别：{level.display_name}",
                ]
            )
            if recommendation is not None:
                lines.append(f"推荐 Zoom：{recommendation.range_text}")
            lines.extend(
                [
                    f"候选瓦片：{estimate.candidate_tiles:,}",
                    f"区域内瓦片：{estimate.selected_tiles:,}",
                    f"过滤瓦片：{estimate.filtered_tiles:,}",
                    f"过滤比例：{estimate.filtered_percent_text}",
                ]
            )
        else:
            lines.append(f"预计瓦片：{tile_count:,}")
        lines.extend(
            [
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
            ]
        )
        if is_polygon and estimate.per_zoom:
            # Spec §17：让用户看到"每一级被裁掉多少"—— 只给一个总数的话，
            # 低 zoom 几乎没被裁、高 zoom 被裁掉九成这件事是看不出来的。
            # Spec §10（Slice 19）再补一列容量：逐级容量用**生产函数**算
            # （``estimate_size_bytes`` + ``format_size``），不在这里乘一个
            # 自己的常数 —— 那会变成第二个"每张瓦片多少字节"的真相。
            lines.append("Zoom 明细（候选 / 选中 / 过滤 / 预计容量）：")
            lines.extend(
                f"  z{item.zoom}  {item.candidate_tiles:,} / "
                f"{item.selected_tiles:,} / {item.filtered_tiles:,}  "
                f"{format_size(estimate_size_bytes(item.selected_tiles))}"
                for item in estimate.per_zoom
            )
        lines.append(
            f"输出文件：{output_path}" if output_path else "输出文件：（请填写地图名称）"
        )
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
    def region_type_combo(self) -> QComboBox:
        """暴露「区域类型」下拉框（Slice 16）。"""
        return self._region_type_combo

    @property
    def country_combo(self) -> QComboBox:
        """暴露「国家」下拉框（Slice 19）。"""
        return self._country_combo

    @property
    def province_combo(self) -> QComboBox:
        """暴露「省份」下拉框（Slice 19；第 0 项是"全部"）。"""
        return self._province_combo

    @property
    def city_combo(self) -> QComboBox:
        """暴露「城市」下拉框（Slice 19；第 0 项是"全部"）。"""
        return self._city_combo

    @property
    def region_combo(self) -> QComboBox:
        """暴露「国家」下拉框（Slice 19 起的三段级联中的第一段）。

        Slice 16/18 那个"一条下拉列全部行政区"的控件已经被级联取代：一条下拉
        里混着国家、省、市，用户看不出哪条挂在谁下面，也就没法做完 Spec §2
        的级联。名字保留是为了不让已有脚本一起改掉 —— 它现在指向国家那一段。
        """
        return self._country_combo

    @property
    def zoom_mode_buttons(self) -> tuple[QRadioButton, QRadioButton]:
        """暴露「推荐 Zoom」/「自定义 Zoom」两个单选（Slice 19）。"""
        return (self._zoom_recommended_radio, self._zoom_custom_radio)

    @property
    def zoom_explain_label(self) -> QLabel:
        """暴露「推荐说明」标签（Slice 19，Spec §1）。"""
        return self._zoom_explain_label

    @property
    def zoom_spins(self) -> tuple[QSpinBox, QSpinBox]:
        """暴露 Min / Max Zoom 两个输入框（``(min, max)``）。

        Slice 19 起这两个框会随「推荐 / 自定义」切换只读状态，而"只读"是**控件
        属性**、不是表单数据 —— ``get_form_data()`` 读不出来。验收脚本要断言
        "推荐模式下用户改不动"，就得拿得到控件本身。
        """
        return (self._min_zoom_spin, self._max_zoom_spin)

    @property
    def bounds_spins(self) -> tuple[QDoubleSpinBox, ...]:
        """暴露四个经纬度输入框（西 / 南 / 东 / 北）。

        行政区域模式下它们是只读的（范围由边界文件决定），同样是控件属性。
        """
        return (self._west_spin, self._south_spin, self._east_spin, self._north_spin)

    @property
    def level_label(self) -> QLabel:
        """暴露「下载级别」标签（Slice 19，Spec §1）。"""
        return self._level_label

    @property
    def region_service(self) -> RegionService:
        """暴露行政区目录服务（Slice 18）。

        界面本身只用它填下拉框；验收脚本与测试用同一份实例做层级断言，
        保证"界面看到的条目"与"目录说的层级"是同一个来源。
        """
        return self._region_service

    @property
    def region_controller(self) -> RegionSelectionController:
        """暴露级联选择控制器（Slice 19）。

        验收脚本用它拿到"当前生效区域 / 模式 / 推荐区间"—— 与界面同源，
        不必从控件上把信息再拼一遍。
        """
        return self._controller

    @property
    def bounds_hint_label(self) -> QLabel:
        """暴露行政区「边界范围」提示标签（Slice 16）。"""
        return self._bounds_hint_label

    @property
    def area_group(self) -> QGroupBox:
        """暴露「下载范围」分组（Slice 16：几何断言用）。"""
        return self._area_group

    @property
    def zoom_hint_label(self) -> QLabel:
        """暴露「Zoom 建议」标签（Slice 17：断言可见性与文案用）。"""
        return self._zoom_hint_label

    @property
    def zoom_hint_title(self) -> QLabel:
        """暴露「Zoom 建议」行标题（自定义等级时与标签一起隐藏）。"""
        return self._zoom_hint_title

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
