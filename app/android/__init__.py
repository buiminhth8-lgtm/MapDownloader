"""Android 设备层（**尚未排期**）。

计划内容：

- ``AdbClient``：封装 ``adb devices`` / ``adb push`` / ``adb shell``，
  统一超时与错误处理。
- 设备列表解析（serial / model / state）。
- 离线包推送到设备目标目录，并在设备侧校验大小。

约束：所有外部进程调用集中在本层，UI 只调用本层暴露的接口。

状态说明
--------

本包目前只有这份文档，没有任何实现。设备管理（ADB）**不在**已排期的 Slice
范围内：Slice 7 排给了"Windows GUI 下载管理与交互完善"，而 Spec 同时明令禁止
在该 Slice 里实现 ADB——也就是不许用设备功能挤掉本 Slice 的交付。

因此界面上的设备区按钮只如实说明"本版本不执行 adb 命令"
（文案见 :data:`app.config.settings.UI_TEXT_DEVICE_HINT`），不假装在工作。
"""
