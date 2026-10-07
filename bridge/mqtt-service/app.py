"""MQTT bridge + persistence service.

Subscribes to the real MDR-6's MQTT broker as the "ia_server" account
(per CDGX-VIDEO-GRP-SFD-INT-XX-XX-Interface_IX_avec_le_serveur_IA_rev02.docx
section 7), keeps a live merged train state, persists every update to
Postgres, triggers tx2's passenger counting start/stop per FR-001
(in_station && cab_active), and exposes everything over HTTP for the
Dashboard-CDGX frontend to consume directly.

Known upstream bug (MDR-6/NVR publisher): train_state's last string field
is missing its closing quote before the final '}'. Auto-repaired as a
fallback after a normal parse fails.
"""

import json
import logging
import os
import ssl
import threading
import time
from contextlib import contextmanager

import paho.mqtt.client as mqtt
import psycopg2
import psycopg2.extras
import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mqtt-service")

# --- MQTT broker (the real MDR-6) ------------------------------------------
MQTT_HOST = os.environ.get("MQTT_HOST", "10.136.115.96")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "ia_server")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "ia_server")
MQTT_CAFILE = os.environ.get("MQTT_CAFILE", "")
MQTT_INSECURE = os.environ.get("MQTT_INSECURE", "true").lower() == "true"
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "train/+/event/#")

# --- tx2 (passenger counting) ----------------------------------------------
TX2_URL = os.environ.get("TX2_URL", "http://localhost:8000").rstrip("/")
INSTANCE_SYNC_INTERVAL_SECONDS = 15.0  # catches instances created mid-session

# --- Postgres ----------------------------------------------------------------
PG_HOST = os.environ.get("POSTGRES_HOST", "db")
PG_PORT = os.environ.get("POSTGRES_PORT", "5432")
PG_DB = os.environ.get("POSTGRES_DB", "mqtt_bridge")
PG_USER = os.environ.get("POSTGRES_USER", "mqtt_bridge")
PG_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "mqtt_bridge")

HISTORY_SAVE_INTERVAL_SECONDS = 2.0

state_lock = threading.Lock()
state = {}
last_trigger = {"value": None}
last_history_save = {"value": 0.0}


# --- Postgres helpers --------------------------------------------------------

def connect_with_retry():
    while True:
        try:
            conn = psycopg2.connect(
                host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=PG_USER, password=PG_PASSWORD
            )
            conn.autocommit = True
            log.info("Connected to Postgres")
            return conn
        except Exception as exc:
            log.warning("Postgres connection failed (%s), retrying in 5s...", exc)
            time.sleep(5)


_conn = None
_conn_lock = threading.Lock()


@contextmanager
def db_cursor():
    global _conn
    with _conn_lock:
        if _conn is None or _conn.closed:
            _conn = connect_with_retry()
        try:
            cur = _conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            yield cur
            cur.close()
        except psycopg2.OperationalError:
            log.warning("Postgres connection lost, will reconnect")
            _conn = None
            raise


def init_db():
    schema = open(os.path.join(os.path.dirname(__file__), "init.sql")).read()
    with db_cursor() as cur:
        cur.execute(schema)
    log.info("Schema ready")


def save_train_state(train_id):
    with state_lock:
        ts = state.get("train_state", {})
        pos = state.get("position", {})
        topo = state.get("topology", {})
        counting_active = state.get("counting_active")
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO train_state_history
                    (train_id, lat, lon, speed_kmh, in_station, cab_active,
                     active_cab_id, station_label, destination_label, trip_id, counting_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    train_id,
                    pos.get("latitude"),
                    pos.get("longitude"),
                    ts.get("speed_kmh"),
                    ts.get("in_station"),
                    ts.get("cab_active"),
                    ts.get("active_cab_id"),
                    topo.get("stop_label"),
                    topo.get("destination_label"),
                    topo.get("trip_id"),
                    counting_active,
                ),
            )
    except Exception as exc:
        log.warning("Failed to save train state history: %s", exc)


def save_alarm(train_id, payload):
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO tcms_alarms
                    (train_id, alarm_source_id, alarm_type, alarm_state,
                     tcms_signal_name, vehicle_zone, raw)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    train_id,
                    payload.get("alarm_source_id"),
                    payload.get("alarm_type"),
                    payload.get("alarm_state"),
                    payload.get("tcms_signal_name"),
                    payload.get("vehicle_zone"),
                    psycopg2.extras.Json(payload),
                ),
            )
    except Exception as exc:
        log.warning("Failed to save alarm: %s", exc)


# --- tx2 integration ----------------------------------------------------------
# Applies to ALL of tx2's instances (cameras/lines), not a single hardcoded
# one — so instances created later (via tx2's own UI) get picked up
# automatically, both immediately on the next train-state change and via
# the periodic sync below (for ones created while the state hasn't changed).

