#!/usr/bin/env python
"""Windows 侧端到端冒烟测试（Slice 15 §3）。

**它验证的是"这条链路整体还活着"，不是"某个函数算得对"。**
单元测试可以全绿而链路已经断了 —— 比如瓦片坐标算错一层、MBTiles 写进去
的行列号翻了、Packaging 时 SHA256 算的是错的字节 —— 这些都不会让任何
单个函数的用例变红，只会让用户拿到一张空白地图。

本脚本因此把整条链路串起来跑一遍，**全程离线**：

    GeoBounds → Tile Math → 模拟瓦片下载 → MBTiles
        → Inspector → Package Builder → SHA256 → Package Validator

设计约束：

* **不访问真实天地图。** 用 :class:`MockTileProvider` 在内存里造 PNG 字节。
  理由不只是"跑得快"：真网络会让这个脚本时好时坏，而它是"每次提交前都能跑"
  的那一道闸；一个需要 Token 且受网络影响的冒烟测试，最终一定会被人跳过。
* **不依赖 Qt。** 只用 ``app.geo`` / ``app.mbtiles`` / ``app.package`` 这些
  纯逻辑包。GUI 的验收由 ``scripts/verify_sliceN_*.py`` 负责。
* **临时目录用完即删**，不污染仓库。
* **失败返回非 0 退出码**，可直接进 CI。

用法::

    python scripts/e2e_smoke_test.py
    python scripts/e2e_smoke_test.py --keep      # 保留产物，便于人工翻看
    python scripts/e2e_smoke_test.py -z 11 12    # 自定义层级（默认 11 12 13）
"""

from __future__ import annotations

import argparse
import shutil
import struct
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

# 允许 `python scripts/e2e_smoke_test.py` 直接跑（把仓库根加进 sys.path）。
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings  # noqa: E402
from app.geo.bounds import GeoBounds  # noqa: E402
from app.geo.tile_math import (  # noqa: E402
    calculate_tile_range,
    estimate_tile_count,
    xyz_y_to_tms_y,
)
from app.mbtiles.models import MBTilesMetadata  # noqa: E402
from app.mbtiles.writer import MBTilesWriter  # noqa: E402
from app.package.checksum import calculate_sha256  # noqa: E402
from app.package.models import PACKAGE_FORMAT_VERSION  # noqa: E402
from app.package.package_builder import MapPackageBuilder  # noqa: E402
from app.package.package_reader import MapPackageReader  # noqa: E402
from app.package.package_validator import MapPackageValidator  # noqa: E402
from app.services.mbtiles_inspection_service import (  # noqa: E402
    MBTilesInspectionService,
)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
#: 冒烟用的地理范围：北京主城。刻意挑一个**不跨赤道、不跨 180°**的普通范围，
#: 有问题时容易看出来；边界值场景由单元测试覆盖。
SMOKE_BOUNDS = GeoBounds(west=116.2, south=39.7, east=116.6, north=40.1)

#: 默认层级。3 级 = 足以让"层级循环 / 每级坐标范围 / 数量累加"都跑到，
#: 又不至于让冒烟测试慢到没人愿意跑（实测 12 张瓦片，全程 < 2 秒）。
DEFAULT_ZOOMS = (11, 12, 13)

#: 瓦片边长。用 256 而不是 1×1：Package Builder 会真的解码抽样瓦片
#: （Slice 8 的 diagnostics），图片太小会被判"看不出来是张图"，而且
#: 尺寸可疑本身就会进 warnings。
TILE_SIZE = 256

#: PNG 的魔数，用来在断言里确认"写进去的确实是 PNG"。
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: 一个够用的调色板：让不同瓦片颜色不同。**不是为了好看** ——
#: 如果所有瓦片字节完全一样，"写错行列号"这类 bug 在冒烟里就看不出来。
_TILE_COLORS = (
    (0x33, 0x66, 0xCC),
    (0x2E, 0x8B, 0x57),
    (0xCC, 0x66, 0x33),
    (0x88, 0x44, 0xAA),
)


