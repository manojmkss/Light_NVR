from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class RemoteAccessSettings(Base):
    """Singleton row (id=1). Tailscale and Cloudflare Tunnel both run as
    subprocesses managed by this backend process (not separate containers),
    so an auth key/token pasted into Settings -> Remote Access takes effect
    immediately with no docker-compose/.env editing or restart required.
    """

    __tablename__ = "remote_access_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)

    tailscale_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    tailscale_authkey: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tailscale_hostname: Mapped[str] = mapped_column(String(64), default="lightnvr")

    cloudflare_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    cloudflare_token: Mapped[str | None] = mapped_column(String(2048), nullable=True)
