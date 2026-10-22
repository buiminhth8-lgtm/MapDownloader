"""行政区数据管理窗口的"点一下"（Slice 26，Spec §18 / §19）。

与任务区窗口测试同一条道理：Qt 槽里抛的异常会被静默吞掉，界面表现成
"点了没反应"，只有"点一下 + 断言结果区文本 / 按钮状态 / 信号"的用例能守
"这个按钮真的接上了"。断言都站在界面这一侧读，不直接调服务。

窗口是同步导入（Spec 里明说不上后台线程），所以点「验证」/「导入」后
结果立即可见。提示框是非模态的（与任务区 / 计划页同一约定），用例可以
真的点掉它。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtTest import QSignalSpy

from app.regions.importer import RegionImportService
from app.ui.region_data_import_dialog import RegionDataImportDialog

from tests.conftest import REGION_IMPORT_FIXTURE_ROOT


@pytest.fixture
def sample_source() -> Path:
    """sample 夹具的源文件路径（窗口是真读文件的）。"""
    return REGION_IMPORT_FIXTURE_ROOT / "sample.geojson"


@pytest.fixture
def dialog(qapp, work_dir: Path) -> RegionDataImportDialog:
    """一个发布到独立临时目录的窗口（不碰真实 resources/regions）。"""
    service = RegionImportService(output_root=work_dir / "generated")
    window = RegionDataImportDialog(service)
    window.show()
    yield window
    window.close()


def _fill_source(dialog: RegionDataImportDialog, source: Path) -> None:
    """直接把路径填进源文件框（不点「浏览」——那是 QFileDialog，自动化里要 mock）。"""
    dialog._source_edit.setText(str(source))


def _pick_field(dialog: RegionDataImportDialog, key: str, value: str) -> None:
    combo = dialog._field_combos[key]
    index = combo.findData(value)
    assert index >= 0, f"字段下拉里没有 {value!r}（key={key}）"
    combo.setCurrentIndex(index)


def _dismiss_box(dialog: RegionDataImportDialog) -> None:
    """点掉当前挂着的提示框（非模态，可同步点掉）。"""
    box = dialog.message_box()
    if box is not None:
        box.button(box.standardButtons()).click()


def test_field_dropdowns_are_filled_from_source_properties(
    dialog: RegionDataImportDialog, sample_source: Path
) -> None:
    """选好文件后，四个字段下拉的候选值来自源数据真实 properties 键（Spec §2）。"""
    _fill_source(dialog, sample_source)

    id_combo = dialog._field_combos["id"]
    available = [id_combo.itemData(i) for i in range(id_combo.count())]
    assert "code" in available
    assert "name" in available
    assert "level" in available
    assert "parent" in available
    # 不假设字段名：猜了常用键，但候选值必须来自文件本身。
    assert dialog._field_combos["id"].currentData() == "code"
    assert dialog._field_combos["name"].currentData() == "name"
    assert dialog._field_combos["level"].currentData() == "level"
    assert dialog._field_combos["parent"].currentData() == "parent"


def test_validate_reports_spec19_statistics_and_enables_import(
    dialog: RegionDataImportDialog, sample_source: Path
) -> None:
    """验证后结果区显示 Spec §19 的统计行，导入按钮变可用。"""
    _fill_source(dialog, sample_source)
    dialog._version_edit.setText("2026-10-test")

    assert not dialog._import_button.isEnabled()  # 验证前禁用

    dialog._validate_button.click()

    text = dialog._result_edit.toPlainText()
    assert "总区域：5" in text
    assert "COUNTRY：1" in text
    assert "PROVINCE：2" in text
    assert "CITY：2" in text
    assert "PASS" in dialog._verdict_label.text()
    assert dialog._import_button.isEnabled()


def test_validate_fail_disables_import(
    dialog: RegionDataImportDialog
) -> None:
    """数据不合格（重复 id）→ FAIL，导入按钮保持禁用（Spec §19）。"""
    _fill_source(dialog, REGION_IMPORT_FIXTURE_ROOT / "duplicate_id.geojson")
    dialog._version_edit.setText("t")

    dialog._validate_button.click()

    assert "Duplicate ID" in dialog._result_edit.toPlainText()
    assert "FAIL" in dialog._verdict_label.text()
    assert not dialog._import_button.isEnabled()


def test_import_publishes_and_emits_signal(
    dialog: RegionDataImportDialog, work_dir: Path
) -> None:
    """验证通过后点导入 → 发布成功、发出 dataset_imported 信号、生成数据集落地。"""
    _fill_source(dialog, REGION_IMPORT_FIXTURE_ROOT / "sample.geojson")
    dialog._version_edit.setText("2026-10-test")
    dialog._validate_button.click()
    assert dialog._import_button.isEnabled()

    spy = QSignalSpy(dialog.dataset_imported)
    dialog._import_button.click()
    _dismiss_box(dialog)

    assert spy.count() == 1  # 信号发了一次
    generated = work_dir / "generated"
    assert (generated / "index.json").is_file()
    assert (generated / "country" / "test_country.geojson").is_file()
    assert (generated / "manifest.json").is_file()


def test_simplify_defaults_off(
    dialog: RegionDataImportDialog, sample_source: Path
) -> None:
    """几何简化默认关闭（Spec §6：不默认 simplify 行政边界）。"""
    assert not dialog._simplify_check.isChecked()
    assert not dialog._simplify_spin.isEnabled()


def test_missing_source_reports_fail_without_crashing(
    dialog: RegionDataImportDialog
) -> None:
    """没选源文件就点验证 → 结果区提示，不抛异常。"""
    dialog._validate_button.click()
    assert "FAIL" in dialog._verdict_label.text()
    assert not dialog._import_button.isEnabled()


def test_parent_field_may_be_absent(
    dialog: RegionDataImportDialog, work_dir: Path
) -> None:
    """全国级数据没有父级列时，「父级字段」允许选「（无父级字段）」。"""
    # 造一份只有全国 + 省（省不带 parent 键）的数据。
    src = work_dir / "no_parent.geojson"
    src.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"code": "c", "name": "国", "level": "country"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[110, 30], [112, 30], [112, 32], [110, 32], [110, 30]]],
                        },
                    },
                    {
                        "type": "Feature",
                        "properties": {"code": "p", "name": "省", "level": "province"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[110, 30], [111, 30], [111, 31], [110, 31], [110, 30]]],
                        },
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _fill_source(dialog, src)

    parent_combo = dialog._field_combos["parent"]
    values = [parent_combo.itemData(i) for i in range(parent_combo.count())]
    # 没有 parent 键 → 「（无父级字段）」占位项仍在，但候选里**不出现** parent。
    assert "" in values
    assert "parent" not in values
