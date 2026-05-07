"""HTTP layer — admin auth + every route the frontend talks to.

Sections:
    1.  Auth core      — bearer-token issue / verify + FastAPI dependencies
    2.  Auth routes    — /auth/login, /auth/me, /auth/logout
                          + disabled Google / Drive placeholders (503)
    3.  Business routes — /health, /upload, /dashboard, /uploads,
                          /uploads/{batch_id}/disconnect, /cache/clear,
                          /query_stream (SSE)

Cross-module rule: this is the topmost layer below `main.py`. It imports
from `agents`, `tools`, `database`. Nothing imports back into `api.py`.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.agents import DashboardAgent, DataCleanAgent, run_query_turn
from app.database import (
    ALLOWED_TABLES,
    SAFE_MESSAGE,
    UploadError,
    cache_size,
    count_rows,
    disconnect_upload,
    envelope,
    invalidate_all,
    list_uploads_meta,
    record_upload_meta,
    settings,
)
from app.tools import (
    EventEmitter,
    GroqClient,
    TurnState,
    format_sse,
    reset_request_groq,
    set_request_groq,
)


log = logging.getLogger("agentic_ai.api")


# ===========================================================================
# 1. AUTH CORE — HMAC-signed bearer tokens
# ===========================================================================

# Token format: base64url(payload).hex(hmac_sha256(secret, payload))
# where payload = "<username>:<expiry_unix_ts>"
#
# Intentionally minimal (single admin, no DB-backed sessions). The secret
# comes from `AUTH_TOKEN_SECRET`; the credentials from `ADMIN_USERNAME` and
# `ADMIN_PASSWORD`. Restarting the server with a new secret invalidates
# every outstanding token at once.

_auth_log = logging.getLogger("agentic_ai.auth_core")


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
    Authorization header is missing / malformed / expired."""
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


# ===========================================================================
# 2. AUTH ROUTES (/auth/login, /auth/me, /auth/logout + disabled Google/Drive)
# ===========================================================================

auth_router = APIRouter()


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=200)
    password: str = Field(..., min_length=1, max_length=200)


@auth_router.post("/auth/login")
async def auth_login(req: LoginRequest):
    """Validate credentials against ADMIN_USERNAME / ADMIN_PASSWORD env vars
    and return a signed bearer token on success."""
    if not credentials_match(req.username, req.password):
        _auth_log.info("login failed for username=%r", req.username)
        return JSONResponse(
            status_code=401,
            content=envelope(
                "Invalid credentials",
                detail="Username or password is incorrect.",
                kind="auth",
            ),
        )
    token, expires_at = create_token(req.username)
    _auth_log.info("login ok username=%r expires=%s", req.username, expires_at)
    return {
        "token":      token,
        "username":   req.username,
        "expires_at": expires_at,
    }


@auth_router.get("/auth/me")
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


@auth_router.post("/auth/logout")
async def auth_logout():
    """Token revocation is client-driven — the client drops the token from
    storage. (Stateless tokens; restart the server with a new
    AUTH_TOKEN_SECRET to invalidate every token at once.)"""
    return {"ok": True}


# Google OAuth + Drive placeholders -----------------------------------------
# The Google Auth + Drive sync flow is intentionally disabled in this build.
# The frontend has UI for it but the backend returns a structured
# `auth_disabled` envelope so it doesn't 404.

def _disabled() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content=envelope(
            "Google authentication is not enabled in this build.",
            detail="The Google Auth + Drive sync flow is intentionally disabled.",
            kind="auth_disabled",
        ),
    )


@auth_router.get("/auth/google/login")
async def google_login():
    return _disabled()


@auth_router.get("/auth/google/callback")
async def google_callback():
    return _disabled()


@auth_router.post("/drive/sync")
async def drive_sync():
    return _disabled()


@auth_router.get("/drive/status")
async def drive_status():
    return {
        "authenticated": False,
        "email": None,
        "imported_files": [],
    }


# ===========================================================================
# 3. BUSINESS ROUTES
# ===========================================================================

api_router = APIRouter()


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

@api_router.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "model": settings.groq_model,
        "financial_db": settings.financial_db_path,
        "cache_entries": cache_size(),
        "sales_rows": await count_rows("sales"),
        "purchase_rows": await count_rows("purchase"),
    }


