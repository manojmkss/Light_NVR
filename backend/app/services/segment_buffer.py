import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass

from app.services.status_tracker import mark_offline
from app.services.storage_manager import storage_manager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BufferSegment:
    path: str
    start: float  # wall clock (unix seconds)
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def _buffer_dir(camera_id: int, name: str) -> str:
    # A sub-directory of the camera's cache dir: startup orphan cleanup and the
    # storage mover only look at files directly in the camera dir, so buffer
    # segments are never mistaken for (or moved as) recordings.
    return os.path.join(storage_manager.cache_dir(), str(camera_id), "buffer", name)


def build_segment_cmd(input_url: str, pattern: str, audio_mode: str, segment_seconds: float) -> list[str]:
    """ffmpeg stream-copying `input_url` into short MPEG-TS segments, printing
    one CSV line (`file,start,end`) to stdout as each segment closes.

    MPEG-TS rather than MP4: a TS file is valid at every packet boundary (no
    moov to finalise), holds H.264/H.265/AAC as-is, and concatenates cleanly -
    the finished clip is then stream-copied into MP4.
    """
    if input_url.startswith(("rtsp://", "rtsps://")):
        # Same transport and silence-timeout reasoning as the recorders.
        input_args = ["-rtsp_transport", "tcp", "-timeout", "10000000", "-i", input_url]
    else:
        input_args = ["-re", "-i", input_url]  # local files (tests): read at real-time speed
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *input_args, "-map", "0:v:0", "-c:v", "copy"]
    if audio_mode == "copy":
        cmd += ["-map", "0:a:0?", "-c:a", "copy"]
    elif audio_mode == "aac":
        cmd += ["-map", "0:a:0?", "-c:a", "aac"]
    else:
        cmd += ["-an"]
    cmd += [
        "-f", "segment",
        "-segment_format", "mpegts",
        # Splits only ever happen on a keyframe, so real segments are
        # max(segment_seconds, camera GOP) long.
        "-segment_time", str(segment_seconds),
        "-reset_timestamps", "1",
        "-segment_list", "pipe:1",
        "-segment_list_type", "csv",
        pattern,
    ]
    return cmd


