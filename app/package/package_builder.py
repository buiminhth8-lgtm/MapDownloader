"""把 ``.mbtiles`` 整理成标准离线地图包（Slice 9，Spec §5 ~ §9 / §15 / §16 / §23 / §24）。

产物结构（Spec §2）::

    <package_dir>/
    ├── map.mbtiles
    └── manifest.json
    （preview.png 是可选能力；本 Slice 不生成，因此 manifest 里也没有 preview 字段）

构建顺序（Spec §5，与代码里的阶段一一对应）
-------------------------------------------

::

    检查源文件
        ↓
    Slice 8 快速诊断 ──FAIL──→ 直接失败（Spec §23：禁止打包）
        ↓ WARNING →（Spec §23）等用户确认
        ↓ PASS / 已确认
    从 metadata + Inspector 取事实（Spec §6）──缺关键字段──→ 失败
        ↓
    定目录（复用 sanitize_map_name，Spec §7）
        ↓
    复制 / 原地构建（Spec §8）
        ↓
    流式 SHA256（Spec §4）
        ↓
    写 manifest.json（Spec §3）
        ↓
    PackageValidator 自校验（Spec §12）
        ↓ 通过才发布
    <package>/（一次 os.replace 出现，Spec §15）

三条贯穿本模块的设计决定
------------------------

**1. 原子构建（Spec §15）。** 所有产物先写进 ``<name>.building/``，全部成功之后
才用一次 ``os.replace`` 把它变成 ``<name>/``。任何一步失败都只会留下一个被清掉的
临时目录 —— 正式目录要么不存在，要么是一个**完整且通过自校验**的包，绝不会出现
"看起来成功"的半成品。唯一的例外是**原地构建**（下面第 3 条）：那时源文件本身
就在 ``<name>/map.mbtiles``，没有可延迟发布的东西，所以只把 manifest 原子地写进去。

**2. 不破坏源文件（Spec §8）。** 默认 copy 而不是 move；覆盖已有地图包时旧的包
先**改名备份**（``<name>.backup-<时间戳>``），绝不静默删除 —— 与 Slice 5 的
"重新下载是改名备份"是同一条约定。

**3. 不猜（Spec §6）。** manifest 的每一项都来自 MBTiles metadata 或 Inspector 的
统计结果，界面不参与构造（界面只显示、只改名）。**标准** MBTiles 必备键
（``name`` / ``format`` / ``bounds``）缺失 → 直接失败并列出缺了哪些键；
而本工具自有的扩展键（``provider`` / ``map_type`` / ``labels`` / ``composition``）
缺失时 —— 那是旧版本文件与三方工具生成的文件的常态 —— 采用**文档化的默认值
并记一条 WARNING**，而不是拒收整个包。"拒绝一个数据完全正确、只是没人记录过
地图类型的包"对用户没有帮助；但"悄悄按 vector 记下来"也不行，所以必须留痕。
``composition`` 是唯一的例外，它**不是取默认值而是由 ``labels`` 推出**
（Spec §24 点名了这条依赖），推导同样留一条 WARNING。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.config.settings import (
    PACKAGE_BACKUP_SUFFIX_FORMAT,
    PACKAGE_BUILD_STAGE_RANGES,
    PACKAGE_BUILDING_SUFFIX,
    PACKAGE_COPY_CHUNK_SIZE,
    PACKAGE_DEFAULT_PROVIDER,
    PACKAGE_DEFAULT_TILE_SCHEME,
    PACKAGE_ERROR_BUILD_FAILED,
    PACKAGE_ERROR_EXISTS,
    PACKAGE_ERROR_METADATA_MISSING,
    PACKAGE_ERROR_NO_TILE,
    PACKAGE_ERROR_SOURCE_INSPECT_FAIL,
    PACKAGE_ERROR_SOURCE_MISSING,
    PACKAGE_ERROR_SOURCE_NOT_FILE,
    PACKAGE_ERROR_SOURCE_WARNING,
    PACKAGE_FORMAT_VERSION,
    PACKAGE_MANIFEST_FILENAME,
    PACKAGE_MBTILES_FILENAME,
    PACKAGE_PRECHECK_SAMPLE_SIZE,
    PACKAGE_SHA256_CHUNK_SIZE,
    PACKAGE_WARNING_CHECKSUM_SKIPPED,
    UI_TEXT_PACKAGE_PROGRESS_COPY,
    UI_TEXT_PACKAGE_PROGRESS_SHA256,
    UI_TEXT_PACKAGE_STAGE_CHECK,
    UI_TEXT_PACKAGE_STAGE_COPY,
    UI_TEXT_PACKAGE_STAGE_DONE,
    UI_TEXT_PACKAGE_STAGE_MANIFEST,
    UI_TEXT_PACKAGE_STAGE_SHA256,
    UI_TEXT_PACKAGE_STAGE_VALIDATE,
    UI_TEXT_PACKAGE_UNKNOWN,
)
from app.geo import format_size
from app.mbtiles import MBTilesDiagnosticsResult
from app.models.map_layer import composition_for
from app.models.naming import sanitize_map_name
from app.package import checksum as package_checksum
from app.package.models import (
    MapPackageManifest,
    PackageAborted,
    PackageBuildResult,
    PackageProgress,
    PackageRegion,
    PackageValidationResult,
    now_iso,
    render_manifest_json,
)
from app.package.package_validator import MapPackageValidator, default_inspection_service

# ``app.planning.region_level`` 是零依赖模块（纯 enum，"刻意不 import 任何东西"），
# 因此 app.package 依赖它不构成层次倒挂，也不会造出循环 —— 与 app.geo.region
# 依赖它是同一条边。这里只借它回答一个问题：这个包的几何是不是"任务区自己那份"。
from app.planning.region_level import RegionLevel
from app.providers import describe_exception

__all__ = [
    "MapPackageBuilder",
    "ProgressCallback",
    "SourceFacts",
    "make_package_id",
]

_logger = logging.getLogger(__name__)

#: 进度回调的签名
ProgressCallback = Callable[[PackageProgress], None]

#: manifest 里的地图类型默认值（Spec §2 的默认底图就是矢量）。
_FALLBACK_MAP_TYPE = "vector"

#: manifest 里的合成方式默认值（单图层）。只在"连 labels 都没记"的极端情况下
#: 使用；正常情况下 :func:`app.models.map_layer.composition_for` 会按 labels 推出
#: 正确的取值（Spec §24）。
_FALLBACK_COMPOSITION = "single"

#: **标准** MBTiles 必备键：缺了就没法生成一份诚实的 manifest（Spec §6）。
#: 这三个键任何合规的 MBTiles 都有，缺失说明文件本身不完整 —— 失败是正确响应。
_REQUIRED_METADATA_KEYS: tuple[str, ...] = ("name", "format", "bounds")

#: ``id`` 里允许的字符与最大长度（它与包目录名会被 Android 当作键与路径片段）
_ID_MAX_LENGTH = 64
_ID_CLEAN_PATTERN = re.compile(r"[^a-z0-9]+")


def make_package_id(name: str, *, created_at: str | None = None) -> str:
    """由包名与日期生成 manifest 的 ``id``（Spec §2 / §3 的 ``beijing_20260916`` 形态）。

    规则（**确定性**，同一份输入永远得到同一个 id，方便"重建同一个包"）：

    1. 转小写，非 ``[a-z0-9]`` 的字符一律压成 ``_``，去掉首尾的 ``_``；
    2. 结果为空（纯中文名，如"北京离线地图"）→ 用 ``pkg`` 打底 ——
       **不猜拼音**：把中文转拼音需要一个词典，而它猜错的时候没人会发现；
    3. 追加创建日期的 ``YYYYMMDD``。

    Args:
        name: 包名（用户可见的名字，可以是中文）。
        created_at: ISO 8601 时间字符串；``None`` 时取当前本地时间。

    Returns:
        形如 ``beijing_20260916`` / ``pkg_20260916`` 的标识。

    >>> make_package_id("Beijing Map", created_at="2026-09-16T09:00:00+08:00")
    'beijing_map_20260916'
    >>> make_package_id("北京离线地图", created_at="2026-09-16T09:00:00+08:00")
    'pkg_20260916'
    """
    stem = _ID_CLEAN_PATTERN.sub("_", str(name).strip().lower()).strip("_")
    if not stem:
        stem = "pkg"
    stamp = _date_stamp(created_at)
    identifier = f"{stem}_{stamp}"
    return identifier[:_ID_MAX_LENGTH].rstrip("_")


def _date_stamp(created_at: str | None) -> str:
    """从 ISO 8601 字符串里取出 ``YYYYMMDD``；解析不出来就用今天。"""
    if created_at:
        try:
            return datetime.fromisoformat(created_at).strftime("%Y%m%d")
        except ValueError:  # pragma: no cover - 防御性分支
            pass
    return datetime.now().strftime("%Y%m%d")


@dataclass(frozen=True)
class SourceFacts:
    """要写进 manifest 的、**来自 MBTiles 本身**的事实（Spec §6）。

    做成独立的小对象而不是一串局部变量：这些值必须**整体**通过校验才允许
    开始复制文件，散成一堆变量时很容易出现"用了新算出来的 tile_count，
    但 bounds 还是上一次的"这类错位。
    """

    name: str
    description: str | None
    provider: str
    map_type: str
    labels: bool
    composition: str
    tile_format: str
    tile_scheme: str
    min_zoom: int
    max_zoom: int
    west: float
    south: float
    east: float
    north: float
    tile_count: int
    #: 区域信息（Slice 16）：来自 MBTiles metadata，缺省 ``None``
    #: （Slice ≤15 生成的文件没有这几项）→ manifest 里不写 ``region``。
    region: PackageRegion | None = None


def _region_from_metadata(metadata: dict[str, str]) -> PackageRegion | None:
    """从 MBTiles ``metadata`` 里读区域信息（Spec §21）。

    四项**都空**时返回 ``None``，manifest 里就不写 ``region`` 子对象 ——
    这样 Slice ≤15 时代的老文件打出来的包与以前**逐字节一致**，
    "旧包兼容"才不只是口头承诺（协议样例的往返用例会守住这一点）。

    同时，同一条规则也让"新工具打老包"和"老工具打新包"两个方向都安全：
    前者不凭空多出一个字段，后者只是多读到一个不认识的键（Android 侧忽略）。

    Slice 22 追加两项（``region_level`` 与 ``buffer_meters``）；Slice 25 起
    ``region_level`` **所有区域都写**（矩形 ``custom`` / 行政区 ``country`` 等 /
    任务区 ``mission``）—— 它是跨端契约，Android 侧没有区域目录，反查不出等级。
    ``buffer_meters`` 与 ``geometryVersion`` 仍然只有任务区有。
    这四项都原样透传、不做任何猜测。
    """
    region_type = str(metadata.get("region_type") or "").strip().lower()
    region_id = str(metadata.get("region_id") or "").strip()
    version = str(metadata.get("boundary_version") or "").strip()
    level = str(metadata.get("region_level") or "").strip().lower()
    if not (region_type or region_id or version or level):
        return None
    return PackageRegion(
        region_type=region_type or "rectangle",
        region_id=region_id or None,
        boundary_version=version or None,
        level=level or None,
        buffer_meters=_optional_float(metadata.get("buffer_meters")),
        # ``geometryVersion`` 回答的是"任务区那份**不在资源目录里**的几何是第几版"
        # （Spec §19），所以只有任务区写 —— 行政区的几何版本已经由
        # ``boundaryVersion`` 表达了，再往 ``geometryVersion`` 抄一份就是
        # 同一个事实存两处：迟早有人只改一处（Slice 19 的精度回归同源）。
        # 判据用等级枚举上的 ``is_mission`` 谓词，而不是在这里写
        # ``RegionLevel.MISSION``：等级成员只许出现在 planning 层
        # （tests/test_zoom_policy.py 的 AST 守卫会红），
        # 而"任务区算不算特殊"这件事本来就该由枚举自己回答。
        geometry_version=(
            (version or None)
            if RegionLevel.parse(level).is_mission
            else None
        ),
    )


def _optional_float(value: object) -> float | None:
    """把 metadata 里的字符串转成 float；空 / 转不动时返回 ``None``。

    转不动**不报错**：这只是一个附加信息，一个手改坏了的包不该因此打不成包
    （Spec §19 的同一条原则：不认识 / 读不懂的附加字段按"没有"处理）。
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class MapPackageBuilder:
    """把一个 ``.mbtiles`` 打包成离线地图包（Spec §5）。

    用法::

        builder = MapPackageBuilder()
        result = builder.build("D:\\\\maps\\\\beijing.mbtiles", "D:\\\\maps\\\\packages",
                               package_name="北京离线地图")
        if result.success:
            print(result.package_dir, result.sha256)

    前台调用会**阻塞**：GB 级文件的复制与 SHA256 是分钟级操作，GUI 必须把它放进
    后台线程（见 :class:`app.ui.package_dialog.PackageBuildWorker`）。
    """

    def __init__(
        self,
        *,
        inspection_service=None,
        validator: MapPackageValidator | None = None,
        sha256: Callable[..., str] = package_checksum.calculate_sha256,
        copy_file: Callable[..., int] = package_checksum.copy_file,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            inspection_service: MBTiles 检查服务；``None`` 时延迟构造默认实现
                （原因见 :func:`app.package.default_inspection_service` 的说明）。
            validator: 自校验器；``None`` 时构造一个（并复用同一个检查服务）。
            sha256: 流式 SHA256 实现（测试注入一个会抛异常的版本，可验证
                Spec §31 的"复制成功、SHA256 阶段异常"收尾）。
            copy_file: 分块复制实现（同上）。
            logger: 可选日志器。
        """
        self._inspection = inspection_service
        self._validator = validator
        self._sha256 = sha256
        self._copy = copy_file
        self._logger = logger or _logger

    # ------------------------------------------------------------------ #
    # 目录约定（Spec §7 / §15）
    # ------------------------------------------------------------------ #
    def package_dir_for(self, output_dir: str | Path, package_name: str) -> Path:
        """``<输出目录>/<规整后的包名>``（Spec §7）。

        目录名走 :func:`app.models.naming.sanitize_map_name` —— 与下载时给
        ``.mbtiles`` 取名用的是**同一个**函数，因此不会出现"下载时把
        ``a/b`` 变成 ``a_b``、打包时却直接建了个叫 ``a`` 的子目录"这种事。
        """
        return Path(output_dir) / sanitize_map_name(package_name)

    def building_dir_for(self, package_dir: str | Path) -> Path:
        """``<package>.building``：原子构建用的临时目录（Spec §15）。

        刻意做成**兄弟目录**而不是 ``<package>/.building``：后者要求先创建
        正式目录，那样"正式目录只可能在成功后出现"这条不变量就没了。
        """
        final = Path(package_dir)
        return final.with_name(final.name + PACKAGE_BUILDING_SUFFIX)

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
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
        package_id: str | None = None,
        verify: bool = True,
        should_stop: Callable[[], bool] | None = None,
    ) -> PackageBuildResult:
        """构建离线地图包（Spec §5）。

        Args:
            mbtiles_path: 源 ``.mbtiles``。
            output_dir: 输出目录（不存在会自动创建）。
            package_name: 包名（也是目录名）；``None`` 时用 metadata 里的 ``name``。
            progress_callback: 进度回调，收 :class:`PackageProgress`
                （``percent`` 已按 :data:`PACKAGE_BUILD_STAGE_RANGES` 折算成总体百分比）。
            accept_warnings: MBTiles 检查为 WARNING 时是否继续。默认 **``False``**
                —— 不生成任何文件，返回 ``needs_confirmation=True`` 的结果，
                由界面去问用户（Spec §23），而不是在这里弹窗（构建器不碰 Qt）。
                默认值取 ``False`` 是刻意的：忘传这个参数时，"停下问一句"永远比
                "悄悄按带警告的数据打了个包"安全；脚本想一路跑到底就显式传 ``True``。
            overwrite: 目标目录已存在时是否覆盖。``False`` 时同样返回
                ``needs_confirmation=True``（``confirmation_reason="exists"`）。
                覆盖不是删除：旧目录会先被改名备份（Spec §16）。
            diagnostics: 已算好的 MBTiles 检查结论（GUI 为了显示地图信息
                通常已经算过一次）。传进来可以省掉一次 ``PRAGMA quick_check``。
            package_id: 显式指定 manifest 的 ``id``；``None`` 时按
                :func:`make_package_id` 生成。
            verify: 是否在发布前跑自校验（Spec §5 的最后一步）。**不建议关**：
                它是"绝不发布半成品"这条保证的执行者。
            should_stop: 可选中止条件（复制与 SHA256 每块查一次）。

        Returns:
            :class:`~app.package.PackageBuildResult`。**本方法不抛异常**：
            任何故障都变成 ``errors`` 里的一行。
        """
        source = Path(mbtiles_path)
        result = PackageBuildResult()

        # --- 1. 源文件 ---
        self._emit(progress_callback, "check", UI_TEXT_PACKAGE_STAGE_CHECK)
        if not source.exists():
            result.errors.append(PACKAGE_ERROR_SOURCE_MISSING.format(path=source))
            return result
        if not source.is_file():
            result.errors.append(PACKAGE_ERROR_SOURCE_NOT_FILE.format(path=source))
            return result

        # --- 2. Slice 8 快速诊断（Spec §23）---
        if diagnostics is None:
            try:
                diagnostics = self._inspect(source)
            except Exception as exc:  # noqa: BLE001 - 构建器不抛异常
                result.errors.append(
                    f"检查 MBTiles 失败：{describe_exception(exc)}"
                )
                return result
        result.diagnostics_status = diagnostics.status

        if diagnostics.status == "FAIL":
            reasons = "；".join(diagnostics.errors[:5]) or "（无更多说明）"
            result.errors.append(
                PACKAGE_ERROR_SOURCE_INSPECT_FAIL.format(reasons=reasons)
            )
            return result
        if diagnostics.status == "WARNING":
            result.warnings.extend(diagnostics.warnings)
            if not accept_warnings:
                result.needs_confirmation = True
                result.confirmation_reason = "warning"
                result.errors.append(PACKAGE_ERROR_SOURCE_WARNING)
                return result

        # --- 3. 事实（Spec §6）---
        facts, errors, warnings = self._collect_facts(
            diagnostics, package_name=package_name
        )
        result.warnings.extend(warnings)
        if errors:
            result.errors.extend(errors)
            return result

        # --- 4. 目录与已存在检查（Spec §7 / §16）---
        final_dir = self.package_dir_for(output_dir, facts.name)
        building_dir = self.building_dir_for(final_dir)
        #: 源文件本身就在 ``<包>/map.mbtiles`` 时原地构建，不重复复制（Spec §8）
        in_place = _same_file(source, final_dir / PACKAGE_MBTILES_FILENAME)

        result.package_dir = str(final_dir)
        result.in_place = in_place
        result.tile_count = facts.tile_count
        result.min_zoom = facts.min_zoom
        result.max_zoom = facts.max_zoom
        result.mbtiles_size = diagnostics.file_size or source.stat().st_size

        if final_dir.exists() and not in_place and not overwrite:
            result.needs_confirmation = True
            result.confirmation_reason = "exists"
            result.errors.append(PACKAGE_ERROR_EXISTS.format(path=final_dir))
            return result

        # --- 5 ~ 9. 真正动手（Spec §3 / §4 / §8 / §9 / §12 / §15）---
        try:
            manifest = self._write_package(
                source=source,
                final_dir=final_dir,
                building_dir=building_dir,
                facts=facts,
                diagnostics=diagnostics,
                in_place=in_place,
                package_id=package_id,
                progress_callback=progress_callback,
                should_stop=should_stop,
                result=result,
            )
        except PackageAborted as exc:
            # 用户中止不是失败：只把话说明白，不留半成品。
            result.errors.append(str(exc))
            self._cleanup(building_dir)
            return result
        except Exception as exc:  # noqa: BLE001 - 构建器不抛异常
            result.errors.append(
                PACKAGE_ERROR_BUILD_FAILED.format(error=describe_exception(exc))
            )
            self._cleanup(building_dir)
            return result

        result.manifest_path = str(final_dir / PACKAGE_MANIFEST_FILENAME)
        result.mbtiles_path = str(final_dir / PACKAGE_MBTILES_FILENAME)
        result.sha256 = manifest.mbtiles_sha256
        result.mbtiles_size = manifest.mbtiles_size

        # --- 10. 自校验（Spec §12）---
        if verify:
            self._emit(progress_callback, "validate", UI_TEXT_PACKAGE_STAGE_VALIDATE)
            validation = self._run_validation(
                building_dir if not in_place else final_dir,
                diagnostics=diagnostics,
                progress_callback=progress_callback,
                should_stop=should_stop,
            )
            result.validation = validation
            # 构建器**刻意**用 ``verify_checksum=False`` 调校验（摘要刚算完，
            # 立刻再算一遍要重读整个文件）。那条"已跳过 SHA256 校验"的提醒是
            # 我们自己的选择，不是这个包的缺陷，不该出现在用户的结论里。
            validation.warnings = [
                message
                for message in validation.warnings
                if message != PACKAGE_WARNING_CHECKSUM_SKIPPED
            ]
            if not validation.valid:
                # 自校验没过 = 我们写出来的 manifest 与库里的事实对不上，
                # 那就是**我们自己**的错，绝不能把它发布出去（Spec §15）。
                result.errors.append(
                    f"自校验未通过，已放弃生成地图包：{validation.errors[0]}"
                )
                result.warnings.extend(validation.warnings)
                self._cleanup(building_dir)
                return result
            result.warnings.extend(validation.warnings)

        # --- 11. 发布（Spec §15 / §16）---
        if not in_place:
            try:
                result.backup_path = self._publish(building_dir, final_dir)
            except OSError as exc:
                result.errors.append(
                    PACKAGE_ERROR_BUILD_FAILED.format(error=describe_exception(exc))
                )
                self._cleanup(building_dir)
                return result
        if result.validation is not None:
            # 校验时看的是临时目录（发布前的那一份）；对外必须报正式路径，
            # 否则界面上会显示一个 ``xxx.building`` 的目录名，用户找不到它。
            result.validation.package_dir = str(final_dir)

        result.success = True
        self._emit(progress_callback, "done", UI_TEXT_PACKAGE_STAGE_DONE)
        self._logger.info(
            "地图包生成完成：%s（%s 张瓦片，%s）",
            final_dir,
            f"{manifest.tile_count:,}",
            manifest.mbtiles_sha256[:12],
        )
        return result

    # ------------------------------------------------------------------ #
    # 事实提取（Spec §6）
    # ------------------------------------------------------------------ #
    def _collect_facts(
        self,
        diagnostics: MBTilesDiagnosticsResult,
        *,
        package_name: str | None,
    ) -> tuple[SourceFacts, list[str], list[str]]:
        """从 Inspector 的结论里提取 manifest 需要的一切（Spec §6）。

        Returns:
            ``(facts, errors, warnings)``。``errors`` 非空时 ``facts`` 无意义。

        关键决定：**缩放区间与瓦片数取实际统计值**（``diagnostics`` 的 zoom
        统计与 ``COUNT(*)``），而不是 metadata 里的字符串声明。原因很简单 ——
        manifest 说的是"这个包里到底有什么"，声明可以是错的，事实不会；
        校验时（Spec §12）也是拿事实去比，两边因此天然一致。声明与实际不符
        这件事本身由 Slice 8 的诊断记成 WARNING，会随构建结果一起展示。
        """
        metadata = dict(diagnostics.metadata or {})
        errors: list[str] = []
        warnings: list[str] = []

        missing = [
            key for key in _REQUIRED_METADATA_KEYS
            if not str(metadata.get(key) or "").strip()
        ]
        if diagnostics.bounds is None and "bounds" not in missing:
            missing.append("bounds（无法解析）")
        if missing:
            errors.append(
                PACKAGE_ERROR_METADATA_MISSING.format(fields="、".join(missing))
            )

        if diagnostics.tile_count <= 0:
            errors.append(PACKAGE_ERROR_NO_TILE)

        tile_format = (diagnostics.format or "").strip().lower()
        if not tile_format and "format" not in missing:
            errors.append(
                PACKAGE_ERROR_METADATA_MISSING.format(fields="format")
            )

        # 缩放区间：实际统计优先，其次才是 metadata 的声明。
        min_zoom = diagnostics.min_zoom
        max_zoom = diagnostics.max_zoom
        if min_zoom is None or max_zoom is None:
            declared = _parse_zoom_pair(metadata)
            if declared is not None:
                min_zoom, max_zoom = declared
                warnings.append(
                    "无法从库里统计出缩放区间，manifest 按 metadata 声明的 "
                    f"{min_zoom}~{max_zoom} 记录"
                )
        if min_zoom is None or max_zoom is None:
            errors.append(
                PACKAGE_ERROR_METADATA_MISSING.format(fields="minzoom/maxzoom")
            )

        if errors:
            return _EMPTY_FACTS, errors, warnings

        assert diagnostics.bounds is not None  # 上面已经保证
        west, south, east, north = diagnostics.bounds

        name = (package_name or "").strip() or str(metadata.get("name", "")).strip()

        # --- 自有扩展键：缺失时用文档化的默认值 + WARNING（见模块说明第 3 条）---
        provider = diagnostics.provider or PACKAGE_DEFAULT_PROVIDER
        if not diagnostics.provider:
            warnings.append(
                f"MBTiles metadata 没记录 provider，manifest 按默认值 "
                f"{PACKAGE_DEFAULT_PROVIDER} 记录"
            )

        map_type = diagnostics.map_type or _FALLBACK_MAP_TYPE
        if not diagnostics.map_type:
            warnings.append(
                f"MBTiles metadata 没记录 map_type（旧版本本工具或三方工具生成的"
                f"文件不带这一项），manifest 按 {_FALLBACK_MAP_TYPE} 记录"
            )

        labels = bool(diagnostics.labels)
        if diagnostics.labels is None:
            warnings.append(
                "MBTiles metadata 没记录 labels，manifest 按 false 记录"
                "（如果这个包实际含中文注记，请核对后用「检查 MBTiles」确认）"
            )

        # Spec §24：``labels=true`` 却"没有 composition"是不可接受的 —— 下游
        # 客户端据此决定要不要再叠一层注记，记错就会出现"注记画两遍"或"没有注记"。
        # 因此这里不是无脑退回 single，而是用**项目里唯一那条映射**
        # （:func:`app.models.map_layer.composition_for`，Slice 6 写 metadata 与
        # 续传一致性校验也用它）由 labels 推出来，并如实记一条 WARNING 说明
        # "这个值是我们推的，不是文件里写的"。
        if diagnostics.composition:
            composition = diagnostics.composition
        else:
            composition = composition_for(bool(labels))
            warnings.append(
                f"MBTiles metadata 没记录 composition，manifest 按 labels="
                f"{'true' if labels else 'false'} 推出 {composition} 记录"
            )

        facts = SourceFacts(
            name=name,
            description=(metadata.get("description") or "").strip() or None,
            provider=provider,
            map_type=map_type,
            labels=labels,
            composition=composition,
            tile_format=tile_format,
            tile_scheme=PACKAGE_DEFAULT_TILE_SCHEME,
            min_zoom=int(min_zoom),
            max_zoom=int(max_zoom),
            west=float(west),
            south=float(south),
            east=float(east),
            north=float(north),
            tile_count=int(diagnostics.tile_count),
            # 区域信息（Slice 16）：原样透传 MBTiles metadata 里的三项。
            # 老文件三项都空 → 不写 region 子对象，manifest 与 Slice 15 逐字节一致。
            region=_region_from_metadata(metadata),
        )
        return facts, errors, warnings

    # ------------------------------------------------------------------ #
    # 写盘：复制 → SHA256 → manifest（Spec §3 / §4 / §8 / §9）
    # ------------------------------------------------------------------ #
    def _write_package(
        self,
        *,
        source: Path,
        final_dir: Path,
        building_dir: Path,
        facts: SourceFacts,
        diagnostics: MBTilesDiagnosticsResult,
        in_place: bool,
        package_id: str | None,
        progress_callback: ProgressCallback | None,
        should_stop: Callable[[], bool] | None,
        result: PackageBuildResult,
    ) -> MapPackageManifest:
        """把所有产物写出来，返回生成好的 manifest。

        ``in_place=True`` 时只写 manifest（源文件已经在最终位置上）；否则一切
        都写进 ``building_dir``，由调用方在自校验通过后发布。
        """
        if in_place:
            # 原地构建：``<包>/map.mbtiles`` 就是源文件本身。没有可延迟发布的
            # 东西，因此不建临时目录 —— 但 manifest 仍然是"临时文件 + 原子替换"，
            # 半截的 manifest 比没有 manifest 更糟（校验器会读到一半的 JSON）。
            final_dir.mkdir(parents=True, exist_ok=True)
            target = source
            self._logger.info("原地构建：%s 已在目标包目录内，跳过复制", source)
        else:
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            # 上一次崩溃留下的 .building 是我们自己的临时目录（名字由本模块构造），
            # 直接清掉；留着它只会让下一次构建在"目录已存在"上摔一跤。
            if building_dir.exists():
                self._logger.warning("清理上一次构建残留的临时目录：%s", building_dir)
                self._cleanup(building_dir)
            building_dir.mkdir(parents=True)
            target = building_dir / PACKAGE_MBTILES_FILENAME

            self._emit(progress_callback, "copy", UI_TEXT_PACKAGE_STAGE_COPY)
            copied = self._copy(
                source,
                target,
                chunk_size=PACKAGE_COPY_CHUNK_SIZE,
                progress_callback=self._copy_progress(progress_callback),
                should_stop=should_stop,
            )
            result.copied = True
            self._logger.info("已复制地图文件：%s → %s（%s）", source, target, format_size(copied))

        # --- SHA256：算的是**包内那一份**，不是源文件 ---
        self._emit(progress_callback, "sha256", UI_TEXT_PACKAGE_STAGE_SHA256)
        digest = self._sha256(
            target,
            chunk_size=PACKAGE_SHA256_CHUNK_SIZE,
            progress_callback=self._sha_progress(progress_callback),
            should_stop=should_stop,
        )

        # --- manifest ---
        self._emit(progress_callback, "manifest", UI_TEXT_PACKAGE_STAGE_MANIFEST)
        created_at = now_iso()
        manifest = MapPackageManifest(
            format_version=PACKAGE_FORMAT_VERSION,
            id=package_id or make_package_id(facts.name, created_at=created_at),
            name=facts.name,
            provider=facts.provider,
            map_type=facts.map_type,
            labels=facts.labels,
            composition=facts.composition,
            tile_scheme=facts.tile_scheme,
            tile_format=facts.tile_format,
            min_zoom=facts.min_zoom,
            max_zoom=facts.max_zoom,
            west=facts.west,
            south=facts.south,
            east=facts.east,
            north=facts.north,
            tile_count=facts.tile_count,
            mbtiles_file=PACKAGE_MBTILES_FILENAME,
            mbtiles_size=target.stat().st_size,
            mbtiles_sha256=digest,
        created_at=created_at,
        description=facts.description,
        region=facts.region,
    )
        _write_text_atomic(
            target.parent / PACKAGE_MANIFEST_FILENAME, render_manifest_json(manifest)
        )
        return manifest

    # ------------------------------------------------------------------ #
    # 发布（Spec §15 / §16）
    # ------------------------------------------------------------------ #
    def _publish(self, building_dir: Path, final_dir: Path) -> str | None:
        """把 ``.building`` 变成正式目录，返回备份路径（没备份则为 ``None``）。

        顺序刻意如此，任何一步失败都不会让用户**丢掉**已有的包：

        1. 已存在同名目录 → 先改名成 ``<name>.backup-<时间戳>``（rename，秒级）；
        2. ``os.replace(building, final)`` —— 同一文件系统内的重命名，原子。

        这里**不做删除**：备份留在原处，用户自己决定什么时候清。Spec §16
        的"覆盖必须二次确认"由界面负责，本方法只保证"覆盖不等于丢数据"。
        """
        backup: str | None = None
        if final_dir.exists():
            backup_path = _unique_backup_path(final_dir)
            final_dir.replace(backup_path)
            backup = str(backup_path)
            self._logger.info("原有地图包已改名备份：%s", backup_path)
        os.replace(building_dir, final_dir)
        return backup

    # ------------------------------------------------------------------ #
    # 自校验
    # ------------------------------------------------------------------ #
    def _run_validation(
        self,
        package_dir: Path,
        *,
        diagnostics: MBTilesDiagnosticsResult,
        progress_callback: ProgressCallback | None,
        should_stop: Callable[[], bool] | None,
    ) -> PackageValidationResult:
        """跑一遍 :class:`MapPackageValidator`。

        传入 ``diagnostics``（构造前对**同一份字节**做的检查结论）而不是让
        验证器再查一次：包内文件是源文件的逐字节副本，重新跑
        ``PRAGMA quick_check`` 只是把同样的几分钟再花一遍。

        这里刻意**只查摘要、不重算**（``verify_checksum=False``）：摘要刚刚
        才算出来并写进 manifest，立刻再算一遍必然相等，而那是又一次全文件读取。
        重算摘要的真实场景是"包被传输过 / 被改过之后"，那正是用户手动点
        「验证地图包」的时候（Spec §21 / §35 第 10 步）。
        """
        validator = self._validator
        if validator is None:
            validator = MapPackageValidator(
                inspection_service=(
                    self._inspection
                    if self._inspection is not None
                    else default_inspection_service()
                )
            )
            self._validator = validator
        return validator.validate(
            package_dir,
            verify_checksum=False,
            inspect_mbtiles=True,
            progress_callback=self._rescale(progress_callback, "validate"),
            diagnostics=diagnostics,
            should_stop=should_stop,
        )

    @staticmethod
    def _rescale(
        callback: ProgressCallback | None,
        stage: str,
    ) -> ProgressCallback | None:
        """把"某一段自己的 0~100 进度"映射到它在总进度里的那一段。

        校验器（Spec §21）是**独立可用**的，它的进度当然是 0~100；但构建流程把
        它当作最后一步（94~100）。不换算的话进度条会先走到 94，再跳回 0 ——
        用户看到的是"已经快好了，怎么又从头开始了"。所以这里做一次线性映射，
        而不是让校验器去关心"我在谁里面跑"。

        文案沿用校验器自己的（``正在读取 manifest…`` / ``正在核对文件大小…``），
        它比"正在验证地图包…"更有信息量；阶段名仍然是 ``validate``。
        """
        if callback is None:
            return None
        start, end = PACKAGE_BUILD_STAGE_RANGES.get(stage, (0, 100))

        def emit(progress: PackageProgress) -> None:
            if progress.percent < 0:
                percent = start
            else:
                span = end - start
                percent = start + round(span * min(progress.percent, 100) / 100)
            callback(
                PackageProgress(
                    stage=stage,
                    message=progress.message,
                    percent=percent,
                    current=progress.current,
                    total=progress.total,
                )
            )

        return emit

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _inspect(self, path: Path) -> MBTilesDiagnosticsResult:
        """跑一遍 Slice 8 的快速检查（Spec §23 要求打包前必须做）。"""
        service = self._inspection or default_inspection_service()
        self._inspection = service
        return service.inspect(path, sample_size=PACKAGE_PRECHECK_SAMPLE_SIZE)

    @staticmethod
    def _cleanup(building_dir: Path) -> None:
        """删掉构建临时目录（尽力而为，失败只记日志）。

        只会被用来删**本模块自己按 ``<name>.building`` 规则构造出来的**路径，
        绝不碰用户的正式目录 —— 这条约束是"失败也不弄坏已有数据"的一部分。
        """
        try:
            shutil.rmtree(building_dir, ignore_errors=True)
        except OSError as exc:  # pragma: no cover - rmtree 已经 ignore_errors
            _logger.warning("清理临时目录失败：%s（%s）", building_dir, exc)

    def _copy_progress(
        self, callback: ProgressCallback | None
    ) -> Callable[[int, int], None] | None:
        """把字节进度翻译成 ``正在复制地图文件…1.20 GB / 4.00 GB``。"""
        if callback is None:
            return None

        def report(done: int, total: int) -> None:
            self._emit(
                callback,
                "copy",
                UI_TEXT_PACKAGE_PROGRESS_COPY.format(
                    done=format_size(done), total=format_size(total)
                ),
                done,
                total,
            )

        return report

    def _sha_progress(
        self, callback: ProgressCallback | None
    ) -> Callable[[int, int], None] | None:
        """把字节进度翻译成 ``正在计算 SHA256…45%``。"""
        if callback is None:
            return None

        def report(done: int, total: int) -> None:
            percent = 0 if total <= 0 else min(100, int(done * 100 / total))
            self._emit(
                callback,
                "sha256",
                UI_TEXT_PACKAGE_PROGRESS_SHA256.format(percent=percent),
                done,
                total,
            )

        return report

    @staticmethod
    def _emit(
        callback: ProgressCallback | None,
        stage: str,
        message: str,
        current: int = 0,
        total: int = 0,
    ) -> None:
        """发一条进度：``percent`` 由阶段区间 + 区间内比例算出（Spec §18）。"""
        if callback is None:
            return
        start, end = PACKAGE_BUILD_STAGE_RANGES.get(stage, (0, 100))
        if total > 0:
            ratio = max(0.0, min(1.0, current / total))
            percent = start + round((end - start) * ratio)
        else:
            percent = start
        percent = 100 if stage == "done" else percent
        callback(
            PackageProgress(
                stage=stage,
                message=message,
                percent=percent,
                current=current,
                total=total,
            )
        )


#: 事实提取失败时用的占位（保证返回值类型稳定，调用方不必判 ``None``）
_EMPTY_FACTS = SourceFacts(
    name=UI_TEXT_PACKAGE_UNKNOWN,
    description=None,
    provider=PACKAGE_DEFAULT_PROVIDER,
    map_type=_FALLBACK_MAP_TYPE,
    labels=False,
    composition=_FALLBACK_COMPOSITION,
    tile_format="png",
    tile_scheme=PACKAGE_DEFAULT_TILE_SCHEME,
    min_zoom=0,
    max_zoom=0,
    west=0.0,
    south=0.0,
    east=0.0,
    north=0.0,
    tile_count=0,
)


def _parse_zoom_pair(metadata: dict[str, str]) -> tuple[int, int] | None:
    """从 metadata 里解析 ``minzoom`` / ``maxzoom``；解析不出来返回 ``None``。"""
    try:
        min_zoom = int(str(metadata.get("minzoom", "")).strip())
        max_zoom = int(str(metadata.get("maxzoom", "")).strip())
    except (TypeError, ValueError):
        return None
    if min_zoom > max_zoom:
        return None
    return min_zoom, max_zoom


def _same_file(left: Path, right: Path) -> bool:
    """两个路径是否指向同一个文件（Spec §8：避免把文件复制到自身）。

    用 ``resolve()`` 比字符串：``D:\\a\\b.mbtiles`` 与 ``d:/a/./b.mbtiles``
    是同一个文件，而大小写和分隔符在两个平台上并不统一。
    """
    try:
        return left.resolve() == right.resolve()
    except OSError:  # pragma: no cover - 网络盘 / 权限异常
        return False


def _unique_backup_path(package_dir: Path) -> Path:
    """给已有地图包取一个不冲突的备份名：``<name>.backup-<时间戳>``。

    撞名（同一秒内连续覆盖两次）时加 ``-2`` / ``-3`` 后缀。**绝不覆盖备份** ——
    备份是用户数据的最后一道防线。
    """
    stamp = datetime.now().strftime(PACKAGE_BACKUP_SUFFIX_FORMAT)
    candidate = package_dir.with_name(f"{package_dir.name}{stamp}")
    index = 2
    while candidate.exists():
        candidate = package_dir.with_name(f"{package_dir.name}{stamp}-{index}")
        index += 1
    return candidate


def _write_text_atomic(path: Path, text: str) -> None:
    """原子地写一个 UTF-8 文本文件（临时文件 → fsync → ``os.replace``）。

    与 Slice 5 的 ``task_store`` 同一套写法，理由也相同：写到一半断电 / 被杀进程
    时，读者要么看到旧版本、要么看到新版本，绝不会看到半截内容。
    ``newline="\\n"`` 让产物在任何平台上都是 LF —— manifest 是给 Android 解析的
    协议文件，不该带着宿主系统的行尾差异。
    """
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
