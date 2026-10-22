# 端到端验收清单（Windows → ADB → Android）

> 这份清单验的是**整条产品线**：Windows 上做好一张离线地图包，传到手机，装进 App，
> 拔网还能看。它穿越两个仓库、三个进程（工具 / adb / App），任何单测都覆盖不到。
>
> 每一步都写了**怎么判定通过**。判定标准要能被机械执行 —— "看起来正常"不是判据。
>
> | 阶段 | 步骤 | 在哪台机器 |
> | --- | --- | --- |
> | A. 制作 | A1 – A7 | Windows |
> | B. 传输 | B8 – B11 | Windows（`adb`） |
> | C. 导入与使用 | C12 – C20 | Android 设备 |
> | D. 分级体系（全国/省/市/县/任务区） | D21 – D25 | 两端都跑 |
> | E. 县级专项 | E26 – E31 | 两端都跑 |
>
> A~C 验的是**一个矩形包**的整条链路；D 验的是**层级**（五级区域各自的 Zoom 区间
> 与等级怎么跨端，以及手机按 Zoom 选对那一档）；E 验的是**县级这一级特有的坑**
> （2882 个县里的直辖市下辖区、同名县、极小县，以及"县级插进阶梯的正确位置"）。
> 第一次验收按顺序全跑；之后回归可以只跑标了 ★ 的那几步。D 段有**全自动版本**，
> 见文末「全自动版本」。
>
> **E 段为什么单独存在**：县级不是"再往下列一级"那么简单。它比市级多出三种
> 真实数据形态（父级直挂省的直辖市下辖区、跨省同名县、面积十几平方公里的极小县），
> 而且它插在阶梯的**中间**（市级 Z12~16 与任务区 Z15~18 之间重叠），
> 最容易出现"包能用但永远选不中"这种**不报错**的缺陷。

---

## 准备

| 项 | 要求 |
| --- | --- |
| Windows | 仓库 `D:\project\MapDownloader`，venv 已建（`.venv\Scripts\python.exe`） |
| Android | 仓库 `D:\project\osmdroid`，app 已装（`org.osmdroid.map`） |
| adb | `D:\develop\android\sdk\platform-tools\adb.exe` |
| 设备 | 一台真机或模拟器，**建议先清空** `offline_maps` 目录（**E29 的阶梯验收必须清空** —— 混进别的包时"这一档为什么选了它"就无法归因，`verify_slice28` 会把这条当断言拦住） |
| 网络 | A 阶段需要；**C15 之后要拔网 / 开飞行模式** |
| 天地图 Token | A 阶段需要（在工具「偏好设置」里填） |

**先记下这三个版本号**，后面 C20 要核对：

```bash
cd D:/project/MapDownloader && .venv/Scripts/python.exe -c "from app.config import settings; print(settings.APP_VERSION, settings.PACKAGE_FORMAT_VERSION)"
cd D:/project/osmdroid && grep -n "OFFLINE_MAP_RUNTIME_VERSION =" OpenStreetMapViewer/src/main/java/org/osmdroid/map/offline/config/MapRuntimeConfig.java
```

---

## A. Windows：制作地图包

### A1 ★ 启动工具

```bash
cd D:/project/MapDownloader && .venv/Scripts/python.exe -m app.main
```

**通过判据**：主窗口出现，标题栏/状态栏显示 `天地图离线地图工具 v<APP_VERSION>`。

**常见失败**：`ModuleNotFoundError: PySide6` → 用了系统 Python，改用 `.venv\Scripts\python.exe`。

---

### A2 环境自检

看主窗口底部状态栏与「连接检查」面板。

**通过判据**（四项分别确认）：

| 检查项 | 期望 | 不满足时的影响 |
| --- | --- | --- |
| Python / PySide6 | 正常 | 工具根本无法启动 |
| 输出目录可写 | 正常 | **无法制作地图包** |
| 剩余空间 | 显示具体数值 | 不足以容纳预计包大小时会拒绝 |
| **ADB** | 可能显示"未找到" | **只影响设备传输**，A 阶段照常进行 |

> **注意**：ADB 那项是独立的。它报"未找到"不该阻止你做地图 ——
> 这条是 Spec §8 明确要求的：设备传输是可选环节。

---

### A3 输入地理范围与层级

在下载面板里输入：

| 字段 | 建议值 |
| --- | --- |
| 范围 | 经度 `116.2 ~ 116.6`，纬度 `39.7 ~ 40.1`（北京主城） |
| 层级 | `11 ~ 13` |
| 图层 | 矢量底图 + 注记 |

**通过判据**：界面显示**预估瓦片数**与**预估大小**，且瓦片数 > 0。

**记下这个预估瓦片数**，A5 / A6 要核对。

---

### A4 下载瓦片

点「开始下载」。

**通过判据**：
- 进度条真的在走（不是停在 0%）
- 已下载数不断增长，最终等于 A3 的预估数
- 结束后状态是"完成"，**不是"失败"或"部分完成"**

**常见失败**：Token 无效 → 见 `troubleshooting.md`「Token 无效」；
卡在 0% → 检查网络与 Token。

---

### A5 检查 MBTiles

下载产出的 `.mbtiles` 会出现在任务列表里。点「检查」。

**通过判据**：
- 结论为 **PASS**（WARNING 需要看具体条目，FAIL 直接停）
- 显示的瓦片数 == A3 的预估数
- Zoom 区间显示 `11~13`
- 抽样图片格式显示 `PNG`

