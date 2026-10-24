

````text
执行 Slice 2：ARCore Optional 接入。基于现有 :ar module 和 Slice 0/1 架构继续实现，先审阅当前代码、Gradle、Manifest 和 ArEngine API；保持 ARCore 细节封装在 :ar 内，不修改现有飞行/RTSP/地图/UDP 业务。完成后提交。

目标：
以 AR Optional 方式接入 Google ARCore，使不支持 ARCore、未安装 Google Play Services for AR 的设备仍可正常运行原 App；为 Slice 3 ArActivity 提供稳定 API。

要求：

1. ARCore 依赖
- 仅在 :ar module 引入官方 `com.google.ar:core` 依赖，选择与当前 Gradle/SDK 兼容的稳定版本。
- app 业务代码不得直接依赖 ARCore 类型。

2. Manifest 使用 AR Optional：

<meta-data
    android:name="com.google.ar.core"
    android:value="optional" />


* 不得声明 ARCore 为 required。
* 不得通过 required camera-ar feature 导致非 ARCore 设备无法安装。
* CAMERA permission 若工程尚未声明，可按 AR 使用需要补充，但本 Slice 不主动请求权限。

3. 在 :ar 内新增/完善：
   engine/arcore/

* ArCoreEngine
* ArCoreSupportChecker（或合理等价实现）
* ArCoreInstallManager（如需要）
* ARCore -> 自定义状态映射

所有对外结果必须转换成 :ar 自己的模型，不向 app 暴露：
Session / Frame / Camera / Pose / ArCoreApk.Availability 等 ARCore 类型。

4. 支持检测
   封装 ARCore availability 检测，统一映射至少：

* SUPPORTED
* UNSUPPORTED
* UNKNOWN
* CHECKING（若现有状态模型需要）

必须正确处理 transient/unknown 状态，不得把 UNKNOWN 当 UNSUPPORTED。

5. 安装状态
   封装 Google Play Services for AR：

* 已安装/可用
* 需要安装/升级
* 不支持
* 检查失败

预留 requestInstall(Activity, ...) 能力供 Slice 3 调用。
本 Slice 不允许 App 启动时自动弹 ARCore 安装界面。

6. ArCoreEngine
   接入现有 ArEngine/ArEngineFactory：

* EngineType.ARCORE 可创建 ArCoreEngine
* isSupported/checkSupport 使用真实 ARCore capability
* 生命周期异常必须转为 :ar 自定义状态或错误结果
* 无 ARCore、设备不支持、服务缺失时不得 crash

若当前 ArEngine 设计要求创建 Session，可在 ArCoreEngine 内最小实现 Session ownership；
若没有 Activity/权限等必要条件，不要强行创建，保持明确状态并由 Slice 3 完成 UI 生命周期接入。

7. 状态/异常
   处理常见场景：

* ARCore service 未安装
* 版本过旧
* device unsupported
* camera permission 缺失
* SDK/session 初始化失败
* availability 暂时未知

禁止使用 catch(Exception) 后静默吞错；转换为清晰的 ArSessionState/错误原因，并保留必要日志。

8. 与 Slice 0 联动
   不要删除 Capability Probe。
   如果 Probe 已存在 ARCore 检测，复用/下沉公共逻辑，避免形成两套互相矛盾的 ARCore 判断。
   Probe 仍然只报告真实检测结果，不伪造 6DoF 支持。

9. 测试
   至少覆盖：

* ARCore Availability -> 自定义状态映射
* unsupported/unknown 分支
* ArEngineFactory 创建 ARCORE
* NoOp/Mock 原行为不回归

不要让 JVM 单测真正依赖设备 ARCore Runtime；把纯状态转换逻辑设计为可单测。

10. 更新 docs/AR_ARCHITECTURE.md 或新增相关说明，记录：

* AR Optional 策略
* availability/install/session 三者区别
* 非 ARCore 设备为什么仍能运行 App
* Slice 3 应如何调用 support/install API

11. 严格禁止本 Slice：

* 新建正式 ArActivity
* Camera preview UI
* OpenGL Renderer
* Frame rendering
* Anchor
* UAV/Telemetry
* 坐标转换
* AR HUD
* 修改飞控、RTSP、地图业务

12. 验证：

* ./gradlew :ar:testDebugUnitTest（若存在）
* ./gradlew assembleDebug
* 检查 merged manifest，确认 ARCore 为 optional
* 确认 :app 没有直接 import com.google.ar.*
* 确认无 ARCore Runtime 时不存在启动期强依赖/crash
* 检查 git diff，无无关修改

若当前环境没有 MC1-5G/ARCore 真机，不伪造真机成功，只记录“编译/单测通过，真机 availability/install 待验证”。

完成后：
git add -A
git commit -m "feat(ar): add optional ARCore integration"

最后输出：

* 修改文件
* ARCore Optional 实现结构
* availability/install/session 处理方式
* 测试与构建结果
* MC1-5G 真机待验证项
* commit hash

严格限制在 Slice 2，不提前实现 Slice 3~13。

````
