import asyncio
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.ai_settings import AISettings
from app.models.alert_settings import AlertSettings
from app.models.camera import Camera
from app.models.detection import Detection
from app.models.event import Event
from app.models.recording import Recording
from app.models.storage_config import StorageConfig
from app.services.events import emit_event
from app.services.storage_manager import storage_manager

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 1800

# Events are a log / timeline-marker table. Motion events inside a recording's
# window are already removed when that clip is purged, but connectivity events
# (offline/online/error), system notices, and low-storage warnings have no
# recording to tie them to and would otherwise accumulate forever on a 24/7
# system. This age cap is the backstop for those. 90 days is comfortably beyond
# any typical retention window, so recordings still on disk keep their markers.
EVENT_RETENTION_DAYS = 90


async def delete_recording_files(recording: Recording) -> None:
    for path in (recording.file_path, recording.thumbnail_path):
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                logger.warning("Could not remove file %s", path)


def _recording_window_end(recording: Recording) -> datetime:
    if recording.ended_at is not None:
        return recording.ended_at
    if recording.duration_seconds:
        return recording.started_at + timedelta(seconds=recording.duration_seconds)
    return recording.started_at


async def _purge_related_events(db: AsyncSession, recording: Recording) -> int:
    """Remove motion-detection Event rows that fall inside this recording's
    time window. They're derived from analyzing this exact footage, so once
    the clip is gone they're orphaned markers on the timeline with nothing to
    play - deleting them keeps the DB consistent and reclaims their row space.
    Camera connectivity events (offline/online/error) are left alone since
    those describe the camera, not this clip, and stay meaningful without it.
    """
    result = await db.execute(
        delete(Event).where(
            Event.camera_id == recording.camera_id,
            Event.type == "motion",
            Event.created_at >= recording.started_at,
            Event.created_at <= _recording_window_end(recording),
        )
    )
    return max(result.rowcount or 0, 0)


async def purge_recording(db: AsyncSession, recording: Recording) -> int:
    """Delete a recording's files, its DB row, and any motion events scoped to
    its time window - the single place all deletion paths (manual delete, bulk
    delete, age-based retention, storage-cap eviction) should go through so
    none of them can drift out of sync with each other. Caller still owns the
    commit so batch loops keep a single commit per batch. Returns the number
    of related Event rows removed.
    """
    await delete_recording_files(recording)
    purged = await _purge_related_events(db, recording)
    await db.delete(recording)
    return purged


async def enforce_retention() -> None:
    async with AsyncSessionLocal() as db:
        cameras = (await db.execute(select(Camera))).scalars().all()
        storage_config = await db.get(StorageConfig, 1)

    total_expired = 0
    for camera in cameras:
        retention_days = (
            camera.retention_days if camera.retention_days is not None else storage_config.default_retention_days
        )
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Recording).where(Recording.camera_id == camera.id, Recording.started_at < cutoff)
            )
            expired = result.scalars().all()
            for recording in expired:
                await purge_recording(db, recording)
            if expired:
                await db.commit()
                total_expired += len(expired)

    if total_expired:
        logger.info("Retention: removed %d expired recording(s) (per-camera age limits)", total_expired)

    # This cap is a global backstop independent of per-camera retention - it
    # guarantees total usage never runs away regardless of individual camera
    # settings, by trimming the system-wide oldest recordings first.
    if storage_config.max_storage_gb > 0:
        await _enforce_storage_cap(storage_config.max_storage_gb)

    await _prune_old_events()
    await _prune_old_detections()
    await _sweep_orphaned_detection_snapshots()
    await _check_low_storage()


