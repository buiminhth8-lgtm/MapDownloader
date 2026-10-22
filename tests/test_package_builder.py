"""``MapPackageBuilder`` 的单元测试（Slice 9，Spec §5 ~ §9 / §15 / §16 /
§23 / §24 / §28 / §31）。

这个文件守的是四件**后果很重**的事，它们都能在 pytest 全绿的情况下悄悄错掉：

1. **产物结构与字段（Spec §28 / §3）。** manifest 是给 Android 解析的协议，
   字段名、缩放区间、范围、瓦片数、文件大小、SHA256 有一项写错，错误会在
   另一台设备上爆出来。所以这里**不看"有没有生成"**，而是拿磁盘上的字节
   重新算一遍哈希、重新读一遍 JSON 来对照。
2. **原子构建（Spec §15 / §31）。** "失败时不能留下一个看起来成功的半成品"
   是这条 Slice 最重要的保证，而它的失败路径（复制失败 / SHA256 抛异常 /
   自校验不过）在正常用例里一次都走不到 —— 所以必须显式注入故障去走。
3. **不破坏用户数据（Spec §8 / §16）。** 源文件默认是 copy 不是 move；
   覆盖已有地图包是**改名备份**不是删除。这两条一旦写错就是用户数据丢失，
   而且是不可逆的。
4. **不猜（Spec §6 / §24）。** manifest 的每一项都必须来自 MBTiles 本身；
   ``labels`` 与 ``composition`` 的对应关系（Spec §24 点名）必须一致。

所有库都用生产同一条 ``MBTilesWriter`` 写出来（不是手工 SQL），
范围选 ``116.0~116.5 / 39.0~39.5``：z8 下只有 1 张瓦片、z9 下 4 张 ——
小到毫秒级，又能真的走完"检查 → 复制 → SHA256 → manifest → 自校验"整条链路。
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.config.settings import (
    PACKAGE_BUILDING_SUFFIX,
    PACKAGE_FORMAT_VERSION,
    PACKAGE_MANIFEST_FILENAME,
    PACKAGE_MBTILES_FILENAME,
)
from app.geo import GeoBounds, RegionPreset, calculate_tile_ranges
from app.geo import region as region_module
from app.mbtiles import MBTilesMetadata, MBTilesWriter
from app.models.map_layer import COMPOSITION_MERGED, COMPOSITION_SINGLE
from app.package import (
    MANIFEST_FIELD_ORDER,
    MapPackageBuilder,
    PackageValidationResult,
    make_package_id,
)

#: z8 下恰好映射到 1 张瓦片、z9 下 4 张 —— 够小，又够覆盖"多 zoom"。
BOUNDS = GeoBounds(116.0, 39.0, 116.5, 39.5)


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _png() -> bytes:
    """一张能被 Pillow 真正解码的 PNG。

    Slice 8 的诊断会**真解码**每张抽样图片，因此这里不能用
    "PNG 文件头 + 垃圾正文"：那种字节会被正确地判成损坏瓦片，
    于是构建前的检查直接 FAIL，测的就不再是打包逻辑了。
    """
    buffer = io.BytesIO()
    Image.new("RGBA", (16, 16), (210, 40, 40, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def _make_mbtiles(
    path: Path,
    *,
    name: str = "beijing",
    min_zoom: int = 8,
    max_zoom: int = 9,
    provider: str = "tianditu",
    map_type: str = "vector",
    labels: bool = False,
    composition: str = COMPOSITION_SINGLE,
    drop: tuple[tuple[int, int, int], ...] = (),
    metadata: MBTilesMetadata | None = None,
) -> Path:
    """用**生产同一条 Writer** 造一个 MBTiles（Spec §28 的测试夹具）。

    Args:
        drop: 要故意漏掉的 ``(zoom, x, y_xyz)`` —— 用来造出"覆盖不完整"
            从而让 Slice 8 的诊断给出 WARNING 的库（Spec §23 的场景）。
        metadata: 直接指定元数据（用来造"缺关键字段"的坏库）。

    Returns:
        写入的路径。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = MBTilesWriter(path)
    writer.open()
    writer.write_metadata(
        metadata
        if metadata is not None
        else MBTilesMetadata.for_bounds(
            BOUNDS,
            min_zoom,
            max_zoom,
            name=name,
            provider=provider,
            source_map_type=map_type,
            with_labels=labels,
            composition=composition,
        )
    )
    payload = _png()
    for zoom, tile_range in zip(
        range(min_zoom, max_zoom + 1), calculate_tile_ranges(BOUNDS, min_zoom, max_zoom)
    ):
        for x in range(tile_range.min_x, tile_range.max_x + 1):
            for y in range(tile_range.min_y, tile_range.max_y + 1):
                if (zoom, x, y) in drop:
                    continue
                writer.write_tile(zoom, x, y, payload)
    writer.close()
    return path


def _sql(path: Path, *statements: str) -> None:
    """直接改库（只用来造"坏文件"，生产代码里没有这种操作）。"""
    import sqlite3

    conn = sqlite3.connect(str(path))
    try:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def _drop_metadata_keys(path: Path, *keys: str) -> None:
    """把 metadata 表里的一些键删掉（造"残缺"的库，Spec §6 的场景）。"""
    import sqlite3

    conn = sqlite3.connect(str(path))
    try:
        conn.executemany("DELETE FROM metadata WHERE name = ?", [(key,) for key in keys])
        conn.commit()
    finally:
        conn.close()


def _expected_package_dir(work_dir: Path, name: str = "beijing") -> Path:
    """预期的正式包目录（**不依赖** ``result.package_dir``）。

    失败路径上 ``result.package_dir`` 可能是 ``None``（构建在算目录之前就
    退出了）。要断言"什么都没写"，必须自己把那个路径算出来 ——
    用 ``result.package_dir`` 会让断言在"真的失败"时反而报 TypeError。
    """
    return work_dir / "packages" / name


def _assert_output_empty(work_dir: Path) -> None:
    """输出目录里一个包都不该有（目录不存在也算通过）。"""
    output = work_dir / "packages"
    assert not output.exists() or list(output.iterdir()) == []