**常见失败**：瓦片数少于预估 → 有瓦片下载失败，重跑下载（支持断点续传）。

---

### A6 ★ 生成地图包

点「生成地图包」。若弹出警告确认框，看清楚内容再决定。

**通过判据**：
- 输出目录里出现 `<包名>/` 目录
- 目录里有且只有两个文件：`manifest.json` 与 `map.mbtiles`
- 生成结束时的自校验结论为 **通过**

**记下包目录路径**，B8 要用。

---

### A7 ★ 核对 manifest

```bash
cd <包目录> && cat manifest.json
```

**逐项核对**：

| 字段 | 期望 |
| --- | --- |
| `formatVersion` | `1` |
| `id` | 与目录名**完全一致** |
| `mapType` | `vector`（若下了注记则 `labels` 为 `true`、`composition` 为 `merged`） |
| `minZoom` / `maxZoom` | `11` / `13` |
| `bounds` | 与 A3 输入的四个值一致 |
| `tileCount` | == A3 的预估数 |
| `mbtiles.file` | `map.mbtiles` |
| `mbtiles.size` | == `map.mbtiles` 的实际字节数 |
| `mbtiles.sha256` | 64 位小写十六进制 |
| `createdAt` | 带时区后缀（`+08:00`） |

**用命令独立验证后两项**（不要只信 manifest 自己）：

```bash
# Windows PowerShell
(Get-Item map.mbtiles).Length
(Get-FileHash map.mbtiles -Algorithm SHA256).Hash.ToLower()
```

两个值都必须与 manifest 里的一致。**这一条是整个流程里最重要的检查** ——
SHA 对不上，手机上必然导入失败，而那时你会怀疑是传输坏了。

---

## B. ADB：把包传到设备

### B8 确认设备可见

```bash
D:/develop/android/sdk/platform-tools/adb.exe devices -l
```

**通过判据**：目标设备出现在列表里，状态是 **`device`**。

| 状态 | 含义 | 怎么办 |
| --- | --- | --- |
| `device` | 正常 | 继续 |
| `unauthorized` | 手机上没点「允许」 | 看手机屏幕，点允许 |
| `offline` | 连接僵死 | `adb kill-server` 后 `adb start-server` |
| 列表为空 | 没连上 | 检查 USB / 无线调试 |

> ⚠️ `adb devices` 会把**所有** adb 设备列出来（模拟器、其他手机）。
> 多设备时后续命令都要加 `-s <serial>`。

---

### B9 推送地图包

```bash
DEV=<你的 serial>
D:/develop/android/sdk/platform-tools/adb.exe -s $DEV push "<包目录>" /sdcard/Download/
```

**通过判据**：输出里有 `1 file pushed`（或两个文件），**没有** `error:`。

> `adb push` 的进度写在 **stderr** 上。用脚本抓输出时只读 stdout 会得到空串，
> 看起来像"什么都没发生"。

---

### B10 核对设备上的字节数

```bash
D:/develop/android/sdk/platform-tools/adb.exe -s $DEV shell ls -l /sdcard/Download/<包名>/
```

**通过判据**：`map.mbtiles` 的字节数 == A7 里 Windows 上的值。

**不一致就不要往下走** —— 说明传输被截断了。重推一次。

---

### B11 ★ 核对设备上的 SHA256

```bash
D:/develop/android/sdk/platform-tools/adb.exe -s $DEV shell sha256sum /sdcard/Download/<包名>/map.mbtiles
```

**通过判据**：输出的哈希 == A7 里 manifest 的 `mbtiles.sha256`。

> 设备上若没有 `sha256sum`（部分精简 ROM），用 `toybox sha256sum` 或 `busybox sha256sum`。
> 都不可用时跳过这步 —— **Android 导入时会自己校验**（C13 会报出来）。

---

## C. Android：导入与使用

### C12 打开离线地图管理页

在 App 里进入「离线地图」（或「地图管理」）。

**通过判据**：
- 页面正常打开，**不闪退**
- 空目录时显示"暂无离线地图"之类的空状态，而不是空白页或转圈不停

---

### C13 ★ 导入地图包

点「导入」→ 选到 `/sdcard/Download/<包名>`（或该目录）。

**通过判据**：
- 导入完成后**明确提示成功**（不是静默返回）
- 列表里出现新的一行，名字是包的 `name`
- 该行**不是**"已损坏"状态
- 摘要里显示的地图类型 / 层级 / 大小与 manifest 一致

**若报"校验失败"**：回 B11 核对 SHA256。若哈希一致却仍失败，
说明 manifest 与 MBTiles 的实际内容不匹配 —— 回 A7。

**若报"空间不足"**：设备剩余空间要大于包大小的两倍以上（导入过程会先写 staging）。

---

### C14 确认导入后目录结构

```bash
D:/develop/android/sdk/platform-tools/adb.exe -s $DEV shell ls -la /storage/emulated/0/Android/data/org.osmdroid.map/files/offline_maps/
```

> 路径以 App 实际为准；`OfflineMapStorage.forApp()` 用的是应用私有目录。
> 有些机型/版本会在 `Android/data/...` 下，有些在 `Android/media/...`。

**通过判据**：
- 出现以包 `id` 命名的目录
- 该目录里有 `manifest.json` 与 `map.mbtiles`
- **没有** `.importing` / `.backup` 残留目录

**若看到 `.importing`**：上一次导入中断了。App 下次启动会清理，也可以在管理页触发清理。

---

### C15 ★ 启用离线模式

