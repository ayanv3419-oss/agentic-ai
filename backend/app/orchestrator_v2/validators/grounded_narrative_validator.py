"""
Grounded-narrative validator — every numeric figure cited in the
``narrate`` step's narrative MUST appear in the aggregates that fed it.

This is the hallucination guard. The narrate capability self-reports
``grounded_numbers``; this validator independently re-extracts numbers
from the narrative text and checks them against every numeric value
present in the upstream step outputs (within a small tolerance).
"""

from __future__ import annotations

import re
from typing import Any

from app.orchestrator_v2.state import ExecutionState, ValidationFailure
from app.orchestrator_v2.validators.base import Validator, register_validator


_NUMERIC_RE = re.compile(r"-?\d+(?:[\.,]\d+)*")
_RELATIVE_TOLERANCE = 0.02


def _collect_numbers(value: Any, sink: list[float]) -> None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        sink.append(float(value))
    elif isinstance(value, dict):
        for v in value.values():
            _collect_numbers(v, sink)
    elif isinstance(value, list) or isinstance(value, tuple):
        for v in value:
            _collect_numbers(v, sink)
    elif isinstance(value, str):
        for m in _NUMERIC_RE.findall(value):
            try:
                sink.append(float(m.replace(",", "")))
            except ValueError:
                pass


def _matches_any(candidate: float, pool: list[float]) -> bool:
    for known in pool:
        denom = max(abs(known), 1.0)
        if abs(candidate - known) / denom <= _RELATIVE_TOLERANCE:
            return True
    return False


@register_validator
class GroundedNarrativeValidator(Validator):
    name = "grounded_narrative_validator"

    def validate(self, state: ExecutionState) -> list[ValidationFailure]:
        failures: list[ValidationFailure] = []

        # Pool of every number visible from upstream step outputs.
        pool: list[float] = []
        for step in state.executed_steps:
            if step.capability == "narrate":
                continue
            if step.status != "done" or not step.output:
                continue
            _collect_numbers(step.output, pool)

        for step in state.executed_steps:
            if step.capability != "narrate" or step.status != "done":
                continue
            out = step.output or {}
            narrative = str(out.get("narrative") or "")
            if not narrative:
                continue
            cited: list[float] = []
            for m in _NUMERIC_RE.findall(narrative):
                try:
                    cited.append(float(m.replace(",", "")))
                except ValueError:
                    continue
            for n in cited:
                if not _matches_any(n, pool):
                    failures.append(ValidationFailure(
                        validator=self.name,
                        severity="blocking",
                        aspect="no_supporting_evidence",
                        description=(
                            f"narrative cites number {n} which does not appear in "
                            f"the executed aggregates"
                        ),
                        step_id=step.step_id,
                    ))
        return failures
