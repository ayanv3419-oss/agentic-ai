"""asyncpg connection pool — production Postgres adapter.

DESIGN

This is a shadow path: when `settings.database_url` is empty (the default
+ all current tests), `pg_pool()` returns None and nothing in the system
calls into asyncpg. When DATABASE_URL is set, callers can `await pg_pool()`
to get a real pool and run async queries.

The migration recipe (when you're ready to flip the primary engine):

  1. Provision Supabase Postgres (or any compatible).
  2. Set `DATABASE_URL=postgres://user:pass@host:port/db` in .env.
  3. Run `python -m app.database.migrate` (TODO — port the
     `_NEW_TABLE_DDL` from infrastructure.py with `?` → `$1` placeholder
     swap and `datetime('now')` → `now()` clock function). This module
     intentionally does NOT auto-migrate — schema changes are a deploy-
     time action, not a request-time one.
  4. Update `infrastructure.fetch_all` / `fetch_one` / `get_connection`
     to delegate to `pg_pool().fetch(...)` when `engine_kind() ==
     "postgres"`. The sql-string compatibility layer goes there:
        - aiosqlite uses `?` placeholders → translate to `$1, $2, ...`
        - aiosqlite `datetime('now')` → asyncpg `now()`
        - `IF NOT EXISTS` works in both
        - Most SELECTs are byte-compatible already.
  5. `init_database` calls the migrator on boot when DATABASE_URL is set
     (and only then).

This adapter intentionally exposes a small surface (pool, fetch, fetchrow,
execute) so we can extend it as call sites migrate without breaking the
SQLite default.

THREADING

asyncpg is async-only and pool acquire is async. Callers MUST use
`async with` to release connections back to the pool.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import asyncpg

from app.infrastructure import settings


_log = logging.getLogger("agentic_ai.database.postgres")

_POOL: Optional[asyncpg.Pool] = None
_POOL_LOCK = asyncio.Lock()
_POOL_OPEN_FAILED: bool = False  # circuit-breaker; trip after N init failures


class PostgresUnavailableError(RuntimeError):
    """Raised by `pg_pool()` when DATABASE_URL is not set OR the pool
    failed to initialize. Catch this at call sites that have a SQLite
    fallback so the app degrades gracefully."""


async def pg_pool() -> asyncpg.Pool:
    """Lazy pool initializer. Raises `PostgresUnavailableError` when
    DATABASE_URL is unset OR the pool can't be opened.

    First call performs the real `asyncpg.create_pool`; subsequent calls
    reuse the same pool. After a single init failure we set a circuit
    breaker so we don't hammer a broken DB forever — operators must
    explicitly `pg_close()` to retry."""
    global _POOL, _POOL_OPEN_FAILED
    url = (settings.database_url or "").strip()
    if not url:
        raise PostgresUnavailableError(
            "DATABASE_URL is not set; running on SQLite fallback."
        )
    if _POOL_OPEN_FAILED:
        raise PostgresUnavailableError(
            "Postgres pool init failed earlier; call pg_close() to retry."
        )
    if _POOL is not None:
        return _POOL
    async with _POOL_LOCK:
        if _POOL is not None:
            return _POOL
        try:
            _POOL = await asyncpg.create_pool(
                dsn=url,
                min_size=1,
                max_size=10,
                command_timeout=30,
                # Bound the per-connection connect step — without this,
                # an unreachable host (firewall, wrong port, dead DNS) can
                # hang startup for 20+ seconds before TCP gives up. 10s
                # is short enough that a misconfigured DATABASE_URL
                # fails-fast at boot, but long enough to complete the
                # TLS handshake + auth roundtrip on slow networks
                # (Supabase from outside the same region typically
                # negotiates in 1-3s).
                timeout=10,
                # Per-connection setup: enable JSONB support + force UTC.
                init=_pg_connection_setup,
            )
            _log.info("postgres pool opened: %s", _mask(url))
            return _POOL
        except Exception as e:
            _POOL_OPEN_FAILED = True
            _log.exception("postgres pool failed to open: %s", e)
            raise PostgresUnavailableError(f"asyncpg.create_pool failed: {e}")


async def _pg_connection_setup(conn: asyncpg.Connection) -> None:
    """Per-connection setup. Runs once when each pool member is opened.
    Configure here whatever every connection needs: JSON codecs, search
    paths, application name."""
    await conn.execute("SET TIME ZONE 'UTC'")


async def pg_close() -> None:
    """Drain + close the pool, reset circuit breaker. Used on shutdown +
    by tests."""
    global _POOL, _POOL_OPEN_FAILED
    async with _POOL_LOCK:
        if _POOL is not None:
            try:
                await _POOL.close()
            except Exception:
                _log.exception("postgres pool close failed")
        _POOL = None
        _POOL_OPEN_FAILED = False


def pg_status() -> dict[str, Any]:
    """Cheap status inspection for /health. Doesn't open the pool."""
    return {
        "configured":   bool((settings.database_url or "").strip()),
        "pool_open":    _POOL is not None,
        "init_failed":  _POOL_OPEN_FAILED,
        "primary":      bool(settings.postgres_primary),
    }


# ===========================================================================
# Query helpers — `pg_fetch_all` / `pg_fetch_one` / `pg_execute`.
#
# These are the asyncpg counterparts of the existing aiosqlite helpers
# in `infrastructure.py`. They accept SQLite-flavoured SQL and translate
# placeholders + clock functions before dispatch, so call sites that
# work against SQLite continue to work against Postgres.
# ===========================================================================


async def pg_fetch_all(sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[dict[str, Any]]:
    """Async list-of-dicts read against the pool. Translates `?` to `$1`."""
    from app.database.dialect import translate
    pool = await pg_pool()
    pg_sql = translate(sql)
    args = tuple(params)
    async with pool.acquire() as conn:
        records = await conn.fetch(pg_sql, *args)
    return [dict(r) for r in records]


async def pg_fetch_one(sql: str, params: tuple[Any, ...] | list[Any] = ()) -> dict[str, Any] | None:
    """Async single-row read. Returns None when the query produces no
    rows. Translates `?` to `$1`."""
    from app.database.dialect import translate
    pool = await pg_pool()
    pg_sql = translate(sql)
    args = tuple(params)
    async with pool.acquire() as conn:
        record = await conn.fetchrow(pg_sql, *args)
    return dict(record) if record is not None else None


async def pg_execute(sql: str, params: tuple[Any, ...] | list[Any] = ()) -> str:
    """Async DML execute. Returns the asyncpg status string (e.g.
    `"INSERT 0 1"`). Translates `?` to `$1` + clock functions."""
    from app.database.dialect import translate
    pool = await pg_pool()
    pg_sql = translate(sql)
    args = tuple(params)
    async with pool.acquire() as conn:
        return await conn.execute(pg_sql, *args)


def _mask(url: str) -> str:
    if "://" not in url or "@" not in url:
        return "<dsn>"
    scheme, rest = url.split("://", 1)
    creds, hostpart = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:****@{hostpart}"
    return f"{scheme}://{creds}@{hostpart}"
