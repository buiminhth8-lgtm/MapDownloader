"""底图 + 中文注记瓦片合成（Slice 6）。

职责单一：**给两个图片字节，返回一张合成后的 PNG 字节**。

    base_data (PNG / JPEG)  ┐
                            ├─→ RGBA ─→ Image.alpha_composite() ─→ PNG bytes
    label_data (PNG / JPEG) ┘

流程里的每一步都是有意为之：

1. **解码**：``Image.open`` 是惰性的，必须显式 ``load()`` 才算真正解码；
   否则"损坏的图片"会在很久之后、在别的地方才炸，异常来源极难定位。
2. **统一转 RGBA**：底图可能是 JPEG（没有 alpha），注记可能是调色板 PNG
   （``P`` 模式 + ``tRNS``）。``alpha_composite`` 只接受同尺寸的 RGBA，
   所以两边都要先归一化 —— 这一步顺带把"透明区域"从各模式的表达
   （``tRNS`` / ``la`` / ``rgba``）统一成真正的 alpha 通道。
3. **尺寸校验**：底图与注记必须是**同一张瓦片**的两层，尺寸不同说明服务端
   给错了东西（或者坐标算错了）。此时报错，而不是把注记贴到一个角落里。
4. **合成**：``alpha_composite`` 按 alpha 逐像素混合。用它而不是 ``paste``，
   原因见包 docstring。
5. **编码**：固定 PNG（Spec §4）。注记依赖 alpha，JPEG 存不下透明通道。
   ``optimize=False``：单张瓦片省几 KB 不值得多花数倍编码时间。

内存与线程
----------

- 全程 ``io.BytesIO``，**不落任何临时文件**（Spec §5 / §25）。
- 纯 CPU 运算，几十毫秒量级，因此**必须在下载 worker 线程里调用**，
  绝不能在 Qt 主线程跑（Spec §16）。本模块自己不碰线程，由调用方保证。
"""

from __future__ import annotations

import logging
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from app.config.settings import (
    COMPOSER_OUTPUT_FORMAT,
    COMPOSER_PNG_OPTIMIZE,
    COMPOSER_SIZE_TOLERANCE_PIXELS,
    TILE_SIZE,
)

# 复用 Slice 2 的 magic bytes 常量：全项目只认一种"这是不是 PNG"的判定，
# 免得出现"合成说自己是 PNG、下载器说它不像 PNG"的互相矛盾。
from app.providers import PNG_SIGNATURE

__all__ = ["TileComposer", "TileComposeError"]


_logger = logging.getLogger(__name__)


class TileComposeError(RuntimeError):
    """两张瓦片无法合成（解码失败、尺寸不一致、编码失败）。

    单独一个异常类型，是为了让上层能把"合成失败"和"下载失败"分开计数：
    Spec §7 要求三类失败原因（``base_download_failed`` /
    ``label_download_failed`` / ``compose_failed``）各算各的，
    否则用户看到"失败 300 张"却不知道是网络问题还是图片问题。
    """