在管理页把这张包设为**默认地图**，或在模式选择里切到 **OFFLINE**。

**通过判据**：
- 模式显示 `OFFLINE`
- 列表里该行有「当前使用」标记
- 地图页底图**有内容**（能看到路网/注记，不是灰白一片）

---

### C16 ★ 拔网验证（这是整条线的意义所在）

开飞行模式或关 WiFi + 数据。

**通过判据**：
- 切到地图页，**底图仍然有内容**
- 拖动地图，新区域的瓦片**能画出来**（说明不是缓存，是真的在读本地库）
- App **不崩溃、不 ANR**

**若底图变白**：说明离线 Provider 没生效。检查 C15 的模式是否真的是 `OFFLINE`。

---

### C17 AUTO 模式

恢复网络，把模式切到 **AUTO**。

**通过判据**：
- 当前位置在包范围内 → 自动选中这张包，底图正常
- 位置在包范围外 → 安全退回在线底图，**不白屏、不崩溃**

**再验一次断网下的 AUTO**：开飞行模式，切 AUTO。

**通过判据**：仍然能用本地包显示（这是"飞行模式 + 提前下地图"的主场景）。

---

### C18 删除地图（安全回退）

在管理页删掉这张包。

**通过判据**：
- 弹出确认框（**不能点了就删**）
- 确认后列表里该行消失
- 地图页**自动退回在线底图**（或显示明确的"无离线地图"提示）
- **不崩溃、不白屏**
- 没有被删包的目录残留

**若删不掉**：可能文件句柄没释放。切到别的模式/页面再试，或重启 App。

---

### C19 多地图与默认地图

导入**两张**不同的包。

**通过判据**：
- 两张都能导入、都能单独启用
- 设其中一张为默认后，AUTO 模式下优先选它（若位置在它范围内）
- 删掉**默认**那张后：默认设置被清掉，不指向不存在的包
- 删掉**当前正在用**的那张后：安全退回，不引用已删的包

---

### C20 ★ 版本核对

```bash
# Windows
cd D:/project/MapDownloader && .venv/Scripts/python.exe -c "from app.config import settings; print('tool', settings.APP_VERSION, 'pkgfmt', settings.PACKAGE_FORMAT_VERSION)"
# Android
cd D:/project/osmdroid && grep -rn "OFFLINE_MAP_RUNTIME_VERSION =" OpenStreetMapViewer/src/main/java/org/osmdroid/map/offline/config/MapRuntimeConfig.java
```

**通过判据**：

| 项 | 期望 |
| --- | --- |
| Windows 工具版本 | 与 README 里写的一致 |
| Package Format Version | `1` |
| Android 运行时版本 | 与 README 里写的一致 |
| **两端格式版本** | Windows 的 `PACKAGE_FORMAT_VERSION` == Android 的 `SUPPORTED_FORMAT_VERSION` |

**记下这三行**，它们是这次验收的"版本快照"。

---

## D. 分级体系（全国 → 省 → 市 → 县 → 任务区）

A~C 阶段验的是**一个矩形包**的整条链路。这一段验的是**层级**：五级区域各自
带着自己的 Zoom 区间与等级，从 Windows 一路走到手机，并且在手机上**按 Zoom
选对那一档**。分级的细节见 `hierarchical_offline_map.md`。

> 这一段可以全自动跑（见文末「全自动版本」的 `hierarchical_map_e2e.py`）。
> 下面写的是**手工版本**，用来在自动化之外独立确认一遍。

### D21 ★ 准备一块有层级的区域

工具 → 「区域」选「行政区域」。如果目录里还没有省市数据，**先用仓库里的
合成夹具**（`tests/fixtures/regions/hierarchy/`）代替 —— 那份夹具是自造的
几何，**不是真实国界**，只用于验收流程本身：

```bash
# 把夹具复制成一个临时资源目录，然后临时指向它（不改仓库里的 resources/）
cd D:/project/MapDownloader
cp -r tests/fixtures/regions/hierarchy /tmp/hier-regions
```

**通过判据**：
- 下拉里能看到 `示例国 / 示例北省 / 示例河市` 三级，选择后**候选范围行**跟着变
- 任务区（航线 + Buffer）能建出来，走廊半径改大 → 选中瓦片数变大

> 夹具里**没有县级**（它是自造几何）。要验县，走 **E 段**，那里用的是真实的
> 2882 条县级数据。

### D22 ★ 五个区域各估一次，看推荐区间对不对

依次选五级区域，点「计算」。

**通过判据**（这是 §5 的硬契约）：

| 选中的等级 | 推荐区间必须是 |
| --- | --- |
| 全国 | `Z4~10` |
| 省级 | `Z9~13` |
| 市级 | `Z12~16` |
| 县级 | `Z14~18` |
| 任务区 | `Z15~18` |

- 手填一个**超出**推荐上界的 Zoom → 出警告；手填区间内 → 无警告
- **手填的值不许被自动改掉**（"我填 Z18，它偷偷改成 Z10" 是缺陷，不是贴心）
- 选中「自定义矩形」→ 不套用任何等级建议（没有警告，也没有推荐）

> 县级与市级、任务区的区间**是重叠的**（14~18 与 12~16 / 15~18 相交）。
> 这不是笔误：重叠段里靠"等级更具体优先"决胜，真机上的验证在 **E29**。

### D23 ★ 建一个五级下载计划

把 `示例国`、`示例北省`、`示例河市`、一个县、任务区**依次**加入下载计划。

