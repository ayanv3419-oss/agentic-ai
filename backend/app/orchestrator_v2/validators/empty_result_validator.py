"""
Empty-result validator — a data query that returned 0 rows must carry an
explanation (``empty_reason``). Silent zero rows are a Critic-aspect:
``empty_result_unexplained``.
"""

from __future__ import annotations

from app.orchestrator_v2.state import ExecutionState, ValidationFailure
from app.orchestrator_v2.validators.base import Validator, register_validator


_DATA_CAPS = {"run_data_query", "compare_periods", "breakdown_by_hierarchy"}


@register_validator
class EmptyResultValidator(Validator):
    name = "empty_result_validator"

    def validate(self, state: ExecutionState) -> list[ValidationFailure]:
        failures: list[ValidationFailure] = []
        for step in state.executed_steps:
            if step.capability not in _DATA_CAPS or step.status != "done":
                continue
            out = step.output or {}
            items = out.get("items") or []
            series = out.get("series") or []
            totals = out.get("totals")
            has_data = bool(items) or bool(series) or bool(totals)
            if has_data:
                continue
            reason = out.get("empty_reason")
            if not reason:
                failures.append(ValidationFailure(
                    validator=self.name,
                    severity="warning",
                    aspect="empty_result_unexplained",
                    description=f"{step.capability} returned no data without an empty_reason",
                    step_id=step.step_id,
                ))
        return failures
