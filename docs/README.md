# 文档地图

> 这里的文档**不按 Slice 编号组织**，按"你现在想干什么"组织。
> 每份文档只回答一个问题，标题下面第一句话就是那个问题。
>
> 全部截图在 `docs/images/`（验收脚本的输出目录就是这个，别往 `docs/` 根目录丢 png）。

---

## 一分钟定位

| 我想…… | 读这份 | 它回答的问题 |
| --- | --- | --- |
| 先跑起来 | [README](../README.md) 的「快速开始」 | 五分钟内怎么下到第一张图 |
| 按省 / 市 / 县下载一块地 | [polygon_region_download](polygon_region_download.md) | 多边形范围怎么变成瓦片 |
| 知道"省和市从哪来" | [region_catalog](region_catalog.md) | 层级目录的数据模型 |
| 用三级/四级下拉选区域 | [region_selection_ui](region_selection_ui.md) | 界面怎么级联、怎么记上次的选择 |
| 不知道该下到多少级 | [zoom_strategy](zoom_strategy.md) | 推荐区间怎么算、为什么允许重叠 |
| 一次规划多块地 | [download_plan](download_plan.md) | 一份计划装多个区域 |
| 让计划自己跑 | [plan_scheduler](plan_scheduler.md) | 串行 / 暂停 / 继续 / 重试 / 跳过 |
| 沿一条航线下一窄条 | [mission_region](mission_region.md) | 按米缓冲的走廊 |
| 怕下太多把盘塞满 | [download_risk_analysis](download_risk_analysis.md) | 四级风险与强确认 |
| 把包交给 Android | [map_package_v1](map_package_v1.md) | **跨端契约**（字段、目录、版本） |
| 搞懂"分级"整条线 | [hierarchical_offline_map](hierarchical_offline_map.md) | 全国→省→市→县→任务区的总纲 |
| 把授权 GeoJSON 导进目录 | [region_data_pipeline](region_data_pipeline.md) | 导入管线与合规要求 |
| 验一遍整条链路 | [e2e_acceptance_checklist](e2e_acceptance_checklist.md) | Windows → ADB → Android 逐步验收 |
| 出问题了 | [troubleshooting](troubleshooting.md) | **按症状**查 |

---

## 推荐阅读路径

### A. 第一次接触这个项目（约 30 分钟）

1. [README](../README.md) —— 这套东西是什么、三个版本号、快速开始
2. [hierarchical_offline_map](hierarchical_offline_map.md) —— 先建立"分级"的整体印象
3. [map_package_v1](map_package_v1.md) —— 两端唯一的契约，先知道包里有什么
4. [e2e_acceptance_checklist](e2e_acceptance_checklist.md) 的 A/B/C 段 —— 看一条真实链路怎么走

### B. 只想用工具下地图（不关心实现）

[README](../README.md)「快速开始」→ [zoom_strategy](zoom_strategy.md)（定 Zoom）
→ [download_risk_analysis](download_risk_analysis.md)（看规模）→
[map_package_v1](map_package_v1.md) §8 自检清单 → README「传输到 Android」

出问题直接跳 [troubleshooting](troubleshooting.md)。

### C. 要改代码

先看你改的那一层：

| 改什么 | 先读 |
| --- | --- |
| 区域目录 / 层级 / 边界文件 | [region_catalog](region_catalog.md) + [region_data_pipeline](region_data_pipeline.md) |
| 下载面板 / 级联下拉 | [region_selection_ui](region_selection_ui.md) |
| Zoom 推荐与警告 | [zoom_strategy](zoom_strategy.md) |
| 计划与调度 | [download_plan](download_plan.md) + [plan_scheduler](plan_scheduler.md) |
| 任务区（走廊） | [mission_region](mission_region.md) |
| 风险与确认框 | [download_risk_analysis](download_risk_analysis.md) |
| 包格式 / 跨端字段 | [map_package_v1](map_package_v1.md) + [hierarchical_offline_map](hierarchical_offline_map.md) §4 |

