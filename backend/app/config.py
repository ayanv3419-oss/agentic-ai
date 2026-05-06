"""Centralized settings — loaded from environment / .env.

DB and cache paths resolve to absolute paths against the project root, so
the cwd of the process can never split data into parallel directories.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
