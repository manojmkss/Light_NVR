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

logger = logging.getLogger(__name__)

# Regenerable, so it sits on the roomy /storage volume, not the DB volume.
TRANSCODE_DIR = "/storage/transcode-cache"
MAX_CACHE_FILES = 24
MAX_CACHE_BYTES = 3 * 1024**3  # 3 GB
TRANSCODE_TIMEOUT_SECONDS = 900  # ceiling for one clip's conversion

# One in-flight transcode per recording: a second viewer of the same clip waits
# on the first instead of launching a duplicate ffmpeg.
_locks: dict[int, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


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


async def get_or_transcode_h264(recording_id: int, src_path: str) -> str:
    """Return the path to an H.264 copy of `src_path`, transcoding + caching it
    on first request. Raises RuntimeError on failure. Serve the returned file
    with a Range-capable response (FileResponse) so playback stays seekable.
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
            tmp,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=TRANSCODE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            _remove_quietly(tmp)
            raise RuntimeError("Transcode timed out")

        if proc.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            detail = stderr.decode(errors="ignore")[-300:] if stderr else "ffmpeg failed"
            _remove_quietly(tmp)
            raise RuntimeError(detail)

        os.replace(tmp, out_path)
        _evict_if_needed()
        logger.info("Transcoded recording %s to H.264 for browser playback", recording_id)
        return out_path
