import asyncio
import os


async def generate_thumbnail(video_path: str, at_seconds: float = 0.5) -> str | None:
    """Writes the thumbnail next to the video (same dir, same basename, .jpg)
    so the storage mover can move/copy a recording's pair of files as one
    unit instead of tracking a separate thumbnail tree per tier.

    `at_seconds`: motion clips pass the moment motion was detected - with
    pre-roll, the first half-second is usually an empty scene.
    """
    thumb_path = os.path.splitext(video_path)[0] + ".jpg"

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{max(0.0, at_seconds):.2f}",
        "-i", video_path,
        "-vframes", "1",
        "-vf", "scale=320:-1",
        thumb_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await proc.wait()
    return thumb_path if os.path.exists(thumb_path) else None
