"""
Chat sessions persistence (read-only history).

Async, portable (SQLite default + Postgres), and DEFENSIVE: every public
function wraps its work in try/except and logs + swallows on failure. A
history-save read/write must NEVER raise to its caller — a persistence
failure must not break a chat turn or an HTTP request.

Storage lives in the `conversations` + `conversation_messages` tables
(DDL in app.infrastructure, registered in both init_database branches).
Timestamps are TEXT ISO strings passed from Python; SQL uses `?`
placeholders which db_engine.translate_sql auto-converts to `$n` on
Postgres. `await db.commit()` is required for SQLite writes and is a no-op
on Postgres.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from app.infrastructure import get_connection

_log = logging.getLogger("conversation_store")

# Max length for a stored conversation title (fallback + LLM-generated).
_TITLE_MAX = 60


def _now() -> str:
    """Current UTC time as a second-precision ISO string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    """A fresh 32-char hex id."""
    return uuid.uuid4().hex


def _fallback_title(question: str) -> str:
    """Trimmed first question, capped to ~60 chars — the title that stays
    if LLM generation fails or is skipped."""
    title = (question or "").strip()
    if len(title) > _TITLE_MAX:
        title = title[:_TITLE_MAX].rstrip()
    return title


async def persist_turn(conversation_id: str | None, *, question: str, answer: str,
                       chart: object | None = None, tenant_id: str | None = None) -> None:
    """Persist one Q&A turn. No-op if conversation_id is falsy OR both
    question and answer are empty.

    Lazily INSERTs the conversations row if missing (title = trimmed first
    question, created_at=updated_at=now, message_count=0). Then INSERTs a
    'user' message (content=question) and an 'assistant' message
    (content=answer, chart_json=json.dumps(chart) if chart else None), each
    with a uuid id + now timestamp, and UPDATEs the conversation
    (updated_at=now, message_count=message_count+2). If we just created the
    row, fires fire-and-forget asyncio.create_task(_generate_title(...)).

    NEVER raises."""
    try:
        if not conversation_id:
            return
        question = question or ""
        answer = answer or ""
        if not question.strip() and not answer.strip():
            return

        now = _now()
        chart_json = json.dumps(chart) if chart else None
        # Fall back to the documented default tenant when the caller did not
        # supply one (e.g. AUTH disabled). The column also defaults to
        # 'public' at the DB level; we set it explicitly so every row this
        # function writes carries a concrete tenant.
        tenant = tenant_id or "public"

        created_now = False
        async with get_connection() as db:
            cur = await db.execute(
                "SELECT id FROM conversations WHERE id = ?",
                (conversation_id,),
            )
            existing = await cur.fetchall()
            await cur.close()

            if not existing:
                await db.execute(
                    "INSERT INTO conversations "
                    "(id, title, tenant_id, created_at, updated_at, message_count) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (conversation_id, _fallback_title(question), tenant,
                     now, now, 0),
                )
                created_now = True

            await db.execute(
                "INSERT INTO conversation_messages "
                "(id, conversation_id, tenant_id, role, content, chart_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_new_id(), conversation_id, tenant, "user", question, None, now),
            )
            await db.execute(
                "INSERT INTO conversation_messages "
                "(id, conversation_id, tenant_id, role, content, chart_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_new_id(), conversation_id, tenant, "assistant", answer, chart_json, now),
            )
            await db.execute(
                "UPDATE conversations "
                "SET updated_at = ?, message_count = message_count + 2 "
                "WHERE id = ?",
                (now, conversation_id),
            )
            await db.commit()

        if created_now:
            # Best-effort, non-blocking title generation. Fire-and-forget so
            # the chat turn never waits on (or fails because of) the LLM.
            try:
                asyncio.create_task(
                    _generate_title(conversation_id, question, answer)
                )
            except Exception:
                _log.warning("could not schedule title generation", exc_info=True)
    except Exception:
        _log.warning("persist_turn failed for conversation %s",
                     conversation_id, exc_info=True)


async def list_conversations(*, limit: int = 50,
                             tenant_id: str | None = None) -> list[dict]:
    """Return [{id, title, updated_at, message_count}] ordered by updated_at
    DESC, capped at `limit`. Scoped to `tenant_id` (default 'public').
    Returns [] on any error."""
    try:
        tenant = tenant_id or "public"
        async with get_connection() as db:
            cur = await db.execute(
                "SELECT id, title, updated_at, message_count "
                "FROM conversations "
                "WHERE tenant_id = ? "
                "ORDER BY updated_at DESC "
                "LIMIT ?",
                (tenant, limit),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "updated_at": r["updated_at"],
                "message_count": r["message_count"],
            }
            for r in (dict(row) for row in rows)
        ]
    except Exception:
        _log.warning("list_conversations failed", exc_info=True)
        return []


