"""
Critic agent — one LLM call per reflection iteration that evaluates the
Worker's draft answer.

Input shape (user prompt JSON):

  {
    "question": "<user question>",
    "narrative": "<narrate step's answer>",
    "aggregates": {...},
    "steps": [{step_id, capability, status, error?}, ...],
    "validation_report": {...}
  }

Output: ``CriticFeedback`` (strict Pydantic; JSON-mode enforced).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.orchestrator_v2.llm.critic_client import CRITIC_SYSTEM_PROMPT
from app.orchestrator_v2.llm.scope import get_critic_client
from app.orchestrator_v2.llm.token_ledger import CallAccount
from app.orchestrator_v2.state import (
    CriticFeedback,
    CriticIssue,
    ExecutionState,
    ValidationReport,
)

log = logging.getLogger("orchestrator_v2.critic")


_DEFAULT_REJECT = CriticFeedback(
    is_acceptable=False,
    confidence=0.0,
    issues=(
        CriticIssue(
            aspect="vague_answer",
            severity="blocking",
            description="critic returned malformed JSON",
        ),
    ),
    summary="critic returned malformed JSON",
)


def _build_user_prompt(
    state: ExecutionState,
    validation_report: ValidationReport,
) -> str:
    narrative = None
    aggregates: dict[str, Any] = {}
    for s in reversed(state.executed_steps):
        if s.capability == "narrate" and s.status == "done" and s.output:
            narrative = s.output.get("narrative")
            break
    for s in state.executed_steps:
        if s.capability in {"compute_kpi", "run_data_query", "compare_periods",
                            "breakdown_by_hierarchy"} and s.status == "done" and s.output:
            aggregates[s.step_id] = s.output

    return json.dumps({
        "question": state.question,
        "narrative": narrative,
        "aggregates": aggregates,
        "steps": [
            {"step_id": s.step_id, "capability": s.capability,
             "status": s.status, "error": s.error}
            for s in state.executed_steps
        ],
        "validation_report": validation_report.model_dump(),
    }, default=str, ensure_ascii=False)


def _parse_feedback(payload: dict[str, Any]) -> CriticFeedback:
    if not isinstance(payload, dict):
        return _DEFAULT_REJECT
    try:
        issues_raw = payload.get("issues") or []
        parsed_issues: list[CriticIssue] = []
        for raw in issues_raw:
            if not isinstance(raw, dict):
                continue
            try:
                parsed_issues.append(CriticIssue(
                    aspect=raw["aspect"],
                    severity=raw.get("severity", "warning"),
                    description=str(raw.get("description") or "")[:240],
                    target_capability=raw.get("target_capability"),
                ))
            except Exception:
                # Skip malformed individual issues; keep the others.
                continue
        return CriticFeedback(
            is_acceptable=bool(payload.get("is_acceptable", False)),
            confidence=float(payload.get("confidence", 0.0)),
            issues=tuple(parsed_issues),
            summary=str(payload.get("summary") or "")[:400] or "critic returned no summary",
        )
    except Exception:
        log.exception("critic feedback parse failed")
        return _DEFAULT_REJECT


async def evaluate(
    state: ExecutionState,
    validation_report: ValidationReport,
) -> tuple[CriticFeedback, CallAccount]:
    """One Critic LLM call. Returns the typed feedback + token account."""
    client = get_critic_client()
    user_prompt = _build_user_prompt(state, validation_report)
    payload, account = await client.complete_json(
        CRITIC_SYSTEM_PROMPT,
        user_prompt,
        temperature=0.0,
        max_tokens=1024,
    )
    return _parse_feedback(payload), account


__all__ = ["evaluate"]
