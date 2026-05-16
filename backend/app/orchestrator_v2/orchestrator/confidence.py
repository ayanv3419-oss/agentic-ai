"""
Confidence scoring + accept/reflect/escalate gate.

Computes the 5-dimension ``ConfidenceScores`` deterministically from the
state. No additional LLM call.
"""

from __future__ import annotations

from typing import Literal

from app.orchestrator_v2.run import CONFIDENCE_ACCEPT, CONFIDENCE_ESCALATE
from app.orchestrator_v2.state import (
    ConfidenceScores,
    CriticFeedback,
    ExecutionState,
    ValidationReport,
)


GateDecision = Literal["accept", "reflect", "escalate"]


def compute_confidence(
    state: ExecutionState,
    validation_report: ValidationReport | None,
    critic_feedback: CriticFeedback | None,
) -> ConfidenceScores:
    # tool: ratio of successful steps over total executed steps.
    if state.executed_steps:
        successes = sum(1 for s in state.executed_steps if s.status == "done")
        tool = successes / len(state.executed_steps)
    else:
        tool = 0.0

    # data: 0 if any data step has an unexplained empty result.
    data = 1.0
    for s in state.executed_steps:
        if s.capability not in {"run_data_query", "compute_kpi"}:
            continue
        if s.status != "done":
            data = min(data, 0.4)
            continue
        out = s.output or {}
        if out.get("value") is None and not (out.get("items") or out.get("series") or out.get("totals")):
            if not out.get("empty_reason"):
                data = min(data, 0.2)

    # validation: ratio of passed validators (1.0 if no report yet).
    if validation_report is None:
        validation = 0.7   # neutral when validators haven't run yet
    else:
        total = max(1, len(validation_report.failures))
        blocking = sum(1 for f in validation_report.failures if f.severity == "blocking")
        validation = 1.0 - (blocking / total) if validation_report.failures else 1.0

    # completeness: 1 - (blocking critic issues / cap_at_5).
    if critic_feedback is None:
        completeness = 0.7
    else:
        blocking = len(critic_feedback.blocking_issues)
        completeness = max(0.0, 1.0 - (blocking / 5.0))

    # reasoning: critic's self-reported confidence (or neutral).
    reasoning = critic_feedback.confidence if critic_feedback is not None else 0.5

    return ConfidenceScores(
        completeness=round(completeness, 4),
        data=round(data, 4),
        tool=round(tool, 4),
        validation=round(validation, 4),
        reasoning=round(reasoning, 4),
    )


def decide_gate(
    scores: ConfidenceScores,
    validation_report: ValidationReport | None,
    critic_feedback: CriticFeedback | None,
    *,
    retries_remaining: int,
) -> GateDecision:
    """
    Final gate. Returns one of:

      * ``accept``    — turn complete; emit final answer.
      * ``reflect``   — Planner produces a delta plan; loop again.
      * ``escalate``  — retries exhausted; emit best-effort + low_confidence.
    """
    has_blocking_validation = (
        validation_report is not None and not validation_report.passed
    )
    has_blocking_critic = (
        critic_feedback is not None and not critic_feedback.is_acceptable
    )

    if (
        scores.overall >= CONFIDENCE_ACCEPT
        and not has_blocking_validation
        and not has_blocking_critic
    ):
        return "accept"

    if retries_remaining > 0:
        return "reflect"

    # Retries exhausted. Escalate if:
    #   * confidence is below the escalation floor, OR
    #   * blocking issues remain (validator OR critic).
    # Otherwise accept the best-effort answer rather than discarding it.
    if (
        scores.overall < CONFIDENCE_ESCALATE
        or has_blocking_validation
        or has_blocking_critic
    ):
        return "escalate"

    return "accept"


__all__ = ["compute_confidence", "decide_gate", "GateDecision"]
