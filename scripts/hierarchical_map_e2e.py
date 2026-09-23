#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""分级离线地图链路端到端验收（Slice 25 Spec §2）。

**它验证的是"整条分级链路还活着"，不是"某个函数算得对"。**

区别于 :mod:`scripts.e2e_smoke_test`（那一份验的是矩形链路：Bounds → TileMath →
MBTiles → Package），本脚本把 Slice 16~24 加进来的那一整层串起来跑：

    区域目录（country / province / city）
        → 多边形边界
        → Zoom 策略（唯一权威来源）
        → 多边形瓦片估算（候选 → 选中）
        → 任务区（航线 → 米制 Buffer）
        → 风险分级
        → 多级下载计划
        → 计划调度器（严格串行）
        → MBTiles
        → Inspector
        → 地图包

设计约束：

* **全程离线。** 瓦片由 :class:`MockTileServer` 通过 ``httpx.MockTransport``
  直接喂给**真实的**下载引擎 —— 走的是与生产完全相同的代码路径（真 SQLite、
  真写队列、真跳过判定、真 risk 分析），只是不发网络。理由同冒烟测试：
  一个需要 Token、受网络影响的验收脚本，最终一定会被人跳过。
* **不依赖 Qt。** 只用 ``app.geo`` / ``app.regions`` / ``app.mission`` /
  ``app.planning`` / ``app.mbtiles`` / ``app.package`` 这些纯逻辑包。
  GUI 的验收由 ``scripts/verify_sliceN_*.py`` 负责。
* **不写死推导值。** 每一段里出现的数字都要么来自同一套生产函数，要么来自
  被测对象的属性 —— 脚本自己算出来的常量只用来**选"跑得动"的区间**。
* **失败返回非 0 退出码**，可直接进 CI。

规模约定（这一条很重要）
------------------------

分级推荐区间**不能照单全下**：本夹具的 ``democity`` 覆盖 Z12~16 需要
13 万张瓦片，``demoprovince`` 覆盖 Z9~13 需要 9800 张 —— 那不是"验收失败"，
而是"夹具选得不合适"（工作区记忆里的坑 47）。所以：

* **第 2 段**断言"策略表给出的推荐区间恰好是 4~10 / 9~13 / 12~16 / 15~18"
  —— 推荐区间由**策略表**覆盖，不靠真下载；
* **第 6/7 段**的四级计划取**每级推荐区间的下界片段**（country Z4~6、
  province Z9~10、city Z12、mission Z15），既落在各自的推荐区间里，
  又把总量压到千级（实测约 1500 张）。

用法::

    python scripts/hierarchical_map_e2e.py
    python scripts/hierarchical_map_e2e.py --keep      # 保留产物，便于人工翻看
    python scripts/hierarchical_map_e2e.py --zoom-boost  # "跑得动"优先（默认）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import struct
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

# 允许 `python scripts/hierarchical_map_e2e.py` 直接跑（把仓库根加进 sys.path）。
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# --------------------------------------------------------------------------- #
# 资源目录重定向必须在**任何** RegionService 实例化之前（区域目录是全局单例语义）。
# --------------------------------------------------------------------------- #
import app.geo.region as _region_module                      # noqa: E402

#: 分级夹具目录（自建几何，**不是**真实行政边界，见该目录的 README）。
HIERARCHY_DIR = BACKEND_ROOT / "tests" / "fixtures" / "regions" / "hierarchy"

#: 任务区航线（LineString）。
MISSION_ROUTE_PATH = HIERARCHY_DIR / "mission_route.geojson"

#: 假 Token（**不是真实密钥**，只用于拼出合法的 WMTS URL）。
FAKE_TOKEN = "abcdefghijklmn"

#: 假瓦片边长。32 够小（省时间），又足够"像个瓦片"。
TILE_SIZE = 32

#: 四级计划各自的 Zoom（每级推荐区间的**下界片段**，见模块 docstring 的规模约定）。
PLAN_ZOOMS: dict[str, tuple[int, int]] = {
    "democountry": (4, 6),
    "demoprovince": (9, 10),
    "democity": (12, 12),
    "mission": (15, 15),
}

#: 第 4 段要比的三个走廊半径（Spec §7：500m / 5km / 20km）。
MISSION_BUFFERS: tuple[float, ...] = (500.0, 5000.0, 20000.0)

#: 第 4 段比 selected_tiles 用的单一 Zoom（任务区推荐区间的下界）。
MISSION_ZOOM = 15

#: 期望的推荐区间（Spec §3 / §5：唯一权威来源，四个等级各一条）。
EXPECTED_RECOMMENDED: dict[str, tuple[int, int]] = {
    "country": (4, 10),
    "province": (9, 13),
    "city": (12, 16),
    "mission": (15, 18),
}

#: 目录里的三级行政区（顺序即层级：country → province → city）。
ADMIN_IDS: tuple[str, ...] = ("democountry", "demoprovince", "democity")


def _expected_levels(mission_id: str) -> dict[str, str]:
    """``region_id → 期望等级``（Spec §4）。

    行政那三级从**夹具目录本身**读，不在这里手抄一份：目录里改了等级而这里
    没跟着改的话，第 ⑩ 段就会拿着过期的手抄表替产品签字
    （"等级落进包里了"验的却是另一回事）。任务区不在目录里，按调用方给的
    id 单独写 —— 它的 id 是运行时生成的（``mission-002`` 之类），写死必错。
    """
    index = json.loads((HIERARCHY_DIR / "index.json").read_text(encoding="utf-8"))
    levels = {item["id"]: str(item["level"]) for item in index["regions"]}
    levels[mission_id] = "mission"
    return levels


def _configure_region_resources() -> None:
    """把区域资源根指到分级夹具目录（模块级副作用，只做一次）。"""
    _region_module.REGION_RESOURCE_DIR = HIERARCHY_DIR


