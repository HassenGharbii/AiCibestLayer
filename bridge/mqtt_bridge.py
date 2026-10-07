"""Production alternative to bridge.py: subscribes to the real MDR-6's MQTT
broker as the "ia_server" account, instead of reading CIP/UDP directly.

Per CDGX-VIDEO-GRP-SFD-INT-XX-XX-Interface_IX_avec_le_serveur_IA_rev02.docx
section 7: the broker runs ON the MDR-6 itself (no separate broker to stand
up), port 8883, MQTT 3.1.1 over mandatory TLS 1.2, username+password auth
(no client certs, no anonymous connections). The "ia_server" account is
subscribe-only; only "nvr" (the MDR-6) may publish.

Topics consumed (train/{train_id}/...):
  event/clock      - time sync (logged, not otherwise used)
  event/topology    - station/destination/trip context
  event/train_state - in_station, speed_kmh, cab_active, active_cab_id
  event/position     - lat/lon
  event/alarm         - TCMS BA/KSA/SOS alarm buttons (forwarded to the
                        Dashboard-CDGX backend's /api/alarms/tcms, separate
                        from Milestone XProtect camera alarms)
  health/mdr6         - MDR-6 heartbeat (logged)

Reuses bridge.py's SharedState/push_loop/make_trigger_handler/simulate_loop
— those are protocol-agnostic; only the transport (MQTT vs CIP/UDP) differs.

NOT YET VERIFIED AGAINST A REAL MDR-6 — no broker address/credentials/TLS
cert were available while building this. Field names/topics match the rev02
spec exactly, but confirm the TLS cert (probably self-signed — use --ca-cert
or --insecure for lab testing) and exact payload shapes once reachable.

Run with --simulate for the same synthetic origin->transit->destination
scenario as bridge.py, no broker needed.
"""

import argparse
import json
import ssl
import sys
import threading
import time

import requests

from bridge import SharedState, make_trigger_handler, push_loop, simulate_loop

DEFAULT_PORT = 8883
DEFAULT_USERNAME = "ia_server"  # per rev02 §7.3.2 — fixed account name


def _topology_handler(state):
    def handle(payload):
        state.update_avms(
            station_label=payload.get("stop_label"),
            destination_label=payload.get("destination_label"),
            trip_id=payload.get("course_id") or payload.get("mission_id"),
        )
    return handle


def _train_state_handler(state, on_trigger_change, last_trigger_box):
    def handle(payload):
        state.update_tcms(
            in_station=payload.get("in_station"),
            speed_kmh=payload.get("speed_kmh"),
            cab_active=payload.get("cab_active"),
            active_cab_id=payload.get("active_cab_id"),
        )
        trigger = state.counting_should_be_active()
        if trigger != last_trigger_box["value"]:
            on_trigger_change(trigger)
            last_trigger_box["value"] = trigger
    return handle


def _position_handler(state):
    def handle(payload):
        if payload.get("gps_valid"):
            state.update_tcms(lat=payload.get("latitude"), lon=payload.get("longitude"))
    return handle


def _alarm_handler(backend_url, train_id):
    def handle(payload):
        try:
            resp = requests.post(
                f"{backend_url.rstrip('/')}/api/alarms/tcms",
                json={**payload, "train_id": train_id},
                timeout=5,
            )
            resp.raise_for_status()
            print(f"TCMS alarm forwarded: {payload.get('alarm_type')} / "
                  f"{payload.get('alarm_source_id')} = {payload.get('alarm_state')}")
        except requests.RequestException as exc:
            print(f"Failed to forward TCMS alarm to backend: {exc}")
    return handle


def run_mqtt(args, state, on_trigger_change):
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("paho-mqtt not installed — `pip install -r requirements.txt`.")
        sys.exit(1)

    last_trigger_box = {"value": None}
    handlers = {
        "event/topology": _topology_handler(state),
        "event/train_state": _train_state_handler(state, on_trigger_change, last_trigger_box),
        "event/position": _position_handler(state),
        "event/alarm": _alarm_handler(args.backend_url, args.train_id),
    }

    def on_connect(client, userdata, flags, rc):
        if rc != 0:
            print(f"MQTT connect failed, rc={rc}")
            return
        print(f"Connected to MDR-6 broker at {args.mdr6_host}:{args.mdr6_port}")
        client.subscribe(f"train/{args.train_id}/event/#", qos=1)
        client.subscribe(f"train/{args.train_id}/health/mdr6", qos=1)

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"Bad payload on {msg.topic}: {exc}")
            return

        if msg.topic.endswith("/health/mdr6"):
            print(f"MDR-6 health: {payload}")
            return
        if msg.topic.endswith("/event/clock"):
            return  # sync only, nothing to do with it here

        for suffix, handler in handlers.items():
            if msg.topic.endswith(suffix):
                handler(payload)
                return
        print(f"Unhandled topic: {msg.topic}")

    def on_disconnect(client, userdata, rc):
        print(f"Disconnected from broker (rc={rc}) — paho will auto-reconnect.")

    client = mqtt.Client()
    client.username_pw_set(args.username, args.password)

    if args.no_tls:
        print("WARNING: TLS disabled (--no-tls) — plaintext, for local test brokers only, never a real MDR-6.")
    elif args.insecure:
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)
        print("WARNING: TLS certificate verification disabled (--insecure) — lab use only.")
    elif args.ca_cert:
        client.tls_set(ca_certs=args.ca_cert)
    else:
        client.tls_set()  # system CA store — will fail against a self-signed MDR-6 cert

    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect

    client.connect(args.mdr6_host, args.mdr6_port, keepalive=30)
    client.loop_forever(retry_first_connection=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tx2-url", default="http://localhost:8000", help="tx2 FastAPI base URL")
    parser.add_argument("--instance-id", required=True, help="tx2 instance id to start/stop")
    parser.add_argument("--backend-url", default="http://localhost:3000",
                         help="Dashboard-CDGX backend base URL (for TCMS alarm forwarding)")
    parser.add_argument("--train-id", help="train_id used in the MQTT topic prefix (required unless --simulate)")
    parser.add_argument("--mdr6-host", help="MDR-6 IP/hostname (the broker runs on it directly)")
    parser.add_argument("--mdr6-port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default="")
    parser.add_argument("--ca-cert", help="path to the MDR-6 broker's CA certificate for TLS verification")
    parser.add_argument("--insecure", action="store_true", help="skip TLS certificate verification (lab only)")
    parser.add_argument("--no-tls", action="store_true", help="plaintext, no TLS at all (local test broker only)")
    parser.add_argument("--simulate", action="store_true", help="synthetic scenario, no real broker needed")
    args = parser.parse_args()

    if not args.simulate and not args.train_id:
        parser.error("--train-id is required unless --simulate is set")
    if not args.simulate and not args.mdr6_host:
        parser.error("--mdr6-host is required unless --simulate is set")

    state = SharedState()
    stop_event = threading.Event()
    on_trigger_change = make_trigger_handler(args.tx2_url, args.instance_id)

    threads = [threading.Thread(target=push_loop, args=(state, args.tx2_url, stop_event), daemon=True)]

    if args.simulate:
        threads.append(threading.Thread(target=simulate_loop, args=(state, stop_event, on_trigger_change), daemon=True))
        for t in threads:
            t.start()
        print("Bridge running (simulate=True). Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop_event.set()
        return

    for t in threads:
        t.start()
    run_mqtt(args, state, on_trigger_change)  # blocks (loop_forever)


if __name__ == "__main__":
    sys.exit(main())
