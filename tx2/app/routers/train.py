import threading
from typing import Optional

from fastapi import APIRouter, HTTPException

from .. import db
from ..schemas import TrainState

router = APIRouter(prefix="/api/train-state", tags=["train"])

_lock = threading.Lock()
_latest: Optional[TrainState] = None


@router.post("", response_model=TrainState)
def push_train_state(state: TrainState) -> TrainState:
    global _latest
    with _lock:
        _latest = state
    db.record_train_state(state.dict())
    return state


@router.get("", response_model=TrainState)
def get_train_state() -> TrainState:
    with _lock:
        if _latest is None:
            raise HTTPException(404, "no train state received yet")
        return _latest
