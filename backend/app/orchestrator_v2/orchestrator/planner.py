"""
Planner — turns a user question into a typed ``Plan`` (DAG of capability
invocations).

Two entry points:

* ``make_initial_plan(state, capabilities)`` — first plan of the turn.
* ``make_delta_plan(state, capabilities, critic_feedback, validation_report)``
  — additive steps that address Critic + Validator findings, leaving the
  already-executed steps untouched.

Both call the Worker LLM client's ``complete_json`` exactly once and
parse the response into a ``Plan``. Errors (malformed JSON, missing
required keys, unknown capability names) raise ``PlannerError`` so the
caller can decide whether to retry or escalate.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.orchestrator_v2.llm.scope import get_worker_client
from app.orchestrator_v2.llm.token_ledger import CallAccount
from app.orchestrator_v2.llm.worker_client import (
    PLANNER_DELTA_PROMPT,
    PLANNER_SYSTEM_PROMPT,
)
from app.orchestrator_v2.state import (
    CriticFeedback,
    ExecutionState,
    Plan,
    PlanStep,
    ValidationReport,
)
from app.orchestrator_v2.tools.registry import CapabilityRegistry

log = logging.getLogger("orchestrator_v2.planner")


class PlannerError(Exception):
    """Raised when the Planner LLM output cannot be turned into a valid Plan."""


def _format_capability_section(registry: CapabilityRegistry) -> str:
    schemas = registry.json_schema_for_planner_prompt()
    return json.dumps({"capabilities": schemas}, indent=2)


def _build_user_prompt(
    state: ExecutionState,
    registry: CapabilityRegistry,
    *,
    prior_plan: Plan | None = None,
    critic_feedback: CriticFeedback | None = None,
    validation_report: ValidationReport | None = None,
) -> str:
    blocks: list[str] = []
    blocks.append("# Registered capabilities (the menu)")
    blocks.append(_format_capability_section(registry))
    blocks.append("# User question")
    blocks.append(state.question.strip())

    if prior_plan is not None:
        blocks.append("# Prior plan (already executed steps below)")
        blocks.append(json.dumps(prior_plan.model_dump(), indent=2, default=str))

    executed = [
        {
            "step_id": s.step_id,
            "capability": s.capability,
            "status": s.status,
            "output_keys": list((s.output or {}).keys()),
            "error": s.error,
        }
        for s in state.executed_steps
    ]
    if executed:
        blocks.append("# Executed steps so far")
        blocks.append(json.dumps(executed, indent=2))

    if critic_feedback is not None:
        blocks.append("# <critic_feedback>")
        blocks.append(json.dumps(critic_feedback.model_dump(), indent=2))
        blocks.append("# </critic_feedback>")

    if validation_report is not None:
        blocks.append("# <validation_report>")
        blocks.append(json.dumps(validation_report.model_dump(), indent=2))
        blocks.append("# </validation_report>")

    return "\n\n".join(blocks)


def _parse_plan(
    payload: dict[str, Any],
    *,
    registry: CapabilityRegistry,
    iteration: int,
    root_question: str,
    existing_ids: set[str],
) -> Plan:
    if not isinstance(payload, dict):
        raise PlannerError(f"planner returned non-object payload: {type(payload).__name__}")

    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlannerError(
            f"planner output has no steps (payload keys: {list(payload.keys())})"
        )

    seen_ids: set[str] = set(existing_ids)
    parsed_steps: list[PlanStep] = []
    for raw in raw_steps:
        if not isinstance(raw, dict):
            raise PlannerError(f"step entry is not an object: {raw!r}")
        step_id = str(raw.get("step_id") or "").strip() or f"s{len(parsed_steps) + 1}"
        if step_id in seen_ids:
            raise PlannerError(f"duplicate step_id {step_id!r}")
        seen_ids.add(step_id)

        cap = str(raw.get("capability") or "").strip()
        if not cap or not registry.has(cap):
            raise PlannerError(f"unknown capability {cap!r} in step {step_id}")

        args = raw.get("args") or {}
        if not isinstance(args, dict):
            raise PlannerError(f"step {step_id} has non-object args: {type(args).__name__}")

        depends_on_raw = raw.get("depends_on") or []
        if not isinstance(depends_on_raw, list):
            raise PlannerError(f"step {step_id} depends_on must be a list")
        depends_on = tuple(str(d) for d in depends_on_raw)

        parallel_group = raw.get("parallel_group")
        rationale = raw.get("rationale")

        parsed_steps.append(
            PlanStep(
                step_id=step_id,
                capability=cap,
                args=args,
                depends_on=depends_on,
                parallel_group=str(parallel_group) if parallel_group else None,
                rationale=str(rationale) if rationale else None,
            )
        )

    return Plan(
        plan_id=f"plan-{uuid.uuid4().hex[:12]}",
        root_question=root_question,
        steps=tuple(parsed_steps),
        iteration=iteration,
    )


async def make_initial_plan(
    state: ExecutionState,
    registry: CapabilityRegistry,
) -> tuple[Plan, CallAccount, str]:
    """
    Produce the first plan of the turn. Returns the plan, the LLM call
    account, and the model's free-text reasoning blurb.
    """
    user_prompt = _build_user_prompt(state, registry)
    client = get_worker_client()
    payload, account = await client.complete_json(
        PLANNER_SYSTEM_PROMPT,
        user_prompt,
        temperature=0.2,
        max_tokens=2048,
    )
    plan = _parse_plan(
        payload,
        registry=registry,
        iteration=0,
        root_question=state.question,
        existing_ids=set(),
    )
    reasoning = str(payload.get("reasoning") or "").strip()
    log.info(
        "planner produced initial plan with %d step(s) for turn %s",
        len(plan.steps), state.turn_id,
    )
    return plan, account, reasoning


async def make_delta_plan(
    state: ExecutionState,
    registry: CapabilityRegistry,
    critic_feedback: CriticFeedback | None,
    validation_report: ValidationReport | None,
) -> tuple[Plan, CallAccount, str]:
    """
    Produce a delta plan that addresses the flagged issues. New steps
    only — existing executed steps stay where they are.
    """
    if state.plan is None:
        raise PlannerError("cannot build delta plan without a prior plan")

    user_prompt = _build_user_prompt(
        state,
        registry,
        prior_plan=state.plan,
        critic_feedback=critic_feedback,
        validation_report=validation_report,
    )
    client = get_worker_client()
    payload, account = await client.complete_json(
        PLANNER_DELTA_PROMPT,
        user_prompt,
        temperature=0.1,
        max_tokens=2048,
    )

    existing_ids = {s.step_id for s in state.plan.steps}
    delta = _parse_plan(
        payload,
        registry=registry,
        iteration=state.plan.iteration + 1,
        root_question=state.question,
        existing_ids=existing_ids,
    )
    reasoning = str(payload.get("reasoning") or "").strip()

    # Merge: prior steps + new steps, in declared order.
    merged_steps = tuple(state.plan.steps) + tuple(delta.steps)
    merged = Plan(
        plan_id=state.plan.plan_id,           # keep the same plan id across iterations
        root_question=state.question,
        steps=merged_steps,
        iteration=state.plan.iteration + 1,
    )
    log.info(
        "planner produced delta with %d new step(s) for turn %s (iteration %d)",
        len(delta.steps), state.turn_id, merged.iteration,
    )
    return merged, account, reasoning


__all__ = [
    "PlannerError",
    "make_initial_plan",
    "make_delta_plan",
]
