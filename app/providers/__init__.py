"""瓦片源 Provider 层（Slice 1+）。

计划内容：
- ``TileProvider`` 抽象基类：统一 ``build_url(layer, z, x, y)`` / 请求头 / 并发上限。
- ``TiandituProvider``：天地图 WMTS 服务实现（本项目 Provider 之一）。
- ``OsmProvider``：OSM 标准 XYZ，作为对照与调试用。

约束：URL 模板与请求参数集中在各 Provider 内，UI 不感知具体服务细节。
本 Slice 不实现任何网络请求。
"""
