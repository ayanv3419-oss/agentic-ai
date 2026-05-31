"""Auth helpers — HMAC-signed bearer tokens + a FastAPI dependency.

Design (matches the Phase-3 plan in CONTEXT.md):
  * Single admin user. Credentials come from ``ADMIN_USERNAME`` /
    ``ADMIN_PASSWORD`` env vars. There is no user table.
  * On ``POST /auth/login`` we mint a self-contained token
    ``<payload_b64>.<sig_b64>``  where ``payload = {"u": username,
    "iat": int, "exp": int}`` and ``sig = HMAC-SHA256(secret, payload_b64)``.
    No JWT library dependency — keeps the wheel slim.
  * ``require_auth`` is a FastAPI dependency that:
      - is a no-op when ``settings.auth_enabled`` is False (legacy mode)
      - reads ``Authorization: Bearer <token>`` and 401s on anything else.

Why HMAC and not just an opaque token: HMAC lets us verify on every
request without a server-side session store (stateless backend, scales
horizontally). The secret rotates by changing the env var, which
invalidates every existing token at once — clean panic-button.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any

from fastapi import HTTPException, Request, status

from app.infrastructure import settings

_log = logging.getLogger("agentic_ai.auth")


def _b64u_encode(b: bytes) -> str:
    """URL-safe base64 without padding — the JWT convention."""
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(payload_b64: str, secret: str) -> str:
    sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"),
                   hashlib.sha256).digest()
    return _b64u_encode(sig)


def mint_token(username: str) -> str:
    """Mint a fresh signed token for ``username``. TTL comes from
    ``AUTH_TOKEN_TTL_HOURS`` (default 168 = 7 days)."""
    now = int(time.time())
    ttl = max(1, settings.auth_token_ttl_hours) * 3600
    payload: dict[str, Any] = {"u": username, "iat": now, "exp": now + ttl}
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_b64 = _b64u_encode(payload_json.encode("utf-8"))
    sig_b64 = _sign(payload_b64, settings.auth_token_secret)
    return f"{payload_b64}.{sig_b64}"


def verify_token(token: str) -> str | None:
    """Return the username if the token is valid + unexpired, else None.

    Never raises — callers handle the None case with whatever HTTP status
    fits their context (401 vs 400 vs a silent guest mode).
    """
    if not token or "." not in token:
        return None
    try:
        payload_b64, sig_b64 = token.rsplit(".", 1)
    except ValueError:
        return None
    expected_sig = _sign(payload_b64, settings.auth_token_secret)
    # constant-time compare — even a millisecond timing difference leaks
    # the secret over enough samples.
    if not hmac.compare_digest(expected_sig, sig_b64):
        return None
    try:
        payload = json.loads(_b64u_decode(payload_b64).decode("utf-8"))
    except Exception:
        return None
    u = payload.get("u")
    exp = payload.get("exp")
    if not isinstance(u, str) or not isinstance(exp, (int, float)):
        return None
    if exp < time.time():
        return None
    return u


async def require_auth(request: Request) -> str | None:
    """FastAPI dependency. No-op when ``AUTH_ENABLED=false`` (returns
    None so callers that bind the result still work). When enabled,
    validates the ``Authorization: Bearer <token>`` header and 401s on
    anything else — invalid signature, expired token, missing header.
    """
    if not settings.auth_enabled:
        return None
    auth_hdr = request.headers.get("Authorization") or ""
    if not auth_hdr.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
            headers={"WWW-Authenticate": 'Bearer realm="agentic-ai"'},
        )
    token = auth_hdr[7:].strip()
    user = verify_token(token)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    return user


def check_admin_credentials(username: str, password: str) -> bool:
    """Validate against env vars. Case-insensitive on username, exact on
    password. Constant-time compare for both.

    No credentials are hardcoded: when ``ADMIN_USERNAME`` / ``ADMIN_PASSWORD``
    are unset there is nothing to match against, so this returns False. (Real
    per-user accounts live in the ``users`` table via ``app.identity`` — this
    legacy admin path is only active when both env vars are explicitly set.)
    """
    admin_u = settings.admin_username
    admin_p = settings.admin_password
    if not admin_u or not admin_p:
        return False
    u_ok = hmac.compare_digest(
        (username or "").strip().lower().encode("utf-8"),
        (admin_u or "").strip().lower().encode("utf-8"),
    )
    p_ok = hmac.compare_digest(
        (password or "").encode("utf-8"),
        (admin_p or "").encode("utf-8"),
    )
    return u_ok and p_ok
