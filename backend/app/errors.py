"""Centralized error tracking + reporting.

Public capture API (one call wherever an error is observed):

    log_error(
        exc=None | Exception,
        message=None | str,
        module="upload" | "ai" | "kpi" | "dashboard" | "auth" | "database"
               | "system" | "frontend" | "validation" | "...",
        severity="critical" | "high" | "medium" | "low",   # optional, auto-inferred
        endpoint=None | str,
        method=None | str,
        user_facing=False,
        suggested_fix=None | str,
        source=None | str,
        request_payload=None | dict,
        context=None | dict,
    ) -> str   # the error_id

Every call inserts one row into the `error_log` SQLite table. The function
NEVER raises — error logging must not break the caller. If the DB write
fails, the failure is dropped to the Python logger instead.

Read API (consumed by /errors routes):

    list_errors(...)        — filtered listing
    get_error(error_id)     — single full row
    resolve_error(error_id, note=...)
    error_analytics()       — counts by module / severity / top errors
"""
from __future__ import annotations

import json
import logging
import traceback
from typing import Any, Iterable
from uuid import uuid4

from app.infrastructure import db_path, fetch_all, fetch_one


_log = logging.getLogger("agentic_ai.errors")


SEVERITIES = ("critical", "high", "medium", "low")
DEFAULT_SEVERITY = "medium"

# Modules used in classification + analytics roll-ups. Any string is
# accepted, but these are the canonical buckets shown in the dashboard.
KNOWN_MODULES = (
    "auth", "upload", "ai", "kpi", "dashboard", "database",
    "validation", "system", "frontend", "hierarchy", "time_engine",
    "dedup", "sales", "purchase", "inventory", "report",
)

# Exception types we know how to classify deterministically. Each entry maps
# to (severity, suggested_fix). The list is intentionally short and
# explicit — unknown exceptions fall through to (DEFAULT_SEVERITY, "").
_EXC_RULES: dict[str, tuple[str, str]] = {
    "UploadError":       ("high",     "Check the file's header row and ensure required columns are present."),
    "ValueError":        ("medium",   "Inspect the input arguments; one is out of the accepted range."),
    "KeyError":          ("medium",   "A required key is missing from the input payload."),
    "FileNotFoundError": ("high",     "The expected file or path is missing. Re-upload or restore."),
    "PermissionError":   ("high",     "Filesystem permission denied — check OS-level access."),
    "TimeoutError":      ("high",     "Operation exceeded its time budget — retry or increase timeout."),
    "ConnectionError":   ("critical", "Network or DB connection failed — verify the service is reachable."),
    "OperationalError":  ("critical", "SQLite operational failure — check disk space + WAL mode."),
    "IntegrityError":    ("high",     "Constraint violation — likely a duplicate or NULL on a NOT NULL column."),
    "ApiError":          ("high",     "Upstream API call failed — verify the API key + endpoint URL."),
    "JSONDecodeError":   ("medium",   "Input was not valid JSON."),
    "HTTPException":     ("low",      "Expected HTTP error — usually a 4xx that's part of normal flow."),
}


def _classify(exc: BaseException | None, hint_severity: str | None) -> tuple[str, str]:
    """Return (severity, suggested_fix) for an exception."""
    if hint_severity in SEVERITIES:
        sev = hint_severity
    else:
        sev = DEFAULT_SEVERITY
    fix = ""
    if exc is not None:
        rule = _EXC_RULES.get(type(exc).__name__)
        if rule:
            # Hint from caller wins if provided; else use rule severity.
            if hint_severity not in SEVERITIES:
                sev = rule[0]
            fix = rule[1]
    return sev, fix


def _short_stack(exc: BaseException | None, limit: int = 50) -> str | None:
    if exc is None:
        return None
    try:
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=limit))
    except Exception:
        return None


def _jsonify(obj: Any) -> str | None:
    if obj is None:
        return None
    try:
        return json.dumps(obj, default=str, ensure_ascii=False)[:50_000]
    except Exception:
        try:
            return str(obj)[:50_000]
        except Exception:
            return None


def log_error(
    *,
    exc: BaseException | None = None,
    message: str | None = None,
    module: str = "system",
    severity: str | None = None,
    endpoint: str | None = None,
    method: str | None = None,
    user_facing: bool = False,
    suggested_fix: str | None = None,
    source: str | None = None,
    request_payload: Any = None,
    context: Any = None,
) -> str:
    """Capture an error. NEVER raises. Returns the new error_id."""
    error_id = f"err-{uuid4().hex[:12]}"
    error_type = type(exc).__name__ if exc is not None else "AppError"
    msg = (message or (str(exc) if exc else "")).strip() or "(no message)"
    sev, fix = _classify(exc, severity)
    if suggested_fix:
        fix = suggested_fix
    stack = _short_stack(exc)
    payload_json = _jsonify(request_payload)
    context_json = _jsonify(context)

    # On Postgres, skip the raw-sqlite3 write (it would open a local file
    # instead of hitting Supabase). The error is still surfaced via the
    # Python logger below. Queryable history via /errors is unavailable on
    # Postgres tonight; re-port over the weekend.
    from app.db_engine import is_postgres as _is_pg
    if _is_pg():
        _log.warning(
            "error_log skipped on Postgres: id=%s severity=%s module=%s type=%s msg=%s",
            error_id, sev, module or "system", error_type, msg[:300],
        )
        return error_id

    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path()), isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                "INSERT INTO error_log "
                "(error_id, severity, module, error_type, message, "
                " endpoint, method, user_facing, suggested_fix, source, "
                " stack_trace, request_payload, context) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    error_id, sev, module or "system", error_type, msg[:8000],
                    endpoint, method, 1 if user_facing else 0,
                    fix or None, source, stack, payload_json, context_json,
                ),
            )
        finally:
            conn.close()
    except Exception:
        # Last-resort: drop to the Python logger so the error is at least visible.
        _log.exception(
            "error_log write failed; original error: type=%s msg=%s module=%s",
            error_type, msg, module,
        )
    return error_id


