"""``MapPackageValidator`` 的单元测试（Slice 9，Spec §12 / §13 / §14 / §30）。

这一层回答的是**唯一一个用户真正关心的问题**："这个包能不能信、有没有传坏。"
它也是最容易被写成"看起来在检查、其实什么都没挡住"的一层 —— 于是这里的用例
几乎全是**坏输入**，而且都要断言到"拒绝的理由对得上号"。

重点（Spec §30 逐条对应）：

- 改一个字节 → **必须** SHA256 FAIL。这是 Spec §35 第 11 步的手工验收
  在代码里的等价物，也是 Android 导入前判断完整性的唯一依据（Spec §14）。
  漏掉它，一个传坏了的包会一路"验证通过"直到设备上加载失败。
- 删文件（``map.mbtiles`` / ``manifest.json``）、manifest 损坏、zoom /
  tileCount / 格式 / 范围被改、库本身坏了 → 全部 FAIL。
- 非关键字段（``mapType`` / ``labels`` / ``composition`` / ``provider``）
  不一致**只记警告**：它们只影响展示与调度策略，不该把"数据完全正确、
  只是标签写错"的包判死（Spec §13）。
- 路径穿越 / 超前的 ``formatVersion`` → 拒绝：manifest 将来可能是别人给的。

所有"正常包"都用**生产同一条构建器**造出来，不是手工拼 JSON —— 手工拼的
manifest 只能证明"验证器能读手工拼的东西"，证明不了它认得我们自己的产物。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.config.settings import (
    PACKAGE_FORMAT_VERSION,
    PACKAGE_MANIFEST_FILENAME,
    PACKAGE_MBTILES_FILENAME,
)
from app.mbtiles import MBTilesDiagnosticsResult
from app.package import (
    MapPackageBuilder,
    ManifestError,
    ManifestPathError,
    UnsupportedFormatVersion,
)
from app.package.package_reader import MapPackageReader
from app.package.package_validator import MapPackageValidator
from app.services import MBTilesInspectionService

#: 复用 builder 测试里的造库夹具（同一个仓库、同一套约定，不重复实现）
from tests.test_package_builder import _make_mbtiles, _png


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _build_package(work_dir: Path, *, name: str = "beijing", **kwargs):
    """造一个**自洽**的地图包（走生产构建器），并返回构建结果。"""
    source = _make_mbtiles(work_dir / f"{name}-src.mbtiles", name=name, **kwargs)
    result = MapPackageBuilder().build(
        source, work_dir / "packages", package_name=name
    )
    assert result.success is True, result.errors
    return result


def _manifest_path(package_dir: str | Path) -> Path:
    return Path(package_dir) / PACKAGE_MANIFEST_FILENAME


def _read_manifest(package_dir: str | Path) -> dict:
    return json.loads(_manifest_path(package_dir).read_text(encoding="utf-8"))


def _write_manifest(package_dir: str | Path, data: dict) -> None:
    _manifest_path(package_dir).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _tamper_manifest(package_dir: str | Path, **overrides) -> dict:
    """改 manifest 里的字段（``mbtiles`` 子对象用 ``mbtiles__size`` 这类写法）。

    刻意**不动** ``mbtiles.size`` / ``mbtiles.sha256``：那些是用来验证
    "文件有没有被改过"的凭据，改了它们就变成另一个测试了。
    """
    data = _read_manifest(package_dir)
    for key, value in overrides.items():
        if "__" in key:
            outer, inner = key.split("__", 1)
            data[outer][inner] = value
        else:
            data[key] = value
    _write_manifest(package_dir, data)
    return data


def _flip_one_byte(path: Path, offset: int = 4200) -> None:
    """把文件里的**一个字节**取反（大小不变）。

    Spec §35 第 11 步的手工验收就是这件事。大小不变很关键：只有这样才能证明
    "大小检查通过、SHA256 仍然挡住了"。挑 4200 而不是 0，是为了不破坏
    SQLite 的页头魔数（否则会变成"文件根本不是 SQLite"的另一个场景）。
    """
    payload = bytearray(path.read_bytes())
    payload[offset] ^= 0xFF
    path.write_bytes(bytes(payload))


def _refresh_mbtiles_facts(package_dir: str | Path) -> None:
    """按**磁盘上的真实字节**重写 manifest 里的 size / sha256。

    用来构造"文件与 manifest 完全对得上、但内容本身有问题"的场景
    （例如库不是 SQLite）—— 这样才能单独测到 MBTiles 检查那一步。
    """
    payload = (Path(package_dir) / PACKAGE_MBTILES_FILENAME).read_bytes()
    _tamper_manifest(
        package_dir,
        mbtiles__size=len(payload),
        mbtiles__sha256=hashlib.sha256(payload).hexdigest(),
    )


class _SpyInspection:
    """记录被调用次数的检查服务（用来证明"该跳过的确实跳过了"）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._real = MBTilesInspectionService()

    def inspect(self, path, **kwargs) -> MBTilesDiagnosticsResult:
        self.calls.append({"path": str(path), **kwargs})
        return self._real.inspect(path, **kwargs)


