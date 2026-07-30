import asyncio
import logging
import os
from datetime import datetime, timezone

from app.services.active_segment import active_segment_registry
from app.services.events import emit_event
from app.services.recording_registry import register_recording
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


def _build_record_cmd(rtsp_url: str, output_path: str, has_audio: bool, duration: int | None) -> list[str]:
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
    cmd += ["-c:a", "aac"] if has_audio else ["-an"]
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
        # In-flight segment finalizations (remux-to-seekable + DB register),
        # run in the background so the next segment starts with no gap.
        self._finalize_tasks: set[asyncio.Task] = set()

    async def _start_sub(self, sub_path: str | None) -> asyncio.subprocess.Process | None:
        """Launch the best-effort parallel sub-stream copy. A separate process
        (not one ffmpeg with two outputs) so a sub-stream hiccup can never fail
        or stall the main recording. Video-only: sub-streams often carry no
        audio, and the full-quality main remains the download source."""
        if not sub_path or not self.sub_url:
            return None
        try:
            cmd = _build_record_cmd(self.sub_url, sub_path, False, SEGMENT_SECONDS)
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
        cmd = _build_record_cmd(self.rtsp_url, output_path, self.has_audio, SEGMENT_SECONDS)

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
            await mark_online(self.camera_id)
            # Remux-to-seekable + register runs in the background: a synchronous
            # remux here would insert a multi-second gap into continuous
            # coverage every segment. The fragmented file is already valid, so
            # nothing is lost if the process dies before it finishes.
            task = asyncio.create_task(self._finalize_segment(output_path, sub_path, started_at, ended_at))
            self._finalize_tasks.add(task)
            task.add_done_callback(self._finalize_tasks.discard)
            return 2

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
            await register_recording(
                self.camera_id, path, "continuous", started_at, ended_at, playback_path=playback_path
            )
        except Exception:
            logger.exception("Failed to finalize/register segment %s for camera %s", path, self.camera_id)

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
    """Starts an ffmpeg recording when motion begins and finalizes it a short
    post-roll delay after motion ends, so brief gaps in detection don't split
    one event into multiple clips. Pre-roll buffering is not implemented in
    Phase 1 - the clip starts at motion detection, not slightly before it.
    """

    POST_ROLL_SECONDS = 10
    MAX_CLIP_SECONDS = 600

    def __init__(self, camera_id: int, rtsp_url: str, has_audio: bool, sub_url: str | None = None):
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.has_audio = has_audio
        self.sub_url = sub_url if sub_url and sub_url != rtsp_url else None
        self._proc: asyncio.subprocess.Process | None = None
        self._sub_proc: asyncio.subprocess.Process | None = None
        self._started_at: datetime | None = None
        self._output_path: str | None = None
        self._sub_path: str | None = None
        self._stop_timer: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def on_motion_start(self) -> None:
        async with self._lock:
            if self._stop_timer:
                self._stop_timer.cancel()
                self._stop_timer = None
            if self._proc is not None:
                return  # already recording this motion episode

            try:
                has_room, detail = await storage_manager.cache_has_room()
                if not has_room:
                    logger.warning("Skipping motion recording for camera %s: %s", self.camera_id, detail)
                    return

                self._started_at = datetime.now(timezone.utc)
                ts = self._started_at.strftime("%Y%m%d_%H%M%S")
                self._output_path = f"{_camera_dir(self.camera_id)}/{ts}_motion.mp4"
                cmd = _build_record_cmd(self.rtsp_url, self._output_path, self.has_audio, self.MAX_CLIP_SECONDS)
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                )
                # Best-effort parallel H.264 sub-stream copy for browser playback.
                self._sub_path = _sub_path_for(self._output_path) if self.sub_url else None
                self._sub_proc = None
                if self._sub_path:
                    try:
                        sub_cmd = _build_record_cmd(self.sub_url, self._sub_path, False, self.MAX_CLIP_SECONDS)
                        self._sub_proc = await asyncio.create_subprocess_exec(
                            *sub_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                        )
                    except Exception:
                        self._sub_path = None
                        self._sub_proc = None
                active_segment_registry.set(self.camera_id, self._output_path, self._started_at, self._sub_path)
            except Exception:
                # Called directly from a scheduled future (StreamViewer's
                # motion callback) with nothing awaiting its result - an
                # uncaught exception here would only ever surface as an
                # asyncio "exception was never retrieved" log line, silently
                # dropping this one motion clip with no visible error.
                logger.exception("Failed to start motion recording for camera %s", self.camera_id)
                self._started_at = None
                self._output_path = None
                self._sub_path = None
                self._sub_proc = None

    async def on_motion_stop(self) -> None:
        async with self._lock:
            if self._proc is None:
                return
            self._stop_timer = asyncio.create_task(self._finalize_after_delay())

    async def _finalize_after_delay(self) -> None:
        await asyncio.sleep(self.POST_ROLL_SECONDS)
        await self._finalize()

    async def _finalize(self) -> None:
        async with self._lock:
            if self._proc is None:
                return
            proc, output_path, started_at = self._proc, self._output_path, self._started_at
            sub_proc, sub_path = self._sub_proc, self._sub_path
            self._proc = None
            self._output_path = None
            self._sub_proc = None
            self._sub_path = None
            self._stop_timer = None
            active_segment_registry.clear(self.camera_id)

        for p in (proc, sub_proc):
            if p is not None and p.returncode is None:
                p.terminate()
                try:
                    await asyncio.wait_for(p.wait(), timeout=5)
                except asyncio.TimeoutError:
                    p.kill()

        ended_at = datetime.now(timezone.utc)
        # Same fragmented -> seekable remux as continuous segments, so a
        # motion clip can be scrubbed in the browser too. Motion clips aren't
        # recorded back-to-back, so doing it inline here adds no coverage gap.
        await _remux_to_seekable(output_path)
        playback_path = await _finalize_playback_copy(sub_path)
        await register_recording(
            self.camera_id, output_path, "motion", started_at, ended_at, playback_path=playback_path
        )

    async def stop(self) -> None:
        if self._stop_timer:
            self._stop_timer.cancel()
        await self._finalize()
