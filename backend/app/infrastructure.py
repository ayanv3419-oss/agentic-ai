"""Storage + ingestion data layer.

This module is the lowest layer of the backend. Nothing inside `app/` may
import from `tools`, `agents`, or `api`; conversely those modules all
ultimately import their persistence helpers from here.

Sections (skim with the section banners below):

    1.  Settings          — pydantic-settings singleton loaded from .env
    2.  Errors            — `envelope` JSON helper + SAFE_MESSAGE constant
    3.  Schema            — SCHEMA_SPEC / ALLOWED_TABLES / HEADER_ALIASES
    4.  Connection        — async + sync DB helpers, DDL bootstrap
    5.  Upload registry   — uploads-table CRUD (`record_upload_meta`, etc.)
    6.  Header detection  — alias index + header-row picker
    7.  Upload parsers    — CSV / XLSX streaming with auto header detect
    8.  Response cache    — `data/response_store.json` atomic JSON store
    9.  Memory / synonyms — entity-resolution backing store
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Standard-library imports used across sections
# ---------------------------------------------------------------------------

import csv
import hashlib
import io
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any, AsyncIterator, Iterable, Iterator

import aiosqlite
from openpyxl import load_workbook
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("agentic_ai.database")


# ===========================================================================
# 1. SETTINGS
# ===========================================================================

# Project root = .../Agentic Ai (the directory containing both backend/ and frontend/).
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _abs(p: str) -> str:
    """Resolve a path against the project root if it isn't already absolute."""
    if not p:
        return p
    return p if os.path.isabs(p) else str(PROJECT_ROOT / p)


class Settings(BaseSettings):
    # Absolute env-file path so settings load deterministically regardless
    # of where the server is launched (project root, backend/, or via
    # uvicorn directly). The previous relative `".env"` only worked when
    # CWD happened to be the project root.
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / "backend" / ".env"),
        extra="ignore",
        case_sensitive=False,
    )

    # --- Groq -----------------------------------------------------------
    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")
    groq_model: str = Field(default="llama-3.3-70b-versatile", alias="GROQ_MODEL")
    groq_base_url: str = Field(default="https://api.groq.com/openai/v1", alias="GROQ_BASE_URL")

    # --- Cost / safety budgets ------------------------------------------
    max_loop_iterations: int = Field(default=8, alias="MAX_LOOP_ITERATIONS")
    cost_limit_usd: float = Field(default=1.0, alias="COST_LIMIT_USD")
    sql_max_bytes_scanned: int = Field(
        default=10 * 1024 * 1024 * 1024, alias="SQL_MAX_BYTES_SCANNED"
    )

    # --- Storage paths (absolute) ---------------------------------------
    financial_db_path: str = Field(
        default=str(PROJECT_ROOT / "data" / "financial_records.db"),
        alias="FINANCIAL_DB_PATH",
    )
    response_store_path: str = Field(
        default=str(PROJECT_ROOT / "data" / "response_store.json"),
        alias="RESPONSE_STORE_PATH",
    )
    synonyms_path: str = Field(
        default=str(PROJECT_ROOT / "backend" / "memory" / "synonyms.json"),
        alias="SYNONYMS_PATH",
    )

    # --- Upload limits --------------------------------------------------
    # 50 MB default — was 1 GB, which is a footgun for a synchronous request
    # handler. Real production uploads should go through the background-job
    # path (queue → worker → Supabase Storage). Bump via env if a tenant
    # genuinely needs more.
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, alias="MAX_UPLOAD_BYTES")  # 50 MB
    upload_chunk_bytes: int = Field(default=1024 * 1024, alias="UPLOAD_CHUNK_BYTES")  # 1 MB

    # --- Backend swap targets (placeholder env vars; today's deployment
    # ignores them, but the abstractions consult them so the same env var
    # names work the day Postgres / Redis / Supabase Storage are wired in).
    database_url:      str = Field(default="", alias="DATABASE_URL")          # postgres://...
    redis_url:         str = Field(default="", alias="REDIS_URL")             # redis://...
    # Upstash Redis REST — HTTPS-based Redis access for hosts that
    # can't open port 6379. When both URL + TOKEN are set, this takes
    # priority over `redis_url` because most deploys that need REST
    # also can't reach 6379.
    upstash_redis_rest_url:    str = Field(default="", alias="UPSTASH_REDIS_REST_URL")
    upstash_redis_rest_token:  str = Field(default="", alias="UPSTASH_REDIS_REST_TOKEN")
    # Feature flag: when True AND DATABASE_URL is set AND the probe
    # succeeds, fetch_all/fetch_one/get_connection route to asyncpg.
    # Default False so existing behaviour (SQLite) is preserved.
    postgres_primary:  bool = Field(default=False, alias="POSTGRES_PRIMARY")
    supabase_url:      str = Field(default="", alias="SUPABASE_URL")
    supabase_anon_key: str = Field(default="", alias="SUPABASE_ANON_KEY")
    supabase_service_role_key: str = Field(default="", alias="SUPABASE_SERVICE_ROLE_KEY")
    supabase_storage_bucket: str = Field(default="agentic-uploads", alias="SUPABASE_STORAGE_BUCKET")

    # --- Rate limiting ---------------------------------------------------
    rate_limit_per_minute: int = Field(default=30, alias="RATE_LIMIT_PER_MINUTE")

    # --- Observability (Sentry) -----------------------------------------
    # Empty DSN = monitoring is a no-op. Sentry SDK is always imported (so
    # span/breadcrumb call sites never branch); when no DSN is configured
    # the SDK silently drops events.
    sentry_dsn:                    str = Field(default="", alias="SENTRY_DSN")
    sentry_environment:            str = Field(default="development", alias="SENTRY_ENVIRONMENT")
    sentry_traces_sample_rate:     float = Field(default=0.1, alias="SENTRY_TRACES_SAMPLE_RATE")
    sentry_profiles_sample_rate:   float = Field(default=0.0, alias="SENTRY_PROFILES_SAMPLE_RATE")
    sentry_send_default_pii:       bool = Field(default=False, alias="SENTRY_SEND_DEFAULT_PII")

    # --- DuckDB analytical acceleration ---------------------------------
    duckdb_enabled:    bool = Field(default=True, alias="DUCKDB_ENABLED")
    duckdb_threads:    int = Field(default=4, alias="DUCKDB_THREADS")
    duckdb_memory_mb:  int = Field(default=512, alias="DUCKDB_MEMORY_MB")

    # --- Vector retrieval -----------------------------------------------
    # 'in_memory' (default) | 'pgvector' | 'qdrant' — only in_memory has
    # an impl in this pass; the other two are future drop-ins reading the
    # same VectorStore protocol.
    vector_backend:    str = Field(default="in_memory", alias="VECTOR_BACKEND")
    vector_dim:        int = Field(default=128, alias="VECTOR_DIM")
    qdrant_url:        str = Field(default="", alias="QDRANT_URL")
    qdrant_api_key:    str = Field(default="", alias="QDRANT_API_KEY")

    # --- Server ---------------------------------------------------------
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")
    reload: bool = Field(default=False, alias="RELOAD")

    # --- Session (placeholder OAuth) ------------------------------------
    session_secret: str = Field(
        default="dev-session-secret-CHANGE-ME", alias="SESSION_SECRET"
    )

    # --- Admin login + bearer-token auth --------------------------------
    # Credentials MUST come from the environment. Empty defaults => login is
    # disabled (every login attempt returns 401) — fail-closed by design.
    admin_username:       str = Field(default="", alias="ADMIN_USERNAME")
    admin_password:       str = Field(default="", alias="ADMIN_PASSWORD")
    auth_token_secret:    str = Field(default="dev-auth-secret-CHANGE-ME", alias="AUTH_TOKEN_SECRET")
    auth_token_ttl_hours: int = Field(default=24 * 7, alias="AUTH_TOKEN_TTL_HOURS")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Force absolute resolution after pydantic parsing.
        self.financial_db_path = _abs(self.financial_db_path)
        self.response_store_path = _abs(self.response_store_path)
        self.synonyms_path = _abs(self.synonyms_path)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


