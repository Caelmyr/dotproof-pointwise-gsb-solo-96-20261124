"""Persist photo inspections and immutable reviewer snapshots."""

from datetime import datetime, timezone
from hashlib import sha256
from math import hypot, isfinite
import json
import re
import shutil
from threading import RLock
from uuid import uuid4

from PIL import Image
from werkzeug.exceptions import NotFound

from image_lab.validation import Conflict, Invalid

from . import grid, photos

KEY = re.compile(r"[0-9a-f]{16}")
DEFAULT_LAYOUT = {
    "origin": None, "axis": None, "axis_mm": 20, "dot_pitch": 2.5,
    "cell_step": 6, "line_step": 10, "radius": .8, "threshold": 9, "pattern": "145 12 - 15",
}


def title(value, fallback):
    if isinstance(value, str) and value.strip():
        return value.strip()[:100]
    return fallback


def numeric(value, lower, upper, label):
    if type(value) not in (int, float) or not isfinite(value) or not lower <= value <= upper:
        raise Invalid(f"{label}应在 {lower} 至 {upper} 之间。")
    return float(value)


def coordinate(value, record):
    if not isinstance(value, list) or len(value) != 2:
        raise Invalid("请选择原片里的一个坐标。")
    return [numeric(v, 0, extent - 1, "端点坐标")
            for v, extent in zip(value, (record["width"], record["height"]))]


def validate_layout(record, updates):
    if not isinstance(updates, dict):
        raise Invalid("请填写点阵版位。")
    form = {**record["layout"], **updates}
    layout = {name: coordinate(form[name], record) if form[name] is not None else None
              for name in ("origin", "axis")}
    if layout["origin"] is not None and layout["axis"] == layout["origin"]:
        raise Invalid("请选择两个不同的标定点。")
    limits = {
        "axis_mm": (.1, 1000, "基线距离"), "dot_pitch": (.5, 20, "点距"),
        "cell_step": (1, 50, "格距"), "line_step": (1, 100, "行距"),
        "radius": (.15, 8, "取样半径"), "threshold": (.1, 255, "明暗差阈值"),
    }
    for name, (lo, hi, label) in limits.items():
        layout[name] = numeric(form[name], lo, hi, label)
    if layout["cell_step"] <= layout["dot_pitch"] or layout["line_step"] <= 2 * layout["dot_pitch"]:
        raise Invalid("格距应大于一点距，行距应大于两点距。")
    if layout["radius"] >= layout["dot_pitch"] / 2:
        raise Invalid("取样半径必须小于半个点距。")
    layout["pattern"] = "\n".join(" ".join(line) for line in grid.pattern(form["pattern"]))
    return layout


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def key(value):
    if not isinstance(value, str) or KEY.fullmatch(value) is None:
        raise NotFound("找不到此照片或留样。")
    return value


def write_file(path, data):
    scratch = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        scratch.write_bytes(data)
        scratch.replace(path)
    finally:
        scratch.unlink(missing_ok=True)


