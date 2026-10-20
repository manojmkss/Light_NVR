import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def test_ensure_indexes_adds_index_missing_from_upgraded_db(tmp_path):
    import app.models  # noqa: F401
    from app.db.schema_sync import ensure_indexes, sync_missing_columns
    from app.db.session import Base

    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        async with engine.begin() as conn:
            # An "old" detections table without the new columns or index.
            await conn.execute(text(
                "CREATE TABLE detections (id INTEGER PRIMARY KEY, camera_id INTEGER, label VARCHAR(64), created_at DATETIME)"
            ))
            await conn.run_sync(Base.metadata.create_all)
            await sync_missing_columns(conn)
            await ensure_indexes(conn)
            cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(detections)"))).fetchall()}
            idx = {r[1] for r in (await conn.execute(text("PRAGMA index_list(detections)"))).fetchall()}
        await engine.dispose()
        return cols, idx

    cols, idx = asyncio.run(go())
    assert {"track_uid", "zones", "stationary_seconds"} <= cols
    assert "ix_detections_track_uid" in idx
