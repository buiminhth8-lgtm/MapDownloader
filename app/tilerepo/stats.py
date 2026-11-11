"""仓库容量与状态统计（Slice 42）。

口径必须先说清楚，否则数字会被误用
----------------------------------

=====================  ============================================  ==========
指标                    含义                                          能回答什么
=====================  ============================================  ==========
``current``            可见（已发布、``state=current``）的坐标数         "仓库里有多少张瓦片"
``superseded``         历史版本行数（被替换掉的旧版本）                  "回滚窗口有多大"
``quarantined``        被判坏、等待修复的坐标数                          "有多少要重下"
``contents``           内容实体数（**去重后**）                          "独立图片有多少份"
``logical_bytes``      可见坐标引用到的图片字节**之和**（重复内容算多次）  "如果不去重，需要多少字节"
``dedup_bytes``        ``sum(contents.size)``（每份内容只算一次）        "图片真正占多少字节"
``database_bytes``     分片 ``.sqlite`` 文件 + WAL + SHM 的实际占用      "磁盘上被仓库吃了多少"
=====================  ============================================  ==========

三条不能混用的边界：

1. **``logical_bytes`` 不是物理占用**。一张空白瓦片被 1000 个坐标引用时，
   ``logical_bytes`` 是 1000 份，磁盘上只有 1 份。
2. **去重范围是"单个分片内"**，不是全仓（契约 §2.1 / 架构 §7.5）。因此
   ``dedup_bytes`` 只在不同分片之间**不**去重；把它的总和说成"全仓去重后大小"
   是对的，但"跨图层共享了几 MB"这种结论是错的。
3. 本模块只**报告**，不保证任何配额。严格预算（超限要暂停、要报"不可行"）
   是 Slice 46 的事；这里给的是事实，不是承诺。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.tilerepo import shard as shard_module
from app.tilerepo.identity import AssetId

if TYPE_CHECKING:  # pragma: no cover
    from app.tilerepo.store import TileRepository

__all__ = ["AssetStats", "RepoStats", "ZoomStats", "collect"]


@dataclass(frozen=True)
class ZoomStats:
    """单个 zoom 的行数分布。"""

    zoom: int
    current: int = 0
    superseded: int = 0
    quarantined: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "zoom": self.zoom,
            "current": self.current,
            "superseded": self.superseded,
            "quarantined": self.quarantined,
        }


@dataclass(frozen=True)
class AssetStats:
    """单个资产的统计。"""

    asset: str
    zooms: tuple[ZoomStats, ...] = ()
    contents: int = 0
    logical_bytes: int = 0
    dedup_bytes: int = 0
    shard_files: int = 0
    shard_bytes: int = 0
    wal_bytes: int = 0
    shm_bytes: int = 0

    @property
    def current(self) -> int:
        return sum(item.current for item in self.zooms)

    @property
    def superseded(self) -> int:
        return sum(item.superseded for item in self.zooms)

    @property
    def quarantined(self) -> int:
        return sum(item.quarantined for item in self.zooms)

    @property
    def physical_bytes(self) -> int:
        return self.shard_bytes + self.wal_bytes + self.shm_bytes

    def as_dict(self) -> dict[str, object]:
        return {
            "asset": self.asset,
            "current": self.current,
            "superseded": self.superseded,
            "quarantined": self.quarantined,
            "contents": self.contents,
            "logical_bytes": self.logical_bytes,
            "dedup_bytes": self.dedup_bytes,
            "shard_files": self.shard_files,
            "shard_bytes": self.shard_bytes,
            "wal_bytes": self.wal_bytes,
            "shm_bytes": self.shm_bytes,
            "physical_bytes": self.physical_bytes,
            "zooms": [item.as_dict() for item in self.zooms],
        }


@dataclass(frozen=True)
class RepoStats:
    """整个仓库的统计（含 catalog 与快照文件）。"""

    assets: tuple[AssetStats, ...] = ()
    catalog_bytes: int = 0
    catalog_wal_bytes: int = 0
    snapshot_bytes: int = 0
    staging_bytes: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def current(self) -> int:
        return sum(item.current for item in self.assets)

    @property
    def contents(self) -> int:
        return sum(item.contents for item in self.assets)

    @property
    def logical_bytes(self) -> int:
        return sum(item.logical_bytes for item in self.assets)

    @property
    def dedup_bytes(self) -> int:
        return sum(item.dedup_bytes for item in self.assets)

    @property
    def physical_bytes(self) -> int:
        return (
            sum(item.physical_bytes for item in self.assets)
            + self.catalog_bytes
            + self.catalog_wal_bytes
            + self.snapshot_bytes
            + self.staging_bytes
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "current": self.current,
            "contents": self.contents,
            "logical_bytes": self.logical_bytes,
            "dedup_bytes": self.dedup_bytes,
            "catalog_bytes": self.catalog_bytes,
            "catalog_wal_bytes": self.catalog_wal_bytes,
            "snapshot_bytes": self.snapshot_bytes,
            "staging_bytes": self.staging_bytes,
            "physical_bytes": self.physical_bytes,
            "assets": [item.as_dict() for item in self.assets],
            "notes": list(self.notes),
        }

    def as_lines(self) -> list[str]:
        from app.geo import format_size

        lines = [
            f"可见坐标：{self.current:,} 张；内容实体：{self.contents:,} 份",
            f"逻辑图片字节：{format_size(self.logical_bytes)}；"
            f"去重后内容字节：{format_size(self.dedup_bytes)}",
            f"物理占用：{format_size(self.physical_bytes)}"
            f"（分片 {format_size(sum(a.physical_bytes for a in self.assets))} / "
            f"catalog {format_size(self.catalog_bytes + self.catalog_wal_bytes)} / "
            f"快照 {format_size(self.snapshot_bytes)} / 暂存 {format_size(self.staging_bytes)}）",
        ]
        for asset in self.assets:
            lines.append(
                f"  {asset.asset}：可见 {asset.current:,}，历史 {asset.superseded:,}，"
                f"隔离 {asset.quarantined:,}，内容 {asset.contents:,}"
            )
        lines.extend(f"注：{note}" for note in self.notes)
        return lines


def collect(repo: TileRepository, asset: str | AssetId | None = None) -> RepoStats:
    """汇总仓库统计（只读，幂等）。

    只统计**可见**（``epoch_id <= published_epoch``）的坐标行；未发布的行会单独
    在 ``notes`` 里提示一句 —— 它们占着磁盘却不该被算成资产。
    """
    targets = (
        [repo.asset(asset)] if asset is not None else [AssetId.parse(item) for item in repo.catalog.list_assets()]
    )
    collected: list[AssetStats] = []
    notes: list[str] = []
    for target in targets:
        collected.append(_asset_stats(repo, target, notes))

    catalog_path = repo.root / "catalog.sqlite"
    snapshot_bytes = _folder_bytes(repo.snapshots_dir)
    staging_bytes = _folder_bytes(repo.staging_dir)
    return RepoStats(
        assets=tuple(collected),
        catalog_bytes=_file_size(catalog_path),
        catalog_wal_bytes=_file_size(catalog_path.with_name(catalog_path.name + "-wal")),
        snapshot_bytes=snapshot_bytes,
        staging_bytes=staging_bytes,
        notes=tuple(notes),
    )


def _asset_stats(repo: TileRepository, asset: AssetId, notes: list[str]) -> AssetStats:
    zooms: dict[int, dict[str, int]] = {}
    contents = logical = dedup = 0
    shard_files = shard_bytes = wal_bytes = shm_bytes = 0
    unpublished = 0
    for band in repo.bands:
        path = repo.shard_path(asset, band)
        if not path.is_file():
            continue
        shard_files += 1
        shard_bytes += _file_size(path)
        wal_bytes += _file_size(path.with_name(path.name + "-wal"))
        shm_bytes += _file_size(path.with_name(path.name + "-shm"))
        watermark = repo.catalog.published_epoch(asset, band)
        connection = shard_module.open_shard(path, read_only=True)
        try:
            rows = connection.execute(
                "SELECT z, "
                "SUM(CASE WHEN state = 0 AND epoch_id <= ? THEN 1 ELSE 0 END) AS visible, "
                "SUM(CASE WHEN state = 0 AND epoch_id >  ? THEN 1 ELSE 0 END) AS unpublished, "
                "SUM(CASE WHEN state = 1 THEN 1 ELSE 0 END) AS superseded, "
                "SUM(CASE WHEN state = 2 THEN 1 ELSE 0 END) AS quarantined "
                "FROM coords GROUP BY z",
                (watermark, watermark),
            ).fetchall()
            for row in rows:
                zoom = int(row["z"])
                bucket = zooms.setdefault(
                    zoom, {"current": 0, "superseded": 0, "quarantined": 0}
                )
                bucket["current"] += int(row["visible"])
                bucket["superseded"] += int(row["superseded"])
                bucket["quarantined"] += int(row["quarantined"])
                unpublished += int(row["unpublished"])

            # 内容实体数 = 去重后的 blob 条数；dedup 字节 = 这些 blob 的实际大小
            row = connection.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS total FROM contents"
            ).fetchone()
            contents += int(row["n"])
            dedup += int(row["total"])

            # 逻辑字节只算可见坐标：同一内容被多坐标引用时按引用次数重复计入
            row = connection.execute(
                "SELECT COALESCE(SUM(c.size), 0) AS total "
                "FROM coords AS n JOIN contents AS c ON c.sha256 = n.sha256 "
                "WHERE n.state = 0 AND n.epoch_id <= ?",
                (watermark,),
            ).fetchone()
            logical += int(row["total"])
        finally:
            connection.close()
    if unpublished:
        notes.append(
            f"{asset.text}：有 {unpublished:,} 张已提交但未发布（干扰恢复流程才能看见）"
        )
    return AssetStats(
        asset=asset.text,
        zooms=tuple(
            ZoomStats(zoom=zoom, current=value["current"], superseded=value["superseded"],
                      quarantined=value["quarantined"])
            for zoom, value in sorted(zooms.items())
        ),
        contents=contents,
        logical_bytes=logical,
        dedup_bytes=dedup,
        shard_files=shard_files,
        shard_bytes=shard_bytes,
        wal_bytes=wal_bytes,
        shm_bytes=shm_bytes,
    )


def _file_size(path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _folder_bytes(folder) -> int:
    if not folder.is_dir():
        return 0
    total = 0
    for path in folder.rglob("*"):
        if path.is_file():
            total += _file_size(path)
    return total
