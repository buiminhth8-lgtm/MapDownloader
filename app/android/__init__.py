"""Android 设备层：ADB 设备发现与离线地图包传输（Slice 10）。

把"Windows 端生成好的离线地图包搬进 Android 设备"这件事收在一个包里::

    设备发现 ──→ 设备详情 ──→ 本地包校验 ──→ 远端目录 ──→ push ──→ 远端校验

模块分工（依赖方向自上而下，无环）::

    adb_client.py          只做一件事：把 adb 命令跑起来，并把一切失败折叠成结果
    device_manager.py      只做一件事：把 ``adb devices -l`` 的输出变成设备对象
    transfer_models.py     数据契约 + 远端路径规则（白名单，Spec §15）
    map_package_transfer.py 只做一件事：按安全顺序把一个包搬过去并证明搬对了

上层还有一个编排入口 :class:`app.services.AndroidTransferService`，
界面只调它——界面不自己拼 adb、不自己读 manifest、不自己算校验和（Spec §28）。

边界（Spec §14 / §37 / §47）
---------------------------

- 只往**公共临时导入区**推（``/sdcard/Download/TiandituOfflineMap/import``），
  不碰 ``/data/data`` / ``/data/user`` / ``/system`` / ``/vendor``；
- **不要求 root**，不执行 ``adb root`` / ``adb remount`` / ``adb install`` /
  ``run-as``；
- 不实现 Android App 代码、不实现 ``OfflineMapImporter``、不实现 osmdroid 加载；
- 不压缩、不改 MBTiles、不保存天地图 Token。

设备端的"把导入区的包搬进 App 私有目录并让 osmdroid 加载"是 **Slice 11** 的事。
"""

from app.android.adb_client import (
    ERROR_ADB_MISSING,
    ERROR_CANCELLED,
    ERROR_DEVICE_DISCONNECTED,
    ERROR_START_FAILED,
    ERROR_TIMEOUT,
    AdbClient,
    AdbCommandResult,
    AdbPushProgress,
    resolve_adb_path,
)
from app.android.device_manager import (
    DeviceManager,
    DeviceRefreshOutcome,
    parse_devices_output,
)
from app.android.map_package_transfer import MapPackageTransfer
from app.android.transfer_models import (
    TransferPlan,
    TransferProgress,
    TransferResult,
    is_safe_remote_root,
    join_remote,
    normalize_remote_root,
    part_path,
    remote_dir_for,
    sanitize_remote_id,
)

__all__ = [
    # ADB
    "AdbClient",
    "AdbCommandResult",
    "AdbPushProgress",
    "resolve_adb_path",
    "ERROR_ADB_MISSING",
    "ERROR_CANCELLED",
    "ERROR_DEVICE_DISCONNECTED",
    "ERROR_START_FAILED",
    "ERROR_TIMEOUT",
    # 设备
    "DeviceManager",
    "DeviceRefreshOutcome",
    "parse_devices_output",
    # 传输
    "MapPackageTransfer",
    "TransferPlan",
    "TransferProgress",
    "TransferResult",
    "sanitize_remote_id",
    "remote_dir_for",
    "normalize_remote_root",
    "is_safe_remote_root",
    "part_path",
    "join_remote",
]
