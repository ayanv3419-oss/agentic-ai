"""
orchestrator_v2.run
===================

Entry point + safety constants for the v2 orchestrator.

* ``run_query_turn_v2(ctx, emit)`` is the symmetric counterpart of v1's
  ``analytics_engine.run_query_turn(state, emit)``. ``core_system.py``
  dispatches to one or the other based on the resolved version.
* Safety constants are module-level so they're easy to grep, but every
  one is env-overridable (see ``_env_int`` / ``_env_float``). The
  reflection loop, executor, retry manager, and token ledger all read
  from here — not from os.environ directly — to keep the contract in
  one place.

This file is intentionally thin during P0 (skeleton). It emits a small
SSE sequence proving the v2 path is reachable end-to-end, then returns.
P2+ replaces the stub body with the real pipeline:

    Plan → Execute DAG → Validate → Critic
         → (reflect if needed, bounded by MAX_REFLECTION_LOOPS) → Finalize
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, TYPE_CHECKING

from app.orchestrator_v2.state import ExecutionState, RequestContext

if TYPE_CHECKING:
    # Imported only for typing — the runtime instance is the
    # PresentationEmitter wrapper from core_system. We avoid importing
    # analytics_engine at module load to keep this package decoupled.
    from app.analytics_engine import EventEmitter

# Toggle: when truthy, v2 falls back to the stub SSE flow (P0 behaviour).
# Useful for re-running pre-P2 smoke tests; also flips automatically if
# Worker LLM client construction fails for any reason.
_V2_USE_STUB = os.environ.get("V2_FORCE_STUB", "").lower() in ("1", "true", "yes")

log = logging.getLogger("orchestrator_v2.run")


# ===========================================================================
# Safety constants
# ===========================================================================
# All env-overridable. The defaults are deliberately conservative — easier
# to relax under load than to tighten after a runaway turn.

def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning("env %s=%r is not an int; using default %d", key, raw, default)
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning("env %s=%r is not a float; using default %f", key, raw, default)
        return default


MAX_REFLECTION_LOOPS: int = _env_int("V2_MAX_REFLECTION_LOOPS", 3)
"""Hard ceiling on Worker→Critic→Validator iterations per turn."""

MAX_PARALLEL_CAPABILITIES: int = _env_int("V2_MAX_PARALLEL_CAPABILITIES", 4)
"""Maximum independent DAG branches the Executor will run concurrently."""

PER_CAPABILITY_TIMEOUT_SEC: float = _env_float("V2_PER_CAPABILITY_TIMEOUT_SEC", 30.0)
"""``asyncio.wait_for`` timeout for one capability invocation."""

PER_TURN_TIMEOUT_SEC: float = _env_float("V2_PER_TURN_TIMEOUT_SEC", 120.0)
"""Wall-clock budget for the entire turn (including all reflection loops)."""

PER_TURN_TOKEN_BUDGET: int = _env_int("V2_PER_TURN_TOKEN_BUDGET", 60_000)
"""Total input+output tokens across all LLM calls in the turn."""

PER_TURN_USD_BUDGET: float = _env_float("V2_PER_TURN_USD_BUDGET", 0.10)
"""Hard cap on estimated Groq spend per turn (USD). Tighter than v1's $1.00."""

CONFIDENCE_ACCEPT: float = _env_float("V2_CONFIDENCE_ACCEPT", 0.85)
"""``ConfidenceScores.overall`` ≥ this AND zero blocking issues → accept."""

CONFIDENCE_ESCALATE: float = _env_float("V2_CONFIDENCE_ESCALATE", 0.50)
"""Below this on retries-exhausted → emit ``low_confidence`` and return best-effort."""

CONVERSATION_WINDOW_TURNS: int = _env_int("V2_CONVERSATION_WINDOW_TURNS", 10)
"""How many prior Q&A pairs ``conversation_memory`` surfaces to the Planner."""


