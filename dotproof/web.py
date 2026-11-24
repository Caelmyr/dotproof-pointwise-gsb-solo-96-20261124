"""Serve the workbench, the measured proof and its photo-linked archives."""

import csv
from io import BytesIO, StringIO
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file

from image_lab import create_app as create_preparation
from image_lab.validation import Invalid

from . import grid, photos, state


def body():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise Invalid("请发送 JSON 校样参数。")
    return payload


def png_file(data, name):
    return send_file(data, mimetype="image/png", download_name=name,
                     as_attachment="download" in request.args, max_age=0)


def create_app(root):
    app = create_preparation(root / "prepare")
    library = state.Library(root)
    desk = Blueprint("dotproof", __name__, url_prefix="/dotproof")

    @app.get("/")
    def home():
        return render_template("dotproof.html")

    @desk.get("/health")
    def health():
        return jsonify(status="ready", service="dotproof")

    @desk.get("/plates")
    def plates():
        return jsonify(library.list())

    @desk.post("/demo")
    def demo():
        return jsonify(library.create(photos.demo(), "合成压样 · 三行点阵", synthetic=True)), 201

    @desk.post("/plates")
    def upload():
        file = request.files.get("image")
        if file is None or not file.filename:
            raise Invalid("选择一张压样原片。")
        image = photos.decode(file.stream.read(photos.MAX_UPLOAD + 1))
        return jsonify(library.create(image, request.form.get("title") or Path(file.filename).stem)), 201

    @desk.post("/plates/prepared")
    def prepared():
        payload = body()
        project, result = payload.get("project"), payload.get("result")
        if not isinstance(project, str) or not isinstance(result, str):
            raise Invalid("请选择可用的照片准备结果。")
        preparation = app.extensions["results"]
        item = preparation.metadata(project, result)
        if item["stale"]:
            raise Invalid("请先重新计算照片准备结果。")
        source = preparation.image_path(project, "image", result)
        with source.open("rb") as stream:
            image = photos.decode(stream.read(photos.MAX_UPLOAD + 1))
        return jsonify(library.create(image, "照片准备 · 压样", origin={"project": project, "result": result})), 201

    @desk.get("/plates/<plate>")
    def open_plate(plate):
        return jsonify(library.record(plate))

    @desk.get("/plates/<plate>/source.png")
    def source(plate):
        return png_file(library.source(plate), "proof-source.png")

    @desk.post("/plates/<plate>/inspect")
    def inspect(plate):
        record = library.record(plate)
        layout = state.validate_layout(record, body().get("layout", {}))
        return jsonify(state.measured(library.image(plate), layout))

    @desk.post("/plates/<plate>/preview")
    def preview(plate):
        record = library.record(plate)
        layout = state.validate_layout(record, body().get("layout", {}))
        return jsonify(state.preview(library.image(plate), record, layout))

    @desk.put("/plates/<plate>/layout")
    def save_layout(plate):
        payload = body()
        return jsonify(library.update(plate, payload.get("revision"), payload.get("layout"),
                                      payload.get("carry_reviews")))

    @desk.put("/plates/<plate>/review")
    def annotate(plate):
        value = body()
        return jsonify(library.annotate(plate, value.get("revision"), value.get("dot_id"), value.get("action"),
                                        value.get("note"), value.get("sample")))

    @desk.get("/plates/<plate>/reports")
    def reports(plate):
        return jsonify(library.reports(plate))

    @desk.post("/plates/<plate>/reports")
    def archive(plate):
        payload = body()
        return jsonify(library.archive(plate, payload.get("revision"), payload.get("title"))), 201

    @desk.get("/plates/<plate>/reports/<report_id>")
    def report(plate, report_id):
        return jsonify(library.report(plate, report_id))

    @desk.get("/plates/<plate>/reports/<report_id>/overlay.png")
    def overlay(plate, report_id):
        report = library.report(plate, report_id)
        render = grid.overlay(library.image(plate), report["results"], report["reviews"],
                              report.get("loose_reviews", []))
        return png_file(BytesIO(photos.png(render)), "proof-overlay.png")

    @desk.get("/plates/<plate>/reports/<report_id>/dots.csv")
    def dots_csv(plate, report_id):
        report = library.report(plate, report_id)
        output = StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["slot", "row", "cell", "dot", "x_px", "y_px", "contrast", "expected", "status",
                         "review", "review_x_px", "review_y_px", "note"])
        for dot in report["results"]["dots"]:
            review = report["reviews"].get(dot["id"], {})
            note = review.get("note", "")
            if note.lstrip().startswith(("=", "+", "-", "@")):
                note = "'" + note
            writer.writerow([dot["id"], dot["row"], dot["cell"], dot["dot"], *dot["xy"], dot["contrast"],
                             dot["expected"], dot["status"], review.get("action", ""),
                             *review.get("sample", ["", ""]), note])
        for item in report.get("loose_reviews", []):
            note = item.get("note", "")
            if note.lstrip().startswith(("=", "+", "-", "@")):
                note = "'" + note
            writer.writerow([item["id"], "", "", "", "", "", "", "", "unplaced",
                             item.get("action", ""), *item.get("sample", ["", ""]),
                             note or "原格点 " + str(item.get("from", ""))])
        return send_file(BytesIO(output.getvalue().encode("utf-8-sig")), mimetype="text/csv",
                         download_name="proof-dots.csv", as_attachment=True, max_age=0)

    app.register_blueprint(desk)
    return app
