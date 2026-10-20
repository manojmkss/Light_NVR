import asyncio
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
_SUMMARY_COOLDOWN_SECONDS = 60
_last_alert_at: dict[tuple[str, int | None], datetime] = {}


_SUBJECTS = {
    "motion_summary": "LightNVR: motion event summary",
    "motion_update": "LightNVR alert: another person arrived",
    "loitering": "LightNVR alert: loitering",
    "line_cross": "LightNVR alert: line crossed",
    "mode_changed": "LightNVR: security mode changed",
    "tamper": "LightNVR alert: camera tampering",
    "arrival": "LightNVR: person arrived",
    "incident_summary": "LightNVR: visit summary",
    "package": "LightNVR: package",
}
# Urgent alerts not acknowledged within this long are sent again (once).
ESCALATE_AFTER_SECONDS = 120
_escalations: dict = {}


async def maybe_send_alert(event: Event, escalation: bool = False) -> None:
    async with AsyncSessionLocal() as db:
        alert_settings = await db.get(AlertSettings, 1)
    if alert_settings is None:
        return

    cooldown = _DEFAULT_COOLDOWN_SECONDS
    if event.type == "motion":
        if not alert_settings.motion_alerts_enabled:
            return
        cooldown = alert_settings.motion_alert_cooldown_seconds
    elif event.type in ("motion_summary", "incident_summary"):
        # One per motion event, sent when it ends - deliberately not under the
        # motion cooldown (that's what keeps the start alerts quiet), just a
        # short floor so a flapping scene can't produce a stream of them.
        if not alert_settings.motion_alerts_enabled:
            return
        cooldown = _SUMMARY_COOLDOWN_SECONDS
    elif event.type == "tamper":
        # Camera health, like "offline": its own switch, short cooldown.
        if not alert_settings.offline_alerts_enabled:
            return
        cooldown = _SUMMARY_COOLDOWN_SECONDS
    elif event.type in ("motion_update", "loitering", "line_cross", "arrival", "incident", "package"):
        # "Another person arrived", or a zone rule the user drew on purpose -
        # each with its own short cooldown, so the motion cooldown can't hide
        # a second visitor or a loiterer.
        if not alert_settings.motion_alerts_enabled:
            return
        cooldown = _SUMMARY_COOLDOWN_SECONDS
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

    # Arm modes + alert rules (no enabled rules = exactly the behaviour above).
    from app.services.notify_policy import decide

    decision = await decide(event)
    if decision.silent and not escalation:
        return
    if decision.cooldown_s is not None:
        cooldown = decision.cooldown_s
    urgent = decision.urgent or (event.priority or 0) >= 2

    key = (event.type, event.camera_id)
    now = datetime.now(timezone.utc)
    last = _last_alert_at.get(key)
    if not urgent and not escalation and last and (now - last).total_seconds() < cooldown:
        return
    _last_alert_at[key] = now

    subject = _SUBJECTS.get(event.type, f"LightNVR alert: {event.type.replace('_', ' ')}")
    if urgent:
        subject = ("UNACKNOWLEDGED - " if escalation else "URGENT - ") + subject
    if urgent and not escalation:
        await _mark_priority(event, 2)
        _schedule_escalation(event)
    # None once the file's confirmed missing, so every channel below skips
    # straight to its text-only path instead of each re-checking the disk.
    snapshot = event.snapshot_path if event.snapshot_path and os.path.exists(event.snapshot_path) else None
    text_body = event.message
    link = await _event_link(event)
    if link:
        text_body = f"{event.message}\n{link}"

    # Each channel is independent - one being misconfigured or down never
    # blocks the others from delivering.
    if alert_settings.smtp_host and alert_settings.alert_email_to:
        success, detail = await send_email(alert_settings, subject, text_body, image_path=snapshot)
        if not success:
            logger.warning("Failed to send alert email: %s", detail)

    caption = f"{subject}\n{text_body}"[:1024]
    if alert_settings.telegram_enabled:
        if snapshot:
            success, detail = await send_telegram_photo(
                alert_settings.telegram_bot_token, alert_settings.telegram_chat_id, snapshot, caption
            )
        else:
            success, detail = await send_telegram_message(
                alert_settings.telegram_bot_token, alert_settings.telegram_chat_id, caption
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
                caption,
            )
        else:
            success, detail = await send_whatsapp_message(
                alert_settings.whatsapp_phone_number_id,
                alert_settings.whatsapp_access_token,
                alert_settings.whatsapp_recipient_number,
                caption,
            )
        if not success:
            logger.warning("Failed to send WhatsApp alert: %s", detail)

    if decision.webhook_url:
        await _call_webhook(decision.webhook_url, event, urgent)

    await _maybe_send_push(event, subject, urgent=urgent)


async def _mark_priority(event: Event, priority: int) -> None:
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(Event, event.id)
            if row is not None and (row.priority or 0) < priority:
                row.priority = priority
                await db.commit()
        event.priority = max(event.priority or 0, priority)
    except Exception:
        logger.exception("Could not mark event %s urgent", event.id)


def _schedule_escalation(event: Event) -> None:
    async def later():
        try:
            await asyncio.sleep(ESCALATE_AFTER_SECONDS)
            async with AsyncSessionLocal() as db:
                row = await db.get(Event, event.id)
            if row is not None and row.acked_at is None:
                await maybe_send_alert(row, escalation=True)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Escalation failed for event %s", event.id)
        finally:
            _escalations.pop(event.id, None)

    if event.id is not None and event.id not in _escalations:
        _escalations[event.id] = asyncio.create_task(later())


async def acknowledge(event_id: int, by: str) -> bool:
    async with AsyncSessionLocal() as db:
        row = await db.get(Event, event_id)
        if row is None:
            return False
        if row.acked_at is None:
            row.acked_at = datetime.now(timezone.utc)
            row.acked_by = by
            await db.commit()
    task = _escalations.pop(event_id, None)
    if task:
        task.cancel()
    return True


async def _event_link(event: Event) -> str | None:
    """Absolute link to the event page when a public address is configured
    (remote access hostname); otherwise none - a LAN-relative path is useless
    in an email read elsewhere."""
    try:
        from app.services.public_url import public_base_url

        base = await public_base_url()
    except Exception:
        base = None
    return f"{base.rstrip('/')}/events/{event.id}" if base and event.id else None


async def _call_webhook(url: str, event: Event, urgent: bool) -> None:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            await client.post(url, json={
                "event_id": event.id, "type": event.type, "camera_id": event.camera_id,
                "message": event.message, "urgent": urgent, "payload": event.payload,
            })
    except Exception as exc:
        logger.warning("Alert webhook %s failed: %s", url, exc)


async def _maybe_send_push(event: Event, subject: str, urgent: bool = False) -> None:
    from app.services.web_push import send_push_to_all

    extra = {"url": f"/events/{event.id}" if event.id else "/", "tag": f"{event.type}-{event.camera_id}",
             "event_id": event.id, "urgent": urgent}
    if event.snapshot_path and event.id:
        from app.services.signed_media import signed_event_snapshot_path

        extra["image"] = await signed_event_snapshot_path(event.id)
    try:
        await send_push_to_all(subject, event.message, extra)
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
