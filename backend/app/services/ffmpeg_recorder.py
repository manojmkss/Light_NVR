import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from app.services.active_segment import active_segment_registry
from app.services.clip_exporter import export_clip
from app.services.events import emit_event
from app.services.recording_registry import register_recording
from app.services.segment_buffer import SegmentBuffer
from app.services.status_tracker import mark_offline, mark_online
from app.services.storage_manager import storage_manager

logger = logging.getLogger(__name__)

SEGMENT_SECONDS = 300  # continuous recording is cut into 5-minute files


def _camera_dir(camera_id: int) -> str:
    # Recording always targets the local cache, never primary/backup directly -
    # the storage mover handles getting finished segments to their real home.
    # This keeps a flaky NAS from ever being able to stall or corrupt a write.
    path = f"{storage_manager.cache_dir()}/{camera_id}"
    os.makedirs(path, exist_ok=True)
    return path


def _build_record_cmd(rtsp_url: str, output_path: str, audio_mode: str, duration: int | None) -> list[str]:
    """audio_mode: "copy" (stream-copy the camera's own AAC - zero CPU), "aac"
    (re-encode to AAC - needed for codecs MP4 can't hold as-is, e.g. the G.711/
    G.726 most cameras actually send over RTSP), or "none" (no audio track)."""
    cmd = [
        "ffmpeg", "-y",
        "-rtsp_transport", "tcp",
        # Abort if the RTSP socket goes silent for 10s (value is microseconds).
        # A healthy stream delivers packets continuously, so 10s of silence
        # means the camera dropped - this fails the segment fast so the loop
        # can reconnect and the camera is marked offline in seconds, instead of
        # ffmpeg blocking for the whole segment window before the watchdog
        # kills it.
        "-timeout", "10000000",
        "-i", rtsp_url,
        "-c:v", "copy",
    ]
    if audio_mode == "copy":
        cmd += ["-c:a", "copy"]
    elif audio_mode == "aac":
        cmd += ["-c:a", "aac"]
    else:
        cmd += ["-an"]
    if duration:
        cmd += ["-t", str(duration)]
    # Fragmented MP4 instead of faststart: each fragment (moof+mdat) is a
    # self-contained, playable unit written incrementally, so the segment
    # currently being recorded can be served and watched *while it's still
    # being written* - faststart's single moov atom is only finalized when the
    # whole file closes, which is what made the last few minutes unplayable
    # until the segment rotated. Bonus: if the process is ever killed
    # mid-segment, every fragment flushed so far stays valid instead of
    # leaving a corrupt file.
    #
    # -flush_packets 1 is not optional here: without it ffmpeg buffers written
    # bytes internally and a concurrent reader sees nothing on disk for many
    # seconds (measured ~8s+ of real footage sitting in ffmpeg's own buffer
    # before a single fragment was flushed) - the in-progress file would look
    # empty/stale to the live-segment endpoint almost the whole time.
    cmd += ["-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-flush_packets", "1", output_path]
    return cmd


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


async def _remux_to_seekable(path: str) -> None:
    """Rewrite a finished segment from fragmented MP4 to a plain faststart MP4.

    The recorder writes fragmented MP4 (`+empty_moov`) so the segment being
    written is playable *while* it's still being written. The cost is that that
    layout carries no seek index - the moov has no sample tables and there's no
    sidx - so a browser can play the file start-to-end but cannot seek within
    it (scrubbing back in the live/kiosk tiles just failed or snapped to the
    start). Once a segment is finalized it's no longer being appended to, so we
    stream-copy it (no re-encode, ~a second or two) into a normal MP4 with a
    single front-loaded moov + full sample tables, which every browser seeks.

    Best-effort and in-place via an atomic replace: on any failure the original
    fragmented file - still valid, just not seekable - is left untouched.
    """
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return
    tmp = f"{path}.seek.tmp"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", path,
        "-c", "copy",
        "-movflags", "+faststart",
        "-f", "mp4",
        tmp,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
    except Exception:
        logger.warning("Could not launch remux for %s (kept fragmented)", path, exc_info=True)
        return
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        _remove_quietly(tmp)
        logger.warning("Remux to seekable MP4 timed out for %s (kept fragmented)", path)
        return

    if proc.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        try:
            os.replace(tmp, path)
        except OSError:
            _remove_quietly(tmp)
            logger.warning("Could not replace %s with its seekable remux (kept fragmented)", path)
    else:
        _remove_quietly(tmp)
        logger.warning(
            "Remux to seekable MP4 failed for %s (kept fragmented): %s",
            path, stderr.decode(errors="ignore")[-200:].strip(),
        )


