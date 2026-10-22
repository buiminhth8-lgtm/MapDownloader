# 分级离线地图体系（全国 → 省 → 市 → 县 → 任务区）

> 这份文档回答一个问题：**"分级"到底分的是什么、谁说了算、信息怎么从 Windows 走到手机。**
>
> 它横跨两个仓库（`D:\project\MapDownloader` 与 `D:\project\osmdroid`），
> 是 Slice 16~25 那条线（多边形区域 → 分级目录 → Zoom 策略 → 估算 → 风险 →
> 下载计划 → 调度 → 打包 → 导入 → AUTO 选包）的总入口。
>
> 想看**怎么手工验收**看 `e2e_acceptance_checklist.md`；想查**出毛病了怎么办**看
> `troubleshooting.md`；每个 Slice 自己的细节看各自的 `docs/*.md`。

---

## 1. 一句话：分级的本质是"让每一档 Zoom 只下一块合理的地"

一块地的瓦片量随 Zoom **指数**增长。同 1°×1° 的框（实测值，`calculate_tile_range`）：

| Zoom | 瓦片数 | 说明 |
| --- | --- | --- |
| Z4 | 1 | 全国也就几十张 |
| Z10 | 20 | 省级够用 |
| Z12 | 208 | 市级 |
| Z14 | 2,760 | 市级细看 |
| Z18 | **688,176** | 只有任务区这种小范围才敢下 |

所以"全国下到 Z18"不是"下得慢一点"：中国范围（约 62°×36°）在 Z18 是 **15 亿张**
（Z8 才 1,518 张）。分级要解决的正是：
**每一级区域有自己的合理 Zoom 区间，超出这个区间要有人拦一下。**

分级由两件正交的事组成，混在一起是最常见的误解：

| | 回答的问题 | 权威来源 |
| --- | --- | --- |
| **几何类型**（`RegionType`） | 这块地是矩形还是多边形？ | `app/geo/region.py` |
| **区域等级**（`RegionLevel`） | 这块地有多大、打算拿它干什么？ | `app/planning/region_level.py` |

一个省级区域既可以是多边形（拿了省界数据），也可以是矩形（用户手填范围）；
一个多边形既可能是全国，也可能是一条航线。**两者互不决定。**

---

## 2. 分级阶梯与唯一真值来源

| 等级 | `value` | 推荐 Zoom | 典型边界来源 | Android 具体度 |
| --- | --- | --- | --- | --- |
| 全国 / 大区域概览 | `country` | **Z4~10** | `generated/`（三份天地图数据合成的全国边界） | 1 |
| 省 / 自治区 / 直辖市 | `province` | **Z9~13** | `generated/`（`scripts/import_china_admin_regions.py`） | 2 |
| 市 / 地区 / 自治州 | `city` | **Z12~16** | 同上 | 3 |
| 县 / 区 / 县级市 / 旗 | `county` | **Z14~18** | 同上 | 4 |
| 作业区 / 航线走廊 | `mission` | **Z15~18** | 用户数据目录（**不在资源目录里**） | 5 |
| 自定义矩形 | `custom` | 不套用建议 | 无 | 0 |
| （Android 专有）未知 | `unknown` | — | 老包 / 认不出的 `level` | -1 |

> **「Android 具体度」是排序用的权重，不是 Zoom 区间**，两者互不相干：它只决定"两张包
> 都盖得住这个点、都支持当前 Zoom 时，哪张更该被选中"（越大越具体）。词汇表一旦分叉，
> 后果不是报错而是**静默降级** —— Android 的 `RegionLevel.parse` 认不出来就返回
> `UNKNOWN`，包照常可用，只是永远排在候选最后。所以这两个仓库的等级清单由
> `tests/test_package_protocol_v1.py` 直接读 Android 源码逐项比对（不再是"读不到就跳过"）。
> 上一次分叉正是 `county`：Windows 从 Slice 27 起就写它，Android 到 Slice 28 才认得。

三条硬规则：

