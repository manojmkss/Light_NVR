"""Whether each camera is online - one answer for the whole NVR.

Several parts watch a camera (live analysis, the recorder, the pre-roll
buffer) and each reports trouble on its own. A camera is only declared
offline - and the user alerted - when *none* of them has seen a picture for
OFFLINE_CONFIRM_SECONDS: a Wi-Fi camera dropping for a few seconds, or one
part reconnecting while the others still get pictures, is not an outage.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from app.db.session import AsyncSessionLocal
from app.models.camera import Camera
from app.services.events import emit_event

logger = logging.getLogger(__name__)

OFFLINE_CONFIRM_SECONDS = 45.0

_alive: dict[int, float] = {}  # camera -> last sign of life (monotonic)
_pending: dict[int, asyncio.Task] = {}  # camera -> offline waiting to be confirmed


def heartbeat(camera_id: int) -> None:
    """A picture or a saved recording arrived. Called per frame (from the
    live-stream thread too): a plain dict write, nothing else."""
    _alive[camera_id] = time.monotonic()


async def mark_online(camera_id: int) -> None:
    heartbeat(camera_id)
    pending = _pending.pop(camera_id, None)
    if pending is not None:
        pending.cancel()
    name = None
    was_offline = False
    async with AsyncSessionLocal() as db:
        camera = await db.get(Camera, camera_id)
        if camera is None:
            return
        was_offline = camera.status == "offline"
        camera.status = "online"
        camera.last_seen_at = datetime.now(timezone.utc)
        camera.last_error = None  # stale failure reasons must not outlive the recovery
        name = camera.name
        await db.commit()

    if was_offline:
        await emit_event(camera_id, "camera_online", f"Camera '{name}' is back online")


async def mark_offline(camera_id: int, error: str | None = None) -> None:
    """Something lost the camera. The reason (when given) is shown on the
    Cameras page at once; the camera is declared offline only if nothing sees
    a picture for OFFLINE_CONFIRM_SECONDS. When `error` is None the existing
    last_error is left alone, so a detailed reason from the recorder isn't
    overwritten by a later generic failure from another component."""
    if error:
        from app.core.log_buffer import scrub_credentials

        async with AsyncSessionLocal() as db:
            camera = await db.get(Camera, camera_id)
            if camera is None:
                return
            camera.last_error = scrub_credentials(error)[:500]
            await db.commit()
    if camera_id not in _pending:
        _pending[camera_id] = asyncio.create_task(_confirm_offline(camera_id))


async def _confirm_offline(camera_id: int) -> None:
    me = asyncio.current_task()
    try:
        await asyncio.sleep(OFFLINE_CONFIRM_SECONDS)
        if time.monotonic() - _alive.get(camera_id, 0.0) < OFFLINE_CONFIRM_SECONDS:
            return  # another part still sees pictures: not an outage
        await _set_offline(camera_id)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Could not mark camera %s offline", camera_id)
    finally:
        if _pending.get(camera_id) is me:
            _pending.pop(camera_id, None)


async def _set_offline(camera_id: int) -> None:
    name = None
    was_online = False
    async with AsyncSessionLocal() as db:
        camera = await db.get(Camera, camera_id)
        if camera is None:
            return
        was_online = camera.status != "offline"
        camera.status = "offline"
        name = camera.name
        await db.commit()

    if was_online:
        await emit_event(camera_id, "camera_offline", f"Camera '{name}' went offline")
