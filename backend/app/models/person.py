from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Person(Base):
    """Someone the household chose to have recognised ("Ramesh - milkman").

    Face recognition is opt-in, local only, and behind the privacy
    acknowledgement in Settings -> AI. Deleting a person deletes every face
    sample and embedding stored for them."""

    __tablename__ = "persons"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)  # "milkman", "daughter"...
    # Trust: household ("Family") | familiar | stranger (seen before, not
    # trusted - treated like an unknown face by alert rules) | watchlist
    # ("Suspicious" - always urgent). Drives alert rules (see notify_policy).
    category: Mapped[str] = mapped_column(String(16), default="familiar")
    # Arrival notification: always | away (only in Away mode) | never.
    notify: Mapped[str] = mapped_column(String(8), default="always")
    arrival_alert: Mapped[bool] = mapped_column(Boolean, default=True)  # kept in step with notify (older clients)
    arrival_cooldown_s: Mapped[int] = mapped_column(Integer, default=1800)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class FaceSample(Base):
    """One face crop + its embedding. person_id NULL = not identified yet
    (kept a few days so it can be named with "Who is this?")."""

    __tablename__ = "face_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"), nullable=True, index=True)
    camera_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding: Mapped[bytes] = mapped_column(LargeBinary)  # 128 x float32, L2-normalised (SFace)
    crop_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    quality: Mapped[float] = mapped_column(Float, default=0.0)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)  # best match score when seen
    track_uid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(16), default="capture")  # capture | upload
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
