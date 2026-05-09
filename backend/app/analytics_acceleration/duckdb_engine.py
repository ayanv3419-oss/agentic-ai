"""DuckDB engine wrapper.

The engine is an in-memory DuckDB connection that ATTACHes the production
SQLite file as a read-only catalog via the `sqlite` extension. Queries are
compiled by DuckDB's columnar engine and pushed down to SQLite when
possible — so we get vectorised aggregation without having to maintain a
second copy of the data.

Usage (from a sub-agent or tool):

    rows = await get_duckdb_engine().query(
        "SELECT substr(\"Date\", 1, 7) AS ym, "
        "       SUM(\"Total Amount\") AS sales "
        "FROM main.sales "
        "WHERE \"Date\" GLOB ? "
        "GROUP BY ym ORDER BY ym",
        ("????-??-??",),
    )

The engine is process-wide and re-uses the same connection. DuckDB's
default isolation is fine here — we never write through this connection.

Threading: DuckDB connections are NOT thread-safe by default but we serialize
through an asyncio.Lock + run_in_executor to keep the call sites async-shaped.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator

import duckdb

from app.infrastructure import settings


_log = logging.getLogger("agentic_ai.acceleration.duckdb")


def _resolve_db_path() -> str:
    return os.path.abspath(settings.financial_db_path)


class DuckDBEngine:
    """Thin async wrapper around a single in-memory DuckDB connection that
    ATTACHes the SQLite source of truth.

    The connection is rebuilt lazily — calling `reset()` after a schema
    change (or after an upload bumps data_version) makes the next query
    pick up the freshest catalog.
    """

    def __init__(self) -> None:
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._sync_lock = threading.Lock()  # guards self._conn assignment
        self._async_lock = asyncio.Lock()
        self._db_path = _resolve_db_path()

    # ---- Connection lifecycle -------------------------------------------

    def _build_connection(self) -> duckdb.DuckDBPyConnection:
        """Build a fresh in-memory DuckDB connection and ATTACH the SQLite
        source of truth as a read-only catalog. Catalog is exposed as
        `main.<table>` (so existing call sites that say `FROM sales` Just
        Work without an alias)."""
        path = _resolve_db_path()
        conn = duckdb.connect(database=":memory:")
        # PRAGMAs come first — DuckDB fails late on invalid values otherwise.
        conn.execute(f"PRAGMA threads = {max(1, int(settings.duckdb_threads))}")
        conn.execute(f"PRAGMA memory_limit = '{max(64, int(settings.duckdb_memory_mb))}MB'")
        # Install + load the sqlite_scanner so we can ATTACH the SQLite
        # database as a read-only catalog.
        conn.execute("INSTALL sqlite")
        conn.execute("LOAD sqlite")
        # Quote the path — Windows paths contain backslashes that break
        # naive concatenation. DuckDB accepts the quoted form.
        path_lit = path.replace("'", "''")
        conn.execute(f"ATTACH '{path_lit}' AS sqlite_src (TYPE sqlite, READ_ONLY)")
        # Promote the attached catalog to be the default search path so
        # `FROM sales` resolves to `sqlite_src.sales` without an alias —
        # query strings are 1:1 with the existing SQLite SQL we already
        # have in `analytical_queries.py`.
        conn.execute("USE sqlite_src")
        _log.info("DuckDB engine built: attached %s (threads=%d memory=%dMB)",
                  path, settings.duckdb_threads, settings.duckdb_memory_mb)
        self._db_path = path
        return conn

    def _connection(self) -> duckdb.DuckDBPyConnection:
        with self._sync_lock:
            current_path = _resolve_db_path()
            if self._conn is None or self._db_path != current_path:
                # Path drift (e.g. tests swap FINANCIAL_DB_PATH) → rebuild.
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                self._conn = self._build_connection()
            return self._conn

    def reset(self) -> None:
        """Force the next query to rebuild the connection. Call after an
        upload changes the SQLite catalog OR after settings.financial_db_path
        is reassigned in tests."""
        with self._sync_lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    @contextmanager
    def _conn_ctx(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Sync context manager used by the async wrapper. Holds the
        connection for the duration of one query — DuckDB's cursor-per-
        statement model means we don't actually need to clone."""
        conn = self._connection()
        yield conn

    # ---- Public query API -----------------------------------------------

    async def query(
        self,
        sql: str,
        params: tuple[Any, ...] | list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Run `sql` against the attached SQLite catalog and return rows as
        list[dict]. Async: serializes through `_async_lock` then offloads to
        the executor since DuckDB's Python API is sync-only."""
        params = tuple(params) if params else ()
        async with self._async_lock:
            return await asyncio.get_running_loop().run_in_executor(
                None, self._query_sync, sql, params,
            )

    def _query_sync(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        with self._conn_ctx() as conn:
            cur = conn.execute(sql, params) if params else conn.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(settings.duckdb_enabled),
            "threads": settings.duckdb_threads,
            "memory_mb": settings.duckdb_memory_mb,
            "attached_path": self._db_path,
            "connection_open": self._conn is not None,
        }


# ---- Singleton + introspection -------------------------------------------

_ENGINE: DuckDBEngine | None = None


def get_duckdb_engine() -> DuckDBEngine:
    """Process-wide engine singleton. Cheap (DuckDB connection is built
    on first query, not on this call)."""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = DuckDBEngine()
    return _ENGINE


def duckdb_status() -> dict[str, Any]:
    """Cheap inspection for /health — does NOT force connection build."""
    if not settings.duckdb_enabled:
        return {"enabled": False, "reason": "DUCKDB_ENABLED=false"}
    if _ENGINE is None:
        return {"enabled": True, "connection_open": False}
    return _ENGINE.status()