# ===========================================================================
# 2. ERRORS — envelope helper used by every JSON route
# ===========================================================================

SAFE_MESSAGE = "Something went wrong (safely handled)"

ErrorKind = str  # "validation" | "auth" | "upload" | "internal" | ...


def envelope(
    error: str,
    *,
    detail: str | None = None,
    kind: ErrorKind = "internal",
    message: str = SAFE_MESSAGE,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": error,
        "detail": detail or "",
        "kind": kind,
        "message": message,
    }
    if extra:
        body.update(extra)
    return body


# ===========================================================================
# 3. SCHEMA — single source of truth for the financial_records.db tables.
# ===========================================================================

# (column_name, sql_type, is_required)
# Required columns must be present + parseable on every uploaded row.
SCHEMA_SPEC: list[tuple[str, str, bool]] = [
    ("Date",                 "TEXT", True),
    ("Order No",             "TEXT", False),
    ("Invoice No",           "TEXT", False),
    ("Party Name",           "TEXT", False),
    ("Product Name",         "TEXT", False),
    ("GSTIN",                "TEXT", False),
    ("Party Phone No.",      "TEXT", False),
    ("Transaction Type",     "TEXT", False),
    ("Total Amount",         "REAL", True),
    ("Loyalty Redeemed",     "REAL", False),
    ("Payment Type",         "TEXT", False),
    ("Received/Paid Amount", "REAL", False),
    ("Balance Due",          "REAL", False),
    ("Payment Status",       "TEXT", False),
    ("Description",          "TEXT", False),
    ("Cash",                 "REAL", False),
    ("BHARAT PAY",           "REAL", False),
    ("Credit",               "REAL", False),
    ("Debit Cards",          "REAL", False),
    ("HDFC BANK",            "REAL", False),
]

SCHEMA_COLUMNS: list[str] = [name for name, _t, _r in SCHEMA_SPEC]
COLUMN_TYPES: dict[str, str] = {name: t for name, t, _r in SCHEMA_SPEC}
REQUIRED_COLUMNS: list[str] = [name for name, _t, r in SCHEMA_SPEC if r]
OPTIONAL_COLUMNS: list[str] = [name for name, _t, r in SCHEMA_SPEC if not r]

ALLOWED_TABLES: tuple[str, ...] = ("sales", "purchase")

# Closed alias map. HeaderMapper rejects an upload whose REQUIRED columns
# don't have a matching header (matching = normalize_key + lookup).
HEADER_ALIASES: dict[str, list[str]] = {
    "Date": [
        "date", "dt", "sale date", "sales date", "purchase date",
        "transaction date", "txn date", "trans date",
        "order date", "bill date", "invoice date", "voucher date",
    ],
    "Order No": [
        "order no", "order number", "order #", "ord no", "ordno",
        "sl no", "sl. no", "s. no", "serial no", "sr no", "sno",
    ],
    "Invoice No": [
        "invoice no", "invoice number", "invoice #", "inv no",
        "bill no", "bill no.", "bill number", "voucher no", "voucher number",
    ],
    "Party Name": [
        "party name", "party", "name", "customer", "customer name",
        "client", "client name", "buyer",
        "supplier", "supplier name", "vendor", "vendor name",
    ],
    "Product Name": [
        "product name", "product", "item", "item name",
        "sku", "sku name", "brand", "brand name", "model",
        "article", "article name", "variant",
    ],
    "GSTIN": ["gstin", "gst no", "gst number", "gst", "gst id"],
    "Party Phone No.": [
        "party phone no", "party phone", "phone", "phone no",
        "phone number", "mobile", "mobile no", "mobile number",
        "contact", "contact no", "contact number",
    ],
    "Transaction Type": ["transaction type", "type", "txn type", "trans type"],
    "Total Amount": [
        "total amount", "total", "amount", "amt", "total amt",
        "grand total", "net amount", "bill amount", "invoice amount",
        "value", "final amount", "sale amount",
    ],
    "Loyalty Redeemed": [
        "loyalty redeemed", "loyalty", "loyalty points",
        "points redeemed", "loyalty pts", "reward points",
    ],
    "Payment Type": [
        "payment type", "payment method", "mode", "pay mode",
        "payment mode", "method",
    ],
    "Received/Paid Amount": [
        "received/paid amount", "received paid amount",
        "received amount", "paid amount", "received", "paid",
        "amount paid", "amount received",
    ],
    "Balance Due": [
        "balance due", "balance", "due", "outstanding",
        "due amount", "amount due", "remaining",
    ],
    "Payment Status": ["payment status", "status", "pay status"],
    "Description": [
        "description", "desc", "note", "notes", "remarks",
        "narration", "particulars",
    ],
    "Cash":        ["cash"],
    "BHARAT PAY":  ["bharat pay", "bharatpay", "bhim", "upi", "bhim upi"],
    "Credit":      ["credit"],
    "Debit Cards": ["debit cards", "debit card", "debit"],
    "HDFC BANK":   ["hdfc bank", "hdfc"],
}


