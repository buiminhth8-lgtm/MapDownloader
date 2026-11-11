"""生成数据集根目录的判定缓存 —— "点省份卡死"的性能回归守卫。

**这条缺陷长什么样**（2026-11 实测，本机部署了 3253 条真实区域）：

在下载面板把「下载范围」切到行政区域，点任意一个省份，窗口**冻住 9~11 秒**
（Windows 直接把标题栏写成"未响应"）。原因不在 GeoJSON，而在
:func:`app.regions.catalog.effective_region_root` —— 它在
``prefer_generated=True`` 时要**完整解析一遍 1.19 MB 的 index.json** 才能确认
"发布的数据真的能读"（74 ms），而这条判定被下面这条链反复调用：

``_fill_city_combo`` / ``_fill_county_combo`` → 每个条目一次
``service.boundary_available`` → ``service.loader`` → ``service.catalog``
→ ``service.root`` → ``effective_region_root()``

**旧实现每次都重新解析**，于是一个 11 市 + 90 县的省 = 上百次 × 159 ms ≈ 10 秒
主线程阻塞。同一件事在 ``test_ui_smoke`` 上也发生过（44 秒 → 400 秒）。

修复是"按 index.json 的 ``(mtime_ns, size)`` 缓存判定结果"。本文件守两件事：

1. **语义没变**：重新发布（mtime/size 变）立刻生效；没有 / 坏掉的 index.json
   仍然回退手工目录，且"没有"这件事不被永久缓存（用户放好数据不必重启）。
2. **性能没退回去**：重复查询不再解析 index.json，且**点一次省份**期间
   一次都不解析 —— 这条断言不依赖计时，因此在慢机器上也不会 flake。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from app.config import settings as settings_module
from app.regions import RegionService
from app.regions import catalog as catalog_module
from app.ui.download_panel import DownloadPanel
from tests.conftest import REGION_FIXTURE_ROOT


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture
def generated_root(work_dir: Path, monkeypatch) -> Path:
    """把合成目录树摆成"已发布的生成数据集"（``REGION_DATA_ROOT`` 指向它）。"""
    target = work_dir / "generated"
    shutil.copytree(REGION_FIXTURE_ROOT, target)
    monkeypatch.setattr(settings_module, "REGION_DATA_ROOT", target)
    return target


@pytest.fixture
def load_calls(monkeypatch) -> list[Path]:
    """记下 ``RegionCatalog.load`` 被调用了几次、读的是哪个根。

    数"解析了几次"而不是量"花了多少毫秒"：计时在别人的机器上会 flake，
    而解析次数与耗时是同一件事的两面（被缓存住就不可能再解析）。
    """
    calls: list[Path] = []
    original = catalog_module.RegionCatalog.load.__func__

    def counting_load(cls, *, root: Path | None = None):
        calls.append(Path(root) if root is not None else Path("."))
        return original(cls, root=root)

    monkeypatch.setattr(catalog_module.RegionCatalog, "load", classmethod(counting_load))
    return calls


def _add_region(root: Path, region_id: str) -> None:
    """往已发布的数据集里加一条省（同时补一份能读的边界文件）。"""
    shutil.copy(
        root / "province" / "southland.geojson",
        root / "province" / f"{region_id}.geojson",
    )
    index = root / "index.json"
    payload = json.loads(index.read_text(encoding="utf-8"))
    payload["regions"].append(
        {
            "id": region_id,
            "name": "示例新省",
            "level": "province",
            "parentId": "demoland",
            "geojson": f"province/{region_id}.geojson",
            "boundaryVersion": "2026-11-test",
        }
    )
    index.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 语义：缓存不改变"什么时候该重读"
# --------------------------------------------------------------------------- #
def test_root_lookup_parses_the_index_only_once(generated_root: Path, load_calls) -> None:
    """同一份 index.json：第一次判定，之后 50 次都走缓存。"""
    assert catalog_module.effective_region_root() == generated_root
    assert len(load_calls) == 1

    for _ in range(50):
        assert catalog_module.effective_region_root() == generated_root
    assert len(load_calls) == 1, "同一份数据被反复解析 —— 又会退化成点省份卡死"


def test_republishing_is_seen_without_restart(generated_root: Path, load_calls) -> None:
    """重新发布（index.json 变了）必须立刻生效 —— Spec §17 "导入后不用重启"。"""
    assert catalog_module.effective_region_root() == generated_root
    assert len(load_calls) == 1

    _add_region(generated_root, "newland")

    assert catalog_module.effective_region_root() == generated_root
    assert len(load_calls) == 2, "index.json 改了却没重新判定，新数据会读不到"

    service = RegionService(prefer_generated=True)
    assert service.catalog.get("newland") is not None


def test_absent_index_is_not_cached_forever(work_dir: Path, monkeypatch, load_calls) -> None:
    """还没有生成数据集时不写缓存：用户放好数据后下一次查询就该看见。"""
    generated = work_dir / "generated"
    generated.mkdir()
    monkeypatch.setattr(settings_module, "REGION_DATA_ROOT", generated)

    assert catalog_module.effective_region_root() == catalog_module.region_resource_root()
    assert load_calls == []

    shutil.copytree(REGION_FIXTURE_ROOT, generated, dirs_exist_ok=True)

    assert catalog_module.effective_region_root() == generated
    assert len(load_calls) == 1


def test_broken_index_falls_back_and_is_not_reparsed(generated_root: Path, load_calls) -> None:
    """文件在但读不出 → 回退手工目录（Spec §17），且同一份坏文件不反复重试。"""
    (generated_root / "index.json").write_text("{ 这不是 json", encoding="utf-8")

    manual = catalog_module.region_resource_root()
    assert catalog_module.effective_region_root() == manual
    assert len(load_calls) == 1

    for _ in range(10):
        assert catalog_module.effective_region_root() == manual
    assert len(load_calls) == 1


# --------------------------------------------------------------------------- #
# 性能：级联下拉不再重复解析
# --------------------------------------------------------------------------- #
def test_boundary_availability_does_not_reparse_the_catalog(
    generated_root: Path, load_calls
) -> None:
    """逐个问"边界在不在"是填下拉的热路径，它一次都不该再解析目录。"""
    service = RegionService(prefer_generated=True)
    entries = service.catalog.list_all()
    baseline = len(load_calls)

    for _ in range(3):
        for entry in entries:
            service.boundary_available(entry.id)

    assert len(load_calls) == baseline, (
        "boundary_available 又在解析 index.json —— "
        f"{len(entries)} 个条目 × 3 轮 = {len(load_calls) - baseline} 次"
    )


def test_selecting_a_province_does_not_reparse_the_catalog(
    qapp: QApplication, generated_root: Path, load_calls
) -> None:
    """**这条就是用户看到的那次卡死**：点一下省份，目录只许解析零次。

    选择省份会依次填「城市」「县区」两个下拉，每加一个条目都要问一次
    ``boundary_available``。旧实现里每一次都是一遍 74 ms 的 index.json 解析，
    真实数据（3253 条）下一个省就是 10 秒 —— 这里用合成目录守住"次数为 0"，
    与机器快慢无关。
    """
    panel = DownloadPanel(region_service=RegionService(prefer_generated=True))
    qapp.processEvents()

    province_index = next(
        index
        for index in range(panel._province_combo.count())
        if (entry := panel._province_combo.itemData(index)) is not None
        and entry.id == "northland"
    )
    baseline = len(load_calls)

    panel._province_combo.setCurrentIndex(province_index)
    qapp.processEvents()

    assert panel._city_combo.count() > 1, "省下面应当列出城市，否则这条用例没测到热路径"
    assert len(load_calls) == baseline, (
        f"选一次省份解析了 {len(load_calls) - baseline} 次目录"
    )