**通过判据**：
- 计划里五行的顺序是 **全国 → 省 → 市 → 县 → 任务区**（大范围先下）
- 每行都有自己的 Zoom 区间与预估张数，合计 = 各行之和
- 停用其中一行 → 合计立刻变小（停用的项**不计入**合计）
- 合计跨过风险阈值时弹确认框，文案说清是哪一维触发（张数 / 容量）

> 规模提醒：夹具的市级区域若照推荐区间（Z12~16）跑是十几万张。
> 手工验收时把每级的 Zoom 缩到**推荐区间的下界一两级**（如市级只填 Z12），
> 十几分钟能跑完。

### D24 ★ 下载、检查、打包，然后核对 manifest

计划跑完后，对**行政区**和**任务区**各打一个包。

**通过判据**（这一段是 Slice 25 修的元数据缺陷的现场）：

| 包 | `manifest.region` 必须有 | 必须**没有** |
| --- | --- | --- |
| 行政区（市级为例） | `type=polygon`、`id`、`boundaryVersion`、**`level=city`** | `bufferMeters`、`geometryVersion` |
| 行政区（**县级**） | 同上，但 **`level=county`** | 同上 |
| 任务区 | 上面那些 + `level=mission` | — |
| 任务区 | `bufferMeters`（走廊半径） | — |
| 任务区 | `geometryVersion`（== `boundaryVersion`） | — |

```bash
cd D:/project/MapDownloader
.venv/Scripts/python.exe -c "
import json,sys; sys.path.insert(0,'.')
from app.package.models import parse_manifest_dict
p=r'<包目录>/manifest.json'
m=parse_manifest_dict(json.load(open(p, encoding='utf-8')))
print(json.dumps(m.region.to_dict(), ensure_ascii=False))
"
```

> **`level` 是最容易漏的一项**，因为它漏了不报错：包照常导入、照常显示，
> 只是 Android 侧把它当成"未知等级"。见 `troubleshooting.md` 第 15 条。

### D25 ★ 传到手机，验证"按 Zoom 选对那一档"

把五个包都推到设备并导入（B 阶段的命令照旧，逐个包做一遍）。

**通过判据**：切到 AUTO 模式，把地图 Zoom 依次调到下面这几档，
地图页状态行第一行的**等级**应当跟着换：

| Zoom | 期望选到的等级 | 说明 |
| --- | --- | --- |
| ~7 | 全国 | 只有全国档支持 |
| ~11 | 省级 | 省级 9~13，市级 12~16 够不着 |
| ~13 | 市级 | 市级 12~16；**县级 14~18 够不着** |
| ~15 | 县级 | 市级也支持，但县级更具体（§5-3） |
| ~17 | 任务区 | 县级也支持，任务区更具体 |
| ~19 | 退回在线 | 所有包的 maxZoom 都 ≤18 |

- 同一档里来回微调 Zoom（±0.3 级）**不应**反复换底图（迟滞在起作用）
- 拔网 / 飞行模式下走到上面每一档，图都还在（这是整条线的意义）

> **Z14 这一档要小心**：全新评估（刚切 AUTO）在 Z14 就该选县级；
> 但如果你是从市级一路放大上来，**Z14.0~14.4 仍停在市级是正确行为** ——
> 迟滞要求越过 `县级.minZoom + 0.5 = 14.5`。这条的完整判据在 **E29**。
>
> 若某一档写的是「未知」→ 那个包的 `level` 没落进包里，回 D24 核对。

---

## E. 县级专项（E26 – E31）

县级不是"再往下列一级"。仓库里的真实数据（`resources/regions/generated/`，
`boundaryVersion=2026-10`）有三种形态是市级没有的：

| 形态 | 数量 / 实例 | 为什么危险 |
| --- | --- | --- |
| **父级直挂省** | 155 个（`156500103 渝中区 → 重庆市`） | 层级校验若只认"县必须挂市"，直辖市与省直辖县级市会整片判错 |
| **跨省同名** | 29 组 / 66 个县（`鼓楼区` ×4：南京 / 徐州 / 开封 / 福州） | 用名字当 id 就会互相覆盖，且**不报错** |
| **极小县** | `156810109 油尖旺区`，外接矩形 0.0009°（约 3×4 km） | 瓦片数极少、Zoom 区间窄，容易出"空包"与 clamp 异常 |

再加上一个结构性问题：县级的推荐区间 `Z14~18` **夹在**市级 `Z12~16` 与任务区
`Z15~18` **中间**，三档两两重叠。于是"县级插在阶梯的正确位置"不能靠"能导入"证明，
只能靠**重叠段里谁被选中**证明。

> 这一段用**真实目录数据**，不用 D 段的合成夹具。跑之前先把县级数据导入
> （`resources/regions/generated/` 已是 gitignore，换机要重跑）：
>
> ```bash
> cd D:/project/MapDownloader
> .venv/Scripts/python.exe scripts/import_china_admin_regions.py \
>   --province <省.geojson> --city <市.geojson> --county <县.geojson> --check
> ```
>
> **通过判据**：`--check` 报告里 `county` 计数为 **2882**，
> 且 `parentId` 直挂省的县为 **155** 个（命令见 E26）。

---

### E26 ★ 县级在目录里选得到（Windows）

打开下载面板，「区域」选「行政区域」。

**(a) 普通县（父级是市）**

| 操作 | 期望 |
| --- | --- |
| 国家 `中国` → 省 `江苏省` → 市 `南京市` | 市下拉能选中 |
| 县区下拉 | 出现 `鼓楼区`（以及南京其余区县） |
| 选中 `鼓楼区` | **候选范围行**跟着变；「下载级别」推导为 `县级`；推荐区间显示 `Z14~18` |

**(b) 直辖市下辖区（父级是省，没有市级）**

| 操作 | 期望 |
| --- | --- |
| 国家 `中国` → 省 `重庆市` | 市级下拉显示 `（该省暂无城市级数据）`（文案可以是"暂无"，**但县区下拉必须可用**） |
| 县区下拉 | 列出重庆的下辖区（`渝中区`、`大渡口区`…） |
| 选中 `渝中区` | 候选范围行有效；下载级别 `县级`；**不弹"层级错误"** |

这一条是 `PARENT_LEVEL_RULES` 里 `COUNTY → {CITY, PROVINCE}` 那个双父级的现场。
只认单父级的实现会在这里把 155 个县全部判成"层级错误"。

**(c) 同名县是不同的两个县**

| 操作 | 期望 |
| --- | --- |
| 省 `江苏省` → 市 `南京市` → 县区 `鼓楼区` | 候选范围行显示**南京**那块（`118.714~118.800 / 32.031~32.134`） |
| 省 `江苏省` → 市 `徐州市` → 县区 `鼓楼区` | 候选范围行显示**徐州**那块（`117.110~117.288 / 34.265~34.358`） |

两者 **id 不同**（`156320106` / `156320302`）、范围不同。
若两次显示的范围一样 → 目录把同名县合成了一条，这是**阻断级缺陷**（会让
E30 的"两个县共存"根本做不到）。

**(d) 用命令独立核对目录**（不要只信界面）

```bash
cd D:/project/MapDownloader
.venv/Scripts/python.exe -c "
import json
items = json.load(open('resources/regions/generated/index.json', encoding='utf-8'))['regions']
counties = [r for r in items if r['level'] == 'county']
prov = {r['id'] for r in items if r['level'] == 'province'}
print('县级总数     :', len(counties))
print('父级直挂省   :', len([r for r in counties if r['parentId'] in prov]))
print('鼓楼区       :', [(r['id'], r['parentId']) for r in counties if r['name'] == '鼓楼区'])
print('油尖旺区     :', [(r['id'], r['bounds']) for r in counties if r['name'] == '油尖旺区'])
"
```

**通过判据**：`2882` / `155` / 四组鼓楼区 id / 油尖旺区的四个 bounds 数字，
与本文写的一致。对不上说明这份数据不是你验的那一版，先解决数据再往下走。

---

### E27 ★ 县级包的 manifest 与 MBTiles metadata

对 E26 选的 `鼓楼区`（南京，`156320106`）下载并生成地图包（A3~A6 照旧，
层级建议填 `Z14~15` 以便几分钟跑完 —— **推荐区间是 14~18，但验收不必下满**）。

**通过判据**（`manifest.json` 的 `region` 子对象）：

| 字段 | 期望 |
| --- | --- |
| `type` | `polygon` |
| `id` | `156320106` |
| `level` | **`county`** |
| `boundaryVersion` | `2026-10` |
| `bufferMeters` | **不存在**（走廊半径只属于任务区） |
| `geometryVersion` | **不存在**（行政区用 `boundaryVersion`） |

再核对 MBTiles 自己的 `metadata` 表（Inspector 读的是这一份，与 manifest 是两条路）：

```bash
cd D:/project/MapDownloader
.venv/Scripts/python.exe -c "
import sqlite3, sys
db = sqlite3.connect(r'<包目录>/map.mbtiles')
rows = dict(db.execute('SELECT name, value FROM metadata').fetchall())
for key in ('region_type', 'region_id', 'region_level', 'boundary_version'):
    print(f'{key:18} = {rows.get(key)!r}')
print('buffer_meters     =', rows.get('buffer_meters', '<不存在，正确>'))
"
```

**通过判据**：`region_level = 'county'`，`region_type = 'polygon'`，
`buffer_meters` 不存在。

> `region_level` 与 `manifest.region.level` **必须同时正确**。前者给 Inspector /
> 事后排查，后者给 Android 选包 —— 只写一个也能"跑通"，但下一次换工具读就露馅。

---

### E28 ★ 手机上等级被认出来（本次跨仓库契约的现场）

把 E27 的包推到设备并导入（B8~B11 推送与校验，C13 导入，照旧）。

**先看一条最容易误判的现状**：管理页列表**不显示等级**（`OfflineMapListItem`
只显示状态与大小）。也就是说 `level` 解析错了，在管理页上**完全看不出来** ——
包能用、能选中、地图能画，只是**永远排在最后**。所以判据必须从"选包结果"取。

**(a) 日志判据（最硬，不依赖点界面）**

```bash
DEV=<serial>
D:/develop/android/sdk/platform-tools/adb.exe -s $DEV logcat -c
# 然后在 App 里让 AUTO 重新评估一次（拖动一下地图，或切一次模式）
D:/develop/android/sdk/platform-tools/adb.exe -s $DEV logcat -d \
  | grep "AUTO evaluate" | tail -3
```

日志形如：

```
AUTO evaluate: location=(32.06, 118.77), zoom=14.6, candidates=2, selected=156320106, reason=COUNTY_MATCH, shouldSwitch=true
```

**通过判据**：`reason=` 是 **`COUNTY_MATCH`**。

