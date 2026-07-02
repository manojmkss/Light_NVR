from pydantic import BaseModel

from app.schemas.common import UtcDatetime


class SystemStatusOut(BaseModel):
    cpu_percent: float
    memory_percent: float
    memory_used_bytes: int
    memory_total_bytes: int
    storage_used_bytes: int
    storage_total_bytes: int
    storage_free_bytes: int
    cameras_total: int
    cameras_online: int
    cameras_offline: int
    active_workers: int
    uptime_seconds: float


class EventOut(BaseModel):
    id: int
    camera_id: int | None
    type: str
    message: str
    created_at: UtcDatetime

    class Config:
        from_attributes = True


class AlertSettingsOut(BaseModel):
    motion_alerts_enabled: bool
    offline_alerts_enabled: bool
    low_storage_alerts_enabled: bool
    low_storage_threshold_percent: int
    motion_alert_cooldown_seconds: int

    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_from: str
    smtp_use_tls: bool
    alert_email_to: str
    # smtp_password/whatsapp_access_token/telegram_bot_token intentionally
    # omitted - secrets are never echoed back to the client

    telegram_enabled: bool
    telegram_chat_id: str

    whatsapp_enabled: bool
    whatsapp_phone_number_id: str
    whatsapp_recipient_number: str

    class Config:
        from_attributes = True


class AlertSettingsUpdate(BaseModel):
    motion_alerts_enabled: bool | None = None
    offline_alerts_enabled: bool | None = None
    low_storage_alerts_enabled: bool | None = None
    low_storage_threshold_percent: int | None = None
    motion_alert_cooldown_seconds: int | None = None

    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_use_tls: bool | None = None
    alert_email_to: str | None = None

    telegram_enabled: bool | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    whatsapp_enabled: bool | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_access_token: str | None = None
    whatsapp_recipient_number: str | None = None


class TestEmailRequest(BaseModel):
    smtp_host: str
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_tls: bool = True
    alert_email_to: str


class TestTelegramRequest(BaseModel):
    bot_token: str
    chat_id: str


class TestWhatsAppRequest(BaseModel):
    phone_number_id: str
    access_token: str
    recipient_number: str


class TestMessageResult(BaseModel):
    success: bool
    message: str


class SystemTimeOut(BaseModel):
    server_utc: str
    server_timestamp: float


class SystemSettingsOut(BaseModel):
    timezone: str
    ntp_server: str

    class Config:
        from_attributes = True


class SystemSettingsUpdate(BaseModel):
    timezone: str | None = None
    ntp_server: str | None = None


class DashboardOut(BaseModel):
    # Today's activity counts (bucketed in the configured display timezone)
    motion_events_today: int
    recording_failures_today: int
    cameras_offline: int
    recordings_today: int
    # AI object detection is not implemented; these stay 0 and the UI shows
    # an "AI not enabled" tag rather than fabricating numbers.
    person_detections_today: int = 0
    vehicle_detections_today: int = 0
    ai_enabled: bool = False
    # Weekly event heatmap: 7 rows (Mon..Sun) x 24 hourly columns, event counts
    heatmap: list[list[int]]
    # Storage projection from recent recording growth
    storage_days_to_full: float | None = None
    storage_full_date: str | None = None
