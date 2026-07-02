import logging

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

logger = logging.getLogger(__name__)

engine = create_async_engine(settings.database_url, echo=False, future=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _connection_record):
    # WAL is materially more crash-resistant than the default rollback
    # journal - a power cut mid-write leaves the WAL file replayable instead
    # of risking a half-written main database file. synchronous=NORMAL is
    # the pairing SQLite's own docs recommend for WAL (synchronous=FULL adds
    # an fsync this app's write volume doesn't need).
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    # Without this, a writer that can't immediately acquire the lock (e.g.
    # VACUUM running against the DB maintenance loop) raises "database is
    # locked" right away instead of waiting - this app has many independent
    # short-lived sessions (camera workers, retention, mover, API requests)
    # that can legitimately collide, so a bounded wait beats an instant error.
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.close()


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    import app.models  # noqa: F401 ensure models are registered
    from app.db.schema_sync import sync_missing_columns

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await sync_missing_columns(conn)


async def check_integrity() -> bool:
    """Cheap startup sanity check - PRAGMA quick_check is much faster than a
    full integrity_check and is what SQLite's own docs suggest for routine
    verification. Only meant to surface corruption from a bad crash/power
    cut, not to attempt any repair.
    """
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA quick_check"))
        rows = result.fetchall()

    ok = len(rows) == 1 and rows[0][0] == "ok"
    if not ok:
        logger.critical("Database integrity check failed: %s", rows)
    return ok
