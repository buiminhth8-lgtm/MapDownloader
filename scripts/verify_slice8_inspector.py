"""Slice 8 真实平台验收脚本：MBTiles 检查与诊断工具。

**为什么不能只跑 pytest**：``pytest`` 跑在 Qt ``offscreen`` 平台上 —— 那里没有真实
字体、没有 DPI 缩放、没有系统深浅色主题，``QDialog`` 也不是真的窗口。而"检查"
这个功能的价值**恰恰全在展示上**：状态那一行的颜色在浅色/深色主题下读不读得清、
表格里的长路径会不会把列撑爆、进度条有没有动 —— 这些在 offscreen 里一个都看不出来。

本脚本是那次"实机看一眼"的可重复版本：

- 真的起 windows 平台插件、真的 ``show()`` 检查对话框；
- 真的点 ``[快速检查]`` / ``[深度检查]``，真的等后台线程跑完；
- 每种结论（PASS / WARNING / FAIL）各截一张图，落在 ``docs/images/slice8_*.png``；
- 全程**只碰临时目录**，不碰你的真实地图与任务记录；
- 附带断言"检查不跑在 Qt 主线程"与"对话框里没有 SQL"。

用法::

    python scripts/verify_slice8_inspector.py
    python scripts/verify_slice8_inspector.py --offscreen   # 无显示环境（CI）
    python scripts/verify_slice8_inspector.py --no-shots    # 只跑检查，不截图
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DOCS = PROJECT_ROOT / "docs" / "images"

#: 连续两个及以上 ASCII 字母 → 判为"漏改的英文按钮"。
_ENGLISH_BUTTON = re.compile(r"[A-Za-z]{2,}")

#: 当前 ``QApplication``；``_shot`` 需要它来空转事件循环（截图前让布局收敛）。
_APP = None


# --------------------------------------------------------------------------- #
# 检查结果收集
# --------------------------------------------------------------------------- #
class Checker:
    """收集检查结果，最后统一汇报并以退出码表达结论。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  [OK]   {label}")
        else:
            self.failures.append(f"{label}{f' — {detail}' if detail else ''}")
            print(f"  [FAIL] {label}{f' — {detail}' if detail else ''}")
        return bool(ok)

    def section(self, title: str) -> None:
        print(f"\n=== {title} ===")


def _pump(app, times: int = 8) -> None:
    for _ in range(times):
        app.processEvents()


