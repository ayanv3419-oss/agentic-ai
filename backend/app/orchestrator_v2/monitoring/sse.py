"""
SSE event emitter + wire-format helpers — extracted from the legacy
``app.analytics_engine`` monolith.

Lives under ``monitoring/`` because the EventEmitter is part of the
observability surface: every SSE event is both user-facing telemetry and
the audit substrate the execution log relies on.

v1 (``app.analytics_engine``) re-exports these symbols so existing import
sites stay compatible. v2 (and ``core_system.py`` post-sunset) imports
directly from here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

_log = logging.getLogger("orchestrator_v2.monitoring.sse")


_COMMENT_MARKER = "__comment__"


def format_sse(event: str, data: Any) -> str:
    """Format a named SSE event for the text/event-stream wire format."""
    payload = json.dumps(data, default=str, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def format_comment(text: str) -> str:
    """Format an SSE comment line — keeps connections alive without
    looking like a named event to the client."""
    safe = text.replace("\n", " ").replace("\r", " ")
    return f": {safe}\n\n"


class EventEmitter:
    """Tiny asyncio.Queue-backed event emitter feeding the SSE stream.

    Coordinator pushes named events; the FastAPI route drains them as
    `text/event-stream`. A separate marker is used for SSE comment lines
    so heartbeats don't collide with named events.
    """

    _SENTINEL = object()

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self._closed = False

    async def emit(self, event: str, data: Any = None) -> None:
        if self._closed:
            return
        payload = data if data is not None else {}
        # Stamp `data_version` on every turn.end and final event so the
        # frontend can detect "data changed since this conversation
        # started" without polling. Done at the emit boundary so all 17
        # call sites (v1 + v2 + front_door) benefit without per-site edits.
        if (
            event in ("turn.end", "final")
            and isinstance(payload, dict)
            and "data_version" not in payload
        ):
            try:
                from app.infrastructure import get_data_version
                payload = {**payload, "data_version": get_data_version()}
            except Exception:
                # Never let an observability concern break the SSE stream.
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


__all__ = [
    "EventEmitter",
    "format_sse",
    "format_comment",
    "_COMMENT_MARKER",
]
