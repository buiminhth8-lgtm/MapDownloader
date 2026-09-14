"""离线地图包模型。

描述一个已经生成好的离线包（未来的 .mbtiles / 目录包）的元信息，
供"包管理"和"导入 Android"环节使用。Slice 0 仅定义结构。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


def new_package_id() -> str:
    """生成离线包 ID。"""
    return uuid.uuid4().hex[:12]


@dataclass
class MapPackage:
    """一个离线地图包的元数据。"""

    id: str = field(default_factory=new_package_id)
    name: str = "未命名离线包"
    path: Path = field(default_factory=Path)
    min_zoom: int = 0
    max_zoom: int = 0
    west: float = 0.0
    south: float = 0.0
    east: float = 0.0
    north: float = 0.0
    #: 字节数
    file_size: int = 0
    #: 内容摘要，用于传输后完整性校验（如 ADB push 之后）
    sha256: str = ""

    created_at: datetime = field(default_factory=datetime.now)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(west, south, east, north) 形式的包围盒。"""
        return (self.west, self.south, self.east, self.north)

    @property
    def size_mb(self) -> float:
        """包体积（MB），保留两位小数。"""
        return round(self.file_size / 1024 / 1024, 2)

    @property
    def is_verified(self) -> bool:
        """是否已计算摘要（即通过完整性校验）。"""
        return bool(self.sha256)
