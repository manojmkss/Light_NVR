import asyncio
import time

import numpy as np


class FrameBus:
    """In-memory latest-frame store and pub/sub used to fan out decoded JPEG
    frames to live-view subscribers without each viewer triggering its own
    decode of the camera stream.

    Two stores per camera:
    - `_latest`: the live-view JPEG (downscaled, rate-limited) that tiles and
      MJPEG streams show.
    - `_raw`: the newest decoded frame at the substream's *native* resolution,
      with its capture time. This is what AI and event snapshots use - the
      live-view JPEG is too small and too compressed for either, and without a
      timestamp there was no way to tell a fresh frame from one left over from
      before the camera dropped.
    """

    def __init__(self) -> None:
        self._latest: dict[int, bytes] = {}
        self._raw: dict[int, tuple[np.ndarray, float]] = {}
        self._subscribers: dict[int, set[asyncio.Queue]] = {}

    def publish_threadsafe(self, loop: asyncio.AbstractEventLoop, camera_id: int, jpeg_bytes: bytes) -> None:
        asyncio.run_coroutine_threadsafe(self.publish(camera_id, jpeg_bytes), loop)

    async def publish(self, camera_id: int, jpeg_bytes: bytes) -> None:
        self._latest[camera_id] = jpeg_bytes
        for queue in list(self._subscribers.get(camera_id, set())):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(jpeg_bytes)
            except asyncio.QueueFull:
                pass

    def get_latest(self, camera_id: int) -> bytes | None:
        return self._latest.get(camera_id)

    def has_subscribers(self, camera_id: int) -> bool:
        return bool(self._subscribers.get(camera_id))

    # ── native-resolution frames for AI / snapshots ─────────────────────────

    def set_raw(self, camera_id: int, frame: np.ndarray, captured_at: float) -> None:
        """Called from the decoder thread. A single dict assignment, so it's
        atomic under the GIL - readers always see a whole (frame, time) pair.
        The decoder hands over a new array per frame and never mutates it."""
        self._raw[camera_id] = (frame, captured_at)

    def get_fresh(self, camera_id: int, max_age: float = 1.5) -> tuple[np.ndarray, float] | None:
        """The newest native frame if it was captured within `max_age` seconds
        (monotonic clock), else None - a stale frame must not be analysed or
        sent as "what's happening now"."""
        item = self._raw.get(camera_id)
        if item is None or time.monotonic() - item[1] > max_age:
            return None
        return item

    def get_fresh_jpeg(self, camera_id: int, max_age: float = 1.5, quality: int = 90) -> bytes | None:
        item = self.get_fresh(camera_id, max_age)
        if item is None:
            return None
        import cv2

        ok, buf = cv2.imencode(".jpg", item[0], [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return buf.tobytes() if ok else None

    def clear(self, camera_id: int) -> None:
        """Forget a camera's frames when its stream drops or its worker stops,
        so nothing downstream mistakes the last frame before a disconnect for a
        live one."""
        self._raw.pop(camera_id, None)
        self._latest.pop(camera_id, None)

    def subscribe(self, camera_id: int) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        self._subscribers.setdefault(camera_id, set()).add(queue)
        return queue

    def unsubscribe(self, camera_id: int, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(camera_id)
        if subs and queue in subs:
            subs.discard(queue)


frame_bus = FrameBus()