# --------------------------------------------------------------------------- #
# 假瓦片 / 假服务端
# --------------------------------------------------------------------------- #
def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    """拼一个 PNG 数据块：长度 + 类型 + 数据 + CRC32。"""
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def make_png(rgb: tuple[int, int, int], size: int = TILE_SIZE) -> bytes:
    """生成一张纯色 PNG（16 位真彩，不带 alpha）。

    刻意**手写**而不是用 PIL：验收脚本不该为了造一张纯色图去依赖图像库，
    而且手写的字节是确定的 —— "同一份输入永远得到同一份产物"在验收里很值钱。
    """
    raw = b"".join(
        b"\x00" + bytes(rgb) * size  # 每行开头一个 filter byte（0 = None）
        for _ in range(size)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, 6))
        + _png_chunk(b"IEND", b"")
    )


#: 瓦片颜色按坐标轮换：写错行列号时图片内容会跟预期不符。
_TILE_COLORS: tuple[tuple[int, int, int], ...] = (
    (214, 92, 78), (92, 158, 214), (120, 190, 120),
    (222, 186, 96), (168, 128, 200), (96, 196, 190),
)


class MockTileServer:
    """假天地图服务端：按 WMTS 参数回一张纯色 PNG，并记录每一次请求。

    请求参数名照搬天地图 WMTS（``TILEMATRIX`` / ``TILECOL`` / ``TILEROW``），
    于是"下载器到底按什么坐标去取"这件事在验收里是可见的 —— 那正是这类
    集成问题最容易错的地方。
    """

    def __init__(self, delay: float = 0.0) -> None:
        self.requests: list[tuple[int, int, int]] = []
        self.delay = delay
        self._png_cache: dict[tuple[int, int], bytes] = {}

    async def __call__(self, request) -> "object":  # httpx.Request → httpx.Response
        import httpx

        params = request.url.params
        matrix = int(params["TILEMATRIX"])
        col = int(params["TILECOL"])
        row = int(params["TILEROW"])
        self.requests.append((matrix, col, row))
        if self.delay:
            await asyncio.sleep(self.delay)
        rgb = _TILE_COLORS[(col + row) % len(_TILE_COLORS)]
        data = self._png_cache.get(rgb)
        if data is None:
            data = make_png(rgb)
            self._png_cache[rgb] = data
        return httpx.Response(200, content=data, headers={"content-type": "image/png"})

    @property
    def count(self) -> int:
        return len(self.requests)


# --------------------------------------------------------------------------- #
# 结果收集
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    """一项检查的结论。"""

    label: str
    passed: bool
    detail: str = ""
    notes: list[str] = field(default_factory=list)


