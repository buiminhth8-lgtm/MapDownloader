"""下载任务持久化与生命周期（Slice 5）。

对外入口::

    from app.tasks import TaskService, TaskStore

    service = TaskService()                      # ~/MapDownloader/data/tasks.json
    service.recover_interrupted_tasks()          # 启动时：运行中 → 已暂停
    task = service.create_task(name="beijing", bounds=..., ...)

模块划分
--------

============= =========================================================
``task_store`` JSON 读写：原子写、损坏隔离、白名单字段（Token 不落盘）
``task_service`` 生命周期：创建/暂停/继续/取消/完成/失败 + 节流落盘 + 启动恢复
============= =========================================================

边界约定
--------

- 本包**不引入数据库**（Spec §8）：就一个 JSON 文件。
- 本包**不保存 Token**（Spec §9）：序列化字段是白名单，另有字段名黑名单自检。
- 本包**不自动开始下载**：启动恢复只把"运行中"降级成"已暂停"。
"""

from app.tasks.task_service import TaskService
from app.tasks.task_store import TaskStore, TaskStoreError

__all__ = ["TaskService", "TaskStore", "TaskStoreError"]
