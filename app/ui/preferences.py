"""界面偏好持久化（Slice 7，Spec §22）。

存什么、不存什么
----------------

**存**（都是"下次打开还在那儿"能明显减少重复劳动的东西）::

    download/output_dir     最近使用的输出目录
    download/map_type       上次选的地图类型
    download/min_zoom       上次的缩放区间
    download/max_zoom
    window/geometry         窗口大小与位置

**不存**：天地图 Token。理由不是"不方便"，而是密钥不该落在注册表 / 配置文件里
——那是个任何程序都能读、还会被各种"清理工具"打包上传的地方。Token 只能在内存
里活一次（Spec §9）。:data:`SECRET_KEY_FRAGMENTS` 把这条规则变成了可断言的常量：
:meth:`UiPreferences.stored_keys` 会把实际写过的键暴露出来，测试据此确认
没有任何一个键沾上密钥。

为什么用 ``QSettings`` 而不是自己写 JSON：§22 明确说"不要做复杂用户配置系统"。
``QSettings`` 是 Qt 自带的、跨平台的、带原子写入的实现，零额外依赖。
它默认走注册表（Windows），测试通过注入 ``IniFormat`` 的实例把落点改到临时文件。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray, QSettings

from app.config.settings import APP_NAME, ORG_NAME

__all__ = ["SECRET_KEY_FRAGMENTS", "UiPreferences"]

_logger = logging.getLogger(__name__)

#: 键名里**禁止**出现的片段（大小写不敏感）。加入新键时由
#: :meth:`UiPreferences.stored_keys` 的返回值兜底检查。
SECRET_KEY_FRAGMENTS: tuple[str, ...] = ("token", "tk", "secret", "password", "passwd")

#: ``QSettings`` 的分组前缀
_DOWNLOAD_GROUP = "download"
_WINDOW_GROUP = "window"

#: 存储键（集中在一处，避免"读了 A 写了 B"这种只在重启后才暴露的错）
_KEY_OUTPUT_DIR = f"{_DOWNLOAD_GROUP}/output_dir"
_KEY_MAP_TYPE = f"{_DOWNLOAD_GROUP}/map_type"
_KEY_MIN_ZOOM = f"{_DOWNLOAD_GROUP}/min_zoom"
_KEY_MAX_ZOOM = f"{_DOWNLOAD_GROUP}/max_zoom"
_KEY_GEOMETRY = f"{_WINDOW_GROUP}/geometry"

#: 本类会写出的全部键（测试用；新增键时这里必须同步）
_MANAGED_KEYS: tuple[str, ...] = (
    _KEY_OUTPUT_DIR,
    _KEY_MAP_TYPE,
    _KEY_MIN_ZOOM,
    _KEY_MAX_ZOOM,
    _KEY_GEOMETRY,
)


class UiPreferences:
    """界面偏好的读写。

    所有读取都**不会抛异常**：偏好是锦上添花，一个读到脏值的注册表不该让
    程序起不来。读不出来就返回 ``None``，由调用方用默认值兜底。
    """

    def __init__(self, settings: QSettings | None = None) -> None:
        """
        Args:
            settings: 底层 ``QSettings``。``None`` 时用"组织 + 应用名"构造的
                默认实例（Windows 上落在注册表）。测试注入
                ``QSettings(<临时 ini 路径>, QSettings.Format.IniFormat)``
                以免污染真实用户环境。
        """
        self._settings = settings or QSettings(ORG_NAME, APP_NAME)
        #: 进程内实际写过的键，供 :meth:`stored_keys` 与测试观察
        self._written: set[str] = set()

    # ------------------------------------------------------------------ #
    # 只读状态
    # ------------------------------------------------------------------ #
    @property
    def settings(self) -> QSettings:
        """底层 ``QSettings``（测试与诊断用）。"""
        return self._settings

    def stored_keys(self) -> tuple[str, ...]:
        """本实例写到过（或读到过有值）的全部键，排序后返回。

        这个方法的唯一用途是让"不存 Token"这条规则**可被断言**：
        光看代码很难证明"没有任何一条路径把 Token 写进去了"，
        但可以断言"实际出现的键集合里没有一个是敏感的"。
        """
        keys = set(self._written)
        for key in _MANAGED_KEYS:
            if self._settings.contains(key):
                keys.add(key)
        return tuple(sorted(keys))

    # ------------------------------------------------------------------ #
    # 下载配置
    # ------------------------------------------------------------------ #
    @property
    def output_dir(self) -> str | None:
        """最近使用的输出目录；没有记录时返回 ``None``。"""
        return self._read_text(_KEY_OUTPUT_DIR)

    def set_output_dir(self, path: str | None) -> None:
        """记住输出目录（``None`` / 空串表示清除）。"""
        self._write_text(_KEY_OUTPUT_DIR, path)

    @property
    def map_type(self) -> str | None:
        """上次使用的 ``MapType.value``；没有记录时返回 ``None``。"""
        return self._read_text(_KEY_MAP_TYPE)

    def set_map_type(self, value: str | None) -> None:
        """记住地图类型。"""
        self._write_text(_KEY_MAP_TYPE, value)

    @property
    def zoom_range(self) -> tuple[int, int] | None:
        """上次使用的 ``(min_zoom, max_zoom)``；记录不完整时返回 ``None``。"""
        low = self._read_int(_KEY_MIN_ZOOM)
        high = self._read_int(_KEY_MAX_ZOOM)
        if low is None or high is None:
            return None
        return (low, high)

    def set_zoom_range(self, min_zoom: int, max_zoom: int) -> None:
        """记住缩放区间。"""
        self._write_int(_KEY_MIN_ZOOM, min_zoom)
        self._write_int(_KEY_MAX_ZOOM, max_zoom)

    # ------------------------------------------------------------------ #
    # 窗口几何
    # ------------------------------------------------------------------ #
    def save_geometry(self, window) -> None:
        """保存窗口大小与位置（鸭子类型：只要有 ``saveGeometry()``）。"""
        try:
            blob = window.saveGeometry()
        except Exception as exc:  # noqa: BLE001 - 关窗路径不该因为存档失败而中断
            _logger.warning("保存窗口位置失败：%s", exc)
            return
        self._settings.setValue(_KEY_GEOMETRY, blob)
        self._written.add(_KEY_GEOMETRY)
        self._sync()

    def restore_geometry(self, window) -> bool:
        """恢复窗口大小与位置。

        Returns:
            ``True`` 表示真的恢复了一次；``False`` 表示没有存档或内容不可用
            （此时窗口保持 ``MainWindow`` 里的默认尺寸）。
        """
        blob = self._settings.value(_KEY_GEOMETRY)
        if not isinstance(blob, QByteArray) or blob.isEmpty():
            return False
        try:
            return bool(window.restoreGeometry(blob))
        except Exception as exc:  # noqa: BLE001 - 存档来自旧版本 / 被改坏
            _logger.warning("恢复窗口位置失败（忽略并沿用默认尺寸）：%s", exc)
            return False

    # ------------------------------------------------------------------ #
    # 清理
    # ------------------------------------------------------------------ #
    def clear(self) -> None:
        """清空本类管理的全部键（不动键空间里的其它内容）。"""
        for key in _MANAGED_KEYS:
            self._settings.remove(key)
        self._written.clear()
        self._sync()

    # ------------------------------------------------------------------ #
    # 内部读写（统一做类型与异常处理）
    # ------------------------------------------------------------------ #
    def _read_text(self, key: str) -> str | None:
        value = self._settings.value(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _write_text(self, key: str, value: str | None) -> None:
        self._assert_not_secret(key)
        text = "" if value is None else str(value).strip()
        if text:
            self._settings.setValue(key, text)
            self._written.add(key)
        else:
            self._settings.remove(key)
            self._written.discard(key)
        self._sync()

    def _read_int(self, key: str) -> int | None:
        value = self._settings.value(key)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            # 注册表被手工改过 / 旧版本写的是别的类型 → 当作没有记录
            return None

    def _write_int(self, key: str, value: int) -> None:
        self._assert_not_secret(key)
        self._settings.setValue(key, int(value))
        self._written.add(key)
        self._sync()

    def _sync(self) -> None:
        """立刻落盘。

        ``QSettings`` 自己有延迟写入，而本类的唯一写入时机是**关窗**——
        进程马上就退了，等它的延迟刷新等于赌它能跑完。这里显式 ``sync()``，
        异常吞掉（关窗路径不能因为存档失败而中断）。
        """
        try:
            self._settings.sync()
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - 磁盘/权限异常
            _logger.warning("保存界面偏好失败：%s", exc)

    @staticmethod
    def _assert_not_secret(key: str) -> None:
        """写入前的最后一道闸：键名沾上密钥片段就直接拒绝。

        这不是"防御性编程"式的空转——它把 Spec §9/§22 的"Token 不落盘"
        从一个约定变成了一个会用 ``ValueError`` 说话的不变量。万一将来有人
        顺手加了个 ``set_token``，第一个测试就会红，而不是等到某天有人
        翻注册表才发现。
        """
        lowered = key.lower()
        for fragment in SECRET_KEY_FRAGMENTS:
            if fragment in lowered:
                raise ValueError(
                    f"界面偏好不允许保存疑似密钥的键：{key!r}（命中 {fragment!r}）"
                )
