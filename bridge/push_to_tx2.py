import json
import os
import time
import urllib.request

SRC = os.environ.get("SRC", "http://localhost:8001/state")
DST = os.environ.get("DST", "http://localhost:8000/api/train-state")


def get(url):
    resp = urllib.request.urlopen(url, timeout=5)
    return json.loads(resp.read())


def post(url, data):
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    urllib.request.urlopen(req, timeout=5).read()


def build_payload(s):
    ts = s.get("train_state", {})
    pos = s.get("position", {})
    topo = s.get("topology", {})
    out = {}
    out["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out["lat"] = pos.get("latitude")
    out["lon"] = pos.get("longitude")
    out["speed_kmh"] = ts.get("speed_kmh")
    out["in_station"] = ts.get("in_station")
    out["cab_active"] = ts.get("cab_active")
    out["active_cab_id"] = ts.get("active_cab_id")
    out["station_label"] = topo.get("stop_label")
    out["destination_label"] = topo.get("destination_label")
    out["trip_id"] = topo.get("trip_id")
    return out


def main():
    print("Pushing " + SRC + " -> " + DST)
    while True:
        try:
            s = get(SRC)
            payload = build_payload(s)
            post(DST, payload)
        except Exception as exc:
            print("error: " + str(exc))
        time.sleep(2)


if __name__ == "__main__":
    main()
