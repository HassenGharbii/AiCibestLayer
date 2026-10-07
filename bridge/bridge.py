"""Bridges the CDGX simulator's live train state to the tx2 counter.

Two data sources on the simulator's target device (default 172.24.1.53):
  1. TCMS byte buffer over CIP (EtherNet/IP explicit messaging) — speed,
     cab-active, GPS, trainset number. Byte offsets are read off the
     simulator's "Train Info & Composition" tab.
  2. AVMS text messages over UDP :50001 — station/topology/trip context
     (DS=clock, GS=GNSS, ES=topology, FS=trip).

It computes `in_station && cab_active` (FR-001) and calls tx2's
/api/instances/{id}/start or /stop on each change, and continuously pushes
the merged state to tx2's /api/train-state for the dashboard.

NOT YET VERIFIED AGAINST THE REAL DEVICE:
  - The CIP assembly instance/attribute numbers below (ASSEMBLY_INSTANCE,
    ASSEMBLY_ATTRIBUTE) are a starting guess (a generic Assembly object,
    Get_Attribute_Single on attribute 3). Confirm against the real target
    once reachable — see the "Open questions" section of the integration doc.
  - Byte order for multi-byte fields (assumed little-endian below).
  - The AVMS text field mapping (route/station/platform order) — inferred
    from two examples in the GUI, not a written spec.
  - UDP sniffing requires this script to see the wire (same host as the
    simulator, or a mirrored switch port) and a packet-capture driver
    (Npcap on Windows, raw socket permission on Linux).

Run with --simulate to exercise the whole pipeline (this script -> tx2
start/stop -> train-state dashboard) without any of the above, e.g. while
network access to the simulator isn't available yet.
"""

import argparse
import struct
import sys
import threading
import time
from datetime import datetime, timezone

import requests

TARGET_IP_DEFAULT = "172.24.1.53"
UDP_PORT = 50001
ASSEMBLY_INSTANCE_DEFAULT = 100  # UNVERIFIED — confirm against the real device
ASSEMBLY_ATTRIBUTE = 3  # CIP Assembly object "Data" attribute
ASSEMBLY_READ_SIZE = 40  # bytes; covers offset 35 (cab) plus slack

POLL_INTERVAL_SECONDS = 1.0
PUSH_INTERVAL_SECONDS = 1.0
IN_STATION_SPEED_THRESHOLD_KMH = 0.5

# Offsets into the TCMS buffer, as labelled in the simulator's Train Info tab.
OFF_TCMS_ALIVE = 0
OFF_LAT = 7
OFF_LON = 11
OFF_ALT = 15
OFF_SPEED = 17
OFF_TRAINSET_NUM = 19
OFF_CAB_ACTIVE = 35


def _now_utc():
    return datetime.now(timezone.utc)


class SharedState:
    def __init__(self):
        self._lock = threading.Lock()
        self.lat = None
        self.lon = None
        self.speed_kmh = None
        self.cab_active = None
        self.active_cab_id = None
        self.trainset_num = None
        self.station_label = None
        self.destination_label = None
        self.trip_id = None
        # MQTT's train_state topic gives an authoritative in_station boolean
        # directly; CIP only gives speed, so we derive it. None = not set,
        # fall back to the speed-derived value below.
        self._in_station_override = None

    @property
    def in_station(self):
        if self._in_station_override is not None:
            return self._in_station_override
        if self.speed_kmh is None:
            return None
        return self.speed_kmh < IN_STATION_SPEED_THRESHOLD_KMH

    def _apply_fields(self, fields):
        for key, value in fields.items():
            if key == "in_station":
                self._in_station_override = value
            else:
                setattr(self, key, value)

    def update_tcms(self, **fields):
        with self._lock:
            self._apply_fields(fields)

    def update_avms(self, **fields):
        with self._lock:
            self._apply_fields(fields)

    def snapshot(self):
        with self._lock:
            return {
                "timestamp": _now_utc().isoformat(),
                "lat": self.lat,
                "lon": self.lon,
                "speed_kmh": self.speed_kmh,
                "in_station": self.in_station,
                "cab_active": self.cab_active,
                "active_cab_id": self.active_cab_id,
                "station_label": self.station_label,
                "destination_label": self.destination_label,
                "trip_id": self.trip_id,
                "trainset_num": self.trainset_num,
            }

    def counting_should_be_active(self):
        return bool(self.in_station) and bool(self.cab_active)


