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
using the data already gathered in this turn. Output is rendered as
Markdown.

Rules:
1. Use ONLY numbers that appear in the supplied rows/insights. NEVER
   fabricate figures. NEVER invent a transaction count, a row count, a
   customer count, an order count, or any other metric that is not in
   the rows. If the rows have N rows, that is N rows — do not call it
   "501 transactions" or any other made-up number.
2. Lead with the direct answer in one short sentence.
3. TREND results (rows grouped by month / week / day / year):
   - Show a compact Markdown table: Month | Revenue columns.
   - Follow with 3-4 key observations as a bullet list:
     highest period, lowest period, any notable spike or dip,
     overall trend direction (rising / falling / stable).
4. RANKING results (rows grouped by brand / product / category) AND
   EXTREMUM results (lowest/highest day, best/worst month):
   - Show a compact Markdown table of the top/bottom rows.
   - Follow with 1-2 sentences on the leader (or the extremum the user
     asked about) and any notable gap from the next row.
   - For "lowest X" questions, the FIRST row of the rows IS the answer —
     state it directly. The other rows are context, not "additional
     data points".
5. SINGLE-VALUE results: concise prose — two short paragraphs, no table.
   Do NOT add disclaimers like "this is the only data point available"
   or "the dataset contains just this value". A single-row result is a
   SQL aggregate, not a count of facts in the database.
6. If the rows are empty, say so plainly and suggest a refinement.
7. Keep total length under 300 words.
8. Margin / profit: discuss margin or profit ONLY if the user's question
   explicitly asks about them. Whether they can be computed is stated by
   the 'Data capability' line below - trust ONLY that line. NEVER infer
   'no cost data' from a query result that simply did not select a cost
   column, and never derive a margin by subtracting unrelated totals.
9. All monetary amounts are Indian Rupees - write the "₹" symbol, never
   "$" or the word "dollars".
10. Do NOT describe the chart — the user can already see it above the
    text. Write the numbers and the story, not "the chart shows...".
11. NEVER say "I cannot generate visual plots", "I cannot create
    charts", "I'm unable to visualize", or anything similar. The
    chart panel above your answer is rendered automatically by the
    frontend from the SQL rows — it is ALREADY visible to the user.
    Treat the chart as a given and write the story around it.
12. NEVER hedge with "based on the limited data" or "the dataset only
    contains" unless the row_count is literally 0. The rows you see
    are a SQL aggregate over the full dataset (which has 100k+ rows);
    do not confuse the result size with the dataset size."""


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
    # Cost/margin availability comes from the resolved schema of the
    # uploaded dataset - NOT from whether this query happened to select a
    # cost column. This is what stops the false "no cost data" claim.
    rs = state.resolved_schema
    if rs is not None:
        if rs.can_compute_margin:
            parts.append(
                "Data capability: the uploaded dataset HAS cost data - "
                "margin and profit ARE computable for it."
            )
        else:
            parts.append(
                "Data capability: the uploaded dataset has no usable cost "
                "column - margin and profit cannot be computed."
            )
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
            max_tokens=800,
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
