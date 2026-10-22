"""scripts/import_china_admin_regions.py 的合并 / 去重逻辑（Slice 27）。

天地图的三份行政区划数据（省 / 市 / 县）有"行政级别有歧义"的重叠单位，本文件
用合成数据钉死 :func:`_merge` 的去重与 parent 推导规则 —— 用真实中国边界做测试
既慢又依赖下载回来的数据，合成数据几毫秒跑完。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_merge():
    script = PROJECT_ROOT / "scripts" / "import_china_admin_regions.py"
    spec = importlib.util.spec_from_file_location("china_admin_import", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module._merge


def _rect(west: float, south: float, east: float, north: float) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
    }


def _fc(*features: dict) -> str:
    return json.dumps({"type": "FeatureCollection", "features": list(features)}, ensure_ascii=False)


def _feature(gb: str, name: str, geometry: dict | None = None) -> dict:
    return {
        "type": "Feature",
        "properties": {"gb": gb, "name": name},
        "geometry": geometry or _rect(100, 20, 101, 21),
    }


def test_merge_dedups_municipalities_and_derives_parents(tmp_path: Path) -> None:
    """直辖市、省直辖县、不设区的市三类的去重 + parent 推导（Slice 27）。"""
    merge = _load_merge()

    # 省：A省(156100000) + 直辖市(156110000，北京式)
    province = _fc(
        _feature("156100000", "A省"),
        _feature("156110000", "B直辖市"),
    )
    # 市：A市(156100100) + 直辖市(与省重叠) + 不设区的市(156100300，后2位00)
    #   + 省直辖县(156100201，后2位非00，本应归县)
    city = _fc(
        _feature("156100100", "A市"),
        _feature("156110000", "B直辖市"),  # 与省重叠 → 跳过
        _feature("156100300", "C不设区的市"),  # 后2位00 → 归市
        _feature("156100201", "D省直辖县"),  # 后2位非00 → 归县
    )
    # 县：A市的县(156100101) + 直辖市重叠(156110000) + 不设区的市(与市重叠)
    #   + 省直辖县(156100201) + 直辖市的区(156110001，挂直辖市)
    county = _fc(
        _feature("156100101", "A市的县"),
        _feature("156110000", "B直辖市"),  # 与省重叠 → 跳过
        _feature("156100300", "C不设区的市"),  # 后2位00 → 跳过（市那份归市）
        _feature("156100201", "D省直辖县"),
        _feature("156110001", "B直辖市的区"),
    )

    p_file = tmp_path / "province.geojson"
    c_file = tmp_path / "city.geojson"
    x_file = tmp_path / "county.geojson"
    out = tmp_path / "merged.geojson"
    p_file.write_text(province, encoding="utf-8")
    c_file.write_text(city, encoding="utf-8")
    x_file.write_text(county, encoding="utf-8")

    n_prov, n_city, n_county = merge(p_file, c_file, x_file, out)

    # 省 2（A省 + 直辖市）；市 2（A市 + 不设区的市）；县 3（A市的县 + 省直辖县 + 直辖市的区）
    assert (n_prov, n_city, n_county) == (2, 2, 3)

    merged = json.loads(out.read_text(encoding="utf-8"))["features"]
    by_gb = {f["properties"]["gb"]: f["properties"] for f in merged}

    # 全国条目
    assert by_gb["china"]["level"] == "country"
    assert by_gb["china"]["parent"] == ""
    # 省挂全国
    assert by_gb["156100000"]["level"] == "province"
    assert by_gb["156100000"]["parent"] == "china"
    # 直辖市是省，挂全国（市文件那份被丢弃）
    assert by_gb["156110000"]["level"] == "province"
    assert by_gb["156110000"]["parent"] == "china"
    # 市挂省
    assert by_gb["156100100"]["level"] == "city"
    assert by_gb["156100100"]["parent"] == "156100000"
    # 不设区的市挂省（后2位00 → 归市，县那份被丢弃）
    assert by_gb["156100300"]["level"] == "city"
    assert by_gb["156100300"]["parent"] == "156100000"
    # 省直辖县挂省（后2位非00 → 归县，市那份被丢弃）
    assert by_gb["156100201"]["level"] == "county"
    assert by_gb["156100201"]["parent"] == "156100000"
    # 正常县挂市
    assert by_gb["156100101"]["level"] == "county"
    assert by_gb["156100101"]["parent"] == "156100100"
    # 直辖市的区挂直辖市（跳过市这一级）
    assert by_gb["156110001"]["level"] == "county"
    assert by_gb["156110001"]["parent"] == "156110000"


def test_merge_filters_boundary_lines(tmp_path: Path) -> None:
    """gb 为空的"境界线"Feature 被过滤掉。"""
    merge = _load_merge()

    province = _fc(
        _feature("156100000", "A省"),
        {"type": "Feature", "properties": {"gb": "", "name": "境界线"}, "geometry": _rect(100, 20, 101, 21)},
    )
    city = _fc()
    county = _fc()

    p_file = tmp_path / "p.geojson"
    c_file = tmp_path / "c.geojson"
    x_file = tmp_path / "x.geojson"
    out = tmp_path / "merged.geojson"
    p_file.write_text(province, encoding="utf-8")
    c_file.write_text(city, encoding="utf-8")
    x_file.write_text(county, encoding="utf-8")

    n_prov, n_city, n_county = merge(p_file, c_file, x_file, out)
    assert (n_prov, n_city, n_county) == (1, 0, 0)
