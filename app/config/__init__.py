"""配置层：集中存放应用常量，避免 magic number 散落各处。"""

from app.config.settings import (
    APP_NAME,
    APP_VERSION,
    DEFAULT_MAX_ZOOM,
    DEFAULT_MIN_ZOOM,
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    LOG_FORMAT,
    LOG_LEVEL,
)

__all__ = [
    "APP_NAME",
    "APP_VERSION",
    "LOG_FORMAT",
    "LOG_LEVEL",
    "DEFAULT_MIN_ZOOM",
    "DEFAULT_MAX_ZOOM",
    "DEFAULT_WINDOW_WIDTH",
    "DEFAULT_WINDOW_HEIGHT",
]
