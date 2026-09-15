"""瓦片合成层（Slice 6）。

把"底图瓦片"与"中文注记瓦片"合成成一张带透明通道的 PNG::

    base tile (vec_w / img_w)      label tile (cva_w / cia_w)
              │                              │
              └──────── RGBA ────────────────┘
                            │
                  Image.alpha_composite()
                            │
                        PNG bytes

为什么用 ``alpha_composite`` 而不是 ``paste``
-------------------------------------------

注记瓦片是"透明背景 + 少量文字"的图片。``paste(label, (0, 0))`` 会把 label 的
**整张矩形**（包括它那些"透明"的像素）直接覆盖上去 —— Pillow 的 ``paste``
不认 alpha，透明像素会按 RGB 值写进去（通常是黑色或白色），结果就是底图被
一张"只有文字是彩色、其余全黑"的贴纸盖住。

``Image.alpha_composite`` 才是按 alpha 做逐像素混合的正确做法：注记透明的地方
保留底图颜色，注记不透明的地方显示文字。

边界约定
--------

- 本层**只做图像运算**：不发请求、不落盘、不 import PySide6。
- 全程内存（``io.BytesIO``）：绝不产生任何临时图片文件（Spec §5）。
- 输入 ``bytes`` 只读，不做任何原地修改。
"""

from app.composer.tile_composer import TileComposeError, TileComposer

__all__ = ["TileComposer", "TileComposeError"]