def decode_cab_byte(value):
    """2 = Ve1 activated (bit 1), 4 = Ve2 activated (bit 2), per the
    simulator's dropdown labels. 0/other = no cab active."""
    if value & 0b10:
        return True, "Ve1"
    if value & 0b100:
        return True, "Ve2"
    return False, "none"


def parse_tcms_buffer(buf, byte_order="<"):
    fields = {}
    if len(buf) > OFF_LAT + 4:
        fields["lat"] = struct.unpack_from(byte_order + "f", buf, OFF_LAT)[0]
    if len(buf) > OFF_LON + 4:
        fields["lon"] = struct.unpack_from(byte_order + "f", buf, OFF_LON)[0]
    if len(buf) > OFF_SPEED + 2:
        fields["speed_kmh"] = struct.unpack_from(byte_order + "H", buf, OFF_SPEED)[0]
    if len(buf) > OFF_TRAINSET_NUM + 2:
        fields["trainset_num"] = str(
            struct.unpack_from(byte_order + "H", buf, OFF_TRAINSET_NUM)[0]
        )
    if len(buf) > OFF_CAB_ACTIVE:
        cab_active, active_cab_id = decode_cab_byte(buf[OFF_CAB_ACTIVE])
        fields["cab_active"] = cab_active
        fields["active_cab_id"] = active_cab_id
    return fields


def cip_poll_loop(state, target_ip, assembly_instance, byte_order, stop_event, on_trigger_change):
    try:
        from pycomm3 import CIPDriver, Services
    except ImportError:
        print("pycomm3 not installed — CIP polling disabled. `pip install -r requirements.txt`.")
        return

    last_trigger = None
    while not stop_event.is_set():
        try:
            with CIPDriver(target_ip) as drv:
                response = drv.generic_message(
                    service=Services.get_attribute_single,
                    class_code=0x04,
                    instance=assembly_instance,
                    attribute=ASSEMBLY_ATTRIBUTE,
                    data_type=bytes,
                )
            if response and response.value:
                fields = parse_tcms_buffer(response.value, byte_order=byte_order)
                state.update_tcms(**fields)
                trigger = state.counting_should_be_active()
                if trigger != last_trigger:
                    on_trigger_change(trigger)
                    last_trigger = trigger
            else:
                print(f"CIP read failed: {response.error if response else 'no response'}")
        except Exception as exc:  # noqa: BLE001 - keep polling despite transient errors
            print(f"CIP poll error: {exc}")
        stop_event.wait(POLL_INTERVAL_SECONDS)


def _parse_nmea_ish_latlon(text):
    """Best-effort parse of the simulator's GS payload, e.g.
    'GSA4852.620,N00221.546,E' -> ddmm.mmm lat/lon with hemisphere letters."""
    import re

    m = re.search(r"(\d{4}\.\d+),([NS]),?(\d{5}\.\d+),([EW])", text)
    if not m:
        return None, None
    lat_raw, lat_hem, lon_raw, lon_hem = m.groups()
    lat = int(lat_raw[:2]) + float(lat_raw[2:]) / 60
    lon = int(lon_raw[:3]) + float(lon_raw[3:]) / 60
    if lat_hem == "S":
        lat = -lat
    if lon_hem == "W":
        lon = -lon
    return lat, lon


def _handle_avms_payload(state, payload):
    if len(payload) < 2:
        return
    prefix, body = payload[:2], payload[2:]
    if prefix == "GS" and body:
        lat, lon = _parse_nmea_ish_latlon(payload)
        if lat is not None:
            state.update_avms(lat=lat, lon=lon)
    elif prefix == "ES" and body:
        lines = [line for line in body.replace("\r", "").split("\n") if line]
        # Field order inferred from examples, not a written spec — verify.
        if len(lines) >= 2:
            state.update_avms(destination_label=lines[1] if len(lines) > 1 else None,
                               station_label=lines[-1])
    elif prefix == "FS" and body:
        lines = [line for line in body.replace("\r", "").split("\n") if line]
        if lines:
            state.update_avms(trip_id=lines[0])


