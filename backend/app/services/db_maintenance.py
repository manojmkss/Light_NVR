import asyncio
import logging
import time
from datetime import datetime, timezone

from app.db.session import AsyncSessionLocal, engine
from app.models.backup_settings import BackupSettings
from app.schemas.backup import OptimizeResult

logger = logging.getLogger(__name__)

OPTIMIZE_INTERVAL_SECONDS = 86400  # daily - this app's DB is small enough that more frequent runs add nothing


async def optimize_database() -> OptimizeResult:
    """VACUUM reclaims space left behind by retention's regular deletes (this
    app's workload is unusual for SQLite in that it deletes rows constantly,
    so free pages would otherwise accumulate indefinitely); PRAGMA optimize
    refreshes the query planner statistics SQLite's own docs recommend
    running periodically for long-lived connections.
    """
    started = time.monotonic()
    try:
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.exec_driver_sql("VACUUM")
            await conn.exec_driver_sql("PRAGMA optimize")
    except Exception as exc:
        logger.exception("Database optimization failed")
        return OptimizeResult(success=False, message=str(exc), duration_seconds=time.monotonic() - started)

    async with AsyncSessionLocal() as db:
        record = await db.get(BackupSettings, 1)
        record.last_optimize_at = datetime.now(timezone.utc)
        await db.commit()

    duration = time.monotonic() - started
    logger.info("Database optimization completed in %.2fs", duration)
    return OptimizeResult(success=True, message="Optimized", duration_seconds=duration)


async def db_maintenance_loop() -> None:
    while True:
        try:
            await optimize_database()
        except Exception:
            logger.exception("DB maintenance loop iteration failed")
        await asyncio.sleep(OPTIMIZE_INTERVAL_SECONDS)
