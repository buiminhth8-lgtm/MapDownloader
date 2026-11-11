"""分层约束、图层隔离与大批量流式遍历（Slice 42 要求 1 / 6 / 10）。

这个文件守的是"以后不会变坏"的三条线：

1. ``app.tilerepo`` **不许**依赖 UI / 打包 / 网络 —— 仓库要能被 CLI、导出器、
   Android 导入工具各自复用，一旦它 import 了 ``app.ui``，这个复用就断了；
2. 不同图层（底图 ``vec_w`` 与注记 ``cva_w``）**互相看不见**：同坐标不同图层是
   两份不同的资产，合并统计是最容易悄悄做错的地方；
3. 大批量必须**流式**：一万多张的遍历不许把整级坐标一次载入内存。
"""

from __future__ import annotations

import ast
import json
import tempfile
import time
from pathlib import Path

import pytest

from app.tilerepo import AssetId, AssetKind, TileRepository, classify_asset_kind
from tests.tilerepo_fakes import distinct_png, make_repo, png_bytes

VEC = "tianditu/vec_w"
CVA = "tianditu/cva_w"

REPO_PACKAGE = Path(__file__).resolve().parent.parent / "app" / "tilerepo"

#: 仓库包**不许**出现的导入前缀（UI / 打包 / 网络）。
FORBIDDEN_PREFIXES = (
    "app.ui",
    "app.package",
    "PySide6",
    "requests",
    "httpx",
    "aiohttp",
    "urllib.request",
    "socket",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def test_repository_package_has_no_ui_or_network_dependency() -> None:
    """AST 守卫（比 import 一次更硬：它连"函数里才 import"也能抓到）。"""
    files = sorted(REPO_PACKAGE.glob("*.py"))
    assert files, "app/tilerepo 里一个模块都没找到，守卫失效了"
    offenders: list[str] = []
    for path in files:
        for module in _imported_modules(path):
            if module == "socket":
                # shard.py 只用 socket.gethostname() 记租约归属，不建连接
                continue
            if module.startswith(FORBIDDEN_PREFIXES):
                offenders.append(f"{path.name}: {module}")
    assert offenders == [], f"仓库层依赖了不该依赖的东西：{offenders}"


def test_identity_never_contains_a_token() -> None:
    """身份字符串里不许出现查询串/Token —— 它会一路进分片名、日志和报告。"""
    for bad in ("tianditu/vec_w?tk=abc", "tianditu/vec_w&tk=abc", "tianditu/", "/vec_w"):
        with pytest.raises(ValueError):
            AssetId.parse(bad)


def test_reports_carry_no_bytes_and_no_paths_outside_root(work_dir: Path) -> None:
    """报告是给人看/给机器读的：不含图片字节，也不含绝对路径。"""
    with make_repo(work_dir) as repo:
        repo.ensure_asset(VEC)
        report = repo.put_tiles(VEC, [(5, 1, 1, png_bytes())])
        payload = json.dumps(
            {
                "put": report.as_dict(),
                "health": repo.health_check("full").as_dict(),
                "stats": repo.stats().as_dict(),
            },
            ensure_ascii=False,
        )
    assert "\\x89PNG" not in payload
    assert "PNG" not in payload.replace("\\x89PNG", "")
    assert str(work_dir).replace("\\", "/") not in payload.replace("\\\\", "/")
    assert "tk=" not in payload


def test_report_models_are_json_serialisable(work_dir: Path) -> None:
    with make_repo(work_dir) as repo:
        repo.ensure_asset(VEC)
        repo.put_tiles(VEC, [(5, 1, 1, png_bytes())])
        meta = repo.freeze_snapshot()
        for payload in (
            repo.put_tiles(VEC, [(5, 1, 1, png_bytes())]).as_dict(),
            repo.health_check().as_dict(),
            repo.recover().as_dict(),
            repo.rebuild_index().as_dict(),
            repo.prune().as_dict(),
            repo.stats().as_dict(),
            repo.query_existence(VEC, (5, 5)).as_dict(),
            meta.as_dict(),
        ):
            json.dumps(payload, ensure_ascii=False)  # 不抛就算通过


# --------------------------------------------------------------------------- #
# 图层隔离
# --------------------------------------------------------------------------- #
def test_layers_are_isolated(work_dir: Path) -> None:
    """同坐标、不同图层：不同分片文件、不同内容、互不覆盖。"""
    vec = png_bytes((10, 20, 30))
    cva = png_bytes((200, 100, 0))
    with make_repo(work_dir) as repo:
        repo.ensure_asset(VEC)
        repo.ensure_asset(CVA)
        repo.put_tiles(VEC, [(5, 3, 3, vec)])
        repo.put_tiles(CVA, [(5, 3, 3, cva)])

        assert repo.shard_path(VEC, (0, 6)) != repo.shard_path(CVA, (0, 6))
        assert repo.read_tile(VEC, 5, 3, 3).record.data == vec
        assert repo.read_tile(CVA, 5, 3, 3).record.data == cva

        # 缺口统计也按图层分开算，不许把注记算进底图
        report = repo.query_existence([VEC, CVA], (5, 5))
        assert [item.asset for item in report.assets] == [VEC, CVA]
        assert all(item.present == 1 for item in report.assets)

        stats = repo.stats()
        assert {item.asset for item in stats.assets} == {VEC, CVA}
        for item in stats.assets:
            assert item.contents == 1


def test_asset_kind_comes_from_identity_not_from_metadata(work_dir: Path) -> None:
    """原始层身份来自 ``AssetId``；``classify_asset_kind`` 只判**旧包**是合成还是未知。

    两者刻意分开：身份（谁）由仓库登记，分类（这份旧包能不能当原始层复用）由
    MBTiles metadata 判 —— 契约 §3.2 的判定式永不返回 ``RAW_*``，因为它无法证明
    "这份包没被叠过字"。
    """
    with make_repo(work_dir) as repo:
        assert AssetId.parse(VEC).role == "base"
        assert AssetId.parse(CVA).role == "label"
        assert AssetId.parse(VEC).is_label is False
        assert AssetId.parse(CVA).is_label is True

        assert classify_asset_kind({"composition": "merged", "labels": "true"}) is AssetKind.SYNTHETIC_MERGED
        assert classify_asset_kind({"composition": "single"}) is AssetKind.SYNTHETIC_SINGLE
        assert classify_asset_kind({"labels": "true", "map_type": "vec"}) is AssetKind.UNKNOWN
        assert classify_asset_kind(None) is AssetKind.UNKNOWN
        assert not any(
            classify_asset_kind(item).is_raw
            for item in (
                None,
                {},
                {"composition": "single"},
                {"composition": "merged", "labels": "true"},
            )
        )
        assert repo.ensure_asset(VEC).text == VEC


# --------------------------------------------------------------------------- #
# 大批量流式
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def big_repo() -> tuple[TileRepository, dict[str, object]]:
    """12,000 张瓦片（32 份不同内容反复引用）—— 覆盖"流式 + 去重"两个量级。"""
    # 刻意不用 pytest 的 tmp_path（本机沙箱会拦 pytest-of-* 根目录）
    base = Path(tempfile.mkdtemp(prefix="mbtiles-big-"))
    repo = TileRepository.open(base / "repo", create=True)
    repo.ensure_asset(VEC)
    palette = [distinct_png(index) for index in range(32)]
    coords = [
        (7, x, y)
        for x in range(128)
        for y in range(128)
    ][:12000]
    started = time.monotonic()
    report = repo.put_tiles(
        VEC, [(z, x, y, palette[(x * 128 + y) % len(palette)]) for z, x, y in coords]
    )
    elapsed = time.monotonic() - started
    yield repo, {"coords": coords, "palette": palette, "report": report, "elapsed": elapsed}
    repo.close()


def test_bulk_write_then_stream(big_repo) -> None:
    repo, info = big_repo
    report = info["report"]
    assert report.accepted == 12000
    assert report.published is True

    # 流式遍历：一次只吐一条，顺序按主键 (z, x, y)
    seen = 0
    previous: tuple[int, int, int] | None = None
    for record in repo.iter_tiles(VEC):
        current = record.coord.as_tuple()
        if previous is not None:
            assert current > previous, "遍历顺序必须严格递增（导出端靠它流式写包）"
        previous = current
        seen += 1
    assert seen == 12000

    # 32 份内容被 12k 个坐标引用：contents 只有 32 条，逻辑字节按引用次数算
    stats = repo.stats()
    asset = stats.assets[0]
    assert asset.contents == len(info["palette"])
    assert asset.dedup_bytes == sum(len(item) for item in info["palette"])
    assert asset.logical_bytes == sum(len(info["palette"][(x * 128 + y) % 32]) for _, x, y in info["coords"])
    assert asset.logical_bytes > asset.dedup_bytes


def test_bulk_gap_report_is_truncated_not_walked(big_repo) -> None:
    """整级缺口统计：数字要准，样例要有上限（不许为了凑样例扫 16k 列）。"""
    repo, _info = big_repo
    started = time.monotonic()
    report = repo.query_existence(VEC, (7, 7))
    elapsed = time.monotonic() - started

    zoom = report.assets[0].zooms[0]
    assert zoom.total == 128 * 128
    assert zoom.present == 12000
    assert zoom.missing == 128 * 128 - 12000
    assert zoom.missing_sample
    assert zoom.sample_truncated is True
    assert elapsed < 30, f"缺口统计花了 {elapsed:.1f}s，说明它在遍历整个级别"


def test_bulk_snapshot_and_stats(big_repo) -> None:
    repo, info = big_repo
    meta = repo.freeze_snapshot(assets=[VEC])
    assert meta.entries == 12000
    assert meta.fingerprint

    with repo.open_snapshot(meta.fingerprint) as view:
        count = sum(1 for _ in view)
    assert count == 12000

    again = repo.freeze_snapshot(assets=[VEC])
    assert again.fingerprint == meta.fingerprint, "同数据两次冻结必须是同一个指纹"
    assert info["report"].shards
