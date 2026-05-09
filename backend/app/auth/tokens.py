"""JWT bearer-token issuance + verification.

The token claims:

    {
      "sub":    "<user_id>",        # Subject = primary key
      "email":  "<email>",
      "tnt":    "<tenant_id>",      # Multi-tenant scope
      "wks":    "<workspace_id>",
      "role":   "owner|admin|viewer",
      "iat":    <unix_ts>,          # Issued at
      "exp":    <unix_ts>,          # Expiry
    }

Signed HS256 with `settings.auth_token_secret`. Rotating the secret
invalidates every outstanding token at once.

`Principal` is the verified-token shape we pass through FastAPI deps —
call sites that need (user_id, tenant_id, role) consume this dataclass
without ever touching jwt directly.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import jwt as _jwt

from app.infrastructure import settings


_log = logging.getLogger("agentic_ai.auth.tokens")

_ALGO = "HS256"
_LEEWAY_SECONDS = 30  # tolerate small clock skew between issuer + verifier


@dataclass(frozen=True)
class Principal:
    """Verified-token shape. Immutable. Carries everything downstream
    code needs to scope a request: who/where/role."""
    user_id: str
    email: str
    tenant_id: str
    workspace_id: str
    role: str
    expires_at: int   # unix ts

    @property
    def is_admin(self) -> bool:
        return self.role in ("owner", "admin")

    @property
    def is_viewer(self) -> bool:
        return self.role == "viewer"


def encode_token(
    *,
    user_id: str,
    email: str,
    tenant_id: str,
    workspace_id: str,
    role: str,
    ttl_hours: int | None = None,
) -> tuple[str, int]:
    """Sign + return (token, expiry_unix_ts)."""
    ttl = int(ttl_hours or settings.auth_token_ttl_hours)
    now = int(time.time())
    expiry = now + ttl * 3600
    payload: dict[str, Any] = {
        "sub":   user_id,
        "email": email,
        "tnt":   tenant_id,
        "wks":   workspace_id,
        "role":  role,
        "iat":   now,
        "exp":   expiry,
    }
    token = _jwt.encode(payload, settings.auth_token_secret, algorithm=_ALGO)
    return token, expiry


def decode_token(token: str) -> Optional[Principal]:
    """Verify + decode. Returns None on ANY failure (bad signature,
    expired, malformed). Never raises — single point where verification
    can fail-closed."""
    if not token:
        return None
    try:
        claims = _jwt.decode(
            token,
            settings.auth_token_secret,
            algorithms=[_ALGO],
            leeway=_LEEWAY_SECONDS,
            options={"require": ["sub", "tnt", "exp"]},
        )
    except (_jwt.PyJWTError, ValueError) as e:
        _log.debug("jwt decode failed: %s", e)
        return None

    try:
        return Principal(
            user_id=str(claims["sub"]),
            email=str(claims.get("email") or ""),
            tenant_id=str(claims["tnt"]),
            workspace_id=str(claims.get("wks") or ""),
            role=str(claims.get("role") or "viewer"),
            expires_at=int(claims["exp"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    """Pull the token out of an `Authorization: Bearer <tok>` header."""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None
