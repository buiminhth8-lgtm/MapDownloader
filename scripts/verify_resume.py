"""断点续传端到端验证脚本（Slice 5）。

模拟真实使用路径，**全程用 ``httpx.MockTransport`` 假服务端，不触网、不消耗配额**，
但走的是与生产完全相同的代码路径（真 SQLite、真写队列、真跳过判定）：

::

    第 1 轮：目标 6 张，下到一半「关闭程序」     → PAUSED
    第 2 轮：重新打开程序，点「继续」            → 只请求剩下的那几张
    第 3 轮：全部已有，再点「继续」              → 0 个请求

每一步都把"HTTP 请求数 / 库里瓦片数 / 任务状态 / 是否达标"打出来，
最后断言关键不变量。任何一条不成立就以非 0 退出码结束。

用法::

    python scripts/verify_resume.py
    python scripts/verify_resume.py --keep       # 保留临时目录，便于手工检查
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

import httpx

# 允许直接以脚本方式运行（脚本位于 scripts/ 下，需要把项目根加入 sys.path）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.downloader import RateLimiter, RetryPolicy  # noqa: E402
from app.geo import GeoBounds, calculate_tile_ranges  # noqa: E402
from app.mbtiles import MBTilesReader  # noqa: E402
from app.models import DownloadTaskStatus, MapLayerType  # noqa: E402
from app.providers import PNG_SIGNATURE, TiandituProvider  # noqa: E402
from app.services import MapDownloadService  # noqa: E402
from app.tasks import TaskService, TaskStore  # noqa: E402

#: 假 Token（**不是真实密钥**，仅用于拼出合法的 WMTS URL）
FAKE_TOKEN = "abcdefghijklmn"

#: 与 Slice 2 一致的假瓦片：只要文件头正确就算合法
FAKE_PNG = PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + b"verify-payload" * 8

BOUNDS = GeoBounds(116.0, 39.0, 116.5, 39.5)
MIN_ZOOM = 13
MAX_ZOOM = 13
LAYER = MapLayerType.VECTOR

#: 假服务端每张瓦片的"服务端耗时"。
#: 之所以需要它：验证的第 1 轮要"下到一半取消"，如果服务端零延迟、并发又高，
#: 整个任务会在一两个事件循环 tick 内跑完，根本没有取消的窗口期。
FAKE_SERVER_DELAY = 0.02


class CountingServer:
    """假天地图服务端：统计请求 + 制造一点延迟，好让"取消"有窗口期。"""

    def __init__(self) -> None:
        self.requests: list[tuple[int, int, int]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        self.requests.append(
            (int(params["TILEMATRIX"]), int(params["TILECOL"]), int(params["TILEROW"]))
        )
        await asyncio.sleep(FAKE_SERVER_DELAY)
        return httpx.Response(
            200, content=FAKE_PNG, headers={"content-type": "image/png"}
        )

    def reset(self) -> None:
        self.requests.clear()


def _target_count() -> int:
    return sum(
        item.tile_count for item in calculate_tile_ranges(BOUNDS, MIN_ZOOM, MAX_ZOOM)
    )


def _all_coords() -> set[tuple[int, int, int]]:
    """完整目标范围展开成 ``{(z, x, y_xyz)}``（与 HTTP 请求同一套坐标）。"""
    coords: set[tuple[int, int, int]] = set()
    for span in calculate_tile_ranges(BOUNDS, MIN_ZOOM, MAX_ZOOM):
        for x in range(span.min_x, span.max_x + 1):
            for y in range(span.min_y, span.max_y + 1):
                coords.add((span.zoom, x, y))
    return coords


def _db_count(path: Path) -> int:
    if not path.is_file():
        return 0
    with MBTilesReader(path) as reader:
        return reader.get_tile_count()


def _make_service(server: CountingServer) -> MapDownloadService:
    return MapDownloadService(
        TiandituProvider(FAKE_TOKEN),
        transport=httpx.MockTransport(server),
        concurrency=2,
        retry_policy=RetryPolicy(
            max_attempts=2, base_delay_seconds=0.0, max_delay_seconds=0.0
        ),
        rate_limiter=RateLimiter.disabled(),
        batch_size=3,
    )


class Checks:
    """收集断言结果：全部跑完再打印，避免"第一条挂了就看不到后面"。"""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def ok(self, condition: bool, label: str) -> None:
        mark = "OK  " if condition else "FAIL"
        print(f"  [{mark}] {label}")
        if not condition:
            self.failures.append(label)


async def _run(work_dir: Path, checks: Checks) -> None:
    server = CountingServer()
    task_store = TaskStore(work_dir / "tasks.json")
    task_service = TaskService(task_store)

    target_path = work_dir / "beijing.mbtiles"
    total = _target_count()
    print(f"目标瓦片数（Tile Math 计算）：{total}")
    print(f"输出文件：{target_path}\n")

    # --- 建档 -------------------------------------------------------------
    task = task_service.create_task(
        name="beijing",
        bounds=BOUNDS,
        min_zoom=MIN_ZOOM,
        max_zoom=MAX_ZOOM,
        map_layer=LAYER,
        output_path=target_path,
        total_tiles=total,
    )

    # --- 第 1 轮：跑到一半取消，模拟"用户关掉程序" -------------------------
    print("第 1 轮：下载到一半取消（模拟关闭程序）")
    service = _make_service(server)
    half = max(1, total // 2)

    async def run_and_cancel():
        handle = asyncio.create_task(service.resume_task(task, FAKE_TOKEN))
        # 判定依据用「已发出的请求数」而不是「库里已有张数」：
        # 前者在 handler 里同步记录，既不碰数据库（避免与写线程抢锁），
        # 也不会因为写队列有延迟而错过取消时机。
        deadline = asyncio.get_running_loop().time() + 30.0
        while len(server.requests) < half:
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("第 1 轮没有在预期时间内下到一半")
            await asyncio.sleep(0.01)
        service.cancel()
        return await handle

    first = await run_and_cancel()
    written = _db_count(target_path)
    print(f"  已请求 {len(server.requests)} 张 · 库里 {written} 张（目标 {total}）")
    checks.ok(first.cancelled, "第 1 轮以「取消」结束")
    checks.ok(0 < written < total, "第 1 轮只下了一部分（不是空文件也不是完整文件）")
    checks.ok(first.tile_count == written, "结果里的瓦片数与库里的实际张数一致")
    task_service.pause_task(task)  # 关窗时 GUI 做的就是这件事
    checks.ok(
        task_store.get(task.id).status is DownloadTaskStatus.PAUSED,
        "任务记录落盘为「已暂停」",
    )

    requests_round1 = len(server.requests)

    # 第 1 轮结束时库里的坐标（XYZ 语义，与 HTTP 请求的 TILEROW 同一套坐标）。
    # 必须在第 2 轮**之前**快照，否则第 2 轮把库填满后就无从对照了。
    with MBTilesReader(target_path) as reader:
        existing_xyz_before = set(reader.get_existing_tiles(MIN_ZOOM))
    checks.ok(
        len(existing_xyz_before) == written,
        "Reader 报出的已有坐标数与统计张数一致",
    )

    # --- 第 2 轮：重新打开程序，点「继续」 --------------------------------
    print("\n第 2 轮：重新打开程序 → 点「继续」")
    reloaded = task_store.get(task.id)
    assert reloaded is not None, "任务记录应当已经落盘"
    server.reset()
    service2 = _make_service(server)
    second = await service2.resume_task(reloaded, FAKE_TOKEN, task_service=task_service)
    print(
        f"  本轮请求 {len(server.requests)} 张 · 跳过 {second.summary.skipped} 张 · "
        f"库里 {second.tile_count} 张"
    )
    checks.ok(len(server.requests) == total - written, "只请求了缺失的那些瓦片")
    checks.ok(second.summary.skipped == written, "已有瓦片全部被跳过")
    checks.ok(second.tile_count == total, "库里张数达到目标")
    checks.ok(second.is_success, "本轮判定为「已完成」")
    checks.ok(
        second.verdict is DownloadTaskStatus.COMPLETED, "任务状态判定为 COMPLETED"
    )
    checks.ok(
        task_store.get(task.id).status is DownloadTaskStatus.COMPLETED,
        "任务记录落盘为「已完成」",
    )
    checks.ok(second.skip_mode is not None, f"跳过策略：{second.skip_mode.display_name}")
    checks.ok(
        len(server.requests) == len(set(server.requests)),
        "本轮没有对同一张瓦片重复发请求",
    )

    # 已有的瓦片一定不在本轮的请求里。
    # 两端都是 XYZ 语义：WMTS ``_w`` 是左上原点（TILEROW = XYZ y），
    # 而 ``get_existing_tiles`` 也返回 XYZ，所以**不需要**翻成 TMS。
    requested = set(server.requests)
    checks.ok(
        requested.isdisjoint(existing_xyz_before),
        "本轮请求与第 1 轮结束时已有的坐标无交集",
    )
    checks.ok(
        requested | existing_xyz_before == _all_coords(),
        "「本轮请求」∪「已有」正好覆盖完整目标范围",
    )

    # --- 第 3 轮：全部已有，再点一次「继续」 ------------------------------
    print("\n第 3 轮：全部已有，再点一次「继续」")
    server.reset()
    service3 = _make_service(server)
    third = await service3.resume_task(
        task_store.get(task.id), FAKE_TOKEN, task_service=task_service
    )
    print(
        f"  本轮请求 {len(server.requests)} 张 · 跳过 {third.summary.skipped} 张 · "
        f"库里 {third.tile_count} 张"
    )
    checks.ok(len(server.requests) == 0, "一张都没重新请求（不浪费天地图配额）")
    checks.ok(third.summary.skipped == total, "全部命中已有瓦片")
    checks.ok(third.is_success, "仍然判定为「已完成」")
    checks.ok(
        _db_count(target_path) == total, "库里仍然是目标张数（没有重复行）"
    )

    # --- 收尾：目录里只能有一个 .mbtiles，没有 -wal / -shm 泄漏 -----------
    leftovers = sorted(
        item.name for item in work_dir.iterdir() if item.name != "tasks.json"
    )
    checks.ok(leftovers == [target_path.name], f"目录内容：{leftovers}")
    text = (work_dir / "tasks.json").read_text(encoding="utf-8")
    checks.ok(FAKE_TOKEN not in text and "token" not in text.lower(), "Token 没有落盘")
    print(f"\n（第 1 轮共请求 {requests_round1} 张，用于对照第 2 轮）")


def main() -> int:
    parser = argparse.ArgumentParser(description="断点续传端到端验证")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    args = parser.parse_args()

    work_dir = Path(tempfile.mkdtemp(prefix="resume-verify-"))
    print(f"工作目录：{work_dir}\n")
    checks = Checks()
    try:
        asyncio.run(_run(work_dir, checks))
    finally:
        if not args.keep:
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            print(f"\n临时目录已保留：{work_dir}")

    print()
    if checks.failures:
        print(f"验证失败：{len(checks.failures)} 项未通过")
        for item in checks.failures:
            print(f"  - {item}")
        return 1
    print("验证通过：断点续传全链路符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