# ---------------------------------------------------------------------------
# /upload  (DataCleanAgent)
# ---------------------------------------------------------------------------

@api_router.post("/upload", dependencies=[Depends(require_auth)])
async def upload(
    request: Request,
    file: UploadFile = File(...),
    target: str = Form("sales"),
):
    """Upload route — every stage logs to `agentic_ai.api.upload` so a tail of
    the backend log shows the exact stage a request reached. EVERY return
    path produces a JSON body so the browser never sees an empty response.
    """
    upload_log = logging.getLogger("agentic_ai.api.upload")
    client_origin = request.headers.get("origin") or "(no-origin)"
    upload_log.info(
        "REQ /upload origin=%s ct=%s filename=%s target=%s",
        client_origin,
        request.headers.get("content-type", ""),
        file.filename, target,
    )

    target_table = (target or "sales").strip().lower()
    filename = file.filename or "upload"
    # Generate batch_id up-front so EVERY upload attempt — including failures —
    # creates a registry entry the UI can display.
    batch_id = str(uuid4())

    async def _record_error(reason: str, target_for_record: str) -> None:
        try:
            await record_upload_meta(
                batch_id=batch_id,
                filename=filename,
                target=target_for_record if target_for_record in ALLOWED_TABLES else "sales",
                rows_inserted=0,
                rows_failed=0,
                source="upload",
                status="error",
                error_message=reason[:500],
            )
        except Exception:
            upload_log.warning("failed to record error meta for batch=%s", batch_id, exc_info=True)

    if target_table not in ALLOWED_TABLES:
        upload_log.warning("RESP 400 invalid target=%r", target_table)
        await _record_error(f"invalid target: {target_table!r}", "sales")
        return JSONResponse(status_code=400, content=envelope(
            "Invalid target",
            detail=f"target must be one of {list(ALLOWED_TABLES)}",
            kind="validation",
        ))

    lower = filename.lower()
    if lower.endswith(".csv"):
        suffix = ".csv"
    elif lower.endswith(".xlsx"):
        suffix = ".xlsx"
    else:
        upload_log.warning("RESP 400 unsupported type=%r", filename)
        await _record_error(f"unsupported file type: {filename!r}", target_table)
        return JSONResponse(status_code=400, content=envelope(
            "Unsupported file type",
            detail="Only .csv and .xlsx are accepted.",
            kind="upload",
        ))

    fd, tmp_path = tempfile.mkstemp(prefix="agentic_upload_", suffix=suffix)
    bytes_written = 0
    try:
        # ---- 1. spool ---------------------------------------------------
        upload_log.info("spool: start tmp=%s", tmp_path)
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = await file.read(settings.upload_chunk_bytes)
                    if not chunk:
                        break
                    bytes_written += len(chunk)
                    if bytes_written > settings.max_upload_bytes:
                        upload_log.warning(
                            "RESP 413 file too large bytes=%d cap=%d",
                            bytes_written, settings.max_upload_bytes,
                        )
                        await _record_error(
                            f"file too large ({bytes_written} > {settings.max_upload_bytes})",
                            target_table,
                        )
                        return JSONResponse(status_code=413, content=envelope(
                            "File too large",
                            detail=f"Max {settings.max_upload_bytes // (1024*1024)} MB per upload.",
                            kind="upload",
                        ))
                    out.write(chunk)
        except Exception as e:
            upload_log.exception("spool: failed")
            await _record_error(f"spool failed: {type(e).__name__}: {e}", target_table)
            return JSONResponse(status_code=400, content=envelope(
                "Could not read upload",
                detail=f"{type(e).__name__}: {e}",
                kind="upload",
            ))
        upload_log.info("spool: done bytes=%d", bytes_written)

        if bytes_written == 0:
            upload_log.warning("RESP 400 empty file")
            await _record_error("empty file", target_table)
            return JSONResponse(status_code=400, content=envelope(
                "Empty file",
                kind="upload",
            ))

        # ---- 2. DataCleanAgent -----------------------------------------
        upload_log.info("dataclean: starting target=%s batch_id=%s",
                        target_table, batch_id)
        agent = DataCleanAgent()
        try:
            result = await agent.run(
                tmp_path=Path(tmp_path),
                filename=filename,
                target=target_table,
                batch_id=batch_id,
            )
        except UploadError as e:
            upload_log.warning("RESP 400 bad upload: %s", e)
            await _record_error(f"bad upload: {e}", target_table)
            return JSONResponse(status_code=400, content=envelope(
                "Bad upload",
                detail=str(e),
                kind="upload",
            ))
        except Exception as e:
            upload_log.exception("dataclean: crashed")
            await _record_error(f"{type(e).__name__}: {e}", target_table)
            return JSONResponse(status_code=500, content=envelope(
                "Ingest failed",
                detail=f"{type(e).__name__}: {e}",
                kind="internal",
            ))
        upload_log.info(
            "dataclean: ok rows_inserted=%d rows_failed=%d sheet=%r",
            result["rows_inserted"], result["rows_failed"], result.get("sheet_name"),
        )

        # ---- 3. cache invalidation -------------------------------------
        invalidate_all()

        # ---- 4. response ------------------------------------------------
        body = {
            "batch_id":          result["batch_id"],
            "filename":          result["filename"],
            "target":            result["target"],
            "rows_inserted":     result["rows_inserted"],
            "rows_failed":       result["rows_failed"],
            "errors":            result["errors"],
            "summary":           result["summary"],
            "unmatched_headers": result["unmatched_headers"],
            "sheet_name":        result.get("sheet_name"),
            "header_row_used":   result.get("header_row_used"),
            "validation":        result.get("validation"),
            "bytes_received":    bytes_written,
            "table_total":       await count_rows(target_table),
        }
        upload_log.info("RESP 200 batch_id=%s", body["batch_id"])
        return body
    except Exception as e:
        # Last-resort handler: NOTHING leaves /upload without a JSON body.
        upload_log.exception("upload: unhandled crash")
        return JSONResponse(status_code=500, content=envelope(
            "Upload failed",
            detail=f"{type(e).__name__}: {e}",
            kind="internal",
        ))
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        except Exception:
            log.warning("temp file cleanup failed: %s", tmp_path, exc_info=True)


