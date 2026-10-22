"""``MapPackageReader`` 与 manifest 字段校验的单元测试（Slice 9，Spec §29）。

这一层守的是**协议**：manifest 是给 Android 解析的文件，字段名、类型、取值范围
一旦放错东西过去，错误会在另一台设备上、用另一种语言爆出来。所以这里的
用例几乎全是"坏输入必须被拒绝"，而且要断言**拒绝的理由**能对上号。

``mbtiles.file`` 的路径穿越（``../`` / ``../../`` / ``D:\\...``）是重点：
manifest 将来可能是**别人给的**（从网上下包、别人拷过来的包），
读一个包不该有"读到包外面去"的可能。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.package import (
    MANIFEST_FIELD_ORDER,
    ManifestError,
    ManifestPathError,
    MapPackageManifest,
    UnsupportedFormatVersion,
    ensure_inside,
    parse_manifest_dict,
    render_manifest_json,
)
from app.package.package_reader import MapPackageReader

GOOD_BOUNDS = {"west": 116.20, "south": 39.70, "east": 116.60, "north": 40.10}
GOOD_SHA = "a" * 64


def _manifest_dict(**overrides) -> dict:
    """一份完整合法的 manifest 结构（用 ``overrides`` 制造各种坏输入）。"""
    data: dict = {
        "formatVersion": 1,
        "id": "beijing_20260916",
        "name": "北京离线地图",
        "provider": "tianditu",
        "mapType": "vector",
        "labels": True,
        "composition": "merged",
        "tileScheme": "web_mercator",
        "tileFormat": "png",
        "minZoom": 8,
        "maxZoom": 17,
        "bounds": dict(GOOD_BOUNDS),
        "tileCount": 52381,
        "mbtiles": {"file": "map.mbtiles", "size": 1523489123, "sha256": GOOD_SHA},
        "createdAt": "2026-09-16T09:00:00+08:00",
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            merged = dict(data[key])
            merged.update(value)
            data[key] = merged
        else:
            data[key] = value
    return data


def _write_package(directory: Path, data, *, name: str = "manifest.json") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return directory


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #
def test_read_manifest_returns_structured_object(work_dir: Path) -> None:
    package = _write_package(work_dir / "pkg", _manifest_dict())

    manifest = MapPackageReader().read_manifest(package)

    assert isinstance(manifest, MapPackageManifest)
    assert manifest.id == "beijing_20260916"
    assert manifest.name == "北京离线地图"
    assert manifest.map_type == "vector"
    assert manifest.labels is True
    assert manifest.composition == "merged"
    assert manifest.is_merged is True
    assert manifest.tile_format == "png"
    assert (manifest.min_zoom, manifest.max_zoom) == (8, 17)
    assert manifest.zoom_text == "8~17"
    assert manifest.tile_count == 52381
    assert manifest.mbtiles_file == "map.mbtiles"
    assert manifest.mbtiles_size == 1523489123
    assert manifest.mbtiles_sha256 == GOOD_SHA
    assert manifest.bounds_text == "116.200000,39.700000,116.600000,40.100000"
    assert manifest.size_text.endswith("GB")


def test_read_manifest_accepts_utf8_bom(work_dir: Path) -> None:
    """Windows 记事本另存 UTF-8 会带 BOM，带 BOM 的 JSON 也必须能读。

    不带这条容错的话，用户手改一下 manifest 再保存就得到
    ``Expecting value: line 1 column 1`` —— 一句指向不了任何地方的报错。
    """
    package = work_dir / "bom"
    package.mkdir()
    text = json.dumps(_manifest_dict(), ensure_ascii=False, indent=2)
    (package / "manifest.json").write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))

    assert MapPackageReader().read_manifest(package).name == "北京离线地图"


def test_read_manifest_ignores_unknown_fields(work_dir: Path) -> None:
    """未知字段忽略而不是报错：将来加字段时老解析器要能装作没看见。"""
    package = _write_package(
        work_dir / "extra", _manifest_dict(preview="preview.png", futureField=42)
    )
    assert MapPackageReader().read_manifest(package).tile_count == 52381


def test_read_manifest_accepts_optional_description(work_dir: Path) -> None:
    package = _write_package(work_dir / "desc", _manifest_dict(description="北京城区"))
    assert MapPackageReader().read_manifest(package).description == "北京城区"


def test_read_package_returns_manifest_and_mbtiles_path(work_dir: Path) -> None:
    package = _write_package(work_dir / "pair", _manifest_dict())
    (package / "map.mbtiles").write_bytes(b"sqlite-ish")

    manifest, mbtiles = MapPackageReader().read_package(package)

    assert manifest.mbtiles_file == "map.mbtiles"
    assert mbtiles.name == "map.mbtiles"
    assert mbtiles.is_file()


def test_manifest_path_and_has_manifest(work_dir: Path) -> None:
    reader = MapPackageReader()
    package = _write_package(work_dir / "has", _manifest_dict())
    assert reader.manifest_path(package).name == "manifest.json"
    assert reader.has_manifest(package) is True
    assert reader.has_manifest(work_dir / "nowhere") is False


# --------------------------------------------------------------------------- #
# 缺失 / 损坏
# --------------------------------------------------------------------------- #
def test_read_manifest_rejects_missing_directory(work_dir: Path) -> None:
    with pytest.raises(ManifestError, match="不存在"):
        MapPackageReader().read_manifest(work_dir / "nope")


def test_read_manifest_rejects_missing_file(work_dir: Path) -> None:
    empty = work_dir / "empty"
    empty.mkdir()
    with pytest.raises(ManifestError, match=r"manifest\.json"):
        MapPackageReader().read_manifest(empty)


def test_read_manifest_rejects_broken_json(work_dir: Path) -> None:
    package = work_dir / "broken"
    package.mkdir()
    (package / "manifest.json").write_text('{"formatVersion": 1,', encoding="utf-8")
    with pytest.raises(ManifestError, match="JSON"):
        MapPackageReader().read_manifest(package)


def test_read_manifest_rejects_non_object_json(work_dir: Path) -> None:
    package = work_dir / "array"
    package.mkdir()
    (package / "manifest.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ManifestError):
        MapPackageReader().read_manifest(package)


@pytest.mark.parametrize("key", ["id", "name", "mapType", "labels", "tileCount"])
def test_read_manifest_rejects_missing_field(work_dir: Path, key: str) -> None:
    data = _manifest_dict()
    data.pop(key)
    package = _write_package(work_dir / f"missing-{key}", data)
    with pytest.raises(ManifestError, match=key):
        MapPackageReader().read_manifest(package)


def test_read_manifest_rejects_missing_nested_field(work_dir: Path) -> None:
    data = _manifest_dict()
    data["mbtiles"].pop("sha256")
    package = _write_package(work_dir / "missing-sha", data)
    with pytest.raises(ManifestError, match="sha256"):
        MapPackageReader().read_manifest(package)


# --------------------------------------------------------------------------- #
# 版本
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("version", [0, -1])
def test_read_manifest_rejects_non_positive_version(work_dir: Path, version: int) -> None:
    package = _write_package(
        work_dir / f"ver{version}", _manifest_dict(formatVersion=version)
    )
    with pytest.raises(UnsupportedFormatVersion):
        MapPackageReader().read_manifest(package)


def test_read_manifest_rejects_future_version(work_dir: Path) -> None:
    """Spec §26：``formatVersion`` 超前就拒绝，绝不"尽力解析"。"""
    package = _write_package(work_dir / "ver99", _manifest_dict(formatVersion=99))
    with pytest.raises(UnsupportedFormatVersion, match="99"):
        MapPackageReader().read_manifest(package)


def test_read_manifest_rejects_non_integer_version(work_dir: Path) -> None:
    package = _write_package(
        work_dir / "ver-str", _manifest_dict(formatVersion="1")
    )
    with pytest.raises(ManifestError):
        MapPackageReader().read_manifest(package)


# --------------------------------------------------------------------------- #
# 非法取值 / 类型
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bounds",
    [
        {"west": 116.6, "south": 39.7, "east": 116.2, "north": 40.1},   # west > east
        {"west": 116.2, "south": 40.1, "east": 116.6, "north": 39.7},   # south > north
        {"west": -200.0, "south": 39.7, "east": 116.6, "north": 40.1},  # 经度越界
        {"west": 116.2, "south": 89.0, "east": 116.6, "north": 90.0},   # 纬度越界
    ],
)
def test_read_manifest_rejects_illegal_bounds(work_dir: Path, bounds: dict) -> None:
    package = _write_package(
        work_dir / f"bounds-{abs(hash(str(bounds))) % 10_000}",
        _manifest_dict(bounds=bounds),
    )
    with pytest.raises(ManifestError):
        MapPackageReader().read_manifest(package)


@pytest.mark.parametrize(
    "overrides",
    [
        {"labels": "true"},          # 字符串不是布尔
        {"tileCount": "52381"},      # 字符串不是整数
        {"minZoom": 3.5},            # 非整数缩放
        {"tileFormat": "webp2"},     # 不认识的图片格式
        {"mbtiles": {"size": -1}},   # 负的大小
        {"createdAt": "2026-09-16"},  # 没有时区
        {"createdAt": "昨天"},        # 根本不是时间
        {"id": "北京地图"},           # id 不允许中文
        {"mbtiles": {"sha256": "abcd"}},  # 不是 64 位
    ],
)
def test_read_manifest_rejects_illegal_field_values(work_dir: Path, overrides: dict) -> None:
    package = _write_package(
        work_dir / f"bad-{abs(hash(str(overrides))) % 10_000}",
        _manifest_dict(**overrides),
    )
    with pytest.raises(ManifestError):
        MapPackageReader().read_manifest(package)


def test_manifest_rejects_nan_bounds() -> None:
    """``NaN`` 会让 ``json.dumps`` 写出非法 JSON（Spec §25），必须挡在门口。"""
    with pytest.raises(ManifestError):
        MapPackageManifest(
            format_version=1,
            id="x",
            name="n",
            provider="tianditu",
            map_type="vector",
            labels=False,
            composition="single",
            tile_scheme="web_mercator",
            tile_format="png",
            min_zoom=1,
            max_zoom=2,
            west=float("nan"),
            south=0.0,
            east=1.0,
            north=1.0,
            tile_count=1,
            mbtiles_file="map.mbtiles",
            mbtiles_size=1,
            mbtiles_sha256=GOOD_SHA,
            created_at="2026-09-16T09:00:00+08:00",
        )


# --------------------------------------------------------------------------- #
# 路径穿越（Spec §11 / §29）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value",
    [
        "../x.mbtiles",
        "../../x.mbtiles",
        "sub/map.mbtiles",
        "sub\\map.mbtiles",
        "D:\\maps\\map.mbtiles",
        "C:/maps/map.mbtiles",
        "/etc/passwd",
        "..",
        ".",
    ],
)
def test_read_manifest_rejects_path_traversal(work_dir: Path, value: str) -> None:
    package = _write_package(
        work_dir / f"trav-{abs(hash(value)) % 10_000}",
        _manifest_dict(mbtiles={"file": value}),
    )
    with pytest.raises(ManifestPathError):
        MapPackageReader().read_manifest(package)


def test_manifest_path_error_is_a_manifest_error() -> None:
    """调用方只 ``except ManifestError`` 就够了（异常树是收敛的）。"""
    assert issubclass(ManifestPathError, ManifestError)


def test_ensure_inside_accepts_contained_path(work_dir: Path) -> None:
    base = work_dir / "base"
    base.mkdir()
    resolved = ensure_inside(base, base / "map.mbtiles")
    assert resolved == (base / "map.mbtiles").resolve()


def test_ensure_inside_rejects_outside_path(work_dir: Path) -> None:
    base = work_dir / "base"
    base.mkdir()
    with pytest.raises(ManifestPathError):
        ensure_inside(base, base / ".." / "outside.mbtiles")


def test_resolve_mbtiles_refuses_escaping_path(work_dir: Path) -> None:
    """即使绕过字段层校验造了一个 manifest，解析阶段也要挡住。"""
    package = work_dir / "escape"
    package.mkdir()
    manifest = MapPackageManifest(
        format_version=1,
        id="x",
        name="n",
        provider="tianditu",
        map_type="vector",
        labels=False,
        composition="single",
        tile_scheme="web_mercator",
        tile_format="png",
        min_zoom=1,
        max_zoom=2,
        west=0.0,
        south=0.0,
        east=1.0,
        north=1.0,
        tile_count=1,
        mbtiles_file="map.mbtiles",
        mbtiles_size=1,
        mbtiles_sha256=GOOD_SHA,
        created_at="2026-09-16T09:00:00+08:00",
    )
    # 模拟"字段层被绕过"（例如将来某个入口直接构造 manifest）：
    object.__setattr__(manifest, "mbtiles_file", "../outside.mbtiles")
    with pytest.raises(ManifestPathError):
        MapPackageReader().resolve_mbtiles(package, manifest)


# --------------------------------------------------------------------------- #
# 渲染 / 字段顺序
# --------------------------------------------------------------------------- #
def test_render_manifest_json_is_utf8_indented_and_ordered() -> None:
    """Spec §3：UTF-8、``indent=2``、``ensure_ascii=False``、字段顺序固定。"""
    manifest = parse_manifest_dict(_manifest_dict())
    text = render_manifest_json(manifest)

    assert "北京离线地图" in text          # ensure_ascii=False（中文不转义）
    assert text.endswith("\n")
    data = json.loads(text)
    keys = list(data)
    # 顶层顺序 = MANIFEST_FIELD_ORDER（去掉没写的可选字段）
    assert keys == [key for key in MANIFEST_FIELD_ORDER if key in data]
    assert list(data["bounds"]) == ["west", "south", "east", "north"]
    assert list(data["mbtiles"]) == ["file", "size", "sha256"]
    # 磁盘上真的是缩进两格
    assert '\n  "formatVersion": 1,' in text


def test_render_manifest_json_omits_absent_description() -> None:
    """可选字段不写 ``null``：``null`` 与"没有这个字段"在 Android 侧语义不同。"""
    data = json.loads(render_manifest_json(parse_manifest_dict(_manifest_dict())))
    assert "description" not in data


def test_render_manifest_json_has_no_absolute_path() -> None:
    """Spec §25：``mbtiles.file`` 必须是 ``map.mbtiles``，不能是 Windows 绝对路径。"""
    text = render_manifest_json(parse_manifest_dict(_manifest_dict()))
    assert "map.mbtiles" in text
    assert "\\\\" not in text
    assert "D:" not in text


def test_manifest_roundtrip_is_stable() -> None:
    """同一份数据渲染两次必须**逐字节相同**（打包脚本要能比对）。"""
    manifest = parse_manifest_dict(_manifest_dict())
    assert render_manifest_json(manifest) == render_manifest_json(manifest)
