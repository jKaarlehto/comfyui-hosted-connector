"""Normalize encoded or raw Notch image uploads to a portable PNG."""

import io

import numpy as np
from PIL import Image, UnidentifiedImageError

from .client import MAX_BYTES


def png(content, metadata=None):
    if len(content) > MAX_BYTES:
        raise ValueError("Input exceeds the bridge's 100 MiB limit")
    if metadata:
        width, height = metadata.get("width"), metadata.get("height")
        if type(width) is not int or type(height) is not int or min(width, height) < 1 or width * height > 16_777_216:
            raise ValueError("Raw image dimensions must be positive and at most 16 megapixels")
        kind = str(metadata.get("format", "")).lower()
        formats = {
            "rgba": (4, np.uint8),
            "bgra": (4, np.uint8),
            "rgb": (3, np.uint8),
            "bgr": (3, np.uint8),
            "float32_rgba": (4, np.dtype("<f4")),
            "float32_bgra": (4, np.dtype("<f4")),
            "float32_rgb": (3, np.dtype("<f4")),
            "float32_bgr": (3, np.dtype("<f4")),
        }
        if kind not in formats:
            raise ValueError("Unsupported raw image format; use rgb/rgba/bgr/bgra or their float32_ variants")
        channels, dtype = formats[kind]
        row_bytes = width * channels * np.dtype(dtype).itemsize
        stride = metadata.get("stride") or row_bytes
        if type(stride) is not int or stride < row_bytes or len(content) != height * stride:
            raise ValueError("Raw image length or stride does not match its metadata")
        rows = np.frombuffer(content, dtype=np.uint8).reshape(height, stride)[:, :row_bytes].copy()
        pixels = rows.view(dtype).reshape(height, width, channels)
        if kind.startswith("float32_"):
            pixels = (np.clip(np.nan_to_num(pixels, nan=0, posinf=1, neginf=0), 0, 1) * 255).round().astype(np.uint8)
        if kind.endswith("bgra") or kind.endswith("bgr"):
            pixels = pixels[:, :, [2, 1, 0, 3] if channels == 4 else [2, 1, 0]]
        image = Image.fromarray(pixels)
    else:
        try:
            image = Image.open(io.BytesIO(content))
            if image.width * image.height > 16_777_216:
                raise ValueError("Image exceeds the bridge's 16 megapixel limit")
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError("Animated images are not supported by the image MVP")
            image.load()
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError("Supply an encoded image or supported raw-buffer metadata") from exc
    result = io.BytesIO()
    image.save(result, format="PNG")
    return result.getvalue(), image.width, image.height
