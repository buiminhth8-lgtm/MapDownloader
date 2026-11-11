"""稳定读取视图（快照）与冻结逻辑（Slice 42）。

为什么不能"开一个 SQLite 连接"就当快照
--------------------------------------

仓库有多个分片文件，**每个文件是一个独立的数据库**。一个连接上的
``BEGIN`` 只能给**那一个文件**一个一致的视图 —— 用它冒充"全仓一致性"，会让导出
读到"Z10 是旧版、Z12 已是新版"的混合体，而这种错误不会报错，只会让交付物里
几块地对不上。

本模块的做法是**逻辑水位快照**：

1. 冻结时取 catalog 的全仓最高 epoch ``E``；
2. 逐分片读 ``state=current AND epoch_id <= min(published_epoch, E)`` 的坐标，
   把 ``(asset, z, x, y, sha256, size)`` 写进清单；
3. 之后所有读取（:class:`SnapshotView`）**按 sha256 取内容**，而不是"按坐标取当前
   版本" —— 于是冻结之后发生的替换、下架都不影响这份视图；
4. 内容被回收 → 抛 :class:`~app.tilerepo.errors.SnapshotExpired`，**不静默跳过**
   （跳过会产出"少了几张但报告说齐全"的包）。

指纹的算法（对契约 §1.5 的一处落地细化）
----------------------------------------

契约写的是 ``fingerprint = sha256(canonical(payload))``，其中 ``payload`` 含
``entries``。条目可能有几百万条，把它们全塞进内存算一次摘要，在真实规模上等于
把导出前的准备工作变成一次 OOM。因此落地为::

    entriesDigest = sha256(逐条 JSON 行的字节流)
    payload       = {version, epoch, selector, entriesDigest, entries}
    fingerprint   = sha256(canonical(payload))

语义不变（同一份数据两次冻结 → 同一个指纹），内存有界。``payload`` 里**没有**
``createdAt`` —— 有它的话同一份数据每次冻结都会得到新指纹，"可复现"就没了。

文件格式：一个 JSON 对象，但 ``entries`` 一行一条（便于流式读），形如::

    {"version":1,"epoch":12,"selector":{...},"entriesDigest":"…","entries":[
    {"a":"tianditu/vec_w","z":5,"x":1,"y":2,"s":"…","n":1234},
    {"a":"tianditu/vec_w","z":5,"x":1,"y":3,"s":"…","n":987}
    ]}

``entries`` 的 ``(asset, z, x, y)`` 唯一且有序（按 ``asset, z, x, y``）：分片带按
``z_min`` 升序、每个分片按主键 ``(z, x, y)`` 顺序扫，天然满足，不需要在内存里排序。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from app.geo import GeoBounds, calculate_tile_range
from app.tilerepo import shard as shard_module
from app.tilerepo.errors import Corrupt, NotFound, RepoClosed, SnapshotExpired
from app.tilerepo.identity import AssetId, ContentDigest, CoordKey, CoordState, TileRecord

if TYPE_CHECKING:  # pragma: no cover - 只为类型检查，运行时不导入（避免循环）
    from app.tilerepo.store import TileRepository

__all__ = ["SnapshotMeta", "SnapshotSelector", "SnapshotView", "freeze_snapshot", "load_meta"]

#: 快照文件格式版本（独立于 catalog/分片的 schema 版本）。
SNAPSHOT_VERSION: int = 1


@dataclass(frozen=True)
class SnapshotSelector:
    """冻结范围。``assets`` 为空 = 全部已登记资产。"""

    assets: tuple[str, ...] = ()
    zoom_range: tuple[int, int] | None = None
    bounds: tuple[float, float, float, float] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "assets": list(self.assets),
            "zoomRange": list(self.zoom_range) if self.zoom_range else None,
            "bounds": list(self.bounds) if self.bounds else None,
        }


@dataclass(frozen=True)
class SnapshotMeta:
    """一份冻结清单的元信息。"""

    fingerprint: str
    epoch: int
    entries: int
    created_at: float
    selector: dict[str, object] = field(default_factory=dict)
    path: Path | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "epoch": self.epoch,
            "entries": self.entries,
            "created_at": self.created_at,
            "selector": self.selector,
            "file": self.path.name if self.path is not None else "",
        }


# --------------------------------------------------------------------------- #
# 冻结
# --------------------------------------------------------------------------- #
def freeze_snapshot(
    repo: TileRepository,
    *,
    assets: Sequence[str | AssetId] | None = None,
    zoom_range: tuple[int, int] | None = None,
    bounds: GeoBounds | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> SnapshotMeta:
    """把当前可见状态冻结成一份清单文件（幂等：同数据 → 同指纹 → 不重写文件）。

    Args:
        repo: 仓库。
        assets: 参与冻结的资产；``None`` = 全部已登记资产。
        zoom_range: 闭区间；``None`` = 全部分片带。
        bounds: 地理范围；``None`` = 该级别全球。
        should_stop: 取消回调；**取消时不留下半份清单**（临时文件被删）。

    Returns:
        :class:`SnapshotMeta`。

    Raises:
        NotFound: 指定的资产未登记。
        RepoClosed: 仓库已关闭。
    """
    if repo.closed:
        raise RepoClosed("仓库已关闭，无法冻结快照")
    targets: list[AssetId] = []
    if assets is None:
        targets = [AssetId.parse(item) for item in repo.catalog.list_assets()]
    else:
        for raw in assets:
            parsed = repo.asset(raw)
            if not repo.catalog.asset_exists(parsed):
                raise NotFound(f"资产未登记：{parsed.text}")
            targets.append(parsed)

    low, high = _normalise_zoom(zoom_range, repo.bands)
    selector = SnapshotSelector(
        assets=tuple(item.text for item in targets),
        zoom_range=(low, high) if zoom_range is not None else None,
        bounds=(bounds.west, bounds.south, bounds.east, bounds.north) if bounds else None,
    )
    epoch = repo.catalog.max_epoch()
    ranges = _tile_ranges(zoom_range, bounds)

    body_path = repo.snapshots_dir / f".{uuid.uuid4().hex}.body.tmp"
    repo.snapshots_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    count = 0
    try:
        with open(body_path, "w", encoding="utf-8", newline="\n") as handle:
            first = True
            for asset in targets:
                for coord, sha, size in _iter_visible(repo, asset, low, high, epoch, ranges):
                    if should_stop is not None and should_stop():
                        raise _FreezeCancelled
                    line = json.dumps(
                        {
                            "a": asset.text,
                            "z": coord.z,
                            "x": coord.x,
                            "y": coord.y,
                            "s": sha.hex(),
                            "n": size,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if not first:
                        handle.write(",\n")
                    handle.write(line)
                    digest.update(line.encode("utf-8"))
                    first = False
                    count += 1
        entries_digest = digest.hexdigest()
    except _FreezeCancelled:
        body_path.unlink(missing_ok=True)
        raise
    except BaseException:
        body_path.unlink(missing_ok=True)
        raise

    payload = {
        "version": SNAPSHOT_VERSION,
        "epoch": epoch,
        "selector": selector.as_dict(),
        "entriesDigest": entries_digest,
        "entryCount": count,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    final_path = repo.snapshots_dir / f"{fingerprint}.json"

    if final_path.is_file():
        body_path.unlink(missing_ok=True)
        meta = load_meta(repo, fingerprint)
    else:
        meta = _write_snapshot(
            repo, final_path, body_path, payload, fingerprint, selector, epoch, count
        )
        body_path.unlink(missing_ok=True)

    repo.catalog.register_snapshot(
        fingerprint,
        epoch,
        final_path.relative_to(repo.root).as_posix(),
        count,
        {"selector": selector.as_dict()},
    )
    return meta


class _FreezeCancelled(Exception):
    """内部信号：冻结被取消（不对外暴露，取消语义由调用方自己判断）。"""


def _normalise_zoom(
    zoom_range: tuple[int, int] | None, bands: tuple[tuple[int, int], ...]
) -> tuple[int, int]:
    if zoom_range is None:
        return (min(item[0] for item in bands), max(item[1] for item in bands))
    low, high = int(zoom_range[0]), int(zoom_range[1])
    if low > high:
        raise ValueError(f"zoom_range 起点不能大于终点：{zoom_range}")
    return (low, high)


def _tile_ranges(
    zoom_range: tuple[int, int] | None, bounds: GeoBounds | None
) -> dict[int, tuple[int, int, int, int]] | None:
    """``{z: (x_min, x_max, y_min, y_max)}``；``bounds`` 为空时返回 ``None``（不过滤）。"""
    if bounds is None or zoom_range is None:
        return None
    result: dict[int, tuple[int, int, int, int]] = {}
    for zoom in range(int(zoom_range[0]), int(zoom_range[1]) + 1):
        tile_range = calculate_tile_range(bounds, zoom)
        result[zoom] = (
            tile_range.min_x,
            tile_range.max_x,
            tile_range.min_y,
            tile_range.max_y,
        )
    return result


def _iter_visible(
    repo: TileRepository,
    asset: AssetId,
    low: int,
    high: int,
    epoch: int,
    ranges: dict[int, tuple[int, int, int, int]] | None,
) -> Iterator[tuple[CoordKey, bytes, int]]:
    """逐分片按主键顺序产出可见坐标（不载入整库）。

    可见 = ``state=current`` 且 ``epoch_id <= min(该分片发布水位, 冻结水位 E)``。
    两个水位取小是刻意的：发布水位挡住"已提交未发布"，E 挡住"冻结之后才发布"。
    """
    for band in repo.bands:
        if band[1] < low or band[0] > high:
            continue
        path = repo.shard_path(asset, band)
        if not path.is_file():
            continue
        watermark = repo.catalog.published_epoch(asset, band)
        cap = min(watermark, epoch)
        if cap <= 0:
            continue
        connection = shard_module.open_shard(path, read_only=True)
        try:
            cursor = connection.execute(
                "SELECT c.z, c.x, c.y, c.sha256, n.size FROM coords c "
                "JOIN contents n ON n.sha256 = c.sha256 "
                "WHERE c.z BETWEEN ? AND ? AND c.state = ? AND c.epoch_id <= ? "
                "ORDER BY c.z, c.x, c.y",
                (max(low, band[0]), min(high, band[1]), CoordState.CURRENT.value, cap),
            )
            while True:
                rows = cursor.fetchmany(2000)
                if not rows:
                    break
                for row in rows:
                    coord = CoordKey(int(row["z"]), int(row["x"]), int(row["y"]))
                    if ranges is not None:
                        allowed = ranges.get(coord.z)
                        if allowed is None:
                            continue
                        if not (
                            allowed[0] <= coord.x <= allowed[1]
                            and allowed[2] <= coord.y <= allowed[3]
                        ):
                            continue
                    yield coord, bytes(row["sha256"]), int(row["size"])
        finally:
            connection.close()


def _write_snapshot(
    repo: TileRepository,
    final_path: Path,
    body_path: Path,
    payload: dict[str, object],
    fingerprint: str,
    selector: SnapshotSelector,
    epoch: int,
    count: int,
) -> SnapshotMeta:
    """组装并原子发布快照文件（``.tmp`` + ``os.replace``）。"""
    temp_path = final_path.with_name(f"{final_path.name}.{uuid.uuid4().hex}.tmp")
    header = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    try:
        with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(header[:-1])  # 去掉结尾的 '}'，把 entries 数组接上去
            handle.write(',"entries":[\n')
            with open(body_path, encoding="utf-8") as body:
                while True:
                    chunk = body.read(1 << 20)
                    if not chunk:
                        break
                    handle.write(chunk)
            handle.write("\n]}\n")
        os.replace(temp_path, final_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return SnapshotMeta(
        fingerprint=fingerprint,
        epoch=epoch,
        entries=count,
        created_at=time.time(),
        selector=selector.as_dict(),
        path=final_path,
    )


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def load_meta(repo: TileRepository, fingerprint: str) -> SnapshotMeta:
    """读一份快照的元信息（不载入 entries）。

    Raises:
        NotFound: 文件或记录不存在。
        Corrupt: 文件结构不符合本模块写出的格式。
    """
    record = repo.catalog.get_snapshot(fingerprint)
    path = repo.snapshots_dir / f"{fingerprint}.json"
    if not path.is_file():
        if record is not None:
            raise NotFound(f"快照登记存在但文件缺失：{fingerprint[:12]}…")
        raise NotFound(f"快照不存在：{fingerprint[:12]}…")
    header = _read_header(path)
    if header.get("version") != SNAPSHOT_VERSION:
        raise Corrupt(f"快照版本不支持：{header.get('version')!r}")
    selector = header.get("selector") if isinstance(header.get("selector"), dict) else {}
    return SnapshotMeta(
        fingerprint=fingerprint,
        epoch=int(header.get("epoch") or 0),
        entries=int(header.get("entryCount") or 0),
        created_at=float(record["created_at"]) if record else path.stat().st_mtime,
        selector=selector,
        path=path,
    )


def _read_header(path: Path) -> dict[str, object]:
    """读第一行（头），并粗验文件结构。

    头一行的形态是 ``{...payload...}`` 去掉结尾 ``}`` 再拼上 ``,"entries":[``
    （见 :func:`_write_snapshot`）。这里按那个拼接点切回去 —— 比"整份文件
    ``json.load`` 一遍"省几百 MB 内存，代价是格式必须由本模块独占写入。
    """
    try:
        with open(path, encoding="utf-8") as handle:
            first = handle.readline()
            rest = handle.read()
    except OSError as exc:
        raise Corrupt(f"快照文件无法读取：{path.name}（{exc}）") from exc
    if not first.startswith("{"):
        raise Corrupt(f"快照文件头不合法：{path.name}")
    if not rest.rstrip().endswith("]}"):
        raise Corrupt(f"快照文件尾不合法（可能被截断）：{path.name}")
    head, _, _ = first.rpartition(',"entries":[')
    try:
        header = json.loads(f"{head}}}")
    except json.JSONDecodeError as exc:
        raise Corrupt(f"快照文件头无法解析：{path.name}") from exc
    if not isinstance(header, dict):
        raise Corrupt(f"快照文件头不是对象：{path.name}")
    return header


class SnapshotView:
    """一份冻结清单上的**稳定读取视图**。

    用法::

        with repo.open_snapshot(meta.fingerprint) as view:
            for record in view:
                ...  # record.data 是冻结那一刻的那一份字节

    一致性保证：即使别的进程在这期间发布了新版本、甚至把当前版本替换掉，
    本视图读到的仍是清单里记录的 ``sha256``；对应的内容若已被回收，
    读到这里会抛 :class:`~app.tilerepo.errors.SnapshotExpired`。
    """

    def __init__(self, repo: TileRepository, meta: SnapshotMeta) -> None:
        self._repo = repo
        self.meta = meta
        self._closed = False
        self._connections: dict[str, object] = {}

    @classmethod
    def open(cls, repo: TileRepository, fingerprint: str) -> SnapshotView:
        return cls(repo, load_meta(repo, fingerprint))

    # -- 生命周期 ------------------------------------------------------ #
    def close(self) -> None:
        """释放视图持有的分片连接。幂等。"""
        for connection in self._connections.values():
            # 关闭失败不该掩盖调用方的异常（收尾路径）
            with contextlib.suppress(Exception):
                connection.close()  # type: ignore[attr-defined]
        self._connections.clear()
        self._closed = True

    def __enter__(self) -> SnapshotView:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- 读取 ---------------------------------------------------------- #
    @property
    def entry_count(self) -> int:
        return self.meta.entries

    @property
    def fingerprint(self) -> str:
        return self.meta.fingerprint

    def __iter__(self) -> Iterator[TileRecord]:
        if self._closed:
            raise RepoClosed("快照视图已关闭")
        path = self.meta.path
        if path is None or not path.is_file():
            raise NotFound(f"快照文件不存在：{self.meta.fingerprint[:12]}…")
        for entry in _iter_entries(path):
            record = self._read_entry(entry)
            yield record

    def _read_entry(self, entry: dict[str, object]) -> TileRecord:
        asset = AssetId.parse(str(entry["a"]))
        coord = CoordKey(int(entry["z"]), int(entry["x"]), int(entry["y"]))
        sha = bytes.fromhex(str(entry["s"]))
        band = self._repo.band_for(coord.z)
        connection = self._connection(asset, band)
        row = connection.execute(
            "SELECT size, encoding, data FROM contents WHERE sha256 = ?", (sha,)
        ).fetchone()
        if row is None:
            raise SnapshotExpired(
                f"快照引用的内容已被回收：{asset.text} z{coord.z}/{coord.x}/{coord.y}"
                f"（{sha.hex()[:16]}…）",
                asset=asset.text,
                coord=[coord.z, coord.x, coord.y],
            )
        return TileRecord(
            asset=asset,
            coord=coord,
            digest=ContentDigest(sha, int(row["size"]), str(row["encoding"])),
            epoch=self.meta.epoch,
            state=CoordState.CURRENT,
            fetched_at=0.0,
            data=bytes(row["data"]),
        )

    def _connection(self, asset: AssetId, band: tuple[int, int]):
        """按 ``(资产, 分片带)`` 缓存只读连接（同一资产的不同 band 是不同文件）。"""
        key = f"{asset.text}|{band[0]}"
        cached = self._connections.get(key)
        if cached is not None:
            return cached
        path = self._repo.shard_path(asset, band)
        if not path.is_file():
            raise SnapshotExpired(
                f"快照引用的分片文件缺失：{path.name}（{asset.text}）",
                asset=asset.text,
            )
        connection = shard_module.open_shard(path, read_only=True)
        self._connections[key] = connection
        return connection


def _iter_entries(path: Path) -> Iterator[dict[str, object]]:
    """流式解析 ``entries``（一行一条），不把整份清单读进内存。"""
    with open(path, encoding="utf-8") as handle:
        header = handle.readline()
        if not header.startswith("{"):
            raise Corrupt(f"快照文件头不合法：{path.name}")
        for line in handle:
            text = line.strip()
            if not text or text in ("]}", "]"):
                continue
            if text.endswith("]"):
                text = text[:-1].strip()
            text = text.rstrip(",").strip()
            if not text:
                continue
            try:
                entry = json.loads(text)
            except json.JSONDecodeError as exc:
                raise Corrupt(f"快照条目无法解析：{path.name}") from exc
            if not isinstance(entry, dict):
                raise Corrupt(f"快照条目不是对象：{path.name}")
            yield entry


def snapshot_files(repo: TileRepository) -> list[Path]:
    """仓库里现存的快照文件（健康检查用）。"""
    base = repo.snapshots_dir
    if not base.is_dir():
        return []
    return sorted(path for path in base.glob("*.json") if path.is_file())
