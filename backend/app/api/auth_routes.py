"""Auth routes.

Real admin auth (bearer-token based) lives at:
    POST /auth/login   — exchange username/password for a token
    GET  /auth/me      — inspect current bearer token
    POST /auth/logout  — client-driven (clears its own token)

The Google OAuth + Drive routes remain as placeholders that return a
structured `auth_disabled` envelope so the frontend doesn't 404.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth_core import create_token, credentials_match, try_auth
from app.errors import envelope

log = logging.getLogger("agentic_ai.api.auth")
router = APIRouter()


# --- Real admin auth -------------------------------------------------------

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=200)
    password: str = Field(..., min_length=1, max_length=200)


@router.post("/auth/login")
async def auth_login(req: LoginRequest):
    """Validate credentials against ADMIN_USERNAME / ADMIN_PASSWORD env vars
    and return a signed bearer token on success."""
    if not credentials_match(req.username, req.password):
        log.info("login failed for username=%r", req.username)
        return JSONResponse(
            status_code=401,
            content=envelope(
                "Invalid credentials",
                detail="Username or password is incorrect.",
                kind="auth",
            ),
        )
    token, expires_at = create_token(req.username)
    log.info("login ok username=%r expires=%s", req.username, expires_at)
    return {
        "token":      token,
        "username":   req.username,
        "expires_at": expires_at,
    }


@router.get("/auth/me")
async def auth_me(authorization: Optional[str] = Header(default=None)):
    """Inspect the current bearer token. Returns `authenticated=false` if
    no/invalid/expired token (so the frontend can render its login screen)."""
    username = try_auth(authorization)
    if username:
        return {
            "authenticated":     True,
            "username":          username,
            "google_configured": False,
        }
    return {
        "authenticated":     False,
        "google_configured": False,
    }


@router.post("/auth/logout")
async def auth_logout():
    """Token revocation is client-driven — the client drops the token from
    storage. (Stateless tokens; restart the server with a new
    AUTH_TOKEN_SECRET to invalidate every token at once.)"""
    return {"ok": True}


# --- Google OAuth + Drive placeholders ------------------------------------

def _disabled() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content=envelope(
            "Google authentication is not enabled in this build.",
            detail="The Google Auth + Drive sync flow is intentionally disabled.",
            kind="auth_disabled",
        ),
    )


@router.get("/auth/google/login")
async def google_login():
    return _disabled()


@router.get("/auth/google/callback")
async def google_callback():
    return _disabled()


@router.post("/drive/sync")
async def drive_sync():
    return _disabled()


@router.get("/drive/status")
async def drive_status():
    return {
        "authenticated": False,
        "email": None,
        "imported_files": [],
    }
