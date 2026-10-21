"""pytest 共享夹具。

关键点：必须在导入 PySide6 之前把 ``QT_QPA_PLATFORM`` 设为 ``offscreen``，
这样 CI / 无显示环境下也能装配 QWidget。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.main import silence_noisy_http_loggers  # noqa: E402
from app.ui.preferences import UiPreferences  # noqa: E402

#: 与生产启动保持一致：把 httpx / httpcore 压到 WARNING。
#:
#: 不只是"少点噪音"——httpx 的 INFO 日志会把**带 ``tk=`` 的完整 URL** 打出来
#: （Slice 2 的硬性禁止项）。生产里 ``app.main`` 会关掉它，测试里如果不管，
#: 任何一处 ``caplog`` / 失败报告都可能把假 Token 抄进产物里。
silence_noisy_http_loggers()


@pytest.fixture(scope="session")
def qapp():
    """整个测试会话共用一个 QApplication 实例。"""
    app = QApplication.instance()
    created = app is None
    if created:
        app = QApplication([])
    yield app
    if created:
        app.quit()


@pytest.fixture
def work_dir() -> Path:
    """每个用例一个干净的可写临时目录。

    刻意**不用** pytest 自带的 ``tmp_path``：本机的 ``pytest-of-<user>`` 根目录
    会被沙箱拦在 ``PermissionError: [WinError 5]`` 上，导致所有用到它的用例直接
    ERROR。``tempfile.mkdtemp()`` 走的是另一条路径，可以正常创建。

    Yields:
        临时目录路径；用例结束后尽力清理。
    """
    path = Path(tempfile.mkdtemp(prefix="mbtiles-test-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def tasks_file() -> Path:
    """任务记录文件（Slice 5 ``tasks.json``）的独立临时路径。

    刻意**不**放进 ``work_dir``：好多个用例会断言 ``work_dir`` 里"只有那个
    .mbtiles"，任务记录要是也写进去就会把那些断言搅黄。

    Yields:
        形如 ``<临时目录>/tasks.json`` 的路径。
    """
    directory = Path(tempfile.mkdtemp(prefix="tasks-test-"))
    try:
        yield directory / "tasks.json"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def preferences():
    """界面偏好（Slice 7）的**隔离**实例：写到临时 ini，而不是注册表。

    这不是"测起来方便"的问题。生产里 ``UiPreferences()`` 会落到
    ``HKEY_CURRENT_USER\\Software\\MapDownloader\\<APP_NAME>``，而主窗口在
    ``closeEvent`` 里会 ``save_geometry`` + ``set_output_dir``。测试只要用默认
    实例，一个用例点过「开始下载」，下一个用例装配窗口时就会**恢复**上一个
    用例的形状与地图类型：窗口变成 900 宽、下拉框变成"影像"、Zoom 变成 12——
    于是断言默认值的用例开始成片地红，而且失败信息会指向"业务逻辑坏了"，
    完全看不出真凶是注册表。

    落点改到临时 ini 之后，每个用例都是一张白纸，注册表也不再被污染。

    Yields:
        ``UiPreferences`` 实例，底层是临时目录里的 IniFormat ``QSettings``。
    """
    from PySide6.QtCore import QSettings  # noqa: PLC0415 - 与 qapp 同层延迟导入

    directory = Path(tempfile.mkdtemp(prefix="prefs-test-"))
    settings = QSettings(str(directory / "ui.ini"), QSettings.Format.IniFormat)
    try:
        yield UiPreferences(settings)
    finally:
        settings.clear()
        shutil.rmtree(directory, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 行政区域目录（Slice 18）
# --------------------------------------------------------------------------- #
#: 合成三层目录的**只读**原件：``tests/fixtures/regions/catalog/``。
#:
#: 顶层是 ``index.json``，边界文件按 ``country/`` / ``province/`` / ``city/``
#: 三棵子树摆放 —— 与产品资源目录（``resources/regions/``）结构完全一致。
#: 里面的多边形是**手写的方块**，不是任何真实行政区边界（Spec §8）。
REGION_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "regions" / "catalog"


@pytest.fixture(scope="session")
def region_fixture_root() -> Path:
    """合成目录的只读原件（用例只读它，不往里写东西）。"""
    return REGION_FIXTURE_ROOT


@pytest.fixture
def region_root(work_dir: Path) -> Path:
    """合成目录在工作区里的**可写副本**。

    为什么要副本而不是直接用原件：好几条用例需要"把某个边界文件删掉/弄坏"
    （测缺失与损坏的分支），往仓库里的夹具上动手会污染后续用例，
    而这类问题只在全量跑、且执行顺序恰好不对时才现形 —— 最难查的一种。
    """
    target = work_dir / "regions"
    shutil.copytree(REGION_FIXTURE_ROOT, target)
    return target


@pytest.fixture
def undeployed_region_root(work_dir: Path, monkeypatch) -> Path:
    """一个**空的**行政区目录：模拟"还没部署边界数据"的机器。

    为什么需要它：``resources/regions/`` 在开发/验收机上可能被部署方放进真实的
    授权边界数据（仓库本身不带任何边界文件，见 ``app.config.settings`` 里
    ``REGION_RESOURCE_DIR`` 的说明）。"边界文件缺失"那一类用例因此**不能**依赖
    默认目录的状态 —— 否则同一份代码在别人的机器上结论相反：Slice 25 实测开发机
    放入 ``china.geojson`` 之后，四条用例两红一挂一红；挂住的那条是因为 calculate
    真的去估算全中国 Z8~18，瓦片数是天文数字，永远算不完。

    重定向到空目录：目录读不出来 → 服务退回内置预设（只剩 china 一条、没有层级）
    → 边界文件不在 → 正是"未部署"那个状态，且与机器上有没有部署数据无关。
    """
    from app.geo import region as region_module

    target = work_dir / "regions_undeployed"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(region_module, "REGION_RESOURCE_DIR", target)
    return target


@pytest.fixture
def shipped_catalog_root(work_dir: Path) -> Path:
    """**随仓库发行**的行政区目录状态：只有 ``index.json``，没有任何边界文件。

    仓库分发的是元数据，边界数据要部署方自己放入（中国国界属法定地图内容，
    见 ``app.config.settings`` 里 ``REGION_RESOURCE_DIR`` 的说明）。"发行状态"
    相关的用例（缺边界只报警告、省市还没有数据）因此不能直接读
    ``resources/regions/`` —— 开发/验收机上可能已经放了授权数据：Slice 25 实测
    放入 ``china.geojson`` 之后，两条"缺边界只报警告"的用例直接红。

    这里把发行版 ``index.json``（它本身随仓库分发，不受部署影响）复制进空树，
    重建那个状态，与机器上部署了什么无关。
    """
    import shutil as _shutil

    from app.config import settings

    target = work_dir / "regions_shipped"
    target.mkdir(parents=True, exist_ok=True)
    _shutil.copy(settings.REGION_RESOURCE_DIR / settings.REGION_INDEX_FILENAME, target)
    return target


@pytest.fixture
def region_catalog(region_root: Path):
    """挂在可写副本上的目录。"""
    from app.regions import RegionCatalog

    return RegionCatalog.load(root=region_root)


@pytest.fixture
def region_loader(region_catalog):
    """与 :func:`region_catalog` 配套的加载器。"""
    from app.regions import RegionLoader

    return RegionLoader(region_catalog)


# --------------------------------------------------------------------------- #
# 多级下载计划（Slice 20）
# --------------------------------------------------------------------------- #
#: 一块**三角形**边界：外包矩形是 1°×1°，但只有一部分瓦片真的与它相交。
#:
#: 为什么需要它：合成目录里的多边形全是**轴对齐矩形**（几何 == 外包矩形），
#: 于是 ``selected == candidate`` —— 那种数据永远测不出"计划按**选中**张数
#: 汇总"这条 Spec §7 的硬要求（用候选数也能让所有断言通过）。
#: 三角形让两者明显分离，一个写错的实现会立刻现形。
TRIANGLE_GEOJSON: dict = {
    "type": "Feature",
    "properties": {
        "name": "示例三角省",
        "id": "triangleland",
        "boundaryVersion": "2026-09-fixture",
    },
    "geometry": {
        "type": "Polygon",
        "coordinates": [
            [
                [118.0, 30.0],
                [119.0, 30.0],
                [118.0, 31.0],
                [118.0, 30.0],
            ]
        ],
    },
}


@pytest.fixture
def region_root_with_triangle(region_root: Path) -> Path:
    """在可写副本上追加一块三角形区域（``triangleland``，省级）。

    只动工作区里的那份副本，仓库里的夹具保持只读（同 :func:`region_root` 的
    理由：往仓库夹具上写东西会污染后续用例）。
    """
    import json

    (region_root / "province" / "triangleland.geojson").write_text(
        json.dumps(TRIANGLE_GEOJSON, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    index_path = region_root / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["regions"].append(
        {
            "id": "triangleland",
            "name": "示例三角省",
            "level": "province",
            "parentId": "demoland",
            "geojson": "province/triangleland.geojson",
            "boundaryVersion": "2026-09-fixture",
        }
    )
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return region_root


@pytest.fixture
def region_resources(monkeypatch, region_root_with_triangle: Path) -> Path:
    """把内置资源目录重定向到合成树（含三角形区域）。

    只改 ``app.geo.region.REGION_RESOURCE_DIR`` 这一个全局：区域服务每次取根
    目录时都重新读它，因此**在构造任何东西之前**改好就够了。
    """
    from app.geo import region as region_module

    monkeypatch.setattr(region_module, "REGION_RESOURCE_DIR", region_root_with_triangle)
    return region_root_with_triangle


@pytest.fixture
def plans_file() -> Path:
    """计划文件（Slice 20 ``plans.json``）的独立临时路径。

    刻意**不**放进 ``work_dir``：不少用例会断言 ``work_dir`` 里"只有那个
    .mbtiles"，计划文件写进去会把那些断言搅黄（与 :func:`tasks_file` 同理）。
    """
    directory = Path(tempfile.mkdtemp(prefix="plans-test-"))
    try:
        yield directory / "plans.json"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Slice 22：任务区（Mission）
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _reset_mission_region_resolver():
    """每个用例结束后清掉"任务区几何解析"的全局注册。

    任务区靠**注册回调**让续传与 Inspector 能按 ``region_id`` 找回几何
    （``app.geo.region`` 的动态解析注册表）。注册表是模块级全局，跨用例留着的话，
    上一个用例那个指向**另一个临时目录**的解析器会继续回答同名的 ``mission-001``
    —— Inspector 于是拿到另一份几何，而结论长得很像真的（这只是"结论错了"，
    不是"报错了"，最难查的一类）。

    因此每个用例都从空表开始。这也是 ``app.mission`` 只允许一个生效解析器的
    另一半保障（见 ``_ACTIVE_RESOLVER``）。
    """
    from app.geo import region as region_module
    from app.mission.mission_region_service import reset_mission_region_resolver

    yield
    region_module.clear_region_resolvers()
    reset_mission_region_resolver()


@pytest.fixture
def mission_root() -> Path:
    """任务区根目录（``data/mission_regions`` 的替身）。"""
    directory = Path(tempfile.mkdtemp(prefix="mission-test-"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def mission_service(mission_root: Path):
    """任务区服务（临时目录、**不做引用检查**）。

    引用检查默认会去读用户主目录里的 ``tasks.json`` / ``plans.json`` ——
    测试绝不该碰那些文件（Slice 7 的教训），因此这里给一个空引用清单。
    需要验证"被引用就拒绝删除"的用例自己传 ``reference_files``。
    """
    from app.mission import MissionRegionService, MissionRegionStore

    return MissionRegionService(
        MissionRegionStore(mission_root), reference_files=()
    )
