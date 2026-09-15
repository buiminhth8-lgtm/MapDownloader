"""天地图离线地图工具 —— 应用包根。

包结构概览::

    app/
      config/     全局配置常量（唯一 magic number 来源）
      models/     数据模型（dataclass / enum），不依赖 UI
      ui/         PySide6 界面层，只做展示与用户输入采集
      geo/        瓦片编号 / 经纬度换算（Slice 1）
      providers/  瓦片源 Provider 接口与实现（Slice 2）
      downloader/ 下载调度 / 重试 / 限速（Slice 3）；断点续传策略（Slice 5）
      mbtiles/    MBTiles 读写（Slice 4）
      services/   编排层：下载 → 落盘 → 校验 → 续传（Slice 4 / Slice 5）
      tasks/      下载任务持久化与生命周期（Slice 5）
      package/    离线包打包 / 校验（Slice 6）
      android/    ADB 设备管理与导入（Slice 6）

依赖方向（单向，禁止反向 import）::

    ui  ->  services  ->  downloader / mbtiles / tasks
        ->  providers / geo  ->  models  ->  config

SQLite 只出现在 ``mbtiles`` 里；``tasks`` 只写 JSON；``downloader`` 既不碰数据库
也不碰文件系统（判断"库里有没有这张瓦片"走 ``downloader.resume.TileProbe`` 这个
结构化 Protocol，因此无需 import ``mbtiles``）。
"""
