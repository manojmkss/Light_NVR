import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.core.deps import get_current_user, get_current_user_flexible, require_admin
from app.db.session import get_db
from app.models.camera import Camera
from app.models.recording import Recording
from app.models.user import User
from app.schemas.recording import BulkDeleteRequest, BulkDeleteResponse, RecordingOut
from app.services.clip_exporter import export_clip, rec_end_unix, select_segments
from app.services.retention import purge_recording

router = APIRouter(prefix="/api/recordings", tags=["recordings"])

# Cap a single export so it can't run away; the client validates too.
MAX_EXPORT_SECONDS = 2 * 60 * 60

# Cap a bulk zip download so a stray "select all" can't try to stream the whole
# archive at once.
MAX_ZIP_FILES = 200


def _safe_filename(suggested: str | None, fallback: str) -> str:
    """Sanitise a client-suggested download name for the Content-Disposition
    header (strip anything that isn't filename-safe, force a .mp4 suffix)."""
    base = suggested or fallback
    base = re.sub(r"\.mp4$", "", base, flags=re.IGNORECASE)
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_")
    if not base:
        base = fallback
    return f"{base[:120]}.mp4"


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


class _StreamBuffer:
    """A minimal write-only, unseekable sink for zipfile. Because it has no
    tell()/seek(), ZipFile falls back to writing data descriptors, which lets us
    stream a zip to the client on the fly without ever staging it on disk - so a
    multi-GB bulk download uses almost no server memory or scratch space."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def write(self, data: bytes) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:  # noqa: D401 - zipfile calls this
        pass

    def drain(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data


@router.get("", response_model=list[RecordingOut])
async def list_recordings(
    camera_id: int | None = None,
    trigger: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=100, le=500),
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    stmt = select(Recording).order_by(Recording.started_at.desc())
    if camera_id is not None:
        stmt = stmt.where(Recording.camera_id == camera_id)
    if trigger is not None:
        stmt = stmt.where(Recording.trigger == trigger)
    if start is not None:
        stmt = stmt.where(Recording.started_at >= start)
    if end is not None:
        stmt = stmt.where(Recording.started_at <= end)
    stmt = stmt.offset(offset).limit(limit)

    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/export")
async def export_clip_download(
    camera_id: int,
    start: float,  # unix seconds (inclusive)
    end: float,  # unix seconds (exclusive)
    name: str | None = None,  # optional client-suggested filename (already tz-formatted)
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user_flexible),
):
    """Cut a single downloadable clip covering [start, end] for one camera. The
    NVR stitches and trims whatever 5-minute segments fall in that window into
    one MP4 with a plain stream copy, so the download is the camera's original
    full-quality footage - not a re-encode.
    """
    if end <= start:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="end must be after start")
    if end - start > MAX_EXPORT_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Clip too long (max {MAX_EXPORT_SECONDS // 60} minutes)",
        )

    camera = await db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found")

    # Overlap query: any recording that started before the window ends and
    # whose own end reaches into the window. Compared against naive-UTC to match
    # how the timestamps are stored.
    end_naive = datetime.fromtimestamp(end, timezone.utc).replace(tzinfo=None)
    stmt = (
        select(Recording)
        .where(Recording.camera_id == camera_id, Recording.started_at <= end_naive)
        .order_by(Recording.started_at)
    )
    result = await db.execute(stmt)
    candidates = [r for r in result.scalars().all() if rec_end_unix(r) >= start]

    segs = select_segments(candidates, start, end)
    if not segs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No footage found in that time range")

    fd, out_path = tempfile.mkstemp(suffix=".mp4", prefix="clip_")
    os.close(fd)
    try:
        await export_clip(segs, out_path)
    except RuntimeError as exc:
        _remove_quietly(out_path)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Clip export failed: {exc}")

    fallback = f"{camera.name}_{int(start)}-{int(end)}"
    filename = _safe_filename(name, fallback)
    return FileResponse(
        out_path,
        media_type="video/mp4",
        filename=filename,
        background=BackgroundTask(_remove_quietly, out_path),
    )


@router.post("/bulk-delete", response_model=BulkDeleteResponse)
async def bulk_delete_recordings(
    payload: BulkDeleteRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Delete many recordings in one request (files + rows). Missing ids are
    reported back rather than failing the whole batch."""
    if not payload.ids:
        return BulkDeleteResponse(deleted=0, not_found=[])

    result = await db.execute(select(Recording).where(Recording.id.in_(payload.ids)))
    found = {r.id: r for r in result.scalars().all()}
    not_found = [rid for rid in payload.ids if rid not in found]

    events_removed = 0
    for rec in found.values():
        events_removed += await purge_recording(db, rec)
    await db.commit()

    return BulkDeleteResponse(deleted=len(found), not_found=not_found, events_removed=events_removed)


