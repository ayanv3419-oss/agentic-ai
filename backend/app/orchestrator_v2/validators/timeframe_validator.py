"""
Timeframe validator — any plan that includes ``run_data_query`` or
``compare_periods`` must also include a resolved time window upstream.

Catches a common Planner mistake: omitting ``resolve_time_window`` and
expecting ``run_data_query`` to use a stale or default window.
"""

from __future__ import annotations

from app.orchestrator_v2.state import ExecutionState, ValidationFailure
from app.orchestrator_v2.validators.base import Validator, register_validator


_TIME_AWARE_CAPS = {"run_data_query", "compare_periods", "breakdown_by_hierarchy"}


@register_validator
class TimeframeValidator(Validator):
    name = "timeframe_validator"

    def validate(self, state: ExecutionState) -> list[ValidationFailure]:
        if state.plan is None:
            return []
        # Cheap presence check: was resolve_time_window in the plan?
        has_time = any(s.capability == "resolve_time_window" for s in state.plan.steps)
        needs_time = any(s.capability in _TIME_AWARE_CAPS for s in state.plan.steps)
        if needs_time and not has_time:
            return [ValidationFailure(
                validator=self.name,
                severity="warning",
                aspect="missing_timeframe",
                description="plan uses a time-aware capability without a resolve_time_window step",
                target_capability="resolve_time_window",
            )]
        return []
