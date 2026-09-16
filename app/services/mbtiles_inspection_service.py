"""MBTiles 检查服务（Slice 8，Spec §15）。

职责
----

把 :mod:`app.mbtiles.reader`（"库里有什么"）、
:mod:`app.mbtiles.validator`（Slice 4/6 的校验规则）、
:mod:`app.mbtiles.diagnostics`（Slice 8 的诊断规则）三件事**按顺序**编排起来，
并在中途上报进度、兜住所有异常。UI 层因此永远不需要碰 SQLite
（Spec §18：检查不许跑在 Qt 主线程，也不许让界面自己写查询）。

三层各自的边界（不要混）
------------------------

===========================  ============================================
:class:`MBTilesReader`       发 SQL、返回事实
:mod:`app.mbtiles.diagnostics` 判定规则、产出结论模型
本模块                        顺序、进度、异常兜底、报告落盘
===========================  ============================================

"绝不抛异常"是硬性要求（Spec §35）
----------------------------------

用户拿一个损坏的文件来检查，期望看到的是 **FAIL**，不是程序崩掉。
因此：

- 文件不存在 / 不是 SQLite / 表缺字段 …… 一律转成 ``result.errors`` 里的一行；
- 任何一项检查自己炸掉，只让**那一项**记一条错误，后面的检查继续跑 ——
  一个库有坏瓦片不代表我们连"它有多少张瓦片"都报不出来。

只读（Spec §23）
----------------

连接由 Reader 以 ``mode=ro`` 打开；本模块只发 SELECT / PRAGMA。
唯一会写盘的是 :meth:`export_report`，写的是**旁边的报告文本**，
不是 ``.mbtiles`` 本身。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from app.config.settings import (
    DIAGNOSTICS_DEEP_SCAN_BATCH_SIZE,
    DIAGNOSTICS_DEFAULT_SAMPLE_SIZE,
    DIAGNOSTICS_MAX_SAMPLE_SIZE,
    DIAGNOSTICS_MIN_SAMPLE_SIZE,
    DIAGNOSTICS_REPORT_SUFFIX,
)
from app.mbtiles import (
    MBTilesDiagnosticsResult,
    MBTilesReader,
    render_report,
    validate_mbtiles,
)
from app.mbtiles import diagnostics as diag

# 复用 Slice 4 修好的"异常链里找第一条非空消息"：``str(exc)`` 常常是空串，
# 真正有用的原因埋在 ``__cause__`` 链深处（例如 SSL 的具体报错）。
from app.providers import describe_exception

__all__ = ["MBTilesInspectionService"]


_logger = logging.getLogger(__name__)

#: 进度回调的签名。用类型别名而不是 Protocol：回调只有一个参数，
#: 再加一层结构类型反而更难读。
ProgressCallback = Callable[[diag.InspectionProgress], None]


class MBTilesInspectionService:
    """检查一个 ``.mbtiles``：结构 → 元数据 → zoom → 坐标 → 覆盖 → 图片。

    用法::

        service = MBTilesInspectionService()
        result = service.inspect("beijing.mbtiles")
        if result.status != "PASS":
            print("\\n".join(result.errors + result.warnings))
        service.export_report(result)        # 写 beijing.mbtiles.report.txt
    """

    def __init__(
        self,
        *,
        validator: Callable[..., object] = validate_mbtiles,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            validator: 可注入的 Slice 4/6 校验器（测试用来观察"确实被调用了"）。
            logger: 可选日志器。
        """
        self._validator = validator
        self._logger = logger or _logger

    # ------------------------------------------------------------------ #
    # 主入口（Spec §15）
    # ------------------------------------------------------------------ #
    def inspect(
        self,
        path: str | Path,
        sample_size: int = DIAGNOSTICS_DEFAULT_SAMPLE_SIZE,
        check_coverage: bool = True,
        deep_scan: bool = False,
        progress_callback: ProgressCallback | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> MBTilesDiagnosticsResult:
        """完整检查一个 MBTiles 文件，返回结构化结论。

        Args:
            path: ``.mbtiles`` 路径。
            sample_size: 图片抽样张数（会被夹到
                ``[DIAGNOSTICS_MIN_SAMPLE_SIZE, DIAGNOSTICS_MAX_SAMPLE_SIZE]``）。
            check_coverage: 是否做覆盖完整性检查（需要 metadata 里有可用的
                ``bounds`` / ``minzoom`` / ``maxzoom``）。
            deep_scan: 是否逐张校验全部 ``tile_data``。**慢**，只在用户显式
                选择"深度检查"时为 ``True``（Spec §11）。
            progress_callback: 进度回调，收 :class:`InspectionProgress`。
            should_stop: 可选中止条件（默认 ``None``）。GUI 关掉对话框时靠它
                让一个跑到一半的深度扫描尽快收手 —— 否则用户关了窗口，
                后台线程还在读几十万张瓦片。**已完成的检查项不受影响**，
                中止只会让剩余项跳过并如实记一条说明。

        Returns:
            :class:`~app.mbtiles.MBTilesDiagnosticsResult`。**任何故障都体现在
            返回值里**，本方法不抛异常。
        """
        file_path = Path(path)
        result = MBTilesDiagnosticsResult(path=str(file_path))
        sample = self._normalize_sample_size(sample_size, result)

        diag.emit(progress_callback, "开始", f"开始检查：{file_path}")

        if not diag.check_file(file_path, result):
            diag.emit(progress_callback, "结束", "检查结束")
            return diag.finalize(result)

        try:
            reader = MBTilesReader(file_path)
            reader.open()
        except FileNotFoundError:
            result.errors.append(f"文件不存在：{file_path}")
            return diag.finalize(result)
        except sqlite3.DatabaseError as exc:
            result.errors.append(f"不是合法的 SQLite 文件：{describe_exception(exc)}")
            return diag.finalize(result)
        except (OSError, RuntimeError) as exc:
            result.errors.append(f"无法打开文件：{describe_exception(exc)}")
            return diag.finalize(result)

        try:
            self._run_stages(
                reader,
                result,
                sample,
                check_coverage,
                deep_scan,
                progress_callback,
                should_stop,
            )
        finally:
            # 无论如何都要关掉：一个没关的连接会拖着 SQLite 的文件句柄，
            # 用户想删掉那个坏文件时会遇到"文件被占用"。
            reader.close()

        diag.emit(
            progress_callback,
            "结束",
            f"检查完成：{result.status}"
            + (f"（{len(result.errors)} 项错误，{len(result.warnings)} 项提示）"
               if result.errors or result.warnings else ""),
        )
        return diag.finalize(result)

    # ------------------------------------------------------------------ #
    # 报告导出（Spec §26）
    # ------------------------------------------------------------------ #
    def export_report(
        self, result: MBTilesDiagnosticsResult, path: str | Path | None = None
    ) -> Path:
        """把诊断结论写成一个 UTF-8 文本报告，返回实际写入的路径。

        默认落点是被检查文件**旁边**的 ``xxx.mbtiles.report.txt``
        （Spec §26 就是这么写的）。刻意不放进临时目录：报告是给用户存档、
        贴进 issue 用的，跑到临时目录里等于没生成。

        Args:
            result: 诊断结论。
            path: 显式指定报告路径。

        Returns:
            报告文件路径。

        Raises:
            OSError: 写不进去（磁盘满 / 目录只读）。调用方负责提示用户 ——
                这里不吞异常，因为"导出失败"必须让用户知道。
        """
        target = (
            Path(path)
            if path is not None
            else Path(result.path).with_name(Path(result.path).name + DIAGNOSTICS_REPORT_SUFFIX)
        )
        target.write_text(render_report(result), encoding="utf-8")
        self._logger.info("诊断报告已导出：%s", target)
        return target

    # ------------------------------------------------------------------ #
    # 内部：按阶段跑
    # ------------------------------------------------------------------ #
    def _run_stages(
        self,
        reader: MBTilesReader,
        result: MBTilesDiagnosticsResult,
        sample: int,
        check_coverage: bool,
        deep_scan: bool,
        progress_callback: ProgressCallback | None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        """按"越前面越致命"的顺序跑完所有检查。

        每一阶段都单独兜异常：坏瓦片不该让"Zoom 统计"也报不出来。
        """
        diag.emit(progress_callback, "数据库结构", "检查数据库结构…")
        self._guard(result, "数据库结构", diag.check_structure, reader, result)

        diag.emit(
            progress_callback,
            "SQLite 自检",
            "运行 PRAGMA integrity_check…" if deep_scan else "运行 PRAGMA quick_check…",
        )
        self._guard(
            result,
            "SQLite 自检",
            diag.check_sqlite_integrity,
            reader,
            result,
            deep_check=deep_scan,
        )

        # Slice 4/6 的规则（必备元数据键、bounds/minzoom 关系、合成信息自洽性）
        # 由既有校验器负责；本 Slice 不重写，只把结论并进来。
        diag.emit(progress_callback, "校验", "读取 metadata…")
        self._guard(result, "校验", self._merge_validation, result, sample)
        self._guard(result, "metadata", diag.check_metadata, reader, result)

        diag.emit(progress_callback, "Zoom 统计", "统计各缩放级别的瓦片数…")
        self._guard(
            result, "Zoom 统计", diag.collect_zoom_stats, reader, result, progress_callback
        )
        self._guard(
            result,
            "metadata 一致性",
            diag.check_metadata_zoom_consistency,
            result,
        )

        # 后面这几项都依赖 tiles 表存在 —— 缺表时上面的结构检查已经报过 ERROR，
        # 再跑一遍只会产生一串"no such table"的噪音。
        if "tiles" not in result.tables:
            diag.emit(progress_callback, "结束", "缺少 tiles 表，跳过后续检查")
            return

        diag.emit(progress_callback, "坐标", "检查瓦片坐标（XYZ/TMS）…")
        self._guard(result, "坐标", diag.check_coordinates, reader, result)
        self._guard(result, "重复瓦片", diag.check_duplicates, reader, result)
        self._guard(result, "空瓦片", diag.check_empty_tiles, reader, result)

        diag.emit(progress_callback, "图片", f"抽样检查 {sample} 张图片…")
        self._guard(result, "图片抽样", diag.check_samples, reader, result, sample)

        if check_coverage:
            diag.emit(progress_callback, "覆盖范围", "检查覆盖完整性…")
            self._guard(
                result,
                "覆盖范围",
                diag.check_coverage,
                reader,
                result,
                progress_callback,
                should_stop,
            )

        if deep_scan:
            total = result.tile_count
            diag.emit(
                progress_callback,
                "深度扫描",
                f"深度检查图片 0 / {total:,}",
                0,
                total,
            )
            self._guard(
                result,
                "深度扫描",
                diag.deep_scan_tiles,
                reader,
                result,
                batch_size=DIAGNOSTICS_DEEP_SCAN_BATCH_SIZE,
                progress_callback=progress_callback,
                total=total,
                should_stop=should_stop,
            )

    def _merge_validation(
        self, result: MBTilesDiagnosticsResult, sample: int
    ) -> None:
        """跑 Slice 4/6 校验器并把结论并进诊断结果。

        用 ``sample_limit=sample``：两次抽样读的瓦片不完全相同（校验器从头取、
        诊断工具分层取），但都在同一个"最多几十张"的量级上，
        不会给"检查完要等几分钟"添砖加瓦。
        """
        validation = self._validator(result.path, sample_limit=sample)
        diag.merge_validation_into(result, validation)

    @staticmethod
    def _guard(
        result: MBTilesDiagnosticsResult,
        stage: str,
        function: Callable[..., object],
        *args,
        **kwargs,
    ) -> None:
        """跑一个检查阶段，把异常转成 ``result.errors`` 里的一行。

        这里刻意捕获**所有** ``Exception`` 而不是只捕 ``sqlite3`` 系列：
        检查工具面对的是"别人的库"，任何一处意外都不应该变成
        "点一下按钮程序就没了"。异常链的还原交给
        :func:`app.providers.describe_exception`（``str(exc)`` 常常是空串）。
        """
        try:
            function(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 见 docstring
            result.errors.append(f"{stage}检查失败：{describe_exception(exc)}")
            _logger.warning("%s检查失败：%s", stage, describe_exception(exc))

    @staticmethod
    def _normalize_sample_size(
        sample_size: int, result: MBTilesDiagnosticsResult
    ) -> int:
        """把抽样张数夹到合法区间，并在真的夹了时说明一句。

        静默改变用户传进来的参数是不可接受的（他会以为"我说了抽 200 张"
        生效了），所以夹的同时补一条 WARNING。
        """
        try:
            value = int(sample_size)
        except (TypeError, ValueError):
            result.warnings.append(
                f"抽样张数 {sample_size!r} 不是整数，已改用默认值 "
                f"{DIAGNOSTICS_DEFAULT_SAMPLE_SIZE}"
            )
            return DIAGNOSTICS_DEFAULT_SAMPLE_SIZE
        if value < DIAGNOSTICS_MIN_SAMPLE_SIZE:
            result.warnings.append(
                f"抽样张数 {value} 小于下限 {DIAGNOSTICS_MIN_SAMPLE_SIZE}，已按下限执行"
            )
            return DIAGNOSTICS_MIN_SAMPLE_SIZE
        if value > DIAGNOSTICS_MAX_SAMPLE_SIZE:
            result.warnings.append(
                f"抽样张数 {value} 超过上限 {DIAGNOSTICS_MAX_SAMPLE_SIZE}，"
                "已按上限执行（要做全量检查请用「深度检查」）"
            )
            return DIAGNOSTICS_MAX_SAMPLE_SIZE
        return value
