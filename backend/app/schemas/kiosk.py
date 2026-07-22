from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import UtcDatetime


class KioskViewCreate(BaseModel):
    name: str
    layout: int = 4
    camera_ids: list[int] = []


class KioskViewUpdate(BaseModel):
    name: str | None = None
    layout: int | None = None
    camera_ids: list[int] | None = None


class KioskViewOut(BaseModel):
    id: int
    name: str
    token: str
    layout: int
    camera_ids: list[int]
    created_at: datetime
    last_viewed_at: datetime | None

    class Config:
        from_attributes = True


class KioskCameraOut(BaseModel):
    id: int
    name: str
    status: str
    recording_mode: str


class KioskPublicOut(BaseModel):
    name: str
    layout: int
    cameras: list[KioskCameraOut]


# Deliberately narrower than RecordingOut - omits file_path (a server
# filesystem path) since this is returned to anonymous, unauthenticated
# viewers of a kiosk link.
class KioskRecordingOut(BaseModel):
    id: int
    camera_id: int
    trigger: str
    codec: str | None = None
    started_at: UtcDatetime
    ended_at: UtcDatetime | None
    duration_seconds: float | None

    class Config:
        from_attributes = True
