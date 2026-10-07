import base64
import os
import uuid

import cv2
from fastapi import APIRouter, File, HTTPException, UploadFile

from ..config import UPLOAD_DIR, resolve_upload_path
from ..schemas import Source, SourceType, SnapshotResponse, UploadResponse

router = APIRouter(prefix="/api/sources", tags=["sources"])


@router.post("/upload", response_model=UploadResponse)
async def upload_video(file: UploadFile = File(...)) -> UploadResponse:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ext = os.path.splitext(file.filename or "")[1] or ".mp4"
    file_id = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOAD_DIR / file_id
    with open(dest, "wb") as out:
        out.write(await file.read())
    return UploadResponse(file_id=file_id, filename=file.filename or file_id)


@router.post("/snapshot", response_model=SnapshotResponse)
def snapshot(source: Source) -> SnapshotResponse:
    if source.type == SourceType.file:
        try:
            uri = str(resolve_upload_path(source.uri))
        except ValueError:
            raise HTTPException(400, "invalid file reference")
    else:
        uri = source.uri

    cap = cv2.VideoCapture(uri)
    if not cap.isOpened():
        raise HTTPException(400, "Could not open source")
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise HTTPException(400, "Could not read a frame from source")

    height, width = frame.shape[:2]
    ok2, buf = cv2.imencode(".jpg", frame)
    if not ok2:
        raise HTTPException(500, "Failed to encode snapshot")

    encoded = base64.b64encode(buf.tobytes()).decode("ascii")
    return SnapshotResponse(image_base64=encoded, width=width, height=height)