# --------------------------------------------------------------------------- #
# 正常包 → PASS
# --------------------------------------------------------------------------- #
def test_valid_package_passes(work_dir: Path) -> None:
    """Spec §13：一个完整自洽的包必须 PASS，而且三行结论都是 PASS。"""
    built = _build_package(work_dir)

    result = MapPackageValidator().validate(built.package_dir)

    assert result.valid is True
    assert result.status == "PASS"
    assert result.manifest_valid is True
    assert result.mbtiles_valid is True
    assert result.checksum_valid is True
    assert result.errors == []
    assert result.warnings == [], result.warnings
    assert result.package_dir == built.package_dir
    assert Path(result.mbtiles_path).name == PACKAGE_MBTILES_FILENAME
    assert result.mbtiles_size == Path(result.mbtiles_path).stat().st_size
    assert result.actual_sha256 == built.sha256


def test_valid_package_exposes_manifest_facts(work_dir: Path) -> None:
    """Spec §21：界面上那几行信息要能从结论里取到，不用再读一遍盘。"""
    built = _build_package(work_dir)

    result = MapPackageValidator().validate(built.package_dir)

    assert result.manifest is not None
    assert result.manifest.name == "beijing"
    assert result.manifest.tile_count == 5
    assert result.manifest.zoom_text == "8~9"
    assert result.manifest.mbtiles_sha256 == built.sha256
    assert result.status_text == "PASS"
    assert result.as_lines()[0] == "结论：PASS"


# --------------------------------------------------------------------------- #
# SHA256：这是整条链路的"传坏了没有"（Spec §14 / §30）
# --------------------------------------------------------------------------- #
def test_modified_byte_fails_checksum(work_dir: Path) -> None:
    """Spec §14 / §30：改一个字节 → 必须 FAIL，而且理由写的是 SHA256 mismatch。

    这是本 Slice 里**最重要**的一条用例：它一旦失效，"传输后完整性校验"
    就是摆设，坏包会一路绿灯到设备上。
    """
    built = _build_package(work_dir)
    mbtiles = Path(built.package_dir) / PACKAGE_MBTILES_FILENAME
    size_before = mbtiles.stat().st_size
    _flip_one_byte(mbtiles)

    result = MapPackageValidator().validate(built.package_dir)

    assert mbtiles.stat().st_size == size_before, "只改字节，不该改大小"
    assert result.valid is False, "改过一个字节的包绝不能仍然是 PASS"
    assert result.status == "FAIL"
    assert result.checksum_valid is False
    assert result.manifest_valid is True, "manifest 本身是好的，问题在文件"
    assert any("SHA256" in message for message in result.errors), result.errors
    # 实际算出来的摘要必须**确实不同**（防止"报错但算的是旧值"这种假 FAIL）
    assert result.actual_sha256 != built.sha256


