"""Dataset-relative time engine.

NEVER use the machine clock for analytics. Every relative-time concept
("today", "yesterday", "last 7 days", "this month", "previous month")
must anchor to the latest date in the uploaded data — scanned across the
sales/purchase tables AND every dynamic u_* table's date columns.

Public surface:
    get_dataset_current_date()        — ISO YYYY-MM-DD or None when no data
    resolve_dataset_date_tokens()     — dict of {dataset_*} substitution tokens
    apply_date_tokens(sql)            — substitute tokens in a SQL string
    resolve_relative_date_range(spec) — (start, end) for named windows
    resolve_last_n_days(n)            — convenience helper
    resolve_this_week()               — dataset-week of dataset_current_date
    resolve_this_month()              — calendar month containing dataset_current_date
    resolve_previous_period(spec)     — same length window immediately prior
    invalidate_cache()                — drop cached tokens (call after uploads)

The cache is keyed by (tenant_schema, data_version) — every upload bumps the
version, and each tenant resolves against its own schema, so one tenant's
"today" is never served to another.

All callers should use these helpers — never `datetime.now()` for analytics.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any

from app.infrastructure import (
    ALLOWED_TABLES,
    fetch_all,
    fetch_one,
    get_data_version,
    quoted,
)


_log = logging.getLogger("agentic_ai.time_engine")


# ===========================================================================
# Cache — keyed by (tenant_schema, data_version)
# ===========================================================================

_CACHE_LOCK = Lock()
# Per-tenant cache: tenant_schema -> (data_version, tokens, current_iso).
# Keyed by the SAME schema pg_connection() routes to (current_tenant_var via
# tenant_schema), so tenant A's "today" can never be served to tenant B.
_cache: dict[str, tuple[int, dict[str, str], str | None]] = {}


def _tenant_key() -> str:
    """Cache key = the schema the current request is bound to.

    Leaf-safe local import (time_engine must not import db_engine at module
    top). Uses tenant_schema(...) — the exact namespace _scan_max_date reads —
    so AUTH-off ("public") stays byte-identical to the old single-slot cache.
    """
    from app.db_engine import current_tenant_var, tenant_schema
    return tenant_schema(current_tenant_var.get())


def invalidate_cache() -> None:
    """Drop all cached dataset dates + tokens (every tenant). Safe to call any
    time; bump_data_version() in infrastructure.py already invalidates
    implicitly, but callers can also force a refresh. Each tenant lazily
    recomputes against its own schema on the next resolve."""
    with _CACHE_LOCK:
        _cache.clear()


# ===========================================================================
# Dataset-current-date scan
# ===========================================================================

async def get_dataset_current_date() -> str | None:
    """Return the latest valid YYYY-MM-DD across sales + purchase tables.

    Returns None when there is no uploaded data yet. Rows with malformed
    or NULL Date values are skipped.
    """
    tokens = await resolve_dataset_date_tokens()
    return tokens.get("dataset_today") or None


def _valid_iso(d: Any) -> bool:
    return (isinstance(d, str) and len(d) == 10
            and d[4] == "-" and d[7] == "-")


def _norm_max_date(value: Any) -> str | None:
    """Normalize a scanned MAX() value to a 'YYYY-MM-DD' string (or None).

    Postgres (asyncpg) returns date/datetime objects from real DATE/
    TIMESTAMP columns; SQLite returns text. Coerce both to an ISO date
    prefix so the downstream _valid_iso check works dialect-agnostically.
    """
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value)[:10]


async def _scan_max_date() -> str | None:
    """Live DB scan — never cache outside this module.

    Scans the canonical sales/purchase tables (the "Date" column) AND
    every dynamically-ingested u_* table (any column whose name contains
    'date'), so the dataset's "today" reflects the user's real uploaded
    data — not just the legacy system tables. Without the u_* scan,
    relative windows like "last 3 days" anchored to stale system-table
    dates instead of the actual uploaded data.
    """
    candidates: list[str] = []

    # 1. Canonical sales / purchase tables — fixed "Date" column.
    # Skip on Postgres: those tables don't exist there.
    from app.db_engine import is_postgres as _is_pg_for_legacy
    iter_tables = [] if _is_pg_for_legacy() else ALLOWED_TABLES
    for table in iter_tables:
        try:
            # MAX() works on date/timestamp AND text columns, so no LIKE
            # predicate (Postgres real DATE/TIMESTAMP columns can't use
            # LIKE: "operator does not exist: timestamp ~~ unknown").
            row = await fetch_one(
                f'SELECT MAX("Date") AS max_d FROM {quoted(table)} '
                f'WHERE "Date" IS NOT NULL'
            )
        except Exception:
            _log.warning("time_engine: scan of %s failed", table, exc_info=True)
            continue
        d = _norm_max_date(row.get("max_d") if row else None)
        if _valid_iso(d):
            candidates.append(d)

    # 2. Dynamic u_* tables — scan every date-like column.
    from app.db_engine import is_postgres as _is_pg
    try:
        if _is_pg():
            u_tables = await fetch_all(
                "SELECT table_name AS name FROM information_schema.tables "
                "WHERE table_schema=current_schema() "
                "AND table_name LIKE 'u\\_%' ESCAPE '\\'"
            )
        else:
            u_tables = await fetch_all(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'u\\_%' ESCAPE '\\'"
            )
    except Exception:
        _log.warning("time_engine: failed to list u_* tables", exc_info=True)
        u_tables = []
    for t in u_tables or []:
        name = (t.get("name") if isinstance(t, dict) else None) or ""
        if not name:
            continue
        try:
            if _is_pg():
                cols = await fetch_all(
                    "SELECT column_name AS name "
                    "FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name=?",
                    (name,),
                )
            else:
                cols = await fetch_all(f"PRAGMA table_info({quoted(name)})")
        except Exception:
            continue
        for c in cols or []:
            col = (c.get("name") if isinstance(c, dict) else None) or ""
            if "date" not in col.lower():
                continue
            try:
                row = await fetch_one(
                    f'SELECT MAX({quoted(col)}) AS max_d FROM {quoted(name)} '
                    f'WHERE {quoted(col)} IS NOT NULL'
                )
            except Exception:
                continue
            d = _norm_max_date(row.get("max_d") if row else None)
            if _valid_iso(d):
                candidates.append(d)

    if not candidates:
        return None
    return max(candidates)


# ===========================================================================
# Token computation
# ===========================================================================

def _parse_iso(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _iso(d: date) -> str:
    return d.isoformat()


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _month_end(d: date) -> date:
    if d.month == 12:
        nxt = d.replace(year=d.year + 1, month=1, day=1)
    else:
        nxt = d.replace(month=d.month + 1, day=1)
    return nxt - timedelta(days=1)


def _previous_month_bounds(d: date) -> tuple[date, date]:
    prev_end = _month_start(d) - timedelta(days=1)
    prev_start = _month_start(prev_end)
    return prev_start, prev_end


def _week_bounds(d: date) -> tuple[date, date]:
    """ISO week — Monday..Sunday containing `d`."""
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _compute_tokens(current_iso: str) -> dict[str, str]:
    """Build every substitution token from a single dataset-current date."""
    today = _parse_iso(current_iso)
    if today is None:
        return {}

    yesterday = today - timedelta(days=1)
    last_7_start = today - timedelta(days=6)     # 7-day inclusive window ending today
    last_30_start = today - timedelta(days=29)
    last_90_start = today - timedelta(days=89)

    month_start = _month_start(today)
    month_end   = _month_end(today)
    prev_month_start, prev_month_end = _previous_month_bounds(today)
    week_start, week_end = _week_bounds(today)
    prev_week_start = week_start - timedelta(days=7)
    prev_week_end   = week_start - timedelta(days=1)

    return {
        # primary
        "dataset_today":           _iso(today),
        "dataset_yesterday":       _iso(yesterday),
        "dataset_year":            f"{today.year:04d}",
        "dataset_month":           f"{today.year:04d}-{today.month:02d}",

        # last-N-days windows (inclusive of dataset_today)
        "dataset_last_7_start":    _iso(last_7_start),
        "dataset_last_30_start":   _iso(last_30_start),
        "dataset_last_90_start":   _iso(last_90_start),

        # current month
        "dataset_month_start":     _iso(month_start),
        "dataset_month_end":       _iso(month_end),

        # previous month
        "dataset_prev_month":      f"{prev_month_start.year:04d}-{prev_month_start.month:02d}",
        "dataset_prev_month_start": _iso(prev_month_start),
        "dataset_prev_month_end":  _iso(prev_month_end),

        # current ISO week
        "dataset_week_start":      _iso(week_start),
        "dataset_week_end":        _iso(week_end),

        # previous ISO week
        "dataset_prev_week_start": _iso(prev_week_start),
        "dataset_prev_week_end":   _iso(prev_week_end),
    }


# ===========================================================================
# Public API
# ===========================================================================

async def resolve_dataset_date_tokens() -> dict[str, str]:
    """Resolve every {dataset_*} token. Cached by (tenant_schema, data_version).

    Returns {} when no data is uploaded yet — callers should treat that as
    a no-op and avoid running time-windowed queries."""
    key = _tenant_key()
    version = get_data_version()
    with _CACHE_LOCK:
        hit = _cache.get(key)
        if hit is not None and hit[0] == version:
            return dict(hit[1])

    current = await _scan_max_date()
    if current is None:
        with _CACHE_LOCK:
            _cache[key] = (version, {}, None)
        return {}

    tokens = _compute_tokens(current)
    with _CACHE_LOCK:
        _cache[key] = (version, tokens, current)
    _log.info("time_engine: dataset_today=%s (tenant=%s data_version=%d)",
              current, key, version)
    return dict(tokens)


def apply_date_tokens(sql: str, tokens: dict[str, str]) -> str:
    """Substitute every `{token}` in `sql` with its value from `tokens`.

    Templates without any tokens are returned unchanged. If a token is
    referenced but not present in the dict, it's left as-is — the SQL will
    then likely fail at execution, which surfaces the bug clearly.
    """
    if not tokens or "{" not in sql:
        return sql
    out = sql
    for key, value in tokens.items():
        placeholder = "{" + key + "}"
        if placeholder in out:
            out = out.replace(placeholder, value)
    return out


# Named-window resolvers — return (start_iso, end_iso) inclusive.

async def resolve_last_n_days(n: int) -> tuple[str, str] | None:
    """The N most-recent dataset days, ending on dataset_today."""
    if n <= 0:
        return None
    tokens = await resolve_dataset_date_tokens()
    today = tokens.get("dataset_today")
    if not today:
        return None
    end = _parse_iso(today)
    start = end - timedelta(days=n - 1)
    return _iso(start), _iso(end)


async def resolve_this_week() -> tuple[str, str] | None:
    tokens = await resolve_dataset_date_tokens()
    if not tokens:
        return None
    return tokens["dataset_week_start"], tokens["dataset_week_end"]


async def resolve_this_month() -> tuple[str, str] | None:
    tokens = await resolve_dataset_date_tokens()
    if not tokens:
        return None
    return tokens["dataset_month_start"], tokens["dataset_month_end"]


async def resolve_previous_period(spec: str) -> tuple[str, str] | None:
    """For a named window, return the period immediately before it.

    Supported specs: 'month', 'week', 'last_7_days', 'last_30_days'.
    """
    tokens = await resolve_dataset_date_tokens()
    if not tokens:
        return None
    spec = (spec or "").lower().strip()
    if spec == "month":
        return tokens["dataset_prev_month_start"], tokens["dataset_prev_month_end"]
    if spec == "week":
        return tokens["dataset_prev_week_start"], tokens["dataset_prev_week_end"]
    if spec in ("last_7_days", "last_7"):
        today = _parse_iso(tokens["dataset_today"])
        prev_end = today - timedelta(days=7)
        prev_start = prev_end - timedelta(days=6)
        return _iso(prev_start), _iso(prev_end)
    if spec in ("last_30_days", "last_30"):
        today = _parse_iso(tokens["dataset_today"])
        prev_end = today - timedelta(days=30)
        prev_start = prev_end - timedelta(days=29)
        return _iso(prev_start), _iso(prev_end)
    return None


async def resolve_relative_date_range(spec: str) -> tuple[str, str] | None:
    """One-stop resolver for named windows. Returns (start, end) inclusive.

    Supported specs (case-insensitive, whitespace-stripped):
        today | yesterday | this_week | this_month | this_year
        last_7_days | last_30_days | last_90_days
        previous_month | previous_week | last_day
    """
    tokens = await resolve_dataset_date_tokens()
    if not tokens:
        return None
    s = (spec or "").lower().strip().replace(" ", "_").replace("-", "_")

    if s in ("today", "last_day", "dataset_today"):
        d = tokens["dataset_today"]
        return d, d
    if s == "yesterday":
        d = tokens["dataset_yesterday"]
        return d, d
    if s == "this_week":
        return tokens["dataset_week_start"], tokens["dataset_week_end"]
    if s == "this_month":
        return tokens["dataset_month_start"], tokens["dataset_month_end"]
    if s == "this_year":
        return f"{tokens['dataset_year']}-01-01", f"{tokens['dataset_year']}-12-31"
    if s in ("last_7_days", "last_7"):
        return await resolve_last_n_days(7)
    if s in ("last_30_days", "last_30"):
        return await resolve_last_n_days(30)
    if s in ("last_90_days", "last_90"):
        return await resolve_last_n_days(90)
    if s in ("previous_month", "prev_month", "last_month"):
        return tokens["dataset_prev_month_start"], tokens["dataset_prev_month_end"]
    if s in ("previous_week", "prev_week", "last_week"):
        return tokens["dataset_prev_week_start"], tokens["dataset_prev_week_end"]
    return None
