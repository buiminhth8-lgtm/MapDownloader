# 行政区域级联选择 + 推荐 Zoom（Slice 19）

这个 Slice 把 Slice 18 准备好的**层级目录**接到了界面上：用户按 **国家 → 省 → 市**
三段下拉选区域，界面按区域的**等级**自动给出推荐 Zoom 区间；想自己定 Zoom 就切到
「自定义 Zoom」，超范围只提醒、不改值。

它**不包含**：多区域下载计划（一次下一串省市）、任务编排（Mission）、区域管理页
（增删改查）、Android 侧任何改动。这些是后续 Slice。

| 这一 Slice 交付 | 在哪 |
| --- | --- |
| 三段级联下拉 + 等级显示 | `app/ui/download_panel.py` |
| 选择状态 / 等级推导 / Zoom 建议的转发 | `app/ui/region_selection.py`（新） |
| 推荐 / 自定义策略与持久化 | `app/ui/preferences.py`、`app/ui/main_window.py` |
| 单元测试 | `tests/test_region_selection_controller.py`（29）、`tests/test_download_panel_region_selection.py`（27） |
| 实机验收 | `scripts/verify_slice19_region_ui.py`（90 项，真窗口 + 真关窗 + 真点「计算」） |

---

## 1. 界面长什么样

行政区域模式下，`下载范围` 分组里从上到下是：

| 控件 | 内容 | 说明 |
| --- | --- | --- |
| `区域类型` | 自定义矩形 / 行政区域 | Slice 16 起就有 |
| `国家` | 目录里的国家级条目 | 新增（Slice 19） |
| `省份` | 首项「全部（不限定省份）」，其余是该国的省 | 新增 |
| `城市` | 首项「全部（不限定城市）」，其余是该省的城市 | 新增 |
| `下载级别` | `城市级 · 演示国 / 北省 / 河城` | 新增：等级 + 完整路径 |
| `边界范围` | 四个只读经纬度 + 候选范围提示 | Slice 16 起就有 |
| `下载策略` | ◉ 推荐 Zoom ○ 自定义 Zoom | 新增 |
| `Zoom 建议` | `推荐 Zoom: Z12~16` / `当前 Zoom: Z12~16` | Slice 17 起就有 |
| `Min Zoom` / `Max Zoom` | 推荐模式下只读 | 值随等级自动填 |
| `推荐说明` | 一句来自策略层的理由 | 新增 |

矩形模式下这 14 个控件里除经纬度框外**全部收起**，界面与 Slice ≤18 逐像素一致
（`QGridLayout` 在整行都隐藏时会把行高压到 0，不留空白条）。

截图（`scripts/verify_slice19_region_ui.py` 生成）：

| 截图 | 内容 |
| --- | --- |
| `docs/slice19_cascade_city.png` | 三段级联选到市级，级别行与推荐区间可见 |
| `docs/slice19_zoom_recommended.png` | 推荐模式：Min/Max 自动填成 Z12~16 |
| `docs/slice19_zoom_warning.png` | 自定义 8~17：给出「城市级」警告但值没被改 |
| `docs/slice19_polygon_estimate.png` | 真点「计算」后的结果区（含逐级明细四列） |

---

## 2. 级联规则

三段的联动只有一条规则：**改了上一段，下面所有段重填**。

| 动作 | 省份下拉 | 城市下拉 | 生效等级 |
| --- | --- | --- | --- |
| 选国家 | 按该国重填，落回「全部」 | 只剩「全部」 | `COUNTRY` |
| 选省份 | 不变 | 按该省重填，落回「全部」 | `PROVINCE` |
| 选城市 | 不变 | 不变 | `CITY` |
| 省选回「全部」 | 不变 | 只剩「全部」 | `COUNTRY` |
| 市选回「全部」 | 不变 | 不变 | `PROVINCE` |
| 换国家 | 按新国家重填 | 只剩「全部」 | 新国家的 `COUNTRY` |

三个细节值得写下来，因为它们都是"看起来能用、其实会出错"的地方：

- **「全部」是 `None`，不是某个哨兵 id。** 下拉里第一项的 `itemData` 是 `None`，
  `current_region_id()` 直接返回 `None`，于是"省没选"这件事在全链路上只有一种表示。
  曾经用 `-1` 表示过，结果是"`-1` 与任何合法下标都不相等"这种判断到处漏。
- **省下没有城市数据时要说明白。** 目录只到省级时城市下拉只有一项，文本被换成
  「（该省暂无城市级数据）」而不是留一个空下拉框让用户猜。空下拉与"还没选省"
  长得一样，而两者该做的事完全不同。
- **换国家必须清掉省和市。** 否则界面上会留下"上一个国家的省" + "新国家"这种
  不可能存在的组合，而它算出来的候选瓦片照样有数字 —— 不会报错，只是错的。

