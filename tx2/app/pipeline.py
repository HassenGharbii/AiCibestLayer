import threading
import time
from typing import Callable, Optional

import cv2
import numpy as np

from .schemas import InstanceConfig, InstanceStatus, SourceType
from .tracker import SimpleTracker, make_counter

OnEvent = Callable[[int, str], None]
OnStatus = Callable[[InstanceStatus, Optional[str]], None]


def _encode_overlay(frame, counter, tracked, label: str) -> Optional[bytes]:
    counter.draw_shape(frame)

    for track_id, box, _class_id in tracked:
        x1, y1, x2, y2 = (int(v) for v in box)
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 200, 0), 2)
        cv2.putText(
            frame, f"#{track_id}", (x1, max(y1 - 6, 0)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 0), 2,
        )
        foot = ((x1 + x2) // 2, y2)
        cv2.circle(frame, foot, 4, (0, 255, 255), -1)

    cv2.rectangle(frame, (0, 0), (12 + 11 * len(label), 30), (0, 0, 0), -1)
    cv2.putText(frame, label, (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

    ok, buf = cv2.imencode(".jpg", frame)
    return buf.tobytes() if ok else None


class InstanceRunner:
    def __init__(
        self,
        instance_id: str,
        source_type: SourceType,
        source_uri: str,
        config: InstanceConfig,
        on_event: OnEvent,
        on_status: OnStatus,
    ):
        self.instance_id = instance_id
        self.source_type = source_type
        self.source_uri = source_uri
        self.config = config
        self.on_event = on_event
        self.on_status = on_status
        self._stop_event = threading.Event()
        self.counting_enabled = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.latest_jpeg: Optional[bytes] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        # Imported lazily so the API can start up without waiting on torch import.
        from .detector import Detector

        loop_file = self.source_type == SourceType.file
        counter = make_counter(self.config)
        tracker = SimpleTracker()
        in_total = 0
        out_total = 0

        cap = None
        try:
            detector = Detector()
            cap = cv2.VideoCapture(self.source_uri)
            if not cap.isOpened():
                self.on_status(InstanceStatus.error, "failed to open source")
                return

            self.on_status(InstanceStatus.running, None)

            while not self._stop_event.is_set():
                ok, frame = cap.read()
                if not ok:
                    if loop_file:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    time.sleep(1.0)
                    cap.release()
                    cap = cv2.VideoCapture(self.source_uri)
                    continue

                xyxy, _confidence, class_id = detector.detect(frame)
                keep = np.isin(class_id, list(counter.CLASSES))
                boxes = [tuple(b) for b in xyxy[keep]]
                classes = [int(c) for c in class_id[keep]]

                tracked = tracker.update(boxes, classes)

                # counter.update() always runs so its internal crossing-line
                # state stays current; only emit/count while enabled, so
                # resuming doesn't produce a burst of stale crossings.
                crossings = counter.update(tracked)
                if self.counting_enabled.is_set():
                    for track_id, direction in crossings:
                        if direction == "in":
                            in_total += 1
                        else:
                            out_total += 1
                        self.on_event(track_id, direction)

                label = counter.label(in_total, out_total)
                self.latest_jpeg = _encode_overlay(frame, counter, tracked, label)
        except Exception as exc:  # noqa: BLE001 - surface any failure as instance error state
            self.on_status(InstanceStatus.error, str(exc))
        else:
            self.on_status(InstanceStatus.stopped, None)
        finally:
            if cap is not None:
                cap.release()
