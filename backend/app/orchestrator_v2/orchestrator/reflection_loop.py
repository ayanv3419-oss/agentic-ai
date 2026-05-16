"""
Reflection loop — orchestrates the Plan → Execute → Validate → Critic →
(maybe reflect) cycle, bounded by ``MAX_REFLECTION_LOOPS``.

Returns the final ``ExecutionState``. Emits SSE events along the way so
the frontend (and observability) sees the loop's progress.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.orchestrator_v2.agents import critic_agent
from app.orchestrator_v2.orchestrator.confidence import (
    compute_confidence,
    decide_gate,
)
from app.orchestrator_v2.orchestrator.executor import execute_plan
from app.orchestrator_v2.orchestrator.planner import (
    PlannerError,
    make_delta_plan,
    make_initial_plan,
)
from app.orchestrator_v2.run import MAX_REFLECTION_LOOPS
from app.orchestrator_v2.state import ExecutionState
from app.orchestrator_v2.tools.registry import (
    CapabilityRegistry,
    get_capability_registry,
)
from app.orchestrator_v2.validators.base import run_validators

if TYPE_CHECKING:
    from app.analytics_engine import EventEmitter

log = logging.getLogger("orchestrator_v2.reflection")


async def run_reflection_loop(
    state: ExecutionState,
    emit: "EventEmitter",
    *,
    registry: CapabilityRegistry | None = None,
    enable_critic: bool = True,
) -> ExecutionState:
    """
    Run the bounded reflection loop.

    Parameters
    ----------
    state : ExecutionState
        Initial state (no plan yet).
    emit : EventEmitter
        SSE emitter (already PresentationEmitter-wrapped).
    enable_critic : bool
        When False, the Critic LLM call is skipped and the gate decides
        on validators + confidence only. Useful for the offline P3 path
        (no Critic client configured) and for unit tests.
    """
    registry = registry or get_capability_registry()

    # ----- Iteration 0: initial plan + execute --------------------------------
    try:
        plan, plan_account, reasoning = await make_initial_plan(state, registry)
    except PlannerError as e:
        log.warning("reflection loop: initial plan failed: %s", e)
        return state.apply(outcome="error", error_message=f"planner: {e}")

    await emit.emit("plan.emitted", {
        "plan_id": plan.plan_id,
        "iteration": plan.iteration,
        "steps": [
            {"step_id": s.step_id, "capability": s.capability,
             "depends_on": list(s.depends_on)}
            for s in plan.steps
        ],
        "reasoning": reasoning,
    })
    if reasoning:
        await emit.emit("loop.iteration", {"iteration": 0, "reasoning": reasoning})

    state = state.apply(
        plan=plan,
        token_usage=state.token_usage.with_addition(
            plan_account.input_tokens, plan_account.output_tokens, plan_account.cost_usd,
        ),
    )
    state = await execute_plan(plan, state, emit, registry=registry)

    for iteration in range(MAX_REFLECTION_LOOPS):
        # Validators
        validation_report = run_validators(state)
        state = state.append_validation(validation_report)
        await emit.emit("validation.report", {
            "iteration": iteration,
            "passed": validation_report.passed,
            "failures": [f.model_dump() for f in validation_report.failures],
        })

        # Critic
        critic_feedback = None
        if enable_critic:
            try:
                critic_feedback, critic_account = await critic_agent.evaluate(
                    state, validation_report
                )
                state = state.append_critic(critic_feedback)
                state = state.apply(
                    token_usage=state.token_usage.with_addition(
                        critic_account.input_tokens,
                        critic_account.output_tokens,
                        critic_account.cost_usd,
                    ),
                )
                await emit.emit("critic.verdict", {
                    "iteration": iteration,
                    "is_acceptable": critic_feedback.is_acceptable,
                    "confidence": critic_feedback.confidence,
                    "summary": critic_feedback.summary,
                    "issues": [i.model_dump() for i in critic_feedback.issues],
                })
            except Exception:
                log.exception("critic evaluate failed; treating as warning")
                critic_feedback = None

        # Confidence + gate
        scores = compute_confidence(state, validation_report, critic_feedback)
        state = state.apply(confidence=scores, reflection_iteration=iteration)
        await emit.emit("confidence.scores", {
            "iteration": iteration,
            "completeness": scores.completeness,
            "data": scores.data,
            "tool": scores.tool,
            "validation": scores.validation,
            "reasoning": scores.reasoning,
            "overall": round(scores.overall, 4),
        })

        retries_remaining = MAX_REFLECTION_LOOPS - 1 - iteration
        decision = decide_gate(
            scores, validation_report, critic_feedback,
            retries_remaining=retries_remaining,
        )

        if decision == "accept":
            return state.apply(outcome="accepted")
        if decision == "escalate":
            await emit.emit("low_confidence", {
                "iteration": iteration,
                "overall": round(scores.overall, 4),
            })
            return state.apply(outcome="escalated")

        # decision == "reflect": Planner produces a delta plan; Executor
        # runs only the new steps.
        await emit.emit("reflection.iteration", {
            "iteration": iteration + 1,
            "max": MAX_REFLECTION_LOOPS,
        })
        try:
            delta_plan, delta_account, delta_reasoning = await make_delta_plan(
                state, registry, critic_feedback, validation_report,
            )
        except PlannerError as e:
            log.warning("delta plan failed: %s; accepting current state", e)
            return state.apply(outcome="accepted")

        await emit.emit("plan.emitted", {
            "plan_id": delta_plan.plan_id,
            "iteration": delta_plan.iteration,
            "steps": [
                {"step_id": s.step_id, "capability": s.capability,
                 "depends_on": list(s.depends_on)}
                for s in delta_plan.steps
            ],
            "reasoning": delta_reasoning,
        })
        if delta_reasoning:
            await emit.emit("loop.iteration", {
                "iteration": iteration + 1,
                "reasoning": delta_reasoning,
            })

        state = state.apply(
            plan=delta_plan,
            token_usage=state.token_usage.with_addition(
                delta_account.input_tokens,
                delta_account.output_tokens,
                delta_account.cost_usd,
            ),
        )
        state = await execute_plan(delta_plan, state, emit, registry=registry)

    # Loop exhausted without an accept — surface as escalation.
    final_scores = state.confidence or compute_confidence(state, None, None)
    state = state.apply(confidence=final_scores, outcome="escalated")
    await emit.emit("low_confidence", {
        "iteration": MAX_REFLECTION_LOOPS - 1,
        "overall": round(final_scores.overall, 4),
        "reason": "reflection_budget_exhausted",
    })
    return state


__all__ = ["run_reflection_loop"]
