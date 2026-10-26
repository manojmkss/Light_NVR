import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.camera import Camera
from app.models.recording import Recording
from app.services.events import emit_event

logger = logging.getLogger(__name__)

# Shorter than this isn't worth keeping (a camera that dropped as it started).
MIN_SALVAGE_SECONDS = 1.0


def _recorded_at(filename: str) -> tuple[datetime | None, str]:
    """`20261026_112530_continuous.mp4` -> (that time in UTC, "continuous")."""
    stem = filename.rsplit(".", 1)[0]
    parts = stem.split("_")
    try:
        when = datetime.strptime(f"{parts[0]}_{parts[1]}", "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    except (IndexError, ValueError):
        return None, "continuous"
    trigger = parts[2] if len(parts) > 2 and parts[2] in ("continuous", "motion") else "continuous"
    return when, trigger


async def cleanup_orphaned_files(recordings_root: str) -> asyncio.Task | None:
    """Recordings left in the cache with no DB row - cut short by a crash, a
    power cut or a kill. They are fragmented MP4s, valid up to the last
    fragment written, so they are *repaired and kept* (made seekable and
    registered), not deleted; only empty or unreadable files are removed.

    Runs before the supervisor starts any recorder: the files are listed now
    (so a brand-new segment can never be mistaken for one), and repaired in
    the background so cameras start recording without waiting for it.
    Returns that background task.
    """
    if not os.path.isdir(recordings_root):
        return None

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Recording.file_path, Recording.thumbnail_path, Recording.playback_path)
            .where(Recording.storage_tier == "cache")
        )).all()
        cameras = set((await db.execute(select(Camera.id))).scalars().all())

    known: set[str] = set()
    for file_path, thumbnail_path, playback_path in rows:
        known.update(p for p in (file_path, thumbnail_path, playback_path) if p)

    candidates: list[tuple[int, str]] = []
    debris: list[str] = []
    for camera_dir in os.listdir(recordings_root):
        # Only per-camera recording folders ("1", "2"...). The cache also holds
        # faces/, events/, detections/ etc. - never recordings, never orphans.
        if not camera_dir.isdigit():
            continue
        full_dir = os.path.join(recordings_root, camera_dir)
        if not os.path.isdir(full_dir):
            continue
        for filename in os.listdir(full_dir):
            path = os.path.join(full_dir, filename)
            if not os.path.isfile(path) or path in known:
                continue
            # Repairable: a main recording with something in it, for a camera
            # that still exists. Everything else untracked is debris - a
            # half-written remux (.tmp), a playback copy (.sub.mp4, regenerable),
            # a thumbnail of a lost file.
            if (filename.endswith(".mp4") and not filename.endswith(".sub.mp4")
                    and int(camera_dir) in cameras and os.path.getsize(path) > 0):
                candidates.append((int(camera_dir), path))
            else:
                debris.append(path)

    for path in debris:
        try:
            os.remove(path)
        except OSError:
            logger.warning("Could not remove leftover file %s", path)
    if debris:
        logger.info("Recovery: removed %d leftover file(s) from the cache", len(debris))
    if not candidates:
        return None
    return asyncio.create_task(_salvage(candidates))


async def _salvage(candidates: list[tuple[int, str]]) -> None:
    from app.services.ffmpeg_recorder import _remux_to_seekable, footage_start, probe_duration
    from app.services.recording_registry import register_recording

    kept, lost = 0, 0
    for camera_id, path in candidates:
        try:
            duration = await probe_duration(path)
            if not duration or duration < MIN_SALVAGE_SECONDS:
                os.remove(path)
                lost += 1
                continue
            ended = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
            named, trigger = _recorded_at(os.path.basename(path))
            started = footage_start(named, ended, duration) if named else ended - timedelta(seconds=duration)
            await _remux_to_seekable(path)
            if await register_recording(camera_id, path, trigger, started, ended) is not None:
                kept += 1
            else:
                lost += 1
        except Exception:
            logger.exception("Recovery: could not repair %s", path)
            lost += 1
    logger.info("Recovery: kept %d recording(s) cut short by a restart; %d unusable", kept, lost)
    if kept:
        await emit_event(None, "system", f"Kept {kept} recording(s) that were cut short by a restart or power cut.",
                         notify=False)
    if lost:
        await emit_event(
            None, "system",
            f"Recovered from an unclean shutdown: {lost} recording file(s) were empty or unreadable and removed.",
        )
