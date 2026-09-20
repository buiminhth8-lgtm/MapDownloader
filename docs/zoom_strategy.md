# Zoom 策略：分级推荐区间（Slice 17）

这份文档回答一个问题：**"我这块地该下到多少级？"**

答案不是一个数字，而是一个区间 + 一句代价提醒。写了这份策略表，Zoom 规则就只有
一个来源（`app/planning/zoom_policy.py`），不会散落在界面、下载器和将来的 Android
客户端里各抄一份。

---

## 1. 默认策略

| 等级 | `RegionLevel` | 推荐 Zoom | 警告阈值 | 面向的场景 |
| --- | --- | --- | --- | --- |
| 全国级 | `COUNTRY` | **Z4 ~ Z10** | > Z10 | 大范围概览：看省界、看路网骨架 |
| 省级 | `PROVINCE` | **Z9 ~ Z13** | > Z14 | 省域浏览、城市定位 |
| 城市级 | `CITY` | **Z12 ~ Z16** | > Z16 | 城市范围的任务规划 |
| 任务区 | `MISSION` | **Z15 ~ Z18** | > Z18 | 高精度作业区（有限范围） |
| 自定义 | `CUSTOM` | 全支持范围 | 不警告 | 自定义矩形；老任务记录 |

这几条值就是 `DEFAULT_ZOOM_POLICIES`，`tests/test_zoom_policy.py` 逐个数钉死。

**它们是推荐值，不是硬限制。** 超出的唯一后果是一句非阻塞的提示：

```text
推荐 Zoom：Z4~10
当前 Zoom：Z8~18
警告：超出全国级推荐范围（当前 Z8~18），建议使用 Z4~10。
      全国级地图建议用于大范围概览，高 Zoom 应按省、市或任务区域下载。
```

不弹窗、不禁用按钮、**更不会偷偷把你的 8 和 18 改成 4 和 10**。理由见第 6 节。

---

## 2. 为什么层级之间**刻意**重叠

```text
COUNTRY   Z4 ------ Z10
PROVINCE          Z9 ------ Z13
CITY                       Z12 ------ Z16
MISSION                              Z15 ------ Z18
                  ↑ 重叠带 Z9~10、Z12~13、Z15~16
```

真实世界不会整齐地落在瓦片级别上：

- 一个**省**如果只取 Z9~Z10，省内的地级市在屏幕上就是一团糊；省级区间的下界必须
  低到"能看见整个省"，上界必须高到"能看见省内城市" —— 所以它和全国、城市级都有重叠。
- **全国概览 + 几个重点省**这种最常见的组合，需要用**同一套级别**拼起来：全国取
  Z4~10、重点省取 Z9~13，重叠的 Z9~10 让两块图在边界处的清晰度是连续的。如果区间
  接成不重叠的阶梯（全国 Z4~8、省级 Z9~13），重叠区就会一层糊一层清。
- 反过来说，**"重叠"不等于"应该下"**：全国 + Z10 与省级 + Z10 是两种东西 ——
  前者的代价是后者的一百多倍（见第 4 节）。重叠是为了让拼接连续，不是为了让你
  在全国范围用省级精度。

`tests/test_zoom_policy.py::test_recommended_ranges_overlap_between_neighbouring_levels`
守着这条：谁把区间"改整齐"了，测试就红。

---

## 3. 警告阈值为什么可以比推荐上界高

省级推荐到 Z13，但直到 Z14 都不报警 —— 中间这条 **Z14 过渡带**是给"我就是想看
这一个省的县城"留的余地：代价确实上去了，但还没到需要打断你的程度。

| 等级 | 推荐上界 | 警告阈值 | 过渡带 |
| --- | --- | --- | --- |
| 全国级 | Z10 | Z10 | 无（全国 Z11 的代价必须提前知道） |
| 省级 | Z13 | Z14 | Z14 |
| 城市级 | Z16 | Z16 | 无 |
| 任务区 | Z18 | Z18 | 无（已是界面可取的最高级别） |

判据只有一条：`max_zoom > warning_max_zoom`。**下界低于推荐不报警** —— 少下一点
只是少一点，没有需要提醒的代价。

---

## 4. 全国高 Zoom 为什么必须拆成省 / 市 / 任务区

同一块地（验收夹具区域，约 0.5° × 0.5°）用生产同一条预估链路实测：

| 区间 | 区域内瓦片 | 预计容量 |
| --- | --- | --- |
| 推荐 Z4~10 | **17** | **510 KB** |
| 用户设置 Z12~18 | **100,981** | **2.89 GB** |

差 **3 个数量级**，而这只覆盖了 0.5° 见方。换成真的全国范围，Z18 是十亿级瓦片、
TB 级容量、几个月都跑不完的请求量 —— 这不是"慢一点"，是**做不成**。

所以正确的做法不是"禁止全国下 Z18"，而是：

1. 全国下 Z4~10 拿概览；
2. 需要在哪个省看清楚，就把那个省作为**独立区域**下 Z9~13；
3. 具体作业区再单独下 Z15~18。

这也是策略表存在的意义：把"该拆"这件事在**你按下开始之前**讲清楚。

---

## 5. 代码怎么用

