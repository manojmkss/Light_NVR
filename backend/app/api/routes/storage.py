import asyncio
import shutil

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_admin
from app.db.session import get_db
from app.models.storage_config import StorageConfig
from app.models.user import User
from app.schemas.storage import (
    StorageConfigOut,
    StorageConfigUpdate,
    StorageHealthOut,
    StorageTestRequest,
    StorageTestResult,
    TierHealth,
)
from app.services import mount_manager
from app.services.storage_manager import storage_manager, probe_writable
from app.services.storage_mover import pending_migration_count, recordings_size_by_tier

router = APIRouter(prefix="/api/storage", tags=["storage"])


@router.get("/config", response_model=StorageConfigOut)
async def get_storage_config(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return await db.get(StorageConfig, 1)


@router.put("/config", response_model=StorageConfigOut)
async def update_storage_config(
    payload: StorageConfigUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    config = await db.get(StorageConfig, 1)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(config, field, value)
    await db.commit()
    await db.refresh(config)

    await storage_manager.reload_config()
    return config


@router.post("/test", response_model=StorageTestResult)
async def test_storage(payload: StorageTestRequest, _: User = Depends(require_admin)):
    if payload.type == "local":
        target_path = storage_manager.primary_dir() if payload.target == "primary" else storage_manager.backup_dir()
        if not target_path:
            return StorageTestResult(success=False, message="No local path configured for this target")
        ok, detail = await probe_writable(target_path)
        return StorageTestResult(success=ok, message=detail)

    if not payload.remote_spec:
        return StorageTestResult(success=False, message="Remote share path is required")

    # Mount to a scratch point rather than the live mountpoint, so testing
    # credentials never disrupts an already-working active mount.
    test_mount_point = f"/mnt/test-{payload.target}"
    success, message = await mount_manager.mount_share(
        test_mount_point, payload.type, payload.remote_spec, payload.username, payload.password
    )
    if not success:
        return StorageTestResult(success=False, message=message)

    write_ok, write_detail = await probe_writable(test_mount_point)
    await mount_manager.unmount(test_mount_point)

    if not write_ok:
        return StorageTestResult(success=False, message=f"Mounted but not writable: {write_detail}")
    return StorageTestResult(success=True, message="Connected and writable")


@router.get("/health", response_model=StorageHealthOut)
async def get_storage_health(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    config = await db.get(StorageConfig, 1)
    tier_sizes = await recordings_size_by_tier()

    cache_usage = await asyncio.to_thread(shutil.disk_usage, storage_manager.cache_dir())
    cache = TierHealth(
        available=True,
        mounted=True,
        free_bytes=cache_usage.free,
        total_bytes=cache_usage.total,
        used_by_recordings_bytes=tier_sizes.get("cache", 0),
        detail="ok",
    )

    primary = await _tier_health("primary", storage_manager.primary_dir(), tier_sizes)

    backup = None
    if config.backup_enabled:
        backup = await _tier_health("backup", storage_manager.backup_dir(), tier_sizes)

    return StorageHealthOut(
        cache=cache,
        primary=primary,
        backup=backup,
        pending_migration_count=await pending_migration_count(),
    )


async def _tier_health(tier: str, path: str, tier_sizes: dict[str, int]) -> TierHealth:
    available = storage_manager.is_primary_available() if tier == "primary" else storage_manager.is_backup_available()
    mounted = mount_manager.is_mounted(path)
    detail = storage_manager.tier_detail(tier)

    free_bytes = total_bytes = None
    if available:
        try:
            usage = await asyncio.to_thread(shutil.disk_usage, path)
            free_bytes, total_bytes = usage.free, usage.total
        except OSError:
            pass

    return TierHealth(
        available=available,
        mounted=mounted,
        free_bytes=free_bytes,
        total_bytes=total_bytes,
        used_by_recordings_bytes=tier_sizes.get(tier, 0),
        detail=detail,
    )
