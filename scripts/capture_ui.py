"""把主窗口渲染成 PNG，用于开发期的界面留档与回归对比。

用法::

    python scripts/capture_ui.py                      # 输出到 ui_main_window.png
    python scripts/capture_ui.py -o docs/ui.png      # 指定输出路径
    python scripts/capture_ui.py --offscreen         # 无显示环境（CI）

说明：脚本只做"装配 + 渲染 + 截图"，不进入 ``app.exec()`` 事件循环，
       因此不会阻塞终端。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

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
    return parser.parse_args(argv)


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

    window = MainWindow()
    window.show()
    app.processEvents()

    _report_startup(window)
    # 触发一次占位动作，让日志区体现 UI -> 数据模型 的读取链路
    window.download_panel.action_buttons["calculate"].click()
    app.processEvents()

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
