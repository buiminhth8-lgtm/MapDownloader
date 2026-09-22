"""任务区持久化（Slice 22，Spec §11 / §12）。

每个任务区一个目录，三个文件是**固定的**：:

    <root>/<mission-id>/route.geojson     原始航线（LineString，人可读）
    <root>/<mission-id>/region.geojson    Buffer 后的多边形（**下载依据**）
    <root>/<mission-id>/metadata.json     半径 / 航点数 / 几何版本 / 范围 / 创建时间

三条硬性要求，每一条都有对应用例守着
------------------------------------

1. **原子写**：先写 ``<文件名>.tmp``，``flush`` + ``fsync`` 之后再
   ``os.replace`` 顶上去。中途断电只会留下一个 tmp，而不会留下一个"读得动
   但缺一半"的 metadata.json —— 那种文件比不存在的文件更危险：它看起来是好的。
2. **路径受限**：任务区 id 只能是单段安全目录名（见
   :func:`~app.mission.models.require_mission_id`），拼出来的路径还要再确认
   "解析后仍在根目录内"。两道都过不去就报错，绝不"清理一下再用"。
3. **只写几何**：落盘的内容里**不允许出现天地图 Token 或瓦片坐标**
   （Spec §12）。落盘器在写之前会扫一遍键名 —— 任务区文件是要跟着用户走的，
   把凭据写进去等于把密钥散播出去。

读取时的完整性校验
------------------

:meth:`MissionRegionStore.load` 默认会把 ``metadata.json`` 记的范围与
``region.geojson`` 的**实际范围**对一遍。两者本来就出自同一份浮点数，
对不上只有一种可能：文件被改过。这时候必须报错 —— 拿着一个和记录不一致的
几何去续传，会让"上次下到哪了"这件事彻底失去依据（Spec §21）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Mapping

from app.config.settings import (
    MISSION_BOUNDS_TOLERANCE,
    MISSION_METADATA_FILENAME,
    MISSION_REGION_FILENAME,
    MISSION_REGIONS_DIR,
    MISSION_ROUTE_FILENAME,
)
from app.geo.bounds import GeoBounds
from app.geo.geojson_loader import GeoJSONError, geometry_to_bounds, load_geojson_geometry
from app.mission.models import (
    MissionRegion,
    MissionRegionError,
    MissionRegionMetadata,
    MissionRegionNotFound,
    MissionWaypoint,
    is_mission_id,
    require_mission_id,
)
from app.mission.route_loader import load_route_waypoints

__all__ = ["MissionRegionStore"]

_logger = logging.getLogger(__name__)

#: 不允许出现在任务区文件里的键名（Spec §12）。
#:
#: 比较的是**键名**（小写化后），不是值：值里出现 "token" 三个字母无关紧要，
#: 真正的错误是"把凭据当一个字段存进去"。
_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "token",
        "tk",
        "apikey",
        "api_key",
        "access_token",
        "password",
        "passwd",
        "secret",
        "credential",
        "tiles",
        "tile",
        "tilecoordinate",
        "tile_coordinate",
        "tile_coordinates",
    }
)


class MissionRegionStore:
    """任务区目录的读写（Spec §11）。

    Attributes:
        root: 任务区根目录（默认 ``~/MapDownloader/data/mission_regions``）。
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self._root = Path(root) if root is not None else MISSION_REGIONS_DIR
        self._root = Path(os.path.abspath(self._root))

    # ------------------------------------------------------------------ #
    # 只读属性
    # ------------------------------------------------------------------ #
    @property
    def root(self) -> Path:
        """任务区根目录（绝对路径）。"""
        return self._root

    # ------------------------------------------------------------------ #
    # 路径
    # ------------------------------------------------------------------ #
    def mission_dir(self, mission_id: str) -> Path:
        """某个任务区的目录（不保证存在）。

        Raises:
            MissionRegionError: id 不合法，或拼出来的路径越出了根目录。
        """
        mid = require_mission_id(mission_id)
        root = self._root.resolve()
        candidate = (root / mid).resolve()
        if candidate != root and root not in candidate.parents:
            raise MissionRegionError(
                f"任务区路径越出了任务区根目录：{candidate} 不在 {root} 内"
            )
        return candidate

    def route_path(self, mission_id: str) -> Path:
        """``route.geojson`` 的路径。"""
        return self.mission_dir(mission_id) / MISSION_ROUTE_FILENAME

    def region_path(self, mission_id: str) -> Path:
        """``region.geojson`` 的路径（下载链路要的就是它）。"""
        return self.mission_dir(mission_id) / MISSION_REGION_FILENAME

    def metadata_path(self, mission_id: str) -> Path:
        """``metadata.json`` 的路径。"""
        return self.mission_dir(mission_id) / MISSION_METADATA_FILENAME

    def exists(self, mission_id: str) -> bool:
        """这个任务区在不在（以 ``metadata.json`` 与 ``region.geojson`` 都在为准）。"""
        try:
            return self.region_path(mission_id).is_file() and self.metadata_path(
                mission_id
            ).is_file()
        except MissionRegionError:
            return False

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    def save(
        self,
        region: MissionRegion,
        *,
        route_geojson: Mapping[str, Any],
        region_geojson: Mapping[str, Any],
        metadata: MissionRegionMetadata | None = None,
    ) -> MissionRegion:
        """落盘一个任务区（三个文件，全部原子写）。

        Args:
            region: 任务区模型。
            route_geojson: :func:`~app.mission.region_builder.route_geojson_document` 的产物。
            region_geojson: :func:`~app.mission.region_builder.region_geojson_document` 的产物。
            metadata: 元数据；``None`` 时由 ``region`` 推导。

        Returns:
            回填了 :attr:`~app.mission.models.MissionRegion.geojson_path`
            （``region.geojson`` 的**绝对路径**）的新实例。

            为什么回填在这里而不是在 builder：路径是"存到哪儿"的属性，
            只有落盘器知道；让 builder 去猜路径，等于让几何层依赖目录布局。

        Raises:
            MissionRegionError: id 不一致 / 文档里出现禁用键名（Token、瓦片坐标）/ 写盘失败。
        """
        mid = require_mission_id(region.id)
        meta = metadata if metadata is not None else MissionRegionMetadata.from_region(region)
        if meta.id != mid:
            raise MissionRegionError(
                f"元数据里的 id（{meta.id}）与任务区 id（{mid}）不一致"
            )
        _assert_no_forbidden(route_geojson, what=f"{mid}/{MISSION_ROUTE_FILENAME}")
        _assert_no_forbidden(region_geojson, what=f"{mid}/{MISSION_REGION_FILENAME}")

        directory = self.mission_dir(mid)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self._write_json(self.route_path(mid), route_geojson)
            self._write_json(self.region_path(mid), region_geojson)
            self._write_json(self.metadata_path(mid), meta.to_dict())
        except OSError as exc:
            raise MissionRegionError(
                f"任务区落盘失败（{directory}）：{exc}"
            ) from exc
        return region.with_geojson_path(str(self.region_path(mid)))

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def load_metadata(self, mission_id: str) -> MissionRegionMetadata:
        """读 ``metadata.json``。

        Raises:
            MissionRegionNotFound: 目录 / 文件不存在。
            MissionRegionError: 内容不合法。
        """
        mid = require_mission_id(mission_id)
        path = self.metadata_path(mid)
        payload = self._read_json(path, not_found=MissionRegionNotFound)
        meta = MissionRegionMetadata.from_dict(payload)
        if meta.id != mid:
            raise MissionRegionError(
                f"任务区目录名与元数据里的 id 不一致：{mid} != {meta.id}"
            )
        return meta

    def load(self, mission_id: str, *, verify: bool = True) -> MissionRegion:
        """读一个任务区（元数据 + 航线 + 几何存在性，必要时校验几何）。

        Args:
            mission_id: 任务区 id。
            verify: 是否把 ``region.geojson`` 的实际范围与元数据记的范围对一遍
                （默认对；测试里想跳过重活时可以关掉）。

        Raises:
            MissionRegionNotFound: 任务区目录 / 文件不存在。
            MissionRegionError: 元数据不合法、几何与元数据不一致、航线文件坏。
        """
        mid = require_mission_id(mission_id)
        meta = self.load_metadata(mid)
        region_path = self.region_path(mid)
        if not region_path.is_file():
            raise MissionRegionNotFound(
                f"任务区 {mid} 的几何文件不存在：{region_path}"
                "（这个目录已经不完整了，可以用 MissionRegionService.delete 清掉它）"
            )
        waypoints = self._load_waypoints(mid)
        if verify:
            self._verify_geometry(mid, meta.bounds)
        return MissionRegion(
            id=mid,
            name=meta.name,
            waypoints=waypoints,
            buffer_meters=meta.buffer_meters,
            bounds=meta.bounds,
            geojson_path=str(region_path),
            geometry_version=meta.geometry_version,
            created_at=meta.created_at,
        )

    def load_waypoints(self, mission_id: str) -> tuple[MissionWaypoint, ...]:
        """只读原始航线（``route.geojson``）。

        Raises:
            MissionRegionNotFound: 航线文件不存在。
            MissionRouteError: 航线文件坏。
        """
        return self._load_waypoints(require_mission_id(mission_id))

    def geometry(self, mission_id: str):
        """加载任务区几何（Shapely 对象）。

        Raises:
            MissionRegionNotFound: 几何文件不存在。
            GeoJSONError: 几何文件坏。
        """
        mid = require_mission_id(mission_id)
        path = self.region_path(mid)
        if not path.is_file():
            raise MissionRegionNotFound(f"任务区 {mid} 的几何文件不存在：{path}")
        return load_geojson_geometry(path)

    def list_all(self) -> tuple[MissionRegion, ...]:
        """列出全部任务区（按 id 排序）。

        单个目录读不出来时**跳过它并记一条警告**，不让它挡住其余任务区 ——
        与 Slice 18 目录校验同一条原则：一个坏条目不该让整份清单不可用。
        """
        if not self._root.is_dir():
            return ()
        found: list[MissionRegion] = []
        for entry in sorted(self._root.iterdir(), key=lambda item: item.name):
            if not entry.is_dir() or not is_mission_id(entry.name):
                continue
            try:
                found.append(self.load(entry.name))
            except (MissionRegionError, GeoJSONError) as exc:
                _logger.warning("任务区目录 %s 读不出来，已跳过：%s", entry, exc)
        return tuple(found)

    # ------------------------------------------------------------------ #
    # 删
    # ------------------------------------------------------------------ #
    def delete(self, mission_id: str) -> bool:
        """删除一个任务区的整个目录。

        **本方法不做引用检查**：它只保证"删的是这个任务区自己的目录，
        而且不会删到根目录外面"。引用保护（正在被任务 / 计划引用时拒绝删除）
        在 :meth:`MissionRegionService.delete` —— 那是业务判断，不是存储的职责。

        Returns:
            ``True`` 表示确实删掉了目录；``False`` 表示本来就不存在。
        """
        mid = require_mission_id(mission_id)
        directory = self.mission_dir(mid)
        root = self._root.resolve()
        if directory == root:  # pragma: no cover - id 非空时不可能走到
            raise MissionRegionError("拒绝删除任务区根目录")
        if not directory.is_dir():
            return False
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            raise MissionRegionError(f"删除任务区目录失败（{directory}）：{exc}") from exc
        return True

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _load_waypoints(self, mission_id: str) -> tuple[MissionWaypoint, ...]:
        """读 ``route.geojson`` 里的航点。

        Raises:
            MissionRegionNotFound: 文件不存在。
            MissionRouteError: 内容坏。
        """
        path = self.route_path(mission_id)
        if not path.is_file():
            raise MissionRegionNotFound(
                f"任务区 {mission_id} 的航线文件不存在：{path}"
                "（任务区目录的三个文件是一起写的，缺一个说明这个目录已经损坏）"
            )
        # 航线文件的格式就是 GeoJSON LineString，直接复用导入器：
        # "怎么读航线"只允许有一份实现，否则盘上的航线与用户刚导入的那份
        # 迟早会走两条不同的解析路径。
        return load_route_waypoints(path)

    def _verify_geometry(self, mission_id: str, declared: GeoBounds) -> None:
        """把几何的实际范围与元数据记的范围对一遍。

        Raises:
            MissionRegionError: 对不上（文件被改过）。
        """
        try:
            geometry = self.geometry(mission_id)
            actual = geometry_to_bounds(geometry)
        except GeoJSONError as exc:
            raise MissionRegionError(
                f"任务区 {mission_id} 的几何文件读不出可用几何：{exc}"
            ) from exc
        tolerance = MISSION_BOUNDS_TOLERANCE
        if (
            abs(actual.west - declared.west) > tolerance
            or abs(actual.south - declared.south) > tolerance
            or abs(actual.east - declared.east) > tolerance
            or abs(actual.north - declared.north) > tolerance
        ):
            raise MissionRegionError(
                f"任务区 {mission_id} 的几何与元数据不一致："
                f"元数据记的是 {declared.west:.6f}, {declared.south:.6f}, "
                f"{declared.east:.6f}, {declared.north:.6f}，"
                f"几何实际是 {actual.west:.6f}, {actual.south:.6f}, "
                f"{actual.east:.6f}, {actual.north:.6f}。"
                "这份几何已经被改过，不能再用它续传（瓦片集会变）"
            )

    def _write_json(self, path: Path, payload: Mapping[str, Any]) -> None:
        """原子写一个 JSON 文件（tmp → fsync → replace）。"""
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        tmp = path.with_name(path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError:
            # 别把半个 tmp 留在目录里：它会让"这个目录里有什么"变成一道谜题。
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:  # pragma: no cover - 清理失败不掩盖真实异常
                pass
            raise

    def _read_json(
        self,
        path: Path,
        *,
        not_found: type[Exception] = MissionRegionError,
    ) -> Any:
        """读一个 JSON 文件。

        Raises:
            not_found: 文件不存在。
            MissionRegionError: 读不出来 / 不是 JSON / 顶层不是对象。
        """
        if not path.is_file():
            raise not_found(f"任务区文件不存在：{path}")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise MissionRegionError(f"任务区文件读不出来（{path}）：{exc}") from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MissionRegionError(
                f"任务区文件不是合法的 JSON（{path}）：第 {exc.lineno} 行 {exc.msg}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise MissionRegionError(
                f"任务区文件的顶层必须是 JSON 对象（{path}），"
                f"实际为 {type(payload).__name__}"
            )
        return payload


def _assert_no_forbidden(payload: Any, *, what: str) -> None:
    """递归扫键名，拒绝 Token / 瓦片坐标进任务区文件（Spec §12）。

    Raises:
        MissionRegionError: 命中禁用键名。
    """
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key).strip().lower() in _FORBIDDEN_KEYS:
                raise MissionRegionError(
                    f"任务区文件里不允许出现 {key!r}（{what}）："
                    "任务区文件会跟着地图包分发给别人，凭据与瓦片坐标都不该写进去"
                )
            _assert_no_forbidden(value, what=what)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            _assert_no_forbidden(item, what=what)
