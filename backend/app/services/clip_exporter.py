import asyncio
import logging
import os
from datetime import datetime, timezone

from app.models.recording import Recording

logger = logging.getLogger(__name__)

# Hard ceiling on decode/copy work per request so one export can't tie up the
# box; the API validates the requested window against this too.
EXPORT_TIMEOUT_SECONDS = 300


def _to_unix(dt: datetime) -> float:
    # DB datetimes are naive UTC wall-clock; interpret them as UTC.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def rec_end_unix(rec: Recording) -> float:
    if rec.ended_at is not None:
        return _to_unix(rec.ended_at)
    return _to_unix(rec.started_at) + (rec.duration_seconds or 0.0)


def select_segments(
    recordings: list[Recording], start_ts: float, end_ts: float
) -> list[tuple[str, float, float, float]]:
    """From the recordings overlapping [start, end], work out how much of each
    file is needed. Returns (file_path, inpoint, outpoint, file_duration) tuples
    in chronological order, where inpoint/outpoint are offsets in seconds from
    that file's own start. Files that are missing on disk are skipped so a
    partially-migrated range still exports what it can.
    """
    segs: list[tuple[str, float, float, float]] = []
    for rec in sorted(recordings, key=lambda r: r.started_at):
        if not rec.file_path or not os.path.exists(rec.file_path):
            continue
        rs = _to_unix(rec.started_at)
        re = rec_end_unix(rec)
        dur = max(0.0, re - rs)
        seg_in = max(0.0, start_ts - rs)
        seg_out = min(dur, end_ts - rs)
        if seg_out - seg_in > 0.1:
            segs.append((rec.file_path, seg_in, seg_out, dur))
    return segs


def _write_concat_file(segs: list[tuple[str, float, float, float]], list_path: str) -> None:
    # ffconcat with per-file inpoint/outpoint lets a single stream-copy pass
    # trim the first/last segment and join the middle ones untouched.
    lines = ["ffconcat version 1.0"]
    for path, seg_in, seg_out, dur in segs:
        safe = path.replace("'", "'\\''")
        lines.append(f"file '{safe}'")
        if seg_in > 0.05:
            lines.append(f"inpoint {seg_in:.3f}")
        if seg_out < dur - 0.05:
            lines.append(f"outpoint {seg_out:.3f}")
    with open(list_path, "w") as f:
        f.write("\n".join(lines) + "\n")


async def export_clip(segs: list[tuple[str, float, float, float]], out_path: str) -> None:
    """Concatenate + trim the selected segments into out_path with a stream
    copy - no re-encode, so the export keeps the camera's original quality.
    Raises RuntimeError on failure.
    """
    list_path = f"{out_path}.concat.txt"
    _write_concat_file(segs, list_path)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-c", "copy",
        "-movflags", "+faststart",
        # Segments are independent recordings, so their timestamps restart at
        # each join; normalise them so players see one continuous timeline.
        "-avoid_negative_ts", "make_zero",
        out_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=EXPORT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("clip export timed out")
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass

    if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        detail = stderr.decode(errors="ignore")[-300:] if stderr else "ffmpeg failed"
        raise RuntimeError(detail)
