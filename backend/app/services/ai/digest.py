"""Tier 3 extra: a daily activity summary rolled up into one event.

Deliberately deterministic aggregation rather than a VLM call: it works even
when no VLM is configured, costs nothing, and cannot hallucinate a burglar.
Stored per-detection descriptions are woven in as examples when they exist.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.ai_settings import AISettings
from app.models.camera import Camera
from app.models.detection import Detection
from app.models.system_settings import SystemSettings
from app.services.events import emit_event

logger = logging.getLogger(__name__)

DIGEST_HOUR_LOCAL = 20  # 8pm, in the NVR's configured display timezone
CHECK_INTERVAL_SECONDS = 600


async def _display_tz():
    """Fire at 8pm the *user's* evening, not 8pm UTC - the NVR already has a
    configured display timezone, so reuse it rather than inventing another."""
    async with AsyncSessionLocal() as db:
        record = await db.get(SystemSettings, 1)
    name = (record.timezone if record else "") or ""
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            logger.warning("Digest: invalid timezone %r - using UTC", name)
    return timezone.utc


async def build_digest_text() -> str | None:
    """None when there were no detections - no daily 'nothing happened'
    notification, which is exactly the kind of noise this feature exists to
    reduce."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Detection).where(Detection.created_at >= cutoff))).scalars().all()
        if not rows:
            return None
        cameras = {c.id: c.name for c in (await db.execute(select(Camera))).scalars().all()}

    per_camera: dict[int, dict[str, int]] = {}
    for r in rows:
        per_camera.setdefault(r.camera_id, {})[r.label] = per_camera.setdefault(r.camera_id, {}).get(r.label, 0) + 1

    parts = []
    for cam_id, counts in per_camera.items():
        name = cameras.get(cam_id, f"camera {cam_id}")
        inner = ", ".join(f"{n}x {label}" for label, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        parts.append(f"{name}: {inner}")

    text = "Daily AI summary (last 24h) - " + "; ".join(parts)

    # A couple of stored VLM sentences turn a spreadsheet into a report.
    described = [r.description for r in sorted(rows, key=lambda r: r.created_at, reverse=True) if r.description]
    for sample in described[:2]:
        text += f' | "{sample}"'
    return text[:900]


async def daily_digest_loop() -> None:
    sent_for = None  # date of the last digest, so exactly one fires per day
    while True:
        try:
            async with AsyncSessionLocal() as db:
                settings = await db.get(AISettings, 1)
            if settings is not None and settings.enabled and settings.vlm_daily_digest:
                now_local = datetime.now(await _display_tz())
                if now_local.hour >= DIGEST_HOUR_LOCAL and sent_for != now_local.date():
                    text = await build_digest_text()
                    if text:
                        await emit_event(None, "system", text)
                        logger.info("Digest: daily AI summary emitted")
                    # Marked regardless of whether there was anything to say,
                    # so a quiet day doesn't re-check every 10 minutes.
                    sent_for = now_local.date()
        except Exception:
            logger.exception("Daily digest check failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
