"""
Business-rule validator — sanity checks against fundamental accounting
relationships. Each rule is a small helper that returns a list of
failures.

Initial rules (more land in P4+):

* margin = revenue − cost  (when both are present in totals)
* mock-flagged rows must be labelled in the answer text
"""

from __future__ import annotations

from app.orchestrator_v2.state import ExecutionState, ValidationFailure
from app.orchestrator_v2.validators.base import Validator, register_validator


@register_validator
class BusinessRuleValidator(Validator):
    name = "business_rule_validator"

    def validate(self, state: ExecutionState) -> list[ValidationFailure]:
        failures: list[ValidationFailure] = []

        # Rule 1: margin = revenue - cost (when all three are present)
        for step in state.executed_steps:
            if step.status != "done" or not step.output:
                continue
            totals = step.output.get("totals")
            if not isinstance(totals, dict):
                continue
            try:
                revenue = float(totals.get("revenue")) if totals.get("revenue") is not None else None
                cost = float(totals.get("cost")) if totals.get("cost") is not None else None
                margin = float(totals.get("margin")) if totals.get("margin") is not None else None
            except (TypeError, ValueError):
                continue
            if revenue is None or cost is None or margin is None:
                continue
            expected = revenue - cost
            if abs(expected - margin) / max(abs(expected), 1.0) > 0.01:
                failures.append(ValidationFailure(
                    validator=self.name,
                    severity="blocking",
                    aspect="inconsistent_business_logic",
                    description=(
                        f"margin ({margin:.2f}) != revenue ({revenue:.2f}) − "
                        f"cost ({cost:.2f}) = {expected:.2f}"
                    ),
                    step_id=step.step_id,
                ))
        return failures
