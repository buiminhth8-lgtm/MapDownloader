"""把主窗口渲染成 PNG，用于开发期的界面留档与回归对比。

用法::

    python scripts/capture_ui.py                      # 输出到 ui_main_window.png
    python scripts/capture_ui.py -o docs/ui.png       # 指定输出路径
    python scripts/capture_ui.py --offscreen          # 无显示环境（CI）
    python scripts/capture_ui.py --with-tasks -o docs/slice5_task_panel.png
    python scripts/capture_ui.py --with-tasks --with-progress -o docs/slice7_download_gui.png

说明：脚本只做"装配 + 渲染 + 截图"，不进入 ``app.exec()`` 事件循环，
       因此不会阻塞终端。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication

# 允许直接以脚本方式运行（脚本位于 scripts/ 下，需要把项目根加入 sys.path）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_OUTPUT = PROJECT_ROOT / "ui_main_window.png"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="渲染主窗口并截图")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"输出图片路径（默认 {DEFAULT_OUTPUT.name}）",
    )
    parser.add_argument(
        "--offscreen",
        action="store_true",
        help="使用 Qt offscreen 平台插件（适合无显示环境；中文字体可能缺失）",
    )
    parser.add_argument(
        "--with-tasks",
        action="store_true",
        help="预置几条任务记录，用于留档「下载任务」面板（Slice 5 / Slice 7 九列）",
    )
    parser.add_argument(
        "--with-progress",
        action="store_true",
        help="把「下载状态」区填成一次下载进行中的样子，并让按钮落在"
        "「下载中」那一格（Slice 7 的按钮矩阵 + 十一个进度字段）",
    )
    return parser.parse_args(argv)


def _seeded_task_service():
    """构造一个指向临时文件、并预置了若干条任务记录的 TaskService。

    用它而不是默认的 ``~/MapDownloader/data/tasks.json``，是为了让截图脚本
    的副作用落在临时目录里——**绝不污染用户真实的任务记录**。
    """
    import tempfile
    from pathlib import Path as _Path
    from types import SimpleNamespace

    from app.geo import GeoBounds
    from app.models import DownloadTaskStatus, MapLayerType, MapType
    from app.tasks import TaskService, TaskStore

    store = TaskStore(_Path(tempfile.mkdtemp(prefix="capture-ui-")) / "tasks.json")
    service = TaskService(store)

    #: (名称, 范围, 缩放区间, 状态, 总数, 已完成, 失败, 地图类型, 是否含注记)
    #: 覆盖面板上要展示的几种状态；「已完成」那条刻意让 completed == total。
    #: Slice 7 起列表有九列，"地图类型"与"Zoom"两列要有真实内容才看得出区别，
    #: 因此这里显式给出一条影像地图 + 不含注记的记录。
    seeds = [
        ("beijing", GeoBounds(116.0, 39.0, 117.0, 40.0), (8, 17),
         DownloadTaskStatus.DOWNLOADING, 19_640, 9_820, 3, MapType.VECTOR, True),
        ("shanghai", GeoBounds(121.0, 30.9, 121.6, 31.4), (10, 16),
         DownloadTaskStatus.PAUSED, 12_480, 4_120, 0, MapType.VECTOR, True),
        ("hangzhou", GeoBounds(119.9, 30.1, 120.4, 30.5), (9, 15),
         DownloadTaskStatus.COMPLETED, 6_204, 6_204, 0, MapType.IMAGE, False),
    ]
    for name, bounds, (lo, hi), status, total, done, failed, map_type, labels in seeds:
        task = service.create_task(
            name=name,
            bounds=bounds,
            min_zoom=lo,
            max_zoom=hi,
            map_layer=map_type.base_layer,
            output_path=_Path("/maps") / f"{name}.mbtiles",
            total_tiles=total,
            with_labels=labels,
        )
        # 必须走 apply_progress()：``task.progress`` 只是 Slice 0 的占位快照，
        # 面板上显示的是 total_tiles/completed_tiles 那组扁平计数器（``percent``
        # 就是从 completed_tiles 算的）。直接改 task.progress 不会影响展示。
        task.apply_progress(
            SimpleNamespace(
                total=total,
                completed=done,
                succeeded=done - failed,
                failed=failed,
                skipped=0,
            )
        )
        # 再落状态：set_status() 会把当前进度一起写盘。若顺序反过来，
        # 面板重新 list_tasks() 读到的仍是旧值。
        service.set_status(task, status)
    return service


def _seeded_preferences():
    """指向临时 ini 的界面偏好。

    生产里 ``UiPreferences()`` 落在注册表，而主窗口装配时会 ``restore_geometry``
    ——也就是说"截图脚本会读出这台机器上一次关窗时的窗口尺寸"，同一份代码在
    不同机器上截出来的图长得不一样。落点改到临时文件之后，截图变成可复现的，
    脚本也不会往用户注册表里写任何东西。
    """
    import tempfile
    from pathlib import Path as _Path

    from PySide6.QtCore import QSettings

    from app.ui.preferences import UiPreferences

    directory = _Path(tempfile.mkdtemp(prefix="capture-prefs-"))
    settings = QSettings(str(directory / "ui.ini"), QSettings.Format.IniFormat)
    return UiPreferences(settings)


def _fill_running_download(window) -> None:
    """把「下载状态」区填成一次下载进行中的样子。

    只改界面（不启动任何线程、不发任何请求）：截图要展示的是 Slice 7 的十一个
    进度字段与"下载中"那一格的按钮矩阵，为此真的去跑一次下载既慢又需要 Token。
    """
    from app.geo import format_size
    from app.ui.task_actions import UiTaskState

    plan_total = 12_345
    window.task_progress.reset(plan_total, name="beijing")
    window.task_progress.set_value("task_name", "beijing")
    window.task_progress.set_value("current_zoom", "13")
    window.task_progress.set_value("tile_done", "4,120")
    window.task_progress.set_value("tile_remaining", "8,225")
    window.task_progress.set_value("tile_skipped", "361")
    window.task_progress.set_value("tile_succeeded", "3,712")
    window.task_progress.set_value("tile_failed", "47")
    window.task_progress.set_value("speed", "42.7 瓦片/秒")
    window.task_progress.set_value("downloaded_bytes", format_size(96_468_992))
    window.task_progress.set_percent(33)

    # 按钮落在「下载中」：可点 = 暂停 / 取消
    window.download_panel.apply_task_state(UiTaskState.DOWNLOADING)
    window.task_progress.set_status(UiTaskState.DOWNLOADING.display_name)


def pump(app: "QApplication", times: int = 3) -> None:
    """空转事件循环若干轮。

    嵌套 QGroupBox / QLabel(wordWrap) 的布局不是一轮 processEvents 就能收敛的，
    只跑一轮会截到被压扁的控件（例如结果区第二行被裁掉）。
    """
    for _ in range(times):
        app.processEvents()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    # 必须在导入 PySide6 之前设置平台插件
    from PySide6.QtWidgets import QApplication

    from app.main import _report_startup, setup_logging
    from app.ui.main_window import MainWindow

    setup_logging()
    app = QApplication(sys.argv[:1])

    window = MainWindow(
        task_service=_seeded_task_service() if args.with_tasks else None,
        preferences=_seeded_preferences(),
    )
    window.show()
    pump(app)

    _report_startup(window)
    # 触发一次占位动作，让日志区体现 UI -> 数据模型 的读取链路
    window.download_panel.action_buttons["calculate"].click()

    if args.with_progress:
        _fill_running_download(window)
    pump(app)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    pixmap = window.grab()
    if not pixmap.save(str(args.output)):
        print(f"截图保存失败：{args.output}", file=sys.stderr)
        return 1

    print(f"platform={app.platformName()} size={pixmap.width()}x{pixmap.height()}")
    print(f"saved={args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