def quoted(name: str) -> str:
    """SQL-quote an identifier that may contain spaces / dots / slashes."""
    return '"' + str(name).replace('"', '""') + '"'


def schema_dict() -> dict:
    """Serialize the schema for SchemaRetriever / SqlValidator consumers."""
    tables: dict[str, dict] = {}
    for table in ALLOWED_TABLES:
        tables[table] = {
            "description": (
                "Customer sales transactions"
                if table == "sales"
                else "Supplier purchase transactions"
            ),
            "columns": [
                {"name": name, "type": t, "required": r, "nullable": not r}
                for name, t, r in SCHEMA_SPEC
            ],
            "indexes": ["Date", "Party Name", "batch_id"],
        }
    return {
        "dialect": "sqlite",
        "database_file": "data/financial_records.db",
        "tables": tables,
        "notes": [
            "Date column is always ISO YYYY-MM-DD (normalized at ingest).",
            "Optional columns are SQL NULL when missing — never the string '0' or 0.0.",
            'Always double-quote columns that contain spaces or dots: "Total Amount", "Party Name", "Party Phone No.".',
        ],
    }


# ===========================================================================
# 4. CONNECTION — DDL bootstrap + async/sync helpers
# ===========================================================================

def db_path() -> Path:
    return Path(settings.financial_db_path)


def _build_create(table: str) -> str:
    cols = []
    for name, sql_type, required in SCHEMA_SPEC:
        null_clause = "NOT NULL" if required else "NULL"
        cols.append(f'  {quoted(name)} {sql_type} {null_clause}')
    cols_sql = ",\n".join(cols)
    # NOTE: tenant_id / workspace_id / user_id are NULLABLE today (single-
    # admin deployment writes NULL) but present so every existing query in
    # the codebase can grow a tenant filter the day multi-tenancy lands —
    # no schema migration required, just a code change to add WHERE clauses.
    return (
        f"CREATE TABLE IF NOT EXISTS {quoted(table)} (\n"
        f"  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        f"  tenant_id TEXT,\n"
        f"  workspace_id TEXT,\n"
        f"  user_id TEXT,\n"
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
        f'CREATE INDEX IF NOT EXISTS "idx_{table}_tenant"  ON {quoted(table)}(tenant_id)',
    ]


# Idempotent column additions for already-created sales/purchase tables —
# the DB on disk pre-dates the multi-tenant columns, so we ALTER TABLE on
# every boot. SQLite swallows duplicate ADD COLUMN errors via the helper
# in `init_database`.
_TABLE_TENANT_ALTERS: tuple[str, ...] = ("tenant_id", "workspace_id", "user_id")


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
    "ALTER TABLE uploads ADD COLUMN tenant_id     TEXT",
    "ALTER TABLE uploads ADD COLUMN workspace_id  TEXT",
    "ALTER TABLE uploads ADD COLUMN user_id       TEXT",
]


# ---------------------------------------------------------------------------
# Multi-user / SaaS tables — workspaces + users + conversations + cache.
#
# Schema is Postgres-shape (UUID-string ids, timestamp columns) so the
# day a real Postgres engine is wired in, the migrations port directly.
# SQLite's TEXT type is forwards-compatible with Postgres VARCHAR/UUID.
# ---------------------------------------------------------------------------

_WORKSPACES_DDL = """
CREATE TABLE IF NOT EXISTS workspaces (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    name          TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_USERS_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    tenant_id       TEXT NOT NULL,
    workspace_id    TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'owner',
                                  -- owner | admin | viewer
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at   TEXT
)
"""

_CONVERSATIONS_DDL = """
CREATE TABLE IF NOT EXISTS conversations (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    workspace_id    TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    title           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_active_at  TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

# Future Redis-backed cache will mirror this shape; today we keep an
# RDBMS table for durable caching across worker restarts.
_ANALYTICS_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS analytics_cache (
    cache_key       TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    workspace_id    TEXT,
    payload         TEXT NOT NULL,    -- JSON
    data_version    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT
)
"""

# memberships — many-to-many relationship between users and workspaces.
# Today (single-workspace-per-user) the row count = users count, but the
# shape supports invitations + multiple workspaces in future.
_MEMBERSHIPS_DDL = """
CREATE TABLE IF NOT EXISTS memberships (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    workspace_id    TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'viewer',  -- owner|admin|analyst|viewer
    invited_by      TEXT,
    invited_at      TEXT,
    accepted_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(workspace_id, user_id)
)
"""

