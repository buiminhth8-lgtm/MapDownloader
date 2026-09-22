"""规划层（Slice 17）：Zoom 策略核心。

回答"这种规模的区域，该用哪个 Zoom 区间"——并且**只回答建议**，
不替用户做决定（Spec §10 / §16）。

模块划分
--------

==============================  ==================================================
模块                            职责
==============================  ==================================================
:mod:`~app.planning.region_level`  :class:`RegionLevel` —— 全国 / 省 / 市 / 任务区 / 自定义
:mod:`~app.planning.models`        :class:`ZoomPolicy`、:class:`ZoomRecommendation`
:mod:`~app.planning.zoom_policy`   :data:`DEFAULT_ZOOM_POLICIES` —— 唯一权威策略表
:mod:`~app.planning.zoom_strategy` :class:`ZoomStrategyService`、:class:`ZoomPlanEstimate`
==============================  ==================================================

用法::

    from app.planning import RegionLevel, ZoomStrategyService

    service = ZoomStrategyService()
    service.recommend(RegionLevel.COUNTRY)          # 推荐 Z4~10
    service.evaluate(RegionLevel.COUNTRY, 8, 18)    # warning=True，但不改用户的 8~18
    service.recommend_with_estimate(region, RegionLevel.COUNTRY)   # 推荐 + 真实张数/容量

本层是**纯策略**：不 import UI、不发起请求、不写磁盘（计划文件除外）。估算部分复用
:mod:`app.geo` 的实现（Spec §9），不另起一套多边形统计。

``import app.planning`` 的导入顺序说明见 :mod:`app.planning.zoom_strategy` 的模块
文档 —— ``app.geo.region`` 需要 :class:`RegionLevel`，因此这里刻意让
:mod:`app.planning.region_level` 先被导入，且不允许任何子模块在导入期
import ``app.geo`` 的区域实现。

多级下载计划（Slice 20）
------------------------

====================================================================  ==================
模块                                                                  职责
====================================================================  ==================
:mod:`~app.planning.download_plan`                                    计划与条目的模型、输出命名规则
:mod:`~app.planning.plan_store`                                       ``data/plans.json`` 的读写
:mod:`~app.planning.plan_builder`                                     增量编辑（加 / 删 / 启停 / 定 Zoom）
:mod:`~app.planning.plan_estimator`                                   逐项估算 + 汇总（可取消）
:mod:`~app.planning.plan_validator`                                   校验（errors / warnings）
:mod:`~app.planning.plan_service`                                     编排入口 + 生成 ``DownloadTask``
====================================================================  ==================

**只有模型被转出**，那五个服务模块请直接从子模块导入::

    from app.planning import DownloadPlan            # 模型：这里就有
    from app.planning.plan_service import PlanService  # 服务：从子模块进

这不是随手定的，是**依赖图的要求**：``app.regions`` 在导入期会拉
``app.geo.region``（它要 :func:`~app.geo.region.is_safe_region_relative_path`
这类纯函数），而 ``app.geo.region`` 又会回头拉 ``app.planning.region_level``
—— 于是 ``import app.geo`` 这一条链会**在 ``app.geo.region`` 还没执行完的时候**
进入本模块的 ``__init__``。此时若本模块去 import ``app.planning.plan_builder``，
那条链就会走进 ``app.regions`` → ``app.geo.region``，撞上一个半初始化的模块::

    ImportError: cannot import name 'is_safe_region_relative_path'
                 from partially initialized module 'app.geo.region'

（这是实测出来的，不是推演；``tests/test_download_plan.py`` 里有一条子进程
用例把"任何导入顺序都能装好"钉住，另有一条断言这五个名字**不在**本模块上。）
"""

from app.planning.region_level import (
    ADMINISTRATIVE_LEVELS,
    PARENT_LEVEL_RULES,
    RegionLevel,
)

