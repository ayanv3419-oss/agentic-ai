"""
run_query_turn - the entry point /query_stream calls.

Mirrors the legacy v1 ``analytics_engine.run_query_turn`` signature so
core_system.py can wire to it with minimal change. Owns:
  * turn.start emission
  * cache lookup (compatible with the existing response cache)
  * looping through the Coordinator
  * insightFmt fallback
  * final + turn.end emission
  * conversation memory append
"""
from __future__ import annotations

import logging
from typing import Any

from app.coordinator.llm import LLMClient
from app.coordinator.loop import run_loop
from app.coordinator.memory import append_turn
from app.coordinator.state import TurnState
from app.coordinator.tools.registry import ToolRegistry, get_registry
from app.coordinator.sub_agents import register_sub_agents
from app.infrastructure import cache_key_for, get_cached, put_cached


_log = logging.getLogger("coordinator.runner")


def _ensure_sub_agents_registered(registry: ToolRegistry) -> None:
    needed = {"sqlWriter", "rcaReasoner", "insightFmt"}
    if needed.issubset(set(registry.names())):
        return
    register_sub_agents(registry)


async def run_query_turn(
    state: TurnState,
    emit,
    *,
    llm: LLMClient | None = None,
    registry: ToolRegistry | None = None,
) -> TurnState:
    """Drive one /query_stream turn end-to-end.

    Emits exactly the SSE events the frontend already understands:
      turn.start -> [cache.hit | loop.iteration* + tool.call/result*] ->
        final -> turn.end
    """
    own_llm = False
    if llm is None:
        llm = LLMClient()
        own_llm = True
    if registry is None:
        registry = get_registry()
    _ensure_sub_agents_registered(registry)

    await emit("turn.start", {
        "turn_id": state.turn_id,
        "question": state.question,
    })

    cache_key = cache_key_for(state.question, conversation_id=state.conversation_id)
    cached = get_cached(cache_key)
    if cached and isinstance(cached, dict) and cached.get("final_answer"):
        await emit("cache.hit", {
            "stored_at": cached.get("stored_at"),
            "mode": cached.get("mode", "agentic"),
        })
        await emit("final", {
            "answer": cached.get("final_answer", ""),
            "mode": cached.get("mode", "agentic"),
            "from_cache": True,
            "iteration_count": cached.get("iteration_count", 0),
            "chart": cached.get("chart"),
        })
        await emit("turn.end", {
            "turn_id": state.turn_id,
            "from_cache": True,
            "errors": [],
            "final_answer": cached.get("final_answer", ""),
            "mode": cached.get("mode", "agentic"),
        })
        try:
            append_turn(
                state.conversation_id,
                question=state.question,
                answer=cached.get("final_answer", ""),
                route=cached.get("route"),
            )
        except Exception:
            pass
        if own_llm:
            await llm.aclose()
        return state.apply(final_answer=cached.get("final_answer", ""), finished=True)

    try:
        state = await run_loop(state, llm=llm, registry=registry, emit=emit)
    except Exception as e:
        _log.exception("coordinator loop crashed for turn %s", state.turn_id)
        state = state.with_error(f"loop_crash:{type(e).__name__}:{e}")

    answer = state.final_answer or (
        "I wasn't able to complete the analysis. "
        "Please try rephrasing the question."
    )

    final_payload: dict[str, Any] = {
        "answer": answer,
        "mode": "agentic",
        "iteration_count": state.iteration,
        "from_cache": False,
    }
    if state.chart_payload is not None:
        final_payload["chart"] = state.chart_payload
    if state.route:
        final_payload["route"] = state.route
    await emit("final", final_payload)
    await emit("turn.end", {
        "turn_id": state.turn_id,
        "from_cache": False,
        "errors": list(state.errors),
        "final_answer": answer,
        "mode": "agentic",
    })

    # Cache only clean answers.
    if not state.errors and state.final_answer:
        try:
            put_cached(cache_key, {
                "final_answer": state.final_answer,
                "mode": "agentic",
                "iteration_count": state.iteration,
                "route": state.route,
                "chart": state.chart_payload,
            })
        except Exception:
            _log.warning("cache write failed", exc_info=True)

    try:
        append_turn(
            state.conversation_id,
            question=state.question,
            answer=state.final_answer,
            route=state.route,
        )
    except Exception:
        pass

    if own_llm:
        await llm.aclose()
    return state


__all__ = ["run_query_turn"]
