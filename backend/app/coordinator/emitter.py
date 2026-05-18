"""
SSE event emission. Format and event names are byte-compatible with the
contract the frontend depends on.

Event names the frontend reads (DO NOT rename):
    turn.start, loop.iteration, tool.call, tool.result,
    agent.result, final, turn.end, cache.hit, clarification.needed

Hidden internal events are silently dropped by PresentationEmitter so
nothing leaks to the frontend.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

_log = logging.getLogger("coordinator.emitter")

_COMMENT_MARKER = "__comment__"


def format_sse(event: str, data: Any) -> str:
    """Format a named SSE event for the text/event-stream wire format."""
    payload = json.dumps(data, default=str, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def format_comment(text: str) -> str:
    safe = text.replace("\n", " ").replace("\r", " ")
    return f": {safe}\n\n"


class EventEmitter:
    """asyncio.Queue-backed event emitter feeding the SSE stream."""

    _SENTINEL = object()

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self._closed = False

    async def emit(self, event: str, data: Any = None) -> None:
        if self._closed:
            return
        payload = data if data is not None else {}
        if (
            event in ("turn.end", "final")
            and isinstance(payload, dict)
            and "data_version" not in payload
        ):
            try:
                from app.infrastructure import get_data_version
                payload = {**payload, "data_version": get_data_version()}
            except Exception:
                pass
        await self.queue.put((event, payload))

    async def comment(self, text: str = "ping") -> None:
        if self._closed:
            return
        await self.queue.put((_COMMENT_MARKER, text))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.queue.put(self._SENTINEL)

    async def stream(self) -> AsyncIterator[str]:
        while True:
            try:
                item = await self.queue.get()
            except asyncio.CancelledError:
                raise
            except Exception:
                break
            if item is self._SENTINEL:
                break
            try:
                event, data = item
            except Exception:
                continue
            try:
                if event == _COMMENT_MARKER:
                    yield format_comment(str(data))
                else:
                    yield format_sse(event, data)
            except Exception:
                yield format_comment("serialization-error")


# Internal events that NEVER reach the frontend.
_HIDDEN_INTERNAL_EVENTS = {
    "tool.start",
    "tool.end",
    "kpi.matched",
    "mode.selected",
    "query.kind",
}

# Payload fields scrubbed before reaching the frontend.
_HIDDEN_PAYLOAD_FIELDS = frozenset({
    "formula", "formula_expression", "sql_used", "sql",
    "required_columns", "missing_columns",
    "kpi_id", "matched_alias", "stack_trace",
    "computed_at", "request_payload", "_internal",
})


def _scrub(data: Any) -> Any:
    try:
        if isinstance(data, dict):
            return {
                k: _scrub(v)
                for k, v in data.items()
                if k not in _HIDDEN_PAYLOAD_FIELDS
            }
        if isinstance(data, list):
            return [_scrub(item) for item in data]
        return data
    except Exception:
        return data


class PresentationEmitter:
    """Filters internal events + scrubs sensitive payload fields."""

    def __init__(self, inner: EventEmitter) -> None:
        self._inner = inner

    async def emit(self, event: str, data: Any) -> None:
        if event in _HIDDEN_INTERNAL_EVENTS:
            return
        await self._inner.emit(event, _scrub(data))

    async def comment(self, text: str) -> None:
        await self._inner.comment(text)

    async def close(self) -> None:
        await self._inner.close()

    def stream(self):
        return self._inner.stream()


__all__ = [
    "EventEmitter",
    "PresentationEmitter",
    "format_sse",
    "format_comment",
]