async def _prune_old_detections() -> None:
    """AI detections are metadata *about* footage and would otherwise outlive
    the clips they describe, forever, along with a snapshot JPEG each. Age is
    driven by the user's own detection_retention_days.

    Snapshot files are removed before their rows: if the process dies between
    the two, the leftover is an orphaned file (harmless, and re-swept on the
    next pass because the row is still there) rather than an orphaned row
    pointing at a file that no longer exists (which the UI would render as a
    broken image forever).
    """
    async with AsyncSessionLocal() as db:
        ai_settings = await db.get(AISettings, 1)
    if ai_settings is None:
        return

    # Naive UTC, matching how SQLite actually stores these (same convention as
    # kiosk.py / the recordings export route). It matters more here than
    # elsewhere: because the rows are loaded first, SQLAlchemy re-evaluates the
    # criteria in *Python* to synchronize the session, and an aware cutoff vs a
    # naive column raises TypeError - which would kill the whole retention loop
    # every 30 minutes, not just this sweep.
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        days=ai_settings.detection_retention_days
    )
    async with AsyncSessionLocal() as db:
        expired = (
            (await db.execute(select(Detection).where(Detection.created_at < cutoff))).scalars().all()
        )
        if not expired:
            return

        # Several detections in one frame share one snapshot, so de-duplicate
        # before unlinking or the 2nd..Nth delete is a guaranteed miss.
        for path in {d.snapshot_path for d in expired if d.snapshot_path}:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    logger.warning("Retention: could not remove detection snapshot %s", path)

        # synchronize_session=False: the rows are already loaded and about to be
        # discarded with the session, so there's nothing worth syncing - and it
        # skips the in-Python re-evaluation of the criteria entirely.
        await db.execute(
            delete(Detection).where(Detection.created_at < cutoff).execution_options(synchronize_session=False)
        )
        await db.commit()
        logger.info(
            "Retention: pruned %d detection(s) older than %d days",
            len(expired),
            ai_settings.detection_retention_days,
        )


async def _sweep_orphaned_detection_snapshots() -> None:
    """Remove snapshot directories belonging to cameras that no longer exist.

    Deleting a camera drops its detection rows immediately (so the request
    stays fast) but leaves the JPEGs behind; without this they'd sit on the
    cache disk forever, since the age-based prune above only ever looks at
    rows that still exist.
    """
    root = os.path.join(storage_manager.cache_dir(), "detections")
    if not os.path.isdir(root):
        return

    async with AsyncSessionLocal() as db:
        live = {str(c) for c in (await db.execute(select(Camera.id))).scalars().all()}

    for entry in os.listdir(root):
        if entry in live:
            continue
        path = os.path.join(root, entry)
        if not os.path.isdir(path):
            continue
        try:
            await asyncio.to_thread(shutil.rmtree, path)
            logger.info("Retention: removed detection snapshots for deleted camera %s", entry)
        except OSError:
            logger.warning("Retention: could not remove orphaned snapshot dir %s", path)


async def _prune_old_events() -> None:
    # Same tz-aware cutoff pattern used for recordings above.
    cutoff = datetime.now(timezone.utc) - timedelta(days=EVENT_RETENTION_DAYS)
    async with AsyncSessionLocal() as db:
        # Snapshots removed before the (bulk, row-less) delete below - same
        # orphan-file-over-orphan-row tradeoff as _prune_old_detections.
        paths = (
            await db.execute(
                select(Event.snapshot_path).where(Event.created_at < cutoff, Event.snapshot_path.is_not(None))
            )
        ).scalars().all()
        for path in set(paths):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    logger.warning("Retention: could not remove event snapshot %s", path)

        result = await db.execute(delete(Event).where(Event.created_at < cutoff))
        removed = result.rowcount or 0
        if removed:
            await db.commit()
            logger.info("Retention: pruned %d event(s) older than %d days", removed, EVENT_RETENTION_DAYS)


async def _enforce_storage_cap(max_storage_gb: int) -> None:
    max_bytes = max_storage_gb * 1024**3

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Recording).order_by(Recording.started_at.asc()))
        recordings = result.scalars().all()
        total = sum(r.size_bytes or 0 for r in recordings)

        removed = 0
        for recording in recordings:
            if total <= max_bytes:
                break
            await purge_recording(db, recording)
            total -= recording.size_bytes or 0
            removed += 1

        if removed:
            await db.commit()
            logger.info("Retention: removed %d recording(s) to stay under %dGB cap", removed, max_storage_gb)


async def _check_low_storage() -> None:
    # Primary is the destination that matters long-term; fall back to cache
    # so the alert still reflects somewhere real if primary is unreachable
    # (storage_manager already alerts separately on that unavailability).
    check_path = storage_manager.primary_dir() if storage_manager.is_primary_available() else storage_manager.cache_dir()

    try:
        usage = await asyncio.to_thread(shutil.disk_usage, check_path)
    except OSError:
        return

    percent_free = (usage.free / usage.total) * 100

    async with AsyncSessionLocal() as db:
        alert_settings = await db.get(AlertSettings, 1)

    if alert_settings and alert_settings.low_storage_alerts_enabled and percent_free < alert_settings.low_storage_threshold_percent:
        await emit_event(
            None,
            "low_storage",
            f"Storage is low: {percent_free:.1f}% free ({usage.free // (1024**3)}GB remaining)",
        )


async def retention_loop() -> None:
    while True:
        try:
            await enforce_retention()
        except Exception:
            logger.exception("Retention check failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
