"use strict";

const el = id => document.getElementById(id);
const root = "/dotproof";
const names = {match: "与字模一致", missing: "缺点待核", extra: "多点待核", unseen: "未入画"};
const fields = ["axis_mm", "dot_pitch", "cell_step", "line_step", "radius", "threshold", "pattern"];
const colors = {match: "#dfecdc", missing: "#ed655d", extra: "#eabb53", unseen: "#7e9baa"};
const geometry = ["origin", "axis", "axis_mm", "dot_pitch", "cell_step", "line_step", "pattern"];
const format = xy => xy ? xy.map(value => Number(value.toFixed(1))).join(", ") + " px" : "未选择";
const s = {plate: null, bitmap: null, endpoints: {}, result: null, archive: null, mode: "grid",
  pending: null, selected: "", sample: null, preview: false, requestId: 0};

async function api(path, method = "GET", data) {
  const options = {method};
  if (data instanceof FormData) {
    options.body = data;
  } else if (data !== undefined) {
    options.headers = {"Content-Type": "application/json"};
    options.body = JSON.stringify(data);
  }
  const response = await fetch(root + path, options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "操作失败");
  return value;
}

function message(text, error = false) {
  el("message").textContent = text;
  el("message").classList.toggle("error", error);
}

function act(fn) {
  return async event => {
    event?.preventDefault();
    try {
      await fn(event);
    } catch (error) {
      message(error.message, true);
    }
  };
}

function path(suffix = "") {
  return "/plates/" + s.plate.id + suffix;
}

function options(select, placeholder, rows) {
  select.replaceChildren(new Option(placeholder, ""), ...rows.map(row => new Option(row.label, row.value)));
}

async function list() {
  const records = await api("/plates");
  options(el("plates"), "选择压样", records.map(item => ({label: item.title, value: item.id})));
  el("plates").value = s.plate?.id ?? "";
}

async function archives() {
  const plate = s.plate.id;
  const records = await api(path("/reports"));
  if (s.plate.id !== plate) return;
  options(el("reports"), "选择复核单", records.map(item => ({label: item.title, value: item.id})));
  el("reports").value = s.archive?.id ?? "";
}

function currentLayout() {
  return {...s.endpoints,
    ...Object.fromEntries(fields.map(name => [name, name === "pattern" ? el(name).value.trim() : el(name).valueAsNumber]))};
}

function fillLayout() {
  const config = s.plate.layout;
  s.endpoints = {origin: config.origin, axis: config.axis};
  for (const key of fields) el(key).value = config[key];
  updateEndpointLabels();
}

function updateEndpointLabels() {
  for (const key of ["origin", "axis"]) el(key + "-text").textContent = format(s.endpoints[key]);
}

function evidenceMarker(context, xy) {
  context.lineWidth = 2;
  context.strokeStyle = "#58edcd";
  const [x, y] = xy;
  context.beginPath();
  context.moveTo(x - 6, y);
  context.lineTo(x + 6, y);
  context.moveTo(x, y - 6);
  context.lineTo(x, y + 6);
  context.stroke();
}

function draw() {
  const canvas = el("photo");
  if (!s.bitmap) return;
  if (canvas.width !== s.bitmap.naturalWidth || canvas.height !== s.bitmap.naturalHeight) {
    canvas.width = s.bitmap.naturalWidth;
    canvas.height = s.bitmap.naturalHeight;
  }
  const context = canvas.getContext("2d");
  context.drawImage(s.bitmap, 0, 0);
  const reviews = s.archive?.reviews ?? s.plate.reviews;
  if (s.mode === "grid") {
    for (const dot of s.result?.dots ?? []) {
      const [x, y] = dot.xy;
      context.beginPath();
      context.strokeStyle = colors[dot.status];
      context.lineWidth = dot.id === s.selected ? 3 : 1.4;
      context.arc(x, y, dot.id === s.selected ? 11 : dot.expected || dot.found ? 8 : 3, 0, Math.PI * 2);
      context.stroke();
      if (!s.preview && reviews[dot.id]) {
        context.fillStyle = "#58edcd";
        context.fillRect(x + 8, y - 12, 4, 4);
      }
    }
  }
  for (const xy of Object.values(s.archive?.layout ?? s.endpoints).slice(0, 2)) {
    if (Array.isArray(xy) && !s.archive) evidenceMarker(context, xy);
  }
  if (s.sample) evidenceMarker(context, s.sample);
}

