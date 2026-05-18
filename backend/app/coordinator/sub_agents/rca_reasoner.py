"""
rcaReasoner - narrate a CausalTree as a structured root-cause
explanation. Sub-agent, LLM-backed.

Output: short markdown that names the top movers, quantifies their
contribution, and surfaces secondary factors. Never invents numbers
that aren't in the supplied tree.
"""
from __future__ import annotations

from typing import Any

from app.coordinator.llm import LLMClient
from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome


_SYSTEM = """You are rcaReasoner, a sub-agent of the Coordinator.

You receive a structured causal_tree describing how a metric moved
between two periods, broken down by a dimension. You produce a short,
factual root-cause explanation in plain English.

Rules:
1. Use ONLY numbers present in the supplied tree - never invent figures.
2. Lead with the headline movement (total delta + percent change).
3. List the top movers (positive and negative) in plain English.
4. Mention the residual when it is material (>10% of |total delta|).
5. Be concise - 4-7 short sentences. No markdown headings, no bullet
   characters; this is prose for an executive.
6. If the prior period total is zero, say so plainly and avoid percent
   calculations against zero."""


class RcaReasonerAgent(Tool):
    name = "rcaReasoner"
    description = (
        "Narrate a causal_tree (from CausalTree) as a plain-English "
        "root-cause explanation. Never invents numbers."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "causal_tree": {
                "type": "object",
                "description": (
                    "The causal tree object. Defaults to the one in "
                    "state if omitted."
                ),
            },
            "metric_label": {
                "type": "string",
                "default": "revenue",
                "description": "Human label for the metric (e.g. 'revenue').",
            },
        },
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        llm: LLMClient | None = ctx.llm
        if llm is None:
            return ToolOutcome(ok=False, error="rcaReasoner requires an LLM client.")
        tree = args.get("causal_tree") or ctx.state.causal_tree
        if not isinstance(tree, dict):
            return ToolOutcome(
                ok=False,
                error="rcaReasoner needs a causal_tree (run CausalTree first).",
            )
        label = str(args.get("metric_label") or "revenue")
        user = (
            f"Metric: {label}\n"
            f"causal_tree:\n{tree}\n\n"
            "Write the explanation now."
        )
        resp = await llm.complete(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=500,
        )
        if resp.error:
            return ToolOutcome(ok=False, error=resp.error)
        text = (resp.content or "").strip()
        if not text:
            return ToolOutcome(ok=False, error="rcaReasoner returned empty text")
        return ToolOutcome(
            ok=True,
            output={"explanation": text},
            state_updates={"insights": [*ctx.state.insights, text]},
        )


__all__ = ["RcaReasonerAgent"]
