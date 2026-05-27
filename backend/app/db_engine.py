"""Postgres engine layer — added 2026-05-27 for Supabase migration.

The legacy SQLite code path stays in infrastructure.py untouched. When
DATABASE_URL is set and looks like Postgres, get_connection() (in
infrastructure) delegates to this module instead.

The shim exposes an aiosqlite-compatible surface so existing call sites
keep working without rewrites:

    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM x WHERE y = ?", (val,))
        rows = await cur.fetchall()        # list[dict]-iterable rows
        await cur.close()
        await db.commit()                   # no-op on Postgres

Mechanical SQL translation (regex, conservative):
    ?               -> $1, $2, ...
    datetime('now') -> NOW()
    julianday(...)  -> EXTRACT(EPOCH FROM (...)::timestamptz) / 86400.0
    PRAGMA *        -> stripped (Postgres ignores)
    INSERT OR REPLACE INTO t (...) VALUES (...) -> handled by callers via UPSERT
    INSERT OR IGNORE  -> caller passes ON CONFLICT DO NOTHING explicitly

Everything else is the caller's problem. The translator is intentionally
small so it's predictable.
"""
from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Iterable

log = logging.getLogger("agentic_ai.db_engine")


# ---------------------------------------------------------------------------
# Engine detection
# ---------------------------------------------------------------------------

def database_url() -> str:
    """The DATABASE_URL env var, or '' if unset. Read fresh every call so
    tests that monkeypatch os.environ work."""
    return os.environ.get("DATABASE_URL", "").strip()


def is_postgres() -> bool:
    url = database_url()
    return url.startswith("postgres://") or url.startswith("postgresql://")


def engine_kind() -> str:
    return "postgres" if is_postgres() else "sqlite"


# ---------------------------------------------------------------------------
# asyncpg pool — lazy singleton
# ---------------------------------------------------------------------------

_pool: Any = None  # asyncpg.Pool


async def get_pool() -> Any:
    """Lazy-create the asyncpg pool. Supabase requires SSL; asyncpg detects
    `sslmode=require` in the URL, but we also default to require if not
    specified."""
    global _pool
    if _pool is not None:
        return _pool
    import asyncpg  # local import — only loaded when Postgres is in use

    url = database_url()
    if not url:
        raise RuntimeError("DATABASE_URL not set; cannot create Postgres pool")

    # Supabase pooler URLs sometimes use `postgres://` (deprecated by
    # SQLAlchemy but accepted by asyncpg). Normalize for clarity.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    # Default ssl=require for Supabase. Caller can override by appending
    # ?sslmode=disable to the URL.
    ssl_arg: Any = "require"
    if "sslmode=" in url:
        ssl_arg = None  # let asyncpg parse the URL's sslmode

    # Supabase Supavisor pooler in transaction mode (port 6543) does NOT
    # support prepared statements. Session mode (port 5432) and direct
    # connections do. Disable asyncpg's statement cache only when needed.
    is_tx_pooler = ":6543/" in url
    statement_cache_size = 0 if is_tx_pooler else 100

    _pool = await asyncpg.create_pool(
        dsn=url,
        min_size=1,
        max_size=5,
        command_timeout=30,
        ssl=ssl_arg,
        statement_cache_size=statement_cache_size,
    )
    log.info(
        "asyncpg pool created (min=1 max=5, tx_pooler=%s, stmt_cache=%d)",
        is_tx_pooler, statement_cache_size,
    )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# SQL translation — SQLite → Postgres
# ---------------------------------------------------------------------------

_DATETIME_NOW_RE = re.compile(r"datetime\(\s*'now'\s*\)", re.IGNORECASE)
_STRFTIME_RE = re.compile(r"strftime\(\s*'([^']+)'\s*,\s*([^)]+)\)", re.IGNORECASE)
_JULIANDAY_RE = re.compile(r"julianday\(\s*([^)]+)\)", re.IGNORECASE)
_PRAGMA_RE = re.compile(r"^\s*PRAGMA\b.*$", re.IGNORECASE | re.MULTILINE)
_AUTOINC_RE = re.compile(
    r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", re.IGNORECASE
)
# 1,200 → 1200 only inside quoted SQL — not handled; numeric literals stay as-is.

# strftime token → Postgres to_char token. SQLite uses %Y-%m-%d style; Postgres
# to_char uses YYYY-MM-DD. The map is intentionally small (only the common
# patterns the codebase uses).
_STRFTIME_TOKEN_MAP = {
    "%Y": "YYYY",
    "%m": "MM",
    "%d": "DD",
    "%H": "HH24",
    "%M": "MI",
    "%S": "SS",
    "%W": "IW",   # ISO week number
    "%j": "DDD",  # day of year
}


def _translate_strftime(match: "re.Match[str]") -> str:
    fmt = match.group(1)
    col = match.group(2)
    pg_fmt = fmt
    for sq, pg in _STRFTIME_TOKEN_MAP.items():
        pg_fmt = pg_fmt.replace(sq, pg)
    # If the format contained a '-', '%Y-%m' -> 'YYYY-MM' works fine.
    return f"to_char(({col})::timestamptz, '{pg_fmt}')"


