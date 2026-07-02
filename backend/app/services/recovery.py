import logging
import os

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.recording import Recording
from app.services.events import emit_event

logger = logging.getLogger(__name__)


async def cleanup_orphaned_files(recordings_root: str) -> None:
    """Removes any file sitting in the cache dir with no matching Recording
    row, regardless of size. A clean shutdown never leaves such a file (the
    DB row is written right after the ffmpeg process exits), so on startup -
    before the supervisor starts any new recording - anything untracked here
    can only be a partial segment from a crash or power cut. A zero-byte-only
    check (the old behavior) missed the more common case: a power cut
    mid-write leaves a non-empty but truncated, unplayable file.
    """
    if not os.path.isdir(recordings_root):
        return

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Recording.file_path, Recording.thumbnail_path).where(Recording.storage_tier == "cache")
        )
        rows = result.all()

    known_paths: set[str] = set()
    for file_path, thumbnail_path in rows:
        known_paths.add(file_path)
        if thumbnail_path:
            known_paths.add(thumbnail_path)

    removed = 0
    removed_bytes = 0
    for camera_dir in os.listdir(recordings_root):
        full_dir = os.path.join(recordings_root, camera_dir)
        if not os.path.isdir(full_dir):
            continue
        for filename in os.listdir(full_dir):
            file_path = os.path.join(full_dir, filename)
            if not os.path.isfile(file_path) or file_path in known_paths:
                continue
            try:
                removed_bytes += os.path.getsize(file_path)
                os.remove(file_path)
                removed += 1
            except OSError:
                logger.warning("Could not remove orphaned file %s", file_path)

    if removed:
        logger.info("Recovery: removed %d orphaned recording file(s) (%d bytes)", removed, removed_bytes)
        await emit_event(
            None,
            "system",
            f"Recovered from an unclean shutdown: removed {removed} incomplete recording file(s) left in cache.",
        )