# ---------------------------------------------------------------------------
# /dashboard (DashboardAgent)
# ---------------------------------------------------------------------------

@api_router.get("/dashboard", dependencies=[Depends(require_auth)])
async def dashboard(month: str | None = None):
    if month is not None and (len(month) != 7 or month[4] != "-"):
        return JSONResponse(status_code=400, content=envelope(
            "Invalid month",
            detail="Expected format YYYY-MM",
            kind="validation",
        ))
    try:
        return await DashboardAgent().run(month=month)
    except ValueError as e:
        return JSONResponse(status_code=400, content=envelope(
            "Invalid dashboard query", detail=str(e), kind="validation",
        ))
    except Exception as e:
        log.exception("dashboard failed")
        return JSONResponse(status_code=500, content=envelope(
            "Dashboard failed",
            detail=f"{type(e).__name__}: {e}",
            kind="internal",
        ))


# ---------------------------------------------------------------------------
# /uploads  (metadata listing for the dataset pill)
# ---------------------------------------------------------------------------

@api_router.get("/uploads", dependencies=[Depends(require_auth)])
async def uploads():
    return {
        "uploads": await list_uploads_meta(),
        "total_rows": {
            "sales":    await count_rows("sales"),
            "purchase": await count_rows("purchase"),
        },
    }


@api_router.post("/uploads/{batch_id}/disconnect", dependencies=[Depends(require_auth)])
async def upload_disconnect(batch_id: str):
    """Remove a dataset from active sources.

    Deletes every row in the target table tagged with this batch_id and
    flips the registry's status to 'removed'. Idempotent — if the dataset
    is already removed, returns 200 with `already_removed: true`.

    Side-effect: invalidates the answer cache (since the data the cached
    answers were grounded in just changed).
    """
    if not batch_id or len(batch_id) > 64:
        return JSONResponse(status_code=400, content=envelope(
            "Invalid batch_id", detail="batch_id is empty or too long.",
            kind="validation",
        ))
    try:
        result = await disconnect_upload(batch_id)
    except ValueError as e:
        return JSONResponse(status_code=404, content=envelope(
            "Dataset not found", detail=str(e), kind="validation",
        ))
    except Exception as e:
        log.exception("disconnect_upload failed")
        return JSONResponse(status_code=500, content=envelope(
            "Disconnect failed",
            detail=f"{type(e).__name__}: {e}",
            kind="internal",
        ))
    invalidate_all()
    return result


