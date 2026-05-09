"""Tracing primitives — context-managed spans + per-request context.

Two pieces:
  - `span(name, **attrs)` / `span_async` — a thin wrapper over Sentry's
    `start_span` that's safe to use with no DSN (becomes a no-op timer).
    Accepts arbitrary keyword tags so call sites can stamp `tool_name`,
    `agent_name`, `tenant_id`, `query_chars`, etc. without consulting the
    Sentry API directly.

  - `set_request_context(request_id, user_id, tenant_id, conversation_id)`
    binds these onto the current Sentry scope so EVERY exception captured
    after this call is enriched with them. Used by the FastAPI middleware
    on each request, and by the SSE runner per turn.

Performance: `span()` runs in O(1) when no DSN is configured (Sentry's
`start_span` short-circuits). Worth using everywhere.
"""
from __future__ import annotations

import contextlib
import time
from typing import Any, AsyncIterator, Iterator

import sentry_sdk


def set_request_context(
    *,
    request_id: str | None = None,
    user_id: str | None = None,
    tenant_id: str | None = None,
    conversation_id: str | None = None,
    **extra: Any,
) -> None:
    """Bind salient identifiers onto the current Sentry scope.

    Every captured exception or transaction created AFTER this call carries
    these fields. Safe to call with all args None (sets nothing)."""
    scope = sentry_sdk.get_current_scope()
    if user_id:
        scope.set_user({"id": user_id})
    if tenant_id:
        scope.set_tag("tenant_id", tenant_id)
    if conversation_id:
        scope.set_tag("conversation_id", conversation_id)
    if request_id:
        scope.set_tag("request_id", request_id)
    for k, v in extra.items():
        if v is None:
            continue
        scope.set_tag(k, str(v))


@contextlib.contextmanager
def span(
    op: str,
    description: str | None = None,
    **attrs: Any,
) -> Iterator[Any]:
    """Synchronous span. `op` is the high-level category (e.g. 'tool.run',
    'sql.execute'); `description` is the specific instance (e.g. tool name,
    SQL fragment). Extra kwargs become Sentry tags on the span.

    Always yields a span object so call sites can `s.set_tag(...)` mid-run
    if more context becomes available.

    No-DSN behaviour: this still measures wall-clock time so you can `print`
    the span manually if you want; otherwise costs ~zero."""
    started = time.perf_counter()
    with sentry_sdk.start_span(op=op, description=description) as s:
        for k, v in attrs.items():
            if v is None:
                continue
            try:
                s.set_tag(k, str(v))
            except Exception:
                pass
        try:
            yield s
        except Exception as exc:
            try:
                s.set_status("internal_error")
                s.set_data("error.type", type(exc).__name__)
                s.set_data("error.message", str(exc)[:500])
            except Exception:
                pass
            raise
        finally:
            try:
                s.set_data("elapsed_ms", round((time.perf_counter() - started) * 1000, 2))
            except Exception:
                pass


@contextlib.asynccontextmanager
async def span_async(
    op: str,
    description: str | None = None,
    **attrs: Any,
) -> AsyncIterator[Any]:
    """Async variant of `span` — same semantics, awaitable."""
    started = time.perf_counter()
    with sentry_sdk.start_span(op=op, description=description) as s:
        for k, v in attrs.items():
            if v is None:
                continue
            try:
                s.set_tag(k, str(v))
            except Exception:
                pass
        try:
            yield s
        except Exception as exc:
            try:
                s.set_status("internal_error")
                s.set_data("error.type", type(exc).__name__)
                s.set_data("error.message", str(exc)[:500])
            except Exception:
                pass
            raise
        finally:
            try:
                s.set_data("elapsed_ms", round((time.perf_counter() - started) * 1000, 2))
            except Exception:
                pass
