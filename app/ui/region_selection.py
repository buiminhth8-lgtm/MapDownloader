"""行政区域级联选择 + Zoom 模式（Slice 19，Spec §2 / §4 / §5 / §6 / §12）。

界面上"国家 → 省 → 市"三段下拉与"推荐 Zoom / 自定义 Zoom"两个单选本身不含
任何业务规则：它们只是把用户的动作转给本模块，由本模块去问
:class:`~app.regions.RegionService` 要数据、问
:class:`~app.planning.ZoomStrategyService` 要建议。规则收在这一处，
``DownloadPanel`` 就只剩下"画控件 + 转发事件"（Spec §12）—— 于是面板里
**没有一处**"等级 → Zoom"的映射，也**没有一处**解析 ``index.json`` 的代码。

三条不变量
----------

1. **层级只来自目录**（Spec §2）。级联的每一段都直接问 ``RegionService``；
   本模块不自己推导 ``parent``、不扫描 GeoJSON、不读 ``index.json``。
   目录里没有的 id 一律拒绝，并把原因写成一句人话（Spec §13）。
2. **Zoom 数值只来自策略表**（Spec §4 / §5）。本模块里没有任何 Zoom 区间常量：
   推荐一律走 :meth:`ZoomStrategyService.recommend`，评估一律走
   :meth:`ZoomStrategyService.evaluate`。改区间只需要改
   :data:`~app.planning.DEFAULT_ZOOM_POLICIES` 那一个地方。
3. **用户的 Zoom 值住在控件里，本类不缓存**（Spec §5 的"不偷偷改用户输入"）。

第 3 条值得多说一句，因为它决定了本类的接口形状。:meth:`recommendation` /
:meth:`effective_range` 都把用户当前的 Min/Max 当**入参**接进来，而不是自己存
一份。好处是"推荐 → 自定义：保留当前 Min/Max 作为初始值"这句话**不需要任何
代码** —— 切过去时控件里本来就是推荐值；也就根本不存在"控制器里那份与界面上
那份不一致"这种只有切几次模式之后才会显形的缺陷。

为什么不做成 ``QObject``
-----------------------

控制器要被纯逻辑用例直接构造与断言（不需要 ``QApplication``），也要能在验收
脚本里脱离窗口单独跑。信号由面板发 —— 面板本来就有 ``region_changed``。
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING, Any

from app.geo import GeoJSONError, RegionBoundaryMissing
from app.geo.bounds import GeoBounds
from app.planning import RegionLevel, ZoomRecommendation, ZoomStrategyService
from app.regions import RegionCatalogError, RegionPreset, RegionService

if TYPE_CHECKING:  # pragma: no cover - 只给类型检查器看
    from app.geo.geojson_loader import GeoJSONDocument
    from app.geo.region import DownloadRegion

__all__ = ["ZoomMode", "RegionSelectionController"]

_logger = logging.getLogger(__name__)


class ZoomMode(Enum):
    """下载策略（Spec §1）。

    ``value`` 会写进界面偏好（``QSettings``），因此它是**持久化契约**：
    改名等于让老配置读不出来（读不出来会安全回落到推荐模式，见
    :meth:`RegionSelectionController.set_zoom_mode`）。
    """

    #: 推荐 Zoom：Min/Max 由区域等级决定，控件只读（Spec §4）。
    RECOMMENDED = "recommended"

    #: 自定义 Zoom：用户说了算，超范围只提醒（Spec §5）。
    CUSTOM = "custom"

    @classmethod
    def parse(cls, value: object) -> "ZoomMode":
        """宽容解析：认不出来的一律按 :attr:`RECOMMENDED` 处理。

        与 :meth:`RegionLevel.parse` 同一套路（"读历史数据的那一侧要宽容"）：
        偏好里存着一个本版本不认识的模式时，"回到推荐"是最安全的落点 ——
        推荐模式不会替用户改任何东西（它自己就是一套被算出来的值），
        而猜成"自定义"则会把上次偶然留下的两个数字当成用户的选择。

        >>> ZoomMode.parse("custom") is ZoomMode.CUSTOM
        True
        >>> ZoomMode.parse(None) is ZoomMode.RECOMMENDED
        True
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            for item in cls:
                if item.value == normalized:
                    return item
        return cls.RECOMMENDED

    @property
    def display_name(self) -> str:
        """界面上那个单选的文案（与 Spec §1 逐字一致）。"""
        return "推荐 Zoom" if self is ZoomMode.RECOMMENDED else "自定义 Zoom"