1. **Zoom 区间只有一个来源**：`app/planning/zoom_policy.py` 的
   `DEFAULT_ZOOM_POLICIES`。界面不存数字、计划不存数字、Android **更不存数字** ——
   Android 只用包里带的 `minZoom` / `maxZoom` 与 `region.level`。
   任何一处出现第二份阈值表，加一级区域时必然有一处漏改。
2. **界面不许改用户输入**。推荐区间只用来**提示**（超范围给警告），
   从不 `clamp()` 用户填的值 —— "我明明填了 Z18，它偷偷改成 Z10" 是最难查的一类投诉。
3. **区间刻意重叠**（Z9~10 / Z12~13 / Z15~16）。重叠是设计，不是 bug：
   它让"再放大一点点"能自然地滑到下一档，而不是必须跨过一道缝。

---

## 3. Windows 侧：从一块地到一个包

```
                       resources/regions/index.json
                                  │
                    RegionCatalog（层级校验：省必须挂在国下）
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        │                         │                         │
   ZoomStrategyService      RegionTileEstimator        MissionRegionService
   （等级 → 推荐区间）       （多边形相交 → 选中张数）    （航线 + 米制 Buffer → 走廊）
        │                         │                         │
        └─────────────────────────┼─────────────────────────┘
                                  │
                          DownloadRiskAnalyzer
                    （**只读**已算好的估算，不重算瓦片）
                                  │
                      DownloadPlan（全国 → 省 → 市 → 县 → 任务区）
                                  │
                          PlanScheduler（严格串行）
                                  │
                     map_download_service → map.mbtiles
                                  │
                     MBTilesInspectionService（覆盖度）
                                  │
                     MapPackageService → 标准离线地图包
                                  │
                    adb push → Android
```

几处容易被忽略但很关键的：

- **估算分"候选"与"选中"两个数**。候选 = 外包矩形覆盖的瓦片；选中 = 与多边形**真的相交**的
  瓦片。分级计划的合计按**选中**算 —— 用候选数会虚报，而虚报的数字会被用户当真。
  轴对齐矩形区域上两者相等，所以这一条只有**非矩形**夹具才能测出来
  （`tests/conftest.py` 专门备了一块三角形）。
- **风险分析器不重算**。它只读估算结果（`selected_tiles` / `estimated_size_bytes` /
  `per_zoom`），因为"分析"调一次、重算一次 27M 张的瓦片范围会把界面冻住。
  这条有 AST 守卫：分析器里不许出现 `estimate` / `calculate_tile_range` 的调用。
- **两道闸门不叠加**。风险闸（10 万张）优先；只有 NORMAL 才轮到 Slice 7 那道
  1 万张的老闸。叠加的结果是"同一件事被问两次"。

风险阈值（两维取更高档，`app/planning/risk_policy.py`）：

| 档位 | 张数 | 容量 |
| --- | --- | --- |
| LARGE | ≥ 100,000 | ≥ 2 GiB |
| VERY_LARGE | ≥ 1,000,000 | ≥ 20 GiB |
| DANGEROUS | ≥ 10,000,000 | ≥ 100 GiB |

**容量与张数是两个维度、取更高的那一档**：一块 Z18 的小范围可能是"张数不多但
容量很大"（合成 PNG 比纯底图大好几倍），只看张数会漏。

---

## 4. 跨端元数据契约（`manifest.region`）

包里的 `region` 子对象是**唯一**一条把"这是什么级别的区域"带过端的通道。

| 键 | 谁写 | 说明 |
| --- | --- | --- |
| `type` | 全部 | **几何类型**：`rectangle` / `polygon`。**不是**等级 |
| `id` | 有区域时 | `china` / `hangzhou` / `mission-001` |
| `boundaryVersion` | 有边界版本时 | 这批瓦片是按哪一版几何裁的 |
| `level` | **全部**（Slice 25 起） | `country` / `province` / `city` / `county` / `mission` / `custom` |
| `bufferMeters` | 只有任务区 | 走廊半径（米） |
| `geometryVersion` | 只有任务区 | 任务区那份**不在资源目录里**的几何的版本 |

