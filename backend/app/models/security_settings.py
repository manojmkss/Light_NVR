from sqlalchemy import Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class SecuritySettings(Base):
    """Singleton row (id=1). Most users never touch this, but it's exposed
    in Settings -> Account so literally nothing requires editing .env.
    """

    __tablename__ = "security_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    access_token_expire_minutes: Mapped[int] = mapped_column(Integer, default=60)
    refresh_token_expire_days: Mapped[int] = mapped_column(Integer, default=7)
