"""界面偏好持久化（Slice 7，Spec §22）。

存什么、不存什么
----------------

**存**（都是"下次打开还在那儿"能明显减少重复劳动的东西）::

    download/output_dir       最近使用的输出目录
    download/map_type         上次选的地图类型
    download/custom_min_zoom  上次**手动**设置的缩放区间（Slice 19 起改名）
    download/custom_max_zoom
    download/region_type      上次的区域类型（自定义矩形 / 行政区域）
    download/country_id       上次选的三级行政区（Slice 19）
    download/province_id
    download/city_id
    download/zoom_mode        上次的下载策略（推荐 / 自定义）
    window/geometry           窗口大小与位置

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
from dataclasses import dataclass

from PySide6.QtCore import QByteArray, QSettings

from app.config.settings import APP_NAME, ORG_NAME

__all__ = ["SECRET_KEY_FRAGMENTS", "RegionSelection", "UiPreferences"]

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
_KEY_CUSTOM_MIN_ZOOM = f"{_DOWNLOAD_GROUP}/custom_min_zoom"
_KEY_CUSTOM_MAX_ZOOM = f"{_DOWNLOAD_GROUP}/custom_max_zoom"
_KEY_REGION_TYPE = f"{_DOWNLOAD_GROUP}/region_type"
_KEY_COUNTRY_ID = f"{_DOWNLOAD_GROUP}/country_id"
_KEY_PROVINCE_ID = f"{_DOWNLOAD_GROUP}/province_id"
_KEY_CITY_ID = f"{_DOWNLOAD_GROUP}/city_id"
_KEY_COUNTY_ID = f"{_DOWNLOAD_GROUP}/county_id"
_KEY_ZOOM_MODE = f"{_DOWNLOAD_GROUP}/zoom_mode"
_KEY_GEOMETRY = f"{_WINDOW_GROUP}/geometry"

#: Slice ≤18 用的键名。**只读不写**（Slice 19，Spec §14 把"用户的 Zoom"
#: 明确成了 ``custom_*``）。留着这一对是为了让老配置里的缩放区间不至于
#: 在升级后被当成"从没设过"—— 那对一个已经调到顺手位置的用户来说是退步。
_LEGACY_KEY_MIN_ZOOM = f"{_DOWNLOAD_GROUP}/min_zoom"
_LEGACY_KEY_MAX_ZOOM = f"{_DOWNLOAD_GROUP}/max_zoom"

#: 本类会写出的全部键（测试用；新增键时这里必须同步）
_MANAGED_KEYS: tuple[str, ...] = (
    _KEY_OUTPUT_DIR,
    _KEY_MAP_TYPE,
    _KEY_CUSTOM_MIN_ZOOM,
    _KEY_CUSTOM_MAX_ZOOM,
    _KEY_REGION_TYPE,
    _KEY_COUNTRY_ID,
    _KEY_PROVINCE_ID,
    _KEY_CITY_ID,
    _KEY_COUNTY_ID,
    _KEY_ZOOM_MODE,
    _KEY_GEOMETRY,
)

#: 只需要**读**（清理与迁移时要一并处理，但不该被当成"我们写的键"）
_LEGACY_KEYS: tuple[str, ...] = (_LEGACY_KEY_MIN_ZOOM, _LEGACY_KEY_MAX_ZOOM)


@dataclass(frozen=True)
class RegionSelection:
    """行政区选择在偏好里的快照（Slice 19，Spec §14；Slice 28 补县级）。

    各段 id 分开存而不是只存一个"当前生效 id"：界面是按四级下拉恢复的，
    只存一个末端 id 就得反推"它的父亲是谁"——那是目录的职责，不该在
    偏好这一层再实现一遍。

    Attributes:
        region_type: ``RegionType.value``（``rectangle`` / ``polygon``）。
        country_id: 国家 id。
        province_id: 省 id（"不限定省"时是 ``None``）。
        city_id: 市 id（"不限定市"时是 ``None``）。
        county_id: 县/区 id（"不限定县区"时是 ``None``）。**必须是独立字段**：
            直辖市下辖区与普通县都可能成为最具体的那一级，而"最具体一级"
            决定了下载范围 —— 丢了它就会静默放大成整个市/省，
            不报错、只是多下一片。
    """

    region_type: str = ""
    country_id: str | None = None
    province_id: str | None = None
    city_id: str | None = None
    county_id: str | None = None

    @property
    def is_empty(self) -> bool:
        """一条都没存过。"""
        return not any(
            (
                self.region_type,
                self.country_id,
                self.province_id,
                self.city_id,
                self.county_id,
            )
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
        """上次**手动设置**的 ``(min_zoom, max_zoom)``；没记录时返回 ``None``。

        没写过新键时会退回读 Slice ≤18 的 ``download/min_zoom`` / ``max_zoom``
        （一次性的迁移读取，见 ``_LEGACY_KEYS``）。两者都缺才算"没有记录"。
        """
        low = self._read_int(_KEY_CUSTOM_MIN_ZOOM, fallback=_LEGACY_KEY_MIN_ZOOM)
        high = self._read_int(_KEY_CUSTOM_MAX_ZOOM, fallback=_LEGACY_KEY_MAX_ZOOM)
        if low is None or high is None:
            return None
        return (low, high)

    def set_zoom_range(self, min_zoom: int, max_zoom: int) -> None:
        """记住缩放区间。"""
        self._write_int(_KEY_CUSTOM_MIN_ZOOM, min_zoom)
        self._write_int(_KEY_CUSTOM_MAX_ZOOM, max_zoom)

    # ------------------------------------------------------------------ #
    # 区域选择（Slice 19，Spec §14）
    # ------------------------------------------------------------------ #
    @property
    def region_selection(self) -> RegionSelection:
        """上次的行政区选择；没记录过时返回一个全空的快照。"""
        return RegionSelection(
            region_type=self._read_text(_KEY_REGION_TYPE) or "",
            country_id=self._read_text(_KEY_COUNTRY_ID),
            province_id=self._read_text(_KEY_PROVINCE_ID),
            city_id=self._read_text(_KEY_CITY_ID),
            county_id=self._read_text(_KEY_COUNTY_ID),
        )

    def set_region_selection(self, selection: RegionSelection) -> None:
        """记住行政区选择（空字段会被清除，不会留下上一次的残值）。

        清除很关键：从"选了县"改回"只选到市"时，若县 id 留着，下次恢复会
        直接落回那个县 —— 界面上看是市，实际下的却是县。
        """
        self._write_text(_KEY_REGION_TYPE, selection.region_type or None)
        self._write_text(_KEY_COUNTRY_ID, selection.country_id)
        self._write_text(_KEY_PROVINCE_ID, selection.province_id)
        self._write_text(_KEY_CITY_ID, selection.city_id)
        self._write_text(_KEY_COUNTY_ID, selection.county_id)

    @property
    def zoom_mode(self) -> str | None:
        """上次的下载策略（``recommended`` / ``custom``）；没记录时返回 ``None``。"""
        return self._read_text(_KEY_ZOOM_MODE)

    def set_zoom_mode(self, value: str | None) -> None:
        """记住下载策略。

        存的是模式**名字**（字符串）而不是控件状态：偏好这一层不认识
        ``ZoomMode`` 枚举，认不出来时由界面回落到推荐模式 —— 一个脏字符串
        不该让程序起不来。
        """
        self._write_text(_KEY_ZOOM_MODE, value)

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
        """清空本类管理的全部键（不动键空间里的其它内容）。

        ``_LEGACY_KEYS`` 也要删：它们虽然不是我们写的，但 :attr:`zoom_range`
        会在新键缺失时**回退读它们**。只删新键的话，一次 ``clear()`` 之后
        "从没设过缩放区间"这个事实会被老配置悄悄推翻。
        """
        for key in (*_MANAGED_KEYS, *_LEGACY_KEYS):
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

    def _read_int(self, key: str, *, fallback: str | None = None) -> int | None:
        """读一个整数键；本键没有而给了 ``fallback`` 时改读它（迁移用）。

        ``fallback`` 只在**本键完全不存在**时才生效：如果用户已经用新版本
        存过值（哪怕存的是 0），老键就不该再有机会覆盖它。
        """
        if not self._settings.contains(key) and fallback is not None:
            key = fallback
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
