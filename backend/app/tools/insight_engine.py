"""InsightEngine — narrates the result.

Two modes:
  * `mode="rule"` (default for QueryAgent) — deterministic descriptive text
    (totals + trend direction).
  * `mode="llm"` (used by AnalyticsAgent / RCAAgent / ForecastAgent) — calls
    Groq for a richer narrative grounded in `state.aggregates`. Falls back
    to rule-based text if the LLM is unavailable so the pipeline never
    blocks on the LLM.

Sets `state.insights = {summary, trend, narrative}`.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.llm import GroqMessage, get_groq
from app.state import TurnState
from app.tools.base import Tool, ToolResult, require

log = logging.getLogger("agentic_ai.insight_engine")


_LLM_SYSTEM = """You are a senior business analyst. Read the provided
aggregates JSON and write a single paragraph (3-5 sentences) of plain-English
insight grounded ONLY in the numbers given. Do NOT invent values.
Output STRICT JSON: {"narrative": "<paragraph>"}.
"""


class InsightEngineArgs(BaseModel):
    mode: str = Field(default="rule")  # "rule" | "llm"


def _trend(series: list[dict]) -> tuple[str, float]:
    if not series or len(series) < 2:
        return "flat", 0.0
    first = series[0].get("sales") or 0.0
    last = series[-1].get("sales") or 0.0
    if first <= 0:
        return ("up" if last > 0 else "flat"), 0.0
    delta_pct = (last - first) / first * 100
    direction = "up" if delta_pct >= 0 else "down"
    return direction, delta_pct


def _rule_summary(aggregates: dict) -> dict:
    totals = aggregates.get("totals") or {}
    series = aggregates.get("series") or []
    sales = float(totals.get("total_sales") or 0)
    orders = int(totals.get("orders") or 0)
    customers = int(totals.get("customers") or 0)
    direction, pct = _trend(series)
    parts = [f"Total sales ₹{sales:,.2f} across {orders:,} orders."]
    if customers:
        parts.append(f"From {customers:,} unique customers.")
    if series and len(series) >= 2:
        parts.append(
            f"Trend across {len(series)} {aggregates.get('granularity','daily')} "
            f"buckets: {direction} {abs(pct):.1f}%."
        )
    narrative = " ".join(parts)
    return {"summary": narrative, "trend": direction, "delta_pct": round(pct, 2),
            "narrative": narrative}


class InsightEngineTool(Tool):
    name = "InsightEngine"
    description = (
        "Builds a textual insight from state.aggregates. Rule-based by default; "
        "LLM-enhanced narrative when mode='llm'."
    )
    args_model = InsightEngineArgs
    independent = False

    async def run(self, state: TurnState, args: InsightEngineArgs) -> ToolResult:
        miss = require(state, "aggregates")
        if miss:
            return miss
        aggregates = state.aggregates or {}
        rule = _rule_summary(aggregates)

        if args.mode != "llm":
            return ToolResult(ok=True, output=rule, state_updates={"insights": rule})

        # LLM mode — wrap aggregates as JSON, ask for a paragraph.
        try:
            groq = get_groq()
            user_payload = {
                "question": state.question,
                "aggregates": aggregates,
                "rule_based_summary": rule["summary"],
            }
            import json as _json
            resp = await groq.complete(
                [
                    GroqMessage(role="system", content=_LLM_SYSTEM),
                    GroqMessage(role="user", content=_json.dumps(user_payload, default=str)),
                ],
                temperature=0.2,
                max_tokens=400,
                force_json=True,
            )
            if resp.error or not resp.content:
                # Soft-fall back to rule-based.
                return ToolResult(
                    ok=True,
                    output={**rule, "llm_error": resp.error},
                    state_updates={"insights": rule},
                    delta_metrics={"tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out},
                )
            try:
                parsed = _json.loads(resp.content)
                narrative = str(parsed.get("narrative") or rule["narrative"])
            except Exception:
                narrative = rule["narrative"]
            insights = {**rule, "narrative": narrative}
            return ToolResult(
                ok=True,
                output=insights,
                state_updates={"insights": insights},
                delta_metrics={"tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out},
            )
        except Exception as e:
            log.warning("InsightEngine LLM call failed: %s", e, exc_info=True)
            return ToolResult(ok=True, output=rule, state_updates={"insights": rule})