# ---------------------------------------------------------------------------
# /cache/clear
# ---------------------------------------------------------------------------

@api_router.post("/cache/clear", dependencies=[Depends(require_auth)])
async def cache_clear():
    n = invalidate_all()
    return {"cleared": n}


# ---------------------------------------------------------------------------
# /query_stream  (Coordinator → analytic sub-agent)
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)


HEARTBEAT_SECONDS = 15.0


def _resolve_groq_key(request: Request) -> str:
    header_key = (request.headers.get("X-Groq-Api-Key") or "").strip()
    return header_key or settings.groq_api_key.strip()


def _validate_pre_stream(req: QueryRequest, api_key: str) -> dict[str, Any] | None:
    if not api_key:
        return envelope(
            "Missing Groq API key",
            detail="Send `X-Groq-Api-Key` header or set `GROQ_API_KEY`.",
            kind="auth",
        )
    if any(c.isspace() for c in api_key):
        return envelope("Invalid Groq API key format",
                        detail="Key must not contain whitespace.", kind="auth")
    if len(api_key) < 20:
        return envelope("Invalid Groq API key format",
                        detail="Key looks too short (expected 20+ chars).", kind="auth")
    if not req.question.strip():
        return envelope("Empty question",
                        detail="Question must contain non-whitespace.", kind="validation")
    return None


def _safe_create_task(coro):
    try:
        return asyncio.create_task(coro)
    except Exception:
        try:
            coro.close()
        except Exception:
            pass
        return None


async def _runner(initial: TurnState, emitter: EventEmitter, api_key: str) -> None:
    client = GroqClient(api_key=api_key)
    token = set_request_groq(client)
    try:
        await run_query_turn(initial, emitter)
    except Exception as e:
        log.exception("coordinator crashed for turn %s", initial.turn_id)
        await emitter.emit("agent.result", envelope(
            f"{type(e).__name__}: {e}", kind="internal",
        ))
        await emitter.emit("turn.end", {
            "turn_id": initial.turn_id,
            "errors": [str(e)],
            "final_answer": None,
        })
    finally:
        try:
            reset_request_groq(token)
        except Exception:
            pass
        try:
            await client.aclose()
        except Exception:
            pass
        await emitter.close()


async def _heartbeat(emitter: EventEmitter) -> None:
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                await emitter.comment("ping")
            except Exception:
                return
    except asyncio.CancelledError:
        pass


async def _emit_pre_stream_error(emitter: EventEmitter, payload: dict) -> None:
    fid = str(uuid4())
    try:
        await emitter.emit("turn.start", {"turn_id": fid, "question": "", "mode": "ERROR"})
        await emitter.emit("agent.result", payload)
        await emitter.emit("turn.end", {
            "turn_id": fid,
            "errors": [payload.get("error", "unknown")],
            "final_answer": None,
        })
    finally:
        await emitter.close()


def _stream_response(
    emitter: EventEmitter,
    *tasks: asyncio.Task | None,
) -> StreamingResponse:
    async def gen():
        try:
            async for chunk in emitter.stream():
                yield chunk
        except asyncio.CancelledError:
            raise
        except Exception as e:
            yield format_sse("agent.result", envelope(
                f"{type(e).__name__}: {e}", kind="internal", message=SAFE_MESSAGE,
            ))
            yield format_sse("turn.end", {"turn_id": "fatal", "errors": [str(e)]})
        finally:
            for t in tasks:
                if t is not None and not t.done():
                    try:
                        t.cancel()
                    except Exception:
                        pass
    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@api_router.post("/query_stream", dependencies=[Depends(require_auth)])
async def query_stream(req: QueryRequest, request: Request):
    api_key = _resolve_groq_key(request)
    emitter = EventEmitter()
    pre_error = _validate_pre_stream(req, api_key)
    if pre_error is not None:
        _safe_create_task(_emit_pre_stream_error(emitter, pre_error))
        return _stream_response(emitter)

    initial = TurnState(question=req.question)
    runner_task = _safe_create_task(_runner(initial, emitter, api_key))
    heartbeat_task = _safe_create_task(_heartbeat(emitter))
    return _stream_response(emitter, runner_task, heartbeat_task)
