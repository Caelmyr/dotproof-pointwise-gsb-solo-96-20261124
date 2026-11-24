"""Find one free circle for a repair press head beside a missed dot.

The head and every old impression of the same mold reserve the configured
sampling radius: a candidate centre needs two sampling radii of clearance from
each bump found in the source photo, including impressions that sit off the
nominal grid.  Bumps are read from the photograph, not from the pattern table,
because the mold can strike a few positions out of place.
"""

from math import ceil, hypot

import cv2
import numpy as np
from scipy.ndimage import center_of_mass, label, maximum_filter
from scipy.spatial import cKDTree

from . import grid


def _basis(layout):
    vector = np.subtract(layout["axis"], layout["origin"])
    distance = hypot(*vector)
    if distance <= 0:
        raise ValueError("基线长度为零")
    return vector / distance, distance


def _field(image, layout):
    """Return the reading maps used to find impressions and free space."""
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    basis, distance = _basis(layout)
    px_per_mm = distance / layout["axis_mm"]
    # Mirror the sampling disk that grid.analyze uses, so the head reserves
    # exactly the configured radius even on very small photographs.
    radius = float(ceil(max(2.0, min(42.0, px_per_mm * layout["radius"]))))
    offsets = grid.opposition(radius)
    dx, dy = offsets
    lx, ly = basis
    xx = np.rint(lx * dx - ly * dy).astype(int)
    yy = np.rint(ly * dx + lx * dy).astype(int)
    pad_x = int(max(-xx.min(), xx.max(), 0))
    pad_y = int(max(-yy.min(), yy.max(), 0))
    kernel = np.zeros((2 * pad_y + 1, 2 * pad_x + 1), dtype=np.float32)
    left = dx < 0
    right = dx > 0
    np.add.at(kernel, (pad_y + yy[left], pad_x + xx[left]), 1.0 / left.sum())
    np.add.at(kernel, (pad_y + yy[right], pad_x + xx[right]), -1.0 / right.sum())
    # The pad only keeps filtering cheap; it never changes a value inside the
    # picture, where the whole sampling disk is available.
    response = cv2.filter2D(gray, -1, kernel, borderType=cv2.BORDER_REPLICATE)
    height, width = gray.shape
    valid = np.zeros((height, width), dtype=bool)
    x0, x1 = max(0, -int(xx.min())), width - max(0, int(xx.max()))
    y0, y1 = max(0, -int(yy.min())), height - max(0, int(yy.max()))
    if x1 > x0 and y1 > y0:
        valid[y0:y1, x0:x1] = True
    # A stain or ink mark darkens the paper as a whole; a bump only swaps light
    # between its left and right shoulders, so its mean brightness barely moves.
    small = cv2.GaussianBlur(gray, (0, 0), max(1.0, radius * 0.8))
    large = cv2.GaussianBlur(gray, (0, 0), max(2.0, radius * 2.2))
    shift = small - large
    wide = cv2.GaussianBlur(gray, (0, 0), max(4.0, radius * 6.0))
    darken = small - wide
    return {"response": response, "shift": shift, "darken": darken, "valid": valid,
            "radius": radius, "px_per_mm": px_per_mm, "basis": basis}


def bumps(image, layout):
    """List the centres of every impression visible in the source photo."""
    field = _field(image, layout)
    response, valid, radius = field["response"], field["valid"], field["radius"]
    height, width = response.shape
    floor = 1.0
    if valid.any():
        values = response[valid]
        noise = float(np.median(np.abs(values - np.median(values))) * 1.4826)
        floor = max(layout["threshold"] * 0.35, noise * 6.0, 1.0)
    # Ink and smudges also break the light balance.  A real bump leaves the
    # local mean close to the paper, while ink is decidedly darker.
    high = maximum_filter(response, size=max(3, int(round(radius)) | 1), mode="nearest")
    rebalance = np.abs(field["shift"]) > np.maximum(0.9, 0.12 * np.abs(response))
    inked = field["darken"] < -np.maximum(2.0, 0.12 * np.maximum(high, 0))
    peaks = response >= high - 1e-6
    peaks &= valid & (response >= floor) & ~rebalance & ~inked
    # An impression can also be cut off by the photograph's edge.  The
    # receptive field there is incomplete, so its response cannot be scored,
    # but it must still reserve space for the head.  Scan that border strip
    # against a wide local maximum instead of the sampling mask.
    strip = ~valid
    if strip.any():
        wide = max(3, 2 * int(round(radius)) + 1)
        crest = maximum_filter(response, size=wide, mode="nearest")
        edge = strip & (response >= crest - 1e-6) & (response >= floor) & ~rebalance
        edge &= ~(field["darken"] < -np.maximum(2.0, 0.12 * np.maximum(crest, 0)))
        peaks |= edge
    labels, count = label(peaks)
    centres = [(float(x), float(y))
               for y, x in center_of_mass(np.maximum(response, 0.0), labels, range(1, count + 1))]
    centres.sort(key=lambda item: (item[1], item[0]))
    # Two nearly touching halves of one bump can survive as separate peaks;
    # one impression only needs one reserved circle.
    merged = []
    for centre in centres:
        if not any(hypot(centre[0] - kept[0], centre[1] - kept[1]) < max(3.0, radius)
                   for kept in merged):
            merged.append(centre)
    return _snap(merged, field), field