@router.get("/download-zip")
async def download_recordings_zip(
    ids: str = Query(..., description="comma-separated recording ids"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user_flexible),
):
    """Stream the selected recordings as a single .zip, built on the fly (no
    temp file). Stored (uncompressed) since video is already compressed."""
    try:
        id_list = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid ids") from exc

    if not id_list:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No recordings selected")
    if len(id_list) > MAX_ZIP_FILES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Too many recordings selected (max {MAX_ZIP_FILES})",
        )

    result = await db.execute(select(Recording).where(Recording.id.in_(id_list)))
    recordings = result.scalars().all()
    # Preserve the caller's order, and only include files that still exist on disk.
    by_id = {r.id: r for r in recordings}
    ordered = [by_id[rid] for rid in id_list if rid in by_id and os.path.exists(by_id[rid].file_path)]
    if not ordered:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No recording files found")

    cam_result = await db.execute(select(Camera))
    cam_names = {c.id: c.name for c in cam_result.scalars().all()}

    def _member_name(rec: Recording, used: set[str]) -> str:
        cam = re.sub(r"[^A-Za-z0-9._-]+", "_", cam_names.get(rec.camera_id, f"cam{rec.camera_id}")).strip("_")
        ts = rec.started_at.strftime("%Y%m%d_%H%M%S") if rec.started_at else str(rec.id)
        name = f"{cam}_{ts}.mp4"
        # Two segments in the same second would collide; disambiguate with the id.
        if name in used:
            name = f"{cam}_{ts}_{rec.id}.mp4"
        used.add(name)
        return name

    def iter_zip():
        buf = _StreamBuffer()
        used: set[str] = set()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
            for rec in ordered:
                arcname = _member_name(rec, used)
                zinfo = zipfile.ZipInfo(filename=arcname)
                zinfo.compress_type = zipfile.ZIP_STORED
                with zf.open(zinfo, mode="w") as dest, open(rec.file_path, "rb") as src:
                    while True:
                        chunk = src.read(262144)
                        if not chunk:
                            break
                        dest.write(chunk)
                        data = buf.drain()
                        if data:
                            yield data
                data = buf.drain()
                if data:
                    yield data
        tail = buf.drain()
        if tail:
            yield tail

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"recordings_{stamp}.zip"
    return StreamingResponse(
        iter_zip(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{recording_id}", response_model=RecordingOut)
async def get_recording(recording_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    recording = await db.get(Recording, recording_id)
    if recording is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording not found")
    return recording


@router.get("/{recording_id}/video")
async def get_recording_video(
    recording_id: int,
    download: bool = False,  # when true, force a Save-As download with a friendly name
    transcode: str | None = None,  # "h264" -> convert HEVC for browsers that can't play it
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user_flexible),
):
    recording = await db.get(Recording, recording_id)
    if recording is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording not found")
    if not os.path.exists(recording.file_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording file missing")

    # On-demand H.265 -> H.264 for browsers (Firefox, many Chrome) that can't
    # play HEVC. Transcoded once, cached, and served with Range support so it
    # stays seekable. Only requested by the frontend when actually needed.
    if transcode == "h264":
        from app.services.transcode_cache import get_or_transcode_h264

        try:
            out_path = await get_or_transcode_h264(recording_id, recording.file_path)
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Could not transcode for playback: {exc}"
            ) from exc
        if download:
            camera = await db.get(Camera, recording.camera_id)
            ts = recording.started_at.strftime("%Y%m%d_%H%M%S") if recording.started_at else str(recording.id)
            return FileResponse(
                out_path, media_type="video/mp4", filename=_safe_filename(None, f"{camera.name if camera else 'camera'}_{ts}_h264")
            )
        return FileResponse(out_path, media_type="video/mp4")

    if download:
        camera = await db.get(Camera, recording.camera_id)
        ts = recording.started_at.strftime("%Y%m%d_%H%M%S") if recording.started_at else str(recording.id)
        fallback = f"{camera.name if camera else 'camera'}_{ts}"
        return FileResponse(
            recording.file_path,
            media_type="video/mp4",
            filename=_safe_filename(None, fallback),
        )
    return FileResponse(recording.file_path, media_type="video/mp4")


@router.get("/{recording_id}/thumbnail.jpg")
async def get_recording_thumbnail(
    recording_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user_flexible),
):
    recording = await db.get(Recording, recording_id)
    if recording is None or not recording.thumbnail_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thumbnail not found")
    return FileResponse(recording.thumbnail_path, media_type="image/jpeg")


@router.delete("/{recording_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_recording(recording_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    recording = await db.get(Recording, recording_id)
    if recording is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording not found")
    await purge_recording(db, recording)
    await db.commit()
