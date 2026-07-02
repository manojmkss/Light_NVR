import asyncio
import os

from fastapi import HTTPException, status
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.models.camera import Camera
from app.schemas.common import serialize_utc
from app.services.active_segment import active_segment_registry
from app.services.frame_bus import frame_bus
from app.services.hq_stream import hq_frame_bus, hq_stream_manager

# Shared by every surface that serves live/recorded media - the authenticated
# Live View and the public, token-scoped Kiosk view both call these so the two
# never drift out of sync with each other.

MJPEG_BOUNDARY = "lightnvrframe"


def wrap_mjpeg_frame(jpeg_bytes: bytes) -> bytes:
    return (
        f"--{MJPEG_BOUNDARY}\r\n"
        f"Content-Type: image/jpeg\r\n"
        f"Content-Length: {len(jpeg_bytes)}\r\n\r\n"
    ).encode() + jpeg_bytes + b"\r\n"


async def mjpeg_stream_response(camera: Camera, quality: str) -> StreamingResponse:
    """quality=sub (default) serves the always-on 640x360 substream. quality=main
    spins up an on-demand decode of the main stream, reference counted so it
    tears down shortly after the last viewer (from either surface) disconnects.
    """
    if quality == "main":
        loop = asyncio.get_running_loop()
        hq_stream_manager.acquire(camera.id, camera.rtsp_main_url, loop)
        bus = hq_frame_bus
    else:
        bus = frame_bus

    async def frame_generator():
        queue = bus.subscribe(camera.id)
        try:
            latest = bus.get_latest(camera.id)
            if latest is not None:
                yield wrap_mjpeg_frame(latest)
            while True:
                frame = await asyncio.wait_for(queue.get(), timeout=30)
                yield wrap_mjpeg_frame(frame)
        except asyncio.TimeoutError:
            return
        finally:
            bus.unsubscribe(camera.id, queue)
            if quality == "main":
                hq_stream_manager.release(camera.id)

    return StreamingResponse(
        frame_generator(),
        media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
    )


def snapshot_response(camera_id: int) -> Response:
    frame = frame_bus.get_latest(camera_id)
    if frame is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="No frame available yet")
    return Response(content=frame, media_type="image/jpeg")


def live_segment_info(camera_id: int) -> dict:
    seg = active_segment_registry.get(camera_id)
    if seg is None or not os.path.exists(seg.file_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No segment currently recording")
    return {"started_at": serialize_utc(seg.started_at)}


def live_segment_video_response(camera_id: int) -> FileResponse:
    seg = active_segment_registry.get(camera_id)
    if seg is None or not os.path.exists(seg.file_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No segment currently recording")
    return FileResponse(seg.file_path, media_type="video/mp4")
