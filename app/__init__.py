"""天地图离线地图工具 —— 应用包根。

包结构概览::

    app/
      config/     全局配置常量（唯一 magic number 来源）
      models/     数据模型（dataclass / enum），不依赖 UI
      ui/          PySide6 界面层，只做展示与用户输入采集
      providers/   瓦片源 Provider 接口与实现（Slice 1+）
      geo/         瓦片编号 / 经纬度换算（Slice 1+）
      downloader/  下载调度与断点续传（Slice 2+）
      mbtiles/     MBTiles 读写（Slice 3+）
      package/     离线包打包 / 校验（Slice 3+）
      android/     ADB 设备管理与导入（Slice 4+）

依赖方向（单向，禁止反向 import）::

    ui  ->  models  ->  config
    ui  ->  providers / downloader / mbtiles / package / android  (Slice 1+)
"""
