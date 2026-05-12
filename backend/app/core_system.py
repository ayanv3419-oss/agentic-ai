"""core_system — FastAPI app, every HTTP route, startup hook.

Single-user local-first MVP — NO AUTHENTICATION. The app opens directly.

Run from the project root:
    python backend/main.py
or:
    uvicorn backend.app.core_system:app --port 8000 --reload
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import (
    APIRouter, FastAPI, File, Form, Request, UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.analytics_engine import (
    DashboardAgent,
    DataCleanAgent,
    EventEmitter,
    GroqClient,
    TurnState,
    format_sse,
    get_registry,
    reset_request_groq,
    run_query_turn,
    set_request_groq,
)
from app.database import engine_kind, engine_status
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
from app.monitoring import (
    init_sentry,
    instrument_fastapi,
    sentry_status,
    set_request_context,
)


log = logging.getLogger("agentic_ai.api")


# ===========================================================================
# 1. RATE LIMITER — in-memory token bucket (keyed by client IP)
# ===========================================================================

_RATE_LOCK = threading.Lock()
_RATE_BUCKETS: dict[str, collections.deque] = {}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _rate_limit_check(
    user_key: str,
    *,
    bucket_namespace: str = "query",
    limit_per_minute: int | None = None,
) -> dict[str, Any] | None:
    now = time.time()
    window = 60.0
    limit = max(1, int(limit_per_minute or settings.rate_limit_per_minute))
    composite_key = f"{bucket_namespace}:{user_key}"
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS.get(composite_key)
        if bucket is None:
            bucket = collections.deque(maxlen=limit + 1)
            _RATE_BUCKETS[composite_key] = bucket
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


# ===========================================================================
# 2. API ROUTES — all public, no auth
# ===========================================================================

api_router = APIRouter()


@api_router.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "version": "3.1.0-no-auth",
        "data_version": get_data_version(),
        "cache": {
            "kind": "json_file",
            "size": cache_size(),
        },
        "database": engine_status(),
        "sales_rows": await count_rows("sales"),
        "purchase_rows": await count_rows("purchase"),
        "sentry": sentry_status(),
    }


# ---------------------------------------------------------------------------
# /upload  (DataCleanAgent)
# ---------------------------------------------------------------------------

@api_router.post("/upload")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    target: str = Form("sales"),
):
    upload_log = logging.getLogger("agentic_ai.api.upload")
    rl = _rate_limit_check(_client_ip(request), bucket_namespace="upload", limit_per_minute=5)
    if rl is not None:
        return JSONResponse(status_code=429, content=rl)

    target_table = (target or "sales").strip().lower()
    filename = file.filename or "upload"
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
        await _record_error(f"unsupported file type: {filename!r}", target_table)
        return JSONResponse(status_code=400, content=envelope(
            "Unsupported file type",
            detail="Only .csv and .xlsx are accepted.",
            kind="upload",
        ))

    fd, tmp_path = tempfile.mkstemp(prefix="agentic_upload_", suffix=suffix)
    bytes_written = 0
    try:
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = await file.read(settings.upload_chunk_bytes)
                    if not chunk:
                        break
                    bytes_written += len(chunk)
                    if bytes_written > settings.max_upload_bytes:
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

        if bytes_written == 0:
            await _record_error("empty file", target_table)
            return JSONResponse(status_code=400, content=envelope(
                "Empty file", kind="upload",
            ))

        agent = DataCleanAgent()
        try:
            result = await agent.run(
                tmp_path=Path(tmp_path),
                filename=filename,
                target=target_table,
                batch_id=batch_id,
            )
        except UploadError as e:
            await _record_error(f"bad upload: {e}", target_table)
            return JSONResponse(status_code=400, content=envelope(
                "Bad upload", detail=str(e), kind="upload",
            ))
        except Exception as e:
            upload_log.exception("dataclean: crashed")
            await _record_error(f"{type(e).__name__}: {e}", target_table)
            return JSONResponse(status_code=500, content=envelope(
                "Ingest failed",
                detail=f"{type(e).__name__}: {e}",
                kind="internal",
            ))

        new_version = bump_data_version()
        invalidate_all()
        upload_log.info(
            "upload ok rows=%d batch=%s data_version=%d",
            result["rows_inserted"], batch_id, new_version,
        )

        return {
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
    except Exception as e:
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
# /dashboard
# ---------------------------------------------------------------------------

@api_router.get("/dashboard")
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
# /uploads — metadata listing + dataset removal
# ---------------------------------------------------------------------------

@api_router.get("/uploads")
async def uploads():
    return {
        "uploads": await list_uploads_meta(),
        "total_rows": {
            "sales":    await count_rows("sales"),
            "purchase": await count_rows("purchase"),
        },
    }


@api_router.post("/uploads/{batch_id}/disconnect")
async def upload_disconnect(batch_id: str):
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

@api_router.post("/cache/clear")
async def cache_clear():
    n = invalidate_all()
    new_version = bump_data_version()
    return {"cleared": n, "data_version": new_version}


# ---------------------------------------------------------------------------
# /query_stream (SSE)
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=128)


HEARTBEAT_SECONDS = 15.0


def _resolve_groq_key(request: Request) -> str:
    header_key = (request.headers.get("X-Groq-Api-Key") or "").strip()
    return header_key or settings.groq_api_key.strip()


def _validate_pre_stream(req: QueryRequest, api_key: str) -> dict[str, Any] | None:
    if not api_key:
        return envelope(
            "Missing Groq API key",
            detail="Set `GROQ_API_KEY` in backend/.env or send `X-Groq-Api-Key` header.",
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


@api_router.post("/query_stream")
async def query_stream(req: QueryRequest, request: Request):
    api_key = _resolve_groq_key(request)
    emitter = EventEmitter()
    pre_error = _validate_pre_stream(req, api_key)
    if pre_error is not None:
        _safe_create_task(_emit_pre_stream_error(emitter, pre_error))
        return _stream_response(emitter)

    rate_error = _rate_limit_check(_client_ip(request))
    if rate_error is not None:
        _safe_create_task(_emit_pre_stream_error(emitter, rate_error))
        return _stream_response(emitter)

    set_request_context(
        conversation_id=req.conversation_id,
        question_chars=len(req.question),
    )

    initial = TurnState(
        question=req.question,
        conversation_id=req.conversation_id,
    )
    runner_task = _safe_create_task(_runner(initial, emitter, api_key))
    heartbeat_task = _safe_create_task(_heartbeat(emitter))
    return _stream_response(emitter, runner_task, heartbeat_task)


# ===========================================================================
# 3. FASTAPI APP — middleware, exception handlers, startup
# ===========================================================================

def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


configure_logging()
_app_log = logging.getLogger("agentic_ai")

init_sentry()

app = FastAPI(
    title="Agentic AI",
    description="Local-first single-user analytics — no authentication.",
    version="3.1.0-no-auth",
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

app.include_router(api_router)

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
    from app.infrastructure import init_database, load_synonyms
    from app.vector import register_vocabulary

    await init_database()
    _app_log.info("database engine: %s", engine_kind())

    registry = get_registry()
    _app_log.info("registry: %d tools registered: %s", len(registry.names), registry.names)
    _app_log.info("financial DB ready at %s", settings.financial_db_path)

    try:
        syns = load_synonyms()
        if syns:
            n = register_vocabulary("entity", syns)
            _app_log.info(
                "vector vocabulary bootstrapped: %d canonicals from synonyms.json", n,
            )
    except Exception:
        _app_log.exception("vector vocabulary bootstrap failed (continuing without)")

    _app_log.info("sentry: %s", sentry_status())
    _app_log.info("auth: DISABLED — all routes public")


@app.on_event("shutdown")
async def _shutdown() -> None:
    return
