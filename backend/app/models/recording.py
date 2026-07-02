from datetime import datetime, timezone

from sqlalchemy import String, Integer, DateTime, ForeignKey, Float
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    file_path: Mapped[str] = mapped_column(String(512))
    thumbnail_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    storage_tier: Mapped[str] = mapped_column(String(16), default="cache")  # cache | primary | backup
    trigger: Mapped[str] = mapped_column(String(16), default="continuous")  # continuous | motion
    started_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
