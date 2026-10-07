"""Standalone MQTT test viewer — zero external dependencies (stdlib only),
for offline boxes with no internet / no pip access.

Wraps the already-working `mosquitto_sub` CLI as a subprocess (instead of
the paho-mqtt Python library, which would need a pip install) and parses its
"-v" output ("<topic> <payload>" per line) to keep a live merged train
state, printed whenever something changes — including the FR-001
counting-should-be-active flag (in_station && cab_active).

Usage (matches the working command you already ran):
    python3 mqtt_test_backend.py \
        --host 10.136.115.96 --port 8883 \
        --username ia_server --password ia_server \
        --cafile /home/nvidia/Desktop/ca.crt --insecure

Requires: the `mosquitto_sub` binary already on PATH (it is, you used it).
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


class State:
    def __init__(self):
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

    def counting_should_be_active(self):
        return bool(self.in_station) and bool(self.cab_active)

    def print_summary(self):
        active = self.counting_should_be_active()
        print(
            f"\n[{now()}] === Train {self.train_id or '?'} ===\n"
            f"  Station      : {self.station_label or '-'}\n"
            f"  Destination  : {self.destination_label or '-'}\n"
            f"  Trip         : {self.trip_id or '-'}\n"
            f"  Speed        : {self.speed_kmh if self.speed_kmh is not None else '-'} km/h\n"
            f"  In station   : {self.in_station}\n"
            f"  Cab active   : {self.cab_active} ({self.active_cab_id or '-'})\n"
            f"  Position     : {self.lat}, {self.lon}\n"
            f"  >>> COUNTING SHOULD BE {'ACTIVE' if active else 'INACTIVE'} (FR-001) <<<\n"
        )


def _try_repair_missing_quote(payload_raw):
    """Known bug in the real MDR-6/NVR's train_state publisher: the last
    string field is missing its closing quote before the final '}', e.g.
    ...,"active_cab_id":"Ve1}  instead of  ...,"active_cab_id":"Ve1"}.
    Only ever applied as a fallback after a normal parse already failed, so
    it can't touch already-valid payloads (position's "altitude_m":0.00}
    style numeric endings parse fine on the first try and never reach here).
    """
    if payload_raw.endswith('}') and not payload_raw.endswith('"}'):
        repaired = payload_raw[:-1] + '"}'
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            return None
    return None


def handle_message(state, topic, payload_raw):
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        payload = _try_repair_missing_quote(payload_raw)
        if payload is None:
            print(f"[{now()}] (unparseable payload on {topic}): {payload_raw[:200]}")
            return
        print(f"[{now()}] (auto-repaired malformed JSON on {topic} - report this to CIBEST/BMA)")

    # topic shape: train/{train_id}/event/{kind}  or  train/{train_id}/health/mdr6
    parts = topic.split("/")
    if len(parts) < 3 or parts[0] != "train":
        print(f"[{now()}] (unexpected topic) {topic}: {payload_raw[:200]}")
        return
    train_id = parts[1]
    kind = "/".join(parts[2:])
    state.train_id = train_id

    changed = True
    if kind == "event/clock":
        state.last_clock = payload.get("sync_datetime")
        changed = False  # too noisy (1/s) to reprint the whole summary on every tick
    elif kind == "event/topology":
        state.station_label = payload.get("stop_label")
        state.destination_label = payload.get("destination_label")
        state.trip_id = payload.get("trip_id") or payload.get("course_id")
    elif kind == "event/train_state":
        state.in_station = payload.get("in_station")
        state.speed_kmh = payload.get("speed_kmh")
        state.cab_active = payload.get("cab_active")
        state.active_cab_id = payload.get("active_cab_id")
    elif kind == "event/position":
        state.lat = payload.get("latitude")
        state.lon = payload.get("longitude")
    elif kind == "event/alarm":
        print(f"\n[{now()}] *** ALARM *** {payload.get('alarm_type')} "
              f"on {payload.get('vehicle_zone')} ({payload.get('alarm_source_id')}) "
              f"= {'ACTIVE' if payload.get('alarm_state') else 'cleared'}\n")
        changed = False
    elif kind == "health/mdr6":
        print(f"[{now()}] MDR-6 health: {payload}")
        changed = False
    else:
        print(f"[{now()}] (unhandled topic kind '{kind}') {payload_raw[:200]}")
        changed = False

    if changed:
        state.print_summary()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=8883)
    parser.add_argument("--username", default="ia_server")
    parser.add_argument("--password", default="ia_server")
    parser.add_argument("--cafile", help="path to the broker's CA certificate")
    parser.add_argument("--insecure", action="store_true", help="skip TLS hostname/cert verification")
    parser.add_argument("--topic", default="train/+/event/#", help="topic filter to subscribe to")
    args = parser.parse_args()

    cmd = [
        "mosquitto_sub",
        "-h", args.host, "-p", str(args.port),
        "-u", args.username, "-P", args.password,
        "-t", args.topic, "-v",
    ]
    if args.cafile:
        cmd += ["--cafile", args.cafile]
    if args.insecure:
        cmd.append("--insecure")

    print(f"[{now()}] Launching: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)

    state = State()
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            if " " not in line:
                print(f"[{now()}] {line}")  # mosquitto_sub's own status lines
                continue
            topic, payload_raw = line.split(" ", 1)
            handle_message(state, topic, payload_raw)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
