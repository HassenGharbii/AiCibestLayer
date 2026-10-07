"""Dependency-free replacement for `supervision`/`trackers` (neither supports
Python 3.6). A simple greedy IoU tracker plus the same line-crossing /
zone-occupancy math this project used before adopting those libraries.
"""

from typing import Dict, List, Tuple

import cv2
import numpy as np

Box = Tuple[float, float, float, float]  # xyxy
TrackedObject = Tuple[int, Box, int]  # (track_id, box, class_id)
Event = Tuple[int, str]  # (track_id, "in" | "out")

PERSON_AND_VEHICLE_CLASSES = {0, 1, 2, 3, 5, 7}
VEHICLE_ONLY_CLASSES = {2, 3, 7}


def _iou(a: Box, b: Box) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def foot_point(box: Box) -> Tuple[float, float]:
    return ((box[0] + box[2]) / 2, box[3])


def _match_score(a: Box, b: Box) -> float:
    """Best of IoU and a centroid-distance score — not IoU-with-a-fallback,
    the *max* of both, since a small-but-nonzero IoU (e.g. 0.14) can still
    be a bad match score even though the boxes technically overlap a bit.
    The distance term matters at low fps, where a person can move further
    between processed frames than their own box size; without it, a big
    enough gap makes the tracker treat the same person as a brand new
    track, silently dropping the "previous point" a crossing check needs.
    """
    iou = _iou(a, b)
    ax, ay = foot_point(a)
    bx, by = foot_point(b)
    dist = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
    diag = ((a[2] - a[0]) ** 2 + (a[3] - a[1]) ** 2) ** 0.5
    dist_score = max(0.0, 1.0 - dist / (2 * diag)) if diag > 0 else 0.0
    return max(iou, dist_score)


class _Track:
    __slots__ = ("id", "box", "class_id", "missed")

    def __init__(self, track_id: int, box: Box, class_id: int):
        self.id = track_id
        self.box = box
        self.class_id = class_id
        self.missed = 0


class SimpleTracker:
    """Greedy IoU matching, frame to frame. No re-identification after a
    track is lost for more than `max_missed` frames — good enough for
    counting, not for long-term identity across occlusion.
    """

    def __init__(self, match_threshold: float = 0.2, max_missed: int = 10):
        self.match_threshold = match_threshold
        self.max_missed = max_missed
        self._tracks: Dict[int, _Track] = {}
        self._next_id = 1

    def update(self, boxes: List[Box], class_ids: List[int]) -> List[TrackedObject]:
        unmatched = set(range(len(boxes)))

        for track in self._tracks.values():
            best_score, best_i = 0.0, None
            for i in unmatched:
                score = _match_score(track.box, boxes[i])
                if score > best_score:
                    best_score, best_i = score, i
            if best_i is not None and best_score >= self.match_threshold:
                track.box = boxes[best_i]
                track.class_id = class_ids[best_i]
                track.missed = 0
                unmatched.discard(best_i)
            else:
                track.missed += 1

        for tid in [tid for tid, t in self._tracks.items() if t.missed > self.max_missed]:
            del self._tracks[tid]

        for i in unmatched:
            self._tracks[self._next_id] = _Track(self._next_id, boxes[i], class_ids[i])
            self._next_id += 1

        return [(t.id, t.box, t.class_id) for t in self._tracks.values() if t.missed == 0]


def _side(p, a, b) -> float:
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def _segments_intersect(p1, p2, p3, p4) -> bool:
    d1, d2 = _side(p1, p3, p4), _side(p2, p3, p4)
    d3, d4 = _side(p3, p1, p2), _side(p4, p1, p2)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 != 0 and d2 != 0


def _crossing_direction(prev_point, curr_point, line_p1, line_p2):
    if not _segments_intersect(prev_point, curr_point, line_p1, line_p2):
        return None
    return "in" if _side(prev_point, line_p1, line_p2) < 0 else "out"


class LineCrossingCounter:
    CLASSES = PERSON_AND_VEHICLE_CLASSES

    def __init__(self, config):
        self._p1 = (config.p1.x, config.p1.y)
        self._p2 = (config.p2.x, config.p2.y)
        self._last_points: Dict[int, Tuple[float, float]] = {}

    def update(self, tracked: List[TrackedObject]) -> List[Event]:
        events: List[Event] = []
        seen_ids = set()
        for track_id, box, _class_id in tracked:
            seen_ids.add(track_id)
            point = foot_point(box)
            prev = self._last_points.get(track_id)
            if prev is not None:
                direction = _crossing_direction(prev, point, self._p1, self._p2)
                if direction:
                    events.append((track_id, direction))
            self._last_points[track_id] = point
        # Drop points for tracks no longer visible, so a re-appearing id
        # (after the tracker's own grace period) starts fresh.
        for track_id in list(self._last_points):
            if track_id not in seen_ids:
                del self._last_points[track_id]
        return events

    def draw_shape(self, frame) -> None:
        p1 = (int(self._p1[0]), int(self._p1[1]))
        p2 = (int(self._p2[0]), int(self._p2[1]))
        cv2.line(frame, p1, p2, (0, 0, 255), 2)

    def label(self, in_total: int, out_total: int) -> str:
        return f"in {in_total}  out {out_total}  total {in_total + out_total}"


class VehicleCountingCounter(LineCrossingCounter):
    CLASSES = VEHICLE_ONLY_CLASSES


class ZoneOccupancyCounter:
    CLASSES = PERSON_AND_VEHICLE_CLASSES

    def __init__(self, config):
        self._polygon = np.array([[p.x, p.y] for p in config.points], dtype=np.float32)
        self._inside_ids = set()

    def _is_inside(self, point) -> bool:
        return cv2.pointPolygonTest(self._polygon, point, False) >= 0

    def update(self, tracked: List[TrackedObject]) -> List[Event]:
        current_ids = set()
        for track_id, box, _class_id in tracked:
            if self._is_inside(foot_point(box)):
                current_ids.add(track_id)

        entered = current_ids - self._inside_ids
        exited = self._inside_ids - current_ids
        self._inside_ids = current_ids
        return [(tid, "in") for tid in entered] + [(tid, "out") for tid in exited]

    def draw_shape(self, frame) -> None:
        cv2.polylines(frame, [self._polygon.astype(np.int32)], True, (0, 0, 255), 2)

    def label(self, in_total: int, out_total: int) -> str:
        return f"inside {in_total - out_total}   entries {in_total}  exits {out_total}"


_COUNTER_CLASSES = {
    "zone_occupancy": ZoneOccupancyCounter,
    "vehicle_counting": VehicleCountingCounter,
}


def make_counter(config):
    return _COUNTER_CLASSES.get(config.type, LineCrossingCounter)(config)
