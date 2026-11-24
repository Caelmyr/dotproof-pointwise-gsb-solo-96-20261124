"""Persist photo inspections and immutable reviewer snapshots."""

from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite
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


def measured(image, layout):
    if layout["origin"] is None or layout["axis"] is None:
        raise Invalid("先在原片标出起点和方向，再读取点阵。")
    return grid.analyze(image, layout)


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
                "layout": photos.DEMO_LAYOUT if synthetic else DEFAULT_LAYOUT, "reviews": {},
            }
            write_json(folder / "proof.json", record)
            return record
        except BaseException:
            shutil.rmtree(folder)
            raise

    def update(self, plate, revision, updates, reset=False):
        with self.lock:
            record = self.record(plate)
            check_revision(record, revision)
            layout = validate_layout(record, updates)
            related = ("origin", "axis", "axis_mm", "dot_pitch", "cell_step", "line_step", "pattern")
            geometry_changed = any(layout[field] != record["layout"][field] for field in related)
            if geometry_changed and record["reviews"] and reset is not True:
                raise Conflict("已有点位复核。请先封存，再明确清空现行复核并应用新版位。")
            if geometry_changed:
                record["reviews"] = {}
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
                "layout": record["layout"], "reviews": record["reviews"], "results": results,
            }
            folder = self.folder(plate) / "reports"
            folder.mkdir(exist_ok=True)
            write_json(folder / (saved["id"] + ".json"), saved)
            return saved
