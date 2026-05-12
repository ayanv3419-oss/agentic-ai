"""JWT bearer-token issuance + verification for single-user MVP.

Claims:
    sub:    username
    iat:    unix ts
    exp:    unix ts

Signed HS256 with `settings.auth_token_secret`.
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
_LEEWAY_SECONDS = 30


@dataclass(frozen=True)
class Principal:
    """Verified-token shape. Single-user MVP — only carries username."""
    username: str
    expires_at: int


def encode_token(*, username: str, ttl_hours: int | None = None) -> tuple[str, int]:
    ttl = int(ttl_hours or settings.auth_token_ttl_hours)
    now = int(time.time())
    expiry = now + ttl * 3600
    payload: dict[str, Any] = {
        "sub": username,
        "iat": now,
        "exp": expiry,
    }
    token = _jwt.encode(payload, settings.auth_token_secret, algorithm=_ALGO)
    return token, expiry


def decode_token(token: str) -> Optional[Principal]:
    if not token:
        return None
    try:
        claims = _jwt.decode(
            token,
            settings.auth_token_secret,
            algorithms=[_ALGO],
            leeway=_LEEWAY_SECONDS,
            options={"require": ["sub", "exp"]},
        )
    except (_jwt.PyJWTError, ValueError) as e:
        _log.debug("jwt decode failed: %s", e)
        return None

    try:
        return Principal(
            username=str(claims["sub"]),
            expires_at=int(claims["exp"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None
