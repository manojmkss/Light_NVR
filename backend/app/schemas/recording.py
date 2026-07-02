from pydantic import BaseModel

from app.schemas.common import UtcDatetime


class RecordingOut(BaseModel):
    id: int
    camera_id: int
    file_path: str
    thumbnail_path: str | None
    trigger: str
    started_at: UtcDatetime
    ended_at: UtcDatetime | None
    duration_seconds: float | None
    size_bytes: int | None

    class Config:
        from_attributes = True


class BulkDeleteRequest(BaseModel):
    ids: list[int]


class BulkDeleteResponse(BaseModel):
    deleted: int
    not_found: list[int] = []
    events_removed: int = 0
