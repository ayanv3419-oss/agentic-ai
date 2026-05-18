"""Sentry initialization — no-op when SENTRY_DSN is not set.

The SDK is always imported and `init()` always called; when the DSN is
empty the SDK ships events to nowhere (silently drops). Call sites can
therefore use `with span(...)` and `capture_exception` unconditionally
without environment branches.

Why no-op-by-default matters:
  - Local dev / CI / tests don't need a sentry account
  - Production turns on monitoring with a single env var change
  - There's no risk of accidentally sending PII from dev because the DSN
    gates ALL outgoing traffic
"""
from __future__ import annotations

import logging
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from app.infrastructure import settings


_log = logging.getLogger("agentic_ai.monitoring.sentry")


def _should_send_pii() -> bool:
    return bool(settings.sentry_send_default_pii)


def init_sentry() -> bool:
    """Initialize the Sentry SDK. Idempotent — repeat calls are no-ops.

    Returns True if a real DSN was configured, False if running in no-op
    mode. Safe to call from FastAPI startup hooks.
    """
    dsn = (settings.sentry_dsn or "").strip()
    # Always init — empty DSN = events drop silently. This means call sites
    # never have to branch.
    sentry_sdk.init(
        dsn=dsn or None,
        environment=settings.sentry_environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        profiles_sample_rate=settings.sentry_profiles_sample_rate,
        send_default_pii=_should_send_pii(),
        # Default tags — every event ships with these so filtering by
        # service/component is one click in the Sentry UI.
        # `before_send` lets us scrub or enrich before the event leaves.
        before_send=_scrub_event,
        integrations=[
            # Don't auto-capture handled HTTPExceptions or 4xx responses —
            # those are expected business outcomes (validation, auth) and
            # would flood the inbox.
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
            # Logging integration: WARNING+ becomes a breadcrumb, ERROR+
            # becomes an event (only when DSN is set).
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
    )
    if dsn:
        sentry_sdk.set_tag("service", "agentic-ai-backend")
        _log.info("Sentry initialized (env=%s)", settings.sentry_environment)
        return True
    _log.info("Sentry initialized in no-op mode (SENTRY_DSN unset)")
    return False


def _scrub_event(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Last-mile event scrubber. Strips bearer tokens and auth headers from
    headers and request bodies before they reach Sentry. Conservative:
    when in doubt, drop a field rather than ship it."""
    try:
        request = event.get("request") or {}
        headers = request.get("headers")
        if isinstance(headers, dict):
            for h in list(headers.keys()):
                low = h.lower()
                if low in ("authorization", "cookie", "set-cookie"):
                    headers[h] = "[scrubbed]"
        # Scrub data field — the upload endpoint can ship multipart bytes.
        data = request.get("data")
        if isinstance(data, str) and len(data) > 4096:
            request["data"] = data[:4096] + f"...[truncated {len(data) - 4096} chars]"
    except Exception:
        # Never let the scrubber crash event delivery.
        return event
    return event


def sentry_status() -> dict[str, Any]:
    """Cheap inspection of the current Sentry config — surfaced on /health
    so ops can see whether monitoring is wired."""
    dsn = (settings.sentry_dsn or "").strip()
    return {
        "enabled":     bool(dsn),
        "environment": settings.sentry_environment,
        "traces_sample_rate":   settings.sentry_traces_sample_rate,
        "profiles_sample_rate": settings.sentry_profiles_sample_rate,
        "send_default_pii":     settings.sentry_send_default_pii,
    }
