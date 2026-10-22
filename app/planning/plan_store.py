"""下载计划的持久化（Slice 20，Spec §17 / §18）。

**一个 JSON 文件，一张表**：``~/MapDownloader/data/plans.json``。

结构与取舍
----------

与 :mod:`app.tasks.task_store` 是同一套做法（原子写 + 字段白名单 +
密钥自检 + 损坏隔离），因为面对的是同一类问题："几十行、会被反复重写、
写坏了不该让程序起不来"。

但**文件是分开的**（见 :data:`~app.config.settings.PLANS_FILE_NAME` 的说明）：
任务记录在下载过程中被高频覆盖，计划只在用户改计划时写一次。共用一个文件
意味着任何一次进度落盘都会连带重写计划，一份半截写入就同时毁掉两份数据。

写盘的字段（Spec §17）
----------------------

计划级：``id`` / ``name`` / ``output_dir`` / ``map_layer`` / ``with_labels``；
条目级：``id`` / ``region_id`` / ``region_name`` / ``region_level`` /
``min_zoom`` / ``max_zoom`` / ``enabled``。

``output_dir`` 与 ``map_layer`` 不在 Spec §17 逐字列出的清单里，但必须存：
一个重启后连"下到哪、哪个图层"都不知道的计划，恢复出来也生成不了任务
（Spec §18 要求恢复后可用）。它们与 Token 无关，也不含几何。

刻意**不**写盘的：Token（根本不存在这个字段）、几何 / GeoJSON、
瓦片坐标列表、以及**估算结果与可用性判定**——后两者是"算出来的结论"，
每次打开重算一遍即可（本地计算，不联网），而写进磁盘就等于把"当时对"
当成"永远对"（用户删掉一个区域后，那份判定会一直显示"可用"）。

条目级字段的白名单是逐层检查的：``region_level`` 这种"看起来像新东西"的
键一旦漏进 payload，会在这里炸，而不是把一份没人审计过的数据写上磁盘。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config.settings import (
    DEFAULT_PLANS_FILE,
    PLANS_CORRUPT_SUFFIX,
    PLANS_FILE_VERSION,
)
from app.planning.download_plan import (
    DownloadPlan,
    DownloadPlanItem,
    new_item_id,
    new_plan_id,
)
from app.planning.region_level import RegionLevel

__all__ = [
    "PlanStore",
    "PlanStoreError",
    "item_from_payload",
    "item_to_payload",
    "plan_from_payload",
    "plan_to_payload",
]


_logger = logging.getLogger(__name__)


class PlanStoreError(RuntimeError):
    """计划无法读写（路径不可写、JSON 损坏到无法解析、字段非法等）。"""


#: 计划级允许出现在 payload 里的键（**白名单**）。
_PLAN_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "output_dir",
    "map_layer",
    "with_labels",
    "items",
)

#: 条目级允许出现的键。
_ITEM_FIELDS: tuple[str, ...] = (
    "id",
    "region_id",
    "region_name",
    "region_level",
    "min_zoom",
    "max_zoom",
    "enabled",
)

#: 字段名里出现这些片段即视为"疑似密钥"，拒绝写盘。
#:
#: 与 :data:`app.tasks.task_store._FORBIDDEN_KEY_FRAGMENTS` **逐字相同**，
#: 但刻意各存一份：``app.planning`` 在导入期不能拉 ``app.tasks``
#: （``app.tasks`` 要 ``app.geo``，而 ``app.geo`` 会回头拉 ``app.planning``，
#: 见 :mod:`app.planning` 的模块文档）。两份之间的同步由
#: ``tests/../test_plan_store`` 里那条"两份黑名单必须一致"的用例守着
#: ——它比"手工记得同步"多一道保险，又不必为一个常量和依赖图打架。
_FORBIDDEN_KEY_FRAGMENTS: tuple[str, ...] = (
    "token",
    "tk",
    "secret",
    "password",
    "passwd",
    "apikey",
    "api_key",
    "credential",
)

#: 单个计划最多保留多少条目。防的是"文件被写坏成一个巨大的数组"这类事故，
#: 不是产品限制：一个计划里出现几十个区域已经远超真实用法。
MAX_PLAN_ITEMS: int = 500


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
def item_to_payload(item: DownloadPlanItem) -> dict[str, Any]:
    """条目 → 可 JSON 序列化的 dict（只含白名单字段）。

    ``region_level`` 写枚举的 ``value``（``"country"``）而不是枚举名：
    与 :class:`~app.planning.RegionLevel` 的持久化契约一致（日志、文档、
    将来的 Android 侧都用同一串标识）。
    """
    payload: dict[str, Any] = {
        "id": item.id,
        "region_id": item.region_id,
        "region_name": item.region_name,
        "region_level": item.region_level.value,
        "min_zoom": int(item.min_zoom),
        "max_zoom": int(item.max_zoom),
        "enabled": bool(item.enabled),
    }
    extra = set(payload) - set(_ITEM_FIELDS)
    if extra:
        raise PlanStoreError(f"计划条目出现白名单外的字段：{sorted(extra)}")
    return payload


def plan_to_payload(plan: DownloadPlan) -> dict[str, Any]:
    """计划 → dict（只含白名单字段）。

    Raises:
        PlanStoreError: 出现白名单外的字段（同步 ``_PLAN_FIELDS`` 时的自检）。
    """
    payload: dict[str, Any] = {
        "id": plan.id,
        "name": plan.name,
        "output_dir": plan.output_dir,
        "map_layer": plan.map_layer,
        "with_labels": bool(plan.with_labels),
        "items": [item_to_payload(item) for item in plan.items],
    }
    extra = set(payload) - set(_PLAN_FIELDS)
    if extra:
        raise PlanStoreError(f"计划 payload 出现白名单外的字段：{sorted(extra)}")
    return payload


def item_from_payload(payload: Any) -> DownloadPlanItem:
    """dict → 条目。

    ``region_id`` 与两个 Zoom **是必填的**：缺了它们这一条就没有内容了。
    刻意**不用默认 Zoom 兜底**：默认值（``DEFAULT_MIN_ZOOM`` ~
    ``DEFAULT_MAX_ZOOM``）比一条手写记录里的常见值大得多，拿它顶上会让
    "文件被手工改坏了"变成"静默下载了几十万张瓦片"。宁可跳过这一条并记日志。

    Raises:
        PlanStoreError: 不是对象、缺 ``region_id``、缺 / 认不出 Zoom。
    """
    if not isinstance(payload, dict):
        raise PlanStoreError(f"计划条目不是 JSON 对象：{type(payload).__name__}")

    region_id = str(payload.get("region_id") or "").strip()
    if not region_id:
        raise PlanStoreError("计划条目缺少 region_id")

    return DownloadPlanItem(
        id=str(payload.get("id") or "") or new_item_id(),
        region_id=region_id,
        region_name=str(payload.get("region_name") or ""),
        region_level=RegionLevel.parse(payload.get("region_level")),
        min_zoom=_as_int(payload.get("min_zoom"), "min_zoom", region_id),
        max_zoom=_as_int(payload.get("max_zoom"), "max_zoom", region_id),
        enabled=_as_bool(payload.get("enabled", True), "enabled", region_id),
    )


def plan_from_payload(payload: Any) -> DownloadPlan:
    """dict → 计划。

    条目的**解析失败不会毁掉整个计划**（Spec §18 的同一条原则）：坏的那一条
    跳过并记一条日志，其余照常读出来。计划本身解析不了才抛
    :class:`PlanStoreError`。

    Raises:
        PlanStoreError: 不是对象、缺 ``items`` 数组、或 ``items`` 不是数组。
    """
    if not isinstance(payload, dict):
        raise PlanStoreError(f"计划不是 JSON 对象：{type(payload).__name__}")

    records = payload.get("items")
    if records is None:
        records = []
    if not isinstance(records, list):
        raise PlanStoreError("计划的 items 字段不是数组")

    items: list[DownloadPlanItem] = []
    skipped = 0
    for record in records[:MAX_PLAN_ITEMS]:
        try:
            items.append(item_from_payload(record))
        except PlanStoreError as exc:
            skipped += 1
            _logger.warning("跳过一条无法解析的计划条目：%s", exc)
    if skipped:
        _logger.warning("计划 %s 中有 %d 条条目无法解析，已跳过", payload.get("id"), skipped)

    plan = DownloadPlan(
        id=str(payload.get("id") or "") or new_plan_id(),
        name=str(payload.get("name") or ""),
        items=items,
        output_dir=str(payload.get("output_dir") or ""),
        map_layer=str(payload.get("map_layer") or ""),
        with_labels=_as_bool(payload.get("with_labels", False), "with_labels", "plan"),
    )
    # 估算结果不落盘（见模块文档），读回来先按"尚未估算"处理。
    plan.recalculate_totals()
    return plan


def _as_int(value: Any, field: str, region_id: str) -> int:
    """宽松转 int（JSON 里可能是 ``8.0`` 或 ``"8"``）。"""
    try:
        return int(float(value))
    except (TypeError, ValueError) as exc:
        raise PlanStoreError(
            f"计划条目 {region_id} 的 {field} 不是整数：{value!r}"
        ) from exc


def _as_bool(value: Any, field: str, region_id: str) -> bool:
    """转 bool，并**拒绝**把字符串 ``"false"`` 当成真。

    与 :func:`app.tasks.task_store._as_bool` 同一个理由：``bool("false")``
    是 ``True``，用它读文件会把用户手工写的 ``"false"`` 解释成"启用"，
    于是一个被明确关掉的条目又参与了下载。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text == "true":
        return True
    if text in ("false", ""):
        return False
    raise PlanStoreError(f"计划条目 {region_id} 的 {field} 不是布尔值：{value!r}")


def _assert_no_secret_keys(payload: Any, *, where: str = "payload") -> None:
    """递归检查 payload 的**键名**，命中疑似密钥片段即拒绝写盘。

    只查键不查值：值可能是用户起的计划名（叫 "token 试验" 也合理），
    而键名是我们自己控制的。
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).lower()
            for fragment in _FORBIDDEN_KEY_FRAGMENTS:
                if fragment in lowered:
                    raise PlanStoreError(
                        f"拒绝把疑似密钥字段写入计划文件：{where}.{key}"
                        "（Token 不得落盘）"
                    )
            _assert_no_secret_keys(value, where=f"{where}.{key}")
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            _assert_no_secret_keys(item, where=f"{where}[{index}]")


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #
class PlanStore:
    """计划文件的 JSON 读写。

    用法::

        store = PlanStore()                 # 默认 ~/MapDownloader/data/plans.json
        store.save(plan)                    # 按 id 新增或覆盖
        plans = store.load_all()

    与 :class:`~app.tasks.task_store.TaskStore` 一样**无状态**（除了路径）：
    几十行数据不值得为"内存里缓存一份"承担一致性风险。
    """

    def __init__(
        self,
        path: str | Path = DEFAULT_PLANS_FILE,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            path: 计划文件路径。父目录会在首次写入时自动创建。
            logger: 可选日志器。
        """
        self._path = Path(path)
        self._logger = logger or _logger

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    @property
    def path(self) -> Path:
        """计划文件路径。"""
        return self._path

    @property
    def exists(self) -> bool:
        """文件当前是否存在。"""
        return self._path.is_file()

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def load_all(self) -> list[DownloadPlan]:
        """读取全部计划。

        Returns:
            计划列表（文件顺序）。文件不存在、损坏到无法解析、或结构不对时
            返回空列表 —— **本方法不抛异常**：一份坏掉的计划文件不该让程序
            起不来（Spec §18：旧版本没有这个文件时也要正常启动）。
        """
        payload = self._read_payload()
        if payload is None:
            return []

        records = payload.get("plans")
        if not isinstance(records, list):
            self._quarantine("plans 字段不是数组")
            return []

        plans: list[DownloadPlan] = []
        skipped = 0
        for record in records:
            try:
                plans.append(plan_from_payload(record))
            except PlanStoreError as exc:
                # 单个计划坏了不拖累其它计划（与任务记录同一策略）。
                skipped += 1
                self._logger.warning("跳过一份无法解析的计划：%s", exc)
        if skipped:
            self._logger.warning("计划文件中有 %d 份计划无法解析，已跳过", skipped)
        return plans

    def get(self, plan_id: str) -> DownloadPlan | None:
        """按 id 取计划；不存在返回 ``None``。"""
        target = str(plan_id)
        for plan in self.load_all():
            if plan.id == target:
                return plan
        return None

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    def save(self, plan: DownloadPlan) -> DownloadPlan:
        """按 id 新增或覆盖一份计划（其余计划保持不动）。"""
        self.save_all([plan], merge=True)
        return plan

    def save_all(
        self, plans: list[DownloadPlan], *, merge: bool = True
    ) -> list[DownloadPlan]:
        """写入一批计划。

        Args:
            plans: 要写入的计划。
            merge: ``True`` 时与文件里现有的计划合并（同 id 覆盖）；
                ``False`` 时**整体替换**（删除等需要精确控制内容的操作用）。

        Returns:
            写入后的完整计划列表。

        Raises:
            PlanStoreError: 路径不可写，或 payload 里出现疑似密钥字段。
        """
        existing: dict[str, DownloadPlan] = {}
        if merge:
            for item in self.load_all():
                existing[item.id] = item
        for plan in plans:
            existing[plan.id] = plan

        payload = {
            "version": PLANS_FILE_VERSION,
            "plans": [plan_to_payload(item) for item in existing.values()],
        }
        # 第二道防线：写盘之前再按字段名自检一次
        _assert_no_secret_keys(payload)
        self._write_payload(payload)
        return self.load_all()

    def delete(self, plan_id: str) -> bool:
        """删除一份计划。

        **只删计划，不碰已生成的下载任务，也不碰任何 ``.mbtiles``**：
        计划是"打算做什么"的草稿，任务是"已经决定要做"的记录，
        它们的生命周期无关。

        Returns:
            ``True`` 表示确实删掉了一份；``False`` 表示本来就没有。
        """
        target = str(plan_id)
        current = self.load_all()
        remaining = [plan for plan in current if plan.id != target]
        if len(remaining) == len(current):
            return False
        payload = {
            "version": PLANS_FILE_VERSION,
            "plans": [plan_to_payload(item) for item in remaining],
        }
        _assert_no_secret_keys(payload)
        self._write_payload(payload)
        return True

    # ------------------------------------------------------------------ #
    # 内部：读写
    # ------------------------------------------------------------------ #
    def _read_payload(self) -> dict[str, Any] | None:
        """读文件并解析 JSON；不存在 / 损坏时返回 ``None``。"""
        if not self._path.is_file():
            return None
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            self._logger.error("读取计划文件失败：%s", exc)
            return None
        if not text.strip():
            self._logger.warning("计划文件为空：%s", self._path)
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            self._quarantine(f"JSON 解析失败：{exc}")
            return None
        if not isinstance(payload, dict):
            self._quarantine("顶层不是 JSON 对象")
            return None
        return payload

    def _write_payload(self, payload: dict[str, Any]) -> None:
        """原子写入：tmp → flush → fsync → ``os.replace``。

        Raises:
            PlanStoreError: 目录创建失败 / 写入失败。
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PlanStoreError(f"无法创建计划目录 {self._path.parent}：{exc}") from exc

        tmp_path = self._tmp_path()
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
        try:
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.write("\n")
                handle.flush()
                # fsync 是这里唯一"贵"的一步：它保证内容真的落到磁盘，
                # 而不是只进了页缓存——否则断电后 os.replace 可能把一个
                # 内容为空的新文件改名到位。
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._path)
        except OSError as exc:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:  # pragma: no cover - 清理失败不影响主流程
                pass
            raise PlanStoreError(f"写入计划文件失败 {self._path}：{exc}") from exc

    def _tmp_path(self) -> Path:
        """临时文件路径：同目录下的 ``plans.json.tmp``（同目录才保证原子改名）。"""
        return self._path.with_name(self._path.name + ".tmp")

    def _quarantine(self, reason: str) -> Path | None:
        """把损坏的计划文件改名隔离（**不删除**），返回新路径。"""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self._path.with_name(
            f"{self._path.name}{PLANS_CORRUPT_SUFFIX}.{stamp}"
        )
        try:
            os.replace(self._path, backup)
        except OSError as exc:  # pragma: no cover - 隔离失败也要让主流程继续
            self._logger.error("计划文件损坏（%s）且隔离失败：%s", reason, exc)
            return None
        self._logger.error(
            "计划文件损坏（%s），已隔离为 %s，本次按「没有计划」处理", reason, backup
        )
        return backup
