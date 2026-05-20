"""
insightFmt - format the final user-facing answer. Sub-agent, LLM-backed.

Inputs: the question, the SQL rows (already in state), any rcaReasoner
insights, the chart payload. Output: clean prose for the frontend.

This is the LAST sub-agent the Coordinator calls before emitting the
'final' SSE event.
"""
from __future__ import annotations

from typing import Any

from app.coordinator.llm import LLMClient
from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome


_SYSTEM = """You are insightFmt, a sub-agent of the Coordinator.

Your job: write the final, user-facing answer to the original question
using the data already gathered in this turn. Output is plain prose.

Rules:
1. Use ONLY numbers that appear in the supplied rows/insights. Never
   fabricate figures.
2. Lead with the direct answer in one short sentence.
3. Follow with 1-3 supporting sentences that quote the most useful
   numbers from the rows (with units / dates as available).
4. Mention notable secondary patterns only if material.
5. Plain prose - no markdown headings, no bullet points unless the
   question literally asks for a list.
6. If the rows are empty, say so plainly and suggest a refinement.
7. Keep total length under 120 words.
8. Margin / profit: if the rows carry no cost or unit-cost column, state
   plainly that the data has no cost information so margin cannot be
   computed. NEVER derive a margin by subtracting unrelated totals.
9. All monetary amounts are Indian Rupees - write the "₹" symbol, never
   "$" or the word "dollars"."""


def _build_user(ctx: ToolContext, args: dict[str, Any]) -> str:
    state = ctx.state
    parts: list[str] = []
    parts.append(f"Question: {state.question}")
    if state.route:
        parts.append(f"Route: {state.route}")
    if state.time_window:
        parts.append(f"Time window: {state.time_window}")
    if state.row_count:
        parts.append(f"Row count: {state.row_count}")
    if state.rows:
        sample = state.rows[: int(args.get("sample_rows") or 25)]
        parts.append(f"Rows (first {len(sample)}): {sample}")
    if state.insights:
        parts.append("Insights:\n" + "\n".join(state.insights))
    if state.causal_tree:
        parts.append(f"Causal tree: {state.causal_tree}")
    parts.append("\nWrite the final answer now.")
    return "\n".join(parts)


class InsightFmtAgent(Tool):
    name = "insightFmt"
    description = (
        "Compose the final user-facing answer from the rows + insights "
        "already in state. Always call this last before declaring done."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "sample_rows": {
                "type": "integer",
                "default": 25,
                "minimum": 1,
                "maximum": 100,
                "description": "How many rows to feed into the prompt.",
            },
        },
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        llm: LLMClient | None = ctx.llm
        if llm is None:
            return ToolOutcome(ok=False, error="insightFmt requires an LLM client.")
        user = _build_user(ctx, args)
        resp = await llm.complete(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=400,
        )
        if resp.error:
            return ToolOutcome(ok=False, error=resp.error)
        text = (resp.content or "").strip()
        if not text:
            return ToolOutcome(ok=False, error="insightFmt returned empty text")
        return ToolOutcome(
            ok=True,
            output={"answer": text},
            state_updates={"final_answer": text},
        )


__all__ = ["InsightFmtAgent"]
