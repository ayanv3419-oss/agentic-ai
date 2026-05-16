"""
Capability: compare_periods
===========================

First-class period-over-period comparison (YoY, MoM, WoW, custom).
Runs ``run_data_query`` twice with two different time windows and
returns absolute + relative deltas.

Currently v1 has no first-class comparison capability — the
``ResultAggregatorTool`` does a one-off comparison probe inside
``run_data_query``. v2 exposes this as a dedicated capability so the
Planner can request "compare X with Y" without coupling it to a data
query node.

Status
------
P1 — typed contract + stub. Real body lands in P3 (introduces the
double-window execution + delta calculation; depends on
``run_data_query``'s P2 body).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.orchestrator_v2.state import ExecutionState, TimeWindow
from app.orchestrator_v2.tools.base import Capability
from app.orchestrator_v2.tools.registry import register_capability


ComparisonShape = Literal["yoy", "mom", "wow", "custom"]


class ComparePeriodsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str = Field(
        ...,
        description="Metric to compare ('revenue', 'orders', 'margin', etc.).",
    )
    shape: ComparisonShape = "custom"
    period_a: TimeWindow
    period_b: TimeWindow
    filters: dict[str, Any] = Field(default_factory=dict)


class PeriodComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    period_a: TimeWindow
    period_b: TimeWindow
    value_a: float | int | None = None
    value_b: float | int | None = None
    absolute_delta: float | int | None = None
    relative_delta_pct: float | None = None


class ComparePeriodsOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    metric: str
    comparison: PeriodComparison | None = None
    placeholder: bool = True
    note: str = "stub — body implementation lands in P3"


@register_capability
class ComparePeriods(Capability[ComparePeriodsArgs, ComparePeriodsOutput]):
    name = "compare_periods"
    description = (
        "Compute a first-class period-over-period comparison (YoY / MoM / "
        "WoW / custom). Returns absolute + relative deltas plus both "
        "underlying aggregates. The Critic flags missing comparisons that "
        "would have been answerable via this capability."
    )
    args_model = ComparePeriodsArgs
    output_model = ComparePeriodsOutput
    requires: tuple[str, ...] = ()
    pure = False

    async def run(
        self,
        state: ExecutionState,
        args: ComparePeriodsArgs,
    ) -> ComparePeriodsOutput:
        # Real body: invoke run_data_query twice with the two time windows
        # and compute absolute + relative deltas on the chosen metric.
        from app.orchestrator_v2.tools.capabilities.run_data_query import (
            RunDataQuery,
            RunDataQueryArgs,
        )

        rdq = RunDataQuery()

        async def _run_window(window) -> float | None:
            sub_args = RunDataQueryArgs(
                intent="summary",
                dimensions=(),
                time_window=window,
                filters=dict(args.filters),
            )
            out = await rdq.run(state, sub_args)
            if out.empty_reason:
                return None
            totals = out.totals or {}
            # The metric may surface under the user-supplied key or the
            # canonical KPI label v1 uses (e.g., "Total Revenue").
            for key in (args.metric, args.metric.title(), "value", "Total Revenue"):
                if key in totals:
                    try:
                        return float(totals[key])
                    except (TypeError, ValueError):
                        continue
            # Fallback: single-value totals dict.
            if isinstance(totals, dict) and len(totals) == 1:
                only = next(iter(totals.values()))
                try:
                    return float(only)
                except (TypeError, ValueError):
                    return None
            return None

        value_a = await _run_window(args.period_a)
        value_b = await _run_window(args.period_b)

        absolute_delta: float | None = None
        relative_pct: float | None = None
        if value_a is not None and value_b is not None:
            absolute_delta = value_b - value_a
            if abs(value_a) > 1e-9:
                relative_pct = round((absolute_delta / value_a) * 100, 3)

        comparison = PeriodComparison(
            period_a=args.period_a,
            period_b=args.period_b,
            value_a=value_a,
            value_b=value_b,
            absolute_delta=absolute_delta,
            relative_delta_pct=relative_pct,
        )
        return ComparePeriodsOutput(
            metric=args.metric,
            comparison=comparison,
            placeholder=False,
            note="",
        )
