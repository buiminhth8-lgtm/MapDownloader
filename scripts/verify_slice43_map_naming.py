"""Slice 43 真实平台验收：地图名按行政层级自动生成。

用户在界面上做的事只有一件 —— 在级联里点省、点市 —— 但要让**名称框**、
「输出文件」和磁盘上那个文件三者同时对得上。``pytest`` 里能断言前两者，
"磁盘上真的叫这个名字"只有起窗口、真点「开始下载」才看得见，所以单独一个脚本。

场景：

::

    A  中国 / 全部省份          → 名称 全国基础包，产物 全国基础包_矢量.mbtiles
    B  中国 / 江苏省 / 全部      → 名称 江苏省，产物 江苏省_矢量.mbtiles
    C  中国 / 江苏省 / 苏州市    → 名称 江苏省_苏州市，**真下载一次**看落盘名字
    D  手改名字后切区域          → 名字不被顶掉
    E  清空名字再「计算」        → 补回区域名（不是坐标名）
    F  切影像                    → 江苏省_苏州市_影像.mbtiles

区域数据是脚本现造的（``resources/regions`` 里默认没有任何边界文件，见 Spec §3），
边界都是小方框，因此 Z10 只有几张瓦片，下载一次是秒级。

用法::

    python scripts/verify_slice43_map_naming.py
    python scripts/verify_slice43_map_naming.py --offscreen   # 无显示环境（CI）
    python scripts/verify_slice43_map_naming.py --no-shots
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 复用 Slice 7 的窗口装配（临时任务库 / 临时偏好 / 打桩网络）与 Slice 31 的
# "把前置弹窗推过去"那套，避免三份实现各自漂移。
import verify_slice7_ui as base  # noqa: E402
import verify_slice31_type_naming as naming  # noqa: E402


def install_regions(root: Path) -> None:
    """造一份"国 → 省 → 市 → 县"的合成目录，含一个直辖市分支。

    边界全部是小方框（0.1° 见方），只为了"能选中、能下载几张"，不追求真实形状：
    本脚本验的是**名字**，不是几何。
    """
    regions = root / "regions"
    for part in ("country", "province", "city", "county"):
        (regions / part).mkdir(parents=True, exist_ok=True)

    def write(relative: str, name: str, region_id: str, bounds) -> None:
        west, south, east, north = bounds
        payload = {
            "type": "Feature",
            "properties": {"name": name, "id": region_id},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [west, south],
                        [east, south],
                        [east, north],
                        [west, north],
                        [west, south],
                    ]
                ],
            },
        }
        (regions / relative).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    write("country/china.geojson", "中国", "china", (110.0, 30.0, 118.0, 36.0))
    write("province/jiangsu.geojson", "江苏省", "jiangsu", (116.0, 31.0, 116.4, 31.2))
    write("city/suzhou.geojson", "苏州市", "suzhou", (116.0, 31.0, 116.1, 31.1))
    write("county/kunshan.geojson", "昆山市", "kunshan", (116.0, 31.0, 116.05, 31.05))
    write("province/beijing.geojson", "北京市", "beijing", (116.2, 39.8, 116.5, 40.0))
    write("county/dongcheng.geojson", "东城区", "dongcheng", (116.2, 39.8, 116.25, 39.85))

    (regions / "index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "regions": [
                    {"id": "china", "name": "中国", "level": "country",
                     "parentId": None, "geojson": "country/china.geojson"},
                    {"id": "jiangsu", "name": "江苏省", "level": "province",
                     "parentId": "china", "geojson": "province/jiangsu.geojson"},
                    {"id": "suzhou", "name": "苏州市", "level": "city",
                     "parentId": "jiangsu", "geojson": "city/suzhou.geojson"},
                    {"id": "kunshan", "name": "昆山市", "level": "county",
                     "parentId": "suzhou", "geojson": "county/kunshan.geojson"},
                    {"id": "beijing", "name": "北京市", "level": "province",
                     "parentId": "china", "geojson": "province/beijing.geojson"},
                    {"id": "dongcheng", "name": "东城区", "level": "county",
                     "parentId": "beijing", "geojson": "county/dongcheng.geojson"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 两个全局都要改：面板默认服务读 REGION_RESOURCE_DIR，主窗口注入的
    # prefer_generated 服务读 settings.REGION_DATA_ROOT（"导入并发布的数据集"）。
    from app.config import settings as settings_module
    from app.geo import region as region_module

    region_module.REGION_RESOURCE_DIR = regions
    settings_module.REGION_DATA_ROOT = regions


def expected_file(panel, map_type, with_labels: bool) -> str:
    """按当前表单算出"应该落盘的那个文件名"（与生产同一个函数）。"""
    from app.services import build_output_path

    data = panel.get_form_data()
    return build_output_path(
        data["output_dir"],
        data["map_name"],
        map_type=map_type,
        with_labels=with_labels,
    ).name


def scene_a_nationwide(window, checker) -> None:
    checker.section("场景 A：中国 / 全部省份 → 全国基础包_矢量.mbtiles")
    from app.models import MapType

    panel = window.download_panel
    assert panel.set_region_id("china") is True

    checker.check(panel.map_name_text() == "全国基础包", "名称框是「全国基础包」",
                  panel.map_name_text())
    checker.check(
        expected_file(panel, MapType.VECTOR, True) == "全国基础包_矢量.mbtiles",
        "产物是 全国基础包_矢量.mbtiles",
        expected_file(panel, MapType.VECTOR, True),
    )


def scene_b_province(window, checker) -> None:
    checker.section("场景 B：中国 / 江苏省 / 全部 → 江苏省_矢量.mbtiles")
    from app.models import MapType

    panel = window.download_panel
    assert panel.set_region_id("jiangsu") is True

    checker.check(panel.map_name_text() == "江苏省", "名称框是「江苏省」",
                  panel.map_name_text())
    checker.check(
        expected_file(panel, MapType.VECTOR, True) == "江苏省_矢量.mbtiles",
        "产物是 江苏省_矢量.mbtiles",
        expected_file(panel, MapType.VECTOR, True),
    )


def scene_c_city_downloads(app, window, checker, work_dir: Path, shots: bool) -> Path:
    checker.section("场景 C：中国 / 江苏省 / 苏州市 → 真下载一次")
    from app.models import MapType

    panel = window.download_panel
    # 真下载需要三样东西：Token、输出目录、区域（名称是从区域推出来的）。
    # 输出目录指向临时目录，否则文件会落到用户主目录里。
    panel._token_edit.setText(base.FAKE_TOKEN)
    panel._output_edit.setText(str(work_dir))
    assert panel.set_region_id("suzhou") is True
    panel.set_map_type(MapType.VECTOR)
    panel._min_zoom_spin.setValue(10)
    panel._max_zoom_spin.setValue(10)
    base._pump(app)

    name = panel.map_name_text()
    checker.check(name == "江苏省_苏州市", "名称框是「江苏省_苏州市」", name)
    checker.check(
        expected_file(panel, MapType.VECTOR, True) == "江苏省_苏州市_矢量.mbtiles",
        "产物是 江苏省_苏州市_矢量.mbtiles",
        expected_file(panel, MapType.VECTOR, True),
    )
    base._shot(window, "slice43_naming_city.png", shots)

    naming._run_download(app, window, checker, "市级")

    path = work_dir / "江苏省_苏州市_矢量.mbtiles"
    checker.check(path.exists(), "磁盘上真的生成了 江苏省_苏州市_矢量.mbtiles", str(path))
    return path


def scene_d_hand_typed_name(window, checker) -> None:
    checker.section("场景 D：手改名字后切区域 → 名字不被顶掉")

    panel = window.download_panel
    panel.set_map_name("我的离线包")
    assert panel.set_region_id("kunshan") is True

    checker.check(
        panel.map_name_text() == "我的离线包",
        "手动改过的名字在切区域后保持不变",
        panel.map_name_text(),
    )


def scene_e_cleared_name_is_refilled(app, window, checker) -> None:
    checker.section("场景 E：清空名字 → 「计算」补回区域名")

    panel = window.download_panel
    panel.map_name_edit.clear()
    base._pump(app)
    base._wait_until(app, lambda: window._estimate_worker is None)

    panel.action_buttons["calculate"].click()
    base._pump(app)
    base._wait_until(app, lambda: window._estimate_worker is None)

    checker.check(
        panel.map_name_text() == "江苏省_苏州市_昆山市",
        "补回的是区域名（不是 map_xxN_xxE 这类坐标名）",
        panel.map_name_text(),
    )


def scene_f_image_type(window, checker) -> None:
    checker.section("场景 F：切影像 → 江苏省_苏州市_影像.mbtiles")
    from app.models import MapType

    panel = window.download_panel
    assert panel.set_region_id("suzhou") is True
    panel.set_map_type(MapType.IMAGE)

    checker.check(
        expected_file(panel, MapType.IMAGE, True) == "江苏省_苏州市_影像.mbtiles",
        "影像包是 江苏省_苏州市_影像.mbtiles",
        expected_file(panel, MapType.IMAGE, True),
    )


def scene_g_municipality(window, checker) -> None:
    checker.section("场景 G：中国 / 北京市 / 东城区 → 两段，不是三段")

    panel = window.download_panel
    assert panel.set_region_id("dongcheng") is True

    checker.check(
        panel.map_name_text() == "北京市_东城区",
        "直辖市下辖区只有两段（不出现北京市__东城区）",
        panel.map_name_text(),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slice 43 地图自动命名真实平台验收")
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
    work_dir = Path(tempfile.mkdtemp(prefix="verify-slice43-"))
    checker = base.Checker()
    shots = not args.no_shots

    print(f"platform={app.platformName()}  work_dir={work_dir}")
    try:
        install_regions(work_dir)
        window = base._build_window(app, work_dir)
        window.show()
        base._pump(app)
        base._stub_service(work_dir)

        scene_a_nationwide(window, checker)
        scene_b_province(window, checker)
        scene_c_city_downloads(app, window, checker, work_dir, shots)
        scene_d_hand_typed_name(window, checker)
        scene_e_cleared_name_is_refilled(app, window, checker)
        scene_f_image_type(window, checker)
        scene_g_municipality(window, checker)

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
