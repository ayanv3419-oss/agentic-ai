"""SQLite connection helpers + DDL bootstrap.

financial_records.db lives at an ABSOLUTE path (resolved by config.py).
This module is the only place that opens connections to it.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

import aiosqlite

from app.config import settings
from app.database.schema import (
    ALLOWED_TABLES,
    COLUMN_TYPES,
    REQUIRED_COLUMNS,
    SCHEMA_COLUMNS,
    SCHEMA_SPEC,
    quoted,
)

log = logging.getLogger("agentic_ai.database")


def db_path() -> Path:
    return Path(settings.financial_db_path)


def _build_create(table: str) -> str:
    cols = []
    for name, sql_type, required in SCHEMA_SPEC:
        null_clause = "NOT NULL" if required else "NULL"
        cols.append(f'  {quoted(name)} {sql_type} {null_clause}')
    cols_sql = ",\n".join(cols)
    return (
        f"CREATE TABLE IF NOT EXISTS {quoted(table)} (\n"
        f"  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        f"  batch_id TEXT NOT NULL,\n"
        f"  source TEXT NOT NULL DEFAULT 'upload',\n"
        f"  file_name TEXT,\n"
        f"  inserted_at TEXT NOT NULL DEFAULT (datetime('now')),\n"
        f"{cols_sql}\n"
        f")"
    )


def _build_indexes(table: str) -> list[str]:
    return [
        f'CREATE INDEX IF NOT EXISTS "idx_{table}_date"    ON {quoted(table)}("Date")',
        f'CREATE INDEX IF NOT EXISTS "idx_{table}_party"   ON {quoted(table)}("Party Name")',
        f'CREATE INDEX IF NOT EXISTS "idx_{table}_batch"   ON {quoted(table)}(batch_id)',
    ]


_UPLOADS_DDL = """
CREATE TABLE IF NOT EXISTS uploads (
    batch_id      TEXT PRIMARY KEY,
    filename      TEXT NOT NULL,
    target        TEXT NOT NULL,
    rows_inserted INTEGER NOT NULL DEFAULT 0,
    rows_failed   INTEGER NOT NULL DEFAULT 0,
    source        TEXT NOT NULL DEFAULT 'upload',
    status        TEXT NOT NULL DEFAULT 'active',  -- active | error | removed
    min_date      TEXT,
    max_date      TEXT,
    error_message TEXT,
    uploaded_at   TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

# Idempotent column additions for already-deployed DBs.
_UPLOADS_ALTERS: list[str] = [
    "ALTER TABLE uploads ADD COLUMN status        TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE uploads ADD COLUMN min_date      TEXT",
    "ALTER TABLE uploads ADD COLUMN max_date      TEXT",
    "ALTER TABLE uploads ADD COLUMN error_message TEXT",
]


async def init_database() -> None:
    """Create / verify the financial DB. Idempotent."""
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(p) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        for table in ALLOWED_TABLES:
            await db.execute(_build_create(table))
            for stmt in _build_indexes(table):
                await db.execute(stmt)
        await db.execute(_UPLOADS_DDL)
        # Forward migrations on already-deployed DBs.
        for stmt in _UPLOADS_ALTERS:
            try:
                await db.execute(stmt)
            except aiosqlite.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        await db.commit()
    log.info("financial DB initialized at %s", p)


@asynccontextmanager
async def get_connection() -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(db_path()) as db:
        db.row_factory = aiosqlite.Row
        yield db


async def fetch_all(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    async with get_connection() as db:
        cur = await db.execute(sql, tuple(params))
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]


async def fetch_one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    rows = await fetch_all(sql, params)
    return rows[0] if rows else None


async def count_rows(table: str) -> int:
    if table not in ALLOWED_TABLES:
        return 0
    row = await fetch_one(f"SELECT COUNT(*) AS n FROM {quoted(table)}")
    return int(row["n"]) if row else 0


def insert_rows(
    table: str,
    rows: list[dict[str, Any]],
    *,
    batch_id: str,
    source: str = "upload",
    file_name: str | None = None,
) -> int:
    """Synchronous bulk insert wrapped in one transaction. Returns rows inserted.

    Run this inside `asyncio.to_thread` from async callers — it uses the
    plain sqlite3 driver because executemany is fastest there.
    """
    if table not in ALLOWED_TABLES:
        raise ValueError(f"unknown table: {table!r}")
    if not rows:
        return 0

    cols = ["batch_id", "source", "file_name", *SCHEMA_COLUMNS]
    placeholders = ",".join(["?"] * len(cols))
    col_sql = ",".join(quoted(c) for c in cols)
    sql = f"INSERT INTO {quoted(table)} ({col_sql}) VALUES ({placeholders})"

    payload: list[tuple[Any, ...]] = []
    for r in rows:
        # Required-column presence is enforced upstream by RowValidator;
        # this layer assumes the row dict is already canonical.
        payload.append((
            batch_id,
            source,
            file_name,
            *(r.get(c) for c in SCHEMA_COLUMNS),
        ))

    conn = sqlite3.connect(str(db_path()), isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN")
        conn.executemany(sql, payload)
        conn.execute("COMMIT")
        return len(payload)
    except Exception:
        log.exception("insert_rows failed")
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return 0
    finally:
        conn.close()


async def record_upload_meta(
    batch_id: str,
    filename: str,
    target: str,
    rows_inserted: int,
    rows_failed: int,
    *,
    source: str = "upload",
    status: str = "active",
    min_date: str | None = None,
    max_date: str | None = None,
    error_message: str | None = None,
) -> None:
    if status not in ("active", "error", "removed"):
        raise ValueError(f"unknown upload status: {status!r}")
    async with get_connection() as db:
        await db.execute(
            """INSERT OR REPLACE INTO uploads
               (batch_id, filename, target, rows_inserted, rows_failed,
                source, status, min_date, max_date, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (batch_id, filename, target, rows_inserted, rows_failed,
             source, status, min_date, max_date, error_message),
        )
        await db.commit()


async def list_uploads_meta(limit: int = 200) -> list[dict[str, Any]]:
    return await fetch_all(
        """SELECT batch_id, filename, target, rows_inserted, rows_failed,
                  source, status, min_date, max_date, error_message, uploaded_at
           FROM uploads
           ORDER BY uploaded_at DESC
           LIMIT ?""",
        (limit,),
    )


async def get_upload_meta(batch_id: str) -> dict[str, Any] | None:
    return await fetch_one(
        """SELECT batch_id, filename, target, rows_inserted, rows_failed,
                  source, status, min_date, max_date, error_message, uploaded_at
           FROM uploads WHERE batch_id = ?""",
        (batch_id,),
    )


async def disconnect_upload(batch_id: str) -> dict[str, Any]:
    """Remove a dataset from active sources.

    Effect:
      • DELETE all rows in `sales` / `purchase` tagged with this batch_id
        — guarantees the query pipeline can't see them again.
      • Flip `uploads.status` to 'removed' (record kept for audit).
    Returns a dict with the rows removed; raises ValueError for unknown
    or already-removed batches so callers can map to clean HTTP responses.
    """
    meta = await get_upload_meta(batch_id)
    if meta is None:
        raise ValueError(f"unknown batch_id: {batch_id}")
    target = meta["target"]
    if target not in ALLOWED_TABLES:
        raise ValueError(f"upload metadata has invalid target: {target!r}")
    if meta["status"] == "removed":
        return {
            "batch_id": batch_id,
            "rows_removed": 0,
            "table": target,
            "already_removed": True,
            "status": "removed",
        }
    async with get_connection() as db:
        cur = await db.execute(
            f'DELETE FROM {quoted(target)} WHERE batch_id = ?',
            (batch_id,),
        )
        rows_removed = cur.rowcount or 0
        await cur.close()
        await db.execute(
            "UPDATE uploads SET status='removed' WHERE batch_id = ?",
            (batch_id,),
        )
        await db.commit()
    log.info(
        "disconnect_upload: batch=%s table=%s rows_removed=%d",
        batch_id, target, rows_removed,
    )
    return {
        "batch_id": batch_id,
        "rows_removed": int(rows_removed),
        "table": target,
        "already_removed": False,
        "status": "removed",
    }
