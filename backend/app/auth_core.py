"""Username + password admin auth with HMAC-signed bearer tokens.

Token format:  base64url(payload).hex(hmac_sha256(secret, payload))
where payload = "<username>:<expiry_unix_ts>"

This is intentionally minimal (single admin, no DB-backed sessions). The
secret comes from `AUTH_TOKEN_SECRET`; the credentials from `ADMIN_USERNAME`
and `ADMIN_PASSWORD`. With the secret + creds in env vars only — never in
source control — restarting the server with a new secret invalidates every
token at once.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import Header, HTTPException

from app.config import settings
from app.errors import envelope

log = logging.getLogger("agentic_ai.auth_core")


# --- token gen / verify ----------------------------------------------------

def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))


def _sign(payload: str) -> str:
    return hmac.new(
        settings.auth_token_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def create_token(username: str) -> tuple[str, str]:
    """Return (token, ISO expires_at). TTL comes from settings."""
    expiry = int(time.time()) + settings.auth_token_ttl_hours * 3600
    payload = f"{username}:{expiry}"
    sig = _sign(payload)
    token = f"{_b64url_encode(payload.encode('utf-8'))}.{sig}"
    expires_at = datetime.fromtimestamp(expiry, tz=timezone.utc).isoformat(
        timespec="seconds"
    )
    return token, expires_at


def verify_token(token: Optional[str]) -> Optional[str]:
    """Return username on success, None on any failure (invalid format,
    bad signature, expired, etc.)."""
    if not token or "." not in token:
        return None
    try:
        payload_b64, sig = token.split(".", 1)
        payload = _b64url_decode(payload_b64).decode("utf-8")
        username, expiry_s = payload.split(":")
        expiry = int(expiry_s)
    except Exception:
        return None
    expected_sig = _sign(payload)
    if not hmac.compare_digest(sig, expected_sig):
        return None
    if time.time() > expiry:
        return None
    return username


# --- credential check ------------------------------------------------------

def credentials_match(username: str, password: str) -> bool:
    """Constant-time compare against the admin credentials.
    Fails-closed when env vars aren't configured."""
    expected_u = settings.admin_username or ""
    expected_p = settings.admin_password or ""
    if not expected_u or not expected_p:
        return False
    u_ok = hmac.compare_digest(
        (username or "").encode("utf-8"), expected_u.encode("utf-8")
    )
    p_ok = hmac.compare_digest(
        (password or "").encode("utf-8"), expected_p.encode("utf-8")
    )
    return u_ok and p_ok


# --- FastAPI dependency ----------------------------------------------------

def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


async def require_auth(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> str:
    """FastAPI dependency. Raises 401 with the standard envelope if the
    Authorization header is missing / malformed / expired.

    Returns the username on success — handlers that want it can take a
    parameter `user: str = Depends(require_auth)` even when the route uses
    the `dependencies=[Depends(require_auth)]` form."""
    token = _extract_bearer(authorization)
    if not token:
        raise HTTPException(
            status_code=401,
            detail=envelope(
                "Authentication required",
                detail="Send `Authorization: Bearer <token>` after logging in via /auth/login.",
                kind="auth",
            ),
        )
    username = verify_token(token)
    if not username:
        raise HTTPException(
            status_code=401,
            detail=envelope(
                "Invalid or expired token",
                detail="Log in again to obtain a new token.",
                kind="auth",
            ),
        )
    return username


def try_auth(authorization: Optional[str]) -> Optional[str]:
    """Non-raising variant for routes that want to know "is the caller
    authenticated?" without rejecting unauthenticated requests."""
    return verify_token(_extract_bearer(authorization))
