import secrets
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def _generate_token() -> str:
    # 256 bits of entropy - this token is the only access control for the
    # public kiosk page, so it must be unguessable rather than just unique.
    return secrets.token_urlsafe(32)


class KioskView(Base):
    """A named, admin-configured view-only display (e.g. "Living room
    tablet"). The token is the sole credential for the public /kiosk/<token>
    page - anyone with the link can view exactly the cameras configured here
    and nothing else (no camera list, no settings, no other cameras).
    """

    __tablename__ = "kiosk_views"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, default=_generate_token)
    layout: Mapped[int] = mapped_column(Integer, default=4)  # cameras per page: 1 | 4 | 9 | 16
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    cameras: Mapped[list["KioskViewCamera"]] = relationship(
        "KioskViewCamera",
        cascade="all, delete-orphan",
        order_by="KioskViewCamera.position",
    )


class KioskViewCamera(Base):
    __tablename__ = "kiosk_view_cameras"

    id: Mapped[int] = mapped_column(primary_key=True)
    kiosk_view_id: Mapped[int] = mapped_column(ForeignKey("kiosk_views.id", ondelete="CASCADE"), index=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