等级是从**目录条目**查出来的，不是从界面状态推的：

```python
# app/ui/region_selection.py
@property
def level(self) -> RegionLevel:
    """当前选择对应的规划等级（没选就是 CUSTOM）。"""
    entry = self.selected
    return RegionLevel.CUSTOM if entry is None else entry.level
```

这条很重要：**边界文件还没放好的条目，等级照样是准的**（等级是元数据）。
缺文件时等级照样显示 `PROVINCE`，用户知道自己选对了地方，只是数据没到位。

---

## 3. 推荐 Zoom / 自定义 Zoom

两个单选互斥（`QButtonGroup`），对应 `ZoomMode`：

| | 推荐 Zoom（默认） | 自定义 Zoom |
| --- | --- | --- |
| Min / Max 的值 | 按等级从策略表取（Z4~10 / Z9~13 / Z12~16） | 用户说了算 |
| Min / Max 可编辑 | ❌ `setReadOnly(True)` | ✅ |
| 超范围时 | 不可能超（值就是建议值） | `Zoom 建议` 行给出警告 |
| 阻塞下载 | 否 | 否 |

**"只警告不改值"是硬约束**，服务层刻意没有 `clamp()`。配套的用例形状是成对的：
每条"看到警告"的用例旁边都有一条"用户的值没被改"。

推荐模式下把 Min/Max 设成只读，是为了避免一类**看不出来的错误**：界面显示
`Z12~16`、实际拿去下载的却是用户上次手打的 `8~17`。只读让"显示的"与"生效的"
不可能不一致。

**模式切换不改值**：

```
推荐（显示 Z12~16）→ 点自定义 → 值仍是 12 ~ 16，但可编辑了
自定义 8~17 → 点推荐 → 值变成 12 ~ 16，且重新只读
```

第一行是有意为之：切模式不等于改参数，用户不该因为点了一下单选就丢掉刚调好的数字。
控制器**不缓存**用户值 —— `effective_range(min_zoom, max_zoom)` 的签名就说明了这点，
它把控件里的值当参数收进来、按模式决定返回哪一个。少一个字段就少一处不同步。

**矩形模式不强制策略**：`CUSTOM` 等级没有"建议区间"可言，所以建议行与推荐说明
整行收起，两个单选的状态也不影响 Zoom 的值（只影响下次切到行政区域时的默认模式）。

---

## 4. 一个下载区域，一个来源

界面上选了三级，但真正生效的只有一个 id：

```python
selected_region_id = (city or province or country)
```

于是"只选国家"和"选到市"走的是同一条下游链路：`RegionService.get_region(id)` →
加载边界 → `DownloadRegion` → 候选瓦片估算（Slice 16）。界面**从不**自己拼一个
`DownloadRegion`：那样就会有两处实现"边界文件在哪、要不要换算坐标系、缺文件怎么办"。

```python
# app/ui/download_panel.py
def current_region(self) -> DownloadRegion | None:
    return self._controller.download_region(
        bounds=self.current_bounds(), document=self._region_document
    )
```

`download_region()` 内部走的是 `RegionService.get_download_region()`，异常被收敛成
`None` + `region_error` 一句话（见 §6 的表）。

### 为什么 `bounds` 传的是**界面上的那四个数**

`bounds` 刻意不是"几何的原始外包矩形"，而是四个经纬度框里的值（6 位小数）。
理由不是省事，是三条都指向同一个选择：

1. 输入框里显示的就是这个数 —— "界面显示的范围"与"实际下载依据"必须是同一个；
2. 输入框的 6 位小数**与写进 MBTiles metadata 的精度一致**。拿 9 位小数的几何去
   枚举候选、却把 6 位的值写进 metadata，会让 Inspector 从 metadata 反推出来的
   瓦片范围**多出一圈**（边界恰好落在瓦片边上时，多出来的那一列 / 一行只是
   "碰到"了多边形边沿，按 Slice 16 的规则碰边也算相交），于是**完整的包被判成
   "缺失 10 张"**、打包器拒绝出包 —— 一处纯粹由精度不一致造出来的假警报；
3. Slice ≤18 本来就是这么传的，改掉它就是行为变更。

这条有专门的回归用例（`test_download_region_bounds_are_the_ones_on_screen`），
它把夹具的西边界改成 `110.123456789`，于是"按界面传"与"按几何传"必然不同。

### 结果区多了两行

行政区域算完之后，结果区第一段是：

```
区域：河城
级别：城市级
推荐 Zoom：Z12~16
候选瓦片：769
区域内瓦片：769
过滤瓦片：0
过滤比例：0.0%
```

