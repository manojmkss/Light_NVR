import os
from datetime import datetime

from app.db.session import AsyncSessionLocal
from app.models.recording import Recording
from app.services.thumbnail import generate_thumbnail


async def register_recording(
    camera_id: int,
    file_path: str,
    trigger: str,
    started_at: datetime,
    ended_at: datetime,
) -> Recording | None:
    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        return None

    thumbnail_path = await generate_thumbnail(file_path)
    size_bytes = os.path.getsize(file_path)
    duration = (ended_at - started_at).total_seconds()

    async with AsyncSessionLocal() as db:
        recording = Recording(
            camera_id=camera_id,
            file_path=file_path,
            thumbnail_path=thumbnail_path,
            trigger=trigger,
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=duration,
            size_bytes=size_bytes,
        )
        db.add(recording)
        await db.commit()
        await db.refresh(recording)
        return recording
