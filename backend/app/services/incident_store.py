"""Database side of incidents: saving them, loading area links, and the
background loop that closes finished visits."""

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.incident import AreaLink, Incident
from app.services.incidents import Link, OpenIncident, incident_manager

logger = logging.getLogger(__name__)


def _dt(ts: float | None):
    return datetime.fromtimestamp(ts, timezone.utc) if ts else None


def _write_snapshot(jpeg: bytes) -> str | None:
    try:
        from app.services.storage_manager import storage_manager

        directory = os.path.join(storage_manager.cache_dir(), "events", "incidents")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}.jpg")
        with open(path, "wb") as f:
            f.write(jpeg)
        return path
    except Exception:
        logger.exception("Could not write incident snapshot")
        return None


async def persist(inc: OpenIncident, closed: bool = False) -> int | None:
    counts: dict[str, int] = {}
    for m in inc.members:
        for label, n in m.counts.items():
            counts[label] = max(counts.get(label, 0), n)
    async with AsyncSessionLocal() as db:
        row = await db.get(Incident, inc.id) if inc.id else None
        if row is None:
            row = Incident(started_at=_dt(inc.started), status="open", mode=inc.mode)
            db.add(row)
        row.camera_ids = inc.camera_ids
        row.areas_path = inc.areas_path
        row.counts = counts or None
        row.identities = sorted(inc.identities) or None
        if closed:
            row.status = "closed"
            row.ended_at = _dt(inc.last_end)
            row.summary = incident_manager.describe(inc)[0]
            if inc.snapshot and not row.snapshot_path:
                row.snapshot_path = await asyncio.to_thread(_write_snapshot, inc.snapshot)
        await db.commit()
        await db.refresh(row)
        return row.id


async def _narrative(message: str, snapshot: bytes | None) -> str:
    """One VLM sentence for the visit, from its clearest picture."""
    if not snapshot:
        return ""
    from app.services.ai.pipeline import ai_pipeline

    settings = await ai_pipeline.settings()
    if not (settings and settings.enabled and settings.vlm_enabled and getattr(settings, "event_summary_vlm", True)):
        return ""
    import cv2
    import numpy as np

    from app.services.ai.vlm import describe_frame
    from app.services.faces import privacy_safe_jpeg

    frame = cv2.imdecode(np.frombuffer(snapshot, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return ""
    jpeg = await privacy_safe_jpeg(settings, frame)
    if not jpeg:
        return ""
    facts = message.replace("\n", "; ")
    prompt = ("This is the clearest picture from one visit to a home, seen on several security cameras. "
              f"Facts already known: {facts}. Describe in at most two factual sentences (40 words) what the person "
              "or vehicle in the picture is doing. No preamble, no speculation beyond the image.")
    try:
        return await asyncio.wait_for(describe_frame(settings, jpeg, [], "visit", prompt=prompt), timeout=30)
    except Exception as exc:
        logger.warning("Visit description failed (%s)", exc)
        return ""


async def emit_incident(kind, message, snapshot, payload, camera_id, inc) -> None:
    from app.services.events import emit_event

    sentence = await _narrative(message, snapshot)
    if sentence:
        first, _, rest = message.partition("\n")
        message = f"{first}\n{sentence}\n{rest}" if rest else f"{first}\n{sentence}"
    path = await asyncio.to_thread(_write_snapshot, snapshot) if snapshot else None
    await emit_event(camera_id, kind, message, snapshot_path=path, payload=payload, incident_id=inc.id)


async def reload_links() -> None:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(AreaLink))).scalars().all()
    incident_manager.set_links([Link(r.area_a, r.area_b, r.min_seconds, r.max_seconds) for r in rows])


async def incident_loop() -> None:
    incident_manager._persist = persist
    incident_manager._emit = emit_incident
    try:
        await reload_links()
    except Exception:
        logger.exception("Could not load area links")
    while True:
        await asyncio.sleep(15)
        try:
            await incident_manager.sweep()
        except Exception:
            logger.exception("Incident sweep failed")
