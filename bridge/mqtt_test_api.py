"""Standalone MQTT test backend, FastAPI, single file, zero new pip installs.

Wraps the already-working `mosquitto_sub` CLI as a background subprocess
(instead of the paho-mqtt Python library, which would need a pip install
you don't have offline access for) and exposes the live merged train state
plus a rolling event log over HTTP, so you can watch results in a browser
instead of a scrolling terminal.

Copy this one file to the Jetson and run it directly:
    python3 mqtt_test_api.py

Defaults match the connection you already proved works. Override via env
vars if needed: MQTT_HOST, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD,
MQTT_CAFILE, MQTT_INSECURE (1/0), MQTT_TOPIC, API_PORT.

Endpoints:
    GET /            - tiny auto-refreshing HTML view
    GET /state       - current merged train state, JSON
    GET /events      - last 50 parsed/raw events, newest first
    GET /health      - broker subprocess alive? last message received when?
"""

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

MQTT_HOST = os.environ.get("MQTT_HOST", "10.136.115.96")
MQTT_PORT = os.environ.get("MQTT_PORT", "8883")
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "ia_server")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "ia_server")
MQTT_CAFILE = os.environ.get("MQTT_CAFILE", "/home/nvidia/Desktop/ca.crt")
MQTT_INSECURE = os.environ.get("MQTT_INSECURE", "1") == "1"
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "train/+/event/#")
API_PORT = int(os.environ.get("API_PORT", "8001"))

EVENT_LOG_MAX = 50


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class SharedState(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.train_id = None
        self.station_label = None
        self.destination_label = None
        self.trip_id = None
        self.in_station = None
        self.speed_kmh = None
        self.cab_active = None
        self.active_cab_id = None
        self.lat = None
        self.lon = None
        self.last_clock = None
        self.last_message_at = None
        self.subprocess_alive = False
        self.events = []  # newest first

    def counting_should_be_active(self):
        return bool(self.in_station) and bool(self.cab_active)

    def snapshot(self):
        with self.lock:
            return {
                "train_id": self.train_id,
                "station_label": self.station_label,
                "destination_label": self.destination_label,
                "trip_id": self.trip_id,
                "in_station": self.in_station,
                "speed_kmh": self.speed_kmh,
                "cab_active": self.cab_active,
                "active_cab_id": self.active_cab_id,
                "lat": self.lat,
                "lon": self.lon,
                "last_clock": self.last_clock,
                "last_message_at": self.last_message_at,
                "counting_should_be_active": self.counting_should_be_active(),
            }

    def add_event(self, topic, parsed_ok, payload_or_error, repaired):
        with self.lock:
            self.events.insert(0, {
                "at": now_iso(),
                "topic": topic,
                "parsed_ok": parsed_ok,
                "repaired": repaired,
                "payload": payload_or_error,
            })
            self.events = self.events[:EVENT_LOG_MAX]
            self.last_message_at = now_iso()


state = SharedState()


def try_repair_missing_quote(payload_raw):
    """Known bug in the real MDR-6/NVR train_state publisher: last string
    field is missing its closing quote before the final '}'. Only ever
    tried as a fallback after a normal parse already failed."""
    if payload_raw.endswith("}") and not payload_raw.endswith("\"}"):
        repaired = payload_raw[:-1] + "\"}"
        try:
            return json.loads(repaired)
        except ValueError:
            return None
    return None


def handle_line(line):
    line = line.rstrip("\n")
    if not line or " " not in line:
        return
    topic, payload_raw = line.split(" ", 1)

    repaired = False
    try:
        payload = json.loads(payload_raw)
    except ValueError:
        payload = try_repair_missing_quote(payload_raw)
        if payload is None:
            state.add_event(topic, False, payload_raw[:300], False)
            return
        repaired = True

    state.add_event(topic, True, payload, repaired)

    parts = topic.split("/")
    if len(parts) < 3 or parts[0] != "train":
        return
    with state.lock:
        state.train_id = parts[1]
    kind = "/".join(parts[2:])

    if kind == "event/clock":
        with state.lock:
            state.last_clock = payload.get("sync_datetime")
    elif kind == "event/topology":
        with state.lock:
            state.station_label = payload.get("stop_label")
            state.destination_label = payload.get("destination_label")
            state.trip_id = payload.get("trip_id") or payload.get("course_id")
    elif kind == "event/train_state":
        with state.lock:
            state.in_station = payload.get("in_station")
            state.speed_kmh = payload.get("speed_kmh")
            state.cab_active = payload.get("cab_active")
            state.active_cab_id = payload.get("active_cab_id")
    elif kind == "event/position":
        with state.lock:
            state.lat = payload.get("latitude")
            state.lon = payload.get("longitude")


def mqtt_subscriber_loop():
    cmd = [
        "mosquitto_sub",
        "-h", MQTT_HOST, "-p", str(MQTT_PORT),
        "-u", MQTT_USERNAME, "-P", MQTT_PASSWORD,
        "-t", MQTT_TOPIC, "-v",
    ]
    if MQTT_CAFILE:
        cmd += ["--cafile", MQTT_CAFILE]
    if MQTT_INSECURE:
        cmd.append("--insecure")

    while True:
        print("Launching: " + " ".join(cmd))
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     universal_newlines=True, bufsize=1)
            state.subprocess_alive = True
            for line in proc.stdout:
                handle_line(line)
            proc.wait()
        except Exception as exc:  # noqa: BLE001 - keep retrying no matter what
            print("mosquitto_sub error: " + str(exc))
        state.subprocess_alive = False
        print("mosquitto_sub exited, restarting in 5s...")
        time.sleep(5)


app = FastAPI(title="MQTT Test Backend")


@app.on_event("startup")
def start_subscriber():
    t = threading.Thread(target=mqtt_subscriber_loop, daemon=True)
    t.start()


@app.get("/state")
def get_state():
    return state.snapshot()


@app.get("/events")
def get_events():
    with state.lock:
        return list(state.events)


@app.get("/health")
def get_health():
    return {
        "subprocess_alive": state.subprocess_alive,
        "last_message_at": state.last_message_at,
        "broker": MQTT_HOST + ":" + str(MQTT_PORT),
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!doctype html>
<html>
<head>
<meta http-equiv="refresh" content="2">
<title>MQTT Test Backend</title>
<style>
body { font-family: monospace; background: #111; color: #eee; padding: 2rem; }
h1 { color: #4af; }
.active { color: #4f4; font-weight: bold; }
.inactive { color: #f44; font-weight: bold; }
a { color: #4af; }
</style>
</head>
<body>
<h1>MQTT Test Backend</h1>
<p>Auto-refreshes every 2s. Raw JSON: <a href="/state">/state</a> - <a href="/events">/events</a> - <a href="/health">/health</a></p>
<div id="state">Loading...</div>
<script>
fetch('/state').then(r => r.json()).then(s => {
  document.getElementById('state').innerHTML =
    '<pre>' + JSON.stringify(s, null, 2) + '</pre>' +
    '<h2 class="' + (s.counting_should_be_active ? 'active' : 'inactive') + '">' +
    'COUNTING SHOULD BE ' + (s.counting_should_be_active ? 'ACTIVE' : 'INACTIVE') + ' (FR-001)</h2>';
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=API_PORT)
