import asyncio
import logging
import os
import threading
import time
from typing import Awaitable, Callable

import cv2

from app.services.frame_bus import frame_bus
from app.services.motion_detector import MotionDetector, MotionSample
from app.services.status_tracker import mark_offline, mark_online
from app.services.stream_stats import stream_stats

logger = logging.getLogger(__name__)

# Force OpenCV's FFmpeg backend to pull RTSP over TCP. The default is UDP, which
# on a busy Wi-Fi/LAN drops packets and shows up as the live view freezing or
# tearing - TCP trades a little latency for a stable, artefact-free stream.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")


class StreamViewer:
    """Always-on per-camera frame source: decodes the substream (falling back
    to the main stream when no substream is configured) in background OS
    threads - cv2.VideoCapture blocks, so it can't run on the asyncio loop.

    Per decoded frame it:
    - stores the native-resolution frame on the frame bus (for AI and event
      snapshots - never downscaled or recompressed);
    - feeds the motion detector (which samples ~6 fps internally);
    - publishes a downscaled, aspect-correct JPEG for live view, rate-limited.

    Tracks camera online/offline status from whether the stream can be
    opened/read at all, since this runs regardless of recording mode.
    """

    LIVE_WIDTH = 640  # live-view JPEG width; height follows the camera's aspect ratio
    LIVE_QUALITY = 80
    LIVE_MAX_FPS = 12
    NO_FRAME_TIMEOUT = 15.0  # a stream that delivers nothing for this long is treated as dropped

    def __init__(
        self,
        camera_id: int,
        stream_url: str,
        motion_enabled: bool = False,
        sensitivity: int = 50,
        motion_zones: list | None = None,
        on_motion_start: Callable[[], Awaitable[None]] | None = None,
        on_motion_stop: Callable[[], Awaitable[None]] | None = None,
    ):
        self.camera_id = camera_id
        self.stream_url = stream_url
        self.motion_enabled = motion_enabled
        self.on_motion_start = on_motion_start
        self.on_motion_stop = on_motion_stop
        self._detector = MotionDetector(sensitivity, motion_zones) if motion_enabled else None
        # The sample that started the current event, and the latest one - read
        # by the event/AI layer (e.g. to know where the motion was).
        self.trigger_sample: MotionSample | None = None
        self.last_sample: MotionSample | None = None

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            await asyncio.get_running_loop().run_in_executor(None, self._thread.join, 5)
        frame_bus.clear(self.camera_id)

    def hold_motion(self, seconds: float) -> None:
        """Keep the current motion event open for at least `seconds` more,
        even if pixels stop changing - the AI layer calls this while it still
        sees a person who is standing still."""
        if self._detector is not None:
            self._detector.hold(time.monotonic() + seconds)

    @property
    def motion_active(self) -> bool:
        return bool(self._detector and self._detector.active)

    def _schedule(self, coro: Awaitable[None]) -> None:
        if self._loop:
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _run(self) -> None:
        # This is a plain OS thread - an uncaught exception here would kill it
        # silently, permanently ending live view AND motion detection for this
        # camera. Every iteration is guarded so a transient OpenCV error
        # degrades to a reconnect retry instead.
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

                backoff = 2
                self._decode_loop(cap)  # releases cap
                frame_bus.clear(self.camera_id)

                # An event cut short by a dropped stream still gets its stop, so
                # the clip is finalised instead of left open.
                if self._detector is not None and self._detector.force_stop() and self.on_motion_stop:
                    self._schedule(self.on_motion_stop())
                if not self._stop_event.is_set():
                    self._schedule(mark_offline(self.camera_id, "Live stream dropped - reconnecting"))
                    time.sleep(backoff)
            except Exception:
                logger.exception("Unexpected error in stream viewer for camera %s", self.camera_id)
                self._schedule(mark_offline(self.camera_id))
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _decode_loop(self, cap: "cv2.VideoCapture") -> None:
        """Reads frames until the stream drops or stop is requested.

        Reading and processing run on separate threads, and processing always
        takes the *newest* frame. If processing falls behind (a busy box, or a
        main stream decoded because there's no substream), frames are dropped
        rather than queued - otherwise live view, motion and AI all drift
        seconds behind reality. (`CAP_PROP_BUFFERSIZE` doesn't achieve this for
        RTSP with OpenCV's FFmpeg backend.)
        """
        slot: dict = {"frame": None, "at": 0.0, "seq": 0, "alive": True, "quit": False}
        cond = threading.Condition()

        def reader() -> None:
            # The reader owns `cap` and releases it itself: releasing from
            # another thread while cap.read() is blocked inside FFmpeg can
            # crash the process. A stuck read returns on OpenCV's own read
            # timeout, after which this thread sees "quit" and cleans up.
            try:
                while not self._stop_event.is_set() and not slot["quit"]:
                    ok, frame = cap.read()
                    with cond:
                        if not ok:
                            slot["alive"] = False
                            cond.notify_all()
                            return
                        slot["frame"], slot["at"] = frame, time.monotonic()
                        slot["seq"] += 1
                        cond.notify_all()
            except Exception:
                logger.exception("Frame reader crashed for camera %s", self.camera_id)
            finally:
                with cond:
                    slot["alive"] = False
                    cond.notify_all()
                cap.release()

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()

        if self._detector is not None:
            self._detector.reset(now=None)  # fresh background + warmup on every (re)connect
        marked_online = False
        seen_seq = 0
        last_live_at = 0.0
        live_interval = 1.0 / self.LIVE_MAX_FPS

        try:
            while not self._stop_event.is_set():
                with cond:
                    if slot["seq"] == seen_seq and slot["alive"]:
                        cond.wait(timeout=self.NO_FRAME_TIMEOUT)
                    if slot["seq"] == seen_seq:
                        if slot["alive"]:
                            logger.warning(
                                "No frames from camera %s for %ss - reconnecting",
                                self.camera_id, self.NO_FRAME_TIMEOUT,
                            )
                        break
                    frame, captured_at, seen_seq = slot["frame"], slot["at"], slot["seq"]

                if not marked_online:
                    marked_online = True
                    self._schedule(mark_online(self.camera_id))

                frame_bus.set_raw(self.camera_id, frame, captured_at)

                if self._detector is not None:
                    sample = self._detector.process(frame, captured_at)
                    if sample is not None:
                        self.last_sample = sample
                        if sample.started:
                            self.trigger_sample = sample
                            if self.on_motion_start:
                                self._schedule(self.on_motion_start())
                        elif sample.stopped and self.on_motion_stop:
                            self._schedule(self.on_motion_stop())

                now = time.monotonic()
                if now - last_live_at >= live_interval:
                    last_live_at = now
                    self._publish_live(frame)
        finally:
            slot["quit"] = True
            # Normally returns within one frame interval; a reader stuck on a
            # dead stream is left to finish (and release) on its own.
            reader_thread.join(timeout=2)

    def _publish_live(self, frame) -> None:
        h, w = frame.shape[:2]
        if w > self.LIVE_WIDTH:
            # Aspect-preserving: the old fixed 640x360 squashed every 4:3 or
            # 5:4 substream (640x480, 704x576...) into 16:9.
            small = cv2.resize(frame, (self.LIVE_WIDTH, round(h * self.LIVE_WIDTH / w)), interpolation=cv2.INTER_AREA)
        else:
            small = frame
        ok, jpeg = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), self.LIVE_QUALITY])
        if ok and self._loop:
            data = jpeg.tobytes()
            frame_bus.publish_threadsafe(self._loop, self.camera_id, data)
            stream_stats.record_frame(self.camera_id, "sub", len(data), small.shape[1], small.shape[0])
