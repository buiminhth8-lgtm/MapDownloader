"""Slice 42 离线验收：统一瓦片仓库（临时目录、不联网、失败即非零退出码）。

它和 ``pytest`` 的分工
-----------------------

``pytest`` 用例回答"某个函数在某种输入下对不对"；这个脚本回答"把仓库当成一个
产品，它现在能不能交付"—— 而且**只依赖磁盘上的真实结果**：

* 真写临时目录、真读回来、真去数 ``contents`` / 字节；
* 真开子进程，在 5 个阶段 ``os._exit(97)`` 把写者强杀，再验证恢复；
* 真起第二个进程抢同一把租约，验证 ``Busy`` 而不是"抢过来了"；
* 真删 ``catalog.sqlite``，验证能只靠分片自述重建。

**不联网、不读现有地图、不碰 ``output/``**：瓦片一律现画（Pillow），仓库根一律
``tempfile.mkdtemp()``。

用法::

    python scripts/verify_tile_repository.py                  # 全量，约 15~30 秒
    python scripts/verify_tile_repository.py --report r.json   # 另存机器可读报告
    python scripts/verify_tile_repository.py --keep            # 保留临时目录便于排查
    python scripts/verify_tile_repository.py --quick           # 跳过子进程崩溃场景

退出码：``0`` 全通过；``1`` 有失败项；``2`` 环境不满足（缺依赖/无法建临时目录）。
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SCRIPT_VERSION = "1.0"
TILE_SIDE = 256
ASSET = "tianditu/vec_w"
LABEL = "tianditu/cva_w"


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
@dataclass
class Report:
    """收集结果并给出机器可读 + 人读两种形态。"""

    checks: list[dict[str, object]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    root: str = ""

    def check(self, section: str, name: str, ok: bool, detail: str = "") -> bool:
        status = "passed" if ok else "failed"
        self.checks.append(
            {"section": section, "name": name, "status": status, "detail": detail}
        )
        mark = "[OK]  " if ok else "[FAIL]"
        print(f"  {mark} {name}{f' — {detail}' if detail and not ok else ''}")
        return bool(ok)

    def skip(self, section: str, name: str, reason: str) -> None:
        self.checks.append(
            {"section": section, "name": name, "status": "skipped", "detail": reason}
        )
        print(f"  [SKIP] {name} — {reason}")

    def section(self, title: str) -> None:
        print(f"\n=== {title} ===")

    # -- 汇总 ---------------------------------------------------------- #
    @property
    def passed(self) -> int:
        return sum(1 for item in self.checks if item["status"] == "passed")

    @property
    def failed(self) -> list[dict[str, object]]:
        return [item for item in self.checks if item["status"] == "failed"]

    @property
    def skipped(self) -> list[dict[str, object]]:
        return [item for item in self.checks if item["status"] == "skipped"]

    def _flag(self, prefix: str) -> bool:
        """某一类检查是否**真的跑过并通过**（用来回答"到底验收了什么"）。

        按 ``section`` 前缀匹配（``crash`` / ``rebuild`` / ``rebuild.catalog_lost``），
        跳过的场景因为状态不是 ``passed`` 而不会让这里变成 True —— "没跑"不许
        被读成"通过"。
        """
        relevant = [item for item in self.checks if str(item["section"]).startswith(prefix)]
        return bool(relevant) and all(item["status"] == "passed" for item in relevant)

    def payload(self) -> dict[str, object]:
        crash_phases = sorted(
            {
                str(item["name"]).split(" · ")[0]
                for item in self.checks
                if str(item["name"]).startswith("crash.")
            }
        )
        return {
            "script": "scripts/verify_tile_repository.py",
            "script_version": SCRIPT_VERSION,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "duration_seconds": round(time.time() - self.started_at, 2),
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "repo_root": self.root,
            "network": "not used",
            "counts": {
                "executed": self.passed + len(self.failed),
                "passed": self.passed,
                "failed": len(self.failed),
                "skipped": len(self.skipped),
            },
            "process_interruption": {
                "verified": self._flag("crash"),
                "method": "subprocess + os._exit(97)（真实进程被强杀，不是抛异常）",
                "phases": crash_phases,
                "recovered": self._flag("crash"),
            },
            "index_rebuild": {
                "verified": self._flag("rebuild"),
                "catalog_loss_rebuild": self._flag("rebuild.catalog_lost"),
                "idempotent_rebuild": self._flag("rebuild.idempotent"),
            },
            "checks": self.checks,
            "conclusion": "PASS" if not self.failed else "FAIL",
        }

    def print_summary(self) -> None:
        payload = self.payload()
        counts = payload["counts"]
        print("\n" + "=" * 72)
        print(
            f"结论：{payload['conclusion']}　执行 {counts['executed']} 项"
            f"（通过 {counts['passed']} / 失败 {counts['failed']} / 跳过 {counts['skipped']}）"
            f"　用时 {payload['duration_seconds']}s"
        )
        interruption = payload["process_interruption"]
        print(
            f"进程中断验收：{'已执行' if interruption['verified'] else '未执行/未通过'}"
            f"（阶段：{', '.join(interruption['phases']) or '无'}）"
        )
        rebuild = payload["index_rebuild"]
        print(
            f"索引重建验收：{'已执行' if rebuild['verified'] else '未执行/未通过'}"
            f"（catalog 丢失后重建：{'是' if rebuild['catalog_loss_rebuild'] else '否'}；"
            f"重建幂等：{'是' if rebuild['idempotent_rebuild'] else '否'}）"
        )
        print("联网：未使用（全部为合成瓦片 + 临时目录）")
        if self.failed:
            print("\n失败项：")
            for item in self.failed:
                print(f"  - [{item['section']}] {item['name']}：{item['detail']}")
        if self.skipped:
            print("\n跳过项：")
            for item in self.skipped:
                print(f"  - [{item['section']}] {item['name']}：{item['detail']}")


# --------------------------------------------------------------------------- #
# 合成瓦片
# --------------------------------------------------------------------------- #
def png_bytes(color: tuple[int, int, int]) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (TILE_SIDE, TILE_SIDE), color).save(buffer, format="PNG")
    return buffer.getvalue()


def pixel_at(data: bytes) -> tuple[int, int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return tuple(image.convert("RGB").getpixel((0, 0)))  # type: ignore[return-value]


def _child_env(fault: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env.pop("TILEREPO_FAULT_INJECT", None)
    if fault:
        env["TILEREPO_FAULT_INJECT"] = fault
    return env


# --------------------------------------------------------------------------- #
# 场景
# --------------------------------------------------------------------------- #
def scene_init(report: Report, work: Path) -> None:
    from app.tilerepo import SchemaUnsupported, TileRepository

    report.section("1. 初始化与重复打开")
    root = work / "repo"
    repo = TileRepository.open(root, create=True)
    try:
        report.check("init", "建库返回 created=True", repo.created is True)
        report.check(
            "init",
            "目录结构齐全（shards/snapshots/repair/staging/lock）",
            all((root / name).is_dir() for name in ("shards", "snapshots", "repair", "staging", "lock")),
            str(sorted(item.name for item in root.iterdir())),
        )
        report.check("init", "catalog.sqlite 落盘", (root / "catalog.sqlite").is_file())
        report.check("init", "schema_version=1", repo.catalog.meta().get("schema_version") == "1")
    finally:
        repo.close()

    with TileRepository.open(root, create=False) as reopened:
        report.check("init", "再次打开不重建（created=False）", reopened.created is False)

    broken = work / "broken"
    broken.mkdir()
    (broken / "catalog.sqlite").write_bytes(b"not a sqlite file")
    try:
        with TileRepository.open(broken, create=False):
            report.check("init", "坏 catalog 必须报错", False, "居然打开成功了")
    except Exception as exc:  # noqa: BLE001 - 这里就是要"任何类型的拒绝"
        report.check("init", "坏 catalog 被拒绝（且不销毁）", True, type(exc).__name__)
        report.check("init", "坏文件原样保留", (broken / "catalog.sqlite").read_bytes()[:4] == b"not ")

    unknown = work / "unknown"
    shutil.copytree(root, unknown)
    import sqlite3

    connection = sqlite3.connect(unknown / "catalog.sqlite", isolation_level=None)
    try:
        connection.execute("UPDATE catalog_meta SET value = '99' WHERE name = 'schema_version'")
    finally:
        connection.close()
    try:
        with TileRepository.open(unknown, create=False):
            report.check("init", "未知 schema 版本被拒绝", False, "居然打开成功了")
    except SchemaUnsupported:
        report.check("init", "未知 schema 版本抛 SchemaUnsupported", True)
    except Exception as exc:  # noqa: BLE001
        report.check("init", "未知 schema 版本抛 SchemaUnsupported", False, type(exc).__name__)


def scene_write(report: Report, root: Path) -> None:
    from app.tilerepo import LookupStatus, TileRepository

    report.section("2. 写入、幂等、冲突与去重")
    first = png_bytes((10, 20, 30))
    second = png_bytes((200, 100, 50))
    with TileRepository.open(root, create=True) as repo:
        repo.ensure_asset(ASSET)
        repo.ensure_asset(LABEL)

        result = repo.put_tiles(ASSET, [(5, 1, 1, first), (5, 1, 2, first)])
        report.check("write", "两张瓦片写入成功", result.accepted == 2, str(result.as_dict()))
        report.check("write", "写入后已发布", result.published is True)

        again = repo.put_tiles(ASSET, [(5, 1, 1, first)])
        report.check("write", "同内容重复写 → unchanged（幂等）", again.unchanged == 1 and again.written == 0)

        conflict = repo.put_tiles(ASSET, [(5, 1, 1, second)])
        report.check(
            "write",
            "同坐标不同内容默认拒绝（Conflict，不覆盖）",
            len(conflict.conflicts) == 1 and conflict.written == 0,
            str(conflict.conflicts[0].as_dict() if conflict.conflicts else "无冲突"),
        )
        still = repo.read_tile(ASSET, 5, 1, 1)
        report.check("write", "被拒绝的写入没有改掉旧字节", pixel_at(still.record.data) == (10, 20, 30))

        replaced = repo.put_tiles(ASSET, [(5, 1, 1, second)], allow_replace=True)
        report.check("write", "授权替换 → replaced=1", replaced.replaced == 1)
        now = repo.read_tile(ASSET, 5, 1, 1)
        report.check("write", "替换后读到新字节", pixel_at(now.record.data) == (200, 100, 50))
        versions = list(repo.iter_versions(ASSET, 5, 1, 1))
        report.check("write", "旧版本仍在（superseded，不是被覆盖）", len(versions) == 2, str(len(versions)))

        # 造一处"两个坐标引用同一份内容"，让去重口径有可比性
        repo.put_tiles(ASSET, [(5, 2, 3, first)])

        stats = repo.stats()
        asset = next(item for item in stats.assets if item.asset == ASSET)
        report.check(
            "dedup",
            "同内容只存一份（contents=2）",
            asset.contents == 2,
            f"contents={asset.contents}",
        )
        report.check(
            "dedup",
            "逻辑字节 > 去重字节（去重真的省了空间）",
            asset.logical_bytes > asset.dedup_bytes,
            f"logical={asset.logical_bytes} dedup={asset.dedup_bytes}",
        )
        report.check(
            "dedup",
            "物理占用 ≥ 去重字节",
            asset.physical_bytes >= asset.dedup_bytes,
            f"physical={asset.physical_bytes}",
        )

        # 再替换一次：主键 (z,x,y,state) 决定同一坐标只能留**一代**历史
        third = png_bytes((7, 8, 9))
        again_replaced = repo.put_tiles(ASSET, [(5, 1, 1, third)], allow_replace=True)
        report.check(
            "write",
            "连续替换不撞主键（只保留一代历史）",
            again_replaced.replaced == 1 and len(list(repo.iter_versions(ASSET, 5, 1, 1))) == 2,
            str(again_replaced.as_dict()),
        )
        report.check(
            "write",
            "连续替换后读到最新一版",
            pixel_at(repo.read_tile(ASSET, 5, 1, 1).record.data) == (7, 8, 9),
        )

        bad = repo.put_tiles(
            ASSET,
            [(5, 1, 3, b""), (5, 1, 4, b"<html>404</html>"), (5, 999, 0, first)],
        )
        report.check("write", "非法输入全部被拒且不写盘", bad.written == 0 and len(bad.rejected) == 3)

        # 未发布的行对普通查询不可见
        repo.put_tiles(ASSET, [(5, 2, 2, first)])
        report.check(
            "read",
            "正常读取状态为 present",
            repo.read_tile(ASSET, 5, 2, 2).status is LookupStatus.PRESENT,
        )
        report.check(
            "read",
            "不存在的坐标返回 absent（不抛异常）",
            repo.read_tile(ASSET, 5, 5, 5).status is LookupStatus.ABSENT,
        )

        # 图层隔离
        repo.put_tiles(LABEL, [(5, 2, 2, second)])
        report.check(
            "layers",
            "同坐标不同图层互不影响",
            pixel_at(repo.read_tile(ASSET, 5, 2, 2).record.data) == (10, 20, 30)
            and pixel_at(repo.read_tile(LABEL, 5, 2, 2).record.data) == (200, 100, 50)
            and repo.shard_path(ASSET, (0, 6)) != repo.shard_path(LABEL, (0, 6)),
        )


def scene_gap(report: Report, root: Path) -> None:
    from app.geo import GeoBounds, calculate_tile_range
    from app.tilerepo import TileRepository

    report.section("3. 缺口统计（目标级，不把父级回退算成齐全）")
    bounds = GeoBounds(west=10.0, south=10.0, east=20.0, north=20.0)
    with TileRepository.open(root, create=False) as repo:
        tile_range = calculate_tile_range(bounds, 5)
        total = tile_range.tile_count
        # 该级此前已被上一个场景写过一张（(5,1,1)），计数必须如实反映
        report.check("gap", "该范围张数为正", total > 0, str(total))
        done = repo.query_existence(ASSET, (5, 5), bounds)
        zoom = done.assets[0].zooms[0]
        report.check("gap", "total 与 calculate_tile_range 一致", zoom.total == total)
        report.check(
            "gap",
            "missing = total − present",
            zoom.missing == total - zoom.present,
            f"total={zoom.total} present={zoom.present} missing={zoom.missing}",
        )
        report.check("gap", "缺失样例不为空", bool(zoom.missing_sample), str(zoom.missing_sample[:3]))
        report.check(
            "gap",
            "样例都是真实存在的坐标（不越界）",
            all(
                tile_range.min_x <= x <= tile_range.max_x and tile_range.min_y <= y <= tile_range.max_y
                for x, y in zoom.missing_sample
            ),
        )


def scene_snapshot(report: Report, root: Path) -> None:
    from app.tilerepo import SnapshotExpired, TileRepository

    report.section("4. 稳定读取视图（快照）")
    with TileRepository.open(root, create=False) as repo:
        first = repo.freeze_snapshot(assets=[ASSET])
        second = repo.freeze_snapshot(assets=[ASSET])
        report.check(
            "snapshot",
            "同数据两次冻结 → 同一个指纹",
            first.fingerprint == second.fingerprint,
            f"{first.fingerprint[:12]}… / {second.fingerprint[:12]}…",
        )
        report.check("snapshot", "指纹文件只落一份", len(list(repo.snapshots_dir.glob("*.json"))) == 1)

        before = repo.read_tile(ASSET, 5, 1, 1).record.data
        repo.put_tiles(ASSET, [(5, 1, 1, png_bytes((33, 44, 55)))], allow_replace=True)
        report.check(
            "snapshot",
            "冻结之后真的发生了替换（当前已变）",
            pixel_at(repo.read_tile(ASSET, 5, 1, 1).record.data) == (33, 44, 55),
        )
        with repo.open_snapshot(first.fingerprint) as view:
            frozen = [(record.coord.as_tuple(), record.data) for record in view]
        target = [data for coord, data in frozen if coord == (5, 1, 1)]
        report.check(
            "snapshot",
            "冻结后发生的替换不影响这份视图",
            bool(target) and hashlib.sha256(target[0]).digest() == hashlib.sha256(before).digest(),
        )

        # 内容被回收 → SnapshotExpired（不静默跳过）
        from app.tilerepo import shard as shard_module

        path = repo.shard_path(ASSET, (0, 6))
        connection = shard_module.open_shard(path)
        try:
            connection.execute("DELETE FROM coords")
            connection.execute("DELETE FROM contents")
        finally:
            connection.close()
        try:
            with repo.open_snapshot(first.fingerprint) as view:
                list(view)
            report.check("snapshot", "内容被回收后抛 SnapshotExpired", False, "居然读成功了")
        except SnapshotExpired:
            report.check("snapshot", "内容被回收后抛 SnapshotExpired", True)


CRASH_CHILD = """
import io, sys
from pathlib import Path
from PIL import Image
from app.tilerepo import TileRepository

