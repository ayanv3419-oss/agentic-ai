"""IntentAnalyzer — extracts {metric, filters, comparison?, overview?} into state.intent.

Deterministic regex-based v1 (no LLM call). The structure is consumed
downstream by SqlPlanner and by ResponseFormatter (for the overview-mode
short-format branch).
"""
from __future__ import annotations

import re

from pydantic import BaseModel

from app.state import TurnState
from app.tools.base import Tool, ToolResult


_METRIC_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:gmv|gross merchandise|sales|revenue|turnover|earned|earnings)\b", re.I), "total_amount"),
    (re.compile(r"\b(?:order count|orders|transactions|number of (?:sales|orders))\b", re.I), "orders"),
    (re.compile(r"\b(?:customers?|buyers?|users?|distinct (?:parties|customers))\b", re.I), "customers"),
    (re.compile(r"\b(?:aov|average order value)\b", re.I), "aov"),
    (re.compile(r"\b(?:refunds?|returns?|loyalty redeemed)\b", re.I), "refunds"),
]

_COMPARISON_RE = re.compile(
    r"\b(compare|vs\.?|versus|change|movement|growth|delta)\b", re.I
)
_TOP_RE = re.compile(r"\btop\s+(\d+)\b", re.I)


# --- Sales-overview detection ----------------------------------------------
# Triggered for short, plain "what is my sales / show my sales / total sales"
# style questions. Demoters (drill-down, comparison, time-period, ranking,
# month names) immediately disqualify the question — those go through the
# normal verbose response format.
_OVERVIEW_KEYWORDS: tuple[str, ...] = (
    "sales", "revenue", "income", "earnings", "turnover", "gmv",
)
_OVERVIEW_DEMOTERS: tuple[str, ...] = (
    # drill-down / period markers
    "by ", " in ", " from ", "between", "during", "for ", "per ",
    "last ", "this ", "yesterday", "today", "tomorrow", " ago",
    "month", "week", "day ", "year", "quarter", "hour",
    "daily", "weekly", "monthly", "yearly", "quarterly",
    "ytd", "year to date",
    # comparisons / analytical
    "vs ", "vs.", "versus", "compare", "compared",
    "growth", "trend", "drop", "decline", "fall", "fell", "rose",
    "why", "what caused", "rca", "root cause",
    "forecast", "predict", "projection",
    # ranking / drill-downs
    "top ", "bottom ", "best ", "worst ", "highest", "lowest",
    "by customer", "by product", "by party",
    # explicit month names → period query, not overview
    "jan ", "january", "feb ", "february", "mar ", "march",
    "apr ", "april", "may ", "jun ", "june",
    "jul ", "july", "aug ", "august", "sep ", "september",
    "oct ", "october", "nov ", "november", "dec ", "december",
)
_MAX_OVERVIEW_TOKENS = 6


def _is_sales_overview(question: str) -> bool:
    q = (question or "").lower().strip()
    if not q:
        return False
    if not any(k in q for k in _OVERVIEW_KEYWORDS):
        return False
    if len(q.split()) > _MAX_OVERVIEW_TOKENS:
        return False
    # Pad with leading/trailing spaces so substring checks like " in " work
    # consistently for terms that appear at the very start of the question.
    padded = f" {q} "
    for d in _OVERVIEW_DEMOTERS:
        if d in padded:
            return False
    return True


class IntentAnalyzerArgs(BaseModel):
    pass


class IntentAnalyzerTool(Tool):
    name = "IntentAnalyzer"
    description = (
        "Extracts metric / filters / comparison / overview flags from the "
        "question. Sets state.intent."
    )
    args_model = IntentAnalyzerArgs
    independent = True

    async def run(self, state: TurnState, args: IntentAnalyzerArgs) -> ToolResult:
        q = (state.question or "").strip()
        if not q:
            return ToolResult(ok=False, error="empty question")

        metric = "total_amount"
        for pat, name in _METRIC_MAP:
            if pat.search(q):
                metric = name
                break

        comparison = bool(_COMPARISON_RE.search(q))
        top = None
        m = _TOP_RE.search(q)
        if m:
            try:
                top = int(m.group(1))
            except ValueError:
                top = None
        overview = _is_sales_overview(q)

        intent = {
            "metric":     metric,
            "comparison": comparison,
            "top_n":      top,
            "overview":   overview,
            "raw":        q,
        }
        return ToolResult(ok=True, output=intent, state_updates={"intent": intent})
