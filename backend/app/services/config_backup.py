import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import settings
from app.db.session import AsyncSessionLocal, engine
from app.models.backup_settings import BackupSettings
from app.services.events import emit_event
from app.services.storage_manager import storage_manager

logger = logging.getLogger(__name__)

BACKUP_SUBDIR = "config-backups"
LOCAL_FALLBACK_DIR = "/data/config-backups"
CHECK_INTERVAL_SECONDS = 1800
SQLITE_MAGIC = b"SQLite format 3\x00"


@dataclass
class BackupInfo:
    filename: str
    location: str  # "primary" | "local"
    path: str
    size_bytes: int
    created_at: datetime


def live_db_path() -> str:
    prefix = "sqlite+aiosqlite:///"
    url = settings.database_url
    if not url.startswith(prefix):
        raise ValueError(f"Unsupported database URL for backup/restore: {url}")
    return url[len(prefix):]


def _backup_dirs() -> list[tuple[str, str]]:
    """All locations backups might live, in preference order."""
    dirs = []
    if storage_manager.is_primary_available():
        dirs.append((os.path.join(storage_manager.primary_dir(), BACKUP_SUBDIR), "primary"))
    dirs.append((LOCAL_FALLBACK_DIR, "local"))
    return dirs


async def _vacuum_into(destination: str) -> None:
    # VACUUM INTO manages its own transaction; running it under SQLAlchemy's
    # normal implicit BEGIN breaks it, so this connection opts out via
    # AUTOCOMMIT instead of using engine.begin().
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.exec_driver_sql("VACUUM INTO ?", (destination,))


async def create_backup() -> BackupInfo:
    primary_available = storage_manager.is_primary_available()
    base_dir = os.path.join(storage_manager.primary_dir(), BACKUP_SUBDIR) if primary_available else LOCAL_FALLBACK_DIR
    location = "primary" if primary_available else "local"

    os.makedirs(base_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc)
    filename = f"lightnvr_backup_{timestamp.strftime('%Y%m%d_%H%M%S')}.db"
    destination = os.path.join(base_dir, filename)

    try:
        await _vacuum_into(destination)
    except Exception as exc:
        logger.exception("Config backup failed")
        async with AsyncSessionLocal() as db:
            record = await db.get(BackupSettings, 1)
            record.last_backup_error = str(exc)[:500]
            await db.commit()
        raise

    size_bytes = os.path.getsize(destination)

    async with AsyncSessionLocal() as db:
        record = await db.get(BackupSettings, 1)
        record.last_backup_at = timestamp
        record.last_backup_filename = filename
        record.last_backup_location = location
        record.last_backup_error = None
        retention_count = record.retention_count
        await db.commit()

    await _prune_old_backups(retention_count)

    logger.info("Created config backup %s (%s, %d bytes)", filename, location, size_bytes)
    return BackupInfo(filename=filename, location=location, path=destination, size_bytes=size_bytes, created_at=timestamp)


async def list_backups() -> list[BackupInfo]:
    backups: list[BackupInfo] = []
    for base_dir, location in _backup_dirs():
        if not os.path.isdir(base_dir):
            continue
        for filename in os.listdir(base_dir):
            if not filename.endswith(".db"):
                continue
            path = os.path.join(base_dir, filename)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            backups.append(
                BackupInfo(
                    filename=filename,
                    location=location,
                    path=path,
                    size_bytes=stat.st_size,
                    created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                )
            )
    backups.sort(key=lambda b: b.created_at, reverse=True)
    return backups


async def _prune_old_backups(retention_count: int) -> None:
    if retention_count <= 0:
        return
    backups = await list_backups()
    for stale in backups[retention_count:]:
        try:
            os.remove(stale.path)
        except OSError:
            logger.warning("Could not remove old backup %s", stale.path)


async def find_backup(filename: str) -> BackupInfo | None:
    for backup in await list_backups():
        if backup.filename == filename:
            return backup
    return None


def is_valid_sqlite_file(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            header = f.read(len(SQLITE_MAGIC))
        return header == SQLITE_MAGIC
    except OSError:
        return False


async def restore_from_path(source_path: str) -> None:
    """Swaps the live database file for source_path, then exits the process
    so Docker's `restart: unless-stopped` policy gives a clean full restart -
    far simpler and more reliable than manually tearing down and reloading
    every in-memory singleton (camera workers, JWT secret, storage config) by
    hand. The caller is expected to send its HTTP response before this fires.
    """
    if not is_valid_sqlite_file(source_path):
        raise ValueError("File is not a valid SQLite database")

    live_path = live_db_path()
    await engine.dispose()

    import shutil

    tmp_path = f"{live_path}.restoring"
    shutil.copy2(source_path, tmp_path)
    os.replace(tmp_path, live_path)

    # Stale WAL/SHM files from the old database would otherwise sit next to
    # the restored file; SQLite recreates them fresh as soon as it reopens it.
    for suffix in ("-wal", "-shm"):
        stale = f"{live_path}{suffix}"
        if os.path.exists(stale):
            try:
                os.remove(stale)
            except OSError:
                pass

    logger.warning("Database restored from %s - restarting process for a clean reload", source_path)

    def _exit():
        os._exit(0)

    asyncio.get_running_loop().call_later(1.5, _exit)


async def backup_loop() -> None:
    while True:
        try:
            async with AsyncSessionLocal() as db:
                settings_row = await db.get(BackupSettings, 1)
                enabled = settings_row.enabled
                interval_hours = settings_row.interval_hours
                last_at = settings_row.last_backup_at

            due = last_at is None or (datetime.now(timezone.utc) - last_at.replace(tzinfo=timezone.utc)).total_seconds() >= interval_hours * 3600
            if enabled and due:
                try:
                    await create_backup()
                except Exception:
                    await emit_event(None, "system", "Scheduled config backup failed - see logs for details")
        except Exception:
            logger.exception("Backup loop iteration failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
