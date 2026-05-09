"""Third-party service integrations.

Each integration is one small module with:
  - lazy initialization (no work at import time)
  - status helper that returns sanitized output for /health (no secrets)
  - graceful fallback when the integration is unconfigured

Today: Supabase (Storage + Auth + REST via service-role key, backend-only).
"""
from app.integrations.supabase import (
    SupabaseUnavailableError,
    get_supabase,
    supabase_close,
    supabase_status,
)

__all__ = [
    "SupabaseUnavailableError",
    "get_supabase",
    "supabase_close",
    "supabase_status",
]
