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

# The H.264 sub-stream playback copy exists only so recent footage plays
# instantly in the browser. Its usefulness ends at the live/kiosk scrub window
# (SCRUB_WINDOW_MS in CameraTile.tsx / KIOSK_REWIND_MINUTES = 60), past which
# old-footage playback is rare and transcodes on demand. So the copy is deleted
# once it ages past that window - reclaiming disk - while the full-quality H.265
# main is kept for the entire retention period. Keep this >= the scrub window.
PLAYBACK_COPY_KEEP_MINUTES = 60

# Free space kept on every disk that holds recordings, whatever else shares it
# (Docker images, the OS, other apps): the oldest footage is removed first, so
# recording never has to pause for space. The GB cap above only counts
# recordings and can't see the disk filling up from elsewhere.
FREE_FLOOR_BYTES = int(float(os.environ.get("LIGHTNVR_MIN_FREE_GB", "15")) * 1024**3)
FREE_FLOOR_PERCENT = 5.0
FREE_HYSTERESIS_BYTES = 2 * 1024**3  # free a little extra, so it doesn't trim one file every check
FREE_CHECK_SECONDS = 300
# Footage newer than this is never removed to make room: if only recent
# recordings are left, the disk is full of something else - say so instead.
PROTECT_RECENT_HOURS = 24
_free_lock = asyncio.Lock()
_full_of_other_warned_at: datetime | None = None


def free_floor(total_bytes: int) -> int:
    return max(FREE_FLOOR_BYTES, int(total_bytes * FREE_FLOOR_PERCENT / 100))


def _remove_files_sync(paths: list[str]) -> None:
    for path in paths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                logger.warning("Could not remove file %s", path)


async def delete_recording_files(recording: Recording) -> None:
    # In a thread with a time limit: recordings live on a NAS, and a hung
    # mount would otherwise freeze the whole event loop on os.remove().
    paths = [recording.file_path, recording.thumbnail_path, recording.playback_path]
    try:
        await asyncio.wait_for(asyncio.to_thread(_remove_files_sync, paths), timeout=60)
    except asyncio.TimeoutError:
        logger.error("Deleting %s timed out - storage unresponsive", recording.file_path)


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

    await _prune_stale_playback_copies()
    await _prune_old_events()
    await _prune_old_detections()
    await _sweep_orphaned_detection_snapshots()
    try:
        # Unnamed face captures are kept only a few days (for "Who is this?").
        from app.api.routes.faces import prune_unknown_faces

        await prune_unknown_faces()
    except Exception:
        logger.exception("Retention: could not prune unknown face samples")
    await _check_low_storage()


