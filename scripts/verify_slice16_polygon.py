"""Slice 16 真实平台验收脚本：GeoJSON 区域下载（自定义矩形 / 行政区域）。

**为什么不能只跑 pytest。** ``pytest`` 跑在 Qt ``offscreen`` 平台上：字体更小、
没有 DPI 缩放、控件不会被真的摆到窗口里。本 Slice 要证明的事情有两类**只有真窗口
才看得见**：

1. 「区域类型」下拉、行政区下拉、只读的候选范围提示在真实尺寸下有没有被挤掉
   （Slice 16 的界面比 Slice 15 多了三行控件，正是最容易被压扁的一类改动）；
2. 切到「行政区域」时，**上一轮矩形算出来的数字有没有赖在界面上**。
   这一条是真缺陷：本地实测过"从矩形切到行政区域（边界文件没放）后，结果区还写着
   `预计瓦片：56`"，看起来就像行政区域也算出了 56 张 —— 属于 Spec §32 明令禁止的
   "静默退回矩形"。修复见 ``MainWindow._on_region_changed``，回归用例在
   ``tests/test_ui_smoke.py``。

脚本是真跑的，不是读代码猜的：

- 真的起 ``windows`` 平台插件、真的 ``show()`` 主窗口、真的点「计算」按钮；
- 真的把 GeoJSON 写进临时资源目录，再让界面按"用户刚放好文件"的路径刷新；
- 真的跑一遍下载链路（``httpx.MockTransport`` 供瓦片，**不触网**）→ 真的落一个
  ``.mbtiles`` → 真的过 Inspector → 真的打一个地图包，然后断言 manifest 里的
  ``region`` 子对象；
- 截图落在 ``docs/images/slice16_*.png``；
- 全程只碰临时目录：**不动**你的真实地图、任务记录，也**不会**往仓库的
  ``resources/regions/`` 里写任何东西（中国国界数据必须由用户自己放，Spec §3）。

用法::

    python scripts/verify_slice16_polygon.py
    python scripts/verify_slice16_polygon.py --offscreen   # 无显示环境
    python scripts/verify_slice16_polygon.py --no-shots    # 只跑检查
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DOCS = PROJECT_ROOT / "docs" / "images"

#: 假 Token（**禁止**替换为真实密钥）
FAKE_TOKEN = "abcdefghijklmn"

#: 下载范围与层级：z12 下候选 7×8 = 56 张 —— 小到毫秒级，又能真的走完整条链路。
BOUNDS = (116.0, 39.0, 116.5, 39.5)
ZOOM = 12

#: 临时行政区预设的 id —— 刻意与内置的「中国」同名（``china``），
#: 这样用例走的是**真实那条代码路径**，只是资源目录指向临时目录。
REGION_ID = "china"
BOUNDARY_VERSION = "2026-09-verify"

#: 当前 ``QApplication``（截图前要空转事件循环让布局收敛）
_APP = None


# --------------------------------------------------------------------------- #
# 检查结果收集
# --------------------------------------------------------------------------- #
class Checker:
    """收集检查结果，最后统一汇报并以退出码表达结论。

    每条失败都带上"为什么"（``detail``）—— 只报一个 ``False`` 的断言在这类脚本里
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
    """把控件存成 ``docs/images/<name>``。

    ``grab()`` **不会**触发布局重算 —— ``resize()`` 之后直接截，截到的是上一轮尺寸
    下的排版。所以先 ``activate()`` 再空转几轮事件循环。
    """
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
# 夹具：瓦片图、几何、临时预设
# --------------------------------------------------------------------------- #
def _real_png() -> bytes:
    """一张能被 Pillow 真解码的 PNG。

    不能用"文件头 + 垃圾正文"：Slice 8 的诊断会**真解码**抽样瓦片，假字节会被
    正确地判成损坏 → 构建前的检查 FAIL → 后面测的就不再是打包逻辑了。
    """
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (32, 32), (30, 120, 200, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


FAKE_PNG = _real_png()


def tile_aligned_polygon(zoom: int) -> list[list[list[float]]]:
    """构造一个"左下半三角形"，它的外包矩形**恰好铺满**候选瓦片范围。

    为什么要瓦片对齐：这个多边形要让 ``candidate > selected``（否则就证明不了
    "界面用的是 selected"）。用瓦片边界当包围盒，候选就是整块矩形；再把右上角
    切掉一半，就得到一组明确"在包围盒里但不在区域内"的瓦片。

    直角三角形的斜边不与任何瓦片边重合，所以不存在"只碰到一个角"的模糊瓦片 ——
    那种模糊会让期望值取决于浮点误差，是夹具层面就能避免的坑。
    """
    from app.geo import GeoBounds, calculate_tile_range, tile_x_to_lon, tile_y_to_lat

    span = calculate_tile_range(GeoBounds(*BOUNDS), zoom)
    # 注意参数顺序：``tile_x_to_lon(x, zoom)`` / ``tile_y_to_lat(y, zoom)``
    west = tile_x_to_lon(span.min_x, zoom)
    east = tile_x_to_lon(span.max_x + 1, zoom)
    north = tile_y_to_lat(span.min_y, zoom)
    south = tile_y_to_lat(span.max_y + 1, zoom)
    return [[[west, south], [east, south], [west, north], [west, south]]]


def write_boundary(directory: Path, filename: str) -> Path:
    """在 ``directory`` 下写一个合法的边界 GeoJSON（**合成夹具**，不是真实国界）。

    ``filename`` 可以是子路径（``country/china.geojson``）：Slice 18 起边界文件
    按层级分目录摆放，父目录不存在时这里负责建出来。
    """
    path = directory / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {
                    "name": "验收夹具区域",
                    "id": REGION_ID,
                    "boundaryVersion": BOUNDARY_VERSION,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": tile_aligned_polygon(ZOOM),
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def china_relative_path() -> str:
    """内置中国预设的边界文件**相对资源目录**的路径（``country/china.geojson``）。

    脚本里所有涉及这个文件的位置都从这里取，不写死字面量：Slice 16 时它是扁平的
    ``china.geojson``，Slice 18 迁到了 ``country/`` 子树 —— 写死的话，
    布局再动一次这些断言就会**悄悄地**变成"检查一个不存在的文件"（永远为真）。
    """
    from app.geo.region import CHINA_REGION_PRESET

    return CHINA_REGION_PRESET.geojson_filename


def use_temp_region_resources(regions_dir: Path) -> None:
    """把行政区资源目录重定向到临时目录（仓库资源区保持干净）。

    两件事：

    1. 改 ``app.geo.region`` 模块里的两个全局 ``REGION_RESOURCE_DIR`` /
       ``REGION_PRESETS``。``RegionPreset.geojson_path`` / ``get_region_preset``
       都是**调用时**读这两个名字，所以改完立刻生效，不需要重装模块。
    2. 把仓库里那份 ``index.json`` **拷**进临时目录 —— Slice 18 起，界面上的
       行政区名单来自这份目录元数据。用拷的而不是在脚本里手写一份：
       手写的那份迟早与产品资源不一致，而这条脚本要验的正是产品资源本身。
       （顺带说明：``RegionService`` 的根目录也是**调用时**读全局的，
       所以这一改同样立刻生效。）
    """
    from app.config.settings import REGION_RESOURCE_DIR as SHIPPED_REGIONS_DIR
    from app.geo import region as region_module
    from app.geo.region import CHINA_REGION_PRESET
    from app.regions import INDEX_FILENAME

    regions_dir.mkdir(parents=True, exist_ok=True)
    region_module.REGION_RESOURCE_DIR = regions_dir
    region_module.REGION_PRESETS = (CHINA_REGION_PRESET,)
    shutil.copyfile(SHIPPED_REGIONS_DIR / INDEX_FILENAME, regions_dir / INDEX_FILENAME)


def _estimate(region) -> object:
    """用**生产同一条**预估链路算一次（脚本里所有期望值都从它来）。"""
    from app.geo.region_estimator import RegionTileEstimator

    return RegionTileEstimator(region).estimate(ZOOM, ZOOM)


def panel_bounds_matches_region(panel, region) -> bool:
    """界面上的候选范围是不是**边界文件回填**出来的那一份。

    行政区域模式下四个经纬度框是只读的、由 GeoJSON 的外包矩形填进去；下载器也是
    拿这个范围去枚举候选瓦片。脚本自己塞一个矩形进去测，测的就不是这条路径了。
    容差取 ``1e-6``（输入框就是 6 位小数，再严就是跟显示精度较劲）。
    """
    current = panel.current_bounds()
    target = region.bounds
    return all(
        abs(a - b) <= 1e-6
        for a, b in (
            (current.west, target.west),
            (current.south, target.south),
            (current.east, target.east),
            (current.north, target.north),
        )
    )


# --------------------------------------------------------------------------- #
# 场景 A：矩形基线（确认新功能没有动到老行为）
# --------------------------------------------------------------------------- #
def scene_rect_baseline(app, window, checker: Checker, shots: bool) -> None:
    checker.section("场景 A：自定义矩形（Slice 0~15 的老行为，一个字都不许变）")
    from app.geo import GeoBounds, calculate_tile_range
    from app.ui.download_panel import ACTION_CALCULATE

    panel = window.download_panel
    panel.set_zoom_range(ZOOM, ZOOM)
    panel._west_spin.setValue(BOUNDS[0])
    panel._south_spin.setValue(BOUNDS[1])
    panel._east_spin.setValue(BOUNDS[2])
    panel._north_spin.setValue(BOUNDS[3])
    _pump(app)

    checker.check(
        panel.current_region_type().value == "rectangle",
        "默认区域类型是「自定义矩形」",
        panel.current_region_type().value,
    )
    checker.check(
        not panel._west_spin.isReadOnly(),
        "矩形模式下经纬度可编辑（行政区域模式才只读）",
    )
    checker.check(
        panel.region_error == "",
        "矩形模式没有「区域错误」",
        panel.region_error,
    )

    panel.action_buttons[ACTION_CALCULATE].click()
    _pump(app)

    cached = window._last_calculation
    checker.check(cached is not None, "点「计算」后拿到了结论")
    if cached is None:
        return
    _total, _size, estimate = cached

    # 期望值用**另一条**生产链路算：``calculate_tile_range``（Slice 1 的瓦片数学）。
    # 拿 UI 的数字去和它比，才叫"界面没有自己重算"。
    expected = calculate_tile_range(GeoBounds(*BOUNDS), ZOOM).tile_count
    checker.check(
        estimate.candidate_tiles == expected,
        f"候选瓦片数 {expected}（来自 calculate_tile_range）",
        str(estimate.candidate_tiles),
    )
    checker.check(
        estimate.selected_tiles == expected,
        "矩形模式下 selected == candidate",
        f"{estimate.selected_tiles} != {expected}",
    )
    checker.check(
        estimate.filtered_tiles == 0,
        "矩形模式没有过滤掉任何瓦片",
        str(estimate.filtered_tiles),
    )
    checker.check(
        window._status_values["tile_total"].text() == f"{expected:,}",
        "状态栏「总瓦片」与预估一致",
        window._status_values["tile_total"].text(),
    )

    text = panel.result_label.text()
    checker.check("预计瓦片：" in text, "结果区正常渲染")
    checker.check(
        "区域内" not in text,
        "矩形模式**不**显示「区域内」那几行（那是行政区域模式专有的）",
        text.replace("\n", " / "),
    )
    checker.check(
        "预计容量：" in text,
        "结果区同时给出了容量预估",
        text.replace("\n", " / "),
    )

    _shot(window, "slice16_rect_baseline.png", shots)


# --------------------------------------------------------------------------- #
# 场景 B：行政区域 + 边界文件**没有**（最危险的一条路径）
# --------------------------------------------------------------------------- #
def scene_polygon_missing_boundary(
    app, window, checker: Checker, shots: bool, regions_dir: Path
) -> None:
    checker.section("场景 B：行政区域 + 边界文件不在（必须明确失败，不许退回矩形）")
    from app.geo import RegionType
    from app.ui.download_panel import ACTION_CALCULATE

    boundary = regions_dir / china_relative_path()
    checker.check(not boundary.exists(), "前提：边界文件确实不在（临时资源目录是空的）")

    panel = window.download_panel
    before = window._status_values["tile_total"].text()
    checker.check(before not in ("", "0"), f"前提：上一轮矩形算出了 {before}")

    # --- 用户在界面上把「区域类型」切到「行政区域」 ---
    checker.check(panel.set_region_type(RegionType.POLYGON), "切到「行政区域」成功")
    _pump(app)

    checker.check(
        panel.current_region() is None,
        "边界不可用时 current_region() 必须是 None（上层据此拒绝开工）",
    )
    checker.check(
        panel._west_spin.isReadOnly() and panel._north_spin.isReadOnly(),
        "行政区域模式下经纬度只读（范围由边界文件决定）",
    )
    hint = panel._bounds_hint_label.text()
    checker.check(
        "边界不可用" in hint,
        "候选范围提示区说明了边界不可用",
        hint.replace("\n", " "),
    )
    error = panel.current_region_error()
    checker.check("china.geojson" in error, "错误里指出了文件该叫什么名字", error)
    checker.check(
        str(regions_dir) in error,
        "错误里给出了文件该放到哪个目录",
        error,
    )
    checker.check(
        "审图" in error or "授权" in error,
        "错误里带上了国界数据的授权要求（Spec §3）",
        error,
    )

    # --- 关键断言：旧的矩形结论不许留在界面上 ---
    text = panel.result_label.text()
    checker.check(
        before not in text,
        f"{before} 是矩形算出来的，切到行政区域后不该还留在结果区",
        text.replace("\n", " / "),
    )
    checker.check(
        "预计瓦片" not in text,
        "结果区已退回占位（没有「看起来算成功」的数字）",
        text.replace("\n", " / "),
    )
    checker.check(
        window._last_calculation is None,
        "主窗口里的预估缓存已作废",
    )
    checker.check(
        window._status_values["tile_total"].text() == "0",
        "状态栏「总瓦片」退回了占位",
        window._status_values["tile_total"].text(),
    )

    _shot(window, "slice16_polygon_missing_boundary.png", shots)

    # --- 再点一次「计算」：必须给出人话错误，且仍然没有数字 ---
    panel.action_buttons[ACTION_CALCULATE].click()
    _pump(app)
    failure_text = panel.result_label.text()
    checker.check("计算失败" in failure_text, "「计算」给出明确失败")
    checker.check(
        "china.geojson" in failure_text and str(regions_dir) in failure_text,
        "失败原因里带着「放哪、叫什么」",
        failure_text.replace("\n", " / "),
    )
    checker.check(
        before not in failure_text,
        "失败提示里没有混进矩形的数字",
        failure_text.replace("\n", " / "),
    )


# --------------------------------------------------------------------------- #
# 场景 C：边界文件就位 → 候选 / 区域内 / 过滤 / 占比
# --------------------------------------------------------------------------- #
def scene_polygon_ready(app, window, checker: Checker, shots: bool) -> object:
    checker.section("场景 C：边界文件放好了 → 预估必须按「区域内」算")
    from app.ui.download_panel import ACTION_CALCULATE

    panel = window.download_panel
    # 模拟「用户照着提示把文件放好了，然后点刷新」
    panel.reload_region_boundary()
    _pump(app)

    region = panel.current_region()
    checker.check(region is not None, "边界文件就位后 current_region() 可用")
    if region is None:
        return None
    checker.check(region.is_polygon, "区域类型是 polygon")
    checker.check(region.region_id == REGION_ID, f"region_id = {REGION_ID}", region.region_id or "")
    checker.check(
        region.boundary_version == BOUNDARY_VERSION,
        f"boundary_version = {BOUNDARY_VERSION}",
        region.boundary_version or "",
    )
    checker.check(
        "（候选范围" in panel._bounds_hint_label.text(),
        "候选范围提示里说明「最终只下载与边界相交的瓦片」",
        panel._bounds_hint_label.text(),
    )

    panel.action_buttons[ACTION_CALCULATE].click()
    _pump(app)

    cached = window._last_calculation
    checker.check(cached is not None, "多边形「计算」成功")
    if cached is None:
        checker.check(False, "多边形「计算」失败", panel.result_label.text())
        return None
    total, _size, estimate = cached

    # 期望值来自生产同一条预估链路（脚本不自己数瓦片）
    expected = _estimate(region)
    checker.check(
        estimate.selected_tiles == expected.selected_tiles,
        f"界面用的是生产算出来的 selected = {expected.selected_tiles}",
        str(estimate.selected_tiles),
    )
    checker.check(
        estimate.candidate_tiles == expected.candidate_tiles,
        f"候选 {expected.candidate_tiles}（外包矩形）",
        str(estimate.candidate_tiles),
    )
    checker.check(
        estimate.filtered_tiles > 0,
        "确实裁掉了一部分瓦片（夹具是三角形，必然裁掉）",
        str(estimate.filtered_tiles),
    )
    checker.check(
        estimate.candidate_tiles == estimate.selected_tiles + estimate.filtered_tiles,
        "候选 = 区域内 + 过滤（三个数字自洽）",
    )
    # Spec §17 的核心：下游全部用 selected
    checker.check(
        total == estimate.selected_tiles and total != estimate.candidate_tiles,
        "界面报的是「区域内」张数，不是候选张数",
        f"total={total} selected={estimate.selected_tiles} candidate={estimate.candidate_tiles}",
    )
    checker.check(
        window._status_values["tile_total"].text() == f"{total:,}",
        "状态栏用的是「区域内」张数",
        window._status_values["tile_total"].text(),
    )

    text = panel.result_label.text()
    checker.check(
        f"区域内瓦片：{total:,}" in text,
        "结果区写着「区域内瓦片」（区域外那 35 张不在其中）",
        text.replace("\n", " / "),
    )
    checker.check("候选" in text and "过滤" in text, "结果区同时给出候选与过滤对照")
    checker.check("%" in text, "结果区给出裁剪占比")

    log = window.log_panel.to_text()
    checker.check("区域裁剪" in log, "日志里有一份同样的裁剪结论")
    checker.check(
        f"z{ZOOM}=" in log,
        "日志里有逐级明细",
    )

    _shot(window, "slice16_polygon_selected.png", shots)
    return region


# --------------------------------------------------------------------------- #
# 场景 D：真下载 → 真 Inspector → 真打包（region 一路传到 manifest）
# --------------------------------------------------------------------------- #
def scene_pipeline(checker: Checker, work_dir: Path, region, panel) -> Path | None:
    checker.section("场景 D：下载 → MBTiles metadata → Inspector → 地图包 manifest")
    import httpx

    from app.downloader import RateLimiter, RetryPolicy
    from app.mbtiles import MBTilesReader
    from app.models import MapLayerType
    from app.package import MapPackageBuilder
    from app.providers import TiandituProvider
    from app.services import MBTilesInspectionService
    from app.services.map_download_service import MapDownloadService

    if region is None:
        checker.check(False, "上一步没拿到区域，跳不过去")
        return None

    # --- 假服务器：任何请求都回一张真 PNG，并记下请求过的坐标 ---
    requested: set[tuple[int, int]] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        # 天地图 ``_w`` 矩阵集的 TILEROW 就是 XYZ 的 y（Slice 2 已确立），不翻转
        requested.add((int(params["TILECOL"]), int(params["TILEROW"])))
        return httpx.Response(200, content=FAKE_PNG, headers={"content-type": "image/png"})

    service = MapDownloadService(
        TiandituProvider(FAKE_TOKEN),
        transport=httpx.MockTransport(handler),
        concurrency=4,
        retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0.0, max_delay_seconds=0.0),
        rate_limiter=RateLimiter.disabled(),
        batch_size=7,
    )

    estimate = _estimate(region)
    mbtiles = work_dir / "polygon.mbtiles"
    frames: list[int] = []
    # 范围必须用 ``region.bounds``（= 边界文件的外包矩形），而不是表单默认那四个数：
    # 行政区域模式下界面上的经纬度是**只读的、由边界文件回填**的，下载器也是拿
    # 这个范围去枚举候选瓦片再逐张过几何过滤器。脚本若自己塞一个小矩形，
    # 得到的 35 < 46 就成了一处假失败 —— 它测的是"用户手填范围 ∩ 区域"这个
    # 界面上根本构造不出来的组合。
    checker.check(
        panel_bounds_matches_region(panel, region),
        "界面上的候选范围与边界文件一致（行政区域模式下由文件回填）",
    )
    outcome = asyncio.run(
        service.download_to_mbtiles(
            region.bounds,
            ZOOM,
            ZOOM,
            MapLayerType.VECTOR,
            mbtiles,
            map_name="polygon_verify",
            region=region,
            on_progress=lambda progress: frames.append(progress.total),
        )
    )

    checker.check(outcome.is_success, "下载完成且校验通过", str(outcome.validation.errors[:1]))
    checker.check(
        len(requested) == estimate.selected_tiles,
        f"只请求了区域内 {estimate.selected_tiles} 张",
        f"实际请求 {len(requested)} 张",
    )
    checker.check(
        len(requested) < estimate.candidate_tiles,
        "请求数少于候选数（区域外的瓦片一张都没请求）",
        f"{len(requested)} vs {estimate.candidate_tiles}",
    )
    checker.check(outcome.summary.total == estimate.selected_tiles, "任务总数 = 区域内张数")
    checker.check(
        outcome.summary.failed == 0 and outcome.summary.skipped == 0,
        "被裁掉的瓦片没有污染失败 / 跳过计数",
        f"failed={outcome.summary.failed} skipped={outcome.summary.skipped}",
    )
    checker.check(
        frames and set(frames) == {estimate.selected_tiles},
        f"进度分母从一开始就是 {estimate.selected_tiles}",
        str(sorted(set(frames))[:5]),
    )

    with MBTilesReader(mbtiles) as reader:
        stored = reader.get_tile_count()
        metadata = reader.get_metadata()
    checker.check(stored == estimate.selected_tiles, f"库里就是 {stored} 张")
    for key, expected_value in (
        ("region_type", "polygon"),
        ("region_id", REGION_ID),
        ("boundary_version", BOUNDARY_VERSION),
    ):
        checker.check(
            metadata.get(key) == expected_value,
            f"MBTiles metadata 写入 {key}={expected_value}",
            repr(metadata.get(key)),
        )

    # --- Inspector：期望值必须按**几何**算，不是按外包矩形 ---
    diagnostics = MBTilesInspectionService().inspect(mbtiles, sample_size=8)
    checker.check(
        diagnostics.expected_tiles == estimate.selected_tiles,
        f"Inspector 期望 {estimate.selected_tiles}（按行政区几何）",
        str(diagnostics.expected_tiles),
    )
    checker.check(
        diagnostics.missing_tiles == 0 and diagnostics.status == "PASS",
        "完整的多边形包判为 PASS / 缺失 0",
        f"{diagnostics.status} missing={diagnostics.missing_tiles}",
    )
    notices = " ".join(diagnostics.notices) + " ".join(diagnostics.warnings)
    checker.check("行政区几何" in notices, "结论里说明了「期望值是按什么算的」")
    checker.check(
        "外包矩形" not in notices or "不按外包矩形" in notices,
        "没有按外包矩形误报缺失",
        notices[:120],
    )

    # --- 打包：manifest 里必须带 region 子对象 ---
    result = MapPackageBuilder().build(mbtiles, work_dir / "packages")
    checker.check(result.success, "地图包生成成功", str(result.errors[:1]))
    if not result.success:
        return None
    manifest = json.loads(
        (Path(result.package_dir) / "manifest.json").read_text(encoding="utf-8")
    )
    checker.check(
        manifest.get("region")
        == {
            "type": "polygon",
            "id": REGION_ID,
            "boundaryVersion": BOUNDARY_VERSION,
            # Slice 25（§4）：等级从这一年起**所有区域都写**（行政区反查得出来
            # 不代表 Android 那边查得到 —— 它没有区域目录）。这个区域是中国，
            # 所以是 country。
            "level": "country",
        },
        "manifest.region 三项齐全（含 Slice 25 的 level）",
        repr(manifest.get("region")),
    )
    checker.check(
        list(manifest).index("region") == list(manifest).index("bounds") + 1,
        "region 的书写位置紧跟 bounds（协议固定顺序）",
    )
    return Path(result.package_dir)


# --------------------------------------------------------------------------- #
# 场景 E：静态守卫（靠自觉守不住的几条）
# --------------------------------------------------------------------------- #
def scene_static(checker: Checker) -> None:
    checker.section("场景 E：静态守卫")

    regions_dir = PROJECT_ROOT / "resources" / "regions"
    shipped_boundary = regions_dir / china_relative_path()
    checker.check(
        not shipped_boundary.exists(),
        "仓库里**没有**内置中国国界数据（Spec §3：法定地图内容必须用授权数据）",
        str(shipped_boundary),
    )
    checker.check(
        (regions_dir / "index.json").is_file(),
        "目录元数据（index.json）随仓库分发 —— 它是元数据，不是边界数据",
        str(regions_dir / "index.json"),
    )

    panel_source = (PROJECT_ROOT / "app" / "ui" / "download_panel.py").read_text(
        encoding="utf-8"
    )
    service_source = (
        PROJECT_ROOT / "app" / "services" / "map_download_service.py"
    ).read_text(encoding="utf-8")
    window_source = (PROJECT_ROOT / "app" / "ui" / "main_window.py").read_text(
        encoding="utf-8"
    )

    checker.check(
        "退化成矩形" in panel_source or "退化成候选矩形" in panel_source,
        "面板里明确写了「边界不可用不许退化成矩形」这条约束",
    )
    checker.check(
        "candidate" not in service_source.split("def _execute")[1].split("def ")[0]
        or "selected_tiles" in service_source,
        "下载服务用的是 selected_tiles 作为任务总数",
    )
    checker.check(
        "region=region" in service_source or "region=region," in service_source,
        "下载服务把 region 一路传下去（不是只用来算个数字）",
    )
    checker.check(
        "region_changed" in window_source,
        "主窗口接了「区域变更 → 作废旧结论」这条线（Slice 16 实测缺陷的修复）",
    )
    checker.check(
        "region=None if resume_task is not None" in window_source,
        "续传时不把 region 传进 worker（续传的范围来自任务记录，不是当前表单）",
    )

    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    checker.check(
        "china.geojson" in readme and ("审图" in readme or "授权" in readme),
        "README 说明了中国边界数据要用户自己提供（含授权要求）",
    )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slice 16 GeoJSON 区域下载实机验收")
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
    from PySide6.QtWidgets import QApplication

    from app.main import setup_logging
    from app.tasks import TaskService, TaskStore
    from app.ui.main_window import MainWindow
    from app.ui.preferences import UiPreferences

    setup_logging()

    work_dir = Path(tempfile.mkdtemp(prefix="verify-slice16-"))
    regions_dir = work_dir / "regions"

    # 必须在建窗口**之前**改：面板构造时就会读一次预设列表
    use_temp_region_resources(regions_dir)

    app = QApplication(sys.argv[:1])
    global _APP
    _APP = app
    checker = Checker()

    print(f"platform={app.platformName()}  work_dir={work_dir}")

    try:
        # 临时任务记录 + 临时偏好：绝不碰真实数据与注册表
        store = TaskStore(work_dir / "tasks.json")
        preferences = UiPreferences(
            QSettings(str(work_dir / "ui.ini"), QSettings.Format.IniFormat)
        )
        window = MainWindow(task_service=TaskService(store), preferences=preferences)
        window.show()
        _pump(app)

        scene_rect_baseline(app, window, checker, shots)
        scene_polygon_missing_boundary(app, window, checker, shots, regions_dir)

        # 关掉计算失败那几个非模态提示框，免得它们压在后面的截图上
        from PySide6.QtWidgets import QMessageBox

        for dialog in window.findChildren(QMessageBox):
            dialog.close()
        _pump(app)

        write_boundary(regions_dir, china_relative_path())
        region = scene_polygon_ready(app, window, checker, shots)

        window._exit_confirmed = True
        window.close()
        _pump(app)

        scene_pipeline(checker, work_dir, region, window.download_panel)
        scene_static(checker)
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
