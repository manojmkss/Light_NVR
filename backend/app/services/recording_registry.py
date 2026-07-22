import os
from datetime import datetime

from app.db.session import AsyncSessionLocal
from app.models.camera import Camera
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
        # Recordings are a stream-copy of the camera's main stream, so the file's
        # codec is whatever the camera reports - store it so playback knows
        # whether the browser will need an H.265 -> H.264 transcode.
        camera = await db.get(Camera, camera_id)
        codec = camera.codec if camera else None

        recording = Recording(
            camera_id=camera_id,
            file_path=file_path,
            thumbnail_path=thumbnail_path,
            trigger=trigger,
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=duration,
            size_bytes=size_bytes,
            codec=codec,
        )
        db.add(recording)
        await db.commit()
        await db.refresh(recording)
        return recording