def test_wrong_sha256_in_manifest_fails(work_dir: Path) -> None:
    """manifest 里写了一个不一致的 SHA256 → FAIL（Spec §14）。"""
    built = _build_package(work_dir)
    _tamper_manifest(built.package_dir, mbtiles__sha256="0" * 64)

    result = MapPackageValidator().validate(built.package_dir)

    assert result.valid is False
    assert result.checksum_valid is False
    assert any("SHA256" in message for message in result.errors)


def test_size_mismatch_fails_and_skips_checksum(work_dir: Path) -> None:
    """大小先对上：大小不一致就已经是 FAIL，没必要再读遍整个文件算摘要。"""
    built = _build_package(work_dir)
    _tamper_manifest(built.package_dir, mbtiles__size=1)

    result = MapPackageValidator().validate(built.package_dir)

    assert result.valid is False
    assert any("大小不一致" in message for message in result.errors)
    assert result.actual_sha256 is None, "大小都不对时不该白读一遍 5 GB"
    assert any("跳过 SHA256" in message for message in result.warnings)


def test_checksum_can_be_skipped_explicitly(work_dir: Path) -> None:
    """``verify_checksum=False``：只有提醒、没有错误 —— "没校验"≠"校验没过"。"""
    built = _build_package(work_dir)

    result = MapPackageValidator().validate(
        built.package_dir, verify_checksum=False
    )

    assert result.valid is True
    assert result.checksum_valid is False
    assert result.errors == []
    assert any("已跳过 SHA256 校验" in message for message in result.warnings)


def test_broken_sha256_implementation_does_not_raise(work_dir: Path) -> None:
    """摘要实现自己炸了 → 变成一条 error，而不是把异常抛给后台线程。"""
    built = _build_package(work_dir)

    def boom(*_args, **_kwargs):
        raise OSError("磁盘读取错误（测试注入）")

    result = MapPackageValidator(sha256=boom).validate(built.package_dir)

    assert result.valid is False
    assert any("SHA256" in message for message in result.errors)


# --------------------------------------------------------------------------- #
# 结构缺失 / 损坏（Spec §30）
# --------------------------------------------------------------------------- #
def test_missing_package_dir_fails(work_dir: Path) -> None:
    result = MapPackageValidator().validate(work_dir / "nowhere")

    assert result.valid is False
    assert any("不存在" in message for message in result.errors)


def test_missing_mbtiles_fails(work_dir: Path) -> None:
    """Spec §30：删掉 ``map.mbtiles`` → FAIL，且错误里指出路径。"""
    built = _build_package(work_dir)
    (Path(built.package_dir) / PACKAGE_MBTILES_FILENAME).unlink()

    result = MapPackageValidator().validate(built.package_dir)

    assert result.valid is False
    assert result.manifest_valid is True
    assert result.mbtiles_valid is False
    assert any("缺少 MBTiles" in message for message in result.errors)


def test_missing_manifest_fails(work_dir: Path) -> None:
    """Spec §30：删掉 ``manifest.json`` → FAIL。"""
    built = _build_package(work_dir)
    _manifest_path(built.package_dir).unlink()

    result = MapPackageValidator().validate(built.package_dir)

    assert result.valid is False
    assert result.manifest_valid is False
    assert result.checksum_valid is False
    assert result.errors


def test_broken_manifest_json_fails(work_dir: Path) -> None:
    built = _build_package(work_dir)
    _manifest_path(built.package_dir).write_text(
        '{"formatVersion": 1,', encoding="utf-8"
    )

    result = MapPackageValidator().validate(built.package_dir)

    assert result.valid is False
    assert any("JSON" in message for message in result.errors)