def get_tx2_instances():
    try:
        resp = requests.get(TX2_URL + "/api/instances", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        log.warning("Failed to fetch tx2 instances: %s", exc)
        return []


def sync_all_instances(active):
    instances = get_tx2_instances()
    action = "start" if active else "stop"
    for inst in instances:
        if bool(inst.get("counting")) == active:
            continue  # already in the desired state, skip
        inst_id = inst.get("id")
        url = TX2_URL + "/api/instances/" + inst_id + "/" + action
        try:
            requests.post(url, timeout=5)
            log.info("tx2 instance %s: %s (active=%s)", inst_id, action.upper(), active)
        except requests.RequestException as exc:
            log.warning("tx2 trigger call failed for instance %s: %s", inst_id, exc)


def periodic_instance_sync_loop():
    while True:
        time.sleep(INSTANCE_SYNC_INTERVAL_SECONDS)
        with state_lock:
            active = state.get("counting_active")
        if active is not None:
            sync_all_instances(active)


def push_train_state_to_tx2():
    with state_lock:
        ts = state.get("train_state", {})
        pos = state.get("position", {})
        topo = state.get("topology", {})
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lat": pos.get("latitude"),
        "lon": pos.get("longitude"),
        "speed_kmh": ts.get("speed_kmh"),
        "in_station": ts.get("in_station"),
        "cab_active": ts.get("cab_active"),
        "active_cab_id": ts.get("active_cab_id"),
        "station_label": topo.get("stop_label"),
        "destination_label": topo.get("destination_label"),
        "trip_id": topo.get("trip_id"),
    }
    try:
        requests.post(TX2_URL + "/api/train-state", json=payload, timeout=5)
    except requests.RequestException as exc:
        log.warning("tx2 train-state push failed: %s", exc)


# --- MQTT message handling ----------------------------------------------------

def try_repair_missing_quote(payload_raw):
    if payload_raw.endswith("}") and not payload_raw.endswith('"}'):
        repaired = payload_raw[:-1] + '"}'
        try:
            return json.loads(repaired)
        except ValueError:
            return None
    return None


def update_trigger_and_persist(train_id):
    with state_lock:
        ts = state.get("train_state", {})
        active = bool(ts.get("in_station")) and bool(ts.get("cab_active"))
        state["counting_active"] = active

    if active != last_trigger["value"]:
        last_trigger["value"] = active
        sync_all_instances(active)

    now = time.time()
    if now - last_history_save["value"] >= HISTORY_SAVE_INTERVAL_SECONDS:
        last_history_save["value"] = now
        save_train_state(train_id)
        push_train_state_to_tx2()


def on_connect(client, userdata, flags, rc):
    if rc != 0:
        log.error("MQTT connect failed, rc=%s", rc)
        return
    log.info("Connected to broker %s:%s", MQTT_HOST, MQTT_PORT)
    client.subscribe(MQTT_TOPIC, qos=1)


def on_disconnect(client, userdata, rc):
    log.warning("Disconnected from broker (rc=%s), paho will auto-reconnect", rc)


def on_message(client, userdata, msg):
    topic = msg.topic
    raw = msg.payload.decode("utf-8", errors="replace")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = try_repair_missing_quote(raw)
        if payload is None:
            log.warning("Unparseable payload on %s: %s", topic, raw[:200])
            return
        log.info("Auto-repaired malformed JSON on %s", topic)

    parts = topic.split("/")
    if len(parts) < 3 or parts[0] != "train":
        return
    train_id = parts[1]
    kind = "/".join(parts[2:])
    if kind.startswith("event/"):
        kind = kind[len("event/"):]

    with state_lock:
        state["train_id"] = train_id

    if kind == "topology":
        with state_lock:
            state["topology"] = payload
    elif kind == "train_state":
        with state_lock:
            state["train_state"] = payload
        update_trigger_and_persist(train_id)
    elif kind == "position":
        with state_lock:
            state["position"] = payload
    elif kind == "clock":
        with state_lock:
            state["clock"] = payload
    elif kind == "alarm":
        log.info(
            "TCMS ALARM: %s on %s = %s",
            payload.get("alarm_type"), payload.get("vehicle_zone"), payload.get("alarm_state"),
        )
        save_alarm(train_id, payload)
    elif topic.endswith("/health/mdr6"):
        with state_lock:
            state["mdr6_health"] = payload


def start_mqtt_client():
    client = mqtt.Client()
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    if MQTT_INSECURE:
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)
    elif MQTT_CAFILE:
        client.tls_set(ca_certs=MQTT_CAFILE)
    else:
        client.tls_set()

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    # connect_async + loop_start: the actual connection attempt happens on
    # the background network thread and paho retries automatically on
    # failure/disconnect. A plain connect() is blocking and raises on
    # failure, which would crash the whole FastAPI app (including /state,
    # /history, /alarms) if the broker is briefly unreachable at startup.
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()
    return client


# --- FastAPI -------------------------------------------------------------------

app = FastAPI(title="MQTT Bridge Service")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def startup():
    init_db()
    start_mqtt_client()
    t = threading.Thread(target=periodic_instance_sync_loop, daemon=True)
    t.start()


@app.get("/state")
def get_state():
    with state_lock:
        return dict(state)


@app.get("/history")
def get_history(limit: int = 100):
    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM train_state_history ORDER BY received_at DESC LIMIT %s",
            (min(limit, 1000),),
        )
        return cur.fetchall()


@app.get("/alarms")
def get_alarms(limit: int = 100):
    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM tcms_alarms ORDER BY received_at DESC LIMIT %s",
            (min(limit, 1000),),
        )
        return cur.fetchall()


@app.get("/health")
def get_health():
    with state_lock:
        has_data = bool(state)
    instances = get_tx2_instances()
    return {
        "status": "ok",
        "mqtt_connected": has_data,
        "tx2_instances_count": len(instances),
        "tx2_instances": [i.get("id") for i in instances],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