级别与推荐区间都取自**当前区域**（目录条目 → 策略表），不在结果区里另算一份。
`Zoom 明细` 那一段每行末尾多了「预计容量」，用的是生产的 `estimate_size_bytes()`
+ `format_size()`（不在这里乘一个自己的常数——那会变成第二个"每张瓦片多少字节"）：

```
Zoom 明细（候选 / 选中 / 过滤 / 预计容量）：
  z12  169 / 169 / 0  4.95 MB
  z13  600 / 600 / 0  17.58 MB
```

---

## 5. 为什么控制器是 Qt-free 的

`RegionSelectionController`（`app/ui/region_selection.py`，约 430 行）**不 import
PySide6**。它只持有三个 id 与一个模式，把目录查询与策略查询转给
`RegionService` / `ZoomStrategyService`。

好处直接体现在测试上：级联、等级推导、模式切换、异常分支这些"容易写错的逻辑"
可以在毫秒级的纯对象用例里断言，而不用把 `QApplication` 拖进来。需要断言控件
行为的那部分（只读、可见性、信号有没有接）才落到 widget 用例里。

它还**不含**任何界面文案的副本：`Zoom 建议` 行的文字来自
`ZoomRecommendation.as_lines()`，`推荐说明` 来自 `ZoomRecommendation.reason`，
取消/`全部` 这类下拉文本才是面板自己的常量。

---

## 6. 异常处理

界面上没有一处会因为这个功能而崩。逐条对应：

| 情况 | 表现 |
| --- | --- |
| 目录读不出来 / JSON 语法错 | `RegionService` 退回内置扁平预设，下拉里仍有「中国」，日志一行 ERROR |
| 目录里只有国家（没有省市） | 省份下拉只有「全部」，城市下拉只有「（该省暂无城市级数据）」 |
| 边界文件缺失 | 条目**照样列出来**，文本带「（边界文件未就绪）」后缀，tooltip 带路径；选中后 `边界范围` 行说明该往哪放什么文件；`current_region()` 返回 `None`（**不退回矩形**） |
| GeoJSON 结构不对 / 加载失败 | `边界不可用：<人话>`，功能其余部分照常 |
| 选中的 id 不在目录里（`set_region_id("ghost")`） | 保持当前选择，`region_error` 说明原因，不抛异常 |
| 策略层拒绝这一对 Zoom（Min > Max） | `Zoom 建议` 行给出一句话，不从 Qt 槽里漏异常出去被静默吞掉 |
| 偏好里存着不存在的 id | 逐级回退（见 §7） |

"边界文件缺失"**不是**退回矩形的理由：退回矩形会拿着一个用户没选过的范围去下载，
而用户从界面上看不出来。宁可报错停住。

---

## 7. 状态与持久化

关窗时记下的键（`app/ui/preferences.py`）：

| 键 | 值 |
| --- | --- |
| `download/region_type` | `rectangle` / `polygon` |
| `download/country_id` | 国家 id |
| `download/province_id` | 省 id（「不限定省」时为空） |
| `download/city_id` | 市 id（「不限定市」时为空） |
| `download/zoom_mode` | `recommended` / `custom` |
| `download/custom_min_zoom` / `custom_max_zoom` | 用户自己定的区间 |

三段 id **分开存**，不存一个"当前生效 id"：界面是按三段恢复的，只存末端 id 就得
反推"它的父亲是谁"——那是目录的职责，不该在偏好这一层再实现一遍。

### 老键的迁移读取

Slice ≤18 存的是 `download/min_zoom` / `download/max_zoom`。这两个键**只读不写**：
读新键没有时回退读它们（一次性的迁移），升级后用户上次调好的区间不会变成
"从没设过"。`clear()` 会把老键一起删 —— 只删新键的话，一次 `clear()` 之后
"从没设过"这个事实会被老配置悄悄推翻（这条是真被测试抓出来的）。

### 脏 id 的逐级回退

恢复顺序是 **市 → 省 → 国**，一个都不认识才落回自定义矩形：

| 存的内容 | 恢复结果 |
| --- | --- |
| 市 / 省 / 国 都在 | 铺满三段，生效 id = 市 |
| 市不在了，省还在 | 铺到省，日志一条 WARNING（"只剩「北省」还在目录里"） |
| 只剩国家 | 铺到国家 |
| 三个都不在 | 回自定义矩形 |

回退要**先问目录里有没有这一条，再动界面**：对着一个不存在的 id 反复调
`set_region_id()`，最后反而会停在多边形模式上 —— 那正是"回退没生效"的样子。

### 恢复顺序：区域先于 Zoom

`_apply_preferences()` 里区域恢复必须**排在 Zoom 之前**。恢复一个行政区域会把模式
带到「推荐」，而推荐模式会立刻把建议值填进 Min/Max；顺序反过来的话，用户上次设的
`8~17` 会被推荐值顶掉，然后才轮到"恢复 Zoom"，恢复出来的两个值都不是用户要的。

