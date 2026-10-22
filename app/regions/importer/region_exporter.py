"""原子发布（Slice 26，Spec §13）。

导入过程是一条单行道：

.. text::

    source → 临时 build 目录 → 全量 validation → Catalog validation → publish

**只有全部通过才发布**，且发布是一次同文件系统的 ``os.replace``（原子）。
失败时旧 Region 数据原封不动 —— "边生成边覆盖生产 RegionCatalog"在这条
路上没有入口（Spec §13 禁止事项）。

为什么先挪走再替换
------------------

Windows 上 ``os.replace`` 对**已存在的非空目标目录**会失败，所以发布分两步：
先把旧数据集改名成 ``.<name>.old-<ns>``（同一父目录 → 同一卷 → 瞬时），
再把 build 目录换上。第二步万一失败（磁盘满、权限被改），把旧目录改回来。
最坏情况是磁盘上多一个 ``.old`` 残骸，而**不会是**一个读一半的目录。

Catalog validation 也在这里
-------------------------

发布前最后一道关是"用 Slice 18 自己的尺子量一遍"：``RegionCatalog.load``
读生成出来的 index.json，``validate_catalog(deep=True)`` 真解析每个
GeoJSON。导入期校验（import_service）与目录期校验（catalog/validator）
是**两套独立的实现** —— 让生成的产物再过一遍消费者的校验，才能证明
"导入器以为对的东西，目录层也真的认得"（Spec §30 的"原 Region / 下载
链路无回归"正指着这里）。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import time
from pathlib import Path

from app.regions.catalog import RegionCatalog
from app.regions.importer.models import (
    RegionImportIssue,
    RegionImportIssueCode,
)
from app.regions.models import CatalogValidationResult, RegionCatalogError
from app.regions.validator import validate_catalog

__all__ = [
    "PublishError",
    "discard_build",
    "publish_dataset",
    "validate_built_catalog",
]


class PublishError(RuntimeError):
    """发布失败（build 目录不可用、目标被占用等）。"""


def validate_built_catalog(build_dir: Path) -> tuple[CatalogValidationResult, list[RegionImportIssue]]:
    """用 Slice 18 的目录校验器量一遍生成产物（Spec §13 的 Catalog validation）。

    Returns:
        ``(catalog_result, issues)`` —— 目录期校验结果，以及折算成导入期
        问题列表的 error（空列表 = 通过）。

    Raises:
        PublishError: build 目录里连 index.json 都读不出来（那说明生成器
            本身坏了，不是数据问题）。
    """
    build_dir = Path(build_dir)
    try:
        catalog = RegionCatalog.load(root=build_dir)
    except RegionCatalogError as exc:
        raise PublishError(f"生成的数据集连目录都读不出来：{exc}") from exc

    result = validate_catalog(catalog, deep=True)
    issues = [
        RegionImportIssue(
            RegionImportIssueCode.CATALOG_INVALID,
            f"生成的数据集未通过目录校验：{issue.describe()}",
            "生成产物",
        )
        for issue in result.errors
    ]
    return result, issues


def publish_dataset(build_dir: Path, output_root: Path) -> Path:
    """把 build 目录原子发布成 ``output_root``（Spec §13）。

    Args:
        build_dir: 生成好的数据集目录（里面的 index.json / manifest.json /
            各等级子目录都已就位）。
        output_root: 目标发布目录（已存在时整体替换）。

    Returns:
        实际发布目录（即 ``output_root``）。

    Raises:
        PublishError: build 目录不存在 / 不含 index.json；目标位置被文件
            占用；替换过程中发生 IO 错误（此时旧数据已恢复原状）。
    """
    build_dir = Path(build_dir)
    output_root = Path(output_root)
    if not build_dir.is_dir():
        raise PublishError(f"构建目录不存在：{build_dir}")
    if not (build_dir / "index.json").is_file():
        raise PublishError(f"构建目录里没有 index.json：{build_dir}")

    parent = output_root.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PublishError(f"发布目录的父目录建不出来：{parent}（{exc}）") from exc

    if output_root.exists() and not output_root.is_dir():
        raise PublishError(
            f"发布位置被一个文件占用：{output_root}。请改一个输出目录，"
            "或手动移走那个文件后再导入"
        )

    if not output_root.exists():
        try:
            os.replace(build_dir, output_root)
        except OSError as exc:
            raise PublishError(
                f"发布失败（{build_dir} → {output_root}）：{exc}"
            ) from exc
        return output_root

    # 旧目录先改名（同卷 rename 是原子操作），再换上新的；失败则改回。
    backup = parent / f".{output_root.name}.old-{time.time_ns()}"
    try:
        os.replace(output_root, backup)
    except OSError as exc:
        raise PublishError(
            f"旧数据集挪不开（{output_root} → {backup}）：{exc}。"
            "它可能正被别的程序占用"
        ) from exc
    try:
        os.replace(build_dir, output_root)
    except OSError as exc:
        with contextlib.suppress(OSError):  # pragma: no cover - 恢复也失败只能如实报
            os.replace(backup, output_root)  # 恢复旧数据
        raise PublishError(
            f"发布失败（{build_dir} → {output_root}）：{exc}；旧数据已尝试恢复"
        ) from exc
    discard_build(backup, label="旧数据集备份")
    return output_root


def discard_build(path: Path, *, label: str = "构建目录") -> bool:
    """删掉一个构建目录 / 备份（尽力而为：失败不掩盖真实异常）。

    Returns:
        ``True`` = 删掉了；``False`` = 本来不在 / 删不掉。
    """
    target = Path(path)
    if not target.exists():
        return False
    try:
        shutil.rmtree(target)
    except OSError:
        return False
    return True
