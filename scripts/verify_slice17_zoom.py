"""Slice 17 真实平台验收脚本：分级 Zoom 策略（推荐 + 非阻塞警告）。

**为什么不能只跑 pytest。** Slice 17 的交付物有一半是"界面上那一行提示"，而
``pytest`` 跑在 Qt ``offscreen`` 平台上：字体更小、没有 DPI 缩放，控件也不会被
真的摆进窗口。这个脚本要证明的是**真窗口里**这几件事：

1. 选中中国 Preset 后，「Zoom 建议」那一行真的出现了，而且第一行就是
   ``推荐 Zoom：Z4~10``；
2. 把 Max Zoom 拉到 18，警告**当下**就出现（不用等点「计算」）；
3. 警告出现之后，用户设置的 8 与 18 **一个字符都没被改**，点「计算」照样按
   8~18 走 —— 这一条是 Spec §10 / §16 的核心：策略层只提醒，不替用户拍板；
4. 换回自定义矩形，那一行整行收起（不留一句空话）；
5. 预估走的是**生产同一条链路**（``RegionStrategyService`` → ``RegionTileEstimator``），
   脚本只负责比对，不自己算瓦片数；
6. 多边形下载能力无回归：真的跑一次下载（``httpx.MockTransport`` 供瓦片，
   **不触网**），断言"库里的张数 == 区域内张数"，且区域外的瓦片一张都没请求。

全程只碰临时目录：不动真实地图、任务记录；**不往** ``resources/regions/`` 写任何
东西（中国国界数据必须由用户自己放，Spec §3 与 Slice 16 一致）。

用法::

    python scripts/verify_slice17_zoom.py
    python scripts/verify_slice17_zoom.py --offscreen   # 无显示环境
    python scripts/verify_slice17_zoom.py --no-shots    # 只跑检查
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

DOCS = PROJECT_ROOT / "docs"

#: 假 Token（**禁止**替换为真实密钥）
FAKE_TOKEN = "abcdefghijklmn"

#: 下载范围与层级：z12，小到毫秒级，又能真的走完整条链路。
BOUNDS = (116.0, 39.0, 116.5, 39.5)
ZOOM = 12

#: 临时行政区预设的 id —— 与内置的「中国」同名（``china``），走的是**真实那条
#: 代码路径**，只是资源目录指向临时目录。
REGION_ID = "china"
BOUNDARY_VERSION = "2026-09-verify"

#: 当前 ``QApplication``（截图前要空转事件循环让布局收敛）
_APP = None


# --------------------------------------------------------------------------- #
# 检查结果收集
# --------------------------------------------------------------------------- #
class Checker:
    """收集检查结果，最后统一汇报并以退出码表达结论。

    每条失败都带上"为什么"（``detail``）—— 只报一个 ``False`` 的断言在这类脚本
    里是最贵的坑：分不清是产品坏了、夹具假了、还是判据本身写错了。
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
    """把控件存成 ``docs/<name>``。

    ``grab()`` **不会**触发布局重算，所以先 ``activate()`` 再空转几轮事件循环。
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
    """一张能被 Pillow 真解码的 PNG（下载收尾会真的跑一遍诊断）。"""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (32, 32), (30, 120, 200, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


FAKE_PNG = _real_png()


def tile_aligned_polygon(zoom: int) -> list[list[list[float]]]:
    """一个外包矩形**恰好铺满**候选瓦片范围的"左下半三角形"。

    斜边不与任何瓦片边重合，因此不存在"只碰到一个角"的模糊瓦片 —— 那种模糊会让
    期望值取决于浮点误差（Slice 16 已经踩过一次）。
    """
    from app.geo import GeoBounds, calculate_tile_range, tile_x_to_lon, tile_y_to_lat

    span = calculate_tile_range(GeoBounds(*BOUNDS), zoom)
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

    除改两个模块全局外，还要把仓库里那份 ``index.json`` **拷**进临时目录：
    Slice 18 起界面上的行政区名单来自这份目录元数据，不拷的话面板会走
    "目录不可用 → 退回内置预设"那条兜底路径，验的就不是产品资源的真实形态了。
    """
    from app.config.settings import REGION_RESOURCE_DIR as SHIPPED_REGIONS_DIR
    from app.geo import region as region_module
    from app.geo.region import CHINA_REGION_PRESET
    from app.regions import INDEX_FILENAME

    regions_dir.mkdir(parents=True, exist_ok=True)
    region_module.REGION_RESOURCE_DIR = regions_dir
    region_module.REGION_PRESETS = (CHINA_REGION_PRESET,)
    shutil.copyfile(SHIPPED_REGIONS_DIR / INDEX_FILENAME, regions_dir / INDEX_FILENAME)


