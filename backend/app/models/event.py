from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[int | None] = mapped_column(ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True, index=True)
    # motion | motion_update | motion_summary | loitering | line_cross | camera_offline | camera_online
    # | low_storage | camera_error | system | ...
    type: Mapped[str] = mapped_column(String(32))
    # Text, not String(512): event summaries run to ~950 characters. (SQLite
    # never enforced the old length, so existing rows need no migration.)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    notified: Mapped[bool] = mapped_column(default=False)
    snapshot_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Structured facts behind the message (objects, zones, identities...), for
    # the UI and rules to use without parsing text.
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)  # 0 normal, 1 important, 2 urgent
    # Urgent alerts repeat until someone acknowledges them (push action, UI, HA).
    acked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    incident_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