def write_json(path, value):
    write_file(path, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def read_json(path):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        raise NotFound("找不到此照片或留样。") from None


def check_revision(record, revision):
    if type(revision) is not int or revision != record["revision"]:
        raise Conflict("这张压样已被修改。请重开后再操作。")


def sample_shift(xy, sample, radius_px):
    """Distance from reviewer evidence to a grid point, or None when out of reach."""
    if not isinstance(sample, list) or len(sample) != 2:
        return None
    try:
        x, y = float(sample[0]), float(sample[1])
    except (TypeError, ValueError):
        return None
    if not (isfinite(x) and isfinite(y)):
        return None
    shift = hypot(x - xy[0], y - xy[1])
    return shift if shift <= radius_px else None


def measured(image, layout):
    if layout["origin"] is None or layout["axis"] is None:
        raise Invalid("先在原片标出起点和方向，再读取点阵。")
    return grid.analyze(image, layout)


def radius_px(layout):
    vector = (layout["axis"][0] - layout["origin"][0], layout["axis"][1] - layout["origin"][1])
    return hypot(*vector) / layout["axis_mm"] * layout["radius"]


def candidates(entries, proof, reach):
    """Pair each stored review with eligible new grid dots it can reach."""
    eligible = [dot for dot in proof["dots"] if dot["found"] is True]
    picked = {}
    for entry in entries:
        sample = entry["review"].get("sample")
        options = []
        for candidate in eligible:
            shift = sample_shift(candidate["xy"], sample, reach)
            if shift is not None:
                options.append((shift, candidate["id"]))
        picked[entry["key"]] = sorted(options)
    return picked


def best_pairs(options, lookup):
    """One-to-one review-to-dot matching: most reviews continued, then closest evidence."""
    names = sorted(options)
    dots = sorted({grid_id for items in options.values() for _, grid_id in items})
    if not names or not dots:
        return {}
    source, sink = 0, len(names) + len(dots) + 1
    review_node = {name: index + 1 for index, name in enumerate(names)}
    dot_node = {grid_id: len(names) + index + 1 for index, grid_id in enumerate(dots)}
    graph = [[] for _ in range(sink + 1)]
    edges = []

    def link(start, end, limit, price):
        graph[start].append(len(edges))
        edges.append([end, limit, price, len(edges) + 1])
        graph[end].append(len(edges))
        edges.append([start, 0, -price, len(edges) - 1])
        return len(edges) - 2

    rank = {"confirmed": 0, "waived": 1, "check": 2}
    moves = {}
    for name in names:
        link(source, review_node[name], 1, 0.0)
        for shift, grid_id in options[name]:
            # Distance decides; a saved conclusion only breaks a near tie.
            price = shift + rank.get(lookup[name].get("action"), 3) * 1e-6
            moves[(name, grid_id)] = link(review_node[name], dot_node[grid_id], 1, price)
    for grid_id in dots:
        link(dot_node[grid_id], sink, 1, 0.0)

    while True:
        # Bellman-Ford relaxes the residual graph; costs stay free of negative cycles.
        distance = [None] * (sink + 1)
        parent = [None] * (sink + 1)
        distance[source] = 0.0
        for _ in range(sink):
            changed = False
            for start in range(sink + 1):
                if distance[start] is None:
                    continue
                for index in graph[start]:
                    end, limit, price, _ = edges[index]
                    if limit <= 0:
                        continue
                    step = distance[start] + price
                    if distance[end] is None or step < distance[end] - 1e-9:
                        distance[end], parent[end] = step, index
                        changed = True
            if not changed:
                break
        if distance[sink] is None:
            break
        step = sink
        while step != source:
            index = parent[step]
            edges[index][1] -= 1
            edges[index ^ 1][1] += 1
            step = edges[index ^ 1][0]

    paired = {}
    for (name, grid_id), index in moves.items():
        if edges[index][1] == 0:
            paired[name] = grid_id
    return paired


def carry(entries, proof, reach):
    """Continue reviews one at a time onto complete samples that show a bump.

    The pairing maximises how many reviews continue, so when two reach the same
    dot and one of them also reaches another, both continue to separate dots.
    A genuine contest resolves to a usable choice: the closer evidence wins,
    with a saved confirmation kept on a near tie.
    """
    options = candidates(entries, proof, reach)
    lookup = {entry["key"]: entry["review"] for entry in entries}
    dot_of = best_pairs(options, lookup)
    shift_of = {(name, grid_id): shift for name, items in options.items() for shift, grid_id in items}
    carried, loose, seen = {}, [], set()
    for entry in sorted(entries, key=lambda item: item["review"].get("created", "")):
        name, review = entry["key"], entry["review"]
        source = review.get("from") or name
        moved = {"action": review.get("action"), "note": review.get("note", ""),
                 "sample": review.get("sample"), "created": review.get("created")}
        if name in dot_of:
            grid_id = dot_of[name]
            carried[grid_id] = {**moved, "from": source,
                                "shift_px": round(shift_of[(name, grid_id)], 3)}
        else:
            # Loose evidence is stored beside the proof, not on a grid dot, so
            # it always needs an id of its own that will not read as a dot id.
            evidence = source if source.startswith("evidence-") else "evidence-" + source
            base, suffix = evidence, 1
            while evidence in seen:
                suffix += 1
                evidence = f"{base}-{suffix}"
            seen.add(evidence)
            loose.append({**moved, "id": evidence, "from": source,
                          "reason": "contested" if options[name] else "no_grid"})
    return {"carried": carried, "loose": loose}


def review_entries(record):
    """Reviews stay keyed by their own evidence id so they can move again later."""
    entries = [{"key": name, "review": review} for name, review in record["reviews"].items()]
    entries += [{"key": item.get("id") or uuid4().hex[:16], "review": item}
                for item in record.get("loose_reviews", [])]
    return entries


def preview(image, record, layout):
    """Read a proposed layout and its continued reviews without saving anything."""
    proof = measured(image, layout)
    return {"proof": proof, **carry(review_entries(record), proof, radius_px(layout))}


class Library:
    def __init__(self, root):
        self.plates = root / "proof-plates"
        self.plates.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()

    def folder(self, plate):
        return self.plates / key(plate)

    def record(self, plate):
        return read_json(self.folder(plate) / "proof.json")

    def source(self, plate):
        self.record(plate)
        return self.folder(plate) / "source.png"

    def image(self, plate):
        with Image.open(self.source(plate)) as opened:
            return opened.convert("RGB")

    def list(self):
        data = [read_json(path) for path in self.plates.glob("*/proof.json")]
        return [{name: row[name] for name in ("id", "title", "synthetic", "created")}
                for row in sorted(data, key=lambda row: row["created"], reverse=True)]

    def create(self, image, name, synthetic=False, origin=None):
        plate = uuid4().hex[:16]
        folder = self.folder(plate)
        folder.mkdir()
        try:
            raw = photos.png(image)
            write_file(folder / "source.png", raw)
            record = {
                "id": plate, "title": title(name, "未命名压样"), "created": now(),
                "synthetic": synthetic, "source_origin": origin, "source_sha256": sha256(raw).hexdigest(),
                "width": image.width, "height": image.height, "revision": 1,
                "layout": photos.DEMO_LAYOUT if synthetic else DEFAULT_LAYOUT,
                "reviews": {}, "loose_reviews": [],
            }
            write_json(folder / "proof.json", record)
            return record
        except BaseException:
            shutil.rmtree(folder)
            raise

    def update(self, plate, revision, updates, carry_reviews=False):
        with self.lock:
            record = self.record(plate)
            check_revision(record, revision)
            layout = validate_layout(record, updates)
            related = ("origin", "axis", "axis_mm", "dot_pitch", "cell_step", "line_step", "pattern")
            geometry_changed = any(layout[field] != record["layout"][field] for field in related)
            current = record["reviews"] or record.get("loose_reviews")
            if geometry_changed and current and carry_reviews is not True:
                raise Conflict("已有点位复核。请先封存，再明确应用新版位并接续复核。")
            if geometry_changed:
                if current:
                    image = self.image(plate)
                    proof = measured(image, layout)
                    moved = carry(review_entries(record), proof, radius_px(layout))
                    record["reviews"] = moved["carried"]
                    # Evidence that did not continue stays findable on the current
                    # proof, apart from grid dots so it is never read as a dot result.
                    record["loose_reviews"] = moved["loose"]
                else:
                    record["reviews"], record["loose_reviews"] = {}, []
            record["layout"] = layout
            record["revision"] += 1
            write_json(self.folder(plate) / "proof.json", record)
            return record

    def annotate(self, plate, revision, dot_id, action, note, sample=None):
        with self.lock:
            record = self.record(plate)
            check_revision(record, revision)
            proof = measured(self.image(plate), record["layout"])
            dot = next((item for item in proof["dots"] if item["id"] == dot_id), None)
            if dot is None or dot["status"] == "unseen":
                raise Invalid("请选择已在原片取样的格点。")
            if action is None:
                record["reviews"].pop(dot_id, None)
            else:
                if not isinstance(action, str) or action not in grid.ACTIONS:
                    raise Invalid("请选择已确认、待复核或允许。")
                sample = coordinate(sample, record) if sample is not None else dot["xy"]
                record["reviews"][dot_id] = {"action": action, "note": title(note, ""),
                                             "sample": sample, "created": now()}
            record["revision"] += 1
            write_json(self.folder(plate) / "proof.json", record)
            return record

    def reports(self, plate):
        self.record(plate)
        entries = [read_json(path) for path in self.folder(plate).glob("reports/*.json")]
        return [{name: row[name] for name in ("id", "title", "created")}
                for row in sorted(entries, key=lambda row: row["created"], reverse=True)]

    def report(self, plate, report):
        return read_json(self.folder(plate) / "reports" / f"{key(report)}.json")

    def archive(self, plate, revision, label):
        with self.lock:
            record = self.record(plate)
            check_revision(record, revision)
            results = measured(self.image(plate), record["layout"])
            saved = {
                "id": uuid4().hex[:16], "title": title(label, "压样复核单"), "created": now(),
                "proof_id": plate, "source_sha256": record["source_sha256"], "revision": record["revision"],
                "layout": record["layout"], "reviews": record["reviews"],
                "loose_reviews": record.get("loose_reviews", []), "results": results,
            }
            folder = self.folder(plate) / "reports"
            folder.mkdir(exist_ok=True)
            write_json(folder / (saved["id"] + ".json"), saved)
            return saved
