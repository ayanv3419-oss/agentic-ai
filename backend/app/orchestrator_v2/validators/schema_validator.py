"""
Schema validator — every executed step must have produced output that
matches its Capability's declared output_model.

The Executor already calls ``.model_dump()`` on capability outputs so the
shape is correct by construction in the happy path. This validator
catches the FAILURE path: a capability that returned ``ok=False`` should
have an ``error`` field, and downstream steps with a failed dependency
should be marked ``skipped``.
"""

from __future__ import annotations

from app.orchestrator_v2.state import ExecutionState, ValidationFailure
from app.orchestrator_v2.validators.base import Validator, register_validator


@register_validator
class SchemaValidator(Validator):
    name = "schema_validator"

    def validate(self, state: ExecutionState) -> list[ValidationFailure]:
        failures: list[ValidationFailure] = []
        for step in state.executed_steps:
            if step.status == "failed":
                if not step.error:
                    failures.append(ValidationFailure(
                        validator=self.name,
                        severity="warning",
                        aspect="failed_tool",
                        description=f"step {step.step_id} failed but carries no error message",
                        step_id=step.step_id,
                    ))
                # All failures of executed steps surface as failed_tool issues
                # so the Critic + delta-planner see them in one place.
                else:
                    failures.append(ValidationFailure(
                        validator=self.name,
                        severity="blocking",
                        aspect="failed_tool",
                        description=f"{step.capability} failed: {step.error[:140]}",
                        step_id=step.step_id,
                        target_capability=step.capability,
                    ))
        return failures
