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
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

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
from app.dynamic_ingest import (
    drop_all_dynamic_tables,
    ingest_workbook,
    list_dynamic_tables,
    reconcile_registry,
)
from app.database import engine_kind, engine_status
from app.kpi import (
    calculate_by_name,
    disable_kpi as kpi_disable,
    enable_kpi as kpi_enable,
    execute_kpi,
    get_kpi,
    init_kpi_table,
    list_kpis,
    match_kpi,
    rebuild_catalog,
    seed_default_catalog,
)
from app.time_engine import (
    invalidate_cache as invalidate_time_cache,
    resolve_dataset_date_tokens,
)
from app.hierarchy import (
    V2_LEVELS,
    create_branch,
    list_branches,
    list_location_hierarchy,
    list_product_hierarchy,
    list_product_master,
    list_sku_master,
    list_v2_tree,
    seed_default_business,
    sync_product_master_from_data,
    sync_product_sku_master,
    v2_drilldown,
)
from app.enrichment import (
    backfill_missing_product_names,
    backfill_quantities,
    cost_master_snapshot,
    forecast_summary,
    inventory_snapshot,
    list_forecast_for_sku,
    list_inventory,
    list_product_costs,
    mock_backfill_stats,
    refresh_forecast,
    refresh_inventory,
    refresh_product_costs,
)
from app.infrastructure import (
    ALLOWED_TABLES,
    SAFE_MESSAGE,
    UploadError,
    archive_upload,
    bump_data_version,
    cache_size,
    count_rows,
    disconnect_upload,
    envelope,
    find_active_upload_by_file_hash,
    get_data_version,
    invalidate_all,
    list_uploads_meta,
    record_upload_meta,
    settings,
    unarchive_upload,
    uploads_dir,
)
from app.dedup import DEDUP_MODES, DEFAULT_DEDUP_MODE, compute_file_hash
from app import google_drive
from app.errors import (
    SEVERITIES,
    error_analytics,
    get_error,
    list_errors,
    log_error,
    resolve_error,
)
from app.monitoring import (
    init_sentry,
    instrument_fastapi,
    sentry_status,
    set_request_context,
)

# v2 orchestrator — gated by ORCHESTRATOR_VERSION env / X-Orchestrator-Version
# header. Imports are unconditional; the package loads cleanly even when v1 is
# the only path actually exercised at runtime.
from app.orchestrator_v2 import run_query_turn_v2
from app.orchestrator_v2.state import RequestContext as V2RequestContext


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


# ---------------------------------------------------------------------------
# Google Drive integration
#
# Single-user local-first OAuth. The app itself has no startup login — these
# routes power the "Connect Google Drive" card on the Upload page and let the
# user sync data files straight from Drive through the same DataCleanAgent
# ingestion pipeline as manual uploads. All Drive/OAuth logic lives in
# app/google_drive.py; these routes are thin adapters. The whole feature
# stays inert (login returns 501) until GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
# are set in backend/.env.
# ---------------------------------------------------------------------------

class DriveSyncRequest(BaseModel):
    file_ids: list[str] = Field(default_factory=list)
    target: str = "sales"
    dedup_mode: str = "skip"


def _drive_not_configured() -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content=envelope(
            "Google Drive integration not configured",
            detail="Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in "
                   "backend/.env (OAuth client from Google Cloud Console) "
                   "to enable Drive sync.",
            kind="not_implemented",
        ),
    )


# ---------------------------------------------------------------------------
# App-login gate (matches the frontend's LoginGate component)
# ---------------------------------------------------------------------------
# This is NOT real auth — the platform is single-user local-first. The
# endpoint exists so the frontend's POST /auth/login resolves with a 2xx
# instead of a 404. Credentials are env-overridable; defaults match what
# the frontend ships with so a fresh deploy works without env edits.

_APP_LOGIN_USER = os.environ.get("APP_LOGIN_USER", "Mansuri").strip()
_APP_LOGIN_PASSWORD = os.environ.get("APP_LOGIN_PASSWORD", "182012")


class LoginRequest(BaseModel):
    """Permissive login body — accepts a couple of common field names so
    different frontend builds slot in cleanly without a backend redeploy."""

    model_config = ConfigDict(extra="ignore")

    username: str | None = Field(default=None, max_length=120)
    user:     str | None = Field(default=None, max_length=120)
    password: str | None = Field(default=None, max_length=200)
    pass_:    str | None = Field(default=None, alias="pass", max_length=200)


@api_router.post("/auth/login")
async def auth_login(req: LoginRequest, request: Request) -> dict:
    """
    App-level login endpoint. Accepts ``{username, password}`` (also
    accepts ``{user, pass}`` as aliases for tolerance). Validates against
    ``APP_LOGIN_USER`` / ``APP_LOGIN_PASSWORD`` env vars (defaults:
    ``Mansuri`` / ``182012``).

    On success returns ``{ok, username, token}``. The token is a short
    opaque string the frontend may forward as ``Authorization: Bearer``
    if it wants — the backend doesn't currently enforce it (single-user
    local-first), but having a token-shaped response keeps the wire
    contract future-compatible.

    Rate-limited per client IP to mitigate brute force.
    """
    rate_error = _rate_limit_check(_client_ip(request))
    if rate_error is not None:
        return JSONResponse(status_code=429, content=rate_error)

    username = (req.username or req.user or "").strip()
    password = req.password or req.pass_ or ""

    if not username or not password:
        return JSONResponse(
            status_code=400,
            content=envelope(
                "Missing credentials",
                detail="Both `username` and `password` are required.",
                kind="validation",
            ),
        )

    # Username is case-insensitive; password is exact.
    if (
        username.lower() != _APP_LOGIN_USER.lower()
        or password != _APP_LOGIN_PASSWORD
    ):
        log.info("auth/login: rejected attempt for user=%r", username[:40])
        return JSONResponse(
            status_code=401,
            content=envelope(
                "Invalid credentials",
                detail="Username or password is incorrect.",
                kind="auth",
            ),
        )

    # Mint a short opaque token. Not enforced server-side today; the
    # frontend may store it and echo it back as Authorization for future
    # multi-tenant work.
    token = uuid4().hex
    log.info("auth/login: success for user=%r", username[:40])
    return {
        "ok": True,
        "authenticated": True,
        "username": _APP_LOGIN_USER,
        "token": token,
    }


@api_router.get("/auth/me")
async def auth_me() -> dict:
    """Google Drive auth status for the Upload page card."""
    try:
        connected = await asyncio.to_thread(google_drive.is_connected)
    except Exception:
        log.warning("drive is_connected check failed", exc_info=True)
        connected = False
    return {
        "authenticated": connected,
        "google_configured": google_drive.is_configured(),
    }


@api_router.post("/auth/logout")
async def auth_logout() -> dict:
    """Disconnect Google Drive — revoke + delete the local OAuth token."""
    try:
        await asyncio.to_thread(google_drive.revoke_credentials)
    except Exception:
        log.warning("drive revoke failed", exc_info=True)
    return {"ok": True}


@api_router.get("/auth/google/login")
async def google_login():
    """Redirect the browser to Google's OAuth consent screen."""
    if not google_drive.is_configured():
        return _drive_not_configured()
    try:
        url, _state = google_drive.get_authorization_url()
    except Exception as e:
        log.exception("drive: building auth url failed")
        return JSONResponse(status_code=500, content=envelope(
            "Could not start Google sign-in",
            detail=f"{type(e).__name__}: {e}", kind="internal",
        ))
    return RedirectResponse(url)


@api_router.get("/auth/google/callback")
async def google_callback(request: Request):
    """OAuth callback — exchange the code for credentials, then bounce the
    browser back to the frontend Upload page."""
    if not google_drive.is_configured():
        return _drive_not_configured()
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")
    if error or not code:
        return RedirectResponse(f"{settings.frontend_url}/?drive=error")
    try:
        await asyncio.to_thread(google_drive.exchange_code, code, state)
    except Exception as e:
        log.exception("drive: token exchange failed")
        return RedirectResponse(f"{settings.frontend_url}/?drive=error")
    return RedirectResponse(f"{settings.frontend_url}/?drive=connected")