---

## 8. 与矩形模式的兼容

| | 矩形模式 | 行政区域模式 |
| --- | --- | --- |
| 三段下拉 / 级别 / 策略 / 推荐说明 | 隐藏 | 显示 |
| 经纬度框 | 可编辑 | 只读（范围来自边界文件） |
| `get_form_data()["region_id"]` | `None` | 生效 id |
| `get_form_data()["region_level"]` | `CUSTOM` | 目录条目的等级 |
| `get_form_data()["zoom_mode"]` | 仍是推荐 / 自定义 | 同左 |
| Zoom 的值 | 不受策略影响 | 推荐模式下 = 建议值 |

`zoom_mode` 在矩形模式下也有值（默认 `recommended`），但它**不改变**任何东西 ——
它只是"下次切到行政区域时用哪种策略"和"关窗时要存哪一项"。

---

## 9. 没做的事

- ❌ 多区域下载计划（一次下一串省 / 市）—— Spec 明确排除
- ❌ 区域管理界面（增删改查 / 校验 / 导入）
- ❌ Android 侧任何改动
- ❌ 下载器内核、MBTiles 写出格式、任务记录格式
- ❌ 界面层解析 `index.json` 或 GeoJSON 来决定层级（`verify_slice19` 场景 H 用 AST 守）
- ❌ 界面里出现第二份等级 → Zoom 的值（由 `ZoomStrategyService` 单一来源提供）

---

## 10. 代码怎么用

```python
from app.ui.region_selection import RegionSelectionController, ZoomMode

controller = RegionSelectionController()           # 自己建一个 RegionService
controller.select_country("china")                 # 国家级
controller.select_province("zhejiang")             # 再选省
controller.select_city("hangzhou")                 # 再选市

controller.selected_region_id   # 'hangzhou'（市 → 省 → 国 的兜底顺序）
controller.level                # RegionLevel.CITY
controller.path_text            # '中国 / 浙江省 / 杭州市'

low, high = controller.recommended_range           # (12, 16)，非行政等级时是 None
controller.set_zoom_mode(ZoomMode.CUSTOM)
low, high = controller.effective_range(8, 17)      # 自定义 → 原样返回 (8, 17)

rec = controller.recommendation(8, 17)             # 评估：warning=True，值没被改
region = controller.download_region()              # DownloadRegion | None
```

面板侧（测试与验收脚本用）：

```python
panel.country_combo / panel.province_combo / panel.city_combo
panel.level_text / panel.zoom_explain_text / panel.zoom_policy_text
panel.zoom_mode_buttons        # (推荐, 自定义)
panel.zoom_spins               # (min, max) 两个输入框（读 isReadOnly）
panel.bounds_spins             # 四个经纬度框
panel.region_controller        # RegionSelectionController
panel.set_zoom_mode(ZoomMode.CUSTOM)
panel.set_zoom_range(8, 17)    # 顺带把模式切到自定义
panel.get_form_data()          # 多了 region_id / region_level / zoom_mode
```

---

## 11. 怎么验收

```bash
# 单元测试
.venv/Scripts/python.exe -m pytest tests/test_region_selection_controller.py \
    tests/test_download_panel_region_selection.py tests/test_ui_preferences.py \
    tests/test_ui_smoke.py -q
.venv/Scripts/python.exe -m pytest -q          # 全量，确认无回归

# 实机验收（真 windows 平台插件、真窗口、真关窗再开、真点「计算」、截图落 docs/）
.venv/Scripts/python.exe scripts/verify_slice19_region_ui.py
.venv/Scripts/python.exe scripts/verify_slice19_region_ui.py --offscreen
.venv/Scripts/python.exe scripts/verify_slice19_region_ui.py --no-shots
```

场景与 Spec §20 手工验收步骤的对应：

| 场景 | 覆盖 |
| --- | --- |
| A | 三级级联、换国家清省 / 市、无城市级数据、控件真的摆得下 |
| B | 三级各自的推荐值 == 策略层输出、只读、模式切换不改值 |
| C | 自定义超范围只警告、不改值、不阻塞；Min > Max 不崩 |
| D | 范围来自 GeoJSON、经纬度只读、结果区含级别 / 推荐区间 / 逐级容量 |
| E | 缺边界文件（旱省）：能选中、取不到区域、提示带路径、点「计算」不崩 |
| F | **真的关一次窗**再开新窗口恢复；脏 id 逐级回退；全脏落回矩形 |
| G | 老键 `download/min_zoom` 迁移读取、新键优先、`clear()` 连老键一起删 |
| H | 界面层无 JSON 解析、无等级字面量、推荐值与策略层逐字一致 |
| I | Spec §20 的 14 条手工步骤逐条对应 |
