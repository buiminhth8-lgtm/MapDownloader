"""离线地图包的校验（Slice 9，Spec §12 / §13 / §14 / §21）。

一句话职责：**读到的包 → 能不能信**。

校验清单（Spec §12，顺序即执行顺序）
------------------------------------

1. 包目录存在；
2. ``manifest.json`` 存在且合法（交给 :class:`~app.package.MapPackageReader`）；
3. 包内 MBTiles 存在（且真的在包目录里 —— 路径穿越在 reader 那一层被挡掉）；
4. 文件大小 == ``manifest.mbtiles.size``；
5. 文件 SHA256 == ``manifest.mbtiles.sha256``（Spec §14：这是 Android 导入时
   判断"传输有没有传坏"的唯一依据，因此**必须**默认开启）；
6. MBTiles 本身有效（复用 Slice 8 的快速检查，不另写一套判定）；
7. MBTiles 的 metadata 与 manifest 一致（Spec §12 最后一项）。

PASS / WARNING / FAIL（Spec §13）
---------------------------------

- **FAIL**（有 ``errors``）：SHA256 不符、文件缺失、manifest 损坏、
  **关键字段**不一致（缩放区间 / 范围 / 瓦片数 / 图片格式）；
- **PASS**（无 ``errors``）：允许存在 ``warnings``，例如"非关键 metadata
  不一致"（``labels`` / ``composition`` / ``mapType`` / ``provider``）或
  "metadata 里没记录这一项，没法比"。

为什么关键 / 非关键这么分
-------------------------

关键的那四项，**错了就一定加载不出来**：缩放区间不对，客户端会去请求不存在的
层级；范围不对，地图会显示在错误的位置；瓦片数不对，说明文件被换过；
图片格式不对，解码器直接失败。

非关键的那几项只影响**展示与调度策略**（要不要叠加注记、在地图列表里标成矢量
还是影像）。它们不一致值得提醒用户，但不该把一个"数据完全正确、只是标签写成
矢量"的包判死。

``verify_checksum=False`` 的语义
--------------------------------

``checksum_valid`` 会保持 ``False``，但**不会**产生 error（只有一条 warning）——
"没校验"和"校验没通过"必须由 ``errors`` 来区分，而不是靠这个布尔值。Spec §21
的界面上三行结论是"这一步做了什么"，最后那行 Result 才是结论。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from app.config.settings import (
    PACKAGE_BOUNDS_TOLERANCE,
    PACKAGE_ERROR_MBTILES_INVALID,
    PACKAGE_ERROR_MBTILES_MISSING,
    PACKAGE_ERROR_METADATA_DIFF,
    PACKAGE_ERROR_SHA256_MISMATCH,
    PACKAGE_ERROR_SIZE_MISMATCH,
    PACKAGE_VALIDATE_STAGE_RANGES,
    PACKAGE_WARNING_CHECKSUM_SKIPPED,
    PACKAGE_WARNING_MBTILES_SKIPPED,
    PACKAGE_WARNING_METADATA_DIFF,
    PACKAGE_WARNING_METADATA_MISSING,
    UI_TEXT_PACKAGE_PROGRESS_SHA256,
    UI_TEXT_PACKAGE_STAGE_MBTILES,
    UI_TEXT_PACKAGE_STAGE_METADATA,
    UI_TEXT_PACKAGE_STAGE_READ_MANIFEST,
    UI_TEXT_PACKAGE_STAGE_SIZE,
)
from app.geo import format_size
from app.mbtiles import MBTilesDiagnosticsResult
from app.package import checksum as package_checksum
from app.package.models import (
    ManifestError,
    MapPackageManifest,
    PackageProgress,
    PackageValidationResult,
)
from app.package.package_reader import MapPackageReader
from app.providers import describe_exception

__all__ = [
    "MapPackageValidator",
    "ProgressCallback",
    "default_inspection_service",
]

_logger = logging.getLogger(__name__)

#: 进度回调的签名（与 Slice 8 的检查服务保持同一套写法）
ProgressCallback = Callable[[PackageProgress], None]


def default_inspection_service():
    """构造默认的 MBTiles 检查服务（**延迟导入**，见下）。

    为什么不写在模块顶部的 ``import`` 里
    ------------------------------------

    这里存在一条真实的循环导入边::

        app.package.package_builder → app.package.package_validator（本模块）
        app.services.__init__       → app.services.map_package_service
        app.services.map_package_service → app.package.package_builder

    如果本模块在顶部 ``from app.services.mbtiles_inspection_service import ...``，
    那么 ``import app.package`` 会连带执行 ``app.services.__init__``，
    而后者一路执行到 ``app.package.package_builder`` —— 那时它正卡在自己的
    第 N 行 import 上（模块在 ``sys.modules`` 里但**类还没定义**），于是抛
    ``ImportError: cannot import name 'MapPackageBuilder'``。

    这条边是**向上**的（包构建器需要"一个能诊断 MBTiles 的东西"，而那个东西
    住在更高的编排层），因此正确的做法是把它变成一个可注入的依赖，只在真的
    需要默认实现时才去 import。测试里注入假检查器就完全不会走到这里。

    一个副产物：这条路径让"能不能脱离服务层单独构建地图包"变成可测的 ——
    见 ``tests/test_package_builder.py`` 里显式构造 ``MapPackageBuilder()`` 的用例。
    """
    from app.services.mbtiles_inspection_service import MBTilesInspectionService

    return MBTilesInspectionService()


class MapPackageValidator:
    """校验一个离线地图包（Spec §12）。

    用法::

        validator = MapPackageValidator()
        result = validator.validate("D:\\\\maps\\\\packages\\\\北京离线地图")
        if not result.valid:
            print("\\n".join(result.errors))

    **本方法不抛异常**（与 Slice 8 的诊断服务同一约定）：任何故障都体现在返回值
    里。理由很实际 —— GUI 后台线程里抛出来的异常只能变成"验证失败：Traceback"，
    而用户需要的是"哪一项没对"。
    """

    def __init__(
        self,
        *,
        reader: MapPackageReader | None = None,
        inspection_service=None,
        sha256: Callable[..., str] = package_checksum.calculate_sha256,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            reader: manifest 读取器（测试可注入）。
            inspection_service: MBTiles 检查服务；``None`` 时延迟构造默认实现。
            sha256: 流式 SHA256 实现（测试可注入一个会抛异常的版本来验证收尾逻辑）。
            logger: 可选日志器。
        """
        self._reader = reader or MapPackageReader()
        self._inspection = inspection_service
        self._sha256 = sha256
        self._logger = logger or _logger

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def validate(
        self,
        package_dir: str | Path,
        verify_checksum: bool = True,
        inspect_mbtiles: bool = True,
        progress_callback: ProgressCallback | None = None,
        diagnostics: MBTilesDiagnosticsResult | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> PackageValidationResult:
        """完整校验一个地图包目录（Spec §12）。

        Args:
            package_dir: 地图包目录。
            verify_checksum: 是否重算并比对 SHA256。默认开启（Spec §14）。
                大文件上这一步要读遍全文件，但它正是"传输后完整性"的唯一凭据。
            inspect_mbtiles: 是否跑一遍 MBTiles 快速检查（Slice 8）。关掉的话
                "MBTiles 是否有效"与"metadata 是否与 manifest 一致"两项都会被跳过
                （各记一条 warning），结论会变得没有意义 —— 这个开关是给构建器
                准备的（它刚刚检查过同一份文件）。
            progress_callback: 进度回调，收 :class:`PackageProgress`。
            diagnostics: **已算好的** MBTiles 检查结论。构建器在打包前已经对
                同一份字节做过检查（复制是逐字节的，结论当然适用），传进来就
                不必把 ``PRAGMA quick_check`` 再跑一遍 —— 5 GB 的库上那是分钟级。
            should_stop: 可选中止条件（每块 SHA256 查一次）。

        Returns:
            :class:`PackageValidationResult`。**任何故障都体现在返回值里**。
        """
        target = Path(package_dir)
        result = PackageValidationResult(package_dir=str(target))

        if not target.is_dir():
            result.errors.append(f"地图包目录不存在：{target}")
            return self._finish(result, progress_callback)

        # --- 1. manifest ---
        self._emit(
            progress_callback, "manifest", UI_TEXT_PACKAGE_STAGE_READ_MANIFEST
        )
        try:
            manifest = self._reader.read_manifest(target)
        except ManifestError as exc:
            result.errors.append(str(exc))
            return self._finish(result, progress_callback)
        except Exception as exc:  # noqa: BLE001 - 验证器绝不抛异常
            result.errors.append(f"读取 manifest 失败：{describe_exception(exc)}")
            return self._finish(result, progress_callback)

        result.manifest = manifest
        result.manifest_valid = True

        # --- 2. 包内 MBTiles 的路径 ---
        try:
            mbtiles = self._reader.resolve_mbtiles(target, manifest)
        except ManifestError as exc:
            result.errors.append(str(exc))
            return self._finish(result, progress_callback)
        result.mbtiles_path = str(mbtiles)

        if not mbtiles.is_file():
            result.errors.append(
                PACKAGE_ERROR_MBTILES_MISSING.format(path=mbtiles)
            )
            return self._finish(result, progress_callback)

        # --- 3. 大小 ---
        self._emit(progress_callback, "size", UI_TEXT_PACKAGE_STAGE_SIZE)
        actual_size = mbtiles.stat().st_size
        result.mbtiles_size = actual_size
        size_ok = actual_size == manifest.mbtiles_size
        if not size_ok:
            result.errors.append(
                PACKAGE_ERROR_SIZE_MISMATCH.format(
                    expected=f"{manifest.mbtiles_size:,}",
                    actual=f"{actual_size:,}",
                )
            )

        # --- 4. SHA256 ---
        # 大小已经不一致时**跳过**重算：要读遍 5 GB 才能得到"它也一定不一致"这个
        # 早已确定的答案。这不是省事，是把几分钟还给用户。
        if not verify_checksum:
            result.warnings.append(PACKAGE_WARNING_CHECKSUM_SKIPPED)
        elif not size_ok:
            result.warnings.append(
                f"文件大小已不一致（{format_size(actual_size)} ≠ "
                f"{format_size(manifest.mbtiles_size)}），跳过 SHA256 计算"
            )
        else:
            self._emit(progress_callback, "checksum", self._checksum_message(0, 1))
            try:
                actual_sha = self._sha256(
                    mbtiles,
                    progress_callback=self._checksum_progress(progress_callback),
                    should_stop=should_stop,
                )
            except Exception as exc:  # noqa: BLE001 - 含用户中止
                result.errors.append(
                    f"计算 SHA256 失败：{describe_exception(exc)}"
                )
                return self._finish(result, progress_callback)

            result.actual_sha256 = actual_sha
            expected = manifest.mbtiles_sha256
            if actual_sha == expected:
                result.checksum_valid = True
            else:
                result.errors.append(
                    PACKAGE_ERROR_SHA256_MISMATCH.format(
                        expected=expected, actual=actual_sha
                    )
                )

        # --- 5. MBTiles 本身 + 6. metadata 一致性 ---
        if not inspect_mbtiles and diagnostics is None:
            result.warnings.append(PACKAGE_WARNING_MBTILES_SKIPPED)
            return self._finish(result, progress_callback)

        if diagnostics is None:
            self._emit(progress_callback, "mbtiles", UI_TEXT_PACKAGE_STAGE_MBTILES)
            diagnostics = self._inspect(mbtiles)
        if diagnostics is None:
            result.errors.append("无法获得 MBTiles 检查结论")
            return self._finish(result, progress_callback)

        result.mbtiles_valid = diagnostics.status != "FAIL"
        if diagnostics.status == "FAIL":
            reasons = "；".join(diagnostics.errors[:5]) or "（无更多说明）"
            result.errors.append(
                PACKAGE_ERROR_MBTILES_INVALID.format(
                    status=diagnostics.status, reasons=reasons
                )
            )
        else:
            # WARNING 与普通提示都如实带出来：包能用，但用户有权知道为什么。
            result.warnings.extend(diagnostics.warnings)

        self._emit(progress_callback, "metadata", UI_TEXT_PACKAGE_STAGE_METADATA)
        self._compare_metadata(manifest, diagnostics, result)

        return self._finish(result, progress_callback)

    # ------------------------------------------------------------------ #
    # metadata 比对（Spec §12 最后一项）
    # ------------------------------------------------------------------ #
    def _compare_metadata(
        self,
        manifest: MapPackageManifest,
        diagnostics: MBTilesDiagnosticsResult,
        result: PackageValidationResult,
    ) -> None:
        """逐项比对 manifest 与 MBTiles metadata（Spec §12 / §13）。

        数据来源分两类，**刻意分开**：

        - ``zoom`` / ``tileCount`` 用**库里的事实**（``diagnostics`` 的 zoom 统计
          与实际张数），而不是 metadata 里的字符串声明 —— 声明可以是错的，
          事实不会；manifest 记的本来就该是事实。
        - 其余项只能来自 metadata（``format`` / ``bounds`` / ``labels`` /
          ``composition`` / ``map_type``），它们是"文件自己怎么说"。

        metadata 里**没有**这一项时只记 warning：老的 / 三方工具生成的
        MBTiles 不写 ``labels`` / ``composition`` 是常态，不能因此把包判死。
        """
        metadata = dict(diagnostics.metadata)
        if metadata.get("region_type") == "coverage" or (manifest.region and manifest.region.region_type == "coverage"):
            from app.geo.zoom_coverage import coverage_from_metadata, coverage_summary
            try:
                expected = coverage_summary(coverage_from_metadata(metadata))
                if manifest.region is None or manifest.region.region_type != "coverage" or manifest.region.coverage != expected:
                    result.errors.append("manifest 多级覆盖规则与 MBTiles 策略不一致")
            except ValueError as exc:
                result.errors.append(f"多级覆盖策略损坏：{exc}")

        # --- 关键项 ---
        # SIM102：不合并成一条 `and`。合并后那行会变成 4 层缩进、跨行的巨型条件，
        # 而这里表达的是"先确认两个值都在，再比较这一对"——两步有先后语义。
        if diagnostics.min_zoom is not None and diagnostics.max_zoom is not None:  # noqa: SIM102
            if (manifest.min_zoom, manifest.max_zoom) != (
                diagnostics.min_zoom,
                diagnostics.max_zoom,
            ):
                result.errors.append(
                    PACKAGE_ERROR_METADATA_DIFF.format(
                        field="zoom",
                        expected=f"{manifest.min_zoom}~{manifest.max_zoom}",
                        actual=f"{diagnostics.min_zoom}~{diagnostics.max_zoom}",
                    )
                )

        if manifest.tile_count != diagnostics.tile_count:
            result.errors.append(
                PACKAGE_ERROR_METADATA_DIFF.format(
                    field="tileCount",
                    expected=manifest.tile_count,
                    actual=diagnostics.tile_count,
                )
            )

        raw_format = (metadata.get("format") or diagnostics.format or "").strip().lower()
        if raw_format:
            if raw_format != manifest.tile_format:
                result.errors.append(
                    PACKAGE_ERROR_METADATA_DIFF.format(
                        field="tileFormat",
                        expected=manifest.tile_format,
                        actual=raw_format,
                    )
                )
        else:
            result.warnings.append(
                PACKAGE_WARNING_METADATA_MISSING.format(field="format")
            )

        raw_bounds = metadata.get("bounds")
        if raw_bounds:
            parsed = self._parse_bounds(raw_bounds)
            if parsed is None:
                result.warnings.append(
                    PACKAGE_WARNING_METADATA_MISSING.format(field="bounds")
                )
            elif not self._bounds_equal(parsed, manifest):
                result.errors.append(
                    PACKAGE_ERROR_METADATA_DIFF.format(
                        field="bounds",
                        expected=manifest.bounds_text,
                        actual=raw_bounds,
                    )
                )
        elif diagnostics.bounds is not None:
            if not self._bounds_equal(diagnostics.bounds, manifest):
                result.errors.append(
                    PACKAGE_ERROR_METADATA_DIFF.format(
                        field="bounds",
                        expected=manifest.bounds_text,
                        actual=diagnostics.bounds_text,
                    )
                )
        else:
            result.warnings.append(
                PACKAGE_WARNING_METADATA_MISSING.format(field="bounds")
            )

        # --- 非关键项（不一致 → warning）---
        self._compare_optional(
            result,
            field="mapType",
            expected=manifest.map_type,
            actual=metadata.get("map_type"),
        )
        self._compare_optional(
            result,
            field="labels",
            expected="true" if manifest.labels else "false",
            actual=metadata.get("labels"),
        )
        self._compare_optional(
            result,
            field="composition",
            expected=manifest.composition,
            actual=metadata.get("composition"),
        )
        self._compare_optional(
            result,
            field="provider",
            expected=manifest.provider,
            actual=metadata.get("provider"),
        )

    @staticmethod
    def _compare_optional(
        result: PackageValidationResult,
        *,
        field: str,
        expected: str,
        actual: str | None,
    ) -> None:
        """比对一个非关键项：缺失 → warning，不同 → warning。"""
        if actual is None or not str(actual).strip():
            result.warnings.append(
                PACKAGE_WARNING_METADATA_MISSING.format(field=field)
            )
            return
        if str(actual).strip().lower() != str(expected).strip().lower():
            result.warnings.append(
                PACKAGE_WARNING_METADATA_DIFF.format(
                    field=field, expected=expected, actual=actual
                )
            )

    @staticmethod
    def _parse_bounds(raw: str) -> tuple[float, float, float, float] | None:
        """解析 ``"west,south,east,north"``；解析不出来返回 ``None``。"""
        parts = [item.strip() for item in str(raw).split(",")]
        if len(parts) != 4:
            return None
        try:
            return (
                float(parts[0]),
                float(parts[1]),
                float(parts[2]),
                float(parts[3]),
            )
        except ValueError:
            return None

    @staticmethod
    def _bounds_equal(
        bounds: tuple[float, float, float, float], manifest: MapPackageManifest
    ) -> bool:
        """范围比对（带容差）。

        manifest 里存的是十进制小数，metadata 里是同一组数字的字符串 —— 两边
        各自转float 之后用 ``==`` 比是可行的，但一旦哪天有人改成 6 位以外的
        精度，就会得到"报错但看起来完全一样"的诡异结论。给一个远小于一个瓦片
        尺寸的容差（:data:`PACKAGE_BOUNDS_TOLERANCE` ≈ 0.1 米）更稳。
        """
        actual = (
            manifest.west,
            manifest.south,
            manifest.east,
            manifest.north,
        )
        return all(
            abs(left - right) <= PACKAGE_BOUNDS_TOLERANCE
            for left, right in zip(bounds, actual)
        )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _inspect(self, path: Path) -> MBTilesDiagnosticsResult | None:
        """跑一遍 Slice 8 的快速检查。**不抛异常**。"""
        service = self._inspection or default_inspection_service()
        self._inspection = service
        try:
            return service.inspect(path, check_coverage=True)
        except Exception as exc:  # noqa: BLE001 - 验证器绝不抛异常
            self._logger.warning("MBTiles 检查失败：%s", exc)
            return None

    def _checksum_progress(
        self, callback: ProgressCallback | None
    ) -> Callable[[int, int], None] | None:
        """把字节级进度翻译成 :class:`PackageProgress`。"""
        if callback is None:
            return None

        def report(done: int, total: int) -> None:
            self._emit(callback, "checksum", self._checksum_message(done, total), done, total)

        return report

    @staticmethod
    def _checksum_message(done: int, total: int) -> str:
        """``正在计算 SHA256…45%``（总量为 0 时不写百分比）。"""
        percent = 0 if total <= 0 else min(100, int(done * 100 / total))
        return UI_TEXT_PACKAGE_PROGRESS_SHA256.format(percent=percent)

    @staticmethod
    def _emit(
        callback: ProgressCallback | None,
        stage: str,
        message: str,
        current: int = 0,
        total: int = 0,
    ) -> None:
        """发一条进度（``percent`` 由阶段区间 + 比例算出来）。"""
        if callback is None:
            return
        start, end = PACKAGE_VALIDATE_STAGE_RANGES.get(stage, (0, 100))
        if total > 0:
            ratio = max(0.0, min(1.0, current / total))
            percent = start + round((end - start) * ratio)
        else:
            percent = start
        callback(
            PackageProgress(
                stage=stage,
                message=message,
                percent=percent,
                current=current,
                total=total,
            )
        )

    @staticmethod
    def _finish(
        result: PackageValidationResult, callback: ProgressCallback | None
    ) -> PackageValidationResult:
        """收尾：定结论 + 把进度条拉满（Spec §20 的 PASS 条件在这里落地）。"""
        result.valid = not result.errors
        if callback is not None:
            callback(
                PackageProgress(
                    stage="done",
                    message=f"验证完成：{result.status}",
                    percent=100,
                    current=1,
                    total=1,
                )
            )
        return result
