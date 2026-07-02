from datetime import datetime

from pydantic import BaseModel


class BackupSettingsOut(BaseModel):
    enabled: bool
    interval_hours: int
    retention_count: int
    last_backup_at: datetime | None
    last_backup_filename: str | None
    last_backup_location: str | None
    last_backup_error: str | None
    last_optimize_at: datetime | None

    class Config:
        from_attributes = True


class BackupSettingsUpdate(BaseModel):
    enabled: bool | None = None
    interval_hours: int | None = None
    retention_count: int | None = None


class BackupInfoOut(BaseModel):
    filename: str
    location: str
    size_bytes: int
    created_at: datetime


class OptimizeResult(BaseModel):
    success: bool
    message: str
    duration_seconds: float
