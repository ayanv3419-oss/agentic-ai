"""Engine status — SQLite default, Postgres when DATABASE_URL is set."""
from __future__ import annotations

from typing import Any

from app.db_engine import engine_kind, is_postgres
from app.infrastructure import settings


def engine_status() -> dict[str, Any]:
    if is_postgres():
        # Don't expose the password — just confirm the engine + host.
        from urllib.parse import urlparse
        u = urlparse(settings.database_url or "")
        return {
            "kind": "postgres",
            "host": u.hostname or "",
            "database": (u.path or "/").lstrip("/"),
        }
    return {
        "kind": "sqlite",
        "path": settings.financial_db_path,
    }


__all__ = ["engine_kind", "engine_status"]
