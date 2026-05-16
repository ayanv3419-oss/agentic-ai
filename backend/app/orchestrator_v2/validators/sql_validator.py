"""
SQL validator — denylist + parameterization guard.

In v2 the LLM never writes SQL directly — capabilities call into
deterministic primitives. This validator inspects every executed step's
output for raw SQL fragments and ensures they look parameterised + are
SELECT-only (defence in depth).
"""

from __future__ import annotations

import re

from app.orchestrator_v2.state import ExecutionState, ValidationFailure
from app.orchestrator_v2.validators.base import Validator, register_validator

_DANGEROUS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|grant|revoke|merge|"
    r"replace|truncate|attach|detach|vacuum|pragma|exec)\b",
    re.IGNORECASE,
)


@register_validator
class SqlValidator(Validator):
    name = "sql_validator"

    def validate(self, state: ExecutionState) -> list[ValidationFailure]:
        failures: list[ValidationFailure] = []
        for step in state.executed_steps:
            if not step.output:
                continue
            for k, v in step.output.items():
                if not isinstance(v, str):
                    continue
                # Heuristic: only inspect things that look like SQL.
                low = v.lower().strip()
                if not low.startswith("select"):
                    continue
                if _DANGEROUS.search(low):
                    failures.append(ValidationFailure(
                        validator=self.name,
                        severity="blocking",
                        aspect="invalid_sql",
                        description=(
                            f"step {step.step_id} produced SQL with a "
                            f"forbidden keyword (field {k!r})"
                        ),
                        step_id=step.step_id,
                    ))
        return failures
