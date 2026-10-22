"""行政区域目录（Slice 18，Spec §3 / §5 / §6）。

目录是**一份 JSON 元数据 + 一棵目录树**：``resources/regions/index.json`` 里逐条
写着"有哪些区域、谁是谁的上级、边界文件在哪"，真正的 GeoJSON 按 ``country`` /
``province`` / ``city`` 分目录摆着。UI 只允许问这个类，**禁止自己扫目录推断层级**
（Spec §3 最后一句）：文件名排序推不出"杭州市属于浙江省"，只有元数据知道。

Lazy load（Spec §5）
--------------------

构造目录时**只读 index.json**，一个 GeoJSON 都不解析。理由是全国省市数据加起来
上千个文件、几百 MB，启动时全读一遍会让程序几十秒打不开 —— 而用户这次可能只是
想下一个自定义矩形。几何只在 :class:`~app.regions.loader.RegionLoader` 里、
真的要用的时候才读。

``tests/test_region_loader.py`` 有一条用例把 ``load_geojson_document`` 包起来数
调用次数：建目录后必须是 0 次。

资源根目录：只有一处真相
------------------------

``root`` 缺省时**每次访问都重新读** :data:`app.geo.region.REGION_RESOURCE_DIR`
这个模块全局，而不是在构造时快照一份。这样做有两个好处：

1. 资源目录只有一处定义（Slice 16 就在 ``app.geo.region`` 里），不会出现
   "目录层以为根是 A、加载层以为是 B"这种查不出来的错位；
2. 验收脚本 / 测试把内置资源重定向到临时目录时（monkeypatch 那个全局），
   目录层**自动跟着走**，不需要再多加一个要同步的开关。

代价是"改完之后已经建好的目录对象要重新 load 一次"，所以这里给了
:meth:`RegionCatalog.stale` —— 比对 index.json 的修改时间与大小，
:class:`~app.regions.service.RegionService` 每次取目录时用它判断要不要重读。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from app.geo import region as region_module
from app.geo.bounds import GeoBounds
from app.planning.region_level import RegionLevel
from app.regions.models import (
    CatalogIssue,
    CatalogIssueCode,
    RegionCatalogError,
    RegionPreset,
    is_safe_region_relative_path,
    normalize_region_relative_path,
)

__all__ = [
    "INDEX_FILENAME",
    "SUPPORTED_INDEX_VERSIONS",
    "REGION_ROOT_KEY",
    "RegionCatalog",
    "region_resource_root",
    "effective_region_root",
    "parse_index_payload",
]

#: 目录元数据的固定文件名（Spec §1 / §18）。
INDEX_FILENAME: str = "index.json"

#: 本版本认得的 index.json 格式版本。遇到更高的版本记一条 error 但**继续读**
#: （向前兼容：多出来的字段我们忽略，少掉的老字段才致命）。
SUPPORTED_INDEX_VERSIONS: tuple[int, ...] = (1,)

#: 一条记录里承载"id / 名称 / 等级 / 上级 / 边界文件 / 版本"的键名。
#: 写成常量是为了让"改键名"变成一次 git diff 能看全的动作。
REGION_ENTRY_KEYS: tuple[str, ...] = (
    "id",
    "name",
    "level",
    "parentId",
    "geojson",
    "boundaryVersion",
    "bounds",
    "code",
)

#: 只为日志/报告用：把规范键名念一遍，好让报错信息有据可依。
REGION_ROOT_KEY: str = "regions"


def region_resource_root() -> Path:
    """行政区资源的根目录（**调用时**读全局，见模块文档）。"""
    return Path(region_module.REGION_RESOURCE_DIR)


def effective_region_root() -> Path:
    """当前生效的行政区数据根目录（Slice 26，Spec §16 / §17）。

    生成数据集（:data:`app.config.settings.REGION_DATA_ROOT`，即
    ``resources/regions/generated/``）**已发布**时选它，否则回退手工资源
    目录（:func:`region_resource_root`）—— "如果 generated dataset 不存在，
    继续使用现有 Region Root"（Spec §27 兼容性）。

    目录层**不区分**两种来源：手工数据与导入器生成的数据用的是同一套
    index.json 格式（Spec §16"RegionCatalog 无需知道数据是手工或 Importer
    生成"）。判断只发生在"选哪个根目录"这一刻，且判断依据是**文件在不在**，
    不是某个开关 —— 开关会骗人（"以为发了其实没发"），文件不会。

    测试与验收脚本应继续 monkeypatch ``app.geo.region.REGION_RESOURCE_DIR``
    （或显式传 ``root=``）：只有当调用方**明确要求**"生成数据优先"时才走
    这里（见 :class:`~app.regions.service.RegionService` 的
    ``prefer_generated``），默认路径的行为与 Slice 18 逐字节一致。
    """
    from app.config import settings as settings_module

    generated = Path(settings_module.REGION_DATA_ROOT)
    index = generated / INDEX_FILENAME
    if index.is_file():
        # 光有 index.json 还不够：发布是原子的，正常不会留下坏文件，但
        # 目录可能被外部弄坏（手删、半截写入）。"文件在但读不出"时不能算
        # "已发布"，否则用户会从上一份能用的数据退化成扁平内置预设
        # （Spec §17"Reload 失败时继续保留旧 Catalog"）。
        try:
            RegionCatalog.load(root=generated)
        except RegionCatalogError:
            return region_resource_root()
        return generated
    return region_resource_root()


# --------------------------------------------------------------------------- #
# 解析 index.json
# --------------------------------------------------------------------------- #
def _strict_level(raw: object, region_id: str, issues: list[CatalogIssue]) -> RegionLevel | None:
    """把 JSON 里的 ``level`` 读成枚举；读不出就记一条问题并返回 ``None``。

    这里**不能**用 :meth:`RegionLevel.parse`（它会把认不出的字符串悄悄变成
    ``CUSTOM``）：那样一条把 ``province`` 拼成 ``provincee`` 的记录会变成
    "一个自定义区域"，安安静静地不出现在任何省的列表里 —— 用户只会觉得
    "我明明加了数据却查不到"。Spec §13 也点名要求校验"未知 RegionLevel"。
    """
    if raw is None or str(raw).strip() == "":
        issues.append(
            CatalogIssue(
                CatalogIssueCode.MISSING_LEVEL,
                "缺少 level 字段，无法判断它属于哪一级（全国 / 省 / 市）",
                region_id,
            )
        )
        return None
    text = str(raw).strip().lower()
    for item in RegionLevel:
        if item.value == text:
            return item
    issues.append(
        CatalogIssue(
            CatalogIssueCode.UNKNOWN_LEVEL,
            f"等级 {raw!r} 不是已知的行政等级（可选："
            + "、".join(item.value for item in RegionLevel)
            + "）",
            region_id,
        )
    )
    return None


def _read_path(raw: object, region_id: str, issues: list[CatalogIssue]) -> str | None:
    """读边界文件路径并做安全检查（Spec §12）。

    复用 :func:`~app.geo.region.is_safe_region_relative_path` 而不是在这里再写一遍
    规则：安全检查一旦有第二份实现，就一定会有一份是错的（而且通常错在宽松那边）。
    """
    text = normalize_region_relative_path(raw)
    if not text:
        issues.append(
            CatalogIssue(
                CatalogIssueCode.MISSING_GEOJSON_PATH,
                "缺少 geojson 字段，没有边界文件就没有裁剪依据",
                region_id,
            )
        )
        return None
    if not is_safe_region_relative_path(text):
        issues.append(
            CatalogIssue(
                CatalogIssueCode.UNSAFE_PATH,
                f"边界文件路径 {text!r} 不安全：必须是资源目录内的相对路径"
                "（不允许绝对路径、盘符或 ..）",
                region_id,
            )
        )
        return None
    return text


def _read_bounds(raw: object, region_id: str, issues: list[CatalogIssue]) -> GeoBounds | None:
    """读可选的 ``bounds``。缺省是 ``None``（真实范围一律从几何算，Spec §11）。"""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        issues.append(
            CatalogIssue(
                CatalogIssueCode.BOUNDS_MALFORMED,
                f"bounds 必须是一个含 west/south/east/north 的对象，实际是 {type(raw).__name__}",
                region_id,
            )
        )
        return None
    try:
        return GeoBounds(
            west=float(raw["west"]),
            south=float(raw["south"]),
            east=float(raw["east"]),
            north=float(raw["north"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        issues.append(
            CatalogIssue(
                CatalogIssueCode.BOUNDS_MALFORMED,
                f"bounds 读不出来（{exc}），可以整条删掉，程序会从几何自己算",
                region_id,
            )
        )
        return None


def _read_entry(raw: object, index: int, issues: list[CatalogIssue]) -> RegionPreset | None:
    """读一条记录；读不出来返回 ``None``（调用方会跳过它）。"""
    if not isinstance(raw, Mapping):
        issues.append(
            CatalogIssue(
                CatalogIssueCode.ENTRY_NOT_OBJECT,
                f"第 {index + 1} 条记录不是一个对象，已跳过",
            )
        )
        return None

    region_id = str(raw.get("id") or "").strip().lower()
    if not region_id:
        issues.append(
            CatalogIssue(
                CatalogIssueCode.MISSING_ID,
                f"第 {index + 1} 条记录缺少 id，已跳过",
            )
        )
        return None

    name = str(raw.get("name") or "").strip()
    if not name:
        issues.append(
            CatalogIssue(
                CatalogIssueCode.MISSING_NAME,
                "缺少 name（展示名），已跳过",
                region_id,
            )
        )
        return None

    level = _strict_level(raw.get("level"), region_id, issues)
    if level is None:
        return None

    path = _read_path(raw.get("geojson"), region_id, issues)
    if path is None:
        return None

    parent = str(raw.get("parentId") or "").strip().lower() or None
    version = str(raw.get("boundaryVersion") or "").strip() or None
    code = str(raw.get("code") or "").strip() or None
    bounds = _read_bounds(raw.get("bounds"), region_id, issues)

    try:
        return RegionPreset(
            id=region_id,
            name=name,
            level=level,
            parent_id=parent,
            geojson_path=path,
            boundary_version=version,
            bounds=bounds,
            code=code,
        )
    except ValueError as exc:  # 归一化阶段就拦下的写法问题（例如路径含反斜杠到越界）
        issues.append(
            CatalogIssue(CatalogIssueCode.UNSAFE_PATH, str(exc), region_id)
        )
        return None


def parse_index_payload(
    payload: object, *, issues: list[CatalogIssue]
) -> tuple[RegionPreset, ...]:
    """把 index.json 的顶层对象解析成一组 :class:`RegionPreset`。

    结构性问题（不是对象 / 没有 ``regions`` 数组）抛 :class:`RegionCatalogError`
    —— 那说明整份文件不是我们认得的格式；单条记录的问题只记 issue 并跳过。

    Raises:
        RegionCatalogError: 顶层不是对象，或 ``regions`` 不是数组。
    """
    if not isinstance(payload, Mapping):
        raise RegionCatalogError(
            f"{INDEX_FILENAME} 顶层必须是一个对象，实际是 {type(payload).__name__}"
        )

    version = payload.get("version", 1)
    try:
        version_number = int(version)
    except (TypeError, ValueError):
        version_number = -1
    if version_number not in SUPPORTED_INDEX_VERSIONS:
        issues.append(
            CatalogIssue(
                CatalogIssueCode.INDEX_VERSION_UNSUPPORTED,
                f"目录格式版本是 {version!r}，本版本只认 "
                + "、".join(str(item) for item in SUPPORTED_INDEX_VERSIONS)
                + "（多出来的字段会被忽略，继续按已知字段读取）",
            )
        )

    raw_regions = payload.get(REGION_ROOT_KEY)
    if raw_regions is None:
        raise RegionCatalogError(f"{INDEX_FILENAME} 缺少 {REGION_ROOT_KEY!r} 数组")
    if not isinstance(raw_regions, list):
        raise RegionCatalogError(
            f"{INDEX_FILENAME} 的 {REGION_ROOT_KEY!r} 必须是一个数组，"
            f"实际是 {type(raw_regions).__name__}"
        )

    presets: list[RegionPreset] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_regions):
        entry = _read_entry(raw, index, issues)
        if entry is None:
            continue
        if entry.id in seen:
            # 保留**第一条**：后出现的同名记录不影响已经成立的目录，
            # 但这件事必须说出来 —— 否则用户改了后面那条却发现没生效。
            issues.append(
                CatalogIssue(
                    CatalogIssueCode.DUPLICATE_ID,
                    f"id 重复（第 {index + 1} 条），已保留先出现的那一条",
                    entry.id,
                )
            )
            continue
        seen.add(entry.id)
        presets.append(entry)
    return tuple(presets)


# --------------------------------------------------------------------------- #
# 目录
# --------------------------------------------------------------------------- #
class RegionCatalog:
    """行政区域目录（Spec §3）。

    不可变视图：``presets`` 在构造后不再变化，查询方法都是纯读。
    ``root`` 缺省时**不固定**在构造那一刻的值：它每次都会去问
    :func:`region_resource_root`，见模块文档。
    """

    def __init__(
        self,
        presets: Iterator[RegionPreset] | tuple[RegionPreset, ...] = (),
        *,
        root: Path | None = None,
        issues: tuple[CatalogIssue, ...] = (),
    ) -> None:
        self._presets: tuple[RegionPreset, ...] = tuple(presets)
        self._explicit_root: Path | None = (
            Path(root).resolve() if root is not None else None
        )
        self._issues: tuple[CatalogIssue, ...] = tuple(issues)
        self._by_id: dict[str, RegionPreset] = {item.id: item for item in self._presets}
        #: 读这份目录时 index.json 的 (mtime_ns, size)；``None`` 表示没有来源文件。
        self._stamp: tuple[int, int] | None = None

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #
    @classmethod
    def empty(cls, *, root: Path | None = None) -> "RegionCatalog":
        """空目录（目录文件还没就位时用它顶上，见 RegionService 的兜底）。"""
        return cls((), root=root)

    @classmethod
    def from_payload(
        cls, payload: object, *, root: Path | None = None
    ) -> "RegionCatalog":
        """从已经读进内存的 JSON 结构构造（测试与诊断用）。"""
        issues: list[CatalogIssue] = []
        presets = parse_index_payload(payload, issues=issues)
        catalog = cls(presets, root=root, issues=tuple(issues))
        return catalog

    @classmethod
    def from_index(cls, path: Path, *, root: Path | None = None) -> "RegionCatalog":
        """读一个显式路径的 index.json。目录不存在时抛 :class:`RegionCatalogError`。"""
        index_path = Path(path)
        if not index_path.is_file():
            raise RegionCatalogError(
                f"行政区目录文件不存在：{index_path}。"
                f"它是元数据（不是边界数据），应当随程序一起分发"
            )
        try:
            text = index_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RegionCatalogError(f"行政区目录文件读不出来：{index_path}（{exc}）") from exc
        except UnicodeDecodeError as exc:
            raise RegionCatalogError(
                f"行政区目录文件不是 UTF-8：{index_path}（{exc}）"
            ) from exc

        try:
            payload: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RegionCatalogError(
                f"行政区目录文件不是合法 JSON：{index_path}（第 {exc.lineno} 行：{exc.msg}）"
            ) from exc

        catalog = cls.from_payload(payload, root=root if root is not None else index_path.parent)
        catalog._stamp = _stat_stamp(index_path)
        return catalog

    @classmethod
    def load(cls, *, root: Path | None = None) -> "RegionCatalog":
        """读 ``<root>/index.json``（``root`` 缺省为资源根目录）。

        Raises:
            RegionCatalogError: 文件不存在 / 读不出 / 结构不对。
        """
        base = Path(root) if root is not None else region_resource_root()
        return cls.from_index(base / INDEX_FILENAME, root=base)

    # ------------------------------------------------------------------ #
    # 只读属性
    # ------------------------------------------------------------------ #
    @property
    def root(self) -> Path:
        """资源根目录（``root`` 缺省时每次重新问全局，见模块文档）。"""
        if self._explicit_root is not None:
            return self._explicit_root
        return region_resource_root().resolve()

    @property
    def index_path(self) -> Path:
        """``index.json`` 的规范路径。"""
        return self.root / INDEX_FILENAME

    @property
    def presets(self) -> tuple[RegionPreset, ...]:
        """全部条目，顺序与 index.json 一致。"""
        return self._presets

    @property
    def issues(self) -> tuple[CatalogIssue, ...]:
        """读目录时记下的问题（重复 id、未知等级、路径越界…）。"""
        return self._issues

    @property
    def ids(self) -> tuple[str, ...]:
        """全部 id（顺序同上）。"""
        return tuple(item.id for item in self._presets)

    def stale(self) -> bool:
        """:attr:`index_path` 是否已经被改过（与读进来那一刻相比）。

        比的是 ``(mtime_ns, size)``：改内容必然改 mtime；mtime 精度不够时
        大小的变化兜底。文件消失也算"变了"（好让上层重读一遍并报错）。
        """
        return _stat_stamp(self.index_path) != self._stamp

    # ------------------------------------------------------------------ #
    # 查询（Spec §3）
    # ------------------------------------------------------------------ #
    def get(self, region_id: str | None) -> RegionPreset | None:
        """按 id 查一条；找不到返回 ``None``（宽容，供 UI 试探）。"""
        if not region_id:
            return None
        return self._by_id.get(str(region_id).strip().lower())

    def require(self, region_id: str | None) -> RegionPreset:
        """按 id 查一条；找不到抛 :class:`RegionCatalogError`。

        Raises:
            RegionCatalogError: 目录里没有这个 id。
        """
        item = self.get(region_id)
        if item is None:
            wanted = str(region_id or "").strip() or "(空)"
            hint = "、".join(self.ids[:8]) if self._presets else "（当前目录是空的）"
            raise RegionCatalogError(
                f"行政区目录里没有 id={wanted!r} 的区域；可用的是：{hint}"
            )
        return item

    def list_all(self) -> tuple[RegionPreset, ...]:
        """全部条目。"""
        return self._presets

    def list_by_level(self, level: object) -> tuple[RegionPreset, ...]:
        """某一等级的全部区域（顺序与目录一致）。

        Raises:
            ValueError: ``level`` 不是 :class:`RegionLevel`，也不是它的 ``value``。
        """
        target = _coerce_level(level)
        return tuple(item for item in self._presets if item.level is target)

    def list_children(self, parent_id: str | None) -> tuple[RegionPreset, ...]:
        """直接下级。

        ``parent_id=None`` 时返回**全部根节点**（正常就是全国那一层）——
        调用方不必先问"根是谁"。
        """
        if parent_id is None:
            return tuple(item for item in self._presets if not item.has_parent)
        wanted = str(parent_id).strip().lower()
        return tuple(item for item in self._presets if item.parent_id == wanted)

    def get_parent(self, region_id: str | None) -> RegionPreset | None:
        """直接上级；根节点或 id 不存在时返回 ``None``。"""
        item = self.get(region_id)
        if item is None or item.parent_id is None:
            return None
        return self.get(item.parent_id)

    def get_ancestors(self, region_id: str | None) -> tuple[RegionPreset, ...]:
        """全部上级，**从最远（根）到最近**——拼起来就是一条面包屑。

        例如 ``get_ancestors("hangzhou")`` → ``(中国, 浙江省)``。

        目录里若存在环（坏数据），遍历会在第二个相同 id 处停下并返回已走过的
        部分：查询方法**永不因为坏数据而死循环**，环本身由
        :func:`~app.regions.validator.validate_catalog` 报出来。
        """
        chain: list[RegionPreset] = []
        seen: set[str] = set()
        current = self.get(region_id)
        if current is None:
            return ()
        seen.add(current.id)
        while current.parent_id is not None:
            if current.parent_id in seen:
                break
            parent = self.get(current.parent_id)
            if parent is None:
                break
            chain.append(parent)
            seen.add(parent.id)
            current = parent
        chain.reverse()
        return tuple(chain)

    def get_descendants(self, region_id: str | None) -> tuple[RegionPreset, ...]:
        """全部下级（递归，宽度优先），顺序稳定。

        同样带访问集合：坏数据里的环不会让它无限展开。
        """
        root = self.get(region_id)
        if root is None:
            return ()
        seen = {root.id}
        frontier = [root.id]
        result: list[RegionPreset] = []
        while frontier:
            next_frontier: list[str] = []
            for parent_id in frontier:
                for child in self.list_children(parent_id):
                    if child.id in seen:
                        continue
                    seen.add(child.id)
                    result.append(child)
                    next_frontier.append(child.id)
            frontier = next_frontier
        return tuple(result)

    def path_text(self, region_id: str | None, *, separator: str = " / ") -> str:
        """面包屑文本，如 ``中国 / 浙江省 / 杭州市``；id 不存在时返回空串。"""
        item = self.get(region_id)
        if item is None:
            return ""
        chain = self.get_ancestors(item.id)
        return separator.join([*(ancestor.name for ancestor in chain), item.name])

    # ------------------------------------------------------------------ #
    # 容器协议
    # ------------------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self._presets)

    def __iter__(self) -> Iterator[RegionPreset]:
        return iter(self._presets)

    def __contains__(self, region_id: object) -> bool:
        return self.get(str(region_id) if region_id is not None else None) is not None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<RegionCatalog {len(self._presets)} 条 root={self.root}>"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _stat_stamp(path: Path) -> tuple[int, int] | None:
    """文件的 ``(mtime_ns, size)``；不存在时 ``None``。"""
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _coerce_level(level: object) -> RegionLevel:
    """把 ``RegionLevel`` 或它的 ``value`` 归一成枚举；其余一律报错。

    **刻意不宽容**（Spec §13 要能查出"未知 RegionLevel"）：把 ``"provinces"``
    悄悄当成 ``CUSTOM`` 只会让调用方拿到一个莫名其妙的空列表。
    """
    if isinstance(level, RegionLevel):
        return level
    if isinstance(level, str):
        text = level.strip().lower()
        for item in RegionLevel:
            if item.value == text:
                return item
    raise ValueError(
        f"level 必须是 RegionLevel 或它的取值，实际为 {level!r}（可选："
        + "、".join(item.value for item in RegionLevel)
        + "）"
    )
