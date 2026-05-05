"""ResponseFormatter — single clean output format for every analytics turn.

Strict rules (per spec):
  • NO workflow diagram in the user-facing answer.
  • NO "FINAL ANSWER" / "CONCLUSION" labels.
  • NO multi-line "Orders / Customers" breakdown.
  • Body is a 2–3 sentence narrative; the chart is the primary visual and
    rides the SSE `final.chart` payload (state.chart_data).
  • Empty-data cases produce a single, plain message — never "₹0.00".
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel

from app.state import TurnState
from app.tools.base import Tool, ToolResult, require


def _empty_message(empty_reason: dict) -> str:
    reason = empty_reason.get("reason")
    if reason == "table_empty":
        return "No sales data available. Please upload data."
    if reason == "filter_excluded_all":
        lo = empty_reason.get("available_min_date") or "?"
        hi = empty_reason.get("available_max_date") or "?"
        return f"No sales found in this time range. Available data is from {lo} to {hi}."
    return "No matching data found for your query."


def _trend_sentence(series: list[dict]) -> str | None:
    """Compute a one-sentence trend from the bucketed series. Returns None
    when the series is too short to support a claim."""
    if not series or len(series) < 2:
        return None
    first = float(series[0].get("sales") or 0)
    last = float(series[-1].get("sales") or 0)
    if first <= 0:
        return None
    delta_pct = (last - first) / first * 100
    if abs(delta_pct) < 5:
        return "Overall performance remained stable across the period."
    if delta_pct > 0:
        return f"Sales show an upward trend of {abs(delta_pct):.1f}% across the period."
    return f"Sales show a downward trend of {abs(delta_pct):.1f}% across the period."


def _build_answer(aggregates: dict) -> str:
    """Single funnel — 2–3 sentence narrative for every analytics turn."""
    empty_reason = aggregates.get("empty_reason")
    if empty_reason:
        return _empty_message(empty_reason)

    totals = aggregates.get("totals") or {}
    sales = float(totals.get("total_sales") or 0)
    orders = int(totals.get("orders") or 0)
    if not sales and not orders:
        return "No sales recorded yet."

    parts: list[str] = []
    if orders:
        parts.append(f"Total sales are ₹{sales:,.2f} across {orders:,} orders.")
    else:
        parts.append(f"Total sales are ₹{sales:,.2f}.")

    trend = _trend_sentence(aggregates.get("series") or [])
    if trend:
        parts.append(trend)

    return " ".join(parts)


class ResponseFormatterArgs(BaseModel):
    pass


class ResponseFormatterTool(Tool):
    name = "ResponseFormatter"
    description = (
        "Builds the clean 2–3 sentence answer + the persisted record. "
        "Workflow diagram and section labels are intentionally suppressed; "
        "the chart payload (state.chart_data) is the primary visual output."
    )
    args_model = ResponseFormatterArgs
    independent = False

    async def run(self, state: TurnState, args: ResponseFormatterArgs) -> ToolResult:
        miss = require(state, "aggregates", "insights")
        if miss:
            return miss

        aggregates = state.aggregates or {}
        insights = state.insights or {}
        body = _build_answer(aggregates)

        record = {
            "turn_id":         state.turn_id,
            "cache_key":       state.cache_key,
            "stored_at":       datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "query":           state.question,
            "sub_agent":       state.sub_agent,
            "route":           state.route,
            "sql":             state.sql_final,
            "rows":            state.rows,
            "aggregates":      aggregates,
            "insights":        insights,
            "chart":           aggregates,
            "final_answer":    body,
            "response_format": "clean",
        }
        return ToolResult(
            ok=True,
            output={"answer_preview": body[:300]},
            state_updates={
                "final_answer":   body,
                "response_record": record,
                "chart_data":     aggregates,
            },
        )
