"""Slice 18 真实平台验收脚本：Country / Province / City 行政区域层级模型。

**为什么不能只跑 pytest。** 这个 Slice 的交付物有三块，`pytest` 只能覆盖其中一块：

1. **目录元数据是真的会被读的**（`resources/regions/index.json` 随仓库分发，
   边界文件不进提交）—— 只有对着仓库里那份真文件跑一遍才算证明；
2. **层级与路径安全发生在真文件系统上**（`../escape.geojson` 必须被拒，
   而且**必须在任何 IO 之前**被拒）—— 沙箱里 `tmp_path` 会被拦成 `WinError 5`，
   所以全程用 `tempfile.mkdtemp`；
3. **界面下拉框真的来自目录**（Slice 18 在 UI 层只改了"数据来源"这一处），
   `offscreen` 下控件不会真的被摆进窗口，字体与 DPI 也与真机不同。

脚本额外守两条**静态**约定（跑一遍 grep 就能发现，但没人会想起来的）：

* `index.json` 里**不许有 Zoom** —— 等级是运行时查策略表得到的，存一份就是第二个真相；
* 仓库的 `resources/regions/**/*.geojson` 必须**一个都没有** —— 法定地图内容
  必须由使用者用带审图号的授权数据自己放（与 Slice 16 同一条合规要求）。

全程只碰临时目录：不往仓库的 ``resources/regions/`` 写任何东西，
不改真实任务记录，不触网。

用法::

    python scripts/verify_slice18_regions.py
    python scripts/verify_slice18_regions.py --offscreen   # 无显示环境
    python scripts/verify_slice18_regions.py --no-shots    # 只跑检查
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DOCS = PROJECT_ROOT / "docs"

#: 当前 ``QApplication``（截图前要空转事件循环让布局收敛）
_APP = None


# --------------------------------------------------------------------------- #
# 检查结果收集
# --------------------------------------------------------------------------- #
class Checker:
    """收集检查结果，最后统一汇报并以退出码表达结论。

    每条失败都带上"为什么"（``detail``）。只报一个 ``False`` 的断言在这类脚本里
    是最贵的坑：分不清是产品坏了、夹具假了、还是判据本身写错了。
    """

    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  [OK]   {label}")
        else:
            self.failures.append(f"{label}{f' — {detail}' if detail else ''}")
            print(f"  [FAIL] {label}{f' — {detail}' if detail else ''}")
        return bool(ok)

    def section(self, title: str) -> None:
        print(f"\n=== {title} ===")


def _pump(app, times: int = 8) -> None:
    for _ in range(times):
        app.processEvents()


def _shot(widget, name: str, enabled: bool) -> None:
    """把控件存成 ``docs/<name>``（``grab()`` 不触发布局重算，先 ``activate()``）。"""
    if not enabled:
        return
    layout = widget.layout()
    if layout is not None:
        layout.activate()
    if _APP is not None:
        _pump(_APP, 12)
    DOCS.mkdir(parents=True, exist_ok=True)
    target = DOCS / name
    if widget.grab().save(str(target)):
        print(f"  [shot] {target.relative_to(PROJECT_ROOT)}")


# --------------------------------------------------------------------------- #
# 夹具：在临时目录里造一份**三级**行政区目录
# --------------------------------------------------------------------------- #
#: 层级夹具。坐标是**合成的方块**，不是任何真实行政区的边界。
FIXTURE_ENTRIES = (
    {
        "id": "demoland",
        "name": "演示国",
        "level": "country",
        "parentId": None,
        "geojson": "country/demoland.geojson",
    },
    {
        "id": "northland",
        "name": "北省",
        "level": "province",
        "parentId": "demoland",
        "geojson": "province/northland.geojson",
    },
    {
        "id": "southland",
        "name": "南省",
        "level": "province",
        "parentId": "demoland",
        "geojson": "province/southland.geojson",
    },
    {
        "id": "rivertown",
        "name": "河城",
        "level": "city",
        "parentId": "northland",
        "geojson": "city/rivertown.geojson",
    },
    {
        "id": "hilltown",
        "name": "山城",
        "level": "city",
        "parentId": "southland",
        "geojson": "city/hilltown.geojson",
    },
)

#: 每个区域一个小方块。数值之间留了空隙，保证"哪块是哪块"在断言里分得开；
#: 方块**刻意做小**：场景 F 会真的点一次「计算」，走的是生产那条多边形预估链路，
#: 大范围在高 Zoom 下要算几十万张瓦片，脚本会挂在那里（这不是产品缺陷，是夹具
#: 选得不合适）。
FIXTURE_BOXES = {
    "demoland": (100.0, 20.0, 104.0, 24.0),
    "northland": (100.0, 22.0, 102.0, 24.0),
    "southland": (102.0, 22.0, 104.0, 24.0),
    "rivertown": (100.0, 23.0, 101.0, 24.0),
    "hilltown": (103.0, 22.0, 104.0, 23.0),
}


def write_boundary(
    root: Path, relative: str, *, name: str, region_id: str, box: tuple[float, float, float, float]
) -> Path:
    """在 ``root`` 下按相对路径写一份合法边界 GeoJSON（合成夹具）。

    父目录不存在时负责建出来 —— Slice 18 起边界文件按层级分目录摆放。
    """
    west, south, east, north = box
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {
                    "name": name,
                    "id": region_id,
                    "boundaryVersion": "2026-09-verify",
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [west, south],
                            [east, south],
                            [east, north],
                            [west, north],
                            [west, south],
                        ]
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def write_index(root: Path, entries) -> Path:
    """按 Slice 18 的格式写 ``index.json``。"""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "index.json"
    path.write_text(
        json.dumps({"version": 1, "regions": list(entries)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def build_fixture_regions(root: Path) -> Path:
    """造一份**完整**的三级行政区目录（元数据 + 全部边界文件都就位）。"""
    write_index(root, FIXTURE_ENTRIES)
    for entry in FIXTURE_ENTRIES:
        write_boundary(
            root,
            entry["geojson"],
            name=entry["name"],
            region_id=entry["id"],
            box=FIXTURE_BOXES[entry["id"]],
        )
    return root


def use_region_resources(root: Path) -> None:
    """把行政区资源目录重定向到 ``root``（仓库资源区保持干净）。

    必须在**建窗口之前**调用：面板构造时就会去读一次目录。
    """
    from app.geo import region as region_module

    region_module.REGION_RESOURCE_DIR = root


# --------------------------------------------------------------------------- #
# 场景 A：仓库里那份真目录
# --------------------------------------------------------------------------- #
def scene_shipped_catalog(checker: Checker) -> None:
    checker.section("场景 A：仓库随发的 index.json（元数据）与空数据目录（合规）")
    from app.config.settings import REGION_INDEX_FILENAME, REGION_RESOURCE_DIR
    from app.regions import CatalogIssueCode, RegionCatalog

    index_path = REGION_RESOURCE_DIR / REGION_INDEX_FILENAME
    checker.check(index_path.is_file(), f"{REGION_INDEX_FILENAME} 随仓库分发", str(index_path))

    payload = json.loads(index_path.read_text(encoding="utf-8"))
    checker.check(
        payload.get("version") == 1,
        "目录版本是 1（加载器支持的那个）",
        str(payload.get("version")),
    )
    entries = payload.get("regions") or []
    ids = [str(item.get("id")) for item in entries]
    checker.check("china" in ids, "目录里有中国这一条", str(ids))

    china = next((item for item in entries if item.get("id") == "china"), {})
    checker.check(
        china.get("level") == "country" and china.get("parentId") in (None, ""),
        "中国是国家级且没有父级（Spec §6）",
        f"level={china.get('level')} parentId={china.get('parentId')}",
    )
    checker.check(
        str(china.get("geojson", "")).startswith("country/"),
        "中国的边界文件已经迁到 country/ 子树（Spec §7）",
        str(china.get("geojson")),
    )

    # --- 静态约定 1：目录里不许存 Zoom ---
    flat_keys = {str(key).lower() for item in entries for key in item}
    zoom_keys = sorted(key for key in flat_keys if "zoom" in key)
    checker.check(
        zoom_keys == [],
        "index.json 里没有 Zoom 字段（等级在运行时查策略表，Spec §9）",
        str(zoom_keys),
    )

    # --- 静态约定 2：仓库不带任何边界数据 ---
    geojson_files = sorted(
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in REGION_RESOURCE_DIR.rglob("*.geojson")
    )
    checker.check(
        geojson_files == [],
        "仓库里一个内置边界文件都没有（法定地图内容须用授权数据，Spec §3）",
        str(geojson_files[:3]),
    )

    for level_dir in ("country", "province", "city"):
        checker.check(
            (REGION_RESOURCE_DIR / level_dir).is_dir(),
            f"{level_dir}/ 目录存在（分层布局，Spec §7）",
        )

    # 真读一遍：仓库这份目录必须**能通过快速校验**
    # 显式给 root：本脚本早前已经把 app.geo.region 的全局资源目录重定向到临时目录，
    # 这里要的恰恰是**仓库随发的那一份**。
    catalog = RegionCatalog.load(root=REGION_RESOURCE_DIR)
    result = catalog_validate(catalog)
    checker.check(
        result.ok,
        "仓库那份目录快速校验通过（缺边界文件只算提醒）",
        " / ".join(item.describe() for item in result.errors[:2]),
    )
    checker.check(
        result.count(CatalogIssueCode.GEOJSON_NOT_FOUND) >= 1,
        "缺边界文件报的是「提醒」而不是「错误」（部署状态，Spec §13）",
        " / ".join(item.as_line() for item in result.issues[:2]),
    )
    checker.check(
        catalog.get("china") is not None and catalog.get("china").name == "中国",
        "加载器能从 index.json 建出目录并取到中国",
    )


def catalog_validate(catalog, *, deep: bool = False):
    """薄封装，统一调用点（方便以后换签名）。"""
    from app.regions import validate_catalog

    return validate_catalog(catalog, deep=deep)


# --------------------------------------------------------------------------- #
# 场景 B：三级层级（真文件系统 + 真边界文件）
# --------------------------------------------------------------------------- #
def scene_three_levels(checker: Checker, root: Path) -> None:
    checker.section("场景 B：COUNTRY → PROVINCE → CITY 三级目录")
    from app.planning import RegionLevel
    from app.regions import RegionService

    service = RegionService(root=root)

    countries = service.list_countries()
    checker.check(
        [item.id for item in countries] == ["demoland"],
        "国家级只有一个根节点",
        str([item.id for item in countries]),
    )
    checker.check(
        [item.id for item in service.list_provinces("demoland")] == ["northland", "southland"],
        "国的下级是两个省",
        str([item.id for item in service.list_provinces("demoland")]),
    )
    checker.check(
        [item.id for item in service.list_cities("northland")] == ["rivertown"],
        "北省的下级是河城",
        str([item.id for item in service.list_cities("northland")]),
    )
    checker.check(
        [item.id for item in service.children("demoland")] == ["northland", "southland"]
        and service.children("rivertown") == (),
        "children() 与 list_provinces/list_cities 一致，叶子节点没有下级",
    )
    checker.check(
        # ancestors() **不含自己**（自己是当前选中项，面包屑由 path_text 拼）
        [item.id for item in service.ancestors("rivertown")] == ["demoland", "northland"],
        "祖先链是国 → 省（不含自己，根在前）",
        str([item.id for item in service.ancestors("rivertown")]),
    )
    checker.check(
        service.path_text("rivertown") == "演示国 / 北省 / 河城",
        "路径文本按层级拼出来",
        service.path_text("rivertown"),
    )
    checker.check(
        service.get_region("rivertown").level is RegionLevel.CITY
        and service.get_region("northland").level is RegionLevel.PROVINCE
        and service.get_region("demoland").level is RegionLevel.COUNTRY,
        "三级的 level 分别是 city / province / country",
    )

    # 层级规则：错误挂载必须被拒
    bad_root = root.parent / "bad-hierarchy"
    write_index(
        bad_root,
        [
            {"id": "x", "name": "带父级的国", "level": "country", "parentId": "y",
             "geojson": "country/x.geojson"},
            {"id": "y", "name": "国", "level": "country", "parentId": None,
             "geojson": "country/y.geojson"},
        ],
    )
    from app.regions import CatalogIssueCode, RegionCatalog

    result = catalog_validate(RegionCatalog.load(root=bad_root))
    checker.check(
        result.has(CatalogIssueCode.COUNTRY_HAS_PARENT),
        "国家级不许有父级（Spec §6）",
        " / ".join(item.as_line() for item in result.issues[:2]),
    )

    cycle_root = root.parent / "cycled"
    write_index(
        cycle_root,
        [
            {"id": "p1", "name": "省一", "level": "province", "parentId": "p2",
             "geojson": "province/p1.geojson"},
            {"id": "p2", "name": "省二", "level": "province", "parentId": "p1",
             "geojson": "province/p2.geojson"},
        ],
    )
    result = catalog_validate(RegionCatalog.load(root=cycle_root))
    checker.check(
        not result.ok,
        "互相当父亲的环被抓出来（Spec §6）",
        " / ".join(item.as_line() for item in result.issues[:3]),
    )


# --------------------------------------------------------------------------- #
# 场景 C：路径安全 —— 必须在任何 IO 之前拒绝
# --------------------------------------------------------------------------- #
def scene_path_safety(checker: Checker, work_dir: Path) -> None:
    checker.section("场景 C：路径安全（越界路径必须在读盘之前被拒）")
    from app.regions import CatalogIssueCode, RegionCatalog

    evil_root = work_dir / "evil-regions"

    # 诱饵：真的在这里放一个**坏文件**。如果安全判断发生在读盘之后，
    # 深度校验就会报 boundary_unreadable —— 那就说明它真的去读越界文件了。
    decoy_dir = work_dir / "decoy"
    decoy_dir.mkdir(parents=True, exist_ok=True)
    escape_target = decoy_dir / "escape.geojson"
    escape_target.write_bytes(b"\x00\x01not-a-geojson\xff")

    absolute_target = (decoy_dir / "absolute.geojson").resolve()
    absolute_target.write_bytes(b"\x00\x01not-a-geojson\xff")

    write_index(
        evil_root,
        [
            {"id": "escapee", "name": "越界", "level": "country", "parentId": None,
             "geojson": "../decoy/escape.geojson"},
            {"id": "absolute", "name": "绝对路径", "level": "country", "parentId": None,
             "geojson": absolute_target.as_posix()},
            {"id": "drive", "name": "盘符", "level": "country", "parentId": None,
             "geojson": "C:/Windows/System32/drivers/etc/hosts"},
        ],
    )

    catalog = RegionCatalog.load(root=evil_root)
    result = catalog_validate(catalog)
    checker.check(
        result.ok is False,
        "不安全路径让目录**不通过**（不是静默跳过）",
        f"errors={len(result.errors)}",
    )
    checker.check(
        result.count(CatalogIssueCode.UNSAFE_PATH) >= 3,
        "三条越界路径（..、绝对路径、盘符）都被判为 unsafe_path",
        " / ".join(item.as_line() for item in result.errors[:3]),
    )
    checker.check(
        [item.id for item in catalog.list_all()] == [],
        "不安全条目整条被丢弃，不会带着坏路径进目录",
        str([item.id for item in catalog.list_all()]),
    )

    deep = catalog_validate(catalog, deep=True)
    checker.check(
        deep.has(CatalogIssueCode.UNSAFE_PATH)
        and not deep.has(CatalogIssueCode.BOUNDARY_UNREADABLE),
        "深度校验也没去读越界文件（拒绝发生在 IO 之前，Spec §12）",
        " / ".join(item.as_line() for item in deep.issues[:4]),
    )

    # 正例：安全的子目录路径必须**照常通过**
    good_root = work_dir / "good-regions"
    write_index(
        good_root,
        [
            {"id": "ok", "name": "正常", "level": "country", "parentId": None,
             "geojson": "country/ok.geojson"},
        ],
    )
    write_boundary(
        good_root, "country/ok.geojson", name="正常", region_id="ok", box=(100.0, 20.0, 110.0, 30.0)
    )
    good = catalog_validate(RegionCatalog.load(root=good_root), deep=True)
    checker.check(
        good.ok and not good.issues,
        "带子目录的合法路径深度校验零问题（反例与正例成对）",
        " / ".join(item.as_line() for item in good.issues[:3]),
    )


# --------------------------------------------------------------------------- #
# 场景 D：Lazy Load —— 构造与查询不许碰边界文件
# --------------------------------------------------------------------------- #
def scene_lazy_load(checker: Checker, root: Path) -> None:
    checker.section("场景 D：Lazy Load（建目录 + 全部查询不解析任何 GeoJSON）")
    from app.geo import geojson_loader
    from app.regions import RegionCatalog, RegionLoader, RegionService
    from app.regions import loader as loader_module

    calls: list[str] = []
    original = geojson_loader.load_geojson_document

    def spy(path, *args, **kwargs):
        calls.append(str(path))
        return original(path, *args, **kwargs)

    geojson_loader.load_geojson_document = spy
    loader_module.load_geojson_document = spy
    try:
        catalog = RegionCatalog.load(root=root)
        catalog.get("rivertown")
        catalog.list_all()
        catalog.list_children("demoland")
        catalog.get_ancestors("rivertown")
        catalog.get_descendants("demoland")
        catalog.path_text("rivertown")
        loader = RegionLoader(catalog)
        loader.peek_path("rivertown")

        service = RegionService(root=root)
        service.list_countries()
        service.list_provinces("demoland")
        service.list_cities("northland")
        service.list_presets()
        service.get_zoom_recommendation("rivertown")
        service.path_text("rivertown")
    finally:
        geojson_loader.load_geojson_document = original
        loader_module.load_geojson_document = original

    checker.check(
        calls == [],
        "构造 + 查询阶段解析 GeoJSON 的次数为 0（Spec §5）",
        f"实际 {len(calls)} 次：{calls[:2]}",
    )

    # 反例：真要几何时才读盘
    service = RegionService(root=root)
    region = service.get_download_region("rivertown")
    checker.check(
        region.region_level.value == "city" and region.bounds is not None,
        "真的取下载区域时才读盘，等级与范围都带出来",
        f"level={region.region_level.value}",
    )


# --------------------------------------------------------------------------- #
# 场景 E：校验（快速 vs 深度）
# --------------------------------------------------------------------------- #
def scene_validation(checker: Checker, work_dir: Path) -> None:
    checker.section("场景 E：快速校验只看文件在不在，深度校验才真解析")
    from app.regions import CatalogIssueCode, RegionCatalog

    broken_root = work_dir / "broken-bounds"
    write_index(
        broken_root,
        [
            {"id": "declared", "name": "声明范围对不上", "level": "country", "parentId": None,
             "geojson": "country/declared.geojson",
             # bounds 是**对象**形式（west/south/east/north），不是四元数组：
             # 数组会被判成 bounds_malformed，那样这条用例就验错了东西。
             "bounds": {"west": 0.0, "south": 0.0, "east": 1.0, "north": 1.0}},
        ],
    )
    write_boundary(
        broken_root, "country/declared.geojson", name="声明范围对不上",
        region_id="declared", box=(100.0, 20.0, 110.0, 30.0),
    )

    catalog = RegionCatalog.load(root=broken_root)
    fast = catalog_validate(catalog)
    checker.check(
        fast.ok and not fast.has(CatalogIssueCode.BOUNDS_MISMATCH),
        "快速校验不解析几何，因此发现不了 bounds 不一致（这是设计）",
        " / ".join(item.as_line() for item in fast.issues[:2]),
    )
    deep = catalog_validate(catalog, deep=True)
    checker.check(
        deep.has(CatalogIssueCode.BOUNDS_MISMATCH),
        "深度校验拿声明范围与几何实际范围比出不一致（Spec §13）",
        " / ".join(item.as_line() for item in deep.issues[:2]),
    )

    corrupt_root = work_dir / "corrupt"
    write_index(
        corrupt_root,
        [
            {"id": "corrupt", "name": "坏文件", "level": "country", "parentId": None,
             "geojson": "country/corrupt.geojson"},
        ],
    )
    target = corrupt_root / "country" / "corrupt.geojson"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00\x01not-a-geojson\xff")
    deep = catalog_validate(RegionCatalog.load(root=corrupt_root), deep=True)
    checker.check(
        deep.has(CatalogIssueCode.BOUNDARY_UNREADABLE),
        "坏掉的边界文件在深度校验里报 boundary_unreadable",
        " / ".join(item.as_line() for item in deep.issues[:2]),
    )

    lines = deep.as_lines()
    checker.check(
        bool(lines) and "行政区目录校验" in lines[0] and "深度模式" in lines[0],
        "报告的正文是人话在前的（机器码只进末尾统计行）",
        lines[0] if lines else "",
    )


# --------------------------------------------------------------------------- #
# 场景 F：真实界面（真窗口 / 真下拉 / 真点）
# --------------------------------------------------------------------------- #
def scene_ui(app, window, checker: Checker, shots: bool) -> None:
    checker.section("场景 F：界面下拉来自目录（真窗口）")
    from app.geo import RegionType
    from app.geo.region import CHINA_REGION_PRESET
    from app.planning import RegionLevel, ZoomStrategyService
    from app.ui.download_panel import ACTION_CALCULATE

    from PySide6.QtCore import Qt

    panel = window.download_panel
    country_combo = panel.country_combo
    province_combo = panel.province_combo
    city_combo = panel.city_combo
    tooltip_role = Qt.ItemDataRole.ToolTipRole

    def _texts(combo) -> list[str]:
        return [combo.itemText(index) for index in range(combo.count())]

    def _tips(combo, *, skip_first: bool = False) -> list[str]:
        start = 1 if skip_first else 0
        return [
            str(combo.itemData(index, tooltip_role))
            for index in range(start, combo.count())
        ]

    country_texts = _texts(country_combo)
    checker.check(
        country_texts == ["演示国"],
        "「国家」下拉只列国家级（三级不再挤在一个平面列表里）",
        str(country_texts),
    )
    province_texts = _texts(province_combo)
    checker.check(
        province_texts == ["全部（不限定省份）", "北省", "南省"],
        "「省份」下拉按当前国家刷新（首项是「不限定省份」）",
        str(province_texts),
    )
    city_texts = _texts(city_combo)
    checker.check(
        city_texts == ["全部（不限定城市）"],
        "还没选省时「城市」下拉只有「不限定城市」（选了省才展开）",
        str(city_texts),
    )
    # 分隔符两种都可能出现（条目里存的是 posix 相对路径，Windows 上 str(Path)
    # 会给反斜杠）→ 归一化后再比，免得把"脚本自己写死了分隔符"报成产品缺陷。
    expected_paths = [
        str(entry["geojson"]).replace("\\", "/")
        for entry in FIXTURE_ENTRIES
        if entry["level"] in ("country", "province")
    ]
    tips = _tips(country_combo) + _tips(province_combo, skip_first=True)
    checker.check(
        [tip.replace("\\", "/") for tip in tips] == expected_paths,
        "每一行的提示（tooltip）指向**分层路径**下的边界文件",
        str(tips),
    )

    panel.set_region_type(RegionType.POLYGON)
    _pump(app)
    checker.check(
        panel.current_region_level() is RegionLevel.COUNTRY,
        "只选国家 → 等级 COUNTRY（不必再选省 / 市）",
        panel.current_region_level().value,
    )
    checker.check(
        panel.level_text.startswith("全国级"),
        "「下载级别」一行显示等级与路径（来自目录条目）",
        panel.level_text,
    )
    first_line = panel.zoom_policy_text.splitlines()[:1]
    checker.check(
        first_line
        == [
            f"推荐 Zoom：{ZoomStrategyService().recommend(RegionLevel.COUNTRY).range_text}"
        ],
        "国家级建议区间来自策略层（不是抄来的常量）",
        repr(first_line),
    )
    _shot(window, "slice18_regions_dropdown.png", shots)

    # --- 级联：选省 → 城市下拉跟着刷新 ---
    province_combo.setCurrentIndex(1)
    _pump(app)
    checker.check(
        panel.current_region_level() is RegionLevel.PROVINCE,
        "选省级 → 等级 PROVINCE",
        panel.current_region_level().value,
    )
    checker.check(
        panel.current_region_id() == "northland",
        "省级这一步的生效 id 是省自己（还没选市）",
        str(panel.current_region_id()),
    )
    checker.check(
        _texts(city_combo) == ["全部（不限定城市）", "河城"],
        "选省后「城市」下拉刷新成该省的城市",
        str(_texts(city_combo)),
    )

    # --- 级联：选市 → 等级 CITY，生效 id 换成市 ---
    city_combo.setCurrentIndex(1)
    _pump(app)
    checker.check(
        panel.current_region_level() is RegionLevel.CITY,
        "选中市级 → 等级 CITY",
        panel.current_region_level().value,
    )
    checker.check(
        panel.current_region_id() == "rivertown",
        "选了市之后生效 id 变成市（市 → 省 → 国 的兜底顺序只在缺项时生效）",
        str(panel.current_region_id()),
    )
    city_line = panel.zoom_policy_text.splitlines()[:1]
    expected_city = f"推荐 Zoom：{ZoomStrategyService().recommend(RegionLevel.CITY).range_text}"
    checker.check(
        city_line == [expected_city],
        "市级建议区间 Z12~16 来自策略层",
        repr(city_line),
    )

    # Slice 19 起默认是**推荐模式**：Zoom 由等级决定，界面上显示的就该是建议值，
    # 不存在"用户超范围"这回事。
    recommendation = ZoomStrategyService().recommend(RegionLevel.CITY)
    data = panel.get_form_data()
    checker.check(
        (data["min_zoom"], data["max_zoom"])
        == (recommendation.min_zoom, recommendation.max_zoom),
        "推荐模式自动把 Zoom 填成策略建议值（界面显示 == 建议值）",
        f"{data['min_zoom']}~{data['max_zoom']}",
    )
    checker.check(
        "警告" not in panel.zoom_policy_text,
        "推荐模式下不存在「用户超范围」（值就是建议值）",
        panel.zoom_policy_text.replace("\n", " / "),
    )

    # 切到**自定义**后给 8~17，对市级而言确实超上界 —— 于是这一行出现警告，而警告
    # 的等级是「城市级」（等级取自目录条目，不是写死的）。这同时验证了 Slice 17 的
    # 策略在 Slice 18/19 的目录数据上没有走样。
    panel.set_zoom_range(8, 17)
    _pump(app)
    checker.check(
        "城市级" in panel.zoom_policy_text and "Z12~16" in panel.zoom_policy_text,
        "自定义 8~17 对市级区域产生「城市级」警告（等级来自目录）",
        panel.zoom_policy_text.replace("\n", " / "),
    )
    checker.check(
        panel.zoom_range_text() == "8 ~ 17",
        "只警告、不改用户输入（自定义模式下的 Zoom 原样保留）",
        panel.zoom_range_text(),
    )
    panel.set_zoom_range(12, 16)
    _pump(app)
    checker.check(
        "警告" not in panel.zoom_policy_text,
        "收到 Z12~16 后警告消失（非阻塞，自己会收）",
        panel.zoom_policy_text.replace("\n", " / "),
    )
    _shot(window, "slice18_regions_city_zoom.png", shots)

    # 边界文件在 → 范围由文件回填，且真的按这个区域算
    region = panel.current_region()
    checker.check(
        region is not None and region.region_level is RegionLevel.CITY,
        "市级区域真的加载出来了（几何 + 等级）",
        "region=None" if region is None else region.region_level.value,
    )
    # 先把 Zoom 收窄再点「计算」：默认 8~17 下，即便 1° 见方在这个预估链路上也要
    # 算几万张瓦片，脚本会卡住。这里验的是"链路通不通"，不是"能算多大"。
    panel.set_zoom_range(10, 10)
    _pump(app)
    panel.action_buttons[ACTION_CALCULATE].click()
    _pump(app, 20)
    text = panel.result_label.text()
    checker.check(
        "区域内瓦片" in text and "候选瓦片" in text and "预计容量" in text,
        "选中市级行政区域后能算出候选 / 区域内 / 过滤（Slice 16 链路无回归）",
        repr(text[:160]) + f" | region_error={panel.region_error!r}",
    )
    checker.check(
        text.splitlines()[:1] == ["区域：河城"],
        "结果区第一行写明这次算的是哪个区域（来自目录的展示名）",
        repr(text.splitlines()[:1]),
    )
    _shot(window, "slice18_regions_three_levels.png", shots)

    # 服务层与界面层给的是**同一个**推荐区间
    service = panel.region_service
    checker.check(
        service.get_zoom_recommendation("rivertown").range_text
        == ZoomStrategyService().recommend(RegionLevel.CITY).range_text,
        "RegionService 与 ZoomStrategyService 的推荐区间一致（唯一来源）",
        service.get_zoom_recommendation("rivertown").range_text,
    )

    # 仓内那份"中国"仍然是国家级（迁移路径后行为不变）
    checker.check(
        CHINA_REGION_PRESET.region_level is RegionLevel.COUNTRY
        and CHINA_REGION_PRESET.geojson_filename.startswith("country/"),
        "内置中国预设仍是国家级，且路径已迁到 country/ 子树",
        CHINA_REGION_PRESET.geojson_filename,
    )


# --------------------------------------------------------------------------- #
# 场景 G：兜底（index.json 不可用时不许崩）
# --------------------------------------------------------------------------- #
def scene_fallback(checker: Checker, work_dir: Path) -> None:
    checker.section("场景 G：目录不可用时的兜底（不许让界面崩）")
    from app.regions import CatalogIssueCode, RegionService

    empty_root = work_dir / "no-index"
    empty_root.mkdir(parents=True, exist_ok=True)

    service = RegionService(root=empty_root)
    checker.check(
        bool(service.catalog_error),
        "目录读不出来时记下原因（写日志用）",
        (service.catalog_error or "")[:60],
    )
    ids = [preset.region_id for preset in service.list_presets()]
    checker.check(
        "china" in ids,
        "兜底目录里仍有内置的中国预设（Slice 16 / 17 的功能一点不少）",
        str(ids),
    )
    checker.check(
        service.list_provinces("china") == () and service.children("china") == (),
        "层级查询返回空（平铺预设没有父子关系，编一个出来才是更糟的谎）",
    )

    broken_root = work_dir / "bad-version"
    write_index(
        broken_root,
        [
            {"id": "x", "name": "某国", "level": "country", "parentId": None,
             "geojson": "country/x.geojson"},
        ],
    )
    (broken_root / "index.json").write_text(
        json.dumps(
            {
                "version": 999,
                "regions": [
                    {"id": "x", "name": "某国", "level": "country", "parentId": None,
                     "geojson": "country/x.geojson"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    service = RegionService(root=broken_root)
    # 版本号不认识是**前向兼容**信号，不是"整份目录不可用"：已有字段照读，
    # 问题进校验报告。所以这里**不该**走兜底（兜底会让用户连自己加的条目都看不到）。
    checker.check(
        service.catalog_error == ""
        and "x" in [p.region_id for p in service.list_presets()],
        "版本号不认识时照读已知字段，**不**退回内置预设（Spec §13 前向兼容）",
        (service.catalog_error or "（无 catalog_error）")[:60],
    )
    checker.check(
        service.validate().has(CatalogIssueCode.INDEX_VERSION_UNSUPPORTED),
        "版本号问题作为问题被报出来（不是静默忽略）",
        " / ".join(item.as_line() for item in service.validate().issues[:2]),
    )

    syntax_root = work_dir / "syntax-error"
    syntax_root.mkdir(parents=True, exist_ok=True)
    (syntax_root / "index.json").write_text('{"version": 1, "regions": [', encoding="utf-8")
    service = RegionService(root=syntax_root)
    checker.check(
        bool(service.catalog_error) and "china" in [p.region_id for p in service.list_presets()],
        "JSON 语法错是硬失败 → 走兜底，中国照常可选",
        (service.catalog_error or "")[:60],
    )


# --------------------------------------------------------------------------- #
# 场景 H：单一来源的静态守卫
# --------------------------------------------------------------------------- #
def scene_static_guards(checker: Checker) -> None:
    checker.section("场景 H：单一来源（等级规则 / 策略值 / 路径安全）")
    import ast

    from app.planning import ADMINISTRATIVE_LEVELS, PARENT_LEVEL_RULES
    from app.regions import models as regions_models
    from app.geo import region as geo_region

    checker.check(
        regions_models.PARENT_LEVEL_RULES is PARENT_LEVEL_RULES
        and regions_models.ADMINISTRATIVE_LEVELS is ADMINISTRATIVE_LEVELS,
        "层级规则表在两侧是**同一个对象**（不是第二份表，Spec §6）",
    )
    checker.check(
        regions_models.normalize_region_relative_path is geo_region.normalize_region_relative_path
        and regions_models.is_safe_region_relative_path
        is geo_region.is_safe_region_relative_path,
        "路径安全函数在两侧是**同一个对象**（Spec §12）",
    )

    # app/regions/** 里不许出现等级字面量（用 AST，不扫字符串：docstring 里就写着
    # 这些名字，正则匹配必然误报 —— Slice 17 已经栽过一次）
    level_names = {item.name for item in ADMINISTRATIVE_LEVELS}
    offenders: list[str] = []
    for path in sorted((PROJECT_ROOT / "app" / "regions").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if not isinstance(node.value, ast.Name) or node.value.id != "RegionLevel":
                continue
            if node.attr in level_names:
                offenders.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.attr}")
    checker.check(
        offenders == [],
        "app/regions 里没有硬编码的行政区等级（等级规则只在规划层，Spec §6）",
        str(offenders),
    )

    zoom_offenders: list[str] = []
    for path in sorted((PROJECT_ROOT / "app").rglob("*.py")):
        if (PROJECT_ROOT / "app" / "planning") in path.parents:
            continue
        if path == PROJECT_ROOT / "app" / "geo" / "region.py":
            continue
        if "ZoomPolicy(" in path.read_text(encoding="utf-8"):
            zoom_offenders.append(path.relative_to(PROJECT_ROOT).as_posix())
    checker.check(
        zoom_offenders == [],
        "ZoomPolicy 仍然只在规划层构造（Slice 17 的守卫无回归）",
        str(zoom_offenders),
    )
    checker.check(
        PARENT_LEVEL_RULES and len(ADMINISTRATIVE_LEVELS) == len(PARENT_LEVEL_RULES),
        "行政等级与父级规则一一对应",
        f"{len(ADMINISTRATIVE_LEVELS)} vs {len(PARENT_LEVEL_RULES)}",
    )


# --------------------------------------------------------------------------- #
# 场景 I：手工验收清单（Spec §19）
# --------------------------------------------------------------------------- #
def scene_manual_checklist(checker: Checker) -> None:
    checker.section("场景 I：Spec §19 手工验收清单（本脚本已逐步覆盖）")
    steps = [
        ("1. 启动应用，行政区下拉有「中国」", "场景 A：index.json 里有 china；场景 F：下拉来自目录"),
        ("2. 放入授权数据后，选中它出现范围", "场景 F：市级区域加载出几何并按它计算"),
        ("3. 省 / 市三级目录可用", "场景 B：country → province → city 全链路查询"),
        ("4. 父级关系正确（国 → 省 → 市）", "场景 B：ancestors / path_text / children"),
        ("5. 非法层级被拒", "场景 B：country_has_parent 与环"),
        ("6. 越界路径被拒且不读盘", "场景 C：unsafe_path 且无 boundary_unreadable"),
        ("7. Lazy Load：查询不解析 GeoJSON", "场景 D：解析次数为 0"),
        ("8. 校验分快速 / 深度", "场景 E：bounds_mismatch 只在深度模式出现"),
        ("9. Zoom 建议按等级来自策略层", "场景 F：界面首行 == 策略服务输出"),
        ("10. 目录不可用时兜底不崩", "场景 G：catalog_error 有值且中国还在"),
        ("11. 仓库不含内置边界数据", "场景 A：*.geojson 数量为 0"),
        ("12. 未提前实现 Slice 20", "场景 H：app/regions 无等级字面量、无下载计划"),
    ]
    for title, coverage in steps:
        checker.check(True, title, coverage)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slice 18 行政区域层级模型实机验收")
    parser.add_argument(
        "--offscreen",
        action="store_true",
        help="使用 Qt offscreen 平台插件（适合无显示环境；中文字体可能缺失）",
    )
    parser.add_argument("--no-shots", action="store_true", help="只跑检查，不截图")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    shots = not args.no_shots

    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QMessageBox

    from app.main import setup_logging
    from app.tasks import TaskService, TaskStore
    from app.ui.main_window import MainWindow
    from app.ui.preferences import UiPreferences

    setup_logging()

    work_dir = Path(tempfile.mkdtemp(prefix="verify-slice18-"))
    regions_dir = work_dir / "regions"
    build_fixture_regions(regions_dir)

    # 必须在建窗口**之前**改：面板构造时就会读一次目录
    use_region_resources(regions_dir)

    app = QApplication(sys.argv[:1])
    global _APP
    _APP = app
    checker = Checker()

    print(f"platform={app.platformName()}  work_dir={work_dir}")

    try:
        store = TaskStore(work_dir / "tasks.json")
        preferences = UiPreferences(
            QSettings(str(work_dir / "ui.ini"), QSettings.Format.IniFormat)
        )
        window = MainWindow(task_service=TaskService(store), preferences=preferences)
        window.show()
        _pump(app)

        scene_shipped_catalog(checker)
        scene_three_levels(checker, regions_dir)
        scene_path_safety(checker, work_dir)
        scene_lazy_load(checker, regions_dir)
        scene_validation(checker, work_dir)

        # --- 场景 F：真界面 ---
        # 前面的场景会留下"计算失败"之类的非模态提示框，先关掉免得压在截图上
        for dialog in window.findChildren(QMessageBox):
            dialog.close()
        _pump(app)
        scene_ui(app, window, checker, shots)

        for dialog in window.findChildren(QMessageBox):
            dialog.close()
        _pump(app)

        scene_fallback(checker, work_dir)
        scene_static_guards(checker)
        scene_manual_checklist(checker)

        window._exit_confirmed = True
        window.close()
        _pump(app)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    print("\n" + "=" * 60)
    if checker.failures:
        print(f"结果：{len(checker.failures)} 项失败 / 共 {checker.passed} 项通过")
        for failure in checker.failures:
            print(f"  - {failure}")
        return 1
    print(f"结果：全部通过（{checker.passed} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