async def _prune_stale_playback_copies() -> None:
    """Delete H.264 playback copies once they age past the scrub window, keeping
    the H.265 main and its DB row intact. Only playback_path is cleared and its
    file removed, so playing older footage simply falls back to on-demand
    transcoding - the copy was never meant to live for the whole retention.

    Naive-UTC cutoff to match how ended_at is stored (same convention as the
    kiosk rewind query); mismatching aware/naive here would silently never match.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=PLAYBACK_COPY_KEEP_MINUTES)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Recording).where(
                Recording.playback_path.is_not(None),
                Recording.ended_at.is_not(None),
                Recording.ended_at < cutoff,
            )
        )
        stale = result.scalars().all()
        if not stale:
            return
        try:
            await asyncio.wait_for(asyncio.to_thread(_remove_files_sync, [r.playback_path for r in stale]), timeout=120)
        except asyncio.TimeoutError:
            logger.error("Pruning playback copies timed out - storage unresponsive")
            return
        for rec in stale:
            rec.playback_path = None
        await db.commit()
        logger.info(
            "Retention: pruned %d H.264 playback copy(ies) older than %d min", len(stale), PLAYBACK_COPY_KEEP_MINUTES
        )


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

        # Visits (multi-camera incidents) follow the same retention.
        from app.models.incident import Incident

        inc_paths = (await db.execute(
            select(Incident.snapshot_path).where(Incident.started_at < cutoff, Incident.snapshot_path.is_not(None))
        )).scalars().all()
        for path in set(inc_paths):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        removed_inc = (await db.execute(delete(Incident).where(Incident.started_at < cutoff))).rowcount or 0
        if removed or removed_inc:
            await db.commit()
            logger.info("Retention: pruned %d event(s) and %d visit(s) older than %d days",
                        removed, removed_inc, EVENT_RETENTION_DAYS)


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


def _tier_dirs() -> dict[str, str]:
    dirs = {"cache": storage_manager.cache_dir()}
    if storage_manager.is_primary_available():
        dirs["primary"] = storage_manager.primary_dir()
    if storage_manager.is_backup_available():
        dirs["backup"] = storage_manager.backup_dir()
    return dirs


def _device_groups(dirs: dict[str, str]) -> list[tuple[str, list[str]]]:
    """[(a dir on the disk, [tiers stored on that disk])] - tiers on the same
    disk (the usual single-disk install) are trimmed together."""
    groups: dict[int, tuple[str, list[str]]] = {}
    for tier, path in dirs.items():
        try:
            dev = os.stat(path).st_dev
        except OSError:
            continue
        groups.setdefault(dev, (path, []))[1].append(tier)
    return list(groups.values())


async def ensure_free_space() -> int:
    """Remove the oldest recordings on any recordings disk that has less than
    the free-space floor left. Returns how many were removed. Safe to call
    often (the retention loop, and the recorder the moment space runs short)."""
    global _full_of_other_warned_at
    removed = freed = 0
    async with _free_lock:
        for path, tiers in _device_groups(_tier_dirs()):
            try:
                usage = await asyncio.to_thread(shutil.disk_usage, path)
            except OSError:
                continue
            floor = free_floor(usage.total)
            if usage.free >= floor:
                continue
            need = floor + FREE_HYSTERESIS_BYTES - usage.free
            protect = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=PROTECT_RECENT_HOURS)
            async with AsyncSessionLocal() as db:
                oldest = (await db.execute(
                    select(Recording).where(Recording.storage_tier.in_(tiers))
                    .order_by(Recording.started_at.asc()).limit(500)
                )).scalars().all()
                for rec in oldest:
                    if need <= 0:
                        break
                    started = rec.started_at.replace(tzinfo=None) if rec.started_at.tzinfo else rec.started_at
                    if started >= protect:
                        now = datetime.now(timezone.utc)
                        if _full_of_other_warned_at is None or now - _full_of_other_warned_at > timedelta(hours=6):
                            _full_of_other_warned_at = now
                            await emit_event(
                                None, "system",
                                f"The recordings disk is almost full ({usage.free // 1024**3} GB free) with only the "
                                f"last {PROTECT_RECENT_HOURS} h of recordings left - something else is filling it. "
                                "Free up space (e.g. 'docker image prune -a') or recording will pause.",
                            )
                        break
                    size = rec.size_bytes or 0
                    await purge_recording(db, rec)
                    need -= size
                    freed += size
                    removed += 1
                if removed:
                    await db.commit()
    if removed:
        logger.warning("Retention: removed %d oldest recording(s) (%.1f GB) to keep the disk's free-space floor",
                       removed, freed / 1024**3)
    return removed


async def free_space_loop() -> None:
    """Every few minutes - a disk can fill much faster than the 30-minute
    retention pass (an update pulling images, another app)."""
    while True:
        try:
            await ensure_free_space()
        except Exception:
            logger.exception("Free-space check failed")
        await asyncio.sleep(FREE_CHECK_SECONDS)


async def _check_low_storage() -> None:
    """Warn when the recordings disk runs low - once, then again only if it
    gets 2 points worse or a day has passed, and once more when it recovers.
    (It used to repeat on every 30-minute pass.)"""
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
        last = (await db.execute(
            select(Event).where(Event.type == "low_storage", Event.camera_id.is_(None))
            .order_by(Event.id.desc()).limit(1)
        )).scalars().first()
    if not (alert_settings and alert_settings.low_storage_alerts_enabled):
        return
    threshold = alert_settings.low_storage_threshold_percent
    facts = (last.payload or {}) if last is not None else {}
    last_pct, cleared = facts.get("percent_free"), facts.get("cleared", False)
    warned = last is not None and not cleared and last_pct is not None
    if percent_free < threshold:
        created = last.created_at.replace(tzinfo=None) if last is not None else None
        stale = created is None or datetime.now(timezone.utc).replace(tzinfo=None) - created > timedelta(hours=24)
        if not warned or stale or last_pct - percent_free >= 2:
            floor_gb = free_floor(usage.total) // 1024**3
            await emit_event(
                None, "low_storage",
                f"Storage is low: {percent_free:.1f}% free ({usage.free // (1024**3)}GB remaining). The oldest "
                f"recordings are removed automatically to keep {floor_gb}GB free, so recording carries on.",
                payload={"percent_free": round(percent_free, 1)},
            )
    elif warned and percent_free >= threshold + 2:
        await emit_event(None, "low_storage", f"Storage is OK again: {percent_free:.1f}% free.",
                         payload={"percent_free": round(percent_free, 1), "cleared": True})


async def retention_loop() -> None:
    while True:
        try:
            await enforce_retention()
        except Exception:
            logger.exception("Retention check failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
