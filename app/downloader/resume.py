"""断点续传策略与"已有瓦片索引"（Slice 5）。

本模块回答一个很简单的问题：

    这张瓦片，这次还要不要下？

    已有且有效  → 跳过（计入 skipped）
    不存在      → 下载
    存在但损坏  → 看策略：重下（默认）或沿用

它**不碰** SQLite，也不 import ``app.mbtiles``：对"库里有哪张瓦片"的访问
通过 :class:`TileProbe` 这个结构化协议注入。这样做的收益有两个：

1. 依赖方向保持干净——``app.mbtiles`` 与 ``app.downloader`` 之间不会互相
   import（跨包环状依赖在 Python 里不报错，但会让人完全没法推理初始化顺序）。
2. 本模块可以用一个**假 probe**（内存里的 set）被完整单测覆盖，
   不需要真的建库；而真正的库由集成测试覆盖。

两种存在性策略（Spec §2 要求）
-----------------------------

- **小任务** → :attr:`SkipMode.CACHED`：一次性把已有坐标读成 ``set``，
  之后每张瓦片只是内存查表（O(1)，无 SQL 往返）。
- **大任务** → :attr:`SkipMode.QUERY`：不预加载，每张瓦片问一次
  ``SELECT 1 ... LIMIT 1``（走主键唯一索引）。

切换依据是**任务规模**而不是"已有多少"：任务目标瓦片数已知（由 Tile Math
算出），而"已有多少"要先查一遍才知道——用一个已知的量做决策，才不会为了
决定"要不要查"而先查一次。

两者都**绝不读 ``tile_data``**（Spec §22）：存在性判断只认坐标三列。
只有显式打开 ``verify_existing_tile`` 时才会读 BLOB，且读取次数被记在
:attr:`ExistingTileStats.blobs_read` 上，测试可以据此断言默认策略一张都没读。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.config.settings import (
    DEFAULT_RESUME_CACHE_MAX_TILES,
    DEFAULT_RESUME_REDOWNLOAD_INVALID_TILE,
    DEFAULT_RESUME_VERIFY_EXISTING_TILE,
)
from app.downloader.models import SkipPredicate, TileCoordinate

# 复用 Slice 2 的 magic bytes 判定：判断"这是不是一张合法瓦片"的规则
# 必须全链路一致，否则会出现"下载时判合法、续传校验时判非法"的诡异现象。
from app.providers import is_valid_tile_response

__all__ = [
    "ResumePolicy",
    "SkipMode",
    "TileProbe",
    "ExistingTileStats",
    "ExistingTileIndex",
    "describe_existing_tiles",
    "make_skip_predicate",
]


# --------------------------------------------------------------------------- #
# 策略
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ResumePolicy:
    """断点续传策略（Spec §3）。

    Attributes:
        enabled: ``False`` 表示完全不做跳过——每张瓦片都重新下载。
            用于"我怀疑这个库是坏的，全部重来"的场景。
        verify_existing_tile: 命中已有瓦片时，是否连内容一起校验
            （读 BLOB + 判 magic bytes）。**默认关闭**：开启后每张已有瓦片
            都要读一次 BLOB，恢复一个几十万张的库会变成分钟级磁盘 IO。
        redownload_invalid_tile: 仅在 ``verify_existing_tile=True`` 时有意义。
            校验发现内容为空/损坏时，``True`` 重新下载覆盖，``False`` 沿用旧数据。
    """

    enabled: bool = True
    verify_existing_tile: bool = DEFAULT_RESUME_VERIFY_EXISTING_TILE
    redownload_invalid_tile: bool = DEFAULT_RESUME_REDOWNLOAD_INVALID_TILE

    @classmethod
    def disabled(cls) -> "ResumePolicy":
        """返回"不做任何跳过"的策略（全量重下）。"""
        return cls(enabled=False)

    @property
    def skips_existing(self) -> bool:
        """是否会跳过已有瓦片。"""
        return self.enabled

    @property
    def reads_blobs(self) -> bool:
        """是否会为了校验而读 BLOB。"""
        return self.enabled and self.verify_existing_tile


# --------------------------------------------------------------------------- #
# 存在性探测协议
# --------------------------------------------------------------------------- #
@runtime_checkable
class TileProbe(Protocol):
    """"这个库里已有哪张瓦片"的最小接口。

    ``MBTilesReader`` / ``MBTilesWriter`` 都天然满足它（结构类型，不需要继承），
    测试里则可以用几行的假对象代替，不必真的建一个 SQLite 文件。
    """

    def has_tile(self, z: int, x: int, y_xyz: int) -> bool:
        """某张瓦片是否在库中（入参 XYZ）。"""
        ...  # pragma: no cover - 协议声明

    def read_tile(self, z: int, x: int, y_xyz: int) -> bytes | None:
        """读取某张瓦片的字节；不存在返回 ``None``（入参 XYZ）。"""
        ...  # pragma: no cover - 协议声明

    def get_existing_tiles(
        self, zoom: int | None = None
    ) -> set[tuple[int, int, int]]:
        """列出已有瓦片坐标 ``(z, x, y_xyz)``（入参/返回值均为 XYZ）。"""
        ...  # pragma: no cover - 协议声明


class SkipMode(Enum):
    """存在性判断的实现方式。"""

    #: 预加载全部已有坐标为 ``set``（小任务）
    CACHED = "cached"
    #: 每张瓦片走 SQL 索引查询（大任务）
    QUERY = "query"

    @property
    def display_name(self) -> str:
        """中文展示名（写日志用）。"""
        return "批量缓存" if self is SkipMode.CACHED else "索引查询"


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
@dataclass
class ExistingTileStats:
    """已有瓦片判断的运行时统计（只增不减，任务结束后可读）。"""

    #: 被问过"存在吗"的次数
    queried: int = 0
    #: 命中本地缓存（无需 SQL）的次数
    cached_hits: int = 0
    #: 确认存在（可跳过）的瓦片数
    existing: int = 0
    #: 存在但内容为空/损坏的瓦片数
    invalid: int = 0
    #: 实际读取 BLOB 的次数。默认策略（verify_existing_tile=False）必须恒为 0。
    blobs_read: int = 0

    def as_lines(self) -> list[str]:
        """压成一行日志。"""
        return [
            f"已有瓦片检查：询问 {self.queried:,} 次，命中缓存 {self.cached_hits:,} 次，"
            f"确认已有 {self.existing:,} 张，损坏 {self.invalid:,} 张，"
            f"读取 BLOB {self.blobs_read:,} 次"
        ]


# --------------------------------------------------------------------------- #
# 索引
# --------------------------------------------------------------------------- #
class ExistingTileIndex:
    """把 :class:`TileProbe` + :class:`ResumePolicy` 合成一个"要不要跳过"的判定器。

    用法::

        index = ExistingTileIndex(writer, policy, expected_total=total, zooms=(9, 10))
        index.prime()                       # CACHED 模式下预加载（QUERY 模式为空操作）
        if index.should_skip(tile):         # 判定
            ...                             # 不计入 HTTP worker
    """

    def __init__(
        self,
        probe: TileProbe,
        policy: ResumePolicy | None = None,
        *,
        expected_total: int = 0,
        zooms: Iterable[int] | None = None,
        cache_max_tiles: int = DEFAULT_RESUME_CACHE_MAX_TILES,
    ) -> None:
        """
        Args:
            probe: 存在性探测源（``MBTilesReader`` / ``MBTilesWriter`` 或测试替身）。
            policy: 续传策略，``None`` 用 :class:`ResumePolicy` 默认值。
            expected_total: 任务目标瓦片数（由 Tile Math 得出）。用于选策略：
                ``<= cache_max_tiles`` 走批量缓存，否则走索引查询。
                ``0`` 视为"规模未知" → 走索引查询（保守，不会突然吃掉几百 MB 内存）。
            zooms: 本任务覆盖的缩放级别。CACHED 模式下只预加载这些级别的坐标，
                避免为了一个 z12 的小任务把库里 z0~z10 的坐标也一并读进来。
            cache_max_tiles: 切换策略的阈值。

        Raises:
            TypeError: ``probe`` 不具备 :class:`TileProbe` 的接口。
        """
        if not isinstance(probe, TileProbe):
            missing = [
                name
                for name in ("has_tile", "read_tile", "get_existing_tiles")
                if not hasattr(probe, name)
            ]
            raise TypeError(
                f"probe 必须实现 TileProbe（缺 {missing}），"
                f"实际为 {type(probe).__name__}"
            )

        self._probe = probe
        self._policy = policy or ResumePolicy()
        self._zooms = tuple(int(z) for z in zooms) if zooms is not None else None
        self._cache_max_tiles = int(cache_max_tiles)
        self._expected_total = max(0, int(expected_total))

        self._cache: set[tuple[int, int, int]] | None = None
        self._cache_loaded = False
        self.stats = ExistingTileStats()

        self._mode = (
            SkipMode.CACHED
            if self._policy.enabled and 0 < self._expected_total <= self._cache_max_tiles
            else SkipMode.QUERY
        )

    # ------------------------------------------------------------------ #
    # 只读状态
    # ------------------------------------------------------------------ #
    @property
    def policy(self) -> ResumePolicy:
        """当前策略。"""
        return self._policy

    @property
    def mode(self) -> SkipMode:
        """存在性判断当前走哪条路（批量缓存 / 索引查询）。"""
        return self._mode

    @property
    def is_primed(self) -> bool:
        """CACHED 模式的缓存是否已加载（QUERY 模式恒为 ``False``）。"""
        return self._cache_loaded

    # ------------------------------------------------------------------ #
    # 预加载
    # ------------------------------------------------------------------ #
    def prime(self) -> int:
        """预加载已有坐标（CACHED 模式），返回缓存里的瓦片数。

        QUERY 模式或策略关闭时是空操作，返回 ``0``。
        重复调用不会重新查库（幂等）。
        """
        if self._mode is not SkipMode.CACHED:
            return 0
        if self._cache_loaded:
            return len(self._cache or ())
        self._cache = self._load_cache()
        self._cache_loaded = True
        return len(self._cache)

    def _load_cache(self) -> set[tuple[int, int, int]]:
        """按 zoom 分批读已有坐标，再并成一个集合。"""
        if self._zooms:
            merged: set[tuple[int, int, int]] = set()
            for zoom in self._zooms:
                merged |= self._probe.get_existing_tiles(zoom)
            return merged
        return self._probe.get_existing_tiles()

    # ------------------------------------------------------------------ #
    # 判定
    # ------------------------------------------------------------------ #
    def exists(self, tile: TileCoordinate) -> bool:
        """库里是否已有这张瓦片（**不看策略、不做校验**）。"""
        cached = self._ensure_cache()
        if cached is not None:
            self.stats.cached_hits += 1
            return tile.as_tuple in cached
        return self._probe.has_tile(tile.z, tile.x, tile.y)

    def should_skip(self, tile: TileCoordinate) -> bool:
        """本次是否应跳过这张瓦片（策略 + 校验的完整判定）。

        Returns:
            ``True`` 表示"已有且可用，不用下载"。
        """
        policy = self._policy
        if not policy.enabled:
            return False

        self.stats.queried += 1
        if not self.exists(tile):
            return False

        self.stats.existing += 1
        if not policy.verify_existing_tile:
            return True

        # 显式要求校验：这里才允许读 BLOB
        self.stats.blobs_read += 1
        data = self._probe.read_tile(tile.z, tile.x, tile.y)
        if data and is_valid_tile_response(data):
            return True

        self.stats.invalid += 1
        # 损坏瓦片：默认重新下载；策略说不重下时就沿用旧数据。
        return not policy.redownload_invalid_tile

    def _ensure_cache(self) -> set[tuple[int, int, int]] | None:
        """CACHED 模式下保证缓存已加载（首次访问时惰性触发）。"""
        if self._mode is not SkipMode.CACHED:
            return None
        if not self._cache_loaded:
            self.prime()
        return self._cache


# --------------------------------------------------------------------------- #
# 便捷工厂
# --------------------------------------------------------------------------- #
def make_skip_predicate(index: ExistingTileIndex) -> SkipPredicate:
    """把索引包成 :class:`~app.downloader.DownloadManager` 认的回调。

    ``DownloadManager.download(should_skip_tile=...)`` 接受同步或协程函数，
    这里给的是同步版本（判定本身是纯内存/SQL 查询，没有 await 的必要）。
    """

    def predicate(tile: TileCoordinate) -> bool:
        return index.should_skip(tile)

    return predicate


def describe_existing_tiles(
    total: int,
    existing: int,
    *,
    skipped_this_run: int | None = None,
) -> list[str]:
    """生成恢复任务时的展示文案（Spec §16）。

    Args:
        total: 任务目标瓦片数。
        existing: 开始前检测到的已有瓦片数。
        skipped_this_run: 本次实际跳过的张数；``None`` 时用 ``existing``。

    Returns:
        形如 ``["已有：32,418 / 52,880", "待下载：20,462"]`` 的多行文案。
    """
    total = max(0, int(total))
    existing = max(0, int(existing))
    skipped = existing if skipped_this_run is None else max(0, int(skipped_this_run))
    pending = max(0, total - existing)
    lines = [
        f"已有：{existing:,} / {total:,}",
        f"待下载：{pending:,}",
    ]
    if skipped_this_run is not None and skipped != existing:
        lines.append(f"本次跳过：{skipped:,}")
    return lines
