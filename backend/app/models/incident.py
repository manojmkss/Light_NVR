from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class AreaLink(Base):
    """Two areas a person can walk between, and how long that takes - e.g.
    Gate -> Driveway in 5-40 seconds. Used to tie events on different cameras
    into one visit. Areas are just the names given to cameras."""

    __tablename__ = "area_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    area_a: Mapped[str] = mapped_column(String(64))
    area_b: Mapped[str] = mapped_column(String(64))
    min_seconds: Mapped[int] = mapped_column(Integer, default=0)
    max_seconds: Mapped[int] = mapped_column(Integer, default=60)


class Incident(Base):
    """One visit: motion events on one or more cameras that belong together."""

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | closed
    camera_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    areas_path: Mapped[list | None] = mapped_column(JSON, nullable=True)  # areas in the order they were visited
    counts: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {"person": 1, "car": 1}
    identities: Mapped[list | None] = mapped_column(JSON, nullable=True)  # names of recognised people
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mode: Mapped[str | None] = mapped_column(String(16), nullable=True)  # security mode when it started
    # Filled by the visit review (services/visit_review.py), which goes over
    # the main-stream recording after the visit, when the system is quiet:
    # [{"kind": known|probable|stranger|unseen, "name", "camera_id", "at", "photo"}]
    people: Mapped[list | None] = mapped_column(JSON, nullable=True)
    strangers: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[str | None] = mapped_column(String(16), nullable=True)  # pending | done | skipped | failed
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