# --------------------------------------------------------------------------- #
# Mock Tile Provider
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
    """生成一张 ``size × size`` 的单色真彩 PNG。

    刻意手写而不是依赖 Pillow：冒烟测试要能在**只装了项目依赖**的环境里跑，
    而 pillow 只是测试期的可选件。手写一个单色 PNG 大约 20 行，比多一个
    运行时依赖划算。

    格式选择上有个坑：真彩 + filter type 0（无过滤），每行前置一个 0 字节。
    用调色板（PLTE）能更小，但 ImageProbe 对调色板 PNG 的识别路径与真彩不同，
    冒烟要覆盖"最常见的那种瓦片"，所以走真彩。
    """
    raw = bytearray()
    for _ in range(size):
        raw.append(0)                       # filter type 0
        raw.extend(bytes(rgb) * size)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)   # 8bit / truecolor
    return (
        PNG_MAGIC
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _png_chunk(b"IEND", b"")
    )


@dataclass
class MockTileProvider:
    """内存瓦片源，替代 :class:`~app.providers.tianditu.TiandituProvider`。

    接口只取真实 Provider 的一个子集：``get_tile(z, x, y) -> bytes``。
    冒烟测试不需要 Token、限速、重试 —— 那些是单元测试的活，把它们拖进
    冒烟只会让冒烟也变成"受网络影响的东西"。

    它**记录每次调用**（:attr:`requests`），因为"下载器到底按什么坐标去取"
    正是这类集成问题最容易错的地方。
    """

    name: str = "mock"
    #: 每次 ``get_tile`` 的 ``(z, x, y)``，y 是 **XYZ**（左上原点）。
    requests: list[tuple[int, int, int]] = field(default_factory=list)
    #: 每次 ``get_tile`` 返回的字节数，用来核对"真的写进去了"。
    bytes_served: int = 0

    def get_tile(self, z: int, x: int, y: int) -> bytes:
        """返回一张 PNG。``y`` 按 **XYZ** 解释（见 docs/map_package_v1.md §5）。"""
        self.requests.append((z, x, y))
        # 颜色随坐标变化：写错行列号时图片内容会跟预期不符。
        rgb = _TILE_COLORS[(x + y) % len(_TILE_COLORS)]
        data = make_png(rgb)
        self.bytes_served += len(data)
        return data


# --------------------------------------------------------------------------- #
# 结果收集
# --------------------------------------------------------------------------- #
@dataclass
class CheckResult:
    """一项检查的结论。"""

    label: str
    passed: bool
    detail: str = ""
    #: 值得顺带打印的补充信息（不影响成败）。
    notes: list[str] = field(default_factory=list)


