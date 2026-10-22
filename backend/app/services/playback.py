""""What plays at this moment?" - one answer for live-view scrubbing.

The live tile used to work this out itself from a list of recent recordings
it polled every 30s plus the one segment being written, so it kept showing
"No recording at this moment" for footage that existed: the list was stale,
a segment vanished from both places while it was being finalized, and the
in-progress file couldn't be seeked. The NVR knows exactly what's on disk, so
the tile now just asks:

  resolve(camera, t) -> the file covering t, the wall time of its first frame
                        (to seek to t), where it ends, or where footage resumes.

Footage is looked for in order: Recording rows, segments that are closed but
still being finalized, the segment being written. A segment without a
Recording row is served as a *snapshot*: an immutable faststart copy of
what's on disk so far (a stream copy, ~a second), which every browser can
seek - unlike the growing fragmented file, which has no index until it
closes and was served without Range support.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from app.models.recording import Recording
from app.services.active_segment import ActiveSegment, active_segment_registry
from app.services.storage_manager import storage_manager

logger = logging.getLogger(__name__)

SNAP_REUSE_SECONDS = 4.0  # a growing segment's snapshot is reused this long
SNAP_KEEP_SECONDS = 15 * 60  # long enough to watch a whole 5-minute snapshot
SNAP_MAX_PER_CAMERA = 8
MERGE_GAP = 2.0  # timeline spans closer than this are drawn as one


def _ts(dt: datetime | None) -> float | None:
    """DB datetimes are naive UTC; registry ones are aware."""
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def _naive(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


@dataclass
class Snapshot:
    snap_id: str
    path: str
    source: str
    origin: float  # wall time of the snapshot's first frame
    end: float  # wall time of its last frame
    made: float  # monotonic
    growing: bool


class SnapshotStore:
    def __init__(self) -> None:
        self._snaps: dict[int, list[Snapshot]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def _dir(self, camera_id: int) -> str:
        path = os.path.join(storage_manager.cache_dir(), "playback_snaps", str(camera_id))
        os.makedirs(path, exist_ok=True)
        if camera_id not in self._snaps:
            # First use since start-up: anything here is left over from before
            # a restart and no longer referenced.
            for name in os.listdir(path):
                _remove(os.path.join(path, name))
            self._snaps[camera_id] = []
        return path

    def get(self, camera_id: int, snap_id: str) -> Snapshot | None:
        return next((s for s in self._snaps.get(camera_id, []) if s.snap_id == snap_id), None)

    async def make(self, camera_id: int, seg: ActiveSegment) -> Snapshot | None:
        lock = self._locks.setdefault(camera_id, asyncio.Lock())
        async with lock:
            # Prefer the H.264 sub-stream copy: small, and plays in every browser.
            source = seg.playback_path if seg.playback_path and _has_data(seg.playback_path) else seg.file_path
            if not _has_data(source):
                return None
            growing = seg.ended_at is None
            for s in reversed(self._snaps.get(camera_id, [])):
                if s.source == source and os.path.exists(s.path) and (
                    not growing or time.monotonic() - s.made < SNAP_REUSE_SECONDS
                ):
                    if s.growing == growing:
                        return s
            snap_id = f"{int(time.time() * 1000)}"
            out = os.path.join(self._dir(camera_id), f"{snap_id}.mp4")
            # The last write to a growing file is (to within a fragment) when
            # its newest frame was captured; a closed one ended at ended_at.
            end_wall = _ts(seg.ended_at) if seg.ended_at else os.path.getmtime(source)
            if not await _remux(source, out):
                return None
            from app.services.ffmpeg_recorder import probe_duration

            duration = await probe_duration(out)
            if not duration:
                _remove(out)
                return None
            snap = Snapshot(snap_id, out, source, end_wall - duration, end_wall, time.monotonic(), growing)
            self._snaps.setdefault(camera_id, []).append(snap)
            self._prune(camera_id)
            return snap

    def _prune(self, camera_id: int) -> None:
        snaps = self._snaps.get(camera_id, [])
        now = time.monotonic()
        # The newest one was just handed to a viewer: never prune it.
        keep = [s for s in snaps[:-1] if now - s.made < SNAP_KEEP_SECONDS][-(SNAP_MAX_PER_CAMERA - 1):] + snaps[-1:]
        for s in snaps:
            if s not in keep:
                _remove(s.path)
        self._snaps[camera_id] = keep


def _has_data(path: str | None) -> bool:
    return bool(path) and os.path.exists(path) and os.path.getsize(path) > 0


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


async def _remux(src: str, out: str) -> bool:
    tmp = f"{out}.tmp"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src, "-c", "copy",
           "-movflags", "+faststart", "-f", "mp4", tmp]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        _remove(tmp)
        logger.warning("Playback snapshot of %s timed out", src)
        return False
    except Exception:
        logger.exception("Playback snapshot of %s failed to start", src)
        return False
    # A growing file ends mid-fragment, so ffmpeg may complain and still
    # produce a perfectly good copy of everything before it.
    if _has_data(tmp):
        os.replace(tmp, out)
        return True
    _remove(tmp)
    return False


snapshots = SnapshotStore()


def _segments(camera_id: int) -> list[ActiveSegment]:
    segs = active_segment_registry.closing(camera_id)
    active = active_segment_registry.get(camera_id)
    if active is not None:
        segs.append(active)
    return segs


async def resolve(db, camera_id: int, at: float, not_before: float | None = None) -> dict:
    """What to play at wall time `at` (unix seconds) on this camera."""
    now = time.time()
    base = {"server_now": now, "at": at}
    if not_before is not None and at < not_before:
        at = not_before
    row = (await db.execute(
        select(Recording)
        .where(Recording.camera_id == camera_id, Recording.started_at <= _naive(at), Recording.ended_at >= _naive(at))
        .order_by(Recording.started_at.desc()).limit(1)
    )).scalar_one_or_none()
    if row is not None:
        return {**base, "found": True, "kind": "recording", "recording_id": row.id, "codec": row.codec,
                "origin": _ts(row.started_at), "end": _ts(row.ended_at), "growing": False}

    for seg in _segments(camera_id):
        start = _ts(seg.started_at)
        end = _ts(seg.ended_at) if seg.ended_at else now
        if start - 5 <= at <= end:
            snap = await snapshots.make(camera_id, seg)
            if snap is not None:
                return {**base, "found": True, "kind": "segment", "snapshot": snap.snap_id,
                        "origin": snap.origin, "end": snap.end, "growing": snap.growing}

    nxt = await next_footage(db, camera_id, at)
    return {**base, "found": False, "next_at": nxt}


async def next_footage(db, camera_id: int, after: float) -> float | None:
    row = (await db.execute(
        select(Recording.started_at)
        .where(Recording.camera_id == camera_id, Recording.started_at > _naive(after))
        .order_by(Recording.started_at).limit(1)
    )).scalar_one_or_none()
    candidates = [_ts(row)] if row is not None else []
    candidates += [_ts(s.started_at) for s in _segments(camera_id) if _ts(s.started_at) > after]
    return min(candidates) if candidates else None


async def timeline(db, camera_id: int, start: float, end: float) -> dict:
    """Which parts of [start, end] have footage - for the scrub bar."""
    rows = (await db.execute(
        select(Recording.started_at, Recording.ended_at)
        .where(Recording.camera_id == camera_id, Recording.ended_at >= _naive(start),
               Recording.started_at <= _naive(end))
    )).all()
    now = time.time()
    spans = [(_ts(a), _ts(b)) for a, b in rows if a and b]
    spans += [(_ts(s.started_at), _ts(s.ended_at) if s.ended_at else now) for s in _segments(camera_id)]
    return {"server_now": now, "spans": merge_spans(spans, start, end)}


def merge_spans(spans: list[tuple[float, float]], start: float, end: float) -> list[list[float]]:
    out: list[list[float]] = []
    for a, b in sorted((max(a, start), min(b, end)) for a, b in spans if b >= start and a <= end):
        if out and a <= out[-1][1] + MERGE_GAP:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [[round(a, 3), round(b, 3)] for a, b in out]
