"""The media engine's websocket: live view and playback for one player
(services/media_engine.py). Signed in with the user's access token, or a
kiosk link's token (that kiosk's cameras, its rewind window only)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.deps import _resolve_user, require_admin
from app.db.session import AsyncSessionLocal
from app.models.kiosk_view import KioskView
from app.models.user import User
from app.services.media_engine import Access, MediaSession, live_hub

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/media", tags=["media"])


async def _access(token: str, kiosk: str) -> Access | None:
    async with AsyncSessionLocal() as db:
        if kiosk:
            view = (await db.execute(
                select(KioskView).options(selectinload(KioskView.cameras)).where(KioskView.token == kiosk)
            )).scalar_one_or_none()
            if view is None:
                return None
            from app.api.routes.kiosk import _kiosk_floor

            return Access(cameras={c.camera_id for c in view.cameras}, floor=_kiosk_floor())
        if not token:
            return None
        try:
            await _resolve_user(token, db)
        except HTTPException:
            return None
        return Access()


@router.websocket("/ws")
async def media_ws(ws: WebSocket):
    access = await _access(ws.query_params.get("token", ""), ws.query_params.get("kiosk", ""))
    if access is None:
        await ws.close(code=4401)
        return
    await ws.accept()
    session = MediaSession(ws, access)
    try:
        await session.run()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("Media session ended", exc_info=True)


@router.get("/stats")
async def media_stats(_: User = Depends(require_admin)):
    """Live streams in use: viewers, data in, restarts, skips (slow viewers)."""
    from app.services import media_engine

    from app.services.camera_gate import camera_gate

    return {"live": live_hub.stats(), "playbacks": media_engine._playbacks, "camera_rest": camera_gate.status()}