function activeDot() {
  return s.result?.dots.find(dot => dot.id === s.selected);
}

function refreshReview() {
  const dot = activeDot();
  const review = (s.archive?.reviews ?? s.plate.reviews)[s.selected];
  el("review-form").hidden = !dot || dot.status === "unseen" || s.archive !== null || s.preview;
  el("remove-review").disabled = !review;
  el("dot-detail").textContent = dot ?
    ("第 " + dot.row + " 行 / 第 " + dot.cell + " 格 / " + dot.dot + " 号点 · " + names[dot.status] + " · " +
    format(dot.xy) + (dot.contrast === null ? "" : " · 明暗差 " + dot.contrast) +
    (s.archive && review ? " · 已留档复核 " + review.action + (review.note ? "（" + review.note + "）" : "") : ""))
    : "点选照片上一个格点，查看判读及原片坐标。";
  el("decision").value = review?.action ?? "confirmed";
  el("note").value = review?.note ?? "";
  s.sample = review?.sample ?? (dot?.status === "unseen" ? null : dot?.xy ?? null);
  el("sample-info").textContent = format(s.sample);
}

function showResult(result, preview = false) {
  s.result = result;
  s.preview = preview;
  s.selected = "";
  el("view-state").textContent = s.archive ? "留样 · " + s.archive.title : preview ? "预览 · 待应用" : "现行版位";
  const odd = result?.dots.filter(dot => dot.status !== "match") ?? [];
  options(el("dots"), "从照片点击或选择异常格点", odd.map(dot =>
    ({label: dot.id + " · " + names[dot.status], value: dot.id})));
  el("counts").replaceChildren(...Object.entries(names).map(([status, name]) => {
    const item = document.createElement("span");
    item.className = "count " + status;
    item.textContent = name + " " + (result?.totals[status] ?? "—");
    return item;
  }));
  el("downloads").hidden = !s.archive;
  el("layout").querySelectorAll("input,textarea,button").forEach(control => { control.disabled = !!s.archive; });
  el("archive").querySelectorAll("input,button").forEach(control => { control.disabled = !result || preview || !!s.archive; });
  refreshReview();
  draw();
}

async function inspect(layout) {
  if (!layout.origin || !layout.axis) return null;
  return api(path("/inspect"), "POST", {layout});
}

async function openPlate(record) {
  const requestId = ++s.requestId;
  const image = new Image();
  const src = root + "/plates/" + record.id + "/source.png";
  image.src = src;
  await image.decode();
  if (requestId !== s.requestId) return;
  s.plate = record;
  s.bitmap = image;
  s.archive = null;
  s.pending = null;
  el("name").textContent = record.title;
  el("source-detail").textContent = (record.synthetic ? "合成练习样片" : "导入样片") + " · " + image.width + " × " + image.height + " px";
  el("raw").href = src + "?download";
  el("empty").hidden = true;
  el("desk").hidden = false;
  fillLayout();
  showResult(null);
  const result = await inspect(record.layout);
  if (requestId !== s.requestId) return;
  showResult(result);
  history.replaceState(null, "", "/?plate=" + record.id);
  await Promise.all([list(), archives()]);
  message(result ? "已打开压样。" : "请标定两点并填写字模。");
}

async function loadSample() {
  await openPlate(await api("/demo", "POST", {}));
}

function selectDot(dot) {
  if (!dot) return;
  s.selected = dot.id;
  if (![...el("dots").options].some(option => option.value === dot.id)) {
    el("dots").add(new Option(dot.id + " · " + names[dot.status], dot.id));
  }
  el("dots").value = dot.id;
  refreshReview();
  draw();
}

function beginPick(kind, text) {
  s.pending = kind;
  message(text);
}

