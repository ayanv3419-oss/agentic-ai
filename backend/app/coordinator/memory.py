"""
Conversation memory. Single-user MVP: a thin rolling window keyed by
conversation_id, kept in-process. Not persisted - matches the legacy
behaviour the frontend already assumes.
"""
from __future__ import annotations

import threading
from collections import OrderedDict, deque
from typing import Any


_MAX_TURNS_PER_CONVO = 8
_MAX_CONVERSATIONS = 256

_LOCK = threading.Lock()
# OrderedDict so we can evict the least-recently-used conversation
# when we hit _MAX_CONVERSATIONS. Without this cap, a long-running
# server with many distinct conversation_ids leaked unbounded memory.
_STORE: "OrderedDict[str, deque]" = OrderedDict()


def _key(tenant_id: str | None, conversation_id: str) -> str:
    return f"{tenant_id or 'public'}::{conversation_id}"


def append_turn(
    conversation_id: str | None,
    *,
    question: str,
    answer: str | None,
    route: str | None = None,
    tenant_id: str | None = None,
) -> None:
    if not conversation_id:
        return
    key = _key(tenant_id, conversation_id)
    with _LOCK:
        bucket = _STORE.get(key)
        if bucket is None:
            bucket = deque(maxlen=_MAX_TURNS_PER_CONVO)
            _STORE[key] = bucket
            if len(_STORE) > _MAX_CONVERSATIONS:
                _STORE.popitem(last=False)  # evict oldest
        else:
            _STORE.move_to_end(key)  # mark MRU
        bucket.append({
            "question": question,
            "answer": answer or "",
            "route": route,
        })


def recent_turns(
    conversation_id: str | None,
    *,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    if not conversation_id:
        return []
    key = _key(tenant_id, conversation_id)
    with _LOCK:
        bucket = _STORE.get(key)
        if not bucket:
            return []
        _STORE.move_to_end(key)  # touch on read
        return list(bucket)


def render_context(
    conversation_id: str | None,
    *,
    max_turns: int = 4,
    tenant_id: str | None = None,
) -> str:
    """Compact text rendering of prior turns for the LLM system prompt.
    Empty string when there's no prior conversation."""
    turns = recent_turns(conversation_id, tenant_id=tenant_id)
    if not turns:
        return ""
    tail = turns[-max_turns:]
    parts: list[str] = []
    for t in tail:
        q = (t.get("question") or "").strip()
        a = (t.get("answer") or "").strip()
        if q:
            parts.append(f"User: {q}")
        if a:
            parts.append(f"Assistant: {a[:400]}")
    return "\n".join(parts)


__all__ = ["append_turn", "recent_turns", "render_context"]
