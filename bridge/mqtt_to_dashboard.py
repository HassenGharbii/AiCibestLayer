"""Production MQTT bridge, dependency-free, pushes into tx2/Dashboard-CDGX."""
import json, os, subprocess, sys, threading, time, urllib.request

MQTT_HOST = os.environ.get("MQTT_HOST", "10.136.115.96")
MQTT_PORT = os.environ.get("MQTT_PORT", "8883")
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "ia_server")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "ia_server")
MQTT_CAFILE = os.environ.get("MQTT_CAFILE", "/home/nvidia/Desktop/ca.crt")
MQTT_INSECURE = os.environ.get("MQTT_INSECURE", "1") == "1"
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "train/+/event/#")
TX2_URL = os.environ.get("TX2_URL", "http://localhost:8000").rstrip("/")
TX2_INSTANCE_ID = os.environ.get("TX2_INSTANCE_ID", "")
BACKEND_URL = os.environ.get("BACKEND_URL", "").rstrip("/")
PUSH_INTERVAL_SECONDS = 1.0


def post_json(url, data):
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=5).read()
        return True
    except Exception as exc:
        print("POST " + url + " failed: " + str(exc))
        return False


class State(object):
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
        self.last_trigger = None

    def counting_should_be_active(self):
        return bool(self.in_station) and bool(self.cab_active)

    def snapshot(self):
        with self.lock:
            d = {}
            d["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            d["lat"] = self.lat
            d["lon"] = self.lon
            d["speed_kmh"] = self.speed_kmh
            d["in_station"] = self.in_station
            d["cab_active"] = self.cab_active
            d["active_cab_id"] = self.active_cab_id
            d["station_label"] = self.station_label
            d["destination_label"] = self.destination_label
            d["trip_id"] = self.trip_id
            d["trainset_num"] = self.train_id
            return d


state = State()


def try_repair_missing_quote(payload_raw):
    if payload_raw.endswith("}") and not payload_raw.endswith("\"}"):
        repaired = payload_raw[:-1] + "\"}"
        try:
            return json.loads(repaired)
        except ValueError:
            return None
    return None


def check_trigger():
    trigger = state.counting_should_be_active()
    if trigger != state.last_trigger and TX2_INSTANCE_ID:
        action = "start" if trigger else "stop"
        url = TX2_URL + "/api/instances/" + TX2_INSTANCE_ID + "/" + action
        if post_json(url, {}):
            print("tx2 counting " + ("STARTED" if trigger else "STOPPED") + " active=" + str(trigger))
        state.last_trigger = trigger


def handle_line(line):
    line = line.rstrip("\n")
    if not line or " " not in line:
        return
    topic, payload_raw = line.split(" ", 1)
    try:
        payload = json.loads(payload_raw)
    except ValueError:
        payload = try_repair_missing_quote(payload_raw)
        if payload is None:
            print("(unparseable payload on " + topic + "): " + payload_raw[:200])
            return
    parts = topic.split("/")
    if len(parts) < 3 or parts[0] != "train":
        return
    train_id = parts[1]
    kind = "/".join(parts[2:])
    with state.lock:
        state.train_id = train_id
    if kind == "event/topology":
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
        check_trigger()
    elif kind == "event/position":
        with state.lock:
            state.lat = payload.get("latitude")
            state.lon = payload.get("longitude")
    elif kind == "event/alarm":
        print("*** TCMS ALARM *** " + str(payload.get("alarm_type")) + " on " + str(payload.get("vehicle_zone")) + " = " + str(payload.get("alarm_state")))
        if BACKEND_URL:
            post_json(BACKEND_URL + "/api/alarms/tcms", dict(payload, train_id=train_id))
    elif kind == "health/mdr6":
        print("MDR-6 health: " + str(payload))


def mqtt_subscriber_loop():
    cmd = ["mosquitto_sub", "-h", MQTT_HOST, "-p", str(MQTT_PORT), "-u", MQTT_USERNAME, "-P", MQTT_PASSWORD, "-t", MQTT_TOPIC, "-v"]
    if MQTT_CAFILE:
        cmd += ["--cafile", MQTT_CAFILE]
    if MQTT_INSECURE:
        cmd.append("--insecure")
    while True:
        print("Launching: " + " ".join(cmd))
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1)
            for line in proc.stdout:
                handle_line(line)
            proc.wait()
        except Exception as exc:
            print("mosquitto_sub error: " + str(exc))
        print("mosquitto_sub exited, restarting in 5s...")
        time.sleep(5)


def push_loop():
    url = TX2_URL + "/api/train-state"
    while True:
        post_json(url, state.snapshot())
        time.sleep(PUSH_INTERVAL_SECONDS)


def main():
    if not TX2_INSTANCE_ID:
        print("WARNING: TX2_INSTANCE_ID not set - start/stop skipped, only train-state will push.")
    t1 = threading.Thread(target=mqtt_subscriber_loop)
    t1.daemon = True
    t1.start()
    t2 = threading.Thread(target=push_loop)
    t2.daemon = True
    t2.start()
    print("Bridge running. tx2=" + TX2_URL + " broker=" + MQTT_HOST + ":" + str(MQTT_PORT) + ". Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")


if __name__ == "__main__":
    sys.exit(main())