def _translate_julianday(match: "re.Match[str]") -> str:
    col = match.group(1)
    # JULIANDAY returns a fractional day count. Subtracting two julianday()
    # values gives days between them. We model JULIANDAY(X) as the unix
    # epoch in days so the same subtraction semantics hold.
    return f"(EXTRACT(EPOCH FROM ({col})::timestamptz) / 86400.0)"


def _qmark_to_dollar(sql: str) -> str:
    """Convert `?` placeholders to `$1, $2, ...`. Ignores `?` inside
    single-quoted strings and SQL comments."""
    out: list[str] = []
    n = 1
    i = 0
    L = len(sql)
    while i < L:
        ch = sql[i]
        # Skip single-quoted strings — '' is an escaped quote inside one.
        if ch == "'":
            out.append(ch)
            i += 1
            while i < L:
                out.append(sql[i])
                if sql[i] == "'":
                    if i + 1 < L and sql[i + 1] == "'":
                        out.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        # Skip `-- line comments` up to newline.
        if ch == "-" and i + 1 < L and sql[i + 1] == "-":
            while i < L and sql[i] != "\n":
                out.append(sql[i])
                i += 1
            continue
        # Skip /* block comments */
        if ch == "/" and i + 1 < L and sql[i + 1] == "*":
            out.append(sql[i])
            out.append(sql[i + 1])
            i += 2
            while i < L:
                if sql[i] == "*" and i + 1 < L and sql[i + 1] == "/":
                    out.append("*/")
                    i += 2
                    break
                out.append(sql[i])
                i += 1
            continue
        if ch == "?":
            out.append(f"${n}")
            n += 1
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def translate_sql(sql: str) -> str:
    """Apply all SQLite→Postgres rewrites. Idempotent for Postgres-native
    SQL — `to_char` / `NOW()` are unchanged by these regexes."""
    s = sql
    s = _PRAGMA_RE.sub("", s)
    s = _AUTOINC_RE.sub("BIGSERIAL PRIMARY KEY", s)
    s = _DATETIME_NOW_RE.sub("NOW()", s)
    s = _STRFTIME_RE.sub(_translate_strftime, s)
    s = _JULIANDAY_RE.sub(_translate_julianday, s)
    s = _qmark_to_dollar(s)
    return s


# ---------------------------------------------------------------------------
# aiosqlite-compatible shim around an asyncpg connection
# ---------------------------------------------------------------------------

class _PgCursor:
    """Mimics aiosqlite.Cursor enough for the codebase's call sites:

        rows = await cur.fetchall()
        row  = await cur.fetchone()
        await cur.close()

    asyncpg.Record supports `dict(record)`, `record['col']`, and
    `record.keys()` so existing `dict(row)` calls work unchanged.
    """

    __slots__ = ("_rows",)

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[Any]:
        return self._rows

    async def fetchone(self) -> Any | None:
        return self._rows[0] if self._rows else None

    async def close(self) -> None:
        return None


class _PgConnection:
    """Mimics aiosqlite.Connection.

    Supports `await db.execute(sql, params=())`, `await db.executemany(...)`,
    `await db.commit()` (no-op), `await db.fetchall(...)` for completeness.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: Iterable[Any] | None = None) -> _PgCursor:
        translated = translate_sql(sql)
        # Decide whether this returns rows. SELECT / WITH / RETURNING all do.
        head = translated.lstrip().split(None, 1)[0].upper() if translated.strip() else ""
        is_query = head in ("SELECT", "WITH") or " RETURNING " in translated.upper()
        args = tuple(params) if params else ()
        try:
            if is_query:
                rows = await self._conn.fetch(translated, *args)
                return _PgCursor(list(rows))
            else:
                await self._conn.execute(translated, *args)
                return _PgCursor([])
        except Exception as e:
            # Re-raise with the translated SQL so debugging shows what
            # actually ran on Postgres.
            log.error("pg execute failed: %s\nSQL: %s\nargs=%r", e, translated, args)
            raise

    async def executemany(self, sql: str, payload: Iterable[Iterable[Any]]) -> _PgCursor:
        translated = translate_sql(sql)
        args = [tuple(p) for p in payload]
        if not args:
            return _PgCursor([])
        try:
            await self._conn.executemany(translated, args)
            return _PgCursor([])
        except Exception as e:
            log.error("pg executemany failed: %s\nSQL: %s\nrows=%d", e, translated, len(args))
            raise

    async def fetchall(self, sql: str, params: Iterable[Any] | None = None) -> list[Any]:
        cur = await self.execute(sql, params)
        return await cur.fetchall()

    async def commit(self) -> None:
        # asyncpg is auto-commit outside an explicit transaction. The
        # codebase calls `await db.commit()` after writes for the SQLite
        # path — on Postgres it's a no-op.
        return None

    async def rollback(self) -> None:
        return None

    # Some call sites use a row_factory attribute; expose a stub.
    @property
    def row_factory(self) -> Any:
        return None

    @row_factory.setter
    def row_factory(self, _value: Any) -> None:
        return None


@asynccontextmanager
async def pg_connection() -> AsyncIterator[_PgConnection]:
    """Acquire a connection from the pool, wrapped in the aiosqlite shim."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield _PgConnection(conn)


__all__ = [
    "close_pool",
    "database_url",
    "engine_kind",
    "get_pool",
    "is_postgres",
    "pg_connection",
    "translate_sql",
]