class Runner:
    """串起十个阶段，逐段记录结论。"""

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir
        self.output_dir = work_dir / "output"
        self.checks: list[Check] = []
        self.notes_buffer: list[str] = []

        # 跨阶段共享的事实
        self.region_service = None
        self.mission_service = None
        self.mission_region = None
        self.plan = None
        self.plan_service = None
        self.execution = None
        self.outputs: dict[str, Path] = {}
        self.packages: dict[str, Path] = {}

    # ------------------------------------------------------------------ #
    # 记录
    # ------------------------------------------------------------------ #
    def note(self, text: str) -> None:
        """记一条补充信息（随后一次 ``section`` 会把它一起打印）。"""
        self.notes_buffer.append(text)

    def check(self, condition: bool, label: str, detail: str = "") -> bool:
        """子检查；失败会被收集起来，段级结论返回 ``False``。"""
        passed = bool(condition)
        if not passed:
            self.checks.append(Check(label=label, passed=False, detail=detail))
            print(f"       [FAIL] {label}" + (f" — {detail}" if detail else ""))
        return passed

    def section(self, title: str, passed: bool, detail: str = "") -> bool:
        """段级结论 —— 打印 Spec §2 要求的那一行 ``[PASS]/[FAIL] <段名>``。"""
        flag = "[PASS]" if passed else "[FAIL]"
        print(f"{flag} {title}" + (f" — {detail}" if detail else ""))
        for note in self.notes_buffer:
            print(f"       {note}")
        self.notes_buffer.clear()
        self.checks.append(Check(label=title, passed=passed, detail=detail))
        return passed

    # ------------------------------------------------------------------ #
    # ① Region hierarchy
    # ------------------------------------------------------------------ #
    def section_region_hierarchy(self) -> bool:
        """区域目录 + 三级几何 + **严格嵌套**（Spec §1 / §3）。

        嵌套这一条要真的算，不能靠"看起来像"：三个多边形都是凸的，
        所以"顶点都在父多边形内"等价于"整体在父多边形内"。
        """
        from shapely.geometry import shape

        from app.mission import CompositeRegionService, MissionRegionService, MissionRegionStore
        from app.planning.zoom_strategy import ZoomStrategyService
        from app.regions.service import RegionService

        strategy = ZoomStrategyService()
        base = RegionService(strategy=strategy)
        self.mission_service = MissionRegionService(
            MissionRegionStore(self.work_dir / "missions"),
            strategy=strategy,
            reference_files=(
                ("下载任务记录", self.work_dir / "tasks.json"),
                ("下载计划", self.work_dir / "plans.json"),
            ),
        )
        self.region_service = CompositeRegionService(base, self.mission_service)

        ok = True
        ids = [preset.id for preset in base.catalog.list_all()]
        ok &= self.check(ids == list(ADMIN_IDS), "目录读到了三级行政区",
                         f"实际 {ids}")
        self.note(f"目录：{' → '.join(ids)}")

        shapes: dict[str, object] = {}
        for region_id in ADMIN_IDS:
            region = base.get_download_region(region_id)
            geometry = shape(_load_geojson_geometry(base.resolve_path(region_id)))
            shapes[region_id] = geometry
            self.note(
                f"{region_id}: level={region.region_level.value} "
                f"type={region.region_type.value} area={geometry.area:.4f} 平方度"
            )

        for child, parent in (("demoprovince", "democountry"), ("democity", "demoprovince")):
            contained = shapes[parent].contains(shapes[child])
            ok &= self.check(contained, f"{child} ⊂ {parent}（严格嵌套）")
        # 反向也要证一次：不嵌套时上面的断言会假绿。
        ok &= self.check(
            not shapes["democity"].contains(shapes["demoprovince"]),
            "反向不成立（嵌套断言不是恒真）",
        )
        # 多边形不是矩形：否则"多边形过滤"与"外包矩形"结果相同，任何忘了做
        # 相交过滤的实现都会静默通过（夹具 README 里写着这一条）。
        for region_id in ADMIN_IDS:
            geom = shapes[region_id]
            ok &= self.check(
                abs(geom.area - _bbox_area(geom)) > 1e-6,
                f"{region_id} 不是矩形（多边形过滤才有意义）",
                f"area={geom.area:.4f} bbox={_bbox_area(geom):.4f}",
            )
        return self.section("Region hierarchy", ok)

    # ------------------------------------------------------------------ #
    # ② Zoom strategy
    # ------------------------------------------------------------------ #
    def section_zoom_strategy(self) -> bool:
        """推荐区间来自**唯一权威来源**（Spec §5）。

        断言的是"策略表给出的四个区间恰好是设计值"，而不是"某个界面显示了
        某个数字" —— 后者是 GUI 验收的活。
        """
        from app.planning.region_level import RegionLevel
        from app.planning.zoom_strategy import ZoomStrategyService

        strategy = ZoomStrategyService()
        ok = True
        for name, expected in EXPECTED_RECOMMENDED.items():
            level = RegionLevel.parse(name)
            policy = strategy.get_policy(level)
            actual = (policy.recommended_min_zoom, policy.recommended_max_zoom)
            ok &= self.check(
                actual == expected,
                f"{name} 推荐区间是 Z{expected[0]}~{expected[1]}",
                f"实际 Z{actual[0]}~{actual[1]}",
            )
            self.note(f"{name:9} 推荐 Z{actual[0]}~{actual[1]}  警告阈值 Z{policy.warning_max_zoom}")

        # "区间刻意重叠"是一条要被钉住的**意图**（Skill：别让下一个人顺手改整齐）。
        country = strategy.get_policy(RegionLevel.COUNTRY)
        province = strategy.get_policy(RegionLevel.PROVINCE)
        ok &= self.check(
            country.recommended_max_zoom >= province.recommended_min_zoom,
            "全国与省级的推荐区间刻意重叠（放大能接上下一档）",
            f"全国上界 Z{country.recommended_max_zoom} "
            f"vs 省级下界 Z{province.recommended_min_zoom}",
        )
        # CUSTOM 在合法范围内永不警告（Spec §5）。
        custom = strategy.get_policy(RegionLevel.CUSTOM)
        ok &= self.check(
            custom.warning_max_zoom >= 18,
            "自定义等级在合法 Zoom 范围内不产生等级警告",
            f"warning_max_zoom={custom.warning_max_zoom}",
        )
        return self.section("Zoom strategy", ok)

    # ------------------------------------------------------------------ #
    # ③ Polygon estimate
    # ------------------------------------------------------------------ #
    def section_polygon_estimate(self) -> bool:
        """候选 → 选中，以及"**不是**按中心点判断"（Spec §6）。

        最后一条是本段的重点：按瓦片中心点判会把边界上的瓦片整片丢掉，
        地图边缘出现锯齿状空洞。判据是"存在一张瓦片，其中心不在多边形内、
        但被选中"——相交判定才会选它。
        """
        from shapely.geometry import Point, shape

        from app.geo.region_estimator import RegionTileEstimator

        estimator = RegionTileEstimator(self.region_service.get_download_region("demoprovince"))
        result = estimator.estimate(9, 10)

        ok = True
        ok &= self.check(
            result.selected_tiles < result.candidate_tiles,
            "多边形过滤真的生效（选中 < 候选）",
            f"candidate={result.candidate_tiles} selected={result.selected_tiles}",
        )
        ok &= self.check(
            result.selected_tiles > 0,
            "过滤后仍选中了瓦片（不是把所有瓦片都滤掉）",
            f"selected={result.selected_tiles}",
        )
        self.note(
            f"demoprovince Z9~10: 候选 {result.candidate_tiles} → 选中 "
            f"{result.selected_tiles}（滤掉 {result.filtered_tiles}，"
            f"{result.filtered_percent_text}）"
        )

        # 逐级明细：每一级都该满足"选中 ≤ 候选"，而且过滤比例随 Zoom 上升
        # （多边形相对瓦片越大，边界那一圈的占比越小）。
        monotonic = all(
            stats.selected_tiles <= stats.candidate_tiles for stats in result.per_zoom
        )
        ok &= self.check(monotonic, "逐级明细里每一级都有 选中 ≤ 候选")

        # ---- 相交判定 vs 中心点判定的分水岭 ----
        region_filter = estimator.region_filter
        bounds = estimator.region.bounds
        geometry = shape(
            _load_geojson_geometry(self.region_service.resolve_path("demoprovince"))
        )
        center_hits = 0
        intersect_hits = 0
        for zoom in (10, 11):
            if zoom > 11:
                continue
            for x, y in region_filter.iter_selected_tiles(bounds, zoom):
                tile_bounds = _tile_bounds(zoom, x, y)
                center = Point(
                    (tile_bounds[0] + tile_bounds[2]) / 2,
                    (tile_bounds[1] + tile_bounds[3]) / 2,
                )
                intersect_hits += 1
                if geometry.contains(center):
                    center_hits += 1
        ok &= self.check(
            intersect_hits - center_hits > 0,
            "存在「中心点在外、但相交所以被选中」的边界瓦片"
            "（说明没有退化成中心点判断）",
            f"相交 {intersect_hits} 张，其中中心点在区域内 {center_hits} 张",
        )
        self.note(
            f"Z10~11 边界瓦片：相交判定选中 {intersect_hits} 张，"
            f"按中心点会漏掉 {intersect_hits - center_hits} 张"
        )
        return self.section("Polygon estimate", ok)

    # ------------------------------------------------------------------ #
    # ④ Mission region
    # ------------------------------------------------------------------ #
    def section_mission_region(self) -> bool:
        """航线 → 米制 Buffer → 走廊多边形（Spec §7）。

        要证明的是**单调性**：半径变大 → 面积变大 → 选中瓦片变多。
        只比面积是不够的（面积变大不代表瓦片变多），所以两条都要。
        """
        from shapely.geometry import shape

        from app.geo.region_estimator import RegionTileEstimator

        ok = True
        created = []
        areas: list[float] = []
        tiles: list[int] = []
        for radius in MISSION_BUFFERS:
            mission = self.mission_service.create(
                f"走廊 {int(radius)} 米",
                route_path=MISSION_ROUTE_PATH,
                buffer_meters=radius,
            )
            created.append(mission)
            # 面积由**几何自己**算，不从半径反推 —— 反推等于把被测对象的答案
            # 抄进断言里。单位是平方度，只用于"严格变大"这条单调性比较，
            # 不代表地面面积（那是米制投影的活）。
            corridor = shape(_load_geojson_geometry(mission.geojson_path))
            areas.append(corridor.area)
            region = self.region_service.get_download_region(mission.id)
            estimate = RegionTileEstimator(region).estimate(MISSION_ZOOM, MISSION_ZOOM)
            tiles.append(estimate.selected_tiles)
            self.note(
                f"buffer {int(radius):>6} 米 → 走廊面积 {corridor.area:.2f} 平方度, "
                f"Z{MISSION_ZOOM} 选中 {estimate.selected_tiles} 张"
            )

        ok &= self.check(
            areas == sorted(areas) and len(set(areas)) == len(areas),
            "半径变大 → 走廊面积严格变大",
            f"{[round(a, 2) for a in areas]} 平方度",
        )
        ok &= self.check(
            tiles == sorted(tiles) and len(set(tiles)) == len(tiles),
            "半径变大 → 选中瓦片严格变多",
            f"{tiles} 张",
        )

        # 里程碑：取 5km 那一版作为后面的计划成员。
        self.mission_region = created[1]
        mission_level = self.region_service.get_download_region(
            self.mission_region.id
        ).region_level
        ok &= self.check(
            mission_level.value == "mission",
            "任务区在**下载链路上**的等级是 mission（几何反查不出来，必须落在包里）",
            f"level={mission_level.value}",
        )

        # Resume 必须用**完全相同的持久化几何**（Spec §7）：
        # 重新 `get()` 一次拿到的路径必须与创建时那份一致，且内容没变过。
        reloaded = self.mission_service.get(self.mission_region.id)
        same_path = str(reloaded.geojson_path) == str(self.mission_region.geojson_path)
        ok &= self.check(same_path, "重新加载任务区拿到同一份几何文件",
                         f"{reloaded.geojson_path}")
        snapshot = Path(reloaded.geojson_path).read_bytes()
        reloaded_again = self.mission_service.get(self.mission_region.id)
        ok &= self.check(
            Path(reloaded_again.geojson_path).read_bytes() == snapshot,
            "第二次加载几何内容逐字节不变（Resume 不会拿到另一份几何）",
        )
        self.note(f"计划成员：{self.mission_region.id}（buffer {int(self.mission_region.buffer_meters)} 米）")
        return self.section("Mission region", ok)

    # ------------------------------------------------------------------ #
    # ⑤ Risk analysis
    # ------------------------------------------------------------------ #
    def section_risk_analysis(self) -> bool:
        """三个固定案例（Spec §8）。

        案例 B 刻意用**合成估算快照**而不是真遍历：本夹具的全国范围在 Z8~18
        是 2700 万张（工作区记忆的坑 47：真跑会挂死）。这**不算放水** ——
        ``DownloadRiskAnalyzer.analyze()`` 的契约就是"只读已算好的估算"
        （Spec §19 明令不许重复遍历瓦片），喂一个快照恰好正面证明这一点。
        快照里的每个数字都由**同一套瓦片数学**算出来，不是手写的常量。
        """
        from types import SimpleNamespace

        from app.config.settings import DEFAULT_AVERAGE_TILE_SIZE
        from app.geo.bounds import GeoBounds
        from app.geo.region_estimator import RegionTileEstimator, ZoomRegionStats
        from app.geo.tile_math import calculate_tile_range
        from app.planning.download_risk_analyzer import DownloadRiskAnalyzer
        from app.planning.region_level import RegionLevel
        from app.planning.risk_level import RiskLevel
        from app.planning.zoom_strategy import ZoomStrategyService

        analyzer = DownloadRiskAnalyzer(strategy=ZoomStrategyService())
        ok = True

        # ---- 案例 A：全国 Z4~10（真估算，491 张）→ 合理风险 ----
        country_region = self.region_service.get_download_region("democountry")
        estimator = RegionTileEstimator(country_region)
        case_a_estimate = estimator.estimate(4, 10)
        case_a = analyzer.analyze(
            RegionLevel.COUNTRY, 4, 10, case_a_estimate
        )
        ok &= self.check(
            case_a.level is RiskLevel.NORMAL,
            "案例 A：全国 Z4~10（推荐区间）→ NORMAL",
            f"实际 {case_a.level.value}（{case_a.total_tiles} 张）",
        )
        ok &= self.check(
            not case_a.requires_confirmation and not case_a.requires_strong_confirmation,
            "案例 A 不要求任何确认（合理规模不该拦人）",
        )
        ok &= self.check(
            (case_a.recommended_min_zoom, case_a.recommended_max_zoom) == (4, 10),
            "案例 A 的报告里带着推荐区间 Z4~10",
            f"实际 Z{case_a.recommended_min_zoom}~{case_a.recommended_max_zoom}",
        )
        self.note(
            f"案例 A：{case_a.total_tiles:,} 张 / "
            f"{case_a.estimated_bytes / 1024 / 1024:.1f} MB → {case_a.level.value}"
        )
        for reason in case_a.reasons:
            self.note(f"       原因：{reason}")

        # ---- 案例 B：全国 Z8~18（合成快照，2700 万张）→ DANGEROUS ----
        bounds = GeoBounds(110.0, 30.0, 116.0, 36.0)
        per_zoom = tuple(
            ZoomRegionStats(
                zoom=zoom,
                candidate_tiles=calculate_tile_range(bounds, zoom).tile_count,
                selected_tiles=calculate_tile_range(bounds, zoom).tile_count,
                filtered_tiles=0,
            )
            for zoom in range(8, 19)
        )
        total_tiles = sum(stats.selected_tiles for stats in per_zoom)
        case_b_estimate = SimpleNamespace(
            region_name=country_region.name,
            selected_tiles=total_tiles,
            estimated_size_bytes=total_tiles * DEFAULT_AVERAGE_TILE_SIZE,
            per_zoom=per_zoom,
        )
        case_b = analyzer.analyze(RegionLevel.COUNTRY, 8, 18, case_b_estimate)
        ok &= self.check(
            case_b.level is RiskLevel.DANGEROUS,
            "案例 B：全国 Z8~18（超大估算）→ DANGEROUS",
            f"实际 {case_b.level.value}（{case_b.total_tiles:,} 张）",
        )
        ok &= self.check(
            case_b.requires_strong_confirmation,
            "案例 B 要求强确认（超大任务必须先拦一下）",
        )
        ok &= self.check(
            case_b.over_recommended_tiles > 0 and case_b.over_recommended_percent > 0,
            "案例 B 的报告里给出「超出推荐上界」的占比（Spec §8）",
            f"{case_b.over_recommended_tiles:,} 张 / "
            f"{case_b.over_recommended_percent:.1f}%",
        )
        ok &= self.check(
            bool(case_b.recommendations),
            "案例 B 的报告里给出了建议方案（不只甩一个危险等级）",
        )
        self.note(
            f"案例 B：{case_b.total_tiles:,} 张 / "
            f"{case_b.estimated_bytes / 1024 / 1024 / 1024:.1f} GB → {case_b.level.value}"
            f"（超推荐 {case_b.over_recommended_percent:.1f}%）"
        )

        # ---- 案例 C：**真正小**的任务区 Z15~18 → 不能仅因 Zoom 高被判危险 ----
        # 夹具那条 80 km 航线在 Z18 是几十万张 —— 那是"大任务"，不是本案例要验的
        # "高 Zoom 但很小"。所以这里临时建一条约 3 km 的短航线，500 m 走廊。
        short = self.mission_service.create(
            "短航线小任务区",
            waypoints=[(112.40, 32.60), (112.42, 32.62)],
            buffer_meters=500.0,
        )
        short_region = self.region_service.get_download_region(short.id)
        case_c_estimate = RegionTileEstimator(short_region).estimate(15, 18)
        case_c = analyzer.analyze(RegionLevel.MISSION, 15, 18, case_c_estimate)
        ok &= self.check(
            case_c.level is RiskLevel.NORMAL,
            "案例 C：小任务区 Z15~18（Zoom 很高但范围很小）→ NORMAL",
            f"实际 {case_c.level.value}（{case_c.total_tiles:,} 张）",
        )
        ok &= self.check(
            case_c.over_recommended_percent == 0.0,
            "案例 C 没有「超出推荐」的张数（Z15~18 就是任务区的推荐区间）",
            f"{case_c.over_recommended_percent}%",
        )
        self.note(
            f"案例 C：{case_c.total_tiles:,} 张 → {case_c.level.value}"
            "（高 Zoom 本身不是危险信号，规模才是）"
        )
        # 对照：同等级、同 Zoom 区间，把范围换成夹具那条 80 km 大走廊 ——
        # 结论就变了。这一条钉住"风险来自规模，不来自 Zoom 数值"。
        big_region = self.region_service.get_download_region(self.mission_region.id)
        big = analyzer.analyze(
            RegionLevel.MISSION,
            15,
            18,
            RegionTileEstimator(big_region).estimate(15, 18),
        )
        ok &= self.check(
            big.total_tiles > case_c.total_tiles,
            "同样的 Zoom 区间下，大走廊的规模确实比小任务区大",
            f"{big.total_tiles:,} vs {case_c.total_tiles:,}",
        )
        self.note(
            f"对照：同等级同 Zoom 区间，80 km 大走廊 {big.total_tiles:,} 张 → "
            f"{big.level.value}（规模才是风险来源）"
        )
        return self.section("Risk analysis", ok)

    # ------------------------------------------------------------------ #
    # ⑥ Download plan
    # ------------------------------------------------------------------ #
    def section_download_plan(self) -> bool:
        """四级计划：顺序 + disabled 语义（Spec §9）。"""
        from app.planning.plan_service import PlanService
        from app.planning.plan_store import PlanStore
        from app.planning.region_level import RegionLevel
        from app.tasks.task_service import TaskService
        from app.tasks.task_store import TaskStore

        self.output_dir.mkdir(parents=True, exist_ok=True)
        task_service = TaskService(TaskStore(self.work_dir / "tasks.json"))
        self.plan_service = PlanService(
            store=PlanStore(self.work_dir / "plans.json"),
            service=self.region_service,
            task_service=task_service,
        )

        ok = True
        for region_id, (low, high) in PLAN_ZOOMS.items():
            key = self.mission_region.id if region_id == "mission" else region_id
            self.plan_service.add_region(
                key, use_recommended_zoom=False, min_zoom=low, max_zoom=high
            )
        self.plan_service.apply_settings(name="分级 E2E", output_dir=self.output_dir)
        self.plan_service.estimate()
        self.plan = self.plan_service.plan

        ordered = [item.region_id for item in self.plan.ordered_enabled_items()]
        expected_order = [
            "democountry", "demoprovince", "democity", self.mission_region.id
        ]
        ok &= self.check(
            ordered == expected_order,
            "计划顺序是 全国 → 省 → 市 → 任务区（大范围先下）",
            f"实际 {ordered}",
        )
        levels = [item.region_level for item in self.plan.ordered_enabled_items()]
        ok &= self.check(
            levels == [RegionLevel.COUNTRY, RegionLevel.PROVINCE,
                       RegionLevel.CITY, RegionLevel.MISSION],
            "四级的等级依次递增（顺序来自等级规则，不是手工排的）",
            f"实际 {[level.value for level in levels]}",
        )
        for item in self.plan.ordered_enabled_items():
            self.note(
                f"{item.region_id:14} {item.level_text:6} {item.zoom_text:9} "
                f"{item.tiles_text:>9} 张  {item.size_text:>10}"
            )

        # ---- disabled 语义：不估算到总量 / 不进 Risk / 不生成 Task ----
        total_before = self.plan.total_tiles
        city_item = next(
            item for item in self.plan.items if item.region_id == "democity"
        )
        city_tiles = city_item.selected_tiles
        ok &= self.check(city_tiles > 0, "本市项在启用时是估算过的（下面才比得出差异）")
        self.plan_service.set_enabled("democity", False)
        self.plan = self.plan_service.plan
        disabled_item = next(
            item for item in self.plan.items if item.region_id == "democity"
        )
        ok &= self.check(
            disabled_item.enabled is False and disabled_item.counts_toward_total is False,
            "被禁用的项不计入总量",
        )
        ok &= self.check(
            self.plan.total_tiles == total_before - city_tiles,
            "计划总量正好少了被禁用那一项的张数",
            f"{total_before} → {self.plan.total_tiles}（差 {city_tiles}）",
        )
        ok &= self.check(
            all(item.region_id != "democity" for item in self.plan.ordered_enabled_items()),
            "被禁用的项不进入可执行序列（不会生成任务、不会执行）",
        )
        # 被禁用的项**仍然要有估算数字**（用户要看得到"打开它要多少张"）。
        ok &= self.check(
            disabled_item.candidate_tiles > 0,
            "被禁用的项仍保留估算（用户能看到「如果打开它要多少」）",
        )
        self.note(f"禁用 democity 后总量 {total_before:,} → {self.plan.total_tiles:,}")

        # 恢复（后面要真跑四项）。
        self.plan_service.set_enabled("democity", True)
        self.plan_service.estimate()
        self.plan = self.plan_service.plan
        ok &= self.check(
            self.plan.total_tiles == total_before,
            "重新启用并重算后总量回到原值",
            f"{self.plan.total_tiles} vs {total_before}",
        )
        self.note(f"四项合计 {self.plan.total_tiles:,} 张")
        return self.section("Download plan", ok)

    # ------------------------------------------------------------------ #
    # ⑦ Scheduler
    # ------------------------------------------------------------------ #
    def section_scheduler(self) -> bool:
        """严格串行地跑完四项（Spec §10）。

        "串行"的判据不是"耗时"也不是"输出文件顺序"，而是**任意时刻最多只有
        一条处于 running** —— 用 ``on_item_changed`` 回调逐帧检查这个不变量。
        """
        import httpx

        from app.downloader import RateLimiter, RetryPolicy
        from app.planning.plan_execution_store import PlanExecutionStore
        from app.planning.plan_scheduler import MapDownloadTaskRunner, PlanScheduler
        from app.providers import TiandituProvider
        from app.tasks.task_service import TaskService
        from app.tasks.task_store import TaskStore

        server = MockTileServer()
        task_service = TaskService(TaskStore(self.work_dir / "tasks.json"))
        runner = MapDownloadTaskRunner(
            TiandituProvider(FAKE_TOKEN),
            task_service=task_service,
            transport=httpx.MockTransport(server),
            concurrency=2,
            retry_policy=RetryPolicy(
                max_attempts=2, base_delay_seconds=0.0, max_delay_seconds=0.0
            ),
            rate_limiter=RateLimiter.disabled(),
            batch_size=64,
        )
        scheduler = PlanScheduler(
            self.plan_service,
            runner,
            task_service=task_service,
            store=PlanExecutionStore(self.work_dir / "executions.json"),
            save_interval_seconds=0.0,
            save_tile_interval=1,
        )

        # 串行不变量：任一帧里 running 的条目数 ≤ 1；顺序就是计划顺序。
        # 执行记录里只有 ``plan_item_id``（没有 region_id），所以先建一张映射表。
        id_to_region = {item.id: item.region_id for item in self.plan.items}
        violations: list[str] = []
        observed: list[tuple[str, str]] = []
        started: list[str] = []

        def on_item_changed(execution) -> None:
            running = [
                item for item in execution.items
                if str(getattr(item.status, "value", item.status)) == "running"
            ]
            if len(running) > 1:
                violations.append(f"同时有 {len(running)} 条在跑")
            current = execution.current
            if current is not None:
                region_id = id_to_region.get(current.plan_item_id, current.plan_item_id)
                status = str(getattr(current.status, "value", ""))
                if not observed or observed[-1] != (region_id, status):
                    observed.append((region_id, status))
                if status == "running" and region_id not in started:
                    started.append(region_id)

        started_at = time.perf_counter()
        self.execution = asyncio.run(
            scheduler.start(
                self.plan,
                token=FAKE_TOKEN,
                on_item_changed=on_item_changed,
            )
        )
        elapsed = time.perf_counter() - started_at

        ok = True
        ok &= self.check(not violations, "任意时刻最多只有一条任务在跑（严格串行）",
                         f"违规 {violations[:3]}")
        expected_order = [
            "democountry", "demoprovince", "democity", self.mission_region.id
        ]
        ok &= self.check(
            started == expected_order,
            "四条任务的启动顺序与计划顺序一致",
            f"实际 {started}",
        )
        ok &= self.check(
            self.execution.status.value == "completed",
            "整份计划跑到了「已完成」",
            f"实际 {self.execution.status.value}",
        )
        ok &= self.check(
            self.execution.completed_tiles >= self.plan.total_tiles,
            "完成张数达到计划总量（没有半路丢掉条目）",
            f"{self.execution.completed_tiles:,} / {self.plan.total_tiles:,}",
        )
        per_item = [
            f"{id_to_region.get(item.plan_item_id, item.plan_item_id)}="
            f"{str(getattr(item.status, 'value', item.status))}"
            for item in self.execution.items
        ]
        self.note(f"四条状态：" + "，".join(per_item))
        self.note(
            f"耗时 {elapsed:.1f}s，服务端收到 {server.count:,} 个瓦片请求"
            f"（计划总量 {self.plan.total_tiles:,} 张）"
        )
        ok &= self.check(
            server.count == self.plan.total_tiles,
            "服务端收到的请求数正好等于计划总量（一张不多、一张不少）",
            f"{server.count:,} vs {self.plan.total_tiles:,}",
        )

        # 记录产物路径。**用生产那个唯一的命名函数**，不自己拼文件名 ——
        # 自己拼出来的名字迟早与"真正写盘的名字"不一致（工作区记忆的坑 53）。
        from app.planning.download_plan import planned_output_paths

        self.outputs = dict(
            planned_output_paths(self.plan, output_dir=self.output_dir)
        )
        return self.section("Scheduler", ok)

    # ------------------------------------------------------------------ #
    # ⑧ MBTiles
    # ------------------------------------------------------------------ #
    def section_mbtiles(self) -> bool:
        """四个产物真的落盘、真的可读、张数对得上（Spec §1）。"""
        from app.mbtiles import MBTilesReader

        if not self.outputs:
            # runner 没回写路径时，按计划的产物命名规则去 output 目录找。
            self.outputs = {
                path.stem: path for path in sorted(self.output_dir.glob("*.mbtiles"))
            }
        ok = True
        ok &= self.check(len(self.outputs) == 4, "四个 MBTiles 都产出了",
                         f"实际 {sorted(self.outputs)}")
        by_region = {}
        for region_id, item in (
            (item.region_id, item) for item in self.plan.ordered_enabled_items()
        ):
            by_region[region_id] = item

        for region_id, path in sorted(self.outputs.items()):
            if not path.exists():
                ok &= self.check(False, f"{path.name} 存在")
                continue
            with MBTilesReader(path) as reader:
                count = reader.get_tile_count()
                zooms = reader.get_zoom_range()
                metadata = reader.get_metadata()
            ok &= self.check(count > 0, f"{path.name} 里有瓦片", f"{count:,} 张")
            zoom_text = f"{zooms[0]}~{zooms[1]}" if zooms else "—"
            self.note(
                f"{path.name}: {count:,} 张, zoom {zoom_text}, "
                f"{metadata.get('region_type', '?')}/{metadata.get('region_id', '-')}"
                f"{'/level=' + metadata['region_level'] if metadata.get('region_level') else ''}"
            )
        # 张数必须等于"选中"而不是"候选"（Spec §6：Polygon 外 Tile 不下载）。
        for region_id, path in sorted(self.outputs.items()):
            item = _find_plan_item(self.plan, region_id)
            if item is None or item.selected_tiles <= 0:
                continue
            with MBTilesReader(path) as reader:
                count = reader.get_tile_count()
            ok &= self.check(
                count == item.selected_tiles,
                f"{region_id}: 落盘张数 == 估算的**选中**张数"
                "（多边形外的瓦片一张都没下）",
                f"库里 {count:,} vs 选中 {item.selected_tiles:,}"
                f"（候选 {item.candidate_tiles:,}）",
            )
        return self.section("MBTiles", ok)

    # ------------------------------------------------------------------ #
    # ⑨ Inspector
    # ------------------------------------------------------------------ #
    def section_inspector(self) -> bool:
        """对**多边形包**做检查（Spec §12）。

        重点不是"能跑"，而是**不许按外包矩形报假缺失**：一个按多边形裁出来的
        完整包，若用外包矩形当期望集合，会被判成"缺了几十张"，而它是完整的。
        """
        import re

        from app.services import MBTilesInspectionService

        service = MBTilesInspectionService()
        ok = True
        missing_claim = re.compile(r"缺失\s*[0-9,]+\s*张")
        for region_id, path in sorted(self.outputs.items()):
            result = service.inspect(path, check_coverage=True)
            ok &= self.check(
                result.status == "PASS",
                f"{path.name} 检查结论 PASS",
                f"实际 {result.status}；{[*result.errors, *result.warnings][:2]}",
            )
            text = "\n".join([*result.errors, *result.warnings, *getattr(result, "notices", [])])
            ok &= self.check(
                not missing_claim.search(text),
                f"{path.name} 没有「按外包矩形算出来的」缺失张数（Spec §12 的假缺失）",
                missing_claim.search(text).group(0) if missing_claim.search(text) else "",
            )
            self.note(f"{path.name}: {result.status}（{len(result.warnings)} 警告）")
        return self.section("Inspector", ok)

    # ------------------------------------------------------------------ #
    # ⑩ Package
    # ------------------------------------------------------------------ #
    def section_package(self) -> bool:
        """打两个包（行政区 + 任务区）并自校验（Spec §1 的最后一环，§4 的元数据一致性）。

        为什么**行政区也要打一个**：``region.level`` 是本链路里唯一一件
        "Windows 查得到、Android 查不到"的信息 —— Windows 有区域目录，
        可以按 ``region_id`` 反查等级；Android 只有这个包。Slice 22 正是
        把这两件事当成一回事（"反查得出来就不用写"），结果行政区包在
        Android 侧全是 ``UNKNOWN``，等级优先级整条失效。所以这里必须
        拿**真实的行政区产物**验一遍，而不是只在单测里断言字典。
        """
        from app.package.models import parse_manifest_dict

        from app.services import MapPackageService

        service = MapPackageService()
        package_dir = self.work_dir / "packages"
        expected_levels = _expected_levels(self.mission_region.id)
        ok = True

        for region_id in ("democity", self.mission_region.id):
            target = self.outputs.get(region_id)
            if target is None:
                ok &= self.check(False, f"{region_id} 有产物可以打包")
                continue

            result = service.build(
                target,
                package_dir,
                package_name=_package_name_for(target),
                accept_warnings=True,
            )
            ok &= self.check(result.success, f"用 {target.name} 生成了地图包",
                             "；".join(result.errors[:2]))
            if not result.success:
                continue

            ok &= self.check(
                result.validation is not None and result.validation.valid,
                f"{region_id}：包的自校验结论 PASS",
                result.validation.status if result.validation else "没跑自校验",
            )
            manifest_path = Path(result.package_dir) / "manifest.json"
            manifest = parse_manifest_dict(
                json.loads(manifest_path.read_text(encoding="utf-8"))
            )
            ok &= self.check(manifest.region is not None, f"{region_id}：manifest 里有 region 子对象")
            if manifest.region is None:
                continue

            payload = manifest.region.to_dict()
            self.note(f"{region_id} 的 manifest.region = "
                      f"{json.dumps(payload, ensure_ascii=False)}")
            ok &= self.check(
                manifest.region.level == expected_levels[region_id],
                f"{region_id}：region.level 是 {expected_levels[region_id]}，"
                f"落到了包里（Android 只能靠它判断等级）",
                f"level={manifest.region.level}",
            )
            zoom_key = "mission" if region_id == self.mission_region.id else region_id
            ok &= self.check(
                manifest.min_zoom == PLAN_ZOOMS[zoom_key][0]
                and manifest.max_zoom == PLAN_ZOOMS[zoom_key][1],
                f"{region_id}：manifest 的 Zoom 区间与计划一致",
                f"Z{manifest.min_zoom}~{manifest.max_zoom}",
            )

            if region_id == self.mission_region.id:
                ok &= self.check(
                    manifest.region.geometry_version == manifest.region.boundary_version,
                    "任务区：boundaryVersion 与 geometryVersion 是同一个事实",
                )
                ok &= self.check(
                    manifest.region.buffer_meters is not None,
                    "任务区：走廊半径在包里（Android 侧要看它）",
                )
            else:
                # 行政区**不该**多出这两项：几何版本已经由 boundaryVersion
                # 表达，走廊半径对行政区没有意义（同一个事实存两处的经典起点）。
                ok &= self.check(
                    manifest.region.geometry_version is None
                    and manifest.region.buffer_meters is None,
                    "行政区：没有凭空多出 geometryVersion / bufferMeters",
                    json.dumps(payload, ensure_ascii=False),
                )

            self.packages[f"{region_id}:{target.stem}"] = Path(result.package_dir)
            self.note(f"{region_id} 的包目录：{result.package_dir}")

        return self.section("Package", ok)

    # ------------------------------------------------------------------ #
    def run(self) -> bool:
        print("=" * 72)
        print("分级离线地图链路 E2E（Slice 25）")
        print(f"工作目录：{self.work_dir}")
        print("=" * 72)
        sections = (
            ("Region hierarchy", self.section_region_hierarchy),
            ("Zoom strategy", self.section_zoom_strategy),
            ("Polygon estimate", self.section_polygon_estimate),
            ("Mission region", self.section_mission_region),
            ("Risk analysis", self.section_risk_analysis),
            ("Download plan", self.section_download_plan),
            ("Scheduler", self.section_scheduler),
            ("MBTiles", self.section_mbtiles),
            ("Inspector", self.section_inspector),
            ("Package", self.section_package),
        )
        results: list[tuple[str, bool]] = []
        for title, fn in sections:
            try:
                passed = bool(fn())
            except Exception as exc:                       # noqa: BLE001
                import traceback

                traceback.print_exc()
                passed = self.section(title, False, f"异常：{exc!r}")
            results.append((title, passed))
        return all(passed for _, passed in results)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _bbox_area(geometry) -> float:
    """几何外包矩形的面积（平方度）——用来判"它是不是个矩形"。"""
    minx, miny, maxx, maxy = geometry.bounds
    return (maxx - minx) * (maxy - miny)


