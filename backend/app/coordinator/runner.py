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
# register_sub_agents removed — sub-agents are internal to capabilities (Phase 2)
from app.infrastructure import cache_key_for, get_cached, put_cached


_log = logging.getLogger("coordinator.runner")


def _ensure_sub_agents_registered(registry: ToolRegistry) -> None:
    # Phase 2: sub-agents (sqlWriter, rcaReasoner, insightFmt) are called
    # internally by capabilities — they are NOT registered in the public
    # registry and must NOT be exposed to the LLM. This function is now a
    # no-op kept for backward-compatibility with any external callers.
    pass


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
    # Multi-tenant SLICE 2c: bind the data-query tenant for THIS turn so the
    # loop's u_* reads / schema introspection resolve to the tenant's own schema
    # (require_principal no longer drives search_path). AUTH-off → "public" →
    # default search_path → byte-for-byte today's behavior. persist_turn writes
    # are public-qualified, so they're unaffected by this.
    from app.tenant_context import set_query_tenant
    set_query_tenant(state.tenant_id)

    own_llm = False
    if llm is None:
        llm = LLMClient()
        own_llm = True
    if registry is None:
        registry = get_registry()
    _ensure_sub_agents_registered(registry)

    try:
        await emit("turn.start", {
            "turn_id": state.turn_id,
            "question": state.question,
        })

        cache_key = cache_key_for(
            state.question,
            conversation_id=state.conversation_id,
            tenant_id=state.tenant_id,
        )
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
            try:
                from app.conversation_store import persist_turn
                await persist_turn(
                    state.conversation_id,
                    question=state.question,
                    answer=cached.get("final_answer", ""),
                    chart=cached.get("chart"),
                    tenant_id=state.tenant_id,
                )
            except Exception:
                pass
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
        try:
            from app.conversation_store import persist_turn
            await persist_turn(
                state.conversation_id,
                question=state.question,
                answer=state.final_answer,
                chart=state.chart_payload,
                tenant_id=state.tenant_id,
            )
        except Exception:
            pass

        return state
    finally:
        # Always release the LLM client we allocated, even if emit() or
        # the loop raises / the task gets cancelled. Without this the
        # underlying httpx connection pool leaks on every aborted turn.
        if own_llm:
            try:
                await llm.aclose()
            except Exception:
                _log.warning("llm close failed", exc_info=True)


__all__ = ["run_query_turn"]
