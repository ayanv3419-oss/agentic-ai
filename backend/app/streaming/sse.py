"""SSE event emitter backed by an asyncio.Queue.

Coordinator pushes named events; the FastAPI route drains them as
`text/event-stream`. A separate marker is used for SSE comment lines so
heartbeats don't collide with named events.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

_COMMENT_MARKER = "__comment__"


def format_sse(event: str, data: Any) -> str:
    payload = json.dumps(data, default=str, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def format_comment(text: str) -> str:
    safe = text.replace("\n", " ").replace("\r", " ")
    return f": {safe}\n\n"


class EventEmitter:
    _SENTINEL = object()

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self._closed = False

    async def emit(self, event: str, data: Any = None) -> None:
        if self._closed:
            return
        await self.queue.put((event, data if data is not None else {}))

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