class RegionSelectionController:
    """国家 / 省 / 市三级选择 + Zoom 模式（Spec §12）。

    Attributes:
        service: 区域业务入口（层级、边界、DownloadRegion）。
    """

    def __init__(
        self,
        service: RegionService | None = None,
        *,
        strategy: ZoomStrategyService | None = None,
    ) -> None:
        """
        Args:
            service: 区域服务。``None`` 时造一个默认实例（它跟随
                ``app.geo.region.REGION_RESOURCE_DIR`` 这个全局，验收脚本把资源
                重定向到临时目录时本控制器会自动跟着走）。
            strategy: Zoom 策略服务。``None`` 时用 ``service`` 持有的那一个 ——
                刻意复用而不是新建：两者读的是同一张策略表，多一个实例不会算出
                不同结果，但会多一处要同步的注入点。
        """
        self._service = service if service is not None else RegionService()
        self._strategy = strategy if strategy is not None else self._service.strategy

        self._country_id: str | None = None
        self._province_id: str | None = None
        self._city_id: str | None = None
        self._county_id: str | None = None
        self._zoom_mode: ZoomMode = ZoomMode.RECOMMENDED
        self._error: str = ""

    # ------------------------------------------------------------------ #
    # 只读状态
    # ------------------------------------------------------------------ #
    @property
    def service(self) -> RegionService:
        """区域服务（面板把它透出去给验收脚本用）。"""
        return self._service

    @property
    def strategy(self) -> ZoomStrategyService:
        """Zoom 策略服务。"""
        return self._strategy

    @property
    def error(self) -> str:
        """最近一次操作失败的原因（人话；正常是空串）。

        只记**最近**一次：界面每次刷新都会重新算一遍，用户需要看到的是
        "现在为什么不行"，而不是一份历史错误清单。
        """
        return self._error

    # ------------------------------------------------------------------ #
    # 级联查询（Spec §2：一律走 RegionService）
    # ------------------------------------------------------------------ #
    def list_countries(self) -> tuple[RegionPreset, ...]:
        """全部全国级区域。

        目录为空（或读不出来）时返回空元组并把原因写进 :attr:`error` ——
        界面据此显示一句可读的错误，而不是崩在 ``QComboBox`` 里（Spec §13）。
        """
        try:
            entries = self._service.list_countries()
        except RegionCatalogError as exc:  # pragma: no cover - 目录本身不抛
            return self._fail(f"行政区域目录不可用：{exc}")
        if not entries:
            self._error = "行政区域目录里没有任何国家级区域，请检查 resources/regions/index.json"
        else:
            self._error = ""
        return entries

    def list_provinces(self, country_id: str | None = None) -> tuple[RegionPreset, ...]:
        """某个国家下的省级区域；没有国家（或国家不在目录里）时返回空元组。"""
        target = country_id if country_id is not None else self._country_id
        if not target:
            return ()
        try:
            return self._service.list_provinces(target)
        except RegionCatalogError as exc:
            return self._fail(f"读不到 {target} 的下级省份：{exc}")

    def list_cities(self, province_id: str | None = None) -> tuple[RegionPreset, ...]:
        """某个省下的城市级区域；没有省（或省不在目录里）时返回空元组。

        省下面没有城市**不是错误**：目录可能只到省级，界面该显示"暂无数据"，
        而不是弹一句报错。
        """
        target = province_id if province_id is not None else self._province_id
        if not target:
            return ()
        try:
            return self._service.list_cities(target)
        except RegionCatalogError as exc:
            return self._fail(f"读不到 {target} 的下级城市：{exc}")

    def list_counties(self, city_id: str | None = None) -> tuple[RegionPreset, ...]:
        """某个市下的县级区域；没有市（或市不在目录里）时返回空元组。

        县直接挂在省下面（直辖市 / 省直辖县级市）时，调用方应以 ``省`` 作为
        父级来查 —— 见 :meth:`~app.regions.RegionService.list_counties` 的说明。
        """
        target = city_id if city_id is not None else self._city_id
        if not target:
            return ()
        try:
            return self._service.list_counties(target)
        except RegionCatalogError as exc:
            return self._fail(f"读不到 {target} 的下级县区：{exc}")

    def children_text(self, parent_id: str | None) -> str:
        """面包屑文本（``中国 / 浙江省 / 杭州市``），给界面显示用。"""
        if parent_id is None:
            return ""
        try:
            return self._service.path_text(parent_id)
        except RegionCatalogError:  # pragma: no cover - id 来自本控制器，必定存在
            return ""

    # ------------------------------------------------------------------ #
    # 级联选择（Spec §2）
    # ------------------------------------------------------------------ #
    def select_country(self, region_id: str | None) -> bool:
        """选中一个国家；**同时清空省、市与县**（Spec §2）。

        Returns:
            ``True`` 表示这个 id 确实是目录里的国家级条目。
        """
        entry = self._lookup(region_id, what="国家")
        if entry is None:
            return False
        if not entry.is_country:
            return bool(self._fail(f"{entry.name} 不是国家级区域（等级：{entry.level.display_name}）"))
        self._country_id = entry.id
        # 换国家 → 旧国家下面的省、市、县一律作废。留着它们会造出"国家是 A、
        # 省是 A 的下级"这种跨国家的组合，而界面上看不出来。
        self._province_id = None
        self._city_id = None
        self._county_id = None
        self._error = ""
        return True

    def select_province(self, region_id: str | None) -> bool:
        """选中一个省；传 ``None`` / 空串表示"全部（不限定省）"。

        "不选省"是合法状态（用户只想下这个国家），此时等级回到国家级。
        """
        if not (region_id or "").strip():
            self._province_id = None
            self._city_id = None
            self._county_id = None
            self._error = ""
            return True
        entry = self._lookup(region_id, what="省份")
        if entry is None:
            return False
        if not entry.is_province:
            return bool(self._fail(f"{entry.name} 不是省级区域（等级：{entry.level.display_name}）"))
        if self._country_id and entry.parent_id != self._country_id:
            return bool(
                self._fail(
                    f"{entry.name} 不属于当前选择的国家（它挂在 {entry.parent_id} 下）"
                )
            )
        self._province_id = entry.id
        self._city_id = None
        self._county_id = None
        self._error = ""
        return True

    def select_city(self, region_id: str | None) -> bool:
        """选中一个市；传 ``None`` / 空串表示"全部（不限定市）"。

        只选到省时等级是省级，选到市才是城市级（Spec §2）。
        """
        if not (region_id or "").strip():
            self._city_id = None
            self._county_id = None
            self._error = ""
            return True
        entry = self._lookup(region_id, what="城市")
        if entry is None:
            return False
        if not entry.is_city:
            return bool(self._fail(f"{entry.name} 不是城市级区域（等级：{entry.level.display_name}）"))
        if self._province_id and entry.parent_id != self._province_id:
            return bool(
                self._fail(
                    f"{entry.name} 不属于当前选择的省份（它挂在 {entry.parent_id} 下）"
                )
            )
        self._city_id = entry.id
        self._county_id = None
        self._error = ""
        return True

    def select_county(self, region_id: str | None) -> bool:
        """选中一个县；传 ``None`` / 空串表示"全部（不限定县）"。

        选到县时等级才是县级（Slice 27）。
        """
        if not (region_id or "").strip():
            self._county_id = None
            self._error = ""
            return True
        entry = self._lookup(region_id, what="县区")
        if entry is None:
            return False
        if not entry.is_county:
            return bool(self._fail(f"{entry.name} 不是县级区域（等级：{entry.level.display_name}）"))
        # 县的父级是市或省（省直辖县 / 直辖市的下辖区），两者都算合法。
        if self._city_id and entry.parent_id != self._city_id:
            return bool(
                self._fail(
                    f"{entry.name} 不属于当前选择的城市（它挂在 {entry.parent_id} 下）"
                )
            )
        if not self._city_id and self._province_id and entry.parent_id != self._province_id:
            return bool(
                self._fail(
                    f"{entry.name} 不属于当前选择的省份（它挂在 {entry.parent_id} 下）"
                )
            )
        self._county_id = entry.id
        self._error = ""
        return True

    def select_region(self, region_id: str | None) -> bool:
        """按任意一条目录 id **铺满三级**（回填任务记录 / 恢复偏好用）。

        与三个 ``select_*`` 的关系：它们是"用户点了某一段下拉"，本方法是
        "给我一条 id，你自己想办法把它拆到三段上"。拆法是走目录的祖先链
        （``RegionService.ancestors`` 从根到直接父），因此**不需要**在这里
        重新实现一遍"谁是谁的父亲"。

        Returns:
            ``True`` 表示这条 id 是目录里的国家 / 省 / 市之一，级联已经对齐。
            非行政层级的条目（将来的任务区之类）返回 ``False`` —— 界面上没有
            这一段下拉，硬塞进来会让"当前选中的是哪个区域"变得看不出来。
        """
        entry = self._lookup(region_id, what="行政区域")
        if entry is None:
            return False
        if not entry.level.is_administrative:
            return bool(
                self._fail(
                    f"{entry.name} 的等级是{entry.level.display_name}，"
                    "当前界面只支持国家 / 省 / 市 / 县四级"
                )
            )
        try:
            chain = [*self._service.ancestors(entry.id), entry]
        except RegionCatalogError as exc:  # pragma: no cover - 条目刚从目录里取出
            return bool(self._fail(str(exc)))

        # 先清空：从"选了县"切到"只选省"时，那条链里根本没有县级条目，
        # 留着旧的县就会造出"省=示例北省、县=示例河县"—— 表面上看着没错，
        # 但用户点的是"杭州市（省）"而实际下载的还是"杭州市（县）"。
        self._country_id = None
        self._province_id = None
        self._city_id = None
        self._county_id = None
        for item in chain:
            if item.is_country:
                self._country_id = item.id
            elif item.is_province:
                self._province_id = item.id
            elif item.is_city:
                self._city_id = item.id
            elif item.is_county:
                self._county_id = item.id
        self._error = ""
        return True

    def reset(self) -> None:
        """回到"什么都没选"（清空四级，保留 Zoom 模式）。"""
        self._country_id = None
        self._province_id = None
        self._city_id = None
        self._county_id = None
        self._error = ""

    # ------------------------------------------------------------------ #
    # 当前区域（Spec §3）
    # ------------------------------------------------------------------ #
    @property
    def country(self) -> RegionPreset | None:
        """当前选中的国家。"""
        return None if self._country_id is None else self._service.get_region(self._country_id)

    @property
    def province(self) -> RegionPreset | None:
        """当前选中的省（"全部"时是 ``None``）。"""
        return None if self._province_id is None else self._service.get_region(self._province_id)

    @property
    def city(self) -> RegionPreset | None:
        """当前选中的市（"全部"时是 ``None``）。"""
        return None if self._city_id is None else self._service.get_region(self._city_id)

    @property
    def county(self) -> RegionPreset | None:
        """当前选中的县（"全部"时是 ``None``）。"""
        return None if self._county_id is None else self._service.get_region(self._county_id)

    @property
    def selected_region_id(self) -> str | None:
        """**当前生效**的区域 id：县 → 市 → 省 → 国（Spec §2 的推导规则）。

        单一字段（而不是四个各自为政的字段）是 Spec §3 的要求：下游只认这一个
        id，不存在"等级说是县、id 却是市"这种组合。
        """
        return self._county_id or self._city_id or self._province_id or self._country_id

    @property
    def selected(self) -> RegionPreset | None:
        """当前生效的目录条目。"""
        region_id = self.selected_region_id
        return None if region_id is None else self._service.get_region(region_id)

    @property
    def level(self) -> RegionLevel:
        """当前区域的规划等级。

        什么都没选时是 :attr:`RegionLevel.CUSTOM` —— "没有等级"与"自定义"
        在这里是同一件事：不套用行政等级规则（Spec §7）。
        """
        entry = self.selected
        return RegionLevel.CUSTOM if entry is None else entry.level

    @property
    def path_text(self) -> str:
        """当前区域的面包屑（``中国 / 浙江省 / 杭州市``）；没有选中时是空串。"""
        return self.children_text(self.selected_region_id)

    @property
    def boundary_available(self) -> bool:
        """当前区域的边界文件就位了没有。"""
        region_id = self.selected_region_id
        if not region_id:
            return False
        try:
            return self._service.boundary_available(region_id)
        except RegionCatalogError:  # pragma: no cover - id 来自本控制器
            return False

    def missing_message(self) -> str:
        """边界文件缺失时给用户看的那段话（放哪、叫什么、要什么授权）。"""
        region_id = self.selected_region_id
        if not region_id:
            return "还没有选择行政区域"
        try:
            return self._service.missing_message(region_id)
        except RegionCatalogError as exc:  # pragma: no cover - id 来自本控制器
            return str(exc)

    def load_document(self, region_id: str | None = None) -> "GeoJSONDocument":
        """读当前（或指定）区域的边界文档 —— 面板据此回填只读的 West/South/…

        读盘这件事交给加载器（路径安全、缺失文案、GeoJSON 报错都在它那里），
        面板只拿到一份已经解释清楚异常的文档，或者一个会被翻译成人话的异常。

        Raises:
            RegionCatalogError: id 不在目录里。
            RegionBoundaryMissing: 边界文件不存在（消息里写清放哪、叫什么）。
            GeoJSONError: 边界文件不合法。
        """
        target = region_id if region_id is not None else self.selected_region_id
        return self._service.loader.load_document(target)

    def download_region(
        self,
        *,
        bounds: GeoBounds | None = None,
        document: "GeoJSONDocument | None" = None,
        geometry: Any = None,
    ) -> "DownloadRegion | None":
        """当前区域对应的 :class:`~app.geo.DownloadRegion`（Spec §3）。

        **本方法永不抛异常**，返回 ``None`` 表示"拿不到可信的区域"，
        原因在 :attr:`error` 里。调用方必须据此拒绝开工 —— 行政区域模式下
        退回矩形会静默下一大片区域外的瓦片（Slice 16 的红线）。

        Args:
            bounds: 已经算好的外包矩形（来自几何，不是手填）。
            document: 面板为了显示范围已经读过的那份文档；给了就复用，
                保证"界面上显示的区域"与"实际裁剪的区域"来自同一次读取。
            geometry: 同上（``document`` 优先）。
        """
        region_id = self.selected_region_id
        if region_id is None:
            self._error = "还没有选择行政区域"
            return None
        try:
            region = self._service.get_download_region(
                region_id, bounds=bounds, document=document, geometry=geometry
            )
        except RegionCatalogError as exc:
            self._error = f"行政区域不在目录里：{exc}"
            return None
        except (RegionBoundaryMissing, GeoJSONError) as exc:
            # 边界缺失 / 损坏是**明确失败**：用户看到的是"文件该放哪"，
            # 而不是类名
            self._error = str(exc)
            return None
        except ValueError as exc:
            self._error = str(exc)
            return None
        self._error = ""
        return region

    # ------------------------------------------------------------------ #
    # Zoom 模式与推荐（Spec §4 / §5 / §6）
    # ------------------------------------------------------------------ #
    @property
    def zoom_mode(self) -> ZoomMode:
        """当前下载策略。"""
        return self._zoom_mode

    def set_zoom_mode(self, mode: object) -> ZoomMode:
        """切换下载策略，返回真正生效的那个。

        本方法**不写任何 Zoom 值**：推荐 → 自定义时"保留当前 Min/Max"
        这件事不需要做（切过去时控件里本来就是推荐值），自定义 → 推荐时
        由面板把推荐值填进控件（见 ``DownloadPanel._apply_zoom_mode``）。
        把写入权只留给面板一处，就不会出现"两个地方都在改控件"的循环。
        """
        self._zoom_mode = ZoomMode.parse(mode)
        return self._zoom_mode

    @property
    def has_level_policy(self) -> bool:
        """当前区域是否有行政等级（矩形 / 未选择 → ``False``）。

        为 ``False`` 时不套用任何等级策略 —— 这正是 Spec §7 要的
        "自定义矩形不要强制使用国家 / 省 / 市推荐策略"。
        """
        return self.level.is_administrative

    @property
    def recommended_range(self) -> tuple[int, int] | None:
        """等级建议区间（纯推荐，不看用户输入）；没有等级时是 ``None``。"""
        if not self.has_level_policy:
            return None
        try:
            recommendation = self._strategy.recommend(self.level)
        except ValueError as exc:
            self._fail(f"Zoom 策略不可用：{exc}")
            return None
        return (recommendation.min_zoom, recommendation.max_zoom)

    def effective_range(self, min_zoom: int, max_zoom: int) -> tuple[int, int]:
        """这次**实际会拿去下载**的区间。

        推荐模式下是等级建议值（Spec §4），自定义模式下原样返回用户的值
        （Spec §5：一个字符都不改）。矩形模式没有等级 → 恒为用户的值。
        """
        if self._zoom_mode is ZoomMode.RECOMMENDED:
            recommended = self.recommended_range
            if recommended is not None:
                return recommended
        return (int(min_zoom), int(max_zoom))

    def recommendation(self, min_zoom: int, max_zoom: int) -> ZoomRecommendation | None:
        """推荐 / 评估结论（Spec §4 / §5）。

        自定义矩形（等级为 :attr:`RegionLevel.CUSTOM`）也返回结论：那份策略
        是"全范围、不警告"，界面据此知道"这一行没有建议可给"。是否显示由
        调用方看 :attr:`ZoomPolicy.is_custom` 决定。

        策略层抛异常（例如用户在自定义模式下把 Min 拉到比 Max 大）时返回
        ``None`` 并把原因写进 :attr:`error` —— 界面显示一句人话，
        而不是让异常从 Qt 槽里漏出去被静默吞掉。
        """
        low, high = self.effective_range(min_zoom, max_zoom)
        try:
            return self._strategy.evaluate(self.level, low, high)
        except ValueError as exc:
            self._fail(f"Zoom 策略不可用：{exc}")
            return None

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _lookup(self, region_id: str | None, *, what: str) -> RegionPreset | None:
        """按 id 取条目；取不到就记一条人话错误并返回 ``None``。"""
        wanted = (region_id or "").strip().lower()
        if not wanted:
            self._fail(f"没有提供{what}的 id")
            return None
        entry = self._service.get_region(wanted)
        if entry is None:
            self._fail(f"目录里没有{what} {wanted!r}，请检查 resources/regions/index.json")
            return None
        return entry

    def _fail(self, message: str) -> str:
        """记下原因并返回空元组（让查询方法能一行写完 ``return self._fail(...)``）。"""
        self._error = message
        _logger.debug("行政区域选择：%s", message)
        return ()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<RegionSelectionController 选中={self.selected_region_id!r} "
            f"模式={self._zoom_mode.value}>"
        )