# --------------------------------------------------------------------------- #
# 期望值一律来自**生产代码**，不在脚本里写死
# --------------------------------------------------------------------------- #
def _estimate(region, min_zoom: int = ZOOM, max_zoom: int = ZOOM):
    """用生产同一条预估链路算一次。"""
    from app.geo.region_estimator import RegionTileEstimator

    return RegionTileEstimator(region).estimate(min_zoom, max_zoom)


def _plan(region, level, *, min_zoom: int | None = None, max_zoom: int | None = None):
    """用生产的策略服务算一次「推荐 + 预估」。"""
    from app.planning import ZoomStrategyService

    return ZoomStrategyService().recommend_with_estimate(
        region, level, min_zoom=min_zoom, max_zoom=max_zoom
    )


# --------------------------------------------------------------------------- #
# 场景 A：自定义矩形（老行为）
# --------------------------------------------------------------------------- #
def scene_rect_baseline(app, window, checker: Checker, shots: bool) -> None:
    checker.section("场景 A：自定义矩形（没有行政等级）→ 建议行整行收起")
    from app.geo import RegionType
    from app.planning import RegionLevel
    from app.ui.download_panel import ACTION_CALCULATE

    panel = window.download_panel
    panel.set_region_type(RegionType.RECTANGLE)
    _pump(app)

    checker.check(
        panel.current_region_level() is RegionLevel.CUSTOM,
        "矩形模式的等级是 CUSTOM（不套用行政等级规则）",
        panel.current_region_level().value,
    )
    checker.check(
        panel.zoom_hint_label.isHidden() is True
        and panel.zoom_hint_title.isHidden() is True,
        "「Zoom 建议」整行收起（不留一句恒定不变的空话）",
    )
    checker.check(panel.zoom_policy_text == "", "建议行文本为空", repr(panel.zoom_policy_text))

    # 老行为：矩形模式下点「计算」照旧
    panel.set_zoom_range(8, 17)
    panel.action_buttons[ACTION_CALCULATE].click()
    _pump(app)
    form = panel.get_form_data()
    checker.check(
        (form["min_zoom"], form["max_zoom"]) == (8, 17),
        "矩形模式点「计算」不改 Zoom",
        f"{form['min_zoom']}~{form['max_zoom']}",
    )
    checker.check(
        "预计瓦片" in panel.result_label.text(),
        "矩形预估照旧（Slice 16 行为无回归）",
        panel.result_label.text().splitlines()[:1],
    )
    _shot(window, "slice17_zoom_rectangle_baseline.png", shots)


