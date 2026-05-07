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
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

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
    max_upload_bytes: int = Field(default=1024 * 1024 * 1024, alias="MAX_UPLOAD_BYTES")  # 1 GB
    upload_chunk_bytes: int = Field(default=1024 * 1024, alias="UPLOAD_CHUNK_BYTES")  # 1 MB

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
# 8. RESPONSE CACHE — data/response_store.json (atomic JSON store)
# ===========================================================================

_CACHE_LOCK = Lock()
_cache_log = logging.getLogger("agentic_ai.cache")


def _cache_path() -> Path:
    return Path(settings.response_store_path)


def _cache_load() -> dict[str, Any]:
    p = _cache_path()
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        _cache_log.warning("response_store load failed; treating as empty", exc_info=True)
        return {}


def _cache_save(data: dict[str, Any]) -> None:
    p = _cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    tmp.replace(p)


def cache_key_for(question: str) -> str:
    """sha256 over the lowercased / stripped question. Deterministic + global."""
    norm = (question or "").strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def get_cached(key: str) -> dict[str, Any] | None:
    with _CACHE_LOCK:
        data = _cache_load()
        entry = data.get(key)
    return entry if isinstance(entry, dict) else None


def put_cached(key: str, record: dict[str, Any]) -> None:
    """Store / replace a cache entry. `record` must be the full ResponseStored
    payload (query, sub_agent, sql, rows, final_answer, chart, ...)."""
    record = dict(record)
    record.setdefault(
        "stored_at", datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    with _CACHE_LOCK:
        data = _cache_load()
        data[key] = record
        _cache_save(data)


def invalidate_all() -> int:
    """Clear the cache. Returns count of removed entries."""
    with _CACHE_LOCK:
        data = _cache_load()
        n = len(data)
        _cache_save({})
    _cache_log.info("cache invalidated (%d entries removed)", n)
    return n


def cache_size() -> int:
    with _CACHE_LOCK:
        data = _cache_load()
    return len(data)


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
