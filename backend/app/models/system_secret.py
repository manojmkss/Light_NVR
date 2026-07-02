from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class SystemSecret(Base):
    """Singleton row (id=1) holding generated-once, persisted-forever secrets
    - the JWT signing secret and the VAPID keypair used to sign Web Push
    messages - so the user never has to invent or manage these manually.
    """

    __tablename__ = "system_secret"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    # Nullable: this row can be created by whichever of ensure_jwt_secret /
    # ensure_vapid_keys runs first and needs somewhere to persist its value -
    # e.g. if JWT_SECRET_KEY is set via env var, ensure_jwt_secret never
    # creates this row at all, so ensure_vapid_keys must be able to create it
    # without a jwt_secret to fill in.
    jwt_secret: Mapped[str | None] = mapped_column(String(128), nullable=True)
    vapid_public_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    vapid_private_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
