"""core_system — FastAPI app, every HTTP route, auth router, middleware,
exception handlers, startup hook.

Consolidates the former app.api (routes + auth) and the former backend/main.py
(FastAPI app construction, middleware, exception handlers, startup hook).

Run from the project root:
    python backend/main.py
or:
    uvicorn backend.app.core_system:app --port 8000 --reload
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

from app.analytics_engine import DashboardAgent, DataCleanAgent, run_query_turn
from app.analytics_acceleration import duckdb_status, get_duckdb_engine
from app.database import AuditLogRepo, engine_kind, engine_status
from app.monitoring import (
    init_sentry,
    instrument_fastapi,
    sentry_status,
    set_request_context,
)
from app.infrastructure import (
    ALLOWED_TABLES,
    SAFE_MESSAGE,
    UploadError,
    bump_data_version,
    cache_size,
    count_rows,
    disconnect_upload,
    envelope,
    get_data_version,
    invalidate_all,
    list_uploads_meta,
    record_upload_meta,
    settings,
)
from app.analytics_engine import (
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
    Authorization header is missing / malformed / expired.

    Returns the user identifier (email for JWT users, username for legacy
    HMAC). Routes that need the FULL principal (tenant_id, workspace_id,
    role) should use `from app.auth import require_principal` instead."""
    from app.auth import try_principal
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
    # Path 1: JWT principals (multi-user model + legacy-HMAC compat).
    principal = try_principal(authorization)
    if principal is not None:
        return principal.email
    # Path 2: legacy-HMAC-only (no JWT principal recognised). The
    # legacy_admin synthesis in try_principal already covers the
    # standard HMAC path, so this is a true no-credentials case.
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


class RegisterRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=8, max_length=200)
    workspace_name: str = Field(default="My Workspace", min_length=1, max_length=200)


@auth_router.post("/auth/register")
async def auth_register(req: RegisterRequest, request: Request):
    """Self-service signup: creates a (tenant, workspace, user) trio and
    returns a JWT immediately so the user lands signed-in. The first user
    of a workspace is the `owner` role.

    Rate-limited by client IP (10/min) to prevent spam-registration.

    Validation:
      - email must contain '@' and '.' (light gate; real validation defers
        to the password reset / email verify flow which doesn't exist yet)
      - password ≥ 8 chars (Pydantic enforces this)

    Returns 409 if the email is already registered."""
    client_ip = _client_ip(request)
    rl = _rate_limit_check(client_ip, bucket_namespace="register", limit_per_minute=10)
    if rl is not None:
        return JSONResponse(status_code=429, content=rl)
    from app.auth import (
        create_user, create_workspace, encode_token, find_user_by_email,
        hash_password,
    )
    email = req.email.strip().lower()
    if "@" not in email or "." not in email.split("@", 1)[-1]:
        return JSONResponse(
            status_code=400,
            content=envelope("Invalid email", detail="Email must look like a@b.c.", kind="validation"),
        )
    existing = await find_user_by_email(email)
    if existing is not None:
        return JSONResponse(
            status_code=409,
            content=envelope(
                "Email already registered",
                detail="Use /auth/login or pick a different email.",
                kind="auth",
            ),
        )
    workspace = await create_workspace(req.workspace_name)
    user = await create_user(
        email=email,
        password_hash=hash_password(req.password),
        tenant_id=workspace.tenant_id,
        workspace_id=workspace.id,
        role="owner",
    )
    token, expires_at = encode_token(
        user_id=user.id, email=user.email,
        tenant_id=user.tenant_id, workspace_id=user.workspace_id,
        role=user.role,
    )
    _auth_log.info(
        "register ok email=%s tenant=%s workspace=%s role=%s",
        email, user.tenant_id, user.workspace_id, user.role,
    )
    await AuditLogRepo.write(
        action="auth.register",
        tenant_id=user.tenant_id,
        workspace_id=user.workspace_id,
        user_id=user.id,
        resource_type="user",
        resource_id=user.id,
        metadata={"email": email, "workspace_name": req.workspace_name},
    )
    return {
        "token":         token,
        "expires_at":    expires_at,
        "user": {
            "id":           user.id,
            "email":        user.email,
            "tenant_id":    user.tenant_id,
            "workspace_id": user.workspace_id,
            "role":         user.role,
        },
        "workspace": {
            "id":   workspace.id,
            "name": workspace.name,
        },
    }