def udp_sniff_loop(state, target_ip, stop_event):
    try:
        from scapy.all import sniff, UDP, IP
    except ImportError:
        print("scapy not installed — AVMS/UDP sniffing disabled. `pip install -r requirements.txt`.")
        return

    def _on_packet(pkt):
        if stop_event.is_set():
            raise KeyboardInterrupt  # unwind sniff()
        if not (pkt.haslayer(UDP) and pkt.haslayer(IP)):
            return
        if pkt[UDP].sport != UDP_PORT and pkt[UDP].dport != UDP_PORT:
            return
        if target_ip not in (pkt[IP].src, pkt[IP].dst):
            return
        try:
            payload = bytes(pkt[UDP].payload).decode("ascii", errors="ignore")
        except Exception:  # noqa: BLE001
            return
        if len(payload) <= 3:
            return  # short acks like "GS\x00"
        _handle_avms_payload(state, payload)

    try:
        sniff(filter=f"udp port {UDP_PORT}", prn=_on_packet, store=False)
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"UDP sniff error (needs packet-capture privileges/driver): {exc}")


def simulate_loop(state, stop_event, on_trigger_change):
    """Cycles through origin -> transit -> destination -> origin, so the rest
    of the pipeline (tx2 start/stop, train-state dashboard) can be exercised
    without reaching the real simulator or target device."""
    scenario = [
        # (duration_s, speed_kmh, cab_active, station_label, destination_label)
        (10, 0.0, True, "Gare de l'Est", "Aeroport Paris-Charles de Gaulle"),
        (15, 200.0, True, "En transit", "Aeroport Paris-Charles de Gaulle"),
        (10, 0.0, True, "Aeroport Paris-Charles de Gaulle", "Gare de l'Est"),
    ]
    last_trigger = None
    print("Running in --simulate mode: no real network calls to the simulator.")
    while not stop_event.is_set():
        for duration, speed, cab_active, station, destination in scenario:
            if stop_event.is_set():
                return
            state.update_tcms(
                lat=48.877, lon=2.3591, speed_kmh=speed,
                cab_active=cab_active, active_cab_id="Ve1" if cab_active else "none",
                trainset_num="6006",
            )
            state.update_avms(station_label=station, destination_label=destination, trip_id="SIM-001")
            trigger = state.counting_should_be_active()
            if trigger != last_trigger:
                on_trigger_change(trigger)
                last_trigger = trigger
            stop_event.wait(duration)


def push_loop(state, tx2_base_url, stop_event):
    url = f"{tx2_base_url.rstrip('/')}/api/train-state"
    while not stop_event.is_set():
        try:
            requests.post(url, json=state.snapshot(), timeout=3)
        except requests.RequestException as exc:
            print(f"train-state push failed: {exc}")
        stop_event.wait(PUSH_INTERVAL_SECONDS)


def make_trigger_handler(tx2_base_url, instance_id):
    def handler(active):
        action = "start" if active else "stop"
        url = f"{tx2_base_url.rstrip('/')}/api/instances/{instance_id}/{action}"
        try:
            resp = requests.post(url, timeout=3)
            resp.raise_for_status()
            print(f"tx2 counting {'STARTED' if active else 'STOPPED'} (in_station && cab_active = {active})")
        except requests.RequestException as exc:
            print(f"failed to {action} tx2 counting: {exc}")

    return handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tx2-url", default="http://localhost:8000", help="tx2 FastAPI base URL")
    parser.add_argument("--instance-id", required=True, help="tx2 instance id to start/stop")
    parser.add_argument("--target-ip", default=TARGET_IP_DEFAULT, help="simulator's target device IP")
    parser.add_argument("--assembly-instance", type=int, default=ASSEMBLY_INSTANCE_DEFAULT)
    parser.add_argument("--byte-order", choices=["<", ">"], default="<", help="little/big-endian for CIP fields")
    parser.add_argument("--no-udp", action="store_true", help="disable AVMS/UDP sniffing")
    parser.add_argument("--simulate", action="store_true", help="synthetic scenario, no real network calls")
    args = parser.parse_args()

    state = SharedState()
    stop_event = threading.Event()
    on_trigger_change = make_trigger_handler(args.tx2_url, args.instance_id)

    threads = [threading.Thread(target=push_loop, args=(state, args.tx2_url, stop_event), daemon=True)]

    if args.simulate:
        threads.append(threading.Thread(target=simulate_loop, args=(state, stop_event, on_trigger_change), daemon=True))
    else:
        threads.append(threading.Thread(
            target=cip_poll_loop,
            args=(state, args.target_ip, args.assembly_instance, args.byte_order, stop_event, on_trigger_change),
            daemon=True,
        ))
        if not args.no_udp:
            threads.append(threading.Thread(target=udp_sniff_loop, args=(state, args.target_ip, stop_event), daemon=True))

    for t in threads:
        t.start()

    print(f"Bridge running (simulate={args.simulate}). Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")
        stop_event.set()
        for t in threads:
            t.join(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