### 4.1 为什么 `level` 必须人人都写

Slice 22 只给**任务区**写 `level`，理由是"行政区的等级能从 `region_id` 反查"。
这句话**只对 Windows 自己成立**：Windows 有区域目录，可以拿 `id` 查表；
**Android 没有**。于是：

```
Windows:  region_id=hangzhou → 查目录 → CITY   ✅
Android:  region_id=hangzhou → 查不到 → RegionLevel.UNKNOWN   ❌（静默降级）
```

后果不是报错，是**静默降级**：包照常能导入、能显示，只是永远排在同级候选的最后。
Android 的 `RegionLevel` 有完整的 `getSpecificity()`（MISSION 4 > CITY 3 > PROVINCE 2 >
COUNTRY 1 > CUSTOM 0 > UNKNOWN -1），`OfflineMapSelector` 的 §5-3 就靠它 ——
等级全是 UNKNOWN 时那一级比较退化成"谁也不比谁优先"。

Slice 25 的修正：**只要有等级就写**（`app/geo/region.py` 的 `metadata_rows()` /
`manifest_region()` / `map_download_service` 的 metadata 写入三处同源）。
纯矩形没有等级，写 `custom` 而不是留空 —— 留空在 Android 侧与"老包没有这条信息"
收敛到同一个 `UNKNOWN`，而"用户画的一块矩形"与"不知道"是两件事。

### 4.2 兼容规则（这一节是给读取方看的）

| 情况 | 必须怎么做 |
| --- | --- |
| 老包（Slice ≤21）没有 `region` 对象 | 按矩形用，**不是**错误 |
| 有 `region` 但 `level` 认不出来 | 落到 `UNKNOWN`，包照常可用 |
| 出现没见过的键 | **忽略**它，不许因为一个附加字段拒收整个包 |
| `formatVersion` 高于支持范围 | **明确拒绝**并说清"包是几、我支持几" |
| `region.type` 认不出来 | 按 `rectangle` 兜底（只多下、不少下） |

"宁可少一条排序依据，不可让旧地图集体失效"—— 这是 Slice 22/24 反复出现的同一条原则。
唯一例外是 `formatVersion`：那一个键的含义就是"结构变了"，猜不得。

---

## 5. Android 侧：AUTO 怎么选包

```
MapResourceManager.applyAuto(lat, lon, zoom)
        │
        ├─ 1. 包本身可用？（id / bounds / minZoom≤maxZoom / mbtiles 文件还在）
        ├─ 2. 当前这张还盖得住（带 1e-4 度容差）+ 还支持当前 Zoom？→ 保持不动
        ├─ 3. 位置覆盖（精确）+ Zoom 在包区间内 → 真正的候选集
        │      └─ 一个都没有：区分"这片地方没图"与"有图但没这一档"
        └─ 4. 排序：默认图 → 等级具体度 → Zoom 匹配度 → 面积 → maxZoom → id
```

两条防抖/防跳规则：

- **容差只用在"当前这张还算不算盖住我"**（`AUTO_BOUNDS_EPSILON = 1e-4°`，约 11 米）。
  GPS 静止时会在几米内抖，不留容差就会两个包来回切、底图反复重建。
  其它候选仍按**精确**判定 —— 否则"我明明在范围外 10 米却用了这张图"说不清。
- **迟滞只挡"往更细的一档切"**（`AUTO_ZOOM_HYSTERESIS = 0.5` 级）：
  切换目标要越过 `目标.minZoom + 0.5`。反方向（当前包已经显示不了当前 Zoom）
  走硬过滤、**不设迟滞** —— 那里的迟滞等于"让用户继续看一张取不到瓦片的图"。

于是"已经在用某张包"之后，选择只会往**更具体**的方向走（全国 → 省 → 市 → 县 → 任务区），
这正好是 `RegionLevel.getSpecificity()` 那一级。**这就是 `level` 必须跨端的全部理由。**

