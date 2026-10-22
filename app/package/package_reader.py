"""离线地图包的读取与 manifest 解析（Slice 9，Spec §11）。

本模块只做**读**，一件事：把一个 ``package_dir`` 变成"结构化的事实"。

职责边界（和 Slice 8 的三层切分同一套思路）::

    package_reader   文件 → MapPackageManifest（只管"能不能读出来、字段对不对"）
    package_validator 读到的事实 → 结论（只管"这算不算问题"）
    package_builder   事实 → 写盘（只管"按什么顺序产出"）

为什么路径校验要做**两层**（Spec §11 / §25）
--------------------------------------------

1. **字段层**（:mod:`app.package.models`）：``mbtiles.file`` 只允许纯文件名，
   连 ``sub/map.mbtiles`` 都不允许。这一层挡掉 99% 的问题，而且错误信息最直白：
   "manifest 的 mbtiles.file 指向包外"。
2. **文件系统层**（本模块的 :func:`ensure_inside`）：把相对路径解析成绝对路径后
   再确认它确实落在包目录里。这一层挡的是字段层挡不住的东西 ——
   **符号链接**。一个 ``map.mbtiles`` 可以是一个指向 ``C:\\Windows\\...`` 的软链接，
   文件名本身完全合法。

第二层看着像是"永远走不到"，但那正是它的价值所在：它守的是一条**不变量**
（"从包里读到的文件一定在包里"），而不是某个已知的输入。将来 Android 导入链路
会从网络上下包，那时 manifest 是**别人给的**，这层校验就从"多虑"变成"必需"。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config.settings import PACKAGE_MANIFEST_FILENAME
from app.package.models import (
    ManifestError,
    ManifestPathError,
    MapPackageManifest,
    parse_manifest_dict,
)

__all__ = [
    "MapPackageReader",
    "ensure_inside",
]

_logger = logging.getLogger(__name__)


def ensure_inside(base: Path, target: Path) -> Path:
    """确认 ``target`` 落在目录 ``base`` 之内，返回解析后的绝对路径（Spec §11）。

    ``resolve()`` 会把符号链接与 ``..`` 一起展开，因此"看起来在包里"的
    软链接会在这里现形。

    Raises:
        ManifestPathError: ``target`` 跑到 ``base`` 之外了。
    """
    root = base.resolve()
    resolved = target.resolve()
    if resolved != root and not resolved.is_relative_to(root):
        raise ManifestPathError(
            f"manifest 指向包目录之外：{target}（包目录：{base}）"
        )
    return resolved


class MapPackageReader:
    """读取离线地图包（Spec §11）。

    用法::

        reader = MapPackageReader()
        manifest = reader.read_manifest("D:\\\\maps\\\\packages\\\\北京离线地图")
        mbtiles = reader.resolve_mbtiles(package_dir, manifest)

    Raises:
        ManifestError: 目录里没有 manifest.json、不是合法 JSON、字段非法、
            或 ``formatVersion`` 超前。**任何**读不出来的原因都是
            :class:`ManifestError` 的子类 —— 调用方（验证器）不需要
            ``except (OSError, ValueError, KeyError, ...)`` 地撒网。
    """

    def __init__(self, *, manifest_name: str = PACKAGE_MANIFEST_FILENAME) -> None:
        """
        Args:
            manifest_name: manifest 的文件名。默认就是协议规定的
                ``manifest.json``；留成参数只是为了让测试能构造"名字不对"的场景。
        """
        self._manifest_name = manifest_name

    # ------------------------------------------------------------------ #
    # 路径
    # ------------------------------------------------------------------ #
    def manifest_path(self, package_dir: str | Path) -> Path:
        """返回 ``<package_dir>/manifest.json`` 的路径（不判断存不存在）。"""
        return Path(package_dir) / self._manifest_name

    def has_manifest(self, package_dir: str | Path) -> bool:
        """包目录里有没有 manifest（只做一次 ``is_file()``）。"""
        return self.manifest_path(package_dir).is_file()

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def read_manifest(self, package_dir: str | Path) -> MapPackageManifest:
        """读取并校验 ``manifest.json``（Spec §11）。

        检查顺序刻意从"最外层"往里走，这样报出来的错误永远是**最靠前**的那个
        原因（不然用户会拿到一堆由第一个错误连锁引发的secondary 错误）：

        1. 目录存在 → 2. ``manifest.json`` 存在 → 3. 文件能读
        → 4. 是合法 JSON → 5. ``formatVersion`` 支持 → 6. 逐字段类型与取值。

        Raises:
            ManifestError: 以上任一环节失败。
            ManifestPathError: ``mbtiles.file`` 不是包内的纯文件名。
            UnsupportedFormatVersion: ``formatVersion`` 高于本工具支持的版本。
        """
        directory = Path(package_dir)
        if not directory.is_dir():
            raise ManifestError(f"地图包目录不存在：{directory}")

        manifest_file = self.manifest_path(directory)
        if not manifest_file.is_file():
            raise ManifestError(f"缺少 {self._manifest_name}：{manifest_file}")

        try:
            # ``utf-8-sig``：Windows 记事本另存为 UTF-8 会带 BOM，而带 BOM 的
            # 文本直接喂给 ``json.loads`` 会抛 "Expecting value: line 1 column 1"，
            # 那条报错完全指不到"你多了个 BOM"。
            text = manifest_file.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ManifestError(f"无法读取 {manifest_file}：{exc}") from exc
        except UnicodeDecodeError as exc:
            raise ManifestError(
                f"{manifest_file} 不是 UTF-8 文本（Spec §3 要求 UTF-8）：{exc}"
            ) from exc

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ManifestError(
                f"{manifest_file} 不是合法的 JSON：第 {exc.lineno} 行第 {exc.colno} 列 —— {exc.msg}"
            ) from exc

        manifest = parse_manifest_dict(data)
        _logger.debug("已读取 manifest：%s", manifest_file)
        return manifest

    def resolve_mbtiles(
        self, package_dir: str | Path, manifest: MapPackageManifest
    ) -> Path:
        """由 manifest 得到包内 MBTiles 的绝对路径（并确认它真在包里）。

        Returns:
            解析后的绝对路径。**不判断文件是否存在** —— "路径不合法"与
            "文件不在"是两件事，前者该抛错，后者该由验证器记成一条 ERROR
            （用户需要看到"包不完整"，而不是一个异常堆栈）。

        Raises:
            ManifestPathError: 解析结果跑到包目录之外。
        """
        return ensure_inside(Path(package_dir), Path(package_dir) / manifest.mbtiles_file)

    def read_package(self, package_dir: str | Path) -> tuple[MapPackageManifest, Path]:
        """便捷入口：一次拿到 ``(manifest, mbtiles 绝对路径)``。

        Raises:
            同 :meth:`read_manifest` / :meth:`resolve_mbtiles`。
        """
        manifest = self.read_manifest(package_dir)
        return manifest, self.resolve_mbtiles(package_dir, manifest)
