"""Geometry and photographic readings for registered six-position dot cells."""

from math import hypot
import re

import numpy as np
from PIL import Image, ImageDraw

from image_lab.validation import Invalid

CELL = re.compile(r"-|[1-6]{1,6}")
ACTIONS = {"confirmed", "check", "waived"}


def pattern(text):
    if not isinstance(text, str) or len(text) > 2400:
        raise Invalid("字模每格用 1 至 6 的点号，空格位写 -。")
    lines = [line.split() for line in text.splitlines() if line.strip()]
    if not 1 <= len(lines) <= 12 or any(not 1 <= len(line) <= 24 for line in lines):
        raise Invalid("一张样片最多 12 行，每行填 1 至 24 格。")
    for line in lines:
        for cell in line:
            if CELL.fullmatch(cell) is None or len(cell) != len(set(cell)):
                raise Invalid("每格使用不重复的 1 至 6 点号，空格位写 -。")
    return lines


def points(layout):
    origin, axis = np.asarray(layout["origin"]), np.asarray(layout["axis"])
    vector = (axis - origin) / layout["axis_mm"]
    down = np.array([-vector[1], vector[0]])
    for row, line in enumerate(pattern(layout["pattern"])):
        for cell, spec in enumerate(line):
            start = origin + vector * (cell * layout["cell_step"]) + down * (row * layout["line_step"])
            for number in range(1, 7):
                column, offset = divmod(number - 1, 3)
                xy = start + (column * vector + offset * down) * layout["dot_pitch"]
                yield row, cell, number, xy, str(number) in spec


def opposition(radius):
    radius = int(np.ceil(radius))
    yy, xx = np.indices((2 * radius + 1, 2 * radius + 1))
    xx = xx - radius
    yy = yy - radius
    disk = np.hypot(xx, yy) <= radius
    return (xx[disk], yy[disk])


def response(field, xy, basis, offsets):
    height, width = field.shape
    dx, dy = offsets
    lx, ly = basis
    xx = np.rint(xy[0] + lx * dx - ly * dy).astype(int)
    yy = np.rint(xy[1] + ly * dx + lx * dy).astype(int)
    if np.any((xx < 0) | (xx >= width) | (yy < 0) | (yy >= height)):
        return None
    left = field[yy[dx < 0], xx[dx < 0]]
    right = field[yy[dx > 0], xx[dx > 0]]
    return round(float(left.mean() - right.mean()), 2)


def analyze(image, layout):
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    vector = np.subtract(layout["axis"], layout["origin"])
    distance = hypot(*vector)
    px_per_mm = distance / layout["axis_mm"]
    radius_px = px_per_mm * layout["radius"]
    offsets = opposition(max(2, min(42, radius_px)))
    dots = []
    totals = dict(match=0, missing=0, extra=0, unseen=0)
    for row, cell, number, xy, expected in points(layout):
        contrast = response(gray, xy, vector / distance, offsets)
        found = None if contrast is None else contrast >= layout["threshold"]
        status = "unseen" if found is None else (
            "match" if found == expected else ("missing" if expected else "extra")
        )
        totals[status] += 1
        dots.append({"id": f"{row + 1}.{cell + 1}.{number}", "row": row + 1, "cell": cell + 1,
                     "dot": number, "xy": [round(float(v), 3) for v in xy], "contrast": contrast,
                     "expected": expected, "found": found, "status": status})
    return {"dots": dots, "totals": totals, "px_per_mm": round(px_per_mm, 6)}


def overlay(image, report, reviews, loose=()):
    board = image.convert("RGBA")
    layer = Image.new("RGBA", image.size)
    draw = ImageDraw.Draw(layer)
    colors = {"match": (224, 237, 220, 235), "missing": (238, 99, 92, 255),
              "extra": (246, 187, 79, 255), "unseen": (120, 155, 169, 245)}
    for dot in report["dots"]:
        x, y = dot["xy"]
        color = colors[dot["status"]]
        radius = 8 if dot["expected"] or dot["found"] else 3
        draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                     outline=color, width=2 if radius == 8 else 1)
        if dot["id"] in reviews:
            draw.rectangle((x + 8, y - 12, x + 13, y - 7), fill=(62, 191, 178, 255))
    for item in loose:
        xy = item.get("sample")
        if not isinstance(xy, list) or len(xy) != 2:
            continue
        x, y = xy
        draw.line((x - 7, y - 7, x + 7, y + 7), fill=(246, 187, 79, 255), width=2)
        draw.line((x + 7, y - 7, x - 7, y + 7), fill=(246, 187, 79, 255), width=2)
    board.alpha_composite(layer)
    return board.convert("RGB")