| 实际看到的 | 含义 | 处置 |
| --- | --- | --- |
| `reason=COUNTY_MATCH` | 等级被正确解析成 `COUNTY` | 通过 |
| `reason=BEST_MATCH` | `region.level` 没解析出来（落成 `UNKNOWN` 或 `CUSTOM`） | 回 E27 核对 `level` 拼写；若 manifest 对，则是 Android 侧枚举缺 `COUNTY` |
| `reason=CITY_MATCH` | 选中了市级包，县级这张没进候选 | 看 `candidates=`：为 0 说明 Zoom 或范围不满足（回 E29） |
| 一行都没有 | AUTO 没在评估 | 确认模式真的是「自动」，且地图页在前台 |

> `reason` 这一栏是"选包决策"的完整答案：`candidates` 是过了覆盖 + Zoom 过滤
> 的数量，`selected` 是选中的包 id。三栏合起来能区分"没候选"与"有候选但没选它"。

**(b) 界面判据**

在管理页点这张包的 **[使用]**，再切回 **[自动]**，读地图页状态行第一行。

**通过判据**：第一行是**三段**且中间那段是 `COUNTY`：

```
自动 · COUNTY · 鼓楼区 Z14~15
```

等级段是**英文大写枚举名**（`COUNTY`），不是中文"县级" ——
它与 D 段的 `COUNTRY` / `CITY` 保持一致，便于 grep。

**若第一行只有两段**（`自动 · <包名>`）→ 等级段被整个省略，说明
`RegionLevel.isKnown()` 为假，也就是落到了 `UNKNOWN`。**这是 FAIL，不是"显示问题"**。

---

### E29 ★ 县级在阶梯里的位置（重叠段谁胜出）

准备四个包**同时装在同一台设备上**、**都覆盖地图中心**：全国、市级、县级、任务区
（省级可选）。用 A~C 段的方法各做一个，Zoom 区间按推荐值：

| 包 | Zoom 区间 |
| --- | --- |
| 全国 | `Z4~10` |
| 市级 | `Z12~16` |
| **县级** | **`Z14~18`** |
| 任务区 | `Z15~18` |

**(a) 全新评估（没有"当前包"）**

在管理页点**对应档**的 [使用] 把 Zoom 定到区间边界，再切 **[自动]** ——
`useAuto()` 不保留当前包，等于完整重新评估一次。

> `10 / 13 / 14 / 15` 四个值都能用 [使用] 精确压到（clamp 只取边界）；
> `16.5` 与 `18.5` 只能靠**双指捏合**放大，压不准也没关系 ——
> 它们的判据是"落在哪个区间"，不是"精确到 0.1"。

| 定到的 Zoom | 候选（支持该 Zoom 的档） | 期望选中的等级 |
| --- | --- | --- |
| `10` | 全国、省级 | `PROVINCE`（**不是全国** —— 省级 9~13 也够得着 Z10） |
| `13` | 省级、市级 | `CITY`（县级 14~18 够不着） |
| `14` | 市级、**县级** | **`COUNTY`**（更具体者胜，§5-3） |
| `15` | 市级、县级、任务区 | `MISSION` |
| `16.5` | 县级、任务区 | `MISSION` |
| `18.5` | 无 | 退回在线，reason 是 `ZOOM_NOT_SUPPORTED` |

**通过判据**：每一档的 `reason` 与等级一致；`14` 与 `16.5` 这两档是关键 ——
`14` 证明**县级压过市级**，`16.5` 证明**县级仍让位于任务区**。
只测 `14` 不测 `16.5`，会把"县级被错排成最具体一级"漏过去。

**(b) 逐级放大（有"当前包"，迟滞生效）**

在 AUTO 模式下，从市级档开始**慢慢放大**（双指捏合；若你的设备双击生效应看到
同样的结论），盯着状态行第一行的等级。

| 当前等级 | Zoom 到达 | 期望 |
| --- | --- | --- |
| `CITY` | `14.0 ~ 14.4` | **保持 `CITY`** —— 迟滞要求越过 `县级.minZoom + 0.5 = 14.5` |
| `CITY` | `≥ 14.5` | 切到 `COUNTY` |
| `COUNTY` | `15.0 ~ 15.4` | **保持 `COUNTY`** —— 切任务区要越过 `15.5` |
| `COUNTY` | `≥ 15.5` | 切到 `MISSION` |
| `COUNTY` | 缩回 `13.9` | **立刻**回 `CITY`（反向是硬过滤，**没有**迟滞） |
| `CITY` | 缩回 `11.9` | 回 `PROVINCE`（省级 9~13） |

> **`Z14.0 ~ 14.4` 停在市级是正确行为，不是缺陷。** 这条最容易被误报成
> "县级没生效"。判据以 `14.5` 为界，不要以 `14` 为界。

**(c) 同 Zoom 同级 tie-break（可选，但建议做一次）**

同时装**两个**县级包、区间都是 `Z14~18`、都覆盖中心（不同 bounds 大小）。

**通过判据**：选中**外接矩形更小**的那个（§5-5）；若范围相同，则
`maxZoom` 更高者优先（§5-6）；再相同则 id 字典序更小者（§5-7）。
这条只验"规则在真机上真的按顺序走"，具体选谁不重要。

---

### E30 同名县共存（跨省同名不是同一个包）

做两个县级包：**南京市鼓楼区**（`156320106`）与**徐州市鼓楼区**（`156320302`）。

> 打包时把 `name` 写成 `鼓楼区（南京）` / `鼓楼区（徐州）` —— **只为验收可读**。
> 真实使用中两个包可以同名，id 不同才是唯一性所在。