async def get_conversation(conversation_id: str, *,
                           tenant_id: str | None = None) -> dict | None:
    """Return {id, title, created_at, updated_at,
               messages: [{id, role, content, chart, created_at}]} ordered by
    created_at ASC, where `chart` is json.loads(chart_json) or None. Return
    None if the conversation doesn't exist for this tenant (or on any error).
    Scoped to `tenant_id` (default 'public')."""
    try:
        tenant = tenant_id or "public"
        async with get_connection() as db:
            cur = await db.execute(
                "SELECT id, title, created_at, updated_at "
                "FROM conversations WHERE id = ? AND tenant_id = ?",
                (conversation_id, tenant),
            )
            head_rows = await cur.fetchall()
            await cur.close()
            if not head_rows:
                return None
            head = dict(head_rows[0])

            cur = await db.execute(
                "SELECT id, role, content, chart_json, created_at "
                "FROM conversation_messages "
                "WHERE conversation_id = ? AND tenant_id = ? "
                "ORDER BY created_at ASC",
                (conversation_id, tenant),
            )
            msg_rows = await cur.fetchall()
            await cur.close()

        messages: list[dict] = []
        for row in msg_rows:
            m = dict(row)
            chart = None
            raw = m.get("chart_json")
            if raw:
                try:
                    chart = json.loads(raw)
                except (ValueError, TypeError):
                    chart = None
            messages.append({
                "id": m["id"],
                "role": m["role"],
                "content": m["content"],
                "chart": chart,
                "created_at": m["created_at"],
            })

        return {
            "id": head["id"],
            "title": head["title"],
            "created_at": head["created_at"],
            "updated_at": head["updated_at"],
            "messages": messages,
        }
    except Exception:
        _log.warning("get_conversation failed for %s",
                     conversation_id, exc_info=True)
        return None


async def delete_conversation(conversation_id: str, *,
                              tenant_id: str | None = None) -> bool:
    """Delete the conversation + its messages. Return True if it existed
    for this tenant, else False (also False on any error). Scoped to
    `tenant_id` (default 'public')."""
    try:
        tenant = tenant_id or "public"
        async with get_connection() as db:
            cur = await db.execute(
                "SELECT id FROM conversations "
                "WHERE id = ? AND tenant_id = ?",
                (conversation_id, tenant),
            )
            existing = await cur.fetchall()
            await cur.close()
            if not existing:
                return False

            await db.execute(
                "DELETE FROM conversation_messages "
                "WHERE conversation_id = ? AND tenant_id = ?",
                (conversation_id, tenant),
            )
            await db.execute(
                "DELETE FROM conversations "
                "WHERE id = ? AND tenant_id = ?",
                (conversation_id, tenant),
            )
            await db.commit()
        return True
    except Exception:
        _log.warning("delete_conversation failed for %s",
                     conversation_id, exc_info=True)
        return False


async def _generate_title(conversation_id: str, question: str,
                          answer: str) -> None:
    """Best-effort, ONE-shot, server-side title generation. Builds a
    short-lived LLMClient, prompts it for a plain 3-6 word title, and on
    success UPDATEs conversations.title. On ANY exception or empty output it
    does nothing (the trimmed first-question fallback title stays). ALWAYS
    awaits llm.aclose() in finally. NEVER raises."""
    from app.coordinator.llm import LLMClient

    llm: "LLMClient | None" = None
    try:
        llm = LLMClient()
        system = (
            "You write very short conversation titles. Given a user question "
            "and the assistant's answer, reply with ONLY a plain 3-6 word "
            "title summarizing the topic. No quotes, no trailing punctuation, "
            "no preamble."
        )
        user = (
            f"Question:\n{(question or '').strip()[:1000]}\n\n"
            f"Answer:\n{(answer or '').strip()[:1000]}\n\n"
            "Title:"
        )
        resp = await llm.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=24,
        )
        if getattr(resp, "error", None):
            return
        title = _clean_title(getattr(resp, "content", "") or "")
        if not title:
            return
        async with get_connection() as db:
            await db.execute(
                "UPDATE conversations SET title = ? WHERE id = ?",
                (title, conversation_id),
            )
            await db.commit()
    except Exception:
        _log.warning("title generation failed for %s",
                     conversation_id, exc_info=True)
    finally:
        if llm is not None:
            try:
                await llm.aclose()
            except Exception:
                _log.warning("llm.aclose failed in _generate_title",
                             exc_info=True)


def _clean_title(raw: str) -> str:
    """Normalize raw LLM output into a plain title: first non-empty line,
    stripped of surrounding quotes and trailing punctuation, capped to
    ~60 chars."""
    text = (raw or "").strip()
    if not text:
        return ""
    # Take only the first non-empty line.
    for line in text.splitlines():
        line = line.strip()
        if line:
            text = line
            break
    # Strip wrapping quotes.
    text = text.strip().strip('"').strip("'").strip()
    # Drop trailing punctuation.
    text = text.rstrip(".!?,;:").strip()
    if len(text) > _TITLE_MAX:
        text = text[:_TITLE_MAX].rstrip()
    return text
