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


class ActiveSegmentRegistry:
    """Tracks the file path + start time of whichever recording segment is
    currently being written for each camera. A segment only becomes a
    queryable Recording row (and a finalized file) once ffmpeg closes it - for
    a 5-minute continuous segment that's up to 5 minutes where footage exists
    on disk but the API has no record of it at all. This registry lets the
    live-segment endpoints serve that in-progress file directly (it's
    fragmented MP4, so a partial file is still valid/playable) instead of the
    last few minutes always looking like "no recording" even though the
    camera is actively recording it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._segments: dict[int, ActiveSegment] = {}

    def set(self, camera_id: int, file_path: str, started_at: datetime, playback_path: str | None = None) -> None:
        with self._lock:
            self._segments[camera_id] = ActiveSegment(file_path, started_at, playback_path)

    def clear(self, camera_id: int) -> None:
        with self._lock:
            self._segments.pop(camera_id, None)

    def get(self, camera_id: int) -> ActiveSegment | None:
        with self._lock:
            return self._segments.get(camera_id)


active_segment_registry = ActiveSegmentRegistry()
