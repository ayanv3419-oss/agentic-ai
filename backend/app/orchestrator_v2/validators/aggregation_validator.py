"""
Aggregation validator — when a ``run_data_query`` step produces both
``totals`` and ``series``, the totals SHOULD equal the sum of the series
values within a tolerance. Catches a class of silent bugs where the
aggregator groups by the wrong key.
"""

from __future__ import annotations

from app.orchestrator_v2.state import ExecutionState, ValidationFailure
from app.orchestrator_v2.validators.base import Validator, register_validator


_RELATIVE_TOLERANCE = 0.02   # 2% — accounts for rounding in v1's aggregator


@register_validator
class AggregationValidator(Validator):
    name = "aggregation_validator"

    def validate(self, state: ExecutionState) -> list[ValidationFailure]:
        failures: list[ValidationFailure] = []
        for step in state.executed_steps:
            if step.capability != "run_data_query" or step.status != "done":
                continue
            out = step.output or {}
            totals = out.get("totals")
            series = out.get("series") or []
            if not isinstance(totals, dict) or not series:
                continue
            # Pick the first numeric totals field that has a like-named
            # series accumulation.
            for key, total_value in totals.items():
                try:
                    expected = float(total_value)
                except (TypeError, ValueError):
                    continue
                acc = 0.0
                for bucket in series:
                    if not isinstance(bucket, dict):
                        continue
                    try:
                        acc += float(bucket.get("value", 0))
                    except (TypeError, ValueError):
                        continue
                if expected == 0 and acc == 0:
                    continue
                denom = max(abs(expected), 1e-9)
                if abs(expected - acc) / denom > _RELATIVE_TOLERANCE:
                    failures.append(ValidationFailure(
                        validator=self.name,
                        severity="warning",
                        aspect="contradictory_metrics",
                        description=(
                            f"{step.capability}: totals[{key}]={expected:.2f} "
                            f"but sum(series.value)={acc:.2f}"
                        ),
                        step_id=step.step_id,
                    ))
                break   # one mismatched key is enough; the Critic gets the message.
        return failures
