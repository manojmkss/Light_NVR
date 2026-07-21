from datetime import datetime, timezone

from app.db.session import AsyncSessionLocal
from app.models.camera import Camera
from app.services.events import emit_event


async def mark_online(camera_id: int) -> None:
    name = None
    was_offline = False
    async with AsyncSessionLocal() as db:
        camera = await db.get(Camera, camera_id)
        if camera is None:
            return
        was_offline = camera.status != "online"
        camera.status = "online"
        camera.last_seen_at = datetime.now(timezone.utc)
        camera.last_error = None  # stale failure reasons must not outlive the recovery
        name = camera.name
        await db.commit()

    if was_offline:
        await emit_event(camera_id, "camera_online", f"Camera '{name}' is back online")


async def mark_offline(camera_id: int, error: str | None = None) -> None:
    """Mark a camera offline, optionally recording *why* (shown on the Cameras
    page). When `error` is None the existing last_error is left alone, so a
    detailed reason from the recorder isn't overwritten by a later generic
    failure from another component.
    """
    from app.core.log_buffer import scrub_credentials

    name = None
    was_online = False
    async with AsyncSessionLocal() as db:
        camera = await db.get(Camera, camera_id)
        if camera is None:
            return
        was_online = camera.status != "offline"
        camera.status = "offline"
        if error:
            camera.last_error = scrub_credentials(error)[:500]
        name = camera.name
        await db.commit()

    if was_online:
        await emit_event(camera_id, "camera_offline", f"Camera '{name}' went offline")