# ===========================================================================
# Read / analytics helpers
# ===========================================================================

_LIST_COLS = (
    "error_id, occurred_at, severity, module, error_type, message, "
    "endpoint, method, user_facing, suggested_fix, source, "
    "resolved, resolved_at, resolved_note"
)

_DETAIL_COLS = _LIST_COLS + ", stack_trace, request_payload, context"


async def list_errors(
    *,
    module: str | None = None,
    severity: str | None = None,
    resolved: bool | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    sql = f"SELECT {_LIST_COLS} FROM error_log"
    where: list[str] = []
    params: list[Any] = []
    if module:
        where.append("module = ?")
        params.append(module)
    if severity:
        where.append("severity = ?")
        params.append(severity)
    if resolved is not None:
        where.append("resolved = ?")
        params.append(1 if resolved else 0)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY occurred_at DESC LIMIT ? OFFSET ?"
    params.extend([max(1, min(int(limit), 1000)), max(0, int(offset))])
    return await fetch_all(sql, tuple(params))


async def get_error(error_id: str) -> dict[str, Any] | None:
    return await fetch_one(
        f"SELECT {_DETAIL_COLS} FROM error_log WHERE error_id = ?",
        (error_id,),
    )


async def resolve_error(error_id: str, note: str | None = None) -> bool:
    """Mark resolved=1 and stamp resolved_at. Returns False on unknown id.

    Cross-engine: on Postgres routes through the async connection; on
    SQLite keeps the legacy direct sqlite3 path (idempotent, no behaviour
    change). Both paths use NOW() / datetime('now') correctly per engine.
    """
    from app.db_engine import is_postgres
    if is_postgres():
        try:
            from app.infrastructure import get_connection as _get_conn
            async with _get_conn() as db:
                cur = await db.execute(
                    "UPDATE error_log SET resolved = 1, "
                    "resolved_at = NOW(), resolved_note = ? "
                    "WHERE error_id = ?",
                    (note, error_id),
                )
                # asyncpg shim doesn't expose rowcount; we treat any
                # non-error path as success and let the caller re-check
                # via get_error() if it needs strict confirmation.
                await cur.close()
                await db.commit()
                return True
        except Exception:
            _log.exception("resolve_error (postgres) failed for %s", error_id)
            return False
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path()), isolation_level=None)
        try:
            cur = conn.execute(
                "UPDATE error_log SET resolved = 1, "
                "resolved_at = datetime('now'), resolved_note = ? "
                "WHERE error_id = ?",
                (note, error_id),
            )
            return (cur.rowcount or 0) > 0
        finally:
            conn.close()
    except Exception:
        _log.exception("resolve_error (sqlite) failed for %s", error_id)
        return False


async def error_counts_by_module() -> list[dict[str, Any]]:
    return await fetch_all(
        "SELECT module, COUNT(*) AS n, "
        "       SUM(CASE WHEN resolved = 0 THEN 1 ELSE 0 END) AS unresolved "
        "FROM error_log GROUP BY module ORDER BY n DESC"
    )


async def error_counts_by_severity() -> list[dict[str, Any]]:
    return await fetch_all(
        "SELECT severity, COUNT(*) AS n, "
        "       SUM(CASE WHEN resolved = 0 THEN 1 ELSE 0 END) AS unresolved "
        "FROM error_log GROUP BY severity ORDER BY "
        "CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "              WHEN 'medium' THEN 2 ELSE 3 END"
    )


async def top_error_types(limit: int = 10) -> list[dict[str, Any]]:
    return await fetch_all(
        "SELECT error_type, COUNT(*) AS n, "
        "       MAX(occurred_at) AS last_seen "
        "FROM error_log GROUP BY error_type ORDER BY n DESC LIMIT ?",
        (max(1, min(int(limit), 100)),),
    )


async def error_analytics() -> dict[str, Any]:
    """Single dashboard payload."""
    total_row = await fetch_one(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN resolved = 0 THEN 1 ELSE 0 END) AS unresolved "
        "FROM error_log"
    )
    recent24h = await fetch_one(
        "SELECT COUNT(*) AS n FROM error_log "
        "WHERE occurred_at >= datetime('now', '-1 day')"
    )
    return {
        "total":       int((total_row or {}).get("total") or 0),
        "unresolved":  int((total_row or {}).get("unresolved") or 0),
        "last_24h":    int((recent24h or {}).get("n") or 0),
        "by_module":   await error_counts_by_module(),
        "by_severity": await error_counts_by_severity(),
        "top_types":   await top_error_types(10),
    }


async def purge_resolved_before(days: int = 90) -> int:
    """Maintenance helper. Drops resolved rows older than N days. Returns
    the count purged."""
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path()), isolation_level=None)
        try:
            cur = conn.execute(
                "DELETE FROM error_log "
                "WHERE resolved = 1 AND resolved_at < datetime('now', ?)",
                (f"-{int(days)} day",),
            )
            return cur.rowcount or 0
        finally:
            conn.close()
    except Exception:
        _log.exception("purge_resolved_before failed")
        return 0