def _snap(centres, field, reach=6):
    """Refine each detected centre onto the nearest impression peak.

    ``center_of_mass`` of the thresholded peak can drift toward a neighbour
    when two impressions are close, which would make the reserved circle look
    roomier than it is.  A short local search on the signed response pulls the
    centre back onto the bump itself.
    """
    response = field["response"]
    height, width = response.shape
    radius = max(1.0, field["radius"])
    step = max(1, int(round(radius * 0.2)))
    refined = []
    for x, y in centres:
        cx, cy = int(round(x)), int(round(y))
        x0, x1 = max(0, cx - reach), min(width, cx + reach + 1)
        y0, y1 = max(0, cy - reach), min(height, cy + reach + 1)
        if x1 <= x0 or y1 <= y0:
            refined.append((float(x), float(y)))
            continue
        best = (response[cy, cx], cx, cy)
        for py in range(y0, y1, step):
            for px in range(x0, x1, step):
                if response[py, px] > best[0]:
                    best = (response[py, px], px, py)
        refined.append((float(best[1]), float(best[2])))
    return refined


def _fits_in_frame(center, field, shape):
    height, width = shape
    x, y = center
    radius = field["radius"]
    if x - radius < -1e-9 or y - radius < -1e-9:
        return False
    if x + radius > width - 1 + 1e-9 or y + radius > height - 1 + 1e-9:
        return False
    return bool(field["valid"][int(round(y)), int(round(x))])


def _clearance(centres, x, y):
    if not centres:
        return float("inf")
    return min(hypot(x - cx, y - cy) for cx, cy in centres)


def find(image, layout, mold, reserved=()):
    """Return one centre for the repair head, or the reason there is no room.

    ``reserved`` holds the centres of heads already adopted on other dots;
    they reserve the same radius as an old impression.
    """
    centres, field = bumps(image, layout)
    centres = centres + [(float(x), float(y)) for x, y in reserved]
    radius = field["radius"]
    required = 2 * radius
    limit = layout["dot_pitch"] * field["px_per_mm"] / 2
    mold = (float(mold[0]), float(mold[1]))
    height, width = field["response"].shape
    report = {"mold": [round(mold[0], 3), round(mold[1], 3)],
              "radius_px": round(radius, 3), "radius_mm": layout["radius"],
              "required_px": round(required, 3), "limit_px": round(limit, 3),
              "bumps": len(centres)}

    def offered(x, y, clearance):
        return {**report, "available": True, "center": [round(x, 3), round(y, 3)],
                "offset_px": round(hypot(x - mold[0], y - mold[1]), 3),
                "clearance_px": round(clearance, 3), "state": "planned"}

    # The nominal mold centre is the cleanest place to land the head, so take it
    # whenever it is both inside the photograph and clear of every old bump.
    if _fits_in_frame(mold, field, (height, width)):
        clearance = _clearance(centres, *mold)
        if clearance + 1e-9 >= required:
            return offered(*mold, clearance)

    x_lo = max(int(ceil(radius)), int(np.floor(mold[0] - limit)))
    x_hi = min(int(np.floor(width - 1 - radius)), int(np.ceil(mold[0] + limit)))
    y_lo = max(int(ceil(radius)), int(np.floor(mold[1] - limit)))
    y_hi = min(int(np.floor(height - 1 - radius)), int(np.ceil(mold[1] + limit)))
    if x_hi < x_lo or y_hi < y_lo:
        return {**report, "available": False,
                "reason": "字模附近没有能完整放进原片的补压头位置。"}
    xs = np.arange(x_lo, x_hi + 1)
    ys = np.arange(y_lo, y_hi + 1)
    grid_x, grid_y = np.meshgrid(xs, ys)
    inside = np.hypot(grid_x - mold[0], grid_y - mold[1]) <= limit + 1e-9
    valid = field["valid"]
    inside &= valid[np.clip(grid_y, 0, height - 1), np.clip(grid_x, 0, width - 1)]
    candidates = np.column_stack((grid_x[inside], grid_y[inside])).astype(np.float64)
    if not len(candidates):
        return {**report, "available": False,
                "reason": "字模附近没有能完整放进原片的补压头位置。"}
    if centres:
        clearance = cKDTree(np.asarray(centres)).query(candidates, k=1)[0]
    else:
        clearance = np.full(len(candidates), np.inf)
    room = clearance + 1e-6 >= required
    if not room.any():
        best = float(clearance.max()) if len(clearance) else 0.0
        return {**report, "available": False, "best_clearance_px": round(best, 3),
                "reason": "字模半格内没有能避开附近旧点的补压头位置。"}
    candidates, clearance = candidates[room], clearance[room]
    offset = np.hypot(candidates[:, 0] - mold[0], candidates[:, 1] - mold[1])
    # Stay as close to the mold as possible; among equally close spots prefer
    # the one with the most room, then a stable pixel order.
    offset = np.round(offset, 6)
    near = offset <= offset.min() + 1e-9
    candidates, clearance = candidates[near], clearance[near]
    order = np.lexsort((candidates[:, 0], candidates[:, 1], -np.round(clearance, 6)))
    x, y = candidates[order[0]]
    return offered(float(x), float(y), float(clearance[order[0]]))
