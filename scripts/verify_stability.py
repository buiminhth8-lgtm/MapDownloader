"""统一离线稳定性验收入口；无需 Token 或 Android 真机。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    environment = os.environ.copy()
    environment.update(QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    phases = [
        ("故障回归与界面接线", ["-m", "pytest", "-q", "-p", "no:cacheprovider",
            "tests/test_transfer_protection.py", "tests/test_download_coordination.py",
            "tests/test_tile_repair.py", "tests/test_tile_repair_ui.py",
            "tests/test_adaptive_download.py",
            "tests/test_storage_budget.py", "tests/test_storage_budget_ui.py",
            "tests/test_zoom_coverage.py", "tests/test_zoom_coverage_ui.py",
            "tests/test_storage_optimization.py", "tests/test_storage_optimization_ui.py",
            "tests/test_ui_smoke.py::test_choose_downloaded_map_builds_and_selects_package",
            "tests/test_ui_smoke.py::test_choose_package_file_uses_its_directory",
            "tests/test_ui_smoke.py::test_choose_map_file_cancel_preserves_selected_package",
            "tests/test_ui_smoke.py::test_choose_downloading_map_does_not_start_packaging",
            "tests/test_ui_smoke.py::test_package_ready_during_transfer_keeps_current_package",
            "tests/test_environment_check.py::test_adb_missing_is_optional_not_blocker"]),
        ("MBTiles 存储优化", ["scripts/verify_storage_optimization.py"]),
        ("多级区域单包覆盖", ["scripts/verify_zoom_coverage.py"]),
        ("容量预算与预测", ["scripts/verify_storage_budget.py"]),
        ("损坏瓦片闭环", ["scripts/verify_repair.py"]),
        ("普通下载打包链路", ["scripts/e2e_smoke_test.py"]),
        ("分级链路与当前 Zoom 策略", ["scripts/hierarchical_map_e2e.py"]),
        ("文档引用", ["scripts/check_docs.py"]),
    ]
    for title, arguments in phases:
        print(f"\n=== {title} ===", flush=True)
        completed = subprocess.run([sys.executable, "-B", *arguments], cwd=ROOT, env=environment)
        if completed.returncode:
            print(f"[FAIL] {title}，退出码 {completed.returncode}", flush=True)
            return completed.returncode
    print("\n[PASS] 离线稳定性验收全部通过；Android 故障恢复仍需真机验收。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
