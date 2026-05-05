"""Auth + Drive routes — placeholders.

Google OAuth flow is "in progress" per the architecture; the frontend renders
"Continue with Google" + Drive sync buttons. These routes return structured
`{ok: false, kind: "auth_disabled"}` so the UI doesn't 404 and shows a clear
"not connected" state.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.errors import envelope

router = APIRouter()


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


@router.get("/auth/me")
async def auth_me():
    # Frontend treats `authenticated=false` as "not signed in"; this is the
    # quiet-default that doesn't break the UI.
    return {
        "authenticated": False,
        "google_configured": False,
    }


@router.post("/auth/logout")
async def auth_logout():
    return {"ok": True}


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
