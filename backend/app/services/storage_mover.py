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


async def _copy_pair(video_src: str, dest_dir: str) -> str | None:
    """Copies video + sibling thumbnail (same basename, .jpg) to dest_dir.
    Copy-then-verify, never move-in-place - the caller only deletes the cache
    originals after this succeeds, so a crash mid-copy can't lose the only
    copy of a recording.
    """
    os.makedirs(dest_dir, exist_ok=True)
    dest_video = os.path.join(dest_dir, os.path.basename(video_src))

    try:
        await asyncio.to_thread(shutil.copy2, video_src, dest_video)
    except OSError as exc:
        logger.warning("Failed to copy %s -> %s: %s", video_src, dest_video, exc)
        _silent_remove(dest_video)
        return None

    if os.path.getsize(dest_video) != os.path.getsize(video_src):
        logger.warning("Size mismatch copying %s -> %s, discarding partial copy", video_src, dest_video)
        _silent_remove(dest_video)
        return None

    thumb_src = os.path.splitext(video_src)[0] + ".jpg"
    if os.path.exists(thumb_src):
        dest_thumb = os.path.splitext(dest_video)[0] + ".jpg"
        try:
            await asyncio.to_thread(shutil.copy2, thumb_src, dest_thumb)
        except OSError:
            pass  # thumbnail is non-critical; the recording itself is already safe

    return dest_video


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
    new_video_path = await _copy_pair(video_src, dest_dir)
    if new_video_path is None:
        return  # copy failed; retry next pass

    new_thumb_path = os.path.splitext(new_video_path)[0] + ".jpg"
    if not (thumb_src and os.path.exists(new_thumb_path)):
        new_thumb_path = None

    async with AsyncSessionLocal() as db:
        recording = await db.get(Recording, recording_id)
        if recording is None:
            # Deleted while we were copying it - clean up the copy we just made.
            _silent_remove(new_video_path)
            if new_thumb_path:
                _silent_remove(new_thumb_path)
            return
        recording.file_path = new_video_path
        recording.thumbnail_path = new_thumb_path
        recording.storage_tier = target_tier
        await db.commit()

    _silent_remove(video_src)
    if thumb_src:
        _silent_remove(thumb_src)

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