def test_mbtiles_that_is_not_sqlite_fails(work_dir: Path) -> None:
    """文件与 manifest 完全对得上，但内容不是 SQLite → MBTiles 检查必须抓住。

    这里刻意先"刷新" manifest 里的 size / sha256：否则第一个错误会是
    SHA256 不符，就测不到 MBTiles 检查那一步了（同一个输入只能证明一件事）。
    """
    built = _build_package(work_dir)
    mbtiles = Path(built.package_dir) / PACKAGE_MBTILES_FILENAME
    mbtiles.write_bytes(b"definitely not a sqlite database" * 64)
    _refresh_mbtiles_facts(built.package_dir)

    result = MapPackageValidator().validate(built.package_dir)

    assert result.valid is False
    assert result.checksum_valid is True, "文件与 manifest 是自洽的"
    assert result.mbtiles_valid is False
    assert any("MBTiles" in message for message in result.errors)


# --------------------------------------------------------------------------- #
# 关键字段一致（Spec §12 最后一项 / §30）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "overrides",
    [
        {"tileCount": 1},                 # 瓦片数对不上
        {"minZoom": 7},                    # 缩放区间下界对不上
        {"maxZoom": 10},                   # 缩放区间上界对不上
        {"tileFormat": "jpg"},             # 图片格式对不上
        {"bounds": {"west": 0.0, "south": 0.0, "east": 1.0, "north": 1.0}},
    ],
)
def test_key_field_mismatch_fails(work_dir: Path, overrides: dict) -> None:
    """Spec §13 / §30：**关键字段**不一致 = FAIL（错了就一定加载不出来）。

    改的是 manifest（文件本身没动），所以 SHA256 依然对得上 —— 被抓住的
    只能是"manifest 与库里的事实不一致"这一条规则。
    """
    built = _build_package(work_dir)
    _tamper_manifest(built.package_dir, **overrides)

    result = MapPackageValidator().validate(built.package_dir)

    assert result.valid is False
    assert result.checksum_valid is True, "文件没动，摘要应当仍然正确"
    assert any("不一致" in message for message in result.errors), result.errors


@pytest.mark.parametrize(
    "overrides",
    [
        {"mapType": "image"},              # 矢量 / 影像只影响展示
        {"labels": True},
        {"composition": "merged"},
        {"provider": "other"},
    ],
)
def test_noncritical_field_mismatch_is_only_a_warning(
    work_dir: Path, overrides: dict
) -> None:
    """Spec §13：非关键字段不一致 → WARNING，结论仍是 PASS。

    它们只影响"要不要叠注记""在地图列表里算矢量还是影像"，
    不该把一个数据完全正确的包判死。
    """
    built = _build_package(work_dir)
    _tamper_manifest(built.package_dir, **overrides)

    result = MapPackageValidator().validate(built.package_dir)

    assert result.valid is True, result.errors
    assert result.status == "PASS"
    assert result.warnings, "非关键不一致必须留下提醒，不能静默"
    assert result.errors == []


def test_metadata_inside_mbtiles_is_compared_against_manifest(work_dir: Path) -> None:
    """Spec §12 最后一项：**库里的 metadata** 与 manifest 必须一致。

    这里改的是库内部的 ``metadata.format``（不是 manifest），然后把
    size / sha256 同步刷新 —— 于是校验必须先过摘要，才会撞上这条规则。
    """
    import sqlite3

    built = _build_package(work_dir)
    mbtiles = Path(built.package_dir) / PACKAGE_MBTILES_FILENAME
    conn = sqlite3.connect(str(mbtiles))
    try:
        conn.execute("UPDATE metadata SET value = 'jpg' WHERE name = 'format'")
        conn.commit()
    finally:
        conn.close()
    _refresh_mbtiles_facts(built.package_dir)

    result = MapPackageValidator().validate(built.package_dir)

    assert result.valid is False
    assert result.checksum_valid is True
    assert any("tileFormat" in message for message in result.errors), result.errors


