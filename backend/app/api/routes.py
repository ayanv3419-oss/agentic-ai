"""HTTP surface — /upload, /dashboard, /query_stream, /uploads, /health, /cache/clear.

Contracts are preserved exactly so the existing frontend types
(UploadResponse, DashboardData, AuthMe, SseEvent) keep working.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.agents.dashboard_agent import DashboardAgent
from app.agents.dataclean_agent import DataCleanAgent
from app.cache import cache_size, invalidate_all
from app.config import settings
from app.coordinator import run_query_turn
from app.database import (
    ALLOWED_TABLES,
    count_rows,
    disconnect_upload,
    get_upload_meta,
    list_uploads_meta,
    record_upload_meta,
)
from app.errors import envelope, SAFE_MESSAGE
from app.llm import GroqClient, set_request_groq, reset_request_groq
from app.state import TurnState
from app.streaming import EventEmitter, format_sse
from app.upload import UploadError

log = logging.getLogger("agentic_ai.api")
router = APIRouter()


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

@router.get("/health")
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

@router.post("/upload")
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
        from pathlib import Path
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

@router.get("/dashboard")
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

@router.get("/uploads")
async def uploads():
    return {
        "uploads": await list_uploads_meta(),
        "total_rows": {
            "sales":    await count_rows("sales"),
            "purchase": await count_rows("purchase"),
        },
    }


@router.post("/uploads/{batch_id}/disconnect")
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

@router.post("/cache/clear")
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


@router.post("/query_stream")
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
