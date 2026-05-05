"""Agentic AI — FastAPI entrypoint.

Run from the project root:
    python main.py
or:
    uvicorn backend.main:app --port 8000 --reload
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Allow `import app.*` whether launched as `uvicorn main:app` from inside
# backend/ or as `uvicorn backend.main:app` from the project root.
_BACKEND_DIR = str(Path(__file__).resolve().parent)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from app.api import api_router, auth_router
from app.config import settings
from app.errors import envelope
from app.logging_setup import configure_logging
from app.tools import get_registry  # registry bootstrap on import

configure_logging()
log = logging.getLogger("agentic_ai")

app = FastAPI(
    title="Agentic AI",
    description="LLM-coordinated analytics over user-uploaded financial data.",
    version="3.0.0",
)

# Middleware order matters: Starlette applies the LAST `add_middleware`
# OUTERMOST. CORS must be the outermost middleware so it can short-circuit
# OPTIONS preflights before the session middleware runs.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="agentic_session",
    same_site="lax",
    max_age=60 * 60 * 24 * 7,
    https_only=False,
)
# Use a regex for allow-origin so the response echoes the request origin.
# The literal `*` is incompatible with `Access-Control-Allow-Credentials: true`
# and causes every credentialed fetch from the browser to fail with the
# opaque "Failed to fetch" error. `allow_origin_regex=".*"` matches every
# origin and lets us safely keep allow_credentials=True.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

app.include_router(api_router)
app.include_router(auth_router)


def _sanitize_validation_errors(errors: list) -> list:
    safe: list = []
    for err in errors:
        e = dict(err) if isinstance(err, dict) else {"msg": str(err)}
        inp = e.get("input")
        if isinstance(inp, (bytes, bytearray)):
            try:
                e["input"] = inp.decode("utf-8", errors="replace")
            except Exception:
                e["input"] = repr(inp)
        elif inp is not None and not isinstance(inp, (str, int, float, bool, list, dict, type(None))):
            e["input"] = repr(inp)
        safe.append(e)
    return safe


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError):
    try:
        detail = _sanitize_validation_errors(exc.errors())
    except Exception:
        detail = [{"msg": "Validation failed"}]
    return JSONResponse(
        status_code=400,
        content=envelope("Invalid request body", detail=str(detail), kind="validation",
                         extra={"validation_errors": detail}),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled exception in %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content=envelope(str(exc) or type(exc).__name__,
                         detail=type(exc).__name__, kind="internal"),
    )


@app.on_event("startup")
async def _startup() -> None:
    from app.database import init_database
    await init_database()
    # Force the tool registry to bootstrap at boot. Any missing/extra tool
    # will raise here and fail fast.
    registry = get_registry()
    log.info("registry: %d tools registered: %s", len(registry.names), registry.names)
    log.info("financial DB ready at %s", settings.financial_db_path)


if __name__ == "__main__":
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=settings.reload)
