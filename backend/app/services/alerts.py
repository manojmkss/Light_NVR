import logging
import os
from datetime import datetime, timezone
from email.message import EmailMessage

import aiosmtplib

from app.db.session import AsyncSessionLocal
from app.models.alert_settings import AlertSettings
from app.models.event import Event
from app.services.telegram import send_telegram_message, send_telegram_photo
from app.services.whatsapp import send_whatsapp_image, send_whatsapp_message

logger = logging.getLogger(__name__)

_DEFAULT_COOLDOWN_SECONDS = 1800
_last_alert_at: dict[tuple[str, int | None], datetime] = {}


async def maybe_send_alert(event: Event) -> None:
    async with AsyncSessionLocal() as db:
        alert_settings = await db.get(AlertSettings, 1)
    if alert_settings is None:
        return

    cooldown = _DEFAULT_COOLDOWN_SECONDS
    if event.type == "motion":
        if not alert_settings.motion_alerts_enabled:
            return
        cooldown = alert_settings.motion_alert_cooldown_seconds
    elif event.type == "camera_offline":
        if not alert_settings.offline_alerts_enabled:
            return
    elif event.type == "low_storage":
        if not alert_settings.low_storage_alerts_enabled:
            return
    elif event.type == "camera_error":
        if not alert_settings.offline_alerts_enabled:
            return
    elif event.type == "system":
        pass  # rare and always high-value (DB corruption, backup failure, cert expiry) - no setting gates this
    else:
        return

    key = (event.type, event.camera_id)
    now = datetime.now(timezone.utc)
    last = _last_alert_at.get(key)
    if last and (now - last).total_seconds() < cooldown:
        return
    _last_alert_at[key] = now

    subject = f"LightNVR alert: {event.type.replace('_', ' ')}"
    # None once the file's confirmed missing, so every channel below skips
    # straight to its text-only path instead of each re-checking the disk.
    snapshot = event.snapshot_path if event.snapshot_path and os.path.exists(event.snapshot_path) else None

    # Each channel is independent - one being misconfigured or down never
    # blocks the others from delivering.
    if alert_settings.smtp_host and alert_settings.alert_email_to:
        success, detail = await send_email(alert_settings, subject, event.message, image_path=snapshot)
        if not success:
            logger.warning("Failed to send alert email: %s", detail)

    if alert_settings.telegram_enabled:
        if snapshot:
            success, detail = await send_telegram_photo(
                alert_settings.telegram_bot_token, alert_settings.telegram_chat_id, snapshot, f"{subject}\n{event.message}"
            )
        else:
            success, detail = await send_telegram_message(
                alert_settings.telegram_bot_token, alert_settings.telegram_chat_id, f"{subject}\n{event.message}"
            )
        if not success:
            logger.warning("Failed to send Telegram alert: %s", detail)

    if alert_settings.whatsapp_enabled:
        if snapshot:
            success, detail = await send_whatsapp_image(
                alert_settings.whatsapp_phone_number_id,
                alert_settings.whatsapp_access_token,
                alert_settings.whatsapp_recipient_number,
                snapshot,
                f"{subject}\n{event.message}",
            )
        else:
            success, detail = await send_whatsapp_message(
                alert_settings.whatsapp_phone_number_id,
                alert_settings.whatsapp_access_token,
                alert_settings.whatsapp_recipient_number,
                f"{subject}\n{event.message}",
            )
        if not success:
            logger.warning("Failed to send WhatsApp alert: %s", detail)

    await _maybe_send_push(event, subject)


async def _maybe_send_push(event: Event, subject: str) -> None:
    from app.services.web_push import send_push_to_all

    try:
        await send_push_to_all(subject, event.message)
    except Exception:
        logger.exception("Failed to dispatch push notifications")


async def send_email(
    smtp: AlertSettings, subject: str, body: str, to_override: str | None = None, image_path: str | None = None
) -> tuple[bool, str]:
    """Takes an AlertSettings-shaped object rather than reading global config
    so the Settings -> Alerts "send test email" button can verify unsaved
    form values before the user commits to them.
    """
    to_address = to_override or smtp.alert_email_to
    if not smtp.smtp_host or not to_address:
        return False, "SMTP host and recipient address are required"

    message = EmailMessage()
    message["From"] = smtp.smtp_from or smtp.smtp_username
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)
    if image_path:
        try:
            with open(image_path, "rb") as f:
                message.add_attachment(f.read(), maintype="image", subtype="jpeg", filename="snapshot.jpg")
        except OSError:
            logger.warning("Could not attach snapshot %s to alert email", image_path)

    try:
        await aiosmtplib.send(
            message,
            hostname=smtp.smtp_host,
            port=smtp.smtp_port,
            username=smtp.smtp_username or None,
            password=smtp.smtp_password or None,
            start_tls=smtp.smtp_use_tls,
        )
        return True, "Sent"
    except Exception as exc:
        return False, str(exc)
