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
>
> 全程约 15 分钟。建议**第一次验收按顺序全跑**；之后回归可以只跑标了 ★ 的那几步。

---

## 准备

| 项 | 要求 |
| --- | --- |
| Windows | 仓库 `D:\project\MapDownloader`，venv 已建（`.venv\Scripts\python.exe`） |
| Android | 仓库 `D:\project\osmdroid`，app 已装（`org.osmdroid.map`） |
| adb | `D:\develop\android\sdk\platform-tools\adb.exe` |
| 设备 | 一台真机或模拟器，**建议先清空** `offline_maps` 目录 |
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

**环境快照**：Windows 工具版本 ______ / Package Format ______ /
Android 运行时 ______ / 设备 ______ / Android 版本 ______

---

## 全自动版本

上面 C 阶段的大部分内容是**真的点界面**，无法完全脚本化。仓库里已有的自动化脚本：

| 脚本 | 覆盖 |
| --- | --- |
| `scripts/verify_slice11_offline_import.py` | C12–C14（导入、目录、校验） |
| `scripts/verify_slice12_offline_runtime.py` | C15–C17（OFFLINE / AUTO / 拔网） |
| `scripts/verify_slice13_offline_management.py` | C18–C19（删除、多地图） |
| `scripts/verify_slice14_offline_stability.py` | 稳定性（长时间切换、压力） |

跑法：

```bash
cd D:/project/osmdroid
python scripts/verify_slice11_offline_import.py --serial <serial>
```

截图落在 `docs/`（**该目录在 .gitignore 里**，是本地证据，不进提交）。

> Windows 侧的 A 阶段用 `python scripts/e2e_smoke_test.py` 做**离线**版本的等价验证
> （Mock 瓦片源，不访问天地图）。它不能替代 A4 的真实下载，但能在几秒内
> 证明"Tile Math → MBTiles → Inspector → Package → SHA256"这条链路没断。
