from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class TlsSettings(Base):
    """Singleton row (id=1). mode is informational/for renewal scheduling -
    the actual active cert/key are always plain files at /certs/cert.pem and
    /certs/key.pem regardless of which mode produced them, so nginx never
    needs to know which mode is active.
    """

    __tablename__ = "tls_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    mode: Mapped[str] = mapped_column(String(16), default="self_signed")  # self_signed | custom | letsencrypt
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_renewal_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_renewal_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
