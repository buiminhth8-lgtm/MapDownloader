"""共享的 PNG/JPEG 完整校验：检查容器，并实际解码像素。"""
from __future__ import annotations

from io import BytesIO

from app.providers.tianditu import detect_image_format


def require_image_decoder():
    """修复模式必须有解码器，不能把缺依赖当作全部瓦片损坏。"""
    from PIL import Image
    return Image


def verify_tile_image(data: bytes) -> tuple[str, int, int]:
    image_module = require_image_decoder()
    if detect_image_format(data) is None:
        raise ValueError("magic bytes 既不是 PNG 也不是 JPEG")
    with image_module.open(BytesIO(data)) as image:
        image_format = image.format
        width, height = image.size
        if image_format not in ("PNG", "JPEG"):
            raise ValueError("瓦片不是 PNG/JPEG 图片")
        image.verify()
    # verify() 不读取 JPEG 像素，也无法识别 CRC 正确但压缩流损坏的 PNG。
    with image_module.open(BytesIO(data)) as image:
        image.load()
    return image_format, width, height


def is_decodable_tile(data: bytes | None) -> bool:
    if not data:
        return False
    try:
        verify_tile_image(data)
    except ImportError:
        raise
    except Exception:
        return False
    return True
