from datetime import datetime
from enum import Enum
from typing import List, Optional, Union

from pydantic import BaseModel, Field
from typing_extensions import Literal


class SourceType(str, Enum):
    rtsp = "rtsp"
    file = "file"


class Source(BaseModel):
    type: SourceType
    uri: str  # rtsp:// URL, or the file_id returned by /api/sources/upload


class Point(BaseModel):
    x: float
    y: float


class LineCrossingConfig(BaseModel):
    type: Literal["line_crossing"] = "line_crossing"
    p1: Point
    p2: Point
    frame_width: int
    frame_height: int


class ZoneOccupancyConfig(BaseModel):
    type: Literal["zone_occupancy"] = "zone_occupancy"
    points: List[Point] = Field(..., min_items=3)
    frame_width: int
    frame_height: int


class VehicleCountingConfig(BaseModel):
    type: Literal["vehicle_counting"] = "vehicle_counting"
    p1: Point
    p2: Point
    frame_width: int
    frame_height: int


InstanceConfig = Union[LineCrossingConfig, ZoneOccupancyConfig, VehicleCountingConfig]


class InstanceStatus(str, Enum):
    starting = "starting"
    running = "running"
    stopped = "stopped"
    error = "error"


class InstanceCreate(BaseModel):
    name: str
    source: Source
    config: InstanceConfig = Field(..., discriminator="type")


class Counts(BaseModel):
    in_count: int = 0
    out_count: int = 0
    total: int = 0
    current: int = 0


class Instance(BaseModel):
    id: str
    name: str
    source: Source
    config: InstanceConfig = Field(..., discriminator="type")
    status: InstanceStatus
    counting: bool = False
    counts: Counts
    error_message: Optional[str] = None
    created_at: datetime


class CrossingEvent(BaseModel):
    id: int
    instance_id: str
    track_id: int
    direction: Literal["in", "out"]
    timestamp: datetime


class SnapshotResponse(BaseModel):
    image_base64: str
    width: int
    height: int


class UploadResponse(BaseModel):
    file_id: str
    filename: str


class TrainState(BaseModel):
    timestamp: datetime
    lat: Optional[float] = None
    lon: Optional[float] = None
    speed_kmh: Optional[float] = None
    in_station: Optional[bool] = None
    cab_active: Optional[bool] = None
    active_cab_id: Optional[str] = None
    station_label: Optional[str] = None
    destination_label: Optional[str] = None
    trip_id: Optional[str] = None
    trainset_num: Optional[str] = None