def _wait_until(app, predicate, timeout_s: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _start_and_wait(dialog, app, *, deep: bool, timeout_ms: int = 60_000):
    """点检查 → 抢在 pump 之前取线程 → 等它跑完 → 把队列信号送回主线程。

    **必须抢在 ``processEvents()`` 之前取线程**：小文件几十毫秒就跑完，
    ``pump`` 正好把"已完成"的队列信号投递到主线程，``_on_completed`` 里已经把
    ``self._worker`` 置成 ``None``，之后再问只会拿到 ``None``。

    等待也不能只看 ``is_running``：线程结束与信号投递之间有一个窗口，
    那时 ``is_running`` 已经是 ``False`` 但 ``result`` 还是 ``None``——
    用 ``is_running`` 当条件会在那个窗口里立刻返回，然后断言到一个空的界面。
    所以既 ``wait()`` 线程，又空转事件循环直到 ``result`` 真的到位。

    Returns:
        ``(worker, settled, was_running)``；``settled`` 表示结论已回主线程，
        ``was_running`` 是**启动瞬间**观察到的运行态（按钮矩阵那一条要用它，
        等线程跑完再问只会是 ``False``）。
    """
    (dialog.start_deep_check if deep else dialog.start_quick_check)()
    worker = dialog.inspection_worker
    was_running = dialog.is_running
    if worker is not None:
        worker.wait(timeout_ms)
    settled = _wait_until(
        app, lambda: dialog.result is not None and not dialog.is_running, timeout_s=10
    )
    return worker, settled, was_running


def _shot(widget, name: str, enabled: bool) -> None:
    """把任意控件存成 ``docs/images/<name>``。

    ``grab()`` **不会**触发布局重算：``resize()`` 之后直接截，截到的是上一轮
    尺寸下的排版（表现是"大小 28.00 KB"被裁成"大小 28"）。所以这里先
    ``activate()`` 再空转几轮事件循环，让布局真正收敛。
    """
    if not enabled:
        return
    layout = widget.layout()
    if layout is not None:
        layout.activate()
    if _APP is not None:
        _pump(_APP, 12)
    DOCS.mkdir(parents=True, exist_ok=True)
    target = DOCS / name
    if widget.grab().save(str(target)):
        print(f"  [shot] {target.relative_to(PROJECT_ROOT)}")


# --------------------------------------------------------------------------- #
# 造几种"要诊断的文件"
# --------------------------------------------------------------------------- #
def _real_png(size: int = 16) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (size, size), (30, 120, 200, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


#: "PNG 文件头 + 垃圾正文"：只看 magic bytes 会放行，真解码必炸。
BROKEN_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"garbage" * 16

BOUNDS = (116.0, 39.0, 116.5, 39.5)
#: ``BOUNDS`` 在 z12 下的瓦片区间是 7×6；用它才造得出"缺几张"。
ZOOM = 12


def _build_good(path: Path) -> Path:
    """覆盖完整的正常地图包（结论应为 PASS）。"""
    from app.geo import GeoBounds, calculate_tile_range
    from app.mbtiles import MBTilesMetadata, MBTilesWriter

    path.parent.mkdir(parents=True, exist_ok=True)
    bounds = GeoBounds(*BOUNDS)
    writer = MBTilesWriter(path)
    writer.open()
    writer.write_metadata(MBTilesMetadata.for_bounds(bounds, ZOOM, ZOOM, name="beijing"))
    tile_range = calculate_tile_range(bounds, ZOOM)
    data = _real_png()
    for x in range(tile_range.min_x, tile_range.max_x + 1):
        for y in range(tile_range.min_y, tile_range.max_y + 1):
            writer.write_tile(ZOOM, x, y, data)
    writer.close()
    return path


def _build_partial(path: Path) -> tuple[Path, int]:
    """缺几张瓦片的地图包（结论应为 WARNING，并给出缺失坐标）。"""
    from app.geo import GeoBounds, calculate_tile_range
    from app.mbtiles import MBTilesMetadata, MBTilesWriter

    path.parent.mkdir(parents=True, exist_ok=True)
    bounds = GeoBounds(*BOUNDS)
    writer = MBTilesWriter(path)
    writer.open()
    writer.write_metadata(MBTilesMetadata.for_bounds(bounds, ZOOM, ZOOM, name="partial"))
    tile_range = calculate_tile_range(bounds, ZOOM)
    data = _real_png()
    missing = 0
    for x in range(tile_range.min_x, tile_range.max_x + 1):
        for y in range(tile_range.min_y, tile_range.max_y + 1):
            # 故意空出前 3 张
            if missing < 3 and x == tile_range.min_x:
                missing += 1
                continue
            writer.write_tile(ZOOM, x, y, data)
    writer.close()
    return path, missing


def _build_corrupt(path: Path) -> Path:
    """含无法解码瓦片的地图包（结论应为 FAIL）。"""
    from app.geo import GeoBounds, calculate_tile_range
    from app.mbtiles import MBTilesMetadata, MBTilesWriter

    path.parent.mkdir(parents=True, exist_ok=True)
    bounds = GeoBounds(*BOUNDS)
    writer = MBTilesWriter(path)
    writer.open()
    writer.write_metadata(MBTilesMetadata.for_bounds(bounds, ZOOM, ZOOM, name="corrupt"))
    tile_range = calculate_tile_range(bounds, ZOOM)
    good = _real_png()
    first = True
    for x in range(tile_range.min_x, tile_range.max_x + 1):
        for y in range(tile_range.min_y, tile_range.max_y + 1):
            payload = BROKEN_PNG if first else good
            first = False
            writer.write_tile(ZOOM, x, y, payload)
    writer.close()
    return path


# --------------------------------------------------------------------------- #
# 场景
# --------------------------------------------------------------------------- #
def scene_service_only(checker: Checker, work_dir: Path) -> None:
    """服务层：三档结论各造一份，确认判定与报告都对。"""
    from app.services import MBTilesInspectionService

    checker.section("场景 A：服务层三档结论")
    service = MBTilesInspectionService()

    good = service.inspect(_build_good(work_dir / "good.mbtiles"), check_coverage=True)
    checker.check(good.status == "PASS", "正常文件判 PASS", f"实际 {good.status}")
    checker.check(good.valid is True, "正常文件 valid=True")
    checker.check(good.missing_tiles == 0, "正常文件无缺失")
    checker.check(good.warnings == [], "正常文件没有 WARNING", str(good.warnings))
    checker.check(
        any("全部可正常解码" in note for note in good.notices),
        "抽样结论落在 notices（不影响 PASS）",
    )

    partial_path, missing = _build_partial(work_dir / "partial.mbtiles")
    partial = service.inspect(partial_path, check_coverage=True)
    checker.check(partial.status == "WARNING", "缺瓦片判 WARNING", f"实际 {partial.status}")
    checker.check(partial.valid is True, "缺瓦片仍 valid=True（不判 FAIL）")
    checker.check(
        partial.missing_tiles == missing,
        f"缺失张数 == {missing}",
        f"实际 {partial.missing_tiles}",
    )
    checker.check(
        len(partial.missing_details) == missing
        and all("/" in detail for detail in partial.missing_details),
        "缺失明细是 XYZ 的 z/x/y",
        str(partial.missing_details),
    )

    corrupt = service.inspect(_build_corrupt(work_dir / "corrupt.mbtiles"), check_coverage=True)
    checker.check(corrupt.status == "FAIL", "含坏图片判 FAIL", f"实际 {corrupt.status}")
    checker.check(corrupt.corrupt_tiles >= 1, "坏图片被识别为损坏瓦片")

    broken = work_dir / "broken.mbtiles"
    broken.write_bytes(b"definitely not a sqlite database" * 8)
    result = service.inspect(broken)
    checker.check(result.status == "FAIL", "非 SQLite 文件判 FAIL")
    checker.check(bool(result.errors), "非 SQLite 文件给出错误原因")

    report = service.export_report(partial)
    text = report.read_text(encoding="utf-8")
    checker.check(report.name == "partial.mbtiles.report.txt", "报告落在源文件旁边")
    checker.check("WARNING" in text, "报告里带结论")
    checker.check("结论" in text, "报告是 UTF-8 中文")
    checker.check(
        all(detail in text for detail in partial.missing_details),
        "报告列出了缺失瓦片明细",
    )


def scene_deep_scan(checker: Checker, work_dir: Path) -> None:
    """深度检查：全量扫描必须抓到**全部**坏瓦片，而不是抽样撞上的那几张。"""
    from app.services import MBTilesInspectionService

    checker.section("场景 B：深度检查（全量）")
    service = MBTilesInspectionService()
    path = _build_corrupt(work_dir / "deep.mbtiles")

    quick = service.inspect(path, check_coverage=True, sample_size=1, deep_scan=False)
    deep = service.inspect(path, check_coverage=True, deep_scan=True)

    checker.check(deep.deep_scan is True, "深度检查标记为已跑")
    checker.check(deep.deep_scan_complete is True, "深度检查跑完了")
    checker.check(
        deep.deep_scanned_tiles == deep.tile_count,
        "深度检查扫过了库里每一张",
        f"{deep.deep_scanned_tiles}/{deep.tile_count}",
    )
    checker.check(deep.corrupt_tiles >= 1, "深度检查找到损坏瓦片")
    checker.check(
        deep.corrupt_tiles >= quick.corrupt_tiles,
        "深度检查的损坏数不少于抽样",
        f"deep={deep.corrupt_tiles} quick={quick.corrupt_tiles}",
    )
    checker.check(deep.sqlite_check is not None, "SQLite 自检有结果")


def scene_dialog(app, window, checker: Checker, work_dir: Path, shots: bool) -> None:
    """对话框：真的打开、真的点、真的等后台线程。"""
    from PySide6.QtCore import QThread

    checker.section("场景 C：检查对话框（真窗口）")

    partial_path, missing = _build_partial(work_dir / "dialog-partial.mbtiles")
    window.open_mbtiles_inspector(str(partial_path))
    _pump(app)
    dialog = window._inspector_dialog
    checker.check(dialog is not None, "对话框已创建")
    if dialog is None:
        return

    # 高度用对话框**自己**的合适尺寸：强行 resize 到比 sizeHint 还矮，Qt 只能
    # 压缩 QFormLayout 的行高，截出来的图上「基本信息」的九行会叠在一起 ——
    # 那是截图脚本的错，不是界面的错，但看图的人分不出来。宽度则给得比
    # sizeHint 宽一点：``QLineEdit`` 的 sizeHint 很窄，照它排版看到的
    # 路径是截断的，不像用户实际看到的样子。
    dialog.show()
    dialog.adjustSize()
    dialog.resize(880, dialog.height())
    _pump(app)
    checker.check(
        dialog.width() >= 700,
        "对话框宽度足够（路径框不被挤扁）",
        f"{dialog.width()}x{dialog.height()}",
    )
    _shot(dialog, "slice8_inspector_idle.png", shots)

    # --- 快速检查 ---
    worker, settled, was_running = _start_and_wait(dialog, app, deep=False)
    checker.check(worker is not None, "[快速检查] 起了后台线程")
    if worker is not None:
        checker.check(
            worker is not QThread.currentThread(),
            "检查跑在后台线程（没占 Qt 主线程）",
        )
    checker.check(was_running, "运行中按钮矩阵已切换")
    checker.check(dialog.is_running is False, "线程结束后回到空闲")
    checker.check(settled, "结论已回主线程（不是只看线程结束）")
    checker.check(
        dialog.status_value.text() == "WARNING",
        "缺瓦片的地图包显示 WARNING",
        dialog.status_value.text(),
    )
    checker.check(dialog.zoom_table.rowCount() >= 1, "Zoom 表格有行")
    checker.check(bool(dialog.result), "对话框持有诊断结论")
    if dialog.result is not None:
        checker.check(
            dialog.result.missing_tiles == missing,
            f"对话框结论里缺失 {missing} 张",
        )
    _shot(dialog, "slice8_inspector_warning.png", shots)

    # --- 深度检查（换一个含坏瓦片的文件）---
    corrupt_path = _build_corrupt(work_dir / "dialog-corrupt.mbtiles")
    dialog.set_path(str(corrupt_path))
    _pump(app)
    worker, settled, _ = _start_and_wait(dialog, app, deep=True)
    checker.check(
        worker is not None and worker.deep_scan is True,
        "[深度检查] 带上了 deep_scan 标记",
    )
    checker.check(settled, "深度检查结论已回主线程")
    checker.check(dialog.status_value.text() == "FAIL", "含坏瓦片显示 FAIL")
    _shot(dialog, "slice8_inspector_fail.png", shots)

    # --- 正常文件 ---
    good_path = _build_good(work_dir / "dialog-good.mbtiles")
    dialog.set_path(str(good_path))
    _pump(app)
    _, settled, _ = _start_and_wait(dialog, app, deep=False)
    checker.check(settled, "正常文件的结论已回主线程")
    checker.check(dialog.status_value.text() == "PASS", "正常文件显示 PASS")
    _shot(dialog, "slice8_inspector_pass.png", shots)

    # --- 没选文件就点检查 ---
    dialog.set_path("")
    _pump(app)
    dialog.start_quick_check()
    _pump(app)
    checker.check(dialog.inspection_worker is None, "没选文件时不起线程")

    # --- 按钮文案 ---
    labels = [button.text() for button in dialog.buttons.values()]
    labels.append(dialog.choose_button.text())
    english = [text for text in labels if _ENGLISH_BUTTON.search(text)]
    checker.check(not english, "对话框按钮全是中文", str(english))

    # --- 关窗安全 ---
    dialog.close()
    _pump(app)
    checker.check(window._inspector_dialog is dialog, "关掉后实例仍被主窗口复用")
    checker.check(dialog.inspection_worker is None, "关窗后后台线程已收掉")


def scene_static(checker: Checker) -> None:
    """静态约束：SQL 不许进对话框、主窗口要能收掉后台线程。"""
    import inspect as _inspect

    from app.ui.main_window import MainWindow
    from app.ui.mbtiles_inspector_dialog import MBTilesInspectorDialog

    checker.section("场景 D：结构与只读约束")

    dialog_source = _inspect.getsource(MBTilesInspectorDialog)
    checker.check("sqlite3" not in dialog_source, "对话框里没有 sqlite3")
    checker.check("SELECT " not in dialog_source, "对话框里没有手写 SELECT")
    checker.check(
        "WA_DeleteOnClose" not in dialog_source,
        "对话框没有 WA_DeleteOnClose（后台线程还在跑时会中止进程）",
    )

    window_source = _inspect.getsource(MainWindow)
    checker.check("MBTilesInspectorDialog" in window_source, "主窗口装配了检查对话框")
    checker.check(
        "shutdown()" in window_source,
        "主窗口关窗时会 shutdown 检查对话框",
    )

    service_source = Path(
        PROJECT_ROOT / "app/services/mbtiles_inspection_service.py"
    ).read_text(encoding="utf-8")
    for keyword in ("INSERT ", "UPDATE ", "DELETE ", "DROP "):
        checker.check(
            keyword not in service_source,
            f"检查服务里没有 {keyword.strip()} 语句（只读保证）",
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slice 8 检查工具实机验收")
    parser.add_argument(
        "--offscreen",
        action="store_true",
        help="使用 Qt offscreen 平台插件（适合无显示环境；中文字体可能缺失）",
    )
    parser.add_argument(
        "--no-shots", action="store_true", help="只跑检查，不截图"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    shots = not args.no_shots

    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from app.main import setup_logging
    from app.tasks import TaskService, TaskStore
    from app.ui.main_window import MainWindow
    from app.ui.preferences import UiPreferences

    setup_logging()

    work_dir = Path(tempfile.mkdtemp(prefix="verify-slice8-"))
    app = QApplication(sys.argv[:1])
    global _APP
    _APP = app
    checker = Checker()

    print(f"platform={app.platformName()}  work_dir={work_dir}")

    try:
        scene_service_only(checker, work_dir)
        scene_deep_scan(checker, work_dir)

        # 主窗口用临时任务记录 + 临时偏好：绝不碰真实数据与注册表
        store = TaskStore(work_dir / "tasks.json")
        preferences = UiPreferences(
            QSettings(str(work_dir / "ui.ini"), QSettings.Format.IniFormat)
        )
        window = MainWindow(task_service=TaskService(store), preferences=preferences)
        window.show()
        _pump(app)

        scene_dialog(app, window, checker, work_dir, shots)
        scene_static(checker)

        window.close()
        _pump(app)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    print("\n" + "=" * 60)
    if checker.failures:
        print(f"结果：{len(checker.failures)} 项失败 / 共 {checker.passed} 项通过")
        for failure in checker.failures:
            print(f"  - {failure}")
        return 1
    print(f"结果：全部通过（{checker.passed} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