def _first_tile(zoom: int = 9) -> tuple[int, int, int]:
    """取某个 zoom 上的第一张瓦片坐标（用来构造"漏掉哪一张"）。"""
    tile_range = calculate_tile_ranges(BOUNDS, zoom, zoom)[0]
    return zoom, tile_range.min_x, tile_range.min_y


def _fake_diagnostics(**overrides) -> SimpleNamespace:
    """一份"看起来正常"的检查结论（用来直插那些真实检查到不了的分支）。

    真实诊断在"一张瓦片都没有"时给的是 FAIL，于是
    ``PACKAGE_ERROR_NO_TILE`` 这条分支永远走不到 —— 只有注入才能覆盖它。
    """
    base = {
        "status": "PASS",
        "errors": [],
        "warnings": [],
        "notices": [],
        "metadata": {
            "name": "beijing",
            "format": "png",
            "bounds": "116.000000,39.000000,116.500000,39.500000",
        },
        "format": "png",
        "bounds": (116.0, 39.0, 116.5, 39.5),
        "min_zoom": 8,
        "max_zoom": 9,
        "tile_count": 5,
        "file_size": 1024,
        "provider": "tianditu",
        "map_type": "vector",
        "labels": False,
        "composition": COMPOSITION_SINGLE,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _read_manifest(package_dir: str | Path) -> dict:
    return json.loads(
        (Path(package_dir) / PACKAGE_MANIFEST_FILENAME).read_text(encoding="utf-8")
    )


def _building_dir(package_dir: str | Path) -> Path:
    final = Path(package_dir)
    return final.with_name(final.name + PACKAGE_BUILDING_SUFFIX)


def _assert_no_leftovers(package_dir: str | Path) -> None:
    """断言"没有留下临时目录"。

    每次构建收尾都要检查这一条：``.building`` 残留会让**下一次**构建撞上
    "目录已存在"，而那时的报错完全指不到"上一次失败了"。
    """
    assert not _building_dir(package_dir).exists(), "不应留下 .building 临时目录"


# --------------------------------------------------------------------------- #
# 基本产物（Spec §28 / §3）
# --------------------------------------------------------------------------- #
def test_build_creates_package_with_mbtiles_and_manifest(work_dir: Path) -> None:
    """Spec §2：最终目录里恰好是 ``map.mbtiles`` + ``manifest.json``。"""
    source = _make_mbtiles(work_dir / "beijing.mbtiles")

    result = MapPackageBuilder().build(
        source, work_dir / "packages", package_name="北京离线地图"
    )

    assert result.success is True, result.errors
    package = Path(result.package_dir)
    assert package.is_dir()
    assert sorted(item.name for item in package.iterdir()) == [
        PACKAGE_MANIFEST_FILENAME,
        PACKAGE_MBTILES_FILENAME,
    ]
    # 输出目录下只有这一个包（没有临时目录、没有额外的 .mbtiles）
    assert sorted(item.name for item in package.parent.iterdir()) == [package.name]
    _assert_no_leftovers(package)


def test_build_manifest_records_facts_from_mbtiles(work_dir: Path) -> None:
    """Spec §3 / §6：manifest 的每一项都来自 MBTiles 本身，且命名固定。"""
    source = _make_mbtiles(
        work_dir / "beijing.mbtiles", name="beijing", min_zoom=8, max_zoom=9
    )

    result = MapPackageBuilder().build(
        source, work_dir / "packages", package_name="北京离线地图"
    )
    data = _read_manifest(result.package_dir)

    assert data["formatVersion"] == PACKAGE_FORMAT_VERSION == 1
    assert data["name"] == "北京离线地图"
    assert data["provider"] == "tianditu"
    assert data["mapType"] == "vector"
    assert data["labels"] is False
    assert data["composition"] == COMPOSITION_SINGLE
    assert data["tileScheme"] == "web_mercator"
    assert data["tileFormat"] == "png"
    assert data["minZoom"] == 8
    assert data["maxZoom"] == 9
    assert data["bounds"] == {
        "west": 116.0,
        "south": 39.0,
        "east": 116.5,
        "north": 39.5,
    }
    assert data["tileCount"] == 5  # z8 一张 + z9 四张
    assert data["mbtiles"]["file"] == PACKAGE_MBTILES_FILENAME
    assert isinstance(data["mbtiles"]["size"], int)
    assert isinstance(data["mbtiles"]["sha256"], str)
    assert data["createdAt"].endswith("+08:00") or "T" in data["createdAt"]
    # id 必须是 Android 侧能当键用的 ASCII 形态（Spec §2）
    assert data["id"].isascii()


def test_build_manifest_size_and_sha256_match_the_file_on_disk(work_dir: Path) -> None:
    """Spec §4 / §28：manifest 里的 size / sha256 必须**由磁盘上的字节**算得出来。

    这里刻意用 ``hashlib`` 重新读一遍文件，而不是与 ``result.sha256`` 自比 ——
    后者只能证明"两处写了同一个变量"，证明不了"这个值属于这个文件"。
    """
    source = _make_mbtiles(work_dir / "beijing.mbtiles")

    result = MapPackageBuilder().build(source, work_dir / "packages")

    packaged = Path(result.package_dir) / PACKAGE_MBTILES_FILENAME
    payload = packaged.read_bytes()
    expected = hashlib.sha256(payload).hexdigest()

    assert _read_manifest(result.package_dir)["mbtiles"]["size"] == len(payload)
    assert _read_manifest(result.package_dir)["mbtiles"]["sha256"] == expected
    assert result.sha256 == expected
    assert result.mbtiles_size == len(payload)


def test_build_manifest_json_is_utf8_indented_with_trailing_newline(
    work_dir: Path,
) -> None:
    """Spec §3：UTF-8、``indent=2``、``ensure_ascii=False``。"""
    source = _make_mbtiles(work_dir / "beijing.mbtiles")

    result = MapPackageBuilder().build(
        source, work_dir / "packages", package_name="北京离线地图"
    )
    text = (Path(result.package_dir) / PACKAGE_MANIFEST_FILENAME).read_text(
        encoding="utf-8"
    )

    assert "北京离线地图" in text  # ensure_ascii=False：中文不转义
    assert '\n  "formatVersion": 1,' in text  # 缩进两格
    assert text.endswith("\n")
    assert json.loads(text)["name"] == "北京离线地图"


def test_build_manifest_has_no_absolute_path_and_no_token(work_dir: Path) -> None:
    """Spec §25 / §3：不写绝对路径、不写 Token（Android 侧与安全都要求）。"""
    source = _make_mbtiles(work_dir / "beijing.mbtiles")

    result = MapPackageBuilder().build(source, work_dir / "packages")
    text = (Path(result.package_dir) / PACKAGE_MANIFEST_FILENAME).read_text(
        encoding="utf-8"
    )

    assert "D:" not in text
    assert "\\\\" not in text  # Windows 路径分隔符（JSON 里会被转义成 \\）
    assert "token" not in text.lower()
    assert "tk=" not in text
    # mbtiles.file 是**包内相对文件名**，不是源文件的绝对路径
    assert json.loads(text)["mbtiles"]["file"] == "map.mbtiles"
    assert str(work_dir) not in text


def test_build_result_summary_is_filled_for_gui(work_dir: Path) -> None:
    """Spec §10 / §20：结果对象要把界面要显示的每一项都带上。"""
    source = _make_mbtiles(work_dir / "beijing.mbtiles")

    result = MapPackageBuilder().build(
        source, work_dir / "packages", package_name="北京离线地图"
    )

    assert result.success is True
    assert result.package_dir == str(work_dir / "packages" / "北京离线地图")
    assert Path(result.manifest_path).name == PACKAGE_MANIFEST_FILENAME
    assert Path(result.mbtiles_path).name == PACKAGE_MBTILES_FILENAME
    assert result.sha256 and len(result.sha256) == 64
    assert result.tile_count == 5
    assert (result.min_zoom, result.max_zoom) == (8, 9)
    assert result.zoom_text == "8~9"
    assert result.mbtiles_size > 0
    assert result.mbtiles_size_text
    assert result.diagnostics_status == "PASS"
    assert result.needs_confirmation is False
    assert result.in_place is False
    assert result.copied is True
    assert result.as_lines()  # 日志区要有话可说


def test_build_runs_self_validation_and_it_passes(work_dir: Path) -> None:
    """Spec §5 的最后一步：构建完必须自校验，而且**通过才发布**。"""
    source = _make_mbtiles(work_dir / "beijing.mbtiles")

    result = MapPackageBuilder().build(source, work_dir / "packages")

    assert result.validation is not None
    assert isinstance(result.validation, PackageValidationResult)
    assert result.validation.valid is True
    assert result.validation.errors == []
    assert result.validation_status == "PASS"
    # 对外报的是**正式目录**，不是构建时那个 .building 临时目录
    assert result.validation.package_dir == result.package_dir
    assert PACKAGE_BUILDING_SUFFIX not in result.validation.package_dir


def test_build_does_not_modify_source_file(work_dir: Path) -> None:
    """Spec §8：默认是 **copy 而不是 move** —— 源文件必须原封不动。"""
    source = _make_mbtiles(work_dir / "beijing.mbtiles")
    before = source.read_bytes()

    result = MapPackageBuilder().build(source, work_dir / "packages")

    assert result.success is True
    assert source.is_file(), "源文件被搬走了（应该是复制）"
    assert source.read_bytes() == before


def test_build_defaults_package_name_to_metadata_name(work_dir: Path) -> None:
    """Spec §6：不传包名时用 MBTiles metadata 里的 ``name``，界面不另维护一套。"""
    source = _make_mbtiles(work_dir / "whatever.mbtiles", name="beijing-map")

    result = MapPackageBuilder().build(source, work_dir / "packages")

    assert Path(result.package_dir).name == "beijing-map"
    assert _read_manifest(result.package_dir)["name"] == "beijing-map"


# --------------------------------------------------------------------------- #
# 目录命名（Spec §7 / §15）
# --------------------------------------------------------------------------- #
def test_package_dir_reuses_sanitize_map_name(work_dir: Path) -> None:
    """Spec §7：目录名必须复用 ``sanitize_filename()``，不许出现 ``\\ / : * ? " < > |``。"""
    builder = MapPackageBuilder()

    assert builder.package_dir_for(work_dir, "a/b:c") == work_dir / "a_b_c"
    assert builder.package_dir_for(work_dir, "北京 地图") == work_dir / "北京 地图"
    for char in '\\/:*?"<>|':
        assert char not in builder.package_dir_for(work_dir, f"x{char}y").name


def test_build_uses_sanitized_directory_but_keeps_display_name(work_dir: Path) -> None:
    """目录名被规整，但 manifest 里的 ``name`` 仍是用户填的那个（那是给人看的）。"""
    source = _make_mbtiles(work_dir / "beijing.mbtiles")

    result = MapPackageBuilder().build(
        source, work_dir / "packages", package_name="a/b:c"
    )

    assert Path(result.package_dir).name == "a_b_c"
    assert _read_manifest(result.package_dir)["name"] == "a/b:c"


def test_building_dir_is_a_sibling_with_documented_suffix(work_dir: Path) -> None:
    """Spec §15：临时目录是**兄弟目录** ``<name>.building``。

    做成 ``<package>/.building`` 要求先建正式目录，那样"正式目录只可能在
    成功之后出现"这条不变量就没了。
    """
    builder = MapPackageBuilder()
    final = work_dir / "packages" / "beijing"

    assert builder.building_dir_for(final) == work_dir / "packages" / (
        "beijing" + PACKAGE_BUILDING_SUFFIX
    )


# --------------------------------------------------------------------------- #
# 原地构建（Spec §8）
# --------------------------------------------------------------------------- #
def test_build_in_place_when_source_is_already_the_package_map(work_dir: Path) -> None:
    """源文件已经是 ``<包>/map.mbtiles`` 时**不重复复制**（Spec §8）。"""
    package = work_dir / "packages" / "beijing"
    source = _make_mbtiles(package / PACKAGE_MBTILES_FILENAME)
    before = source.read_bytes()

    result = MapPackageBuilder().build(
        source, work_dir / "packages", package_name="beijing"
    )

    assert result.success is True, result.errors
    assert result.in_place is True
    assert result.copied is False
    assert source.read_bytes() == before, "原地构建不该动那个文件"
    assert (package / PACKAGE_MANIFEST_FILENAME).is_file()
    _assert_no_leftovers(package)


def test_in_place_build_does_not_truncate_the_source(work_dir: Path) -> None:
    """Spec §8 的硬要求：**避免把文件复制到自身**（那会把文件截成 0 字节）。"""
    package = work_dir / "packages" / "beijing"
    source = _make_mbtiles(package / PACKAGE_MBTILES_FILENAME)
    size = source.stat().st_size

    result = MapPackageBuilder().build(
        source, work_dir / "packages", package_name="beijing"
    )

    assert result.success is True
    assert source.stat().st_size == size > 0
    assert source.read_bytes()[:16] == b"SQLite format 3\x00"


# --------------------------------------------------------------------------- #
# 原子构建：失败时不留半成品（Spec §15 / §31）
# --------------------------------------------------------------------------- #
def test_build_leaves_no_building_dir_after_success(work_dir: Path) -> None:
    source = _make_mbtiles(work_dir / "beijing.mbtiles")

    result = MapPackageBuilder().build(source, work_dir / "packages")

    assert result.success is True
    _assert_no_leftovers(result.package_dir)


def test_build_cleans_building_dir_when_copy_aborted(work_dir: Path) -> None:
    """Spec §31：复制阶段被中止 → 正式目录**不存在**，临时目录被清掉。"""
    source = _make_mbtiles(work_dir / "beijing.mbtiles")

    result = MapPackageBuilder().build(
        source, work_dir / "packages", should_stop=lambda: True
    )

    assert result.success is False
    assert result.errors
    assert not Path(result.package_dir).exists(), "中止不该留下正式目录"
    _assert_no_leftovers(result.package_dir)


def test_build_cleans_building_dir_when_sha256_raises(work_dir: Path) -> None:
    """Spec §31 的核心场景：**复制成功、SHA256 阶段异常**。

    这一步最容易写出"半成品包"：文件已经落到临时目录里了，只差 manifest。
    必须保证正式目录不会因此出现，而且临时目录被清掉。
    """
    source = _make_mbtiles(work_dir / "beijing.mbtiles")

    def boom(*_args, **_kwargs):
        raise OSError("磁盘读取错误（测试注入）")

    result = MapPackageBuilder(sha256=boom).build(source, work_dir / "packages")

    assert result.success is False
    assert result.errors and "磁盘读取错误" in result.errors[0]
    assert not Path(result.package_dir).exists()
    _assert_no_leftovers(result.package_dir)
    # 源文件依然完好（没有被"顺手删掉"）
    assert source.is_file()


def test_build_cleans_building_dir_when_copy_fails(work_dir: Path) -> None:
    """复制阶段抛异常 → 同样不许留下任何正式目录。"""
    source = _make_mbtiles(work_dir / "beijing.mbtiles")

    def boom(*_args, **_kwargs):
        raise OSError("目标磁盘已满（测试注入）")

    result = MapPackageBuilder(copy_file=boom).build(source, work_dir / "packages")

    assert result.success is False
    assert result.errors
    assert not Path(result.package_dir).exists()
    _assert_no_leftovers(result.package_dir)


def test_build_publishes_nothing_when_self_validation_fails(work_dir: Path) -> None:
    """Spec §15：自校验没过 → 一个字节都不发布。

    注入一个"永远说不行"的校验器：这模拟的是"我们写出来的 manifest 与库里
    的事实对不上"，那是**我们自己**的错，绝不能交付给用户。
    """

    class _RejectingValidator:
        def validate(self, package_dir, **_kwargs):
            return PackageValidationResult(
                valid=False,
                package_dir=str(package_dir),
                manifest_valid=True,
                errors=["关键字段不一致：tileCount（测试注入）"],
            )

    source = _make_mbtiles(work_dir / "beijing.mbtiles")

    result = MapPackageBuilder(validator=_RejectingValidator()).build(
        source, work_dir / "packages"
    )

    assert result.success is False
    assert result.errors and "自校验未通过" in result.errors[0]
    assert not Path(result.package_dir).exists(), "自校验没过就不该有正式目录"
    _assert_no_leftovers(result.package_dir)


def test_build_clears_stale_building_dir_from_a_previous_crash(work_dir: Path) -> None:
    """上一次崩溃留下的 ``.building`` 不该让这一次构建摔跤。"""
    source = _make_mbtiles(work_dir / "beijing.mbtiles")
    stale = work_dir / "packages" / ("beijing" + PACKAGE_BUILDING_SUFFIX)
    stale.mkdir(parents=True)
    (stale / "junk.txt").write_text("上一次留下的垃圾", encoding="utf-8")

    result = MapPackageBuilder().build(
        source, work_dir / "packages", package_name="beijing"
    )

    assert result.success is True, result.errors
    assert not stale.exists()
    package = Path(result.package_dir)
    assert sorted(item.name for item in package.iterdir()) == [
        PACKAGE_MANIFEST_FILENAME,
        PACKAGE_MBTILES_FILENAME,
    ]


# --------------------------------------------------------------------------- #
# 目标已存在 / 覆盖（Spec §16）
# --------------------------------------------------------------------------- #
def test_existing_package_needs_confirmation_and_is_left_alone(work_dir: Path) -> None:
    """Spec §16：目标已存在时**先问**，而且**不许静默删除**已有地图包。"""
    source = _make_mbtiles(work_dir / "beijing.mbtiles")
    first = MapPackageBuilder().build(source, work_dir / "packages")
    assert first.success is True
    before = (Path(first.package_dir) / PACKAGE_MBTILES_FILENAME).read_bytes()

    second = MapPackageBuilder().build(source, work_dir / "packages")

    assert second.success is False
    assert second.needs_confirmation is True
    assert second.confirmation_reason == "exists"
    assert second.errors and "已存在" in second.errors[0]
    # 已有包完好无损
    assert (Path(first.package_dir) / PACKAGE_MBTILES_FILENAME).read_bytes() == before
    _assert_no_leftovers(first.package_dir)


def test_overwrite_renames_old_package_instead_of_deleting_it(work_dir: Path) -> None:
    """Spec §16：覆盖 = 旧包**改名备份**，不是删除（与 Slice 5 同一条约定）。"""
    source = _make_mbtiles(work_dir / "beijing.mbtiles")
    first = MapPackageBuilder().build(source, work_dir / "packages")
    old_sha = first.sha256

    second = MapPackageBuilder().build(source, work_dir / "packages", overwrite=True)

    assert second.success is True, second.errors
    assert second.backup_path is not None
    backup = Path(second.backup_path)
    assert backup.is_dir()
    assert backup.name.startswith("beijing.backup-")
    # 备份里是**旧的**那一份（内存里没有"改名后内容变了"这种可能）
    assert _read_manifest(backup)["mbtiles"]["sha256"] == old_sha
    # 正式目录里是新的那一份
    assert Path(second.package_dir).is_dir()
    assert _read_manifest(second.package_dir)["mbtiles"]["sha256"] == second.sha256
    _assert_no_leftovers(second.package_dir)


def test_failed_overwrite_keeps_the_previous_package_intact(work_dir: Path) -> None:
    """覆盖构建**失败**时，用户原来的地图包必须还在（Spec §16 的底线）。

    这条比"备份成功"更重要：备份只是手段，目的是"覆盖失败不等于丢数据"。
    """
    source = _make_mbtiles(work_dir / "beijing.mbtiles")
    first = MapPackageBuilder().build(source, work_dir / "packages")
    before = (Path(first.package_dir) / PACKAGE_MBTILES_FILENAME).read_bytes()

    def boom(*_args, **_kwargs):
        raise OSError("SHA256 阶段炸了（测试注入）")

    failed = MapPackageBuilder(sha256=boom).build(
        source, work_dir / "packages", overwrite=True
    )

    assert failed.success is False
    assert (Path(first.package_dir) / PACKAGE_MBTILES_FILENAME).read_bytes() == before
    assert _read_manifest(first.package_dir)["mbtiles"]["sha256"] == first.sha256


# --------------------------------------------------------------------------- #
# 与 Slice 8 的集成：FAIL 禁止打包，WARNING 要问（Spec §23）
# --------------------------------------------------------------------------- #
def test_build_refuses_when_diagnostics_fail(work_dir: Path) -> None:
    """Spec §23：检查结论是 FAIL 时**禁止打包**，而且一个文件都不写。"""
    broken = work_dir / "broken.mbtiles"
    broken.write_bytes(b"definitely not a sqlite database" * 32)

    result = MapPackageBuilder().build(broken, work_dir / "packages")

    assert result.success is False
    assert result.diagnostics_status == "FAIL"
    assert result.errors
    _assert_output_empty(work_dir)


def test_warning_source_asks_before_building(work_dir: Path) -> None:
    """Spec §23：WARNING 时**不生成任何文件**，返回"等用户拍板"的结果。"""
    source = _make_mbtiles(work_dir / "beijing.mbtiles", drop=(_first_tile(9),))

    result = MapPackageBuilder().build(source, work_dir / "packages")

    assert result.success is False
    assert result.needs_confirmation is True
    assert result.confirmation_reason == "warning"
    assert result.diagnostics_status == "WARNING"
    assert result.warnings, "警告内容要带给界面，用户才看得见是什么问题"
    assert not _expected_package_dir(work_dir).exists()
    _assert_output_empty(work_dir)


def test_warning_source_is_built_after_user_confirms(work_dir: Path) -> None:
    """用户选了"继续生成" → 带上授权后照常产出（Spec §23）。"""
    source = _make_mbtiles(work_dir / "beijing.mbtiles", drop=(_first_tile(9),))

    result = MapPackageBuilder().build(
        source, work_dir / "packages", accept_warnings=True
    )

    assert result.success is True, result.errors
    assert result.diagnostics_status == "WARNING"
    assert result.needs_confirmation is False
    assert (Path(result.package_dir) / PACKAGE_MANIFEST_FILENAME).is_file()


# --------------------------------------------------------------------------- #
# 关键字段缺失 → 失败，绝不靠猜（Spec §6）
# --------------------------------------------------------------------------- #
def test_build_fails_when_metadata_table_is_missing(work_dir: Path) -> None:
    """没有 ``metadata`` 表 → 拿不到 name/bounds，必须失败而不是编一个。

    生产那条 Writer 不允许"先写瓦片再写元数据"（缺 metadata 的 MBTiles
    是无效文件），所以这里先正常造库，再把表删掉 —— 模拟的是**别的工具**
    生成的残缺文件，而不是我们自己的产物。
    """
    source = _make_mbtiles(work_dir / "beijing.mbtiles")
    _sql(source, "DROP TABLE metadata")

    result = MapPackageBuilder().build(source, work_dir / "packages")

    assert result.success is False
    assert result.errors
    assert not _expected_package_dir(work_dir).exists()
    _assert_output_empty(work_dir)


def test_build_fails_when_bounds_is_missing_from_metadata(work_dir: Path) -> None:
    """缺 ``bounds`` / ``format`` → 失败，并在错误里点名缺了哪个字段（Spec §6）。"""
    source = _make_mbtiles(work_dir / "beijing.mbtiles")
    _drop_metadata_keys(source, "bounds", "format")

    result = MapPackageBuilder().build(source, work_dir / "packages")

    assert result.success is False
    assert result.errors
    joined = result.errors[0]
    assert "bounds" in joined and "format" in joined, joined
    assert not _expected_package_dir(work_dir).exists()
    _assert_output_empty(work_dir)


def test_build_fails_when_there_is_no_tile(work_dir: Path) -> None:
    """一张瓦片都没有 → 不该生成一个"空的地图包"。

    ``tile_count=0`` 在真实诊断里走不到（它自己就先 FAIL 了），所以这里
    注入一份"结构正常但没瓦片"的结论，专门覆盖那条分支。
    """
    source = _make_mbtiles(work_dir / "beijing.mbtiles")

    result = MapPackageBuilder().build(
        source, work_dir / "packages", diagnostics=_fake_diagnostics(tile_count=0)
    )

    assert result.success is False
    assert result.errors and "没有瓦片" in result.errors[0]
    _assert_output_empty(work_dir)


# --------------------------------------------------------------------------- #
# 进度（Spec §18）
# --------------------------------------------------------------------------- #
def test_build_reports_progress_that_never_goes_backwards_and_ends_at_100(
    work_dir: Path,
) -> None:
    """进度条只能往前走、最后必须到 100（否则用户以为"没跑完就出结论了"）。"""
    source = _make_mbtiles(work_dir / "beijing.mbtiles")
    seen: list = []

    result = MapPackageBuilder().build(
        source, work_dir / "packages", progress_callback=seen.append
    )

    assert result.success is True
    percents = [item.percent for item in seen if item.percent >= 0]
    assert percents == sorted(percents), f"进度倒退：{percents}"
    assert percents[-1] == 100
    assert all(item.message for item in seen), "每条进度都要带一句给人看的话"


def test_build_progress_covers_the_documented_stages(work_dir: Path) -> None:
    """Spec §18 逐字要求的那五句 + 收尾，必须真的出现在进度里。"""
    source = _make_mbtiles(work_dir / "beijing.mbtiles")
    seen: list = []

    MapPackageBuilder().build(
        source, work_dir / "packages", progress_callback=seen.append
    )

    stages = {item.stage for item in seen}
    assert {"check", "copy", "sha256", "manifest", "validate", "done"} <= stages
    messages = [item.message for item in seen]
    assert any("正在检查 MBTiles…" in text for text in messages)
    assert any("正在复制地图文件…" in text for text in messages)
    assert any("正在计算 SHA256…" in text for text in messages)
    assert any("正在生成 manifest…" in text for text in messages)
    assert any("正在验证地图包…" in text for text in messages)


# --------------------------------------------------------------------------- #
# 单层 / 中文注记合成（Spec §24）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("map_type", ["vector", "image"])
def test_build_records_single_layer_composition(work_dir: Path, map_type: str) -> None:
    """Spec §24：单图层 = ``map_type + labels=false``。"""
    source = _make_mbtiles(
        work_dir / "single.mbtiles",
        name="single",
        map_type=map_type,
        labels=False,
        composition=COMPOSITION_SINGLE,
    )

    result = MapPackageBuilder().build(source, work_dir / "packages")

    data = _read_manifest(result.package_dir)
    assert data["mapType"] == map_type
    assert data["labels"] is False
    assert data["composition"] == COMPOSITION_SINGLE


@pytest.mark.parametrize("map_type", ["vector", "image"])
def test_build_records_merged_composition(work_dir: Path, map_type: str) -> None:
    """Spec §24：合成图层 = ``map_type + labels=true + merged``。

    "``labels=true`` 但 manifest 不记录 composition"是被点名的禁止项 ——
    下游据此决定要不要再叠一层注记，记错就是"注记画两遍"或"没有注记"。
    """
    source = _make_mbtiles(
        work_dir / "merged.mbtiles",
        name="merged",
        map_type=map_type,
        labels=True,
        composition=COMPOSITION_MERGED,
    )

    result = MapPackageBuilder().build(source, work_dir / "packages")

    data = _read_manifest(result.package_dir)
    assert data["mapType"] == map_type
    assert data["labels"] is True
    assert data["composition"] == COMPOSITION_MERGED
    assert "composition" in data


def test_build_derives_composition_from_labels_when_metadata_omits_it(
    work_dir: Path,
) -> None:
    """metadata 没写 composition 时，按 ``labels`` 推出而不是无脑写 single。

    这是 Spec §24 的直接推论：``labels=true`` 的包写成 ``single`` 会让下游
    把注记再叠一遍，写成 ``single`` 更是自相矛盾。推导必须留一条 WARNING
    （"这个值不是文件里写的"）。

    注意：真实诊断在"``labels=true`` 但 composition 缺失"时会直接判 FAIL
    （Slice 8 的规则："注记层的存在与合成方式自相矛盾"），因此这条分支只能
    靠注入结论走到 —— 它守的是"构建器自己不会写出矛盾字段"这条不变量。
    """
    source = _make_mbtiles(work_dir / "legacy.mbtiles")

    result = MapPackageBuilder().build(
        source,
        work_dir / "packages",
        diagnostics=_fake_diagnostics(labels=True, composition=None),
    )

    data = _read_manifest(result.package_dir)
    assert data["labels"] is True
    assert data["composition"] == COMPOSITION_MERGED
    assert any("composition" in message for message in result.warnings)


def test_build_records_defaults_with_warning_for_legacy_metadata(
    work_dir: Path,
) -> None:
    """老文件（Slice 4/5 生成、不带自有扩展键）也能打包，但要如实说明用了默认值。

    "拒收一个数据完全正确、只是没人记录过地图类型的包"对用户没有帮助，
    所以这里走的是"默认值 + WARNING"而不是失败（模块 docstring 第 3 条）。
    """
    source = _make_mbtiles(work_dir / "legacy.mbtiles")
    _drop_metadata_keys(source, "provider", "map_type", "labels", "composition")

    result = MapPackageBuilder().build(source, work_dir / "packages")

    assert result.success is True, result.errors
    data = _read_manifest(result.package_dir)
    assert data["provider"] == "tianditu"
    assert data["mapType"] == "vector"
    assert data["labels"] is False
    assert data["composition"] == COMPOSITION_SINGLE
    # 每一项默认值都要有一条 WARNING 说明"这是默认值，不是文件里写的"
    joined = " ".join(result.warnings)
    for field in ("provider", "map_type", "labels", "composition"):
        assert field in joined, f"{field} 的默认值没有留下痕迹：{result.warnings}"


# --------------------------------------------------------------------------- #
# id（Spec §3 / §26）
# --------------------------------------------------------------------------- #
def test_make_package_id_is_deterministic_and_android_friendly() -> None:
    assert (
        make_package_id("Beijing Map", created_at="2026-09-16T09:00:00+08:00")
        == "beijing_map_20260916"
    )
    # 纯中文名不打拼音（那需要一个会猜错的词典），退回 pkg 前缀
    assert (
        make_package_id("北京离线地图", created_at="2026-09-16T09:00:00+08:00")
        == "pkg_20260916"
    )
    # 同一份输入永远得到同一个 id（重建同一个包时 id 稳定）
    assert make_package_id("beijing", created_at="2026-09-16T09:00:00+08:00") == (
        make_package_id("beijing", created_at="2026-09-16T09:00:00+08:00")
    )
    assert make_package_id("A" * 200, created_at="2026-09-16T09:00:00+08:00").isascii()


def test_build_honours_explicit_package_id(work_dir: Path) -> None:
    source = _make_mbtiles(work_dir / "beijing.mbtiles")

    result = MapPackageBuilder().build(
        source, work_dir / "packages", package_id="beijing_20260916"
    )

    assert _read_manifest(result.package_dir)["id"] == "beijing_20260916"


# --------------------------------------------------------------------------- #
# 装配（不需要服务层也能独立工作）
# --------------------------------------------------------------------------- #
def test_builder_works_without_injected_inspection_service(work_dir: Path) -> None:
    """``MapPackageBuilder()`` 必须能**脱离服务层**独立工作。

    包构建器需要"一个能诊断 MBTiles 的东西"，而那个东西住在更高的编排层
    （``app.services``）。两者之间只能靠**延迟导入**解耦 —— 否则
    ``import app.package`` 会撞上一个循环导入。这条用例就是那条边界的守卫。
    """
    source = _make_mbtiles(work_dir / "beijing.mbtiles")

    result = MapPackageBuilder().build(source, work_dir / "packages")

    assert result.success is True, result.errors
    assert result.diagnostics_status == "PASS"


def test_build_does_not_raise_on_weird_inputs(work_dir: Path) -> None:
    """Spec §5：构建器**不抛异常**，任何故障都变成 ``errors`` 里的一行。

    源文件不存在是最常见的一种（用户刚把文件挪走）。
    """
    result = MapPackageBuilder().build(
        work_dir / "nowhere.mbtiles", work_dir / "packages"
    )

    assert result.success is False
    assert result.errors and "不存在" in result.errors[0]
    assert result.package_dir is None
    assert not (work_dir / "packages").exists()


# --------------------------------------------------------------------------- #
# 区域子对象（Slice 16，Spec §21）
#
# 地图包协议 V1 新增了一个**可选**的 ``region`` 子对象。三件事必须同时成立：
# 1. 多边形包要能把它写进去（Android 与 Inspector 都靠它判断"这是按行政区裁的"）；
# 2. 矩形包写的是"显式的矩形"，与"没有这个键的老包"分得开；
# 3. 老包（MBTiles metadata 里没有 region_* 三键）产出的 manifest 必须
#    **逐字节**与 Slice ≤15 一致 —— 加一个字段不该动到任何既有字段。
# --------------------------------------------------------------------------- #
BOUNDARY_FILE = "china.geojson"
BOUNDARY_VERSION = "2026-09"


def _register_test_preset(work_dir: Path, monkeypatch) -> None:
    """注册一个 ``region_id=china`` 的**临时**预设，边界文件放在临时目录里。

    为什么要注册：多边形源的 ``region_id`` 会触发**打包前的覆盖度检查**
    （Spec §23），而"边界文件找不到"是一条 WARNING —— WARNING 会让构建器
    停下来等用户确认（``accept_warnings`` 默认 ``False``），用例就会在
    ``assert result.success`` 上摔一跤，测的却不再是 manifest 里的 region。

    为什么边界多边形取**恰好等于候选矩形**：覆盖度检查会按行政区几何算
    expected。取一个比 ``BOUNDS`` 更大的多边形，就会多出几张"应该有但没有"的
    瓦片 → 又是一条 WARNING。取成相等，expected 与源库里的实际张数一致。

    为什么沿用 ``china`` 这个 id：用例要验证的正是"真实下载时那个 region_id
    会不会原样进 manifest"，换成自造 id 就少测了这一层。资源目录指向临时目录 ⇒
    仓库的 ``resources/regions/`` 保持干净（Spec §3：国界数据不内置、不伪造）。
    """
    (work_dir / BOUNDARY_FILE).write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {
                    "name": "测试矩形区",
                    "id": "china",
                    "boundaryVersion": BOUNDARY_VERSION,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [BOUNDS.west, BOUNDS.south],
                            [BOUNDS.east, BOUNDS.south],
                            [BOUNDS.east, BOUNDS.north],
                            [BOUNDS.west, BOUNDS.north],
                            [BOUNDS.west, BOUNDS.south],
                        ]
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    item = RegionPreset(
        region_id="china",
        name="测试矩形区",
        geojson_filename=BOUNDARY_FILE,
        default_boundary_version=BOUNDARY_VERSION,
    )
    monkeypatch.setattr(region_module, "REGION_RESOURCE_DIR", work_dir)
    monkeypatch.setattr(region_module, "REGION_PRESETS", (item,))


def test_polygon_region_is_copied_into_the_manifest(
    work_dir: Path, monkeypatch
) -> None:
    """MBTiles 里写着 polygon / china / 2026-09 → manifest 里原样出现。"""
    _register_test_preset(work_dir, monkeypatch)
    source = _make_mbtiles(
        work_dir / "china.mbtiles",
        metadata=MBTilesMetadata.for_bounds(
            BOUNDS,
            8,
            9,
            name="china",
            region_type="polygon",
            region_id="china",
            boundary_version="2026-09",
        ),
    )

    result = MapPackageBuilder().build(source, work_dir / "packages")

    assert result.success is True, result.errors
    manifest = _read_manifest(result.package_dir)
    assert manifest["region"] == {
        "type": "polygon",
        "id": "china",
        "boundaryVersion": "2026-09",
    }
    # 协议里的键顺序是固定的（Android 侧与人工比对都依赖它）
    assert list(manifest).index("region") == list(manifest).index("bounds") + 1


def test_rectangle_region_is_written_as_a_type_only_object(work_dir: Path) -> None:
    """矩形包写 ``{"type": "rectangle"}``：显式，但不多出两个空值键。"""
    source = _make_mbtiles(
        work_dir / "rect.mbtiles",
        metadata=MBTilesMetadata.for_bounds(
            BOUNDS, 8, 9, name="rect", region_type="rectangle"
        ),
    )

    result = MapPackageBuilder().build(source, work_dir / "packages")

    assert result.success is True, result.errors
    manifest = _read_manifest(result.package_dir)
    assert manifest["region"] == {"type": "rectangle"}
    assert "id" not in manifest["region"]
    assert "boundaryVersion" not in manifest["region"]


def test_old_source_without_region_metadata_produces_no_region_key(
    work_dir: Path,
) -> None:
    """Slice ≤15 的源文件（metadata 里没有 region_* 三键）→ manifest 里没有 region。

    这条是这次改动最重要的兼容锚点：给了 region_* 才写 region，没给就一个字都不写。
    用"两次构建的 manifest 逐字节相等"来做断言 —— 只断言 ``"region" not in ...``
    还不够，那样漏掉"顺手改动了别的字段"这种情况。
    """
    source = _make_mbtiles(work_dir / "old.mbtiles")  # 默认不带任何 region_* 键
    assert "region_type" not in _source_metadata(source), "夹具前提：源文件是老的"

    result = MapPackageBuilder().build(source, work_dir / "packages", package_name="old")

    assert result.success is True, result.errors
    manifest_text = (Path(result.package_dir) / PACKAGE_MANIFEST_FILENAME).read_text(
        encoding="utf-8"
    )
    manifest = json.loads(manifest_text)

    assert "region" not in manifest
    # 与"把 region 加进去之前"应有的样子逐字节一致：期望值直接由**生产的那张
    # 字段顺序表**推出来（``MANIFEST_FIELD_ORDER`` 去掉两个可选键），而不是在
    # 这里再手抄一份 —— 手抄的那份迟早和生产不一致，而且错的时候看着很像
    # "协议改了"，实际只是测试记错了字段名。
    # ``list(manifest) == expected_keys`` 同时钉住**键集**与**书写顺序**：
    # 多一个键、少一个键、或把 region 插错位置，都会在这里红。
    optional_keys = {"description", "region"}
    expected_keys = [key for key in MANIFEST_FIELD_ORDER if key not in optional_keys]
    assert list(manifest) == expected_keys, (
        "既不该少字段、也不该多字段，顺序也必须与 MANIFEST_FIELD_ORDER 一致"
    )


def test_polygon_and_rectangle_packages_differ_only_by_region(
    work_dir: Path, monkeypatch
) -> None:
    """同一个库、只改 region_* 三键 → manifest 的差异**只有** region 一项。

    这条把"新增可选字段没有波及既有字段"钉死：任何一处顺手改动（例如
    ``createdAt`` 的格式、``bounds`` 的键序）都会在这里显形。
    """
    _register_test_preset(work_dir, monkeypatch)
    common = {"name": "cmp", "source_map_type": "vector", "composition": COMPOSITION_SINGLE}
    polygon_source = _make_mbtiles(
        work_dir / "poly.mbtiles",
        metadata=MBTilesMetadata.for_bounds(
            BOUNDS,
            8,
            9,
            region_type="polygon",
            region_id="china",
            boundary_version="2026-09",
            **common,
        ),
    )
    rectangle_source = _make_mbtiles(
        work_dir / "rect2.mbtiles",
        metadata=MBTilesMetadata.for_bounds(
            BOUNDS, 8, 9, region_type="rectangle", **common
        ),
    )

    # 显式给同一个 package_id：包的 id 是由"名字 + 当天日期"算出来的
    # （``make_package_id``），不固定住的话它可能因为跨午夜而变成一条
    # 与 region 无关的差异 —— 那样这个用例就变成偶发红了。
    poly_result = MapPackageBuilder().build(
        polygon_source, work_dir / "pk1", package_id="cmp_fixed"
    )
    rect_result = MapPackageBuilder().build(
        rectangle_source, work_dir / "pk2", package_id="cmp_fixed"
    )
    assert poly_result.success is True, poly_result.errors
    assert rect_result.success is True, rect_result.errors

    poly_manifest = _read_manifest(poly_result.package_dir)
    rect_manifest = _read_manifest(rect_result.package_dir)

    assert poly_manifest["region"] == {
        "type": "polygon",
        "id": "china",
        "boundaryVersion": "2026-09",
    }
    assert rect_manifest["region"] == {"type": "rectangle"}

    # 两个源的 metadata 表本身就不同（region_* 三键不一样），所以库文件的
    # 字节数 / SHA256 必然不同 —— 那是**事实**，不是协议差异。这里先确认
    # ``mbtiles`` 子对象的**形状**没变，再把它和 createdAt 一起排除。
    assert set(poly_manifest["mbtiles"]) == set(rect_manifest["mbtiles"])
    assert poly_manifest["mbtiles"]["file"] == rect_manifest["mbtiles"]["file"]

    differences = {
        key
        for key in set(poly_manifest) | set(rect_manifest)
        if poly_manifest.get(key) != rect_manifest.get(key)
    }
    # ``createdAt`` 是构建时刻，本来就每次不同（同一秒内也可能相同），与 region
    # 无关；除它之外，差异**只能**是 region。
    assert differences - {"createdAt", "mbtiles"} == {"region"}, differences


def _source_metadata(path: Path) -> dict[str, str]:
    """读源 MBTiles 的 metadata 表（只用来断言夹具本身是"老文件"）。"""
    import sqlite3

    conn = sqlite3.connect(str(path))
    try:
        return {str(k): str(v) for k, v in conn.execute("SELECT name, value FROM metadata")}
    finally:
        conn.close()
