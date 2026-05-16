"""
Capability: format_response
============================

Final-step packager. Takes the narrate output + the chart-ready
aggregates and produces the ``final`` SSE event envelope the frontend
consumes: ``{answer, chart, mode, ...}``.

This is the **last** node in every plan DAG. The Planner emits exactly
one ``format_response`` step at the leaf.

Status
------
P1 — typed contract + stub. Body lands in P2 (delegates to the existing
``ResponseFormatterTool`` primitive whose chart-payload shape is already
matched to ``frontend/src/client_core.ts``'s ``SalesChart`` type).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.orchestrator_v2.state import ExecutionState
from app.orchestrator_v2.tools.base import Capability
from app.orchestrator_v2.tools.registry import register_capability


class FormatResponseArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    narrative: str = Field(..., min_length=1, max_length=4000)
    aggregates: dict[str, Any] = Field(default_factory=dict)
    mode: str = "summary"
    metric_label: str | None = None


class FormatResponseOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    answer: str = ""
    chart: dict[str, Any] | None = None
    mode: str = "summary"
    placeholder: bool = True
    note: str = "stub — body implementation lands in P2"


@register_capability
class FormatResponse(Capability[FormatResponseArgs, FormatResponseOutput]):
    name = "format_response"
    description = (
        "Final step. Compose the narrate output + aggregates into the "
        "``final`` SSE envelope the frontend consumes "
        "({answer, chart, mode, metric_label}). Every plan DAG ends here."
    )
    args_model = FormatResponseArgs
    output_model = FormatResponseOutput
    # Format-response is always last; Planner ensures upstream steps are
    # present by construction. We don't list requires here to keep this
    # capability composable when partial-retry skips intermediate nodes.
    requires: tuple[str, ...] = ()
    pure = True   # pure transformation; no I/O

    async def run(
        self,
        state: ExecutionState,
        args: FormatResponseArgs,
    ) -> FormatResponseOutput:
        # P2 body: pure transform. Pack the narrative + chart-ready
        # aggregates into the v2 ``final`` SSE envelope shape. Chart
        # payload is only emitted when the aggregates carry the keys
        # the frontend Recharts components expect (series/items/totals).
        chart_payload: dict[str, Any] | None = None
        agg = args.aggregates or {}

        if agg.get("series") or agg.get("items") or agg.get("totals"):
            chart_payload = {
                "kind":        agg.get("kind") or args.mode,
                "totals":      agg.get("totals"),
                "series":      agg.get("series") or [],
                "items":       agg.get("items") or [],
                "comparison":  agg.get("comparison"),
                "granularity": agg.get("granularity"),
            }

        return FormatResponseOutput(
            answer=args.narrative.strip(),
            chart=chart_payload,
            mode=args.mode,
            placeholder=False,
            note="",
        )
