"""断点续传策略与已有瓦片索引（Slice 5，``app.downloader.resume``）。

覆盖 Spec §2 / §3 / §21 / §22：

- :class:`ResumePolicy` 的默认值与"关闭"语义；
- 两种存在性策略的**选择依据**（目标张数，而不是"已有多少"）；
- CACHED 模式：一次预加载、之后纯内存查表、**绝不读 BLOB**；
- QUERY 模式：逐张问 ``has_tile``，同样不读 BLOB；
- ``verify_existing_tile`` 打开后才允许读 BLOB，且损坏瓦片按策略"重下"或"沿用"；
- 传入的坐标是 **XYZ**（TMS 的翻转只发生在存储边界，见 §21）；
- ``describe_existing_tiles`` 的文案。

本模块用**假 probe**（内存里的 set）覆盖全部判定分支，因此不建任何 SQLite 文件；
真库上的行为由 ``test_resume_download.py`` 端到端覆盖。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.downloader import (
    ExistingTileIndex,
    ResumePolicy,
    SkipMode,
    TileCoordinate,
    describe_existing_tiles,
    make_skip_predicate,
)
from app.providers import PNG_SIGNATURE

#: 一张合法的"瓦片"（只要 magic bytes 对就算合法）
VALID_TILE = PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + b"payload" * 16

#: 一张损坏的"瓦片"（HTML 错误页）
BROKEN_TILE = b"<html><body>404</body></html>"


# --------------------------------------------------------------------------- #
# 假 probe
# --------------------------------------------------------------------------- #
class FakeProbe:
    """内存版的 :class:`~app.downloader.TileProbe`。

    刻意记录"到底问了什么"，这样测试才能断言
    "XYZ 进、XYZ 出"以及"默认策略下 BLOB 读取次数为 0"。
    """

    def __init__(
        self,
        existing: set[tuple[int, int, int]] | None = None,
        *,
        broken: set[tuple[int, int, int]] | None = None,
    ) -> None:
        self.existing = set(existing or ())
        self.broken = set(broken or ())
        #: 每次 ``has_tile`` 的入参（XYZ）
        self.has_tile_calls: list[tuple[int, int, int]] = []
        #: 每次 ``get_existing_tiles`` 的入参（zoom 或 None）
        self.list_calls: list[int | None] = []
        #: 每次 ``read_tile`` 的入参（XYZ）
        self.read_calls: list[tuple[int, int, int]] = []

    # --- TileProbe ---
    def has_tile(self, z: int, x: int, y_xyz: int) -> bool:
        self.has_tile_calls.append((z, x, y_xyz))
        return (z, x, y_xyz) in self.existing

    def read_tile(self, z: int, x: int, y_xyz: int) -> bytes | None:
        self.read_calls.append((z, x, y_xyz))
        key = (z, x, y_xyz)
        if key not in self.existing:
            return None
        return BROKEN_TILE if key in self.broken else VALID_TILE

    def get_existing_tiles(self, zoom: int | None = None) -> set[tuple[int, int, int]]:
        self.list_calls.append(zoom)
        if zoom is None:
            return set(self.existing)
        return {item for item in self.existing if item[0] == zoom}


def _tile(z: int = 10, x: int = 843, y: int = 388) -> TileCoordinate:
    return TileCoordinate(z=z, x=x, y=y)


# --------------------------------------------------------------------------- #
# ResumePolicy
# --------------------------------------------------------------------------- #
def test_policy_defaults_match_spec() -> None:
    """Spec §3：默认开启跳过、**不校验内容**、损坏则重下。"""
    policy = ResumePolicy()
    assert policy.enabled is True
    assert policy.verify_existing_tile is False
    assert policy.redownload_invalid_tile is True
    # 默认值必须来自 settings，不能在两个地方各写一份
    assert policy.verify_existing_tile == settings.DEFAULT_RESUME_VERIFY_EXISTING_TILE
    assert policy.redownload_invalid_tile == settings.DEFAULT_RESUME_REDOWNLOAD_INVALID_TILE


def test_policy_disabled_means_download_everything() -> None:
    policy = ResumePolicy.disabled()
    assert policy.enabled is False
    assert policy.skips_existing is False
    assert policy.reads_blobs is False


def test_policy_reads_blobs_only_when_verifying() -> None:
    assert ResumePolicy(enabled=True, verify_existing_tile=False).reads_blobs is False
    assert ResumePolicy(enabled=True, verify_existing_tile=True).reads_blobs is True
    # 关掉跳过时，"要不要校验"根本无从谈起
    assert ResumePolicy(enabled=False, verify_existing_tile=True).reads_blobs is False


def test_policy_is_frozen() -> None:
    """冻结的 dataclass：计划对象在 UI 层四处传递，不该被谁顺手改掉。"""
    policy = ResumePolicy()
    with pytest.raises(Exception):
        policy.enabled = False  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 策略选择（Spec §2）
# --------------------------------------------------------------------------- #
def test_small_task_uses_cached_mode() -> None:
    index = ExistingTileIndex(FakeProbe(), expected_total=1_000)
    assert index.mode is SkipMode.CACHED
    assert index.mode.display_name == "批量缓存"


def test_large_task_uses_query_mode() -> None:
    """超过阈值就走索引查询：几十万张的 ``set`` 会吃掉几百 MB 内存。"""
    threshold = settings.DEFAULT_RESUME_CACHE_MAX_TILES
    index = ExistingTileIndex(FakeProbe(), expected_total=threshold + 1)
    assert index.mode is SkipMode.QUERY
    assert index.mode.display_name == "索引查询"


def test_mode_switch_is_based_on_target_size_not_existing_count() -> None:
    """切换依据是**任务规模**：用一个已知量做决策，才不会"为了决定要不要查而先查一次"。"""
    probe = FakeProbe(existing={(0, 0, 0)})  # 库里只有 1 张
    big = ExistingTileIndex(probe, expected_total=10_000_000)
    assert big.mode is SkipMode.QUERY

    probe2 = FakeProbe(existing={_tile().as_tuple})  # 库里有 1 张，目标也是 1 张
    small = ExistingTileIndex(probe2, expected_total=1)
    assert small.mode is SkipMode.CACHED


def test_unknown_size_falls_back_to_query() -> None:
    """``expected_total=0`` = 规模未知 → 保守走查询，不突然吃掉几百 MB。"""
    assert ExistingTileIndex(FakeProbe(), expected_total=0).mode is SkipMode.QUERY


def test_disabled_policy_always_uses_query_but_never_skips() -> None:
    index = ExistingTileIndex(
        FakeProbe(existing={_tile().as_tuple}),
        ResumePolicy.disabled(),
        expected_total=10,
    )
    assert index.mode is SkipMode.QUERY  # 不做缓存 = 一根内存都不占
    assert index.should_skip(_tile()) is False
    assert index.stats.queried == 0  # 直接短路，连问都不问


def test_probe_must_implement_protocol() -> None:
    class NotAProbe:
        pass

    with pytest.raises(TypeError, match="TileProbe"):
        ExistingTileIndex(NotAProbe())  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# CACHED 模式
# --------------------------------------------------------------------------- #
def test_cached_mode_primes_once_and_never_reads_blob() -> None:
    """Spec §22：存在性判断只查坐标三列，一张 BLOB 都不能读。"""
    probe = FakeProbe(existing={(10, 843, 388), (10, 844, 388)})
    index = ExistingTileIndex(probe, expected_total=100, zooms=(10,))

    assert index.is_primed is False
    assert index.prime() == 2
    assert index.is_primed is True
    assert probe.list_calls == [10]

    # 幂等：再 prime 不会重复查库
    assert index.prime() == 2
    assert probe.list_calls == [10]

    # 判定纯内存：一次 SQL 都不需要
    assert index.should_skip(TileCoordinate(10, 843, 388)) is True
    assert index.should_skip(TileCoordinate(10, 999, 388)) is False
    assert probe.has_tile_calls == []
    assert probe.read_calls == []
    assert index.stats.blobs_read == 0
    assert index.stats.existing == 1
    assert index.stats.cached_hits == 2


def test_cached_mode_primes_lazily_on_first_query() -> None:
    """忘了显式 prime 也不会退化成"逐张查库"。"""
    probe = FakeProbe(existing={(10, 1, 1)})
    index = ExistingTileIndex(probe, expected_total=10, zooms=(10,))
    assert index.should_skip(TileCoordinate(10, 1, 1)) is True
    assert index.is_primed is True
    assert probe.has_tile_calls == []


def test_cached_mode_only_loads_requested_zooms() -> None:
    """z12 的小任务不该把库里 z0~z10 的坐标也读进来。"""
    probe = FakeProbe(existing={(0, 0, 0), (10, 1, 1), (12, 2, 2)})
    index = ExistingTileIndex(probe, expected_total=10, zooms=(12,))
    index.prime()
    assert probe.list_calls == [12]
    assert index.should_skip(TileCoordinate(12, 2, 2)) is True
    assert index.should_skip(TileCoordinate(0, 0, 0)) is False


def test_cached_mode_without_zooms_loads_everything() -> None:
    probe = FakeProbe(existing={(0, 0, 0), (12, 2, 2)})
    index = ExistingTileIndex(probe, expected_total=10)
    assert index.prime() == 2
    assert probe.list_calls == [None]


# --------------------------------------------------------------------------- #
# QUERY 模式
# --------------------------------------------------------------------------- #
def test_query_mode_asks_has_tile_per_tile() -> None:
    probe = FakeProbe(existing={(10, 843, 388)})
    index = ExistingTileIndex(probe, expected_total=1_000_000, zooms=(10,))

    assert index.prime() == 0  # QUERY 模式 prime 是空操作
    assert probe.list_calls == []

    assert index.should_skip(TileCoordinate(10, 843, 388)) is True
    assert index.should_skip(TileCoordinate(10, 843, 389)) is False
    # 入参必须是 XYZ —— TMS 的翻转只发生在存储边界
    assert probe.has_tile_calls == [(10, 843, 388), (10, 843, 389)]
    assert probe.read_calls == []
    assert index.stats.blobs_read == 0
    assert index.stats.queried == 2
    assert index.stats.existing == 1


# --------------------------------------------------------------------------- #
# verify_existing_tile（显式校验）
# --------------------------------------------------------------------------- #
def test_verify_reads_blob_and_redownloads_invalid_by_default() -> None:
    """开启校验后：合法瓦片沿用、损坏瓦片重下（默认）。"""
    good = (10, 1, 1)
    bad = (10, 2, 2)
    probe = FakeProbe(existing={good, bad}, broken={bad})
    index = ExistingTileIndex(
        probe,
        ResumePolicy(enabled=True, verify_existing_tile=True),
        expected_total=10,
        zooms=(10,),
    )

    assert index.should_skip(TileCoordinate(10, 1, 1)) is True
    assert index.should_skip(TileCoordinate(10, 2, 2)) is False  # 损坏 → 重下
    assert probe.read_calls == [good, bad]
    assert index.stats.blobs_read == 2
    assert index.stats.existing == 2
    assert index.stats.invalid == 1


def test_verify_keeps_invalid_when_redownload_disabled() -> None:
    """``redownload_invalid_tile=False`` → 损坏也沿用（离线环境下的"能用就行"）。"""
    bad = (10, 2, 2)
    probe = FakeProbe(existing={bad}, broken={bad})
    index = ExistingTileIndex(
        probe,
        ResumePolicy(
            enabled=True, verify_existing_tile=True, redownload_invalid_tile=False
        ),
        expected_total=10,
        zooms=(10,),
    )
    assert index.should_skip(TileCoordinate(10, 2, 2)) is True
    assert index.stats.invalid == 1
    assert index.stats.blobs_read == 1


def test_verify_treats_empty_blob_as_invalid() -> None:
    """空 BLOB 也是损坏（``read_tile`` 返回 ``None`` 或 ``b""``）。"""

    class EmptyProbe(FakeProbe):
        def read_tile(self, z: int, x: int, y_xyz: int) -> bytes | None:
            self.read_calls.append((z, x, y_xyz))
            return b"" if (z, x, y_xyz) in self.existing else None

    probe = EmptyProbe(existing={(10, 1, 1)})
    index = ExistingTileIndex(
        probe, ResumePolicy(enabled=True, verify_existing_tile=True), expected_total=10
    )
    assert index.should_skip(TileCoordinate(10, 1, 1)) is False
    assert index.stats.invalid == 1


# --------------------------------------------------------------------------- #
# 跳过回调
# --------------------------------------------------------------------------- #
def test_make_skip_predicate_is_sync_and_reusable() -> None:
    probe = FakeProbe(existing={(10, 1, 1)})
    index = ExistingTileIndex(probe, expected_total=10)
    predicate = make_skip_predicate(index)

    results = [predicate(TileCoordinate(10, 1, 1)), predicate(TileCoordinate(10, 9, 9))]
    assert results == [True, False]
    # 返回值必须是"同步 bool"：DownloadManager 会按普通返回值处理
    assert all(isinstance(item, bool) for item in results)


def test_stats_lines_are_loggable() -> None:
    index = ExistingTileIndex(FakeProbe(existing={(10, 1, 1)}), expected_total=10)
    index.should_skip(TileCoordinate(10, 1, 1))
    line = index.stats.as_lines()[0]
    assert "已有瓦片检查" in line
    assert "读取 BLOB 0 次" in line


# --------------------------------------------------------------------------- #
# 展示文案（Spec §16）
# --------------------------------------------------------------------------- #
def test_describe_existing_tiles_matches_spec_wording() -> None:
    lines = describe_existing_tiles(52_880, 34_201)
    assert lines == ["已有：34,201 / 52,880", "待下载：18,679"]


def test_describe_existing_tiles_clamps_negative_input() -> None:
    lines = describe_existing_tiles(-1, -5)
    assert lines == ["已有：0 / 0", "待下载：0"]


def test_describe_existing_tiles_reports_actual_skips_when_different() -> None:
    """本次跳过的张数与已有可能不同（校验模式下损坏的会被重下）。"""
    lines = describe_existing_tiles(100, 60, skipped_this_run=58)
    assert lines[-1] == "本次跳过：58"
    # 一致时就不重复啰嗦
    assert len(describe_existing_tiles(100, 60, skipped_this_run=60)) == 2


# --------------------------------------------------------------------------- #
# XYZ / TMS（Spec §21）
# --------------------------------------------------------------------------- #
def test_xyz_tms_flip_is_only_at_storage_boundary() -> None:
    """续传层全部用 XYZ；翻转只在 ``app.geo`` 里做一次。"""
    from app.geo import tms_y_to_xyz_y, xyz_y_to_tms_y

    # 锁定值：z=3, y_xyz=2 → tile_row=5
    assert xyz_y_to_tms_y(3, 2) == 5
    assert tms_y_to_xyz_y(3, 5) == 2
    # 往返一致
    for zoom in range(0, 6):
        for y in range(1 << zoom):
            assert tms_y_to_xyz_y(zoom, xyz_y_to_tms_y(zoom, y)) == y


def test_resume_module_does_not_import_mbtiles() -> None:
    """依赖方向护栏：``app.downloader`` 不能反向依赖 ``app.mbtiles``。"""
    source = Path(__file__).resolve().parents[1] / "app" / "downloader" / "resume.py"
    text = source.read_text(encoding="utf-8")
    assert "import app.mbtiles" not in text
    assert "from app.mbtiles" not in text
