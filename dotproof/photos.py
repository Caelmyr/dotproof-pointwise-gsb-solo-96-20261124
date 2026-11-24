"""Decode inspection photographs to a stable display coordinate system."""

from io import BytesIO

import numpy as np
from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from image_lab.validation import Invalid

MAX_UPLOAD = 12 * 1024 * 1024
MAX_PIXELS = 3_200_000


def decode(data):
    if len(data) > MAX_UPLOAD:
        raise Invalid("单张原片至多 12 MiB。")
    try:
        with Image.open(BytesIO(data)) as source:
            width, height = source.size
            if source.format not in {"PNG", "JPEG", "TIFF"} or getattr(source, "n_frames", 1) != 1:
                raise Invalid("请选择单帧 PNG、JPEG 或 TIFF。")
            if min(width, height) < 20 or width * height > MAX_PIXELS:
                raise Invalid("原片短边至少 20 像素，总量至多 320 万像素。")
            source = ImageOps.exif_transpose(source)
            if "A" in source.getbands() or "transparency" in source.info:
                flattened = Image.new("RGBA", source.size, "white")
                flattened.alpha_composite(source.convert("RGBA"))
                return flattened.convert("RGB")
            if source.mode.startswith("I;16"):
                raster = np.asarray(source, dtype=np.uint16)
                return Image.fromarray((raster // 257).astype(np.uint8)).convert("RGB")
            return source.convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise Invalid("读不到这张原片。") from error


def png(image):
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()



def demo():
    """Draw a synthetic side-lit embossing proof with faint and missing impressions."""
    width, height = 840, 560
    yy, xx = np.indices((height, width), dtype=np.float32)
    rng = np.random.default_rng(497)
    base = 202 + 9 * np.cos(xx / 230) + 5 * np.sin(yy / 130) + rng.normal(0, 1.5, xx.shape)
    for row, line in enumerate(DEMO_PATTERN.splitlines()):
        for cell, command in enumerate(line.split()):
            for dot in command:
                if not dot.isdigit() or (row, cell, dot) == (1, 3, "2"):
                    continue
                column, offset = divmod(int(dot) - 1, 3)
                x = 140 + cell * 105 + column * 30
                y = 144 + row * 131 + offset * 30
                x0, y0 = x - 12, y - 12
                dx, dy = xx[y0:y + 13, x0:x + 13] - x, yy[y0:y + 13, x0:x + 13] - y
                amplitude = 11 if (row, cell, dot) == (0, 3, "6") else 31
                base[y0:y + 13, x0:x + 13] += amplitude * (
                    np.exp(-((dx + 3.5)**2 + dy**2) / 18.0) - np.exp(-((dx - 3.5)**2 + dy**2) / 18.0)
                )
    base[179:190, 355:366] -= 13
    field = np.clip(base, 0, 255).astype(np.uint8)
    image = Image.fromarray(np.stack((field, np.clip(field.astype(int) - 9, 0, 255),
                                     np.clip(field.astype(int) - 25, 0, 255)), axis=2).astype(np.uint8))
    ink = ImageDraw.Draw(image)
    ink.line((140, 494, 340, 494), width=2, fill=(127, 106, 77))
    for x in range(140, 341, 25):
        ink.line((x, 490, x, 497), fill=(127, 106, 77))
    ink.text((222, 504), "20 mm", fill=(80, 65, 50))
    return image


DEMO_PATTERN = "145 12 - 156 146 1345\n123 14 124 123 15 1245\n15 145 12 134 135 246"
DEMO_LAYOUT = {
    "origin": [140, 144], "axis": [340, 144], "axis_mm": 20,
    "dot_pitch": 3, "cell_step": 10.5, "line_step": 13.1,
    "radius": 1.0, "threshold": 9, "pattern": DEMO_PATTERN,
}
