const INSTANCES_POLL_MS = 2000;
const TRAIN_POLL_MS = 1000;

function point(pair) {
  const [x, y] = pair.split(",").map((v) => parseFloat(v.trim()));
  return { x, y };
}

function buildConfig(form) {
  const type = form.config_type.value;
  const frame_width = parseInt(form.frame_width.value, 10);
  const frame_height = parseInt(form.frame_height.value, 10);
  if (type === "zone_occupancy") {
    // Reuses the line's two points as a minimal 3+ point polygon fallback.
    return {
      type,
      points: [point(form.p1.value), point(form.p2.value), { x: frame_width / 2, y: frame_height }],
      frame_width,
      frame_height,
    };
  }
  return {
    type,
    p1: point(form.p1.value),
    p2: point(form.p2.value),
    frame_width,
    frame_height,
  };
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
