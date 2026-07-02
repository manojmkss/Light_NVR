from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class StorageConfig(Base):
    """Singleton row (id=1) describing the storage topology: a local cache
    that recordings are always written to first, a primary destination they
    get migrated to, and an optional backup destination used when primary is
    unreachable. primary/backup *_path is the local mountpoint the share gets
    mounted at (or the direct path, for type=local); *_remote_spec is the
    network address (//server/share for smb, server:/export for nfs).
    """

    __tablename__ = "storage_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)

    cache_path: Mapped[str] = mapped_column(String(512), default="/storage/cache")
    cache_max_gb: Mapped[int] = mapped_column(Integer, default=20)

    # Global retention defaults (per-camera override lives on Camera.retention_days)
    default_retention_days: Mapped[int] = mapped_column(Integer, default=14)
    max_storage_gb: Mapped[int] = mapped_column(Integer, default=0)  # 0 = unlimited, age-based retention only

    # primary/backup _path is always a local mountpoint inside the container -
    # for type=smb/nfs the backend mounts the share there itself at runtime;
    # for type=local it must be bind-mounted to the desired drive in
    # docker-compose.yml (Docker can't attach a new host path to a running
    # container, so "local dedicated drive" still needs that one compose edit).
    primary_type: Mapped[str] = mapped_column(String(16), default="local")  # local | smb | nfs
    primary_path: Mapped[str] = mapped_column(String(512), default="/mnt/primary")
    primary_remote_spec: Mapped[str | None] = mapped_column(String(512), nullable=True)
    primary_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    primary_password: Mapped[str | None] = mapped_column(String(256), nullable=True)

    backup_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    backup_type: Mapped[str] = mapped_column(String(16), default="local")
    backup_path: Mapped[str | None] = mapped_column(String(512), default="/mnt/backup", nullable=True)
    backup_remote_spec: Mapped[str | None] = mapped_column(String(512), nullable=True)
    backup_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    backup_password: Mapped[str | None] = mapped_column(String(256), nullable=True)
