"""瓦片仓库的错误分类（Slice 42）。

为什么要一个专门的异常树而不是到处 ``ValueError``
--------------------------------------------------

仓库的调用方（Slice 43 导入、44 下载、45 导出、46 预算）对失败的处理**完全不同**：

* ``Conflict`` —— 同坐标不同内容，多半是图层/注记串了。要**报告并停止**，不能重试；
* ``Busy`` —— 另一个进程正拿着租约。要**提示并稍后重试**，不能当损坏；
* ``Corrupt`` —— 摘要不符、悬空引用。要**隔离并触发重建**，绝不能静默跳过；
* ``Cancelled`` —— 用户点了取消。要**如实回报已提交的部分**，不能当失败重试。

把它们压成一个异常，调用方只能靠 ``str(exc)`` 猜，于是"仓库被占用"会被显示成
"仓库损坏"，用户去删文件。所以每个分类是一个类型，消息只用于人读。

冻结约定（见 docs/tile_repository_contracts §5.9）
--------------------------------------------------

异常消息里**不得出现**：token、带查询串的完整 URL、用户主目录里的用户名段。
``Busy`` 携带的持有者信息只有 ``pid`` / ``host`` / ``label`` 三个字段。
"""

from __future__ import annotations

__all__ = [
    "Busy",
    "Cancelled",
    "Conflict",
    "Corrupt",
    "InsufficientBudget",
    "InsufficientRequiredTiles",
    "InvalidTile",
    "NotFound",
    "RepoClosed",
    "RepoError",
    "SchemaUnsupported",
    "SnapshotExpired",
    "UnsupportedOperation",
]


class RepoError(Exception):
    """仓库错误的根类型。

    Attributes:
        code: 稳定的机器可读分类名（``'conflict'`` / ``'busy'`` …）。日志与
            报告用它分组，**不要**用 ``str(exc)``——那是给人读的中文句子。
        detail: 结构化的补充信息（不含 token、不含图片字节）。子类各自约定键。
    """

    code: str = "internal"

    def __init__(self, message: str, **detail: object) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, object] = detail

    def as_dict(self) -> dict[str, object]:
        """报告用的扁平形态（JSON 可序列化）。"""
        return {"code": self.code, "message": self.message, **self.detail}


class NotFound(RepoError):
    """资产 / 分片 / 快照不存在。

    注意：**单张瓦片不存在不算 ``NotFound``** —— 那是查询结果 ``ABSENT``。
    这条区别很重要：缺口计算要的是计数，把缺图抛成异常会让循环变成 try/except 迷宫。
    """

    code = "not_found"


class Corrupt(RepoError):
    """分片结构坏、摘要不符、悬空引用、schema 标记丢失。

    一旦抛出，该分片立即退出服务（健康检查会把它列出来），不做"尽力而为"的读取
    —— 半信半疑地读一个坏库比读不到更危险。
    """

    code = "corrupt"


class Conflict(RepoError):
    """同坐标不同内容，且调用方没有授权替换。

    Attributes:
        coords: 冲突坐标清单（``[(z, x, y), ...]``，可能有上限截断）。
        truncated: 清单是否被截断。
    """

    code = "conflict"


class Busy(RepoError):
    """租约被占用（同分片已有别的写入者）。

    不排队、不阻塞等待：调用方决定是提示用户还是稍后重试。
    """

    code = "busy"


class SnapshotExpired(RepoError):
    """快照引用的内容已经被回收。

    必须抛错而不是跳过：跳过会让导出产出"少了几张但报告说齐全"的包。
    """

    code = "snapshot_expired"


class Cancelled(RepoError):
    """调用方通过 ``should_stop`` 中止。

    注意：本 Slice 的写接口**不抛它**，而是返回 ``cancelled=True`` 的报告
    （已提交的批次要如实回报）。保留该类型给"必须整体中止"的接口（Slice 43 起）。
    """

    code = "cancelled"


class SchemaUnsupported(RepoError):
    """schema 版本认不出来（比代码新，或分片写了一半没写版本）。

    明确拒绝服务，**不自动重建**：自动重建会把"版本更新的仓库"当成空库清掉。
    """

    code = "schema_unsupported"


class InvalidTile(RepoError):
    """输入不是一张合法瓦片（空、magic bytes 不对、尺寸不符）。

    这是"调用方给我的东西有问题"，与 ``Corrupt``（"仓库里的东西坏了"）不同：
    前者不入仓，后者要隔离。
    """

    code = "invalid_tile"


class RepoClosed(RepoError):
    """仓库实例已经 ``close()``，后续操作一律拒绝。

    比"文件句柄已关闭"的 ``sqlite3.ProgrammingError`` 明确得多 —— 后者在
    多线程环境下表现为随机报错，看不出真凶是生命周期。
    """

    code = "repo_closed"


class UnsupportedOperation(RepoError):
    """本 Slice 明确不实现的能力（例如真正删除内容）。"""

    code = "unsupported"


class InsufficientRequiredTiles(RepoError):
    """缺图且策略为 ``reject_if_missing``（Slice 45 使用，此处仅定义分类）。"""

    code = "insufficient_tiles"


class InsufficientBudget(RepoError):
    """超预算 / 磁盘不足（Slice 46 使用，此处仅定义分类）。"""

    code = "insufficient_budget"