def test_missing_optional_metadata_only_warns(work_dir: Path) -> None:
    """老文件不带 ``labels`` / ``composition`` 是常态：只提醒，不判死。"""
    import sqlite3

    built = _build_package(work_dir)
    mbtiles = Path(built.package_dir) / PACKAGE_MBTILES_FILENAME
    conn = sqlite3.connect(str(mbtiles))
    try:
        conn.execute(
            "DELETE FROM metadata WHERE name IN ('labels', 'composition', 'map_type')"
        )
        conn.commit()
    finally:
        conn.close()
    _refresh_mbtiles_facts(built.package_dir)

    result = MapPackageValidator().validate(built.package_dir)

    assert result.valid is True, result.errors
    assert any("缺少" in message for message in result.warnings), result.warnings


# --------------------------------------------------------------------------- #
# 恶意的 / 不兼容的 manifest（Spec §11 / §26）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value", ["../x.mbtiles", "../../x.mbtiles", "D:\\maps\\x.mbtiles", "sub/x.mbtiles"]
)
def test_path_traversal_is_rejected(work_dir: Path, value: str) -> None:
    """Spec §11：``mbtiles.file`` 指向包外 → 拒绝（manifest 可能是别人给的）。"""
    built = _build_package(work_dir)
    _tamper_manifest(built.package_dir, mbtiles__file=value)

    result = MapPackageValidator().validate(built.package_dir)

    assert result.valid is False
    assert result.manifest_valid is False
    assert result.checksum_valid is False


def test_future_format_version_is_rejected(work_dir: Path) -> None:
    """Spec §26：``formatVersion`` 超前 → 拒绝，绝不"尽力解析"。"""
    built = _build_package(work_dir)
    _tamper_manifest(built.package_dir, formatVersion=PACKAGE_FORMAT_VERSION + 1)

    result = MapPackageValidator().validate(built.package_dir)

    assert result.valid is False
    assert any("formatVersion" in message for message in result.errors), result.errors


# --------------------------------------------------------------------------- #
# 契约：不抛异常、能省则省、能被放弃
# --------------------------------------------------------------------------- #
def test_validate_never_raises_when_reader_explodes(work_dir: Path) -> None:
    """Spec §12：验证器**不抛异常** —— 后台线程里抛出来的异常只能变成 Traceback。"""

    class _ExplodingReader:
        def read_manifest(self, package_dir):
            raise RuntimeError("reader 炸了（测试注入）")

    result = MapPackageValidator(reader=_ExplodingReader()).validate(work_dir)

    assert result.valid is False
    assert any("读取 manifest 失败" in message for message in result.errors)


def test_injected_diagnostics_avoids_a_second_inspection(work_dir: Path) -> None:
    """构建器走的路径：**已算好的**诊断结论要能直接复用，不再查一遍。

    5 GB 的库上 ``PRAGMA quick_check`` 是分钟级，重复一次就是让用户白等。
    """
    built = _build_package(work_dir)
    mbtiles = Path(built.package_dir) / PACKAGE_MBTILES_FILENAME
    diagnostics = MBTilesInspectionService().inspect(mbtiles, check_coverage=True)
    spy = _SpyInspection()

    result = MapPackageValidator(inspection_service=spy).validate(
        built.package_dir, diagnostics=diagnostics
    )

    assert result.valid is True
    assert spy.calls == [], "传了 diagnostics 就不该再查一次"
    assert result.mbtiles_valid is True


def test_inspection_is_used_when_no_diagnostics_given(work_dir: Path) -> None:
    """不传结论时当然要自己查（否则 metadata 一致性就没法比了）。"""
    built = _build_package(work_dir)
    spy = _SpyInspection()

    result = MapPackageValidator(inspection_service=spy).validate(built.package_dir)

    assert result.valid is True
    assert len(spy.calls) == 1
    assert spy.calls[0]["path"].endswith(PACKAGE_MBTILES_FILENAME)


