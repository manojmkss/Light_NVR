from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class AlertSettings(Base):
    __tablename__ = "alert_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    motion_alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    offline_alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    low_storage_alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    low_storage_threshold_percent: Mapped[int] = mapped_column(Integer, default=10)
    motion_alert_cooldown_seconds: Mapped[int] = mapped_column(Integer, default=300)

    # Email delivery - configured entirely from Settings -> Alerts, no .env editing required
    smtp_host: Mapped[str] = mapped_column(String(255), default="")
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    smtp_username: Mapped[str] = mapped_column(String(255), default="")
    smtp_password: Mapped[str] = mapped_column(String(255), default="")
    smtp_from: Mapped[str] = mapped_column(String(255), default="")
    smtp_use_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    alert_email_to: Mapped[str] = mapped_column(String(255), default="")

    # Telegram - free, no business verification; create a bot via @BotFather
    telegram_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    telegram_bot_token: Mapped[str] = mapped_column(String(255), default="")
    telegram_chat_id: Mapped[str] = mapped_column(String(128), default="")

    # WhatsApp - official Cloud API only; requires a Meta Business app +
    # verified WhatsApp Business Account set up outside LightNVR first
    whatsapp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    whatsapp_phone_number_id: Mapped[str] = mapped_column(String(128), default="")
    whatsapp_access_token: Mapped[str] = mapped_column(String(512), default="")
    whatsapp_recipient_number: Mapped[str] = mapped_column(String(32), default="")
