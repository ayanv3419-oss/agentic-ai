"""Database adapter layer — SQLite only (single-user MVP)."""
from app.database.engine import engine_kind, engine_status

__all__ = ["engine_kind", "engine_status"]