class TileComposer:
    """把底图与注记合成成一张 PNG。

    无状态、可复用（同一个实例可以连续合成几万张），构造之后不持有任何
    打开的文件或连接::

        composer = TileComposer()
        merged = composer.compose(base_bytes, label_bytes)

    Attributes:
        tile_size: 预期的瓦片边长（默认 256，但**不写死**：见 :meth:`compose`）。
    """

    def __init__(
        self,
        *,
        tile_size: int = TILE_SIZE,
        size_tolerance: int = COMPOSER_SIZE_TOLERANCE_PIXELS,
        strict_tile_size: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            tile_size: 预期边长。只用于**提醒**（或 ``strict_tile_size`` 时拒绝），
                不参与合成本身 —— 合成只要求 base 与 label 尺寸相同，
                因此将来服务端改用 512 像素瓦片时不必改这里的逻辑。
            size_tolerance: 与预期边长的最大允许偏差（像素）。
            strict_tile_size: ``True`` 时，尺寸与 ``tile_size`` 不符直接报错；
                默认 ``False``（只记 WARNING）——一次服务端抖动不该让一个
                几万张瓦片的任务全盘失败。
            logger: 日志器，默认用本模块的 logger。

        Raises:
            ValueError: ``tile_size`` 非正，或 ``size_tolerance`` 为负。
        """
        size = int(tile_size)
        if size <= 0:
            raise ValueError(f"tile_size 必须为正，实际为 {tile_size!r}")
        tolerance = int(size_tolerance)
        if tolerance < 0:
            raise ValueError(f"size_tolerance 不能为负，实际为 {size_tolerance!r}")

        self._tile_size = size
        self._size_tolerance = tolerance
        self._strict_tile_size = bool(strict_tile_size)
        self._logger = logger or _logger

    # ------------------------------------------------------------------ #
    # 只读状态
    # ------------------------------------------------------------------ #
    @property
    def tile_size(self) -> int:
        """预期边长（像素）。"""
        return self._tile_size

    @property
    def strict_tile_size(self) -> bool:
        """尺寸与预期不符时是否直接报错。"""
        return self._strict_tile_size

    @property
    def output_format(self) -> str:
        """输出图片格式，固定 ``PNG``。"""
        return COMPOSER_OUTPUT_FORMAT

    # ------------------------------------------------------------------ #
    # 合成
    # ------------------------------------------------------------------ #
    def compose(self, base_data: bytes, label_data: bytes) -> bytes:
        """把底图与注记合成为一张 PNG。

        Args:
            base_data: 底图瓦片字节（PNG 或 JPEG）。
            label_data: 注记瓦片字节（PNG 或 JPEG，通常是带透明通道的 PNG）。

        Returns:
            合成后的 PNG 字节。以 ``89 50 4E 47`` 开头。

        Raises:
            TileComposeError: 数据为空 / 无法解码 / 两张图尺寸不一致 /
                尺寸与预期严重不符且开启了 ``strict_tile_size`` / 编码失败。
        """
        base = self._decode("底图", base_data)
        try:
            label = self._decode("注记", label_data)
            try:
                self._check_sizes(base, label)
                # 唯一的合成动作：按 alpha 混合，透明处保留底图
                merged = Image.alpha_composite(base, label)
            finally:
                label.close()

            try:
                return self._encode(merged)
            finally:
                merged.close()
        finally:
            base.close()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _decode(self, role: str, data: bytes) -> Image.Image:
        """解码成 RGBA 图像。

        Args:
            role: ``"底图"`` / ``"注记"``，仅用于错误信息。
            data: 图片字节。

        Returns:
            已 ``load()``（真正解码完成）的 RGBA 图像。

        Raises:
            TileComposeError: 类型不对 / 为空 / 不是图片 / 解码中途失败。
        """
        if isinstance(data, (bytearray, memoryview)):
            payload = bytes(data)
        elif isinstance(data, bytes):
            payload = data
        else:
            raise TileComposeError(
                f"{role}瓦片必须是 bytes，实际为 {type(data).__name__}"
            )
        if not payload:
            raise TileComposeError(f"{role}瓦片数据为空，无法合成")

        try:
            # BytesIO 只是给 Pillow 一个只读的视图；输入 bytes 不会被改写，
            # 全程也不会产生任何磁盘文件。
            image = Image.open(BytesIO(payload))
            # 关键：Image.open 是惰性的，不 load() 的话"坏图"会在很后面的
            # 地方才炸。这里当场解码，异常归属就清楚了。
            image.load()
        except UnidentifiedImageError as exc:
            raise TileComposeError(
                f"{role}瓦片不是可识别的图片（{len(payload)} 字节）"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - Pillow 的异常类型很杂
            raise TileComposeError(
                f"{role}瓦片解码失败（{len(payload)} 字节）：{exc}"
            ) from exc

        try:
            return image.convert("RGBA")
        except Exception as exc:  # noqa: BLE001
            image.close()
            raise TileComposeError(f"{role}瓦片无法转换为 RGBA：{exc}") from exc

    def _check_sizes(self, base: Image.Image, label: Image.Image) -> None:
        """尺寸校验：两者必须一致；与预期不符时告警或报错。

        Raises:
            TileComposeError: 两张图尺寸不一致，或严格模式下与预期不符。
        """
        if base.size != label.size:
            raise TileComposeError(
                "底图与注记瓦片尺寸不一致："
                f"底图 {base.width}×{base.height}，注记 {label.width}×{label.height}"
                "（它们必须是同一张瓦片的两个图层）"
            )

        expected = self._tile_size
        tolerance = self._size_tolerance
        for role, image in (("底图", base), ("注记", label)):
            deviated = (
                abs(image.width - expected) > tolerance
                or abs(image.height - expected) > tolerance
            )
            if not deviated:
                continue
            message = (
                f"{role}瓦片尺寸 {image.width}×{image.height} "
                f"与预期 {expected}×{expected} 不符"
            )
            if self._strict_tile_size:
                raise TileComposeError(message)
            # 不阻断：尺寸异常但两层一致的瓦片仍然能合出正确结果，
            # 一次服务端抖动不该让几万张瓦片的任务全盘失败。
            self._logger.warning("%s（仍按实际尺寸合成）", message)

    def _encode(self, image: Image.Image) -> bytes:
        """把合成结果编码成 PNG 字节（纯内存）。

        Raises:
            TileComposeError: 编码失败，或产物不是 PNG。
        """
        output = BytesIO()
        try:
            image.save(
                output,
                format=COMPOSER_OUTPUT_FORMAT,
                optimize=COMPOSER_PNG_OPTIMIZE,
            )
            data = output.getvalue()
        except Exception as exc:  # noqa: BLE001
            raise TileComposeError(f"合成结果编码为 PNG 失败：{exc}") from exc
        finally:
            output.close()

        # 自检：产物必须是 PNG。合成地图的 metadata 里写的是 format=png，
        # 一旦这里悄悄产出别的格式，osmdroid 侧会静默显示空白。
        if not data.startswith(PNG_SIGNATURE):
            raise TileComposeError("合成结果的图片头不是 PNG，已放弃写入")
        return data
