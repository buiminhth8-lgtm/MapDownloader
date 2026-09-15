"""已有瓦片扫描线程（Slice 7，Spec §7 与 §11）。

为什么必须有这个类
------------------

"目标文件里已经有多少张瓦片"要靠 SQLite 的 ``GROUP BY zoom_level`` 查询得到。
这个查询本身很快（不读 BLOB），但**快不代表可以在 UI 线程里跑**：

- 一个几十万张瓦片的库，计数查询实测在百毫秒到秒级；换个机械盘或
  杀毒软件正在扫这个文件时，会明显更久；
- 它的代价随用户的库增长而增长——今天点击不卡，不代表明年不卡；
- 更糟的是它出现在两条高频路径上（「开始下载」与任务列表的「继续」），
  一旦卡住，用户看到的是"整个窗口没反应"，而不是"某个按钮慢"。

因此 Spec §7 把 SQLite 明确列进"禁止在 Qt 主线程执行"的名单。本模块是
那条禁令的落点：把 :func:`app.services.count_existing_tiles` 挪到一个
``QThread`` 里，结果通过信号送回主线程。

为什么不用 ``concurrent.futures`` / ``asyncio``：这是**一次性、短时、无并发**
的工作，``QThread`` + 一个信号就够，而且与本项目已有的
:class:`~app.ui.download_worker.DownloadWorker`、
:class:`~app.ui.connection_check.ConnectionTestWorker` 是同一套模式
（起线程 → 干一件事 → 发信号 → 退出）。引入第二种并发范式只会让人多学一样东西。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from app.services import count_existing_tiles

__all__ = ["ExistingTileScan", "ExistingTileScanWorker"]


@dataclass(frozen=True)
class ExistingTileScan:
    """一次扫描的结果。

    Attributes:
        output_path: 被扫描的 ``.mbtiles`` 路径。
        exists: 扫描时文件是否存在。
        tiles: 该文件在目标 zoom 区间里的瓦片数（不存在时为 0）。
        zooms: 参与统计的缩放级别，用于日志与断言。
        error: 扫描本身失败的原因；``None`` 表示没出问题。
            注意"文件不存在"**不算**错误（那是正常情况：全新下载）。
    """

    output_path: Path
    exists: bool = False
    tiles: int = 0
    zooms: tuple[int, ...] = ()
    error: str | None = None


class ExistingTileScanWorker(QThread):
    """在后台线程里数一次已有瓦片，然后发信号退出。"""

    #: 扫描结束，携带 :class:`ExistingTileScan`
    scanned = Signal(object)

    def __init__(
        self,
        output_path: str | Path,
        zooms: list[int] | tuple[int, ...],
        parent: QObject | None = None,
    ) -> None:
        """
        Args:
            output_path: 目标 ``.mbtiles``；不存在也没关系。
            zooms: 要统计的缩放级别（通常即任务的 zoom 区间）。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._output_path = Path(output_path)
        self._zooms = tuple(sorted({int(zoom) for zoom in zooms}))
        #: 用户放弃本次下载时置位：结果照常发出去，但带上"已被放弃"的标记
        self._abandoned = False

    # ------------------------------------------------------------------ #
    # 只读状态
    # ------------------------------------------------------------------ #
    @property
    def output_path(self) -> Path:
        """被扫描的路径。"""
        return self._output_path

    @property
    def zooms(self) -> tuple[int, ...]:
        """参与统计的缩放级别。"""
        return self._zooms

    @property
    def abandoned(self) -> bool:
        """是否已被调用方放弃（用户点了取消）。"""
        return self._abandoned

    # ------------------------------------------------------------------ #
    # 控制
    # ------------------------------------------------------------------ #
    def abandon(self) -> None:
        """放弃本次扫描的结果。

        刻意**不**试图中断正在执行的 SQL 语句：``sqlite3`` 的查询默认不可中断
        （要做到得用 ``interrupt()`` + 另一个连接，代价远大于收益——计数查询
        最多几百毫秒）。这里只做一个标记，主线程收到信号后直接丢弃，
        用户感知到的是"立刻取消"。
        """
        self._abandoned = True

    # ------------------------------------------------------------------ #
    # 线程入口
    # ------------------------------------------------------------------ #
    def run(self) -> None:  # noqa: D102 - QThread 约定的入口
        exists = False
        tiles = 0
        error: str | None = None
        try:
            exists = self._output_path.is_file()
            if exists:
                tiles = count_existing_tiles(self._output_path, self._zooms)
        except Exception as exc:  # noqa: BLE001 - 绝不让异常冒泡进 Qt 事件循环
            # 扫描失败不该阻断下载：调用方会拿到 tiles=0，效果等于"当作没有已有
            # 瓦片"，最多多下几张（写库是 INSERT OR REPLACE，重复写是安全的）。
            error = f"扫描已有瓦片失败：{exc}"
        self.scanned.emit(
            ExistingTileScan(
                output_path=self._output_path,
                exists=exists,
                tiles=tiles,
                zooms=self._zooms,
                error=error,
            )
        )
