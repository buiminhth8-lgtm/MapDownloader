"""Slice 31 真实平台验收：矢量与影像各存一份，不再抢同一个文件。

**为什么单独一个脚本**：这一条的判据全在"磁盘上到底有几个文件、每个包里是什么"
——``pytest`` 里断言的是 ``build_output_path()`` 的返回值，而真正会出问题的是
"界面显示的名字 → 实际落盘的名字 → 续传校验认的那个文件"这三者是否一致。
所以这里真的起窗口、真的点「开始下载」（网络走 ``httpx.MockTransport``，
不触网、不耗配额），跑完再去磁盘上数文件、读 metadata。

场景：

::

    A  矢量下一遍  → 产物 beijing_vec.mbtiles，metadata.map_type = vector
    B  切影像再下  → 产物 beijing_img.mbtiles，**两个文件并存**，且不弹「已有文件」
    C  勾中文注记  → 「输出文件」变成 beijing_img_cn.mbtiles
    D  同名两个计划（一矢量一影像）→ 产物文件名不同、目录相同

用法::

    python scripts/verify_slice31_type_naming.py
    python scripts/verify_slice31_type_naming.py --offscreen   # 无显示环境（CI）
    python scripts/verify_slice31_type_naming.py --no-shots
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 复用 Slice 7 那套"假服务端 + 临时目录 + 打桩 service"的装配，避免两份实现漂移。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_slice7_ui as base  # noqa: E402


def _close_complete_dialog(app, window) -> None:
    """关掉完成小结（非模态，不关会挡住后面的操作与截图）。"""
    from PySide6.QtWidgets import QMessageBox

    dialog = window._complete_dialog
    if dialog is None:
        return
    button = dialog.button(QMessageBox.StandardButton.Close)
    if button is not None:
        button.click()
    else:  # pragma: no cover - 防御性分支
        dialog.close()
    base._pump(app)


def _run_download(app, window, checker, label: str) -> bool:
    """点一次「开始下载」并等它结束；返回是否真的跑完了。"""
    window.download_panel.action_buttons["start"].click()
    base._pump(app)
    finished = base._wait_until(app, lambda: window._download_worker is None)
    checker.check(finished, f"{label}：任务在超时内结束")
    result = window.download_panel.download_result_label.text()
    checker.check("验证：PASS" in result, f"{label}：产物通过校验",
                  result.splitlines()[0] if result else "（空）")
    _close_complete_dialog(app, window)
    return bool(finished)


def _output_preview(window) -> str:
    return window.download_panel.result_label.text()


def scene_a_vector(app, window, checker, work_dir: Path, shots: bool) -> Path:
    checker.section("场景 A：矢量地图 → beijing_vec.mbtiles")

    from app.models import MapType

    panel = window.download_panel
    base._fill_form(window, work_dir, name="beijing", labels=False)
    panel.set_map_type(MapType.VECTOR)
    panel.action_buttons["calculate"].click()
    base._pump(app)

    vec_path = work_dir / "beijing_vec.mbtiles"
    text = _output_preview(window)
    checker.check(f"输出文件：{vec_path}" in text, "「输出文件」显示 beijing_vec.mbtiles")

    _run_download(app, window, checker, "矢量")
    checker.check(vec_path.exists(), "磁盘上真的生成了 beijing_vec.mbtiles", str(vec_path))

    from app.mbtiles import MBTilesReader

    with MBTilesReader(vec_path) as reader:
        metadata = reader.get_metadata()
        tiles = reader.get_tile_count()
    source_type = (metadata.get("map_type") or "").strip().lower()
    checker.check(
        source_type == "vector",
        "包的 metadata.map_type 是 vector",
        source_type,
    )
    checker.check(tiles > 0, "包里有瓦片", f"{tiles} 张")
    return vec_path


def scene_b_image(app, window, checker, work_dir: Path, vec_path: Path, shots: bool) -> None:
    checker.section("场景 B：切影像再下一遍 → 两个包并存")

    from app.models import MapType

    panel = window.download_panel
    before = panel.map_name_text()
    panel.set_map_type(MapType.IMAGE)
    base._pump(app)

    img_path = work_dir / "beijing_img.mbtiles"
    text = _output_preview(window)
    checker.check(f"输出文件：{img_path}" in text, "切换类型后「输出文件」立刻变成 _img")
    checker.check(panel.map_name_text() == before, "地图名称没有被改写（只有后缀变）",
                  panel.map_name_text())
    base._shot(window, "slice31_image_output.png", shots)

    _run_download(app, window, checker, "影像")
    checker.check(
        window._exists_dialog is None,
        "没有弹「已有文件」——影像包与矢量包不是同一个目标",
    )
    checker.check(img_path.exists(), "磁盘上真的生成了 beijing_img.mbtiles", str(img_path))
    checker.check(vec_path.exists(), "矢量包原样还在（没被覆盖 / 没被备份走）", str(vec_path))

    from app.mbtiles import MBTilesReader

    with MBTilesReader(img_path) as reader:
        image_type = (reader.get_metadata().get("map_type") or "").strip().lower()
    checker.check(
        image_type == "image",
        "影像包的 metadata.map_type 是 image",
        image_type,
    )
    checker.check(
        sorted(p.name for p in work_dir.glob("*.mbtiles"))
        == ["beijing_img.mbtiles", "beijing_vec.mbtiles"],
        "目录里正好两个包，没有 -2 / .bak 之类的残留",
        str(sorted(p.name for p in work_dir.glob("*.mbtiles"))),
    )


def scene_c_labels(app, window, checker, work_dir: Path) -> None:
    checker.section("场景 C：勾中文注记 → 第三个名字")

    panel = window.download_panel
    panel._labels_check.setChecked(True)
    base._pump(app)

    text = _output_preview(window)
    checker.check(
        f"输出文件：{work_dir / 'beijing_img_cn.mbtiles'}" in text,
        "「输出文件」变成 beijing_img_cn.mbtiles（底图与合成不可混写）",
    )
    panel._labels_check.setChecked(False)
    base._pump(app)


def scene_d_plans(checker, work_dir: Path) -> None:
    checker.section("场景 D：同名两个计划（一矢量一影像）不互撞")

    from app.planning.download_plan import (
        DownloadPlan,
        DownloadPlanItem,
        planned_output_paths,
    )

    def make_plan(layer: str) -> DownloadPlan:
        plan = DownloadPlan(name="offline_plan", output_dir=str(work_dir), map_layer=layer)
        plan.add_item(
            DownloadPlanItem(
                region_id="demoland", region_name="演示国", min_zoom=4, max_zoom=10,
                selected_tiles=10, candidate_tiles=10,
            )
        )
        return plan

    vector_paths = planned_output_paths(make_plan("vec_w"))
    image_paths = planned_output_paths(make_plan("img_w"))
    checker.check(
        vector_paths["demoland"].parent == image_paths["demoland"].parent,
        "两个计划的输出目录相同（计划名都是 offline_plan）",
    )
    checker.check(
        vector_paths["demoland"].name == "demoland_z4_10_vec.mbtiles",
        "矢量计划的产物是 demoland_z4_10_vec.mbtiles",
        vector_paths["demoland"].name,
    )
    checker.check(
        image_paths["demoland"].name == "demoland_z4_10_img.mbtiles",
        "影像计划的产物是 demoland_z4_10_img.mbtiles",
        image_paths["demoland"].name,
    )
    checker.check(
        vector_paths["demoland"] != image_paths["demoland"], "两个计划不会写进同一个文件"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slice 31 类型命名真实平台验收")
    parser.add_argument("--offscreen", action="store_true", help="使用 offscreen 平台")
    parser.add_argument("--no-shots", action="store_true", help="不生成截图")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    from PySide6.QtWidgets import QApplication

    from app.main import silence_noisy_http_loggers

    silence_noisy_http_loggers()
    base.FAKE_PNG = base._real_png()

    app = QApplication(sys.argv[:1])
    work_dir = Path(tempfile.mkdtemp(prefix="verify-slice31-"))
    checker = base.Checker()
    shots = not args.no_shots

    print(f"platform={app.platformName()}  work_dir={work_dir}")
    try:
        window = base._build_window(app, work_dir)
        window.show()
        base._pump(app)
        base._stub_service(work_dir)

        vec_path = scene_a_vector(app, window, checker, work_dir, shots)
        base._shot(window, "slice31_vector_output.png", shots)
        scene_b_image(app, window, checker, work_dir, vec_path, shots)
        scene_c_labels(app, window, checker, work_dir)
        scene_d_plans(checker, work_dir)

        window._exit_confirmed = True
        if window._download_worker is not None and window._download_worker.isRunning():
            window._download_worker.cancel()
            window._download_worker.wait(15_000)
        window.close()
        base._pump(app, 4)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    print("\n" + "=" * 62)
    if checker.failures:
        print(f"FAILED：{len(checker.failures)} 项检查未通过（通过 {checker.passed} 项）")
        for failure in checker.failures:
            print(f"  - {failure}")
        return 1
    print(f"PASSED：{checker.passed} 项检查全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
