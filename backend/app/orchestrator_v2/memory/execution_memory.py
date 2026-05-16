"""
SQLite-backed execution-memory writer — persists a JSON snapshot of
each turn's ExecutionState for replay/audit/tuning.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.infrastructure import get_connection

log = logging.getLogger("orchestrator_v2.memory.execution")


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS v2_execution_log (
    turn_id              TEXT    PRIMARY KEY,
    request_id           TEXT,
    conversation_id      TEXT,
    question             TEXT,
    plan_json            TEXT,
    executed_steps_json  TEXT,
    validation_json      TEXT,
    critic_json          TEXT,
    confidence_json      TEXT,
    token_usage_json     TEXT,
    outcome              TEXT,
    error_message        TEXT,
    final_answer         TEXT,
    duration_ms          REAL,
    created_at           TEXT    DEFAULT (datetime('now'))
);
"""
_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_v2_exec_log_conv "
    "ON v2_execution_log (conversation_id, created_at DESC);"
)


_INITIALISED = False


async def _ensure_table() -> None:
    global _INITIALISED
    if _INITIALISED:
        return
    async with get_connection() as conn:
        await conn.execute(_CREATE_SQL)
        await conn.execute(_INDEX_SQL)
        await conn.commit()
    _INITIALISED = True


def _dumps(obj: Any) -> str | None:
    if obj is None:
        return None
    try:
        return json.dumps(obj, default=str, ensure_ascii=False)
    except Exception as e:
        log.warning("execution_memory: dump failed: %s", e)
        return None


class SqliteExecutionMemoryWriter:
    """Concrete ExecutionMemoryWriter — writes one row per turn."""

    async def record(self, snapshot: dict[str, Any]) -> None:
        try:
            await _ensure_table()
            async with get_connection() as conn:
                await conn.execute(
                    "INSERT OR REPLACE INTO v2_execution_log ("
                    "turn_id, request_id, conversation_id, question, "
                    "plan_json, executed_steps_json, validation_json, "
                    "critic_json, confidence_json, token_usage_json, "
                    "outcome, error_message, final_answer, duration_ms"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        snapshot.get("turn_id"),
                        snapshot.get("request_id"),
                        snapshot.get("conversation_id"),
                        snapshot.get("question"),
                        _dumps(snapshot.get("plan")),
                        _dumps(snapshot.get("executed_steps")),
                        _dumps(snapshot.get("validation_history")),
                        _dumps(snapshot.get("critic_history")),
                        _dumps(snapshot.get("confidence")),
                        _dumps(snapshot.get("token_usage")),
                        snapshot.get("outcome"),
                        snapshot.get("error_message"),
                        (snapshot.get("final_answer") or "")[:8000],
                        snapshot.get("duration_ms"),
                    ),
                )
                await conn.commit()
        except Exception:
            # NEVER raise from audit — observability failures must not
            # surface to the user-facing SSE stream.
            log.exception("execution_memory: record failed")


__all__ = ["SqliteExecutionMemoryWriter"]
