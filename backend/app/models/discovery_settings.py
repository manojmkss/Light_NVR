from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class DiscoverySettings(Base):
    """Singleton row (id=1). custom_subnets is a comma-separated list of
    CIDRs the admin has added because their network isn't covered by
    DEFAULT_SCAN_SUBNETS - included in every automatic fallback scan from
    then on, same as the built-in defaults.
    """

    __tablename__ = "discovery_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    custom_subnets: Mapped[str] = mapped_column(String(1024), default="")
