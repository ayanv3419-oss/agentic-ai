"""Monitoring + observability surface.

Public re-exports so existing call sites can `from app.monitoring import span`
without knowing the submodule split.
"""
from app.monitoring.sentry_config import init_sentry, sentry_status
from app.monitoring.tracing import set_request_context, span, span_async
from app.monitoring.instrumentation import (
    instrument_fastapi,
    instrument_tool,
    record_breadcrumb,
)

__all__ = [
    "init_sentry",
    "instrument_fastapi",
    "instrument_tool",
    "record_breadcrumb",
    "sentry_status",
    "set_request_context",
    "span",
    "span_async",
]
