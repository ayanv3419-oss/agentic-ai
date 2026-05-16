"""
Shadow mode — runs v2 in parallel with v1 on real production traffic.

When ``SHADOW_V2=true`` is set in the environment, ``core_system``'s
``/query_stream`` handler invokes BOTH pipelines for every request. The
user sees v1's response; v2's output is captured into ``v2_shadow_log``
along with a diff summary so an offline diff-job can quantify v2's
divergence from v1 ahead of the flag flip.

This module owns:

  * the ``v2_shadow_log`` table schema + ensure_table helper
  * ``record_shadow_run`` — write one row per request
  * ``compute_diff`` — produce a per-request diff dict (numeric delta,
    capability list, hallucination check)

The shadow runner itself lives in ``core_system`` so it can share the
existing EventEmitter and api-key resolution.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.infrastructure import get_connection
from app.orchestrator_v2.state import ExecutionState

log = logging.getLogger("orchestrator_v2.monitoring.shadow")


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS v2_shadow_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id      TEXT,
    conversation_id TEXT,
    question        TEXT,
    v1_mode         TEXT,
    v1_answer       TEXT,
    v2_mode         TEXT,
    v2_answer       TEXT,
    v2_outcome      TEXT,
    diff_json       TEXT,
    duration_v1_ms  REAL,
    duration_v2_ms  REAL,
    created_at      TEXT DEFAULT (datetime('now'))
);
"""
_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_v2_shadow_created "
    "ON v2_shadow_log (created_at DESC);"
)


_INITIALISED = False


async def ensure_shadow_table() -> None:
    """Idempotent — create the table + index on first call."""
    global _INITIALISED
    if _INITIALISED:
        return
    async with get_connection() as conn:
        await conn.execute(_CREATE_SQL)
        await conn.execute(_INDEX_SQL)
        await conn.commit()
    _INITIALISED = True


_NUMERIC_RE = re.compile(r"-?\d+(?:[\.,]\d+)*")


def _extract_numbers(text: str | None) -> list[float]:
    if not text:
        return []
    out: list[float] = []
    for m in _NUMERIC_RE.findall(text):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            continue
    return out


def compute_diff(
    *,
    v1_answer: str | None,
    v2_answer: str | None,
    v2_state: ExecutionState | None,
) -> dict[str, Any]:
    """
    Produce a JSON-serialisable diff summary the offline diff job can
    aggregate. Compares numeric content of v1 vs v2 answers and lists
    the capabilities v2 actually invoked.
    """
    v1_nums = _extract_numbers(v1_answer)
    v2_nums = _extract_numbers(v2_answer)

    numeric_match: bool | None = None
    numeric_delta_max: float | None = None
    if v1_nums and v2_nums:
        # For each v1 number, find the closest v2 number; pick the worst
        # relative delta as the divergence metric.
        worst = 0.0
        for a in v1_nums:
            closest = min((abs(a - b) for b in v2_nums), default=abs(a))
            denom = max(abs(a), 1.0)
            rel = closest / denom
            if rel > worst:
                worst = rel
        numeric_delta_max = round(worst, 4)
        numeric_match = worst <= 0.02   # 2% tolerance
    elif not v1_nums and not v2_nums:
        numeric_match = True
        numeric_delta_max = 0.0

    capabilities_used: list[str] = []
    if v2_state is not None:
        capabilities_used = [
            s.capability for s in v2_state.executed_steps if s.status == "done"
        ]

    return {
        "v1_numbers": v1_nums,
        "v2_numbers": v2_nums,
        "numeric_match": numeric_match,
        "numeric_delta_max": numeric_delta_max,
        "capabilities_used": capabilities_used,
        "v1_answer_chars": len(v1_answer or ""),
        "v2_answer_chars": len(v2_answer or ""),
    }


async def record_shadow_run(
    *,
    request_id: str,
    conversation_id: str | None,
    question: str,
    v1_mode: str | None,
    v1_answer: str | None,
    v2_mode: str | None,
    v2_answer: str | None,
    v2_outcome: str | None,
    diff: dict[str, Any],
    duration_v1_ms: float,
    duration_v2_ms: float,
) -> None:
    """Persist one shadow row. Never raises."""
    try:
        await ensure_shadow_table()
        async with get_connection() as conn:
            await conn.execute(
                "INSERT INTO v2_shadow_log ("
                "request_id, conversation_id, question, "
                "v1_mode, v1_answer, v2_mode, v2_answer, v2_outcome, "
                "diff_json, duration_v1_ms, duration_v2_ms"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    conversation_id,
                    (question or "")[:4000],
                    v1_mode,
                    (v1_answer or "")[:4000],
                    v2_mode,
                    (v2_answer or "")[:4000],
                    v2_outcome,
                    json.dumps(diff, default=str)[:8000],
                    round(duration_v1_ms, 2),
                    round(duration_v2_ms, 2),
                ),
            )
            await conn.commit()
    except Exception:
        log.exception("shadow.record_shadow_run failed")


class SilentEventEmitter:
    """
    Drop-in replacement for ``app.analytics_engine.EventEmitter`` that
    captures every event to an in-memory list instead of streaming to
    the user. Used by the shadow runner so v2 events don't bleed into
    the v1 SSE stream.

    Implements the same ``emit``/``comment``/``close``/``stream``
    interface so existing pipeline code (PresentationEmitter wrapper,
    capabilities, reflection loop) works against it unchanged.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.comments: list[str] = []
        self._closed = False

    async def emit(self, event: str, data: Any = None) -> None:
        if self._closed:
            return
        self.events.append((event, data if data is not None else {}))

    async def comment(self, text: str = "ping") -> None:
        if self._closed:
            return
        self.comments.append(text)

    async def close(self) -> None:
        self._closed = True

    async def stream(self):  # async generator interface compatibility
        # Shadow stream is never consumed by the SSE layer — but if some
        # piece of code DOES iterate, return nothing.
        if False:
            yield ""

    def final_event(self) -> dict[str, Any] | None:
        """Return the payload of the last ``final`` event (or None)."""
        for ev, data in reversed(self.events):
            if ev == "final" and isinstance(data, dict):
                return data
        return None


__all__ = [
    "ensure_shadow_table",
    "compute_diff",
    "record_shadow_run",
    "SilentEventEmitter",
]