@api_router.get("/drive/status")
async def drive_status():
    """List the user's ingestible Drive files for the frontend file picker."""
    if not google_drive.is_configured():
        return {"connected": False, "configured": False, "files": []}
    try:
        creds = await asyncio.to_thread(google_drive.load_credentials)
    except Exception:
        creds = None
    if creds is None:
        return {"connected": False, "configured": True, "files": []}
    try:
        files = await google_drive.list_drive_files(creds)
    except Exception as e:
        log.exception("drive: listing files failed")
        return JSONResponse(status_code=502, content=envelope(
            "Could not list Google Drive files",
            detail=f"{type(e).__name__}: {e}", kind="upstream",
        ))
    return {"connected": True, "configured": True, "files": files}


@api_router.post("/drive/sync")
async def drive_sync(request: Request, body: DriveSyncRequest):
    """Ingest the selected Drive files through the DataCleanAgent pipeline."""
    rl = _rate_limit_check(_client_ip(request), bucket_namespace="drive", limit_per_minute=5)
    if rl is not None:
        return JSONResponse(status_code=429, content=rl)
    if not google_drive.is_configured():
        return _drive_not_configured()

    target_table = (body.target or "sales").strip().lower()
    if target_table not in ALLOWED_TABLES:
        return JSONResponse(status_code=400, content=envelope(
            "Invalid target",
            detail=f"target must be one of {list(ALLOWED_TABLES)}",
            kind="validation",
        ))
    if body.dedup_mode not in DEDUP_MODES:
        return JSONResponse(status_code=400, content=envelope(
            "Invalid dedup_mode",
            detail=f"dedup_mode must be one of {list(DEDUP_MODES)}",
            kind="validation",
        ))
    if not body.file_ids:
        return JSONResponse(status_code=400, content=envelope(
            "No files selected",
            detail="file_ids must contain at least one Drive file id.",
            kind="validation",
        ))

    creds = await asyncio.to_thread(google_drive.load_credentials)
    if creds is None:
        return JSONResponse(status_code=401, content=envelope(
            "Google Drive not connected",
            detail="Connect Google Drive on the Upload page first.",
            kind="auth",
        ))

    try:
        results = await google_drive.ingest_drive_files(
            creds, body.file_ids, target_table, body.dedup_mode,
        )
    except Exception as e:
        log.exception("drive: sync failed")
        return JSONResponse(status_code=500, content=envelope(
            "Drive sync failed",
            detail=f"{type(e).__name__}: {e}", kind="internal",
        ))
    return {
        "target": target_table,
        "dedup_mode": body.dedup_mode,
        "results": results,
        "rows_inserted_total": sum(r.get("rows_inserted", 0) for r in results),
    }


# ---------------------------------------------------------------------------
# KPI Registry API
# Centralized formula engine — see app/kpi/*. The AI fast-path in
# run_query_turn also reads this registry; these routes give the dashboard
# and external tooling a direct interface.
# ---------------------------------------------------------------------------

@api_router.get("/kpi")
async def kpi_list(category: str | None = None, enabled_only: bool = True):
    rows = await list_kpis(category=category, enabled_only=enabled_only)
    return {
        "count": len(rows),
        "kpis": [
            {
                "id":                 r.id,
                "kpi_name":           r.kpi_name,
                "kpi_category":       r.kpi_category,
                "description":        r.description,
                "formula_expression": r.formula_expression,
                "required_columns":   r.required_columns,
                "aggregation_type":   r.aggregation_type,
                "output_type":        r.output_type,
                "chart_supported":    r.chart_supported,
                "aliases":            r.aliases,
                "enabled":            r.enabled,
            }
            for r in rows
        ],
    }


@api_router.get("/kpi/match")
async def kpi_match_route(question: str):
    """Resolve a natural-language question to a registered KPI."""
    m = await match_kpi(question)
    if m is None:
        return {"matched": False, "question": question}
    return {
        "matched":         True,
        "question":        question,
        "kpi_id":          m.kpi.id,
        "kpi_name":        m.kpi.kpi_name,
        "confidence":      m.confidence,
        "matched_alias":   m.matched_alias,
        "reason":          m.reason,
    }


@api_router.get("/kpi/{kpi_id}")
async def kpi_get(kpi_id: str):
    kpi = await get_kpi(kpi_id)
    if kpi is None:
        return JSONResponse(status_code=404, content=envelope(
            "KPI not found", detail=f"unknown id/name: {kpi_id!r}", kind="validation",
        ))
    return {
        "id":                 kpi.id,
        "kpi_name":           kpi.kpi_name,
        "kpi_category":       kpi.kpi_category,
        "description":        kpi.description,
        "formula_expression": kpi.formula_expression,
        "required_columns":   kpi.required_columns,
        "sql_template":       kpi.sql_template,
        "aggregation_type":   kpi.aggregation_type,
        "output_type":        kpi.output_type,
        "chart_supported":    kpi.chart_supported,
        "aliases":            kpi.aliases,
        "enabled":            kpi.enabled,
        "created_at":         kpi.created_at,
        "updated_at":         kpi.updated_at,
    }


@api_router.post("/kpi/{kpi_id}/calculate")
async def kpi_calculate(kpi_id: str):
    """Execute a KPI by id or display name. Returns a structured result —
    error population on the same payload (never throws to the client)."""
    res = await calculate_by_name(kpi_id)
    if res.error:
        return JSONResponse(status_code=400, content=res.to_dict())
    return res.to_dict()


@api_router.post("/kpi/{kpi_id}/disable")
async def kpi_disable_route(kpi_id: str):
    ok = await kpi_disable(kpi_id)
    if not ok:
        return JSONResponse(status_code=404, content=envelope(
            "KPI not found", detail=f"unknown id: {kpi_id!r}", kind="validation",
        ))
    # KPI definition changed -> any cached chat answer that resolved this
    # KPI is now stale. Bump version + drop the response cache.
    bump_data_version()
    invalidate_all()
    return {"ok": True, "kpi_id": kpi_id, "enabled": False}


@api_router.post("/kpi/{kpi_id}/enable")
async def kpi_enable_route(kpi_id: str):
    ok = await kpi_enable(kpi_id)
    if not ok:
        return JSONResponse(status_code=404, content=envelope(
            "KPI not found", detail=f"unknown id: {kpi_id!r}", kind="validation",
        ))
    bump_data_version()
    invalidate_all()
    return {"ok": True, "kpi_id": kpi_id, "enabled": True}


@api_router.post("/kpi/rebuild")
async def kpi_rebuild():
    """Force-reseed every KPI in the default catalog. User-added KPIs are
    left untouched."""
    n = await rebuild_catalog()
    return {"ok": True, "rewritten": n}


# ---------------------------------------------------------------------------
# Hierarchy API — product + location inspection and branch management
# ---------------------------------------------------------------------------

@api_router.get("/hierarchy/products")
async def hierarchy_products():
    """Return the product hierarchy tree + the product → hierarchy mapping
    table. Both are populated from the currently uploaded data only."""
    return {
        "hierarchy": await list_product_hierarchy(),
        "products":  await list_product_master(),
    }


@api_router.post("/hierarchy/products/sync")
async def hierarchy_products_sync():
    """Rebuild product_master by scanning distinct Product Name values."""
    stats = await sync_product_master_from_data()
    # Hierarchy was rebuilt -> any cached drill-down / category answer
    # is stale. Bump version + drop the response cache.
    bump_data_version()
    invalidate_all()
    return {"ok": True, **stats}


# ---------------------------------------------------------------------------
# Enrichment API — Inventory + Forecast (derived from real sales)
# ---------------------------------------------------------------------------

@api_router.get("/enrichment/costs")
async def enrichment_costs_list(limit: int = 500):
    """View the per-product unit cost master (deterministic mock + user manual)."""
    return {
        "count": (await cost_master_snapshot()).get("total", 0),
        "snapshot": await cost_master_snapshot(),
        "items": await list_product_costs(limit=limit),
    }


@api_router.post("/enrichment/costs/refresh")
async def enrichment_costs_refresh():
    """Force-regenerate the synthetic cost master + quantity backfill.
    User-supplied (source='manual') cost rows are preserved."""
    cost_stats = await refresh_product_costs()
    qty_stats  = await backfill_quantities()
    # Cost / margin computations referenced by cached chat answers are
    # now stale -> bump version + drop cache.
    bump_data_version()
    invalidate_all()
    return {"ok": True, "costs": cost_stats, "quantities": qty_stats}


@api_router.get("/enrichment/mock-stats")
async def enrichment_mock_stats():
    """How many sales/purchase rows have been mock-named vs real-named.
    Useful for the user to audit how much of the analytics is backed by
    real product attribution vs deterministic backfill."""
    return await mock_backfill_stats()


