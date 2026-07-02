from pydantic import BaseModel

StorageType = str  # "local" | "smb" | "nfs"


class StorageConfigOut(BaseModel):
    cache_path: str
    cache_max_gb: int
    default_retention_days: int
    max_storage_gb: int

    primary_type: StorageType
    primary_path: str
    primary_remote_spec: str | None
    primary_username: str | None
    # primary_password intentionally omitted - never echoed back to the client

    backup_enabled: bool
    backup_type: StorageType
    backup_path: str | None
    backup_remote_spec: str | None
    backup_username: str | None

    class Config:
        from_attributes = True


class StorageConfigUpdate(BaseModel):
    cache_max_gb: int | None = None
    default_retention_days: int | None = None
    max_storage_gb: int | None = None

    primary_type: StorageType | None = None
    primary_remote_spec: str | None = None
    primary_username: str | None = None
    primary_password: str | None = None

    backup_enabled: bool | None = None
    backup_type: StorageType | None = None
    backup_remote_spec: str | None = None
    backup_username: str | None = None
    backup_password: str | None = None


class StorageTestRequest(BaseModel):
    target: str  # "primary" | "backup"
    type: StorageType
    remote_spec: str | None = None
    username: str | None = None
    password: str | None = None


class StorageTestResult(BaseModel):
    success: bool
    message: str


class TierHealth(BaseModel):
    available: bool
    mounted: bool
    free_bytes: int | None = None
    total_bytes: int | None = None
    used_by_recordings_bytes: int | None = None
    detail: str | None = None


class StorageHealthOut(BaseModel):
    cache: TierHealth
    primary: TierHealth
    backup: TierHealth | None = None
    pending_migration_count: int
