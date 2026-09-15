"""Android 设备层（Slice 7）。

计划内容：
- ``AdbClient``：封装 ``adb devices`` / ``adb push`` / ``adb shell``，统一超时与错误处理。
- 设备列表解析（serial / model / state）。
- 离线包推送到设备目标目录，并在设备侧校验大小。

约束：所有外部进程调用集中在本层，UI 只调用本层暴露的接口。
本 Slice 不执行任何 adb 命令。
"""