```python
from app.planning import RegionLevel, ZoomStrategyService

service = ZoomStrategyService()

# 1) 纯推荐：还没有用户输入时
service.recommend(RegionLevel.COUNTRY).range_text        # 'Z4~10'

# 2) 评估：拿用户设置比一比（**不改写**用户的 8 和 18）
r = service.evaluate(RegionLevel.COUNTRY, 8, 18)
r.range_text                  # 'Z4~10'
r.requested_range_text        # 'Z8~18'
r.warning                     # True
r.exceeds_recommendation      # True
r.warning_text                # 那句给用户看的话

# 3) 推荐 + 真实预估（复用 RegionTileEstimator，不重写多边形统计）
plan = service.recommend_with_estimate(region, RegionLevel.COUNTRY)
plan.selected_tiles           # 区域内的瓦片数（**唯一**该用的分母）
plan.size_text                # '510.00 KB'
plan.as_lines()               # 推荐 / 当前 / 警告 / 预计张数 / 预计容量
```

界面（`DownloadPanel`）只做了一件事：把 `as_lines()` 的内容显示在一个折行标签里，
Zoom 一改就刷新，自定义等级时整行收起。**界面里没有任何 Zoom 区间常量，也没有
一句推荐文案的副本**（`tests/test_zoom_policy.py` 里有三条静态守卫盯着这件事）。

### 模块职责

| 文件 | 职责 |
| --- | --- |
| `app/planning/region_level.py` | `RegionLevel`：全国 / 省 / 市 / 任务区 / 自定义 |
| `app/planning/models.py` | `ZoomPolicy`、`ZoomRecommendation`（不可变值对象） |
| `app/planning/zoom_policy.py` | `DEFAULT_ZOOM_POLICIES` —— **唯一**权威策略表 |
| `app/planning/zoom_strategy.py` | `ZoomStrategyService`、`ZoomPlanEstimate` |

`app.planning` 是纯策略层：不 import UI、不发起请求、不写磁盘；也只有
`app.geo.region` 依赖它的 `RegionLevel` 这一个方向（等级是区域的一个属性）。

---

## 6. 为什么没有 `enforce()` / `clamp()`

因为一旦存在，早晚会有人调用它。策略层的职责是**把代价讲清楚**，不是替用户拍板：

- 用户设了 Z8~18 然后发现"下载的其实是 Z4~10" —— 这比多下 2.89 GB 更糟，
  他会以为程序漏下了；
- 一个 20GB 的下载任务如果真的开始跑，用户需要的是**提前知道**，而不是
  "开始之后才发现"。

所以 `evaluate()` 只做读入与比较，返回值里用户的区间原样保留
（`requested_min_zoom` / `requested_max_zoom`），要么提醒、要么不提醒。
`min > max` 这种**非法输入**是另一回事：直接抛 `ValueError`，不"顺手帮你调过来"。

---

## 7. 与既有功能的兼容

- **等级不落盘**：`tasks.json` 与地图包 manifest 里都**没有** `region_level`
  字段，它由 `region_id` 反查预设得到。多存一份只会多一处不同步的地方。
- **老 Region / 老任务**：没有等级的一律读作 `CUSTOM` —— 不套用任何行政等级规则，
  行为与 Slice ≤16 逐位一致，也不会因为"缺少等级字段"而报警。
- **自定义矩形**：恒为 `CUSTOM`，界面上那一行建议整行收起（没有等级就没什么可建议的）。
- **中国预设**：`CHINA_REGION_PRESET.region_level = COUNTRY`，于是选中它就显示
  「推荐 Zoom：Z4~10」。注意等级来自**预设**而不是边界文件：文件还没放好时，
  用户也应该看到"选中的是哪个区域、它建议多少 Zoom"。
- **多边形下载链路**（Slice 16）一个字节都没动：策略层只在"算之前"给建议，
  `RegionTileEstimator` / `region_filter` / MBTiles / Inspector / 打包全部原样。

本 Slice **只做策略核心 + 测试 + 最小提示**，没有实现省市行政区数据、分层下载计划、
Mission 航线缓冲，也没有改 Downloader 核心与 Android 端。

---

## 8. 怎么验收

```bash
# 单元测试（策略表 + 服务 + 界面提示）
python -m pytest tests/test_zoom_policy.py tests/test_zoom_strategy.py -q
python -m pytest -q                      # 全量，确认无回归

# 实机验收（真 windows 平台插件、真窗口、真点按钮、真下载）
python scripts/verify_slice17_zoom.py
python scripts/verify_slice17_zoom.py --offscreen    # 无显示环境
python scripts/verify_slice17_zoom.py --no-shots     # 只跑检查，不落截图
```

`scripts/verify_slice17_zoom.py` 覆盖 Spec §17 的 7 步手工验收，截图落在
`docs/slice17_zoom_*.png`：

| 截图 | 内容 |
| --- | --- |
| `slice17_zoom_rectangle_baseline.png` | 自定义矩形：建议行整行收起 |
| `slice17_zoom_recommended.png` | 中国 + Z4~10：只有推荐与当前，没有警告 |
| `slice17_zoom_warning.png` | 中国 + Z8~18：警告出现，Zoom 仍是你设的 8~18 |

---

## 9. 加一个省 / 市预设时

Slice 18 之后，行政区域的数据源是 `resources/regions/index.json` 那份**目录**，
不再是一个个往 `REGION_PRESETS` 里加常量：

1. 放数据：`resources/regions/<level>/<id>.geojson`
   （**必须**是带审图号的合法授权数据，见 `resources/regions/README.md`）；
2. 在 `index.json` 的 `regions` 里加一条，写清 `id` / `name` / `level` /
   `parentId` / `geojson`；
3. 若还需新的等级，加 `RegionLevel` 成员**并同时**在 `DEFAULT_ZOOM_POLICIES`
   里补一条（缺了会被 `ZoomStrategyService` 与测试拦下）。

策略核心、界面提示、预估链路、加载器都不需要改动。详见
[docs/region_catalog.md](region_catalog.md)。
