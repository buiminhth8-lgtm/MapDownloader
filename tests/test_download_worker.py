"""下载线程胶水层（Slice 7，Spec §7 / §26）。

:class:`~app.ui.download_worker.DownloadWorker` 本身**不含业务逻辑**：瓦片枚举、
并发、限速在 ``app.downloader``；落盘在 ``app.mbtiles``；编排在 ``app.services``。
它只做四件事：

1. 在另一个线程里起一个 ``asyncio`` 事件循环（Qt 主线程的事件循环不是 asyncio 循环）；
2. 把进度 / 结果通过 Qt 信号送回主线程；
3. 支持 ``pause`` / ``resume`` / ``cancel``，包括"任务还没真正启动就被点了"；
4. **绝不**让异常冒泡进 Qt 事件循环。

这四条正是本文件要测的全部内容。下载算法本身由
:mod:`tests.test_download_to_mbtiles` / :mod:`tests.test_composition_download`
覆盖，这里不重复。
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import httpx
import pytest
from PySide6.QtCore import QThread
from PIL import Image

from app.geo import GeoBounds
from app.mbtiles import MBTilesReader, MBTilesWriteError
from app.models import (
    DownloadTask,
    DownloadTaskStatus,
    MapLayerType,
    MapType,
)
from app.providers import PNG_SIGNATURE, TiandituProvider
from app.services import MapResumeError
from app.ui import download_worker as download_worker_module
from app.ui.download_worker import DownloadWorker

FAKE_TOKEN = "abcdefghijklmn"

#: z12 下 116.0~116.1 / 39.0~39.1 是一个只有几张瓦片的小区域
_BOUNDS = GeoBounds(116.0, 39.0, 116.1, 39.1)
_ZOOM = 12


def _png(size: int = 256) -> bytes:
    """一张真的能被解码的 PNG（合成模式要 ``Image.open``，桩数据不能糊弄）。"""
    buffer = io.BytesIO()
    Image.new("RGBA", (size, size), (0, 0, 0, 0)).save(buffer, format="PNG")
    data = buffer.getvalue()
    assert data.startswith(PNG_SIGNATURE)
    return data


FAKE_PNG = _png()


def _handler(
    status_code: int = 200,
    content: bytes = FAKE_PNG,
    content_type: str = "image/png",
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code, content=content, headers={"content-type": content_type}
        )

    return handler


class _SlowTransport(httpx.AsyncBaseTransport):
    """在每个请求前加一个**异步**延迟，用来制造"任务还在跑"的窗口。

    用 ``asyncio.sleep`` 而不是 ``time.sleep``：后者会把 worker 线程里的事件
    循环一起按住，取消指令要等请求回来才生效，测试就退化成"取消一个已经跑完
    的任务"。
    """

    def __init__(self, handler, delay: float) -> None:
        self._delay = delay
        self._inner = httpx.MockTransport(handler)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(self._delay)
        return await self._inner.handle_async_request(request)


def _make_worker(
    output_path: Path,
    *,
    handler=None,
    delay: float = 0.0,
    layer: MapLayerType | MapType = MapType.VECTOR,
    with_labels: bool = False,
) -> DownloadWorker:
    return DownloadWorker(
        TiandituProvider(FAKE_TOKEN),
        _BOUNDS,
        _ZOOM,
        _ZOOM,
        layer,
        output_path,
        with_labels=with_labels,
        transport=_SlowTransport(handler or _handler(), delay)
        if delay
        else httpx.MockTransport(handler or _handler()),
    )


def _collect(worker: DownloadWorker) -> dict[str, list]:
    """挂上三个信号的记录器（信号名 → 收到的载荷列表）。"""
    seen: dict[str, list] = {"progress": [], "completed": [], "failed": []}
    worker.progress_changed.connect(seen["progress"].append)
    worker.completed.connect(seen["completed"].append)
    worker.failed.connect(seen["failed"].append)
    return seen


def _run_to_completion(worker: DownloadWorker, qapp, timeout: int = 20_000) -> None:
    worker.start()
    assert worker.wait(timeout) is True, "下载线程没有在超时前结束"
    for _ in range(8):
        qapp.processEvents()


def _wait_until(qapp, predicate, timeout_s: float = 20.0) -> bool:
    """轮询等待条件成立，期间持续处理 Qt 事件（主线程不阻塞）。

    用途：取消类用例需要"任务已经真的写出了一些瓦片"这个中间状态。若在
    第一张瓦片落盘前就取消，测试就退化成"取消一个空任务"——那样的绿是假的。
    """
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _fake_resume_task(path: Path) -> DownloadTask:
    """一条最小的续传任务记录（只用来把 worker 推上 ``resume_task`` 分支）。"""
    return DownloadTask(
        id="task-1",
        name="beijing",
        status=DownloadTaskStatus.PAUSED,
        west=_BOUNDS.west,
        south=_BOUNDS.south,
        east=_BOUNDS.east,
        north=_BOUNDS.north,
        min_zoom=_ZOOM,
        max_zoom=_ZOOM,
        map_layer=MapLayerType.VECTOR.value,
        output_path=path,
        total_tiles=6,
    )


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #
def test_worker_downloads_and_writes_mbtiles(qapp, work_dir: Path) -> None:
    path = work_dir / "beijing.mbtiles"
    worker = _make_worker(path)
    seen = _collect(worker)

    _run_to_completion(worker, qapp)

    assert seen["failed"] == []
    assert len(seen["completed"]) == 1
    outcome = seen["completed"][0]
    assert outcome.cancelled is False
    assert outcome.is_success is True
    assert outcome.tile_count > 0

    with MBTilesReader(path) as reader:
        assert reader.get_tile_count() == outcome.tile_count


def test_worker_emits_progress_before_completion(qapp, work_dir: Path) -> None:
    """进度信号必须在完成信号**之前**到（否则进度条永远是 0）。"""
    path = work_dir / "beijing.mbtiles"
    events: list[str] = []
    worker = _make_worker(path)
    worker.progress_changed.connect(lambda _p: events.append("progress"))
    worker.completed.connect(lambda _o: events.append("completed"))

    _run_to_completion(worker, qapp)

    assert events, "至少应当有一帧进度"
    assert events[-1] == "completed"
    assert "progress" in events


def test_progress_payload_is_a_download_progress(qapp, work_dir: Path) -> None:
    path = work_dir / "beijing.mbtiles"
    worker = _make_worker(path)
    seen = _collect(worker)

    _run_to_completion(worker, qapp)

    progress = seen["progress"][-1]
    assert progress.total > 0
    assert 0 <= progress.completed <= progress.total
    assert progress.succeeded + progress.failed == progress.completed


# --------------------------------------------------------------------------- #
# 线程边界
# --------------------------------------------------------------------------- #
def test_worker_runs_off_the_main_thread(qapp, work_dir: Path, monkeypatch) -> None:
    """下载必须发生在后台线程：网络 + SQLite + Pillow 一样都不许碰主线程。

    做法是给 ``MapDownloadService.download_to_mbtiles`` 套一层记录，看它被调用
    时所在的线程。**不要**用 ``worker.currentThread()``：那是静态方法，返回的是
    调用方自己的线程，"在当前线程问它在哪个线程"永远得到当前线程。
    """
    path = work_dir / "beijing.mbtiles"
    seen_threads: list[QThread] = []
    original = download_worker_module.MapDownloadService.download_to_mbtiles

    async def spy(self, *args: object, **kwargs: object):
        seen_threads.append(QThread.currentThread())
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(
        download_worker_module.MapDownloadService, "download_to_mbtiles", spy
    )

    worker = _make_worker(path, delay=0.05)
    main_thread = qapp.thread()

    worker.start()
    assert worker.isRunning() is True
    assert worker.isCurrentThread() is False  # 我们不在它的线程里
    assert worker.wait(20_000) is True
    for _ in range(8):
        qapp.processEvents()

    assert len(seen_threads) == 1
    assert seen_threads[0] is not main_thread, "下载编排跑在了主线程上"


def test_signals_are_delivered_on_the_main_thread(qapp, work_dir: Path) -> None:
    """信号处理必须在主线程执行——UI 只能在主线程被改。"""
    path = work_dir / "beijing.mbtiles"
    worker = _make_worker(path)
    threads: list[QThread] = []
    worker.completed.connect(lambda _o: threads.append(QThread.currentThread()))

    _run_to_completion(worker, qapp)

    assert threads == [qapp.thread()]


def test_worker_is_not_started_until_start_is_called(qapp, work_dir: Path) -> None:
    worker = _make_worker(work_dir / "beijing.mbtiles")
    assert worker.isRunning() is False
    assert worker.is_resume is False


# --------------------------------------------------------------------------- #
# 暂停 / 继续 / 取消
# --------------------------------------------------------------------------- #
def test_pause_and_resume_flags(qapp, work_dir: Path) -> None:
    """``pause()`` / ``resume()`` 的可见效果就是 :attr:`pause_requested`。"""
    worker = _make_worker(work_dir / "beijing.mbtiles")

    assert worker.pause_requested is False
    worker.pause()
    assert worker.pause_requested is True
    worker.resume()
    assert worker.pause_requested is False


def test_control_calls_before_start_do_not_crash(qapp, work_dir: Path) -> None:
    """用户点得快（或测试点得快）时，指令会在 service 建好之前到达。

    此时 ``_service`` 还是 ``None``，指令只能先记在标志位里——但**不能崩**。
    """
    worker = _make_worker(work_dir / "beijing.mbtiles")

    worker.pause()
    worker.resume()
    worker.cancel()

    assert worker.pause_requested is False


def test_cancel_stops_the_download_and_keeps_the_file(qapp, work_dir: Path) -> None:
    """取消：任务停下来，但**已经下到的瓦片留在文件里**（Spec §15）。

    这是"取消"与"删除"的分界线。用户为这些瓦片花掉的是天地图的配额和真实的
    时间；把它们丢掉等于让用户白付一次代价。
    """
    path = work_dir / "beijing.mbtiles"
    worker = _make_worker(path, delay=0.05)
    seen = _collect(worker)

    worker.start()
    # 等到引擎确实报过"已经下了至少一张"，否则测的是"取消一个空任务"
    assert _wait_until(
        qapp, lambda: any(p.completed > 0 for p in seen["progress"])
    ), "下了一张瓦片都没等到"

    worker.cancel()
    assert worker.wait(20_000) is True
    for _ in range(8):
        qapp.processEvents()

    assert seen["failed"] == []
    assert len(seen["completed"]) == 1
    outcome = seen["completed"][0]
    assert outcome.cancelled is True
    # 文件存在、可打开、里面的瓦片数就是已经写进去的那些
    assert path.exists()
    with MBTilesReader(path) as reader:
        assert reader.get_tile_count() == outcome.tile_count


def test_cancel_before_any_tile_is_still_handled(qapp, work_dir: Path) -> None:
    """"还没开始就取消"也必须干净收尾（指令要在首帧进度回调里落地）。"""
    path = work_dir / "beijing.mbtiles"
    worker = _make_worker(path, delay=0.05)
    seen = _collect(worker)

    worker.start()
    worker.cancel()  # service 可能还没建好

    assert worker.wait(20_000) is True
    for _ in range(8):
        qapp.processEvents()

    assert seen["failed"] == []
    assert len(seen["completed"]) == 1
    assert seen["completed"][0].cancelled is True


# --------------------------------------------------------------------------- #
# 失败路径：异常必须变成信号，而不是冒泡
# --------------------------------------------------------------------------- #
def test_write_error_becomes_a_failed_signal(
    qapp, work_dir: Path, monkeypatch
) -> None:
    """写盘失败（磁盘满 / 无权限 / 库损坏）要把原话交给用户。"""

    async def boom(*_args: object, **_kwargs: object) -> object:
        raise MBTilesWriteError("磁盘已满")

    monkeypatch.setattr(
        download_worker_module.MapDownloadService, "download_to_mbtiles", boom
    )

    worker = _make_worker(work_dir / "beijing.mbtiles")
    seen = _collect(worker)
    _run_to_completion(worker, qapp)

    assert seen["completed"] == []
    assert len(seen["failed"]) == 1
    assert "写入 MBTiles 失败" in seen["failed"][0]
    assert "磁盘已满" in seen["failed"][0]


def test_resume_error_message_is_passed_through(
    qapp, work_dir: Path, monkeypatch
) -> None:
    """续传参数不匹配时，消息本身就是给用户看的——不该被套上一层前缀。"""

    async def boom(*_args: object, **_kwargs: object) -> object:
        raise MapResumeError("已有文件属于另一个范围")

    monkeypatch.setattr(
        download_worker_module.MapDownloadService, "resume_task", boom
    )

    worker = DownloadWorker(
        TiandituProvider(FAKE_TOKEN),
        _BOUNDS,
        _ZOOM,
        _ZOOM,
        MapLayerType.VECTOR,
        work_dir / "beijing.mbtiles",
        transport=httpx.MockTransport(_handler()),
        resume_task=_fake_resume_task(work_dir / "beijing.mbtiles"),
    )
    seen = _collect(worker)
    _run_to_completion(worker, qapp)

    assert seen["failed"] == ["已有文件属于另一个范围"]


def test_unexpected_exception_becomes_a_failed_signal(
    qapp, work_dir: Path, monkeypatch
) -> None:
    """任何意外异常都必须被兜住。

    让异常冒泡进 Qt 事件循环的后果是进程直接消失、连 traceback 都没有
    ——那比"给用户看一句不好看的话"糟得多。
    """

    async def boom(*_args: object, **_kwargs: object) -> object:
        raise ValueError("谁也没想到")

    monkeypatch.setattr(
        download_worker_module.MapDownloadService, "download_to_mbtiles", boom
    )

    worker = _make_worker(work_dir / "beijing.mbtiles")
    seen = _collect(worker)
    _run_to_completion(worker, qapp)

    assert len(seen["failed"]) == 1
    assert "下载异常终止" in seen["failed"][0]
    assert "谁也没想到" in seen["failed"][0]


# --------------------------------------------------------------------------- #
# 只读状态
# --------------------------------------------------------------------------- #
def test_read_only_properties_reflect_constructor(work_dir: Path) -> None:
    path = work_dir / "beijing.mbtiles"
    worker = DownloadWorker(
        TiandituProvider(FAKE_TOKEN),
        _BOUNDS,
        10,
        12,
        MapType.IMAGE,
        path,
        with_labels=True,
    )

    assert worker.layer is MapType.IMAGE
    assert worker.with_labels is True
    assert worker.min_zoom == 10
    assert worker.max_zoom == 12
    assert worker.output_path == path
    assert worker.is_resume is False


def test_no_public_attribute_exposes_the_token(work_dir: Path) -> None:
    """Token 不落盘、不展示：worker 上不该有**公开**属性把它吐出来。

    Spec §9 把"Token 只在内存里活一次"当成硬性要求，因此这里顺手确认
    ``DownloadWorker`` 没有变成一条新的泄露路径。

    只看公开名字（不以 ``_`` 开头）：``_token`` 是续传时必须持有的私有状态
    （任务记录里不存 Token），这本身不是泄露；泄露指的是"某个界面 / 日志能
    顺手读到它"。
    """
    worker = DownloadWorker(
        TiandituProvider(FAKE_TOKEN),
        _BOUNDS,
        _ZOOM,
        _ZOOM,
        MapLayerType.VECTOR,
        work_dir / "beijing.mbtiles",
    )

    public = [name for name in dir(worker) if not name.startswith("_")]
    for name in public:
        assert "token" not in name.lower(), f"{name} 是一条公开的 Token 出口"

    # 打印出来也不该带出密钥（Qt 侧不会打印 __dict__，但这条能挡住
    # 将来有人给 worker 加个 __repr__ 顺手把字段全列出来）
    assert FAKE_TOKEN not in repr(worker)
    assert FAKE_TOKEN not in str(worker)
    assert FAKE_TOKEN not in repr(worker.__dict__)
