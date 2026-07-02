from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.deps import require_admin
from app.db.session import AsyncSessionLocal, get_db
from app.models.camera import Camera
from app.models.kiosk_view import KioskView, KioskViewCamera
from app.models.recording import Recording
from app.models.user import User
from app.schemas.kiosk import (
    KioskCameraOut,
    KioskPublicOut,
    KioskRecordingOut,
    KioskViewCreate,
    KioskViewOut,
    KioskViewUpdate,
)
from app.services.live_media import (
    live_segment_info,
    live_segment_video_response,
    mjpeg_stream_response,
    snapshot_response,
)
from app.services.stream_stats import stream_stats

router = APIRouter(tags=["kiosk"])

# Kiosk's recordings access is capped to the same window as the live-view
# scrub bar's 30-minute rewind (see THIRTY_MIN_MS in CameraTile.tsx) rather
# than the full recordings history a logged-in user gets - anyone holding a
# kiosk link can rewind recent footage, but can't browse or fetch anything
# further back, even by guessing a recording id directly.
KIOSK_REWIND_MINUTES = 30


def _to_out(view: KioskView) -> KioskViewOut:
    return KioskViewOut(
        id=view.id,
        name=view.name,
        token=view.token,
        layout=view.layout,
        camera_ids=[c.camera_id for c in view.cameras],
        created_at=view.created_at,
        last_viewed_at=view.last_viewed_at,
    )


async def _set_cameras(db: AsyncSession, view: KioskView, camera_ids: list[int]) -> None:
    await db.execute(KioskViewCamera.__table__.delete().where(KioskViewCamera.kiosk_view_id == view.id))
    for position, camera_id in enumerate(camera_ids):
        db.add(KioskViewCamera(kiosk_view_id=view.id, camera_id=camera_id, position=position))


# ---------- Admin CRUD (/api/kiosk/views) ----------

admin_router = APIRouter(prefix="/api/kiosk/views", tags=["kiosk"])


