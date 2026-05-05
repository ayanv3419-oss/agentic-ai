"""Coordinator loop — drives a single /query_stream turn end-to-end.

Flow:
  turn.start
    → cache lookup
        ├── HIT  → cache.hit + final + turn.end (no LLM, no tools)
        └── MISS
            → intent_router → mode.selected
                ├── "chat"    → chat_responder (1 Groq call, no tools)
                └── "agentic" → dispatcher (Groq picks sub-agent)
                                  → sub_agent.dispatched
                                  → sub_agent.run() (emits tool.call/.result + final/.end)
"""
from __future__ import annotations

import logging
from typing import Any

from app.agents import get_analytic_agent
from app.cache import cache_key_for, get_cached
from app.coordinator.chat_responder import respond_chat
from app.coordinator.dispatcher import select_sub_agent
from app.coordinator.intent_router import classify
from app.safety import CostGuardError, check_cost, check_loop_iteration
from app.state import TurnState
from app.streaming import EventEmitter

log = logging.getLogger("agentic_ai.coordinator.loop")


async def run_query_turn(state: TurnState, emit: EventEmitter) -> TurnState:
    await emit.emit("turn.start", {
        "turn_id":  state.turn_id,
        "question": state.question,
    })

    cache_key = cache_key_for(state.question)
    state = state.apply(cache_key=cache_key)

    # ---- 1. Cache lookup --------------------------------------------------
    cached = get_cached(cache_key)
    if cached:
        cached_mode = cached.get("mode", "agentic")
        await emit.emit("cache.hit", {
            "cache_key": cache_key,
            "stored_at": cached.get("stored_at"),
            "mode":      cached_mode,
        })
        await emit.emit("final", {
            "answer":     cached.get("final_answer", ""),
            "chart":      cached.get("chart") or cached.get("aggregates"),
            "from_cache": True,
            "mode":       cached_mode,
        })
        await emit.emit("turn.end", {
            "turn_id":      state.turn_id,
            "from_cache":   True,
            "errors":       [],
            "final_answer": cached.get("final_answer"),
            "mode":         cached_mode,
        })
        return state.apply(
            sub_agent=cached.get("sub_agent"),
            final_answer=cached.get("final_answer"),
            chart_data=cached.get("chart") or cached.get("aggregates"),
            response_record=cached,
        )

    # ---- 2. Intent router (deterministic, zero LLM cost) -----------------
    mode, reason = classify(state.question)
    log.info("intent_router: mode=%s reason=%s question=%r",
             mode, reason, state.question)
    await emit.emit("mode.selected", {"mode": mode, "reason": reason})

    if mode == "chat":
        # Direct LLM reply — no tools, no agents.
        return await respond_chat(state, emit, reason)

    # ---- 3. Agentic mode: cost guard + dispatch + sub-agent ---------------
    try:
        check_loop_iteration(state)
        check_cost(state)
        sub_agent_name, sa_reason, metrics = await select_sub_agent(state.question)
    except CostGuardError as e:
        await emit.emit("agent.result", {"error": str(e), "kind": "cost_guard"})
        await emit.emit("turn.end", {
            "turn_id": state.turn_id, "errors": [str(e)], "mode": "agentic",
        })
        return state.append_error(str(e))
    except ValueError as e:
        await emit.emit("agent.result", {"error": str(e), "kind": "dispatch_error"})
        await emit.emit("turn.end", {
            "turn_id": state.turn_id, "errors": [str(e)], "mode": "agentic",
        })
        return state.append_error(str(e))

    state = state.apply(
        sub_agent=sub_agent_name,
        tokens_in=state.tokens_in + int(metrics.get("tokens_in", 0)),
        tokens_out=state.tokens_out + int(metrics.get("tokens_out", 0)),
    )
    await emit.emit("sub_agent.dispatched", {
        "sub_agent": sub_agent_name,
        "reason":    sa_reason,
    })

    agent = get_analytic_agent(sub_agent_name)
    try:
        state = await agent.run(state, emit)
    except Exception as e:
        log.exception("sub-agent crashed")
        await emit.emit("agent.result", {
            "error": f"{type(e).__name__}: {e}",
            "kind":  "internal",
        })
        await emit.emit("turn.end", {
            "turn_id": state.turn_id,
            "errors":  state.errors + [f"{type(e).__name__}: {e}"],
            "mode":    "agentic",
        })
        return state.append_error(f"{type(e).__name__}: {e}")

    if state.errors:
        await emit.emit("agent.result", {
            "error": state.errors[-1], "kind": "internal",
        })
        await emit.emit("turn.end", {
            "turn_id":      state.turn_id,
            "errors":       state.errors,
            "final_answer": state.final_answer,
            "mode":         "agentic",
        })
        return state

    # Success — emit final.
    await emit.emit("final", {
        "answer":     state.final_answer or "",
        "chart":      state.chart_data,
        "from_cache": False,
        "mode":       "agentic",
    })
    await emit.emit("turn.end", {
        "turn_id":      state.turn_id,
        "iterations":   state.iteration,
        "tokens_in":    state.tokens_in,
        "tokens_out":   state.tokens_out,
        "errors":       [],
        "final_answer": state.final_answer,
        "tool_calls":   [tc.model_dump() for tc in state.tool_calls],
        "mode":         "agentic",
    })
    return state
