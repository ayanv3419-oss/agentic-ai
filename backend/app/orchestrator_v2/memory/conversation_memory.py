"""
SQLite-backed conversation memory — rolling window of (question, answer)
pairs per conversation_id.

Uses the existing ``infrastructure.get_connection`` aiosqlite connection
factory so it inherits WAL + busy-timeout settings. Table is created
idempotently on first access.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.infrastructure import get_connection

log = logging.getLogger("orchestrator_v2.memory.conversation")


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS v2_conversation_turns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT    NOT NULL,
    turn_index      INTEGER NOT NULL,
    question        TEXT    NOT NULL,
    answer          TEXT,
    metadata_json   TEXT,
    created_at      TEXT    DEFAULT (datetime('now'))
);
"""
_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_v2_conv_turns_conv_id "
    "ON v2_conversation_turns (conversation_id, turn_index DESC);"
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


class SqliteConversationMemory:
    """Concrete ConversationMemory impl. Rolling window of last N turns."""

    async def recent(
        self,
        conversation_id: str,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if not conversation_id:
            return []
        await _ensure_table()
        async with get_connection() as conn:
            async with conn.execute(
                "SELECT turn_index, question, answer, metadata_json, created_at "
                "FROM v2_conversation_turns "
                "WHERE conversation_id = ? "
                "ORDER BY turn_index DESC LIMIT ?",
                (conversation_id, max(1, int(limit))),
            ) as cur:
                rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in reversed(rows):  # oldest-first when surfacing
            md = None
            if r[3]:
                try:
                    md = json.loads(r[3])
                except Exception:
                    md = None
            out.append({
                "turn_index": r[0],
                "question":   r[1],
                "answer":     r[2],
                "metadata":   md or {},
                "created_at": r[4],
            })
        return out

    async def record(
        self,
        conversation_id: str,
        *,
        question: str,
        answer: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not conversation_id:
            return
        await _ensure_table()
        async with get_connection() as conn:
            async with conn.execute(
                "SELECT COALESCE(MAX(turn_index), -1) "
                "FROM v2_conversation_turns WHERE conversation_id = ?",
                (conversation_id,),
            ) as cur:
                row = await cur.fetchone()
            next_idx = (row[0] if row and row[0] is not None else -1) + 1
            await conn.execute(
                "INSERT INTO v2_conversation_turns "
                "(conversation_id, turn_index, question, answer, metadata_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    int(next_idx),
                    question[:4000],
                    (answer or "")[:8000],
                    json.dumps(metadata, default=str) if metadata else None,
                ),
            )
            await conn.commit()


__all__ = ["SqliteConversationMemory"]
