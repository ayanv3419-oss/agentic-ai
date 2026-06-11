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

import asyncio
import contextvars
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Iterable

log = logging.getLogger("agentic_ai.db_engine")


# ---------------------------------------------------------------------------
# Multi-tenant: per-request current tenant + schema isolation
# ---------------------------------------------------------------------------
# A per-request contextvar holds the active tenant id. pg_connection() reads it
# and points search_path at that tenant's schema. The default "public" maps to
# the existing `public` schema, so AUTH-off / single-tenant behavior is
# byte-for-byte unchanged. This module is a LEAF — it must NOT import
# app.tenant_context or app.identity (would create an import cycle); the
# contextvar is set by tenant_context.require_principal instead.
current_tenant_var: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "current_tenant", default="public"
)


def tenant_schema(tenant_id: str | None) -> str:
    """Map a tenant id to its Postgres schema name.

    The default tenant ``"public"`` (``DEFAULT_TENANT_ID``, AUTH-off) maps to the
    existing ``public`` schema. Any real tenant id maps to ``t_<sanitized>``.
    Schema names are interpolated into SQL (identifiers can't be parameterized),
    so the id is sanitized to ``[a-z0-9_]`` here — this is the ONLY place schema
    names are derived, which keeps them identifier-injection safe.
    """
    t = (tenant_id or "public").strip().lower()
    if t == "public":
        return "public"
    safe = "".join(ch for ch in t if ch.isalnum() or ch == "_")
    return f"t_{safe}" if safe else "public"


# ---------------------------------------------------------------------------
# Engine detection
# ---------------------------------------------------------------------------

def database_url() -> str:
    """The DATABASE_URL value, or '' if unset. Reads os.environ first
    (so tests that monkeypatch it work, and so a shell-exported value
    wins). Falls back to the pydantic Settings, which loads from the
    backend's .env file — without this fallback, putting DATABASE_URL
    in .env silently has no effect since pydantic Settings populate
    a separate Settings object, not os.environ."""
    val = os.environ.get("DATABASE_URL", "").strip()
    if val:
        return val
    try:
        from app.infrastructure import get_settings
        return (get_settings().database_url or "").strip()
    except Exception:
        return ""


def is_postgres() -> bool:
    url = database_url()
    return url.startswith("postgres://") or url.startswith("postgresql://")


def engine_kind() -> str:
    return "postgres" if is_postgres() else "sqlite"


# ---------------------------------------------------------------------------
# asyncpg pool — lazy singleton
# ---------------------------------------------------------------------------

_pool: Any = None  # asyncpg.Pool
_pool_lock: "asyncio.Lock | None" = None  # lazily created; guards pool creation


