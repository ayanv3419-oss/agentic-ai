"""Supabase client — backend-only.

Why httpx-direct instead of `supabase-py`:
  - We only need a small surface today (storage bucket list, future REST
    + storage uploads). The official client pulls in postgrest, gotrue,
    realtime, and storage3 — ~6 MB of deps for things we don't use yet.
  - httpx is already a dependency. Direct REST calls give us tighter
    control over timeouts, masking, and the Sentry instrumentation
    surface than wrapping a sync client.

When use cases grow (auth admin, realtime subscriptions, edge functions),
swap `SupabaseClient` for the official client behind the same
`get_supabase()` accessor — the call sites won't change.

SECURITY
--------
The service-role key bypasses Row Level Security. It MUST stay on the
backend. This module:
  - Never logs the full key (always masked via `_mask_key`)
  - Never returns the key from any public function
  - Never echoes it to /health, /auth/me, or any error response
  - Skips request-body capture in Sentry for Supabase requests (handled
    by the existing scrubber via the `Authorization` + `apikey` header
    check in monitoring/sentry_config.py)

If the key leaks, rotate it in Supabase: Project Settings → API →
"Reset service_role key".
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from app.infrastructure import settings


_log = logging.getLogger("agentic_ai.integrations.supabase")


class SupabaseUnavailableError(RuntimeError):
    """Raised when SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are unset OR
    the client failed to initialize. Catch at call sites that have a
    fallback (e.g. LocalFsBlobStore)."""


_CLIENT: Optional["SupabaseClient"] = None
_CLIENT_LOCK = asyncio.Lock()
_INIT_FAILED: bool = False


def _mask_key(key: str | None) -> str:
    """Mask a service-role key for safe logging. Shows enough to identify
    which key (first 12 chars) without revealing the secret. Returns
    `<unset>` for empty input."""
    if not key:
        return "<unset>"
    if len(key) < 24:
        return "<short_key_masked>"
    return f"{key[:12]}...***...{key[-4:]}"


def _mask_url(url: str | None) -> str:
    """Project URL is not secret but we keep mask helpers symmetrical so
    /health can use a single sanitizer."""
    return url or "<unset>"


class SupabaseClient:
    """Async wrapper around the Supabase REST + Storage HTTP API.

    Connections via `httpx.AsyncClient` so the pool is reused across
    requests. The auth headers are applied by default; per-call
    headers can override.
    """

    def __init__(self, url: str, service_role_key: str) -> None:
        if not url or not service_role_key:
            raise ValueError("SupabaseClient requires non-empty url + key")
        self._url = url.rstrip("/")
        self._key = service_role_key
        # Default headers — Supabase requires both `apikey` and `Authorization`
        # for service-role operations.
        self._http = httpx.AsyncClient(
            base_url=self._url,
            headers={
                "apikey":         service_role_key,
                "Authorization":  f"Bearer {service_role_key}",
                "Content-Type":   "application/json",
                # Identify ourselves to the Supabase logs without exposing
                # internal version info.
                "User-Agent":     "agentic-ai-backend/1.0",
            },
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0),
        )

    # --- Lifecycle -------------------------------------------------------

    async def close(self) -> None:
        try:
            await self._http.aclose()
        except Exception:
            _log.exception("supabase http close failed")

    # --- Health probe ---------------------------------------------------

    async def ping(self) -> dict[str, Any]:
        """Cheap connectivity + auth check. List buckets via the storage
        admin endpoint — works only with the service-role key, so a 200
        response proves both URL and key are valid.

        Returns a sanitized dict (bucket count + status) — never the
        bucket contents, since they may include internal names. Raises
        SupabaseUnavailableError on any failure."""
        try:
            r = await self._http.get("/storage/v1/bucket")
        except Exception as e:
            raise SupabaseUnavailableError(
                f"supabase ping network error: {type(e).__name__}: {e}"
            )
        if r.status_code != 200:
            # Don't surface response body — it may leak project structure.
            raise SupabaseUnavailableError(
                f"supabase ping HTTP {r.status_code}"
            )
        try:
            buckets = r.json()
        except Exception:
            raise SupabaseUnavailableError("supabase ping: response not JSON")
        if not isinstance(buckets, list):
            raise SupabaseUnavailableError("supabase ping: unexpected payload")
        # Surface ONLY the count + ID list. Bucket IDs are tenant-neutral
        # config (e.g. "agentic-uploads"), safe to expose.
        return {
            "buckets":      len(buckets),
            "bucket_ids":   [b.get("id") for b in buckets if isinstance(b, dict)],
        }

    # --- Storage helpers (small, expand as call sites need them) -------

    async def list_buckets(self) -> list[dict[str, Any]]:
        """Full bucket list — for admin tooling. Returns `[]` on any
        error so callers don't need try/except for listing. Errors are
        logged with the key MASKED."""
        try:
            r = await self._http.get("/storage/v1/bucket")
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            _log.warning("supabase list_buckets failed: %s: %s",
                         type(e).__name__, e)
            return []


# ----- Public accessors --------------------------------------------------

async def get_supabase() -> SupabaseClient:
    """Lazy singleton. Raises SupabaseUnavailableError when the env vars
    are unset OR initialization failed. Idempotent — repeat calls return
    the same client."""
    global _CLIENT, _INIT_FAILED
    url = (settings.supabase_url or "").strip()
    key = (settings.supabase_service_role_key or "").strip()
    if not url or not key:
        raise SupabaseUnavailableError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must both be set"
        )
    if _INIT_FAILED:
        raise SupabaseUnavailableError(
            "supabase init failed previously; call supabase_close() to retry"
        )
    if _CLIENT is not None:
        return _CLIENT
    async with _CLIENT_LOCK:
        if _CLIENT is not None:
            return _CLIENT
        try:
            client = SupabaseClient(url=url, service_role_key=key)
        except Exception as e:
            _INIT_FAILED = True
            _log.exception("supabase client construction failed: %s", e)
            raise SupabaseUnavailableError(f"client construction failed: {e}")
        _CLIENT = client
        # Note: we log the URL + masked key, never the full key.
        _log.info("supabase client initialized url=%s key=%s",
                  _mask_url(url), _mask_key(key))
        return _CLIENT


async def supabase_close() -> None:
    """Close the underlying httpx pool + reset the circuit breaker.
    Used on shutdown + by tests."""
    global _CLIENT, _INIT_FAILED
    async with _CLIENT_LOCK:
        if _CLIENT is not None:
            await _CLIENT.close()
        _CLIENT = None
        _INIT_FAILED = False


def supabase_status() -> dict[str, Any]:
    """Cheap inspection for /health. NEVER includes the service-role key.

    Returns:
      - configured:      both env vars set
      - client_open:     a connection has been established
      - init_failed:     last attempt errored
      - url:             project URL (not secret; safe to surface)
      - key_fingerprint: first 12 chars of the key (for ops to verify
                         which key is wired without exposing it)
    """
    url = (settings.supabase_url or "").strip()
    key = (settings.supabase_service_role_key or "").strip()
    return {
        "configured":      bool(url and key),
        "client_open":     _CLIENT is not None,
        "init_failed":     _INIT_FAILED,
        "url":             _mask_url(url) if url else "<unset>",
        "key_fingerprint": _mask_key(key),
    }