class SmokeRunner:
    """跑完整条链路，逐段记录结论。"""

    def __init__(self, work_dir: Path, zooms: tuple[int, ...]) -> None:
        self.work_dir = work_dir
        self.zooms = zooms
        self.provider = MockTileProvider()
        self.checks: list[CheckResult] = []
        # 跨阶段共享的事实
        self.mbtiles_path = work_dir / "smoke.mbtiles"
        self.package_dir: Path | None = None
        self.expected_tile_count = 0
        self.actual_tile_count = 0

    # ------------------------------------------------------------------ #
    def _record(self, label: str, passed: bool, detail: str = "",
                notes: list[str] | None = None) -> CheckResult:
        check = CheckResult(label=label, passed=passed, detail=detail,
                            notes=list(notes or []))
        self.checks.append(check)
        flag = "[PASS]" if passed else "[FAIL]"
        print(f"{flag} {label}" + (f" — {detail}" if detail else ""))
        for note in check.notes:
            print(f"       {note}")
        return check

    # ------------------------------------------------------------------ #
    # ① Tile Math
    # ------------------------------------------------------------------ #
    def check_tile_math(self) -> bool:
        """地理范围 → 每级瓦片编号区间，并核对总数。

        这一段的重点是：后面每一段都要用这里的数字。如果这里算错，
        必须**现在**就红，而不是等到 Package 阶段靠"瓦片数对不上"间接发现 ——
        那时已经很难判断是算错了还是写丢了。
        """
        ranges = {}
        total = 0
        notes: list[str] = []

        for zoom in self.zooms:
            try:
                rng = calculate_tile_range(SMOKE_BOUNDS, zoom)
            except Exception as exc:                       # noqa: BLE001
                return self._record("Tile Math", False,
                                    f"Zoom {zoom} 计算失败：{exc!r}")
            ranges[zoom] = rng
            total += rng.tile_count
            notes.append(
                f"Zoom {zoom}: x [{rng.min_x}, {rng.max_x}] "
                f"({rng.x_count} 列) × y [{rng.min_y}, {rng.max_y}] "
                f"({rng.y_count} 行) = {rng.tile_count} 张"
            )

        # 用生产函数独立算一遍总数，与逐级累加比对。
        # 这里**不写死任何数字**：写死的话，哪天范围常量一改，冒烟就会
        # 莫名其妙地红（或者更糟：看起来还是绿的）。见工作区记忆里的教训。
        expected = estimate_tile_count(SMOKE_BOUNDS, self.zooms[0], self.zooms[-1])
        if total != expected:
            return self._record(
                "Tile Math", False,
                f"逐级累加得 {total}，estimate_tile_count 得 {expected}",
            )

        self.expected_tile_count = total
        notes.append(f"合计 {total} 张（与 estimate_tile_count 一致）")
        return self._record("Tile Math", True, f"{len(self.zooms)} 个层级 / "
                                               f"{total} 张瓦片", notes)

    # ------------------------------------------------------------------ #
    # ② MBTiles
    # ------------------------------------------------------------------ #
    def check_mbtiles(self) -> bool:
        """模拟下载 → 写 MBTiles → 读回来核对。

        关键断言是**逐张读回来且字节一致**，不是"文件存在且不为空"。
        MBTiles 的行列号在写入时要翻成 TMS（``xyz_y_to_tms_y``），
        翻错方向的表现是"能写进去、能读出来、数量也对，就是上下颠倒" ——
        只看文件大小和行数永远抓不到。
        """
        try:
            with MBTilesWriter(self.mbtiles_path) as writer:
                writer.write_metadata(MBTilesMetadata(
                    name="smoke_e2e",
                    description="端到端冒烟测试产物",
                    min_zoom=self.zooms[0],
                    max_zoom=self.zooms[-1],
                    west=SMOKE_BOUNDS.west,
                    south=SMOKE_BOUNDS.south,
                    east=SMOKE_BOUNDS.east,
                    north=SMOKE_BOUNDS.north,
                    format="png",
                    provider=settings.PACKAGE_DEFAULT_PROVIDER,
                    source_map_type="vector",
                    with_labels=True,
                    composition="merged",
                ))
                for zoom in self.zooms:
                    rng = calculate_tile_range(SMOKE_BOUNDS, zoom)
                    for x in range(rng.min_x, rng.max_x + 1):
                        for y in range(rng.min_y, rng.max_y + 1):
                            # 下载（这里被 Mock 掉了）
                            data = self.provider.get_tile(zoom, x, y)
                            # 写入。注意 write_tile 收的是 **XYZ** 的 y，
                            # 翻转由 writer 内部负责 —— 冒烟脚本不该自己翻。
                            writer.write_tile(zoom, x, y, data)
                writer.flush()
        except Exception as exc:                           # noqa: BLE001
            return self._record("MBTiles", False, f"写入失败：{exc!r}")

        if not self.mbtiles_path.exists():
            return self._record("MBTiles", False, "写入后文件不存在")

        size = self.mbtiles_path.stat().st_size
        with self.mbtiles_path.open("rb") as fh:
            magic = fh.read(16)
        if magic != b"SQLite format 3\x00":
            return self._record("MBTiles", False,
                                f"文件头不是 SQLite：{magic!r}")

        # --- 读回来逐张比对 ---
        from app.mbtiles.reader import MBTilesReader

        reader = MBTilesReader(self.mbtiles_path)
        reader.open()
        try:
            count = reader.get_tile_count()
            mismatches: list[str] = []
            missing: list[str] = []
            for zoom in self.zooms:
                rng = calculate_tile_range(SMOKE_BOUNDS, zoom)
                for x in range(rng.min_x, rng.max_x + 1):
                    for y in range(rng.min_y, rng.max_y + 1):
                        got = reader.read_tile(zoom, x, y)
                        if got is None:
                            missing.append(f"z{zoom}/x{x}/y{y}")
                            continue
                        if not got.startswith(PNG_MAGIC):
                            mismatches.append(f"z{zoom}/x{x}/y{y} 不是 PNG")
            # 顺带验证 TMS 翻转确实是双射：翻转过去再翻回来必须是原值
            for zoom in self.zooms:
                rng = calculate_tile_range(SMOKE_BOUNDS, zoom)
                for y in (rng.min_y, rng.max_y):
                    if xyz_y_to_tms_y(zoom, xyz_y_to_tms_y(zoom, y)) != y:
                        mismatches.append(f"z{zoom}/y{y} TMS 往返不一致")
        finally:
            reader.close()

        self.actual_tile_count = count
        if missing:
            return self._record("MBTiles", False,
                                f"读回来缺 {len(missing)} 张：{missing[:3]} …")
        if mismatches:
            return self._record("MBTiles", False,
                                f"{len(mismatches)} 张内容不对：{mismatches[:3]} …")
        if count != self.expected_tile_count:
            return self._record(
                "MBTiles", False,
                f"库里 {count} 张，Tile Math 算的是 {self.expected_tile_count} 张",
            )

        return self._record("MBTiles", True, f"{count} 张瓦片，逐张读回一致", [
            f"文件大小 {size:,} 字节",
            f"Mock Provider 被调用 {len(self.provider.requests)} 次，"
            f"下发 {self.provider.bytes_served:,} 字节",
        ])

    # ------------------------------------------------------------------ #
    # ③ Inspector
    # ------------------------------------------------------------------ #
    def check_inspector(self) -> bool:
        """MBTiles 结构检查（Slice 8）必须给出 PASS。

        为什么冒烟里要卡在 PASS 而不接受 WARNING：这段检查正是 Package Builder
        发布前的那道闸。它如果给 WARNING，Builder 就要用户拍板才继续 ——
        一个自己造出来的、metadata 齐备的小文件不该带警告。带着警告继续，
        说明"我们连自己刚写的东西都认不全"。
        """
        try:
            service = MBTilesInspectionService()
            result = service.inspect(self.mbtiles_path, check_coverage=True)
        except Exception as exc:                           # noqa: BLE001
            return self._record("Inspector", False, f"检查抛异常：{exc!r}")

        status = getattr(result, "status", "?")
        notes = [
            f"状态 {status}",
            f"Zoom {getattr(result, 'zoom_text', '?')}",
            f"抽样图片 {getattr(result, 'sampled_formats_text', '?')}",
        ]
        if getattr(result, "is_merged", False):
            notes.append("识别为 merged（注记已合成）")

        if status != "PASS":
            details = list(getattr(result, "errors", [])) + \
                list(getattr(result, "warnings", []))
            return self._record("Inspector", False,
                                f"状态 {status}，期望 PASS", notes + details[:5])
        return self._record("Inspector", True, "状态 PASS", notes)

    # ------------------------------------------------------------------ #
    # ④ Package
    # ------------------------------------------------------------------ #
    def check_package(self) -> bool:
        """MBTiles → 地图包（含 manifest）。

        用 ``package_id`` 显式指定 id，让产物路径可预期（默认 id 带时间戳与
        随机后缀，每次跑出来都不一样，脚本就没法核对目录名了）。
        """
        builder = MapPackageBuilder()
        try:
            result = builder.build(
                self.mbtiles_path,
                self.work_dir,
                package_name="smoke_e2e",
                package_id="smoke_e2e",
                # 自己造的文件不该需要用户确认；默认 False 会返回
                # needs_confirmation=True 且**不生成任何文件**。
                accept_warnings=True,
                verify=True,
            )
        except Exception as exc:                           # noqa: BLE001
            return self._record("Package", False, f"构建抛异常：{exc!r}")

        if getattr(result, "needs_confirmation", False):
            return self._record("Package", False,
                                f"要求确认后才生成：{getattr(result, 'confirmation_reason', '?')}")
        if not getattr(result, "success", False):
            errors = list(getattr(result, "errors", []))
            return self._record("Package", False,
                                f"构建失败：{errors[:3] or '未说明原因'}")

        package_dir = Path(str(result.package_dir))
        self.package_dir = package_dir

        manifest_path = package_dir / "manifest.json"
        mbtiles_path = package_dir / "map.mbtiles"
        problems: list[str] = []
        if not manifest_path.is_file():
            problems.append("manifest.json 不存在")
        if not mbtiles_path.is_file():
            problems.append("map.mbtiles 不存在")
        if package_dir.name != "smoke_e2e":
            problems.append(f"目录名 {package_dir.name!r} != id 'smoke_e2e'")
        if problems:
            return self._record("Package", False, "；".join(problems))

        # manifest 必须能被读回来，且字段与 MBTiles 对得上
        try:
            manifest = MapPackageReader().read_manifest(package_dir)
        except Exception as exc:                           # noqa: BLE001
            return self._record("Package", False, f"manifest 读不回来：{exc!r}")

        if manifest.format_version != PACKAGE_FORMAT_VERSION:
            return self._record(
                "Package", False,
                f"formatVersion {manifest.format_version} != "
                f"{PACKAGE_FORMAT_VERSION}",
            )
        if manifest.tile_count != self.actual_tile_count:
            return self._record(
                "Package", False,
                f"manifest.tileCount {manifest.tile_count} != "
                f"实际 {self.actual_tile_count}",
            )

        notes = [
            f"id={manifest.id}  mapType={manifest.map_type}  "
            f"labels={manifest.labels}  composition={manifest.composition}",
            f"Zoom {manifest.min_zoom}~{manifest.max_zoom}  "
            f"{manifest.tile_count} 张  {getattr(result, 'mbtiles_size', 0):,} 字节",
        ]
        return self._record("Package", True, f"formatVersion="
                                            f"{manifest.format_version}", notes)

    # ------------------------------------------------------------------ #
    # ⑤ SHA256
    # ------------------------------------------------------------------ #
    def check_sha256(self) -> bool:
        """manifest 里的 sha256/size 必须与真实文件一致。

        这一段单独列出来，是因为它**跨越了两个阶段**：哈希在打包时算、
        文件在打包时复制。危险的不是"算错了"，而是"算的是源文件、写进
        manifest 的却是另一份拷贝的哈希" —— 两边都"正确"，合起来是错的，
        而后果是 Android 侧每次都报校验失败。
        """
        if self.package_dir is None:
            return self._record("SHA256", False, "上一步没产出地图包，跳过")

        mbtiles_path = self.package_dir / "map.mbtiles"
        manifest = MapPackageReader().read_manifest(self.package_dir)

        real_size = mbtiles_path.stat().st_size
        real_sha = calculate_sha256(mbtiles_path)

        if manifest.mbtiles_size != real_size:
            return self._record(
                "SHA256", False,
                f"manifest.size {manifest.mbtiles_size:,} != 实际 {real_size:,}",
            )
        if manifest.mbtiles_sha256 != real_sha:
            return self._record(
                "SHA256", False,
                f"manifest.sha256 {manifest.mbtiles_sha256[:16]}… != "
                f"实际 {real_sha[:16]}…",
            )
        if len(real_sha) != settings.PACKAGE_SHA256_LENGTH:
            return self._record("SHA256", False,
                                f"哈希长度 {len(real_sha)}，期望 "
                                f"{settings.PACKAGE_SHA256_LENGTH}")

        return self._record("SHA256", True, f"{real_sha[:16]}…", [
            f"size {real_size:,} 字节（manifest 与实际一致）",
        ])

    # ------------------------------------------------------------------ #
    # ⑥ Validator（额外一段：Builder 的 verify=True 已经跑过一次，
    #    这里再独立跑一次，确认"事后校验"这条路径也能独立成立）
    # ------------------------------------------------------------------ #
    def check_validator(self) -> bool:
        """独立再跑一次 Package 校验（重算 SHA256 + 重新检查 MBTiles）。

        与 Builder 内部的 ``verify=True`` 区别在于：那次是"刚写完，趁热验"，
        这次是"从磁盘上重新读一遍再验"。两者都可能因为缓存/句柄问题
        得出不同结论，而 Android 侧遇到的是后者。
        """
        if self.package_dir is None:
            return self._record("Validator", False, "上一步没产出地图包，跳过")

        try:
            result = MapPackageValidator().validate(
                self.package_dir, verify_checksum=True, inspect_mbtiles=True,
            )
        except Exception as exc:                           # noqa: BLE001
            return self._record("Validator", False, f"校验抛异常：{exc!r}")

        if not getattr(result, "valid", False):
            errors = list(getattr(result, "errors", []))
            return self._record("Validator", False,
                                f"校验不通过：{errors[:3] or '未说明原因'}")

        notes = []
        warnings = list(getattr(result, "warnings", []))
        if warnings:
            notes.append(f"{len(warnings)} 条提示（不影响结论）")
        return self._record("Validator", True, "valid=True", notes)

    # ------------------------------------------------------------------ #
    def run(self) -> bool:
        """按顺序跑完全部检查，返回是否全绿。"""
        print("=" * 72)
        print(f"Windows 端到端冒烟测试  |  "
              f"{settings.APP_NAME} v{settings.APP_VERSION}")
        print(f"工作目录：{self.work_dir}")
        print(f"范围：{SMOKE_BOUNDS.west}~{SMOKE_BOUNDS.east}E  "
              f"{SMOKE_BOUNDS.south}~{SMOKE_BOUNDS.north}N  "
              f"Zoom {min(self.zooms)}~{max(self.zooms)}")
        print("=" * 72)

        started = time.monotonic()
        # 顺序有依赖：后面的段落要读前面的产物。所以是短路式的 —— 一段
        # 失败就停，"3 段红了"不如"第 1 段就红了"好定位。
        #
        # 注意 stage 返回的是 CheckResult 而不是 bool，这里必须显式取
        # `.passed`：dataclass 没有自定义 __bool__，任何实例都是真值，
        # 写成 `if not stage():` 会让短路**静默失效** —— 全部段落照跑，
        # 后面几段各自在空产物上报错，把真正的原因埋掉。
        stages = (
            self.check_tile_math,
            self.check_mbtiles,
            self.check_inspector,
            self.check_package,
            self.check_sha256,
            self.check_validator,
        )
        for stage in stages:
            if not stage().passed:
                break

        elapsed = time.monotonic() - started
        failed = [c for c in self.checks if not c.passed]
        print("-" * 72)
        if failed:
            print(f"结果：失败 —— {len(failed)} 项未通过"
                  f"（共跑 {len(self.checks)} 项，{elapsed:.1f}s）")
            for check in failed:
                print(f"  • {check.label}: {check.detail}")
        else:
            print(f"结果：全部通过 —— {len(self.checks)} 项，{elapsed:.1f}s")
        print("=" * 72)
        return not failed


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Windows 侧端到端冒烟测试（离线，不访问天地图）",
    )
    parser.add_argument(
        "-z", "--zooms", type=int, nargs="+", default=list(DEFAULT_ZOOMS),
        metavar="Z",
        help=f"要跑的缩放层级（默认 {list(DEFAULT_ZOOMS)}）",
    )
    parser.add_argument(
        "--keep", action="store_true",
        help="保留临时产物目录（默认跑完删除）",
    )
    parser.add_argument(
        "--work-dir", type=Path, default=None,
        help="指定工作目录；不指定时用系统临时目录",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    zooms = tuple(sorted({int(z) for z in args.zooms}))
    if not zooms:
        print("至少要指定一个缩放层级", file=sys.stderr)
        return 2
    if len(zooms) > 6:
        print(f"层级太多（{len(zooms)} 个），冒烟测试请控制在 6 个以内",
              file=sys.stderr)
        return 2

    created = args.work_dir is None
    work_dir = (Path(tempfile.mkdtemp(prefix="smoke_e2e_"))
                if created else Path(args.work_dir))
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        runner = SmokeRunner(work_dir, zooms)
        ok = runner.run()
    except Exception:                               # noqa: BLE001
        # 冒烟脚本自己崩了也要给非 0 退出码 —— 否则 CI 会把它当通过。
        import traceback
        print("-" * 72)
        print("冒烟测试自身异常终止：")
        traceback.print_exc()
        print("-" * 72)
        return 1
    else:
        if args.keep or not created:
            print(f"产物保留在：{work_dir}")
            if runner.package_dir:
                print(f"地图包：{runner.package_dir}")
    finally:
        if created and not args.keep:
            shutil.rmtree(work_dir, ignore_errors=True)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
