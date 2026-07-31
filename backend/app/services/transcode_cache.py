"""On-demand H.265 -> H.264 transcoding for browser playback.

H.265/HEVC recordings won't play in Firefox and many Chrome installs. Rather
than re-encode at record time (which would burn CPU 24/7 and lose the efficient
stream-copy recorder), a clip is transcoded to H.264 only when someone actually
opens it in a browser that can't play HEVC. The result is cached so re-watching
is instant, and the cache is bounded so it can't run away.

The cached files are fully regenerable, so they live outside the DB-backed
volume and are cleared on startup.
"""

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)

# Regenerable, so it sits on the roomy /storage volume, not the DB volume.
TRANSCODE_DIR = "/storage/transcode-cache"
MAX_CACHE_FILES = 24
MAX_CACHE_BYTES = 3 * 1024**3  # 3 GB
TRANSCODE_TIMEOUT_SECONDS = 900  # ceiling for one clip's conversion

# Cap on concurrent transcodes. Opening a multi-camera playback view fires one
# request per camera at once; without a cap that's N full-file H.265->H.264
# transcodes pegging every core. Extra requests queue on this instead. 2 leaves
# headroom for recording + motion on a typical box.
MAX_CONCURRENT_TRANSCODES = 2
_transcode_slots = asyncio.Semaphore(MAX_CONCURRENT_TRANSCODES)

# One in-flight transcode per recording: a second viewer of the same clip waits
# on the first instead of launching a duplicate ffmpeg.
_locks: dict[int, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _kill_when_client_leaves(proc: "asyncio.subprocess.Process", request: "Request") -> None:
    """Kill the transcode as soon as the viewer navigates away / closes the
    tab, instead of burning CPU on a clip nobody is waiting for. Polls the ASGI
    disconnect signal (works even when the server doesn't cancel the handler)."""
    try:
        while True:
            if await request.is_disconnected():
                if proc.returncode is None:
                    proc.kill()
                return
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass


def _cache_path(recording_id: int) -> str:
    return os.path.join(TRANSCODE_DIR, f"{recording_id}.mp4")


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


async def _lock_for(recording_id: int) -> asyncio.Lock:
    async with _locks_guard:
        lock = _locks.get(recording_id)
        if lock is None:
            lock = asyncio.Lock()
            _locks[recording_id] = lock
        return lock


def cleanup_on_startup() -> None:
    """Clear stale transcodes from a previous run - they're regenerable and a
    restart may have invalidated them (retention could have deleted the source)."""
    try:
        if os.path.isdir(TRANSCODE_DIR):
            for name in os.listdir(TRANSCODE_DIR):
                _remove_quietly(os.path.join(TRANSCODE_DIR, name))
    except OSError:
        pass


def _evict_if_needed() -> None:
    """Keep the cache under both a file-count and a byte cap, evicting the
    least-recently-used (by mtime) first."""
    try:
        entries = [
            os.path.join(TRANSCODE_DIR, n) for n in os.listdir(TRANSCODE_DIR)
        ]
        files = [f for f in entries if os.path.isfile(f)]
    except OSError:
        return
    files.sort(key=lambda f: os.path.getmtime(f))  # oldest first
    total = sum(os.path.getsize(f) for f in files)
    while files and (len(files) > MAX_CACHE_FILES or total > MAX_CACHE_BYTES):
        victim = files.pop(0)
        try:
            total -= os.path.getsize(victim)
        except OSError:
            pass
        _remove_quietly(victim)


async def get_or_transcode_h264(recording_id: int, src_path: str, request: "Request | None" = None) -> str:
    """Return the path to an H.264 copy of `src_path`, transcoding + caching it
    on first request. Raises RuntimeError on failure. Serve the returned file
    with a Range-capable response (FileResponse) so playback stays seekable.

    Pass `request` so the transcode is aborted the moment the viewer navigates
    away, and is bounded by a global concurrency cap so a multi-camera view
    can't spawn one ffmpeg per camera at once.
    """
    os.makedirs(TRANSCODE_DIR, exist_ok=True)
    out_path = _cache_path(recording_id)

    lock = await _lock_for(recording_id)
    async with lock:
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            try:
                os.utime(out_path, None)  # touch: mark as recently used for LRU
            except OSError:
                pass
            return out_path

        tmp = out_path + ".tmp"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", src_path,
            # veryfast + a moderate CRF keeps the conversion quick (this is a
            # transient playback copy, not an archive) while staying watchable.
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-pix_fmt", "yuv420p",           # max compatibility (some HEVC is 10-bit)
            "-c:a", "aac",
            "-movflags", "+faststart",       # playable/seekable as a normal MP4
            # The temp path ends in .tmp, so ffmpeg can't infer the container
            # from the extension - name the muxer explicitly.
            "-f", "mp4",
            tmp,
        ]

        # Queue behind the concurrency cap. If the viewer already left while we
        # were waiting for a slot, don't bother starting ffmpeg at all.
        async with _transcode_slots:
            if request is not None and await request.is_disconnected():
                _remove_quietly(tmp)
                raise RuntimeError("client disconnected before transcode started")

            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            watcher = (
                asyncio.create_task(_kill_when_client_leaves(proc, request)) if request is not None else None
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=TRANSCODE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                _remove_quietly(tmp)
                raise RuntimeError("Transcode timed out")
            except asyncio.CancelledError:
                # Handler was cancelled (client disconnect) - don't leave ffmpeg
                # orphaned burning CPU.
                proc.kill()
                await proc.wait()
                _remove_quietly(tmp)
                raise
            finally:
                if watcher is not None:
                    watcher.cancel()

        if proc.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            detail = stderr.decode(errors="ignore")[-300:] if stderr else "ffmpeg failed"
            _remove_quietly(tmp)
            raise RuntimeError(detail)

        os.replace(tmp, out_path)
        _evict_if_needed()
        logger.info("Transcoded recording %s to H.264 for browser playback", recording_id)
        return out_path
