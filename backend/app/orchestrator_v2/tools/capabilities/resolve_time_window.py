"""
Capability: resolve_time_window
================================

Convert a free-text time phrase ("last 30 days", "Q3 2024",
"this month so far") into a concrete ``TimeWindow`` (start, end,
granularity). Anchored to ``MAX(Date)`` across sales+purchase tables
via the existing ``app.time_engine`` cache.

Status
------
P1 (this phase) — typed contract + stub body. Body implementation
lands in P2 when ``ExecutionState`` plumbing is in place; it will
delegate to the existing ``TimeKPITool`` primitive (re-exported via
``orchestrator_v2.tools.primitives``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.orchestrator_v2.state import ExecutionState, TimeWindow
from app.orchestrator_v2.tools.base import Capability, StubOutput
from app.orchestrator_v2.tools.registry import register_capability


class ResolveTimeWindowArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phrase: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Free-text time phrase from the user, e.g. 'last 30 days'.",
    )
    default_granularity: Literal["hour", "day", "week", "month", "quarter", "year"] = "day"


class ResolveTimeWindowOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    window: TimeWindow | None = None
    resolved: bool = False
    placeholder: bool = True  # flip to False in P2
    note: str = "stub — body implementation lands in P2"


@register_capability
class ResolveTimeWindow(Capability[ResolveTimeWindowArgs, ResolveTimeWindowOutput]):
    name = "resolve_time_window"
    description = (
        "Resolve a natural-language time phrase to an ISO date window and "
        "granularity, anchored to the dataset's most recent date."
    )
    args_model = ResolveTimeWindowArgs
    output_model = ResolveTimeWindowOutput
    requires: tuple[str, ...] = ()
    pure = False  # touches the time_engine cache

    async def run(
        self,
        state: ExecutionState,
        args: ResolveTimeWindowArgs,
    ) -> ResolveTimeWindowOutput:
        # P2 body: delegate to the dataset-relative time engine. Returns
        # (start, end) for known named windows; None for free-form phrases
        # we can't resolve deterministically — those land in the LLM
        # planner's prompt for it to map to one of the supported specs.
        from app.time_engine import resolve_relative_date_range

        window_pair = await resolve_relative_date_range(args.phrase)
        if window_pair is None:
            return ResolveTimeWindowOutput(
                window=None,
                resolved=False,
                placeholder=False,
                note=f"time engine could not resolve {args.phrase!r}",
            )
        start, end = window_pair
        return ResolveTimeWindowOutput(
            window=TimeWindow(
                start_date=start,
                end_date=end,
                granularity=args.default_granularity,
                label=args.phrase,
            ),
            resolved=True,
            placeholder=False,
            note="",
        )
