"""
Audit log writer — snapshots an ``ExecutionState`` into the
``v2_execution_log`` table after every turn (success or failure).

Never raises; failures here must not surface to the user-facing SSE.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.orchestrator_v2.memory.execution_memory import SqliteExecutionMemoryWriter
from app.orchestrator_v2.state import ExecutionState

log = logging.getLogger("orchestrator_v2.monitoring.audit")

_WRITER = SqliteExecutionMemoryWriter()


def _snapshot(state: ExecutionState) -> dict[str, Any]:
    """Build the JSON-serialisable dict the writer expects."""
    return {
        "turn_id":            state.turn_id,
        "request_id":         state.request_id,
        "conversation_id":    state.conversation_id,
        "question":           state.question,
        "plan":               state.plan.model_dump() if state.plan else None,
        "executed_steps":     [s.model_dump() for s in state.executed_steps],
        "validation_history": [v.model_dump() for v in state.validation_history],
        "critic_history":     [c.model_dump() for c in state.critic_history],
        "confidence":         state.confidence.model_dump() if state.confidence else None,
        "token_usage":        state.token_usage.model_dump(),
        "outcome":            state.outcome,
        "error_message":      state.error_message,
        "final_answer":       state.final_answer,
        "duration_ms":        (time.time() - state.started_at) * 1000.0,
    }


async def record_turn(state: ExecutionState) -> None:
    """Persist the state. Never raises."""
    try:
        snapshot = _snapshot(state)
        await _WRITER.record(snapshot)
    except Exception:
        log.exception("audit.record_turn failed")


__all__ = ["record_turn"]