def _tile_bounds(zoom: int, x: int, y: int) -> tuple[float, float, float, float]:
    """XYZ 瓦片的经纬度范围（west, south, east, north）。"""
    import math

    span = 1 << zoom

    def lon_of(tile_x: int) -> float:
        return tile_x / span * 360.0 - 180.0

    def lat_of(tile_y: int) -> float:
        n = math.pi * (1.0 - 2.0 * tile_y / span)
        return math.degrees(math.atan(math.sinh(n)))

    return (lon_of(x), lat_of(y + 1), lon_of(x + 1), lat_of(y))


def _load_geojson_geometry(path) -> dict:
    """读一个 GeoJSON 文件，取其中第一个几何。

    ``Feature`` / ``FeatureCollection`` / 裸几何三种写法都吃 —— 行政区边界文件
    是 ``Feature``，任务区几何是裸 ``Polygon``，本脚本两处都要读。
    """
    import json as _json

    document = _json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("type") == "FeatureCollection":
        return document["features"][0]["geometry"]
    if document.get("type") == "Feature":
        return document["geometry"]
    return document


def _find_plan_item(plan, region_id: str):
    for item in plan.items:
        if item.region_id == region_id:
            return item
    return None


def _package_name_for(mbtiles_path: Path) -> str:
    """按源文件名推一个包名（小写字母 / 数字 / 下划线 / 连字符）。"""
    import re as _re

    stem = _re.sub(r"[^a-z0-9_-]+", "-", mbtiles_path.stem.lower()).strip("-")
    return stem or "package"


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="分级离线地图链路端到端验收（全程离线）"
    )
    parser.add_argument("--keep", action="store_true",
                        help="保留临时产物，便于人工翻看")
    parser.add_argument("--work-dir", default=None,
                        help="指定工作目录（默认在系统临时目录里新建）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _configure_region_resources()

    if not HIERARCHY_DIR.exists():
        print(f"[FAIL] 找不到分级夹具目录：{HIERARCHY_DIR}", file=sys.stderr)
        return 2

    if args.work_dir:
        work_dir = Path(args.work_dir).resolve()
        # **先清空**：复用同一个目录会让"已有瓦片被跳过"变成正常现象，
        # 于是"服务端请求数 == 计划总量"这类断言不再成立（那一次是续传，
        # 不是本脚本要验的首次下载）。验收要的是一份干净的起点。
        if work_dir.exists():
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="hier-e2e-"))

    started = time.perf_counter()
    runner = Runner(work_dir)
    try:
        passed = runner.run()
    finally:
        elapsed = time.perf_counter() - started

    print("-" * 72)
    failed = [check for check in runner.checks if not check.passed]
    if passed:
        print(f"全部通过（10 段 / {len(runner.checks)} 项检查，{elapsed:.1f}s）")
    else:
        print(f"失败 {len(failed)} 项（共 {len(runner.checks)} 项，{elapsed:.1f}s）：")
        for check in failed:
            print(f"  - {check.label}" + (f"：{check.detail}" if check.detail else ""))

    if args.keep:
        print(f"产物保留在：{work_dir}")
    else:
        shutil.rmtree(work_dir, ignore_errors=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
