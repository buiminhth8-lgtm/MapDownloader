"""MBTiles 读写层（Slice 3+）。

计划内容：
- 建库与元数据表（``metadata``）+ 瓦片表（``tiles``，TMS Y 轴翻转）。
- 批量事务写入，避免逐条提交。
- 从瓦片目录反向打包，以及 MBTiles 元信息读取。

约束：使用标准 SQLite 单文件格式，保证 osmdroid 可直接读取。
本 Slice 不实现任何数据库代码。
"""
