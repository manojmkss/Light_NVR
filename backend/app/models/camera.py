from datetime import datetime, timezone

from sqlalchemy import JSON, String, Integer, Boolean, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Camera(Base):
    __tablename__ = "cameras"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    onvif_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rtsp_main_url: Mapped[str] = mapped_column(String(512))
    rtsp_sub_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    password: Mapped[str | None] = mapped_column(String(128), nullable=True)
    codec: Mapped[str] = mapped_column(String(16), default="h264")  # h264 | h265
    has_audio: Mapped[bool] = mapped_column(Boolean, default=False)

    recording_mode: Mapped[str] = mapped_column(String(16), default="continuous")  # continuous | motion | off
    motion_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    motion_sensitivity: Mapped[int] = mapped_column(Integer, default=50)  # 0-100
    # Optional motion regions, as [{"kind": "include"|"exclude", "points":
    # [[x,y]...]}] with x/y normalized 0-1. exclude = ignore motion there
    # (a waving tree, a busy road); include = watch only there. None/empty =
    # the whole frame is watched, i.e. the previous behaviour.
    motion_zones: Mapped[list | None] = mapped_column(JSON, nullable=True)
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)  # overrides global retention when set

    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False)  # pinned to the dashboard strip
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # online | offline | unknown
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Why the camera is offline, shown on the Cameras page so the user can fix
    # it without reading logs. Credential-scrubbed at write time; cleared on
    # the next successful connect.
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Stable per-device identity (ONVIF SerialNumber), captured at add/redetect
    # time. Lets the self-healing relocator recognise this exact camera at a
    # new IP after a DHCP change and update its address automatically.
    hardware_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
