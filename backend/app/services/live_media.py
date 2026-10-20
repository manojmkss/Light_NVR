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

# How long to wait for a new frame before re-sending the last one to hold the
# connection open. Comfortably under the 60s idle timeout proxies typically
# default to, so an MJPEG viewer is never dropped for being quiet.
KEEPALIVE_SECONDS = 10


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
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    # The camera has gone quiet - reconnecting, a brief network
                    # drop, a reboot. Ending the response here would complete
                    # the multipart stream *cleanly*, and a clean end is not an
                    # error to an <img>: it fires no onerror, so the viewer has
                    # nothing to react to and the tile sits frozen/blank until
                    # something else reloads the page. Re-send the last known
                    # frame instead. That keeps this connection (and any proxy
                    # in front of it) alive, and the tile picks straight back up
                    # on its own the moment real frames return.
                    frame = bus.get_latest(camera.id)
                    if frame is None:
                        continue  # nothing to hold the picture with yet
                yield wrap_mjpeg_frame(frame)
        finally:
            bus.unsubscribe(camera.id, queue)
            if quality == "main":
                hq_stream_manager.release(camera.id)

    return StreamingResponse(
        frame_generator(),
        media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
    )


def snapshot_response(camera_id: int) -> Response:
    # Full-resolution still when the stream is live; the live-view JPEG otherwise.
    frame = frame_bus.get_fresh_jpeg(camera_id, max_age=3.0) or frame_bus.get_latest(camera_id)
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
    if seg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No segment currently recording")
    # Prefer the in-progress H.264 sub-stream copy when the camera has one: an
    # H.265 main won't decode in Firefox/most Chrome (black screen), so serving
    # the sub is what makes the last few minutes - the segment still being
    # written - actually watchable. Falls back to the main file otherwise.
    path = seg.playback_path if seg.playback_path and os.path.exists(seg.playback_path) else seg.file_path
    if not os.path.exists(path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No segment currently recording")
    return FileResponse(path, media_type="video/mp4")