async def get_pool() -> Any:
    """Lazy-create the asyncpg pool. Supabase requires SSL; asyncpg detects
    `sslmode=require` in the URL, but we also default to require if not
    specified."""
    global _pool, _pool_lock
    if _pool is not None:
        return _pool
    # Double-checked locking: serialize concurrent first-callers so a startup
    # burst can't race to create duplicate pools. The lock is created lazily so
    # it binds to the running event loop.
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()
    async with _pool_lock:
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
            min_size=2,
            max_size=20,
            command_timeout=30,
            ssl=ssl_arg,
            statement_cache_size=statement_cache_size,
        )
        log.info(
            "asyncpg pool created (min=2 max=20, tx_pooler=%s, stmt_cache=%d)",
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
# The [^)]+ pattern breaks on nested calls like JULIANDAY(MAX(col)) because
# it stops at the first ')'. We use a balanced-paren extractor instead.
_JULIANDAY_OPEN_RE = re.compile(r"julianday\s*\(", re.IGNORECASE)


def _extract_julianday_arg(sql: str, start: int) -> tuple[str, int] | None:
    """Return (inner_expr, end_index) for the JULIANDAY( call whose opening
    paren is at ``sql[start-1]`` (start is the index after the open paren).
    Handles arbitrary nesting. Returns None if parens are unbalanced."""
    depth = 1
    i = start
    while i < len(sql) and depth > 0:
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
        i += 1
    if depth != 0:
        return None
    return sql[start : i - 1], i
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


def _translate_julianday_expr(col: str) -> str:
    """Translate a single JULIANDAY argument into its Postgres equivalent."""
    return f"(EXTRACT(EPOCH FROM ({col})::timestamptz) / 86400.0)"


def _translate_julianday_balanced(sql: str) -> str:
    """Replace all JULIANDAY(...) calls using a balanced-paren scan so that
    nested calls like JULIANDAY(MAX(col)) are captured correctly.

    Matches from the regex iterator are skipped when they fall inside a range
    already consumed by a prior match (handles expressions like
    JULIANDAY(MAX(col)) where MAX contains parentheses but is not itself a
    JULIANDAY call)."""
    result: list[str] = []
    i = 0
    for m in _JULIANDAY_OPEN_RE.finditer(sql):
        if m.start() < i:
            # This match is inside a range we already consumed — skip it.
            continue
        result.append(sql[i : m.start()])
        inner_start = m.end()
        extracted = _extract_julianday_arg(sql, inner_start)
        if extracted is None:
            # Unbalanced — leave it verbatim; let Postgres surface the error.
            result.append(m.group(0))
            i = inner_start
        else:
            inner, end = extracted
            result.append(_translate_julianday_expr(inner))
            i = end
    result.append(sql[i:])
    return "".join(result)


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
    s = _translate_julianday_balanced(s)
    s = _qmark_to_dollar(s)
    return s


# ---------------------------------------------------------------------------
# aiosqlite-compatible shim around an asyncpg connection
# ---------------------------------------------------------------------------

class _PgCursor:
    """Mimics aiosqlite.Cursor enough for the codebase's call sites:

        rows = await cur.fetchall()
        row  = await cur.fetchone()
        cur.rowcount
        await cur.close()

    asyncpg.Record supports `dict(record)`, `record['col']`, and
    `record.keys()` so existing `dict(row)` calls work unchanged.
    """

    __slots__ = ("_rows", "rowcount")

    def __init__(self, rows: list[Any], rowcount: int = -1) -> None:
        self._rows = rows
        self.rowcount = rowcount

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
                status = await self._conn.execute(translated, *args)
                # asyncpg returns a status string like "DELETE 3" or "UPDATE 1".
                # Parse it so callers can read cur.rowcount correctly.
                rc = -1
                if isinstance(status, str):
                    parts = status.split()
                    if len(parts) >= 2 and parts[-1].isdigit():
                        rc = int(parts[-1])
                return _PgCursor([], rowcount=rc)
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

    def transaction(self) -> Any:
        # asyncpg's native transaction context manager. Use as:
        #   async with db.transaction():
        #       await db.execute(...)
        # On exception inside the block, the transaction is ROLLBACKed.
        return self._conn.transaction()

    # Some call sites use a row_factory attribute; expose a stub.
    @property
    def row_factory(self) -> Any:
        return None

    @row_factory.setter
    def row_factory(self, _value: Any) -> None:
        return None


@asynccontextmanager
async def pg_connection() -> AsyncIterator[_PgConnection]:
    """Acquire a connection from the pool, wrapped in the aiosqlite shim.

    Before yielding, point ``search_path`` at the current tenant's schema (read
    from ``current_tenant_var``) so unqualified table references and
    ``information_schema`` introspection resolve to the tenant's OWN schema. When
    the tenant is ``"public"`` (the default) the search_path is left untouched —
    byte-for-byte identical to the pre-multitenant behavior.
    """
    pool = await get_pool()
    # Bounded acquire: under pool exhaustion (e.g. many concurrent users) fail
    # fast with a clean asyncpg timeout instead of hanging forever. The timeout
    # applies to BOTH the "public" default path and the per-tenant path below,
    # since both acquire from this same connection.
    async with pool.acquire(timeout=15) as conn:
        schema = tenant_schema(current_tenant_var.get())
        if schema == "public":
            # Default tenant: leave the search_path untouched — byte-for-byte the
            # pre-multitenant behavior.
            yield _PgConnection(conn)
        else:
            # Real tenant: wrap the whole block in ONE transaction and SET LOCAL the
            # search_path. SET LOCAL is transaction-scoped, which means it (a) applies
            # reliably on Supabase's transaction pooler — a session-level `SET` would
            # NOT persist across pooled statements there — and (b) auto-resets at
            # COMMIT, so a pooled connection can never leak this tenant's search_path
            # into a later request for a different tenant.
            #
            # STRICT (slice 2c): the tenant schema ONLY — NO `, public` fallback. A
            # missing table must error inside the tenant's own schema rather than
            # silently resolving to another tenant's (or the shared public) data.
            # First-party tables that legitimately live in public are reached by
            # callers that qualify them explicitly (e.g. `public.conversations`).
            async with conn.transaction():
                await conn.execute(f'SET LOCAL search_path TO "{schema}"')
                yield _PgConnection(conn)


async def ensure_tenant_schema(tenant_id: str) -> None:
    """Idempotently provision a tenant's schema (``CREATE SCHEMA IF NOT EXISTS``).

    Additive and safe to call repeatedly. The ``"public"`` tenant already has its
    schema, so it's a no-op. Uses a RAW pool connection (bypassing pg_connection's
    search_path wrapper) so the CREATE SCHEMA runs before the schema exists — using
    pg_connection() here would attempt SET LOCAL search_path to the not-yet-created
    schema and fail."""
    schema = tenant_schema(tenant_id)
    if schema == "public":
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')


@asynccontextmanager
async def tenant_pg_connection(tenant_id: str) -> AsyncIterator[_PgConnection]:
    """Like pg_connection, but binds the tenant for the transaction via a
    transaction-local GUC (`app.current_tenant`). Added for multi-tenant
    slice 2a: it's a harmless no-op until RLS policies read that GUC in a
    later step. Yields the same aiosqlite shim so call sites are unchanged."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_tenant', $1, true)", tenant_id
            )
            yield _PgConnection(conn)


__all__ = [
    "close_pool",
    "current_tenant_var",
    "database_url",
    "engine_kind",
    "ensure_tenant_schema",
    "get_pool",
    "is_postgres",
    "pg_connection",
    "tenant_pg_connection",
    "tenant_schema",
    "translate_sql",
]
