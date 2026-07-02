import asyncio
import logging
import threading
import time

import cv2

from app.services.frame_bus import FrameBus
from app.services.stream_stats import stream_stats

logger = logging.getLogger(__name__)

# HQ frames live on their own bus so the high-bitrate main-stream feed never
# clobbers the always-on 640x360 substream frames in the shared frame_bus -
# a grid tile and a maximised tile of the same camera can be served at the
# same time from the two buses independently.
hq_frame_bus = FrameBus()

# Keep decoding a little while after the last viewer leaves so a quick
# un-maximise / re-maximise doesn't tear down and re-establish the RTSP
# connection (which costs a couple of seconds of black screen each time).
IDLE_GRACE_SECONDS = 8.0

# The main stream exists to give a maximised / fullscreen tile the camera's
# full detail, so we serve it at native resolution (2K/4MP/4K all pass through
# untouched). The cap is only a guard against a pathologically huge frame; it
# sits above 4K so real cameras are never downscaled.
MAX_DIMENSION = 4096

# Serve every Nth decoded frame. The main stream exists for clarity when a user
# is staring at one camera, not for high frame rate, so halving it keeps CPU on
# a low-power host in check.
FRAME_SKIP = 2


class _HQDecoder:
    """One background OS thread decoding a single camera's main stream on
    demand. cv2.VideoCapture blocks, so like StreamViewer it can't live on the
    asyncio loop. Reference counted by HQStreamManager: it starts on the first
    viewer and is torn down once no viewers remain past the idle grace window.
    """

    def __init__(self, camera_id: int, url: str, loop: asyncio.AbstractEventLoop):
        self.camera_id = camera_id
        self.url = url
        self._loop = loop
        self.refcount = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        stream_stats.clear(self.camera_id, "main")

    def _run(self) -> None:
        backoff = 2
        while not self._stop.is_set():
            try:
                cap = cv2.VideoCapture(self.url)
                if not cap.isOpened():
                    logger.warning("HQ: could not open main stream for camera %s", self.camera_id)
                    cap.release()
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue
                backoff = 2
                self._decode_loop(cap)
                cap.release()
            except Exception:
                logger.exception("HQ decoder error for camera %s", self.camera_id)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _decode_loop(self, cap: "cv2.VideoCapture") -> None:
        frame_count = 0
        while not self._stop.is_set():
            ok, frame = cap.read()
            if not ok:
                break
            frame_count += 1
            if frame_count % FRAME_SKIP != 0:
                continue

            h, w = frame.shape[:2]
            if w > MAX_DIMENSION:
                scale = MAX_DIMENSION / float(w)
                frame = cv2.resize(frame, (MAX_DIMENSION, int(round(h * scale))))
                h, w = frame.shape[:2]

            ok_enc, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ok_enc:
                data = jpeg.tobytes()
                hq_frame_bus.publish_threadsafe(self._loop, self.camera_id, data)
                stream_stats.record_frame(self.camera_id, "main", len(data), w, h)


class HQStreamManager:
    """Owns the lifecycle of on-demand main-stream decoders. Viewers acquire
    on connect and release on disconnect; the decoder is reference counted and
    reaped a few seconds after the last viewer leaves.
    """

    def __init__(self) -> None:
        self._decoders: dict[int, _HQDecoder] = {}
        self._lock = threading.Lock()

    def acquire(self, camera_id: int, url: str, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            dec = self._decoders.get(camera_id)
            if dec is None:
                dec = _HQDecoder(camera_id, url, loop)
                self._decoders[camera_id] = dec
                dec.start()
            dec.refcount += 1

    def release(self, camera_id: int) -> None:
        with self._lock:
            dec = self._decoders.get(camera_id)
            if dec is None:
                return
            dec.refcount = max(0, dec.refcount - 1)
            if dec.refcount == 0:
                # Schedule a deferred reap; if a new viewer arrives inside the
                # grace window the refcount climbs back above zero and the
                # reap becomes a no-op.
                dec._loop.call_later(IDLE_GRACE_SECONDS, self._reap, camera_id)

    def _reap(self, camera_id: int) -> None:
        with self._lock:
            dec = self._decoders.get(camera_id)
            if dec is not None and dec.refcount == 0:
                dec.stop()
                del self._decoders[camera_id]

    async def shutdown(self) -> None:
        with self._lock:
            decoders = list(self._decoders.values())
            self._decoders.clear()
        for dec in decoders:
            dec.stop()


hq_stream_manager = HQStreamManager()