@auth_router.post("/auth/login")
async def auth_login(req: LoginRequest, request: Request):
    """Validate credentials and return a JWT.

    Two paths supported in priority order:
      1. **Real users** (registered via /auth/register) — looked up by email
         against the `users` table; bcrypt-verified. Returns a JWT with
         user_id / tenant_id / workspace_id / role claims.
      2. **Legacy admin** — falls back to the env-var ADMIN_USERNAME /
         ADMIN_PASSWORD pair, issuing the OLD HMAC bearer for backward
         compatibility with any deployment that hasn't migrated yet.

    Treats `username` field as either email (path 1) or username (path 2);
    the underscore lets old clients keep working without a payload schema
    change.

    Rate-limited by client IP (20/min) — defends against credential-stuffing
    attacks. The limit is per-IP, not per-username, so attackers can't
    bypass by rotating usernames."""
    client_ip = _client_ip(request)
    rl = _rate_limit_check(client_ip, bucket_namespace="login", limit_per_minute=20)
    if rl is not None:
        return JSONResponse(status_code=429, content=rl)
    from app.auth import (
        encode_token, find_user_by_email, update_last_login, verify_password,
    )
    raw = (req.username or "").strip()
    # Path 1: real users table (lookup by email).
    if "@" in raw:
        result = await find_user_by_email(raw.lower())
        if result is None or not verify_password(req.password, result[1]):
            _auth_log.info("login failed (jwt path) for email=%r", raw)
            # Audit-log the failure (no user_id since the lookup failed
            # OR the password was wrong — we don't reveal which).
            await AuditLogRepo.write(
                action="auth.login_failed",
                resource_type="user",
                metadata={"email": raw},
            )
            return JSONResponse(
                status_code=401,
                content=envelope(
                    "Invalid credentials",
                    detail="Email or password is incorrect.",
                    kind="auth",
                ),
            )
        user, _hash = result
        token, expires_at = encode_token(
            user_id=user.id, email=user.email,
            tenant_id=user.tenant_id, workspace_id=user.workspace_id,
            role=user.role,
        )
        await update_last_login(user.id)
        _auth_log.info("login ok email=%s tenant=%s role=%s",
                       user.email, user.tenant_id, user.role)
        await AuditLogRepo.write(
            action="auth.login",
            tenant_id=user.tenant_id,
            workspace_id=user.workspace_id,
            user_id=user.id,
            resource_type="user",
            resource_id=user.id,
            metadata={"email": user.email, "role": user.role},
        )
        return {
            "token":      token,
            "username":   user.email,                     # legacy alias
            "expires_at": _expiry_iso(expires_at),
            "user": {
                "id":           user.id,
                "email":        user.email,
                "tenant_id":    user.tenant_id,
                "workspace_id": user.workspace_id,
                "role":         user.role,
            },
        }

    # Path 2: legacy env-var admin (back-compat for existing deployments).
    if not credentials_match(req.username, req.password):
        _auth_log.info("login failed (legacy path) for username=%r", req.username)
        return JSONResponse(
            status_code=401,
            content=envelope(
                "Invalid credentials",
                detail="Username or password is incorrect.",
                kind="auth",
            ),
        )
    token, expires_at_iso = create_token(req.username)
    _auth_log.info("login ok (legacy) username=%r expires=%s",
                   req.username, expires_at_iso)
    return {
        "token":      token,
        "username":   req.username,
        "expires_at": expires_at_iso,
    }


