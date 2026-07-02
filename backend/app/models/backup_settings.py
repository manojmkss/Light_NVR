from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class BackupSettings(Base):
    """Singleton row (id=1). Mixes user-editable settings with last-run
    status, same as Camera does with config + runtime state - this is a
    config backup of the database itself (cameras, users, all settings),
    not the recorded video.
    """

    __tablename__ = "backup_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    retention_count: Mapped[int] = mapped_column(Integer, default=14)

    last_backup_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_backup_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_backup_location: Mapped[str | None] = mapped_column(String(16), nullable=True)  # primary | local
    last_backup_error: Mapped[str | None] = mapped_column(String(512), nullable=True)

    last_optimize_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