SAFETY_CONSTANTS: dict[str, Any] = {
    "max_reflection_loops": MAX_REFLECTION_LOOPS,
    "max_parallel_capabilities": MAX_PARALLEL_CAPABILITIES,
    "per_capability_timeout_sec": PER_CAPABILITY_TIMEOUT_SEC,
    "per_turn_timeout_sec": PER_TURN_TIMEOUT_SEC,
    "per_turn_token_budget": PER_TURN_TOKEN_BUDGET,
    "per_turn_usd_budget": PER_TURN_USD_BUDGET,
    "confidence_accept": CONFIDENCE_ACCEPT,
    "confidence_escalate": CONFIDENCE_ESCALATE,
    "conversation_window_turns": CONVERSATION_WINDOW_TURNS,
}


# ===========================================================================
# Entry point
# ===========================================================================


async def _emit_stub(state: ExecutionState, emit: "EventEmitter") -> ExecutionState:
    """P0 fallback path — emits a recognisable stub SSE sequence."""
    await emit.emit("turn.start", {
        "turn_id": state.turn_id,
        "question": state.question,
        "mode": "v2_stub",
    })
    await emit.emit("reflection.iteration", {
        "iteration": 0,
        "max": MAX_REFLECTION_LOOPS,
    })
    await emit.emit("final", {
        "answer": (
            "The v2 orchestrator skeleton is active. Full pipeline ships in "
            "later phases. Switch `ORCHESTRATOR_VERSION` to `v1` for the "
            "existing engine."
        ),
        "chart": None,
        "from_cache": False,
        "mode": "v2_stub",
    })
    elapsed_ms = (time.time() - state.started_at) * 1000.0
    await emit.emit("turn.end", {
        "turn_id": state.turn_id,
        "from_cache": False,
        "errors": [],
        "final_answer": "v2_stub",
        "mode": "v2_stub",
        "duration_ms": round(elapsed_ms, 2),
    })
    return state.apply(outcome="accepted", final_answer="v2_stub")


def _extract_final(state: ExecutionState) -> tuple[str | None, dict[str, Any] | None, str]:
    """
    Find the final ``format_response`` step's output and pull (answer,
    chart, mode) for the user-facing ``final`` SSE event. Falls back to
    the most recent narrate step if no format_response succeeded.
    """
    final_step = None
    narrate_step = None
    for s in reversed(state.executed_steps):
        if s.capability == "format_response" and s.status == "done":
            final_step = s
            break
        if narrate_step is None and s.capability == "narrate" and s.status == "done":
            narrate_step = s
    src = final_step or narrate_step
    if src is None or src.output is None:
        return None, None, "v2_failed"
    out = src.output
    answer = out.get("answer") or out.get("narrative") or None
    chart = out.get("chart")
    mode = out.get("mode") or "summary"
    return answer, chart, mode


