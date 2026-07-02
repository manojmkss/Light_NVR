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


class ContinuousRecorder:
    """Records fixed-length segments back-to-back. Stream-copies (no re-encode)
    straight from the RTSP main stream to keep CPU usage low. Each segment is a
    separate ffmpeg invocation rather than ffmpeg's own segment muxer so we get
    a clean per-file completion point to register the Recording row from.
    """

    def __init__(self, camera_id: int, rtsp_url: str, has_audio: bool):
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.has_audio = has_audio
        self._stop = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        self._paused_for_space = False

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
        cmd = _build_record_cmd(self.rtsp_url, output_path, self.has_audio, SEGMENT_SECONDS)

        # Published before ffmpeg even starts writing so the live-segment
        # endpoints have somewhere to point as soon as the file exists; the
        # next segment's call overwrites this a moment after this one closes.
        active_segment_registry.set(self.camera_id, output_path, started_at)

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

        ended_at = datetime.now(timezone.utc)

        if self._stop.is_set():
            active_segment_registry.clear(self.camera_id)
            return backoff

        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            await mark_online(self.camera_id)
            await register_recording(self.camera_id, output_path, "continuous", started_at, ended_at)
            return 2

        logger.warning(
            "Continuous recording failed for camera %s: %s",
            self.camera_id,
            stderr.decode(errors="ignore")[-300:],
        )
        await mark_offline(self.camera_id)
        await asyncio.sleep(backoff)
        return min(backoff * 2, 60)

    async def stop(self) -> None:
        self._stop.set()
        active_segment_registry.clear(self.camera_id)
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()


class MotionRecorder:
    """Starts an ffmpeg recording when motion begins and finalizes it a short
    post-roll delay after motion ends, so brief gaps in detection don't split
    one event into multiple clips. Pre-roll buffering is not implemented in
    Phase 1 - the clip starts at motion detection, not slightly before it.
    """

    POST_ROLL_SECONDS = 10
    MAX_CLIP_SECONDS = 600

    def __init__(self, camera_id: int, rtsp_url: str, has_audio: bool):
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.has_audio = has_audio
        self._proc: asyncio.subprocess.Process | None = None
        self._started_at: datetime | None = None
        self._output_path: str | None = None
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
                active_segment_registry.set(self.camera_id, self._output_path, self._started_at)
            except Exception:
                # Called directly from a scheduled future (StreamViewer's
                # motion callback) with nothing awaiting its result - an
                # uncaught exception here would only ever surface as an
                # asyncio "exception was never retrieved" log line, silently
                # dropping this one motion clip with no visible error.
                logger.exception("Failed to start motion recording for camera %s", self.camera_id)
                self._started_at = None
                self._output_path = None

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
            self._proc = None
            self._output_path = None
            self._stop_timer = None
            active_segment_registry.clear(self.camera_id)

        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()

        ended_at = datetime.now(timezone.utc)
        await register_recording(self.camera_id, output_path, "motion", started_at, ended_at)

    async def stop(self) -> None:
        if self._stop_timer:
            self._stop_timer.cancel()
        await self._finalize()
