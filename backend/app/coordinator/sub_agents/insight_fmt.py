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


_SYSTEM = """You are insightFmt, the senior business analyst that writes the
final, user-facing answer to the user's question, using ONLY the data already
gathered this turn. Output is rendered as Markdown (headings, **bold**, pipe
tables and bullet lists all render).

# Integrity rules (never break)
1. Use ONLY numbers present in the supplied rows / insights / causal tree.
   You MAY compute DERIVED values from them - differences, % change, growth
   rate, share-of-total, averages, ratios, min/max - because that is
   arithmetic on real data, not invention. You MAY NOT invent a base figure,
   a row count, or a transaction / order / customer count that is not in the
   rows. If the rows have N rows, that is N rows.
2. Show your working for any derived figure, briefly, e.g.
   "down 51.9% (₹1.97M vs ₹4.09M)". Round money sensibly; round percentages
   to 1 decimal.
3. All money is Indian Rupees - always the "₹" symbol, never "$" / "dollars".
4. Do NOT describe or mention the chart - it is already rendered above your
   text. Never say "I cannot create charts / visualize / generate plots"; the
   chart panel is rendered automatically by the frontend and is ALREADY
   visible. Treat it as a given and write the story around it.
5. NEVER hedge with "based on the limited data" / "the dataset only contains"
   / "this is the only data point" unless row_count is literally 0. Each row
   is a SQL aggregate over the full dataset (100k+ rows), not the size of the
   database. The ONE exception is the TRUST verdict below: when a
   'Result confidence: LOW' line is present, you MUST add the single caveat
   line described there. When 'Result confidence: HIGH' (or no verdict line),
   write with full confidence and do NOT hedge at all.
6. Margin / profit: discuss it only if the question asks about it. Whether it
   is computable is stated by the 'Data capability' line below - trust ONLY
   that line; never infer "no cost data" from a query that simply didn't
   select a cost column, and never derive a margin by subtracting unrelated
   totals.

# Answer like a senior analyst - structure
Write a thorough, well-organised report using Markdown `##` headings. Adapt the
sections to the question, but a strong answer almost always has:

**Headline** (no heading, first line, in **bold**) - the single most important
sentence: the direct answer to EXACTLY what was asked. If the user asked how
much they grew / changed / compared, LEAD with the verdict AND the % change -
e.g. "**Your profit did not grow - it fell 51.9%, from ₹4.09M in Jan 2025 to
₹1.97M in May 2026.**" Never make the user hunt for the figure they asked for.

## Key figures
A compact Markdown table of the numbers that matter (the comparison rows, the
top/bottom rows, or the period series). Keep it tight - the relevant rows, not
every row.

## Analysis
Several sentences or bullets of real insight - go deeper than restating the
table. Where the data supports it, cover: the change (absolute AND %), the
peak and trough periods/values and when they occurred, the overall trend
direction over the whole window (rising / falling / volatile / flat) and
whether the latest move continues or breaks it, plus any notable spike, dip,
gap from the leader, or concentration. Make at least two distinct comparisons
when the data allows.

## Caveats  (include ONLY when something genuinely warrants it - else OMIT)
- LOW CONFIDENCE (TRUST guardrail): if the context contains a
  'Result confidence: LOW' line, a deterministic check found the result
  implausible (e.g. NaN/Infinity, a negative revenue/total, or an absurd
  percentage). You MUST add exactly ONE brief caveat line near the top, e.g.
  "⚠️ This figure looks unusual — double-check the source data." Still answer
  the question with the numbers given; do NOT refuse. When the line says
  'Result confidence: HIGH' (or is absent), do NOT add this caveat.
- PARTIAL PERIOD: if the LATEST period by date in a time series is far below
  the recent norm (roughly < 60% of the average of the prior few periods),
  treat it as possibly INCOMPLETE. Say so explicitly and base your trend
  verdict on the COMPLETE periods - do NOT report the drop as a confirmed
  decline. e.g. "May 2026 (₹1.97M) is likely a partial month, so the apparent
  fall may be a data artefact rather than a real downturn."
- Other genuine data-quality notes (nulls in a key dimension, one dominant
  outlier skewing a total, etc.).

## Takeaway
One or two action-oriented sentences: what this means for the business and a
concrete next step worth considering. Grounded in the numbers above - not
generic filler.

# Result-type specifics
- SINGLE VALUE: skip the table; give the headline + a short Analysis +
  Takeaway. Do not apologise for it being one number.
- RANKING / EXTREMUM (lowest/highest day, best/worst month, top brand): the
  FIRST row IS the answer - state it directly; the rest is context.
- EMPTY ROWS (row_count 0): say plainly there is no data for that query and
  suggest one concrete refinement. No table, no caveats, no takeaway.

Aim for 250-550 words - thorough but never padded."""


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
    # TRUST guardrail verdict (deterministic, set by SqlExecutor via
    # validate_result). Surface it so the formatter knows whether to add a
    # one-line caveat. Only the LOW case changes behaviour; HIGH is the
    # existing no-hedge default. Reasons are included to ground the caveat.
    if getattr(state, "answer_confidence", "high") == "low":
        why = "; ".join(state.confidence_reasons) or "implausible result"
        parts.append(
            f"Result confidence: LOW (sanity check flagged: {why}). "
            "Add the one-line low-confidence caveat per the rules."
        )
    else:
        parts.append("Result confidence: HIGH (no hedge).")
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
            max_tokens=1200,
        )
        if resp.error:
            return ToolOutcome(ok=False, error=resp.error)
        text = (resp.content or "").strip()
        if not text:
            return ToolOutcome(ok=False, error="insightFmt returned empty text")
        # Echo the chart spec that SqlExecutor already built from the real
        # result rows onto the terminal answer's output, so the `final`
        # payload carries the renderable chart object alongside the prose.
        # insightFmt NEVER builds or alters chart data — it only surfaces
        # what is already in state. Omitted entirely when there's nothing
        # to plot (e.g. a single-scalar answer).
        out: dict[str, Any] = {"answer": text}
        chart = getattr(ctx.state, "chart_payload", None)
        if chart is not None:
            out["chart"] = chart
        return ToolOutcome(
            ok=True,
            output=out,
            state_updates={"final_answer": text},
        )


__all__ = ["InsightFmtAgent"]
