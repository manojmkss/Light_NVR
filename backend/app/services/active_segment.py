import threading
from dataclasses import dataclass
from datetime import datetime


@dataclass
class ActiveSegment:
    file_path: str
    started_at: datetime
    # In-progress H.264 sub-stream copy, when the camera has a distinct sub
    # stream. Browsers can play this raw (unlike an H.265 main), so the
    # live-segment endpoint serves it to make the last few minutes watchable.
    playback_path: str | None = None
    # Set once ffmpeg has closed the file; it then waits (seconds to a minute
    # under load) for remux + thumbnail + its Recording row.
    ended_at: datetime | None = None


class ActiveSegmentRegistry:
    """Tracks the continuous-recording segments that exist on disk but have no
    Recording row yet, per camera:

    - the one being written right now (a 5-minute segment is up to 5 minutes
      of footage with no Recording row at all), and
    - segments ffmpeg has closed that are still being finalized (made seekable,
      playback copy, thumbnail) before they're registered.

    Without the second list, the moment a segment closed its footage vanished
    from live-view scrubbing until its Recording row appeared - the "No
    recording at this moment" gap after every segment change. The playback
    resolver (playback.py) looks here after the Recording table.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._segments: dict[int, ActiveSegment] = {}
        self._closing: dict[int, list[ActiveSegment]] = {}

    def set(self, camera_id: int, file_path: str, started_at: datetime, playback_path: str | None = None) -> None:
        with self._lock:
            self._segments[camera_id] = ActiveSegment(file_path, started_at, playback_path)

    def clear(self, camera_id: int) -> None:
        with self._lock:
            self._segments.pop(camera_id, None)

    def get(self, camera_id: int) -> ActiveSegment | None:
        with self._lock:
            return self._segments.get(camera_id)

    def close(self, camera_id: int, file_path: str, ended_at: datetime) -> None:
        """The segment at `file_path` stopped growing; keep it findable until
        `finalized()` is called for it."""
        with self._lock:
            seg = self._segments.get(camera_id)
            if seg is None or seg.file_path != file_path:
                return
            self._segments.pop(camera_id, None)
            closed = ActiveSegment(seg.file_path, seg.started_at, seg.playback_path, ended_at)
            self._closing.setdefault(camera_id, []).append(closed)

    def finalized(self, camera_id: int, file_path: str) -> None:
        with self._lock:
            left = [s for s in self._closing.get(camera_id, []) if s.file_path != file_path]
            if left:
                self._closing[camera_id] = left
            else:
                self._closing.pop(camera_id, None)

    def closing(self, camera_id: int) -> list[ActiveSegment]:
        with self._lock:
            return list(self._closing.get(camera_id, []))


active_segment_registry = ActiveSegmentRegistry()
