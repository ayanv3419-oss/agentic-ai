"""Direct CHAT responder — bypasses every tool and sub-agent.

Triggered by the intent router when the question is small talk / general
knowledge that does NOT require system data. Calls Groq once and emits a
single `final` SSE event. The reply is cached so identical greetings hit
the cache on the next turn and don't re-call the LLM.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.cache import put_cached
from app.llm import GroqMessage, get_groq
from app.state import TurnState
from app.streaming import EventEmitter

log = logging.getLogger("agentic_ai.coordinator.chat")


_CHAT_SYSTEM_PROMPT = """You are Agentic AI — a friendly assistant for a small-business analytics product.

Reply briefly and warmly to greetings, small talk, and general questions.

If the user asks about their sales, purchases, dashboard, customers, orders,
or any other business data, gently say "Ask me a specific data question and
I'll run it through the analytics pipeline." Do NOT invent numbers or
fabricate any business data.

Keep responses to 1–3 short sentences unless explicitly asked for more.
"""


async def respond_chat(state: TurnState, emit: EventEmitter, reason: str) -> TurnState:
    """Run a single Groq chat call, emit `final`, cache the reply."""
    log.info("chat-mode: question=%r reason=%s", state.question, reason)

    groq = get_groq()
    messages = [
        GroqMessage(role="system", content=_CHAT_SYSTEM_PROMPT),
        GroqMessage(role="user",   content=state.question),
    ]

    # Stream first; fall back to one-shot on stream error.
    buf: list[str] = []
    stream_error: str | None = None
    async for chunk in groq.complete_stream(
        messages, temperature=0.4, max_tokens=300, force_json=False,
    ):
        if chunk.error:
            stream_error = chunk.error
            break
        if chunk.delta:
            buf.append(chunk.delta)
            await emit.emit("agent.token", {"delta": chunk.delta})
    text = "".join(buf).strip()
    tokens_in = sum(len(m.content) for m in messages) // 4
    tokens_out = max(len(text) // 4, 1)

    if stream_error or not text:
        resp = await groq.complete(
            messages, temperature=0.4, max_tokens=300, force_json=False,
        )
        if resp.error:
            err = f"Chat LLM failed: {resp.error_kind}: {resp.error}"
            log.warning(err)
            await emit.emit("agent.result", {"error": err, "kind": "llm"})
            await emit.emit("turn.end", {
                "turn_id":      state.turn_id,
                "errors":       [err],
                "final_answer": None,
                "mode":         "chat",
            })
            return state.append_error(err)
        text = (resp.content or "").strip()
        tokens_in = resp.tokens_in
        tokens_out = resp.tokens_out

    record = {
        "turn_id":      state.turn_id,
        "cache_key":    state.cache_key,
        "stored_at":    datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "query":        state.question,
        "mode":         "chat",
        "sub_agent":    None,
        "route":        None,
        "sql":          None,
        "rows":         None,
        "aggregates":   None,
        "insights":     None,
        "chart":        None,
        "final_answer": text,
        "router_reason": reason,
    }
    if state.cache_key:
        try:
            put_cached(state.cache_key, record)
        except Exception:
            log.warning("chat-mode: cache write failed", exc_info=True)

    await emit.emit("final", {
        "answer":     text,
        "chart":      None,
        "from_cache": False,
        "mode":       "chat",
    })
    await emit.emit("turn.end", {
        "turn_id":      state.turn_id,
        "tokens_in":    tokens_in,
        "tokens_out":   tokens_out,
        "errors":       [],
        "final_answer": text,
        "mode":         "chat",
    })
    return state.apply(
        final_answer=text,
        response_record=record,
        tokens_in=state.tokens_in + tokens_in,
        tokens_out=state.tokens_out + tokens_out,
    )