def _sub_path_for(main_path: str) -> str:
    """Sibling path for the H.264 playback copy: `{name}.sub.mp4`. Kept as a
    sibling (same dir, derived name) so the storage mover and retention carry
    it along with the main file without extra bookkeeping."""
    root, ext = os.path.splitext(main_path)
    return f"{root}.sub{ext}"


async def _probe_video_codec(path: str) -> str | None:
    """ffprobe the first video stream's codec name (e.g. 'h264', 'hevc'), or
    None if it can't be determined."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except Exception:
        return None
    return out.decode(errors="ignore").strip() or None


async def probe_duration(path: str) -> float | None:
    """Seconds of media in a file (works on a still-growing fragmented MP4:
    it reports what has been written so far), or None."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        value = float(out.decode(errors="ignore").strip())
    except Exception:
        return None
    return value if value > 0 else None


def footage_start(wall_start: datetime, wall_end: datetime, duration: float | None) -> datetime:
    """When a segment's first frame was really captured.

    `wall_start` is taken before ffmpeg even connects to the camera, so it runs
    early by the RTSP connect + first-keyframe wait (1-3s, sometimes much more)
    and every seek into the segment landed that much late. ffmpeg stops right
    after the last packet, so anchoring on the end with the file's real
    duration is accurate; anything implausible keeps the wall-clock start.
    """
    if duration is None:
        return wall_start
    anchored = wall_end - timedelta(seconds=duration)
    if wall_start - timedelta(seconds=5) <= anchored <= wall_end:
        return max(anchored, wall_start)
    return wall_start