# audit_logs — every privileged action lands here. Read by ops + future
# admin UI. Append-only by convention; never UPDATE this table.
_AUDIT_LOGS_DDL = """
CREATE TABLE IF NOT EXISTS audit_logs (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT,
    workspace_id    TEXT,
    user_id         TEXT,
    request_id      TEXT,
    action          TEXT NOT NULL,           -- e.g. 'auth.register', 'upload.success'
    resource_type   TEXT,                    -- 'user' | 'upload' | 'cache' | ...
    resource_id     TEXT,
    metadata        TEXT,                    -- JSON
    ip              TEXT,
    user_agent      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

# feedback — per-turn 👍/👎 from the user. Per-tenant; lets us improve the
# rule layer without an LLM. Empty until /feedback endpoint is added.
_FEEDBACK_DDL = """
CREATE TABLE IF NOT EXISTS feedback (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    workspace_id    TEXT,
    user_id         TEXT NOT NULL,
    conversation_id TEXT,
    turn_id         TEXT,
    rating          INTEGER NOT NULL,        -- -1 (👎), 0 (neutral), 1 (👍)
    comment         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_NEW_TABLE_DDL: tuple[tuple[str, str], ...] = (
    ("workspaces",       _WORKSPACES_DDL),
    ("users",            _USERS_DDL),
    ("conversations",    _CONVERSATIONS_DDL),
    ("analytics_cache",  _ANALYTICS_CACHE_DDL),
    ("memberships",      _MEMBERSHIPS_DDL),
    ("audit_logs",       _AUDIT_LOGS_DDL),
    ("feedback",         _FEEDBACK_DDL),
)

_NEW_TABLE_INDEXES: tuple[str, ...] = (
    'CREATE INDEX IF NOT EXISTS "idx_users_tenant"        ON users(tenant_id)',
    'CREATE INDEX IF NOT EXISTS "idx_users_workspace"     ON users(workspace_id)',
    'CREATE INDEX IF NOT EXISTS "idx_workspaces_tenant"   ON workspaces(tenant_id)',
    'CREATE INDEX IF NOT EXISTS "idx_conv_tenant_user"    ON conversations(tenant_id, user_id)',
    'CREATE INDEX IF NOT EXISTS "idx_cache_tenant"        ON analytics_cache(tenant_id, data_version)',
    'CREATE INDEX IF NOT EXISTS "idx_uploads_tenant"      ON uploads(tenant_id)',
    'CREATE INDEX IF NOT EXISTS "idx_memberships_workspace" ON memberships(workspace_id, user_id)',
    'CREATE INDEX IF NOT EXISTS "idx_memberships_user"    ON memberships(user_id)',
    'CREATE INDEX IF NOT EXISTS "idx_audit_tenant_action" ON audit_logs(tenant_id, action, created_at)',
    'CREATE INDEX IF NOT EXISTS "idx_audit_user"          ON audit_logs(user_id, created_at)',
    'CREATE INDEX IF NOT EXISTS "idx_feedback_tenant"     ON feedback(tenant_id, created_at)',
)


async def init_database() -> None:
    """Create / verify the financial DB. Idempotent."""
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(p) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        for table in ALLOWED_TABLES:
            await db.execute(_build_create(table))
            # Forward-migration FIRST — add tenant / workspace / user
            # columns to already-deployed tables that pre-date multi-
            # tenant support. Must run BEFORE _build_indexes because the
            # tenant index references tenant_id; on an old DB the column
            # doesn't exist yet.
            # ADD COLUMN is cheap in SQLite + idempotent via the duplicate-
            # column swallow below.
            for tenant_col in _TABLE_TENANT_ALTERS:
                try:
                    await db.execute(
                        f'ALTER TABLE {quoted(table)} ADD COLUMN {tenant_col} TEXT'
                    )
                except aiosqlite.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            # NOW create indexes — tenant_id is guaranteed to exist.
            for stmt in _build_indexes(table):
                await db.execute(stmt)
            # Forward-migration: add any SCHEMA_SPEC column that an older
            # version of the DB doesn't have yet. SQLite's `ADD COLUMN` is
            # cheap (no row rewrite) and safe to run on every boot.
            for col_name, sql_type, required in SCHEMA_SPEC:
                null_clause = "NOT NULL" if required else "NULL"
                try:
                    await db.execute(
                        f'ALTER TABLE {quoted(table)} '
                        f'ADD COLUMN {quoted(col_name)} {sql_type} {null_clause}'
                    )
                except aiosqlite.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
        await db.execute(_UPLOADS_DDL)
        # Forward migrations on already-deployed DBs.
        for stmt in _UPLOADS_ALTERS:
            try:
                await db.execute(stmt)
            except aiosqlite.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        # Multi-user / SaaS tables — workspaces, users, conversations,
        # analytics_cache. All idempotent.
        for table_name, ddl in _NEW_TABLE_DDL:
            await db.execute(ddl)
        for stmt in _NEW_TABLE_INDEXES:
            await db.execute(stmt)
        await db.commit()
    log.info("financial DB initialized at %s", p)


class _PgConnectionAdapter:
    """Adapts an asyncpg.Connection to the small subset of the
    aiosqlite.Connection API that this codebase actually uses:

      - `.execute(sql, params)` (returns nothing useful in our usage)
      - `.commit()` is a no-op (asyncpg is autocommit outside explicit
        transactions; our existing call sites don't rely on
        transactional semantics across multiple .execute calls).

    Placeholder + clock translation runs inside `.execute` so call-site
    SQL stays SQLite-flavoured."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        from app.database.dialect import translate
        pg_sql = translate(sql)
        return await self._conn.execute(pg_sql, *tuple(params))

    async def commit(self) -> None:
        """No-op — asyncpg outside an explicit transaction is autocommit.
        Existing call sites that do `await db.commit()` after one or
        two execs work correctly without further action."""
        return None


@asynccontextmanager
async def get_connection() -> AsyncIterator[Any]:
    """Engine-routed connection context. Yields:
      - an aiosqlite.Connection      when POSTGRES_PRIMARY=false (default)
      - a `_PgConnectionAdapter`     when POSTGRES_PRIMARY=true and the
                                     pool is reachable

    On any Postgres error during acquire, falls back to SQLite — same
    graceful-degradation policy as `fetch_all` / `fetch_one`."""
    if _postgres_primary_active():
        try:
            from app.database.pg_adapter import pg_pool
            pool = await pg_pool()
            async with pool.acquire() as conn:
                yield _PgConnectionAdapter(conn)
                return
        except Exception as e:
            log.warning(
                "postgres get_connection failed; falling back to SQLite: %s: %s",
                type(e).__name__, e,
            )
    async with aiosqlite.connect(db_path()) as db:
        db.row_factory = aiosqlite.Row
        yield db


def _postgres_primary_active() -> bool:
    """True when the operator has flipped POSTGRES_PRIMARY=true AND
    DATABASE_URL is set. This is the single gate that switches read/
    write traffic from SQLite to asyncpg.

    The function is cheap + side-effect-free (doesn't probe the pool);
    pool readiness is verified separately at startup."""
    return bool(settings.postgres_primary and settings.database_url)


async def fetch_all(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    """Engine-routed list-of-dicts read.

    When `POSTGRES_PRIMARY=true` AND DATABASE_URL is reachable, queries
    are translated `?` → `$1, $2, ...` and run against asyncpg. On any
    Postgres error the call falls back to SQLite — so a transient
    Postgres blip doesn't take the system down. The fallback is logged
    so ops can see degradation."""
    if _postgres_primary_active():
        try:
            from app.database.pg_adapter import pg_fetch_all
            return await pg_fetch_all(sql, tuple(params))
        except Exception as e:
            log.warning(
                "postgres fetch_all failed; falling back to SQLite: %s: %s",
                type(e).__name__, e,
            )
    async with get_connection() as db:
        cur = await db.execute(sql, tuple(params))
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]


async def fetch_one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    """Engine-routed single-row read. Same routing logic as `fetch_all`."""
    if _postgres_primary_active():
        try:
            from app.database.pg_adapter import pg_fetch_one
            return await pg_fetch_one(sql, tuple(params))
        except Exception as e:
            log.warning(
                "postgres fetch_one failed; falling back to SQLite: %s: %s",
                type(e).__name__, e,
            )
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
    tenant_id: str | None = None,
    workspace_id: str | None = None,
    user_id: str | None = None,
) -> int:
    """Synchronous bulk insert wrapped in one transaction. Returns rows inserted.

    Run this inside `asyncio.to_thread` from async callers — it uses the
    plain sqlite3 driver because executemany is fastest there.

    Multi-tenant: every row is stamped with `tenant_id` / `workspace_id` /
    `user_id` (NULLABLE). The dashboard + analytics WHEREs read these so a
    user only ever sees their own tenant's data.
    """
    if table not in ALLOWED_TABLES:
        raise ValueError(f"unknown table: {table!r}")
    if not rows:
        return 0

    cols = [
        "batch_id", "source", "file_name",
        "tenant_id", "workspace_id", "user_id",
        *SCHEMA_COLUMNS,
    ]
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
            tenant_id,
            workspace_id,
            user_id,
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


# ===========================================================================
# 5. UPLOAD REGISTRY — uploads-table CRUD
# ===========================================================================

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


# ===========================================================================
# 6. HEADER DETECTION — alias index + best-row picker
# ===========================================================================

def normalize_key(s: Any) -> str:
    """Lowercase, replace any non-alphanumeric char with space, collapse whitespace.

    Examples:
      'TOTAL AMOUNT '        -> 'total amount'
      'Total.Amount'         -> 'total amount'
      'Party-Phone-No.'      -> 'party phone no'
      'Received / Paid Amt'  -> 'received paid amt'
    """
    raw = str(s if s is not None else "").lower()
    cleaned_chars = [
        ch if ch.isalnum() or ch.isspace() else " "
        for ch in raw
    ]
    return " ".join("".join(cleaned_chars).split())


# Alias index built once at import.
_ALIAS_INDEX: dict[str, str] = {}
for _canonical, _aliases in HEADER_ALIASES.items():
    _ALIAS_INDEX[normalize_key(_canonical)] = _canonical
    for _alias in _aliases:
        _ALIAS_INDEX[normalize_key(_alias)] = _canonical


def alias_lookup(raw: Any) -> str | None:
    """Return the canonical column name for a raw header cell, or None."""
    if raw is None:
        return None
    return _ALIAS_INDEX.get(normalize_key(raw))


def score_row_as_header(
    cells: list[Any],
) -> tuple[int, dict[str, str], list[str]]:
    """Score how well `cells` could serve as a header row.

    Returns:
      score            — required matches × 100 + optional matches × 1
                         (so any row missing a REQUIRED column scores < 100)
      header_index     — raw_cell_string → canonical column name
      missing_required — REQUIRED columns not matched in this row
    """
    seen_canonical: set[str] = set()
    header_index: dict[str, str] = {}
    for raw in cells:
        if raw is None:
            continue
        s = str(raw).strip()
        if not s:
            continue
        canonical = _ALIAS_INDEX.get(normalize_key(s))
        if canonical is None:
            continue
        if canonical in seen_canonical:
            # First occurrence wins — duplicate header columns are reported as extras.
            continue
        header_index[s] = canonical
        seen_canonical.add(canonical)

    required_matched = sum(1 for c in REQUIRED_COLUMNS if c in seen_canonical)
    optional_matched = len(seen_canonical) - required_matched
    score = required_matched * 100 + optional_matched
    missing = [c for c in REQUIRED_COLUMNS if c not in seen_canonical]
    return score, header_index, missing


def find_header_row(
    rows_buffer: list[list[Any]],
) -> tuple[int, list[Any], dict[str, str]]:
    """Pick the best valid header row from a small buffer of candidate rows.

    A row is *valid* iff it matches all REQUIRED columns. Among valid rows,
    the one with the highest score wins; ties broken by earliest row.

    Raises ValueError with a diagnostic sample if no row qualifies.
    """
    candidates: list[tuple[int, int, list[Any], dict[str, str]]] = []
    for idx, row in enumerate(rows_buffer):
        if row is None:
            continue
        score, header_index, missing = score_row_as_header(list(row))
        if missing:
            continue
        candidates.append((score, idx, list(row), header_index))

    if not candidates:
        sample = []
        for i, row in enumerate(rows_buffer[:5]):
            preview = [str(c)[:30] for c in (row or [])][:8]
            sample.append(f"row {i + 1}: {preview}")
        raise ValueError(
            f"No row in the first {len(rows_buffer)} contained all required "
            f"columns ({REQUIRED_COLUMNS}). Sample: {' | '.join(sample)}"
        )

    candidates.sort(key=lambda x: (-x[0], x[1]))
    score, idx, header_cells, header_index = candidates[0]
    return idx, header_cells, header_index


def map_headers_strict(
    header: list[str],
) -> tuple[dict[str, str], list[str], list[str]]:
    """Map an already-chosen header row to canonical columns.

    Returns (header_index, missing_required, unmatched_extras).
    """
    seen_canonical: set[str] = set()
    header_index: dict[str, str] = {}
    unmatched: list[str] = []
    for raw in header:
        if not raw:
            continue
        canonical = _ALIAS_INDEX.get(normalize_key(raw))
        if canonical is None:
            unmatched.append(raw)
            continue
        if canonical in seen_canonical:
            unmatched.append(raw)
            continue
        header_index[raw] = canonical
        seen_canonical.add(canonical)
    missing = [c for c in REQUIRED_COLUMNS if c not in seen_canonical]
    return header_index, missing, unmatched


# ===========================================================================
# 7. UPLOAD PARSERS — CSV + XLSX with auto header detection
# ===========================================================================

MAX_HEADER_SCAN = 15


class UploadError(ValueError):
    """File-level failure: empty / unsupported / unreadable / no header."""


def _row_to_record(keys: list[str], row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    out: dict[str, Any] = {}
    for i, k in enumerate(keys):
        if not k:
            continue
        v = row[i] if i < len(row) else None
        out[k] = v
    return out


# ---------- in-memory parsers (kept for callers that already hold bytes) ----

def parse_csv_bytes(content: bytes) -> tuple[list[str], dict[str, str], list[dict[str, Any]]]:
    text = content.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise UploadError("CSV is empty")
    return _materialize(rows)


def parse_xlsx_bytes(content: bytes) -> tuple[list[str], dict[str, str], list[dict[str, Any]]]:
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as e:
        raise UploadError(f"Could not open xlsx: {e}")
    try:
        ws = _select_xlsx_sheet(wb)[1]
        rows = [list(r) if r is not None else [] for r in ws.iter_rows(values_only=True)]
        if not rows:
            raise UploadError("Selected sheet is empty")
        return _materialize(rows)
    finally:
        try:
            wb.close()
        except Exception:
            pass


def _materialize(rows: list[list[Any]]) -> tuple[list[str], dict[str, str], list[dict[str, Any]]]:
    """Detect header in `rows[:MAX_HEADER_SCAN]`, then build header + data list."""
    buffer = rows[:MAX_HEADER_SCAN]
    try:
        header_idx, header_cells, header_index = find_header_row(buffer)
    except ValueError as e:
        raise UploadError(str(e)) from e
    header = [str(c).strip() if c is not None else "" for c in header_cells]
    data = []
    for row in rows[header_idx + 1 :]:
        rec = _row_to_record(header, row)
        if rec:
            data.append(rec)
    return header, header_index, data


# ---------- streaming with auto-detection (used by /upload tempfile path) ---

def stream_parse_csv_with_detection(
    path: Path,
) -> tuple[list[str], dict[str, str], Iterator[dict[str, Any]]]:
    """Open `path`, detect header row in first MAX_HEADER_SCAN rows, return
    (header, header_index, generator).

    The generator owns the file handle: it closes the file in its `finally`
    block when exhausted (or when its `.close()` is called).
    """
    f = open(path, "r", encoding="utf-8-sig", errors="replace", newline="")
    try:
        reader = csv.reader(f)
        buffer: list[list[Any]] = []
        for _ in range(MAX_HEADER_SCAN):
            try:
                buffer.append(next(reader))
            except StopIteration:
                break
        if not buffer:
            f.close()
            raise UploadError("CSV is empty")
        try:
            header_idx, header_cells, header_index = find_header_row(buffer)
        except ValueError as e:
            f.close()
            raise UploadError(str(e)) from e
        header = [str(c).strip() if c is not None else "" for c in header_cells]
        # Anything in the buffer between the header row and end-of-buffer is
        # the first stretch of data; the rest comes from `reader`.
        data_buffer_tail = list(buffer[header_idx + 1 :])
    except Exception:
        f.close()
        raise

    def _gen() -> Iterator[dict[str, Any]]:
        try:
            for row in data_buffer_tail:
                rec = _row_to_record(header, row)
                if rec:
                    yield rec
            for row in reader:
                rec = _row_to_record(header, row)
                if rec:
                    yield rec
        finally:
            try:
                f.close()
            except Exception:
                pass

    return header, header_index, _gen()


def _select_xlsx_sheet(wb) -> tuple[str, Any, int, list[Any], dict[str, str]]:
    """Pick the sheet to read from. Returns (sheet_name, worksheet, header_idx,
    header_cells, header_index).

    Algorithm:
      1. Try wb.active first. If first MAX_HEADER_SCAN rows yield a valid
         header, use it (per spec: "default to first sheet").
      2. Otherwise, scan every sheet's first MAX_HEADER_SCAN rows; pick the
         highest-scoring valid sheet.
      3. If still none found, raise UploadError.
    """
    def _scan_sheet(name: str) -> tuple[int, int, list[Any], dict[str, str]] | None:
        ws_local = wb[name]
        buffer: list[list[Any]] = []
        for i, row in enumerate(ws_local.iter_rows(values_only=True)):
            if i >= MAX_HEADER_SCAN:
                break
            buffer.append(list(row) if row is not None else [])
        if not buffer:
            return None
        try:
            idx, cells, hi = find_header_row(buffer)
        except ValueError:
            return None
        score = len(hi)
        return score, idx, cells, hi

    active_name = wb.active.title if wb.active is not None else None
    if active_name:
        active_result = _scan_sheet(active_name)
        if active_result is not None:
            score, idx, cells, hi = active_result
            log.info("xlsx: using active sheet %r (score=%d, header_row=%d)",
                     active_name, score, idx + 1)
            return active_name, wb[active_name], idx, cells, hi
        log.info("xlsx: active sheet %r has no valid header — scanning others", active_name)

    candidates: list[tuple[int, str, int, list[Any], dict[str, str]]] = []
    for name in wb.sheetnames:
        if name == active_name:
            continue  # already tried
        result = _scan_sheet(name)
        if result is None:
            continue
        score, idx, cells, hi = result
        candidates.append((score, name, idx, cells, hi))

    if not candidates:
        raise UploadError(
            f"No sheet contained a valid header row in first {MAX_HEADER_SCAN} rows. "
            f"Sheets tried: {wb.sheetnames}"
        )
    candidates.sort(key=lambda x: -x[0])
    score, name, idx, cells, hi = candidates[0]
    log.info("xlsx: using sheet %r (score=%d, header_row=%d)", name, score, idx + 1)
    return name, wb[name], idx, cells, hi


def stream_parse_xlsx_with_detection(
    path: Path,
) -> tuple[list[str], dict[str, str], Iterator[dict[str, Any]], str]:
    """Open the XLSX, pick the best sheet, detect header, return
    (header, header_index, generator, sheet_name).
    """
    wb = load_workbook(filename=str(path), read_only=True, data_only=True)
    try:
        sheet_name, ws, header_idx, header_cells, header_index = _select_xlsx_sheet(wb)
        header = [str(c).strip() if c is not None else "" for c in header_cells]
    except Exception:
        try:
            wb.close()
        except Exception:
            pass
        raise

    def _gen() -> Iterator[dict[str, Any]]:
        try:
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i <= header_idx:
                    continue
                row_list = list(row) if row is not None else []
                rec = _row_to_record(header, row_list)
                if rec:
                    yield rec
        finally:
            try:
                wb.close()
            except Exception:
                pass

    return header, header_index, _gen(), sheet_name


# Backwards-compatible name used elsewhere if it shows up in tests.
def parse_file(filename: str, content: bytes):
    name = (filename or "").lower()
    if name.endswith(".csv"):
        return parse_csv_bytes(content)
    if name.endswith(".xlsx"):
        return parse_xlsx_bytes(content)
    raise UploadError(f"Unsupported file type: {filename!r} (use .csv or .xlsx)")


# ===========================================================================
# 8. RESPONSE CACHE — version-stamped, tenant-aware, swappable backend
# ===========================================================================
#
# Architecture today: two things in one section.
#
# 1) `CacheStore` Protocol + `JsonFileCacheStore` impl. The protocol is the
#    contract; today's deployment uses the JSON-file impl. To swap to Redis
#    later, write a `RedisCacheStore(CacheStore)` and bind it via env var.
#    No call site changes.
#
# 2) `cache_key_for(question, *, tenant_id, conversation_id)` — every key
#    embeds the global `data_version` counter (bumped by every upload), so
#    the cache is correctness-by-construction: a stale entry is unreachable
#    once new data lands. No "did we remember to invalidate" footguns.
#
# Module-level helper functions (`get_cached`, `put_cached`, `invalidate_all`,
# `cache_size`) keep the legacy import paths working — they delegate to the
# active store, which `set_cache_store` swaps under test or in production.

_CACHE_LOCK = Lock()
_cache_log = logging.getLogger("agentic_ai.cache")


# ---- Data-version counter -------------------------------------------------

_DATA_VERSION_LOCK = Lock()


def _data_version_path() -> Path:
    return Path(settings.financial_db_path).with_suffix(".version")


def get_data_version() -> int:
    """Read the global data_version counter. Bumped by every upload so that
    cached responses keyed against the OLD version are unreachable to new
    requests. Today this is a one-line file; trivially Redis-replaceable."""
    p = _data_version_path()
    if not p.exists():
        return 0
    try:
        return int(p.read_text(encoding="utf-8").strip() or "0")
    except (ValueError, OSError):
        return 0


def bump_data_version() -> int:
    """Increment + persist the data version. Called after every successful
    upload OR any other path that mutates analytical data. Returns the new
    version."""
    with _DATA_VERSION_LOCK:
        v = get_data_version() + 1
        p = _data_version_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".version.tmp")
        tmp.write_text(str(v), encoding="utf-8")
        tmp.replace(p)
    _cache_log.info("data_version bumped to %d", v)
    return v


# ---- CacheStore protocol --------------------------------------------------

class CacheStore:
    """Minimal interface for response/conversation/rate-limit storage.
    Today's impl is JSON file; tomorrow's Redis impl honours the same five
    methods. Keep this dependency-free (no aiosqlite, no aiofiles)."""

    def get(self, key: str) -> dict[str, Any] | None: ...   # noqa: E704
    def put(self, key: str, value: dict[str, Any]) -> None: ...   # noqa: E704
    def invalidate_all(self) -> int: ...   # noqa: E704
    def size(self) -> int: ...   # noqa: E704
    def kind(self) -> str: ...   # noqa: E704  identifier for logs


class JsonFileCacheStore(CacheStore):
    """Atomic-JSON-file impl. Single-process, single-writer. Suitable for
    today's single-admin deployment; production should swap to Redis once
    REDIS_URL is provisioned."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def kind(self) -> str:
        return "json_file"

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            _cache_log.warning(
                "response_store load failed; treating as empty", exc_info=True,
            )
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        tmp.replace(self._path)

    def get(self, key: str) -> dict[str, Any] | None:
        with _CACHE_LOCK:
            data = self._load()
            entry = data.get(key)
        return entry if isinstance(entry, dict) else None

    def put(self, key: str, value: dict[str, Any]) -> None:
        record = dict(value)
        record.setdefault(
            "stored_at", datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        with _CACHE_LOCK:
            data = self._load()
            data[key] = record
            self._save(data)

    def invalidate_all(self) -> int:
        with _CACHE_LOCK:
            data = self._load()
            n = len(data)
            self._save({})
        return n

    def size(self) -> int:
        with _CACHE_LOCK:
            return len(self._load())


_CACHE_STORE: CacheStore = JsonFileCacheStore(Path(settings.response_store_path))


def get_cache_store() -> CacheStore:
    return _CACHE_STORE


def set_cache_store(store: CacheStore) -> None:
    """Swap the active cache backend. Used by tests + by the production-time
    Redis adapter wiring."""
    global _CACHE_STORE
    _CACHE_STORE = store
    _cache_log.info("cache store swapped to %s", store.kind())


# ---- Public API (legacy-compatible signatures) ---------------------------

def cache_key_for(
    question: str,
    *,
    tenant_id: str | None = None,
    conversation_id: str | None = None,
) -> str:
    """Compose a deterministic cache key that embeds:
       - tenant_id (so different tenants never collide)
       - data_version (so a new upload makes ALL old keys unreachable)
       - conversation_id (so a follow-up like "and october" doesn't share
         a key with a different conversation's "and october")
       - normalized question text

    Each component is namespaced + colon-separated so a key collision
    requires sha256 collision AND identical version + tenant + conversation.
    """
    tenant = (tenant_id or "global").strip().lower()
    convo = (conversation_id or "default").strip()
    version = get_data_version()
    norm_q = (question or "").strip().lower()
    payload = f"{tenant}|{version}|{convo}|{norm_q}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_cached(key: str) -> dict[str, Any] | None:
    return _CACHE_STORE.get(key)


def put_cached(key: str, record: dict[str, Any]) -> None:
    """Store / replace a cache entry. `record` must be the full ResponseStored
    payload (query, sub_agent, sql, rows, final_answer, chart, ...)."""
    _CACHE_STORE.put(key, record)


def invalidate_all() -> int:
    """Clear the cache. Returns count of removed entries.

    Today's call sites (every upload, /cache/clear endpoint) still call this
    for forward compat — but with version-stamped keys the cache is
    self-invalidating, and `bump_data_version()` is the more idiomatic call.
    Both are safe to call together."""
    n = _CACHE_STORE.invalidate_all()
    _cache_log.info("cache invalidated (%d entries removed)", n)
    return n


def cache_size() -> int:
    return _CACHE_STORE.size()


# ===========================================================================
# 8b. CONVERSATION MEMORY — replaces module-level `_LAST_KIND`
# ===========================================================================
#
# `ConversationStore` keeps per-(user, conversation_id) state — currently
# just the last query kind (chat/data_query/missing_data/general_knowledge)
# so the coordinator's continuity check can read it without a global. The
# default in-memory impl is process-local with a TTL eviction; a future
# Redis-backed impl reads/writes the same shape to a hash key.
#
# Conversation state should NEVER be a module global — that's the bug we
# just fixed (`_LAST_KIND` leaking across users).

class ConversationStore:
    def get_last_kind(self, user_key: str) -> str | None: ...   # noqa: E704
    def set_last_kind(self, user_key: str, kind: str) -> None: ...   # noqa: E704
    def reset(self, user_key: str | None = None) -> None: ...   # noqa: E704
    def kind(self) -> str: ...   # noqa: E704


class InMemoryConversationStore(ConversationStore):
    """Process-local. Keys are usually `f"{user_id}:{conversation_id}"`.
    A 1k-entry LRU bound prevents leaks; TTL is implicit (eviction).
    Single-worker only — horizontal scale must move to a shared backend."""

    _MAX_ENTRIES = 1024

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._lock = Lock()

    def kind(self) -> str:
        return "in_memory"

    def get_last_kind(self, user_key: str) -> str | None:
        with self._lock:
            return self._data.get(user_key)

    def set_last_kind(self, user_key: str, kind: str) -> None:
        with self._lock:
            if len(self._data) >= self._MAX_ENTRIES and user_key not in self._data:
                # FIFO eviction; not strict LRU but bounded.
                first_key = next(iter(self._data))
                self._data.pop(first_key, None)
            self._data[user_key] = kind

    def reset(self, user_key: str | None = None) -> None:
        with self._lock:
            if user_key is None:
                self._data.clear()
            else:
                self._data.pop(user_key, None)


_CONVO_STORE: ConversationStore = InMemoryConversationStore()


def get_conversation_store() -> ConversationStore:
    return _CONVO_STORE


def set_conversation_store(store: ConversationStore) -> None:
    """Swap the active conversation backend. Tests use this to reset state
    between cases; production swaps in a Redis impl."""
    global _CONVO_STORE
    _CONVO_STORE = store
    _cache_log.info("conversation store swapped to %s", store.kind())


def make_conversation_key(user_id: str | None, conversation_id: str | None) -> str:
    """Compose the per-conversation key. Anonymous user falls through to a
    shared 'guest' bucket — that's still better than the previous global
    state because at least guest sessions don't bleed into authenticated
    sessions."""
    u = (user_id or "anon").strip()
    c = (conversation_id or "default").strip()
    return f"{u}:{c}"


# ===========================================================================
# 8c. BLOB STORE — local filesystem today, Supabase Storage tomorrow
# ===========================================================================
#
# Today the upload pipeline spools to a local tmp file. Production should
# move to Supabase Storage (or any S3-compatible). The `BlobStore` interface
# below is what those backends will implement; today's `LocalFsBlobStore`
# adapts the existing tmp-file behaviour so call sites that move over use
# the same API today and don't change once the swap happens.

class BlobStore:
    def write(self, key: str, data: bytes) -> str:
        """Persist `data` under `key`. Returns a URI/identifier (file path,
        S3 key, signed URL, etc.) the caller can pass back to `read`."""
        raise NotImplementedError

    def read(self, key: str) -> bytes: ...   # noqa: E704
    def delete(self, key: str) -> None: ...   # noqa: E704
    def kind(self) -> str: ...   # noqa: E704


class LocalFsBlobStore(BlobStore):
    """Adapts the existing `data/uploads/` directory to the BlobStore API.
    Production deployments should swap in `SupabaseStorageBlobStore` once
    SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY are set."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def kind(self) -> str:
        return "local_fs"

    def _path(self, key: str) -> Path:
        # Sanitize: forbid `..` traversal.
        safe = key.replace("..", "_").replace("\\", "/").lstrip("/")
        return self._root / safe

    def write(self, key: str, data: bytes) -> str:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
        return str(p)

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()


_BLOB_STORE: BlobStore = LocalFsBlobStore(Path(settings.financial_db_path).parent / "uploads")


def get_blob_store() -> BlobStore:
    return _BLOB_STORE


def set_blob_store(store: BlobStore) -> None:
    global _BLOB_STORE
    _BLOB_STORE = store
    _cache_log.info("blob store swapped to %s", store.kind())


# ===========================================================================
# 9. MEMORY — entity-resolution synonyms (backing store for EntityResolver)
# ===========================================================================

_synonyms_log = logging.getLogger("agentic_ai.memory.synonyms")

_SYNONYM_DEFAULTS: dict[str, list[str]] = {
    "swiggy":      ["swiggy ltd", "bundl technologies", "swiggy app"],
    "zomato":      ["zomato ltd", "zomato app"],
    "groceries":   ["grocery", "kirana", "fmcg"],
    "electronics": ["consumer electronics", "appliances"],
    "fashion":     ["apparel", "clothing", "lifestyle"],
}


def _synonyms_path() -> Path:
    return Path(settings.synonyms_path)


def load_synonyms() -> dict[str, list[str]]:
    p = _synonyms_path()
    if not p.exists():
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(_SYNONYM_DEFAULTS, indent=2), encoding="utf-8")
        except Exception:
            _synonyms_log.warning("could not write default synonyms file", exc_info=True)
        return dict(_SYNONYM_DEFAULTS)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        _synonyms_log.warning("synonyms.json unreadable; using defaults", exc_info=True)
        return dict(_SYNONYM_DEFAULTS)


def resolve_entities(question: str) -> list[dict]:
    """Return canonical entities matched in the question.

    Returns a list of dicts: {canonical, matched_aliases}.
    """
    if not question:
        return []
    q = question.lower()
    syns = load_synonyms()
    out: list[dict] = []
    for canonical, aliases in syns.items():
        hits = [a for a in [canonical, *aliases] if a.lower() in q]
        if hits:
            out.append({"canonical": canonical, "matched_aliases": hits})
    return out
