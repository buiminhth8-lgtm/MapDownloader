"""批量判存（``RegionLoader.available_many``）的守卫。

守的是什么：界面刷新下拉时要问"每条区域的边界文件在不在"。单条版
``available()`` 早先走 ``peek_path()``，也就是
:func:`~app.geo.region.resolve_region_path` —— 那里面每条都要
``Path.resolve()`` 两次（root 一次、候选路径一次），Windows 上落到
``nt._getfinalpathname``。条目一多就是上千次系统调用；磁盘慢（网络盘、
机械盘、杀毒软件逐个扫）时这一步能拖到几秒，用户看到的是"点一下省份就卡住"。

**为什么断言"调用次数"而不是"耗时"**：这类退化在开发机上只有几十毫秒，
计时断言只会变成 flaky；而"给 N 个条目判存，列目录几次、解析真实路径几次"
是稳定的结构事实，恰恰就是卡顿的成因。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import app.regions.loader as loader_module
from app.regions import RegionCatalog, RegionLoader

#: 县区数量：足够把"每条一次 resolve"放大成可观的开销，又不至于拖慢用例。
COUNTY_COUNT = 120


def _write_catalog(root: Path, counties: int = COUNTY_COUNT) -> Path:
    """在 ``root`` 下造一份"1 国 + 1 省 + N 县"的合成目录（三个目录）。"""
    for name in ("country", "province", "county"):
        (root / name).mkdir(parents=True, exist_ok=True)
    records = [
        {
            "id": "testland",
            "name": "测试国",
            "level": "country",
            "parentId": None,
            "geojson": "country/testland.geojson",
            "boundaryVersion": "test",
        },
        {
            "id": "testprovince",
            "name": "测试省",
            "level": "province",
            "parentId": "testland",
            "geojson": "province/testprovince.geojson",
            "boundaryVersion": "test",
        },
    ]
    for index in range(counties):
        records.append(
            {
                "id": f"county{index:03d}",
                "name": f"测试县{index:03d}",
                "level": "county",
                "parentId": "testprovince",
                "geojson": f"county/county{index:03d}.geojson",
                "boundaryVersion": "test",
            }
        )
    for record in records:
        (root / record["geojson"]).write_text("{}", encoding="utf-8")
    (root / "index.json").write_text(
        json.dumps({"version": 1, "regions": records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def catalog_root(tmp_path: Path) -> Path:
    return _write_catalog(tmp_path / "catalog")


@pytest.fixture
def loader(catalog_root: Path) -> RegionLoader:
    return RegionLoader(RegionCatalog.load(root=catalog_root), root=catalog_root)


def _ids(loader: RegionLoader) -> list[str]:
    return [entry.id for entry in loader.catalog.list_all()]


def test_available_many_lists_each_directory_once(
    loader: RegionLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """122 条区域只该列 3 次目录（country / province / county 各一次）。"""
    ids = _ids(loader)
    seen: list[Path] = []
    real_scandir = os.scandir

    def counting_scandir(path):
        seen.append(Path(path))
        return real_scandir(path)

    monkeypatch.setattr(loader_module.os, "scandir", counting_scandir)

    answer = loader.available_many(ids)

    assert set(answer) == set(ids)
    assert set(answer.values()) == {True}
    assert len(seen) == 3, f"按目录各列一次即可，实际列了 {len(seen)} 次：{seen}"


def test_available_many_does_not_resolve_real_paths(
    loader: RegionLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """批量判存**不能**走 ``resolve_region_path``（那就是卡顿的来源）。

    补丁必须打在 ``app.regions.models`` 上：那里是
    ``from app.geo.region import resolve_region_path`` 的直接绑定，改
    ``app.geo.region`` 上的同名属性拦不住它 —— 第一版就是这么写的，计数永远
    是 0，等于没守（这类"补丁打错层"的守卫比没有更糟，它给的是假的绿）。
    """
    import app.regions.models as models_module

    def boom(*_args, **_kwargs):
        raise AssertionError("批量判存不该解析真实路径")

    monkeypatch.setattr(models_module, "resolve_region_path", boom)

    answer = loader.available_many(_ids(loader))

    assert set(answer.values()) == {True}


def test_available_many_agrees_with_single_lookup(
    loader: RegionLoader, catalog_root: Path
) -> None:
    """删掉一个文件后，批量结论与单条结论必须一致（同一份判定口径）。"""
    (catalog_root / "county" / "county042.geojson").unlink()
    ids = _ids(loader)

    single = {region_id: loader.available(region_id) for region_id in ids}
    assert single["county042"] is False
    assert loader.available_many(ids) == single


def test_available_many_treats_unknown_and_empty_ids_as_unavailable(
    loader: RegionLoader,
) -> None:
    """目录里没有的 id、``None``、空串：一律不可用，**不抛异常**。"""
    assert loader.available_many(["nope", None, ""]) == {"nope": False}
    assert loader.available(None) is False
    assert loader.available("") is False


def test_available_many_falls_back_when_directory_cannot_be_listed(
    loader: RegionLoader, catalog_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目录列不出来（权限 / 路径过长）→ 退回逐个问磁盘，结论不变。"""
    real_scandir = os.scandir

    def flaky_scandir(path):
        if Path(path).name == "county":
            raise PermissionError(13, "拒绝访问")
        return real_scandir(path)

    monkeypatch.setattr(loader_module.os, "scandir", flaky_scandir)

    assert set(loader.available_many(_ids(loader)).values()) == {True}
    assert loader.available("county007") is True

    (catalog_root / "county" / "county007.geojson").unlink()
    assert loader.available("county007") is False
