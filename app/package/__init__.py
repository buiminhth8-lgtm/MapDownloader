"""离线地图包（Slice 9，Spec §1 ~ §32）。

把单个 ``.mbtiles`` 升级成**标准、可校验、可供 Android 导入**的离线地图包::

    <package_dir>/
    ├── map.mbtiles
    └── manifest.json

``manifest.json`` 记录了地图类型 / Zoom 区间 / 范围 / 瓦片数 / 文件大小 /
SHA256 / 格式版本 —— Android 侧据此判断"这个包是什么、能不能用、有没有传坏"。

模块分工（依赖方向**自上而下**，无环）::

    models.py            数据模型：MapPackageManifest / 构建与校验结论 / manifest 格式定义
    checksum.py          流式文件工具：分块复制 + 流式 SHA256（GB 级不整读进内存）
    package_reader.py    文件 → manifest（含路径穿越校验）
    package_validator.py 读到的事实 → 结论（PASS / FAIL）
    package_builder.py   事实 → 写盘（原子构建：先写 .building，成功后才发布）

上层还有一个编排入口 :class:`app.services.MapPackageService`，GUI 只调它。

本 Slice **不实现**：ADB / ``adb push`` / Android App / osmdroid / Android
MapImporter / 网络上传 / zip 压缩 / 加密 / 数字签名（Spec §34）。
``preview.png`` 是可选能力（Spec §22），本 Slice 不生成 —— 因为
``manifest.json`` 里没有 ``preview`` 字段，没有它也是完整合法的包。

Token 不落盘：manifest 里**没有**任何 token 字段，构建器也不接触 Token（Spec §3）。
"""

from app.package.checksum import (
    calculate_sha256,
    copy_file,
    file_size,
    verify_sha256,
)
from app.package.models import (
    MANIFEST_BOUNDS_FIELDS,
    MANIFEST_FIELD_ORDER,
    MANIFEST_MBTILES_FIELDS,
    MapPackageManifest,
    ManifestError,
    ManifestPathError,
    PackageAborted,
    PackageBuildResult,
    PackageError,
    PackageProgress,
    PackageValidationResult,
    UnsupportedFormatVersion,
    now_iso,
    parse_manifest_dict,
    render_manifest_json,
)
from app.package.package_builder import MapPackageBuilder, SourceFacts, make_package_id
from app.package.package_reader import MapPackageReader, ensure_inside
from app.package.package_validator import MapPackageValidator, default_inspection_service

__all__ = [
    # 模型
    "MapPackageManifest",
    "PackageBuildResult",
    "PackageValidationResult",
    "PackageProgress",
    "SourceFacts",
    "MANIFEST_FIELD_ORDER",
    "MANIFEST_BOUNDS_FIELDS",
    "MANIFEST_MBTILES_FIELDS",
    "render_manifest_json",
    "parse_manifest_dict",
    "now_iso",
    "make_package_id",
    # 异常
    "PackageError",
    "PackageAborted",
    "ManifestError",
    "ManifestPathError",
    "UnsupportedFormatVersion",
    # 文件工具
    "calculate_sha256",
    "copy_file",
    "verify_sha256",
    "file_size",
    # 读取 / 校验 / 构建
    "MapPackageReader",
    "ensure_inside",
    "MapPackageValidator",
    "default_inspection_service",
    "MapPackageBuilder",
]
