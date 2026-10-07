import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from . import db
from .config import resolve_upload_path
from .pipeline import InstanceRunner
from .schemas import (
    CrossingEvent,
    Counts,
    Instance,
    InstanceCreate,
    InstanceStatus,
    SourceType,
)

MAX_EVENTS_PER_INSTANCE = 200


class _ManagedInstance:
    def __init__(self, data: Instance):
        self.data = data
        self.runner: Optional[InstanceRunner] = None
        self.events: List[CrossingEvent] = []
        self._event_seq = 0

    def next_event_id(self) -> int:
        self._event_seq += 1
        return self._event_seq


class InstanceManager:
    def __init__(self):
        self._instances: Dict[str, _ManagedInstance] = {}
        self._lock = threading.Lock()

    def create(self, req: InstanceCreate) -> Instance:
        instance_id = uuid.uuid4().hex[:12]

        source_uri = req.source.uri
        if req.source.type == SourceType.file:
            source_uri = str(resolve_upload_path(req.source.uri))

        data = Instance(
            id=instance_id,
            name=req.name,
            source=req.source,
            config=req.config,
            status=InstanceStatus.starting,
            counts=Counts(),
            created_at=datetime.now(timezone.utc),
        )
        managed = _ManagedInstance(data)

        def on_status(status: InstanceStatus, error_message: Optional[str]) -> None:
            managed.data.status = status
            managed.data.error_message = error_message

        def on_event(track_id: int, direction: str) -> None:
            managed.data.counts.total += 1
            if direction == "in":
                managed.data.counts.in_count += 1
                managed.data.counts.current += 1
            else:
                managed.data.counts.out_count += 1
                managed.data.counts.current -= 1
            timestamp = datetime.now(timezone.utc)
            event = CrossingEvent(
                id=managed.next_event_id(),
                instance_id=instance_id,
                track_id=track_id,
                direction=direction,
                timestamp=timestamp,
            )
            managed.events.append(event)
            if len(managed.events) > MAX_EVENTS_PER_INSTANCE:
                managed.events.pop(0)
            db.record_crossing_event(instance_id, track_id, direction, timestamp)

        runner = InstanceRunner(
            instance_id=instance_id,
            source_type=req.source.type,
            source_uri=source_uri,
            config=req.config,
            on_event=on_event,
            on_status=on_status,
        )
        managed.runner = runner

        with self._lock:
            self._instances[instance_id] = managed

        runner.start()
        db.record_instance(
            instance_id, req.name, req.source.type.value, source_uri,
            req.config.dict(), data.created_at,
        )
        return data

    def _get_managed(self, instance_id: str) -> Optional[_ManagedInstance]:
        with self._lock:
            return self._instances.get(instance_id)

    def start_counting(self, instance_id: str) -> Optional[Instance]:
        managed = self._get_managed(instance_id)
        if not managed or not managed.runner:
            return None
        managed.runner.counting_enabled.set()
        managed.data.counting = True
        return managed.data

    def stop_counting(self, instance_id: str) -> Optional[Instance]:
        managed = self._get_managed(instance_id)
        if not managed or not managed.runner:
            return None
        managed.runner.counting_enabled.clear()
        managed.data.counting = False
        return managed.data

    def list(self) -> List[Instance]:
        with self._lock:
            return [m.data for m in self._instances.values()]

    def get(self, instance_id: str) -> Optional[Instance]:
        with self._lock:
            managed = self._instances.get(instance_id)
        return managed.data if managed else None

    def get_events(self, instance_id: str) -> Optional[List[CrossingEvent]]:
        with self._lock:
            managed = self._instances.get(instance_id)
        return list(managed.events) if managed else None

    def get_latest_jpeg(self, instance_id: str) -> Optional[bytes]:
        with self._lock:
            managed = self._instances.get(instance_id)
        if not managed or not managed.runner:
            return None
        return managed.runner.latest_jpeg

    def delete(self, instance_id: str) -> bool:
        with self._lock:
            managed = self._instances.pop(instance_id, None)
        if not managed:
            return False
        if managed.runner:
            managed.runner.stop()
        return True


manager = InstanceManager()
