import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Stat:
    """Rolling one-second window of a live JPEG feed's throughput. Frames and
    bytes accumulate until at least a second has elapsed, then fps/kbps are
    recomputed and the window resets - so the exposed numbers reflect the
    feed as it is right now, not a lifetime average that lags reality.
    """

    fps: float = 0.0
    kbps: float = 0.0
    width: int = 0
    height: int = 0
    last_update: float = field(default_factory=time.monotonic)
    _frames: int = 0
    _bytes: int = 0
    _window_start: float = field(default_factory=time.monotonic)


class StreamStats:
    """Thread-safe registry of live-feed throughput, keyed by
    (camera_id, quality). The decode threads (StreamViewer for the always-on
    substream, the HQ decoder for on-demand main streams) call record_frame
    for every JPEG they publish; the API reads snapshots to drive the
    per-tile bitrate overlay. Entries not updated recently are treated as
    stale (the camera dropped) and omitted from snapshots.
    """

    STALE_AFTER_SECONDS = 5.0

    def __init__(self) -> None:
        self._stats: dict[tuple[int, str], _Stat] = {}
        self._lock = threading.Lock()

    def record_frame(self, camera_id: int, quality: str, nbytes: int, width: int, height: int) -> None:
        now = time.monotonic()
        with self._lock:
            key = (camera_id, quality)
            st = self._stats.get(key)
            if st is None:
                st = _Stat()
                self._stats[key] = st
            st._frames += 1
            st._bytes += nbytes
            st.width = width
            st.height = height
            st.last_update = now

            elapsed = now - st._window_start
            if elapsed >= 1.0:
                st.fps = round(st._frames / elapsed, 1)
                st.kbps = round((st._bytes * 8 / 1000) / elapsed, 1)
                st._frames = 0
                st._bytes = 0
                st._window_start = now

    def clear(self, camera_id: int, quality: str) -> None:
        with self._lock:
            self._stats.pop((camera_id, quality), None)

    def snapshot(self) -> list[dict]:
        now = time.monotonic()
        out: list[dict] = []
        with self._lock:
            stale = [key for key, st in self._stats.items() if now - st.last_update > self.STALE_AFTER_SECONDS]
            for key in stale:
                del self._stats[key]
            for (camera_id, quality), st in self._stats.items():
                out.append(
                    {
                        "camera_id": camera_id,
                        "quality": quality,
                        "fps": st.fps,
                        "kbps": st.kbps,
                        "width": st.width,
                        "height": st.height,
                    }
                )
        return out


stream_stats = StreamStats()
