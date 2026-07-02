import asyncio
import logging
import os
import shutil

from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.recording import Recording
from app.models.storage_config import StorageConfig
from app.services import mount_manager
from app.services.events import emit_event

logger = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL_SECONDS = 30
PROBE_TIMEOUT_SECONDS = 5
MIN_FREE_BYTES = 500 * 1024**2  # absolute floor regardless of configured cap


def _tier_params(config: StorageConfig, tier: str) -> dict | None:
    if tier == "primary":
        return {
            "type": config.primary_type,
            "path": config.primary_path,
            "remote_spec": config.primary_remote_spec,
            "username": config.primary_username,
            "password": config.primary_password,
        }
    if tier == "backup":
        if not config.backup_enabled:
            return None
        return {
            "type": config.backup_type,
            "path": config.backup_path,
            "remote_spec": config.backup_remote_spec,
            "username": config.backup_username,
            "password": config.backup_password,
        }
    return None


def _probe_writable_sync(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    test_file = os.path.join(path, ".lightnvr_health_check")
    with open(test_file, "w") as f:
        f.write("ok")
    os.remove(test_file)


async def probe_writable(path: str) -> tuple[bool, str]:
    """Actually tries to write, not just checks /proc/mounts - a stale or
    half-dead network mount often still shows as mounted while being
    unusable. Wrapped in a timeout since the underlying syscall can hang
    rather than error on a half-dead connection.
    """
    try:
        await asyncio.wait_for(asyncio.to_thread(_probe_writable_sync, path), timeout=PROBE_TIMEOUT_SECONDS)
        return True, "ok"
    except asyncio.TimeoutError:
        return False, f"timed out after {PROBE_TIMEOUT_SECONDS}s (path unresponsive)"
    except OSError as exc:
        return False, str(exc)


class StorageManager:
    """Owns mounting and health tracking for the primary/backup storage tiers
    and the always-local cache. Health is probed on a background loop rather
    than inline in the recording hot path, so a slow/half-dead network mount
    can never stall a write - callers just read the last-known state.
    """

    def __init__(self):
        self._config: StorageConfig | None = None
        self._primary_available = False
        self._primary_detail = "not checked yet"
        self._backup_available = False
        self._backup_detail = "not checked yet"
        self._was_primary_available: bool | None = None
        self._was_backup_available: bool | None = None
        self._health_task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.reload_config()
        self._health_task = asyncio.create_task(self._health_loop())

    async def shutdown(self) -> None:
        if self._health_task:
            self._health_task.cancel()
            await asyncio.gather(self._health_task, return_exceptions=True)

    async def reload_config(self) -> None:
        async with AsyncSessionLocal() as db:
            config = await db.get(StorageConfig, 1)
        self._config = config
        os.makedirs(config.cache_path, exist_ok=True)
        await self._ensure_mounted("primary")
        await self._ensure_mounted("backup")
        await self.refresh_health()

    async def _ensure_mounted(self, tier: str) -> None:
        params = _tier_params(self._config, tier)
        if params is None:
            return
        if params["type"] == "local":
            os.makedirs(params["path"], exist_ok=True)
            return
        if not params["remote_spec"]:
            logger.warning("Storage tier '%s' is type=%s but has no remote share configured", tier, params["type"])
            return
        success, message = await mount_manager.mount_share(
            params["path"], params["type"], params["remote_spec"], params["username"], params["password"]
        )
        if not success:
            logger.warning("Could not mount %s share for tier '%s': %s", params["type"], tier, message)

    async def refresh_health(self) -> None:
        config = self._config
        if config is None:
            return

        primary_ok, primary_detail = await probe_writable(config.primary_path)
        self._set_primary(primary_ok, primary_detail)

        if config.backup_enabled:
            backup_ok, backup_detail = await probe_writable(config.backup_path)
            self._set_backup(backup_ok, backup_detail)
        else:
            self._backup_available = False
            self._backup_detail = "disabled"
            self._was_backup_available = None

    def _set_primary(self, available: bool, detail: str) -> None:
        if self._was_primary_available is not None and self._was_primary_available != available:
            asyncio.create_task(self._emit_tier_event("primary", available, detail))
        self._was_primary_available = available
        self._primary_available = available
        self._primary_detail = detail

    def _set_backup(self, available: bool, detail: str) -> None:
        if self._was_backup_available is not None and self._was_backup_available != available:
            asyncio.create_task(self._emit_tier_event("backup", available, detail))
        self._was_backup_available = available
        self._backup_available = available
        self._backup_detail = detail

    async def _emit_tier_event(self, tier: str, available: bool, detail: str) -> None:
        if available:
            await emit_event(None, "system", f"{tier.capitalize()} storage is back online")
        else:
            await emit_event(None, "system", f"{tier.capitalize()} storage is unreachable: {detail}")

    async def _health_loop(self) -> None:
        while True:
            try:
                config = self._config
                if config and config.primary_type != "local" and not self._primary_available:
                    await self._ensure_mounted("primary")
                if config and config.backup_enabled and config.backup_type != "local" and not self._backup_available:
                    await self._ensure_mounted("backup")
                await self.refresh_health()
            except Exception:
                logger.exception("Storage health check failed")
            await asyncio.sleep(HEALTH_CHECK_INTERVAL_SECONDS)

    # --- accessors used by the recorder/mover/API ---

    def cache_dir(self) -> str:
        return self._config.cache_path if self._config else "/storage/cache"

    def primary_dir(self) -> str:
        return self._config.primary_path if self._config else "/mnt/primary"

    def backup_dir(self) -> str | None:
        if self._config and self._config.backup_enabled:
            return self._config.backup_path
        return None

    def is_primary_available(self) -> bool:
        return self._primary_available

    def is_backup_available(self) -> bool:
        return self._backup_available

    def tier_detail(self, tier: str) -> str:
        return self._primary_detail if tier == "primary" else self._backup_detail

    async def cache_has_room(self) -> tuple[bool, str]:
        config = self._config
        if config is None:
            return True, "ok"

        try:
            usage = await asyncio.to_thread(shutil.disk_usage, config.cache_path)
        except OSError as exc:
            return False, f"cache path unreadable: {exc}"

        if usage.free < MIN_FREE_BYTES:
            return False, "cache disk almost full"

        if config.cache_max_gb > 0:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(func.coalesce(func.sum(Recording.size_bytes), 0)).where(Recording.storage_tier == "cache")
                )
                used_by_cache = result.scalar_one()
            if used_by_cache >= config.cache_max_gb * 1024**3:
                return False, f"cache at configured cap ({config.cache_max_gb}GB)"

        return True, "ok"


storage_manager = StorageManager()
