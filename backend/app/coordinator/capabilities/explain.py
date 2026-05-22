"""
explain_change capability.

Collapses the RCA pipeline into one LLM call:
  CausalTree → rcaReasoner

The LLM calls this after it has fetched both current-period and prior-period
data with run_data_query. The current-period rows live in state.rows;
prior-period rows must be passed explicitly as prior_rows.
"""
from __future__ import annotations

from typing import Any

from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome
from app.coordinator.tools.causal_tree import CausalTreeTool
from app.coordinator.sub_agents.rca_reasoner import RcaReasonerAgent


_causal_tool  = CausalTreeTool()
_rca_agent    = RcaReasonerAgent()


class ExplainChangeCapability(Tool):
    name = "explain_change"
    description = (
        "Decompose a metric change into its top contributors and produce a "
        "plain-English root-cause explanation. "
        "Use this for RCA-routed questions after you have already run "
        "run_data_query for the current period. "
        "Supply prior_rows if you have a comparison period; omit them for a "
        "single-period contribution breakdown."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "dimension": {
                "type": "string",
                "description": "Column to decompose by, e.g. 'Product Name', 'Brand'.",
            },
            "prior_rows": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "Rows from the comparison/prior period. "
                    "Omit to get a contribution-only breakdown."
                ),
            },
            "metric_label": {
                "type": "string",
                "default": "revenue",
                "description": "Human label for the metric being explained.",
            },
            "top_n": {
                "type": "integer",
                "default": 5,
                "minimum": 1,
                "maximum": 20,
            },
        },
        "required": ["dimension"],
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        if ctx.llm is None:
            return ToolOutcome(ok=False, error="explain_change requires an LLM client")

        dim          = str(args.get("dimension") or "").strip()
        prior_rows   = args.get("prior_rows")
        metric_label = str(args.get("metric_label") or "revenue")
        top_n        = max(1, min(20, int(args.get("top_n") or 5)))

        if not dim:
            return ToolOutcome(ok=False, error="dimension is required")

        updates: dict[str, Any] = {}
        state = ctx.state

        # ── Step 1: CausalTree ──────────────────────────────────────────
        causal_args: dict[str, Any] = {
            "dimension": dim,
            "top_n": top_n,
        }
        if isinstance(prior_rows, list):
            causal_args["prior_rows"] = prior_rows

        sub = ToolContext(state=state, llm=ctx.llm)
        causal_outcome = await _causal_tool.run(causal_args, sub)
        if not causal_outcome.ok:
            return ToolOutcome(
                ok=False,
                error=f"CausalTree failed: {causal_outcome.error}",
            )
        if causal_outcome.state_updates:
            updates.update(causal_outcome.state_updates)
            state = state.apply(**causal_outcome.state_updates)

        # ── Step 2: rcaReasoner ─────────────────────────────────────────
        sub = ToolContext(state=state, llm=ctx.llm)
        rca_outcome = await _rca_agent.run(
            {"metric_label": metric_label}, sub,
        )
        if not rca_outcome.ok:
            # Non-fatal — return the tree even without narration.
            return ToolOutcome(
                ok=True,
                output={
                    "causal_tree": causal_outcome.output,
                    "explanation": None,
                    "warning": f"rcaReasoner failed: {rca_outcome.error}",
                },
                state_updates=updates,
            )
        if rca_outcome.state_updates:
            updates.update(rca_outcome.state_updates)

        return ToolOutcome(
            ok=True,
            output={
                "causal_tree": causal_outcome.output,
                "explanation": (rca_outcome.output or {}).get("explanation"),
            },
            state_updates=updates,
        )


__all__ = ["ExplainChangeCapability"]