**通过判据**：

| 检查 | 期望 |
| --- | --- |
| 管理页列表 | **两行都在**，不是"后导入的覆盖了先导入的" |
| 两行的 id / 路径 | 分别是 `156320106` 与 `156320302` 两个目录 |
| AUTO 在南京（`32.06, 118.77`） | 选中的是南京那张（状态行来源一行显示 `鼓楼区（南京）`） |
| AUTO 在徐州（`34.27, 117.18`） | 选中的是徐州那张 |
| 删掉南京那张 | 徐州那张**不受影响**；在南京位置退回在线，`reason=NO_COVERAGE` |
| 全程 | 不崩、不白屏、管理页不出现"已损坏" |

**若两行只出现一行**：说明包目录名或 manifest `id` 用了名字派生 ——
两个同名县落到了同一个目录。这是**阻断级缺陷**（真实数据里 66 个县会遇到）。

---

### E31 极小县与删除回退

用 `油尖旺区`（`156810109`，外接矩形 `114.1537~114.1818 / 22.2930~22.3263`，
约 3×4 km），Zoom 用 `Z14~18`（推荐区间，不必缩）。

**通过判据**：

| 检查 | 期望 |
| --- | --- |
| 预估瓦片数 | 大于 0，且量级在**几十到几百**（一个 3×4km 的区在 Z14~18 就是这个量级） |
| 下载完成 | 状态"完成"；瓦片数 == 预估数 |
| 包体积 | 小（几 MB 级），不出现 0 字节的 `map.mbtiles` |
| Inspector | `PASS`；Zoom 区间 `14~18`；抽样格式 `PNG` |
| 导入 | 成功，不报"空包"/"无效包" |
| OFFLINE [使用] | Zoom 被 **clamp 到 14**（例如原来在 13 → 变 14） |
| 拔网看图 | 底图有内容，拖动不白屏 |
| 删除当前县级包 | 回退到更粗一级（市级/省级）或在线；**不崩、不白屏**、无目录残留 |

**若预估瓦片数是 0**：县太小、或边界几何退化（自相交/零面积）。
Inspector 那一步会暴露；回 `troubleshooting.md` 的边界条目。

**若 OFFLINE 下 Zoom 没被 clamp 到 14**：说明包的 `minZoom` 没写进 manifest
或写成了 13 —— 回 E27 核对。

---

### E 段的已知缺陷（当前版本会红 —— 修好之前别把它当"通过"）

| 缺陷 | 怎么复现 | 后果 | 状态 |
| --- | --- | --- | --- |
| **县级选择不持久化** | E26(c) 选到 `鼓楼区` → 关掉工具 → 重开 | 县区下拉回到「全部（不限定县区）」，下载范围**悄悄变成整个市**（或省）。`RegionSelection` 只存 `country/province/city` 三段 id，`UiPreferences.set_region_selection` 不写县 | 未修（Slice 27 的收尾遗漏） |

这一条的危险在于**它不报错**：重启后界面一切正常，只是范围变大；而范围变大
通常只表现为"预估瓦片数变多"，很容易被当成自己看错了。修法是把
`county_id` 加进 `RegionSelection` / `_KEY_COUNTY_ID` / `_MANAGED_KEYS`
并让恢复流程把它应用到第四段下拉 —— 改动会牵到偏好往返测试，属于独立的一小块活。

验证方式（修好后应当**通过**）：

```bash
# 1. 选到 江苏省 / 南京市 / 鼓楼区，关工具
# 2. 重开工具，看县区下拉
# 期望：仍停在「鼓楼区」，下载级别仍是「县级」
cd D:/project/MapDownloader && .venv/Scripts/python.exe -m app.main
```

### E 段的已知限制（不是缺陷，别当 bug 报）

AUTO 的"覆盖"判定用的是**外接矩形**（`bounds`），不是多边形。
manifest 里没有、也不打算有多边形（包体积与解析成本都不值当）。

县级面积小、边界曲折，所以**邻县误覆盖**的概率显著高于市级：
站在 A 县境内、却正好落在 B 县的外接矩形里，AUTO 会选 B 那张。

验收时的判断方法：

1. 用 E26 的 `index.json` 查出两个县的 `bounds`；
2. 若当前点确实同时落在两者的外接矩形内 → **这是已知限制**，记录即可；
3. 若当前点只落在 A 县外接矩形内却选了 B → 那是真缺陷（覆盖判定或排序错了）。

### E 段的实测记录

这套条目不是只写在纸上。**2026-10-23** 在 Lenovo TB320FC（Android 15，
`HA1YGV6R`）上跑过一遍，用 `scripts/verify_slice28_county_selection.py`：

| 段 | 检查数 | 结果 | 关键读数 |
| --- | --- | --- | --- |
| bootstrap（装机 + 5 个夹具 + 前提） | 6 | 全过 | 设备无其它包、五档都盖中心 |
| ladder（E28 日志判据 + E29 阶梯） | 18 | 全过 | Z10→`PROVINCE`、Z13→`CITY`、**Z14→`COUNTY`**、Z15→`MISSION`；`reason` 分别是 `PROVINCE_MATCH` / `CITY_MATCH` / **`COUNTY_MATCH`** / `MISSION_MATCH`，candidates 2/2/2/3 |
| same_name（E30） | 10 | 全过 | 两个县两个目录；删徐州那张，南京那张不受影响 |
| delete_current（E31 后半） | 10 | 全过 | 删当前县级包后回 AUTO，底图仍有瓦片、无 FATAL/ANR、无目录残留 |
| cleanup | 1 | 全过 | 夹具自删干净 |

