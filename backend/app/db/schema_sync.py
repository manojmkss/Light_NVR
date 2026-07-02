import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.session import Base

logger = logging.getLogger(__name__)


def _default_clause(column) -> str:
    if column.default is None or not column.default.is_scalar:
        return ""
    value = column.default.arg
    if isinstance(value, bool):
        return f" DEFAULT {1 if value else 0}"
    if isinstance(value, (int, float)):
        return f" DEFAULT {value}"
    if isinstance(value, str):
        return f" DEFAULT '{value}'"
    return ""


async def sync_missing_columns(conn: AsyncConnection) -> None:
    """SQLite has no `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` or `ALTER
    COLUMN`, and Base.metadata.create_all() only creates tables that don't
    exist yet - it never alters existing ones. Without this, changing a
    model crashes every upgrade against anyone's existing database file.
    Handles the two cases that actually come up here: a new column, and an
    existing NOT NULL column relaxed to nullable (a table rebuild is the
    only way SQLite supports that).
    """
    for table in Base.metadata.sorted_tables:
        result = await conn.execute(text(f"PRAGMA table_info({table.name})"))
        # row: (cid, name, type, notnull, dflt_value, pk)
        existing = {row[1]: row for row in result.fetchall()}
        if not existing:
            continue  # table doesn't exist yet - create_all() just made it complete already

        needs_rebuild = False
        for column in table.columns:
            if column.name not in existing:
                col_type = column.type.compile(dialect=conn.dialect)
                logger.info("Schema sync: adding column %s.%s", table.name, column.name)
                await conn.execute(
                    text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type}{_default_clause(column)}")
                )
            elif existing[column.name][3] and column.nullable:
                needs_rebuild = True

        if needs_rebuild:
            await _relax_constraints(conn, table)


async def _relax_constraints(conn: AsyncConnection, table) -> None:
    """Only safe/used for tables with no incoming foreign keys and a simple
    primary key - currently just system_secret. A table with relationships
    pointing at it would need the rebuild to also juggle those.
    """
    logger.info("Schema sync: rebuilding %s to relax a NOT NULL constraint", table.name)
    tmp_name = f"{table.name}__rebuild"
    column_defs = ", ".join(
        f"{col.name} {col.type.compile(dialect=conn.dialect)}" + ("" if col.nullable else " NOT NULL")
        for col in table.columns
    )
    column_names = ", ".join(col.name for col in table.columns)
    pk_columns = [col.name for col in table.primary_key.columns]
    pk_clause = f", PRIMARY KEY ({', '.join(pk_columns)})" if pk_columns else ""

    await conn.execute(text(f"DROP TABLE IF EXISTS {tmp_name}"))
    await conn.execute(text(f"CREATE TABLE {tmp_name} ({column_defs}{pk_clause})"))
    await conn.execute(text(f"INSERT INTO {tmp_name} ({column_names}) SELECT {column_names} FROM {table.name}"))
    await conn.execute(text(f"DROP TABLE {table.name}"))
    await conn.execute(text(f"ALTER TABLE {tmp_name} RENAME TO {table.name}"))