el("demo").onclick = act(loadSample);
el("empty-demo").onclick = act(loadSample);
el("plates").onchange = act(async () => {
  if (el("plates").value) await openPlate(await api("/plates/" + el("plates").value));
});
el("upload").onchange = act(async () => {
  if (!el("upload").files.length) return;
  const form = new FormData();
  form.append("image", el("upload").files[0]);
  el("upload").value = "";
  await openPlate(await api("/plates", "POST", form));
});
el("mark-origin").onclick = () => beginPick("origin", "在原片点选第一行第一格的左上点。");
el("mark-axis").onclick = () => beginPick("axis", "沿第一行从左至右选择基线方向点。");
el("mark-sample").onclick = () => beginPick("sample", "在原片点一下，要复核的印点、纸纹或污渍将以此坐标留档。");
el("photo").onclick = event => {
  const canvas = event.currentTarget;
  const rect = canvas.getBoundingClientRect();
  const xy = [Math.round((event.clientX - rect.left) * canvas.width / rect.width),
    Math.round((event.clientY - rect.top) * canvas.height / rect.height)]
    .map((value, index) => Math.max(0, Math.min(value, (index === 0 ? canvas.width : canvas.height) - 1)));
  if (s.pending === "sample") {
    s.sample = xy;
    el("sample-info").textContent = format(xy);
    message("已选证据坐标，可保存点位复核。");
  } else if (s.pending) {
    s.endpoints[s.pending] = xy;
    updateEndpointLabels();
    message("已记下端点，可预览或应用版位。");
  } else {
    const clicked = (s.result?.dots ?? []).map(dot => [Math.hypot(dot.xy[0] - xy[0], dot.xy[1] - xy[1]), dot])
      .reduce((best, item) => item[0] < best[0] ? item : best, [18, null])[1];
    selectDot(clicked);
  }
  s.pending = null;
  draw();
};

for (const mode of ["photo", "grid"]) {
  el(mode + "-view").onclick = () => {
    s.mode = mode;
    for (const item of ["photo", "grid"]) el(item + "-view").setAttribute("aria-pressed", String(mode === item));
    draw();
  };
}
el("dots").onchange = () => selectDot(s.result?.dots.find(dot => dot.id === el("dots").value));
el("preview").onclick = act(async () => {
  if (!el("layout").reportValidity()) return;
  const layout = currentLayout();
  const result = await inspect(layout);
  if (!result) throw new Error("先在原片标出起点和方向点。");
  showResult(result, true);
  message("预览读数。确认后应用版位。");
});
el("layout").onsubmit = act(async () => {
  const layout = currentLayout();
  const changed = geometry.some(key => JSON.stringify(layout[key]) !== JSON.stringify(s.plate.layout[key]));
  const hasReviews = Object.keys(s.plate.reviews).length > 0;
  if (!layout.origin || !layout.axis) throw new Error("先在原片标出起点和方向点。");
  if (changed && hasReviews && !confirm("应用新版位将清空现行点位复核，已封存的复核单仍可重开。继续？")) return;
  s.plate = await api(path("/layout"), "PUT", {layout, revision: s.plate.revision, reset_reviews: changed && hasReviews});
  fillLayout();
  showResult(await inspect(s.plate.layout));
  message("已应用版位。");
});

async function writeReview(action) {
  const previous = activeDot();
  if (!previous || s.preview || s.archive) return;
  s.plate = await api(path("/review"), "PUT", {revision: s.plate.revision,
    dot_id: s.selected, action, note: el("note").value, sample: s.sample});
  selectDot(previous);
  message(action === null ? "已删除点位复核。" : "已保存点位复核。");
}
el("save-review").onclick = act(() => writeReview(el("decision").value));
el("remove-review").onclick = act(() => writeReview(null));
el("archive").onsubmit = act(async () => {
  const record = await api(path("/reports"), "POST", {revision: s.plate.revision, title: el("report-title").value});
  el("report-title").value = "";
  await openArchive(record);
  await archives();
  message("已封存，可下载复核证据。");
});

async function openArchive(record) {
  s.archive = record;
  s.pending = null;
  showResult(record.results);
  const base = root + path("/reports/" + record.id);
  el("download-png").href = base + "/overlay.png?download";
  el("download-csv").href = base + "/dots.csv";
}

el("reports").onchange = act(async () => {
  if (el("reports").value) {
    await openArchive(await api(path("/reports/" + el("reports").value)));
    message("已打开复核单。");
  } else if (s.archive) {
    await openPlate(s.plate);
  }
});
el("return-current").onclick = act(() => openPlate(s.plate));

act(async () => {
  await list();
  const active = new URLSearchParams(location.search).get("plate");
  if (active) await openPlate(await api("/plates/" + encodeURIComponent(active)));
})();
