"""仓库的调试用命令行（Slice 42）。

存在的理由：Slice 42 没有界面。要验证"仓库到底存了什么、坏没坏、恢复有没有用"，
命令行比让每个调用方自己拼 ``sqlite3`` 可靠得多。**它不是产品功能**：
参数只覆盖验收需要的动作，不带任何业务编排（那些属于 Slice 43~46）。

用法::

    python -m app.tilerepo.cli --root D:/tiles/repo init
    python -m app.tilerepo.cli --root D:/tiles/repo health --full
    python -m app.tilerepo.cli --root D:/tiles/repo stats
    python -m app.tilerepo.cli --root D:/tiles/repo rebuild
    python -m app.tilerepo.cli --root D:/tiles/repo put --asset tianditu/vec_w \
        --z 5 --x 1 --y 2 --file tile.png
    python -m app.tilerepo.cli --root D:/tiles/repo read --asset tianditu/vec_w --z 5 --x 1 --y 2

退出码：0 = 成功；1 = 失败（健康检查发现 error 级问题也算失败）；2 = 参数错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.tilerepo.errors import RepoError
from app.tilerepo.store import TileRepository


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.tilerepo.cli",
        description="统一瓦片仓库的调试命令（Slice 42）",
    )
    parser.add_argument("--root", type=Path, default=None, help="仓库根目录（默认取设置项）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化仓库（幂等）")
    sub.add_parser("info", help="列出资产与分片带")

    put = sub.add_parser("put", help="写入一张瓦片（用文件内容）")
    put.add_argument("--asset", required=True)
    put.add_argument("--z", type=int, required=True)
    put.add_argument("--x", type=int, required=True)
    put.add_argument("--y", type=int, required=True)
    put.add_argument("--file", type=Path, required=True)
    put.add_argument("--allow-replace", action="store_true", help="同坐标不同内容时授权替换")

    read = sub.add_parser("read", help="读一张瓦片到文件或 stdout 尺寸信息")
    read.add_argument("--asset", required=True)
    read.add_argument("--z", type=int, required=True)
    read.add_argument("--x", type=int, required=True)
    read.add_argument("--y", type=int, required=True)
    read.add_argument("--out", type=Path, default=None)
    read.add_argument("--epoch", type=int, default=None, help="指定版本（默认当前）")

    health = sub.add_parser("health", help="健康检查（默认只读 quick）")
    health.add_argument("--full", action="store_true")

    sub.add_parser("recover", help="接管未完成的操作")

    rebuild = sub.add_parser("rebuild", help="从分片重建 catalog 登记")
    rebuild.add_argument("--asset", default=None)

    sub.add_parser("prune", help="孤立内容/过期版本报告（只报告）")

    stats = sub.add_parser("stats", help="容量与状态统计")
    stats.add_argument("--asset", default=None)

    snapshot = sub.add_parser("snapshot", help="冻结一份快照清单")
    snapshot.add_argument("--asset", action="append", default=None)
    snapshot.add_argument("--zoom", default=None, help="形如 5-6")

    return parser


def _zoom_range(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    head, _, tail = text.partition("-")
    return (int(head), int(tail or head))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            repo = TileRepository.open(args.root, create=True)
            with repo:
                payload = {
                    "root": str(repo.root),
                    "created": repo.created,
                    "bands": [list(item) for item in repo.bands],
                    "assets": repo.catalog.list_assets(),
                }
            return _emit(payload, args.json, ["仓库已就绪（幂等）"])

        repo = TileRepository.open(args.root, create=False)
        with repo:
            if args.command == "info":
                payload = {
                    "root": str(repo.root),
                    "bands": [list(item) for item in repo.bands],
                    "assets": repo.catalog.list_assets(),
                    "shards": [
                        {
                            "asset": item.asset_id,
                            "band": [item.z_min, item.z_max],
                            "tiles": item.tile_count,
                            "published_epoch": item.published_epoch,
                            "path": item.path,
                        }
                        for item in repo.catalog.list_shards()
                    ],
                    "epoch": repo.catalog.max_epoch(),
                }
                return _emit(payload, args.json, [f"资产 {len(payload['assets'])} 个"])

            if args.command == "put":
                data = args.file.read_bytes()
                report = repo.put_tiles(
                    args.asset,
                    [(args.z, args.x, args.y, data)],
                    allow_replace=args.allow_replace,
                )
                return _emit(report.as_dict(), args.json, report.as_lines())

            if args.command == "read":
                lookup = repo.read_tile(
                    args.asset, args.z, args.x, args.y, epoch=args.epoch
                )
                payload = {"status": lookup.status.value, "reason": lookup.reason}
                if lookup.record is not None:
                    payload.update(
                        {
                            "size": lookup.record.size,
                            "encoding": lookup.record.digest.encoding,
                            "sha256": lookup.record.digest.hex,
                            "epoch": lookup.record.epoch,
                        }
                    )
                    if args.out is not None:
                        args.out.write_bytes(lookup.record.data)
                        payload["written_to"] = str(args.out)
                return _emit(payload, args.json, [f"状态：{lookup.status.value} {lookup.reason}"])

            if args.command == "health":
                report = repo.health_check("full" if args.full else "quick")
                ok = report.ok  # type: ignore[attr-defined]
                _emit(report.as_dict(), args.json, report.as_lines())
                return 0 if ok else 1

            if args.command == "recover":
                report = repo.recover()
                return _emit(report.as_dict(), args.json, report.as_lines())

            if args.command == "rebuild":
                report = repo.rebuild_index(args.asset)
                return _emit(report.as_dict(), args.json, report.as_lines())

            if args.command == "prune":
                report = repo.prune(dry_run=True)
                return _emit(report.as_dict(), args.json, report.as_lines())

            if args.command == "stats":
                report = repo.stats(args.asset)
                return _emit(report.as_dict(), args.json, report.as_lines())

            if args.command == "snapshot":
                meta = repo.freeze_snapshot(args.asset, zoom_range=_zoom_range(args.zoom))
                return _emit(meta.as_dict(), args.json, [f"快照 {meta.fingerprint}（{meta.entries} 条）"])

        raise SystemExit(2)
    except RepoError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


def _emit(payload: dict, as_json: bool, lines: list[str]) -> int:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        for line in lines:
            print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
