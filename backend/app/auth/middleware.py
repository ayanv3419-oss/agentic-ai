"""FastAPI auth dependencies — single-user MVP."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import Header, HTTPException

from app.auth.tokens import Principal, decode_token, extract_bearer_token
from app.infrastructure import envelope


_log = logging.getLogger("agentic_ai.auth.middleware")


async def require_principal(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> Principal:
    """Raises 401 on missing / malformed / expired token."""
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
    principal = decode_token(token)
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


def try_principal(authorization: Optional[str]) -> Optional[Principal]:
    """Non-raising variant for /auth/me."""
    return decode_token(extract_bearer_token(authorization))
