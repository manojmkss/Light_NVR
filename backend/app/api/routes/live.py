from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_current_user_flexible
from app.db.session import get_db
from app.models.camera import Camera
from app.models.user import User
from app.schemas.camera import MotionStatusOut
from app.services.live_media import (
    live_segment_info,
    live_segment_video_response,
    mjpeg_stream_response,
    snapshot_response,
)
from app.services.motion_state import motion_state_registry
from app.services.stream_stats import stream_stats

router = APIRouter(prefix="/api/cameras", tags=["live"])


async def _get_camera(camera_id: int, db: AsyncSession) -> Camera:
    camera = await db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found")
    return camera


@router.get("/stream-stats")
async def get_stream_stats(_: User = Depends(get_current_user)):
    """Live per-feed throughput (fps / kbps / resolution) for every camera
    currently being decoded, keyed by camera_id and quality (sub|main). Drives
    the bitrate readout in each live tile's overlay.
    """
    return {"stats": stream_stats.snapshot()}


@router.get("/motion-status", response_model=dict[int, MotionStatusOut])
async def get_motion_status(_: User = Depends(get_current_user)):
    """Live "is motion active right now" state for every camera with motion
    detection enabled, keyed by camera_id. The event log only records a
    one-shot "motion started" row (no "stopped" counterpart), so this is the
    only place that current on/off state - and the timestamp of the last
    transition either way - can be read from; API consumers (e.g. a Home
    Assistant motion sensor) poll this instead of the event log.
    """
    return motion_state_registry.snapshot()


@router.get("/{camera_id}/live-segment")
async def get_live_segment(
    camera_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user_flexible),
):
    """Metadata for the segment currently being recorded for this camera, if
    any. Continuous recording writes in fixed-length chunks and only becomes
    a queryable Recording row once a chunk finishes - without this, scrubbing
    into the last few minutes always looks like "no recording" even though
    the camera is actively writing it to disk right now.
    """
    await _get_camera(camera_id, db)
    return live_segment_info(camera_id)


@router.get("/{camera_id}/live-segment/video")
async def get_live_segment_video(
    camera_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user_flexible),
):
    """Streams the in-progress recording segment as it stands right now. It's
    fragmented MP4 (see ffmpeg_recorder._build_record_cmd), so this snapshot -
    everything ffmpeg has flushed to disk so far - is itself valid, playable
    video even though the underlying file is still being appended to.
    """
    await _get_camera(camera_id, db)
    return live_segment_video_response(camera_id)


@router.get("/{camera_id}/snapshot.jpg")
async def get_snapshot(
    camera_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user_flexible),
):
    await _get_camera(camera_id, db)
    return snapshot_response(camera_id)


@router.get("/{camera_id}/stream.mjpeg")
async def get_mjpeg_stream(
    camera_id: int,
    quality: str = Query(default="sub", pattern="^(sub|main)$"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user_flexible),
):
    """Live MJPEG feed. quality=sub (default) serves the always-on 640x360
    substream that powers the grid. quality=main spins up an on-demand decode
    of the camera's main stream (up to 1080p) for when a single tile is
    maximised; it is reference counted and torn down shortly after the last
    viewer disconnects.
    """
    camera = await _get_camera(camera_id, db)
    return await mjpeg_stream_response(camera, quality)
