import asyncio
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.alert_settings import AlertSettings
from app.models.camera import Camera
from app.models.event import Event
from app.models.recording import Recording
from app.models.storage_config import StorageConfig
from app.services.events import emit_event
from app.services.storage_manager import storage_manager

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 1800


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

    await _check_low_storage()


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