@api_router.post("/enrichment/backfill-products")
async def enrichment_backfill():
    """Manually trigger the mock-product-name backfill. Idempotent — rows
    with a real product name are never touched. After backfill the
    standard hierarchy + inventory + forecast sync chain runs so the new
    names land in every downstream table immediately."""
    fill_stats = await backfill_missing_product_names()
    sync_stats = await sync_product_master_from_data()
    v2_stats   = await sync_product_sku_master()
    inv_stats  = await refresh_inventory()
    fc_stats   = await refresh_forecast()
    return {
        "ok": True,
        "filled":          fill_stats,
        "hierarchy_v1":    sync_stats,
        "hierarchy_v2":    v2_stats,
        "inventory":       inv_stats,
        "forecast":        fc_stats,
    }


@api_router.get("/inventory/snapshot")
async def inventory_snapshot_route():
    """Headline counts of inventory health (ok / low / overstocked / dead)."""
    return await inventory_snapshot()


@api_router.get("/inventory")
async def inventory_list(status: str | None = None):
    """Per-SKU inventory with status, on-hand qty, velocity, days of cover.
    Optional `?status=low` filter."""
    rows = await list_inventory(status=status)
    return {"count": len(rows), "items": rows}


@api_router.post("/inventory/refresh")
async def inventory_refresh_route():
    """Force-recompute the inventory snapshot from real sales velocity."""
    result = await refresh_inventory()
    # Inventory rows changed -> any cached inventory / "what's low?"
    # answer is stale. Bump version + drop the response cache.
    bump_data_version()
    invalidate_all()
    return {"ok": True, **result}


@api_router.get("/forecast/summary")
async def forecast_summary_route():
    """Aggregated 7-day and 30-day revenue projection."""
    return await forecast_summary()


@api_router.get("/forecast/sku/{sku_code}")
async def forecast_sku_route(sku_code: str):
    """Per-SKU 14-day forecast."""
    rows = await list_forecast_for_sku(sku_code)
    return {"sku_code": sku_code, "horizon_days": len(rows), "forecast": rows}


@api_router.post("/forecast/refresh")
async def forecast_refresh_route():
    """Force-recompute the 14-day per-SKU forecast."""
    result = await refresh_forecast()
    # Forecast rows changed -> any cached forecast answer is stale.
    bump_data_version()
    invalidate_all()
    return {"ok": True, **result}


@api_router.get("/hierarchy/v2/tree")
async def hierarchy_v2_tree():
    """The full 6-level synthetic enterprise hierarchy + SKU master.
    Strictly additive — never overlaps with the v1 hierarchy endpoints."""
    return {
        "levels":   list(V2_LEVELS) + ["item"],
        "tree":     await list_v2_tree(),
        "skus":     await list_sku_master(),
    }


@api_router.post("/hierarchy/v2/sync")
async def hierarchy_v2_sync():
    """Rebuild product_sku_master + product_hierarchy_v2 from the current
    sales/purchase rows. Idempotent: existing SKU codes stay stable."""
    result = await sync_product_sku_master()
    # Hierarchy v2 changed -> cached drill-down answers are stale.
    bump_data_version()
    invalidate_all()
    return {"ok": True, **result}


@api_router.get("/hierarchy/v2/drilldown")
async def hierarchy_v2_drilldown_route(
    level: str, parent_id: str | None = None,
):
    if level not in (*V2_LEVELS, "item"):
        return JSONResponse(status_code=400, content=envelope(
            "Invalid level",
            detail=f"level must be one of {list(V2_LEVELS) + ['item']}",
            kind="validation",
        ))
    if level == "item":
        # Items live in product_sku_master keyed by parent type_id.
        from app.infrastructure import fetch_all as _fa
        if parent_id is None:
            rows = await _fa(
                "SELECT id, sku_code, product_name FROM product_sku_master "
                "ORDER BY sku_code"
            )
        else:
            rows = await _fa(
                "SELECT id, sku_code, product_name FROM product_sku_master "
                "WHERE type_id = ? ORDER BY sku_code",
                (parent_id,),
            )
        return {"level": "item", "parent_id": parent_id, "items": rows}
    return {
        "level":     level,
        "parent_id": parent_id,
        "nodes":     await v2_drilldown(level, parent_id),
    }


@api_router.get("/hierarchy/locations")
async def hierarchy_locations():
    return {
        "hierarchy": await list_location_hierarchy(),
        "branches":  await list_branches(enabled_only=False),
    }


class CreateBranchRequest(BaseModel):
    branch_name: str = Field(..., min_length=1, max_length=200)
    city: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=500)


@api_router.post("/hierarchy/branches")
async def hierarchy_branch_create(req: CreateBranchRequest):
    try:
        return await create_branch(req.branch_name, city=req.city, address=req.address)
    except ValueError as e:
        return JSONResponse(status_code=400, content=envelope(
            "Branch validation failed", detail=str(e), kind="validation",
        ))
    except Exception as e:
        log.exception("create_branch failed")
        return JSONResponse(status_code=500, content=envelope(
            "Branch creation failed",
            detail=f"{type(e).__name__}: {e}",
            kind="internal",
        ))


# ---------------------------------------------------------------------------
# Time engine diagnostics
# Returns the current dataset-relative tokens. The AI uses these for every
# relative-time KPI; this route surfaces them for the dashboard / debugging.
# ---------------------------------------------------------------------------

@api_router.get("/time")
async def time_tokens():
    tokens = await resolve_dataset_date_tokens()
    if not tokens:
        return {
            "has_data": False,
            "message": "No uploaded data — relative-time analytics are disabled until first upload.",
        }
    return {
        "has_data": True,
        "dataset_today":     tokens.get("dataset_today"),
        "dataset_yesterday": tokens.get("dataset_yesterday"),
        "dataset_month":     tokens.get("dataset_month"),
        "dataset_year":      tokens.get("dataset_year"),
        "dataset_prev_month": tokens.get("dataset_prev_month"),
        "last_7_window":    [tokens.get("dataset_last_7_start"),    tokens.get("dataset_today")],
        "last_30_window":   [tokens.get("dataset_last_30_start"),   tokens.get("dataset_today")],
        "this_week":        [tokens.get("dataset_week_start"),      tokens.get("dataset_week_end")],
        "this_month":       [tokens.get("dataset_month_start"),     tokens.get("dataset_month_end")],
        "previous_month":   [tokens.get("dataset_prev_month_start"), tokens.get("dataset_prev_month_end")],
        "previous_week":    [tokens.get("dataset_prev_week_start"), tokens.get("dataset_prev_week_end")],
        "tokens": tokens,
    }


# ---------------------------------------------------------------------------
# Error tracking API
# Centralized error log: every uncaught exception, upload failure, AI/KPI
# crash, and explicitly-reported frontend error lands in error_log.
# ---------------------------------------------------------------------------

@api_router.get("/errors")
async def errors_list(
    module: str | None = None,
    severity: str | None = None,
    resolved: bool | None = None,
    limit: int = 200,
    offset: int = 0,
):
    if severity is not None and severity not in SEVERITIES:
        return JSONResponse(status_code=400, content=envelope(
            "Invalid severity",
            detail=f"severity must be one of {list(SEVERITIES)}",
            kind="validation",
        ))
    rows = await list_errors(
        module=module, severity=severity, resolved=resolved,
        limit=limit, offset=offset,
    )
    return {"count": len(rows), "errors": rows}


@api_router.get("/errors/analytics")
async def errors_analytics():
    return await error_analytics()


@api_router.get("/errors/{error_id}")
async def errors_get(error_id: str):
    row = await get_error(error_id)
    if row is None:
        return JSONResponse(status_code=404, content=envelope(
            "Error not found", detail=f"unknown error_id: {error_id!r}",
            kind="validation",
        ))
    # Parse JSON fields for the consumer.
    import json as _json
    for col in ("request_payload", "context"):
        v = row.get(col)
        if isinstance(v, str) and v:
            try:
                row[col] = _json.loads(v)
            except Exception:
                pass
    return row


@api_router.post("/errors/{error_id}/resolve")
async def errors_resolve(error_id: str, note: str | None = None):
    ok = await resolve_error(error_id, note=note)
    if not ok:
        return JSONResponse(status_code=404, content=envelope(
            "Error not found", detail=f"unknown error_id: {error_id!r}",
            kind="validation",
        ))
    return {"ok": True, "error_id": error_id, "resolved": True}