root = Path(sys.argv[1])
buffer = io.BytesIO()
Image.new('RGB', (256, 256), (11, 22, 33)).save(buffer, format='PNG')
repo = TileRepository.open(root, create=True)
repo.ensure_asset('tianditu/vec_w')
repo.put_tiles('tianditu/vec_w', [(5, 1, 2, buffer.getvalue())])
repo.close()
print('CHILD_FINISHED')
"""

#: ``阶段 -> (恢复分类, 崩溃后立刻可见性, 恢复后可见性)``
CRASH_PHASES: dict[str, tuple[str, str, str]] = {
    "after_operation": ("aborted", "absent", "absent"),
    "during_shard_write": ("aborted", "absent", "absent"),
    "after_shard_commit": ("republished", "unpublished", "present"),
    "before_publish": ("republished", "unpublished", "present"),
    "after_publish": ("completed", "present", "present"),
}


def scene_crash(report: Report, work: Path, quick: bool) -> None:
    from app.tilerepo import TileRepository

    report.section("5. 进程中断（子进程 os._exit）与恢复")
    if quick:
        for phase in CRASH_PHASES:
            report.skip("crash", f"crash.{phase}", "--quick：跳过子进程崩溃场景")
        return

    for phase, (category, before, after) in CRASH_PHASES.items():
        root = work / f"crash-{phase}"
        completed = subprocess.run(
            [sys.executable, "-c", CRASH_CHILD, str(root)],
            cwd=str(PROJECT_ROOT),
            env=_child_env(f"crash:{phase}"),
            capture_output=True,
            text=True,
            timeout=180,
        )
        report.check(
            "crash",
            f"crash.{phase} · 子进程真的被杀（退出码 97）",
            completed.returncode == 97,
            f"rc={completed.returncode} stderr={completed.stderr.strip()[:200]}",
        )
        report.check(
            "crash",
            f"crash.{phase} · 子进程没有走到最后一行",
            "CHILD_FINISHED" not in completed.stdout,
        )
        with TileRepository.open(root, create=False) as repo:
            status = repo.read_tile(ASSET, 5, 1, 2).status.value
            report.check(
                "crash",
                f"crash.{phase} · 崩溃后可见性如实（{status}）",
                status == before,
                f"期望 {before}，实际 {status}",
            )
            health = repo.health_check()
            report.check(
                "crash",
                f"crash.{phase} · 健康检查没被未完成操作打成红色",
                health.ok is True,
                "；".join(item.detail for item in health.errors),
            )
            recovery = repo.recover()
            report.check(
                "crash",
                f"crash.{phase} · 恢复分类为 {category}",
                bool(getattr(recovery, category)),
                str(recovery.as_dict()),
            )
            report.check(
                "crash",
                f"crash.{phase} · 恢复后可见性为 {after}",
                repo.read_tile(ASSET, 5, 1, 2).status.value == after,
            )
            report.check(
                "crash",
                f"crash.{phase} · 第二次恢复什么都不改（幂等）",
                repo.recover().changed == 0,
            )


HOLD_LEASE_CHILD = """
import os, sys, time
from pathlib import Path
from app.tilerepo import shard
root = Path(sys.argv[1])
lease = shard.acquire_lease(root / 'lock', sys.argv[2], label='验收占位')
print('LOCKED', os.getpid(), flush=True)
time.sleep(60)
lease.release()
"""


def scene_lease(report: Report, work: Path, quick: bool) -> None:
    from app.tilerepo import Busy, TileRepository

    report.section("6. 写租约（活锁拒绝 / 死锁接管）")
    if quick:
        report.skip("lease", "lease.live_holder_busy", "--quick：跳过子进程租约场景")
        report.skip("lease", "lease.dead_holder_takeover", "--quick：跳过子进程租约场景")
        return

    root = work / "lease"
    with TileRepository.open(root, create=True) as repo:
        repo.ensure_asset(ASSET)
        repo.put_tiles(ASSET, [(5, 1, 1, png_bytes((1, 1, 1)))])

    child = subprocess.Popen(
        [sys.executable, "-c", HOLD_LEASE_CHILD, str(root), "tianditu-vec_w-z00-06"],
        cwd=str(PROJECT_ROOT),
        env=_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        handshake = child.stdout.readline().split() if child.stdout is not None else []
        holder_pid = int(handshake[1]) if handshake[:1] == ["LOCKED"] else 0
        report.check(
            "lease",
            "第二个进程拿到了租约（占位成功）",
            holder_pid > 0,
            (child.stderr.read() if holder_pid == 0 and child.stderr else "无握手输出")[:200],
        )
        with TileRepository.open(root, create=False) as repo:
            try:
                repo.put_tiles(ASSET, [(5, 1, 2, png_bytes((2, 2, 2)))])
                report.check("lease", "活着的持有者 → Busy", False, "居然写成功了")
            except Busy as exc:
                report.check(
                    "lease",
                    "活着的持有者 → Busy（带 pid，可排查）",
                    bool(exc.detail.get("holder", {}).get("pid")),
                    str(exc.detail),
                )
            report.check(
                "lease",
                "抢不到时没有留下半个写入",
                repo.read_tile(ASSET, 5, 1, 2).status.value == "absent",
            )
    finally:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(child.pid)], capture_output=True, check=False)
        else:  # pragma: no cover
            child.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):  # pragma: no cover
            child.wait(timeout=30)

    # 占位进程已经退出：下一个写者必须立刻接管（不是等 90 秒超时）
    started = time.monotonic()
    with TileRepository.open(root, create=False) as repo:
        result = repo.put_tiles(ASSET, [(5, 1, 2, png_bytes((2, 2, 2)))])
        elapsed = time.monotonic() - started
        report.check(
            "lease",
            "持有者已死 → 下一次写入立即接管（< 10s）",
            result.accepted == 1 and elapsed < 10,
            f"用时 {elapsed:.1f}s，accepted={result.accepted}",
        )
        report.check(
            "lease",
            "接管后数据可读",
            repo.read_tile(ASSET, 5, 1, 2).status.value == "present",
        )


def scene_health(report: Report, work: Path) -> None:
    from app.tilerepo import LookupStatus, TileRepository, UnsupportedOperation
    from app.tilerepo import shard as shard_module

    report.section("7. 健康检查 / 恢复 / 索引重建 / 清理报告")
    root = work / "health"
    with TileRepository.open(root, create=True) as repo:
        repo.ensure_asset(ASSET)
        repo.put_tiles(ASSET, [(5, 1, 1, png_bytes((3, 3, 3))), (5, 1, 2, png_bytes((4, 4, 4)))])
        healthy = repo.health_check("full")
        report.check("health", "干净仓库 health.ok=True", healthy.ok is True)
        report.check("health", "干净仓库无 warning", healthy.issues == ())

        # 计数漂移：只能是 warning，且 rebuild 能修
        with repo.catalog.transaction() as connection:
            connection.execute("UPDATE shards SET tile_count = 999 WHERE asset_id = ?", (ASSET,))
        drifted = repo.health_check()
        report.check(
            "health",
            "计数漂移只算 warning（ok 仍为 True）",
            drifted.ok is True and any(item.code == "count_mismatch" for item in drifted.issues),
        )
        fixed = repo.rebuild_index()
        report.check("rebuild", "rebuild 修好计数漂移", fixed.fixed_shards == 1, str(fixed.as_dict()))
        report.check("rebuild.idempotent", "第二遍 rebuild 不再改动（幂等）", repo.rebuild_index().fixed_shards == 0)

        # 悬空引用：必须是 error
        path = repo.shard_path(ASSET, (0, 6))
        connection = shard_module.open_shard(path)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DELETE FROM contents")
        finally:
            connection.close()
        broken = repo.health_check()
        report.check(
            "health",
            "悬空引用判为 error（ok=False）",
            broken.ok is False and broken.dangling_refs > 0,
            str([item.code for item in broken.errors]),
        )
        report.check(
            "read",
            "悬空引用在读取路径上返回 corrupt（不崩、不装作有）",
            repo.read_tile(ASSET, 5, 1, 1).status is LookupStatus.CORRUPT,
        )

        report.check("health", "prune 只报告", repo.prune().dry_run is True)
        try:
            repo.prune(dry_run=False)
            report.check("health", "prune(dry_run=False) 明确拒绝", False, "居然执行了")
        except UnsupportedOperation:
            report.check("health", "prune(dry_run=False) 明确拒绝（本版本不删数据）", True)

    # catalog 整个删掉 → 只靠分片自述重建
    loss = work / "loss"
    with TileRepository.open(loss, create=True) as repo:
        repo.ensure_asset(ASSET)
        data = png_bytes((6, 7, 8))
        repo.put_tiles(ASSET, [(5, 2, 2, data)])
        meta = repo.freeze_snapshot(assets=[ASSET])
    for item in loss.glob("catalog.sqlite*"):
        item.unlink()
    with TileRepository.open(loss, create=True) as repo:
        report.check("rebuild.catalog_lost", "catalog 丢失后能重新初始化", repo.created is True)
        report.check("rebuild.catalog_lost", "重建前资产是空的（不是假装没丢）", repo.catalog.list_assets() == [])
        rebuilt = repo.rebuild_index()
        report.check(
            "rebuild.catalog_lost",
            "从分片重建出 1 个分片登记",
            rebuilt.registered_shards == 1,
            str(rebuilt.as_dict()),
        )
        lookup = repo.read_tile(ASSET, 5, 2, 2)
        report.check(
            "rebuild.catalog_lost",
            "重建后能读回原字节",
            lookup.status is LookupStatus.PRESENT and hashlib.sha256(lookup.record.data).hexdigest()
            == hashlib.sha256(data).hexdigest(),
        )
        with repo.open_snapshot(meta.fingerprint) as view:
            report.check("rebuild.catalog_lost", "快照文件也被补登记（仍可读）", sum(1 for _ in view) == 1)
        report.check("rebuild.catalog_lost", "重建后健康检查通过", repo.health_check().ok is True)


def scene_concurrency(report: Report, work: Path) -> None:
    import threading

    from app.tilerepo import TileRepository

    report.section("8. 并发（同进程多线程写同一分片）")
    root = work / "concurrent"
    with TileRepository.open(root, create=True) as repo:
        repo.ensure_asset(ASSET)
        errors: list[BaseException | None] = [None, None]
        barrier = threading.Barrier(2)

        def write(index: int, x: int) -> None:
            try:
                barrier.wait(timeout=30)
                repo.put_tiles(ASSET, [(5, x, 1, png_bytes((index * 40 + 10, 20, 30)))])
            except BaseException as exc:  # noqa: BLE001
                errors[index] = exc

        threads = [threading.Thread(target=write, args=(index, index + 1)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        report.check(
            "concurrency",
            "两个线程写同一分片都成功",
            errors == [None, None],
            str([repr(item) for item in errors if item is not None]),
        )
        rows = list(repo.iter_tiles(ASSET))
        report.check("concurrency", "两行都在，且只有两行", len(rows) == 2, f"{len(rows)} 行")
        report.check("concurrency", "并发之后健康检查通过", repo.health_check().ok is True)


def scene_bulk(report: Report, work: Path) -> None:
    from app.tilerepo import TileRepository

    report.section("9. 批量写入与流式遍历（2,000 张）")
    root = work / "bulk"
    palette = [png_bytes(((index * 31) % 256, (index * 17) % 256, (index * 7) % 256)) for index in range(16)]
    coords = [(7, x, y) for x in range(64) for y in range(64)][:2000]
    with TileRepository.open(root, create=True) as repo:
        repo.ensure_asset(ASSET)
        started = time.monotonic()
        result = repo.put_tiles(
            ASSET, [(z, x, y, palette[(x * 64 + y) % 16]) for z, x, y in coords]
        )
        elapsed = time.monotonic() - started
        report.check(
            "bulk",
            "2,000 张写入成功",
            result.accepted == 2000,
            f"{result.accepted} 张，用时 {elapsed:.1f}s",
        )

        previous = None
        ordered = True
        count = 0
        for record in repo.iter_tiles(ASSET):
            current = record.coord.as_tuple()
            if previous is not None and current <= previous:
                ordered = False
            previous = current
            count += 1
        report.check("bulk", "流式遍历条数正确", count == 2000, f"{count} 条")
        report.check("bulk", "流式遍历顺序严格递增（可直接顺序写包）", ordered)

        stats = repo.stats()
        asset = stats.assets[0]
        report.check(
            "bulk",
            "去重后内容实体只有 16 份",
            asset.contents == 16,
            f"contents={asset.contents}",
        )
        report.check(
            "bulk",
            "逻辑字节 > 去重字节（同一份内容被多个坐标引用）",
            asset.logical_bytes > asset.dedup_bytes,
            f"logical={asset.logical_bytes} dedup={asset.dedup_bytes}",
        )

        snapshot = repo.freeze_snapshot(assets=[ASSET])
        report.check("bulk", "2,000 条的快照条目数正确", snapshot.entries == 2000, str(snapshot.entries))


def scene_static_guards(report: Report) -> None:
    report.section("10. 静态守卫（分层与不联网）")
    package = PROJECT_ROOT / "app" / "tilerepo"
    forbidden = ("app.ui", "app.package", "PySide6", "requests", "httpx", "aiohttp", "urllib.request")
    offenders: list[str] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
            for module in modules:
                if module.startswith(forbidden):
                    offenders.append(f"{path.name}: {module}")
    report.check(
        "static",
        "仓库层不依赖 UI / 打包 / 网络库",
        offenders == [],
        "；".join(offenders),
    )
    report.check(
        "static",
        "仓库层没有 PySide6（可在无界面环境下复用）",
        all("PySide6" not in offender for offender in offenders),
    )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Slice 42 统一瓦片仓库离线验收（临时目录、不联网）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--report", type=Path, help="把机器可读报告写到这个路径（JSON）")
    parser.add_argument("--keep", action="store_true", help="保留临时仓库目录（排查用）")
    parser.add_argument("--quick", action="store_true", help="跳过子进程场景（崩溃/租约）")
    args = parser.parse_args(argv)

    try:
        import PIL  # noqa: F401
    except ImportError:
        print("缺少 Pillow（合成瓦片需要它）：python -m pip install pillow", file=sys.stderr)
        return 2

    try:
        work = Path(tempfile.mkdtemp(prefix="tilerepo-verify-"))
    except OSError as exc:
        print(f"无法创建临时目录：{exc}", file=sys.stderr)
        return 2

    report = Report(root=str(work))
    print(f"临时仓库根：{work}")
    print(f"解释器：{sys.executable}")
    try:
        scene_init(report, work / "shard-a")
        scene_write(report, work / "shard-b")
        scene_gap(report, work / "shard-b")
        scene_snapshot(report, work / "shard-b")
        scene_crash(report, work, args.quick)
        scene_lease(report, work, args.quick)
        scene_health(report, work)
        scene_concurrency(report, work)
        scene_bulk(report, work)
        scene_static_guards(report)
    except Exception:  # noqa: BLE001 - 脚本自己崩了也要给出可读结论与非零退出码
        traceback.print_exc()
        report.check("script", "验收脚本自身没有异常退出", False, traceback.format_exc().splitlines()[-1])
    finally:
        report.print_summary()
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(report.payload(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"\n机器可读报告：{args.report}")
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"保留临时目录：{work}")

    return 0 if not report.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
