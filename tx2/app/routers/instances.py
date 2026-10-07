import time
from typing import Generator, List

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..manager import manager
from ..schemas import CrossingEvent, Instance, InstanceCreate

router = APIRouter(prefix="/api/instances", tags=["instances"])

STREAM_INTERVAL_SECONDS = 0.1  # lower fps cap than the main backend — this board has less to give


@router.post("", response_model=Instance)
def create_instance(req: InstanceCreate) -> Instance:
    return manager.create(req)


@router.get("", response_model=List[Instance])
def list_instances() -> List[Instance]:
    return manager.list()


@router.get("/{instance_id}", response_model=Instance)
def get_instance(instance_id: str) -> Instance:
    instance = manager.get(instance_id)
    if not instance:
        raise HTTPException(404, "instance not found")
    return instance


@router.get("/{instance_id}/events", response_model=List[CrossingEvent])
def get_events(instance_id: str) -> List[CrossingEvent]:
    events = manager.get_events(instance_id)
    if events is None:
        raise HTTPException(404, "instance not found")
    return events


@router.delete("/{instance_id}")
def delete_instance(instance_id: str) -> dict:
    if not manager.delete(instance_id):
        raise HTTPException(404, "instance not found")
    return {"status": "deleted"}


@router.post("/{instance_id}/start", response_model=Instance)
def start_counting(instance_id: str) -> Instance:
    instance = manager.start_counting(instance_id)
    if not instance:
        raise HTTPException(404, "instance not found")
    return instance


@router.post("/{instance_id}/stop", response_model=Instance)
def stop_counting(instance_id: str) -> Instance:
    instance = manager.stop_counting(instance_id)
    if not instance:
        raise HTTPException(404, "instance not found")
    return instance


def _mjpeg_frames(instance_id: str) -> Generator[bytes, None, None]:
    while manager.get(instance_id) is not None:
        frame = manager.get_latest_jpeg(instance_id)
        if frame is not None:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            )
        time.sleep(STREAM_INTERVAL_SECONDS)


@router.get("/{instance_id}/stream")
def stream_instance(instance_id: str) -> StreamingResponse:
    if manager.get(instance_id) is None:
        raise HTTPException(404, "instance not found")
    return StreamingResponse(
        _mjpeg_frames(instance_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )
