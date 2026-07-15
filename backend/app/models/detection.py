from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Detection(Base):
    """One thing the AI layer recognised in one frame.

    Deliberately a single table for all four tiers rather than four parallel
    ones: they all describe "something was seen on camera X at time T", and
    each tier just fills in more of the same row -
      Tier 1 (objects)      -> label + confidence + bbox
      Tier 2 (search)       -> embedding
      Tier 3 (descriptions) -> description
      Tier 4 (face/plate)   -> label + text
    That keeps the timeline/search queries one flat scan instead of a
    four-way union, and means a later tier enriches existing rows instead of
    duplicating them.
    """

    __tablename__ = "detections"

    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    # Both nullable: a detection is produced the moment motion fires, which is
    # before any recording row exists (continuous segments only become rows
    # when the chunk closes), and recording_mode=off produces none at all.
    recording_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    label: Mapped[str] = mapped_column(String(64), index=True)  # person | car | dog | face | plate ...
    confidence: Mapped[int] = mapped_column(Integer, default=0)  # 0-100

    # Normalised 0..1 against the frame, not pixels - the substream resolution
    # varies per camera and can change when streams are re-detected, so pixel
    # coords would silently stop lining up with the frames they're drawn on.
    bbox_x: Mapped[float] = mapped_column(Float, default=0.0)
    bbox_y: Mapped[float] = mapped_column(Float, default=0.0)
    bbox_w: Mapped[float] = mapped_column(Float, default=0.0)
    bbox_h: Mapped[float] = mapped_column(Float, default=0.0)

    # Tier 4: the plate string, or the matched known-face name.
    text: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Tier 3: VLM sentence describing the scene.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Tier 2: CLIP vector, float32 little-endian. Stored as a blob because
    # SQLite has no vector type; at home-NVR volumes a brute-force cosine scan
    # over a few thousand rows is milliseconds, so a vector DB would be
    # infrastructure for a problem this project doesn't have.
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    snapshot_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )

    __table_args__ = (
        # The two queries that actually run: "what did camera X see recently"
        # and the retention sweep by age.
        Index("ix_detections_camera_created", "camera_id", "created_at"),
    )