def _expiry_iso(expiry_unix: int) -> str:
    """Format unix expiry as ISO so the frontend's existing parser keeps
    working (the legacy HMAC path returned ISO; we match that here)."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(expiry_unix, tz=timezone.utc).isoformat(timespec="seconds")


@auth_router.get("/auth/me")
async def auth_me(authorization: Optional[str] = Header(default=None)):
    """Inspect the current bearer token. Returns `authenticated=false` if
    no/invalid/expired token (so the frontend can render its login screen).

    NEW: when authenticated via JWT, surfaces the full principal —
    user_id / tenant_id / workspace_id / role — so the frontend can route
    based on role and display the workspace name."""
    from app.auth import try_principal, get_workspace_by_id
    principal = try_principal(authorization)
    if principal is not None:
        # JWT path — multi-user model.
        workspace = None
        if principal.workspace_id and principal.workspace_id != "legacy_admin":
            ws = await get_workspace_by_id(principal.workspace_id)
            if ws:
                workspace = {"id": ws.id, "name": ws.name}
        return {
            "authenticated":     True,
            "username":          principal.email,
            "user_id":           principal.user_id,
            "tenant_id":         principal.tenant_id,
            "workspace_id":      principal.workspace_id,
            "role":              principal.role,
            "workspace":         workspace,
            "google_configured": False,
        }
    # Legacy path (HMAC username) preserved for already-deployed sessions.
    legacy_username = try_auth(authorization)
    if legacy_username:
        return {
            "authenticated":     True,
            "username":          legacy_username,
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
    from app.cache import redis_status, upstash_status
    from app.database.pg_adapter import pg_status
    from app.infrastructure import (
        get_blob_store, get_cache_store, get_conversation_store, settings as _s,
    )
    from app.integrations import supabase_status
    from app.vector import get_vector_store
    from app.workers import get_job_queue
    return {
        "status": "ok",
        "model": settings.groq_model,
        "financial_db": settings.financial_db_path,
        "data_version": get_data_version(),
        "cache_entries": cache_size(),
        "cache_backend": get_cache_store().kind(),
        "conversation_backend": get_conversation_store().kind(),
        "blob_backend": get_blob_store().kind(),
        "rate_limit_per_minute": settings.rate_limit_per_minute,
        "sales_rows": await count_rows("sales"),
        "purchase_rows": await count_rows("purchase"),
        # New subsystem statuses — `/health` is the single source of truth
        # for "is monitoring wired", "is DuckDB wired", etc.
        "duckdb": duckdb_status(),
        "sentry": sentry_status(),
        "vector_backend": get_vector_store().kind_label(),
        "vector_dim": get_vector_store().dim(),
        "vector_size": get_vector_store().size(),
        "worker_backend": get_job_queue().backend_kind,
        "engine": engine_status(),
        "redis": redis_status(),
        "upstash": upstash_status(),    # masked: token never echoed
        "postgres": pg_status(),
        "postgres_primary": bool(_s.postgres_primary),
        "supabase": supabase_status(),  # masked: key never echoed
    }


# ---------------------------------------------------------------------------
# /upload  (DataCleanAgent)
# ---------------------------------------------------------------------------

@api_router.post("/upload", dependencies=[Depends(require_auth)])
async def upload(
    request: Request,
    file: UploadFile = File(...),
    target: str = Form("sales"),
    auth_user: str = Depends(require_auth),
):
    """Upload route — every stage logs to `agentic_ai.api.upload` so a tail of
    the backend log shows the exact stage a request reached. EVERY return
    path produces a JSON body so the browser never sees an empty response.

    Rate-limited per authenticated user (5/min) so a runaway tab can't
    DoS the ingestion pipeline."""
    upload_log = logging.getLogger("agentic_ai.api.upload")
    rl = _rate_limit_check(auth_user, bucket_namespace="upload", limit_per_minute=5)
    if rl is not None:
        return JSONResponse(status_code=429, content=rl)
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
        # Resolve principal so every inserted row carries tenant/workspace/
        # user attribution. Legacy HMAC users get the synthetic
        # "legacy_admin" tenant.
        from app.auth import try_principal
        principal = try_principal(request.headers.get("Authorization"))
        upload_tenant    = principal.tenant_id    if principal else None
        upload_workspace = principal.workspace_id if principal else None
        upload_user      = principal.user_id      if principal else None
        upload_log.info(
            "dataclean: starting target=%s batch_id=%s tenant=%s",
            target_table, batch_id, upload_tenant or "<none>",
        )
        agent = DataCleanAgent()
        try:
            result = await agent.run(
                tmp_path=Path(tmp_path),
                filename=filename,
                target=target_table,
                batch_id=batch_id,
                tenant_id=upload_tenant,
                workspace_id=upload_workspace,
                user_id=upload_user,
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
        # Bump the data_version counter — every cached response keyed against
        # the OLD version is now unreachable (cache_key_for embeds the
        # version). Belt + braces: also call invalidate_all() so the JSON
        # file shrinks back to empty rather than growing unbounded.
        new_version = bump_data_version()
        invalidate_all()
        upload_log.info("data_version bumped to %d after upload", new_version)
        # Audit log: record the successful upload so ops can correlate
        # data-version bumps with users + batches.
        await AuditLogRepo.write(
            action="upload.success",
            tenant_id=upload_tenant,
            workspace_id=upload_workspace,
            user_id=upload_user,
            resource_type="upload",
            resource_id=batch_id,
            metadata={
                "filename":      filename,
                "target":        target_table,
                "rows_inserted": result["rows_inserted"],
                "rows_failed":   result["rows_failed"],
                "data_version":  new_version,
            },
        )

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
async def dashboard(
    request: Request,
    month: str | None = None,
):
    from app.auth import try_principal
    if month is not None and (len(month) != 7 or month[4] != "-"):
        return JSONResponse(status_code=400, content=envelope(
            "Invalid month",
            detail="Expected format YYYY-MM",
            kind="validation",
        ))
    # Tenant-scope the dashboard. Legacy HMAC users get a synthetic
    # "legacy_admin" tenant — they only see rows uploaded by that tenant.
    principal = try_principal(request.headers.get("Authorization"))
    tenant_id = principal.tenant_id if principal else None
    try:
        return await DashboardAgent().run(month=month, tenant_id=tenant_id)
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
    bump_data_version()
    invalidate_all()
    return result


# ---------------------------------------------------------------------------
# /cache/clear
# ---------------------------------------------------------------------------

@api_router.post("/cache/clear", dependencies=[Depends(require_auth)])
async def cache_clear(request: Request):
    from app.auth import try_principal
    principal = try_principal(request.headers.get("Authorization"))
    n = invalidate_all()
    new_version = bump_data_version()
    await AuditLogRepo.write(
        action="cache.cleared",
        tenant_id=principal.tenant_id if principal else None,
        workspace_id=principal.workspace_id if principal else None,
        user_id=principal.user_id if principal else None,
        resource_type="cache",
        metadata={"entries_removed": n, "new_data_version": new_version},
    )
    return {"cleared": n, "data_version": new_version}


# ---------------------------------------------------------------------------
# /query_stream  (Coordinator → analytic sub-agent)
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    # Optional conversation identifier — frontend generates a UUID per
    # chat session and persists it in localStorage. Lets the coordinator
    # isolate continuity context per (user, conversation) so two
    # logged-in users (or two browser tabs) can't bleed state.
    conversation_id: str | None = Field(default=None, max_length=128)


HEARTBEAT_SECONDS = 15.0


# ---------------------------------------------------------------------------
# Rate limiter — in-memory token bucket per user. Defends `/query_stream`
# + auth + upload against runaway tabs / accidental loops / token-burning
# bots / signup spam. The shape is Redis-compatible: when REDIS_URL is set
# later, swap `_RATE_BUCKETS` for a Redis-backed sliding-window counter
# behind the same `_rate_limit_check` signature. No call-site changes.
#
# `_client_ip` extracts a rate-limit key for unauthenticated endpoints
# (login + register). Honours `X-Forwarded-For` since deployments behind
# Render / Cloudflare / nginx all set it; falls back to `request.client`.
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    """Best-effort client identifier for rate limiting unauth routes."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        # First entry is the client; the rest are proxy hops.
        return fwd.split(",")[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"

import collections
import threading

_RATE_LOCK = threading.Lock()
_RATE_BUCKETS: dict[str, collections.deque] = {}


def _rate_limit_check(
    user_key: str,
    *,
    bucket_namespace: str = "query",
    limit_per_minute: int | None = None,
) -> dict[str, Any] | None:
    """Token-bucket rate limiter. Per-(user_key, namespace) so different
    endpoints get separate buckets.

    Returns None on success, an envelope dict on rate-limit hit."""
    now = time.time()
    window = 60.0
    limit = max(1, int(limit_per_minute or settings.rate_limit_per_minute))
    composite_key = f"{bucket_namespace}:{user_key}"
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS.get(composite_key)
        if bucket is None:
            bucket = collections.deque(maxlen=limit + 1)
            _RATE_BUCKETS[composite_key] = bucket
        # Drop entries older than the window.
        while bucket and (now - bucket[0]) > window:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_in = max(1, int(window - (now - bucket[0])))
            return envelope(
                f"Rate limit hit ({limit}/min)",
                detail=f"Too many {bucket_namespace} requests. Retry in ~{retry_in}s.",
                kind="rate_limit",
                extra={
                    "retry_after_seconds": retry_in,
                    "limit_per_minute":    limit,
                    "namespace":           bucket_namespace,
                },
            )
        bucket.append(now)
    return None


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
async def query_stream(
    req: QueryRequest,
    request: Request,
    auth_user: str = Depends(require_auth),
):
    from app.auth import try_principal
    api_key = _resolve_groq_key(request)
    emitter = EventEmitter()
    pre_error = _validate_pre_stream(req, api_key)
    if pre_error is not None:
        _safe_create_task(_emit_pre_stream_error(emitter, pre_error))
        return _stream_response(emitter)

    # Rate limit per user (in-memory token bucket, swappable to Redis).
    rate_error = _rate_limit_check(auth_user)
    if rate_error is not None:
        _safe_create_task(_emit_pre_stream_error(emitter, rate_error))
        return _stream_response(emitter)

    # Resolve the principal so we can stamp tenant_id / workspace_id / role
    # into TurnState. For legacy HMAC tokens this synthesises a
    # `legacy_admin` tenant (single-tenant slice).
    principal = try_principal(request.headers.get("Authorization"))
    user_id = principal.user_id if principal else auth_user
    tenant_id = principal.tenant_id if principal else "legacy_admin"

    # Bind salient identifiers onto the Sentry scope so any exception
    # captured downstream (a tool crash, a Groq timeout) is enriched with
    # who/what/where context. No-op when SENTRY_DSN is unset.
    set_request_context(
        user_id=user_id,
        tenant_id=tenant_id,
        conversation_id=req.conversation_id,
        question_chars=len(req.question),
    )

    initial = TurnState(
        question=req.question,
        user_id=user_id,
        tenant_id=tenant_id,
        conversation_id=req.conversation_id,
    )
    runner_task = _safe_create_task(_runner(initial, emitter, api_key))
    heartbeat_task = _safe_create_task(_heartbeat(emitter))
    return _stream_response(emitter, runner_task, heartbeat_task)


# ===========================================================================
# FASTAPI APP — middleware, exception handlers, startup (was backend/main.py)
# ===========================================================================

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from app.analytics_engine import get_registry  # registry bootstrap on import


def configure_logging(level: int = logging.INFO) -> None:
    """One-shot logging configuration."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Quiet noisy upstreams.
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


configure_logging()
_app_log = logging.getLogger("agentic_ai")

# Initialize Sentry BEFORE FastAPI() so the SDK's FastAPI integration sees
# every route as it's registered. No-op when SENTRY_DSN is unset.
init_sentry()

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

# Per-request UUID + Sentry scope priming. Adds the X-Request-ID response
# header so a client can quote it when reporting issues.
instrument_fastapi(app)


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
    _app_log.exception("Unhandled exception in %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content=envelope(str(exc) or type(exc).__name__,
                         detail=type(exc).__name__, kind="internal"),
    )


@app.on_event("startup")
async def _startup() -> None:
    from app.cache import wire_redis_if_configured
    from app.infrastructure import init_database, load_synonyms
    from app.vector import register_vocabulary
    from app.workers import get_job_queue, register_default_tasks
    await init_database()
    # Background workers — registers the @task decorators + builds the
    # inline (or future arq-backed) queue singleton. Pure registration:
    # no Redis call, no network.
    register_default_tasks()
    _app_log.info("workers: backend=%s", get_job_queue().backend_kind)

    # Try to swap in Redis-backed cache + conversation stores. No-op
    # when REDIS_URL is unset; on failure we log + stay on in-memory.
    redis_state = await wire_redis_if_configured()
    _app_log.info("redis bootstrap: %s", redis_state)
    _app_log.info("database engine: %s", engine_kind())

    # Eager Supabase probe — when SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY
    # are both set, list buckets ONCE at startup so a bad URL / revoked
    # key surfaces immediately. NEVER raises (broken Supabase falls back
    # to local-fs blob store + JSON cache, system continues serving).
    # The service-role key is masked in every log line.
    from app.integrations import (
        SupabaseUnavailableError, get_supabase, supabase_status,
    )
    sb_cfg = supabase_status()
    if sb_cfg["configured"]:
        try:
            sb = await get_supabase()
            ping = await sb.ping()
            _app_log.info(
                "supabase probe OK url=%s buckets=%d (key fp=%s)",
                sb_cfg["url"], ping["buckets"], sb_cfg["key_fingerprint"],
            )
        except SupabaseUnavailableError as e:
            _app_log.warning(
                "supabase probe FAILED — system continues without Supabase: %s", e,
            )
        except Exception as e:
            _app_log.warning(
                "supabase probe FAILED — unexpected: %s: %s",
                type(e).__name__, e,
            )
    else:
        _app_log.info("supabase: unconfigured (SUPABASE_URL/SERVICE_ROLE_KEY unset)")

    # Eager Postgres probe — when DATABASE_URL is set, attempt the pool
    # connection ONCE at startup so an invalid DSN / wrong password fails
    # loud + visible in the boot log instead of failing at first query
    # later. NEVER raises out of this hook (broken Postgres must not
    # block the app from booting against the SQLite fallback).
    if engine_kind() == "postgres":
        from app.database.pg_adapter import (
            PostgresUnavailableError, pg_pool, pg_status,
        )
        try:
            pool = await pg_pool()
            # Cheap roundtrip — proves auth + network reachability.
            async with pool.acquire() as conn:
                version = await conn.fetchval("SELECT version()")
            _app_log.info(
                "postgres probe OK — pool ready, server reports: %s",
                str(version)[:80],
            )
        except PostgresUnavailableError as e:
            _app_log.warning(
                "postgres probe FAILED — DATABASE_URL set but unreachable; "
                "system continues on SQLite fallback: %s", e,
            )
        except Exception as e:
            _app_log.warning(
                "postgres probe FAILED — unexpected error; system continues "
                "on SQLite fallback: %s: %s", type(e).__name__, e,
            )
        _app_log.info("postgres status after probe: %s", pg_status())
    # Force the tool registry to bootstrap at boot. Any missing/extra tool
    # will raise here and fail fast.
    registry = get_registry()
    _app_log.info("registry: %d tools registered: %s", len(registry.names), registry.names)
    _app_log.info("financial DB ready at %s", settings.financial_db_path)

    # Bootstrap the vector vocabulary from the deterministic synonyms file.
    # Today this is the main source of canonical entity names; later,
    # uploads can extend the vocabulary with newly-seen Product/Party
    # names. Wrapped in try/except so a vector-layer failure can never
    # block FastAPI startup.
    try:
        syns = load_synonyms()
        if syns:
            n = register_vocabulary("entity", syns)
            _app_log.info(
                "vector vocabulary bootstrapped: %d canonicals from synonyms.json",
                n,
            )
    except Exception:
        _app_log.exception("vector vocabulary bootstrap failed (continuing without)")

    # Surface DuckDB + Sentry status in the boot log so ops sees them
    # without needing /health.
    _app_log.info("duckdb: %s", duckdb_status())
    _app_log.info("sentry: %s", sentry_status())


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Release pooled clients (Supabase httpx, Postgres asyncpg, Redis).
    Keep this cheap + best-effort — never raise out of shutdown, the
    process is going down regardless."""
    from app.cache import upstash_close
    from app.cache.redis_client import redis_close
    from app.database.pg_adapter import pg_close
    from app.integrations import supabase_close
    for label, fn in (
        ("supabase", supabase_close),
        ("upstash",  upstash_close),
        ("postgres", pg_close),
        ("redis",    redis_close),
    ):
        try:
            await fn()
        except Exception:
            _app_log.exception("%s shutdown failed", label)


# Direct execution is unsupported — use `python backend/main.py` (the entry shim).
