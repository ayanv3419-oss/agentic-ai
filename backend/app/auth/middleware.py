"""FastAPI dependencies for auth + RBAC.

`require_principal` is the standard "this route requires a logged-in user"
dependency — call sites attach it via `Depends(require_principal)`. It
returns a `Principal` so the route handler can read user_id / tenant_id /
workspace_id / role without parsing headers.

`require_role(*roles)` is a factory that wraps require_principal and adds
a role check on top: `Depends(require_role("owner", "admin"))`.

`try_principal` is the non-raising variant for routes that want to know
"is the caller authenticated?" without rejecting unauthenticated requests
(e.g. `/auth/me` returns `authenticated=false` rather than 401).

For backward compatibility with the legacy single-admin HMAC token, this
middleware ALSO accepts the old token shape and synthesises a `Principal`
with `tenant_id="legacy_admin"`. That lets the existing test suites keep
working while a real user table is being populated. Once every test has
moved to JWT, the legacy path can be deleted.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, Header, HTTPException

from app.auth.tokens import Principal, decode_token, extract_bearer_token
from app.infrastructure import envelope, settings


_log = logging.getLogger("agentic_ai.auth.middleware")


# --- Legacy HMAC compatibility shim --------------------------------------

def _try_legacy_hmac(token: str) -> Optional[Principal]:
    """Accept the OLD HMAC bearer token (from /auth/login pre-JWT) and
    synthesise a Principal so existing single-admin deployments + tests
    keep working without re-registration. Returns None on any failure."""
    if not token or "." not in token:
        return None
    try:
        import base64
        import hashlib
        import hmac
        import time
        b64, sig = token.split(".", 1)
        pad = "=" * (-len(b64) % 4)
        payload = base64.urlsafe_b64decode((b64 + pad).encode("ascii")).decode("utf-8")
        username, expiry_s = payload.split(":")
        expiry = int(expiry_s)
        expected = hmac.new(
            settings.auth_token_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if time.time() > expiry:
            return None
    except Exception:
        return None
    # Map the legacy admin to a synthetic principal. tenant_id is fixed
    # so legacy-admin queries land in their own tenant slice — never
    # mixed with multi-user tenants.
    return Principal(
        user_id=f"legacy::{username}",
        email=f"{username}@legacy.local",
        tenant_id="legacy_admin",
        workspace_id="legacy_admin",
        role="owner",
        expires_at=expiry,
    )


def _verify_any(token: str | None) -> Optional[Principal]:
    if not token:
        return None
    p = decode_token(token)
    if p is not None:
        return p
    return _try_legacy_hmac(token)


# --- FastAPI dependencies -----------------------------------------------

async def require_principal(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> Principal:
    """Standard auth dependency. Raises 401 with the JSON envelope on
    missing / malformed / expired token."""
    token = extract_bearer_token(authorization)
    if not token:
        raise HTTPException(
            status_code=401,
            detail=envelope(
                "Authentication required",
                detail="Send `Authorization: Bearer <token>` after logging in via /auth/login.",
                kind="auth",
            ),
        )
    principal = _verify_any(token)
    if principal is None:
        raise HTTPException(
            status_code=401,
            detail=envelope(
                "Invalid or expired token",
                detail="Log in again to obtain a new token.",
                kind="auth",
            ),
        )
    return principal


def require_role(*allowed: str):
    """Dependency factory — `Depends(require_role('owner', 'admin'))`.
    Returns the Principal so handlers can also read user_id / tenant_id
    without an extra dep."""
    allowed_set = frozenset(allowed)

    async def _dep(principal: Principal = Depends(require_principal)) -> Principal:
        if principal.role not in allowed_set:
            raise HTTPException(
                status_code=403,
                detail=envelope(
                    "Forbidden",
                    detail=(
                        f"Role {principal.role!r} cannot perform this action; "
                        f"required: {sorted(allowed_set)}"
                    ),
                    kind="auth",
                ),
            )
        return principal

    return _dep


def try_principal(authorization: Optional[str]) -> Optional[Principal]:
    """Non-raising variant — used by /auth/me and any route that wants
    "are you signed in?" without forcing a 401."""
    return _verify_any(extract_bearer_token(authorization))
