import os
import tempfile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.api.routes.auth import setup_lock
from app.core.deps import require_admin, require_admin_or_setup_incomplete
from app.core.uploads import read_limited
from app.db.session import AsyncSessionLocal
from app.models.backup_settings import BackupSettings
from app.models.user import User
from app.schemas.backup import BackupInfoOut, BackupSettingsOut, BackupSettingsUpdate, OptimizeResult
from app.services.config_backup import create_backup, find_backup, list_backups, restore_from_path
from app.services.db_maintenance import optimize_database

router = APIRouter(prefix="/api/backup", tags=["backup"])

MAX_RESTORE_UPLOAD_BYTES = 500 * 1024 * 1024


@router.get("/settings", response_model=BackupSettingsOut)
async def get_backup_settings(_: User = Depends(require_admin)):
    async with AsyncSessionLocal() as db:
        return await db.get(BackupSettings, 1)


@router.put("/settings", response_model=BackupSettingsOut)
async def update_backup_settings(payload: BackupSettingsUpdate, _: User = Depends(require_admin)):
    async with AsyncSessionLocal() as db:
        record = await db.get(BackupSettings, 1)
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(record, field, value)
        await db.commit()
        await db.refresh(record)
        return record


@router.get("/list", response_model=list[BackupInfoOut])
async def get_backup_list(_: User = Depends(require_admin)):
    backups = await list_backups()
    return [
        BackupInfoOut(filename=b.filename, location=b.location, size_bytes=b.size_bytes, created_at=b.created_at)
        for b in backups
    ]


@router.post("/create", response_model=BackupInfoOut, status_code=status.HTTP_201_CREATED)
async def trigger_backup(_: User = Depends(require_admin)):
    try:
        info = await create_backup()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Backup failed: {exc}") from exc
    return BackupInfoOut(filename=info.filename, location=info.location, size_bytes=info.size_bytes, created_at=info.created_at)


@router.get("/download/{filename}")
async def download_backup(filename: str, _: User = Depends(require_admin)):
    backup = await find_backup(filename)
    if backup is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backup not found")
    return FileResponse(backup.path, media_type="application/octet-stream", filename=backup.filename)


@router.delete("/{filename}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_backup(filename: str, _: User = Depends(require_admin)):
    backup = await find_backup(filename)
    if backup is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backup not found")
    try:
        os.remove(backup.path)
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@router.post("/restore/{filename}", status_code=status.HTTP_202_ACCEPTED)
async def restore_existing_backup(filename: str, _: User = Depends(require_admin)):
    backup = await find_backup(filename)
    if backup is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backup not found")
    try:
        await restore_from_path(backup.path)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"detail": "Restoring - the server will restart in a few seconds. You'll need to log in again."}


@router.post("/restore-upload", status_code=status.HTTP_202_ACCEPTED)
async def restore_uploaded_backup(
    file: UploadFile = File(...),
    anonymous: bool = Depends(require_admin_or_setup_incomplete),
):
    contents = await read_limited(file, MAX_RESTORE_UPLOAD_BYTES)

    if anonymous:
        # The dependency's "no admin exists" check is a fast-path hint, not
        # the final word - re-verify under the same lock /auth/setup uses so
        # two concurrent anonymous requests can't both pass the check before
        # either commits.
        async with setup_lock:
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(User).where(User.role == "admin"))
                if result.scalar_one_or_none() is not None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT, detail="Setup has already been completed"
                    )
            await _write_and_restore(contents)
    else:
        await _write_and_restore(contents)

    return {"detail": "Restoring - the server will restart in a few seconds."}


async def _write_and_restore(contents: bytes) -> None:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        tmp_path = tmp.name
        tmp.write(contents)

    try:
        await restore_from_path(tmp_path)
    except ValueError as exc:
        os.remove(tmp_path)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/optimize", response_model=OptimizeResult)
async def trigger_optimize(_: User = Depends(require_admin)):
    return await optimize_database()