@admin_router.get("", response_model=list[KioskViewOut])
async def list_kiosk_views(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    result = await db.execute(select(KioskView).options(selectinload(KioskView.cameras)))
    return [_to_out(v) for v in result.scalars().all()]


@admin_router.post("", response_model=KioskViewOut, status_code=status.HTTP_201_CREATED)
async def create_kiosk_view(payload: KioskViewCreate, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    view = KioskView(name=payload.name, layout=payload.layout)
    db.add(view)
    await db.flush()
    await _set_cameras(db, view, payload.camera_ids)
    await db.commit()

    result = await db.execute(select(KioskView).options(selectinload(KioskView.cameras)).where(KioskView.id == view.id))
    return _to_out(result.scalar_one())


@admin_router.put("/{view_id}", response_model=KioskViewOut)
async def update_kiosk_view(
    view_id: int, payload: KioskViewUpdate, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)
):
    result = await db.execute(select(KioskView).options(selectinload(KioskView.cameras)).where(KioskView.id == view_id))
    view = result.scalar_one_or_none()
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Kiosk view not found")

    if payload.name is not None:
        view.name = payload.name
    if payload.layout is not None:
        view.layout = payload.layout
    if payload.camera_ids is not None:
        await _set_cameras(db, view, payload.camera_ids)
    await db.commit()

    result = await db.execute(select(KioskView).options(selectinload(KioskView.cameras)).where(KioskView.id == view_id))
    return _to_out(result.scalar_one())


@admin_router.post("/{view_id}/regenerate-token", response_model=KioskViewOut)
async def regenerate_token(view_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    from app.models.kiosk_view import _generate_token

    result = await db.execute(select(KioskView).options(selectinload(KioskView.cameras)).where(KioskView.id == view_id))
    view = result.scalar_one_or_none()
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Kiosk view not found")

    view.token = _generate_token()
    await db.commit()
    await db.refresh(view)
    return _to_out(view)


@admin_router.delete("/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kiosk_view(view_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    view = await db.get(KioskView, view_id)
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Kiosk view not found")
    await db.delete(view)
    await db.commit()


# ---------- Public, unauthenticated (/api/kiosk/public/{token}) ----------
# No login is possible or required here by design - the token itself (256
# bits, generated server-side, never guessable) is the only access control.
# Every lookup below is scoped to exactly the cameras configured for this
# token's view; nothing else in the system is reachable through this path.
#
# This surface mirrors the authenticated Live View feature-for-feature
# (adaptive main/sub stream, freeze-frame snapshot, the in-progress recording
# segment, and browsing/playing finalized recordings) so a kiosk link is a
# full substitute for logging in and opening Live View - by design, anyone
# holding the link can scrub back and watch stored footage for these cameras,
# not just the current live feed.

public_router = APIRouter(prefix="/api/kiosk/public", tags=["kiosk"])


async def _get_view_by_token(token: str, db: AsyncSession) -> KioskView:
    result = await db.execute(
        select(KioskView).options(selectinload(KioskView.cameras)).where(KioskView.token == token)
    )
    view = result.scalar_one_or_none()
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This kiosk link is no longer valid")
    return view


async def _require_view_camera(token: str, camera_id: int, db: AsyncSession) -> tuple[KioskView, Camera]:
    view = await _get_view_by_token(token, db)
    if camera_id not in [c.camera_id for c in view.cameras]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not in this kiosk view")
    camera = await db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found")
    return view, camera


@public_router.get("/{token}", response_model=KioskPublicOut)
async def get_public_view(token: str, db: AsyncSession = Depends(get_db)):
    view = await _get_view_by_token(token, db)

    camera_ids = [c.camera_id for c in view.cameras]
    cameras_out: list[KioskCameraOut] = []
    if camera_ids:
        result = await db.execute(select(Camera).where(Camera.id.in_(camera_ids)))
        by_id = {c.id: c for c in result.scalars().all()}
        for cid in camera_ids:
            camera = by_id.get(cid)
            if camera is not None:
                cameras_out.append(
                    KioskCameraOut(id=camera.id, name=camera.name, status=camera.status, recording_mode=camera.recording_mode)
                )

    async with AsyncSessionLocal() as write_db:
        live_view = await write_db.get(KioskView, view.id)
        live_view.last_viewed_at = datetime.now(timezone.utc)
        await write_db.commit()

    return KioskPublicOut(name=view.name, layout=view.layout, cameras=cameras_out)


@public_router.get("/{token}/stream-stats")
async def get_public_stream_stats(token: str, db: AsyncSession = Depends(get_db)):
    view = await _get_view_by_token(token, db)
    allowed = {c.camera_id for c in view.cameras}
    return {"stats": [s for s in stream_stats.snapshot() if s["camera_id"] in allowed]}


@public_router.get("/{token}/cameras/{camera_id}/stream.mjpeg")
async def get_public_stream(
    token: str,
    camera_id: int,
    quality: str = Query(default="sub", pattern="^(sub|main)$"),
    db: AsyncSession = Depends(get_db),
):
    _, camera = await _require_view_camera(token, camera_id, db)
    return await mjpeg_stream_response(camera, quality)


@public_router.get("/{token}/cameras/{camera_id}/snapshot.jpg")
async def get_public_snapshot(token: str, camera_id: int, db: AsyncSession = Depends(get_db)):
    await _require_view_camera(token, camera_id, db)
    return snapshot_response(camera_id)


@public_router.get("/{token}/cameras/{camera_id}/live-segment")
async def get_public_live_segment(token: str, camera_id: int, db: AsyncSession = Depends(get_db)):
    await _require_view_camera(token, camera_id, db)
    return live_segment_info(camera_id)


@public_router.get("/{token}/cameras/{camera_id}/live-segment/video")
async def get_public_live_segment_video(token: str, camera_id: int, db: AsyncSession = Depends(get_db)):
    await _require_view_camera(token, camera_id, db)
    return live_segment_video_response(camera_id)


@public_router.get("/{token}/cameras/{camera_id}/recordings", response_model=list[KioskRecordingOut])
async def list_public_recordings(
    token: str,
    camera_id: int,
    limit: int = Query(default=20, le=100),
    db: AsyncSession = Depends(get_db),
):
    await _require_view_camera(token, camera_id, db)
    # DB datetimes are naive UTC wall-clock (see recordings.py's export route
    # for the same convention) - the cutoff must match or the comparison
    # silently breaks (SQLite compares these lexicographically as text).
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=KIOSK_REWIND_MINUTES)
    stmt = (
        select(Recording)
        .where(Recording.camera_id == camera_id, Recording.ended_at >= cutoff)
        .order_by(Recording.started_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return result.scalars().all()


@public_router.get("/{token}/recordings/{recording_id}/video")
async def get_public_recording_video(token: str, recording_id: int, db: AsyncSession = Depends(get_db)):
    view = await _get_view_by_token(token, db)
    allowed = {c.camera_id for c in view.cameras}
    recording = await db.get(Recording, recording_id)
    if recording is None or recording.camera_id not in allowed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording not found")
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=KIOSK_REWIND_MINUTES)
    if recording.ended_at is None or recording.ended_at < cutoff:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording not found")
    return FileResponse(recording.file_path, media_type="video/mp4")