**改契约字段的话**：先读 [hierarchical_offline_map](hierarchical_offline_map.md) §4
与 [map_package_v1](map_package_v1.md) §6，再跑
`tests/test_package_protocol_v1.py`（它会跨仓库核对 Android 侧的枚举与字段）。

### D. 要验收 / 发布

[e2e_acceptance_checklist](e2e_acceptance_checklist.md) —— 按 A~E 段走。
标 ★ 的是回归必跑项。E 段末尾有"已知限制"与"已修缺陷"两张表，
**别把已知限制当 bug 报，也别把已修缺陷当通过**。

---

## 按主题分组（想看全貌时用）

### 1. 下什么（范围）

- [polygon_region_download](polygon_region_download.md) —— 矩形 / 多边形两种范围
- [region_catalog](region_catalog.md) —— 层级目录：国家 / 省 / 市 / 县
- [mission_region](mission_region.md) —— 航线走廊（非行政范围）

### 2. 下多少（Zoom 与规模）

- [zoom_strategy](zoom_strategy.md) —— 推荐区间、为什么刻意重叠、警告阈值
- [download_risk_analysis](download_risk_analysis.md) —— 四级风险、强确认、计划级四选一

### 3. 怎么组织（计划与调度）

- [download_plan](download_plan.md) —— 一份计划装多块地，先算总量
- [plan_scheduler](plan_scheduler.md) —— 串行执行、七种状态、暂停/继续/取消/重试/跳过

### 4. 怎么交出去（打包与跨端）

- [map_package_v1](map_package_v1.md) —— **包格式契约**（目录、字段、版本兼容）
- [hierarchical_offline_map](hierarchical_offline_map.md) —— 分级体系总纲，含 Android 侧 AUTO 选包
- [e2e_acceptance_checklist](e2e_acceptance_checklist.md) —— 端到端验收

### 5. 数据从哪来

- [region_data_pipeline](region_data_pipeline.md) —— 授权 GeoJSON → RegionCatalog
- [region_catalog](region_catalog.md) §7 —— 授权要求（**必读，有法律约束**）

### 6. 界面与排查

- [region_selection_ui](region_selection_ui.md) —— 级联选择与偏好持久化
- [troubleshooting](troubleshooting.md) —— 按症状查

---

## 文档规范（改文档前先看）

1. **一份文档只回答一个问题。** 标题下面第一句话必须是那个问题，
   读完第一句就知道要不要继续。写不下就拆，别把两件事塞进一份。
2. **不拿 Slice 编号当状态。** "Slice 19 已实现"会过期，写"已实现"或
   "本版本不支持"。Slice 编号只留在历史章节和 commit 里。
3. **不重复写数字。** 版本号、字段名、Zoom 区间、阈值都只有一个真值来源
   （代码里的常量或 `map_package_v1.md`），文档解释它、不复制它。
   两份文档写同一个数字，迟早有一份是错的。
4. **截图放 `docs/images/`**，命名 `sliceNN_场景.png`。验收脚本
   （`scripts/verify_slice*.py`、`scripts/capture_ui.py`）已经默认写到这里。
   例外：`capture_ui.py` 的默认产物 `ui_main_window.png` 是**临时渲染图**
   （每次内容都不同），`.gitignore` 按文件名挡住了它，**文档里不要引用它** ——
   要留档就用 `-o docs/images/sliceNN_xxx.png` 指定成正式名字再提交。
5. **改完自查**（一条命令）：
   ```bash
   python scripts/check_docs.py
   ```
   它查四件事：`docs/` 根目录有没有杂物、图片引用有没有断、文档间链接有没有断、
   有没有孤儿文档（新增文档忘了加进本索引就会被抓到）。退出码非 0 就是没整理好。
6. **跨端契约只有一份真值。** Windows 侧与 Android 侧的字段、枚举、
   版本号一致性由测试机械核对（`tests/test_package_protocol_v1.py`），
   文档里的表是给人读的解释，**不是定义**。
