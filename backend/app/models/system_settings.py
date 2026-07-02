from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class SystemSettings(Base):
    __tablename__ = "system_settings"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    timezone: Mapped[str] = mapped_column(String(64), default="")
    ntp_server: Mapped[str] = mapped_column(String(256), default="pool.ntp.org")