async def run_query_turn_v2(
    ctx: RequestContext,
    emit: "EventEmitter",
) -> ExecutionState:
    """
    Run one v2 turn.

    Pipeline (P2 cut — no reflection yet, that lands in P4):

        Plan (Worker LLM)
          → Execute DAG (deterministic, parallel where possible)
          → Emit final + turn.end

    P3 adds the Validator chain after Execute. P4 wraps everything in a
    bounded reflection loop. P5 adds memory + audit persistence.
    """
    from app.orchestrator_v2.llm.critic_client import GroqCriticLLMClient
    from app.orchestrator_v2.llm.scope import (
        reset_critic_client,
        reset_worker_client,
        set_critic_client,
        set_worker_client,
    )
    from app.orchestrator_v2.llm.worker_client import GroqWorkerLLMClient
    from app.orchestrator_v2.orchestrator.reflection_loop import run_reflection_loop
    from app.orchestrator_v2.tools.registry import get_capability_registry

    state = ExecutionState.from_request_context(ctx)

    if _V2_USE_STUB:
        return await _emit_stub(state, emit)

    # ----- Front door (tiers 1-3): cache / clarification / KPI fast-path ---
    # Zero-LLM short-circuits. If any tier fires, ALL SSE events (including
    # turn.end) are already emitted; we return without running the reflection
    # loop and without consuming any tokens.
    from app.orchestrator_v2.front_door import try_front_door
    fd = await try_front_door(ctx, emit, turn_id=state.turn_id)
    if fd.short_circuited:
        return state.apply(
            outcome="accepted",
            final_answer=f"front_door:{fd.tier}",
        )

    # ----- Construct + scope LLM clients -----------------------------------
    try:
        worker = GroqWorkerLLMClient(api_key=ctx.groq_api_key)
        critic = GroqCriticLLMClient(api_key=ctx.groq_api_key)
    except Exception:
        log.exception("v2: LLM client construction failed; falling back to stub")
        return await _emit_stub(state, emit)

    worker_token = set_worker_client(worker)
    critic_token = set_critic_client(critic)

    # NOTE: ``turn.start`` was already emitted by ``front_door.try_front_door``
    # above, regardless of whether any tier fired. Emitting it again here
    # would produce a duplicate event the frontend renders twice.

    try:
        registry = get_capability_registry()

        # Reflection loop drives Plan → Execute → Validate → Critic →
        # (maybe delta plan) until accept or escalate.
        state = await run_reflection_loop(state, emit, registry=registry)

        # ----- Memory + audit (P5) ----------------------------------------
        # Persist the full turn snapshot before extracting the final
        # answer so even a failed extraction still ends up in the
        # execution log. Audit never raises — observability failures
        # must not bleed into the user-facing SSE.
        from app.orchestrator_v2.memory.conversation_memory import (
            SqliteConversationMemory,
        )
        from app.orchestrator_v2.monitoring.audit import record_turn

        await record_turn(state)

        # ----- Final answer ------------------------------------------------
        answer, chart, mode = _extract_final(state)
        if state.conversation_id and answer:
            try:
                await SqliteConversationMemory().record(
                    state.conversation_id,
                    question=state.question,
                    answer=answer,
                    metadata={
                        "turn_id": state.turn_id,
                        "outcome": state.outcome,
                        "confidence": state.confidence.overall if state.confidence else None,
                    },
                )
            except Exception:
                log.exception("conversation memory write failed (non-fatal)")
        if answer is None:
            errors = [s.error or "unknown" for s in state.executed_steps if s.status != "done"]
            await emit.emit("agent.result", {
                "error": "no_final_answer",
                "detail": "; ".join(filter(None, errors))[:240] or "unknown",
            })
            await emit.emit("turn.end", {
                "turn_id": state.turn_id,
                "errors": ["no_final_answer"],
                "final_answer": None,
                "mode": "v2",
            })
            return state.apply(outcome="error", error_message="no_final_answer")

        await emit.emit("final", {
            "answer": answer,
            "chart": chart,
            "from_cache": False,
            "mode": "v2",
        })

        elapsed_ms = (time.time() - state.started_at) * 1000.0
        await emit.emit("turn.end", {
            "turn_id": state.turn_id,
            "from_cache": False,
            "errors": [],
            "final_answer": answer[:200] if isinstance(answer, str) else "v2",
            "mode": "v2",
            "duration_ms": round(elapsed_ms, 2),
        })

        return state.apply(outcome="accepted", final_answer=answer, chart_payload=chart)
    finally:
        try:
            reset_worker_client(worker_token)
        except Exception:
            pass
        try:
            reset_critic_client(critic_token)
        except Exception:
            pass
        try:
            await worker.aclose()
        except Exception:
            pass
        try:
            await critic.aclose()
        except Exception:
            pass


__all__ = [
    "run_query_turn_v2",
    "SAFETY_CONSTANTS",
    "MAX_REFLECTION_LOOPS",
    "MAX_PARALLEL_CAPABILITIES",
    "PER_CAPABILITY_TIMEOUT_SEC",
    "PER_TURN_TIMEOUT_SEC",
    "PER_TURN_TOKEN_BUDGET",
    "PER_TURN_USD_BUDGET",
    "CONFIDENCE_ACCEPT",
    "CONFIDENCE_ESCALATE",
    "CONVERSATION_WINDOW_TURNS",
]