class SegmentBuffer:
    """Keeps the last few seconds of a camera stream on disk as short segments,
    continuously, so a motion clip can start *before* motion was detected and
    with no RTSP connect / keyframe wait at the moment it matters.

    Segment wall-clock times: ffmpeg reports each segment's start/end in stream
    time as it closes. The offset to wall time is estimated as the minimum of
    (time the line arrived - reported end) over the run - arrival can only lag
    the true end, so the minimum is the best estimate and is typically within a
    few milliseconds.

    Segments older than `keep_seconds` are deleted unless an open event holds
    them (`hold`/`release`).
    """

    SEGMENT_SECONDS = 2
    PRUNE_MARGIN = 10.0

    def __init__(
        self,
        camera_id: int,
        input_url: str,
        audio_mode_resolver=None,
        name: str = "main",
        keep_seconds: float = 5.0,
        report_offline: bool = True,
    ):
        self.camera_id = camera_id
        self.input_url = input_url
        self.name = name
        self.keep_seconds = keep_seconds
        self.report_offline = report_offline
        # Coroutine returning "copy" | "aac" | "none"; resolved once per buffer.
        self._audio_mode_resolver = audio_mode_resolver
        self._audio_mode: str | None = None
        self.directory = _buffer_dir(camera_id, name)
        self._segments: list[BufferSegment] = []
        self._holds: dict[object, float] = {}
        self._proc: asyncio.subprocess.Process | None = None
        self._stop = asyncio.Event()
        self._new_segment = asyncio.Condition()
        self._run_no = 0
        self.running = False  # ffmpeg is up and producing segments

    # ── queries ──────────────────────────────────────────────────────────────

    @property
    def segments(self) -> list[BufferSegment]:
        return list(self._segments)

    def latest_end(self) -> float | None:
        return self._segments[-1].end if self._segments else None

    def segments_overlapping(self, start: float, end: float) -> list[BufferSegment]:
        return [s for s in self._segments if s.end > start and s.start < end and os.path.exists(s.path)]

    async def wait_until_covered(self, until: float, timeout: float) -> bool:
        """Wait until a closed segment reaches wall time `until` (the segment
        containing it has to finish first). False on timeout or if the buffer
        isn't producing - the caller then uses whatever is there."""
        deadline = time.time() + timeout
        async with self._new_segment:
            while (self.latest_end() or 0) < until:
                remaining = deadline - time.time()
                if remaining <= 0 or self._stop.is_set():
                    return False
                try:
                    await asyncio.wait_for(self._new_segment.wait(), timeout=min(remaining, 1.0))
                except asyncio.TimeoutError:
                    if not self.running and time.time() > until + 2 * self.SEGMENT_SECONDS:
                        return False
        return True

    # ── holds ────────────────────────────────────────────────────────────────

    def hold(self, key: object, from_wall: float) -> None:
        """Keep every segment ending after `from_wall` until released."""
        self._holds[key] = from_wall

    def release(self, key: object) -> None:
        self._holds.pop(key, None)
        self._prune()

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        await asyncio.to_thread(self._reset_dir)
        backoff = 2
        while not self._stop.is_set():
            try:
                produced = await self._run_once()
                backoff = 2 if produced else min(backoff * 2, 60)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Segment buffer (%s) crashed for camera %s", self.name, self.camera_id)
                backoff = min(backoff * 2, 60)
            if not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass

    async def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
        async with self._new_segment:
            self._new_segment.notify_all()

    def cleanup(self) -> None:
        """Delete the whole buffer directory (after stop, once nothing holds it)."""
        shutil.rmtree(self.directory, ignore_errors=True)

    # ── internals ────────────────────────────────────────────────────────────

    def _reset_dir(self) -> None:
        # Anything left from a previous run is unreferenced - start clean.
        shutil.rmtree(self.directory, ignore_errors=True)
        os.makedirs(self.directory, exist_ok=True)

    async def _run_once(self) -> bool:
        if self._audio_mode is None:
            self._audio_mode = await self._audio_mode_resolver() if self._audio_mode_resolver else "none"
        self._run_no += 1
        pattern = os.path.join(self.directory, f"r{self._run_no:04d}_%06d.ts")
        cmd = build_segment_cmd(self.input_url, pattern, self._audio_mode, self.SEGMENT_SECONDS)
        self._proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stderr_task = asyncio.create_task(self._proc.stderr.read())
        offset: float | None = None
        produced = False
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                arrived = time.time()
                parsed = self._parse_line(line)
                if parsed is None:
                    continue
                fname, s_start, s_end = parsed
                offset = arrived - s_end if offset is None else min(offset, arrived - s_end)
                seg = BufferSegment(os.path.join(self.directory, os.path.basename(fname)), s_start + offset, s_end + offset)
                if self._segments and seg.start < self._segments[-1].end:
                    # A new run (or clock-estimate jitter) must never overlap
                    # what's already buffered - that would duplicate footage.
                    seg = BufferSegment(seg.path, self._segments[-1].end, max(seg.end, self._segments[-1].end))
                self._segments.append(seg)
                if not produced:
                    produced = True
                    self.running = True
                async with self._new_segment:
                    self._new_segment.notify_all()
                self._prune()
        finally:
            self.running = False
            if self._proc.returncode is None:
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
            stderr = await stderr_task
            async with self._new_segment:
                self._new_segment.notify_all()

        if not self._stop.is_set():
            tail = stderr.decode(errors="ignore")[-300:].strip()
            logger.warning(
                "Segment buffer (%s) for camera %s exited (code %s): %s",
                self.name, self.camera_id, self._proc.returncode, tail or "no output",
            )
            if self.report_offline and not produced:
                await mark_offline(self.camera_id, f"Recording failed: {tail[-200:]}" if tail else None)
        return produced

    @staticmethod
    def _parse_line(line: bytes) -> tuple[str, float, float] | None:
        try:
            # filename may be quoted; times are the last two fields.
            fname, s_start, s_end = line.decode(errors="ignore").strip().rsplit(",", 2)
            return fname.strip('"'), float(s_start), float(s_end)
        except ValueError:
            return None

    def _prune(self) -> None:
        cutoff = time.time() - self.keep_seconds - self.PRUNE_MARGIN
        if self._holds:
            cutoff = min(cutoff, min(self._holds.values()))
        keep: list[BufferSegment] = []
        for seg in self._segments:
            if seg.end < cutoff:
                try:
                    os.remove(seg.path)
                except OSError:
                    pass
            else:
                keep.append(seg)
        self._segments = keep