---

## 6. 一条真实的数字（`scripts/hierarchical_map_e2e.py` 的输出）

夹具是自造的合成目录（`tests/fixtures/regions/hierarchy/`：
`democountry ⊃ demoprovince ⊃ democity` + 一条 `mission_route.geojson` 航线），
**不是真实国界**。每级只取推荐区间的下界片段以控制规模：

| 计划项 | 等级 | Zoom | 选中瓦片 | 预计容量 |
| --- | --- | --- | --- | --- |
| `democountry` | 全国级 | Z4~6 | 10 张 | 300 KB |
| `demoprovince` | 省级 | Z9~10 | 178 张 | 5.21 MB |
| `democity` | 城市级 | Z12~12 | 432 张 | 12.66 MB |
| `mission-002` | 任务区（5 km 走廊） | Z15~15 | 2,027 张 | 59.38 MB |
| **合计** | | | **2,647 张** | **≈ 77.5 MB** |

跑完一遍约 **22~24 秒**（全程离线：`httpx.MockTransport` 供瓦片，不触网）。同样是四个区域，
若照推荐区间**照单全下**：`democity` 的 Z12~16 需要 13 万张、`demoprovince` 的 Z9~13 需要
9,800 张 —— 那不是"验收失败"，是**分级策略在正常工作**（推荐区间是"这一级最多能到哪"，
不是"必须下到哪里"）。

---

## 7. 怎么验

| 层次 | 命令 | 看什么 |
| --- | --- | --- |
| 单元 / 集成 | `.venv\Scripts\python.exe -m pytest -q` | 全绿（≈2400 条） |
| 主链路（离线） | `.venv\Scripts\python.exe scripts\e2e_smoke_test.py` | 6 段：Tile Math → MBTiles → Inspector → Package → SHA256 → Validator |
| **分级全链路** | `.venv\Scripts\python.exe scripts\hierarchical_map_e2e.py` | 10 段：层级 / Zoom / 多边形估算 / 任务区 / 风险 / 计划 / 调度 / MBTiles / Inspector / 包 |
| 历史 Slice 回归 | `scripts\verify_slice7_ui.py` … `verify_slice23_risk.py` | 真窗口、真按钮（不是 offscreen） |
| Android | `cd D:\project\osmdroid && ./gradlew :OpenStreetMapViewer:testDebugUnitTest` | 538 条单测（含 Slice 25 的 7 条 AUTO 稳定性） |
| Android 真机 | `python scripts\verify_slice24_auto_selection.py --serial <serial>` | AUTO 阶梯与迟滞 |
| Android 最终验收 | `python scripts\verify_slice25_final_acceptance.py --serial <serial>` | §26 二十步：导入 / 阶梯 / 回退 / 重启 / 无崩溃 |

`hierarchical_map_e2e.py` 的退出码：`0` 全通过 / `1` 有失败 / `2` 夹具缺失。
加 `--keep` 可以保留工作目录去翻产物：

```bash
.venv\Scripts\python.exe scripts\hierarchical_map_e2e.py --keep
```

---

## 8. 已知边界（这些是设计取舍，不是待办）

- **多 MBTiles 同时渲染不支持**。一张地图同一时刻只有一个离线底图 +
  一个可选的注记层（天地图注记是另一个 `TilesOverlay`）。
- **没有在线获取边界数据的能力**。省市边界必须由用户自己放进
  `resources/regions/`，附授权要求（`resources/regions/README.md`）。
- **等级不落盘在任务记录里**（`region_level` 由 `region_id` 反查），
  只有**包**里落。这意味着"老任务记录"在目录变了之后可能反查出不同等级 ——
  这是有意的：等级是"这块地现在算什么"，不是"当初下单时的快照"。
- **Android 不做下载**。它只导入与渲染，所有下载都在 Windows 侧。
- **行政区包与任务区包在 Android 侧同构**：都是"一个多边形 + 一个 Zoom 区间"，
  差别只在 `level` 与 `bufferMeters`。