def test_inspecting_can_be_switched_off(work_dir: Path) -> None:
    """``inspect_mbtiles=False``：跳过 MBTiles 检查并如实说明（构建器不用它）。"""
    built = _build_package(work_dir)

    result = MapPackageValidator().validate(
        built.package_dir, inspect_mbtiles=False
    )

    assert result.valid is True
    assert result.mbtiles_valid is False
    assert any("已跳过 MBTiles 检查" in message for message in result.warnings)


def test_inspection_failure_becomes_an_error_not_an_exception(work_dir: Path) -> None:
    """检查服务自己炸了 → 一条 error，仍然不抛异常。"""

    class _ExplodingInspection:
        def inspect(self, path, **_kwargs):
            raise RuntimeError("检查服务炸了（测试注入）")

    built = _build_package(work_dir)

    result = MapPackageValidator(inspection_service=_ExplodingInspection()).validate(
        built.package_dir
    )

    assert result.valid is False
    assert result.errors


def test_validate_can_be_stopped(work_dir: Path) -> None:
    """Spec §19：关掉对话框时，算到一半的 SHA256 要能立刻收手。"""
    built = _build_package(work_dir)

    result = MapPackageValidator().validate(
        built.package_dir, should_stop=lambda: True
    )

    assert result.valid is False
    assert result.errors


# --------------------------------------------------------------------------- #
# 进度与展示（Spec §18 / §21）
# --------------------------------------------------------------------------- #
def test_progress_ends_at_100_and_covers_the_stages(work_dir: Path) -> None:
    """Spec §18 / §21：进度必须走完并停在 100，且四个阶段都报过。"""
    built = _build_package(work_dir)
    seen: list = []

    MapPackageValidator().validate(built.package_dir, progress_callback=seen.append)

    percents = [item.percent for item in seen if item.percent >= 0]
    assert percents == sorted(percents), f"进度倒退：{percents}"
    assert percents[-1] == 100
    assert {"manifest", "size", "checksum", "mbtiles", "metadata", "done"} <= {
        item.stage for item in seen
    }
    assert any("正在读取 manifest…" in item.message for item in seen)
    assert any("正在核对文件大小…" in item.message for item in seen)
    assert any("正在计算 SHA256…" in item.message for item in seen)


def test_as_lines_marks_errors_and_warnings(work_dir: Path) -> None:
    """报告文本要能一眼分清错误与警告（Spec §21 的展示与导出都用它）。"""
    built = _build_package(work_dir)
    _tamper_manifest(built.package_dir, mbtiles__sha256="0" * 64, labels=True)

    result = MapPackageValidator().validate(built.package_dir)
    text = "\n".join(result.as_lines())

    assert "结论：FAIL" in text
    assert "[ERROR]" in text
    assert "[WARNING]" in text


def test_validator_and_builder_agree_on_a_fresh_package(work_dir: Path) -> None:
    """构建器自校验说 PASS 的包，用户手动点「验证地图包」也必须 PASS。

    这两条路径用的是同一个校验器但**参数不同**（构建时 ``verify_checksum=False``），
    所以"自校验 PASS 而手动验证 FAIL"是真实存在的可能 —— 那条差异会让用户
    以为"刚生成就坏了"。这条用例把两者钉在一起。
    """
    built = _build_package(work_dir)
    assert built.validation is not None and built.validation.valid is True

    manual = MapPackageValidator().validate(built.package_dir)

    assert manual.valid is True, manual.errors
    assert manual.status == built.validation_status == "PASS"
    assert manual.actual_sha256 == built.sha256


def test_reader_and_validator_exception_types_are_distinct() -> None:
    """异常树是收敛的：调用方 ``except ManifestError`` 就能覆盖字段与路径两类问题。

    （验证器本身不抛，但 reader 是独立可用的 API —— 那条边界要守。）
    """
    assert issubclass(ManifestPathError, ManifestError)
    assert issubclass(UnsupportedFormatVersion, ManifestError)
    reader = MapPackageReader()
    assert callable(reader.read_manifest)