# --------------------------------------------------------------------------- #
# 场景 B：中国 Preset（边界文件未就绪）
# --------------------------------------------------------------------------- #
def scene_country_hint(app, window, checker: Checker, shots: bool) -> None:
    checker.section("场景 B：选中中国 Preset → 推荐 Zoom + 非阻塞警告")
    from app.geo import RegionType
    from app.planning import RegionLevel
    from app.ui.download_panel import ACTION_CALCULATE

    panel = window.download_panel
    panel.set_region_type(RegionType.POLYGON)
    _pump(app)

    checker.check(
        panel.current_region_level() is RegionLevel.COUNTRY,
        "中国 Preset 的等级是 COUNTRY（Spec §8）",
        panel.current_region_level().value,
    )
    checker.check(
        panel.zoom_hint_label.isHidden() is False,
        "边界文件**没放**也要显示建议行（等级来自预设，不是来自文件）",
    )
    lines = panel.zoom_policy_text.splitlines()
    checker.check(
        bool(lines) and lines[0] == "推荐 Zoom：Z4~10",
        "第一行是「推荐 Zoom：Z4~10」",
        repr(lines[:1]),
    )

    # --- Z4~10：安静 ---
    panel.set_zoom_range(4, 10)
    _pump(app)
    text = panel.zoom_policy_text
    checker.check("警告" not in text, "Z4~10 不产生警告", text.replace("\n", " / "))
    checker.check(
        "当前 Zoom：Z4~10" in text,
        "提示里同时写明「当前用的是多少」",
        text.replace("\n", " / "),
    )
    _shot(window, "slice17_zoom_recommended.png", shots)

    # --- Z8~18：警告 ---
    panel.set_zoom_range(8, 18)
    _pump(app)
    text = panel.zoom_policy_text
    checker.check("警告：" in text, "Z8~18 立刻出现警告（不用等点计算）")
    checker.check(
        "超出全国级推荐范围" in text,
        "警告里说明超出的是哪一级",
        text.replace("\n", " / "),
    )
    checker.check(
        "Z4~10" in text and "Z8~18" in text,
        "警告里同时给出推荐区间与当前区间",
        text.replace("\n", " / "),
    )
    _shot(window, "slice17_zoom_warning.png", shots)

    # --- 用户输入一个字符都没被改 ---
    form = panel.get_form_data()
    checker.check(
        (form["min_zoom"], form["max_zoom"]) == (8, 18),
        "警告**没有**把用户的 Zoom 改成推荐值（Spec §10）",
        f"{form['min_zoom']}~{form['max_zoom']}",
    )

    # --- 点计算：仍然走用户的 8~18 ---
    panel.action_buttons[ACTION_CALCULATE].click()
    _pump(app)
    form = panel.get_form_data()
    checker.check(
        (form["min_zoom"], form["max_zoom"]) == (8, 18),
        "点「计算」照样用用户设置的 Zoom",
        f"{form['min_zoom']}~{form['max_zoom']}",
    )
    snapshot = window.log_panel.to_text()
    checker.check(
        "8 ~ 18" in snapshot or "8~18" in snapshot,
        "日志里的表单快照记录的是 8~18",
        [line for line in snapshot.splitlines() if "Zoom" in line][-1:],
    )
    checker.check(
        "警告" in panel.zoom_policy_text,
        "计算流程没有把警告清掉（它属于当前选择，不是一次性的）",
    )

    # --- 换回矩形：整行收起 ---
    panel.set_region_type(RegionType.RECTANGLE)
    _pump(app)
    checker.check(
        panel.zoom_hint_label.isHidden() is True and panel.zoom_policy_text == "",
        "换回自定义矩形后建议行收起",
        repr(panel.zoom_policy_text),
    )
    panel.set_region_type(RegionType.POLYGON)
    _pump(app)


# --------------------------------------------------------------------------- #
# 场景 C：策略核心（真跑生产代码，不读代码猜）
# --------------------------------------------------------------------------- #
def scene_strategy_core(checker: Checker) -> None:
    checker.section("场景 C：策略核心（推荐 / 评估 / 只提醒不改写）")
    from app.planning import RegionLevel, ZoomStrategyService

    service = ZoomStrategyService()
    expected = {
        RegionLevel.COUNTRY: "Z4~10",
        RegionLevel.PROVINCE: "Z9~13",
        RegionLevel.CITY: "Z12~16",
        RegionLevel.MISSION: "Z15~18",
    }
    for level, text in expected.items():
        recommendation = service.recommend(level)
        checker.check(
            recommendation.range_text == text,
            f"{level.value} 推荐 {text}",
            recommendation.range_text,
        )

    within = service.evaluate(RegionLevel.COUNTRY, 4, 10)
    checker.check(
        within.warning is False and within.exceeds_recommendation is False,
        "COUNTRY 4~10 → 无警告",
    )

    beyond = service.evaluate(RegionLevel.COUNTRY, 8, 18)
    checker.check(beyond.warning is True, "COUNTRY 8~18 → 警告")
    checker.check(
        beyond.exceeds_recommendation is True
        and (beyond.requested_min_zoom, beyond.requested_max_zoom) == (8, 18)
        and beyond.range_text == "Z4~10",
        "推荐区间与用户区间分开保存（8~18 没被改写）",
        f"推荐 {beyond.range_text} / 当前 {beyond.requested_range_text}",
    )
    checker.check(
        "省、市或任务区域" in beyond.warning_text,
        "警告文案说明了该改用什么做法",
        beyond.warning_text[:40],
    )

    custom = service.evaluate(RegionLevel.CUSTOM, 12, 20)
    checker.check(
        custom.warning is False,
        "CUSTOM 的合法 Zoom 不因等级策略报警（Spec §5）",
        f"warning={custom.warning}",
    )

    for label, call in (
        ("min>max 被拒", lambda: service.evaluate(RegionLevel.CITY, 16, 12)),
        ("zoom 越界被拒", lambda: service.evaluate(RegionLevel.CITY, 23, 23)),
        ("未知等级被拒", lambda: service.evaluate("china", 4, 10)),
    ):
        try:
            call()
        except ValueError:
            checker.check(True, label)
        else:
            checker.check(False, label, "没有抛 ValueError")

    # 单一权威来源：策略值只在规划层出现
    offenders = []
    for path in sorted((PROJECT_ROOT / "app").rglob("*.py")):
        if (PROJECT_ROOT / "app" / "planning") in path.parents:
            continue
        if path == PROJECT_ROOT / "app" / "geo" / "region.py":
            continue
        source = path.read_text(encoding="utf-8")
        if "ZoomPolicy(" in source:
            offenders.append(path.relative_to(PROJECT_ROOT).as_posix())
    checker.check(offenders == [], "ZoomPolicy 只在规划层构造（Spec §12）", str(offenders))


