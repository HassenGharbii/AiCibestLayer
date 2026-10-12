const INSTANCES_POLL_MS = 2000;
const TRAIN_POLL_MS = 1000;
const MAX_ZONE_POINTS = 12;

// --- Source preview + click-to-draw line/zone ------------------------------
// Backed by tx2's existing POST /api/sources/snapshot (grabs one frame from
// the RTSP/file source) — this was already built server-side, just never
// wired up to a UI before.

const canvas = document.getElementById("preview-canvas");
const ctx = canvas.getContext("2d");
const previewImg = new Image();
let previewPoints = []; // real pixel coords, in source-frame space
let previewReady = false;

// --- File upload (source_type = "file") ------------------------------------
// Backed by tx2's existing POST /api/sources/upload — also already built
// server-side, just never had a file picker wired up to it before.

const sourceTypeSelect = document.getElementsByName("source_type")[0];
const fileUploadRow = document.getElementById("file-upload-row");
const sourceUriInput = document.getElementsByName("source_uri")[0];

function toggleSourceUriMode() {
  const isFile = sourceTypeSelect.value === "file";
  fileUploadRow.style.display = isFile ? "flex" : "none";
  sourceUriInput.readOnly = isFile;
  sourceUriInput.placeholder = isFile ? "Upload a file below to fill this in…" : "rtsp://...";
  if (isFile) sourceUriInput.value = "";
}
sourceTypeSelect.addEventListener("change", toggleSourceUriMode);
toggleSourceUriMode();

document.getElementById("upload-btn").addEventListener("click", async () => {
  const fileInput = document.getElementById("file-input");
  const hint = document.getElementById("upload-hint");
  if (!fileInput.files.length) {
    alert("Choose a file first.");
    return;
  }
  const body = new FormData();
  body.append("file", fileInput.files[0]);
  hint.textContent = "Uploading…";
  try {
    const res = await fetch("/api/sources/upload", { method: "POST", body });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    sourceUriInput.value = data.file_id;
    hint.textContent = `Uploaded: ${data.filename}`;
  } catch (err) {
    hint.textContent = `Upload failed: ${err.message}`;
  }
});

function point(pair) {
  const [x, y] = pair.split(",").map((v) => parseFloat(v.trim()));
  return { x, y };
}

function pointsLabel(p) {
  return `${Math.round(p.x)},${Math.round(p.y)}`;
}

function redrawPreview() {
  if (!previewReady) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(previewImg, 0, 0, canvas.width, canvas.height);

  const type = document.getElementsByName("config_type")[0].value;
  const closeLoop = type === "zone_occupancy" && previewPoints.length >= 3;

  ctx.strokeStyle = "#e0333f";
  ctx.fillStyle = "#e0333f";
  ctx.lineWidth = Math.max(2, canvas.width / 320);

  if (previewPoints.length > 1) {
    ctx.beginPath();
    ctx.moveTo(previewPoints[0].x, previewPoints[0].y);
    for (let i = 1; i < previewPoints.length; i++) ctx.lineTo(previewPoints[i].x, previewPoints[i].y);
    if (closeLoop) ctx.lineTo(previewPoints[0].x, previewPoints[0].y);
    ctx.stroke();
  }
  const r = Math.max(4, canvas.width / 160);
  previewPoints.forEach((p) => {
    ctx.beginPath();
    ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    ctx.fill();
  });
}

function updatePointsHint() {
  const type = document.getElementsByName("config_type")[0].value;
  const hint = document.getElementById("points-hint");
  const clearBtn = document.getElementById("clear-points-btn");
  clearBtn.style.display = previewPoints.length ? "inline-block" : "none";
  if (!previewReady) {
    hint.textContent = "";
    return;
  }
  if (type === "zone_occupancy") {
    hint.textContent = previewPoints.length < 3
      ? `Click ${3 - previewPoints.length} more point(s) to close the zone (${previewPoints.length} so far).`
      : `Zone set — ${previewPoints.length} points. Click again to add more, or Create.`;
  } else {
    hint.textContent = previewPoints.length < 2
      ? "Click the two endpoints of the counting line."
      : "Line set — click again to redraw it from scratch.";
  }
}

function syncManualFieldsFromPoints() {
  const form = document.getElementById("create-form");
  const type = form.config_type.value;
  if (type !== "zone_occupancy" && previewPoints.length >= 2) {
    form.p1.value = pointsLabel(previewPoints[0]);
    form.p2.value = pointsLabel(previewPoints[1]);
  }
}

canvas.addEventListener("click", (e) => {
  if (!previewReady) return;
  const rect = canvas.getBoundingClientRect();
  const x = ((e.clientX - rect.left) / rect.width) * canvas.width;
  const y = ((e.clientY - rect.top) / rect.height) * canvas.height;
  const type = document.getElementsByName("config_type")[0].value;

  if (type === "zone_occupancy") {
    if (previewPoints.length >= MAX_ZONE_POINTS) previewPoints = [];
    previewPoints.push({ x, y });
  } else {
    // Line types only ever need 2 points — a 3rd click starts a fresh line.
    if (previewPoints.length >= 2) previewPoints = [];
    previewPoints.push({ x, y });
  }
  syncManualFieldsFromPoints();
  redrawPreview();
  updatePointsHint();
});

document.getElementById("clear-points-btn").addEventListener("click", () => {
  previewPoints = [];
  redrawPreview();
  updatePointsHint();
});

document.getElementsByName("config_type")[0].addEventListener("change", () => {
  previewPoints = []; // point count/meaning differs per type, don't carry over
  redrawPreview();
  updatePointsHint();
});

