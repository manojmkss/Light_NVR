import asyncio
import logging
import os
import threading
import time
from typing import Awaitable, Callable

import cv2

from app.services.frame_bus import frame_bus
from app.services.status_tracker import mark_offline, mark_online
from app.services.stream_stats import stream_stats

logger = logging.getLogger(__name__)

# Force OpenCV's FFmpeg backend to pull RTSP over TCP. The default is UDP, which
# on a busy Wi-Fi/LAN drops packets and shows up as the live view freezing or
# tearing - TCP trades a little latency for a stable, artefact-free stream.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")


class StreamViewer:
    """Always-on per-camera frame source: decodes the substream (falling back
    to the main stream when no substream is configured) in a background OS
    thread - cv2.VideoCapture blocks, so it can't run on the asyncio loop -
    and publishes JPEG snapshots to the frame bus for live view and
    thumbnails. Tracks camera online/offline status from whether the stream
    can be opened/read at all, since this runs regardless of recording mode.

    When motion_enabled, it also runs MOG2 background subtraction on the same
    decoded frames so motion detection doesn't require a second decode of the
    stream - important on low-power hosts like a Raspberry Pi.
    """

    MOTION_END_DEBOUNCE_SECONDS = 3
    WARMUP_FRAMES = 30
    # Motion detection is expensive (MOG2) and doesn't need every frame; sample
    # it so the CPU freed up can go to publishing a smoother live view.
    MOTION_FRAME_SKIP = 3

    def __init__(
        self,
        camera_id: int,
        stream_url: str,
        motion_enabled: bool = False,
        sensitivity: int = 50,
        on_motion_start: Callable[[], Awaitable[None]] | None = None,
        on_motion_stop: Callable[[], Awaitable[None]] | None = None,
    ):
        self.camera_id = camera_id
        self.stream_url = stream_url
        self.motion_enabled = motion_enabled
        self.min_area = self._sensitivity_to_area(sensitivity)
        self.on_motion_start = on_motion_start
        self.on_motion_stop = on_motion_stop

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @staticmethod
    def _sensitivity_to_area(sensitivity: int) -> int:
        sensitivity = max(1, min(100, sensitivity))
        return int(5000 - (sensitivity / 100) * 4800)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            await asyncio.get_running_loop().run_in_executor(None, self._thread.join, 5)

    def _schedule(self, coro: Awaitable[None]) -> None:
        if self._loop:
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _run(self) -> None:
        # This is a plain OS thread (cv2.VideoCapture blocks, so it can't run
        # on the asyncio loop) - an uncaught exception here just kills the
        # thread silently (Python only prints it to stderr), permanently
        # ending live view AND motion detection for this camera with no
        # visible error until the app is restarted. Every iteration is
        # guarded so a transient OpenCV error degrades to a reconnect retry
        # instead.
        backoff = 2
        while not self._stop_event.is_set():
            try:
                cap = cv2.VideoCapture(self.stream_url)
                if not cap.isOpened():
                    logger.warning("Could not open stream for camera %s", self.camera_id)
                    cap.release()
                    # OpenCV gives no reason for the failure; name the likely
                    # causes so the Cameras page shows something actionable.
                    self._schedule(mark_offline(
                        self.camera_id,
                        "Could not connect to the live stream - camera unreachable, or the "
                        "stream URL/credentials are wrong (try Re-detect)",
                    ))
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue

                # Keep only the newest frame buffered so live view stays close to
                # real time instead of drifting seconds behind under load.
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

                backoff = 2
                motion_was_active = self._decode_loop(cap)
                cap.release()

                if motion_was_active and self.on_motion_stop:
                    self._schedule(self.on_motion_stop())
                if not self._stop_event.is_set():
                    self._schedule(mark_offline(self.camera_id, "Live stream dropped - reconnecting"))
                    time.sleep(backoff)
            except Exception:
                logger.exception("Unexpected error in stream viewer for camera %s", self.camera_id)
                self._schedule(mark_offline(self.camera_id))
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _decode_loop(self, cap: "cv2.VideoCapture") -> bool:
        """Reads frames until the stream drops or stop is requested. Returns
        whether motion was active at the moment the loop exited, so the
        caller can emit a closing motion-stop for an interrupted clip.
        """
        bg_subtractor = (
            cv2.createBackgroundSubtractorMOG2(history=200, varThreshold=32, detectShadows=False)
            if self.motion_enabled
            else None
        )
        motion_active = False
        last_motion_at = 0.0
        frame_count = 0
        marked_online = False

        while not self._stop_event.is_set():
            ok, frame = cap.read()
            if not ok:
                break

            frame_count += 1
            if not marked_online:
                marked_online = True
                self._schedule(mark_online(self.camera_id))

            small = cv2.resize(frame, (640, 360))

            if bg_subtractor is not None and frame_count % self.MOTION_FRAME_SKIP == 0:
                fg_mask = bg_subtractor.apply(small)
                _, thresh = cv2.threshold(fg_mask, 200, 255, cv2.THRESH_BINARY)
                thresh = cv2.dilate(thresh, None, iterations=2)
                contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                detected = any(cv2.contourArea(c) > self.min_area for c in contours)

                now = time.monotonic()
                if detected:
                    last_motion_at = now
                    if not motion_active and frame_count > self.WARMUP_FRAMES:
                        motion_active = True
                        if self.on_motion_start:
                            self._schedule(self.on_motion_start())
                elif motion_active and (now - last_motion_at) > self.MOTION_END_DEBOUNCE_SECONDS:
                    motion_active = False
                    if self.on_motion_stop:
                        self._schedule(self.on_motion_stop())

            # Publish every decoded frame so the live view runs at the camera's
            # own substream frame rate rather than a third of it.
            ok_enc, jpeg = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
            if ok_enc and self._loop:
                data = jpeg.tobytes()
                frame_bus.publish_threadsafe(self._loop, self.camera_id, data)
                stream_stats.record_frame(self.camera_id, "sub", len(data), small.shape[1], small.shape[0])

        return motion_active