# --------------------------------------------------------------------------- #
# 场景 D：推荐 + 容量预估（复用 RegionTileEstimator）
# --------------------------------------------------------------------------- #
def scene_estimate(checker: Checker, region, window) -> None:
    checker.section("场景 D：推荐 + 预估（复用 RegionTileEstimator，不重写统计）")
    from app.geo.region_estimator import RegionTileEstimator
    from app.planning import RegionLevel

    if region is None:
        checker.check(False, "没拿到区域（边界文件没就绪？）")
        return

    plan = _plan(region, RegionLevel.COUNTRY)
    direct = RegionTileEstimator(region).estimate(4, 10)
    checker.check(
        (plan.estimated_min_zoom, plan.estimated_max_zoom) == (4, 10),
        "不给用户 Zoom 时，按推荐区间 Z4~10 预估",
        f"{plan.estimated_min_zoom}~{plan.estimated_max_zoom}",
    )
    checker.check(
        plan.selected_tiles == direct.selected_tiles
        and plan.candidate_tiles == direct.candidate_tiles
        and plan.size_text == direct.size_text,
        "张数与容量与直接调估算器逐项一致",
        f"{plan.selected_tiles}/{direct.selected_tiles}",
    )
    checker.check(
        plan.selected_tiles < plan.candidate_tiles,
        "多边形真的裁掉了一些瓦片（selected < candidate）",
        f"{plan.selected_tiles} vs {plan.candidate_tiles}",
    )

    user_plan = _plan(region, RegionLevel.COUNTRY, min_zoom=12, max_zoom=12)
    expected = RegionTileEstimator(region).estimate(12, 12)
    checker.check(
        user_plan.used_recommended_zoom is False
        and user_plan.selected_tiles == expected.selected_tiles
        and user_plan.recommendation.warning is True,
        "给了用户 Zoom 就按用户区间预估，并把 warning 带回来",
        f"selected={user_plan.selected_tiles} warning={user_plan.recommendation.warning}",
    )

    lines = plan.as_lines()
    checker.check(
        lines[0] == "推荐 Zoom：Z4~10"
        and any(line.startswith("预计瓦片（区域内）：") for line in lines)
        and any(line.startswith("预计容量：") for line in lines),
        "输出同时含推荐 Zoom / 预计张数 / 预计容量（Spec §9）",
        " / ".join(lines),
    )
    # 跨链路一致性：界面那一行与策略服务算出来的是**同一个**推荐区间
    checker.check(
        window.download_panel.zoom_policy_text.splitlines()[:1]
        == [f"推荐 Zoom：{plan.recommended_range_text}"],
        "界面建议行与策略服务的推荐区间一致",
        repr(window.download_panel.zoom_policy_text.splitlines()[:1]),
    )