document.getElementById("preview-btn").addEventListener("click", async () => {
  const form = document.getElementById("create-form");
  const hint = document.getElementById("preview-hint");
  if (!form.source_uri.value.trim()) {
    alert("Enter a source URI first.");
    return;
  }
  hint.textContent = "Loading preview…";
  try {
    const res = await fetch("/api/sources/snapshot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: form.source_type.value, uri: form.source_uri.value }),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    previewImg.onload = () => {
      canvas.width = data.width;
      canvas.height = data.height;
      canvas.style.display = "block";
      previewReady = true;
      previewPoints = [];
      form.frame_width.value = data.width;
      form.frame_height.value = data.height;
      redrawPreview();
      updatePointsHint();
      hint.textContent = "Preview loaded — click on the image.";
    };
    previewImg.src = `data:image/jpeg;base64,${data.image_base64}`;
  } catch (err) {
    hint.textContent = `Preview failed: ${err.message}`;
  }
});

function buildConfig(form) {
  const type = form.config_type.value;
  const frame_width = parseInt(form.frame_width.value, 10);
  const frame_height = parseInt(form.frame_height.value, 10);
  if (type === "zone_occupancy") {
    const pts = previewPoints.length >= 3
      ? previewPoints
      : [point(form.p1.value), point(form.p2.value), { x: frame_width / 2, y: frame_height }];
    return { type, points: pts, frame_width, frame_height };
  }
  const [p1, p2] = previewPoints.length >= 2
    ? previewPoints
    : [point(form.p1.value), point(form.p2.value)];
  return { type, p1, p2, frame_width, frame_height };
}

document.getElementById("create-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const body = {
    name: form.name.value,
    source: { type: form.source_type.value, uri: form.source_uri.value },
    config: buildConfig(form),
  };
  const res = await fetch("/api/instances", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    alert(`Failed to create instance: ${await res.text()}`);
    return;
  }
  form.reset();
  previewPoints = [];
  previewReady = false;
  canvas.style.display = "none";
  document.getElementById("preview-hint").textContent = "";
  document.getElementById("upload-hint").textContent = "";
  updatePointsHint();
  toggleSourceUriMode();
  refreshInstances();
});

async function callAction(id, action) {
  await fetch(`/api/instances/${id}/${action}`, { method: "POST" });
  refreshInstances();
}

async function deleteInstance(id) {
  await fetch(`/api/instances/${id}`, { method: "DELETE" });
  refreshInstances();
}

function renderInstances(instances) {
  const body = document.getElementById("instances-body");
  body.innerHTML = "";
  for (const inst of instances) {
    const tr = document.createElement("tr");
    const countingLabel = inst.counting ? "counting" : "paused";
    tr.innerHTML = `
      <td>${inst.name}</td>
      <td>${inst.status}</td>
      <td>${countingLabel}</td>
      <td>${inst.counts.in_count}</td>
      <td>${inst.counts.out_count}</td>
      <td>${inst.counts.current}</td>
      <td>${inst.counts.total}</td>
      <td><img class="stream" src="/api/instances/${inst.id}/stream" /></td>
      <td>
        <button data-action="start" data-id="${inst.id}" ${inst.counting ? "disabled" : ""}>Start</button>
        <button class="secondary" data-action="stop" data-id="${inst.id}" ${inst.counting ? "" : "disabled"}>Stop</button>
        <button class="secondary" data-action="delete" data-id="${inst.id}">Delete</button>
      </td>
    `;
    body.appendChild(tr);
  }
  body.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const { action, id } = btn.dataset;
      if (action === "delete") deleteInstance(id);
      else callAction(id, action);
    });
  });
}

async function refreshInstances() {
  try {
    const res = await fetch("/api/instances");
    renderInstances(await res.json());
  } catch (err) {
    console.error("failed to refresh instances", err);
  }
}

function fmt(value, digits = 4) {
  return value === null || value === undefined ? "—" : Number(value).toFixed(digits);
}

async function refreshTrainState() {
  const badge = document.getElementById("train-badge");
  try {
    const res = await fetch("/api/train-state");
    if (res.status === 404) {
      badge.textContent = "Train state: no data yet";
      badge.className = "badge badge-unknown";
      return;
    }
    const s = await res.json();
    document.getElementById("tf-station").textContent = s.station_label ?? "—";
    document.getElementById("tf-destination").textContent = s.destination_label ?? "—";
    document.getElementById("tf-trip").textContent = s.trip_id ?? "—";
    document.getElementById("tf-speed").textContent = s.speed_kmh != null ? `${fmt(s.speed_kmh, 1)} km/h` : "—";
    document.getElementById("tf-cab").textContent = s.cab_active ? `yes (${s.active_cab_id})` : "no";
    document.getElementById("tf-pos").textContent = s.lat != null ? `${fmt(s.lat)}, ${fmt(s.lon)}` : "—";
    document.getElementById("tf-in-station").textContent = s.in_station == null ? "—" : (s.in_station ? "yes" : "no");
    document.getElementById("tf-updated").textContent = new Date(s.timestamp).toLocaleTimeString();

    const active = Boolean(s.in_station) && Boolean(s.cab_active);
    badge.textContent = `Counting should be: ${active ? "ACTIVE" : "inactive"} (FR-001)`;
    badge.className = `badge ${active ? "badge-active" : "badge-inactive"}`;
  } catch (err) {
    badge.textContent = "Train state: unreachable";
    badge.className = "badge badge-unknown";
  }
}

refreshInstances();
refreshTrainState();
setInterval(refreshInstances, INSTANCES_POLL_MS);
setInterval(refreshTrainState, TRAIN_POLL_MS);
