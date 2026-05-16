"""
Chart-shape validator — the ``format_response`` step's ``chart`` payload
must conform to the shape the frontend's Recharts components expect.

Specifically: when chart is present it must be a dict with at least one
of {series, items, totals} populated, OR null. An empty {} chart is
flagged because it makes Recharts render an empty placeholder.
"""

from __future__ import annotations

from app.orchestrator_v2.state import ExecutionState, ValidationFailure
from app.orchestrator_v2.validators.base import Validator, register_validator


@register_validator
class ChartShapeValidator(Validator):
    name = "chart_shape_validator"

    def validate(self, state: ExecutionState) -> list[ValidationFailure]:
        failures: list[ValidationFailure] = []
        for step in state.executed_steps:
            if step.capability != "format_response" or step.status != "done":
                continue
            out = step.output or {}
            chart = out.get("chart")
            if chart is None:
                continue
            if not isinstance(chart, dict):
                failures.append(ValidationFailure(
                    validator=self.name,
                    severity="blocking",
                    aspect="chart_data_invalid",
                    description=f"chart payload is {type(chart).__name__}, expected dict|null",
                    step_id=step.step_id,
                ))
                continue
            if not any(chart.get(k) for k in ("series", "items", "totals")):
                failures.append(ValidationFailure(
                    validator=self.name,
                    severity="warning",
                    aspect="chart_data_invalid",
                    description="chart payload is present but empty (no series/items/totals)",
                    step_id=step.step_id,
                ))
        return failures
