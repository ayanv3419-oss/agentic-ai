"""Engine selection — reports which database backend is currently active.

The selection itself is a pure function of `settings.database_url`:
  - empty   → "sqlite"
  - non-empty → "postgres"

`engine_status()` is surfaced on `/health` so ops can verify which
backend is wired without poking at code.
"""
from __future__ import annotations

from typing import Any

from app.infrastructure import settings


def engine_kind() -> str:
    """Returns 'postgres' when DATABASE_URL is set, else 'sqlite'.
    Pure function — never errors."""
    url = (settings.database_url or "").strip()
    if not url:
        return "sqlite"
    # Defensive: accept postgres:// and postgresql:// (asyncpg is fine
    # with both since 0.29).
    return "postgres"


def engine_status() -> dict[str, Any]:
    """Cheap inspection for /health. Doesn't open a connection — only
    reports config."""
    kind = engine_kind()
    if kind == "sqlite":
        return {
            "kind": "sqlite",
            "path": settings.financial_db_path,
        }
    # Mask the password in DATABASE_URL before echoing.
    raw = settings.database_url or ""
    masked = raw
    try:
        # postgres://user:pass@host:port/db → mask 'pass'
        if "://" in raw and "@" in raw:
            scheme, rest = raw.split("://", 1)
            creds, hostpart = rest.split("@", 1)
            if ":" in creds:
                user, _ = creds.split(":", 1)
                masked = f"{scheme}://{user}:****@{hostpart}"
    except Exception:
        masked = "<masked>"
    return {
        "kind":  "postgres",
        "url":   masked,
        # `pool_open` is filled in by pg_adapter.pg_status() if/when the
        # pool has been initialized. Keep this status function side-effect
        # free.
    }
