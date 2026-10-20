from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

MODES = ("home", "away", "night", "disarmed")


class SecurityState(Base):
    """Singleton (id=1): the arm mode set by hand, the app, or Home Assistant.

    Modes change *alerting*, never recording. With no alert rules enabled the
    mode has no effect at all, so an upgrade changes nothing until the user
    opts in."""

    __tablename__ = "security_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    mode: Mapped[str] = mapped_column(String(16), default="home")
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual | api | ha | schedule
    set_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    set_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    # A manual choice normally lasts until the next scheduled change; "hold"
    # keeps it until changed by hand again.
    hold: Mapped[bool] = mapped_column(Boolean, default=False)


class ModeSchedule(Base):
    """A weekly transition: on these days at HH:MM (display timezone), switch
    to `mode`. The current scheduled mode is the latest transition at or
    before now, looking back over the week."""

    __tablename__ = "mode_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    days_mask: Mapped[int] = mapped_column(Integer, default=127)  # bit 0 = Monday ... bit 6 = Sunday
    at_hhmm: Mapped[str] = mapped_column(String(5), default="23:00")
    mode: Mapped[str] = mapped_column(String(16), default="night")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AlertRule(Base):
    """Ordered; the first enabled rule that matches an event decides what
    happens to it. Empty filters match everything."""

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="Rule")
    position: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    modes: Mapped[list | None] = mapped_column(JSON, nullable=True)  # ["away", "night"]
    camera_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    kinds: Mapped[list | None] = mapped_column(JSON, nullable=True)  # event types: motion, loitering, ...
    objects: Mapped[list | None] = mapped_column(JSON, nullable=True)  # person, car ...
    zones: Mapped[list | None] = mapped_column(JSON, nullable=True)  # named zones
    identities: Mapped[list | None] = mapped_column(JSON, nullable=True)  # known | unknown | person:<id> (faces)
    # notify | notify_high (bypasses cooldown, re-alerts until acknowledged) | silent
    action: Mapped[str] = mapped_column(String(16), default="notify")
    cooldown_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    webhook_url: Mapped[str | None] = mapped_column(String(512), nullable=True)  # e.g. a siren via HA