async def _probe_audio_codec(rtsp_url: str) -> str | None:
    """ffprobe the RTSP stream's first audio codec name (e.g. 'aac',
    'pcm_mulaw'), or None if there's no audio track or it can't be determined.
    Bounded timeout: this runs against a live camera, not a file."""
    cmd = [
        "ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "a:0",
        "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", rtsp_url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except Exception:
        return None
    return out.decode(errors="ignore").strip() or None


async def _resolve_audio_mode(camera_id: int, rtsp_url: str, has_audio: bool) -> str:
    """"copy" only when the camera's own audio is already AAC - the one codec
    that's both valid inside MP4 and something cameras actually send, so a
    stream-copy costs zero CPU. Anything else (G.711/G.726, which MP4 can't
    hold directly, or a probe that failed) falls back to re-encoding to AAC -
    the previous universal behavior - rather than risk `-c:a copy` producing a
    file ffmpeg's MP4 muxer rejects outright, which would silently kill video
    recording too since it's the same output file."""
    if not has_audio:
        return "none"
    codec = await _probe_audio_codec(rtsp_url)
    mode = "copy" if codec == "aac" else "aac"
    logger.info("Camera %s audio: %s -> recording as %s", camera_id, codec or "unknown", mode)
    return mode


async def _finalize_playback_copy(sub_path: str | None) -> str | None:
    """Finalize the parallel sub-stream recording into a browser-ready playback
    copy. Kept only if it's genuinely H.264 - the whole point is giving HEVC-shy
    browsers something to play without a transcode, so an H.265 sub buys nothing
    over the main file. Made seekable like any finalized segment. Returns the
    path to store as playback_path, or None to fall back to on-demand transcode.
    """
    if not sub_path or not os.path.exists(sub_path) or os.path.getsize(sub_path) == 0:
        return None
    if await _probe_video_codec(sub_path) != "h264":
        _remove_quietly(sub_path)
        return None
    await _remux_to_seekable(sub_path)
    return sub_path


class ContinuousRecorder:
    """Records fixed-length segments back-to-back. Stream-copies (no re-encode)
    straight from the RTSP main stream to keep CPU usage low. Each segment is a
    separate ffmpeg invocation rather than ffmpeg's own segment muxer so we get
    a clean per-file completion point to register the Recording row from.

    When the camera exposes a distinct H.264 sub-stream, a second stream-copy of
    it is recorded in parallel as a small, instantly-browser-playable companion
    (see _finalize_playback_copy) - so H.265 recordings play and scrub with no
    on-demand transcode, while the H.265 main stays the full-quality original.
    """

    def __init__(self, camera_id: int, rtsp_url: str, has_audio: bool, sub_url: str | None = None):
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.has_audio = has_audio
        # Only worth copying if it's a *different* stream from the main; equal
        # (or absent) means there's no separate sub-stream to lean on.
        self.sub_url = sub_url if sub_url and sub_url != rtsp_url else None
        self._stop = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        self._sub_proc: asyncio.subprocess.Process | None = None
        self._paused_for_space = False
        # Resolved once (probing a live RTSP stream isn't free) and reused for
        # every segment this instance records; a fresh instance after a
        # restart re-resolves it, picking up any camera-side audio change.
        self._audio_mode: str | None = None
        # In-flight segment finalizations (remux-to-seekable + DB register),
        # run in the background so the next segment starts with no gap.
        self._finalize_tasks: set[asyncio.Task] = set()
        # When the last segment finished with footage (monotonic), for the
        # supervisor's "recording stalled" check.
        self.started_at = time.monotonic()
        self.last_segment_at = 0.0

    async def _resolve_audio_mode(self) -> str:
        if self._audio_mode is None:
            self._audio_mode = await _resolve_audio_mode(self.camera_id, self.rtsp_url, self.has_audio)
        return self._audio_mode

    async def _start_sub(self, sub_path: str | None) -> asyncio.subprocess.Process | None:
        """Launch the best-effort parallel sub-stream copy. A separate process
        (not one ffmpeg with two outputs) so a sub-stream hiccup can never fail
        or stall the main recording. Video-only: sub-streams often carry no
        audio, and the full-quality main remains the download source."""
        if not sub_path or not self.sub_url:
            return None
        try:
            cmd = _build_record_cmd(self.sub_url, sub_path, "none", SEGMENT_SECONDS)
            return await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
        except Exception:
            logger.warning("Could not start sub-stream playback copy for camera %s", self.camera_id, exc_info=True)
            return None

    async def _reap_sub(self) -> None:
        proc, self._sub_proc = self._sub_proc, None
        if proc is None:
            return
        try:
            await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        except Exception:
            pass

    async def run(self) -> None:
        backoff = 2
        while not self._stop.is_set():
            try:
                backoff = await self._run_one_segment(backoff)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Anything unexpected here (DB locked, disk error, a bug) must
                # not escape this loop - this coroutine is fire-and-forget
                # (asyncio.create_task with no watchdog above it), so an
                # uncaught exception would silently kill recording for this
                # camera forever, with nothing but an asyncio "exception was
                # never retrieved" warning in the logs.
                logger.exception("Unexpected error in continuous recorder for camera %s", self.camera_id)
                try:
                    await mark_offline(self.camera_id)
                except Exception:
                    logger.exception("mark_offline also failed for camera %s", self.camera_id)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _run_one_segment(self, backoff: int) -> int:
        """Records one segment and returns the backoff to use next time -
        reset to the base value on success, doubled (capped) on failure."""
        has_room, detail = await storage_manager.cache_has_room()
        if not has_room:
            if not self._paused_for_space:
                self._paused_for_space = True
                await emit_event(self.camera_id, "low_storage", f"Recording paused: {detail}")
            await asyncio.sleep(30)
            return backoff
        if self._paused_for_space:
            self._paused_for_space = False
            await emit_event(self.camera_id, "system", "Recording resumed - cache space available again")

        started_at = datetime.now(timezone.utc)
        ts = started_at.strftime("%Y%m%d_%H%M%S")
        output_path = f"{_camera_dir(self.camera_id)}/{ts}_continuous.mp4"
        sub_path = _sub_path_for(output_path) if self.sub_url else None
        audio_mode = await self._resolve_audio_mode()
        cmd = _build_record_cmd(self.rtsp_url, output_path, audio_mode, SEGMENT_SECONDS)

        # Published before ffmpeg even starts writing so the live-segment
        # endpoints have somewhere to point as soon as the file exists; the
        # next segment's call overwrites this a moment after this one closes.
        active_segment_registry.set(self.camera_id, output_path, started_at, sub_path)

        # Best-effort parallel H.264 sub-stream copy for browser playback.
        self._sub_proc = await self._start_sub(sub_path)

        stderr = b""
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(self._proc.communicate(), timeout=SEGMENT_SECONDS + 30)
        except asyncio.TimeoutError:
            if self._proc:
                self._proc.kill()
                await self._proc.wait()
            stderr = b"timed out waiting for ffmpeg"

        # The sub copy has the same -t, so it's finishing around now; reap it.
        await self._reap_sub()

        ended_at = datetime.now(timezone.utc)

        if self._stop.is_set():
            active_segment_registry.clear(self.camera_id)
            return backoff

        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            self.last_segment_at = time.monotonic()
            # Still playable (from the registry) while it's being finalized.
            active_segment_registry.close(self.camera_id, output_path, ended_at)
            await mark_online(self.camera_id)
            # Remux-to-seekable + register runs in the background: a synchronous
            # remux here would insert a multi-second gap into continuous
            # coverage every segment. The fragmented file is already valid, so
            # nothing is lost if the process dies before it finishes.
            task = asyncio.create_task(self._finalize_segment(output_path, sub_path, started_at, ended_at))
            self._finalize_tasks.add(task)
            task.add_done_callback(self._finalize_tasks.discard)
            return 2

        active_segment_registry.clear(self.camera_id)
        stderr_tail = stderr.decode(errors="ignore")[-300:].strip()
        logger.warning("Continuous recording failed for camera %s: %s", self.camera_id, stderr_tail)
        # The ffmpeg error tail is the most specific "why" we ever get
        # (401 Unauthorized, Connection refused, timeout...). mark_offline
        # scrubs any embedded credentials before storing it.
        await mark_offline(self.camera_id, f"Recording failed: {stderr_tail[-200:]}" if stderr_tail else None)
        await asyncio.sleep(backoff)
        return min(backoff * 2, 60)

    async def _finalize_segment(self, path: str, sub_path: str | None, started_at: datetime, ended_at: datetime) -> None:
        """Make the just-closed segment seekable, finalize its H.264 playback
        copy, then register it. Runs off the recording loop so it never delays
        the next segment."""
        try:
            await _remux_to_seekable(path)
            playback_path = await _finalize_playback_copy(sub_path)
            started_at = footage_start(started_at, ended_at, await probe_duration(path))
            await register_recording(
                self.camera_id, path, "continuous", started_at, ended_at, playback_path=playback_path
            )
        except Exception:
            logger.exception("Failed to finalize/register segment %s for camera %s", path, self.camera_id)
        finally:
            active_segment_registry.finalized(self.camera_id, path)

    async def stop(self) -> None:
        self._stop.set()
        active_segment_registry.clear(self.camera_id)
        for proc in (self._proc, self._sub_proc):
            if proc and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
        # Let any in-flight finalizations finish so a stop/restart doesn't drop
        # the segment that was just recorded (its DB row would otherwise never
        # be written).
        if self._finalize_tasks:
            await asyncio.gather(*self._finalize_tasks, return_exceptions=True)


class MotionRecorder:
    """Motion clips cut from an always-running rolling buffer (SegmentBuffer).

    The buffer keeps the last few seconds of the main stream on disk, so when
    motion starts the clip simply *begins `pre_roll` seconds in the past* -
    nothing is launched and nothing is lost to RTSP connect + keyframe wait.
    When motion stops, the clip stays open for `post_roll` seconds (a re-trigger
    in that window extends the same clip), then the covering segments are
    stitched into one MP4 with a stream copy. Clip start/end times come from
    the segments themselves, so they match the footage exactly.

    Very long events are cut into MAX_CLIP_SECONDS pieces instead of stopping
    recording. An H.264 sub-stream buffer is kept alongside an H.265 main so
    every clip has a browser-playable copy (see _finalize_playback_copy).
    """

    MAX_CLIP_SECONDS = 600
    COVER_TIMEOUT = 20.0  # how long to wait for the segment containing the clip end to close

    def __init__(
        self,
        camera_id: int,
        rtsp_url: str,
        has_audio: bool,
        sub_url: str | None = None,
        pre_roll_seconds: float = 5,
        post_roll_seconds: float = 10,
        on_clip_saved=None,
    ):
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.has_audio = has_audio
        self.sub_url = sub_url if sub_url and sub_url != rtsp_url else None
        self.pre_roll = max(0.0, float(pre_roll_seconds))
        self.post_roll = max(0.0, float(post_roll_seconds))
        # Awaitable callback(Recording, clip_start, clip_end) - lets the event
        # layer link its summary to the saved clip.
        self.on_clip_saved = on_clip_saved

        async def resolve_audio() -> str:
            return await _resolve_audio_mode(camera_id, rtsp_url, has_audio)

        self.buffer = SegmentBuffer(camera_id, rtsp_url, resolve_audio, "main", keep_seconds=self.pre_roll)
        self.sub_buffer = (
            SegmentBuffer(camera_id, self.sub_url, None, "sub", keep_seconds=self.pre_roll, report_offline=False)
            if self.sub_url else None
        )
        self._tasks: list[asyncio.Task] = []
        self._clip_start: float | None = None  # wall time; None = no clip open
        self._trigger_at: float | None = None
        self._stop_timer: asyncio.Task | None = None
        self._rollover_task: asyncio.Task | None = None
        self._finalize_tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

    @property
    def _buffers(self) -> list[SegmentBuffer]:
        return [b for b in (self.buffer, self.sub_buffer) if b is not None]

    async def start(self) -> None:
        for buf in self._buffers:
            self._tasks.append(asyncio.create_task(buf.run()))

    async def on_motion_start(self) -> None:
        async with self._lock:
            if self._stop_timer:
                self._stop_timer.cancel()
                self._stop_timer = None
            if self._clip_start is not None:
                return  # re-trigger inside the post-roll: same clip continues
            now = time.time()
            self._trigger_at = now
            self._clip_start = now - self.pre_roll
            for buf in self._buffers:
                buf.hold(self, self._clip_start)
            self._rollover_task = asyncio.create_task(self._rollover_loop())

    async def on_motion_stop(self) -> None:
        async with self._lock:
            if self._clip_start is None:
                return
            if self._stop_timer:
                self._stop_timer.cancel()
            self._stop_timer = asyncio.create_task(self._finalize_after_delay())

    async def _finalize_after_delay(self) -> None:
        await asyncio.sleep(self.post_roll)
        await self._close_clip(time.time())

    async def _close_clip(self, end: float, cover_timeout: float | None = None) -> None:
        async with self._lock:
            if self._clip_start is None:
                return
            start, trigger_at = self._clip_start, self._trigger_at
            self._clip_start = None
            self._trigger_at = None
            self._stop_timer = None
            if self._rollover_task:
                self._rollover_task.cancel()
                self._rollover_task = None
        try:
            await self._assemble(start, end, trigger_at, wait=True, cover_timeout=cover_timeout)
        finally:
            for buf in self._buffers:
                buf.release(self)

    async def _rollover_loop(self) -> None:
        """Cut very long events into MAX_CLIP_SECONDS pieces, at a segment
        boundary so no footage is duplicated or dropped between pieces."""
        try:
            while True:
                await asyncio.sleep(5)
                boundary = self.buffer.latest_end()
                async with self._lock:
                    start = self._clip_start
                    if start is None or boundary is None or boundary - start < self.MAX_CLIP_SECONDS:
                        continue
                    trigger_at = self._trigger_at
                    self._clip_start = boundary
                    self._trigger_at = None
                    # The finished piece gets its own hold so its segments
                    # survive until stitched, even if the event closes (and
                    # releases the clip-level hold) meanwhile.
                    piece = object()
                    for buf in self._buffers:
                        buf.hold(piece, start)
                        buf.hold(self, boundary)
                task = asyncio.create_task(self._assemble_piece(piece, start, boundary, trigger_at))
                self._finalize_tasks.add(task)
                task.add_done_callback(self._finalize_tasks.discard)
        except asyncio.CancelledError:
            pass

    async def _assemble_piece(self, piece: object, start: float, end: float, trigger_at: float | None) -> None:
        try:
            await self._assemble(start, end, trigger_at, wait=False)
        finally:
            for buf in self._buffers:
                buf.release(piece)

    async def _assemble(
        self, start: float, end: float, trigger_at: float | None, wait: bool, cover_timeout: float | None = None
    ) -> None:
        try:
            if wait:
                await self.buffer.wait_until_covered(end, cover_timeout or self.COVER_TIMEOUT)
            segs = self.buffer.segments_overlapping(start, end)
            if not segs:
                logger.warning(
                    "Motion clip for camera %s has no buffered footage (is the main stream reachable?)", self.camera_id
                )
                return
            has_room, detail = await storage_manager.cache_has_room()
            if not has_room:
                logger.warning("Skipping motion clip for camera %s: %s", self.camera_id, detail)
                return

            clip_start, clip_end = segs[0].start, segs[-1].end
            ts = datetime.fromtimestamp(clip_start, timezone.utc).strftime("%Y%m%d_%H%M%S")
            output_path = f"{_camera_dir(self.camera_id)}/{ts}_motion.mp4"
            await export_clip([(s.path, 0.0, s.duration, s.duration) for s in segs], output_path)

            playback_path = None
            if self.sub_buffer is not None:
                if wait:
                    await self.sub_buffer.wait_until_covered(clip_end, 5)
                sub_segs = self.sub_buffer.segments_overlapping(clip_start, clip_end)
                if sub_segs:
                    sub_path = _sub_path_for(output_path)
                    try:
                        await export_clip([(s.path, 0.0, s.duration, s.duration) for s in sub_segs], sub_path)
                        playback_path = await _finalize_playback_copy(sub_path)
                    except RuntimeError as exc:
                        logger.warning("Playback copy for motion clip %s failed: %s", output_path, exc)
                        _remove_quietly(sub_path)

            thumb_at = max(0.5, (trigger_at - clip_start) if trigger_at else 0.5)
            recording = await register_recording(
                self.camera_id,
                output_path,
                "motion",
                datetime.fromtimestamp(clip_start, timezone.utc),
                datetime.fromtimestamp(clip_end, timezone.utc),
                playback_path=playback_path,
                thumbnail_at=min(thumb_at, max(0.5, clip_end - clip_start - 0.5)),
            )
            if recording is not None and self.on_clip_saved:
                await self.on_clip_saved(recording, clip_start, clip_end)
        except Exception:
            # Runs from a timer task nothing awaits - log, never raise, or the
            # failure would only show as "exception was never retrieved".
            logger.exception("Failed to save motion clip for camera %s", self.camera_id)

    async def stop(self) -> None:
        if self._stop_timer:
            self._stop_timer.cancel()
        # An event still open at shutdown is saved with what was buffered
        # (short wait: a container stop gives us ~10s in total).
        await self._close_clip(time.time(), cover_timeout=3)
        if self._finalize_tasks:
            await asyncio.gather(*self._finalize_tasks, return_exceptions=True)
        for buf in self._buffers:
            await buf.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for buf in self._buffers:
            await asyncio.to_thread(buf.cleanup)