`Z14 → COUNTY`（candidates=2，市+县）这一条就是**本次县级支持真的生效**的证据：
同一次评估里市级与县级都合格，选的是更具体的那一级。

脚本自己在 `--only ladder` 时会连带跑 bootstrap（重推夹具 + 装 APK），
所以单跑一段也是自洽的。

---

## 验收结论记录

| 步骤 | 结果 | 证据 / 备注 |
| --- | --- | --- |
| A1 启动 | | |
| A2 环境自检 | | |
| A3 范围与层级 | | 预估瓦片数： |
| A4 下载 | | |
| A5 检查 MBTiles | | |
| A6 生成地图包 | | 包路径： |
| A7 核对 manifest | | sha256: |
| B8 设备可见 | | serial: |
| B9 推送 | | |
| B10 字节数一致 | | |
| B11 SHA256 一致 | | |
| C12 管理页打开 | | |
| C13 导入 | | |
| C14 目录结构 | | |
| C15 离线可用 | | |
| C16 拔网仍可用 | | ★ 关键 |
| C17 AUTO | | |
| C18 删除回退 | | |
| C19 多地图 | | |
| C20 版本核对 | | |
| D21 层级区域可选 | | 用的哪份目录： |
| D22 推荐区间五级 | | 全国/省/市/县/任务区： |
| D23 计划顺序 | | 合计张数： |
| D24 manifest.region 的 level | | 行政区 level: / 县级 level: / 任务区 level: |
| D25 按 Zoom 选等级 | | Z7/11/13/15/17/19 → |
| E26 县级目录可选 | | 2882 / 155 / 鼓楼区 4 条： |
| E27 县级 manifest 与 metadata | | level= / metadata region_level= |
| E28 等级被认出 | | logcat reason= / 状态行第一行： |
| E29 阶梯重叠段 | | Z14→ / Z14.0~14.4→ / Z14.5→ / Z16.5→ |
| E30 同名县共存 | | 两行 id： / 南京→ / 徐州→ |
| E31 极小县与删除回退 | | 预估张数： / clamp→ / 删除后→ |

**环境快照**：Windows 工具版本 ______ / Package Format ______ /
Android 运行时 ______ / 设备 ______ / Android 版本 ______

---

## 全自动版本

上面 C 阶段的大部分内容是**真的点界面**，无法完全脚本化。仓库里已有的自动化脚本：

| 脚本 | 覆盖 |
| --- | --- |
| `scripts/e2e_smoke_test.py`（Windows） | A 阶段的**离线**等价链路（Tile Math → MBTiles → Inspector → Package → SHA256 → Validator） |
| `scripts/hierarchical_map_e2e.py`（Windows） | **D21 – D24 全部**：层级目录 → Zoom 策略 → 多边形估算 → 任务区 → 风险 → 下载计划 → 调度 → MBTiles → Inspector → 打包。全程离线（Mock 瓦片源），10 段全 PASS 才算过 |
| `scripts/verify_slice11_offline_import.py` | C12–C14（导入、目录、校验） |
| `scripts/verify_slice12_offline_runtime.py` | C15–C17（OFFLINE / AUTO / 拔网） |
| `scripts/verify_slice13_offline_management.py` | C18–C19（删除、多地图） |
| `scripts/verify_slice14_offline_stability.py` | 稳定性（长时间切换、压力） |
| `scripts/verify_slice24_auto_selection.py` | **D25 的自动化版本**：真机上的 AUTO 阶梯与迟滞 |
| `scripts/verify_slice28_county_selection.py` | **E28 的日志判据 + E29 的阶梯 + E30/E31 的共存与删除回退**：真机上验"县级赢市级、让位任务区"、`reason=COUNTY_MATCH`、同名县两个目录、删当前县级包的回退。夹具自造（5 档 + 第 2 个县），跑完自删 |

跑法：

```bash
cd D:/project/osmdroid
python scripts/verify_slice11_offline_import.py --serial <serial>

# 县级专项（先 bootstrap 推夹具，再跑阶梯；--only 可单跑一段）
python scripts/verify_slice28_county_selection.py --serial <serial>
python scripts/verify_slice28_county_selection.py --serial <serial> --only ladder
```

> `verify_slice28` **测不到迟滞的小数边界**（14.5 / 15.5）：真机上 `[使用]`
> 只能把 Zoom clamp 到区间边界，双击放大在测试设备上是预存在缺陷。
> 那一半由 `OfflineMapSelectorTest` 守着 —— 脚本跑完会把这条写进报告，
> 免得把"没测"看成"测过且通过"。

Windows 侧的分级全链路：

```bash
cd D:/project/MapDownloader
.venv/Scripts/python.exe scripts/hierarchical_map_e2e.py
# 退出码 0 = 10 段全过；加 --keep 保留工作目录去翻产物（.mbtiles / manifest.json）
```

截图落在 `docs/`（**该目录在 .gitignore 里**，是本地证据，不进提交）。

> Windows 侧的 A 阶段用 `python scripts/e2e_smoke_test.py` 做**离线**版本的等价验证
> （Mock 瓦片源，不访问天地图）。它不能替代 A4 的真实下载，但能在几秒内
> 证明"Tile Math → MBTiles → Inspector → Package → SHA256"这条链路没断。
