"""任务区域对话框（Slice 22，Spec §19）。

一个窗口管三件事：**导入航线 → 看走廊有多大 / 要下多少张 → 加进下载计划**。

放在这里的理由：Slice 16/18 的"下载区域"下拉框是"矩形 / 行政区"两选一，
而任务区的输入是一份文件（航线）加一个数字（走廊半径），塞进那个下拉框会
把"选哪块地"和"怎么得到这块地"混成一件事。所以它有自己的入口，产出的东西
仍然是**一块地** —— 一样进下载计划、一样由计划统一生成任务。

刻意**不做地图编辑器**（Spec §19 明令）：本工具是"离线图打包器"，
不是 GIS 编辑器。航线在别的软件里画好、导出 GeoJSON，这里只负责"按米缓冲、
算清代价、进计划"。自己画一个半吊子编辑器只会让用户既画不准也导不出。

Zoom 一栏沿用了"下载区域"那一套的两条纪律（Spec §16）：

- **默认就是推荐模式**，推荐值来自 :class:`~app.planning.ZoomStrategyService`
  （MISSION → Z15~18），界面里**没有任何 Zoom 常量**；
- **自定义模式只提示、绝不改用户输入**：超出推荐区间的后果写在提示行里，
  那两个数字原样进计划（要不要接受是用户的决定）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)

from app.config.settings import (
    DEFAULT_MISSION_BUFFER_METERS,
    MAX_SUPPORTED_ZOOM,
    MIN_SUPPORTED_ZOOM,
    MISSION_BUFFER_METERS_MAX,
    MISSION_BUFFER_METERS_MIN,
    UI_TEXT_MISSION_DIALOG_TITLE,
    UI_TEXT_MISSION_EMPTY,
    UI_TEXT_MISSION_NAME_REQUIRED,
    UI_TEXT_MISSION_ROUTE_REQUIRED,
)
from app.geo.geojson_loader import GeoJSONError
from app.mission.mission_region_service import MissionRegionService
from app.mission.models import (
    MISSION_REGION_LEVEL,
    MissionError,
    MissionRegion,
    MissionRegionReferenced,
    km_to_meters,
    meters_to_km,
)
from app.planning.zoom_strategy import ZoomPlanEstimate
from app.ui.widgets import WrappingLabel, apply_secondary_text

__all__ = ["MissionRegionDialog"]


#: 路径下拉里"没有航线文件"时输入框里的占位文案
_ROUTE_PLACEHOLDER = "选择 GeoJSON 航线文件（LineString）"

#: 结果区还没算之前的占位文案
_RESULT_PLACEHOLDER = "尚未计算：选好航线与半径，点「生成任务区」再点「计算」"

#: 未接入计划服务时的说明
_NO_PLAN_SERVICE = "本窗口没有拿到下载计划服务，无法加入计划"

#: 航线文件选择框的过滤器
_ROUTE_FILTER = "GeoJSON 航线 (*.geojson *.json);;全部文件 (*)"


class MissionRegionDialog(QDialog):
    """任务区域（航线 Buffer）对话框。

    Signals:
        plan_changed: 计划被改动（加了一项 / 删了一项），主窗口据此刷新摘要。
        regions_changed: 任务区列表变了（新建 / 删除）。
    """

    plan_changed = Signal()
    regions_changed = Signal()

    def __init__(
        self,
        mission_service: MissionRegionService,
        plan_service=None,
        parent=None,
    ) -> None:
        """
        Args:
            mission_service: 任务区服务（**必须**：本窗口的一切都经它）。
            plan_service: 下载计划服务；``None`` 时「加入下载计划」不可用
                （按钮仍在，点了会说清原因 —— 灰掉一个按钮而不解释，
                用户只会以为程序坏了）。
            parent: 父窗口。
        """
        super().__init__(parent)
        self._mission = mission_service
        self._plan_service = plan_service
        self._last_estimate: ZoomPlanEstimate | None = None
        self._confirm_box: QMessageBox | None = None
        #: 最近一次"加入 / 更新计划"的结果（提示行第一行）
        self._plan_message: str = ""

        self.setWindowTitle(UI_TEXT_MISSION_DIALOG_TITLE)
        self.setMinimumWidth(760)

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_input_group())
        layout.addWidget(self._build_zoom_group())
        layout.addWidget(self._build_action_row())
        layout.addWidget(self._build_result_group(), 1)
        layout.addWidget(self._build_close_box())

        self.refresh()
        # 构造时**必须自己套用一次**当前的 Zoom 模式。
        #
        # ``_on_zoom_mode_changed`` 挂在 radio 的 ``toggled`` 上，而"推荐"是
        # 构造时就勾好的 —— 那个信号不会为初始状态发一次。漏掉这一步的后果
        # 很具体：窗口打开时两个数字框显示的是 ``QSpinBox`` 的缺省值（1/1）
        # 且可编辑，而下面一行就写着"推荐 Zoom：Z15~18" —— 界面上摆着一个
        # 与自己的说明相矛盾的数。这类"初始状态没套用"的问题只有真开一次
        # 窗口才看得见。
        self._on_zoom_mode_changed()

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build_input_group(self) -> QGroupBox:
        """名称 / 航线文件 / 缓冲半径 + 已有任务区。"""
        group = QGroupBox("航线与走廊", self)
        outer = QVBoxLayout(group)

        form = QFormLayout()
        self._name_edit = QLineEdit(group)
        self._name_edit.setPlaceholderText("例如：西湖巡检航线")
        form.addRow("任务区名称", self._name_edit)

        route_row = QHBoxLayout()
        self._route_edit = QLineEdit(group)
        self._route_edit.setPlaceholderText(_ROUTE_PLACEHOLDER)
        self._browse_button = QPushButton("浏览…", group)
        self._browse_button.setToolTip("选择导出的 GeoJSON 航线（只认 LineString）")
        self._browse_button.clicked.connect(self._on_browse)
        route_row.addWidget(self._route_edit, 1)
        route_row.addWidget(self._browse_button)
        form.addRow("航线文件", route_row)

        self._buffer_spin = QDoubleSpinBox(group)
        self._buffer_spin.setDecimals(2)
        self._buffer_spin.setSingleStep(0.5)
        self._buffer_spin.setRange(
            meters_to_km(MISSION_BUFFER_METERS_MIN),
            meters_to_km(MISSION_BUFFER_METERS_MAX),
        )
        self._buffer_spin.setValue(meters_to_km(DEFAULT_MISSION_BUFFER_METERS))
        self._buffer_spin.setSuffix(" km")
        self._buffer_spin.setToolTip(
            "走廊半径：航线两侧各扩这么多米。半径按**米**在地面上量，"
            "不受纬度影响（Spec §7）"
        )
        form.addRow("Buffer 半径", self._buffer_spin)
        outer.addLayout(form)

        self._create_button = QPushButton("生成任务区", group)
        self._create_button.setToolTip(
            "读航线 → 投影到米制平面 → 按半径缓冲 → 存成任务区（三个 GeoJSON / 元数据文件）"
        )
        self._create_button.clicked.connect(self._on_create)

        self._region_combo = QComboBox(group)
        self._region_combo.setMinimumWidth(280)
        self._delete_button = QPushButton("删除任务区", group)
        self._delete_button.setToolTip(
            "删掉这个任务区的几何与元数据。被下载任务 / 计划引用时会被拒绝 —— "
            "删了它，那些任务就再也说不清自己要下哪块地"
        )
        self._delete_button.clicked.connect(self._on_delete)

        existing_row = QHBoxLayout()
        existing_row.addWidget(QLabel("已有任务区", group))
        existing_row.addWidget(self._region_combo, 1)
        existing_row.addWidget(self._delete_button)
        outer.addLayout(existing_row)

        button_row = QHBoxLayout()
        button_row.addWidget(self._create_button)
        button_row.addStretch(1)
        outer.addLayout(button_row)
        return group

    def _build_zoom_group(self) -> QGroupBox:
        """推荐 / 自定义 Zoom（与「下载区域」那一套同一套纪律）。"""
        group = QGroupBox("Zoom", self)
        layout = QVBoxLayout(group)

        self._zoom_recommended_radio = QRadioButton("使用推荐 Zoom", group)
        self._zoom_custom_radio = QRadioButton("自定义 Zoom", group)
        self._zoom_recommended_radio.setChecked(True)
        self._zoom_recommended_radio.toggled.connect(self._on_zoom_mode_changed)

        mode_row = QHBoxLayout()
        mode_row.addWidget(self._zoom_recommended_radio)
        mode_row.addWidget(self._zoom_custom_radio)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        self._min_zoom_spin = QSpinBox(group)
        self._max_zoom_spin = QSpinBox(group)
        # 范围用**全程序同一份**常量，不写 1 与 18：这两个数在"下载区域"那一栏
        # 也是同一个来源，两处写死迟早会分家（一边能填 20、另一边只能填 18），
        # 而"能填多少"与"算不算得出来"是同一件事。
        for spin in (self._min_zoom_spin, self._max_zoom_spin):
            spin.setRange(MIN_SUPPORTED_ZOOM, MAX_SUPPORTED_ZOOM)

        zoom_row = QHBoxLayout()
        zoom_row.addWidget(QLabel("Min", group))
        zoom_row.addWidget(self._min_zoom_spin)
        zoom_row.addWidget(QLabel("Max", group))
        zoom_row.addWidget(self._max_zoom_spin)
        zoom_row.addStretch(1)
        layout.addLayout(zoom_row)

        self._zoom_hint_label = WrappingLabel("", group)
        apply_secondary_text(self._zoom_hint_label)
        layout.addWidget(self._zoom_hint_label)
        return group

    def _build_action_row(self) -> QGroupBox:
        """[计算] 与 [加入下载计划]。"""
        group = QGroupBox("下一步", self)
        layout = QVBoxLayout(group)

        self._estimate_button = QPushButton("计算", group)
        self._estimate_button.setToolTip(
            "按当前 Zoom 区间统计走廊内的真实瓦片数（逐张判相交，不是按矩形估）"
        )
        self._estimate_button.clicked.connect(self._on_estimate)

        self._add_button = QPushButton("加入下载计划", group)
        self._add_button.setToolTip(
            "把当前任务区加进下载计划（与「下载区域」里的行政区走同一个计划）"
        )
        self._add_button.clicked.connect(self._on_add_to_plan)

        row = QHBoxLayout()
        row.addWidget(self._estimate_button)
        row.addWidget(self._add_button)
        row.addStretch(1)
        layout.addLayout(row)

        self._plan_hint_label = WrappingLabel("", group)
        apply_secondary_text(self._plan_hint_label)
        layout.addWidget(self._plan_hint_label)
        return group

    def _build_result_group(self) -> QGroupBox:
        """结果区：航点 / 半径 / 范围 / 张数 / 容量 / Zoom 明细。"""
        group = QGroupBox("任务区预估", self)
        layout = QVBoxLayout(group)
        self._result_label = WrappingLabel(_RESULT_PLACEHOLDER, self)
        layout.addWidget(self._result_label)
        layout.addStretch(1)
        return group

    def _build_close_box(self) -> QDialogButtonBox:
        """只有「关闭」：本窗口的每个动作都是即时生效的，没有"提交"这一步。"""
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        box.rejected.connect(self.close)
        return box

    # ------------------------------------------------------------------ #
    # 只读访问（用例与验收脚本按这些入口操作界面）
    # ------------------------------------------------------------------ #
    @property
    def mission_service(self) -> MissionRegionService:
        """本窗口使用的任务区服务。"""
        return self._mission

    def name_edit(self) -> QLineEdit:
        """任务区名称输入框。"""
        return self._name_edit

    def route_edit(self) -> QLineEdit:
        """航线文件输入框。"""
        return self._route_edit

    def buffer_spin(self) -> QDoubleSpinBox:
        """Buffer 半径输入框（单位 km）。"""
        return self._buffer_spin

    def region_combo(self) -> QComboBox:
        """已有任务区下拉。"""
        return self._region_combo

    def zoom_spins(self) -> tuple[QSpinBox, QSpinBox]:
        """``(min_zoom, max_zoom)`` 两个输入框。"""
        return self._min_zoom_spin, self._max_zoom_spin

    def zoom_mode(self) -> str:
        """当前 Zoom 模式（``"recommended"`` / ``"custom"``）。"""
        return "recommended" if self._zoom_recommended_radio.isChecked() else "custom"

    def buttons(self) -> dict[str, QPushButton]:
        """四个动作按钮（``create`` / ``estimate`` / ``add`` / ``delete`` / ``browse``）。"""
        return {
            "create": self._create_button,
            "estimate": self._estimate_button,
            "add": self._add_button,
            "delete": self._delete_button,
            "browse": self._browse_button,
        }

    def result_lines(self) -> list[str]:
        """结果区的原始文本行（``WrappingLabel.text()`` 返回未折行的原文）。"""
        return str(self._result_label.text()).splitlines()

    def zoom_hint_text(self) -> str:
        """Zoom 提示行文本（推荐区间 / 超出提醒）。"""
        return str(self._zoom_hint_label.text())

    def plan_hint_text(self) -> str:
        """计划提示行文本。"""
        return str(self._plan_hint_label.text())

    def last_estimate(self) -> ZoomPlanEstimate | None:
        """最近一次「计算」的结果（没算过时为 ``None``）。"""
        return self._last_estimate

    def current_region(self) -> MissionRegion | None:
        """当前选中的任务区（没有时 ``None``）。"""
        mission_id = self._region_combo.currentData(Qt.ItemDataRole.UserRole)
        if not mission_id:
            return None
        try:
            return self._mission.get(str(mission_id))
        except MissionError:
            return None

    def message_box(self) -> QMessageBox | None:
        """上一个弹出的消息框（用例点按钮用；与下载计划页同一约定）。"""
        return self._confirm_box

    # ------------------------------------------------------------------ #
    # 刷新
    # ------------------------------------------------------------------ #
    @Slot()
    def refresh(self) -> None:
        """重读任务区列表（不重算任何数字）。"""
        previous = self._region_combo.currentData(Qt.ItemDataRole.UserRole)
        self._region_combo.clear()
        regions = self._mission.list_all()
        if not regions:
            self._region_combo.addItem(UI_TEXT_MISSION_EMPTY, None)
        else:
            for region in regions:
                self._region_combo.addItem(region.describe(), region.id)
        if previous:
            index = self._region_combo.findData(previous)
            if index >= 0:
                self._region_combo.setCurrentIndex(index)
        self._refresh_zoom_hint()
        self._refresh_plan_hint()
        self._refresh_buttons()

    def select_region(self, mission_id: str) -> bool:
        """选中一个任务区（用例与"刚创建完"都走这里）。

        Returns:
            选中了返回 ``True``；列表里没有这个 id 返回 ``False``。
        """
        index = self._region_combo.findData(mission_id)
        if index < 0:
            return False
        self._region_combo.setCurrentIndex(index)
        self._last_estimate = None
        return True

    # ------------------------------------------------------------------ #
    # 槽
    # ------------------------------------------------------------------ #
    @Slot()
    def _on_browse(self) -> None:
        """选航线文件（只负责填输入框，不读文件内容）。"""
        start = self._route_edit.text().strip()
        directory = str(Path(start).parent) if start else ""
        chosen, _filter = QFileDialog.getOpenFileName(
            self, "选择航线文件（GeoJSON LineString）", directory, _ROUTE_FILTER
        )
        if chosen:
            self._route_edit.setText(chosen)

    @Slot()
    def _on_create(self) -> None:
        """生成任务区（真读文件、真投影、真落盘）。"""
        name = self._name_edit.text().strip()
        if not name:
            self._warn(UI_TEXT_MISSION_NAME_REQUIRED, "任务区名称")
            return
        route = self._route_edit.text().strip()
        if not route:
            self._warn(UI_TEXT_MISSION_ROUTE_REQUIRED, "航线文件")
            return

        radius_m = km_to_meters(self._buffer_spin.value())
        try:
            region = self._mission.create(
                name, route_path=route, buffer_meters=radius_m
            )
        except (MissionError, GeoJSONError, OSError, ValueError) as exc:
            self._warn(str(exc), "生成任务区失败")
            return

        self._last_estimate = None
        self.refresh()
        self.select_region(region.id)
        self._result_label.set_text(
            "\n".join(
                [
                    f"已生成：{region.describe()}",
                    f"几何版本：{region.geometry_version}",
                    f"范围：{region.bounds_text()}",
                    f"文件：{region.geojson_path}",
                    "",
                    "点「计算」查看这个走廊的真实瓦片数与容量",
                ]
            )
        )
        self.regions_changed.emit()

    @Slot()
    def _on_estimate(self) -> None:
        """计算瓦片数（推荐模式用策略区间；自定义模式用用户那两个数）。"""
        region = self.current_region()
        if region is None:
            self._warn("请先选择或生成一个任务区", "计算")
            return
        try:
            if self.zoom_mode() == "recommended":
                estimate = self._mission.estimate(region.id)
            else:
                estimate = self._mission.estimate(
                    region.id,
                    min_zoom=self._min_zoom_spin.value(),
                    max_zoom=self._max_zoom_spin.value(),
                )
        except (MissionError, GeoJSONError, ValueError) as exc:
            self._warn(str(exc), "计算失败")
            return

        self._last_estimate = estimate
        self._refresh_zoom_hint()
        self._result_label.set_text("\n".join(self._estimate_lines(region, estimate)))

    @Slot()
    def _on_add_to_plan(self) -> None:
        """加进下载计划（与「下载区域」里的行政区走同一个计划）。"""
        region = self.current_region()
        if region is None:
            self._warn("请先选择或生成一个任务区", "加入下载计划")
            return
        if self._plan_service is None:
            self._warn(_NO_PLAN_SERVICE, "加入下载计划")
            return
        custom = self.zoom_mode() == "custom"
        try:
            result = self._plan_service.add_region(
                region.id,
                use_recommended_zoom=not custom,
                min_zoom=self._min_zoom_spin.value() if custom else None,
                max_zoom=self._max_zoom_spin.value() if custom else None,
            )
        except (MissionError, GeoJSONError, ValueError, OSError) as exc:
            self._warn(str(exc), "加入下载计划失败")
            return

        verb = "已加入" if result.created else "已更新"
        self._plan_message = f"{verb}下载计划：{result.item.describe()}"
        self._refresh_plan_hint()
        self.plan_changed.emit()

    @Slot()
    def _on_delete(self) -> None:
        """删除任务区（先确认；被引用时由服务拒绝并说清谁在引用）。"""
        region = self.current_region()
        if region is None:
            self._warn("请先选择要删除的任务区", "删除任务区")
            return
        try:
            referenced = self._mission.references(region.id)
        except MissionError as exc:
            self._warn(str(exc), "删除任务区")
            return
        if referenced:
            # 引用保护先在**界面**这一层说清"删不掉"，而不是等点完「是」再报错：
            # 让用户先做一次无意义的确认，是把"谁在引用"这条信息藏在一层点击之后。
            self._warn(
                f"该任务区域正在被下载计划引用（{'、'.join(referenced)}），无法删除",
                "删除任务区",
            )
            return
        self._ask(
            QMessageBox.Icon.Question,
            "删除任务区",
            f"确定删除任务区「{region.name}」？"
            f"\n（{region.waypoint_count} 个航点 / 半径 {region.buffer_km:.2f} km 的几何"
            "与元数据都会从磁盘上删掉，不可恢复）",
            on_yes=lambda: self._delete_region(region),
        )

    def _delete_region(self, region: MissionRegion) -> None:
        """真的删（由确认框的「是」触发）。"""
        try:
            self._mission.delete(region.id)
        except MissionRegionReferenced as exc:
            self._warn(str(exc), "删除任务区")
            return
        except MissionError as exc:
            self._warn(str(exc), "删除任务区失败")
            return

        self._last_estimate = None
        self.refresh()
        self._result_label.set_text(f"已删除任务区：{region.name}")
        self.regions_changed.emit()

    @Slot()
    def _on_zoom_mode_changed(self) -> None:
        """切换推荐 / 自定义：只改可用性与提示，**绝不改写用户已经填的数字**。"""
        recommended = self._zoom_recommended_radio.isChecked()
        for spin in (self._min_zoom_spin, self._max_zoom_spin):
            spin.setReadOnly(recommended)
        if recommended:
            self._fill_recommended_zoom()
        self._refresh_zoom_hint()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _fill_recommended_zoom(self) -> None:
        """把推荐区间填进两个输入框（推荐模式专用）。"""
        recommendation = self._mission.strategy.recommend(MISSION_REGION_LEVEL)
        self._min_zoom_spin.setValue(recommendation.min_zoom)
        self._max_zoom_spin.setValue(recommendation.max_zoom)

    def _refresh_zoom_hint(self) -> None:
        """Zoom 提示行（推荐区间来自策略表；自定义只提示不改值）。"""
        if self.zoom_mode() == "recommended":
            recommendation = self._mission.strategy.recommend(MISSION_REGION_LEVEL)
            self._zoom_hint_label.set_text(
                "\n".join(recommendation.as_lines())
                + "\n（推荐区间来自 Zoom 策略表；要改请切到「自定义 Zoom」）"
            )
            return
        recommendation = self._mission.strategy.evaluate(
            MISSION_REGION_LEVEL, self._min_zoom_spin.value(), self._max_zoom_spin.value()
        )
        self._zoom_hint_label.set_text("\n".join(recommendation.as_lines()))

    def _refresh_plan_hint(self) -> None:
        """计划提示行：最近一次操作结果 + 计划当前规模。"""
        if self._plan_service is None:
            self._plan_hint_label.set_text(_NO_PLAN_SERVICE)
            return
        lines = [self._plan_message] if self._plan_message else []
        lines.append(self._plan_service.summary_text())
        self._plan_hint_label.set_text("\n".join(lines))

    def _refresh_buttons(self) -> None:
        """按钮可用性。

        「计算 / 加入计划 / 删除」**刻意不禁用**（与其他面板的"点不动"结论相反，
        这里是有意的）：它们能不能用取决于"有没有选中任务区"，而用户刚打开窗口时
        列表可能就是空的。灰掉三个按钮却不说明原因，用户只会以为程序坏了；
        点了会说清"请先选择或生成一个任务区"，然后用户就知道下一步该干什么。
        """
        return

    def _estimate_lines(
        self, region: MissionRegion, estimate: ZoomPlanEstimate
    ) -> list[str]:
        """结果区的行（Spec §20：候选矩形 vs 多边形选中 vs 过滤掉，三者都要在）。"""
        inner = estimate.estimate
        lines = [
            f"任务区：{region.describe()}",
            f"航点：{region.waypoint_count} 个",
            f"Buffer 半径：{region.buffer_km:.2f} km（{region.buffer_meters:.0f} 米）",
            f"几何版本：{region.geometry_version}",
            f"多边形范围：{region.bounds_text()}",
        ]
        lines.extend(estimate.recommendation.as_lines())
        lines.extend(
            [
                f"候选瓦片（外包矩形）：{inner.candidate_tiles:,}",
                f"区域内瓦片（多边形）：{inner.selected_tiles:,}",
                f"过滤掉：{inner.filtered_tiles:,}（{inner.filtered_percent_text}）",
                f"预计容量：{inner.size_text}",
            ]
        )
        if inner.per_zoom:
            lines.append("Zoom 明细：")
            lines.extend(
                f"  z{item.zoom}  候选 {item.candidate_tiles:,}  "
                f"选中 {item.selected_tiles:,}  过滤 {item.filtered_tiles:,}"
                for item in inner.per_zoom
            )
        return lines

    def _ask(
        self,
        icon: QMessageBox.Icon,
        title: str,
        text: str,
        *,
        on_yes=None,
    ) -> QMessageBox:
        """弹一个**非模态**提示框（与 ``MainWindow`` / 计划页同一套做法）。

        不用 ``exec()``：那会让本窗口卡在一个必须点掉的框上，于是"点掉它"这件事
        在自动化里只能靠另一条线程 —— 一个只为了显示信息的框不该有这种代价。
        本窗口的每个动作都是即时生效的，因此"确认"必须能由调用方
        （用例 / 验收脚本）真的点一下。
        """
        self._close_message_box()
        box = QMessageBox(icon, title, text, parent=self)
        if on_yes is not None:
            box.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            box.setDefaultButton(QMessageBox.StandardButton.No)
            box.button(QMessageBox.StandardButton.Yes).clicked.connect(
                lambda _checked=False: on_yes()
            )
        else:
            box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.finished.connect(lambda _result: self._forget_message_box(box))
        self._confirm_box = box
        box.show()
        return box

    def _warn(self, text: str, title: str) -> QMessageBox:
        """弹一条说明（同时写进结果区 —— 弹窗关掉之后还能看到）。"""
        self._result_label.set_text(f"{title}：{text}")
        return self._ask(QMessageBox.Icon.Warning, title, text)

    def _close_message_box(self) -> None:
        box = self._confirm_box
        if box is not None:
            box.close()

    def _forget_message_box(self, box: QMessageBox) -> None:
        if self._confirm_box is box:
            self._confirm_box = None

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """关窗口时把挂着的提示框一起收掉（否则它会孤零零留在屏幕上）。"""
        self._close_message_box()
        self._confirm_box = None
        super().closeEvent(event)
