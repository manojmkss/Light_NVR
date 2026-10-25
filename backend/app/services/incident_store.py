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

    from app.services.ai.vlm import describe_frame, timeout_for
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
        return await asyncio.wait_for(describe_frame(settings, jpeg, [], "visit", prompt=prompt),
                                      timeout=timeout_for(settings))
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


async def merge_rows(src_id: int, dst_id: int) -> None:
    """Visit src turned out to be part of visit dst (the same person in
    another camera group): its events and detections move to dst."""
    from sqlalchemy import delete, update

    from app.models.detection import Detection
    from app.models.event import Event

    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        # The copy of the visit number inside each event's details too.
        await db.execute(text("UPDATE events SET payload = json_set(payload, '$.incident_id', :dst) "
                              "WHERE incident_id = :src AND payload IS NOT NULL AND json_valid(payload)"),
                         {"src": src_id, "dst": dst_id})
        await db.execute(update(Event).where(Event.incident_id == src_id).values(incident_id=dst_id))
        await db.execute(update(Detection).where(Detection.incident_id == src_id).values(incident_id=dst_id))
        await db.execute(delete(Incident).where(Incident.id == src_id))
        await db.commit()


async def load_join_window() -> None:
    from app.models.system_settings import SystemSettings

    async with AsyncSessionLocal() as db:
        s = await db.get(SystemSettings, 1)
    seconds = getattr(s, "visit_join_seconds", None) if s else None
    if seconds:
        incident_manager.join_window = float(seconds)


async def reload_links() -> None:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(AreaLink))).scalars().all()
    incident_manager.set_links([Link(r.area_a, r.area_b, r.min_seconds, r.max_seconds) for r in rows])


async def incident_loop() -> None:
    incident_manager._persist = persist
    incident_manager._emit = emit_incident
    incident_manager._merged = merge_rows
    try:
        await load_join_window()
    except Exception:
        logger.exception("Could not load the visit join window")
    try:
        # Open visits live in memory; any still "open" in the database were
        # cut off by a restart. Close them so they don't show as in progress.
        from sqlalchemy import update

        async with AsyncSessionLocal() as db:
            await db.execute(
                update(Incident).where(Incident.status == "open").values(
                    status="closed", summary="Visit interrupted by a restart of the NVR."
                )
            )
            await db.commit()
    except Exception:
        logger.exception("Could not close visits interrupted by a restart")
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


async def name_visits(db, sample_ids: list[int], person, previous_person=None) -> list[int]:
    """A face was named on the People page (or the name was taken back): the
    visits that face came from say who it was, as if it had been recognised
    there and then. Returns the visits changed. The caller commits."""
    from sqlalchemy import select

    from app.models.detection import Detection
    from app.models.person import FaceSample

    tracks = {t for t in (await db.execute(
        select(FaceSample.track_uid).where(FaceSample.id.in_(sample_ids), FaceSample.track_uid.is_not(None))
    )).scalars().all() if t}
    if not tracks:
        return []
    dets = (await db.execute(select(Detection).where(Detection.track_uid.in_(tracks)))).scalars().all()
    changed: set[int] = set()
    for d in dets:
        if person is not None:
            if d.person_id is None:
                d.person_id = person.id
        elif previous_person is not None and d.person_id == previous_person.id:
            d.person_id = None
        if d.incident_id is not None:
            changed.add(d.incident_id)
    await db.flush()
    out = []
    for vid in sorted(changed):
        row = await db.get(Incident, vid)
        if row is None:
            continue
        names = set(row.identities or [])
        if person is not None:
            names.add(person.name)
        elif previous_person is not None:
            still = (await db.execute(select(Detection.id).where(
                Detection.incident_id == vid, Detection.person_id == previous_person.id).limit(1))).first()
            if still is None:
                names.discard(previous_person.name)
        if names != set(row.identities or []):
            row.identities = sorted(names) or None
            out.append(vid)
        # A visit still open keeps its people in memory and would write the
        # old list back when it next saves.
        live = next((i for i in incident_manager.open if i.id == vid), None)
        if live is not None:
            if person is not None:
                live.identities.add(person.name)
            elif previous_person is not None and previous_person.name not in names:
                live.identities.discard(previous_person.name)
    return out
