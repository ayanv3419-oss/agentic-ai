"""Wiring helpers — FastAPI middleware + tool-runtime instrumentation.

`instrument_fastapi(app)` adds:
  - per-request UUID assignment + `X-Request-ID` response header
  - Sentry scope priming (user/tenant/conversation from auth + request body)
  - SSE-friendly: doesn't try to read the request body for streaming routes

`instrument_tool(name)` is a decorator-like context manager you wrap each
tool invocation in. Captures duration + tool args fingerprint + exception
on failure. Used by the analytics_engine tool dispatcher.

`record_breadcrumb(category, message, **data)` is a thin pass-through to
sentry_sdk so callers don't need to import the SDK directly.
"""
from __future__ import annotations

import contextlib
import logging
import uuid
from typing import Any, Iterator

import sentry_sdk
from fastapi import FastAPI, Request, Response

from app.monitoring.tracing import set_request_context

_log = logging.getLogger("agentic_ai.monitoring.instrumentation")


def record_breadcrumb(category: str, message: str, **data: Any) -> None:
    """Drop a breadcrumb on the current Sentry scope. Cheap when no DSN."""
    sentry_sdk.add_breadcrumb(
        category=category,
        message=message,
        level="info",
        data={k: v for k, v in data.items() if v is not None},
    )


def instrument_fastapi(app: FastAPI) -> None:
    """Attach the request-id + Sentry-scope + security-headers middleware
    to a FastAPI app.

    Call once, after middleware order is fixed (so this runs INSIDE CORS).
    """

    @app.middleware("http")
    async def _request_context_middleware(request: Request, call_next):
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        # Bind the request id eagerly so any exception during the route
        # is enriched with it.
        set_request_context(request_id=rid)
        # Tag the path + method as Sentry tags so transactions are
        # filterable. (FastAPI integration usually does this, but we set
        # the tags explicitly so they're visible on no-DSN runs too.)
        sentry_sdk.set_tag("http.method", request.method)
        sentry_sdk.set_tag("http.path", request.url.path)
        try:
            response: Response = await call_next(request)
        except Exception:
            # FastApiIntegration captures the exception itself; we re-raise
            # so the framework's exception handler runs. The request_id
            # is already on the scope.
            raise
        # Echo the request id back so a client can quote it when reporting.
        response.headers["X-Request-ID"] = rid
        # Security headers — set on every response. Defensive defaults
        # appropriate for an API server; tighten further at the edge
        # (CDN / load balancer) if you have one.
        # X-Content-Type-Options: stop browsers sniffing
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        # X-Frame-Options: this app is not embeddable
        response.headers.setdefault("X-Frame-Options", "DENY")
        # Referrer-Policy: don't leak full URLs to other origins
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # Permissions-Policy: deny dangerous browser features
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=(), interest-cohort=()",
        )
        # CSP for API responses — frameworks usually ignore this for
        # JSON, but if a developer ever opens an API URL in a browser
        # we still want defense-in-depth. The frontend bundle has its
        # own CSP set by Vite/the host.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none';",
        )
        return response


@contextlib.contextmanager
def instrument_tool(name: str, **attrs: Any) -> Iterator[Any]:
    """Wrap a tool invocation in a Sentry span + a logger breadcrumb.

    Usage:
        with instrument_tool("SqlExecutor", table=plan["table"]):
            await tool.run(...)

    The breadcrumb shows up in Sentry under the parent transaction so a
    failed tool run has a chain of crumbs leading to it (intent.classified,
    sub_agent.dispatched, IntentAnalyzer, TimeKPI, SqlPlanner, ...).
    """
    record_breadcrumb("tool", f"{name} start", **attrs)
    with sentry_sdk.start_span(op="tool.run", description=name) as span:
        for k, v in attrs.items():
            if v is None:
                continue
            try:
                span.set_tag(k, str(v))
            except Exception:
                pass
        try:
            yield span
        except Exception as exc:
            try:
                span.set_status("internal_error")
                span.set_data("error.type", type(exc).__name__)
            except Exception:
                pass
            record_breadcrumb("tool", f"{name} FAILED",
                              error_type=type(exc).__name__,
                              error=str(exc)[:200])
            raise
        else:
            record_breadcrumb("tool", f"{name} ok")
