"""离线地图包的编排层（Slice 9，Spec §1 / §5 / §17 / §21）。

把"构建"与"验证"两件事收成一组**界面可以直接调**的动作::

    .mbtiles ──build()──→ <包目录>/（map.mbtiles + manifest.json）
    <包目录> ──validate()──→ PackageValidationResult（PASS / FAIL）

为什么还要一层服务（GUI 直接调 Builder 不就行了）
-------------------------------------------------

四个理由，都是 Slice 4 / 8 已经证明过的：

1. **默认接线只写一处**：构建器要一个 MBTiles 检查服务、验证器要另一个（其实是
   同一个）；把它们在这里装配好，GUI 与脚本就不必各自 new 一遍。
2. **界面不碰业务细节**：对话框需要"输出目录默认放哪"、"包名默认叫什么"、
   "这个 .mbtiles 是什么地图"——这些是策略，不是界面。
3. **统一兜异常**：本层的每个方法都保证**不抛异常**（构建器与验证器本身也是），
   于是后台线程里只剩"emit 结果"这一件事。
4. **可测试**：注入假构建器就能断言 GUI 传对了参数（``accept_warnings`` 有没有
   跟着用户的确认走），而不必真的去打包一个 GB 级的库。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from app.config.settings import (
    DEFAULT_OUTPUT_DIR,
    DIAGNOSTICS_DEFAULT_SAMPLE_SIZE,
    PACKAGE_DEFAULT_DIR_NAME,
    PACKAGE_MBTILES_FILENAME,
)
from app.mbtiles import MBTilesDiagnosticsResult
from app.models.naming import sanitize_map_name
from app.package import (
    MapPackageBuilder,
    MapPackageValidator,
    PackageBuildResult,
    PackageProgress,
    PackageValidationResult,
)
from app.providers import describe_exception
from app.services.mbtiles_inspection_service import MBTilesInspectionService

__all__ = ["MapPackageService", "ProgressCallback"]

_logger = logging.getLogger(__name__)

#: 进度回调的签名（构建与验证共用一种进度对象）
ProgressCallback = Callable[[PackageProgress], None]


class MapPackageService:
    """离线地图包的编排入口（Spec §5 / §21）。

    用法::

        service = MapPackageService()
        info = service.inspect_source("beijing.mbtiles")      # 界面显示地图信息
        result = service.build("beijing.mbtiles", "D:\\\\maps\\\\packages",
                               package_name="北京离线地图")
        if result.needs_confirmation:
            ...                                              # 问用户（Spec §16 / §23）
        check = service.validate(result.package_dir)          # Spec §21
    """

    def __init__(
        self,
        *,
        inspection_service: MBTilesInspectionService | None = None,
        builder: MapPackageBuilder | None = None,
        validator: MapPackageValidator | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            inspection_service: MBTiles 检查服务（构建前诊断 + 界面显示）。
            builder: 包构建器；``None`` 时构造一个并复用同一个检查服务。
            validator: 包校验器；``None`` 时构造一个并复用同一个检查服务。
            logger: 可选日志器。
        """
        self._logger = logger or _logger
        self._inspection = inspection_service or MBTilesInspectionService()
        self._builder = builder or MapPackageBuilder(
            inspection_service=self._inspection
        )
        self._validator = validator or MapPackageValidator(
            inspection_service=self._inspection
        )

    # ------------------------------------------------------------------ #
    # 只读依赖（测试与界面要用到）
    # ------------------------------------------------------------------ #
    @property
    def builder(self) -> MapPackageBuilder:
        """包构建器。"""
        return self._builder

    @property
    def validator(self) -> MapPackageValidator:
        """包校验器。"""
        return self._validator

    @property
    def inspection_service(self) -> MBTilesInspectionService:
        """MBTiles 检查服务。"""
        return self._inspection

    # ------------------------------------------------------------------ #
    # 默认值（Spec §7 / §17）
    # ------------------------------------------------------------------ #
    def default_output_dir(self, base_dir: str | Path | None = None) -> Path:
        """默认输出目录：``<base_dir>/packages``（Spec §17 的 ``D:\\maps\\packages``）。

        Args:
            base_dir: 基准目录；``None`` 时用 :data:`DEFAULT_OUTPUT_DIR`
                （``~/MapDownloader/output``）。

        Returns:
            输出目录路径（**不创建**，创建发生在真正写盘时）。
        """
        if base_dir is None:
            # 与下载的落点保持一层之隔：包是"整理好的产物"，
            # 不该和一堆原始 .mbtiles 混在同一个目录里。
            base_dir = DEFAULT_OUTPUT_DIR
        return Path(base_dir) / PACKAGE_DEFAULT_DIR_NAME

    def package_dir_for(self, output_dir: str | Path, package_name: str) -> Path:
        """``<输出目录>/<规整后的包名>``（转发给构建器，界面用来预判"会不会已存在"）。"""
        return self._builder.package_dir_for(output_dir, package_name)

    def suggest_package_name(
        self, mbtiles_path: str | Path, diagnostics: MBTilesDiagnosticsResult | None = None
    ) -> str:
        """建议的包名（Spec §6：**不另维护一套数据**，优先用 MBTiles 里的 ``name``）。

        顺序：metadata 的 ``name`` → 文件名（去掉 ``.mbtiles``）→ ``DEFAULT_MAP_NAME``。
        两种情况都会先过 :func:`sanitize_map_name`，于是界面上的预填值就是
        最终目录名，用户不会遇到"我明明填了这个名字、目录却是另一个"。
        """
        name = ""
        if diagnostics is not None:
            name = str((diagnostics.metadata or {}).get("name") or "").strip()
        if not name:
            stem = Path(mbtiles_path).stem.strip()
            # 文件名就叫 ``map.mbtiles`` 时，``map`` 是个毫无信息量的包名，
            # 宁可留空让调用方去用自己的兜底值。
            if stem and stem != Path(PACKAGE_MBTILES_FILENAME).stem:
                name = stem
        return sanitize_map_name(name) if name else ""

    def mbtiles_in_package_name(self) -> str:
        """包内 MBTiles 的固定文件名（``map.mbtiles``，Spec §25）。"""
        return PACKAGE_MBTILES_FILENAME

    # ------------------------------------------------------------------ #
    # 动作
    # ------------------------------------------------------------------ #
    def inspect_source(
        self,
        mbtiles_path: str | Path,
        sample_size: int = DIAGNOSTICS_DEFAULT_SAMPLE_SIZE,
        progress_callback: Callable[[object], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> MBTilesDiagnosticsResult:
        """对源 ``.mbtiles`` 做一次快速诊断（Spec §23 的打包前检查）。

        界面用它来填「地图类型 / Zoom / 瓦片」这几个只读字段 —— 数据来自
        检查结论而不是用户手上的输入（Spec §6）。

        **不抛异常**：炸了就返回一个带 error 的结论。
        """
        return self._inspection.inspect(
            mbtiles_path,
            sample_size=sample_size,
            check_coverage=True,
            progress_callback=progress_callback,
            should_stop=should_stop,
        )

    def build(
        self,
        mbtiles_path: str | Path,
        output_dir: str | Path,
        package_name: str | None = None,
        progress_callback: ProgressCallback | None = None,
        *,
        accept_warnings: bool = False,
        overwrite: bool = False,
        diagnostics: MBTilesDiagnosticsResult | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> PackageBuildResult:
        """构建离线地图包（Spec §5）。

        Args:
            mbtiles_path: 源 ``.mbtiles``。
            output_dir: 输出目录。
            package_name: 包名；``None`` 时用 MBTiles metadata 里的 ``name``。
            progress_callback: 进度回调。
            accept_warnings: 用户在确认框里选了"继续生成"之后才传 ``True``
                （Spec §23）。默认 ``False`` 表示"遇到警告先停下来问"。
            overwrite: 用户在"地图包已存在"确认框里选了「覆盖」之后才传 ``True``
                （Spec §16）。
            diagnostics: 已算好的源文件诊断（界面通常已经算过一次）。
            should_stop: 可选中止（用户关掉对话框）。

        Returns:
            :class:`PackageBuildResult`。**不抛异常。** 注意
            ``needs_confirmation=True`` 时它不是失败，而是在等用户拍板
            （``confirmation_reason`` 说明等的是什么）。
        """
        return self._builder.build(
            mbtiles_path,
            output_dir,
            package_name,
            progress_callback,
            accept_warnings=accept_warnings,
            overwrite=overwrite,
            diagnostics=diagnostics,
            should_stop=should_stop,
        )

    def validate(
        self,
        package_dir: str | Path,
        verify_checksum: bool = True,
        inspect_mbtiles: bool = True,
        progress_callback: ProgressCallback | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> PackageValidationResult:
        """校验一个地图包（Spec §21）。

        与构建时那一步的**唯一区别**：这里默认 ``verify_checksum=True``。
        构建时摘要刚算完、立刻重算没有意义；而"验证一个已经存在的包"时，
        重算摘要正是用户想要的那件事 —— 它的目的就是发现"文件被改过 / 传坏了"。

        **不抛异常。**
        """
        return self._validator.validate(
            package_dir,
            verify_checksum=verify_checksum,
            inspect_mbtiles=inspect_mbtiles,
            progress_callback=progress_callback,
            should_stop=should_stop,
        )

    # ------------------------------------------------------------------ #
    # 小工具（界面用）
    # ------------------------------------------------------------------ #
    @staticmethod
    def format_error(error: BaseException | str) -> str:
        """把异常变成一行可读文本（复用 Slice 4 的异常链还原）。"""
        if isinstance(error, BaseException):
            return describe_exception(error)
        return str(error)
