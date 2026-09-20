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
import struct
import sys
import tempfile
import zlib
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

#: 假瓦片的边长。64 够小（省时间），又足够"像个瓦片"。
TILE_SIZE = 64


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    """拼一个 PNG 数据块：长度 + 类型 + 数据 + CRC32。"""
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def _make_valid_png(rgb: tuple[int, int, int] = (0x33, 0x66, 0xCC),
                    size: int = TILE_SIZE) -> bytes:
    """生成一张**真正能被解码**的单色真彩 PNG。

    为什么不能用"文件头对、正文随便"的老夹具：**校验器会抽样解码**。
    Slice 8 给 ``MapDownloadValidator`` 加了"抽 20 张试着解码"这一层，
    于是老夹具（``PNG_SIGNATURE + IHDR + b"verify-payload" * 8``）**必然**
    把整轮判成失败：瓦片数 195/195、零失败，却报
    「MBTiles 校验未通过：抽样 20 张中有 20 张图片无法解码：… 文件头对但正文
    不是有效图片」，任务状态跟着变成 FAILED。

    **判断方向很重要**：真实天地图返回的就是有效 PNG，所以**产品是对的、
    夹具是假的**。这一类"判据变严导致旧脚本假红"和"产品真的坏了"长得
    一模一样，只有看 ``outcome.failure_text`` 才分得开 —— 只报一个
    ``is_success=False`` 的话，看的人会跑去查下载逻辑，方向全错。
    （同一个坑在 Android 侧也踩过：夹具用 2048 个随机字节冒充 ``map.mbtiles``，
    加了一层内容探测之后 8 个用例一起红。）
    """
    raw = bytearray()
    for _ in range(size):
        raw.append(0)                       # filter type 0
        raw.extend(bytes(rgb) * size)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)   # 8bit / 真彩
    return (
        PNG_SIGNATURE
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _png_chunk(b"IEND", b"")
    )


#: 假瓦片。**必须是一张能真解码的 PNG**，见 :func:`_make_valid_png`。
FAKE_PNG = _make_valid_png()

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

    def ok(self, condition: bool, label: str, detail: str = "") -> None:
        """``detail`` 只在失败时打印 —— 用来回答"为什么不是期望的那个"。

        这一条不是装饰：``is_success`` 是**四个条件的与**（未取消 + 校验通过 +
        零失败 + 库里张数达标），只报一个 ``False`` 的话，看的人得回去读产品代码
        才知道是哪一条挂了。挂的层次不同，修法完全不同（校验口径变了 vs 真的少瓦片）。
        """
        mark = "OK  " if condition else "FAIL"
        print(f"  [{mark}] {label}{f' — {detail}' if detail and not condition else ''}")
        if not condition:
            self.failures.append(label if not detail else f"{label} — {detail}")


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
    checks.ok(second.is_success, "本轮判定为「已完成」",
              f"failure_text={second.failure_text!r} "
              f"cancelled={second.cancelled} "
              f"valid={second.validation.valid} "
              f"errors={list(second.validation.errors)} "
              f"failed={second.summary.failed} "
              f"tile_count={second.tile_count}/{second.target_tiles}")
    checks.ok(
        second.verdict is DownloadTaskStatus.COMPLETED, "任务状态判定为 COMPLETED",
        f"verdict={second.verdict}",
    )
    checks.ok(
        task_store.get(task.id).status is DownloadTaskStatus.COMPLETED,
        "任务记录落盘为「已完成」",
        f"实际 {task_store.get(task.id).status}",
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
    checks.ok(third.is_success, "仍然判定为「已完成」",
              f"failure_text={third.failure_text!r} "
              f"cancelled={third.cancelled} "
              f"valid={third.validation.valid} "
              f"errors={list(third.validation.errors)} "
              f"failed={third.summary.failed} "
              f"tile_count={third.tile_count}/{third.target_tiles}")
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
