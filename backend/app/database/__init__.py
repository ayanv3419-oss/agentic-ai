"""Database adapter layer.

Today's call sites use the SQLite functions in `infrastructure.py`
directly (`get_connection`, `fetch_one`, `fetch_all`). Production should
swap to PostgreSQL when `DATABASE_URL` is set.

Strategy: this module owns the adapter abstraction; call sites continue
calling `infrastructure.fetch_all(sql, params)` etc., and that module
delegates to the right backend based on `engine_kind()`.

Public surface:
  - `engine_kind()`         — "sqlite" | "postgres" — for /health
  - `engine_status()`       — dict with backend + pool stats
  - `pg_pool()`             — lazy asyncpg pool when DATABASE_URL is set
  - `RepositoryError`       — raised on illegal queries (cross-tenant etc.)
  - Repositories            — tenant-safe accessors (UserRepo, etc.)

The Postgres adapter is a SHADOW path today — it only initializes when
DATABASE_URL is set. Tests + dev keep working against SQLite without any
config change. The migration recipe lives in `pg_adapter.py` docstring.
"""
from app.database.engine import engine_kind, engine_status
from app.database.pg_adapter import (
    PostgresUnavailableError,
    pg_pool,
    pg_close,
)
from app.database.repositories import (
    RepositoryError,
    AuditLogRepo,
    FeedbackRepo,
)

__all__ = [
    "AuditLogRepo",
    "FeedbackRepo",
    "PostgresUnavailableError",
    "RepositoryError",
    "engine_kind",
    "engine_status",
    "pg_close",
    "pg_pool",
]
