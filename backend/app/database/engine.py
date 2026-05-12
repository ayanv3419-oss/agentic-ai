"""Engine status — single-user MVP uses SQLite only."""
from __future__ import annotations

from typing import Any

from app.infrastructure import settings


def engine_kind() -> str:
    return "sqlite"


def engine_status() -> dict[str, Any]:
    return {
        "kind": "sqlite",
        "path": settings.financial_db_path,
    }
