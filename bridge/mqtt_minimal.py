import json
import os
import subprocess
import threading
import time
from fastapi import FastAPI

HOST = os.environ.get("MQTT_HOST", "10.136.115.96")
CAFILE = os.environ.get("MQTT_CAFILE", "/home/nvidia/Desktop/ca.crt")
state = {}


def repair(p):
    ok = p.endswith("}") and not p.endswith("\"}")
    if not ok:
        return None
    try:
        return json.loads(p[:-1] + "\"}")
    except ValueError:
        return None


def build_cmd():
    cmd = []
    cmd.append("mosquitto_sub")
    cmd.append("-h")
    cmd.append(HOST)
    cmd.append("-p")
    cmd.append("8883")
    cmd.append("-u")
    cmd.append("ia_server")
    cmd.append("-P")
    cmd.append("ia_server")
    cmd.append("-t")
    cmd.append("train/+/event/#")
    cmd.append("-v")
    cmd.append("--cafile")
    cmd.append(CAFILE)
    cmd.append("--insecure")
    return cmd


def run_once(cmd):
    out = subprocess.PIPE
    err = subprocess.STDOUT
    proc = subprocess.Popen(cmd, stdout=out, stderr=err, universal_newlines=True)
    for line in proc.stdout:
        handle_line(line)


def handle_line(line):
    if " " not in line:
        return
    topic, raw = line.strip().split(" ", 1)
    raw = raw.strip()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = repair(raw)
        if payload is None:
            return
    kind = topic
    if "/event/" in topic:
        kind = topic.split("/event/")[-1]
    state[kind] = payload
    update_trigger()


def update_trigger():
    ts = state.get("train_state", {})
    a = bool(ts.get("in_station"))
    b = bool(ts.get("cab_active"))
    state["counting_active"] = a and b


def loop():
    cmd = build_cmd()
    while True:
        try:
            run_once(cmd)
        except Exception as exc:
            print("error: " + str(exc))
        time.sleep(5)


app = FastAPI()


@app.on_event("startup")
def start():
    t = threading.Thread(target=loop)
    t.daemon = True
    t.start()


@app.get("/state")
def get_state():
    return state


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