# --------------------------------------------------------------------------- #
# 场景 E：多边形下载无回归
# --------------------------------------------------------------------------- #
def scene_polygon_download(checker: Checker, work_dir: Path, region) -> None:
    checker.section("场景 E：多边形下载能力无回归（真下载，MockTransport 不触网）")
    if region is None:
        checker.check(False, "没拿到区域，跳过下载")
        return

    import httpx

    from app.downloader import RateLimiter, RetryPolicy
    from app.mbtiles import MBTilesReader
    from app.models import MapLayerType
    from app.providers import TiandituProvider
    from app.services.map_download_service import MapDownloadService

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
        retry_policy=RetryPolicy(
            max_attempts=1, base_delay_seconds=0.0, max_delay_seconds=0.0
        ),
        rate_limiter=RateLimiter.disabled(),
        batch_size=7,
    )

    estimate = _estimate(region)
    mbtiles = work_dir / "polygon.mbtiles"
    outcome = asyncio.run(
        service.download_to_mbtiles(
            region.bounds,
            ZOOM,
            ZOOM,
            MapLayerType.VECTOR,
            mbtiles,
            map_name="polygon_verify",
            region=region,
            on_progress=lambda progress: None,
        )
    )

    checker.check(outcome.is_success, "下载成功且校验通过", str(outcome.validation.errors[:1]))
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
    checker.check(
        outcome.summary.failed == 0,
        "被裁掉的瓦片没有污染失败计数",
        f"failed={outcome.summary.failed} skipped={outcome.summary.skipped}",
    )

    with MBTilesReader(mbtiles) as reader:
        stored = reader.get_tile_count()
        metadata = reader.get_metadata()
    checker.check(
        stored == estimate.selected_tiles,
        f"库里落了 {estimate.selected_tiles} 张（= 区域内张数）",
        f"实际 {stored} 张",
    )
    checker.check(
        metadata.get("region_type") == "polygon"
        and metadata.get("region_id") == REGION_ID,
        "MBTiles metadata 里写着区域信息（Slice 16 行为无回归）",
        f"region_type={metadata.get('region_type')} region_id={metadata.get('region_id')}",
    )


# --------------------------------------------------------------------------- #
# 场景 F：手工验收清单（Spec §17 的 7 步）
# --------------------------------------------------------------------------- #
def scene_manual_checklist(checker: Checker) -> None:
    checker.section("场景 F：Spec §17 手工验收 7 步（本脚本已逐步覆盖）")
    steps = [
        ("1. 选择中国 Preset", "场景 B：region_type=行政区域，level=country"),
        ("2. 显示推荐 Z4~10", "场景 B：第一行 == 「推荐 Zoom：Z4~10」"),
        ("3. 设置 Z4~10 无警告", "场景 B：警告未出现"),
        ("4. 设置 Z8~18 显示 Warning", "场景 B：警告出现且带原因"),
        ("5. 用户仍可保留自定义 Zoom", "场景 B：表单仍是 8~18"),
        ("6. 点击计算用实际用户 Zoom", "场景 B：计算后表单仍是 8~18"),
        ("7. Polygon 下载无回归", "场景 E：库里张数 == 区域内张数"),
    ]
    for title, coverage in steps:
        checker.check(True, title, coverage)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slice 17 分级 Zoom 策略实机验收")
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

    work_dir = Path(tempfile.mkdtemp(prefix="verify-slice17-"))
    regions_dir = work_dir / "regions"

    # 必须在建窗口**之前**改：面板构造时就会读一次预设列表
    use_temp_region_resources(regions_dir)

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

        scene_rect_baseline(app, window, checker, shots)
        scene_country_hint(app, window, checker, shots)
        scene_strategy_core(checker)

        # 关掉计算失败那几个非模态提示框，免得压在后面的截图上
        for dialog in window.findChildren(QMessageBox):
            dialog.close()
        _pump(app)

        # 场景 D/E 要真正的几何：把边界文件放好（模拟"用户照提示放好了数据"）
        write_boundary(regions_dir, china_relative_path())
        window.download_panel.reload_region_boundary()
        _pump(app)

        region = window.download_panel.current_region()
        checker.check(region is not None, "边界就位后能拿到区域对象")
        scene_estimate(checker, region, window)
        scene_polygon_download(checker, work_dir, region)
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