# 风险等级是**零依赖**的叶子模块（只 import Enum），因此它紧跟 region_level。
# 风险报告与策略只依赖它 + config + tile_math，也可以安全地在本层被装好。
# 分析器（``download_risk_analyzer``）**刻意不在这里转出**：它会拉
# ``app.geo`` 的估算层与计划模型，理由与下面那段导入顺序说明完全一样。
from app.planning.risk_level import RISK_LEVEL_ORDER, RISK_LABEL, RiskLevel
from app.planning.risk_models import (
    DownloadRiskReport,
    PlanItemRisk,
    PlanRiskReport,
)
from app.planning.risk_policy import DEFAULT_RISK_POLICY, RiskPolicy

# 先导入 region_level 再导入其余模块：`app.geo.region` 依赖它，而
# `app.planning` 的 __init__ 有可能正是被那条依赖触发的（见 zoom_strategy 文档）。
from app.planning.models import (
    ZOOM_CURRENT_LABEL,
    ZOOM_RECOMMEND_LABEL,
    ZOOM_WARNING_PREFIX,
    ZoomPolicy,
    ZoomRecommendation,
)

# 注意导入顺序：`download_plan` 只依赖叶子模块（region_level / tile_math /
# models.naming），因此它可以安全地出现在这里，也可以被 `app.geo` 那条链
# 上的任何模块顺带装好。
from app.planning.download_plan import (
    PLAN_LEVEL_ORDER,
    DownloadPlan,
    DownloadPlanItem,
    PlanItemStatus,
    item_file_stem,
    item_output_path,
    level_sort_key,
    plan_directory,
    plan_folder_name,
    planned_output_paths,
)

# 执行态模型（Slice 21）。同样只有**模型**：调度器、执行记录 store、后台 worker
# 一律从子模块导入（``app.planning.plan_scheduler`` / ``.plan_execution_store``）
# —— 理由与下面那段导入顺序说明完全一样，而且更硬：``plan_scheduler`` 会拉
# ``plan_service``（进而拉区域目录与任务服务），一旦从这里转出，``import app.geo``
# 那条链就会拖着半个应用一起初始化。
#
# 注意名字分工：``PlanItemStatus``（上面那个）是**可用性**（可用 / 不可用），
# ``PlanItemRunStatus``（这里）是**执行态**（等待 / 执行中 / 已完成 …）。
# 刻意不合并、也不重名，理由见 ``plan_execution`` 的模块文档。
from app.planning.plan_execution import (
    PlanExecution,
    PlanExecutionStatus,
    PlanItemExecution,
    PlanItemRunStatus,
    PlanProgress,
)
from app.planning.zoom_policy import (
    DEFAULT_ZOOM_POLICIES,
    get_zoom_policy,
    list_zoom_policies,
)
from app.planning.zoom_strategy import (
    PLAN_SIZE_LABEL,
    PLAN_TILES_LABEL,
    ZoomPlanEstimate,
    ZoomStrategyService,
)

__all__ = [
    "RegionLevel",
    "PARENT_LEVEL_RULES",
    "ADMINISTRATIVE_LEVELS",
    "ZoomPolicy",
    "ZoomRecommendation",
    "ZoomPlanEstimate",
    "ZoomStrategyService",
    "DEFAULT_ZOOM_POLICIES",
    "get_zoom_policy",
    "list_zoom_policies",
    "ZOOM_RECOMMEND_LABEL",
    "ZOOM_CURRENT_LABEL",
    "ZOOM_WARNING_PREFIX",
    "PLAN_TILES_LABEL",
    "PLAN_SIZE_LABEL",
    # --- 下载风险分析（Slice 23）：同样只有模型 + 策略 ---
    "RiskLevel",
    "RISK_LEVEL_ORDER",
    "RISK_LABEL",
    "DownloadRiskReport",
    "PlanItemRisk",
    "PlanRiskReport",
    "RiskPolicy",
    "DEFAULT_RISK_POLICY",
    # --- 多级下载计划（Slice 20）：只有模型 ---
    "DownloadPlan",
    "DownloadPlanItem",
    "PlanItemStatus",
    "PLAN_LEVEL_ORDER",
    "level_sort_key",
    "plan_folder_name",
    "plan_directory",
    "item_file_stem",
    "item_output_path",
    "planned_output_paths",
    # --- 计划执行（Slice 21）：同样只有模型 ---
    "PlanExecution",
    "PlanExecutionStatus",
    "PlanItemExecution",
    "PlanItemRunStatus",
    "PlanProgress",
]
