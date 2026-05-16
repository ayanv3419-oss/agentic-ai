"""
orchestrator_v2.front_door
==========================

Shared deterministic front door — cache lookup, hierarchy clarification,
and KPI fast-path. These three short-circuits run BEFORE either v1 or v2's
agentic core; they cost zero LLM tokens on a hit.

Per plan Q2:

    Tier 1: cache lookup            (SHA-256 of normalized question)
    Tier 2: hierarchy clarification (detect_ambiguity on multi-branch q's)
    Tier 3: KPI fast-path           (match_kpi at confidence ≥ 0.85)

If any tier short-circuits, the orchestrator never runs.

Status
------
P0 (skeleton) — this module exposes the **interface** and a no-op
implementation. The real extraction of tiers 1–3 from
``analytics_engine.run_query_turn`` happens in P1 alongside the move of
the 14 primitives. Until then, v1 continues to run its own inline front
door, and v2's stub path skips it.

Once the real implementation lands, ``core_system.query_stream`` will
call ``try_front_door`` once, regardless of ``ORCHESTRATOR_VERSION``;
only ``run_query_turn_v1`` / ``run_query_turn_v2`` are version-routed.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from app.orchestrator_v2.state import RequestContext

if TYPE_CHECKING:
    from app.analytics_engine import EventEmitter

log = logging.getLogger("orchestrator_v2.front_door")


# ===========================================================================
# Outcome model
# ===========================================================================


FrontDoorTier = Literal[
    "cache",                # Tier 1: response cache hit
    "clarification",        # Tier 2: hierarchy ambiguity surfaced
    "kpi_fast_path",        # Tier 3: deterministic KPI answer
    "miss",                 # No short-circuit; hand off to orchestrator core
]


class FrontDoorOutcome(BaseModel):
    """
    Result of running the deterministic front door against a request.

    ``tier`` indicates which short-circuit fired (or ``"miss"`` if none).
    When ``tier != "miss"``, the front door has ALREADY emitted the
    appropriate SSE events (``cache.hit`` / ``clarification.needed`` /
    ``final`` / ``turn.end``) to the user-facing emitter, and the
    orchestrator core must NOT run for this turn.
    """

    model_config = ConfigDict(frozen=True)

    tier: FrontDoorTier
    detail: dict[str, Any] | None = None

    @property
    def short_circuited(self) -> bool:
        return self.tier != "miss"


# ===========================================================================
# Entry point — stub for P0
# ===========================================================================


async def try_front_door(
    ctx: RequestContext,
    emit: "EventEmitter",
    *,
    turn_id: str | None = None,
) -> FrontDoorOutcome:
    """
    Try each deterministic short-circuit in order. Returns ``"miss"`` if
    none fired — caller then dispatches to the orchestrator core.

    When a tier short-circuits, this function emits the appropriate SSE
    events (``cache.hit`` / ``clarification.needed`` / ``analyzing`` /
    ``final`` / ``turn.end``) before returning. The caller MUST NOT emit
    those events again.

    Logic mirrors the legacy ``analytics_engine.run_query_turn``
    front-door section (lines 4422-4545) so v1 and v2 short-circuit
    identically — the user-facing wire shape stays unchanged.
    """
    # Late imports so this module can be imported during package init
    # without triggering the analytics_engine dependency graph.
    from app.infrastructure import cache_key_for, get_cached, put_cached
    from app.kpi import execute_kpi, match_kpi

    cache_key = cache_key_for(
        ctx.question,
        conversation_id=ctx.conversation_id,
    )
    surrogate_turn_id = turn_id or f"fd-{ctx.request_id[:12]}"

    # Emit turn.start eagerly so the wire format matches v1's flow even
    # on a cache/KPI short-circuit. Frontend listens for turn.start to
    # render the "thinking" indicator; without it the UI sits idle until
    # the first event arrives.
    await emit.emit("turn.start", {
        "turn_id":  surrogate_turn_id,
        "question": ctx.question,
    })

    # --- Tier 1: cache lookup -----------------------------------------------
    try:
        cached = get_cached(cache_key)
    except Exception:
        log.exception("front_door: cache get failed")
        cached = None

    if cached:
        cached_mode = cached.get("mode", "agentic")
        await emit.emit("cache.hit", {
            "stored_at": cached.get("stored_at"),
            "mode":      cached_mode,
        })
        final_payload: dict[str, Any] = {
            "answer":     cached.get("final_answer", ""),
            "chart":      cached.get("chart"),
            "from_cache": True,
            "mode":       cached_mode,
        }
        cached_agg = cached.get("aggregates")
        if isinstance(cached_agg, dict) and "format" in cached_agg:
            final_payload["metric"] = cached_agg
        await emit.emit("final", final_payload)
        await emit.emit("turn.end", {
            "turn_id":      surrogate_turn_id,
            "from_cache":   True,
            "errors":       [],
            "final_answer": cached.get("final_answer"),
            "mode":         cached_mode,
        })
        return FrontDoorOutcome(tier="cache", detail={"cache_key": cache_key})

    # --- Tier 2: hierarchy clarification ------------------------------------
    try:
        from app.hierarchy import detect_ambiguity
        clar = await detect_ambiguity(ctx.question)
    except Exception:
        log.exception("front_door: clarification check failed")
        clar = None
    if clar is not None:
        await emit.emit("clarification.needed", clar.to_dict())
        await emit.emit("final", {
            "answer":         clar.prompt,
            "chart":          None,
            "from_cache":     False,
            "mode":           "clarification",
            "clarification":  clar.to_dict(),
        })
        await emit.emit("turn.end", {
            "turn_id":      surrogate_turn_id,
            "from_cache":   False,
            "errors":       [],
            "final_answer": clar.prompt,
            "mode":         "clarification",
        })
        return FrontDoorOutcome(tier="clarification", detail=clar.to_dict())

    # --- Tier 3: KPI fast-path (zero-LLM) -----------------------------------
    try:
        kpi_match = await match_kpi(ctx.question)
    except Exception:
        log.exception("front_door: kpi match failed")
        kpi_match = None

    if kpi_match is not None and kpi_match.confidence >= 0.85:
        try:
            kpi_result = await execute_kpi(kpi_match.kpi)
        except Exception:
            log.exception("front_door: kpi execution failed")
            return FrontDoorOutcome(tier="miss")

        await emit.emit("analyzing", {"stage": "computing your metric"})

        # Build the executive-style answer text exactly like v1 does.
        # We import the v1 helper rather than duplicating its formatting
        # logic; it's a small pure function with no TurnState coupling.
        from app.analytics_engine import _narrate_kpi
        answer = _narrate_kpi(kpi_result)

        user_metric = kpi_result.to_user_dict()
        await emit.emit("final", {
            "answer":     answer,
            "chart":      None,
            "from_cache": False,
            "mode":       "kpi",
            "metric":     user_metric,
        })
        await emit.emit("turn.end", {
            "turn_id":      surrogate_turn_id,
            "from_cache":   False,
            "errors":       [] if not kpi_result.error else [
                "Could not compute the requested metric."
            ],
            "final_answer": answer,
            "mode":         "kpi",
        })

        # Cache the answer so the next identical question is instant.
        try:
            put_cached(cache_key, {
                "mode":         "kpi",
                "sub_agent":    "KPIEngine",
                "final_answer": answer,
                "chart":        None,
                "aggregates":   user_metric,
                "_internal":    kpi_result.to_dict(),
            })
        except Exception:
            log.exception("front_door: cache put failed (non-fatal)")

        return FrontDoorOutcome(tier="kpi_fast_path", detail={
            "kpi": kpi_match.kpi,
            "confidence": kpi_match.confidence,
        })

    # --- Tier 4: miss → caller dispatches to orchestrator core --------------
    return FrontDoorOutcome(tier="miss")


__all__ = [
    "FrontDoorTier",
    "FrontDoorOutcome",
    "try_front_door",
]
