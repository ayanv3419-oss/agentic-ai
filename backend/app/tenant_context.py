"""Request-time identity / tenant resolution — the ``Principal`` dependency.

Multi-tenant SLICE 1. This module answers one question per request: *who is
making it?* It does NOT bind the DB connection, set ``app.current_tenant``, or
enforce Row-Level Security — that is a deliberately separate, later slice. Here
we only resolve identity.

Behaviour mirrors :func:`app.auth.require_auth`:
  * ``AUTH_ENABLED=false`` (the default) → a no-op. We hand back an implicit
    single-tenant ``Principal`` so callers that depend on it keep working and
    the app behaves EXACTLY as it does today.
  * ``AUTH_ENABLED=true`` → read ``Authorization: Bearer <token>``, validate it
    via :func:`app.identity.verify_token`, and 401 on anything missing/invalid.
"""
from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.infrastructure import settings
from app.identity import Principal, verify_token

# Documented sentinel for single-tenant / AUTH-off mode. This is NOT a secret
# and NOT a real tenant row — it is the stand-in identity the whole app already
# runs under today, surfaced so downstream code has something concrete to read.
DEFAULT_TENANT_ID = "public"


async def require_principal(request: Request) -> Principal:
    """FastAPI dependency resolving the calling :class:`Principal`.

    When ``AUTH_ENABLED`` is false this returns the implicit single-tenant
    principal and never inspects the request. When auth is enabled it requires a
    valid ``Authorization: Bearer <token>`` header (minted by
    :func:`app.identity.mint_token`) and raises ``HTTPException(401)`` otherwise.
    """
    if not settings.auth_enabled:
        principal = Principal(
            user_id=DEFAULT_TENANT_ID,
            tenant_id=DEFAULT_TENANT_ID,
            email="",
        )
        # SLICE 2d: scope the whole request to the principal's schema CENTRALLY
        # so every route (uploads included) auto-isolates. For "public" this
        # leaves the default search_path untouched → byte-for-byte unchanged.
        set_query_tenant(principal.tenant_id)
        return principal

    auth_hdr = request.headers.get("Authorization") or ""
    if not auth_hdr.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
            headers={"WWW-Authenticate": 'Bearer realm="agentic-ai"'},
        )
    token = auth_hdr[7:].strip()
    principal = verify_token(token)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    # SLICE 2d: scope the whole request to the authenticated tenant's schema
    # CENTRALLY so every route (uploads included) auto-isolates. For an operator
    # account inheriting "public" this is a no-op (default search_path).
    set_query_tenant(principal.tenant_id)
    return principal


def set_query_tenant(tenant_id: str) -> None:
    """Bind the active tenant for the DATA-QUERY path (``u_*`` reads).

    Multi-tenant SLICE 2c. ``require_principal`` no longer drives the
    ``search_path`` contextvar — identity resolution and data-path schema
    isolation are deliberately decoupled. Call this explicitly at the top of a
    code path that issues unqualified ``u_*`` queries so ``pg_connection()``
    points ``search_path`` at the tenant's OWN schema. First-party tables
    (``conversations`` / ``uploads``) stay in ``public`` and isolate via
    ``WHERE tenant_id = ?`` instead, so they intentionally do NOT call this.

    ``tenant_id`` falls back to ``"public"`` (``DEFAULT_TENANT_ID``) so the
    AUTH-off / single-tenant path keeps the default ``public`` search_path —
    byte-for-byte today's behavior. The ``current_tenant_var`` import is local
    so this module never imports ``db_engine`` at module top (db_engine is a
    leaf; that keeps the import graph acyclic)."""
    from app.db_engine import current_tenant_var

    current_tenant_var.set(tenant_id or "public")
