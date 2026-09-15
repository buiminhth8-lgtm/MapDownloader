"""应用入口。

启动链路::

    setup_logging()  ->  QApplication  ->  MainWindow  ->  show()  ->  exec()

运行方式::

    python -m app.main

本模块是组合根（composition root）：负责日志初始化、异常兜底、QApplication
生命周期。业务逻辑不写在这里。
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence

from PySide6.QtWidgets import QApplication

from app.config.settings import (
    APP_NAME,
    APP_VERSION,
    HTTP_LIBRARY_LOG_LEVEL,
    HTTP_LIBRARY_LOGGER_NAMES,
    LOG_DATE_FORMAT,
    LOG_FORMAT,
    LOG_LEVEL,
    ORG_NAME,
)
from app.ui.main_window import MainWindow

logger = logging.getLogger(__name__)

#: 启动后必须出现在日志区的两条记录
STARTUP_MESSAGES: tuple[str, ...] = (
    "Application started",
    "Slice 7 GUI download manager enabled (start/pause/resume/cancel + resume across restarts)",
)


def silence_noisy_http_loggers() -> None:
    """压掉 httpx / httpcore 的 INFO 日志。

    ``httpx`` 每发一个请求都会在 INFO 级别打印完整 URL，而天地图的 URL 里带着
    ``tk=<Token>``。根 logger 是 INFO 级别，所以不加这一步就等于把 Token 明文
    打到控制台——违反"日志中禁止出现完整 Token"的硬性约定。

    这里不改根 logger 的级别：本项目自己的 INFO 日志是需要的，
    只把不必要的第三方噪音关掉。
    """
    for name in HTTP_LIBRARY_LOGGER_NAMES:
        logging.getLogger(name).setLevel(HTTP_LIBRARY_LOG_LEVEL)


def setup_logging() -> None:
    """初始化 ``logging``：控制台输出、UTF-8 友好、INFO 级别。

    暂不写文件日志；UI 日志与 ``logging`` 目前互相独立，
    后续若需要打通，只需在 ``LogPanel`` 上挂一个 ``logging.Handler``。
    """
    stream = sys.stdout
    if stream is None:
        # pythonw.exe 场景下没有标准输出，退化为仅配置根 logger
        logging.basicConfig(level=LOG_LEVEL, force=True)
        silence_noisy_http_loggers()
        return

    encoding = (getattr(stream, "encoding", "") or "").lower()
    if encoding and "utf" not in encoding:
        # 控制台不是 UTF-8（例如旧版 cmd 的 cp936）时，尽量切到 UTF-8，
        # 避免中文日志乱码；失败则保持原样，不影响程序运行。
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):  # pragma: no cover
            pass

    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    logging.basicConfig(level=LOG_LEVEL, handlers=[handler], force=True)
    silence_noisy_http_loggers()


def install_excepthook() -> None:
    """把未捕获异常写进日志，避免 GUI 直接静默退出。"""

    def _hook(exc_type, exc_value, exc_tb) -> None:  # type: ignore[no-untyped-def]
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = _hook


def _report_startup(window: MainWindow) -> None:
    """向控制台与 UI 日志区同时写入启动记录。"""
    for message in STARTUP_MESSAGES:
        logger.info(message)
        window.log_panel.append_log(message)


def main(argv: Sequence[str] | None = None) -> int:
    """程序主入口，返回进程退出码。"""
    setup_logging()
    install_excepthook()
    logger.info("%s v%s 启动", APP_NAME, APP_VERSION)

    app = QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(ORG_NAME)

    try:
        window = MainWindow()
        window.show()
    except Exception:  # pragma: no cover - 启动期兜底
        logger.exception("主窗口初始化失败")
        return 1

    _report_startup(window)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
