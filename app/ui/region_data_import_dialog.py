"""行政区数据管理对话框（Slice 26，Spec §18 / §19）。

一个窗口管两件事：**验证一份源数据能不能用**，以及**把它发布成生成
数据集**。发布走 :class:`~app.regions.importer.RegionImportService`
（临时目录 → 全量校验 → 原子发布），成功后由调用方负责 reload 行政区
目录 —— 本窗口只发一个 :attr:`dataset_imported` 信号，不自己去刷别的
面板（窗口之间互相伸手，迟早变成循环依赖）。

界面纪律（Spec §18 / §19）
-------------------------

- **字段名不假设**：四个下拉的候选值来自源数据里真实存在的 properties
  键（选好文件后自动填），用户再按实际情况选。父级字段允许"无" ——
  全国级条目本来就没有父级。
- **[验证] 与 [导入] 分开**：验证是 dry-run（一个字节都不写），
  FAIL 时导入按钮保持禁用。Spec §19 原话："FAIL 时禁止 Import"。
- **结果区永远显示统计**（总区域 / COUNTRY / PROVINCE / CITY /
  Geometry Invalid / Missing Parent / Duplicate ID + 结论），
  不只是"成功/失败" —— 用户要靠那几行判断"是不是选错了字段"。

关于耗时
--------

导入是同步执行的。行政区数据是几千个面几何的 MB 级文件，校验 + 写盘
是秒级的事；这里刻意不上后台线程 —— 一个"导入"按钮配一个进度条的
复杂度，换来的是几秒钟的界面冻结，不划算（与任务区窗口同一取舍）。
真到了分钟级（几十万 Feature），该改的是分批导入，不是加线程。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from app.regions.importer import (
    RegionImportConfig,
    RegionImportError,
    RegionImportResult,
    RegionImportService,
    peek_property_keys,
)
from app.regions.importer.region_exporter import PublishError

__all__ = ["RegionDataImportDialog"]

#: 源文件选择框的过滤器。
_SOURCE_FILTER = "GeoJSON (*.geojson *.json);;全部文件 (*)"

#: 字段下拉里"这个字段不存在"的占位项。
_NO_FIELD_TEXT = "（未选择）"

#: 父级字段下拉里"源数据没有父级列"的占位项。
_NO_PARENT_TEXT = "（无父级字段）"

#: 选好文件后优先尝试勾选的键名（按顺序取第一个命中的）。
_PREFERRED_ID_KEYS = ("code", "id", "adcode", "gbcode", "region_code")
_PREFERRED_NAME_KEYS = ("name", "title", "region_name")
_PREFERRED_LEVEL_KEYS = ("level", "region_level", "admin_level")
_PREFERRED_PARENT_KEYS = ("parent", "parentId", "parent_id", "parent_code", "parentCode")

#: 简化容差 SpinBox 的范围（度）。行政边界是 1e-2 ~ 1e-4 量级的东西，
#: 再大就把省界抹平了。
_SIMPLIFY_MIN = 0.0001
_SIMPLIFY_MAX = 1.0

#: 结果区最多列几条问题明细（再多就只看统计 + 前几条，避免一屏放不下）。
_MAX_ISSUE_LINES = 12


class RegionDataImportDialog(QDialog):
    """行政区数据管理（Slice 26，Spec §18）。

    Signals:
        dataset_imported: 数据集发布成功。主窗口接到后 reload 行政区目录
            并重填下载面板的级联下拉（Spec §17 / §24）。
    """

    dataset_imported = Signal()

    def __init__(
        self,
        service: RegionImportService,
        region_service=None,
        parent=None,
    ) -> None:
        """
        Args:
            service: 行政区导入服务（**必须**：本窗口的一切都经它）。
            region_service: 行政区目录服务（:class:`~app.regions.RegionService`）。
                发布成功后由本窗口调它的 ``reload()``；``None`` 时只发信号
                （测试或独立使用）。
            parent: 父窗口。
        """
        super().__init__(parent)
        self._service = service
        self._region_service = region_service
        self._last_result: RegionImportResult | None = None
        self._confirm_box: QMessageBox | None = None

        self.setWindowTitle("行政区数据管理")
        self.setMinimumWidth(680)

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_source_group())
        layout.addWidget(self._build_mapping_group())
        layout.addWidget(self._build_meta_group())
        layout.addWidget(self._build_action_row())
        layout.addWidget(self._build_result_group(), 1)

        self._import_button.setEnabled(False)
        self._set_result_text(
            "选好源数据后点「验证」；验证通过才能导入。", verdict="尚未验证"
        )

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build_source_group(self) -> QGroupBox:
        group = QGroupBox("源数据", self)
        form = QFormLayout(group)
        row = QHBoxLayout()
        self._source_edit = QLineEdit(group)
        self._source_edit.setPlaceholderText("选择行政区 GeoJSON（FeatureCollection）")
        self._browse_button = QPushButton("浏览…", group)
        self._browse_button.setToolTip(
            "只处理本机文件：本工具不在线抓取行政边界（Spec §29）"
        )
        self._browse_button.clicked.connect(self._on_browse)
        row.addWidget(self._source_edit, 1)
        row.addWidget(self._browse_button)
        form.addRow("源数据文件", row)
        self._source_edit.textChanged.connect(self._on_source_changed)
        return group

    def _build_mapping_group(self) -> QGroupBox:
        group = QGroupBox("字段映射", self)
        form = QFormLayout(group)
        self._field_combos: dict[str, QComboBox] = {}
        for label, key, tooltip in (
            ("ID 字段", "id", "取 region id 的属性（通常是行政区划 code）。"
                              "优先用数据里的稳定 code，不要用中文名称"),
            ("名称字段", "name", "取界面展示名"),
            ("级别字段", "level", "取行政等级：country/province/city、1/2/3、"
                                 "全国/省/市 都认"),
            ("父级字段", "parent", "取父级 id 的属性；全国级条目没有父级"),
        ):
            combo = QComboBox(group)
            combo.setMinimumWidth(220)
            combo.setToolTip(tooltip)
            form.addRow(label, combo)
            self._field_combos[key] = combo
        # 父级字段多一个"没有这一列"的选项。
        self._field_combos["parent"].addItem(_NO_PARENT_TEXT, "")
        return group

    def _build_meta_group(self) -> QGroupBox:
        group = QGroupBox("数据集信息", self)
        form = QFormLayout(group)

        self._version_edit = QLineEdit(group)
        self._version_edit.setPlaceholderText("例如：2026-10")
        self._version_edit.setToolTip(
            "数据集版本：写进每个边界文件、index.json 与 manifest，"
            "是「这批瓦片按哪版边界裁的」的唯一线索"
        )
        form.addRow("边界版本", self._version_edit)

        self._source_name_edit = QLineEdit(group)
        self._source_name_edit.setPlaceholderText("例如：×× 授权数据 2026 年版")
        form.addRow("来源", self._source_name_edit)

        self._license_edit = QLineEdit(group)
        self._license_edit.setPlaceholderText("例如：审图号 GS(2026)xxxx")
        self._license_edit.setToolTip(
            "中国国界、省界、市界属于法定地图内容，必须使用带审图号的"
            "合法授权数据（本工具不提供也不分发边界数据）"
        )
        form.addRow("授权说明", self._license_edit)

        simplify_row = QHBoxLayout()
        self._simplify_check = QCheckBox("启用几何简化", group)
        self._simplify_check.setToolTip(
            "默认关闭。简化会改变边界形状，可能影响「哪些瓦片落在区域内」"
            "（边界 Tile 漏下载）；只有确有必要时才启用"
        )
        self._simplify_spin = QDoubleSpinBox(group)
        self._simplify_spin.setDecimals(4)
        self._simplify_spin.setRange(_SIMPLIFY_MIN, _SIMPLIFY_MAX)
        self._simplify_spin.setValue(0.01)
        self._simplify_spin.setSuffix(" °")
        self._simplify_spin.setEnabled(False)
        self._simplify_check.toggled.connect(self._simplify_spin.setEnabled)
        simplify_row.addWidget(self._simplify_check)
        simplify_row.addWidget(self._simplify_spin)
        simplify_row.addStretch(1)
        form.addRow("几何简化", simplify_row)
        return group

    def _build_action_row(self) -> QGroupBox:
        group = QGroupBox(self)
        row = QHBoxLayout(group)
        self._validate_button = QPushButton("验证", group)
        self._validate_button.setToolTip(
            "dry-run：读源数据、校验几何与层级、统计，一个字节都不写"
        )
        self._validate_button.clicked.connect(self._on_validate)
        self._import_button = QPushButton("导入", group)
        self._import_button.setToolTip(
            "验证通过后发布：临时目录生成 → 全量校验 → 原子替换生成数据集"
        )
        self._import_button.clicked.connect(self._on_import)
        self._close_button = QPushButton("关闭", group)
        self._close_button.clicked.connect(self.close)
        row.addWidget(self._validate_button)
        row.addWidget(self._import_button)
        row.addStretch(1)
        row.addWidget(self._close_button)
        return group

    def _build_result_group(self) -> QGroupBox:
        group = QGroupBox("验证结果", self)
        layout = QVBoxLayout(group)
        self._verdict_label = QLabel("结果：尚未验证", group)
        self._result_edit = QPlainTextEdit(group)
        self._result_edit.setReadOnly(True)
        self._result_edit.setMinimumHeight(180)
        layout.addWidget(self._verdict_label)
        layout.addWidget(self._result_edit, 1)
        return group

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    @Slot()
    def _on_browse(self) -> None:
        chosen, _filter = QFileDialog.getOpenFileName(
            self, "选择行政区数据 GeoJSON", "", _SOURCE_FILTER
        )
        if chosen:
            self._source_edit.setText(chosen)

    @Slot()
    def _on_source_changed(self) -> None:
        """源文件变了 → 按文件里真实的 properties 键重填字段下拉。"""
        path = self._source_edit.text().strip()
        keys = peek_property_keys(path) if path else ()
        for key, combo in self._field_combos.items():
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            if key == "parent":
                combo.addItem(_NO_PARENT_TEXT, "")
            else:
                combo.addItem(_NO_FIELD_TEXT, "")
            for name in keys:
                combo.addItem(name, name)
            combo.blockSignals(False)
            # 尽量保留用户上次的选择；保不住就按常用键名猜一个。
            wanted = current if current in keys else None
            if wanted is None:
                preferred = {
                    "id": _PREFERRED_ID_KEYS,
                    "name": _PREFERRED_NAME_KEYS,
                    "level": _PREFERRED_LEVEL_KEYS,
                    "parent": _PREFERRED_PARENT_KEYS,
                }[key]
                for name in preferred:
                    if name in keys:
                        wanted = name
                        break
            if wanted is not None:
                index = combo.findData(wanted)
                if index >= 0:
                    combo.setCurrentIndex(index)
        # 文件都还没选好，验证结果没有意义。
        self._import_button.setEnabled(False)
        self._last_result = None

    # ------------------------------------------------------------------ #
    # 验证 / 导入
    # ------------------------------------------------------------------ #
    def _build_config(self) -> RegionImportConfig | None:
        """从界面收集配置；缺东西时在结果区说清缺什么并返回 ``None``。"""
        source = self._source_edit.text().strip()
        if not source:
            self._set_result_text("请先选择源数据文件。", verdict="FAIL")
            return None
        if not Path(source).is_file():
            self._set_result_text(f"源数据文件不存在：{source}", verdict="FAIL")
            return None

        fields = {}
        missing: list[str] = []
        for key, label in (
            ("id", "ID 字段"),
            ("name", "名称字段"),
            ("level", "级别字段"),
        ):
            value = str(self._field_combos[key].currentData() or "").strip()
            if not value:
                missing.append(label)
            fields[key] = value
        if missing:
            self._set_result_text(
                "还没选字段映射：" + "、".join(missing), verdict="FAIL"
            )
            return None
        parent = str(self._field_combos["parent"].currentData() or "").strip() or None

        version = self._version_edit.text().strip()
        if not version:
            self._set_result_text("请先填写边界版本（数据集版本号）。", verdict="FAIL")
            return None

        try:
            return RegionImportConfig(
                source_file=Path(source),
                id_field=fields["id"],
                name_field=fields["name"],
                level_field=fields["level"],
                parent_field=parent,
                output_root=self._service.output_root,
                boundary_version=version,
                source_name=self._source_name_edit.text().strip(),
                source_license=self._license_edit.text().strip(),
                simplify_tolerance=(
                    self._simplify_spin.value() if self._simplify_check.isChecked() else None
                ),
            )
        except ValueError as exc:
            self._set_result_text(f"导入配置不成立：{exc}", verdict="FAIL")
            return None

    @Slot()
    def _on_validate(self) -> None:
        config = self._build_config()
        if config is None:
            return
        try:
            result = self._service.validate(config)
        except RegionImportError as exc:
            self._last_result = None
            self._import_button.setEnabled(False)
            self._set_result_text(str(exc), verdict="FAIL")
            return
        self._last_result = result
        self._import_button.setEnabled(result.ok)
        self._render_result(result)

    @Slot()
    def _on_import(self) -> None:
        config = self._build_config()
        if config is None:
            return
        try:
            report = self._service.import_dataset(config)
        except (RegionImportError, PublishError) as exc:
            self._import_button.setEnabled(False)
            self._set_result_text(f"导入失败：{exc}", verdict="FAIL")
            self._ask(QMessageBox.Icon.Critical, "行政区数据导入", f"导入失败：{exc}")
            return

        if not report.ok:
            self._import_button.setEnabled(False)
            self._render_result(report.result)
            self._ask(
                QMessageBox.Icon.Critical,
                "行政区数据导入",
                "数据未通过验证，没有发布。\n\n"
                + "\n".join(item.describe() for item in report.result.errors[:_MAX_ISSUE_LINES]),
            )
            return

        self._render_result(report.result)
        #  reload：发布成功后让目录服务重读，国家 / 省 / 市立即可见
        # （Spec §17：无需重启程序）。
        if self._region_service is not None:
            try:
                self._region_service.reload()
            except Exception as exc:  # noqa: BLE001 - reload 失败不该堵住信号
                self._result_edit.appendPlainText(
                    f"（目录重读失败，重启程序后可见：{exc}）"
                )
        self.dataset_imported.emit()
        self._ask(
            QMessageBox.Icon.Information,
            "行政区数据导入",
            f"已发布到：\n{report.output_root}\n\n{report.manifest.summary_text()}",
        )

    # ------------------------------------------------------------------ #
    # 提示框（非模态，与任务区 / 计划页同一约定）
    # ------------------------------------------------------------------ #
    def message_box(self) -> QMessageBox | None:
        """上一个弹出的消息框（用例点按钮用；与任务区窗口同一约定）。"""
        return self._confirm_box

    def _ask(self, icon: QMessageBox.Icon, title: str, text: str) -> None:
        """弹一个**非模态**提示框（不用 ``exec()``，否则自动化里只能靠另一条线程点）。

        导入是同步的、动作即时生效，所以"确认/关闭"必须能由调用方真的点一下。
        """
        self._close_message_box()
        box = QMessageBox(icon, title, text, parent=self)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.finished.connect(lambda _result: self._forget_message_box(box))
        self._confirm_box = box
        box.show()

    def _close_message_box(self) -> None:
        box = self._confirm_box
        if box is not None:
            box.close()

    def _forget_message_box(self, box: QMessageBox) -> None:
        if self._confirm_box is box:
            self._confirm_box = None

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """关窗口时把挂着的提示框一起收掉。"""
        self._close_message_box()
        self._confirm_box = None
        super().closeEvent(event)

    # ------------------------------------------------------------------ #
    # 结果区
    # ------------------------------------------------------------------ #
    def _render_result(self, result: RegionImportResult) -> None:
        """Spec §19 的统计行 + 结论 + 问题明细。"""
        verdict_text = {
            "pass": "PASS",
            "warning": "WARNING",
            "fail": "FAIL",
        }[result.verdict.value]
        lines = result.summary_lines()
        details = [item.describe() for item in result.issues[:_MAX_ISSUE_LINES]]
        if len(result.issues) > _MAX_ISSUE_LINES:
            details.append(f"……另有 {len(result.issues) - _MAX_ISSUE_LINES} 条，见日志")
        self._set_result_text(
            "\n".join(lines + (["", "问题明细："] + details if details else [])),
            verdict=verdict_text,
        )

    def _set_result_text(self, text: str, *, verdict: str) -> None:
        """写结果区；结论行按 PASS / WARNING / FAIL 换色。"""
        self._verdict_label.setText(f"结果：{verdict}")
        color = {"PASS": "#1a7f37", "WARNING": "#9a6700", "FAIL": "#cf222e"}.get(
            verdict, "#57606a"
        )
        self._verdict_label.setStyleSheet(f"color: {color}; font-weight: bold;")
        self._result_edit.setPlainText(text)