class FrontendErrorReport(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    module: str = Field(default="frontend", max_length=80)
    severity: str | None = Field(default=None)
    source: str | None = Field(default=None, max_length=400)
    stack: str | None = Field(default=None, max_length=20_000)
    url: str | None = Field(default=None, max_length=2000)
    user_agent: str | None = Field(default=None, max_length=600)
    context: dict | None = Field(default=None)


@api_router.post("/errors/report")
async def errors_report(req: FrontendErrorReport):
    """Frontend error-boundary reporter. UI crashes / fetch failures /
    chart-render exceptions all POST here."""
    sev = req.severity if req.severity in SEVERITIES else "medium"
    # Stack arrived as a plain string from the browser; pass it through
    # the `context` field so it shows in the dashboard but doesn't pretend
    # to be a Python traceback.
    ctx = dict(req.context or {})
    if req.stack:
        ctx["js_stack"] = req.stack
    if req.url:
        ctx["url"] = req.url
    if req.user_agent:
        ctx["user_agent"] = req.user_agent
    eid = log_error(
        message=req.message,
        module=req.module or "frontend",
        severity=sev,
        source=req.source or "frontend",
        user_facing=True,
        context=ctx,
    )
    return {"ok": True, "error_id": eid}


@api_router.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        # Pull from the FastAPI app so a version bump in one place
        # (the FastAPI(...) constructor) propagates here automatically
        # instead of drifting like the previous hardcoded string did.
        "version": app.version,
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
# Shared post-ingest refresh — runs after ANY ingestion path (file upload or
# Google Drive sync) so derived state stays consistent: data version bump,
# cache invalidation, product-name backfill, hierarchy v1/v2 sync, inventory
# + forecast + cost refresh, quantity backfill. Every step is best-effort —
# a failure in one is logged and the rest still run. Returns the new data
# version.
# ---------------------------------------------------------------------------

async def _post_ingest_refresh() -> int:
    refresh_log = logging.getLogger("agentic_ai.api.ingest")
    new_version = bump_data_version()
    invalidate_all()
    # Invalidate the dataset-relative time cache so the next analytics call
    # recomputes MAX(Date) against the freshly inserted rows.
    invalidate_time_cache()
    # Mock product-name backfill — fill any rows whose Product Name came in
    # blank with a deterministic footwear name. MUST run before hierarchy
    # sync so the new names get classified into the v1 + v2 trees this cycle.
    try:
        mock_stats = await backfill_missing_product_names()
        if any(v > 0 for v in mock_stats.values()):
            refresh_log.info("mock product backfill: %s", mock_stats)
    except Exception:
        refresh_log.warning("mock product backfill failed (continuing)", exc_info=True)
    # Re-sync product hierarchy from the now-updated sales/purchase tables.
    try:
        sync_stats = await sync_product_master_from_data()
        refresh_log.info("product hierarchy v1 synced: %s", sync_stats)
    except Exception:
        refresh_log.warning("product hierarchy v1 sync failed (continuing)", exc_info=True)
    # v2 sync — enterprise 6-level hierarchy. Additive; v1 stays intact.
    try:
        v2_stats = await sync_product_sku_master()
        refresh_log.info("product hierarchy v2 synced: %s", v2_stats)
    except Exception:
        refresh_log.warning("product hierarchy v2 sync failed (continuing)", exc_info=True)
    # Enrichment refresh — inventory + forecast derived from real sales.
    try:
        inv_stats = await refresh_inventory()
        refresh_log.info("inventory refreshed: %s", inv_stats)
    except Exception:
        refresh_log.warning("inventory refresh failed (continuing)", exc_info=True)
    try:
        fc_stats = await refresh_forecast()
        refresh_log.info("forecast refreshed: %s", fc_stats)
    except Exception:
        refresh_log.warning("forecast refresh failed (continuing)", exc_info=True)
    # Cost master + quantity backfill — feeds profit / margin / unit velocity
    # KPIs. Costs depend on product+line classification so this runs AFTER
    # hierarchy v2 sync.
    try:
        cost_stats = await refresh_product_costs()
        refresh_log.info("product costs refreshed: %s", cost_stats)
        qty_stats = await backfill_quantities()
        refresh_log.info("quantity backfill: %s", qty_stats)
    except Exception:
        refresh_log.warning("cost/quantity refresh failed (continuing)", exc_info=True)
    return new_version


# ---------------------------------------------------------------------------
# /upload  (DataCleanAgent)
# ---------------------------------------------------------------------------

@api_router.post("/upload")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    target: str = Form("sales"),
    dedup_mode: str = Form(DEFAULT_DEDUP_MODE),
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

    if dedup_mode not in DEDUP_MODES:
        return JSONResponse(status_code=400, content=envelope(
            "Invalid dedup_mode",
            detail=f"dedup_mode must be one of {list(DEDUP_MODES)}",
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

    # Persist the source file under data/uploads/{batch_id}{suffix} — kept
    # forever until the user explicitly disconnects this dataset.
    persistent_path = uploads_dir() / f"{batch_id}{suffix}"
    bytes_written = 0
    crashed = False
    try:
        try:
            with persistent_path.open("wb") as out:
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
                        crashed = True
                        return JSONResponse(status_code=413, content=envelope(
                            "File too large",
                            detail=f"Max {settings.max_upload_bytes // (1024*1024)} MB per upload.",
                            kind="upload",
                        ))
                    out.write(chunk)
        except Exception as e:
            upload_log.exception("spool: failed")
            await _record_error(f"spool failed: {type(e).__name__}: {e}", target_table)
            crashed = True
            return JSONResponse(status_code=400, content=envelope(
                "Could not read upload",
                detail=f"{type(e).__name__}: {e}",
                kind="upload",
            ))

        if bytes_written == 0:
            await _record_error("empty file", target_table)
            crashed = True
            return JSONResponse(status_code=400, content=envelope(
                "Empty file", kind="upload",
            ))

        # File-level duplicate detection: SHA256 the spooled bytes and look
        # up any existing active upload with the same hash. On collision +
        # dedup_mode='block', refuse with a 409 so the user explicitly
        # chooses how to proceed.
        file_hash, file_bytes_total = compute_file_hash(persistent_path)
        existing = await find_active_upload_by_file_hash(file_hash)
        if existing is not None and dedup_mode == "block":
            crashed = True   # delete the just-spooled redundant copy
            return JSONResponse(status_code=409, content=envelope(
                "Duplicate file already on record",
                detail=(
                    f"This exact file was already uploaded as batch "
                    f"{existing.get('batch_id')!r} on {existing.get('uploaded_at')}. "
                    f"Re-upload with dedup_mode=skip / replace / append to override."
                ),
                kind="duplicate",
                extra={
                    "existing_batch_id":  existing.get("batch_id"),
                    "existing_filename":  existing.get("filename"),
                    "existing_target":    existing.get("target"),
                    "existing_uploaded_at": existing.get("uploaded_at"),
                    "existing_rows_inserted": existing.get("rows_inserted"),
                    "file_hash":          file_hash,
                    "options": ["skip", "replace", "append"],
                },
            ))

        agent = DataCleanAgent()
        try:
            result = await agent.run(
                tmp_path=persistent_path,
                filename=filename,
                target=target_table,
                batch_id=batch_id,
                dedup_mode=dedup_mode,
            )
        except UploadError as e:
            await _record_error(f"bad upload: {e}", target_table)
            log_error(
                exc=e, module="upload", severity="high",
                endpoint="/upload", method="POST", user_facing=True,
                source="DataCleanAgent",
                context={"target": target_table, "batch_id": batch_id, "filename": filename},
            )
            crashed = True
            return JSONResponse(status_code=400, content=envelope(
                "Bad upload", detail=str(e), kind="upload",
            ))
        except Exception as e:
            upload_log.exception("dataclean: crashed")
            await _record_error(f"{type(e).__name__}: {e}", target_table)
            log_error(
                exc=e, module="upload", severity="critical",
                endpoint="/upload", method="POST", user_facing=False,
                source="DataCleanAgent",
                context={"target": target_table, "batch_id": batch_id, "filename": filename},
            )
            crashed = True
            return JSONResponse(status_code=500, content=envelope(
                "Ingest failed",
                detail=f"{type(e).__name__}: {e}",
                kind="internal",
            ))

        # Stamp persisted path + file hash onto the uploads metadata row.
        # DataCleanAgent already inserted the success record; we patch
        # these audit fields on top so disconnect_upload + the duplicate
        # detector can find them later.
        try:
            from app.infrastructure import get_connection
            async with get_connection() as db:
                await db.execute(
                    "UPDATE uploads SET file_path = ?, file_hash = ?, "
                    "file_bytes = ? WHERE batch_id = ?",
                    (str(persistent_path), file_hash, file_bytes_total, batch_id),
                )
                await db.commit()
        except Exception:
            upload_log.warning("could not stamp file_path on uploads row", exc_info=True)

        new_version = await _post_ingest_refresh()
        upload_log.info(
            "upload ok rows=%d batch=%s file=%s data_version=%d",
            result["rows_inserted"], batch_id, persistent_path.name, new_version,
        )

        return {
            "batch_id":          result["batch_id"],
            "filename":          result["filename"],
            "target":            result["target"],
            "rows_inserted":     result["rows_inserted"],
            "rows_failed":       result["rows_failed"],
            "rows_skipped_duplicate": result.get("rows_skipped_duplicate", 0),
            "rows_replaced":     result.get("rows_replaced", 0),
            "dedup_mode":        result.get("dedup_mode", dedup_mode),
            "dedup":             result.get("dedup"),
            "errors":            result["errors"],
            "summary":           result["summary"],
            "unmatched_headers": result["unmatched_headers"],
            "sheet_name":        result.get("sheet_name"),
            "header_row_used":   result.get("header_row_used"),
            "validation":        result.get("validation"),
            "bytes_received":    bytes_written,
            "table_total":       await count_rows(target_table),
            "file_path":         str(persistent_path),
            "file_hash":         file_hash,
            # Stamp the post-ingest data_version so the frontend can
            # mark its cached Dashboard / Uploads list as stale and
            # auto-refetch without polling.
            "data_version":      new_version,
        }
    except Exception as e:
        upload_log.exception("upload: unhandled crash")
        crashed = True
        return JSONResponse(status_code=500, content=envelope(
            "Upload failed",
            detail=f"{type(e).__name__}: {e}",
            kind="internal",
        ))
    finally:
        # On any error path, clean up the partial file from data/uploads/ so
        # the directory doesn't accumulate orphans. Successful uploads keep
        # the file — that's the whole point of persistence.
        if crashed:
            try:
                if persistent_path.exists():
                    persistent_path.unlink()
            except Exception:
                log.warning("cleanup of failed upload file failed: %s", persistent_path, exc_info=True)


# ---------------------------------------------------------------------------
# /upload_workbook — multi-sheet dynamic ingestion (ADR-0005)
# ---------------------------------------------------------------------------
# Drops the whole .xlsx, each sheet becomes its own `u_<sheet>` table.
# Bypasses the strict sales/purchase SCHEMA_SPEC — column types are inferred
# from the data, no alias mapping required. Re-uploading replaces the per-
# sheet tables. Used by the "drop a workbook" flow on the Upload page when
# the user has multi-domain data (sales + inventory + product catalog +
# hierarchy) that doesn't fit the legacy two-table model.

@api_router.post("/upload_workbook")
async def upload_workbook(
    request: Request,
    file: UploadFile = File(...),
):
    upload_log = logging.getLogger("agentic_ai.api.upload_workbook")
    rl = _rate_limit_check(_client_ip(request), bucket_namespace="upload", limit_per_minute=5)
    if rl is not None:
        return JSONResponse(status_code=429, content=rl)

    filename = file.filename or "upload"
    batch_id = str(uuid4())

    lower = filename.lower()
    if not lower.endswith(".xlsx"):
        return JSONResponse(status_code=400, content=envelope(
            "Unsupported file type",
            detail="/upload_workbook only accepts .xlsx files (multi-sheet). "
                   "Use /upload for single-sheet xlsx or csv.",
            kind="upload",
        ))

    # Spool to disk under data/uploads/{batch_id}.xlsx — kept until disconnect.
    persistent_path = uploads_dir() / f"{batch_id}.xlsx"
    bytes_written = 0
    crashed = False
    try:
        try:
            with persistent_path.open("wb") as out:
                while True:
                    chunk = await file.read(settings.upload_chunk_bytes)
                    if not chunk:
                        break
                    bytes_written += len(chunk)
                    if bytes_written > settings.max_upload_bytes:
                        crashed = True
                        return JSONResponse(status_code=413, content=envelope(
                            "File too large",
                            detail=f"Max {settings.max_upload_bytes // (1024*1024)} MB per upload.",
                            kind="upload",
                        ))
                    out.write(chunk)
        except Exception as e:
            upload_log.exception("spool: failed")
            crashed = True
            return JSONResponse(status_code=400, content=envelope(
                "Could not read upload",
                detail=f"{type(e).__name__}: {e}",
                kind="upload",
            ))

        if bytes_written == 0:
            crashed = True
            return JSONResponse(status_code=400, content=envelope(
                "Empty file", kind="upload",
            ))

        # Ingest every sheet — drop+create+insert per sheet.
        try:
            summary = ingest_workbook(
                wb_path=persistent_path,
                source_file_name=filename,
                batch_id=batch_id,
            )
        except Exception as e:
            upload_log.exception("dynamic_ingest: crashed")
            crashed = True
            return JSONResponse(status_code=500, content=envelope(
                "Ingest failed",
                detail=f"{type(e).__name__}: {e}",
                kind="internal",
            ))

        # Audit row + bump data_version so caches invalidate.
        total_rows = sum(s["rows_inserted"] for s in summary["ingested"])
        try:
            await record_upload_meta(
                batch_id=batch_id,
                filename=filename,
                target="(workbook)",
                rows_inserted=total_rows,
                rows_failed=0,
                source="upload",
                status="active",
            )
        except Exception:
            upload_log.warning("could not record uploads meta", exc_info=True)
        new_version = await _post_ingest_refresh()

        upload_log.info(
            "upload_workbook ok sheets=%d rows=%d batch=%s file=%s data_version=%d",
            len(summary["ingested"]), total_rows, batch_id, persistent_path.name,
            new_version,
        )

        return {
            "batch_id":      batch_id,
            "filename":      filename,
            "sheet_count":   summary["sheet_count"],
            "tables":        summary["tables"],
            "ingested":      summary["ingested"],
            "skipped":       summary["skipped"],
            "total_rows":    total_rows,
            "rows_inserted": total_rows,   # matches UploadResponse contract
            "data_version":  new_version,
            "bytes_received": bytes_written,
            "file_path":     str(persistent_path),
        }
    except Exception as e:
        upload_log.exception("upload_workbook: unhandled crash")
        crashed = True
        return JSONResponse(status_code=500, content=envelope(
            "Upload failed",
            detail=f"{type(e).__name__}: {e}",
            kind="internal",
        ))
    finally:
        if crashed:
            try:
                if persistent_path.exists():
                    persistent_path.unlink()
            except Exception:
                log.warning("cleanup of failed upload file failed: %s", persistent_path, exc_info=True)


# ---------------------------------------------------------------------------
# /tables/dynamic — list all u_* tables currently ingested (debug/UI helper)
# ---------------------------------------------------------------------------

@api_router.get("/tables/dynamic")
async def list_dynamic_tables_route():
    return {"tables": list_dynamic_tables()}


@api_router.post("/tables/dynamic/disconnect_all")
async def disconnect_all_dynamic_tables():
    dropped = await drop_all_dynamic_tables()
    new_version = await _post_ingest_refresh()
    return {"dropped": dropped, "data_version": new_version}


# ---------------------------------------------------------------------------
# /upload/preview — dry-run a file: parse, validate, classify, but DO NOT insert.
# Returns the duplicate classification so the user can pick a dedup_mode
# before committing.
# ---------------------------------------------------------------------------

@api_router.post("/upload/preview")
async def upload_preview(
    request: Request,
    file: UploadFile = File(...),
    target: str = Form("sales"),
):
    upload_log = logging.getLogger("agentic_ai.api.upload.preview")
    target_table = (target or "sales").strip().lower()
    if target_table not in ALLOWED_TABLES:
        return JSONResponse(status_code=400, content=envelope(
            "Invalid target",
            detail=f"target must be one of {list(ALLOWED_TABLES)}",
            kind="validation",
        ))

    filename = file.filename or "preview"
    lower = filename.lower()
    if lower.endswith(".csv"):
        suffix = ".csv"
    elif lower.endswith(".xlsx"):
        suffix = ".xlsx"
    else:
        return JSONResponse(status_code=400, content=envelope(
            "Unsupported file type",
            detail="Only .csv and .xlsx are accepted.",
            kind="upload",
        ))

    # Spool to a TEMP path (preview doesn't keep the file).
    import tempfile
    fd, tmp_path = tempfile.mkstemp(prefix="agentic_preview_", suffix=suffix)
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
                        return JSONResponse(status_code=413, content=envelope(
                            "File too large",
                            detail=f"Max {settings.max_upload_bytes // (1024*1024)} MB.",
                            kind="upload",
                        ))
                    out.write(chunk)
        except Exception as e:
            return JSONResponse(status_code=400, content=envelope(
                "Could not read upload",
                detail=f"{type(e).__name__}: {e}",
                kind="upload",
            ))

        if bytes_written == 0:
            return JSONResponse(status_code=400, content=envelope(
                "Empty file", kind="upload",
            ))

        # File-hash check against existing active uploads
        file_hash, _ = compute_file_hash(tmp_path)
        existing = await find_active_upload_by_file_hash(file_hash)
        existing_summary = None
        if existing is not None:
            existing_summary = {
                "batch_id":      existing.get("batch_id"),
                "filename":      existing.get("filename"),
                "target":        existing.get("target"),
                "uploaded_at":   existing.get("uploaded_at"),
                "rows_inserted": existing.get("rows_inserted"),
            }

        # Run the agent in preview-only mode for row-level classification
        agent = DataCleanAgent()
        try:
            result = await agent.run(
                tmp_path=Path(tmp_path),
                filename=filename,
                target=target_table,
                preview_only=True,
                dedup_mode="skip",   # any non-block mode skips the early raise
            )
        except UploadError as e:
            return JSONResponse(status_code=400, content=envelope(
                "Bad upload", detail=str(e), kind="upload",
            ))
        except Exception as e:
            upload_log.exception("preview crashed")
            return JSONResponse(status_code=500, content=envelope(
                "Preview failed",
                detail=f"{type(e).__name__}: {e}",
                kind="internal",
            ))

        return {
            "preview":               True,
            "filename":              filename,
            "target":                target_table,
            "file_hash":             file_hash,
            "bytes_received":        bytes_written,
            "existing_file_upload":  existing_summary,
            "rows_failed":           result.get("rows_failed", 0),
            "errors":                result.get("errors", []),
            "header_row_used":       result.get("header_row_used"),
            "unmatched_headers":     result.get("unmatched_headers"),
            "sheet_name":            result.get("sheet_name"),
            "dedup":                 result.get("dedup"),
            "recommended_mode": (
                "skip" if existing_summary or (
                    result.get("dedup", {}).get("rows_duplicate", 0) > 0
                ) else "block"
            ),
        }
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        except Exception:
            log.warning("preview temp cleanup failed: %s", tmp_path, exc_info=True)


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
        result = await DashboardAgent().run(month=month)
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
    # Stamp data_version so the frontend can detect server-side changes
    # since its last fetch and short-circuit redundant reloads.
    if isinstance(result, dict) and "data_version" not in result:
        result = {**result, "data_version": get_data_version()}
    return result


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


@api_router.post("/uploads/{batch_id}/archive")
async def upload_archive(batch_id: str):
    """Soft-deactivate a dataset. AI immediately stops using it. Source
    file is preserved on disk; rows are moved to the archive table. Use
    `/uploads/{batch_id}/unarchive` to reactivate."""
    if not batch_id or len(batch_id) > 64:
        return JSONResponse(status_code=400, content=envelope(
            "Invalid batch_id", detail="batch_id is empty or too long.",
            kind="validation",
        ))
    try:
        result = await archive_upload(batch_id)
    except ValueError as e:
        return JSONResponse(status_code=400, content=envelope(
            "Archive failed", detail=str(e), kind="validation",
        ))
    except Exception as e:
        log.exception("archive_upload failed")
        return JSONResponse(status_code=500, content=envelope(
            "Archive failed",
            detail=f"{type(e).__name__}: {e}",
            kind="internal",
        ))
    bump_data_version()
    invalidate_all()
    invalidate_time_cache()
    return result


@api_router.post("/uploads/{batch_id}/unarchive")
async def upload_unarchive(batch_id: str):
    """Restore an archived dataset back to active. Rows move from the
    archive table to the live table; AI starts using them again."""
    if not batch_id or len(batch_id) > 64:
        return JSONResponse(status_code=400, content=envelope(
            "Invalid batch_id", detail="batch_id is empty or too long.",
            kind="validation",
        ))
    try:
        result = await unarchive_upload(batch_id)
    except ValueError as e:
        return JSONResponse(status_code=400, content=envelope(
            "Unarchive failed", detail=str(e), kind="validation",
        ))
    except Exception as e:
        log.exception("unarchive_upload failed")
        return JSONResponse(status_code=500, content=envelope(
            "Unarchive failed",
            detail=f"{type(e).__name__}: {e}",
            kind="internal",
        ))
    bump_data_version()
    invalidate_all()
    invalidate_time_cache()
    return result


@api_router.get("/datasets")
async def datasets_view():
    """Single-call dataset management view. Returns the full upload
    inventory bucketed by status, plus live + archive row counts. The
    frontend dataset manager renders this directly."""
    all_uploads = await list_uploads_meta(limit=1000)
    by_status: dict[str, list] = {"active": [], "archived": [], "error": [], "removed": []}
    for u in all_uploads:
        by_status.setdefault(u.get("status") or "active", []).append(u)

    return {
        "data_version": get_data_version(),
        "totals": {
            "sales_live":         await count_rows("sales"),
            "purchase_live":      await count_rows("purchase"),
            "sales_archived":     await count_rows("sales_archive"),
            "purchase_archived":  await count_rows("purchase_archive"),
        },
        "active":   by_status["active"],
        "archived": by_status["archived"],
        "errored":  by_status["error"],
        "removed":  by_status["removed"],
        "counts": {
            "active":   len(by_status["active"]),
            "archived": len(by_status["archived"]),
            "errored":  len(by_status["error"]),
            "removed":  len(by_status["removed"]),
            "total":    len(all_uploads),
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


# Internal event names that MUST NOT reach the user-facing SSE stream.
# Dropped silently by PresentationEmitter; the originals stay in the
# backend logs and Sentry breadcrumbs for debugging.
#
# Note: `loop.iteration` (emitted by the AgenticLoop each time the LLM picks
# a capability) is intentionally NOT hidden — it carries the loop's
# decision/reasoning for the frontend to surface, and its payload (iteration,
# capability, args, reasoning) contains no internal-only fields.
_HIDDEN_INTERNAL_EVENTS = {
    "tool.start",            # internal pipeline step names
    "tool.end",
    "kpi.matched",           # leaks kpi_id + matched_alias internals
    "mode.selected",         # internal mode classifier
    "query.kind",            # internal kind classifier output
}

# Fields stripped from any event payload before it streams to the user.
_HIDDEN_PAYLOAD_FIELDS = frozenset({
    "formula", "formula_expression", "sql_used", "sql",
    "required_columns", "missing_columns",
    "kpi_id", "matched_alias", "stack_trace",
    "computed_at", "request_payload", "_internal",
})


def _scrub_payload(data):
    """Recursively strip forbidden keys from a payload before emit.
    Never raises — falls through to original data on any error."""
    try:
        if isinstance(data, dict):
            return {
                k: _scrub_payload(v)
                for k, v in data.items()
                if k not in _HIDDEN_PAYLOAD_FIELDS
            }
        if isinstance(data, list):
            return [_scrub_payload(item) for item in data]
        return data
    except Exception:
        return data


class PresentationEmitter:
    """Wraps the real EventEmitter to enforce the user-facing presentation
    contract:

      1. Internal event names (tool.start, tool.end, kpi.matched, etc.)
         are silently dropped.
      2. Any event payload has its `formula` / `sql_used` / `required_columns`
         / `stack_trace` / `_internal` fields stripped recursively.
      3. Heartbeats, comments, and `close()` pass through unchanged.

    The wrapper is applied at the SSE-runner boundary, so EVERY code path
    that emits through it (KPI fast-path, 14-tool fallback, chat, error
    handlers) gets the same sanitization for free.
    """

    def __init__(self, inner: EventEmitter):
        self._inner = inner

    async def emit(self, event: str, data):
        if event in _HIDDEN_INTERNAL_EVENTS:
            return
        await self._inner.emit(event, _scrub_payload(data))

    async def comment(self, text: str):
        await self._inner.comment(text)

    async def close(self):
        await self._inner.close()

    def stream(self):
        return self._inner.stream()


# ---------------------------------------------------------------------------
# Orchestrator version resolution (v1 / v2)
# ---------------------------------------------------------------------------
# v2 is the reflective Worker/Critic/Validator pipeline under
# ``app.orchestrator_v2``. During the parallel-flag rollout (plan Q1), v1
# stays the default and v2 is opt-in per-deployment or per-request.

_VALID_ORCHESTRATOR_VERSIONS = {"v1", "v2"}


def _resolve_orchestrator_version(request: Request) -> str:
    """
    Pick which orchestrator handles this turn.

    Resolution order (later items override earlier ones):
      1. Default: ``"v2"`` (post-P8 flip).
      2. ``ORCHESTRATOR_VERSION`` environment variable.
      3. ``FORCE_V1`` env var truthy → pins to v1 (emergency rollback).
      4. ``X-Orchestrator-Version`` request header (per-request override).

    Unknown values are ignored and the lower-precedence value is kept.
    The ``FORCE_V1`` lever exists so a deployment can pin back to v1
    without a code change if v2 misbehaves in production.
    """
    chosen = "v2"   # P8: default flipped from v1 → v2.

    env_val = (os.environ.get("ORCHESTRATOR_VERSION") or "").strip().lower()
    if env_val in _VALID_ORCHESTRATOR_VERSIONS:
        chosen = env_val
    elif env_val:
        log.warning("unknown ORCHESTRATOR_VERSION env=%r — keeping %s", env_val, chosen)

    if (os.environ.get("FORCE_V1") or "").strip().lower() in ("1", "true", "yes"):
        chosen = "v1"

    header_val = (request.headers.get("X-Orchestrator-Version") or "").strip().lower()
    if header_val in _VALID_ORCHESTRATOR_VERSIONS:
        chosen = header_val
    elif header_val:
        log.warning("unknown X-Orchestrator-Version=%r — keeping %s", header_val, chosen)

    return chosen


def _shadow_v2_enabled(request: Request) -> bool:
    """
    Shadow mode: when on, every v1 request also runs v2 in parallel
    (silently) so we can quantify v2's divergence pre-flag-flip.
    Controlled by ``SHADOW_V2`` env or ``X-Shadow-V2`` header. Off by
    default. Mutually exclusive with the user actually opting into v2
    (then there's no v1 to shadow against).
    """
    if (request.headers.get("X-Shadow-V2") or "").strip().lower() in ("1", "true", "yes"):
        return True
    return (os.environ.get("SHADOW_V2") or "").strip().lower() in ("1", "true", "yes")


async def _runner_v2_shadow(
    ctx: V2RequestContext,
    v1_question: str,
) -> None:
    """
    Background-task runner for shadow mode. Runs v2 against a silent
    emitter, computes a diff against the (already-streamed-to-user) v1
    answer, and writes one row to ``v2_shadow_log``. Never raises.
    """
    import time as _time

    from app.orchestrator_v2.monitoring.shadow import (
        SilentEventEmitter,
        compute_diff,
        record_shadow_run,
    )
    from app.orchestrator_v2 import run_query_turn_v2

    started = _time.perf_counter()
    silent = SilentEventEmitter()
    final_state = None
    try:
        final_state = await run_query_turn_v2(ctx, silent)
    except Exception:
        log.exception("shadow runner crashed")
    duration_v2_ms = (_time.perf_counter() - started) * 1000.0

    v2_final = silent.final_event() or {}
    v2_answer = v2_final.get("answer")
    v2_mode = v2_final.get("mode") or "v2"

    # We don't have v1's answer here — it streamed to the user. The diff
    # job below records v2-side only; an offline join (by request_id +
    # timestamp) lines it up with the v1 record persisted in
    # response_store.json or the existing audit infrastructure.
    diff = compute_diff(
        v1_answer=None,
        v2_answer=v2_answer,
        v2_state=final_state,
    )
    await record_shadow_run(
        request_id=ctx.request_id,
        conversation_id=ctx.conversation_id,
        question=v1_question,
        v1_mode="v1",
        v1_answer=None,
        v2_mode=v2_mode,
        v2_answer=v2_answer,
        v2_outcome=(final_state.outcome if final_state else "crashed"),
        diff=diff,
        duration_v1_ms=0.0,  # filled in by the offline join job
        duration_v2_ms=duration_v2_ms,
    )


async def _runner_v2(
    ctx: V2RequestContext,
    emitter: EventEmitter,
) -> None:
    """
    v2 turn runner. Symmetric to ``_runner`` but takes a ``RequestContext``
    instead of a ``TurnState`` — v2 owns its own state model
    (``ExecutionState``). Cleanup semantics (PresentationEmitter wrap,
    safe-error envelope, emitter.close()) mirror the v1 runner exactly so
    the SSE wire format stays compatible.
    """
    user_emitter = PresentationEmitter(emitter)
    try:
        await run_query_turn_v2(ctx, user_emitter)
    except Exception:
        log.exception("orchestrator_v2 crashed for request %s", ctx.request_id)
        # User-safe error: never leak the exception class or message.
        await user_emitter.emit("agent.result", envelope(
            "We hit an issue answering your question.",
            detail="The v2 analytics pipeline encountered an error. Try again or rephrase.",
            kind="internal",
        ))
        await user_emitter.emit("turn.end", {
            "turn_id": f"v2-{ctx.request_id}",
            "errors": ["pipeline_error_v2"],
            "final_answer": None,
        })
    finally:
        await emitter.close()


async def _runner(initial: TurnState, emitter: EventEmitter, api_key: str) -> None:
    client = GroqClient(api_key=api_key)
    token = set_request_groq(client)
    # Wrap with the presentation filter so NO code path can leak technical
    # details into the SSE stream. The real emitter is used for the queue
    # (heartbeats, close) — only emit() goes through the sanitizer.
    user_emitter = PresentationEmitter(emitter)
    try:
        await run_query_turn(initial, user_emitter)
    except Exception as e:
        log.exception("coordinator crashed for turn %s", initial.turn_id)
        # User-safe error: never leak the exception class or message.
        await user_emitter.emit("agent.result", envelope(
            "We hit an issue answering your question.",
            detail="The analytics pipeline encountered an error. Try again or rephrase.",
            kind="internal",
        ))
        await user_emitter.emit("turn.end", {
            "turn_id": initial.turn_id,
            "errors": ["pipeline_error"],
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

    orchestrator_version = _resolve_orchestrator_version(request)
    shadow_v2 = _shadow_v2_enabled(request) and orchestrator_version == "v1"
    shadow_task = None

    if orchestrator_version == "v2":
        # v2 reflective pipeline. State stays inside v2 — RequestContext
        # is the minimal handoff.
        ctx = V2RequestContext(
            request_id=uuid4().hex,
            question=req.question,
            conversation_id=req.conversation_id,
            groq_api_key=api_key,
        )
        runner_task = _safe_create_task(_runner_v2(ctx, emitter))
    else:
        # v1 path — original engine.
        initial = TurnState(
            question=req.question,
            conversation_id=req.conversation_id,
        )
        runner_task = _safe_create_task(_runner(initial, emitter, api_key))

        # Shadow mode: spawn a parallel v2 run that won't stream to the
        # user. Logs to v2_shadow_log for offline diffing.
        if shadow_v2:
            shadow_ctx = V2RequestContext(
                request_id=uuid4().hex,
                question=req.question,
                conversation_id=req.conversation_id,
                groq_api_key=api_key,
            )
            shadow_task = _safe_create_task(
                _runner_v2_shadow(shadow_ctx, req.question)
            )

    heartbeat_task = _safe_create_task(_heartbeat(emitter))
    # ``shadow_task`` is INTENTIONALLY excluded from the stream cleanup
    # tuple — it must run to completion regardless of whether the user's
    # SSE stream finishes first (it would otherwise be cancelled the
    # moment v1's ``turn.end`` arrives, before its DB write).
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
    description=(
        "Local-first single-user analytics. v2 reflective orchestrator is "
        "the default; set FORCE_V1=1 to pin back to v1."
    ),
    version="4.0.0-v2",
)

_raw_origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
_allow_all = _raw_origins == ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _allow_all else _raw_origins,
    allow_origin_regex=None if _allow_all else None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

app.include_router(api_router)

# Mount the FastMCP server at /mcp so external MCP clients (Claude Desktop,
# the MCP inspector, other agents) can list + ingest Google Drive files.
# Guarded: if `fastmcp` is not installed the rest of the app still boots.
# The mounted ASGI app has its own lifespan (the streamable-HTTP session
# manager) which Starlette does NOT auto-run for mounted sub-apps — the
# _startup / _shutdown hooks below drive it manually.
_mcp_sub_app = None
_mcp_lifespan_cm = None
try:
    from app.mcp_server import mcp_app

    _mcp_sub_app = mcp_app()
    app.mount("/mcp", _mcp_sub_app)
    log.info("FastMCP server mounted at /mcp")
except Exception:
    log.warning("FastMCP server not mounted (fastmcp unavailable?)", exc_info=True)

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
    # Record into the central error log so the /errors dashboard sees it.
    try:
        error_id = log_error(
            exc=exc,
            module=_module_for_path(request.url.path),
            endpoint=request.url.path,
            method=request.method,
            user_facing=False,
            source="core_system.unhandled_exception_handler",
        )
    except Exception:
        error_id = None
    return JSONResponse(
        status_code=500,
        content=envelope(
            str(exc) or type(exc).__name__,
            detail=type(exc).__name__,
            kind="internal",
            extra={"error_id": error_id} if error_id else None,
        ),
    )


def _module_for_path(path: str) -> str:
    """Best-effort path → module mapping for the error_log table."""
    p = (path or "").lower()
    if p.startswith("/upload"):     return "upload"
    if p.startswith("/uploads"):    return "upload"
    if p.startswith("/dashboard"):  return "dashboard"
    if p.startswith("/query"):      return "ai"
    if p.startswith("/kpi"):        return "kpi"
    if p.startswith("/hierarchy"):  return "hierarchy"
    if p.startswith("/auth"):       return "auth"
    if p.startswith("/drive"):      return "auth"
    if p.startswith("/datasets"):   return "upload"
    if p.startswith("/cache"):      return "system"
    if p.startswith("/time"):       return "time_engine"
    if p.startswith("/health"):     return "system"
    if p.startswith("/errors"):     return "system"
    return "system"


@app.on_event("startup")
async def _startup() -> None:
    from app.infrastructure import init_database, list_uploads_meta, load_synonyms
    from app.vector import register_vocabulary

    await init_database()
    _app_log.info("database engine: %s", engine_kind())

    # Registry reconcile — rebuild dynamic_tables.json from SQLite so AI
    # queries work immediately after restart even if the JSON was lost.
    try:
        rec = reconcile_registry()
        if rec["added"] or rec["removed"]:
            _app_log.info("dynamic table registry reconciled: %s", rec)
        else:
            _app_log.info("dynamic table registry: up to date")
    except Exception:
        _app_log.exception("dynamic table registry reconcile failed (continuing)")

    # KPI registry — table + default catalog. Always rebuild on startup so
    # shipped template updates (e.g. dataset-relative time tokens) propagate
    # to existing DBs without a manual /kpi/rebuild call. User-added KPIs
    # (ids not in DEFAULT_KPIS) are untouched because rebuild_catalog only
    # upserts the shipped default catalog.
    try:
        await init_kpi_table()
        n = await rebuild_catalog()
        _app_log.info("kpi registry rebuilt: %d shipped KPIs upserted", n)
        kpi_rows = await list_kpis(enabled_only=False)
        _app_log.info(
            "kpi registry: %d total (%d enabled)",
            len(kpi_rows), sum(1 for k in kpi_rows if k.enabled),
        )
    except Exception:
        _app_log.exception("kpi registry bootstrap failed (continuing)")

    # Time engine — invalidate any stale cached tokens, then probe once so
    # the operator sees the current dataset date in the boot log.
    try:
        invalidate_time_cache()
        tokens = await resolve_dataset_date_tokens()
        if tokens:
            _app_log.info(
                "time engine: dataset_today=%s month=%s prev_month=%s",
                tokens.get("dataset_today"), tokens.get("dataset_month"),
                tokens.get("dataset_prev_month"),
            )
        else:
            _app_log.info("time engine: no uploaded data yet — relative-time KPIs will fail until first upload")
    except Exception:
        _app_log.exception("time engine probe failed (continuing)")

    # Mock product-name backfill — runs BEFORE hierarchy so newly-named
    # rows propagate to the trees in the same boot cycle. Idempotent:
    # rows that already have a real product name are never touched.
    try:
        mock_stats = await backfill_missing_product_names()
        if any(v > 0 for v in mock_stats.values()):
            _app_log.info("mock product backfill (startup): %s", mock_stats)
    except Exception:
        _app_log.exception("mock product backfill failed (continuing)")

    # Hierarchy — seed default business/branch + sync product master from
    # whatever data is already in the SQLite tables (v1 + v2 in parallel).
    try:
        seeds = await seed_default_business()
        _app_log.info("default branch chain seeded: %s", seeds)
        sync_stats = await sync_product_master_from_data()
        _app_log.info("product hierarchy v1: %s", sync_stats)
        v2_stats = await sync_product_sku_master()
        _app_log.info("product hierarchy v2: %s", v2_stats)
        branches = await list_branches()
        _app_log.info("branches: %d enabled", len(branches))
    except Exception:
        _app_log.exception("hierarchy bootstrap failed (continuing)")

    # Enrichment — inventory + forecast derived from the real sales data.
    try:
        inv_stats = await refresh_inventory()
        _app_log.info("inventory enrichment: %s", inv_stats)
        fc_stats = await refresh_forecast()
        _app_log.info("forecast enrichment: %s", fc_stats)
        cost_stats = await refresh_product_costs()
        _app_log.info("cost master enrichment: %s", cost_stats)
        qty_stats = await backfill_quantities()
        _app_log.info("quantity backfill (startup): %s", qty_stats)
    except Exception:
        _app_log.exception("enrichment bootstrap failed (continuing)")

    registry = get_registry()
    _app_log.info("registry: %d tools registered: %s", len(registry.names), registry.names)
    _app_log.info("financial DB ready at %s", settings.financial_db_path)

    # Report persistent dataset state at boot so the operator can confirm
    # data survived the restart. SQLite tables hold the parsed rows;
    # data/uploads/ holds the original source files.
    try:
        sales_rows = await count_rows("sales")
        purchase_rows = await count_rows("purchase")
        uploads_meta = await list_uploads_meta(limit=1000)
        active_uploads = [u for u in uploads_meta if u.get("status") == "active"]
        _app_log.info(
            "persistent data: sales=%d rows, purchase=%d rows, "
            "active datasets=%d (total uploads=%d)",
            sales_rows, purchase_rows, len(active_uploads), len(uploads_meta),
        )
        if active_uploads:
            for u in active_uploads[:5]:
                _app_log.info(
                    "  dataset batch=%s file=%s target=%s rows=%d uploaded_at=%s",
                    u["batch_id"][:8],
                    u.get("filename"),
                    u.get("target"),
                    u.get("rows_inserted"),
                    u.get("uploaded_at"),
                )
    except Exception:
        _app_log.exception("could not summarize persistent data at startup")

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

    # Drive the mounted FastMCP sub-app's lifespan — Starlette does not run
    # lifespans of mounted ASGI apps, so the streamable-HTTP session manager
    # would otherwise never start. Best-effort: a failure here only disables
    # /mcp, the rest of the app stays up.
    global _mcp_lifespan_cm
    if _mcp_sub_app is not None:
        try:
            _mcp_lifespan_cm = _mcp_sub_app.router.lifespan_context(_mcp_sub_app)
            await _mcp_lifespan_cm.__aenter__()
            _app_log.info("FastMCP /mcp lifespan started")
        except Exception:
            _mcp_lifespan_cm = None
            _app_log.exception("FastMCP /mcp lifespan failed to start (continuing)")


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _mcp_lifespan_cm
    if _mcp_lifespan_cm is not None:
        try:
            await _mcp_lifespan_cm.__aexit__(None, None, None)
        except Exception:
            _app_log.warning("FastMCP /mcp lifespan shutdown error", exc_info=True)
        finally:
            _mcp_lifespan_cm = None
