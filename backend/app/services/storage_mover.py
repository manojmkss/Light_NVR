import asyncio
import logging
import os
import shutil

from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.recording import Recording
from app.services.storage_manager import storage_manager

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 10
BATCH_SIZE = 20


def _copy_verified_sync(src: str, dest_dir: str) -> str | None:
    """Copy one file into dest_dir and verify its size. Runs in a thread:
    every call here touches the (possibly network) destination, and a hung
    NAS mount blocks filesystem calls indefinitely - on the event loop that
    would freeze recording, live view and the web UI together."""
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(src))
    try:
        shutil.copy2(src, dest)
        if os.path.getsize(dest) != os.path.getsize(src):
            logger.warning("Size mismatch copying %s -> %s, discarding partial copy", src, dest)
            _silent_remove(dest)
            return None
    except OSError as exc:
        logger.warning("Failed to copy %s -> %s: %s", src, dest, exc)
        _silent_remove(dest)
        return None
    return dest


async def _copy_with_timeout(src: str, dest_dir: str) -> str | None:
    try:
        size = os.path.getsize(src)
    except OSError:
        return None
    # Generous: even a slow NAS moves 2 MB/s. Past this the destination is
    # treated as hung; the (unkillable) thread is abandoned and the cache
    # original stays - nothing is deleted until a copy is verified.
    timeout = 120 + size / (2 * 1024 * 1024)
    try:
        return await asyncio.wait_for(asyncio.to_thread(_copy_verified_sync, src, dest_dir), timeout=timeout)
    except asyncio.TimeoutError:
        logger.error("Copy of %s to %s timed out after %ds - storage unresponsive", src, dest_dir, int(timeout))
        return None


async def _copy_pair(video_src: str, dest_dir: str) -> tuple[str, str | None] | None:
    """Copies video + sibling thumbnail (same basename, .jpg) to dest_dir.
    Copy-then-verify, never move-in-place - the caller only deletes the cache
    originals after this succeeds, so a crash mid-copy can't lose the only
    copy of a recording.
    """
    dest_video = await _copy_with_timeout(video_src, dest_dir)
    if dest_video is None:
        return None
    dest_thumb = None
    thumb_src = os.path.splitext(video_src)[0] + ".jpg"
    if os.path.exists(thumb_src):
        dest_thumb = await _copy_with_timeout(thumb_src, dest_dir)  # non-critical
    return dest_video, dest_thumb


async def _copy_single(src: str, dest_dir: str) -> str | None:
    """Copy one extra sibling file (e.g. the H.264 playback copy) to dest_dir,
    verifying size. Non-critical: a failure just means that convenience file
    doesn't make the move, never the recording itself."""
    return await _copy_with_timeout(src, dest_dir)


def _silent_remove(path: str) -> None:
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


async def _migrate_one(recording_id: int) -> None:
    async with AsyncSessionLocal() as db:
        recording = await db.get(Recording, recording_id)
        if recording is None or recording.storage_tier != "cache":
            return
        camera_id = recording.camera_id
        video_src = recording.file_path
        thumb_src = recording.thumbnail_path
        playback_src = recording.playback_path

    if not os.path.exists(video_src):
        # File's gone already (e.g. retention deleted it before migration
        # caught up) - nothing to migrate, leave the row alone.
        return

    if storage_manager.is_primary_available():
        target_tier, base_dir = "primary", storage_manager.primary_dir()
    elif storage_manager.is_backup_available():
        target_tier, base_dir = "backup", storage_manager.backup_dir()
    else:
        return  # neither destination reachable right now; retry next pass

    dest_dir = f"{base_dir}/{camera_id}"
    copied = await _copy_pair(video_src, dest_dir)
    if copied is None:
        return  # copy failed; retry next pass
    new_video_path, new_thumb_path = copied
    if not thumb_src:
        new_thumb_path = None

    # Best-effort move of the H.264 playback copy alongside its recording; if it
    # doesn't make it, playback for this clip just falls back to transcoding.
    new_playback_path = None
    if playback_src and os.path.exists(playback_src):
        new_playback_path = await _copy_single(playback_src, dest_dir)

    async with AsyncSessionLocal() as db:
        recording = await db.get(Recording, recording_id)
        if recording is None:
            # Deleted while we were copying it - clean up the copies we made
            # (in a thread: these are on the destination storage).
            for path in (new_video_path, new_thumb_path, new_playback_path):
                if path:
                    try:
                        await asyncio.wait_for(asyncio.to_thread(_silent_remove, path), timeout=30)
                    except asyncio.TimeoutError:
                        pass
            return
        recording.file_path = new_video_path
        recording.thumbnail_path = new_thumb_path
        recording.playback_path = new_playback_path
        recording.storage_tier = target_tier
        await db.commit()

    _silent_remove(video_src)
    if thumb_src:
        _silent_remove(thumb_src)
    if playback_src:
        _silent_remove(playback_src)

    logger.info("Migrated recording %d to %s", recording_id, target_tier)


async def pending_migration_count() -> int:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Recording.id).where(Recording.storage_tier == "cache"))
        return len(result.all())


async def recordings_size_by_tier() -> dict[str, int]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Recording.storage_tier, func.coalesce(func.sum(Recording.size_bytes), 0)).group_by(
                Recording.storage_tier
            )
        )
        return dict(result.all())


async def storage_mover_loop() -> None:
    while True:
        try:
            if storage_manager.is_primary_available() or storage_manager.is_backup_available():
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(Recording.id)
                        .where(Recording.storage_tier == "cache")
                        .order_by(Recording.started_at.asc())
                        .limit(BATCH_SIZE)
                    )
                    pending_ids = result.scalars().all()

                for recording_id in pending_ids:
                    await _migrate_one(recording_id)
        except Exception:
            logger.exception("Storage mover pass failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
